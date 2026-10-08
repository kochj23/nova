#!/usr/bin/env python3
"""nova_butlerian_ledger.py — THE BUTLERIAN LEDGER (with Aub's Drill): can Little Mister still run
the house without Nova?

In Herbert's Dune books the Butlerian Jihad is remembered as a revolt against letting machines do
humanity's thinking and steering, not against machines as monsters; afterwards people trained
Mentats so that human minds could again do the work the computers had done. In Asimov's "The
Feeling of Power" a future people has handed all arithmetic to computers and forgotten it, until a
low-ranking technician named Aub works out from old machines how to multiply by hand, and the
generals at once see a use for him; the story is usually read as a warning about losing a skill by
never using it. Nova's version: a weekly ledger of how much of Little Mister's judgement has moved
over to her, a hand-kept list of house tasks he should be able to do while she is down (each with a
cold-readable runbook on the NAS, the "Lagash archive"), and Aub's Drill: at most once a quarter,
and only if he has opted in, one offered "Nova is down" exercise whose outcome he records himself.

Rising acceptance may simply mean Nova is right. The ledger only counts; it never diagnoses him,
never nags, never posts. Read the trend alongside her calibration, never alone.

Minimal first version:
  * weekly, from coagency_proposals: of the decisions on her proposals, how many he took unchanged
    (no qualifier in the note), how many were blanket ("approve all"), how many he edited or
    declined, how many were decided for him (claude-reviewer) or by her (earned autonomy), and the
    median hours to accept; plus autonomy_ledger actions and his own requests in gateway_traces
    (person 'jordan' only: nobody else in the house is counted). Every number cites proposal ids.
  * TASKS: eight hand-curated house tasks, each checked for a runbook in the runbook dir.
  * --offer-drill: one quarterly drill offer, filed as a reflection_question that Ask one posts.
    It does nothing unless service_config butlerian/aub_drill_consent is true; it never starts a
    drill, and it is never run by --run. --record-drill stores what he says happened.
The conversation classifier, the monthly note and explain-don't-decide wait for version two.

CLI:    --run [--dry-run] [--weeks N]   --offer-drill [--dry-run]
        --record-drill TASK --result could|partly|could_not [--note TEXT]   --selftest
Tables: butlerian_weekly (one row per ISO week), butlerian_drills (one offer per quarter)
Config: service_config butlerian/aub_drill_consent (absent = no), butlerian/runbook_dir
Schedule: weekly Monday 05:15 --run; quarterly --offer-drill (no-op without consent).
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

SERVICE = "butlerian"
DEFAULT_RUNBOOK_DIR = "/Volumes/nas/nova/runbooks/lagash"   # the mounted UNAS share ("Nightfall"'s Lagash)
# Hand-curated: the house tasks he should be able to do with Nova down. Runbook = <dir>/<key>.md.
TASKS = {
    "restart_fleet": "restart the Nova fleet (gateway, memory server, Ollama nodes) after a power cut",
    "dns_failover": "fail the house over to public DNS when the home resolver is down",
    "find_device": "find a device on the network: its address, what it is, which room",
    "read_cameras": "pull up and read the cameras without Nova",
    "recover_home_assistant": "bring Home Assistant and the Zigbee coordinator back",
    "pg_failover": "promote the PostgreSQL standby when the primary is down",
    "restore_backup": "restore a file or a database from the NAS backups",
    "internet_outage": "bring the internet back: modem, router, provider status",
}
RESULTS = ("could", "partly", "could_not")
ACCEPT = {"approved", "executed", "acknowledged"}
REJECT = {"rejected", "refused"}
# ponytail: a word list stands in for "edited"; the proposals table has no edit flag. Ceiling: a
# terse "approved" after a long Slack thread of changes still reads as unchanged.
QUALIFIER_RE = re.compile(r"\b(but|except|only|temporar\w*|instead|edit\w*|chang\w*|revert\w*|unless|if)\b", re.I)
BLANKET_RE = re.compile(r"approve all|yes on anything|batch|blanket|all of (them|it)", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS butlerian_weekly (
  week date PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
  decided int, him_accepted int, him_unchanged int, him_blanket int, him_rejected int,
  delegated int, nova_decided int, median_hours_to_accept real,
  autonomous_actions int, his_requests int, dependence real, cited jsonb);
CREATE TABLE IF NOT EXISTS butlerian_drills (
  id bigserial PRIMARY KEY, quarter text UNIQUE, task text NOT NULL, offered_at timestamptz,
  question_id int, ran_at timestamptz,
  result text CHECK (result IN ('could', 'partly', 'could_not')), note text);
"""


def log(m: str) -> None:
    print(f"[butlerian {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a failed read degrades to "nothing known"
        log(f"query failed: {e}")
        return []


def _table(cur, name: str) -> bool:
    r = _q(cur, "SELECT to_regclass(%s)", (name,))
    return bool(r and r[0][0])


# ── pure ────────────────────────────────────────────────────────────────────

def classify(status: str, by: str, note: str) -> dict | None:
    """One decided proposal -> {who, accepted, unchanged, blanket}, or None when it says nothing
    about whose judgement it was (superseded, blocked, no decider)."""
    by, note = by or "", note or ""
    if status not in ACCEPT | REJECT:
        return None
    if by.startswith("claude-reviewer"):
        who = "delegated"
    elif by.startswith("nova:"):
        who = "nova"
    elif "jordan" in by.lower():
        who = "him"
    else:
        return None
    acc = status in ACCEPT
    return {"who": who, "accepted": acc, "unchanged": acc and not QUALIFIER_RE.search(note),
            "blanket": acc and bool(BLANKET_RE.search(f"{by} {note}"))}


def week_of(ts: datetime) -> str:
    d = ts.astimezone(W.TZ).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def weekly(decisions, autonomy_ts=(), request_ts=()) -> dict:
    """decisions: (id, created_at, decided_at, status, decided_by, note). -> {week: metrics}."""
    out: dict = {}

    def wk(ts):
        return out.setdefault(week_of(ts), {
            "decided": 0, "him_accepted": 0, "him_unchanged": 0, "him_blanket": 0, "him_rejected": 0,
            "delegated": 0, "nova_decided": 0, "hours": [], "autonomous_actions": 0, "his_requests": 0,
            "cited": {k: [] for k in ("unchanged", "edited", "rejected", "blanket", "delegated", "nova")}})
    for pid, created, decided, status, by, note in decisions:
        c = classify(status, by, note)
        if c is None or decided is None:
            continue
        w = wk(decided)
        w["decided"] += 1
        if c["who"] != "him":
            w["delegated" if c["who"] == "delegated" else "nova_decided"] += 1
            w["cited"][c["who"]].append(pid)
            continue
        if not c["accepted"]:
            w["him_rejected"] += 1
            w["cited"]["rejected"].append(pid)
            continue
        w["him_accepted"] += 1
        w["hours"].append((decided - created).total_seconds() / 3600)
        w["him_unchanged"] += c["unchanged"]
        w["cited"]["unchanged" if c["unchanged"] else "edited"].append(pid)
        if c["blanket"]:
            w["him_blanket"] += 1
            w["cited"]["blanket"].append(pid)
    for ts in autonomy_ts:
        wk(ts)["autonomous_actions"] += 1
    for ts in request_ts:
        wk(ts)["his_requests"] += 1
    for w in out.values():
        h = w.pop("hours")
        w["median_hours_to_accept"] = round(statistics.median(h), 1) if h else None
        # ponytail: one blunt index (decisions his judgement did not shape / all decisions).
        w["dependence"] = (round((w["him_unchanged"] + w["delegated"] + w["nova_decided"]) / w["decided"], 3)
                           if w["decided"] else None)
    return dict(sorted(out.items()))


def trend(weeks: dict, n: int = 3, min_decided: int = 3, step: float = 0.1) -> str:
    """Last n weeks' mean dependence against the n before it. 'insufficient' if too thin."""
    vals = [w["dependence"] for w in weeks.values() if w["decided"] >= min_decided]
    if len(vals) < 2 * n:
        return "insufficient"
    d = statistics.mean(vals[-n:]) - statistics.mean(vals[-2 * n:-n])
    return "rising" if d > step else "falling" if d < -step else "steady"


def summary(weeks: dict, tr: str) -> str:
    if not weeks:
        return "No decided proposals in the window; nothing to say about the drift yet."
    tot = {k: sum(w[k] for w in weeks.values()) for k in ("decided", "him_unchanged", "delegated", "nova_decided")}
    line = (f"Over {len(weeks)} weeks, {tot['decided']} decisions on Nova's proposals: he took "
            f"{tot['him_unchanged']} unchanged, {tot['delegated']} were decided for him by the Claude "
            f"reviewer, {tot['nova_decided']} by Nova herself. Trend: {tr}.")
    if tr == "rising":
        line += (" This may only mean her proposals got better; worth reading next to her calibration "
                 "before reading anything into it.")
    return line


def runbooks(dir_: str, tasks=TASKS) -> dict:
    """{task: True/False} for a runbook at <dir>/<task>.md. A missing or unmounted dir reads False."""
    d = Path(dir_)
    try:
        return {t: (d / f"{t}.md").is_file() for t in tasks}
    except OSError:
        return dict.fromkeys(tasks, False)


def quarter(ts: datetime) -> str:
    return f"{ts.year}Q{(ts.month - 1) // 3 + 1}"


def pick_task(present: dict, drilled: dict) -> str | None:
    """A task with a runbook: never drilled first, then the longest since. drilled = {task: ts}."""
    have = [t for t, ok in present.items() if ok]
    if not have:
        return None
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return min(have, key=lambda t: (t in drilled, drilled.get(t) or floor))


def drill_question(task: str, dir_: str) -> str:
    return (f"Aub's Drill, the once-a-quarter one you opted into: if I were down this week, could you "
            f"{TASKS[task]} on your own, from the runbook at {dir_}/{task}.md? Reply yes to try it "
            f"when it suits you, or no to skip this quarter. Either answer is fine.")


# ── PG ──────────────────────────────────────────────────────────────────────

def this_monday(now: datetime | None = None) -> datetime:
    loc = (now or datetime.now(timezone.utc)).astimezone(W.TZ)
    return (loc - timedelta(days=loc.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def gather(cur, since, until) -> dict:
    dec = _q(cur, "SELECT id, created_at, decided_at, status, coalesce(decided_by, ''), coalesce(decision_note, '') "
                  "FROM coagency_proposals WHERE decided_at >= %s AND decided_at < %s", (since, until))
    aut = [r[0] for r in _q(cur, "SELECT ts FROM autonomy_ledger WHERE ts >= %s AND ts < %s", (since, until))]
    req = [r[0] for r in _q(cur, "SELECT created_at FROM gateway_traces WHERE person = 'jordan' "
                                 "AND created_at >= %s AND created_at < %s", (since, until))]
    return weekly(dec, aut, req)


WEEK_COLS = ("decided", "him_accepted", "him_unchanged", "him_blanket", "him_rejected", "delegated",
             "nova_decided", "median_hours_to_accept", "autonomous_actions", "his_requests", "dependence")
# Built once from the constant column list above; every value is a %s parameter.
UPSERT = ("INSERT INTO butlerian_weekly (week, " + ", ".join(WEEK_COLS) + ", cited) VALUES (%s, "
          + ", ".join(["%s"] * len(WEEK_COLS)) + ", %s::jsonb) ON CONFLICT (week) DO UPDATE SET ts = now(), "
          + ", ".join(c + " = EXCLUDED." + c for c in WEEK_COLS) + ", cited = EXCLUDED.cited")


def write_weeks(cur, weeks: dict) -> None:
    ensure_schema(cur)
    for wk, w in weeks.items():
        cur.execute(UPSERT, (wk, *(w[c] for c in WEEK_COLS), json.dumps(w["cited"])))


def runbook_dir(cur) -> str:
    # Not W.get_config: it json-decodes every str, and a jsonb string comes back as a bare path.
    r = _q(cur, "SELECT value #>> '{}' FROM service_config WHERE service = %s AND key = %s", (SERVICE, "runbook_dir"))
    return (r[0][0] if r else None) or DEFAULT_RUNBOOK_DIR


def consented(cur) -> bool:
    try:
        return W.get_config(cur, SERVICE, "aub_drill_consent", False) is True
    except Exception as e:  # noqa: BLE001 — unreadable consent is no consent
        log(f"consent unreadable: {e}")
        return False


def run(dry: bool = False, n_weeks: int = 8) -> dict:
    conn = W.connect()
    try:
        cur = conn.cursor()
        until = this_monday()
        weeks = gather(cur, until - timedelta(weeks=n_weeks), until)
        tr = trend(weeks)
        rb_dir = runbook_dir(cur)
        rb = runbooks(rb_dir)
        log(f"{'DRY RUN ' if dry else ''}{len(weeks)} weeks before {until.date()}")
        for wk, w in weeks.items():
            print(f"  {wk}  decided {w['decided']:>3}  unchanged {w['him_unchanged']:>3}  blanket {w['him_blanket']:>3}"
                  f"  edited {len(w['cited']['edited']):>2}  declined {w['him_rejected']:>2}  reviewer {w['delegated']:>2}"
                  f"  nova {w['nova_decided']:>2}  med_h {w['median_hours_to_accept']}  auto {w['autonomous_actions']:>2}"
                  f"  asks {w['his_requests']:>3}  dep {w['dependence']}")
        print(summary(weeks, tr))
        print(f"runbooks in {rb_dir}: {sum(rb.values())}/{len(rb)} present; missing: "
              f"{', '.join(t for t, ok in rb.items() if not ok) or 'none'}")
        print(f"Aub's Drill consent: {'yes' if consented(cur) else 'no (no drill will be offered)'}")
        if not dry and weeks:
            write_weeks(cur, weeks)
            log(f"wrote {len(weeks)} weeks to butlerian_weekly")
        return {"weeks": weeks, "trend": tr, "runbooks": rb}
    finally:
        conn.close()


def quiet_active(cur) -> bool:
    try:
        import nova_relationship
        return bool(nova_relationship.quiet_mode(cur).get("active"))
    except Exception:  # noqa: BLE001 — missing module reads "not quiet"; the consent gate still holds
        return False


def offer_drill(dry: bool = False, now: datetime | None = None) -> str | None:
    """File at most one drill offer per quarter, only with consent. Returns the question or None."""
    now = now or datetime.now(timezone.utc)
    conn = W.connect()
    try:
        cur = conn.cursor()
        if not consented(cur):
            log("no consent in service_config butlerian/aub_drill_consent; offering nothing")
            return None
        has = _table(cur, "butlerian_drills")
        q = quarter(now)
        if has and _q(cur, "SELECT 1 FROM butlerian_drills WHERE quarter = %s", (q,)):
            log(f"a drill was already offered in {q}; one a quarter")
            return None
        if quiet_active(cur):
            log("quiet mode (hard stretch); not offering a drill")
            return None
        rb_dir = runbook_dir(cur)
        drilled = dict(_q(cur, "SELECT task, max(coalesce(ran_at, offered_at)) FROM butlerian_drills "
                               "GROUP BY task")) if has else {}
        task = pick_task(runbooks(rb_dir), drilled)
        if not task:
            log(f"no runbook in {rb_dir} yet; a drill needs one to read from")
            return None
        text = drill_question(task, rb_dir)
        print(text)
        if dry:
            return text
        ensure_schema(cur)
        cur.execute("INSERT INTO butlerian_drills (quarter, task, offered_at) VALUES (%s, %s, now()) "
                    "ON CONFLICT (quarter) DO NOTHING RETURNING id", (q, task))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("INSERT INTO reflection_questions (memory_source, question) VALUES ('butlerian_drill', %s) "
                    "RETURNING id", (text,))
        cur.execute("UPDATE butlerian_drills SET question_id = %s WHERE id = %s", (cur.fetchone()[0], row[0]))
        log(f"offered {task} for {q}; Ask one will post it")
        return text
    finally:
        conn.close()


def record_drill(task: str, result: str, note: str = "", dry: bool = False) -> int:
    if task not in TASKS or result not in RESULTS:
        log(f"unknown task or result: {task!r} {result!r}")
        return 2
    if dry:
        print(f"would record {task}: {result} {note[:500]!r}")
        return 0
    conn = W.connect()
    try:
        cur = conn.cursor()
        ensure_schema(cur)
        cur.execute("UPDATE butlerian_drills SET ran_at = now(), result = %s, note = %s WHERE id = "
                    "(SELECT max(id) FROM butlerian_drills WHERE task = %s AND result IS NULL)",
                    (result, note[:500], task))
        if not cur.rowcount:
            cur.execute("INSERT INTO butlerian_drills (task, ran_at, result, note) VALUES (%s, now(), %s, %s)",
                        (task, result, note[:500]))
        log(f"recorded {task}: {result}")
        return 0
    finally:
        conn.close()


def selftest() -> int:
    t = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)   # a Tuesday
    assert classify("executed", "jordan", "Jordan via Claude: approved")["unchanged"]
    assert not classify("executed", "jordan", "approved TEMPORARILY, revert later")["unchanged"]
    assert classify("acknowledged", "jordan (via Claude, 2026-09-25: yes on anything)", "")["blanket"]
    assert classify("rejected", "claude-reviewer", "")["who"] == "delegated"
    assert classify("executed", "nova:earned-autonomy", "")["who"] == "nova"
    assert classify("superseded", "jordan", "") is None and classify("executed", "", "") is None
    dec = [(1, t - timedelta(hours=4), t, "executed", "jordan", "approved"),
           (2, t, t, "acknowledged", "jordan", "approve all"),
           (3, t, t, "executed", "jordan", "approved but only the first half"),
           (4, t, t, "rejected", "jordan", "declined"),
           (5, t, t, "executed", "claude-reviewer", "")]
    w = weekly(dec, [t], [t, t])["2026-10-05"]
    assert (w["decided"], w["him_unchanged"], w["him_blanket"], w["him_rejected"], w["delegated"]) == (5, 2, 1, 1, 1), w
    assert w["cited"]["edited"] == [3] and w["dependence"] == 0.6 and w["his_requests"] == 2, w
    assert w["median_hours_to_accept"] == 0.0
    mk = lambda d: {"decided": 5, "dependence": d}  # noqa: E731
    assert trend({str(i): mk(d) for i, d in enumerate([.2, .2, .2, .6, .6, .6])}) == "rising"
    assert trend({"a": mk(.5)}) == "insufficient"
    assert pick_task({"a": True, "b": True, "c": False}, {"a": t}) == "b"
    assert pick_task({"a": False}, {}) is None
    assert quarter(t) == "2026Q4" and "on your own" in drill_question("find_device", "/x")
    assert "calibration" in summary({"w": w}, "rising")
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="compute the weekly ledger and store it")
    ap.add_argument("--dry-run", action="store_true", help="print only; write nothing")
    ap.add_argument("--weeks", type=int, default=8, help="complete weeks to (re)compute (default 8)")
    ap.add_argument("--offer-drill", action="store_true", help="file this quarter's drill offer (needs consent)")
    ap.add_argument("--record-drill", choices=sorted(TASKS), help="record a drill he ran")
    ap.add_argument("--result", choices=RESULTS)
    ap.add_argument("--note", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.record_drill:
        if not a.result:
            ap.error("--record-drill needs --result")
        return record_drill(a.record_drill, a.result, a.note, dry=a.dry_run)
    if a.offer_drill:
        offer_drill(dry=a.dry_run)
        return 0
    if a.run:
        run(dry=a.dry_run, n_weeks=max(1, min(a.weeks, 104)))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
