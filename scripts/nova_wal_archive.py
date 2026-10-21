#!/usr/bin/env python3
"""nova_wal_archive.py — continuous WAL archiving for the nova-core Postgres primary (Tier-0 RPO in minutes).

Little Mister, 2026-10-15 (#36): "PG WAL archiving to the NAS so Tier0 RPO becomes minutes."
SRE book ch.26 (Data Integrity): a nightly pg_dump is a 24-hour recovery point; WAL archiving plus a
periodic base backup is point-in-time recovery to within one archive_timeout (300 s).

Pieces:
  nova_wal_archive_cmd.sh   archive_command (zstd segment -> NAS, never overwrites, safety valve)
  --install                 copy that script to the LOCAL path Postgres runs (survives a NAS outage)
  --stage                   install + ALTER SYSTEM archive_command + reload (no restart, no effect yet)
  --enable                  the one-shot: pre-checks -> ALTER SYSTEM archive_mode=on -> restart
                            postgresql-17 -> post-checks -> pg_switch_wal -> verify archived ->
                            service_config wal_archive/enabled=true -> first base backup.
                            Waits (up to --wait-until HH:MM) while pg_dump / a base backup runs.
  --basebackup              pg_basebackup (tar, zstd, WAL streamed) to NAS basebackup/<ts>; recorded
                            in nova_ops.pg_basebackups. Weekly, plus on demand after a gap.
  --cleanup                 keep the newest KEEP_BASE successful base backups; delete archived WAL
                            older than the oldest kept backup's start segment (pg_archivecleanup rule)
  --monitor                 pg_stat_archiver: alarm when archiving stalls or fails (Tier-0 RPO 15 m),
                            when segments were dropped by the safety valve (chain broken -> Nova
                            starts a fresh base backup herself), or when the installed command
                            drifted from the repo copy.

Paths (env overrides): NOVA_WAL_ARCHIVE_DIR=/mnt/nas/backups/pg_wal_archive,
NOVA_WAL_BASE_DIR=/mnt/nas/backups/pg_basebackup. Runs on nova-core (scheduler-core).
Restore runbook: agent_docs doc_type 'data-platform' ("WAL archiving & PITR").

Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

ARCHIVE_DIR = Path(os.environ.get("NOVA_WAL_ARCHIVE_DIR", "/mnt/nas/backups/pg_wal_archive"))
BASE_DIR = Path(os.environ.get("NOVA_WAL_BASE_DIR", "/mnt/nas/backups/pg_basebackup"))
PG_BIN = Path(os.environ.get("NOVA_PG_BIN", "/home/linuxbrew/.linuxbrew/opt/postgresql@17/bin"))
PG_UNIT = os.environ.get("NOVA_PG_UNIT", "postgresql-17")
PG_PORT = os.environ.get("NOVA_PG_PRIMARY_PORT", "5434")
# The primary BY NAME (same alias nova_dsn uses), never localhost: scheduler-core may run on its standby
# nova-core3 (.5), where localhost:5434 refused (2026-10-18 and 10-21). Port 5434 is still the direct
# postgres port (archiving facts are per-server, never via pgbouncer/socat).
PG_HOST = os.environ.get("NOVA_PG_PRIMARY_HOST") or os.environ.get("NOVA_PG_HOST", "pg-primary.digitalnoise.net")
LOCAL_CMD = Path(os.environ.get("NOVA_WAL_CMD_PATH", str(Path.home() / ".local/libexec/nova/nova_wal_archive_cmd.sh")))
REPO_CMD = SCRIPTS_DIR / "nova_wal_archive_cmd.sh"
GAP_LOG = Path(os.environ.get("NOVA_WAL_GAP_LOG", str(Path.home() / ".local/state/nova/wal_gaps.log")))
LOCK = Path.home() / ".local/state/nova/wal_basebackup.lock"
KEEP_BASE = 2
NO_BASE_KEEP_S = 3 * 86400          # WAL with no base backup to replay onto is useless; keep 3 days
STALL_WARN_S = 15 * 60              # Tier-0 RPO: newest archived segment older than this = stalled
STALL_CRIT_S = 30 * 60
MIN_REPLICAS = 2                    # restart pre-check: at least this many streaming standbys
MAX_REPLAY_LAG_S = 60
CONFIG_SERVICE = "wal_archive"      # service_config (wal_archive, enabled) — set by --enable on success

SEG_RE = re.compile(r"^([0-9A-F]{8})([0-9A-F]{8})([0-9A-F]{8})(\.partial)?(\.zst)?$")
BACKUP_LABEL_RE = re.compile(r"^([0-9A-F]{24})\.[0-9A-F]{8}\.backup(\.zst)?$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pg_basebackups (
    id          bigserial PRIMARY KEY,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    host        text,
    path        text,
    bytes       bigint,
    start_lsn   text,
    start_wal   text,
    timeline    integer,
    ok          boolean NOT NULL DEFAULT false,
    error       text,
    trigger     text
)
"""


def log(msg):
    print(f"[wal-archive {datetime.now():%H:%M:%S}] {msg}", flush=True)


# ── Pure helpers (unit-tested) ────────────────────────────────────────────────
def lsn_to_segment(lsn: str, timeline: int = 1, seg_bytes: int = 16 * 1024 * 1024) -> str:
    """'307/74000028' -> '000000010000030700000074' (pg_walfile_name for 16 MB segments)."""
    hi, lo = (int(x, 16) for x in lsn.split("/"))
    segs_per_xlogid = 0x100000000 // seg_bytes
    segno = ((hi << 32) | lo) // seg_bytes
    return f"{timeline:08X}{segno // segs_per_xlogid:08X}{segno % segs_per_xlogid:08X}"


def parse_basebackup_stderr(text: str):
    """-> (start_lsn, timeline) from `pg_basebackup -v` output, or (None, None)."""
    m = re.search(r"write-ahead log start point: ([0-9A-F]+/[0-9A-F]+) on timeline (\d+)", text or "")
    return (m.group(1), int(m.group(2))) if m else (None, None)


def wal_to_delete(names, oldest_start_wal: str | None, now_epoch: float, mtimes: dict | None = None):
    """Archived names older than the oldest kept base backup's start segment (pg_archivecleanup's
    rule: same 24-char ordering, timeline included). .history files are always kept. With no base
    backup at all, only segments older than NO_BASE_KEEP_S (by mtime) go. Pure."""
    out = []
    for n in names:
        if n.endswith(".history") or n.endswith(".history.zst") or n.startswith("."):
            continue
        m = SEG_RE.match(n)
        b = BACKUP_LABEL_RE.match(n)
        key = (m.group(1) + m.group(2) + m.group(3)) if m else (b.group(1) if b else None)
        if key is None:
            continue
        if oldest_start_wal:
            if key < oldest_start_wal:
                out.append(n)
        elif mtimes and now_epoch - mtimes.get(n, now_epoch) > NO_BASE_KEEP_S:
            out.append(n)
    return out


def archiver_state(stats: dict, now_epoch: float, enabled: bool) -> tuple[str, str]:
    """stats: archive_mode, archived_count, last_archived_time (epoch), failed_count,
    last_failed_time (epoch), last_failed_wal. -> (level, message); level ok|info|warning|critical."""
    if not enabled:
        mode = stats.get("archive_mode")
        return ("info", f"archiving not enabled yet (archive_mode={mode})")
    if stats.get("archive_mode") not in ("on", "always"):
        return ("critical", f"archive_mode={stats.get('archive_mode')} but wal_archive/enabled is true — "
                            "archiving silently off (config reverted?)")
    last_ok = stats.get("last_archived_time")
    last_fail = stats.get("last_failed_time")
    if last_ok is None:
        if last_fail:
            return ("critical", f"no segment ever archived; last failure {stats.get('last_failed_wal')}")
        return ("info", "no segment archived yet (just enabled?)")
    age = now_epoch - last_ok
    failing = last_fail is not None and last_fail > last_ok
    if age > STALL_CRIT_S or (failing and age > STALL_WARN_S):
        return ("critical", f"WAL archiving STALLED: last archived {age / 60:.0f} min ago"
                            + (f", failing on {stats.get('last_failed_wal')}" if failing else "")
                            + " — Tier-0 RPO (15 m) is being missed")
    if age > STALL_WARN_S:
        return ("warning", f"WAL archiving slow: last archived {age / 60:.0f} min ago")
    if failing:
        return ("warning", f"archive_command failing on {stats.get('last_failed_wal')} (retrying; last "
                           f"success {age / 60:.0f} min ago)")
    return ("ok", f"archiving OK: last segment {age / 60:.1f} min ago, {stats.get('archived_count')} archived, "
                  f"{stats.get('failed_count')} failures since reset")


def gaps_since(gap_lines, since_epoch: float | None):
    """Gap-log lines ('<iso8601Z> DROPPED <name> ...') newer than since_epoch (None = all). Pure."""
    out = []
    for line in gap_lines:
        parts = line.split()
        if len(parts) < 3 or parts[1] != "DROPPED":
            continue
        try:
            ts = datetime.strptime(parts[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if since_epoch is None or ts > since_epoch:
            out.append((ts, parts[2]))
    return out


def precheck_restart(replicas, busy, in_recovery: bool, wal_free_gb: float | None):
    """Can the primary be restarted now? replicas: [(name, state, replay_lag_s)]; busy: list of
    running pg_dump/basebackup descriptions. -> (ok, reasons). Pure."""
    reasons = []
    if in_recovery:
        reasons.append("target is a standby, not the primary")
    streaming = [r for r in replicas if r[1] == "streaming"]
    if len(streaming) < MIN_REPLICAS:
        reasons.append(f"only {len(streaming)} streaming replicas (need {MIN_REPLICAS})")
    lagging = [r[0] for r in streaming if r[2] is not None and r[2] > MAX_REPLAY_LAG_S]
    if lagging:
        reasons.append(f"replicas lagging > {MAX_REPLAY_LAG_S}s: {', '.join(lagging)}")
    if busy:
        reasons.append("busy: " + "; ".join(busy))
    if wal_free_gb is not None and wal_free_gb < 60:
        reasons.append(f"only {wal_free_gb:.0f} GB free on the data volume")
    return (not reasons), reasons


def file_sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# ── IO helpers ────────────────────────────────────────────────────────────────
def connect(dbname="nova_ops"):
    """Direct to the primary's postgres port (5434) — archiving facts are per-server, never via pgbouncer."""
    import psycopg2
    return psycopg2.connect(host=PG_HOST, port=PG_PORT, dbname=dbname, user="kochj", connect_timeout=10)


def on_primary_host() -> bool:
    """True when this machine owns the primary's address: the archive_command copy and the gap log
    it writes are local files there, so only there can they be checked."""
    try:
        ip = socket.gethostbyname(PG_HOST)
        with socket.socket() as sk:
            sk.bind((ip, 0))
        return True
    except OSError:
        return False


def pg_basebackup_bin() -> str:
    """The linuxbrew postgresql@17 client on the primary; elsewhere (standby .5) the distro's."""
    p = PG_BIN / "pg_basebackup"
    return str(p) if p.exists() else (shutil.which("pg_basebackup") or str(p))


def q1(cur, sql, params=None):
    cur.execute(sql, params)
    return cur.fetchone()


def archiver_stats(cur) -> dict:
    cur.execute("""SELECT current_setting('archive_mode'), archived_count,
                          extract(epoch FROM last_archived_time), last_archived_wal, failed_count,
                          extract(epoch FROM last_failed_time), last_failed_wal FROM pg_stat_archiver""")
    mode, ac, lat, law, fc, lft, lfw = cur.fetchone()
    return {"archive_mode": mode, "archived_count": ac, "last_archived_time": float(lat) if lat else None,
            "last_archived_wal": law, "failed_count": fc, "last_failed_time": float(lft) if lft else None,
            "last_failed_wal": lfw}


def is_enabled(cur) -> bool:
    r = q1(cur, "SELECT value FROM service_config WHERE service = %s AND key = 'enabled'", (CONFIG_SERVICE,))
    return bool(r and r[0] is True)


def set_config(cur, key, value):
    cur.execute("INSERT INTO service_config (service, key, value, updated_by) VALUES (%s, %s, %s, 'nova_wal_archive') "
                "ON CONFLICT (service, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), "
                "updated_by = EXCLUDED.updated_by", (CONFIG_SERVICE, key, json.dumps(value)))


def notify(title, body, level, key):
    try:
        from nova_notify import notify as _n
        _n(title, body=body, level=level, category="backup", source="nova_wal_archive.py",
           dedup_key=f"wal-archive:{key}")
    except Exception as e:  # noqa: BLE001
        log(f"notify failed: {e}; {level}: {title}: {body}")


def install() -> bool:
    """Copy the repo archive_command to its local path (atomic). True if it changed."""
    LOCAL_CMD.parent.mkdir(parents=True, exist_ok=True)
    if file_sha(LOCAL_CMD) == file_sha(REPO_CMD):
        log(f"{LOCAL_CMD} already current")
        return False
    tmp = LOCAL_CMD.with_suffix(".tmp")
    shutil.copyfile(REPO_CMD, tmp)
    os.chmod(tmp, 0o755)
    os.replace(tmp, LOCAL_CMD)
    log(f"installed {LOCAL_CMD}")
    return True


def archive_command() -> str:
    return f"{LOCAL_CMD} %p %f"


def stage(cur):
    """Install + set archive_command + reload. No restart; inert while archive_mode is off."""
    install()
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    probe = ARCHIVE_DIR / f".probe.{os.getpid()}"
    probe.write_text("ok")
    probe.unlink()
    cur.execute("ALTER SYSTEM SET archive_command = %s", (archive_command(),))
    cur.execute("SELECT pg_reload_conf()")
    log(f"staged archive_command = {archive_command()!r}; archive dir {ARCHIVE_DIR} writable")


def replicas(cur):
    cur.execute("SELECT coalesce(application_name, client_addr::text), state, "
                "extract(epoch FROM replay_lag) FROM pg_stat_replication")
    return [(n, s, float(l) if l is not None else None) for n, s, l in cur.fetchall()]


def busy_jobs(cur):
    cur.execute("SELECT application_name, backend_type, now() - backend_start FROM pg_stat_activity "
                "WHERE application_name ILIKE 'pg_dump%' OR application_name ILIKE 'pg_basebackup%' "
                "OR (backend_type = 'walsender' AND query ILIKE '%BASE_BACKUP%')")
    return [f"{a or b} running {str(d).split('.')[0]}" for a, b, d in cur.fetchall()]


def data_free_gb(cur):
    try:
        d = q1(cur, "SHOW data_directory")[0]
        return shutil.disk_usage(d).free / 1e9
    except Exception:  # noqa: BLE001
        return None


def wait_for(fn, timeout_s, step_s=2.0):
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(step_s)
    return False


def enable(wait_until: str | None, dry_run=False) -> int:
    """The one-shot. Never restarts unless every pre-check passes; files to claude_queue if it can't."""
    deadline = None
    if wait_until:
        hh, mm = (int(x) for x in wait_until.split(":"))
        now = datetime.now()
        deadline = now.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()
        if deadline < now.timestamp():
            deadline += 86400
    while True:
        conn = connect()
        conn.autocommit = True
        cur = conn.cursor()
        st = archiver_stats(cur)
        if st["archive_mode"] == "on" and is_enabled(cur):
            log("already enabled — nothing to do")
            return 0
        ok, reasons = precheck_restart(replicas(cur), busy_jobs(cur),
                                       q1(cur, "SELECT pg_is_in_recovery()")[0], data_free_gb(cur))
        if ok:
            break
        log("pre-check not ready: " + "; ".join(reasons))
        conn.close()
        if deadline is None or time.time() + 300 > deadline:
            return give_up("pre-checks never passed: " + "; ".join(reasons))
        time.sleep(300)
    stage(cur)
    if file_sha(LOCAL_CMD) != file_sha(REPO_CMD):
        return give_up("installed archive_command differs from repo after install")
    if dry_run:
        log("dry-run: pre-checks pass; would ALTER SYSTEM archive_mode=on and restart " + PG_UNIT)
        return 0
    cur.execute("ALTER SYSTEM SET archive_mode = 'on'")
    conn.close()
    t0 = time.time()
    log(f"restarting {PG_UNIT} …")
    r = subprocess.run(["sudo", "-n", "systemctl", "restart", PG_UNIT], capture_output=True, text=True, timeout=400)
    if r.returncode != 0:
        log(f"restart failed rc={r.returncode}: {r.stderr.strip()[:300]}")
    up = wait_for(lambda: connect().close() is None, 300)
    down_s = time.time() - t0
    if not up:
        notify("Postgres primary did not come back after WAL-archiving restart",
               f"{PG_UNIT} not accepting connections {down_s:.0f}s after restart. Recovery: "
               "ALTER SYSTEM RESET archive_mode is NOT needed to start; check journalctl -u " + PG_UNIT,
               "critical", "restart")
        return give_up(f"primary not back within 300s of restart (rc={r.returncode})", priority=1)
    log(f"primary back in {down_s:.0f}s")
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()
    if q1(cur, "SHOW archive_mode")[0] != "on":
        return give_up("archive_mode still off after restart")
    seg = q1(cur, "SELECT pg_walfile_name(pg_current_wal_lsn())")[0]
    cur.execute("SELECT pg_switch_wal()")
    archived = wait_for(lambda: (ARCHIVE_DIR / f"{seg}.zst").exists(), 180, 3)
    st = archiver_stats(cur)
    if not archived:
        return give_up(f"segment {seg} not archived within 180s; archiver: {st}")
    r = wait_for(lambda: len([x for x in replicas(cur) if x[1] == "streaming"]) >= MIN_REPLICAS, 180, 5)
    set_config(cur, "enabled", True)
    set_config(cur, "enabled_at", datetime.now(timezone.utc).isoformat())
    log(f"WAL archiving ON: {seg} archived; replicas streaming={r}; downtime {down_s:.0f}s")
    notify("WAL archiving enabled on the Postgres primary",
           f"archive_mode=on after a {down_s:.0f}s restart; {seg} archived to the NAS; "
           f"replicas re-streaming: {r}. First base backup starting now.", "info", "enabled")
    conn.close()
    return basebackup(trigger="enable")


def give_up(reason, priority=3) -> int:
    log("GIVE UP: " + reason)
    try:
        conn = connect()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("INSERT INTO claude_sessions (session_id, status) VALUES ('nova-wal-archive', 'active') "
                    "ON CONFLICT (session_id) DO NOTHING")
        desc = "[wal-archive] enabling WAL archiving on the primary did not complete"
        cur.execute("INSERT INTO claude_queue (session_id, status, priority, description, context) "
                    "SELECT 'nova-wal-archive', 'queued', %s, %s, %s WHERE NOT EXISTS (SELECT 1 FROM claude_queue "
                    "WHERE description = %s AND status IN ('queued', 'pending', 'in_progress'))",
                    (priority, desc, reason, desc))
        conn.close()
    except Exception as e:  # noqa: BLE001
        log(f"could not file claude_queue item: {e}")
    notify("WAL archiving enable did not complete", reason, "warning", "enable")
    return 1


def basebackup(trigger="schedule") -> int:
    """pg_basebackup to NAS (tar + zstd, WAL streamed so each backup is self-contained)."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    lockf = open(LOCK, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another base backup is running — skipping")
        return 0
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BASE_DIR / f"base_{ts}"
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()
    from nova_pg_schema import safe_ddl
    safe_ddl(cur, SCHEMA)
    rid = q1(cur, "INSERT INTO pg_basebackups (host, path, trigger) VALUES (%s, %s, %s) RETURNING id",
             (socket.gethostname(), str(dest), trigger))[0]
    log(f"base backup -> {dest}")
    t0 = time.time()
    r = subprocess.run([pg_basebackup_bin(), "-h", PG_HOST, "-p", PG_PORT, "-U", "kochj", "-w",
                        "-D", str(dest), "-Ft", "-X", "stream", "--compress=client-zstd:3",
                        "--checkpoint=fast", "--label", f"nova-{ts}", "-v"],
                       capture_output=True, text=True, timeout=8 * 3600)
    lsn, tli = parse_basebackup_stderr(r.stderr)
    ok = r.returncode == 0 and (dest / "backup_manifest").exists() and lsn is not None
    size = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file()) if dest.exists() else 0
    err = None if ok else (r.stderr or "").strip()[-600:]
    cur.execute("UPDATE pg_basebackups SET finished_at = now(), bytes = %s, start_lsn = %s, start_wal = %s, "
                "timeline = %s, ok = %s, error = %s WHERE id = %s",
                (size, lsn, lsn_to_segment(lsn, tli) if lsn else None, tli, ok, err, rid))
    conn.close()
    if ok:
        (dest / "NOVA_OK").write_text(json.dumps({"start_lsn": lsn, "timeline": tli, "bytes": size}))
        log(f"base backup OK: {size / 1e9:.1f} GB in {(time.time() - t0) / 60:.0f} min, start {lsn}")
        return 0
    log(f"base backup FAILED rc={r.returncode}: {err}")
    notify("Postgres base backup FAILED", f"{dest}: {err}", "warning", "basebackup")
    return 1


def kept_bases():
    """Successful base backups on the NAS, newest first: [(path, start_wal)]."""
    out = []
    if not BASE_DIR.is_dir():
        return out
    for p in sorted(BASE_DIR.glob("base_*"), reverse=True):
        try:
            meta = json.loads((p / "NOVA_OK").read_text())
            out.append((p, lsn_to_segment(meta["start_lsn"], int(meta["timeline"]))))
        except (OSError, ValueError, KeyError):
            continue
    return out


def cleanup(dry_run=False) -> int:
    bases = kept_bases()
    keep, old = bases[:KEEP_BASE], bases[KEEP_BASE:]
    oldest = keep[-1][1] if keep else None
    for p, _ in old:
        log(f"{'would remove' if dry_run else 'removing'} old base backup {p.name}")
        if not dry_run:
            shutil.rmtree(p, ignore_errors=True)
    # half-finished base dirs (no NOVA_OK) older than a day
    for p in BASE_DIR.glob("base_*") if BASE_DIR.is_dir() else []:
        if not (p / "NOVA_OK").exists() and time.time() - p.stat().st_mtime > 86400:
            log(f"{'would remove' if dry_run else 'removing'} failed base backup {p.name}")
            if not dry_run:
                shutil.rmtree(p, ignore_errors=True)
    names = [p.name for p in ARCHIVE_DIR.iterdir()] if ARCHIVE_DIR.is_dir() else []
    mtimes = {} if oldest else {n: (ARCHIVE_DIR / n).stat().st_mtime for n in names}
    doomed = wal_to_delete(names, oldest, time.time(), mtimes)
    log(f"WAL archive: {len(names)} files, {len(doomed)} older than {oldest or 'retention window'}")
    if not dry_run:
        for n in doomed:
            try:
                (ARCHIVE_DIR / n).unlink()
            except OSError as e:
                log(f"could not remove {n}: {e}")
    return 0


def monitor() -> int:
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()
    st = archiver_stats(cur)
    enabled = is_enabled(cur)
    now = time.time()
    level, msg = archiver_state(st, now, enabled)
    log(f"{level}: {msg}")
    problems = 0
    if level in ("warning", "critical"):
        notify(f"WAL archiving {'STALLED' if level == 'critical' else 'degraded'}", msg, level, "stall")
        problems += 1
    local = on_primary_host()
    if enabled and not local:
        log(f"not on the primary ({PG_HOST}): archive_command drift + gap-log checks skipped (they are local files there)")
    if enabled and local:
        if file_sha(LOCAL_CMD) != file_sha(REPO_CMD):
            changed = install()
            log(f"installed archive_command drifted from repo — {'re-installed' if changed else 'check'}")
        bases = kept_bases()
        newest_base = None
        r = q1(cur, "SELECT extract(epoch FROM max(finished_at)) FROM pg_basebackups WHERE ok") \
            if q1(cur, "SELECT to_regclass('public.pg_basebackups') IS NOT NULL")[0] else None
        if r and r[0]:
            newest_base = float(r[0])
        try:
            lines = GAP_LOG.read_text().splitlines()
        except OSError:
            lines = []
        gaps = gaps_since(lines, newest_base)
        if gaps:
            notify("WAL archive chain broken — taking a fresh base backup",
                   f"{len(gaps)} segment(s) dropped by the safety valve since the last base backup "
                   f"(first {gaps[0][1]}). Point-in-time recovery is limited to before the gap until "
                   "the new base backup completes.", "critical", "gap")
            problems += 1
            if level == "ok":       # archiving healthy again: close the loop ourselves
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--basebackup",
                                  "--trigger", "gap"], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                log("started gap-repair base backup in the background")
        if not bases and level == "ok":
            log("no successful base backup yet — WAL alone cannot be replayed")
    conn.close()
    return 1 if problems else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nova WAL archiving (Tier-0 RPO)")
    g = ap.add_mutually_exclusive_group(required=True)
    for f in ("install", "stage", "enable", "basebackup", "cleanup", "monitor"):
        g.add_argument(f"--{f}", action="store_true")
    ap.add_argument("--wait-until", help="--enable: keep re-checking pre-conditions until HH:MM local")
    ap.add_argument("--trigger", default="schedule")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.install:
        install()
        return 0
    if a.stage:
        conn = connect()
        conn.autocommit = True
        stage(conn.cursor())
        return 0
    if a.enable:
        return enable(a.wait_until, a.dry_run)
    if a.basebackup:
        return basebackup(a.trigger)
    if a.cleanup:
        return cleanup(a.dry_run)
    return monitor()


if __name__ == "__main__":
    sys.exit(main())
