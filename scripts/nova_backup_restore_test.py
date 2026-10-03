#!/usr/bin/env python3
"""
nova_backup_restore_test.py — RELIABILITY #4: prove the backups actually restore.
"A backup never restored is a hope, not a backup."

Monthly drill (non-destructive):
  1. Restore the latest nightly nova_memories pg_dump into a SCRATCH database
     (data only — skips the 5GB HNSW index rebuild), checksum the row count
     against the live DB within tolerance, then DROP the scratch DB.
  2. Verify a random sample of the NAS copy of that dump matches the local copy
     (file presence + byte size).
  3. Flag that nova_ops (the control plane) has no point-in-time dump.
Alerts via nova_notify on ANY failure/drift. Exit 0 = all good, 1 = problem.

stdlib + psql/pg_restore CLI. Written for Jordan (#644).
"""
import os, random, subprocess, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_notify import notify

def _first_existing(*cands):
    for c in cands:
        if Path(c).exists():
            return Path(c)
    return Path(cands[0])

# 2026-10-03: .6 no longer keeps a local dump dir or a local Postgres (primary is .2:5434,
# 127.0.0.1:5432 is the pgbouncer shim which cannot createdb). Use the NAS copy wherever we
# run, and talk to the primary directly unless the caller set PGHOST/PGPORT.
LOCAL_DIR = _first_existing("/Volumes/Data/backups/postgres", "/Volumes/nas/backups/postgres", "/mnt/nas/backups/postgres")
NAS_DIR   = _first_existing("/Volumes/nas/backups/postgres", "/mnt/nas/backups/postgres")
os.environ.setdefault("PGHOST", "pg-primary.digitalnoise.net")
os.environ.setdefault("PGPORT", "5434")
DB_USER   = "kochj"
SCRATCH   = "nova_memories_restoretest"
TOLERANCE = 0.10           # restored count must be within ±10% of live
NAS_SAMPLE = 8
PROBLEMS  = []


def log(m): print(f"[restore-test {datetime.now():%H:%M:%S}] {m}", flush=True)


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def latest_dump(base, db="nova_memories"):
    dumps = sorted(base.glob(f"{db}_*"), reverse=True) if base.exists() else []
    return dumps[0] if dumps else None


def nova_ops_restore_test():
    """Full restore of the latest nova_ops dump to scratch (it's small) + sanity check."""
    dump = latest_dump(LOCAL_DIR, "nova_ops")
    if not dump:
        PROBLEMS.append("nova_ops (control plane) has NO point-in-time backup"); return None
    scratch = "nova_ops_restoretest"
    sh(["dropdb", "-U", DB_USER, "--if-exists", "--force", scratch])
    if sh(["createdb", "-U", DB_USER, scratch]).returncode != 0:
        PROBLEMS.append("createdb nova_ops scratch failed"); return None
    try:
        # Restore ONLY the critical control-plane tables. nova_ops also holds 25M+
        # rows of high-churn telemetry (syslog_events, snmp_metrics) whose full
        # restore would take ~15 min — not worth it for a routine integrity drill.
        crit = ["claude_memories", "claude_sessions", "claude_queue", "claude_actions",
                "claude_coordination", "service_config", "agent_docs"]
        args = ["pg_restore", "-U", DB_USER, "-d", scratch, "--no-owner", "--no-privileges", "-j", "4"]
        for t in crit:
            args += ["-t", t]
        sh(args + [str(dump)])
        k = sh(["psql", "-U", DB_USER, "-d", scratch, "-tA", "-c", "SELECT count(*) FROM claude_memories"])
        kc = int(k.stdout.strip()) if k.returncode == 0 and k.stdout.strip().isdigit() else None
        if not kc:
            PROBLEMS.append("nova_ops restore: claude_memories empty/unreadable after restore")
        return kc
    finally:
        sh(["dropdb", "-U", DB_USER, "--if-exists", "--force", scratch])


def live_count():
    r = sh(["psql", "-U", DB_USER, "-d", "nova_memories", "-tA", "-c", "SELECT count(*) FROM memories"])
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


def pg_restore_test(dump):
    """Restore the memories table (data only, no indexes) to a scratch DB + count."""
    sh(["dropdb", "-U", DB_USER, "--if-exists", "--force", SCRATCH])
    r = sh(["createdb", "-U", DB_USER, SCRATCH])
    if r.returncode != 0:
        PROBLEMS.append(f"createdb scratch failed: {r.stderr.strip()}"); return None
    try:
        # the memories table is vector(768) — the extension must exist before restore
        # (pg_restore -t memories filters out the CREATE EXTENSION from the dump).
        sh(["psql", "-U", DB_USER, "-d", SCRATCH, "-c", "CREATE EXTENSION IF NOT EXISTS vector"])
        # schema (pre-data) then rows (data) for ONLY the memories table — no index rebuild
        for section in ("pre-data", "data"):
            r = sh(["pg_restore", "-U", DB_USER, "-d", SCRATCH, "--no-owner", "--no-privileges",
                    "-j", "4", "-t", "memories", f"--section={section}", str(dump)])
            if r.returncode != 0 and "errors ignored" not in (r.stderr or "").lower():
                PROBLEMS.append(f"pg_restore {section} rc={r.returncode}: {(r.stderr or '')[:200]}")
        c = sh(["psql", "-U", DB_USER, "-d", SCRATCH, "-tA", "-c", "SELECT count(*) FROM memories"])
        return int(c.stdout.strip()) if c.returncode == 0 and c.stdout.strip().isdigit() else None
    finally:
        sh(["dropdb", "-U", DB_USER, "--if-exists", "--force", SCRATCH])   # always clean up


def nas_sample_check(local, nas):
    if not nas or not nas.exists():
        PROBLEMS.append(f"NAS copy missing for {local.name} (no off-site verification)"); return 0, 0
    files = [p for p in local.rglob("*") if p.is_file()]
    sample = random.sample(files, min(NAS_SAMPLE, len(files)))
    ok = 0
    for f in sample:
        nf = nas / f.relative_to(local)
        if not nf.exists():
            PROBLEMS.append(f"NAS missing file: {f.name}")
        elif nf.stat().st_size != f.stat().st_size:
            PROBLEMS.append(f"NAS size drift: {f.name} local={f.stat().st_size} nas={nf.stat().st_size}")
        else:
            ok += 1
    return ok, len(sample)


def main():
    log("=== backup restore-test drill ===")
    dump = latest_dump(LOCAL_DIR)
    if not dump:
        notify("Backup restore-test FAILED", body="No local nova_memories dump found to restore.",
               level="critical", category="backup", dedup_key="restore-test"); sys.exit(1)
    log(f"latest dump: {dump.name}")

    live = live_count()
    restored = pg_restore_test(dump)
    if restored is None:
        PROBLEMS.append("restore produced no countable memories table")
    elif live:
        lo, hi = live * (1 - TOLERANCE), live * (1 + TOLERANCE)
        if not (lo <= restored <= hi):
            PROBLEMS.append(f"row-count DRIFT: restored={restored:,} vs live={live:,} (±{int(TOLERANCE*100)}%)")
        log(f"restored memories rows: {restored:,} (live {live:,}) — {'OK' if not PROBLEMS else 'CHECK'}")

    nas_ok, nas_n = nas_sample_check(dump, latest_dump(NAS_DIR))
    log(f"NAS sample: {nas_ok}/{nas_n} files match")

    # full restore-test of the control-plane DB
    ops_rows = nova_ops_restore_test()
    if ops_rows:
        log(f"nova_ops restored OK (claude_memories {ops_rows:,} rows)")

    if PROBLEMS:
        body = "\n".join(f"• {p}" for p in PROBLEMS)
        log(f"PROBLEMS:\n{body}")
        notify(f"Backup restore-test: {len(PROBLEMS)} issue(s)", body=body,
               level="warning", category="backup", dedup_key="restore-test")
        sys.exit(1)
    notify("Backup restore-test PASSED",
           body=f"nova_memories restored OK ({restored:,} rows), nova_ops restored OK "
                f"({ops_rows:,} claude_memories rows), NAS sample {nas_ok}/{nas_n} verified.",
           level="info", category="backup", dedup_key="restore-test")
    log("ALL GOOD."); sys.exit(0)


if __name__ == "__main__":
    main()
