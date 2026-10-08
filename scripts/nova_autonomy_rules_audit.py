#!/usr/bin/env python3
"""nova_autonomy_rules_audit.py — find autonomy_rules rows whose tool no longer exists.

autonomy_rules (nova_ops) gates every gateway tool call by action_type. Rows outlive their
tools: on 2026-10-08 seven orphans were found (quarantine_device, unquarantine_device,
secret_set, secret_list, secret_check, run_sandboxed, list_watchers) — tools that were
planned or retired but are not in the gateway registry. An orphan is harmless until a new
tool reuses the name and silently inherits a stale level (e.g. 'auto'), so audit them.

Usage:
  nova_autonomy_rules_audit.py            # report orphans (read-only, default)
  nova_autonomy_rules_audit.py --prune    # delete orphans by explicit action_type list

The known-tool set is the gateway's TOOL_REGISTRY plus tools_extended.EXTENDED_TOOLS (an
extended tool that is not wired yet still counts as a real tool, so its rule is kept).
Written by Jordan Koch (via Claude).
"""
import argparse
import sys
import time
from pathlib import Path

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
SCRIPTS = Path(__file__).resolve().parent


def known_tools() -> set:
    """Every tool name the gateway can dispatch (registry + extended). Never raises."""
    sys.path.insert(0, str(SCRIPTS))
    names = set()
    try:
        from nova_gateway import tools as t
        names |= set(t.TOOL_REGISTRY)
    except Exception as e:  # noqa: BLE001
        print(f"[audit] gateway registry unavailable: {e}", file=sys.stderr)
    try:
        from nova_gateway import tools_extended as tx
        names |= set(tx.EXTENDED_TOOLS)
    except Exception as e:  # noqa: BLE001
        print(f"[audit] extended tools unavailable: {e}", file=sys.stderr)
    return names


def orphan_rules(rule_types, known) -> list:
    """Sorted action_types present in autonomy_rules but absent from the known tools.
    An empty `known` set means the registry could not be read — return [] (fail safe:
    never call every rule an orphan)."""
    known = set(known or ())
    if not known:
        return []
    return sorted({r for r in (rule_types or ()) if r and r != "*" and r not in known})


def _connect(attempts: int = 3, backoff_s: float = 1.0):
    """psycopg2.connect with 3 attempts and exponential backoff; last failure re-raised."""
    import psycopg2
    for i in range(attempts):
        try:
            return psycopg2.connect(OPS_DSN)
        except Exception as e:  # noqa: BLE001
            print(f"[audit] pg connect attempt {i + 1}/{attempts} failed: {e}", file=sys.stderr)
            if i == attempts - 1:
                raise
            time.sleep(backoff_s * (2 ** i))


def run(prune: bool = False, conn=None) -> list:
    """Report (and optionally delete) orphan rules. Returns the orphan action_types."""
    known = known_tools()
    conn = conn or _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT action_type FROM autonomy_rules")
        orphans = orphan_rules([r[0] for r in cur.fetchall()], known)
        for o in orphans:
            print(f"orphan rule: {o}")
        if prune and orphans:
            cur.execute("DELETE FROM autonomy_rules WHERE action_type = ANY(%s)", (orphans,))
            conn.commit()
            print(f"pruned {cur.rowcount} row(s)")
        if not orphans:
            print("no orphan autonomy_rules")
        return orphans
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prune", action="store_true", help="delete orphan rows (explicit action_type list)")
    a = ap.parse_args(argv)
    run(prune=a.prune)
    return 0


if __name__ == "__main__":
    sys.exit(main())
