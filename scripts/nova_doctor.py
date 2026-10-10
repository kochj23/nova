#!/usr/bin/env python3
"""
nova_doctor.py — Post-boot health self-check for the Nova stack.

MERGED 2026-10-09 (organ audit M4): this now runs as `nova_selfcheck.py --boot`. main() is a thin
wrapper that delegates there; the check functions below (CHECKS, heal_home_assistant,
wait_for_boot, format_report) stay here and are what selfcheck --boot calls. The report now goes
to #nova-alerts on FAIL, else #nova-digest (was #nova-bb), and every check lands in selfcheck_runs
as boot-*.

Runs the checks that, when they silently fail at boot, take Nova down:
volumes, Postgres, gateway, MLX, the face-recognition import, WAL archiving,
and disk capacity. Posts ONE consolidated report to Slack (#nova-bb) so a bad
boot is visible at a glance instead of being discovered piecemeal days later.

Modes (both delegate to nova_selfcheck.py):
  --boot   Wait for /Volumes/Data + /Volumes/MoreData to mount, give services a
           grace period to come up, then run.   -> nova_selfcheck.py --boot
  (none)   Run immediately and report.          -> nova_selfcheck.py --boot --now
  --dry-run is passed through (no HA restart, no Slack, no PG writes).

Exit code: 0 if all checks pass, 1 if any FAIL (WARN does not fail the run).
Written by Jordan Koch.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_API = nova_config.SLACK_API
REPORT_CHANNEL = nova_config.SLACK_BB  # #nova-bb — monitoring/alerts

import nova_dsn as _nova_dsn  # noqa: E402
PG_DSN = _nova_dsn.pg_dsn("nova_ops")
PKG_PATH = "/Volumes/Data/AI/python_packages"
GATEWAY_HEALTH = "http://192.168.1.2:18792/health"
MLX_MODELS = "http://127.0.0.1:5050/v1/models"
# The pool behind the :5050 nginx LB — probed individually only when the LB blips,
# so a one-minute transient 502 to one backend (network hiccup) doesn't page when the
# pool is still serving. Keep in sync with nginx/servers/mlx-lb.conf.
MLX_BACKENDS = [
    "http://192.168.1.251:5050/v1/models",  # M4 Pro, 64GB
    "http://192.168.1.7:5050/v1/models",    # M2 Pro, 32GB
]
OLLAMA_TAGS = "http://127.0.0.1:11434/api/tags"

OK, WARN, FAIL = "ok", "warn", "fail"
ICON = {OK: ":white_check_mark:", WARN: ":warning:", FAIL: ":rotating_light:"}


def http_json(url, timeout=6):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


# ── Individual checks ─────────────────────────────────────────────────────────
# Each returns (status, detail).

def check_volumes():
    """Present AND working: right server, not read-only, writable (shared with nova_selfcheck)."""
    from nova_selfcheck import MOUNTS, mount_problem   # ponytail: one definition of 'working', two callers
    bad = {m: r for m, want in MOUNTS.items() if (r := mount_problem(m, want))}
    if bad:
        return FAIL, "; ".join(f"{m}: {r}" for m, r in bad.items())
    return OK, "Data + MoreData + nas + external mounted, right server, writable"


def check_postgres():
    try:
        import psycopg2
        with psycopg2.connect(PG_DSN, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM claude_memories;")
                mem = cur.fetchone()[0]
        return OK, f"nova_ops accepting ({mem} memories)"
    except Exception as e:
        return FAIL, f"unreachable: {e}"


def check_wal_archiver():
    try:
        import psycopg2
        with psycopg2.connect(PG_DSN, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT archived_count, failed_count, last_failed_time "
                            "FROM pg_stat_archiver;")
                archived, failed, last_failed = cur.fetchone()
        if failed and failed > 0:
            return WARN, f"{failed} failed archives (last {last_failed}); {archived} ok"
        return OK, f"{archived} archived, 0 failed"
    except Exception as e:
        return WARN, f"could not read pg_stat_archiver: {e}"


def check_gateway():
    try:
        d = http_json(GATEWAY_HEALTH)
        if not d.get("ok"):
            return FAIL, f"health ok=false (v{d.get('version','?')})"
        if d.get("degraded"):
            return WARN, f"degraded=true (v{d.get('version','?')})"
        return OK, f"v{d.get('version','?')}, uptime {int(d.get('uptime_s',0))}s"
    except Exception as e:
        return FAIL, f"no /health response: {e}"


def check_mlx():
    # Retry the LB a few times before deciding anything: a single transient 502 (a
    # ~1-minute network blip to one backend) shouldn't page — the pool is redundant and
    # self-heals via nginx failover. Only WARN when the WHOLE pool is unreachable.
    last = None
    for attempt in range(3):
        try:
            d = http_json(MLX_MODELS)
            models = [m.get("id", "?") for m in d.get("data", [])]
            return OK, f"serving {os.path.basename(models[0]) if models else '?'}"
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(3)
    # LB still failing after retries — distinguish "LB/transient blip" from "pool down"
    # by probing the backends directly. If any backend is serving, the pool has capacity
    # and this is a transient LB hiccup, not an outage worth alerting on.
    up = sum(1 for b in MLX_BACKENDS if _reachable_json(b))
    if up:
        return OK, f"LB blip (transient) — {up}/{len(MLX_BACKENDS)} backends serving directly"
    return WARN, f"MLX pool DOWN — 0/{len(MLX_BACKENDS)} backends reachable: {last}"


def _reachable_json(url):
    try:
        http_json(url)
        return True
    except Exception:
        return False


def check_ollama():
    try:
        d = http_json(OLLAMA_TAGS)
        return OK, f"{len(d.get('models', []))} models loaded"
    except Exception as e:
        return WARN, f"not serving on :11434: {e}"


def check_face_stack():
    """Import the face-recognition stack the way the scheduler does — this is the
    canary for the /Volumes/Data Python-package breakage seen after reboots."""
    code = ("import PIL.Image, PIL._imaging, face_recognition; "
            "print('ok', PIL.__version__)")
    env = dict(os.environ, PYTHONPATH=PKG_PATH)
    try:
        out = subprocess.run([sys.executable, "-c", code], env=env,
                             capture_output=True, text=True, timeout=120)   # 2026-10-03: cold import took >60s right after login
        if out.returncode == 0:
            return OK, f"imports clean ({out.stdout.strip()})"
        return FAIL, f"import error: {out.stderr.strip().splitlines()[-1][:120]}"
    except Exception as e:
        return FAIL, f"import check failed: {e}"


def check_disk():
    try:
        st = os.statvfs("/")
        pct = 100.0 * (st.f_blocks - st.f_bfree) / st.f_blocks
        free_gb = st.f_bavail * st.f_frsize / 1e9
        status = FAIL if pct >= 95 else WARN if pct >= 85 else OK
        return status, f"root {pct:.0f}% used, {free_gb:.0f}GB free"
    except Exception as e:
        return WARN, f"statvfs failed: {e}"


def check_pg_dedup():
    """Only the dedicated com.kochj.postgresql17 job should manage Postgres;
    the brew job kept erroring (-78) and racing it at boot."""
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
        brew = "homebrew.mxcl.postgresql@17" in out
        mine = "com.kochj.postgresql17" in out
        if brew:
            return WARN, "brew postgres job is loaded again (should be disabled)"
        if not mine:
            return WARN, "com.kochj.postgresql17 not loaded"
        return OK, "single dedicated launchd job"
    except Exception as e:
        return WARN, f"launchctl check failed: {e}"


def ha_availability():
    """(total, available) HA entity counts, or None if HA is unreachable.
    HA commonly comes up after a reboot with every entity 'unavailable' because
    it raced the network — this lets the boot run detect and self-heal that."""
    try:
        import nova_ha_poller as ha
        states = ha.ha_get_states() or []
        if not states:
            return None
        una = sum(1 for s in states if s.get("state") == "unavailable")
        return (len(states), len(states) - una)
    except Exception:
        return None


def check_home_assistant():
    av = ha_availability()
    if av is None:
        return FAIL, "unreachable on :8123"
    total, avail = av
    pct = 100.0 * avail / total if total else 0
    status = FAIL if pct < 40 else WARN if pct < 70 else OK
    return status, f"{avail}/{total} entities available ({pct:.0f}%)"


def heal_home_assistant():
    """If HA is up but mostly-unavailable (boot race), restart it once."""
    av = ha_availability()
    if not av or not av[0]:
        return
    total, avail = av
    if avail / total < 0.5:
        print(f"[nova_doctor] HA degraded ({avail}/{total} available) — restarting once")
        subprocess.run(["launchctl", "kickstart", "-k",
                        f"gui/{os.getuid()}/com.nova.homeassistant"],
                       capture_output=True, timeout=15)
        time.sleep(75)  # let integrations reconnect before the report runs


CHECKS = [
    ("Volumes", check_volumes),
    ("Postgres", check_postgres),
    ("WAL archive", check_wal_archiver),
    ("PG launchd", check_pg_dedup),
    ("Gateway", check_gateway),
    ("Home Assistant", check_home_assistant),
    ("MLX", check_mlx),
    ("Ollama", check_ollama),
    ("Face stack", check_face_stack),
    ("Disk", check_disk),
]


def post_slack(text):
    if not SLACK_TOKEN:
        print("[nova_doctor] no Slack token; skipping post", file=sys.stderr)
        return
    payload = json.dumps({"channel": REPORT_CHANNEL, "text": text,
                          "unfurl_links": False}).encode()
    req = urllib.request.Request(f"{SLACK_API}/chat.postMessage", data=payload,
                                 headers={"Authorization": f"Bearer {SLACK_TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if not resp.get("ok"):
            print(f"[nova_doctor] slack error: {resp.get('error')}", file=sys.stderr)
    except Exception as e:
        print(f"[nova_doctor] slack post failed: {e}", file=sys.stderr)


def wait_for_boot():
    """Block until both volumes are mounted, then give services a grace period."""
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if all(os.path.ismount(v) for v in ("/Volumes/Data", "/Volumes/MoreData")):
            break
        time.sleep(5)
    # Volumes up (or timed out) — let Postgres/gateway/MLX finish coming online.
    time.sleep(45)


def format_report(results):
    """[(name, status, detail)] -> (worst status, Slack/stdout report text)."""
    worst = FAIL if any(s == FAIL for _, s, _ in results) else \
            WARN if any(s == WARN for _, s, _ in results) else OK

    header = {OK: ":robot_face: *Nova boot check — all green*",
              WARN: ":robot_face: *Nova boot check — warnings*",
              FAIL: ":robot_face: *Nova boot check — FAILURES*"}[worst]
    lines = [header]
    for name, status, detail in results:
        lines.append(f"{ICON[status]} *{name}* — {detail}")

    return worst, "\n".join(lines)


def main(argv=None):
    """Thin wrapper: merged into nova_selfcheck.py --boot on 2026-10-09."""
    argv = sys.argv[1:] if argv is None else list(argv)
    if "-h" in argv or "--help" in argv:
        print("usage: nova_doctor.py [--boot] [--dry-run]  (merged into nova_selfcheck.py --boot on 2026-10-09)")
        return 0
    import nova_selfcheck
    nova_selfcheck.log("nova_doctor.py was merged into nova_selfcheck.py --boot on 2026-10-09 — delegating")
    args = ["--boot"] + ([] if "--boot" in argv else ["--now"]) + (["--dry-run"] if "--dry-run" in argv else [])
    return nova_selfcheck.main(args)


if __name__ == "__main__":
    sys.modules.setdefault("nova_doctor", sys.modules[__name__])  # selfcheck --boot imports nova_doctor: reuse this copy
    sys.exit(main())
