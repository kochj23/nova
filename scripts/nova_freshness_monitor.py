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

THE ONE SILENCE DETECTOR (organ-audit merge M1, 2026-10-09)
------------------------------------------------------------
Four organs used to ask "has this gone quiet?" about overlapping data. They now all run
from this one entry point, one schedule, one alert per fact:

  * FEEDS (was nova_watchtower.py's feed half): per-feed Streams with a WHERE filter and
    an `owner` (the device a feed hangs off). Same SQL, same thresholds. They alert
    through the transition logic below; several feeds of one owner going stale in the
    same pass collapse into ONE "likely root cause" alert. Every pass still writes the
    telemetry.net_liveness tier='feed' sample and the net_problems 'stale:<feed>' episode
    the weekly network-health report reads. Watchtower keeps inventory + wired liveness.
  * PRESENCE-METHOD SILENCE (was nova_negative_space.py's sensor_quiet check): same SQL
    (each method against its own 7-day rhythm), same message, same source, category and
    dedup key, so nova_buick8_log's feed of these events is unchanged.
  * LEARNED CADENCE (was nova_cadence_watch.py): per-stream learned interval, written to
    cadence_state, at most once an hour inside the normal pass, or on demand with
    --learn. The epistemic split is a per-stream attribute: this pass only ever records
    a source as OK or SILENT (no witness: cause unknown, never "missing"); MISSING needs
    a third-party witness and goes through nova_cadence_watch.record_missing(). A SILENT
    transition is not re-alerted when a tuned freshness SLA on the same table would
    already have fired first (one alert per fact); its memory note is still written.
  * The Dead man's switch's delivery check went to nova_output_drift.py, not here.

    python3 nova_freshness_monitor.py            # one pass, notify on breach, exit
    python3 nova_freshness_monitor.py --dry-run  # one pass, print report, DO NOT notify
    python3 nova_freshness_monitor.py --loop      # run forever every INTERVAL_S
    python3 nova_freshness_monitor.py --learn     # cadence pass only (cadence_state)
    python3 nova_freshness_monitor.py --learn --dry-run

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
    where: str = ""              # optional code-defined row filter (feeds, e.g. one climate source)
    owner: Optional[str] = None  # device a feed hangs off: stale feeds of one owner collapse
    alert: bool = True           # False = tracked + recorded, but another stream alerts this fact
    feed_key: Optional[str] = None  # set for watchtower feeds: net_liveness / net_problems key

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

# ── Feeds (absorbed from nova_watchtower.py, 2026-10-09) ───────────────────────
# (key, schema, table, ts col, WHERE filter, stale-after minutes, owner). Same SQL and
# thresholds watchtower used. 'weather_station' is the same fact as the explicit
# telemetry.weather stream (tighter SLA, critical), so it is recorded for the weekly report
# but never alerts on its own (one alert per fact).
_FEEDS = [
    ("climate:zigbee",     "telemetry", "climate", "ts", "source='zigbee'", 30, "zigbee-coordinator"),
    ("climate:hue_bridge", "telemetry", "climate", "ts", "source='hue_bridge'", 30, "hue-bridge"),
    ("climate:weather",    "telemetry", "climate", "ts", "source='weather_station'", 20, "weather-station"),
    ("climate:fp300",      "telemetry", "climate", "ts", "source='fp300'", 30, None),
    ("climate:homekit",    "telemetry", "climate", "ts", "source='homekit'", 30, None),
    ("weather_station",    "telemetry", "weather", "ts", "", 20, "weather-station"),
    ("air_quality",        "telemetry", "air_quality", "ts", "", 45, None),
    ("hue_light_state",    "public", "hue_light_state", "polled_at", "", 20, "hue-bridge"),
    ("ha_sensors",         "telemetry", "ha_sensors", "ts", "", 20, None),
    # last_heard, not ts (ts only advances on a brand-new node)
    ("lora_mesh",          "telemetry", "mesh_nodes", "last_heard", "", 360, None),
    # a SUCCESSFUL nightly (ok=true) within 26h
    ("nas_backup",         "telemetry", "backup_runs", "ts", "ok", 1560, "nas-backup"),
]
_FEED_ALIASES = {"weather_station"}      # fact already alerted by telemetry.weather
FEED_STREAMS: List[Stream] = [
    Stream(f"feed:{key}", schema, table, col, "timestamptz", mins * 60, "warning",
           where=where, owner=owner, alert=key not in _FEED_ALIASES, feed_key=key,
           note=f"feed (was watchtower), stale after {mins} min"
                + (f"; hangs off {owner}" if owner else ""))
    for key, schema, table, col, where, mins, owner in _FEEDS
]

# Explicit and feed (schema, table) pairs are skipped during auto-discovery so their tuned
# SLA wins (a feed's threshold is always tighter than the 24h discovery default).
_EXPLICIT_TABLES = {(s.schema, s.table) for s in EXPLICIT_STREAMS + FEED_STREAMS}

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
    # 2026-10-08: the only producer, nova_jarvis_brain, was retired (its consumer, the chatroom, was
    # archived 2026-10-06; nothing else reads telemetry.activity). Silence here is intended.
    "telemetry.activity": "producer nova_jarvis_brain retired 2026-10-08 (plist in retired-launchagents/)",
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
    # `where` is code-defined (FEED_STREAMS), never user input.
    return f"SELECT {expr} FROM {ident}" + (f" WHERE {stream.where}" if stream.where else "")


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
    """Full watch list: explicit (tuned) streams, then feeds, then auto-discovered ones."""
    return list(EXPLICIT_STREAMS) + list(FEED_STREAMS) + discover_streams(conn)


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


_LEVEL_RANK = {"info": 0, "warning": 1, "critical": 2}


def collapse_by_owner(outbox: list) -> list:
    """Root-cause collapse (carried over from watchtower): when two or more feeds that hang
    off the same owner go stale in the same pass, send ONE alert naming the owner instead of
    one per feed. Everything else passes through unchanged and in order. Pure."""
    groups: dict = {}
    for m in outbox:
        owner = m["stream"].owner
        if owner and m["action"] == "transition":
            groups.setdefault(owner, []).append(m)
    out, done = [], set()
    for m in outbox:
        owner = m["stream"].owner
        grp = groups.get(owner) if owner and m["action"] == "transition" else None
        if not grp or len(grp) < 2:
            out.append(m)
            continue
        if owner in done:
            continue
        done.add(owner)
        names = [g["stream"].name for g in grp]
        out.append({
            "stream": grp[0]["stream"], "action": "transition",
            "title": f"Stale feeds, likely root cause: {owner}",
            "body": (f"{len(grp)} feeds that hang off {owner} went stale together: "
                     f"{', '.join(names)}. Check {owner} first. "
                     + " ".join(f"[{g['stream'].name}: {_fmt_age(g['meta'].get('age_s')).strip()}]"
                                for g in grp)),
            "level": max((g["level"] for g in grp), key=lambda lv: _LEVEL_RANK.get(lv, 1)),
            "dedup": f"freshness:owner:{owner}",
            "meta": {"stream": ",".join(names), "owner": owner, "streams": names,
                     "action": "transition"},
        })
    return out


def seed_feed_states(conn, streams: List[Stream], states: dict) -> None:
    """Handover from watchtower (2026-10-09). A feed this monitor has never seen inherits
    watchtower's episode: an open net_problems 'stale:<feed>' row was already alerted, so it
    stays a silent known-dead baseline; otherwise the feed starts as 'fresh', so a stale
    reading now is news and alerts. Read-only; never raises."""
    new = [s for s in streams if s.feed_key and s.name not in states]
    if not new:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key FROM telemetry.net_problems "
                        "WHERE status='open' AND left(key, 6) = 'stale:'")
            open_now = {row[0] for row in cur.fetchall()}
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[freshness] feed handover read failed (first sight stays silent): {e}", flush=True)
        return
    for s in new:
        if f"stale:{s.feed_key}" not in open_now:
            states[s.name] = {"state": "fresh", "problem_kind": None, "first_stale_ts": None,
                              "last_alerted_ts": None, "last_alert_num": 0}


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
    if have_state:
        seed_feed_states(conn, streams, states)
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
    outbox: list = []    # notifications, sent after the loop so same-owner feeds can collapse
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

        if not r.stream.alert:
            # state tracked above, but another stream already alerts this same fact
            actions[name] = f"{action}(q)"
            continue
        outbox.append({"stream": r.stream, "action": action, "title": title, "body": body,
                       "level": level, "dedup": dedup,
                       "meta": {"stream": name, "age_s": r.age_s, "sla_s": r.stream.sla_s,
                                "kind": r.stream.kind, "action": action}})

    for msg in collapse_by_owner(outbox):
        try:
            notify_fn(msg["title"], body=msg["body"], level=msg["level"], category="freshness",
                      source="nova_freshness_monitor.py", dedup_key=msg["dedup"],
                      meta=msg["meta"])
        except Exception as e:              # notifier must never crash us
            print(f"[freshness] notify failed for {msg['meta'].get('stream')}: {e}", flush=True)

    emitted = [n for n, a in actions.items()
               if a in ("transition", "reescalate", "recovered")]
    # Concise report to stdout (captured into the launchd log).
    print(f"[freshness] {len(results)} streams checked | "
          f"{len(breaches)} breach(es) | {len(errors)} error(s) | "
          f"{len(emitted)} alert(s) emitted"
          f"{' | DRY-RUN (no notifications/state writes)' if dry_run else ''}", flush=True)
    for r in sorted(results, key=lambda x: (not x.breach, x.stream.name)):
        flag = "BREACH " if r.breach else "  ok   "
        src = "disc" if r.stream.discovered else "feed" if r.stream.feed_key else "expl"
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


def _rollback(conn) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


# ── Feeds: the weekly network-health report's time-series (was watchtower) ───────
def record_feeds(conn, results: List[Result], dry_run: bool = False) -> dict:
    """For every feed stream: one telemetry.net_liveness sample (tier='feed') and the
    net_problems 'stale:<feed>' episode opened/cleared — the rows nova_network_health reads.
    Watchtower owns the other net_problems keys ('down:<mac>'); only 'stale:' keys are
    touched here. Never raises."""
    feeds = [r for r in results if r.stream.feed_key]
    out = {"opened": [], "cleared": []}
    if dry_run or not feeds:
        return out
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key FROM telemetry.net_problems "
                        "WHERE status='open' AND left(key, 6) = 'stale:'")
            open_now = {row[0] for row in cur.fetchall()}
            current = set()
            for r in feeds:
                key = r.stream.feed_key
                pk = f"stale:{key}"
                current.add(pk)
                fresh = not (r.breach or r.error)
                cur.execute("INSERT INTO telemetry.net_liveness (mac,name,tier,online) "
                            "VALUES (%s,%s,'feed',%s)", (f"feed:{key}", key, fresh))
                if not fresh and pk not in open_now:
                    detail = ("no data ever" if r.age_s is None
                              else f"{r.age_s / 60:.0f} min old (> {r.stream.sla_s // 60} min)")
                    cur.execute(
                        """INSERT INTO telemetry.net_problems (key,kind,entity,tier,detail)
                           VALUES (%s,%s,%s,%s,%s) ON CONFLICT (key) DO UPDATE
                           SET status='open', detail=EXCLUDED.detail, cleared_at=NULL, opened_at=now()""",
                        (pk, "feed_stale", key, "sensor_feed", f"feed '{key}' {detail}"))
                    out["opened"].append(key)
            # clear recovered feeds, and any open episode for a feed that no longer exists
            for pk in sorted(open_now):
                r = next((x for x in feeds if f"stale:{x.stream.feed_key}" == pk), None)
                if r is None or not (r.breach or r.error):
                    cur.execute("UPDATE telemetry.net_problems SET status='cleared', cleared_at=now() "
                                "WHERE key=%s", (pk,))
                    out["cleared"].append(pk[6:])
        conn.commit()
    except Exception as e:
        _rollback(conn)
        print(f"[freshness] feed liveness record failed: {e}", flush=True)
    return out


# ── Presence-method silence (was nova_negative_space.py's sensor_quiet) ──────────
# 2026-10-06: judge each method against its OWN rhythm. Event-driven sensors (the outdoor front
# motion sensor, AV power) are legitimately silent for hours — 'ha_motion' was paged as broken while
# reporting 64 times that week. Quiet now = the current gap exceeds both 6 h and 1.5x the longest
# gap that method had between events in the past 7 days.
PRESENCE_QUIET_SQL = """
    WITH ev AS (
        SELECT method, ts, ts - lag(ts) OVER (PARTITION BY method ORDER BY ts) AS between
        FROM telemetry.presence
        WHERE ts > now() - interval '7 days'),
    m AS (
        SELECT method, max(ts) AS last, now() - max(ts) AS gap,
               coalesce(max(between), interval '0') AS longest
        FROM ev GROUP BY 1)
    SELECT method, last, gap FROM m
    WHERE gap > greatest(interval '6 hours', longest * 1.5)
    ORDER BY 3 DESC"""
PRESENCE_DEDUP_WINDOW_S = 28800          # negative space's re-notify window (8h)


def presence_quiet_message(method, last, gap) -> str:
    """The exact text negative space emitted (nova_buick8_log parses it)."""
    return (f"Presence method '{method}' has reported nothing for {str(gap).split('.')[0]} "
            f"(last: {last:%Y-%m-%d %H:%M}). A sensor that goes silent is usually broken, "
            f"not observing stillness.")


def check_presence_silence(conn, dry_run: bool = False,
                           notify_fn: Optional[Callable] = None) -> list:
    """Presence methods that went quiet against their own rhythm. Alerts exactly as negative
    space did (its alert_findings: source nova_negative_space.py, category security, stable
    negspace:sensor_quiet:* key, 8h window). A finding whose key was already SENT inside the
    window is not re-emitted (the notifier would fold it anyway), so the bus is not refilled
    every pass. Returns [(kind, msg)]. Never raises."""
    try:
        with conn.cursor() as cur:
            cur.execute(PRESENCE_QUIET_SQL)
            rows = cur.fetchall()
    except Exception as e:
        _rollback(conn)
        print(f"[freshness] presence silence check failed: {e}", flush=True)
        return []
    findings = [("sensor_quiet", presence_quiet_message(m, last, gap)) for m, last, gap in rows]
    for _, msg in findings:
        print(f"  QUIET  [presence] {msg}", flush=True)
    if dry_run or not findings:
        return findings
    try:
        import nova_negative_space as ns
    except Exception as e:                          # pragma: no cover - defensive
        print(f"[freshness] negative-space helpers unavailable: {e}", flush=True)
        return findings
    due = []
    for kind, msg in findings:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM telemetry.events WHERE dedup_key=%s AND status='sent' "
                            "AND ts > now() - make_interval(secs => %s) LIMIT 1",
                            (ns.negspace_dedup_key(kind, msg), PRESENCE_DEDUP_WINDOW_S))
                if cur.fetchone():
                    continue
        except Exception:
            _rollback(conn)
        due.append((kind, msg))
    if due:
        ns.alert_findings(due, notify=notify_fn)
    return findings


# ── Learned cadence (was nova_cadence_watch.py) ──────────────────────────────────
CADENCE_EVERY_S = 3300                   # inside the 15-min pass, learn at most ~hourly


def cadence_due(conn) -> bool:
    """True when cadence_state is missing or was last written >= CADENCE_EVERY_S ago."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.cadence_state')")
            if cur.fetchone()[0] is None:
                return True
            cur.execute("SELECT EXTRACT(EPOCH FROM now() - max(updated_at)) FROM cadence_state")
            row = cur.fetchone()
        return not row or row[0] is None or float(row[0]) >= CADENCE_EVERY_S
    except Exception:
        _rollback(conn)
        return True


def cadence_covered(source: str, threshold_s: float) -> Optional[str]:
    """Name of the freshness stream that already alerts this source's silence first (an
    unfiltered explicit stream on the same table whose SLA is no looser than the learned
    threshold), or a muted stream. None means the cadence alert is the only one."""
    if source in MUTED_STREAMS:
        return source
    for s in EXPLICIT_STREAMS:
        if f"{s.schema}.{s.table}" == source and not s.where and s.sla_s <= threshold_s:
            return s.name
    return None


def run_cadence(conn, dry_run: bool = False) -> dict:
    """One learned-cadence pass over nova_cadence_watch.STREAMS (its learn/classify/upsert/
    alert/memory functions, unchanged). Writes cadence_state; each stream's state is OK or
    SILENT with witness=None — never MISSING. Never raises."""
    from datetime import datetime, timezone
    import nova_cadence_watch as cw
    now = datetime.now(timezone.utc)
    summary = {"checked": 0, "silent": [], "ok": 0, "skipped": []}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.cadence_state')")
            if not dry_run or cur.fetchone()[0] is None:
                cw.ensure_schema(cur)
        conn.commit()
    except Exception as e:
        _rollback(conn)
        cw.log(f"cadence_state unavailable: {e}")
        return summary

    for table, ts_col in cw.STREAMS:
        try:
            with conn.cursor() as cur:
                learned = cw.learn(cur, table, ts_col)
        except Exception as e:
            _rollback(conn)
            cw.log(f"{table}: learn failed: {e}")
            summary["skipped"].append({"source": table, "why": str(e)[:120]})
            continue
        if not learned:
            summary["skipped"].append({"source": table, "why": "insufficient history"})
            continue
        summary["checked"] += 1
        state, age, threshold = cw.classify(learned, now)
        median = learned["median_gap_s"]
        entry = {"source": table, "state": state, "witness": None, "age_s": round(age, 1),
                 "median_gap_s": round(median, 2)}
        detail = {"median_gap_s": round(median, 2), "observations": learned["n"],
                  "age_s": round(age, 1), "silent_threshold_s": round(threshold, 1),
                  "silent_factor": cw.SILENT_FACTOR}
        if dry_run:
            cw.log(f"{table}: state={state} witness=none age={cw._human_age(age)} "
                   f"median={cw._human_age(median)} n={learned['n']}")
            if state == "SILENT":
                summary["silent"].append(entry)
            else:
                summary["ok"] += 1
            continue
        try:
            with conn.cursor() as cur:
                prev, transitioned = cw.upsert_cadence(cur, table, "stream", learned, state, detail)
            conn.commit()
        except Exception as e:
            _rollback(conn)
            cw.log(f"{table}: cadence_state write failed: {e}")
            continue
        if state == "SILENT":
            summary["silent"].append({**entry, "since_new": transitioned})
            if transitioned:
                cw.log(f"{table}: → SILENT (quiet {cw._human_age(age)}, usual ~{cw._human_age(median)})")
                covered = cadence_covered(table, threshold)
                if covered:
                    cw.log(f"  alert left to freshness stream {covered} (same fact, tighter SLA)")
                else:
                    cw._alert_silent(table, age, median, False)
                cw._write_silence_memory(table, age, median)
            else:
                cw.log(f"{table}: still SILENT ({cw._human_age(age)})")
        else:
            summary["ok"] += 1
            if prev == "SILENT":
                cw.log(f"{table}: recovered → OK (fresh within cadence)")
    cw.log(f"pass done: {summary['checked']} learned, {len(summary['silent'])} SILENT, "
           f"{summary['ok']} OK, {len(summary['skipped'])} skipped")
    return summary


def run_pass(conn, dry_run: bool = False, notify_fn: Optional[Callable] = None) -> dict:
    """The full silence-detector pass: streams + feeds (run_once), the feed time-series,
    presence-method silence, and the learned-cadence pass when due (always in dry-run)."""
    summary = run_once(conn, notify_fn=notify_fn, dry_run=dry_run)
    summary["feeds"] = record_feeds(conn, summary["results"], dry_run)
    summary["presence_quiet"] = [m for _, m in check_presence_silence(conn, dry_run, notify_fn)]
    summary["cadence"] = run_cadence(conn, dry_run) if (dry_run or cadence_due(conn)) else None
    return summary


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
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    dry_run = "--dry-run" in argv
    loop = "--loop" in argv

    if "--learn" in argv:                   # cadence pass only (what nova_cadence_watch ran)
        conn = _connect()
        try:
            run_cadence(conn, dry_run=dry_run)
            return 0
        finally:
            try:
                conn.close()
            except Exception:
                pass

    if loop:
        conn = None
        while True:
            try:
                if conn is None or conn.closed:
                    conn = _connect()
                run_pass(conn, dry_run=dry_run)
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
        summary = run_pass(conn, dry_run=dry_run)
        cad = summary.get("cadence") or {}
        log_action(
            conn,
            description=f"freshness pass: {summary['checked']} streams, "
                        f"breaches={summary['breaches']}, errors={summary['errors']}, "
                        f"presence_quiet={len(summary['presence_quiet'])}, "
                        f"cadence={'skipped' if not cad else str(len(cad['silent'])) + ' silent'}",
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
