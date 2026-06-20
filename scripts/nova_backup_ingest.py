#!/opt/homebrew/bin/python3
"""
nova_backup_ingest.py — bridge the Synology->UNAS NAS backup summary from Nova's
vector memory into a structured telemetry table for clean Grafana graphing/alerting.

The Synology backup (nova_nas_backup.sh, daily 03:30) POSTs a per-run summary into
nova_memories (source='operations', metadata.type='nas_backup'). The memory carries
structured metadata fields: rc, elapsed_s, files, bytes, errors, ok, date — plus a
human-readable text blob. Graphing/alerting against jsonb-in-vector-DB is awkward, so
this collector reads the LATEST nas_backup memory and UPSERTs one row per (ts, job)
into telemetry.backup_runs in nova_ops (the Grafana 'nova-ops-pg' datasource).

Robustness:
  - Prefers structured metadata fields. Falls back to parsing the text blob for
    older memories written before the script emitted structured fields.
  - UPSERT keyed on (ts, job): re-running is idempotent; a re-POST for the same run
    (same date) overwrites rather than duplicating.
  - All work is wrapped — a transient failure writes nothing rather than crashing.

  python3 nova_backup_ingest.py            # read latest memory + upsert
  python3 nova_backup_ingest.py --dry-run  # read + print, no write
  python3 nova_backup_ingest.py --all      # backfill: ingest all nas_backup memories

Runs on .6 via nova_scheduler (config/scheduler.yaml: backup_ingest, every 1h).
Written by Jordan Koch.
"""

import argparse
import re
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

OPS_DSN = "host=localhost dbname=nova_ops user=kochj"
MEM_DSN = "host=localhost dbname=nova_memories user=kochj"
JOB = "nas_backup"

DDL = """
CREATE SCHEMA IF NOT EXISTS telemetry;
CREATE TABLE IF NOT EXISTS telemetry.backup_runs (
    ts        timestamptz NOT NULL,
    job       text        NOT NULL,
    rc        integer,
    elapsed_s integer,
    files     bigint,
    bytes     bigint,
    errors    integer,
    ok        boolean,
    mem_id    text,
    PRIMARY KEY (ts, job)
);
CREATE INDEX IF NOT EXISTS idx_backup_runs_job_ts ON telemetry.backup_runs (job, ts DESC);
CREATE INDEX IF NOT EXISTS idx_backup_runs_ts ON telemetry.backup_runs (ts DESC);
"""

UPSERT = """
INSERT INTO telemetry.backup_runs (ts, job, rc, elapsed_s, files, bytes, errors, ok, mem_id)
VALUES (%(ts)s, %(job)s, %(rc)s, %(elapsed_s)s, %(files)s, %(bytes)s, %(errors)s, %(ok)s, %(mem_id)s)
ON CONFLICT (ts, job) DO UPDATE SET
    rc        = EXCLUDED.rc,
    elapsed_s = EXCLUDED.elapsed_s,
    files     = EXCLUDED.files,
    bytes     = EXCLUDED.bytes,
    errors    = EXCLUDED.errors,
    ok        = EXCLUDED.ok,
    mem_id    = EXCLUDED.mem_id;
"""


def log(msg):
    print(f"[backup_ingest {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _int(v):
    """Coerce a possibly-string/None/human value to int, or None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    digits = re.sub(r"[^0-9]", "", str(v))
    return int(digits) if digits else None


def _parse_date(meta, created_at):
    """Prefer metadata.date ('YYYY-MM-DD HH:MM:SS'); fall back to row created_at."""
    d = meta.get("date") if isinstance(meta, dict) else None
    if d:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(d.strip(), fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return created_at if created_at else datetime.now(timezone.utc)


def _from_text(text, key):
    """Fallback parse for legacy memories without structured metadata.
    The text holds bracketed per-share blocks, e.g.
      [nas: rc=0, 12s, files=100, transferred=5 (1.23M), ..., errors=0]
    Sums files-transferred and errors across blocks; bytes is best-effort."""
    if not text:
        return None
    if key == "errors":
        vals = [int(x) for x in re.findall(r"errors=(\d+)", text)]
        return sum(vals) if vals else None
    if key == "files":
        vals = [int(x) for x in re.findall(r"transferred=(\d+)", text)]
        return sum(vals) if vals else None
    return None


def fetch_memories(all_rows):
    """Return list of (id, created_at, metadata, text), newest first."""
    conn = psycopg2.connect(MEM_DSN)
    try:
        with conn.cursor() as cur:
            limit = "" if all_rows else "LIMIT 1"
            cur.execute(
                f"""
                SELECT id, created_at, metadata, text
                FROM memories
                WHERE source = 'operations' AND metadata->>'type' = 'nas_backup'
                ORDER BY created_at DESC
                {limit}
                """
            )
            return cur.fetchall()
    finally:
        conn.close()


def to_row(mem_id, created_at, meta, text):
    meta = meta or {}
    rc = _int(meta.get("rc"))
    elapsed = _int(meta.get("elapsed_s"))
    files = _int(meta.get("files"))
    if files is None:
        files = _from_text(text, "files")
    nbytes = _int(meta.get("bytes"))
    errors = _int(meta.get("errors"))
    if errors is None:
        errors = _from_text(text, "errors")

    ok = meta.get("ok")
    if not isinstance(ok, bool):
        # Derive: ok iff rc==0 and no errors (matches the script's own logic).
        ok = (rc == 0) and ((errors or 0) == 0)

    return {
        "ts": _parse_date(meta, created_at),
        "job": JOB,
        "rc": rc,
        "elapsed_s": elapsed,
        "files": files,
        "bytes": nbytes,
        "errors": errors,
        "ok": ok,
        "mem_id": str(mem_id),
    }


def write_rows(rows, dry_run):
    if not rows:
        log("no nas_backup memories found — nothing to ingest")
        return 0
    for r in rows:
        log(f"  {r['ts']} job={r['job']} rc={r['rc']} elapsed={r['elapsed_s']}s "
            f"files={r['files']} bytes={r['bytes']} errors={r['errors']} ok={r['ok']}")
    if dry_run:
        log(f"DRY RUN — would upsert {len(rows)} row(s)")
        return 0
    conn = psycopg2.connect(OPS_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            psycopg2.extras.execute_batch(cur, UPSERT, rows)
        return len(rows)
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="Ingest NAS backup memory -> telemetry.backup_runs")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true", help="backfill all nas_backup memories")
    args = ap.parse_args()

    try:
        mems = fetch_memories(args.all)
    except Exception as e:
        log(f"memory query failed: {e}")
        return
    try:
        rows = [to_row(*m) for m in mems]
        n = write_rows(rows, args.dry_run)
        if not args.dry_run:
            log(f"upserted {n} row(s) into telemetry.backup_runs")
    except Exception as e:
        log(f"ingest failed: {e}")


if __name__ == "__main__":
    main()
