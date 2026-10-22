#!/usr/bin/env python3
"""nova_python_guard.py — keep /opt/homebrew/bin/python3 pointing at the interpreter that
actually has Nova's site-packages, and repair it if Homebrew moves it.

WHY THIS EXISTS (incident 2026-10-22 03:14 -> 10:26, ~7h)
---------------------------------------------------------
agent_docs runbook-mac-autoupdate pins `python@3.14` on the Studio because "103 LaunchAgents
hardcode /opt/homebrew/bin/python3". A PIN STOPS UPGRADES OF A FORMULA; IT DOES NOT STOP A NEW
FORMULA FROM STEALING THE UNVERSIONED SYMLINK. At 03:14 a brew run upgraded awscli / nmap / pipx /
summarize / httpie, which had moved upstream onto `python@3.15`. brew installed python@3.15
(INSTALL_RECEIPT installed_on_request=false) and linking it repointed
/opt/homebrew/bin/python3 3.14 -> 3.15. 3.15's site-packages was essentially empty -- no psycopg2,
yaml, numpy, requests, psutil, torch, mlx -- so every consumer of that path lost its modules.

Observed blast radius: ~17 scheduled tasks failed repeatedly on `ModuleNotFoundError: No module
named 'psycopg2'` (satire_articles, satire_pool, ops_lessons, hard_stretch, tracker_watch,
yt_subs_baseline, journal_publish_check, daily_threat_assessment, local_burbank, article_watchdog,
care_checkin_harvest, live_docs, op_sync, yellow_eye, journal_stranded_fleet ...). The running
nova_scheduler survived only because it had already resolved 3.14 at launch; its LaunchAgent
(com.nova.scheduler) names /opt/homebrew/bin/python3, and 3.15 has no `yaml`, so the NEXT restart
or reboot would have left the whole scheduler dead. That latent outage was the real danger.

Fix applied: `brew unlink python@3.15 && brew unlink python@3.14 && brew link --overwrite
python@3.14`. NOTE the middle step -- unlinking 3.15 alone deletes /opt/homebrew/bin/python3
outright (python@3.14 reports "Already linked" and will not recreate the unversioned symlink
without a relink), which is briefly WORSE than the wrong version. This script therefore always
relinks rather than merely unlinking.

WHY A PERIODIC CHECK AND NOT A HOOK IN brew_autoupdate.sh
---------------------------------------------------------
The 03:14 run was NOT brew-autoupdate (that last ran 2026-10-11 and is Sundays 04:30), so the
breakage can arrive from any ad-hoc brew invocation by a person or an agent. A guard that only ran
inside the sanctioned updater would have missed this exact incident.

The real long-term fix is to stop depending on the floating `python3` at all, but that means
editing ~103 LaunchAgents plus scheduler.yaml; until someone does that, this guard holds the line.

Usage: nova_python_guard.py [--repair] [--quiet]
         default  : check only, exit 1 if wrong (safe to run anywhere)
         --repair : relink the wanted formula, re-verify, and notify
Check:   nova_python_guard.py --selftest    (pure logic, touches nothing)
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, "/Volumes/nas/nova-fs/scripts")

PY3 = "/opt/homebrew/bin/python3"
WANT_FORMULA = "python@3.14"
WANT_PREFIX = "3.14"
BREW = "/opt/homebrew/bin/brew"

# Modules that must import under PY3 or Nova is broken. psycopg2 and yaml are the two that take the
# fleet down (PG access, and the scheduler's own config parse); the rest are the widest consumers.
REQUIRED = ("psycopg2", "yaml", "numpy", "requests", "psutil")


def version_of(py: str = PY3) -> str | None:
    """"3.14.6" for the interpreter at `py`, or None if it is missing/not runnable."""
    try:
        r = subprocess.run([py, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip() or None
    except Exception:
        return None


def missing_modules(py: str = PY3) -> list[str]:
    """Which REQUIRED modules do not import under `py`. One subprocess, not one per module."""
    probe = ("import importlib,sys;"
             "print(' '.join(m for m in sys.argv[1:] if importlib.util.find_spec(m) is None))")
    try:
        r = subprocess.run([py, "-c", probe, *REQUIRED], capture_output=True, text=True, timeout=60)
        return r.stdout.split()
    except Exception:
        return list(REQUIRED)


def verdict(version: str | None, missing: list[str]) -> tuple[bool, str]:
    """(healthy, one-line reason). Pure, so --selftest can exercise every branch."""
    if version is None:
        return False, f"{PY3} is MISSING or not runnable (brew unlink leaves no unversioned python3)"
    if not version.startswith(WANT_PREFIX + "."):
        return False, f"{PY3} is {version}, want {WANT_PREFIX}.x ({WANT_FORMULA} holds the site-packages)"
    if missing:
        return False, f"{PY3} is {version} but cannot import: {', '.join(missing)}"
    return True, f"{PY3} is {version}, all required modules import"


def repair() -> list[str]:
    """Relink WANT_FORMULA so the unversioned python3 comes back on the right interpreter.

    Always unlink-then-link: `brew link` on an already-linked keg is a no-op that will NOT recreate
    a python3 symlink some other formula's unlink removed.
    """
    env = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1"}
    steps = []
    for cmd in (["unlink", WANT_FORMULA], ["link", "--overwrite", WANT_FORMULA]):
        try:
            r = subprocess.run([BREW] + cmd, capture_output=True, text=True, timeout=300, env=env)
            steps.append(f"brew {' '.join(cmd)}: rc={r.returncode} {(r.stdout or r.stderr).strip().splitlines()[-1][:90] if (r.stdout or r.stderr).strip() else ''}")
        except Exception as e:
            steps.append(f"brew {' '.join(cmd)}: FAILED {type(e).__name__}: {e}")
    return steps


def main(argv: list[str]) -> int:
    flags = {a for a in argv[1:] if a.startswith("--")}

    if "--selftest" in flags:
        assert verdict("3.14.6", [])[0] is True
        assert verdict("3.15.0", [])[0] is False               # right modules, wrong interpreter
        assert verdict(None, [])[0] is False                   # symlink deleted entirely
        assert verdict("3.14.6", ["psycopg2"])[0] is False      # right version, broken site-packages
        assert "3.15" in verdict("3.15.0", [])[1]
        assert "MISSING" in verdict(None, [])[1]
        assert "psycopg2" in verdict("3.14.6", ["psycopg2"])[1]
        # a 3.14 prefix match must not accept 3.140 or 3.1
        assert verdict("3.140.0", [])[0] is False
        assert verdict("3.1.0", [])[0] is False
        print("selftest OK")
        return 0

    quiet = "--quiet" in flags
    healthy, reason = verdict(version_of(), missing_modules())
    if healthy:
        if not quiet:
            print(f"OK: {reason}")
        return 0

    print(f"BROKEN: {reason}", file=sys.stderr)
    if "--repair" not in flags:
        return 1

    for line in repair():
        print(line, file=sys.stderr)
    healthy2, reason2 = verdict(version_of(), missing_modules())
    print(("REPAIRED: " if healthy2 else "STILL BROKEN: ") + reason2, file=sys.stderr)

    try:
        import nova_notify
        nova_notify.notify(
            ("python3 interpreter repaired" if healthy2 else "python3 interpreter BROKEN"),
            body=f"was: {reason}\nnow: {reason2}\nSee agent_docs runbook-mac-autoupdate "
                 f"(a pin on {WANT_FORMULA} does not stop a newer python@ formula taking "
                 f"the unversioned python3 symlink).",
            level=("warning" if healthy2 else "critical"),
            category="fleet", source="nova_python_guard.py",
            dedup_key="python-interpreter-guard",
        )
    except Exception as e:
        print(f"(notify failed: {type(e).__name__}: {e})", file=sys.stderr)
    return 0 if healthy2 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
