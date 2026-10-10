#!/opt/homebrew/bin/python3
"""nova_action_why.py — say, in plain language, what Nova did recently and why.

Reads claude_actions (nova_ops). Each action is shown with the rationale recorded when it was taken. If no
rationale was recorded, the output says so, rather than making one up: the missing reason is itself the thing
the action audit should surface.

Usage: nova_action_why.py [--limit 10] [--target SUBSTR] [--missing-only]
Written by Jordan Koch (via Claude).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn()
NO_RATIONALE = "no rationale was recorded for this action"


def explain(row: dict) -> str:
    """One action -> one plain-language line. Pure."""
    when = str(row.get("ts", ""))[:16]
    what = row.get("description") or row.get("action_type") or "an action"
    target = f" on {row['target']}" if row.get("target") else ""
    outcome = f" ({row['outcome']})" if row.get("outcome") else ""
    why = (row.get("rationale") or "").strip() or NO_RATIONALE
    return f"{when}  {what}{target}{outcome}\n    why: {why}"


def summarise(rows: list) -> str:
    """How many of the shown actions have a recorded reason. Pure."""
    missing = sum(1 for r in rows if not (r.get("rationale") or "").strip())
    return f"{len(rows)} actions shown, {missing} with no recorded rationale"


def fetch(conn, limit: int, target: str | None, missing_only: bool) -> list:
    where, args = [], []
    if target:
        where.append("target ILIKE %s")
        args.append(f"%{target}%")
    if missing_only:
        where.append("(rationale IS NULL OR btrim(rationale) = '')")
    sql = ("SELECT ts, action_type, target, description, outcome, rationale FROM claude_actions"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts DESC LIMIT %s")
    cur = conn.cursor()
    cur.execute(sql, (*args, limit))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--target", help="only actions whose target contains this text")
    ap.add_argument("--missing-only", action="store_true", help="only actions with no recorded rationale")
    a = ap.parse_args(argv)
    import psycopg2
    conn = _nova_dsn.pg_connect()
    try:
        rows = fetch(conn, a.limit, a.target, a.missing_only)
    finally:
        conn.close()
    for r in rows:
        print(explain(r))
    print(summarise(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
