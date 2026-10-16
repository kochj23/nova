#!/usr/bin/env python3
"""nova_weekly_retro.py — Nova's weekly retro, drafted from data and acted on by Nova (plan #57, 2026-10-15).

Run through `nova_hotwash.py --weekly-retro [--dry-run]` (Sun, before the 18:00 digest). Deterministic, no LLM.

Measures the last 7 days against the 7 before:
  flow            claude_queue: done vs new (throughput/inflow), WIP, p85 lead time (claude_queue_flow)
  top problems    telemetry.incidents by recurrence_key
  change failures deployment_runs failure / all
  toil            toil_events human minutes, top cause
  paging          alert_triage_log decision='page' per day, top paging source; precision = 1 - false_page_rate

Then keep / stop / try, and Nova acts on it herself: at most 3 claude_queue items per week —
  item 1 is ALWAYS the status of last week's retro actions (filed already 'done' when they all are),
  items 2-3 are the two highest-scoring "try" actions. Each item is deduped on its description (week key in it).

Outputs:
  weekly_retro (week PK, metrics, keep/stop/try, queue_ids, summary)    — history
  service_config ('weekly_retro','latest') = {week, lines[3], queue_ids, generated_at}
      three lines for Jordan's 18:00 digest (he is informed, never asked).
"""
from __future__ import annotations

import json
from datetime import date

QUEUE_SESSION = "nova-weekly-retro"
MAX_ITEMS = 3
PAGES_PER_DAY_TARGET = 5
PRECISION_TARGET = 0.5

SCHEMA = """
CREATE TABLE IF NOT EXISTS weekly_retro (
  week text PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  metrics jsonb NOT NULL,
  keep jsonb NOT NULL DEFAULT '[]',
  stop jsonb NOT NULL DEFAULT '[]',
  try jsonb NOT NULL DEFAULT '[]',
  queue_ids int[] NOT NULL DEFAULT '{}',
  summary text NOT NULL)
"""


def week_key(d: date | None = None) -> str:
    y, w, _ = (d or date.today()).isocalendar()
    return f"{y}-W{w:02d}"


def _one(cur, sql, params=None):
    try:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row if row else None
    except Exception:  # noqa: BLE001 — a missing table/view must not sink the whole retro
        return None


def _all(cur, sql, params=None):
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    except Exception:  # noqa: BLE001
        return []


def _num(v):
    return None if v is None else float(v)


def window(cur, start_days: int, end_days: int) -> dict:
    """Metrics for (now - start_days, now - end_days]. start_days=7,end_days=0 is this week."""
    a, b = f"{start_days} days", f"{end_days} days"
    m = {}
    r = _one(cur, "SELECT count(*) FILTER (WHERE status='done' AND completed_at > now()-%s::interval AND completed_at <= now()-%s::interval),"
                  " count(*) FILTER (WHERE created_at > now()-%s::interval AND created_at <= now()-%s::interval) FROM claude_queue",
             (a, b, a, b))
    m["throughput"], m["inflow"] = (int(r[0]), int(r[1])) if r else (None, None)
    r = _one(cur, "SELECT count(*) FROM alert_triage_log WHERE decision='page' AND ts > now()-%s::interval AND ts <= now()-%s::interval",
             (a, b))
    m["pages_per_day"] = round(r[0] / 7.0, 1) if r else None
    r = _one(cur, "SELECT count(*) FILTER (WHERE status='failure'), count(*) FROM deployment_runs "
                  "WHERE status <> 'unreachable' AND started_at > now()-%s::interval AND started_at <= now()-%s::interval", (a, b))
    m["change_failures"], m["changes"] = (int(r[0]), int(r[1])) if r else (None, None)
    m["change_failure_rate"] = round(m["change_failures"] / m["changes"], 3) if m.get("changes") else None
    r = _one(cur, "SELECT coalesce(sum(minutes_estimate),0) FROM toil_events WHERE human AND at > now()-%s::interval AND at <= now()-%s::interval",
             (a, b))
    m["toil_minutes"] = _num(r[0]) if r else None
    return m


def gather(cur) -> dict:
    now, prev = window(cur, 7, 0), window(cur, 14, 7)
    flow = _one(cur, "SELECT wip, queued, oldest_open_days, lead_p85_days FROM claude_queue_flow")
    now.update(dict(zip(("wip", "queued", "oldest_open_days", "lead_p85_days"), [_num(x) for x in flow])) if flow else {})
    r = _one(cur, "SELECT 1 - false_page_rate FROM alert_triage_precision ORDER BY id DESC LIMIT 1")
    now["page_precision"] = round(float(r[0]), 3) if r and r[0] is not None else None
    r = _one(cur, "SELECT 1 - false_page_rate FROM alert_triage_precision WHERE computed_at < now() - interval '7 days' "
                  "ORDER BY id DESC LIMIT 1")
    prev["page_precision"] = round(float(r[0]), 3) if r and r[0] is not None else None
    now["top_problems"] = [(k, int(n)) for k, n in _all(cur,
        "SELECT recurrence_key, count(*) FROM telemetry.incidents WHERE recurrence_key IS NOT NULL "
        "AND opened_at > now() - interval '7 days' GROUP BY 1 ORDER BY 2 DESC LIMIT 5")]
    now["top_pager"] = next(iter([(s, int(n)) for s, n in _all(cur,
        "SELECT source, count(*) FROM alert_triage_log WHERE decision='page' AND ts > now() - interval '7 days' "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 1")]), None)
    now["top_toil"] = next(iter([(k, _num(mins)) for k, mins in _all(cur,
        "SELECT cause_key, sum(minutes_estimate) FROM toil_events WHERE human AND at > now() - interval '7 days' "
        "GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 1")]), None)
    now["top_failing_change"] = next(iter([(k, int(n)) for k, n in _all(cur,
        "SELECT node_name, count(*) FROM deployment_runs WHERE status='failure' AND started_at > now() - interval '7 days' "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 1")]), None)
    return {"now": now, "prev": prev}


def _better(now, prev, key, lower_is_better=True):
    a, b = now.get(key), prev.get(key)
    if a is None or b is None or a == b:
        return None
    return (a < b) if lower_is_better else (a > b)


def decide(m: dict) -> dict:
    """Pure: metrics -> keep / stop / try. Each try = {score, title, why}."""
    now, prev = m["now"], m["prev"]
    keep, stop, tries = [], [], []
    for key, label, lower in (("pages_per_day", "pages/day", True), ("page_precision", "page precision", False),
                              ("change_failure_rate", "change failure rate", True), ("toil_minutes", "human toil", True)):
        b = _better(now, prev, key, lower)
        if b is True:
            keep.append(f"{label} improved {prev[key]} -> {now[key]}")
        elif b is False:
            stop.append(f"{label} worsened {prev[key]} -> {now[key]}")
    if now.get("throughput") is not None and now.get("inflow") is not None:
        (keep if now["throughput"] >= now["inflow"] else stop).append(
            f"queue flow {now['throughput']} done vs {now['inflow']} new")
    if now.get("top_problems"):
        key, n = now["top_problems"][0]
        stop.append(f"re-opening {key} ({n} incidents)")
        if n >= 5:
            tries.append(dict(score=n, title=f"close recurring problem {key}",
                              why=f"{n} incidents in 7 days; find the cause, fix it, and confirm 14 quiet days"))
    ppd = now.get("pages_per_day")
    if ppd is not None and ppd > PAGES_PER_DAY_TARGET and now.get("top_pager"):
        src, n = now["top_pager"]
        tries.append(dict(score=n / 7.0, title=f"cut pages from {src}",
                          why=f"{ppd}/day vs target <{PAGES_PER_DAY_TARGET}; {src} paged {n} times in 7 days — "
                              f"downgrade or dedupe what is not actionable"))
    prec = now.get("page_precision")
    if prec is not None and prec < PRECISION_TARGET:
        tries.append(dict(score=(PRECISION_TARGET - prec) * 100, title="raise page precision",
                          why=f"precision {prec:.0%} vs target >{PRECISION_TARGET:.0%}; review the false pages hotwash filed"))
    cfr = now.get("change_failure_rate")
    if cfr is not None and cfr > 0.2 and now.get("top_failing_change"):
        node, n = now["top_failing_change"]
        tries.append(dict(score=n, title=f"fix failing converges on {node}",
                          why=f"change failure rate {cfr:.0%}; {node} failed {n} times"))
    if now.get("top_toil") and (now["top_toil"][1] or 0) >= 60:
        cause, mins = now["top_toil"]
        subject = cause.split(":", 1)[-1].strip()
        same = [t for t in tries if t["title"].startswith("close recurring problem") and subject and subject in t["title"]]
        if same:                                  # the toil IS the top problem: one action, not two
            same[0]["why"] += f"; it also cost {mins:.0f} human minutes"
            same[0]["score"] += mins / 30.0
        else:
            tries.append(dict(score=mins / 30.0, title=f"automate toil: {cause}",
                              why=f"{mins:.0f} human minutes in 7 days"))
    tries.sort(key=lambda t: -t["score"])
    return {"keep": keep, "stop": stop, "try": tries}


def last_week_status(cur, week: str) -> tuple[list, int, int]:
    """[(id, status, description)] for the previous retro's queue items, done count, total."""
    r = _one(cur, "SELECT queue_ids FROM weekly_retro WHERE week < %s ORDER BY week DESC LIMIT 1", (week,))
    ids = list(r[0]) if r and r[0] else []
    if not ids:
        return [], 0, 0
    rows = _all(cur, "SELECT id, status, left(description, 90) FROM claude_queue WHERE id = ANY(%s) ORDER BY id", (ids,))
    return rows, sum(1 for _, s, _ in rows if s == "done"), len(rows)


def _file(cur, desc: str, context: str, status: str = "queued", priority: int = 3) -> int:
    r = _one(cur, "SELECT id FROM claude_queue WHERE description = %s LIMIT 1", (desc,))
    if r:
        return r[0]
    cur.execute("INSERT INTO claude_sessions (session_id, status) VALUES (%s,'active') ON CONFLICT (session_id) DO NOTHING",
                (QUEUE_SESSION,))
    cur.execute("INSERT INTO claude_queue (session_id, status, priority, description, context, completed_at) "
                "VALUES (%s,%s,%s,%s,%s, CASE WHEN %s='done' THEN now() END) RETURNING id",
                (QUEUE_SESSION, status, priority, desc, context, status))
    return cur.fetchone()[0]


def summary_lines(m: dict, d: dict, items: list, done_prev: int, total_prev: int) -> list:
    now = m["now"]
    p85 = now.get("lead_p85_days")
    prec = now.get("page_precision")
    l1 = (f"Flow: {now.get('throughput')} done / {now.get('inflow')} new, WIP {int(now.get('wip') or 0)}"
          + (f", p85 lead {p85:g}d" if p85 is not None else "")
          + f"; pages {now.get('pages_per_day')}/day" + (f" at {prec:.0%} precision" if prec is not None else "") + ".")
    tp = now.get("top_problems") or []
    cfr = now.get("change_failure_rate")
    l2 = ("Top problem: " + (f"{tp[0][0]} ({tp[0][1]} incidents)" if tp else "none recurring")
          + (f"; change failures {now['change_failures']}/{now['changes']} ({cfr:.0%})" if cfr is not None else "")
          + (f"; human toil {now['toil_minutes'] / 60:.0f}h" if now.get("toil_minutes") is not None else "") + ".")
    acts = [f"{t['title']} (#{qid})" if qid else t["title"] for t, qid in items]
    l3 = ("Nova will: " + ("; ".join(acts) if acts else "hold course")
          + (f". Last week's actions: {done_prev}/{total_prev} done." if total_prev else "."))
    return [l1, l2, l3]


def run(cur, dry: bool = False, today: date | None = None) -> dict:
    import nova_pg_schema
    week = week_key(today)
    if not dry:
        nova_pg_schema.safe_ddl(cur, SCHEMA)
    m = gather(cur)
    d = decide(m)
    prev_rows, done_prev, total_prev = last_week_status(cur, week)
    items, queue_ids = [], []
    if prev_rows:                                     # item 1: always last week's actions
        all_done = done_prev == total_prev
        ctx = (f"{done_prev}/{total_prev} of last week's retro actions done. "
               + ("Nothing open." if all_done else "Finish or re-plan the open ones:") + "\n"
               + "\n".join(f"#{i} {s}: {desc}" for i, s, desc in prev_rows))
        status_item = dict(title=f"Weekly retro {week}: status of last week's actions", why=ctx)
        qid = None if dry else _file(cur, status_item["title"], ctx, status="done" if all_done else "queued", priority=2)
        items.append((status_item, qid)); queue_ids.append(qid)
    for t in d["try"][:MAX_ITEMS - len(items)]:
        desc = f"Weekly retro {week}: {t['title']}"
        qid = None if dry else _file(cur, desc, f"{t['why']}\n\nFrom Nova's weekly retro (nova_weekly_retro.py). "
                                               f"Metrics: {json.dumps(m['now'], default=str)[:1500]}")
        items.append((t, qid)); queue_ids.append(qid)
    lines = summary_lines(m, d, [(t, q) for t, q in items if not t["title"].startswith("Weekly retro ")],
                          done_prev, total_prev)
    out = {"week": week, "lines": lines, "queue_ids": [q for q in queue_ids if q], "keep": d["keep"],
           "stop": d["stop"], "try": [t["title"] for t in d["try"]]}
    if not dry:
        cur.execute("""INSERT INTO weekly_retro (week, metrics, keep, stop, try, queue_ids, summary)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (week) DO UPDATE SET metrics=excluded.metrics, keep=excluded.keep, stop=excluded.stop,
                         try=excluded.try, queue_ids=(SELECT array_agg(DISTINCT x) FROM unnest(weekly_retro.queue_ids || excluded.queue_ids) x),
                         summary=excluded.summary, created_at=now()""",
                    (week, json.dumps(m, default=str), json.dumps(d["keep"]), json.dumps(d["stop"]),
                     json.dumps(d["try"], default=str), out["queue_ids"], "\n".join(lines)))
        cur.execute("""INSERT INTO service_config (service, key, value, updated_by) VALUES ('weekly_retro','latest',%s,'nova_weekly_retro')
                       ON CONFLICT (service, key) DO UPDATE SET value=excluded.value, updated_at=now(), updated_by=excluded.updated_by""",
                    (json.dumps({"week": week, "lines": lines, "queue_ids": out["queue_ids"],
                                 "generated_at": date.today().isoformat()}),))
    return out
