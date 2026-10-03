#!/usr/bin/env python3
"""Nova data-liveness checks — verify subsystems PRODUCE correct output, not just that a
process is alive. Motivated by 3 silent failures on 2026-07-06 (pgvector gone, emlx dead
path, meeting dupes) that only Jordan noticed. See agent_docs `architecture-principles` #1.

Design:
  * Declarative registry of checks; each returns (ok: bool, message: str).
  * A DB/query/socket error is itself a signal — every check is fail-closed (an exception
    means "cannot verify" -> not-ok), never silent.
  * run_due_checks() honors per-check intervals (call it every Big Brother sweep).
  * Standalone: `python3 nova_data_checks.py` runs ALL checks once and prints PASS/FAIL
    (the runnable self-check) — no alerts, exit 1 if any fail.

Big Brother integration (in its sweep):
    from nova_data_checks import run_due_checks
    for r in run_due_checks():
        iid = f"data:{r.id}"
        if r.ok:
            was, suffix = _resolve_escalation(iid)
            if was: _notify(f":white_check_mark: {r.id}{suffix}")
        else:
            send, suffix = should_notify(iid, r.severity)
            if send: _notify(r.message + suffix, is_critical=(r.severity == "critical"))
"""
import os
import time
import json
import glob
import socket
import subprocess
import urllib.request
from collections import namedtuple

import psycopg2

# ── Config (all thresholds/intervals in one place — declarative) ───────────────
PG_HOST            = "127.0.0.1"
PG_USER           = "kochj"
MEMORY_SERVER_URL  = "http://127.0.0.1:18790/health"
MQTT_HOST, MQTT_PORT = "192.168.1.6", 1883
JOURNAL_REPO       = os.path.expanduser("~/nova-journal")
BACKUP_DIRS        = ["/Volumes/Data/backups/postgres", "/Volumes/nas/backups/postgres",
                      "/mnt/nas/backups/postgres"]   # 2026-10-03: where nova_pg_backup.sh actually writes

EMAIL_MAX_AGE_S    = 24 * 3600      # email_archive must gain a memory within 24h
ENERGY_MAX_AGE_S   = 15 * 60        # energy_readings must get a row within 15 min
ARTICLE_MAX_AGE_S  = 36 * 3600      # a journal commit within ~36h
BACKUP_MAX_AGE_S   = 48 * 3600      # a DB backup within 48h
INGEST_WINDOW      = "24 hours"     # ingest_jobs must store > 0 in this window

CheckResult = namedtuple("CheckResult", "id ok severity message")


def _conn(dbname):
    return psycopg2.connect(f"dbname={dbname} user={PG_USER} host={PG_HOST}", connect_timeout=5)


def _scalar(dbname, sql):
    with _conn(dbname) as c, c.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


# ── Individual checks: each returns (ok, message) ──────────────────────────────

def _check_vector():
    """pgvector must be loadable and the memories table queryable."""
    try:
        _scalar("nova_memories", "SELECT 1 FROM memories LIMIT 1")
        return True, ""
    except Exception as e:
        return False, f":rotating_light: VECTOR STORE DOWN — nova_memories unqueryable (pgvector?): {str(e)[:120]}"


def _check_email():
    """email_archive must have gained a memory recently (catches a dead ingest path)."""
    try:
        age = _scalar("nova_memories",
                      "SELECT EXTRACT(EPOCH FROM (now() - max(created_at))) "
                      "FROM memories WHERE source IN ('email','email_archive')")   # 2026-10-03: mail agent stores source='email'
        if age is None:
            return False, ":x: email ingestion: no email_archive memories exist"
        if age > EMAIL_MAX_AGE_S:
            return False, f":x: email ingestion STALE — newest email_archive memory {age/3600:.0f}h ago (> {EMAIL_MAX_AGE_S//3600}h)"
        return True, ""
    except Exception as e:
        return False, f"cannot verify email freshness: {str(e)[:100]}"


def _check_ingest():
    """The ingest pipeline must have stored memories in the last window."""
    try:
        # 2026-10-03: ingest_jobs is only the bulk-file queue (idle most days); the real
        # signal is whether ANY memories landed in the vector store in the window.
        n = _scalar("nova_memories",
                    f"SELECT count(*) FROM memories "
                    f"WHERE created_at > now() - interval '{INGEST_WINDOW}'")
        if not n or n <= 0:
            return False, f":x: ingest pipeline idle — 0 memories stored in last {INGEST_WINDOW}"
        return True, ""
    except Exception as e:
        return False, f"cannot verify ingest throughput: {str(e)[:100]}"


def _check_energy():
    """energy_readings must be getting fresh rows (catches poller/broker outage)."""
    try:
        age = _scalar("nova_ops", "SELECT EXTRACT(EPOCH FROM (now() - max(ts))) FROM energy_readings")
        if age is None:
            return False, ":x: energy_readings empty — no meter data at all"
        if age > ENERGY_MAX_AGE_S:
            return False, f":x: energy flow STALLED — no reading in {age/60:.0f} min (> {ENERGY_MAX_AGE_S//60} min); z-wave/zigbee poller or MQTT broker down?"
        return True, ""
    except Exception as e:
        return False, f"cannot verify energy flow: {str(e)[:100]}"


def _check_article():
    """The journal must have committed an article recently."""
    try:
        out = subprocess.run(["git", "-C", JOURNAL_REPO, "log", "-1", "--format=%ct"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode != 0 or not out.stdout.strip():
            return False, f"cannot verify article cadence: git log failed ({out.stderr.strip()[:80]})"
        age = time.time() - int(out.stdout.strip())
        if age > ARTICLE_MAX_AGE_S:
            return False, f":x: journal quiet — no article commit in {age/3600:.0f}h (> {ARTICLE_MAX_AGE_S//3600}h)"
        return True, ""
    except Exception as e:
        return False, f"cannot verify article cadence: {str(e)[:100]}"


def _check_mqtt():
    """The MQTT broker must accept connections (home-automation bus)."""
    try:
        with socket.create_connection((MQTT_HOST, MQTT_PORT), timeout=3):
            return True, ""
    except Exception as e:
        return False, f":x: MQTT broker unreachable at {MQTT_HOST}:{MQTT_PORT} — {str(e)[:80]}"


def _check_memory_server():
    """The vector-memory server must report healthy."""
    try:
        with urllib.request.urlopen(MEMORY_SERVER_URL, timeout=5) as r:
            data = json.loads(r.read())
        if data.get("status") == "ok":
            return True, ""
        return False, f":x: memory-server unhealthy: {str(data)[:100]}"
    except Exception as e:
        return False, f":x: memory-server not responding ({MEMORY_SERVER_URL}): {str(e)[:80]}"


def _check_backup():
    """A DB backup must exist and be recent."""
    try:
        newest = 0.0
        for d in BACKUP_DIRS:
            for f in glob.glob(os.path.join(d, "*")):
                try:
                    newest = max(newest, os.path.getmtime(f))
                except OSError:
                    continue
        if newest == 0.0:
            return False, f":x: no DB backups found in {BACKUP_DIRS}"
        age = time.time() - newest
        if age > BACKUP_MAX_AGE_S:
            return False, f":x: DB backups STALE — newest is {age/3600:.0f}h old (> {BACKUP_MAX_AGE_S//3600}h)"
        return True, ""
    except Exception as e:
        return False, f"cannot verify backup freshness: {str(e)[:100]}"


# ── Registry: (id, interval_s, severity, fn, depends_on) ───────────────────────
Check = namedtuple("Check", "id interval severity fn depends_on")
CHECKS = [
    Check("vector_liveness",   300,  "critical", _check_vector,        None),
    Check("email_freshness",   3600, "warning",  _check_email,         "vector_liveness"),  # same DB — defer if vector down
    Check("ingest_throughput", 3600, "warning",  _check_ingest,        None),
    Check("energy_flow",       300,  "warning",  _check_energy,        None),
    Check("article_cadence",   3600, "warning",  _check_article,       None),
    Check("mqtt_broker",       300,  "warning",  _check_mqtt,          None),
    Check("memory_server",     300,  "warning",  _check_memory_server, "vector_liveness"),  # also rides pgvector
    Check("backup_freshness",  3600, "warning",  _check_backup,        None),
]

_last_run = {}   # id -> last epoch it ran


def _run_one(chk):
    try:
        ok, msg = chk.fn()
    except Exception as e:                       # belt-and-suspenders: never let a check crash the sweep
        ok, msg = False, f"check {chk.id} raised: {str(e)[:100]}"
    return CheckResult(chk.id, ok, chk.severity, msg)


def run_checks(force=False):
    """Run checks that are due (or all, if force). Returns list[CheckResult].
    depends_on: if a dependency failed this pass, the dependent is skipped (no double-alert)."""
    now = time.time()
    results = []
    failed = set()
    for chk in CHECKS:
        if not force and (now - _last_run.get(chk.id, 0)) < chk.interval:
            continue
        if chk.depends_on and chk.depends_on in failed:
            continue                             # upstream owns the alert; don't double-report
        _last_run[chk.id] = now
        r = _run_one(chk)
        if not r.ok:
            failed.add(chk.id)
        results.append(r)
    return results


def run_due_checks():
    return run_checks(force=False)


if __name__ == "__main__":
    # Runnable self-check: run every check once, print PASS/FAIL, no alerts.
    import sys
    bad = 0
    print("nova_data_checks — dry run (no alerts)\n" + "-" * 44)
    for r in run_checks(force=True):
        flag = "PASS" if r.ok else f"FAIL [{r.severity}]"
        print(f"  {flag:16} {r.id}" + (f"  — {r.message}" if not r.ok else ""))
        bad += 0 if r.ok else 1
    print("-" * 44)
    print(f"{len(CHECKS) - bad}/{len(CHECKS)} passing")
    sys.exit(1 if bad else 0)
