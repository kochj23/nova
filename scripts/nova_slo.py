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

Usage: nova_slo.py [--dry-run]   (scheduler-core, every 5 min)
       nova_slo.py --report      (print the current state of every objective)
Written by Jordan Koch (via Claude).
"""
import argparse
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
    state            text,               -- ok | ticket | page | no_data
    PRIMARY KEY (name, at)
);
CREATE INDEX IF NOT EXISTS scheduler_runs_started ON scheduler_runs (started_at);
"""

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
     0.98, ["nova_scheduler"]),
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


def evaluate(burns):
    """-> 'page' | 'ticket' | 'ok' from {window: burn}, by RULES. Pure."""
    for sev in ("page", "ticket"):
        for s, long_w, short_w, thr in RULES:
            if s == sev and burns.get(long_w, 0) >= thr and burns.get(short_w, 0) >= thr:
                return sev
    return "ok"


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
    cur.execute(SCHEMA)
    for name, desc, sql, target, comps in SEED:   # seed once; later edits live in the table
        cur.execute("INSERT INTO slo_definitions (name, description, sli_sql, target, components) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (name) DO NOTHING", (name, desc, sql, target, comps))
    cur.execute("SELECT name, description, sli_sql, target, window_days FROM slo_definitions WHERE enabled ORDER BY name")
    out = []
    for name, desc, sql, target, wdays in cur.fetchall():
        try:
            good, total = _sli(cur, sql, now - timedelta(days=wdays), now)
            burns = {w: round(burn_rate(*_sli(cur, sql, now - d, now), target), 3) for w, d in WINDOWS.items()}
        except Exception as e:  # one bad SLI query must not stop the others
            conn.rollback() if not conn.autocommit else None
            print(f"[slo] {name}: SLI query failed: {e}", file=sys.stderr)
            continue
        state = evaluate(burns) if total else "no_data"
        row = dict(name=name, desc=desc, good=good, total=total, target=target,
                   sli=(good / total) if total else None,
                   budget=budget_remaining(good, total, target), burn=burns, state=state)
        out.append(row)
        if dry_run:
            continue
        import json
        cur.execute("INSERT INTO slo_status (name, at, good, total, sli, target, budget_remaining, burn, state) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (name, now, good, total, row["sli"], target, row["budget"], json.dumps(burns), state))
        if state in ("page", "ticket"):
            _alert(row)
    conn.close()
    return out


def _alert(row):
    from nova_notify import notify
    page = row["state"] == "page"
    b = row["burn"]
    notify(f"SLO {row['name']} is burning its error budget{' fast' if page else ''}",
           body=(f"{row['desc']}. SLI {row['sli']:.4%} vs target {row['target']:.2%} (28d); "
                 f"{row['budget']:.0%} of the budget left. Burn rate 1h {b['1h']}x, 6h {b['6h']}x, 3d {b['3d']}x."),
           level="critical" if page else "warning", category="slo",
           dedup_key=f"slo:{row['name']}:{row['state']}",
           meta={"slo": row["name"], "burn": b, "budget_remaining": row["budget"]})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args(argv)
    rows = run(dry_run=a.dry_run or a.report)
    for r in rows:
        sli = f"{r['sli']:.4%}" if r["sli"] is not None else "—"
        print(f"{r['name']:<17} {r['state']:<8} SLI {sli:>9} / {r['target']:.2%}  budget {r['budget']:>7.1%}  "
              f"burn 1h {r['burn']['1h']:>6} 6h {r['burn']['6h']:>6} 3d {r['burn']['3d']:>6}  ({r['total']} events)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
