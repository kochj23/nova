#!/opt/homebrew/bin/python3
"""nova_action_why.py — say, in plain language, what Nova did recently and why.

Reads claude_actions (nova_ops). Each action is shown with the rationale recorded when it was taken. If no
rationale was recorded, the output says so, rather than making one up: the missing reason is itself the thing
the action audit should surface.

MERGED 2026-10-15 (consolidation #18): this is now `nova_action_audit.py --why` (the ledger audit's
plain-language view of claude_actions). explain/summarise/fetch/run stay here; main() is a thin wrapper.

Usage: nova_action_why.py [--limit 10] [--target SUBSTR] [--missing-only]
       == nova_action_audit.py --why [--limit 10] [--target SUBSTR] [--missing-only]
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


MERGED_ON = "2026-10-15"


def run(limit: int = 10, target: str | None = None, missing_only: bool = False) -> int:
    """The --why view (nova_action_audit.py --why calls this): print each action and its reason."""
    conn = _nova_dsn.pg_connect()
    try:
        rows = fetch(conn, limit, target, missing_only)
    finally:
        conn.close()
    for r in rows:
        print(explain(r))
    print(summarise(rows))
    return 0


def main(argv=None) -> int:
    """Merged into nova_action_audit.py --why on 2026-10-15: thin wrapper for old invocations."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--target", help="only actions whose target contains this text")
    ap.add_argument("--missing-only", action="store_true", help="only actions with no recorded rationale")
    a = ap.parse_args(argv)
    import nova_action_audit
    return nova_action_audit.main(["--why", "--limit", str(a.limit)]
                                  + (["--target", a.target] if a.target else [])
                                  + (["--missing-only"] if a.missing_only else []))


if __name__ == "__main__":
    sys.exit(main())
