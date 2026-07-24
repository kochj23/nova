#!/usr/bin/env python3
"""
nova_fact.py — manage nova.ground_truth, the shared facts every Nova
narrative-generating script inherits automatically via nova_voice.system_prompt().

For stable facts that aren't derivable from a live query and would otherwise
need re-explaining in every script forever: identity collisions (an IP or
hostname reused by a different physical device), retirements, physical
locations, ownership. NOT for fast-changing operational state (is a job
still running right now) — that should keep coming from live queries.

Usage:
  nova_fact.py add <key> <fact> [--category=identity|retirement|location|general]
  nova_fact.py list
  nova_fact.py remove <key>

Written by Jordan Koch (via Claude).
"""
import sys

import psycopg2

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"


def add(key, fact, category="general"):
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nova.ground_truth (key, fact, category) VALUES (%s, %s, %s) "
            "ON CONFLICT (key) DO UPDATE SET fact = EXCLUDED.fact, category = EXCLUDED.category, "
            "updated_at = now()",
            (key, fact, category),
        )
    print(f"saved: {key}")


def list_facts():
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT key, category, fact, updated_at FROM nova.ground_truth ORDER BY category, key")
        rows = cur.fetchall()
    if not rows:
        print("(no facts stored)")
        return
    for key, category, fact, updated_at in rows:
        print(f"[{category}] {key} — {fact}  ({updated_at:%Y-%m-%d})")


def remove(key):
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM nova.ground_truth WHERE key = %s", (key,))
        removed = cur.rowcount
    print(f"removed: {key}" if removed else f"not found: {key}")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "add":
        if len(sys.argv) < 4:
            sys.exit("usage: nova_fact.py add <key> <fact> [--category=X]")
        category = "general"
        args = []
        for a in sys.argv[2:]:
            if a.startswith("--category="):
                category = a.split("=", 1)[1]
            else:
                args.append(a)
        key, fact = args[0], " ".join(args[1:])
        add(key, fact, category)
    elif cmd == "list":
        list_facts()
    elif cmd == "remove":
        if len(sys.argv) < 3:
            sys.exit("usage: nova_fact.py remove <key>")
        remove(sys.argv[2])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
