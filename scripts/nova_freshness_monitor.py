#!/usr/bin/env python3
"""
nova_freshness_monitor.py — the ONE data-freshness / silent-failure monitor.

THE FAILURE CLASS THIS CATCHES
------------------------------
Over and over the same shape of bug bit us: a *writer/poller died silently* while
its *consumer kept showing the last value as if nothing was wrong*:

  * nas_localdiff reported "success" while its source was dead,
  * a mount watchdog read an empty dir as "healthy",
  * the nova-control-web history pool wedged for 32 DAYS,
  * the energy poller stuck,
  * the SNMP poller was dead for 2 WEEKS.

Every one of them is the *same* question: "is the freshest row in table X actually
recent, or has the thing that writes X quietly stopped?" Individual health checks on
each producer keep missing this because a producer can report "up" while writing
nothing (detect *disabled-ness*, not just *down-ness*). So instead of trusting N
producers we watch the ONE thing that can't lie: the data itself. If max(timestamp)
for a stream is older than that stream's SLA, the writer is (effectively) dead —
regardless of what any health check claims.

DESIGN
------
* A DECLARATIVE table (`EXPLICIT_STREAMS`) of the streams we know matter, each with
  a max-staleness SLA. Plus AUTO-DISCOVERY of every `telemetry.*` table with a `ts`
  timestamptz column, given a generous default SLA — so a new poller is watched the
  day it's added, with zero code change, even if nobody remembered to register it.
* Handles the four timestamp encodings that exist in this DB: timestamptz, naive
  timestamp, epoch-seconds stored as double, and date (incl. date-as-text).
* For each stream: age = now - max(ts_col); breach if age > SLA (or if the table is
  empty / unqueryable — "no data at all" is the loudest silent failure).
* On breach → `nova_notify.notify(..., category='freshness', dedup_key=<per-stream>)`
  so an ongoing breach collapses to one alert instead of re-firing every 15 min.
* NEVER crashes the loop. Every per-stream query is wrapped; one bad stream is
  reported and the rest still run. A dead monitor is exactly the disease it treats.

Runs as its OWN launchd job (net.digitalnoise.nova-freshness-monitor, StartInterval
900) — deliberately not under any scheduler, so the scheduler dying can't blind it.

    python3 nova_freshness_monitor.py            # one pass, notify on breach, exit
    python3 nova_freshness_monitor.py --dry-run  # one pass, print report, DO NOT notify
    python3 nova_freshness_monitor.py --loop      # run forever every INTERVAL_S

Written by Jordan Koch.
"""
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# nova_notify is the ONE notification path (event bus -> nova_notifier routing).
try:
    from nova_notify import notify as _notify
except Exception:                       # pragma: no cover - defensive import
    def _notify(*a, **k):               # never let a missing import kill the monitor
        return False

DSN = os.environ.get("NOVA_PG_DSN", "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")
INTERVAL_S = 900                        # matches the launchd StartInterval
CONNECT_ATTEMPTS = 3
CONNECT_BACKOFF_S = 2

# Default SLA for any auto-discovered telemetry.* table (24h): generous on purpose so
# daily/event-driven/sparse tables don't cry wolf, while a truly dead poller still trips.
DEFAULT_SLA_S = 24 * 3600

# The timestamp encodings present in this database. Each maps to a SQL expression that
# yields the stream's age in SECONDS as a float, robust to the column's storage type.
VALID_KINDS = ("timestamptz", "timestamp", "epoch", "date", "date_text")


@dataclass
class Stream:
    name: str                    # logical id — used in the report and the dedup_key
    schema: str
    table: str
    col: str
    kind: str = "timestamptz"
    sla_s: int = DEFAULT_SLA_S
    level: str = "warning"       # notify level on breach: info | warning | critical
    is_matview: bool = False     # documentation only; query path is identical
    note: str = ""
    discovered: bool = False     # True if auto-discovered rather than explicitly listed

    def dedup_key(self) -> str:
        return f"freshness:{self.name}"


# ── The declarative SLA table ────────────────────────────────────────────────
# SLA = max tolerable staleness (max-staleness, NOT the cadence). It is set to a safe
# multiple of the observed write cadence so normal jitter never flaps. Tune freely.
EXPLICIT_STREAMS: List[Stream] = [
    # High-frequency core telemetry — a stall here is a real outage → critical.
    Stream("telemetry.energy",  "telemetry", "energy",  "ts", "timestamptz",   600, "critical",
           note="smart-plug power poller (~2m cadence)"),
    Stream("telemetry.weather", "telemetry", "weather", "ts", "timestamptz",   600, "critical",
           note="weather station (~1m cadence)"),
    Stream("telemetry.network", "telemetry", "network", "ts", "timestamptz",   900, "warning",
           note="UniFi client poller"),
    Stream("snmp_metrics",      "public",    "snmp_metrics", "timestamp", "timestamptz", 900, "warning",
           note="SNMP poller (was silently dead 2 weeks) — public schema, 'timestamp' col (~2m)"),
    Stream("telemetry.av_state","telemetry", "av_state","ts", "timestamptz", 3600, "warning",
           note="AV device state; nova_av_poller now heartbeats off/unreachable devices "
                "every ~10m, so >1h stale means the POLLER died (not just gear off)"),
    # jarvis_brain heartbeats the current activity every ~15m (was change-only, went silent
    # 2026-09-02). >1h stale now means the classifier is dead/stuck, not merely steady.
    Stream("telemetry.activity", "telemetry", "activity", "ts", "timestamptz", 3600, "warning",
           note="jarvis activity classifier; heartbeats every ~15m"),
    # TRUE event table: only power transitions land here, so it is legitimately silent for
    # days when gear is off. NOT a liveness signal (av_state covers poller liveness). Long
    # SLA + info so a genuinely-frozen scraper still eventually surfaces without false pages.
    Stream("telemetry.device_power_events", "telemetry", "device_power_events", "ts", "timestamptz",
           7 * 24 * 3600, "info", note="AV power transitions; event-driven, av_state is the liveness signal"),

    # Dashboard streams the nova-control-web UI renders (the pool that wedged 32 days).
    Stream("dashboard_snapshots", "public", "dashboard_snapshots", "ts", "epoch", 1800, "warning",
           note="host CPU/mem snapshot; ts is epoch-seconds double"),
    Stream("dashboard_memory_count_history", "public", "dashboard_memory_count_history", "ts", "epoch", 1800, "warning",
           note="vector memory count; ts is epoch-seconds double"),
    Stream("dashboard_cost_history", "public", "dashboard_cost_history", "date", "date_text", 2 * 24 * 3600, "warning",
           note="daily LLM cost rollup; 'date' is YYYY-MM-DD text"),

    # Informational — long SLA, low severity; we still want to know if it flatlines.
    Stream("inference_latency", "public", "inference_latency", "timestamp", "timestamptz", 7 * 24 * 3600, "info",
           note="LLM latency samples; informational, only alerts on a very long silence"),

    # Derived materialized views — stale means the refresh job (nova_matview_refresh) died.
    Stream("telemetry.energy_hourly", "telemetry", "energy_hourly", "hour", "timestamptz", 3 * 3600, "warning",
           is_matview=True, note="matview; stale => matview refresh stopped"),
    Stream("telemetry.weather_daily", "telemetry", "weather_daily", "day", "date", 2 * 24 * 3600, "warning",
           is_matview=True, note="matview; 'day' is a date"),

    # Batch/periodic producers.
    Stream("telemetry.backup_runs",   "telemetry", "backup_runs",   "ts", "timestamptz", 2 * 24 * 3600, "warning",
           note="backup job results; nightly-ish"),
    Stream("telemetry.chp_incidents", "telemetry", "chp_incidents", "ts", "timestamptz", 24 * 3600, "info",
           note="CHP incident scraper; event-driven (only writes when there are incidents), "
                "so a quiet stretch is normal — 24h/info avoids false pages"),
]

# Explicit (schema, table) pairs are skipped during auto-discovery so their tuned SLA wins.
_EXPLICIT_TABLES = {(s.schema, s.table) for s in EXPLICIT_STREAMS}


def age_sql(stream: Stream) -> str:
    """SQL that returns ONE float: the stream's age in seconds (NULL if the table is empty).

    Identifiers are quoted; every value in a Stream is code-defined (never user input),
    so there is no injection surface here — but we quote anyway for correctness.
    """
    ident = f'"{stream.schema}"."{stream.table}"'
    c = f'"{stream.col}"'
    if stream.kind == "timestamptz":
        expr = f"EXTRACT(EPOCH FROM now() - max({c}))"
    elif stream.kind == "timestamp":
        expr = f"EXTRACT(EPOCH FROM now() - max({c})::timestamptz)"
    elif stream.kind == "epoch":
        # column already holds epoch seconds (double) — subtract from now()'s epoch.
        expr = f"EXTRACT(EPOCH FROM now()) - max({c})"
    elif stream.kind == "date":
        expr = f"EXTRACT(EPOCH FROM now() - max({c})::timestamptz)"
    elif stream.kind == "date_text":
        expr = f"EXTRACT(EPOCH FROM now() - to_date(max({c}), 'YYYY-MM-DD')::timestamptz)"
    else:
        raise ValueError(f"unknown timestamp kind: {stream.kind!r}")
    return f"SELECT {expr} FROM {ident}"


def discover_streams(conn) -> List[Stream]:
    """Auto-register every telemetry.* base table with a `ts` timestamptz column.

    Partition children (…_YYYYMM) are skipped — their parent is already covered — as
    are any table already in EXPLICIT_STREAMS. Never raises: discovery failure just
    means we fall back to the explicit list.
    """
    out: List[Stream] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT table_name FROM information_schema.columns
                   WHERE table_schema = 'telemetry'
                     AND column_name = 'ts'
                     AND data_type = 'timestamp with time zone'
                   ORDER BY table_name""")
            for (tbl,) in cur.fetchall():
                # skip monthly partition children like energy_202609
                if len(tbl) > 7 and tbl[-7] == "_" and tbl[-6:].isdigit():
                    continue
                if ("telemetry", tbl) in _EXPLICIT_TABLES:
                    continue
                out.append(Stream(
                    name=f"telemetry.{tbl}", schema="telemetry", table=tbl, col="ts",
                    kind="timestamptz", sla_s=DEFAULT_SLA_S, level="warning",
                    discovered=True, note="auto-discovered telemetry.* (default SLA)"))
    except Exception as e:                          # pragma: no cover - defensive
        print(f"[freshness] discovery failed (continuing with explicit list): {e}", flush=True)
    return out


def build_streams(conn) -> List[Stream]:
    """Full watch list: explicit (tuned) streams first, then auto-discovered ones."""
    return list(EXPLICIT_STREAMS) + discover_streams(conn)


@dataclass
class Result:
    stream: Stream
    age_s: Optional[float] = None
    breach: bool = False
    error: Optional[str] = None

    @property
    def reason(self) -> str:
        if self.error:
            return f"query error: {self.error}"
        if self.age_s is None:
            return "NO DATA (table empty or max(ts) is NULL)"
        return f"age {int(self.age_s)}s > SLA {self.stream.sla_s}s"


def check_stream(conn, stream: Stream) -> Result:
    """Compute one stream's age and decide breach. NEVER raises — errors are captured.

    An empty table (age NULL) is treated as a breach: a stream that has *no* recent
    row is exactly the silent-failure we exist to catch.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(age_sql(stream))
            row = cur.fetchone()
        age = None if not row or row[0] is None else float(row[0])
        if age is None:
            return Result(stream, age_s=None, breach=True)
        return Result(stream, age_s=age, breach=age > stream.sla_s)
    except Exception as e:
        # Roll back so a failed statement doesn't poison the rest of the pass.
        try:
            conn.rollback()
        except Exception:
            pass
        return Result(stream, error=str(e).strip().splitlines()[0] if str(e).strip() else repr(e))


def _fmt_age(age_s: Optional[float]) -> str:
    if age_s is None:
        return "   n/a"
    m, s = divmod(int(age_s), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d:
        return f"{d}d{h:02d}h"
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def log_action(conn, description: str, outcome: str) -> None:
    """Best-effort audit row in claude_actions (action_type='feature')."""
    try:
        with conn.cursor() as cur:
            # claude_actions.session_id has an FK to claude_sessions — keep a stable row.
            cur.execute(
                "INSERT INTO claude_sessions (session_id, status) "
                "VALUES ('nova-freshness-monitor','active') ON CONFLICT (session_id) DO NOTHING")
            cur.execute(
                """INSERT INTO claude_actions (session_id, action_type, target, description, outcome)
                   VALUES (%s, 'feature', 'nova_freshness_monitor', %s, %s)""",
                ("nova-freshness-monitor", description[:2000], outcome[:500]))
        conn.commit()
    except Exception as e:                          # pragma: no cover - defensive
        print(f"[freshness] claude_actions log failed: {e}", flush=True)


def run_once(conn, notify_fn: Optional[Callable] = None, dry_run: bool = False) -> dict:
    """One full pass over every stream. Returns a summary dict; never raises."""
    notify_fn = notify_fn or _notify
    streams = build_streams(conn)
    results: List[Result] = [check_stream(conn, s) for s in streams]

    breaches = [r for r in results if r.breach]
    errors = [r for r in results if r.error]

    # Notify per breaching / erroring stream (deduped per stream at the notifier).
    if not dry_run:
        for r in breaches:
            if r.error:
                continue  # errored streams are reported below, not double-notified here
            try:
                notify_fn(
                    f"Stale data stream: {r.stream.name}",
                    body=(f"{r.stream.name} — {r.reason}. "
                          f"The writer for this stream has likely stopped. {r.stream.note}"),
                    level=r.stream.level,
                    category="freshness",
                    source="nova_freshness_monitor.py",
                    dedup_key=r.stream.dedup_key(),
                    meta={"stream": r.stream.name, "age_s": r.age_s,
                          "sla_s": r.stream.sla_s, "kind": r.stream.kind},
                )
            except Exception as e:                  # notifier must never crash us
                print(f"[freshness] notify failed for {r.stream.name}: {e}", flush=True)
        for r in errors:
            try:
                notify_fn(
                    f"Freshness check errored: {r.stream.name}",
                    body=f"Could not evaluate {r.stream.name}: {r.error}",
                    level="warning", category="freshness",
                    source="nova_freshness_monitor.py",
                    dedup_key=f"freshness:error:{r.stream.name}",
                )
            except Exception:
                pass

    # Concise report to stdout (captured into the launchd log).
    print(f"[freshness] {len(results)} streams checked | "
          f"{len(breaches)} breach(es) | {len(errors)} error(s)"
          f"{' | DRY-RUN (no notifications)' if dry_run else ''}", flush=True)
    for r in sorted(results, key=lambda x: (not x.breach, x.stream.name)):
        flag = "BREACH " if r.breach else "  ok   "
        src = "disc" if r.stream.discovered else "expl"
        print(f"  {flag}[{src}] {r.stream.name:<42} age={_fmt_age(r.age_s):>7} "
              f"sla={_fmt_age(r.stream.sla_s):>7}"
              + (f"  <- {r.reason}" if r.breach else ""), flush=True)

    return {
        "checked": len(results),
        "breaches": [r.stream.name for r in breaches],
        "errors": [r.stream.name for r in errors],
        "results": results,
    }


def _connect(dsn: str = DSN):
    """Connect with a small retry — a transient PG hiccup shouldn't skip a whole pass."""
    import psycopg2
    last = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            c = psycopg2.connect(dsn, connect_timeout=10)
            c.autocommit = False
            return c
        except Exception as e:
            last = e
            print(f"[freshness] connect attempt {attempt}/{CONNECT_ATTEMPTS} failed: {e}", flush=True)
            if attempt < CONNECT_ATTEMPTS:
                time.sleep(CONNECT_BACKOFF_S * attempt)
    raise last


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    loop = "--loop" in argv

    if loop:
        conn = None
        while True:
            try:
                if conn is None or conn.closed:
                    conn = _connect()
                run_once(conn, dry_run=dry_run)
            except Exception as e:
                print(f"[freshness] cycle error: {e}", flush=True)
                try:
                    if conn:
                        conn.close()
                except Exception:
                    pass
                conn = None
            time.sleep(INTERVAL_S)

    # default: single pass (launchd StartInterval re-invokes us every 900s)
    conn = _connect()
    try:
        summary = run_once(conn, dry_run=dry_run)
        log_action(
            conn,
            description=f"freshness pass: {summary['checked']} streams, "
                        f"breaches={summary['breaches']}, errors={summary['errors']}",
            outcome=("clean" if not summary["breaches"] and not summary["errors"]
                     else f"{len(summary['breaches'])} breach / {len(summary['errors'])} error"),
        )
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
