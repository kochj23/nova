#!/usr/bin/env python3
"""nova_slo.py — Nova's SLO organ: service level objectives, error budgets and burn-rate alerts.

Little Mister, 2026-10-13 (after the SRE books were ingested): "Do it all! Add those organs!"
Google SRE book ch.4 (Service Level Objectives) and Workbook ch.2 (Implementing SLOs) + ch.5 (Alerting on SLOs).

Each objective in `slo_definitions` names an SLI query that returns (good, total) events between
%(s)s and %(e)s, a target (e.g. 0.99) and a 28-day window. Every run computes the SLI over the
alerting windows below, writes one row per objective to `slo_status`, and alerts on the
Workbook's multiwindow, multi-burn-rate rules (table 5-8):

    page    burn >= 14.4 over 1h AND 5m    (2% of a 28-day budget gone in an hour)
    page    burn >=  6   over 6h AND 30m   (5% gone in six hours)
    ticket  burn >=  1   over 3d AND 6h    (on course to spend the whole budget)

burn = error rate / (1 - target). A window with no events has burn 0. Pages go out as
critical (category `slo`), tickets as warning; the notifier's 24h state-change dedup keeps
a persisting burn to one post a day. Cause-based alerts that an SLO now covers are routed
to #nova-digest as tickets by nova_notifier (TICKET_CATEGORIES).

Synthetic probes stand in for real traffic where traffic is thin (Workbook ch.2: chat volume
in gateway_traces is a few dozen a week, too few for a meaningful ratio).

Little Mister, 2026-10-15 — reliability core (#41, #42, #53, #37):
  * #42 state bug: `memory_embedding` read 'ok' with budget -0.91 because state came only from
    the burn-rate windows; a 28-day budget already spent while the last 3 days were calm never
    tripped a rule. state_for(): an exhausted budget is at least 'ticket'.
  * #41 error-budget policy: budget_remaining < 0 (with >= min_events in the window) sets
    service_config (service='slo_freeze', key=<slo>) — change paths call is_frozen() — and files
    ONE claude_queue reliability item per exhaustion episode (tag [slo-freeze:<slo>]). The flag
    lifts itself when the budget is back >= 0. Nova owns this; nothing waits on Jordan.
  * #53 real-traffic SLI `chat_jordan`: good = a gateway_traces row for person='jordan' with a
    non-empty, non-fallback response ("Something went wrong on my end", "error...", traceback)
    within CHAT_JORDAN_LATENCY_MS. 120 s, not 30 s: over the last 60 days Jordan's Slack replies
    ran p50 60 s / p90 100 s / max < 120 s (33% under 30 s) — Slack is asynchronous and a 30 s
    bar would score today's normal answers as failures and freeze changes forever. The SLO
    guards "Nova answered, usefully, before the moment passed"; speed gets its own objective
    once the backends are faster (tighten then — Workbook ch.2). Target 0.95.
    min_events (new column): a burn rule only fires when its long window holds >= min_events
    events, and a budget only freezes with >= min_events in the 28-day window — at a few dozen
    messages a week one failed message in an hour must not page.
  * #37 honest incident stats in --report: monthly p50/p90 time-to-resolve and time-to-ack, ack
    counts per severity and repeat share — never means (one 3-day outlier makes a mean lie).

Usage: nova_slo.py [--dry-run]   (scheduler-core, every 5 min)
       nova_slo.py --report [--months N]   (every objective + monthly incident stats)
       nova_slo.py --incidents [--months N] (incident stats only)
Written by Jordan Koch (via Claude).
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_dsn  # noqa: E402

DSN = nova_dsn.pg_dsn("nova_ops")
WINDOWS = {"5m": timedelta(minutes=5), "30m": timedelta(minutes=30), "1h": timedelta(hours=1),
           "6h": timedelta(hours=6), "3d": timedelta(days=3)}
RULES = [  # (severity, long window, short window, burn threshold)
    ("page", "1h", "5m", 14.4),
    ("page", "6h", "30m", 6.0),
    ("ticket", "3d", "6h", 1.0),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS slo_definitions (
    name        text PRIMARY KEY,
    description text NOT NULL,
    sli_sql     text NOT NULL,          -- returns (good, total) for %(s)s <= t < %(e)s
    target      double precision NOT NULL CHECK (target > 0 AND target < 1),
    window_days integer NOT NULL DEFAULT 28,
    components  text[] NOT NULL DEFAULT '{}',   -- scripts/services this objective covers (read by nova_prr)
    enabled     boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS slo_status (
    name             text NOT NULL,
    at               timestamptz NOT NULL DEFAULT now(),
    good             bigint, total bigint,
    sli              double precision,   -- over the full window
    target           double precision,
    budget_remaining double precision,   -- 1.0 = untouched, 0 = spent, negative = overspent
    burn             jsonb,              -- {"5m": x, "30m": x, "1h": x, "6h": x, "3d": x}
    state            text,               -- ok | ticket | page | stale | no_data
    PRIMARY KEY (name, at)
);
CREATE INDEX IF NOT EXISTS scheduler_runs_started ON scheduler_runs (started_at);
ALTER TABLE slo_definitions ADD COLUMN IF NOT EXISTS min_events integer NOT NULL DEFAULT 0;
"""

FREEZE_SERVICE = "slo_freeze"            # service_config (service, key=<slo>) — read by change paths
QUEUE_SESSION = "nova-slo"
OPEN_QUEUE = ("queued", "pending", "in_progress", "deferred")
CHAT_JORDAN_LATENCY_MS = 120_000
CHAT_JORDAN_MIN_EVENTS = 10
# The gateway's fallback replies are failures even though response is non-empty
# (same set nova_weight_of_presence counts as LOST, minus "timed out", which real answers mention).
_FALLBACK_RE = r"(something went wrong on my end|^\s*error\y|\ytraceback\y)"

_PROBE = ("SELECT count(*) FILTER (WHERE ok), count(*) FROM telemetry.probe_results "
          "WHERE probe = '{p}' AND ts >= %(s)s AND ts < %(e)s")
SEED = [  # (name, description, sli_sql, target, components)
    ("chat", "Nova answers: the inference probe gets a reply from a chat backend",
     _PROBE.format(p="inference_vantage"), 0.99, ["nova_gateway", "nova_inference_router", "ollama"]),
    ("memory_recall", "Nova remembers: a memory written can be recalled (round trip)",
     _PROBE.format(p="memory_roundtrip"), 0.995, ["nova_memory_server", "memory-server"]),
    ("memory_embedding", "New memories get embedded (embedding endpoint answers)",
     _PROBE.format(p="embedding"), 0.99, ["nova_memory_server", "ollama"]),
    ("web", "Nova's HTTP endpoints answer (journal, dashboards, APIs)",
     _PROBE.format(p="http_endpoints"), 0.995, ["nova_prober", "grafana", "cloudflared"]),
    ("database", "nova_ops Postgres answers queries",
     _PROBE.format(p="postgres"), 0.999, ["postgres", "pg-primary"]),
    ("morning_bluf", "The Morning BLUF is delivered by 07:10 Pacific every day",
     """SELECT count(*) FILTER (WHERE ok), count(*) FROM (
          SELECT EXISTS (SELECT 1 FROM scheduler_runs r WHERE r.task_id = 'morning_brief' AND r.status = 'success'
                   AND r.ended_at BETWEEN extract(epoch FROM (d + interval '7 hours') AT TIME ZONE 'America/Los_Angeles') * 1000
                                      AND extract(epoch FROM (d + interval '7 hours 10 minutes') AT TIME ZONE 'America/Los_Angeles') * 1000) AS ok
          FROM generate_series(date_trunc('day', %(s)s AT TIME ZONE 'America/Los_Angeles'),
                               %(e)s AT TIME ZONE 'America/Los_Angeles', interval '1 day') d
          WHERE (d + interval '7 hours 10 minutes') AT TIME ZONE 'America/Los_Angeles' >= %(s)s
            AND (d + interval '7 hours 10 minutes') AT TIME ZONE 'America/Los_Angeles' < %(e)s) x""",
     0.95, ["nova_morning_brief"]),
    ("scheduled_jobs", "Nova's scheduled jobs succeed (both schedulers)",
     """SELECT count(*) FILTER (WHERE status = 'success'), count(*) FROM scheduler_runs
        WHERE status IN ('success', 'failure', 'timeout', 'orphaned')
          AND started_at >= extract(epoch FROM %(s)s) * 1000 AND started_at < extract(epoch FROM %(e)s) * 1000""",
     0.98, ["nova_scheduler", "scheduler:*"]),   # scheduler:* = covers every scheduled task (nova_prr)
    ("chat_jordan", "Jordan's real messages get a real answer within 120 s (gateway_traces)",
     f"""SELECT count(*) FILTER (WHERE coalesce(btrim(response), '') <> ''
                                   AND response !~* '{_FALLBACK_RE}'
                                   AND total_ms IS NOT NULL AND total_ms <= {CHAT_JORDAN_LATENCY_MS}),
               count(*)
        FROM gateway_traces WHERE person = 'jordan' AND created_at >= %(s)s AND created_at < %(e)s""",
     0.95, ["nova_gateway", "gateway", "chat_jordan"]),
    ("alert_delivery", "Notifications reach Slack (no delivery errors)",
     """SELECT count(*) FILTER (WHERE status <> 'error'), count(*) FROM telemetry.events
        WHERE status IN ('sent', 'suppressed', 'rollup', 'error') AND ts >= %(s)s AND ts < %(e)s""",
     0.999, ["nova_notifier", "nova_notify"]),
]


def burn_rate(good, total, target):
    """Error rate divided by the error budget rate. No events -> 0. Pure."""
    if not total:
        return 0.0
    return (1 - good / total) / (1 - target)


def budget_remaining(good, total, target):
    """Share of the window's error budget left: 1 untouched, 0 spent, <0 overspent. Pure."""
    if not total:
        return 1.0
    return 1 - (total - good) / (total * (1 - target))


def is_stale(total_window, total_1h, window_days):
    """An SLI that averages more than one event an hour, with none in the last hour, has gone blind. Pure."""
    return total_1h == 0 and total_window > window_days * 24


MIN_EVENTS = {"chat_jordan": CHAT_JORDAN_MIN_EVENTS}   # seeded into slo_definitions.min_events


def evaluate(burns, totals=None, min_events=0):
    """-> 'page' | 'ticket' | 'ok' from {window: burn}, by RULES. A rule whose LONG window holds
    fewer than min_events events is skipped (low-traffic SLIs). totals: {window: event count}. Pure."""
    for sev in ("page", "ticket"):
        for s, long_w, short_w, thr in RULES:
            if s != sev:
                continue
            if min_events and (totals or {}).get(long_w, 0) < min_events:
                continue
            if burns.get(long_w, 0) >= thr and burns.get(short_w, 0) >= thr:
                return sev
    return "ok"


def budget_exhausted(budget, total, min_events=0):
    """The 28-day budget is overspent on enough events to mean it. Pure."""
    return budget is not None and budget < 0 and total >= max(1, min_events or 0)


def state_for(burns, budget, total, totals=None, min_events=0):
    """#42: burn rules first; an exhausted budget is at least a 'ticket' even when the recent
    windows are calm (memory_embedding read 'ok' at budget -0.91). Pure."""
    if not total:
        return "no_data"
    st = evaluate(burns, totals, min_events)
    if st == "ok" and budget_exhausted(budget, total, min_events):
        return "ticket"
    return st


def _sli(cur, sql, start, end):
    cur.execute(sql, {"s": start, "e": end})
    good, total = cur.fetchone()
    return int(good or 0), int(total or 0)


def run(dry_run=False):
    import psycopg2
    now = datetime.now(timezone.utc)
    conn = psycopg2.connect(DSN, connect_timeout=10)
    conn.autocommit = True
    cur = conn.cursor()
    from nova_pg_schema import safe_ddl
    safe_ddl(cur, SCHEMA)
    for name, desc, sql, target, comps in SEED:   # seed once; later edits live in the table
        cur.execute("INSERT INTO slo_definitions (name, description, sli_sql, target, components, min_events) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (name) DO NOTHING",
                    (name, desc, sql, target, comps, MIN_EVENTS.get(name, 0)))
    cur.execute("SELECT name, description, sli_sql, target, window_days, min_events FROM slo_definitions "
                "WHERE enabled ORDER BY name")
    out = []
    for name, desc, sql, target, wdays, min_ev in cur.fetchall():
        try:
            good, total = _sli(cur, sql, now - timedelta(days=wdays), now)
            counts = {w: _sli(cur, sql, now - d, now) for w, d in WINDOWS.items()}
            burns = {w: round(burn_rate(g, t, target), 3) for w, (g, t) in counts.items()}
        except Exception as e:  # one bad SLI query must not stop the others
            conn.rollback() if not conn.autocommit else None
            print(f"[slo] {name}: SLI query failed: {e}", file=sys.stderr)
            continue
        budget = budget_remaining(good, total, target)
        totals = {w: t for w, (_, t) in counts.items()}
        state = state_for(burns, budget, total, totals, min_ev or 0)
        if is_stale(total, counts["1h"][1], wdays):
            state = "stale"   # the SLI normally has hourly data and the last hour has none: monitoring is blind
        row = dict(name=name, desc=desc, good=good, total=total, target=target,
                   sli=(good / total) if total else None, min_events=min_ev or 0,
                   budget=budget, burn=burns, state=state,
                   burn_state=evaluate(burns, totals, min_ev or 0),
                   exhausted=budget_exhausted(budget, total, min_ev or 0))
        out.append(row)
        if dry_run:
            continue
        cur.execute("INSERT INTO slo_status (name, at, good, total, sli, target, budget_remaining, burn, state) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (name, now, good, total, row["sli"], target, row["budget"], json.dumps(burns), state))
        if state in ("page", "ticket", "stale"):
            _alert(row)
        try:
            apply_budget_policy(cur, row, now)
        except Exception as e:  # noqa: BLE001 - the policy must never stop the SLO loop
            print(f"[slo] {name}: budget policy failed: {e}", file=sys.stderr)
    conn.close()
    return out


def freeze_value(row, now, since=None):
    """service_config value for an active freeze. Pure."""
    return {"since": since or now.isoformat(), "budget_remaining": round(row["budget"], 4),
            "sli": row["sli"], "target": row["target"], "events": row["total"], "set_by": "nova_slo.py",
            "policy": "error budget exhausted: reliability work before risky changes (Workbook ch.2/ch.4)"}


def queue_description(row):
    return (f"[slo-freeze:{row['name']}] SLO {row['name']} has spent its error budget "
            f"({row['budget']:.0%} left) — reliability work before risky changes")


def apply_budget_policy(cur, row, now):
    """#41: set/lift slo_freeze/<slo> and file one claude_queue item per exhaustion episode.
    -> 'set' | 'held' | 'lifted' | None."""
    name = row["name"]
    cur.execute("SELECT value FROM service_config WHERE service = %s AND key = %s", (FREEZE_SERVICE, name))
    cur_row = cur.fetchone()
    if not row.get("exhausted"):
        if cur_row:
            cur.execute("DELETE FROM service_config WHERE service = %s AND key = %s", (FREEZE_SERVICE, name))
            print(f"[slo] {name}: budget back to {row['budget']:.0%} — freeze lifted")
            return "lifted"
        return None
    since = (cur_row[0] or {}).get("since") if cur_row else None
    val = freeze_value(row, now, since)
    cur.execute("INSERT INTO service_config (service, key, value, updated_by) VALUES (%s, %s, %s, 'nova_slo') "
                "ON CONFLICT (service, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), "
                "updated_by = EXCLUDED.updated_by", (FREEZE_SERVICE, name, json.dumps(val)))
    tag = f"[slo-freeze:{name}]%"
    cur.execute("INSERT INTO claude_sessions (session_id, status) VALUES (%s, 'active') "
                "ON CONFLICT (session_id) DO NOTHING", (QUEUE_SESSION,))
    cur.execute("INSERT INTO claude_queue (session_id, status, priority, description, context) "
                "SELECT %s, 'queued', 3, %s, %s WHERE NOT EXISTS (SELECT 1 FROM claude_queue "
                "WHERE description LIKE %s AND (status = ANY(%s) OR created_at >= %s::timestamptz))",
                (QUEUE_SESSION, queue_description(row),
                 json.dumps({"slo": name, "freeze": val, "owner": "nova",
                             "read_flag": f"service_config service='{FREEZE_SERVICE}' key='{name}'"}),
                 tag, list(OPEN_QUEUE), val["since"]))
    return "held" if cur_row else "set"


def is_frozen(cur, slo=None):
    """For change paths: names of SLOs under an error-budget freeze (or bool for one slo)."""
    if slo:
        cur.execute("SELECT 1 FROM service_config WHERE service = %s AND key = %s", (FREEZE_SERVICE, slo))
        return cur.fetchone() is not None
    cur.execute("SELECT key FROM service_config WHERE service = %s ORDER BY key", (FREEZE_SERVICE,))
    return [r[0] for r in cur.fetchall()]


def _alert(row):
    from nova_notify import notify
    if row["state"] == "stale":
        notify(f"SLO {row['name']} has no data for the last hour",
               body=f"{row['desc']}. Its SLI normally has data every hour; whatever records it has stopped, "
                    f"so this objective is unmonitored until it resumes.",
               level="warning", category="slo", dedup_key=f"slo:{row['name']}:stale",
               meta={"slo": row["name"]})
        return
    page = row["state"] == "page"
    b = row["burn"]
    if row["state"] == "ticket" and row.get("exhausted") and row.get("burn_state", "ok") == "ok":
        notify(f"SLO {row['name']} has spent its error budget",
               body=(f"{row['desc']}. SLI {row['sli']:.4%} vs target {row['target']:.2%} (28d); "
                     f"{row['budget']:.0%} of the budget left. Recent windows are calm (3d burn {b['3d']}x) "
                     f"but the window is overspent: change freeze slo_freeze/{row['name']} is on until it recovers."),
               level="warning", category="slo", dedup_key=f"slo:{row['name']}:exhausted",
               meta={"slo": row["name"], "burn": b, "budget_remaining": row["budget"]})
        return
    notify(f"SLO {row['name']} is burning its error budget{' fast' if page else ''}",
           body=(f"{row['desc']}. SLI {row['sli']:.4%} vs target {row['target']:.2%} (28d); "
                 f"{row['budget']:.0%} of the budget left. Burn rate 1h {b['1h']}x, 6h {b['6h']}x, 3d {b['3d']}x."),
           level="critical" if page else "warning", category="slo",
           dedup_key=f"slo:{row['name']}:{row['state']}",
           meta={"slo": row["name"], "burn": b, "budget_remaining": row["budget"]})


# ── #37 honest incident stats: percentiles and counts, never means ─────────────
INCIDENT_SQL = r"""
WITH t AS (
    SELECT 'telemetry' AS src, severity, opened_at AS opened, acked_at AS acked, resolved_at AS resolved,
           coalesce(recurrence_key, lower(regexp_replace(title, '\d+', '#', 'g'))) AS k
    FROM telemetry.incidents
  UNION ALL
    SELECT 'sre', severity, started_at, NULL, resolved_at,
           lower(regexp_replace(title, '\d+', '#', 'g'))
    FROM public.incidents
), w AS (
    SELECT *, lag(opened) OVER (PARTITION BY src, k ORDER BY opened) AS prev FROM t
)
SELECT src, to_char(date_trunc('month', opened AT TIME ZONE 'America/Los_Angeles'), 'YYYY-MM') AS month,
       coalesce(severity, 'unknown'),
       extract(epoch FROM resolved - opened) AS ttr_s,
       extract(epoch FROM acked - opened) AS tta_s,
       (prev IS NOT NULL AND opened - prev <= interval '30 days') AS repeat
FROM w WHERE opened >= %(since)s
"""


def percentile(values, q):
    """Linear-interpolated percentile (as percentile_cont) of the non-None values; None if empty. Pure."""
    v = sorted(float(x) for x in values if x is not None)
    if not v:
        return None
    pos = (len(v) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def incident_stats(rows):
    """rows: (src, month, severity, ttr_s, tta_s, repeat) -> sorted list of dicts per (src, month,
    severity) with n, resolved, ttr_p50/p90, acked, tta_p50/p90, repeat_share. No means. Pure."""
    groups = {}
    for src, month, sev, ttr, tta, rep_ in rows:
        g = groups.setdefault((src, month, sev), {"ttr": [], "tta": [], "n": 0, "rep": 0})
        g["n"] += 1
        g["rep"] += 1 if rep_ else 0
        if ttr is not None and ttr >= 0:
            g["ttr"].append(ttr)
        if tta is not None and tta >= 0:
            g["tta"].append(tta)
    out = []
    for (src, month, sev), g in sorted(groups.items()):
        out.append({"src": src, "month": month, "severity": sev, "n": g["n"],
                    "resolved": len(g["ttr"]), "ttr_p50": percentile(g["ttr"], 0.5),
                    "ttr_p90": percentile(g["ttr"], 0.9), "acked": len(g["tta"]),
                    "tta_p50": percentile(g["tta"], 0.5), "tta_p90": percentile(g["tta"], 0.9),
                    "repeat_share": g["rep"] / g["n"]})
    return out


def fmt_dur(s):
    """Seconds -> '42s' / '17m' / '3.2h' / '2.1d'; None -> '—'. Pure."""
    if s is None:
        return "—"
    s = float(s)
    if s < 120:
        return f"{s:.0f}s"
    if s < 7200:
        return f"{s / 60:.0f}m"
    if s < 2 * 86400:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"


def incident_report_lines(stats):
    lines = ["incidents (p50/p90 only — means hide the long tail; repeat = same key within 30d)",
             f"{'source':<9} {'month':<7} {'severity':<9} {'n':>4} {'resolved':>8} {'TTR p50':>8} {'TTR p90':>8} "
             f"{'acked':>5} {'TTA p50':>8} {'TTA p90':>8} {'repeat':>6}"]
    for r in stats:
        lines.append(f"{r['src']:<9} {r['month']:<7} {r['severity']:<9} {r['n']:>4} {r['resolved']:>8} "
                     f"{fmt_dur(r['ttr_p50']):>8} {fmt_dur(r['ttr_p90']):>8} {r['acked']:>5} "
                     f"{fmt_dur(r['tta_p50']):>8} {fmt_dur(r['tta_p90']):>8} {r['repeat_share']:>6.0%}")
    return lines


def incident_rows(months=6):
    import psycopg2
    since = (datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
             - timedelta(days=31 * (months - 1))).replace(day=1)
    with psycopg2.connect(DSN, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute("SET statement_timeout = '60s'")
        cur.execute(INCIDENT_SQL, {"since": since})
        return cur.fetchall()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--incidents", action="store_true", help="monthly incident stats only")
    ap.add_argument("--months", type=int, default=6)
    a = ap.parse_args(argv)
    if a.incidents:
        print("\n".join(incident_report_lines(incident_stats(incident_rows(a.months)))))
        return 0
    rows = run(dry_run=a.dry_run or a.report)
    for r in rows:
        sli = f"{r['sli']:.4%}" if r["sli"] is not None else "—"
        print(f"{r['name']:<17} {r['state']:<8} SLI {sli:>9} / {r['target']:.2%}  budget {r['budget']:>7.1%}  "
              f"burn 1h {r['burn']['1h']:>6} 6h {r['burn']['6h']:>6} 3d {r['burn']['3d']:>6}  ({r['total']} events)"
              + ("  FREEZE" if r.get("exhausted") else ""))
    if a.report:
        try:
            print()
            print("\n".join(incident_report_lines(incident_stats(incident_rows(a.months)))))
        except Exception as e:  # noqa: BLE001 - stats are a report extra, never fatal
            print(f"incident stats unavailable: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
