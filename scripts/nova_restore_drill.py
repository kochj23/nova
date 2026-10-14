#!/usr/bin/env python3
"""nova_restore_drill.py — weekly Postgres RECOVERY drill (BSRS ch.9 "Design for Recovery";
SRE book ch.26 "Data Integrity"): a backup is only a hope until it has been restored.

What it does, every run:
  1. Picks the newest COMPLETE nova_ops pg_dump (directory format, from nova_pg_backup.sh on the
     NAS: /mnt/nas|/Volumes/nas/backups/postgres/nova_ops_YYYYMMDD_HHMMSS/).
  2. Starts a THROWAWAY postgres:17 container on nova-core2 (.86, 786 GB free, not a DB node),
     reached with DOCKER_HOST=ssh://kochj@192.168.1.86 — the primary (.2:5434) only ever sees a
     handful of read-only count(*) queries, and nothing is written to the replicated cluster
     except one row in restore_drills. The container has no network listener (unix socket +
     trust inside the container only), fsync off, and --rm so its anonymous volume is deleted.
  3. Restores the dump into database nova_ops_restore_drill (pg_restore -j 8, full: schema,
     data, indexes, constraints). --skip-big-mb N leaves out the TABLE DATA of bulk tables whose
     compressed data file exceeds N MB (key tables are always restored) for a faster drill.
  4. Verifies: pg_restore succeeded, key tables exist (claude_memories, agent_docs,
     scheduler_runs, telemetry.events, incidents), row counts within a sane ratio of live,
     newest data timestamp in the restored copy (RPO = now - newest data), elapsed (RTO).
  5. nova_memories (2.2M vectors): CHEAP check only — newest dump exists, size, age, and
     `pg_restore --list` reads its TOC. Never a full restore.
  6. Records the result in nova_ops.restore_drills, drops the drill DB, removes the container,
     and alerts via nova_notify (warning on failure or RPO > 26 h).

Runs from scheduler-core on nova-core (.2) — needs the NAS at /mnt/nas, docker CLI, and key-based
ssh to .86. Usage: nova_restore_drill.py [--skip-big-mb N] [--dry-run] [--no-notify]
Exit 0 = PASS, 1 = FAIL.

Written by Jordan Koch (via Claude). 2026-10-13.
"""
import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

# ── Config ────────────────────────────────────────────────────────────────────
DUMP_DIR_CANDIDATES = ("/mnt/nas/backups/postgres", "/Volumes/nas/backups/postgres",
                       "/Volumes/nas-1/backups/postgres")
DOCKER_HOST = os.environ.get("NOVA_DRILL_DOCKER_HOST", "ssh://kochj@192.168.1.86")
REMOTE_DUMP_DIR = os.environ.get("NOVA_DRILL_REMOTE_DUMP_DIR", "/mnt/nas/backups/postgres")
IMAGE = os.environ.get("NOVA_DRILL_IMAGE", "postgres:17")
CONTAINER = "nova-restore-drill"
DRILL_DB = "nova_ops_restore_drill"
SOURCE_DB = "nova_ops"
RPO_MAX_H = 26.0                  # nightly 02:00 dump + slack; older = a missed night
RTO_MAX_S = 4 * 3600              # a nova_ops restore slower than this is itself an incident
RATIO_LO, RATIO_HI = 0.80, 1.05   # restored/live — live grows after the dump; retention prunes
RATIO_ABS_SLACK = 50              # small tables: an absolute diff this small is always fine
SETTLE_S = 15 * 60                # a dump whose newest file is younger than this may be mid-rsync
MEM_MAX_AGE_H = 26.0
DUMP_TZ = ZoneInfo("America/Los_Angeles")   # nova_pg_backup.sh names dumps in local (Pacific) time

# Key tables: (qualified name, SQL expression for the newest row as epoch seconds or None)
KEY_TABLES = [
    ("public.claude_memories", "extract(epoch from max(greatest(created_at, updated_at)))"),
    ("public.agent_docs", "max(updated_at)/1000.0"),
    ("public.scheduler_runs", "max(started_at)/1000.0"),
    ("telemetry.events", "extract(epoch from max(ts))"),
    ("public.incidents", "extract(epoch from max(started_at))"),
]
FRESHNESS_TABLES = ("public.scheduler_runs", "telemetry.events")   # written every minute

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS public.restore_drills (
    id               bigserial PRIMARY KEY,
    ran_at           timestamptz NOT NULL DEFAULT now(),
    host             text,
    source_db        text NOT NULL,
    backup_path      text,
    backup_taken_at  timestamptz,
    backup_bytes     bigint,
    newest_data_at   timestamptz,
    rpo_s            double precision,
    rto_s            double precision,
    rto_full_est_s   double precision,
    mode             text,
    tables_data_skipped int,
    restore_errors   int,
    checks           jsonb,
    memories_check   jsonb,
    passed           boolean NOT NULL,
    verdict          text
)"""


def log(msg):
    print(f"[restore-drill {datetime.now():%H:%M:%S}] {msg}", flush=True)


# ── Pure helpers (unit-tested) ────────────────────────────────────────────────
_DUMP_RE = re.compile(r"^(?P<db>[a-z0-9_]+?)_(?P<ts>\d{8}_\d{6})$")


def parse_dump_name(name):
    """'nova_ops_20261013_045521' -> ('nova_ops', aware datetime in DUMP_TZ) or None."""
    m = _DUMP_RE.match(name)
    if not m:
        return None
    try:
        return m.group("db"), datetime.strptime(m.group("ts"), "%Y%m%d_%H%M%S").replace(tzinfo=DUMP_TZ)
    except ValueError:
        return None


def rpo_seconds(now_epoch, newest_epoch):
    """Data-loss window if we had to restore right now: now - newest row in the backup."""
    if newest_epoch is None:
        return None
    return max(0.0, float(now_epoch) - float(newest_epoch))


def rto_full_estimate(elapsed_s, bytes_restored, bytes_total):
    """Extrapolate a partial restore's wall time to the whole archive by compressed bytes."""
    if not elapsed_s or not bytes_restored or not bytes_total:
        return None
    return float(elapsed_s) * max(1.0, bytes_total / bytes_restored)


def ratio_ok(restored, live, lo=RATIO_LO, hi=RATIO_HI, abs_slack=RATIO_ABS_SLACK):
    """Is a restored row count sane against the live count? None = unknown -> not ok."""
    if restored is None or live is None:
        return False
    if live > 0 and restored == 0:
        return False
    if abs(restored - live) <= abs_slack:
        return True
    if live == 0:
        return False
    return lo <= restored / live <= hi


def select_toc(toc_lines, data_sizes, skip_big_bytes, keep_tables):
    """Filter a `pg_restore -l` listing. Drops TABLE DATA entries whose data file is larger than
    skip_big_bytes (None = keep all) unless the table is in keep_tables ('schema.table').
    Returns (kept_lines, skipped_tables, bytes_kept, bytes_total)."""
    kept, skipped, b_kept, b_total = [], [], 0, 0
    for line in toc_lines:
        m = re.match(r"^(\d+);\s+\d+\s+\d+\s+TABLE DATA\s+(\S+)\s+(\S+)\s", line)
        if not m:
            kept.append(line)
            continue
        size = data_sizes.get(m.group(1), 0)
        b_total += size
        qual = f"{m.group(2)}.{m.group(3)}"
        if skip_big_bytes is not None and size > skip_big_bytes and qual not in keep_tables:
            skipped.append(qual)
            continue
        kept.append(line)
        b_kept += size
    return kept, skipped, b_kept, b_total


def verdict(restore_ok, checks, rpo_s, rto_s, rpo_max_s=RPO_MAX_H * 3600, rto_max_s=RTO_MAX_S):
    """Overall pass/fail + human reasons. checks = {table: {"exists", "ratio_ok", ...}}."""
    reasons = []
    if not restore_ok:
        reasons.append("pg_restore failed")
    for t, c in sorted(checks.items()):
        if not c.get("exists"):
            reasons.append(f"{t} missing from restore")
        elif not c.get("ratio_ok"):
            reasons.append(f"{t} row count off: restored={c.get('restored')} live={c.get('live')}")
    if rpo_s is None:
        reasons.append("RPO unknown (no newest-row timestamp)")
    elif rpo_s > rpo_max_s:
        reasons.append(f"RPO {rpo_s / 3600:.1f}h > {rpo_max_s / 3600:.0f}h")
    if rto_s is not None and rto_s > rto_max_s:
        reasons.append(f"RTO {rto_s / 60:.0f}m > {rto_max_s / 60:.0f}m")
    return (not reasons), reasons


def is_settled(newest_mtime, now_epoch, settle_s=SETTLE_S):
    return newest_mtime is not None and (now_epoch - newest_mtime) >= settle_s


# ── Side-effecting helpers ────────────────────────────────────────────────────
def sh(args, timeout=None, env=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)


def _docker_env():
    env = dict(os.environ)
    if DOCKER_HOST:
        env["DOCKER_HOST"] = DOCKER_HOST
    return env


def docker(*args, timeout=None):
    return sh(["docker", *args], timeout=timeout, env=_docker_env())


def dexec(*args, timeout=None):
    """Run a client command inside the drill container as postgres over the unix socket."""
    return docker("exec", "-u", "postgres", "-e", "PGHOST=/var/run/postgresql",
                  "-e", "PGUSER=postgres", CONTAINER, *args, timeout=timeout)


def dpsql(sql, db=DRILL_DB, timeout=600):
    r = dexec("psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", db, "-tA", "-c", sql, timeout=timeout)
    return r.stdout.strip() if r.returncode == 0 else None


def dump_base():
    for c in DUMP_DIR_CANDIDATES:
        p = Path(c)
        try:
            if p.is_dir():
                return p
        except OSError:
            continue
    return None


def newest_complete_dump(base, db, now_epoch):
    """Newest <db>_YYYYMMDD_HHMMSS dir that has toc.dat and has stopped changing."""
    cands = []
    for p in base.iterdir():
        parsed = parse_dump_name(p.name)
        if parsed and parsed[0] == db and p.is_dir():
            cands.append((parsed[1], p))
    for taken, p in sorted(cands, reverse=True):
        files = [f for f in p.iterdir() if f.is_file()]
        if not any(f.name == "toc.dat" for f in files):
            continue
        sizes = {f.name: f.stat().st_size for f in files}
        newest = max(f.stat().st_mtime for f in files)
        if is_settled(newest, now_epoch):
            return p, taken, sizes
        log(f"skipping {p.name}: still being written ({now_epoch - newest:.0f}s since last change)")
    return None, None, {}


def data_sizes_by_toc_id(file_sizes):
    """{'8721.dat.gz': n} -> {'8721': n}."""
    out = {}
    for name, n in file_sizes.items():
        m = re.match(r"^(\d+)\.dat(\..+)?$", name)
        if m:
            out[m.group(1)] = out.get(m.group(1), 0) + n
    return out


def live_counts():
    import nova_dsn
    import psycopg2
    out = {}
    with psycopg2.connect(nova_dsn.pg_dsn(SOURCE_DB)) as c, c.cursor() as cur:
        cur.execute("SET statement_timeout = '120s'")
        for t, _ in KEY_TABLES:
            try:
                cur.execute(f"SELECT count(*) FROM {t}")
                out[t] = cur.fetchone()[0]
            except Exception as e:
                c.rollback()
                cur.execute("SET statement_timeout = '120s'")
                log(f"live count {t} failed: {e}")
                out[t] = None
    return out


def record(row):
    import nova_dsn
    import psycopg2
    cols = list(row)
    with psycopg2.connect(nova_dsn.pg_dsn(SOURCE_DB)) as c, c.cursor() as cur:
        cur.execute(SCHEMA_SQL)
        cur.execute(f"INSERT INTO public.restore_drills ({', '.join(cols)}) VALUES "
                    f"({', '.join(['%s'] * len(cols))}) RETURNING id",
                    [json.dumps(row[k]) if isinstance(row[k], (dict, list)) else row[k] for k in cols])
        rid = cur.fetchone()[0]
        c.commit()
    return rid


def start_container(remote_dump_path):
    docker("rm", "-f", "-v", CONTAINER, timeout=120)        # a leftover from a crashed run
    pw = secrets.token_urlsafe(24)                     # never used: trust over the socket only
    r = docker("run", "-d", "--rm", "--name", CONTAINER,
               "--memory=12g", "--shm-size=1g",
               "-e", f"POSTGRES_PASSWORD={pw}", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
               "-v", f"{remote_dump_path}:/dump:ro",
               IMAGE,
               "-c", "listen_addresses=", "-c", "fsync=off", "-c", "synchronous_commit=off",
               "-c", "full_page_writes=off", "-c", "wal_level=minimal", "-c", "max_wal_senders=0",
               "-c", "max_wal_size=16GB", "-c", "maintenance_work_mem=1GB",
               "-c", "shared_buffers=1GB", "-c", "autovacuum=off",
               timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"docker run failed: {r.stderr.strip()[:300]}")
    for _ in range(90):
        if dexec("pg_isready", "-q").returncode == 0 and dpsql("SELECT 1", db="postgres") == "1":
            return
        time.sleep(2)
    raise RuntimeError("drill postgres never became ready")


def memories_cheap_check(base, now_epoch):
    """nova_memories: newest dump exists, size, age, TOC readable — never a full restore."""
    out = {"ok": False}
    p, taken, sizes = newest_complete_dump(base, "nova_memories", now_epoch)
    if not p:
        out["error"] = "no complete nova_memories dump found"
        return out
    age_h = (now_epoch - taken.timestamp()) / 3600
    out.update(path=p.name, bytes=sum(sizes.values()), age_h=round(age_h, 2))
    r = docker("run", "--rm", "-v", f"{REMOTE_DUMP_DIR}/{p.name}:/dump:ro", IMAGE,
               "pg_restore", "--list", "/dump", timeout=900)
    entries = [l for l in r.stdout.splitlines() if l and not l.startswith(";")]
    out["toc_entries"] = len(entries)
    out["has_memories_data"] = any(re.search(r"TABLE DATA \S+ memories ", l) for l in entries)
    out["ok"] = (r.returncode == 0 and out["has_memories_data"] and age_h <= MEM_MAX_AGE_H
                 and out["bytes"] > 1 << 30)
    if r.returncode != 0:
        out["error"] = r.stderr.strip()[:200]
    return out


# ── Main ──────────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--skip-big-mb", type=float, default=None,
                    help="skip TABLE DATA of non-key tables whose compressed file exceeds N MB")
    ap.add_argument("--dry-run", action="store_true", help="pick the dump and plan; no restore")
    ap.add_argument("--no-notify", action="store_true")
    a = ap.parse_args(argv)

    now = time.time()
    base = dump_base()
    if not base:
        return finish_fail("NAS backup dir not mounted (" + ", ".join(DUMP_DIR_CANDIDATES) + ")", a)
    dump, taken, file_sizes = newest_complete_dump(base, SOURCE_DB, now)
    if not dump:
        return finish_fail(f"no complete {SOURCE_DB} dump under {base}", a)
    backup_age_s = now - taken.timestamp()
    log(f"dump: {dump.name} ({sum(file_sizes.values()) / 1e9:.2f} GB, taken {backup_age_s / 3600:.1f}h ago)")
    skip_bytes = int(a.skip_big_mb * 1e6) if a.skip_big_mb is not None else None
    mode = "full" if skip_bytes is None else f"skip>{a.skip_big_mb:g}MB"
    if a.dry_run:
        log(f"dry-run: would restore into {DRILL_DB} on {DOCKER_HOST or 'local docker'} ({mode})")
        return 0

    t0 = time.time()
    checks, restore_ok, restore_errors, skipped, b_kept, b_total = {}, False, None, [], 0, 0
    newest = None
    mem = {}
    err_tail = ""
    try:
        start_container(f"{REMOTE_DUMP_DIR}/{dump.name}")
        log(f"container up on {DOCKER_HOST or 'local'} in {time.time() - t0:.0f}s")
        if dpsql(f"CREATE DATABASE {DRILL_DB}", db="postgres") is None:
            raise RuntimeError("CREATE DATABASE failed in drill container")
        r = dexec("pg_restore", "-l", "/dump", timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"pg_restore -l failed: {r.stderr.strip()[:200]}")
        kept, skipped, b_kept, b_total = select_toc(
            r.stdout.splitlines(), data_sizes_by_toc_id(file_sizes), skip_bytes,
            {t for t, _ in KEY_TABLES})
        listing = "\n".join(kept) + "\n"
        w = subprocess.run(["docker", "exec", "-i", "-u", "postgres", CONTAINER, "sh", "-c",
                            "cat > /tmp/drill.list"], input=listing, capture_output=True, text=True,
                           env=_docker_env(), timeout=120)
        if w.returncode != 0:
            raise RuntimeError(f"could not stage TOC list: {w.stderr.strip()[:200]}")
        log(f"restoring {mode}: {len(kept)} TOC entries, {len(skipped)} bulk tables' data skipped")
        tr = time.time()
        r = dexec("pg_restore", "-d", DRILL_DB, "-j", "8", "--no-owner", "--no-privileges",
                  "-L", "/tmp/drill.list", "/dump", timeout=6 * 3600)
        m = re.search(r"errors ignored on restore: (\d+)", r.stderr or "")
        restore_errors = int(m.group(1)) if m else (0 if r.returncode == 0 else -1)
        err_tail = "\n".join((r.stderr or "").strip().splitlines()[-6:])
        restore_ok = r.returncode == 0 and restore_errors == 0
        log(f"pg_restore rc={r.returncode} errors={restore_errors} in {time.time() - tr:.0f}s")
        if err_tail:
            log("pg_restore stderr tail:\n" + err_tail)

        live = live_counts()
        newest_vals = []
        for t, ts_expr in KEY_TABLES:
            exists = dpsql(f"SELECT to_regclass('{t}') IS NOT NULL") == "t"
            restored = int(dpsql(f"SELECT count(*) FROM {t}")) if exists else None
            nts = None
            if exists:
                v = dpsql(f"SELECT {ts_expr} FROM {t}")
                nts = float(v) if v not in (None, "") else None
            if t in FRESHNESS_TABLES and nts:
                newest_vals.append(nts)
            checks[t] = {"exists": exists, "restored": restored, "live": live.get(t),
                         "ratio_ok": ratio_ok(restored, live.get(t)),
                         "newest": datetime.fromtimestamp(nts, timezone.utc).isoformat() if nts else None}
            log(f"  {t:24} restored={restored} live={live.get(t)} ok={checks[t]['ratio_ok']}")
        newest = max(newest_vals) if newest_vals else None
        rto_s = time.time() - t0
        try:
            mem = memories_cheap_check(base, time.time())
        except Exception as e:
            mem = {"ok": False, "error": str(e)[:200]}
        log(f"nova_memories cheap check: {mem}")
        dpsql(f"DROP DATABASE IF EXISTS {DRILL_DB}", db="postgres")
    except Exception as e:
        rto_s = time.time() - t0
        err_tail = (err_tail + "\n" + str(e)).strip()
        log(f"drill error: {e}")
    finally:
        rm = docker("rm", "-f", "-v", CONTAINER, timeout=300)
        log(f"container removed (rc={rm.returncode})")

    rpo_s = rpo_seconds(now, newest)
    passed, reasons = verdict(restore_ok, checks, rpo_s, rto_s)
    if mem and not mem.get("ok"):
        reasons.append(f"nova_memories backup check failed: {mem.get('error') or mem}")
        passed = False
    if not checks and not reasons:
        reasons.append("restore never reached verification")
        passed = False
    rto_full = rto_full_estimate(rto_s, b_kept, b_total)
    vtxt = "PASS" if passed else "FAIL: " + "; ".join(reasons)
    row = dict(host=socket.gethostname(), source_db=SOURCE_DB, backup_path=str(dump),
               backup_taken_at=taken, backup_bytes=sum(file_sizes.values()),
               newest_data_at=datetime.fromtimestamp(newest, timezone.utc) if newest else None,
               rpo_s=rpo_s, rto_s=rto_s, rto_full_est_s=rto_full, mode=mode,
               tables_data_skipped=len(skipped), restore_errors=restore_errors,
               checks={"tables": checks, "skipped": skipped[:50], "stderr_tail": err_tail[-800:],
                       "backup_age_s": backup_age_s},
               memories_check=mem, passed=passed, verdict=vtxt)
    try:
        rid = record(row)
        log(f"recorded restore_drills id={rid}")
    except Exception as e:
        log(f"could not record drill result: {e}")
        reasons.append(f"result not recorded: {e}")
    rpo_txt = f"{rpo_s / 3600:.1f}h" if rpo_s is not None else "?"
    summary = (f"{SOURCE_DB} restore drill {'PASSED' if passed else 'FAILED'} — RPO {rpo_txt}, "
               f"RTO {rto_s / 60:.1f}m ({mode}), dump {dump.name}")
    log(summary + ("" if passed else " :: " + "; ".join(reasons)))
    if not a.no_notify:
        _notify(summary, "\n".join(f"• {r}" for r in reasons) or None, "info" if passed else "warning")
    return 0 if passed else 1


def _notify(title, body, level):
    try:
        from nova_notify import notify
        notify(title, body=body, level=level, category="backup", dedup_key="restore-drill")
    except Exception as e:
        log(f"notify failed: {e}")


def finish_fail(reason, a):
    log("FAIL: " + reason)
    try:
        record(dict(host=socket.gethostname(), source_db=SOURCE_DB, passed=False, verdict="FAIL: " + reason))
    except Exception as e:
        log(f"could not record drill result: {e}")
    if not a.no_notify:
        _notify(f"{SOURCE_DB} restore drill FAILED", reason, "warning")
    return 1


if __name__ == "__main__":
    sys.exit(main())
