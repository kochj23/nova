#!/opt/homebrew/bin/python3
"""nova_whole_picture.py — a daily digest of what Nova has checked and what she can't account for.

Covers four things: candidate rule conflicts from the latest pairwise review, whether the HomeKit rooms are
reporting, how many recent actions have no recorded rationale, and how many checks failed. It names its own
gaps. It does not read memory content, email, messages or any private source, so it can be shared as is.

Usage: nova_whole_picture.py [--hours 24] [--notify] [--dry-run]
Written by Jordan Koch (via Claude).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn()


def digest_lines(conflicts: list, reachability: str, actions: tuple, review_errors: int) -> list:
    """Pure: the digest as lines. actions = (total, missing_rationale)."""
    total, missing = actions
    lines = []
    if conflicts:
        lines.append(f"Rule conflicts to review: {len(conflicts)}")
        lines += [f"  - {a} <-> {b}: {why}" for a, b, why in conflicts]
    else:
        lines.append("Rule conflicts to review: none")
    lines.append(f"HomeKit: {reachability}")
    if total:
        lines.append(f"Actions in window: {total}, of which {missing} have no recorded rationale")
    else:
        lines.append("Actions in window: none")
    if review_errors:
        lines.append(f"Checks that failed (not counted as conflicts): {review_errors}")
    return lines


def gather(conn, hours: int) -> dict:
    cur = conn.cursor()
    cur.execute("""SELECT rule_a, rule_b, reason FROM directive_conflict_reviews
                   WHERE conflict AND reviewed_at = (SELECT max(reviewed_at) FROM directive_conflict_reviews)""")
    conflicts = cur.fetchall()
    cur.execute("""SELECT count(*), count(*) FILTER (WHERE rationale IS NULL OR btrim(rationale) = '')
                   FROM claude_actions WHERE ts > now() - make_interval(hours => %s)""", (hours,))
    total, missing = cur.fetchone()
    cur.execute("""SELECT count(*) FROM directive_conflict_reviews
                   WHERE error AND reviewed_at = (SELECT max(reviewed_at) FROM directive_conflict_reviews)""")
    review_errors = cur.fetchone()[0]
    import nova_home_reachability as H
    res = H.diagnose(H.latest_rows(conn))
    reach = "all rooms reporting" if res["verdict"] == "ok" else f"{res['verdict']}: {res['text']}"
    return {"conflicts": conflicts, "reachability": reach, "actions": (total, missing),
            "review_errors": review_errors}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    import psycopg2
    conn = _nova_dsn.pg_connect()
    try:
        g = gather(conn, a.hours)
    finally:
        conn.close()
    lines = digest_lines(g["conflicts"], g["reachability"], g["actions"], g["review_errors"])
    print("\n".join(lines))
    if a.notify and not a.dry_run:
        import nova_notify
        nova_notify.notify("Nova whole-picture digest", "\n".join(lines), level="info", category="governance",
                           source="nova_whole_picture", dedup_key=f"whole-picture-{a.hours}h")
    return 0


if __name__ == "__main__":
    sys.exit(main())
