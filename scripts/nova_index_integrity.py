#!/usr/bin/env python3
"""nova_index_integrity.py — verify btree indexes actually match their tables.

WHY: on 2026-07-28 an overdue restore test failed on two unique constraints. The tables held
duplicate rows that the unique indexes did not reflect, which should be impossible — the
constraints exist and are enforced. An amcheck sweep found 14 corrupt indexes in nova_ops and 1
in nova_media, including claude_memories_name_key. Cause was almost certainly the 2026-07-27
unclean shutdown, the same event that put a hole in the WAL and forced an 84GB replica rebuild.

Nothing detected it. Not the backup job, not Big Brother, not the health checks — because a
corrupt index does not raise errors. It silently returns incomplete results and silently stops
enforcing uniqueness. The database reports itself healthy the entire time, which is the
counterfeit failure mode this fleet keeps rediscovering.

The only reason it surfaced is that a restore was attempted. This check makes that detection
routine instead of accidental.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

DBS = ["nova_ops", "nova_memories", "nova_media", "nova"]
HOST = os.environ.get("PGHOST", "pg-primary.digitalnoise.net")
# Indexes above this size are skipped by default: bt_index_check reads the whole index, and a
# multi-GB embedding index would run for many minutes every night. --full includes them.
DEFAULT_MAX_MB = 300


def sweep(db, max_mb, full):
    import psycopg2
    out = []
    try:
        c = psycopg2.connect(f"host={HOST} dbname={db} user=kochj")
    except Exception as e:
        return [("__connect__", f"cannot connect: {str(e).strip()[:120]}")], 0
    c.autocommit = True
    cur = c.cursor()
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS amcheck")
    except Exception:
        pass
    size_filter = "" if full else f"AND pg_relation_size(c.oid) < {max_mb} * 1024 * 1024"
    cur.execute(f"""
        SELECT c.oid::regclass::text, c.relname
        FROM pg_class c
        JOIN pg_index i ON i.indexrelid = c.oid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relam = (SELECT oid FROM pg_am WHERE amname='btree')
          AND (i.indisunique OR i.indisprimary)
          AND i.indisvalid AND i.indisready
          -- matviews included on purpose: telemetry.energy_hourly is a matview whose unique
          -- index broke a restore while every table-level check reported clean.
          AND c.relkind = 'i' 
          AND n.nspname NOT IN ('pg_catalog','information_schema')
          {size_filter}
        ORDER BY pg_relation_size(c.oid)""")
    idxs = cur.fetchall()
    for qualified, name in idxs:
        try:
            # heapallindexed=true is the whole point. Plain bt_index_check only validates the
            # index's INTERNAL structure; it cannot see rows that exist in the table and are
            # missing from the index. That is precisely the damage here: on 2026-07-28 every
            # repaired index passed the structural check while telemetry.energy_hourly still
            # broke a restore, because the heap held keys the index had never recorded. A
            # structure-only check would have declared this database clean and been wrong.
            cur.execute("SELECT bt_index_check(%s::regclass, heapallindexed => true)", (qualified,))
        except Exception as e:
            out.append((name, str(e).strip().splitlines()[0][:140]))
    c.close()
    return out, len(idxs)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--full", action="store_true", help="include large indexes (slow)")
    ap.add_argument("--max-mb", type=int, default=DEFAULT_MAX_MB)
    ap.add_argument("--quiet", action="store_true", help="only speak up when something is wrong")
    a = ap.parse_args()

    all_bad, total = {}, 0
    for db in DBS:
        bad, n = sweep(db, a.max_mb, a.full)
        total += n
        if bad:
            all_bad[db] = bad
        if not a.quiet:
            print(f"  {db:15} {n - len(bad)}/{n} ok" + (f", {len(bad)} CORRUPT" if bad else ""))

    if not all_bad:
        if not a.quiet:
            print(f"=== {total} unique/PK indexes verified, 0 corrupt ===")
        return 0

    lines = ["🔴 *Index corruption detected on the PG primary*", ""]
    for db, bad in all_bad.items():
        lines.append(f"*{db}* — {len(bad)} corrupt:")
        lines += [f"  • `{n}` — {msg}" for n, msg in bad[:12]]
    lines += ["", "A corrupt unique index stops enforcing uniqueness and can return incomplete "
                  "results, while every health check keeps reporting green. Repair: check for real "
                  "duplicates with a seq scan first (`SET enable_indexscan=off`), remove them, then "
                  "`REINDEX INDEX CONCURRENTLY`, then re-run this."]
    msg = "\n".join(lines)
    print(msg)
    try:
        import nova_config
        nova_config.post_both(msg, slack_channel=nova_config.SLACK_INFO)
    except Exception as e:
        print(f"(alert failed: {e})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
