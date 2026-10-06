#!/usr/bin/env python3
"""nova_selfcheck.py — the "ignore Nova for a week" watchdog.

Runs every 30 min from launchd (net.digitalnoise.nova-selfcheck). Checks the
outcomes that matter (is data flowing?), not just whether processes exist,
auto-fixes the failure classes we have already lived through, records every
run to nova_ops.selfcheck_runs, and posts a one-message daily digest to
#nova-digest around 07:00.

Born 2026-08-24 after a weekend where the PG primary died and five separate
things failed silently. Design rule: every check must alarm on the OUTCOME
(backup landed, memory written, heartbeat fresh) so that a wedged-but-running
process can never look healthy.

Fix policy: one automatic fix attempt per check per run; if the re-check still
fails, escalate once per 6h to a headless `claude -p` session, and always leave
the failure in selfcheck_runs for the digest.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PSQL = ["/opt/homebrew/bin/psql", "-h", "localhost", "-U", "kochj", "-tA"]
STATE_DIR = Path.home() / ".openclaw" / "state"
LOG = Path.home() / ".openclaw" / "logs" / "nova_selfcheck.log"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
PRIMARY = "192.168.1.2"  # SWITCHOVER 2026-09-28 14:07: back to nova-core (.2). Native PG on :5434; :5432 is the socat shim -> :5434 (what we use). Standbys .10/.7/.125 — see agent_docs db-topology.
PG_REBUILD_PENDING = STATE_DIR / "pg_rebuild_pending.json"  # {"ips":[...],"note":...}; standbys listed here are being re-seeded — do not auto-restart, do not escalate (ignored after 48h)
SLACK_DIGEST_CHANNEL = "C0BLJLKQMMZ"   # #nova-digest
SLACK_ALERT_CHANNEL = "C0BMK83BLFJ"    # #nova-alerts
CLAUDE = "/opt/homebrew/bin/claude"
ESCALATION_COOLDOWN_S = 6 * 3600

results = []  # (check, status, action, detail)


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def sh(cmd, timeout=60):
    """Run a command, return (rc, stdout+stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def pg(sql, db="nova_ops", host="localhost", timeout=20):
    rc, out = sh(["/opt/homebrew/bin/psql", "-h", host, "-U", "kochj", "-d", db, "-tA", "-c", sql], timeout)
    return (out if rc == 0 else None)


def record(check, status, action=None, detail=None):
    results.append((check, status, action, detail))
    log(f"{check}: {status}" + (f" | fix: {action}" if action else "") + (f" | {detail}" if detail else ""))


def slack(channel, text):
    try:
        token = subprocess.check_output(
            ["security", "find-generic-password", "-s", "nova-slack-bot-token", "-w"], text=True).strip()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps({"channel": channel, "text": text}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:  # noqa: BLE001
        log(f"slack post failed: {e}")


# ── Checks ────────────────────────────────────────────────────────────────────

def check_primary():
    if pg("SELECT 1") == "1":
        record("pg-primary", "ok")
        return True
    # pgbouncer may be wedged while the primary itself is fine
    if pg("SELECT 1", host=PRIMARY) == "1":
        sh(["pkill", "-HUP", "pgbouncer"])
        time.sleep(3)
        if pg("SELECT 1") == "1":
            record("pg-primary", "fixed", "HUP pgbouncer", "primary fine, local pgbouncer was wedged")
            return True
        record("pg-primary", "FAIL", "HUP pgbouncer", "primary reachable direct but not via pgbouncer")
        return False
    record("pg-primary", "CRITICAL", None,
           f"primary {PRIMARY}:5432 unreachable — manual failover required (see agent_docs data-platform)")
    slack(SLACK_ALERT_CHANNEL,
          f":rotating_light: *selfcheck: PG PRIMARY UNREACHABLE* — {PRIMARY} (nova-core) is down. "
          "No auto-failover configured; promote a replica per the data-platform runbook.")
    return False


def check_replication():
    out = pg("SELECT count(*), COALESCE(max(EXTRACT(EPOCH FROM replay_lag)),0)::int FROM pg_stat_replication",
             host=PRIMARY)
    if out is None:
        record("replication", "SKIP", None, "primary unreachable")
        return
    count, lag = out.split("|")
    if int(count) >= 2 and int(lag) < 900:
        record("replication", "ok", None, f"{count} replicas, max lag {lag}s")
        return
    # Topology since the 2026-09-28 switchover: primary .2, standbys .10 (native postgresql-17.service),
    # .7 (LaunchDaemon com.kochj.postgresql17-replica), .125 (docker pg17-replica).
    STANDBYS = (
        ("192.168.1.10", "nova-core5/.10 native postgresql-17", SSH + ["kochj@192.168.1.10", "sudo -n systemctl restart postgresql-17"], 60),
        ("192.168.1.7", ".7 tv_movies LaunchDaemon", SSH + ["kochj@192.168.1.7", "sudo -n launchctl kickstart -k system/com.kochj.postgresql17-replica"], 60),
        ("192.168.1.125", "core7/.125 pg17-replica container", SSH + ["kochj@192.168.1.125", "docker restart pg17-replica"], 90),
    )
    pending = set()
    try:
        if PG_REBUILD_PENDING.exists() and time.time() - PG_REBUILD_PENDING.stat().st_mtime < 48 * 3600:
            pending = set(json.loads(PG_REBUILD_PENDING.read_text()).get("ips", []))
    except Exception as e:  # noqa: BLE001
        log(f"pg_rebuild_pending unreadable: {e}")
    addrs = pg("SELECT COALESCE(string_agg(client_addr::text, ','), '') FROM pg_stat_replication", host=PRIMARY) or ""
    fixes = []
    for ip, name, cmd, tmo in STANDBYS:
        if ip in addrs or ip in pending:
            continue
        sh(cmd, tmo)
        fixes.append(f"restarted {name}")
    if fixes:
        time.sleep(20)
    addrs2 = pg("SELECT COALESCE(string_agg(client_addr::text, ','), '') FROM pg_stat_replication", host=PRIMARY) or ""
    present = [a for a in addrs2.split(",") if a]
    missing = [name for ip, name, _, _ in STANDBYS if ip not in addrs2]
    missing_pending = [name for ip, name, _, _ in STANDBYS if ip not in addrs2 and ip in pending]
    detail = f"was {count} replicas (lag {lag}s), now {len(present)} [{addrs2 or 'none'}]. MISSING: {', '.join(missing) or 'none'}."
    if missing_pending:
        detail += f" REBUILD PENDING (not auto-restarted): {', '.join(missing_pending)}."
    if any(ip == "192.168.1.125" for ip, n, _, _ in STANDBYS if n in missing):
        detail += " core7 history: OOM-killed 2026-09-19 when shared_buffers exceeded the docker memory cap — check `docker events`/`docker stats`."
    if any(ip == "192.168.1.7" for ip, n, _, _ in STANDBYS if n in missing):
        detail += " NOTE .7 dies if macOS re-revokes Local Network — needs a GUI login via vnc://192.168.1.7 to re-approve."
    if len(present) >= 2:
        status = "fixed" if fixes else "ok"
    elif missing and len(missing) == len(missing_pending):
        status = "PENDING"  # every missing standby is a known rebuild — no escalation
    else:
        status = "FAIL"
    record("replication", status, ", ".join(fixes) or None, detail)


def check_backups():
    bad = []
    for job in ("nova-backup:nas", "nova-backup:external"):
        out = pg("SELECT count(*) FROM telemetry.backup_runs WHERE job LIKE '" + job +
                 ":%' AND job NOT LIKE '%lockguard%' AND ok AND ts > now() - interval '30 hours'")
        if out == "0":
            bad.append(job)
    if not bad:
        record("backups", "ok")
        return
    # One rerun attempt per day — the agent now clears its own stale locks
    marker = STATE_DIR / "selfcheck_backup_rerun.ts"
    if marker.exists() and time.time() - marker.stat().st_mtime < 20 * 3600:
        record("backups", "FAIL", "rerun already attempted today", ",".join(bad))
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    rc, out = sh(SSH + ["kochj@192.168.1.11", "/volume1/homes/kochj/nova_backup_agent.sh incremental"], 3600)
    if rc == 3:
        # Agent exits 3 (stdout "LOCKED: ...") when another NAS job holds the flock — e.g. the Sunday
        # 04:30 full run, which takes most of the day. Nothing is broken; do not burn today's rerun
        # budget or escalate. (2026-10-04: this was reported as "FAIL ... agent rc=0" and escalated.)
        marker.unlink(missing_ok=True)
        record("backups", "PENDING", None,
               f"stale: {','.join(bad)}; a backup job is already running on the synology (lock held) — "
               "will re-check next cycle. If this persists >24h the agent's lock-guard kills the holder.")
        return
    ok_now = all(pg("SELECT count(*) FROM telemetry.backup_runs WHERE job LIKE '" + j +
                    ":%' AND job NOT LIKE '%lockguard%' AND ok AND ts > now() - interval '2 hours'") != "0"
                 for j in bad)
    record("backups", "fixed" if ok_now else "FAIL", "reran backup agent on synology",
           f"stale: {','.join(bad)}, agent rc={rc} {out[-200:]!r}. Agent reports telemetry to PG via "
           "postgresql://kochj@192.168.1.2:5432 (+~/.pgpass on the synology); check nova_backup.log for "
           "'telemetry FAILED' vs real rsync failures — the backup may have succeeded while telemetry did not.")


MESH_FIX = {
    "nova-core": (SSH + ["kochj@192.168.1.2", "sudo -n systemctl restart nova-mesh-agent"]),
    "nova-core2": (SSH + ["kochj@192.168.1.86", "sudo -n systemctl restart nova-mesh-agent"]),
    "nova-core3": (SSH + ["kochj@192.168.1.5", "sudo -n systemctl restart nova-mesh-agent"]),
    "nova-core4": (SSH + ["kochj@192.168.1.250", "sudo -n systemctl restart nova-mesh-agent"]),
    "nuk": (SSH + ["kochj@192.168.1.10", "sudo -n systemctl restart nova-mesh-agent"]),
    "tv-movies-mini": (SSH + ["kochj@192.168.1.7", "sudo -n launchctl kickstart -k system/net.digitalnoise.nova-mesh-agent"]),
    "mac-mini": (SSH + ["kochj@192.168.1.77", "launchctl kickstart -k gui/501/net.digitalnoise.nova-mesh-agent"]),  # .77 = wired (.251 is Wi-Fi); agent is a GUI LaunchAgent, not system/
    "mac-studio": ["launchctl", "kickstart", "-k", "gui/501/net.digitalnoise.nova-mesh-agent"],
}


def check_heartbeats():
    out = pg("SELECT COALESCE(string_agg(node_name, ','), '') FROM node_status "
             "WHERE last_heartbeat < now() - interval '10 minutes'")
    if out is None:
        record("heartbeats", "SKIP", None, "PG unreachable")
        return
    stale = [n for n in out.split(",") if n]
    if not stale:
        record("heartbeats", "ok")
        return
    for node in stale:
        if node in MESH_FIX:
            sh(MESH_FIX[node], 45)
    time.sleep(30)
    out2 = pg("SELECT COALESCE(string_agg(node_name, ','), '') FROM node_status "
              "WHERE last_heartbeat < now() - interval '10 minutes'") or ""
    still = [n for n in out2.split(",") if n]
    record("heartbeats", "FAIL" if still else "fixed",
           f"restarted mesh agents: {','.join(stale)}",
           f"still stale: {','.join(still) or 'none'}")


def check_ingest():
    out = pg("SELECT EXTRACT(EPOCH FROM (now() - max(created_at)))::int FROM memories", db="nova_memories")
    if out is None:
        record("memory-ingest", "SKIP", None, "nova_memories unreachable")
        return
    age_h = int(out) / 3600
    if age_h < 6:
        record("memory-ingest", "ok", None, f"last memory {age_h:.1f}h ago")
        return
    sh(SSH + ["kochj@192.168.1.2", "sudo -n systemctl restart nova-memory-server"], 60)
    time.sleep(30)
    rc, health = sh(["curl", "-s", "-m", "8", "http://127.0.0.1:18790/health"])
    ok = rc == 0 and '"status":"ok"' in health.replace(" ", "")
    record("memory-ingest", "fixed" if ok else "FAIL",
           "restarted nova-memory-server on nova-core",
           f"last memory {age_h:.1f}h ago; health after restart: {health[:120]}")


SERVICES = [
    ("memory-server", "http://127.0.0.1:18790/health",
     SSH + ["kochj@192.168.1.2", "sudo -n systemctl restart nova-memory-server"]),
    ("gateway-v2", "http://127.0.0.1:18792/health",
     ["launchctl", "kickstart", "-k", "gui/501/net.digitalnoise.nova-gateway-v2"]),
]


# Local backends that can actually answer *private* (home/personal) chat. openrouter is
# excluded on purpose: the privacy blocklist bars cloud for that traffic, so if every local
# backend is dead, Nova is voiceless for real chat even while /health says ok:true. That
# "up but can't speak" state is exactly what took the gateway dark on 2026-09-18.
_VOICE_BACKENDS = ("ollama", "mlx", "llamacpp")


def _gateway_voiceless(out: str) -> bool:
    """True iff the gateway reports ok but no local backend can answer chat."""
    try:
        h = json.loads(out)
    except Exception:
        return False  # unparseable → let the plain ok-string test decide
    backends = h.get("backends")
    if not isinstance(backends, dict):
        return False  # no backend detail (e.g. memory-server) → not our concern
    return not any(backends.get(b, {}).get("healthy") for b in _VOICE_BACKENDS)


def check_services():
    for name, url, fix in SERVICES:
        rc, out = sh(["curl", "-s", "-m", "8", url])
        alive = rc == 0 and ('"ok": true' in out or '"status":"ok"' in out.replace(" ", "") or '"ok":true' in out.replace(" ", ""))
        if alive and _gateway_voiceless(out):
            alive = False  # process up but no local LLM → self-heal, don't report ok
        if alive:
            record(f"svc-{name}", "ok")
            continue
        sh(fix, 60)
        time.sleep(15)
        rc2, out2 = sh(["curl", "-s", "-m", "8", url])
        ok = rc2 == 0 and ("ok" in out2) and not _gateway_voiceless(out2)
        record(f"svc-{name}", "fixed" if ok else "FAIL", "restarted", out2[:100])


def check_disks():
    out = pg("SELECT COALESCE(string_agg(node_name || ':' || round(disk_percent) || '%', ', '), '') "
             "FROM node_status WHERE disk_percent > 90")
    if out:
        record("disk", "WARN", None, out)
    elif out is not None:
        record("disk", "ok")


MOUNTS = {"/Volumes/nas": "192.168.1.69", "/Volumes/external": "192.168.1.69",   # want: server the share must come from
          "/Volumes/Data": None, "/Volumes/MoreData": None}
SHARE_MOUNT_JOB = "net.digitalnoise.nova-mac-share-mount"


def mount_state(path):
    """(source, options) from `mount`, or (None, '') if not mounted. The mount table is honest in
    launchd/TCC contexts where `ls` is not."""
    rc, out = sh(["/sbin/mount"], 10)
    for line in (out or "").splitlines():
        if f" on {path} (" in line:
            src, _, rest = line.partition(" on ")
            return src.strip(), rest[rest.find("(") + 1:rest.rfind(")")]
    return None, ""


def mount_problem(path, want_src):
    """None when the mount is present AND working; else a short reason. 'Working' = right server,
    not read-only, and a byte can be written (EROFS counts, EACCES on a root-owned dir does not)."""
    import errno, os
    src, opts = mount_state(path)
    if src is None:
        return "not mounted"
    if want_src and want_src not in src:
        return f"on {src} (want {want_src})"
    if "read-only" in opts:
        return f"read-only ({src})"
    probe = os.path.join(path, ".nova_write_probe")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        if e.errno != errno.EACCES:          # root-owned volume root is fine; a server refusing writes is not
            return f"write failed: {e.strerror} ({src})"
    return None


def check_mounts():
    """2026-10-02: both NAS shares sat on the Synology READ-ONLY fallback for 12h after the macOS
    27 reboot while every 'is it mounted' check stayed green. Present is not working."""
    bad = {m: r for m, want in MOUNTS.items() if (r := mount_problem(m, want))}
    if not bad:
        record("mounts", "ok"); return
    detail = "; ".join(f"{m}: {r}" for m, r in bad.items())
    shares = [m for m in bad if MOUNTS[m]]
    if not shares:
        record("mounts", "FAIL", None, detail); return
    # the share-mount job's own umount -f could not drop the fallback from its launchd context, but a
    # plain umount from a sibling context did (this morning's fix) — try that, then let the job remount.
    for m in shares:
        sh(["/sbin/umount", m], 20)
    sh(["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{SHARE_MOUNT_JOB}"], 20)
    time.sleep(40)
    still = {m: r for m in shares if (r := mount_problem(m, MOUNTS[m]))}
    record("mounts", "FAIL" if still else "fixed", f"umount + kickstart {SHARE_MOUNT_JOB}",
           "; ".join(f"{m}: {r}" for m, r in still.items()) or detail)


# ── Escalation & digest ───────────────────────────────────────────────────────

def escalate(failures):
    marker = STATE_DIR / "selfcheck_escalation.ts"
    if marker.exists() and time.time() - marker.stat().st_mtime < ESCALATION_COOLDOWN_S:
        log("escalation suppressed (cooldown)")
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    summary = "; ".join(f"{c}: {d or s}" for c, s, a, d in failures)
    prompt = (
        "You are the escalation step of nova_selfcheck.py on mac-studio. These checks failed and their "
        f"automatic fixes did not work: {summary}. Investigate and fix them. Fleet knowledge is in "
        "nova_ops.agent_docs (doc_type='data-platform'). Log what you do to claude_actions "
        "(session_id 'selfcheck-escalation'). If a fix needs a human, post specifics to Slack #nova-alerts.")
    log(f"escalating to claude: {summary}")
    slack(SLACK_ALERT_CHANNEL, f":robot_face: selfcheck escalating to Claude: {summary}")
    rc, out = sh([CLAUDE, "-p", prompt, "--permission-mode", "bypassPermissions"], timeout=1500)
    log(f"claude escalation rc={rc}: {out[-400:]}")


FORCE_DIGEST = False


def post_digest():
    """Post one digest covering the last 24h, at most once per day, around 07:00."""
    marker = STATE_DIR / "selfcheck_digest.date"
    today = datetime.now().strftime("%Y-%m-%d")
    if not FORCE_DIGEST and (datetime.now().hour < 7 or (marker.exists() and marker.read_text().strip() == today)):
        return
    rows = pg("SELECT check_name, status, count(*) FROM selfcheck_runs "
              "WHERE ts > now() - interval '24 hours' GROUP BY 1,2 ORDER BY 1,2")
    fixes = pg("SELECT ts::timestamp(0) || ' ' || check_name || ': ' || action FROM selfcheck_runs "
               "WHERE ts > now() - interval '24 hours' AND status IN ('fixed','FAIL','CRITICAL') "
               "AND action IS NOT NULL ORDER BY ts DESC LIMIT 10")
    counts = {}
    for line in (rows or "").splitlines():
        check, status, n = line.split("|")
        counts.setdefault(check, []).append(f"{status}×{n}")
    body = "\n".join(f"  {'✅' if all(s.startswith('ok') for s in v) else '⚠️'} {k}: {', '.join(v)}"
                     for k, v in sorted(counts.items()))
    actions = ("\n*Actions taken:*\n" + "\n".join(f"  • {l}" for l in fixes.splitlines())) if fixes else ""
    slack(SLACK_DIGEST_CHANNEL,
          f":shield: *Nova self-check daily digest* ({today})\n{body}{actions}\n"
          f"_checks run every 30 min; details in nova_ops.selfcheck_runs_")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(today)
    log("daily digest posted")


def lan_up():
    rc, _ = sh(["ping", "-c", "1", "-t", "3", "192.168.1.1"], timeout=10)
    return rc == 0


def main():
    log("=== selfcheck run start ===")
    # 2026-09-02: a ~3 min site network blip made the primary look dead and
    # triggered a false "manual failover required" escalation. If we can't even
    # reach the gateway, nothing here is diagnosable (and PG/Slack/claude are
    # all unreachable anyway) — wait out a blip, else skip the run.
    if not lan_up():
        time.sleep(60)
        if not lan_up():
            log("=== local network down (gateway 192.168.1.1 unreachable) — skipping run ===")
            return
        log("network blip recovered after 60s retry")
    primary_up = check_primary()
    if primary_up:
        check_replication()
        check_backups()
        check_heartbeats()
        check_ingest()
        check_disks()
    check_services()
    check_mounts()

    # persist results (best effort — PG may be the thing that is down)
    for check, status, action, detail in results:
        sql = ("INSERT INTO selfcheck_runs (check_name, status, action, detail) VALUES "
               f"($novaq${check}$novaq$, $novaq${status}$novaq$, "
               f"$novaq${action or ''}$novaq$, $novaq${(detail or '')[:500]}$novaq$)")
        pg(sql)

    failures = [r for r in results if r[1] in ("FAIL", "CRITICAL")]
    if failures:
        escalate(failures)
    post_digest()
    log(f"=== selfcheck run done: {sum(1 for r in results if r[1]=='ok')} ok, "
        f"{sum(1 for r in results if r[1]=='fixed')} fixed, {len(failures)} failing ===")


if __name__ == "__main__":
    if "--digest-now" in sys.argv:
        FORCE_DIGEST = True
    main()
