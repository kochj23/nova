#!/usr/bin/env python3
"""nova_okr.py — quarterly objectives as goals rows + measurable key results (plan item #58, 2026-10-15).

The objective is an ordinary `goals` row (project='okr'). Each key result is a row in
goal_key_results with the SELECT that measures it, so any grader (continuity_review's monthly
self-review owns grading) can re-measure without knowing the metric:

  goal_key_results
    goal_id      -> goals.id (the objective)
    kr_key       stable key, unique per goal (pages_per_day, page_precision, top10_recurrence_closed)
    title        human sentence
    metric_sql   one SELECT returning a single numeric; read-only; may reference this row's
                 detail via (SELECT detail FROM goal_key_results WHERE kr_key=...)
    comparator   '<' | '<=' | '>' | '>=' — met when current <comparator> target
    target       numeric goal
    baseline     value measured when the KR was created
    current      last measured value (NULL until first measure)
    measured_at  when current was taken
    grade        0..1 (1 = met; linear progress baseline->target otherwise, clamped)
    detail       jsonb (top10_recurrence_closed: {"keys": [...], "since": ...} frozen at creation)

  nova_okr.py --seed       create schema + the Q4-2026 objective (idempotent)
  nova_okr.py --measure    re-measure every KR of active OKR goals, write current/grade, log to goal_log
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_dsn  # noqa: E402
import nova_pg_schema  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS goal_key_results (
  id bigserial PRIMARY KEY,
  goal_id text NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  kr_key text NOT NULL,
  title text NOT NULL,
  metric_sql text NOT NULL,
  comparator text NOT NULL CHECK (comparator IN ('<','<=','>','>=')),
  target numeric NOT NULL,
  baseline numeric,
  current numeric,
  measured_at timestamptz,
  grade numeric,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (goal_id, kr_key)
)
"""

OBJECTIVE = dict(
    id="okr-2026q4-interrupt",
    title="Jordan is interrupted only when it matters",
    description=("Q4 2026 objective (approved 2026-10-15). Nova owns it: fewer pages, and the ones "
                 "that go out are real. KRs in goal_key_results; graded monthly by continuity_review."),
    priority="high", project="okr", deadline="2026-12-31", check_in_days=7)

TOP10_SQL = ("SELECT recurrence_key FROM telemetry.incidents WHERE recurrence_key IS NOT NULL "
             "AND opened_at > now() - interval '30 days' GROUP BY 1 ORDER BY count(*) DESC LIMIT 10")

KRS = [
    dict(kr_key="pages_per_day", title="Pages per day < 5 (7-day average of alert_triage decision='page')",
         metric_sql=("SELECT count(*)/7.0 FROM alert_triage_log "
                     "WHERE decision='page' AND ts > now() - interval '7 days'"),
         comparator="<", target=5),
    dict(kr_key="page_precision", title="Page precision > 50% (1 - false_page_rate, latest alert_triage_precision)",
         metric_sql="SELECT 1 - false_page_rate FROM alert_triage_precision ORDER BY id DESC LIMIT 1",
         comparator=">", target=0.5),
    dict(kr_key="top10_recurrence_closed",
         title="Top-10 recurrence-key problems closed (no incident with that key in 14 days)",
         metric_sql=("SELECT count(*) FROM jsonb_array_elements_text((SELECT detail->'keys' FROM goal_key_results "
                     "WHERE goal_id='okr-2026q4-interrupt' AND kr_key='top10_recurrence_closed')) k "
                     "WHERE NOT EXISTS (SELECT 1 FROM telemetry.incidents i WHERE i.recurrence_key = k "
                     "AND i.opened_at > now() - interval '14 days')"),
         comparator=">=", target=10),
]


def grade(comparator, target, baseline, current):
    """1.0 when met; else linear progress from baseline toward target, clamped to [0, 1)."""
    if current is None:
        return None
    current, target = float(current), float(target)
    met = {"<": current < target, "<=": current <= target, ">": current > target, ">=": current >= target}[comparator]
    if met:
        return 1.0
    if baseline is None or float(baseline) == target:
        return 0.0
    g = (current - float(baseline)) / (target - float(baseline))
    return round(max(0.0, min(0.99, g)), 3)


def _scalar(cur, sql):
    cur.execute(sql)
    row = cur.fetchone()
    return None if not row or row[0] is None else float(row[0])


def seed(cur):
    nova_pg_schema.safe_ddl(cur, DDL)
    o = OBJECTIVE
    cur.execute("""INSERT INTO goals (id, title, description, status, priority, project, deadline, check_in_days)
                   VALUES (%s,%s,%s,'active',%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
                (o["id"], o["title"], o["description"], o["priority"], o["project"], o["deadline"], o["check_in_days"]))
    cur.execute(TOP10_SQL)
    keys = [r[0] for r in cur.fetchall()]
    for kr in KRS:
        detail = {"keys": keys, "frozen_at": "seed", "window": "30d"} if kr["kr_key"] == "top10_recurrence_closed" else {}
        cur.execute("""INSERT INTO goal_key_results (goal_id, kr_key, title, metric_sql, comparator, target, detail)
                       VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (goal_id, kr_key) DO NOTHING""",
                    (o["id"], kr["kr_key"], kr["title"], kr["metric_sql"], kr["comparator"], kr["target"], json.dumps(detail)))
    measure(cur, set_baseline=True)


def measure(cur, set_baseline=False):
    cur.execute("""SELECT k.id, k.goal_id, k.kr_key, k.metric_sql, k.comparator, k.target, k.baseline
                   FROM goal_key_results k JOIN goals g ON g.id = k.goal_id
                   WHERE g.project = 'okr' AND g.status = 'active' ORDER BY k.id""")
    out = []
    for kid, gid, key, sql, comp, target, baseline in cur.fetchall():
        if not sql.lstrip().upper().startswith("SELECT"):
            continue                                   # metric_sql is read-only by contract
        cur.execute("SAVEPOINT kr")
        try:
            val = _scalar(cur, sql)
            cur.execute("RELEASE SAVEPOINT kr")
        except Exception as e:                         # one broken metric never blocks the others
            cur.execute("ROLLBACK TO SAVEPOINT kr")
            out.append((key, None, None, str(e)[:120]))
            continue
        if set_baseline and baseline is None:
            baseline = val
        g = grade(comp, target, baseline, val)
        cur.execute("""UPDATE goal_key_results SET current=%s, measured_at=now(), grade=%s,
                       baseline=COALESCE(baseline, %s) WHERE id=%s""", (val, g, val if set_baseline else None, kid))
        cur.execute("INSERT INTO goal_log (id, goal_id, event_type, note) VALUES (left(md5(random()::text), 8),%s,'kr_measured',%s)",
                    (gid, f"{key}={val if val is None else round(val, 3)} target {comp}{target} grade={g}"))
        out.append((key, val, g, None))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--measure", action="store_true")
    a = ap.parse_args(argv)
    conn = nova_dsn.pg_connect()
    with conn, conn.cursor() as cur:
        if a.seed:
            seed(cur)
        res = measure(cur)
    for key, val, g, err in res:
        print(f"{key}: {err or f'{val} grade={g}'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
