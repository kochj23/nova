#!/usr/bin/env python3
"""
nova_config_drift.py — config-as-code + drift detection (#645).

The source-of-truth for the fleet's launchd config is versioned in PostgreSQL
(nova_ops.config_snapshots), per Nova's "all state in PG" rule — NOT the public
git repo, whose secret-scanner (correctly) blocks the plists' absolute /Users
paths and personal config. PG is local, snapshots are PII-redacted.

  nova_config_drift.py --bless    snapshot the CURRENT nova launchd plists as the
                                  accepted baseline (run after a reviewed change)
  nova_config_drift.py            compare live plists vs the baseline; alert via
                                  nova_notify on drift:
                                    MODIFIED  — live plist differs from baseline
                                    UNTRACKED — running plist not in baseline
                                    MISSING   — baseline plist gone from disk
                                    ORPHAN    — loaded job with no plist file

Catches the "20 stale scheduler tasks for a week" class of silent divergence.
Pairs with nova_self_audit (scheduler<->disk).
"""
import hashlib
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_notify import notify

LIVE_DIR = Path.home() / "Library/LaunchAgents"
PREFIXES = ("net.digitalnoise.", "com.nova.", "com.kochj.", "com.digitalnoise.")
PSQL = ["psql", "-h", "localhost", "-U", "kochj", "-d", "nova_ops", "-tAc"]

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TOKEN = re.compile(r"xox[baprs]-[A-Za-z0-9-]{8,}")


def _redact(s):
    return _TOKEN.sub("__TOKEN__", _EMAIL.sub("__EMAIL__", s))


def _live():
    return {p.name: _redact(p.read_text(errors="replace"))
            for p in LIVE_DIR.glob("*.plist") if p.name.startswith(PREFIXES)}


def _sql(q):
    return subprocess.run(PSQL + [q], capture_output=True, text=True, timeout=20)


def _ensure():
    _sql("CREATE TABLE IF NOT EXISTS config_snapshots ("
         "label text PRIMARY KEY, content_hash text NOT NULL, content text, "
         "blessed_at timestamptz DEFAULT now(), source_host text)")


def _loaded():
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return set()
    return {p[2] for p in (ln.split("\t") for ln in out.splitlines()[1:])
            if len(p) >= 3 and p[2].startswith(PREFIXES)}


def bless():
    _ensure()
    plists = _live()
    _sql("DELETE FROM config_snapshots WHERE label LIKE 'launchd:%'")
    for name, content in plists.items():
        h = hashlib.sha256(content.encode()).hexdigest()
        e = lambda v: v.replace("'", "''")
        _sql(f"INSERT INTO config_snapshots (label, content_hash, content, source_host) "
             f"VALUES ('launchd:{e(name)}','{h}','{e(content)}','mac-studio') "
             f"ON CONFLICT (label) DO UPDATE SET content_hash=EXCLUDED.content_hash, "
             f"content=EXCLUDED.content, blessed_at=now()")
    print(f"[config-drift] blessed {len(plists)} launchd plists as the baseline.")


def check():
    _ensure()
    live = _live()
    r = _sql("SELECT label, content_hash FROM config_snapshots WHERE label LIKE 'launchd:%'")
    base = {}
    for ln in r.stdout.strip().splitlines():
        if "|" in ln:
            lbl, h = ln.split("|", 1)
            base[lbl.replace("launchd:", "", 1)] = h
    if not base:
        print("[config-drift] no baseline — run `nova_config_drift.py --bless` first.")
        return 0
    live_h = {n: hashlib.sha256(c.encode()).hexdigest() for n, c in live.items()}
    loaded = _loaded()

    modified  = sorted(n for n in (set(live) & set(base)) if live_h[n] != base[n])
    untracked = sorted(set(live) - set(base))
    missing   = sorted(set(base) - set(live))
    orphans   = sorted(lbl for lbl in loaded if f"{lbl}.plist" not in live)

    drift = []
    if modified:  drift.append(f":pencil2: MODIFIED (live differs from baseline): {', '.join(modified)}")
    if untracked: drift.append(f":new: UNTRACKED (running, not in baseline): {', '.join(untracked)}")
    if missing:   drift.append(f":x: MISSING (baseline plist gone from disk): {', '.join(missing)}")
    if orphans:   drift.append(f":ghost: ORPHAN (loaded job, no plist): {', '.join(orphans)}")

    print(f"[config-drift] live={len(live)} baseline={len(base)} loaded={len(loaded)} | "
          f"modified={len(modified)} untracked={len(untracked)} missing={len(missing)} orphan={len(orphans)}")
    for d in drift:
        print(f"  {d}")
    if drift:
        n = len(modified) + len(untracked) + len(missing) + len(orphans)
        notify(f"Config drift — {n} launchd discrepancy(ies)",
               body="\n".join(drift) + "\n\nReconcile, then bless: nova_config_drift.py --bless",
               level="warning", category="config", dedup_key="config-drift")
        return 1
    print("[config-drift] no drift — running config matches the blessed baseline.")
    return 0


def main():
    if "--bless" in sys.argv or "--init" in sys.argv:
        bless()
        sys.exit(0)
    sys.exit(check())


if __name__ == "__main__":
    main()
