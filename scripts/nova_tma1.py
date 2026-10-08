#!/usr/bin/env python3
"""nova_tma1.py — TMA-1: an alarm on something whose legitimate access count is zero.

From Clarke's "The Sentinel": a lunar expedition finds a small pyramid on the Moon behind an
invisible shield. Twenty years on, people break through the shield with atomic power and
wreck the machine. The narrator concludes it was a sentinel that had been signalling into
space for ages, and that its silence is the message: whoever left it will now know that
someone has learned to reach and break it. In 2001 the buried monolith, dug up in the crater
Tycho, sends a single burst toward Saturn's moon Iapetus at the first touch of sunlight.
Nova's version is a honeytoken: decoys nobody should ever read, so any read is the signal.

Minimal first version:
  * one decoy table in nova_ops (fake rows; never a real credential), watched through the
    read-only counters in pg_stat_user_tables (seq_scan + idx_scan, last_*_scan)
  * one decoy service_config key whose value points at that table. pgaudit is not installed,
    so a read of the key alone is invisible; whoever follows the pointer trips the table.
  * readers attributed, when the pg_stat_statements view exists, by statement text and role
Every --run compares the counters with the last tma1_checks row. A read files a Buick 8
'tripwire_touched' entry (cause unknown) and a nova_notify warning deduped per decoy.
pg_dump's "COPY ... TO stdout" is classed as a backup and logged without an alert. Reads by
Nova's own processes count, so it is also a scope-creep tripwire. With no decoy planted
(service_config tma1/decoys absent) it reports that and exits 0.

Planting is a write, so it is a separate command a human runs once:  --plant
CLI:   --run [--dry-run]   --show   --plant   --selftest
Tables: tma1_checks (one row per touch, plus a daily "intact" row); the decoy table itself
Config: service_config tma1/decoys = {"table", "config_service", "config_key", "planted_at"}
Schedule: every 15 minutes.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

DECOY_TABLE = "legacy_api_keys"
DECOY_CONFIG = ("legacy_vault", "master_api_key")
HEARTBEAT = timedelta(hours=20)
NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
BACKUP_RE = re.compile(r"^\s*COPY\s.+\sTO\s+stdout", re.I | re.S)
READ_RE = re.compile(r"^\s*(SELECT|WITH|COPY|TABLE|FETCH|DECLARE)\b", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tma1_checks (
  ts timestamptz NOT NULL DEFAULT now(), decoy text NOT NULL, scans bigint, last_scan timestamptz,
  touched boolean NOT NULL DEFAULT false, verdict text, readers jsonb);
CREATE INDEX IF NOT EXISTS tma1_checks_decoy_ts ON tma1_checks (decoy, ts);
"""


def log(m: str) -> None:
    print(f"[tma1 {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a failed read degrades to "nothing known"
        log(f"query failed: {e}")
        return []


# ── pure ────────────────────────────────────────────────────────────────────

def delta_readers(prev: dict, cur: dict) -> list:
    """Statements whose call count grew. Both: {queryid: {"role", "calls", "query"}}."""
    out = []
    for qid, r in cur.items():
        d = r["calls"] - (prev.get(qid) or {}).get("calls", 0)
        if d > 0:
            out.append(dict(r, calls=d))
    return out


def classify(readers: list) -> str:
    """'backup' if every new read is pg_dump-shaped, 'reader' if any other read, else 'unattributed'."""
    # ponytail: classifies by statement shape; a hostile COPY ... TO stdout looks like a backup.
    # Per-process attribution (client app, host) needs pgaudit or log_connections.
    reads = [r for r in readers if READ_RE.match(r["query"] or "")]
    if not reads:
        return "unattributed"
    return "backup" if all(BACKUP_RE.match(r["query"]) for r in reads) else "reader"


def judge(prev: dict | None, now_stats: dict, readers: list, now=None) -> dict:
    """prev/now_stats: {"scans", "last_scan"}. Returns {"touched", "verdict", "write"}."""
    now = now or datetime.now(timezone.utc)
    if prev is None:
        return {"touched": False, "verdict": "baseline", "write": True}
    if now_stats["scans"] < (prev["scans"] or 0):
        return {"touched": False, "verdict": "stats reset", "write": True}
    if now_stats["scans"] > (prev["scans"] or 0):
        return {"touched": True, "verdict": classify(readers), "write": True}
    return {"touched": False, "verdict": "intact", "write": now - prev["ts"] >= HEARTBEAT}


# ── PG reads ────────────────────────────────────────────────────────────────

def registry(cur) -> dict | None:
    try:
        reg = W.get_config(cur, "tma1", "decoys")
    except Exception as e:  # noqa: BLE001
        log(f"registry unreadable: {e}")
        return None
    return reg if reg and NAME_RE.match(reg.get("table") or "") else None


def table_stats(cur, table: str) -> dict | None:
    rows = _q(cur, "SELECT coalesce(seq_scan,0) + coalesce(idx_scan,0), greatest(last_seq_scan, last_idx_scan) "
                   "FROM pg_stat_user_tables WHERE schemaname='public' AND relname=%s", (table,))
    return {"scans": int(rows[0][0]), "last_scan": rows[0][1]} if rows else None


def readers_now(cur, table: str) -> dict:
    """{queryid: {role, calls, query}} for statements naming the decoy; {} if the view is absent."""
    exists = _q(cur, "SELECT to_regclass('pg_stat_statements')")
    if not exists or exists[0][0] is None:
        return {}
    rows = _q(cur, "SELECT s.queryid::text, r.rolname, s.calls, left(s.query, 300) FROM pg_stat_statements s "
                   "LEFT JOIN pg_roles r ON r.oid = s.userid WHERE s.query ~* %s", (r"\m" + table + r"\M",))
    return {q: {"role": role, "calls": int(c), "query": text} for q, role, c, text in rows}


def last_check(cur, table: str) -> tuple:
    """(prev stats dict or None, prev readers dict)."""
    exists = _q(cur, "SELECT to_regclass('tma1_checks')")
    if not exists or exists[0][0] is None:
        return None, {}
    rows = _q(cur, "SELECT ts, scans, last_scan, readers FROM tma1_checks WHERE decoy=%s ORDER BY ts DESC LIMIT 1",
              (table,))
    if not rows:
        return None, {}
    ts, scans, last, readers = rows[0]
    rd = json.loads(readers) if isinstance(readers, str) else (readers or {})
    return {"ts": ts, "scans": scans, "last_scan": last}, rd


# ── run ─────────────────────────────────────────────────────────────────────

def alert(cur, table: str, stats: dict, prev: dict, readers: list, verdict: str) -> None:
    from nova_buick8_log import log_unexplained
    from nova_notify import notify
    who = ", ".join(sorted({r["role"] or "?" for r in readers})) or "no attributable statement"
    desc = (f"decoy table {table} was read {stats['scans'] - (prev['scans'] or 0)} time(s), last at "
            f"{stats['last_scan']}; reader: {who}")
    log_unexplained("tripwire_touched", f"pg_table:{table}", desc,
                    evidence={"verdict": verdict, "last_scan": stats["last_scan"], "readers": readers},
                    occurrence_key=str(stats["last_scan"]), source="tma1", cur=cur)
    notify(f"TMA-1: decoy {table} was read", body=desc + ". Cause unknown. Nothing legitimate reads it.",
           level="warning", category="security", source="nova_tma1", dedup_key=f"tma1:{table}")


def run(dry: bool = False) -> dict:
    conn = W.connect()
    try:
        cur = conn.cursor()
        reg = registry(cur)
        if not reg:
            log("no decoy planted (service_config tma1/decoys absent); nothing to watch")
            return {"verdict": "unplanted", "touched": False}
        table = reg["table"]
        stats = table_stats(cur, table)
        if stats is None:
            log(f"decoy table {table} missing from pg_stat_user_tables; nothing to watch")
            return {"verdict": "decoy missing", "touched": False}
        prev, prev_readers = last_check(cur, table)
        now_readers = readers_now(cur, table)
        new_readers = delta_readers(prev_readers, now_readers)
        j = judge(prev, stats, new_readers)
        log(f"{'DRY RUN ' if dry else ''}{table}: scans {prev and prev['scans']} -> {stats['scans']}, "
            f"last {stats['last_scan']}; {j['verdict']}" + (" TOUCHED" if j["touched"] else ""))
        for r in new_readers:
            print(f"  {r['role']} x{r['calls']}: {r['query'][:120]}")
        if not dry:
            ensure_schema(cur)
            if j["write"]:
                cur.execute("INSERT INTO tma1_checks (decoy, scans, last_scan, touched, verdict, readers) "
                            "VALUES (%s,%s,%s,%s,%s,%s::jsonb)",
                            (table, stats["scans"], stats["last_scan"], j["touched"], j["verdict"],
                             json.dumps(now_readers)))
            if j["touched"] and j["verdict"] != "backup":
                alert(cur, table, stats, prev, new_readers, j["verdict"])
        return j
    finally:
        conn.close()


def show() -> int:
    conn = W.connect()
    try:
        rows = _q(conn.cursor(), "SELECT ts, decoy, scans, last_scan, touched, verdict FROM tma1_checks "
                                 "ORDER BY ts DESC LIMIT 20")
        for ts, d, s, ls, t, v in rows:
            print(f"{ts:%Y-%m-%d %H:%M} {d} scans={s} last={ls or '-'} {'TOUCHED' if t else ''} {v}")
        if not rows:
            print("no checks yet")
        return 0
    finally:
        conn.close()


def plant() -> int:
    """Human-run, once: create the decoy table, the decoy config key and the registry."""
    conn = W.connect()
    try:
        cur = conn.cursor()
        if registry(cur):
            print("already planted; refusing to plant twice")
            return 1
        ensure_schema(cur)
        cur.execute("CREATE TABLE IF NOT EXISTS legacy_api_keys (service text, api_key text, created date)")
        for svc in ("billing", "backup-s3", "router-admin"):     # random filler, never a real credential
            cur.execute("INSERT INTO legacy_api_keys VALUES (%s, %s, current_date - 400)",
                        (svc, "tma1-decoy-" + secrets.token_hex(16)))
        cur.execute("INSERT INTO service_config (service, key, value, updated_by) VALUES (%s,%s,%s::jsonb,'tma1') "
                    "ON CONFLICT (service, key) DO NOTHING",
                    (*DECOY_CONFIG, json.dumps({"store": f"nova_ops.public.{DECOY_TABLE}", "migrated": True})))
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        except Exception as e:  # noqa: BLE001 — attribution is optional; detection works without it
            log(f"pg_stat_statements not enabled ({e}); reads will be unattributed")
        stats = table_stats(cur, DECOY_TABLE) or {"scans": 0, "last_scan": None}
        cur.execute("INSERT INTO tma1_checks (decoy, scans, last_scan, verdict, readers) VALUES (%s,%s,%s,'planted',%s::jsonb)",
                    (DECOY_TABLE, stats["scans"], stats["last_scan"], json.dumps(readers_now(cur, DECOY_TABLE))))
        W.set_config(cur, "tma1", "decoys", {"table": DECOY_TABLE, "config_service": DECOY_CONFIG[0],
                                             "config_key": DECOY_CONFIG[1], "never_ingest": True,
                                             "planted_at": datetime.now(timezone.utc).isoformat()}, by="tma1")
        print(f"planted {DECOY_TABLE} and service_config {'/'.join(DECOY_CONFIG)}. Tag both never-ingest in any "
              "crawler that walks nova_ops.")
        return 0
    finally:
        conn.close()


def selftest() -> int:
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    prev = {"ts": t0, "scans": 4, "last_scan": t0}
    assert judge(None, {"scans": 4}, [])["verdict"] == "baseline"
    j = judge(prev, {"scans": 4}, [], now=t0 + timedelta(minutes=15))
    assert not j["touched"] and not j["write"] and j["verdict"] == "intact"
    assert judge(prev, {"scans": 4}, [], now=t0 + timedelta(hours=21))["write"]
    assert judge(prev, {"scans": 1}, [])["verdict"] == "stats reset"
    dump = {"role": "kochj", "calls": 1, "query": "COPY public.legacy_api_keys (a) TO stdout;"}
    peek = {"role": "nova", "calls": 2, "query": "SELECT * FROM legacy_api_keys"}
    plant_ins = {"role": "kochj", "calls": 3, "query": "INSERT INTO legacy_api_keys VALUES ($1,$2)"}
    assert judge(prev, {"scans": 5}, [dump])["verdict"] == "backup"
    assert judge(prev, {"scans": 5}, [dump, peek])["verdict"] == "reader"
    assert judge(prev, {"scans": 5}, [plant_ins])["verdict"] == "unattributed"
    d = delta_readers({"1": dict(peek, calls=2)}, {"1": dict(peek, calls=5), "2": dump})
    assert sorted(r["calls"] for r in d) == [1, 3], d
    assert NAME_RE.match(DECOY_TABLE) and not NAME_RE.match("x; DROP TABLE y")
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="compare decoy read counters, file any touch")
    ap.add_argument("--dry-run", action="store_true", help="with --run: print the verdict, write nothing")
    ap.add_argument("--show", action="store_true", help="last 20 checks")
    ap.add_argument("--plant", action="store_true", help="HUMAN, ONCE: create the decoys (writes to nova_ops)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.plant:
        return plant()
    if a.run:
        run(dry=a.dry_run)
        return 0
    if a.show:
        return show()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
