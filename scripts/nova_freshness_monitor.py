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

# ── Transition-based alerting (noise control) ────────────────────────────────
# The monitor runs every 15 min (launchd StartInterval 900). Before this change it
# re-notified EVERY breaching stream on EVERY pass, so a single chronically-dead
# stream produced ~96 alerts/day; the 8 currently-dead streams alone generated ~768
# freshness alerts/24h — 26% of ALL alert traffic. That is pure noise: the operator
# already knows the stream is dead after the first page.
#
# We now alert on STATE TRANSITIONS, not on every observation, persisting per-stream
# state in nova_ops.public.freshness_state (crash-safe: state survives restarts):
#   * fresh -> stale        : ONE alert at the stream's real severity (the news).
#   * stale (still)         : re-escalate at most once per RE_ESCALATE_S (daily),
#                             as a low-severity "still stale" reminder — never hourly.
#   * stale -> fresh        : ONE low-severity "recovered" note, then reset so a
#                             future staleness alerts again.
#   * first-ever sight stale: initialise state SILENTLY as already-known-dead (no
#                             page) — this is how long-dead producers like
#                             telemetry.activity (dead since 2026-09-12) stop paging
#                             hourly; they surface as a single daily reminder 24h later
#                             via the re-escalation path, not a flood.
STATE_SCHEMA = "public"                 # nova_ops DB, public schema
STATE_TABLE = "freshness_state"
RE_ESCALATE_S = 24 * 3600               # while stale, re-remind at most once per 24h

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

# Human-muted streams: still checked and state-recorded, but NEVER paged/re-escalated.
# Use this ONLY for a stream whose staleness has a known, accepted, out-of-band cause
# (a device-side producer we can't restart from here) — mute the noise, don't hide a
# fixable outage. Map stream_name -> reason (shown in the recorded state, so the "why"
# survives). Re-flowing data will still show as "muted"; remove the entry to un-mute.
MUTED_STREAMS = {
    # Approved by Jordan 2026-09-16 (co-agency proposal #14). Root cause: the Apple
    # Shortcuts automation that POSTs HomeKit accessory data to nova-homekit-receiver
    # stopped feeding it on 2026-09-12, so the battery poller sees 0 devices and this
    # stream reads stale. The receiver/poller are healthy; the fix is on the iOS side.
    # Un-mute once the Shortcuts push is restored.
    "telemetry.battery": "HomeKit Shortcuts push stopped 2026-09-12 (device-side); poller healthy",
}


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


def ensure_state_table(conn) -> bool:
    """Idempotently create the per-stream state store. Never raises."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS {STATE_SCHEMA}.{STATE_TABLE} (
                        stream_name     text PRIMARY KEY,
                        state           text NOT NULL DEFAULT 'fresh',   -- 'fresh' | 'stale'
                        problem_kind    text,                            -- 'stale' | 'nodata' | 'error'
                        first_stale_ts  timestamptz,                     -- when this episode began
                        last_alerted_ts timestamptz,                     -- last page/reminder emitted
                        last_alert_num  integer NOT NULL DEFAULT 0,      -- escalations this episode
                        last_age_s      double precision,
                        last_reason     text,
                        last_checked_ts timestamptz NOT NULL DEFAULT now(),
                        updated_at      timestamptz NOT NULL DEFAULT now()
                    )""")
        conn.commit()
        return True
    except Exception as e:                          # pragma: no cover - defensive
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[freshness] state table ensure failed (alerting will fall back to "
              f"per-pass, but continuing): {e}", flush=True)
        return False


def load_states(conn) -> dict:
    """Load every stored per-stream state row into a dict keyed by stream_name."""
    out: dict = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT stream_name, state, problem_kind, first_stale_ts,
                           last_alerted_ts, last_alert_num
                    FROM {STATE_SCHEMA}.{STATE_TABLE}""")
            for name, state, kind, first_ts, last_ts, num in cur.fetchall():
                out[name] = {"state": state, "problem_kind": kind,
                             "first_stale_ts": first_ts, "last_alerted_ts": last_ts,
                             "last_alert_num": num or 0}
    except Exception as e:                          # pragma: no cover - defensive
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[freshness] state load failed (treating all as unknown): {e}", flush=True)
    return out


def decide_action(prior: Optional[dict], result: Result, now, since_alert_s):
    """Pure transition logic. Returns (action, problem_kind).

    action ∈ {none, init_silent, transition, reescalate, suppress, recovered}.
    `since_alert_s` = seconds since last alert for this episode (None if never/unknown).
    """
    problem = bool(result.breach or result.error)
    kind = ("error" if result.error
            else "nodata" if result.age_s is None
            else "stale") if problem else None

    if prior is None:                       # never seen before
        # First-ever sight of an already-broken stream = KNOWN-DEAD baseline: record
        # it, do NOT page. (This is what tames long-dead producers like
        # telemetry.activity.) A daily reminder follows via the re-escalation path.
        return ("init_silent" if problem else "none"), kind

    if prior["state"] != "stale":           # was fresh / healthy
        return ("transition" if problem else "none"), kind

    # was stale/problem
    if problem:
        if since_alert_s is None or since_alert_s >= RE_ESCALATE_S:
            return "reescalate", kind
        return "suppress", kind
    return "recovered", None                # stale -> fresh


def _upsert_state(conn, name, state, kind, first_ts, last_ts, num, age_s, reason):
    """Crash-safe state write, committed immediately so a mid-pass crash can't double-page."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {STATE_SCHEMA}.{STATE_TABLE}
                        (stream_name, state, problem_kind, first_stale_ts, last_alerted_ts,
                         last_alert_num, last_age_s, last_reason, last_checked_ts, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now(), now())
                    ON CONFLICT (stream_name) DO UPDATE SET
                        state=EXCLUDED.state, problem_kind=EXCLUDED.problem_kind,
                        first_stale_ts=EXCLUDED.first_stale_ts,
                        last_alerted_ts=EXCLUDED.last_alerted_ts,
                        last_alert_num=EXCLUDED.last_alert_num,
                        last_age_s=EXCLUDED.last_age_s, last_reason=EXCLUDED.last_reason,
                        last_checked_ts=now(), updated_at=now()""",
                (name, state, kind, first_ts, last_ts, num, age_s, reason))
        conn.commit()
    except Exception as e:                          # pragma: no cover - defensive
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[freshness] state upsert failed for {name}: {e}", flush=True)


def run_once(conn, notify_fn: Optional[Callable] = None, dry_run: bool = False) -> dict:
    """One full pass over every stream. Returns a summary dict; never raises."""
    notify_fn = notify_fn or _notify
    streams = build_streams(conn)
    results: List[Result] = [check_stream(conn, s) for s in streams]

    breaches = [r for r in results if r.breach]
    errors = [r for r in results if r.error]

    # Transition-based alerting. Ensure the state store exists (idempotent), load it,
    # and take one authoritative `now` from PG so the escalation clock is immune to any
    # host clock skew. In dry-run we read state to SHOW decisions but never write/notify.
    have_state = ensure_state_table(conn)
    states = load_states(conn) if have_state else {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT now()")
            now = cur.fetchone()[0]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

    actions: dict = {}   # stream_name -> action string (for the report)
    for r in results:
        name = r.stream.name
        prior = states.get(name)
        since_alert_s = None
        if prior and prior.get("last_alerted_ts") is not None:
            since_alert_s = (now - prior["last_alerted_ts"]).total_seconds()
        action, kind = decide_action(prior, r, now, since_alert_s)
        actions[name] = action

        if dry_run or not have_state:
            continue

        first_ts = (prior["first_stale_ts"] if prior and prior.get("first_stale_ts")
                    else now)
        num = (prior["last_alert_num"] if prior else 0)

        if name in MUTED_STREAMS:
            # Human-muted: record observed staleness but emit NOTHING and never
            # re-escalate. Checked every pass BEFORE any notify branch, so it holds
            # regardless of the transition state it would otherwise be in.
            _upsert_state(conn, name, "muted", kind, first_ts,
                          (prior["last_alerted_ts"] if prior else now), num,
                          r.age_s, f"muted: {MUTED_STREAMS[name]}")
            actions[name] = "muted"
            continue

        if action == "none":
            _upsert_state(conn, name, "fresh", None, None, None, 0, r.age_s, r.reason if r.breach else "ok")
            continue
        if action == "suppress":
            # stay stale, keep episode fields, just refresh observed age/checked-at
            _upsert_state(conn, name, "stale", kind, first_ts,
                          prior["last_alerted_ts"], num, r.age_s, r.reason)
            continue
        if action == "init_silent":
            # known-dead baseline: record as stale, start the daily clock at `now`,
            # emit NOTHING. (num=0 so the first reminder lands ~RE_ESCALATE_S later.)
            _upsert_state(conn, name, "stale", kind, now, now, 0, r.age_s, r.reason)
            continue

        # ---- actions that emit a notification ----
        if action == "transition":
            new_num = 1
            _upsert_state(conn, name, "stale", kind, now, now, new_num, r.age_s, r.reason)
            title = (f"Freshness check errored: {name}" if kind == "error"
                     else f"Stale data stream: {name}")
            body = (f"Could not evaluate {name}: {r.error}" if kind == "error"
                    else (f"{name} — {r.reason}. The writer for this stream has likely "
                          f"stopped. {r.stream.note}"))
            level = r.stream.level
            dedup = r.stream.dedup_key()
        elif action == "reescalate":
            new_num = num + 1
            _upsert_state(conn, name, "stale", kind, first_ts, now, new_num, r.age_s, r.reason)
            still = _fmt_age((now - first_ts).total_seconds())
            title = f"Still stale ({still}): {name}"
            body = (f"{name} is STILL {kind} after {still} ({r.reason}). "
                    f"Reminder #{new_num}; next in ~24h until it recovers. {r.stream.note}")
            level = "info"                  # ongoing reminder — low severity, never a page
            dedup = f"freshness:{name}:reesc:{new_num}"
        elif action == "recovered":
            _upsert_state(conn, name, "fresh", None, None, None, 0, r.age_s, "recovered")
            title = f"Recovered: {name}"
            body = f"{name} is fresh again (age {_fmt_age(r.age_s)}). Freshness state reset."
            level = "info"
            dedup = f"freshness:recovered:{name}:{int(time.time())}"
        else:
            continue

        try:
            notify_fn(title, body=body, level=level, category="freshness",
                      source="nova_freshness_monitor.py", dedup_key=dedup,
                      meta={"stream": name, "age_s": r.age_s, "sla_s": r.stream.sla_s,
                            "kind": r.stream.kind, "action": action})
        except Exception as e:              # notifier must never crash us
            print(f"[freshness] notify failed for {name}: {e}", flush=True)

    emitted = [n for n, a in actions.items()
               if a in ("transition", "reescalate", "recovered")]
    # Concise report to stdout (captured into the launchd log).
    print(f"[freshness] {len(results)} streams checked | "
          f"{len(breaches)} breach(es) | {len(errors)} error(s) | "
          f"{len(emitted)} alert(s) emitted"
          f"{' | DRY-RUN (no notifications/state writes)' if dry_run else ''}", flush=True)
    for r in sorted(results, key=lambda x: (not x.breach, x.stream.name)):
        flag = "BREACH " if r.breach else "  ok   "
        src = "disc" if r.stream.discovered else "expl"
        act = actions.get(r.stream.name, "?")
        print(f"  {flag}[{src}] {r.stream.name:<42} age={_fmt_age(r.age_s):>7} "
              f"sla={_fmt_age(r.stream.sla_s):>7} act={act:<11}"
              + (f"  <- {r.reason}" if r.breach else ""), flush=True)

    return {
        "checked": len(results),
        "breaches": [r.stream.name for r in breaches],
        "errors": [r.stream.name for r in errors],
        "emitted": emitted,
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
