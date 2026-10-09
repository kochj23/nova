#!/usr/bin/env python3
"""nova_busab.py — BuSab: slow Nova's own law-making before it outruns her reliability.

From Frank Herbert's ConSentiency stories (Whipping Star, The Dosadi Experiment): a pure
democracy once passed laws so fast that no one could weigh their effects, and the sentients
of the galaxy answered by founding a Bureau of Sabotage to slow the wheels of government.
Its saboteur extraordinary, Jorj X. McKie, distrusts power whoever holds it, his own included.
Beside it, Arthur C. Clarke's "Superiority" (1951): a defeated fleet commander explains from a
prisoner's cell how his side lost a war it should have won. Professor-General Norden, the new
Chief of the Research Staff, kept replacing working weapons with newer ones; the fleet paused
to refit, each new system brought new faults, and the enemy won with cruder weapons that
worked. (Which weapons Norden introduced is not checked here and not relied on.)
Nova's version: measure how fast she changes against how reliable she is, and when change
outpaces reliability, recommend a freeze in one line. Little Mister decides; BuSab never
blocks anything. BuSab is itself an organ, so its own birth counts toward the change it measures.

Minimal first version (weekly, Monday, for the last complete Mon-Sun week, America/Los_Angeles):
  * change   = new nova_*.py files + new scheduler entries that week (the same git-derived
               births nova_yellow_eye.py uses: script_births, scheduler_births)
  * reliability = chronic-failure tasks that week: tasks with more than FAIL_PER_DAY non-success
               runs on at least DAYS days in scheduler_runs (thresholds of nova_chronic_failures)
  * if BOTH exceed their trailing 8-week medians, or change alone is a burst (at least 10x its
    median and at least 25), one claude_queue note "change freeze
    recommended", naming that week's new organs and the half-deployed items scraped from
    agent_docs ("Open items" / "Adoption points" bullets, "BUILT DISABLED" / "not wired" lines).
Every run writes one busab_weekly row. Least-used-organ reporting waits for version two.

CLI:   --run [--dry-run] [--week YYYY-MM-DD]   --show   --selftest
Table: busab_weekly.
Schedule: Monday 09:30 (before the Commander's Intent review at 10:00).
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402
from nova_chronic_failures import DAYS, FAIL_PER_DAY  # noqa: E402
from nova_yellow_eye import OPEN, scheduler_births, script_births  # noqa: E402

TRAILING = 8
# A burst this far above the median recommends a freeze even with flat failures: chronic failures
# lag new organs (an untested organ cannot fail chronically in its first week).
# ponytail: fixed multiplier and floor; tune once a few months of busab_weekly exist.
BURST_X, BURST_MIN = 10, 25
QUEUE_SESSION = "nova-busab"
QUEUE_PREFIX = "BuSab: change freeze recommended"
MAX_ITEMS = 15
OPEN_HEAD = re.compile(r"(?i)^#+ .*(open items|adoption point)")
OPEN_LINE = re.compile(r"(?i)built disabled|not (yet )?wired|unwired")

SCHEMA = """
CREATE TABLE IF NOT EXISTS busab_weekly (
  week_start date PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
  new_scripts int, new_entries int, chronic int, med_change real, med_chronic real,
  recommend boolean, new_organs jsonb, half_deployed jsonb, queue_id int);
"""


def log(m: str) -> None:
    print(f"[busab {datetime.now():%H:%M:%S}] {m}", flush=True)


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

def week_of(ts) -> date:
    d = ts.astimezone(W.TZ).date() if isinstance(ts, datetime) else ts
    return d - timedelta(days=d.weekday())


def per_week(births: dict) -> dict:
    out = {}
    for ts in births.values():
        w = week_of(ts)
        out[w] = out.get(w, 0) + 1
    return out


def assess(scripts: dict, entries: dict, chronic: dict, w0: date, trailing: int = TRAILING) -> dict:
    """scripts/entries/chronic: {week_start: n}. Change and chronic for w0 against the median of
    the `trailing` weeks before it (missing weeks count 0)."""
    # ponytail: unweighted counts; a new organ and a one-line fix weigh the same (spec v2 weights them).
    change = lambda w: scripts.get(w, 0) + entries.get(w, 0)  # noqa: E731
    prior = [w0 - timedelta(weeks=i) for i in range(1, trailing + 1)]
    med_c = statistics.median(change(w) for w in prior)
    med_f = statistics.median(chronic.get(w, 0) for w in prior)
    c, f = change(w0), chronic.get(w0, 0)
    return {"week_start": w0, "new_scripts": scripts.get(w0, 0), "new_entries": entries.get(w0, 0),
            "change": c, "chronic": f, "med_change": med_c, "med_chronic": med_f,
            "burst": c >= max(BURST_X * max(med_c, 1), BURST_MIN),
            "recommend": (c > med_c and f > med_f) or c >= max(BURST_X * max(med_c, 1), BURST_MIN)}


def scrape_open(doc_type: str, content: str) -> list:
    """Half-deployed lines: top-level bullets under an Open items / Adoption points heading,
    plus any line saying BUILT DISABLED or not wired."""
    out, inside = [], False
    for line in (content or "").splitlines():
        if line.startswith("#"):
            inside = bool(OPEN_HEAD.match(line))
        hit = (inside and line.startswith("- ")) or OPEN_LINE.search(line)
        if hit:
            out.append(f"{doc_type}: {line.lstrip('#- ').strip()[:140]}")
    return out


def note(a: dict, organs: list, half: list) -> tuple:
    desc = (f"{QUEUE_PREFIX} (week of {a['week_start']}): {a['change']} changes vs median {a['med_change']:g}, "
            f"{a['chronic']} chronic failures vs median {a['med_chronic']:g}. Little Mister decides.")
    ctx = ("New this week: " + (", ".join(organs) or "none") + "\nHalf-deployed (finish before building more):\n"
           + "\n".join(f"- {h}" for h in half[:MAX_ITEMS])
           + (f"\n... and {len(half) - MAX_ITEMS} more" if len(half) > MAX_ITEMS else "")
           + "\nBuSab only recommends; it blocks nothing. It counts its own birth too.")
    return desc, ctx


# ── PG ──────────────────────────────────────────────────────────────────────

def chronic_per_week(cur, since: date) -> dict:
    rows = _q(cur, """
        SELECT wk, count(*) FROM (
          SELECT wk, task_id FROM (
            SELECT date_trunc('week', to_timestamp(ended_at/1000.0) AT TIME ZONE 'America/Los_Angeles')::date wk,
                   task_id, (to_timestamp(ended_at/1000.0) AT TIME ZONE 'America/Los_Angeles')::date d, count(*) n
            FROM scheduler_runs WHERE status <> 'success'
              AND ended_at >= extract(epoch from %s::date::timestamp AT TIME ZONE 'America/Los_Angeles') * 1000
            GROUP BY 1, 2, 3) x
          WHERE n > %s GROUP BY wk, task_id HAVING count(*) >= %s) y
        GROUP BY wk""", (since, FAIL_PER_DAY, DAYS))
    return {w: n for w, n in rows}


def half_deployed(cur) -> list:
    out = []
    for dt, content in _q(cur, "SELECT doc_type, content FROM agent_docs WHERE agent_id='all' ORDER BY doc_type"):
        out += scrape_open(dt, content)
    return out


def write(cur, a: dict, organs: list, half: list, qid) -> None:
    ensure_schema(cur)
    cur.execute(
        "INSERT INTO busab_weekly (week_start, new_scripts, new_entries, chronic, med_change, med_chronic, recommend, "
        "new_organs, half_deployed, queue_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s) "
        "ON CONFLICT (week_start) DO UPDATE SET ts=now(), new_scripts=EXCLUDED.new_scripts, "
        "new_entries=EXCLUDED.new_entries, chronic=EXCLUDED.chronic, med_change=EXCLUDED.med_change, "
        "med_chronic=EXCLUDED.med_chronic, recommend=EXCLUDED.recommend, new_organs=EXCLUDED.new_organs, "
        "half_deployed=EXCLUDED.half_deployed, queue_id=coalesce(EXCLUDED.queue_id, busab_weekly.queue_id)",
        (a["week_start"], a["new_scripts"], a["new_entries"], a["chronic"], a["med_change"], a["med_chronic"],
         a["recommend"], json.dumps(organs), json.dumps(half), qid))


def file_note(cur, desc: str, ctx: str):
    """At most one note per week: an existing note for that week (any status) is never repeated."""
    head = desc.split(":")[0] + ":%"
    if _q(cur, "SELECT id FROM claude_queue WHERE session_id=%s AND description LIKE %s LIMIT 1", (QUEUE_SESSION, head)):
        return None
    # claude_queue.session_id is a foreign key: register this organ's session first (2026-10-09 audit).
    cur.execute("INSERT INTO claude_sessions (session_id, status) VALUES (%s,'active') "
                "ON CONFLICT (session_id) DO NOTHING", (QUEUE_SESSION,))
    cur.execute("INSERT INTO claude_queue (session_id, created_at, updated_at, status, priority, description, context) "
                "VALUES (%s, now(), now(), 'queued', 3, %s, %s) RETURNING id", (QUEUE_SESSION, desc, ctx))
    return cur.fetchone()[0]


def run(dry: bool = False, week: date | None = None) -> dict:
    w0 = week_of(week) if week else week_of(datetime.now(timezone.utc)) - timedelta(weeks=1)
    scripts, entries = script_births(), scheduler_births()
    organs = sorted(s for s, ts in scripts.items() if week_of(ts) == w0)
    conn = W.connect()
    try:
        cur = conn.cursor()
        chronic = chronic_per_week(cur, w0 - timedelta(weeks=TRAILING))
        a = assess(per_week(scripts), per_week(entries), chronic, w0)
        half = half_deployed(cur) if a["recommend"] else []
        log(f"{'DRY RUN ' if dry else ''}week of {w0}: change {a['change']} ({a['new_scripts']} scripts + "
            f"{a['new_entries']} entries) vs median {a['med_change']:g}; chronic {a['chronic']} vs median "
            f"{a['med_chronic']:g} -> {'FREEZE RECOMMENDED' if a['recommend'] else 'within budget'}")
        qid = None
        if a["recommend"]:
            desc, ctx = note(a, organs, half)
            print(f"  {desc}\n" + "\n".join(f"  {line}" for line in ctx.splitlines()))
            if not dry:
                qid = file_note(cur, desc, ctx)
        if not dry:
            write(cur, a, organs, half, qid)
            log(f"wrote busab_weekly {w0}; queue note {qid or 'none'}")
        return a
    finally:
        conn.close()


def show() -> int:
    conn = W.connect()
    try:
        cur = conn.cursor()
        ok = _q(cur, "SELECT to_regclass('busab_weekly')")
        if not ok or ok[0][0] is None:
            print("no busab_weekly table yet")
            return 0
        for r in _q(cur, "SELECT week_start, new_scripts, new_entries, chronic, med_change, med_chronic, recommend, "
                         "queue_id FROM busab_weekly ORDER BY week_start DESC LIMIT 12"):
            print("{} scripts={} entries={} chronic={} med_change={:g} med_chronic={:g} recommend={} queue={}".format(*r))
        return 0
    finally:
        conn.close()


def selftest() -> int:
    w0 = date(2026, 10, 5)
    assert week_of(date(2026, 10, 11)) == w0 and week_of(w0) == w0
    assert week_of(datetime(2026, 10, 12, 3, tzinfo=timezone.utc)) == w0   # Sunday evening in LA
    prior = {w0 - timedelta(weeks=i): 2 for i in range(1, 9)}
    a = assess({**prior, w0: 20}, {w0: 10}, {**{w: 1 for w in prior}, w0: 3}, w0)
    assert a["recommend"] and a["change"] == 30 and a["med_change"] == 2, a
    assert not assess({w0: 20}, {}, {}, w0)["recommend"]          # change up, reliability fine
    assert not assess({}, {}, {w0: 5}, w0)["recommend"]           # failing, but not changing
    burst = assess({**prior, w0: 111}, {}, {w: 3 for w in [*prior, w0]}, w0)
    assert burst["recommend"] and burst["burst"], burst            # 2026-10-08: 22x median, failures flat
    doc = "# x\n## Open items\n- Spectroscope: decide\n  - sub\n## Other\n- not this\n## 5. The Shine — BUILT DISABLED\n"
    assert scrape_open("d", doc) == ["d: Spectroscope: decide", "d: 5. The Shine — BUILT DISABLED"], scrape_open("d", doc)
    desc, ctx = note(a, ["nova_x.py"], ["d: y"] * 20)
    assert desc.startswith(QUEUE_PREFIX) and "Little Mister decides" in desc and "and 5 more" in ctx
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="assess last week, write busab_weekly, note if over budget")
    ap.add_argument("--dry-run", action="store_true", help="with --run: print only, write nothing")
    ap.add_argument("--week", type=date.fromisoformat, help="assess the week containing this date instead")
    ap.add_argument("--show", action="store_true", help="recent busab_weekly rows")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.run:
        run(dry=a.dry_run, week=a.week)
        return 0
    if a.show:
        return show()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
