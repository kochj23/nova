#!/usr/bin/env python3
"""nova_guard_log.py — one row per guard decision (plan item #27, Jordan approved 2026-10-15).

Some of Nova's guards decided things and left no trace: nova_untrusted (prompt-injection gate),
nova_privacy_guards (P3 camera/face), nova_dials (dial changes) and nova_annie_rule (absence-guilt
gate). Each now calls log() once per decision, which appends one row to nova_ops.guard_decisions.

Rules (the guard always wins):
  * log() never raises, never blocks and never changes what the guard returns. It only puts the
    row on a bounded in-memory queue (put_nowait; dropped when full); one daemon thread drains it
    with a 2 s connect timeout. A dead PG costs the guard nothing.
  * No secrets or PII in a row: free text is stored only as subject_hash(text) — a 16-hex sha256
    prefix plus its length. Labels (verdict, flags, dial names, purposes) are truncated to 120 chars.
  * Off under tests (pytest loaded, NOVA_TEST_QUIET set, or a tests/test_*.py entry point) and when
    NOVA_GUARD_LOG=0, so offline suites never write to the live table.

Table: guard_decisions(id, ts, guard, decision, subject, detail jsonb) — created via
nova_pg_schema.safe_ddl on first write. CLI: --selftest | --recent [N]
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import sys
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS guard_decisions (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  guard text NOT NULL,
  decision text NOT NULL,
  subject text,
  detail jsonb);
CREATE INDEX IF NOT EXISTS guard_decisions_guard_ts ON guard_decisions (guard, ts DESC);
"""
INSERT = "INSERT INTO guard_decisions (guard, decision, subject, detail) VALUES (%s, %s, %s, %s::jsonb)"
QMAX = 1000
CONNECT_TIMEOUT = 2
_LABEL = 120

_q: "queue.Queue" = queue.Queue(maxsize=QMAX)
_worker = None
_lock = threading.Lock()
_schema_ok = False
ENABLED = None          # None = decide from the environment; tests may force True/False


def enabled() -> bool:
    if ENABLED is not None:
        return bool(ENABLED)
    if os.environ.get("NOVA_GUARD_LOG", "1") == "0" or os.environ.get("NOVA_TEST_QUIET"):
        return False
    if "pytest" in sys.modules:
        return False
    a0 = os.path.basename(sys.argv[0] if sys.argv else "")
    return not (a0.startswith("test_") and a0.endswith(".py"))


def subject_hash(text) -> str:
    """Never the text itself: 'sha256:<16 hex>/<len>'."""
    t = "" if text is None else str(text)
    return f"sha256:{hashlib.sha256(t.encode('utf-8', 'replace')).hexdigest()[:16]}/{len(t)}"


def _label(v) -> str:
    return str(v)[:_LABEL]


def _clean(detail):
    if not isinstance(detail, dict):
        return {}
    out = {}
    for k, v in list(detail.items())[:20]:
        if isinstance(v, (int, float, bool)) or v is None:
            out[_label(k)] = v
        elif isinstance(v, (list, tuple)):
            out[_label(k)] = [_label(x) for x in list(v)[:20]]
        else:
            out[_label(k)] = _label(v)
    return out


def _connect():
    import psycopg2
    import nova_dsn
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"), connect_timeout=CONNECT_TIMEOUT)
    conn.autocommit = True
    return conn


def write_row(row, connect=None) -> bool:
    """Synchronous insert (the worker's body). Never raises; True when written."""
    global _schema_ok
    conn = None
    try:
        conn = (connect or _connect)()
        cur = conn.cursor()
        if not _schema_ok:
            import nova_pg_schema
            nova_pg_schema.safe_ddl(cur, SCHEMA)
            _schema_ok = True
        cur.execute(INSERT, row)
        return True
    except Exception:  # noqa: BLE001 — a guard log must never hurt anything
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _drain():
    while True:
        q = _q                   # re-read each pass: never stay parked on a replaced queue
        try:
            row = q.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            write_row(row)
        finally:
            q.task_done()


def _ensure_worker():
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_drain, name="nova-guard-log", daemon=True)
            _worker.start()


def log(guard, decision, subject=None, detail=None) -> bool:
    """Queue one decision row. Returns True if queued. Never raises, never blocks."""
    try:
        if not enabled():
            return False
        row = (_label(guard), _label(decision), None if subject is None else _label(subject),
               json.dumps(_clean(detail), default=str))
        _q.put_nowait(row)
        _ensure_worker()
        return True
    except Exception:  # noqa: BLE001 — queue full, thread limit, anything: drop the row
        return False


def flush(timeout: float = 3.0) -> None:
    """Best effort wait for the queue to drain (CLI / short-lived scripts). Never raises."""
    import time
    end = time.monotonic() + timeout
    try:
        while _q.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.05)
    except Exception:  # noqa: BLE001
        pass


def selftest() -> int:
    assert subject_hash("secret text").startswith("sha256:") and "secret" not in subject_hash("secret text")
    assert subject_hash(None).endswith("/0")
    assert _clean({"a": "x" * 500, "b": [1, 2], "c": 3})["a"] == "x" * 120
    assert _clean("nope") == {}
    print("selftest ok")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--recent" in sys.argv:
        i = sys.argv.index("--recent")
        n = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 20
        c = _connect().cursor()
        c.execute("SELECT ts, guard, decision, subject, detail FROM guard_decisions ORDER BY ts DESC LIMIT %s", (n,))
        for r in c.fetchall():
            print(*r, sep=" | ")
        sys.exit(0)
    print(__doc__)
