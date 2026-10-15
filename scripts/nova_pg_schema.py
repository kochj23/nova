"""nova_pg_schema.py — lock-safe "ensure schema" DDL for scripts that self-migrate every run.

Why this exists (2026-10-15): `ALTER TABLE t ADD COLUMN IF NOT EXISTS c ...` takes an
ACCESS EXCLUSIVE lock on t EVEN WHEN c ALREADY EXISTS. The nightly pg_backup (02:00) holds
ACCESS SHARE on every nova_ops table for ~18 min, so every routine ensure_schema ALTER
queued behind it until statement_timeout (60s) fired — coagency_earned_autonomy,
incident_lifecycle and nova_speaks_sweep failed 02:00-02:17 every night. Worse, a queued
ACCESS EXCLUSIVE request blocks every other reader/writer of that table while it waits.

safe_ddl(cur, sql) runs a DDL script statement by statement and:
  * drops ADD COLUMN IF NOT EXISTS clauses whose column already exists (catalog check, no lock),
    and skips the ALTER entirely when nothing is missing;
  * skips CREATE [UNIQUE] INDEX IF NOT EXISTS <name> when the index already exists
    (CREATE INDEX takes a SHARE lock before checking for the name);
  * runs whatever DDL is still needed under a short lock_timeout, so a real migration
    fails fast instead of stalling the table behind a long-running lock holder.
Works with psycopg2 cursors (autocommit or not).
"""
from __future__ import annotations

import re

_ADD_COL = re.compile(r"^\s*ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+\"?(\w+)\"?", re.I)
_ALTER = re.compile(r"^\s*ALTER\s+TABLE\s+(?:ONLY\s+)?(?:IF\s+EXISTS\s+)?([\w.\"]+)\s+(.*)$", re.I | re.S)
_CREATE_IDX = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?IF\s+NOT\s+EXISTS\s+\"?(\w+)\"?\s+ON\s+(?:ONLY\s+)?([\w.\"]+)",
    re.I)


def _split_top(s: str, sep: str = ",") -> list[str]:
    """split on sep outside parentheses and single quotes"""
    out, depth, q, cur = [], 0, False, []
    for ch in s:
        if ch == "'":
            q = not q
        elif not q and ch == "(":
            depth += 1
        elif not q and ch == ")":
            depth -= 1
        if ch == sep and not q and depth == 0:
            out.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [p for p in out if p.strip()]


def _statements(sql: str) -> list[str]:
    if "$" in sql:          # function bodies etc. — don't try to split
        return [sql]
    sql = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in _split_top(sql, ";") if s.strip()]


def _column_exists(cur, table: str, col: str) -> bool | None:
    """True/False; None when the table itself doesn't exist (yet)."""
    cur.execute("SELECT to_regclass(%s)::oid", (table,))
    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    cur.execute("SELECT 1 FROM pg_attribute WHERE attrelid=%s AND attname=%s AND NOT attisdropped",
                (row[0], col.lower()))
    return cur.fetchone() is not None


def _index_exists(cur, table: str, name: str) -> bool:
    schema = table.replace('"', "").rsplit(".", 1)[0] + "." if "." in table else ""
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (schema + name,))
    row = cur.fetchone()
    return bool(row and row[0])


def _needed(cur, stmt: str) -> str | None:
    """the statement to actually run, or None if it is a no-op against the current catalog"""
    m = _ALTER.match(stmt)
    if m:
        table, rest = m.group(1), m.group(2)
        clauses = _split_top(rest)
        if not all(_ADD_COL.match(c) for c in clauses):
            return stmt                          # some other ALTER — run as written
        missing = [c for c in clauses if _column_exists(cur, table, _ADD_COL.match(c).group(1)) is not True]
        if not missing:
            return None
        return f"ALTER TABLE {table} " + ", ".join(c.strip() for c in missing)
    m = _CREATE_IDX.match(stmt)
    if m and _index_exists(cur, m.group(2), m.group(1)):
        return None
    return stmt


def safe_ddl(cur, sql: str, lock_timeout: str = "5s") -> int:
    """Run an idempotent DDL script without taking locks it doesn't need. Returns #statements run.

    lock_timeout is SET LOCAL inside the DDL's own transaction, so it never leaks onto a
    pooled (pgbouncer transaction-mode) server session. Nothing at all is sent beyond the
    catalog reads when the schema is already current — the normal case."""
    ran = 0
    lt = "'" + lock_timeout.replace("'", "") + "'"
    autocommit = bool(getattr(getattr(cur, "connection", None), "autocommit", False))
    for stmt in _statements(sql):
        todo = _needed(cur, stmt)
        if not todo:
            continue
        if autocommit:
            try:
                cur.execute(f"BEGIN; SET LOCAL lock_timeout = {lt}; {todo}; COMMIT")
            except Exception:
                try:
                    cur.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                raise
        else:   # caller owns the transaction; SET LOCAL ends with it
            cur.execute(f"SET LOCAL lock_timeout = {lt}")
            cur.execute(todo)
        ran += 1
    return ran
