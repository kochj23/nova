#!/usr/bin/env python3
"""
nova_doctor.py — Post-boot health self-check for the Nova stack.

Runs the checks that, when they silently fail at boot, take Nova down:
volumes, Postgres, gateway, MLX, the face-recognition import, WAL archiving,
and disk capacity. Posts ONE consolidated report to Slack (#nova-bb) so a bad
boot is visible at a glance instead of being discovered piecemeal days later.

Modes:
  --boot   Wait for /Volumes/Data + /Volumes/MoreData to mount, give services a
           grace period to come up, then run. (Used by the launchd boot job.)
  (none)   Run immediately and report. (Manual / on-demand.)

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

PG_DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"
PKG_PATH = "/Volumes/Data/AI/python_packages"
GATEWAY_HEALTH = "http://192.168.1.2:18792/health"
MLX_MODELS = "http://127.0.0.1:5050/v1/models"
OLLAMA_TAGS = "http://127.0.0.1:11434/api/tags"

OK, WARN, FAIL = "ok", "warn", "fail"
ICON = {OK: ":white_check_mark:", WARN: ":warning:", FAIL: ":rotating_light:"}


def http_json(url, timeout=6):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


# ── Individual checks ─────────────────────────────────────────────────────────
# Each returns (status, detail).

def check_volumes():
    missing = [v for v in ("/Volumes/Data", "/Volumes/MoreData") if not os.path.ismount(v)]
    if missing:
        return FAIL, f"not mounted: {', '.join(missing)}"
    return OK, "Data + MoreData mounted"


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
    try:
        d = http_json(MLX_MODELS)
        models = [m.get("id", "?") for m in d.get("data", [])]
        return OK, f"serving {os.path.basename(models[0]) if models else '?'}"
    except Exception as e:
        return WARN, f"not serving on :5050: {e}"


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
                             capture_output=True, text=True, timeout=60)
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


def main():
    if "--boot" in sys.argv:
        wait_for_boot()
        heal_home_assistant()  # self-correct the HA-races-network boot failure

    results = [(name, *fn()) for name, fn in CHECKS]
    worst = FAIL if any(s == FAIL for _, s, _ in results) else \
            WARN if any(s == WARN for _, s, _ in results) else OK

    header = {OK: ":robot_face: *Nova boot check — all green*",
              WARN: ":robot_face: *Nova boot check — warnings*",
              FAIL: ":robot_face: *Nova boot check — FAILURES*"}[worst]
    lines = [header]
    for name, status, detail in results:
        lines.append(f"{ICON[status]} *{name}* — {detail}")

    report = "\n".join(lines)
    print(report)
    post_slack(report)
    return 0 if worst != FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
