#!/usr/bin/env python3
"""nova_hotwash.py — HOTWASH: blameless after-action reviews within 24 h, and a weekly roll-up.

The Army runs an After Action Review the same day, while memory is fresh, and it is blameless:
the point is the next mission, not the last one's scapegoat. Nova's version, for four kinds of
event:

  false_alarm       she paged and alert_learn later graded it noise  (alert_triage_log page/was_noise)
  missed_event      she suppressed/downgraded and it was real        (suppress|downgrade / was_real)
  wrong_prediction  a forecast resolved incorrect                    (predictions; called from the
                    "I was wrong" loop in nova_predictions.do_resolve right after own_mistake, and
                    backstopped by the sweep)
  overreach         she tried something a guard refused (restraint_ledger channel='guard'), an
                    autonomous action was vetoed or reverted (autonomy_ledger), or an escalation
                    Jordan marked unneeded (escalation_log.feedback, read only if that exists)

Each row answers the four AAR questions from the data — deterministic, no LLM, no flattery and
no self-blame theatre:
  q1 What was supposed to happen?   q2 What actually happened?   q3 Why was there a difference?
  q4 What will we sustain / fix?    (+ fix_kind threshold|rule|data|none and a proposed_change)
The sweep is idempotent: one row per ref ('pred:<id>', 'triage:<fa|me>:<source>:<day>',
'ledger:<id>', 'guard:<restraint id>', 'escalation:<id>'); an open source-day row is refreshed
as alert_learn grades more of that day.

WEEKLY ROLL-UP (--rollup, Sun): open fix items grouped by (kind, subject). A group with >= 2
items, or a false-alarm group with >= 10 pages, becomes ONE proposed threshold/rule change,
filed through nova_coagency.file_proposal(origin='hotwash') — redline + value_check +
pending_human, so Jordan approves it in the existing slack_answers flow. Nothing is applied here.
At most 5 proposals a week. The reviewer is pluggable (service_config hotwash/reviewer, default
'jordan'); there is no Claude auto-reviewer in the repo today, so any other value falls back to
Jordan and says so in the log.

CARDINAL: q3 cites the source's Admiralty grade from nova_cardinal.load_ledger(). Outcomes are
NOT re-fed to record_outcome — alert_triage_log and predictions are already harvested there, and
counting them twice would inflate the record.

CLI: --sweep [--dry-run] [--hours 72] | --rollup [--dry-run] | --list [--days N] | --selftest
Table: nova_ops.hotwash.  Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

KINDS = ("false_alarm", "missed_event", "wrong_prediction", "overreach")
MAX_PROPOSALS = 5
BIG_FALSE_ALARM = 10
SCHEMA = """
CREATE TABLE IF NOT EXISTS hotwash (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  kind text NOT NULL CHECK (kind IN ('false_alarm','missed_event','wrong_prediction','overreach')),
  ref text NOT NULL UNIQUE,
  subject text,
  occurred_at timestamptz,
  q1_expected text NOT NULL,
  q2_actual text NOT NULL,
  q3_why text NOT NULL,
  q4_sustain text NOT NULL,
  q4_fix text NOT NULL,
  fix_kind text NOT NULL DEFAULT 'none' CHECK (fix_kind IN ('threshold','rule','data','none')),
  weight int NOT NULL DEFAULT 1,
  proposed_change jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','rolled_up','proposed','declined')),
  proposal_id bigint,
  rollup_week text);
CREATE INDEX IF NOT EXISTS hotwash_status ON hotwash (status, kind);
"""
COLS = ("kind", "ref", "subject", "occurred_at", "q1_expected", "q2_actual", "q3_why", "q4_sustain",
        "q4_fix", "fix_kind", "weight", "proposed_change")


def log(m: str) -> None:
    print(f"[hotwash {datetime.now():%H:%M:%S}] {m}", flush=True)


def _retry(fn, what: str, attempts: int = 3, base: float = 1.0, _sleep=time.sleep):
    """Bounded retry with linear backoff; re-raises the last error (callers decide fail-open)."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"{what} failed ({e}) — attempt {i + 1}/{attempts}")
            if i < attempts - 1:
                _sleep(base * (i + 1))
    raise last


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def week_key(d) -> str:
    y, w, _ = (d.date() if isinstance(d, datetime) else d).isocalendar()
    return f"{y}-W{w:02d}"


def _clip(s, n=160) -> str:
    return " ".join(str(s or "").split())[:n]


def grade_note(ledger: dict, sid: str) -> str:
    r = (ledger or {}).get(sid) or (ledger or {}).get(sid.removesuffix(".py")) or {}
    if not r:
        return f"CARDINAL has no track record for {sid} (grade F)."
    hr = f", {r['hit_rate']:.0%} real" if r.get("hit_rate") is not None else ""
    s = f"CARDINAL grades {sid} {r.get('reliability', 'F')} (n={r.get('n', 0)}{hr})."
    if r.get("compromise_suspect"):
        s += f" It is flagged as behaving unlike itself: {r.get('compromise_reason')}."
    return s


# ── pure AAR builders ───────────────────────────────────────────────────────

def aar_prediction(pred_id, statement, domain, conf, reasoning, db=None, occurred_at=None) -> dict:
    conf = float(conf or 0)
    why = f"The resolver found: {_clip(reasoning, 220).rstrip('.')}." if reasoning else "The resolver gave no reason."
    if db:
        why += (f" In {domain}, {db['n']} resolved forecasts have a base rate of {db['base']:.0%} and Brier skill "
                f"{db['skill']:+.2f} against simply saying the base rate.")
        no_skill = db["skill"] <= 0
    else:
        why += f" There are too few resolved {domain} forecasts to measure skill."
        no_skill = False
    change = {"type": "confidence_ceiling", "domain": domain}
    if db:
        change["base_rate"] = db["base"]
        change["skill"] = db["skill"]
    return {
        "kind": "wrong_prediction", "ref": f"pred:{pred_id}", "subject": f"domain:{domain}",
        "occurred_at": occurred_at,
        "q1_expected": f"Forecast #{pred_id} ({domain}) to come true, stated at {conf:.0%}: \"{_clip(statement, 200)}\".",
        "q2_actual": "It resolved incorrect.",
        "q3_why": why,
        "q4_sustain": "Writing the forecast with resolution criteria made the miss measurable; the self_correction "
                      "note and the soft-certainty calibration already fold it into the next number.",
        "q4_fix": (f"State {domain} forecasts no higher than the base rate until skill is positive." if no_skill
                   else "None beyond calibration unless the domain keeps missing."),
        "fix_kind": "threshold" if no_skill else "none", "weight": 1, "proposed_change": change}


def aar_false_alarm(source, day, n, titles, dedup_keys, ledger=None, first_ts=None) -> dict:
    keys = [k for k in (dedup_keys or []) if k][:5]
    rep = f" The same dedup_key repeated across {n} pages." if len(set(keys)) == 1 and n > 1 else ""
    return {
        "kind": "false_alarm", "ref": f"triage:fa:{source}:{day}", "subject": f"source:{source}",
        "occurred_at": first_ts,
        "q1_expected": f"Pages from {source} on {day} would be things Little Mister needed to act on.",
        "q2_actual": f"{n} page(s) went out and alert_learn graded all of them noise. Examples: "
                     + "; ".join(_clip(t, 80) for t in (titles or [])[:3]) + ".",
        "q3_why": f"AI triage chose 'page' for alerts that turned out not to need anyone.{rep} "
                  + grade_note(ledger, f"detector:{source}"),
        "q4_sustain": "alert_learn grading every page is what made this visible; keep it.",
        "q4_fix": f"Raise the paging bar for {source}: digest-level unless a real one has been graded recently.",
        "fix_kind": "threshold", "weight": int(n),
        "proposed_change": {"type": "route_to_digest", "source": source, "pages": int(n), "dedup_keys": keys}}


def aar_missed(source, day, n, titles, decisions, ledger=None, first_ts=None) -> dict:
    return {
        "kind": "missed_event", "ref": f"triage:me:{source}:{day}", "subject": f"source:{source}",
        "occurred_at": first_ts,
        "q1_expected": f"Real problems reported by {source} on {day} would reach Little Mister.",
        "q2_actual": f"{n} alert(s) were {'/'.join(sorted(set(decisions or [])))} by triage and alert_learn later "
                     f"graded them real. Examples: " + "; ".join(_clip(t, 80) for t in (titles or [])[:3]) + ".",
        "q3_why": "Triage judged them routine and held them back, and the follow-up showed they were real. "
                  + grade_note(ledger, f"detector:{source}"),
        "q4_sustain": "The suppressed alerts were still recorded and graded, so the miss was caught.",
        "q4_fix": f"Stop triage from suppressing {source}: page it unconditionally for 14 days and re-measure.",
        "fix_kind": "rule", "weight": int(n),
        "proposed_change": {"type": "hard_page", "source": source, "missed": int(n), "days": 14}}


def aar_guard(rid, ts, context, reason, guard, source, repeat=False) -> dict:
    return {
        "kind": "overreach", "ref": f"guard:{rid}", "subject": f"guard:{guard}:{source}",
        "occurred_at": ts,
        "q1_expected": f"{source} would only attempt actions inside Nova's safety guards.",
        "q2_actual": f"It attempted something the {guard} guard refused ({_clip(context, 80)}): {_clip(reason, 160).rstrip('.')}."
                     + (" It was a repeat of an earlier refused attempt." if repeat else ""),
        "q3_why": "The request reached execution without being checked against the guard first; the guard caught it.",
        "q4_sustain": "The guard held and the refusal was logged and reported (honest stopping).",
        "q4_fix": (f"Make {source} check the {guard} guard before proposing, so it stops asking." if repeat
                   else "None if it does not recur; a second attempt makes this a rule fix."),
        "fix_kind": "rule" if repeat else "none", "weight": 1,
        "proposed_change": {"type": "precheck_guard", "source": source, "guard": guard}}


def aar_ledger(lid, ts, action, target, vetoed, reverted, note, source) -> dict:
    what = "vetoed" if vetoed else "reverted"
    return {
        "kind": "overreach", "ref": f"ledger:{lid}", "subject": f"autonomy:{source}",
        "occurred_at": ts,
        "q1_expected": f"Autonomous action #{lid} by {source} would be one Little Mister was glad she took.",
        "q2_actual": f"It was {what}: {_clip(action, 160)}" + (f" on {target}" if target else "")
                     + (f". Note: {_clip(note, 120)}" if note else "."),
        "q3_why": "She judged it inside her autonomy; the veto/revert says that judgement was wrong this time.",
        "q4_sustain": "The veto window and rollback path worked.",
        "q4_fix": f"Require a proposal (human approval) for this class of action from {source}.",
        "fix_kind": "rule", "weight": 1,
        "proposed_change": {"type": "require_approval", "source": source, "action": _clip(action, 120)}}


def aar_escalation(eid, ts, source, rung, reason, feedback) -> dict:
    return {
        "kind": "overreach", "ref": f"escalation:{eid}", "subject": f"escalation:{source}",
        "occurred_at": ts,
        "q1_expected": f"Escalation #{eid} from {source} ({rung}) would be one Little Mister needed.",
        "q2_actual": f"He marked it unneeded: {_clip(feedback, 120)}. It went out because: {_clip(reason, 160)}.",
        "q3_why": "The two keys it was allowed on did not reflect what he cared about.",
        "q4_sustain": "The decision and its keys were logged, so it can be reviewed.",
        "q4_fix": f"Raise the escalation bar for {source} by one rung.",
        "fix_kind": "threshold", "weight": 1, "proposed_change": {"type": "raise_rung", "source": source}}


def plan_rollup(rows: list, cap: int = MAX_PROPOSALS) -> list:
    """rows: [{id, kind, subject, fix_kind, weight, proposed_change, q4_fix}] (open). -> proposals
    [{kind, subject, ids, weight, action, rationale}], heaviest first, capped. Pure."""
    groups: dict = {}
    for r in rows:
        if r.get("fix_kind") == "none" and r["kind"] != "wrong_prediction":
            continue
        groups.setdefault((r["kind"], r.get("subject") or "?"), []).append(r)
    out = []
    for (kind, subj), g in groups.items():
        w = sum(int(x.get("weight") or 1) for x in g)
        if not (len(g) >= 2 or (kind == "false_alarm" and w >= BIG_FALSE_ALARM)):
            continue
        if kind == "wrong_prediction" and not any(x.get("fix_kind") == "threshold" for x in g):
            continue
        out.append({"kind": kind, "subject": subj, "ids": [x["id"] for x in g], "weight": w,
                    "action": proposal_action(kind, subj, g, w),
                    "rationale": f"Hotwash roll-up: {len(g)} after-action review(s) of kind {kind} for {subj} "
                                 f"this week (weight {w}). Fix each one named: {_clip(g[0].get('q4_fix'), 200)}"})
    out.sort(key=lambda p: -p["weight"])
    return out[:cap]


def proposal_action(kind, subj, g, w) -> str:
    name = subj.split(":", 1)[-1]
    if kind == "false_alarm":
        return (f"Route alerts from {name} to #nova-digest (info) instead of paging, unless alert_learn graded one "
                f"real in the last 14 days ({w} false pages in {len(g)} day(s) this week).")
    if kind == "missed_event":
        return (f"Mark {name} hard-page so AI triage cannot suppress or downgrade it for 14 days "
                f"({w} real alerts held back in {len(g)} day(s)).")
    if kind == "wrong_prediction":
        pc = next((x["proposed_change"] for x in g if (x.get("proposed_change") or {}).get("base_rate") is not None), {})
        base = pc.get("base_rate")
        cap_txt = f"{base:.0%}" if base is not None else "its base rate"
        sk = f" (Brier skill {pc['skill']:+.2f})" if pc.get("skill") is not None else ""
        return (f"Cap stated confidence for {name} predictions at the base rate {cap_txt}{sk} until "
                f"nova_soft_certainty shows positive skill ({len(g)} misses this week).")
    if subj.startswith("guard:"):
        _g, guard, src = (subj.split(":", 2) + ["", ""])[:3]
        return f"Have {src} check the {guard} guard before proposing, so refused actions stop recurring ({len(g)} refusals)."
    if subj.startswith("autonomy:"):
        return f"Require human approval for the autonomous actions from {name} that were vetoed/reverted ({len(g)} this week)."
    return f"Raise the escalation bar one rung for {name} ({len(g)} unneeded escalations)."


# ── DB side ─────────────────────────────────────────────────────────────────

def upsert(cur, row: dict):
    """Insert one AAR; an existing OPEN row with the same ref is refreshed. -> id or None."""
    vals = [row.get(c) for c in COLS]
    vals[COLS.index("proposed_change")] = json.dumps(row.get("proposed_change") or {}, default=str)
    cur.execute(
        "INSERT INTO hotwash (kind, ref, subject, occurred_at, q1_expected, q2_actual, q3_why, q4_sustain, q4_fix, "
        "fix_kind, weight, proposed_change) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) "
        "ON CONFLICT (ref) DO UPDATE SET q2_actual=EXCLUDED.q2_actual, q3_why=EXCLUDED.q3_why, "
        "weight=EXCLUDED.weight, proposed_change=EXCLUDED.proposed_change WHERE hotwash.status='open' "
        "RETURNING id", vals)
    r = cur.fetchone()
    return r[0] if r else None


def aar_unlogged(ref: str, producer: str, summary: str, ts=None) -> dict:
    """Guardrail 7a (no unlogged actions): an action Nova was observed taking with no ledger row."""
    return {
        "kind": "overreach", "ref": ref[:200], "subject": f"unlogged:{producer}", "occurred_at": ts,
        "q1_expected": f"Every action by {producer} would leave a row in an action/restraint ledger.",
        "q2_actual": _clip(summary, 200) + ".",
        "q3_why": f"{producer} acts through a path that does not write a ledger (it predates the rule).",
        "q4_sustain": "The daily action audit saw it from the outside, so the gap is visible.",
        "q4_fix": f"Route {producer} through a ledgered path (nova_notify / post_both / autonomy_ledger).",
        "fix_kind": "rule", "weight": 1, "proposed_change": {"type": "ledger_adoption", "producer": producer}}


def file_hotwash(cur, kind: str = "overreach", ref: str = "", summary: str = "", producer: str | None = None):
    """Adapter for other organs (nova_action_audit). Only 'overreach' via the unlogged-action path today."""
    if kind != "overreach" or not ref:
        return None
    ensure_schema(cur)
    prod = producer or ref.split(":")[-1]
    return upsert(cur, aar_unlogged(ref, prod, summary))


def from_prediction(oc, pred_id, statement, domain, conf, reasoning) -> int | None:
    """Hook for the "I was wrong" loop. Never raises — a hotwash hiccup must not block resolution."""
    try:
        db = None
        try:
            import nova_soft_certainty as sc
            db = sc.domain_brier(oc, domain)
        except Exception:  # noqa: BLE001
            db = None
        ensure_schema(oc)
        return upsert(oc, aar_prediction(pred_id, statement, domain, conf, reasoning, db,
                                         datetime.now(timezone.utc)))
    except Exception as e:  # noqa: BLE001
        log(f"from_prediction #{pred_id} failed: {e}")
        return None


def _exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (table,))
    r = cur.fetchone()
    return bool(r and r[0])


def _has_col(cur, table: str, col: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s", (table, col))
    return bool(cur.fetchone())


def gather(cur, hours: int = 72) -> list:
    """All AAR rows the data supports for the last `hours`. Read-only."""
    try:
        from nova_cardinal import load_ledger
        ledger = load_ledger(cur)
    except Exception:  # noqa: BLE001
        ledger = {}
    rows = []
    tz = "America/Los_Angeles"
    cur.execute(
        "SELECT source, (ts AT TIME ZONE %s)::date, count(*), (array_agg(title ORDER BY ts))[1:3], "
        "(array_agg(DISTINCT dedup_key))[1:5], min(ts) FROM alert_triage_log WHERE decision='page' "
        "AND outcome='was_noise' AND ts > now() - make_interval(hours => %s) GROUP BY 1,2", (tz, hours))
    for src, day, n, titles, keys, first in cur.fetchall():
        rows.append(aar_false_alarm(src or "unknown", day, n, titles, keys, ledger, first))
    cur.execute(
        "SELECT source, (ts AT TIME ZONE %s)::date, count(*), (array_agg(title ORDER BY ts))[1:3], "
        "array_agg(DISTINCT decision), min(ts) FROM alert_triage_log WHERE decision IN ('suppress','downgrade') "
        "AND outcome='was_real' AND ts > now() - make_interval(hours => %s) GROUP BY 1,2", (tz, hours))
    for src, day, n, titles, decs, first in cur.fetchall():
        rows.append(aar_missed(src or "unknown", day, n, titles, decs, ledger, first))
    cur.execute(
        "SELECT id, ts, context, reason_held_back, coalesce(detail->>'guard','?'), coalesce(detail->>'source','?'), "
        "coalesce((detail->>'repeat_attempt')::boolean, false) FROM restraint_ledger WHERE channel='guard' "
        "AND ts > now() - make_interval(hours => %s)", (hours,))
    for rid, ts, ctx, why, guard, src, rep in cur.fetchall():
        rows.append(aar_guard(rid, ts, ctx, why, guard, src, rep))
    cur.execute(
        "SELECT id, ts, action, target, vetoed, reverted, veto_note, source FROM autonomy_ledger "
        "WHERE (vetoed OR reverted) AND ts > now() - make_interval(hours => %s)", (hours,))
    for lid, ts, act, tgt, vet, rev, note, src in cur.fetchall():
        rows.append(aar_ledger(lid, ts, act, tgt, vet, rev, note, src))
    if _exists(cur, "escalation_log") and _has_col(cur, "escalation_log", "feedback"):
        cur.execute("SELECT id, ts, source, rung, reason, feedback FROM escalation_log WHERE allowed "
                    "AND feedback ILIKE %s AND ts > now() - make_interval(hours => %s)", ("unneeded%", hours))
        for eid, ts, src, rung, why, fb in cur.fetchall():
            rows.append(aar_escalation(eid, ts, src, rung, why, fb))
    hw = _exists(cur, "hotwash")
    cur.execute(
        "SELECT p.id, p.statement, p.domain, p.confidence, p.reasoning, p.resolved_at FROM predictions p "
        "WHERE p.status='resolved' AND p.outcome='incorrect' AND p.resolved_at > now() - interval '48 hours'"
        + (" AND NOT EXISTS (SELECT 1 FROM hotwash h WHERE h.ref = 'pred:' || p.id)" if hw else ""))
    preds = cur.fetchall()
    dbs: dict = {}
    for pid, st, dom, conf, why, rts in preds:
        if dom not in dbs:
            try:
                import nova_soft_certainty as sc
                dbs[dom] = sc.domain_brier(cur, dom)
            except Exception:  # noqa: BLE001
                dbs[dom] = None
        rows.append(aar_prediction(pid, st, dom, conf, why, dbs[dom], rts))
    return rows


def sweep(cur, dry: bool = False, hours: int = 72) -> list:
    rows = gather(cur, hours)
    if dry:
        return rows
    ensure_schema(cur)
    n = sum(1 for r in rows if upsert(cur, r) is not None)
    log(f"sweep: {len(rows)} after-action review(s) found, {n} new or refreshed")
    return rows


def reviewer(cur) -> str:
    try:
        cur.execute("SELECT value FROM service_config WHERE service='hotwash' AND key='reviewer'")
        r = cur.fetchone()
        v = (r[0] if r else "jordan") or "jordan"
        v = json.loads(v) if isinstance(v, str) and v.startswith('"') else v
        return str(v)
    except Exception:  # noqa: BLE001
        return "jordan"


def _file_with_jordan(cur, p: dict) -> dict:
    import nova_coagency
    return _retry(lambda: nova_coagency.file_proposal(cur, origin="hotwash", action=p["action"],
                                                      rationale=p["rationale"],
                                                      context=json.dumps({"hotwash_ids": p["ids"]})),
                  "file_proposal")


REVIEWERS = {"jordan": _file_with_jordan}


def rollup(cur, dry: bool = False) -> list:
    ensure_schema(cur) if not dry else None
    if dry and not _exists(cur, "hotwash"):
        return []
    cur.execute("SELECT id, kind, subject, fix_kind, weight, proposed_change, q4_fix FROM hotwash WHERE status='open' "
                "AND ts > now() - interval '8 days'")
    cols = ("id", "kind", "subject", "fix_kind", "weight", "proposed_change", "q4_fix")
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        if isinstance(r["proposed_change"], str):
            r["proposed_change"] = json.loads(r["proposed_change"])
    plan = plan_rollup(rows)
    if dry:
        return plan
    wk = week_key(datetime.now(timezone.utc))
    who = reviewer(cur)
    fn = REVIEWERS.get(who)
    if fn is None:
        log(f"reviewer '{who}' is not implemented (no Claude auto-reviewer exists) — falling back to jordan")
        fn = REVIEWERS["jordan"]
    proposed_ids = set()
    for p in plan:
        try:
            res = fn(cur, p)
        except Exception as e:  # noqa: BLE001
            log(f"could not file proposal for {p['subject']}: {e}")
            continue
        pid = res.get("pid") if res.get("filed") else None
        status = "proposed" if pid else "rolled_up"
        cur.execute("UPDATE hotwash SET status=%s, proposal_id=%s, rollup_week=%s WHERE id = ANY(%s)",
                    (status, pid, wk, p["ids"]))
        proposed_ids.update(p["ids"])
        log(f"{p['subject']}: {res.get('status')} #{pid} — {p['action'][:100]}")
    rest = [r["id"] for r in rows if r["id"] not in proposed_ids]
    if rest:
        cur.execute("UPDATE hotwash SET status='rolled_up', rollup_week=%s WHERE id = ANY(%s) AND status='open' "
                    "AND ts < now() - interval '7 days'", (wk, rest))
    return plan


def list_rows(cur, days: int = 7) -> int:
    cur.execute("SELECT id, ts, kind, ref, q1_expected, q2_actual, q3_why, q4_sustain, q4_fix, status FROM hotwash "
                "WHERE ts > now() - make_interval(days => %s) ORDER BY ts DESC LIMIT 50", (days,))
    for r in cur.fetchall():
        print(f"#{r[0]} {r[1]:%Y-%m-%d %H:%M} {r[2]} {r[3]} [{r[9]}]\n  1 expected: {r[4]}\n  2 actual:   {r[5]}"
              f"\n  3 why:      {r[6]}\n  4 sustain:  {r[7]}\n    fix:      {r[8]}")
    return 0


def selftest() -> int:
    a = aar_false_alarm("nova_x.py", date(2026, 10, 8), 12, ["t1", "t2"], ["k", "k"])
    assert a["ref"] == "triage:fa:nova_x.py:2026-10-08" and a["fix_kind"] == "threshold" and a["weight"] == 12
    p = aar_prediction(5, "s", "self", 0.8, "r", {"n": 100, "base": 0.45, "skill": -0.1})
    assert p["fix_kind"] == "threshold" and "base rate" in p["q4_fix"]
    plan = plan_rollup([dict(a, id=1), dict(p, id=2), dict(p, id=3, ref="pred:6")])
    assert {x["kind"] for x in plan} == {"false_alarm", "wrong_prediction"}, plan
    assert "45%" in [x for x in plan if x["kind"] == "wrong_prediction"][0]["action"]
    g = aar_guard(1, None, "ctx", "why", "physical", "scene-runner", False)
    assert plan_rollup([dict(g, id=1), dict(g, id=2)]) == []
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sweep", action="store_true", help="file after-action reviews for the last --hours")
    ap.add_argument("--rollup", action="store_true", help="weekly: turn repeated fixes into gated proposals")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--hours", type=int, default=72)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not (a.sweep or a.rollup or a.list):
        ap.print_help()
        return 0
    import nova_watch_common as W
    cur = W.connect().cursor()
    if a.sweep:
        rows = sweep(cur, a.dry_run, a.hours)
        if a.dry_run:
            for r in rows:
                print(f"{r['kind']:<16} {r['ref']}\n  1 {r['q1_expected']}\n  2 {r['q2_actual']}\n  3 {r['q3_why']}"
                      f"\n  4 {r['q4_sustain']} | fix: {r['q4_fix']}")
    if a.rollup:
        plan = rollup(cur, a.dry_run)
        for p in plan:
            print(f"PROPOSE [{p['kind']} {p['subject']} w={p['weight']} ids={p['ids']}]: {p['action']}")
        if not plan:
            print("rollup: nothing qualifies this week")
    if a.list:
        list_rows(cur, a.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
