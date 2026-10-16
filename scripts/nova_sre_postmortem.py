#!/usr/bin/env python3
"""nova_sre_postmortem.py — blameless postmortems in the Google SRE template, with tracked action items.

SRE book ch.15 ("Postmortem Culture: Learning from Failure") and Workbook ch.10. This is NOT the journal
column (nova_postmortem.py writes sarcastic pieces for the website); this is the engineering record.

Which incidents get one (telemetry.incidents, resolved within --hours):
  critical    every incident with severity='critical'
  recurring   every incident whose recurrence_key (stamped by nova_incident_lifecycle) opened
              >= lifecycle.RECURRENCE_THRESHOLD times in the RECURRENCE_LOOKBACK_DAYS before it
  Incidents rooted in Nova's own notifications (root category incident / incident_recurring /
  incident_escalation, e.g. "UNRESOLVED x78: <key> needs a PERMANENT fix") are skipped: they are the
  lifecycle escalating a key that already gets its own postmortem.

Idempotency: one postmortem per pm_key. A non-recurring critical incident -> 'inc:<id>'. A recurring
key -> 'rk:<recurrence_key>:<ISO week>' — every incident of that key in the week is folded into the
same document (incident_ids[]) instead of 70 copies of the same story. The doc is re-rendered while
status='draft'; it becomes 'final' when every action item is closed.

Document (deterministic, from the incident's own data): summary, impact, root causes, trigger,
detection, resolution, action items (owner, kind prevent|mitigate|detect), lessons learned (what went
well / what went wrong / where we got lucky) and the timeline from telemetry.events. Blameless: it
names systems and conditions, never a person (acked_by is reported as "acknowledged", not by whom).
--llm adds a short narrative from LOCAL Ollama (same model as nova_postmortem); it fails open — the
postmortem is written either way.

Action items: rows in postmortem_actions AND one claude_queue item each (same insert path as
nova_task_sentinel.run_daily: register the session for the FK, then insert). The queue description
is stable per (subject, kind), so an action already open from last week is linked, not re-filed.
At most MAX_FILE new queue items per run; the rest are filed on the next run. --sync marks actions
done/cancelled from their queue item's status and finalizes postmortems with nothing left open.

Little Mister, 2026-10-15 (plan item #40) — 75 queue items in 3 days, 0 done. Reworked:
  * ONE live postmortem per recurrence key (= per problem, nova_problems): a recurring key folds
    into its open draft/reviewed document instead of opening a new one every ISO week; a key whose
    problem is already a known_error / fix_in_change gets no new postmortem (the problem record is
    the follow-up). At most one open claude_queue item per problem key; MAX_FILE is 5 per run.
  * The templated prevent/mitigate/detect triple is gone. 0-3 SPECIFIC actions, each backed by a
    fact in the record (a runbook that failed, a detector graded mostly noise, detection slower than
    30 min, a problem with no workaround). No fact, no action.
  * New fields: due_at (critical 3d, else 5d), contributing_factors, slo_budget_consumed (largest
    error-budget drop across SLOs over the incident window), detect_s + detection_halved (time from
    first event to first notification, vs the previous postmortem of the same key), near_miss (no
    notification reached a channel and no SLO budget spent), problem_id.
  * REVIEW GATE, Nova's own (--review, part of --sync): draft -> reviewed when the gates pass
    (contributing factors, impact, detection, actions specific or justified none, problem linked);
    an overdue draft is accepted with its gaps written down. Only a reviewed postmortem with no open
    action becomes final. Jordan is never the reviewer.

CLI: --run [--hours 48] [--llm] [--dry-run] | --sync [--dry-run] | --review | --show <pm id> | --selftest
Tables: nova_ops.postmortems, nova_ops.postmortem_actions.  Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

KINDS = ("prevent", "mitigate", "detect")
OWNER = "nova-sre (claude_queue)"   # a role, never a person: blameless
SESSION = "sre-postmortem"
MAX_FILE = 5                         # was 15: 75 items in 3 days, none done (#40)
MAX_ACTIONS = 3
DUE_DAYS = {"critical": 3}           # everything else DUE_DAYS_DEFAULT
DUE_DAYS_DEFAULT = 5
LATE_DETECT_S = 1800
NOISY_DETECTOR_SHARE = 0.8
NOISY_DETECTOR_MIN = 5
LIVE = ("draft", "reviewed")
RETIRED_REASON = "templated prevent/mitigate/detect triple retired 2026-10-15 (#40); replaced by specific actions"
TEMPLATED_PREFIXES = ("Find and fix the underlying cause of", "Confirm the root cause of", "Alert on ",
                      "Check that ", "Verify the existing runbook", "Add a remediation runbook")
TIMELINE_MAX = 60
SELF_REFERENTIAL = ("incident", "incident_recurring", "incident_escalation")
QUEUE_OPEN = ("queued", "pending", "in_progress", "claimed", "deferred")
QUEUE_DONE = ("done", "completed", "resolved", "built")
QUEUE_CANCELLED = ("cancelled", "aged_out")
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "qwen3-coder:30b"
SCHEMA = """
CREATE TABLE IF NOT EXISTS postmortems (
  id bigserial PRIMARY KEY,
  pm_key text NOT NULL UNIQUE,
  incident_id bigint NOT NULL,
  incident_ids bigint[] NOT NULL DEFAULT '{}',
  recurrence_key text,
  trigger_reason text NOT NULL CHECK (trigger_reason IN ('critical','recurring','critical+recurring')),
  severity text,
  title text NOT NULL,
  doc text NOT NULL,
  narrative text,
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','final')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  finalized_at timestamptz);
CREATE INDEX IF NOT EXISTS postmortems_incident ON postmortems (incident_id);
CREATE INDEX IF NOT EXISTS postmortems_status ON postmortems (status, created_at);
CREATE TABLE IF NOT EXISTS postmortem_actions (
  id bigserial PRIMARY KEY,
  postmortem_id bigint NOT NULL REFERENCES postmortems(id),
  kind text NOT NULL CHECK (kind IN ('prevent','mitigate','detect')),
  owner text NOT NULL,
  description text NOT NULL,
  queue_desc text NOT NULL,
  queue_id int,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','cancelled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  done_at timestamptz,
  UNIQUE (postmortem_id, kind));
CREATE INDEX IF NOT EXISTS postmortem_actions_open ON postmortem_actions (status) WHERE status='open';
"""
# #40 additions — lock-safe (nova_pg_schema.safe_ddl skips what already exists)
SCHEMA_V2 = """
ALTER TABLE postmortems ADD COLUMN IF NOT EXISTS problem_id bigint,
  ADD COLUMN IF NOT EXISTS due_at timestamptz,
  ADD COLUMN IF NOT EXISTS contributing_factors jsonb NOT NULL DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS slo_budget_consumed double precision,
  ADD COLUMN IF NOT EXISTS detect_s integer,
  ADD COLUMN IF NOT EXISTS detection_halved boolean,
  ADD COLUMN IF NOT EXISTS near_miss boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS reviewed_at timestamptz,
  ADD COLUMN IF NOT EXISTS review_notes text;
ALTER TABLE postmortem_actions ADD COLUMN IF NOT EXISTS close_reason text;
"""
STATUS_CHECK = "CHECK (status IN ('draft','reviewed','final','superseded'))"


def log(m: str) -> None:
    print(f"[sre-postmortem {datetime.now():%H:%M:%S}] {m}", flush=True)


def _clip(s, n=200) -> str:
    return " ".join(str(s or "").split())[:n]


def week_key(d) -> str:
    y, w, _ = (d.date() if isinstance(d, datetime) else d).isocalendar()
    return f"{y}-W{w:02d}"


def _mins(seconds) -> str:
    if seconds is None:
        return "unknown"
    m = seconds / 60.0
    return f"{m:.0f} min" if m < 120 else f"{m / 60:.1f} h"


def _ts(t) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S %Z").strip() if t else "?"


# ── pure: selection + keys ──────────────────────────────────────────────────

def trigger_reason(severity: str, root_cat: str | None, recur_count: int, threshold: int) -> str | None:
    """Why this incident needs a postmortem, or None. Pure."""
    if (root_cat or "") in SELF_REFERENTIAL:
        return None
    crit = (severity or "") == "critical"
    rec = recur_count >= threshold
    if crit and rec:
        return "critical+recurring"
    return "critical" if crit else ("recurring" if rec else None)


def pm_key(incident_id: int, reason: str, recurrence_key: str | None, opened_at) -> str:
    """Idempotency key. Recurring -> one doc per key per ISO week; otherwise one per incident. Pure."""
    if "recurring" in reason and recurrence_key:
        return f"rk:{recurrence_key}:{week_key(opened_at)}"
    return f"inc:{incident_id}"


def subject_of(inc: dict) -> str:
    return inc.get("recurrence_key") or f"incident #{inc['id']}"


# ── pure: action items + lessons ────────────────────────────────────────────

def specific_actions(inc: dict, facts: dict) -> list:
    """0-3 actions, each backed by a fact in the record (#40, 2026-10-15). No fact -> no action.
    facts: {failed_runbooks: [..], noisy: (source, noise, graded) | None, detect_s, problem: {id, key,
    state} | None}. Worded per subject so the same action recurs with the same text. Pure."""
    subj = subject_of(inc)
    host, cat = inc.get("host") or "unknown", inc.get("root_cat") or "uncategorized"
    out = []
    prob = facts.get("problem") or {}
    if prob and prob.get("state") == "candidate":
        out.append({"kind": "prevent", "owner": OWNER, "description":
                    f"Problem #{prob['id']} ({prob['key']}) has no known workaround: find the cause and land a "
                    f"code/config/capacity fix, then record it with nova_problems.py --fix {prob['key']} --ref <commit>."})
    failed = sorted(set(facts.get("failed_runbooks") or []))
    if failed:
        out.append({"kind": "mitigate", "owner": OWNER, "description":
                    f"Runbook {', '.join(failed)} failed on {host}/{cat}: make it succeed (or retire it) so recovery "
                    f"of {subj} is automatic."})
    noisy = facts.get("noisy")
    if noisy:
        src, noise, graded = noisy
        out.append({"kind": "detect", "owner": OWNER, "subject": f"detector {src}", "description":
                    f"Tune detector {src}: {noise} of its {graded} graded alerts in 30 days were noise."})
    elif (facts.get("detect_s") or 0) > LATE_DETECT_S:
        out.append({"kind": "detect", "owner": OWNER, "description":
                    f"Detection of {subj} took {_mins(facts['detect_s'])} from first event to first notification; "
                    f"add an earlier probe or SLO signal for {host}/{cat}."})
    return out[:MAX_ACTIONS]


def is_templated(text: str | None) -> bool:
    """The retired generic triple (#40). Pure."""
    return (text or "").startswith(TEMPLATED_PREFIXES)


def contributing_factors(inc: dict, recur_count: int, remediations: list, pages: int, facts: dict) -> list:
    """Data-backed contributing factors (not the root cause, which stays a hypothesis). Pure."""
    f = []
    if recur_count > 1:
        f.append(f"{inc.get('recurrence_key')} recurred {recur_count}x in 7 days — earlier closures did not fix it")
    bad = sorted({r["action"] for r in remediations if r.get("status") == "failed"})
    if bad:
        f.append(f"remediation failed: {', '.join(bad)}")
    if not any(r.get("status") == "executed" for r in remediations) and not inc.get("resolution"):
        f.append("no automated or recorded fix — recovery was self-healing")
    if facts.get("noisy"):
        src, noise, graded = facts["noisy"]
        f.append(f"detector {src} is noisy ({noise}/{graded} graded alerts were noise)")
    if (facts.get("detect_s") or 0) > LATE_DETECT_S:
        f.append(f"late detection ({_mins(facts['detect_s'])} to first notification)")
    if pages > 3:
        f.append(f"{pages} notifications for one problem")
    if facts.get("change"):
        f.append(f"a change landed in the 24h before: {facts['change']}")
    return f


def due_at(created, severity: str | None):
    """critical 3 days, everything else 5 (#40). Pure."""
    from datetime import timedelta
    return created + timedelta(days=DUE_DAYS.get(severity or "", DUE_DAYS_DEFAULT))


def detection_halved(cur_s, prev_s):
    """Did time-to-detect halve against the previous postmortem of the same key? None = no baseline. Pure."""
    if cur_s is None or not prev_s:
        return None
    return cur_s <= prev_s / 2.0


def near_miss(pages: int, budget) -> bool:
    """Caught and cleared with no notification reaching a channel and no SLO budget spent. Pure."""
    return pages == 0 and not (budget or 0) > 0


def review_gaps(pm: dict, now=None) -> list:
    """Nova's review gate (#40). pm: {contributing_factors, detect_s, pages, actions[], recurring, problem_id,
    problem_state}. Impact (duration, notifications, SLO budget where an SLO covered the window) is always
    rendered, so it is not a gate. -> list of gaps (empty = passes). Pure."""
    gaps = []
    if not pm.get("contributing_factors"):
        gaps.append("no contributing factors recorded")
    if pm.get("detect_s") is None and (pm.get("pages") or 0) > 0:
        gaps.append("detection time unknown although notifications went out")
    acts = pm.get("actions") or []
    if len(acts) > MAX_ACTIONS:
        gaps.append(f"{len(acts)} actions (max {MAX_ACTIONS})")
    if any(is_templated(a.get("description")) for a in acts):
        gaps.append("templated action present")
    if not acts and pm.get("problem_state") == "candidate":
        gaps.append("no action although the problem has no workaround")
    if pm.get("recurring") and not pm.get("problem_id"):
        gaps.append("recurring but not linked to a problem record")
    return gaps


def queue_desc(subject: str, kind: str, text: str) -> str:
    """Stable claude_queue description for one action — the dedup key across weeks. Pure."""
    return f"Postmortem action [{kind}] {subject}: {_clip(text, 300)}"


def lessons(inc: dict, recur_count: int, remediations: list, pages: int) -> dict:
    """What went well / wrong / where we got lucky — from data, blameless. Pure."""
    well, wrong, lucky = [], [], []
    if inc.get("first_source"):
        well.append(f"Detected automatically by {inc['first_source']} — no human had to notice it.")
    if (inc.get("member_count") or 0) > 1:
        well.append(f"The correlator folded {inc['member_count']} symptom events into one incident.")
    if inc.get("mttr_s") is not None:
        well.append(f"The lifecycle closed it and measured MTTR ({_mins(inc['mttr_s'])}).")
    ok = [r for r in remediations if r.get("status") == "executed"]
    bad = [r for r in remediations if r.get("status") == "failed"]
    if ok:
        well.append(f"Automated remediation ran: {', '.join(sorted({r['action'] for r in ok}))}.")
    if recur_count > 1:
        wrong.append(f"The same condition ({inc.get('recurrence_key')}) opened {recur_count} times in 7 days — "
                     f"earlier closures treated the symptom, not the cause.")
    if bad:
        wrong.append(f"Remediation failed: {', '.join(sorted({r['action'] for r in bad}))}.")
    if inc.get("acked_at") is None:
        wrong.append("No acknowledgement was recorded, so MTTA is unknown and nobody owned it while it was open.")
    if (inc.get("mttr_s") or 0) > 3600:
        wrong.append(f"Time to resolve was {_mins(inc['mttr_s'])}.")
    if pages > 3:
        wrong.append(f"{pages} notifications went out for one problem.")
    if not ok and not inc.get("resolution"):
        lucky.append("It cleared with no recorded fix — recovery was self-healing or luck we cannot explain, "
                     "not engineered resilience.")
    if inc.get("severity") == "critical" and inc.get("acked_at") is None:
        lucky.append("A critical condition went unacknowledged and still resolved.")
    return {"well": well or ["Nothing notable."], "wrong": wrong or ["Nothing notable."],
            "lucky": lucky or ["Nothing notable."]}


# ── pure: render ────────────────────────────────────────────────────────────

def render(inc: dict, reason: str, incidents: list, events: list, actions: list, les: dict,
           recur_count: int, remediations: list, pages: int, narrative: str | None = None,
           extra: dict | None = None) -> str:
    """Markdown postmortem in the SRE-book template. `inc` is the primary (latest) incident. `extra`
    (#40): {factors, budget, detect_s, halved, near_miss, due_at, problem}. Pure."""
    x = extra or {}
    subj = subject_of(inc)
    n = len(incidents)
    hosts = sorted({i.get("host") or "unknown" for i in incidents})
    total_mttr = sum((i.get("mttr_s") or 0) for i in incidents)
    title = (f"Postmortem: {subj} ({n} incidents, week {week_key(inc['opened_at'])})" if n > 1
             else f"Postmortem: {_clip(inc.get('title'), 120)}")
    L = [f"# {title}", "",
         f"**Status:** draft · **Trigger:** {reason} · **Incidents:** "
         + ", ".join(f"#{i['id']}" for i in incidents) + " · **Blameless:** yes"
         + (f" · **Due:** {x['due_at']:%Y-%m-%d}" if x.get("due_at") else "")
         + (f" · **Problem:** #{x['problem']['id']} ({x['problem']['state']})" if x.get("problem") else ""), ""]
    if narrative:
        L += ["## Narrative (local LLM, unverified)", narrative.strip(), ""]
    L += ["## Summary",
          f"{inc.get('severity', 'warning').capitalize()} incident on {', '.join(hosts)}: "
          f"{_clip(inc.get('title'), 160)}."
          + (f" This condition opened {recur_count} times in the 7 days before the latest occurrence."
             if recur_count > 1 else ""), "",
          "## Impact",
          f"- Severity: {inc.get('severity')}; hosts: {', '.join(hosts)}",
          f"- Duration: {_mins(total_mttr)} total across {n} incident(s)",
          f"- Symptom events: {sum((i.get('member_count') or 0) for i in incidents)}; notifications sent: {pages}",
          "- SLO error budget consumed: " + (f"{x['budget']:.1%}" if x.get("budget") is not None else "none measured"),
          f"- Near miss: {'yes — no notification reached a channel and no budget was spent' if x.get('near_miss') else 'no'}",
          "",
          "## Root causes",
          f"- Correlator's hypothesis (unverified): {_clip(inc.get('summary'), 500) or 'none recorded.'}"]
    if recur_count > 1:
        L.append(f"- Contributing: {inc.get('recurrence_key')} recurred {recur_count}x in 7 days, so the underlying "
                 f"condition survived every earlier closure.")
    L += ["", "## Trigger",
          f"{_clip(inc.get('root_title'), 200) or 'Unknown'}"
          + (f" — {_clip(inc.get('root_body'), 300)}" if inc.get("root_body") else ""), "",
          "## Detection",
          f"Detected by {inc.get('first_source') or 'unknown source'} at {_ts(inc.get('first_ts'))} "
          f"(level {inc.get('first_level') or '?'}, category {inc.get('root_cat') or '?'}). "
          + (f"Acknowledged after {_mins(inc.get('mtta_s'))}." if inc.get("acked_at") else "Never acknowledged.")
          + f" Time to detect (first event -> first notification): {_mins(x.get('detect_s'))}; detection halved vs "
            f"the previous postmortem of this key: "
          + {True: "yes", False: "no", None: "no baseline"}[x.get("halved")] + ".",
          "", "## Contributing factors"] + [f"- {c}" for c in (x.get("factors") or ["none recorded"])] + [
          "", "## Resolution"]
    if inc.get("resolution"):
        L.append(_clip(inc["resolution"], 400))
    elif remediations:
        L.append("; ".join(f"{r['action']} {r['status']}" for r in remediations[:6]) + ".")
    else:
        L.append("Auto-closed by nova_incident_lifecycle after its events went quiet; no fix was recorded.")
    L += ["", "## Action items"]
    if not actions:
        L.append("None: no fact in the record calls for one"
                 + (" (near miss)." if x.get("near_miss") else
                    f" (problem #{x['problem']['id']} is {x['problem']['state']})." if x.get("problem") else "."))
    else:
        L += ["| Kind | Owner | Action | Tracking |", "|---|---|---|---|"]
    for a in actions:
        trk = f"claude_queue #{a['queue_id']}" if a.get("queue_id") else "to file"
        L.append(f"| {a['kind']} | {a['owner']} | {a['description']} | {trk} ({a.get('status', 'open')}) |")
    L += ["", "## Lessons learned", "### What went well"] + [f"- {x}" for x in les["well"]]
    L += ["### What went wrong"] + [f"- {x}" for x in les["wrong"]]
    L += ["### Where we got lucky"] + [f"- {x}" for x in les["lucky"]]
    L += ["", "## Timeline"]
    tl = []
    for i in incidents:
        tl.append((i["opened_at"], f"incident #{i['id']} opened ({i.get('severity')})"))
        if i.get("acked_at"):
            tl.append((i["acked_at"], f"incident #{i['id']} acknowledged"))
        if i.get("resolved_at"):
            tl.append((i["resolved_at"], f"incident #{i['id']} resolved"))
    for e in events:
        tl.append((e["ts"], f"[{e.get('level')}] {e.get('source')}: {_clip(e.get('title'), 120)}"
                   + (f" -> {e['channel']}" if e.get("channel") else "")))
    for r in remediations:
        tl.append((r.get("executed_at") or r["requested_at"], f"remediation {r['action']} {r['status']}"))
    tl.sort(key=lambda x: x[0])
    if len(tl) > TIMELINE_MAX:
        tl = tl[:TIMELINE_MAX // 2] + [(None, f"... {len(tl) - TIMELINE_MAX} entries omitted ...")] + tl[-TIMELINE_MAX // 2:]
    L += [f"- {t:%m-%d %H:%M:%S} {txt}" if t else f"- {txt}" for t, txt in tl]
    return "\n".join(L) + "\n"


# ── optional LLM narrative (fail open) ──────────────────────────────────────

def narrative_llm(doc: str, _urlopen=urllib.request.urlopen) -> str | None:
    """3-4 sentence blameless narrative from LOCAL Ollama. Any failure -> None (postmortem still written)."""
    prompt = ("/no_think\nYou write blameless SRE postmortem summaries. In 3-4 plain sentences, say what failed, "
              "the impact, and the most important fix. Never name or blame a person. Use only these facts:\n\n"
              + doc[:6000])
    body = json.dumps({"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "think": False,
                       "options": {"temperature": 0.2, "num_predict": 300}}).encode()
    try:
        req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
        with _urlopen(req, timeout=90) as r:
            txt = (json.loads(r.read()).get("response") or "").strip()
        if "</think>" in txt:
            txt = txt.split("</think>", 1)[-1].strip()
        return txt[:1500] or None
    except Exception as e:  # noqa: BLE001
        log(f"LLM narrative unavailable ({e}) — writing the deterministic postmortem")
        return None


# ── DB side ─────────────────────────────────────────────────────────────────

def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, SCHEMA_V2)
    # status gains 'reviewed' (Nova's review gate) and 'superseded' (folded duplicates) — #40
    cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='postmortems_status_check'")
    r = cur.fetchone()
    if r and "reviewed" not in (r[0] or ""):
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("ALTER TABLE postmortems DROP CONSTRAINT postmortems_status_check, "
                    "ADD CONSTRAINT postmortems_status_check " + STATUS_CHECK)


def _runbooks() -> dict:
    try:
        import nova_remediation
        return dict(nova_remediation.RUNBOOKS)
    except Exception:  # noqa: BLE001
        return {}


def _threshold():
    try:
        import nova_incident_lifecycle as lc
        return lc.RECURRENCE_THRESHOLD, lc.RECURRENCE_LOOKBACK_DAYS
    except Exception:  # noqa: BLE001
        return 3, 7


INC_COLS = ("id", "opened_at", "resolved_at", "acked_at", "severity", "host", "title", "summary", "member_count",
            "recurrence_key", "mtta_s", "mttr_s", "resolution", "root_cat", "root_title", "root_body")
INC_SQL = ("SELECT i.id, i.opened_at, i.resolved_at, i.acked_at, i.severity, i.host, i.title, i.summary, "
           "i.member_count, i.recurrence_key, i.mtta_s, i.mttr_s, i.resolution, e.category, e.title, e.body "
           "FROM telemetry.incidents i LEFT JOIN telemetry.events e ON e.id = i.root_event ")
GROUP_MAX = 100


def candidates(cur, hours: int) -> list:
    """Resolved incidents in the window that need a postmortem: [(inc, reason, recur_count)]."""
    thr, days = _threshold()
    cur.execute(INC_SQL + "WHERE i.status='resolved' AND i.resolved_at > now() - make_interval(hours => %s) "
                "ORDER BY i.opened_at", (hours,))
    out = []
    for row in cur.fetchall():
        inc = dict(zip(INC_COLS, row))
        n = 0
        if inc["recurrence_key"]:
            cur.execute("SELECT count(*) FROM telemetry.incidents WHERE recurrence_key=%s AND opened_at <= %s "
                        "AND opened_at > %s - make_interval(days => %s)",
                        (inc["recurrence_key"], inc["opened_at"], inc["opened_at"], days))
            n = cur.fetchone()[0]
        reason = trigger_reason(inc["severity"], inc["root_cat"], n, thr)
        if reason:
            out.append((inc, reason, n))
    return out


def _group_incidents(cur, key: str, reason: str, inc: dict) -> list:
    """All resolved incidents covered by this pm_key, oldest first. A reused live key (#40) covers
    everything since its own ISO week, capped at the latest GROUP_MAX."""
    if key.startswith("rk:"):
        wk = key.rsplit(":", 1)[-1]
        if not re.fullmatch(r"\d{4}-W\d{2}", wk):
            wk = week_key(inc["opened_at"])
        y, w = int(wk[:4]), int(wk[-2:])
        cur.execute(INC_SQL + "WHERE i.recurrence_key=%s AND i.status='resolved' "
                    "AND i.opened_at >= to_date(%s, 'IYYY-IW') ORDER BY i.opened_at DESC LIMIT %s",
                    (inc["recurrence_key"], f"{y}-{w:02d}", GROUP_MAX))
        rows = [dict(zip(INC_COLS, r)) for r in cur.fetchall()][::-1]
        return rows or [inc]
    return [inc]


def time_to_detect(events: list) -> int | None:
    """Median over incidents of (first event -> first notification that reached a channel), seconds. Pure."""
    by: dict = {}
    for e in events:
        by.setdefault(e.get("incident_id"), []).append(e)
    vals = []
    for evs in by.values():
        first = min((e["ts"] for e in evs if e.get("ts")), default=None)
        sent = [e["sent_at"] for e in evs if e.get("sent_at") and e.get("channel")
                and not str(e["channel"]).startswith(("triage", "detector", "maintenance", "problem"))]
        if first and sent:
            vals.append(max(0, int((min(sent) - first).total_seconds())))
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def _enrich(cur, incidents: list):
    ids = [i["id"] for i in incidents]
    cur.execute("SELECT ts, level, source, title, channel, incident_id, sent_at FROM telemetry.events "
                "WHERE incident_id = ANY(%s) ORDER BY ts", (ids,))
    events = [dict(zip(("ts", "level", "source", "title", "channel", "incident_id", "sent_at"), r))
              for r in cur.fetchall()]
    rem = []
    cur.execute("SELECT to_regclass('telemetry.remediations')")
    if cur.fetchone()[0]:
        cur.execute("SELECT action, status, requested_at, executed_at FROM telemetry.remediations "
                    "WHERE incident_id = ANY(%s) AND NOT dry_run ORDER BY requested_at", (ids,))
        rem = [dict(zip(("action", "status", "requested_at", "executed_at"), r)) for r in cur.fetchall()]
    for i in incidents:
        first = next((e for e in events if e["incident_id"] == i["id"]), None)
        i["first_source"] = first["source"] if first else None
        i["first_ts"] = first["ts"] if first else i["opened_at"]
        i["first_level"] = first["level"] if first else None
    pages = count_pages(events)
    return events, rem, pages


def count_pages(events: list) -> int:
    """Notifications that actually reached a channel (triage-suppressed/held ones are not pages). Pure."""
    return sum(1 for e in events if e.get("channel")
               and not str(e["channel"]).startswith(("triage", "detector", "maintenance", "problem")))


def _q1(cur, sql: str, params=()):
    """Optional-organ lookup inside a savepoint, so a missing table never aborts the run's transaction."""
    try:
        cur.execute("SAVEPOINT pm_opt")
        cur.execute(sql, params)
        r = cur.fetchone()
        cur.execute("RELEASE SAVEPOINT pm_opt")
        return r
    except Exception as e:  # noqa: BLE001 — optional organs
        try:
            cur.execute("ROLLBACK TO SAVEPOINT pm_opt")
        except Exception:  # noqa: BLE001
            pass
        log(f"optional lookup skipped ({e})")
        return None


def _problem_for(cur, key: str | None) -> dict | None:
    if not key:
        return None
    r = _q1(cur, "SELECT id, state FROM problems WHERE recurrence_key=%s", (key,))
    return {"id": r[0], "state": r[1], "key": key} if r else None


def _noisy_detector(cur, source: str | None):
    """(source, noise, graded) when alert_learn graded this source mostly noise in 30 days, else None."""
    if not source:
        return None
    r = _q1(cur, "SELECT count(*) FILTER (WHERE outcome='was_noise'), count(*) FROM alert_triage_log "
                 "WHERE source=%s AND outcome IN ('was_real','was_noise') AND ts > now() - interval '30 days'",
            (source,))
    if r and r[1] and r[1] >= NOISY_DETECTOR_MIN and r[0] / r[1] >= NOISY_DETECTOR_SHARE:
        return (source, int(r[0]), int(r[1]))
    return None


def _slo_budget(cur, start, end) -> float | None:
    """Largest error-budget drop across SLOs between start and end (0.02 = 2% of the budget)."""
    if not start or not end:
        return None
    r = _q1(cur, "SELECT max(b0 - b1) FROM (SELECT name, "
                 "(SELECT budget_remaining FROM slo_status s0 WHERE s0.name=d.name AND s0.at <= %s ORDER BY at DESC LIMIT 1) b0, "
                 "(SELECT budget_remaining FROM slo_status s1 WHERE s1.name=d.name AND s1.at >= %s ORDER BY at LIMIT 1) b1 "
                 "FROM slo_definitions d WHERE d.enabled) x WHERE b0 IS NOT NULL AND b1 IS NOT NULL",
            (start, end))
    if not r or r[0] is None:
        return None
    return max(0.0, float(r[0]))


def _prev_detect(cur, key: str | None, pm_key_now: str):
    if not key:
        return None
    r = _q1(cur, "SELECT detect_s FROM postmortems WHERE recurrence_key=%s AND pm_key<>%s AND detect_s IS NOT NULL "
                 "ORDER BY created_at DESC LIMIT 1", (key, pm_key_now))
    return r[0] if r else None


def _recent_change(cur, start) -> str | None:
    r = _q1(cur, "SELECT id, left(proposed_action, 80) FROM coagency_proposals WHERE status='executed' "
                 "AND executed_at BETWEEN %s - interval '24 hours' AND %s ORDER BY executed_at DESC LIMIT 1",
            (start, start))
    return f"co-agency proposal #{r[0]} ({r[1]})" if r else None


def _ensure_session(cur) -> None:
    cur.execute("INSERT INTO claude_sessions (session_id, status, project) VALUES (%s, 'active', 'nova-sre') "
                "ON CONFLICT (session_id) DO NOTHING", (SESSION,))


def file_action(cur, pm_title: str, a: dict) -> int | None:
    """Link to an open queue item with the same description — or any open item for the same problem
    key (#40: one open item per problem) — else insert one. -> queue id."""
    cur.execute("SELECT id FROM claude_queue WHERE description=%s AND status IN %s ORDER BY id LIMIT 1",
                (a["queue_desc"], QUEUE_OPEN))
    r = cur.fetchone()
    if r:
        return r[0]
    if a.get("subject"):
        cur.execute("SELECT id FROM claude_queue WHERE description LIKE %s AND status IN %s ORDER BY id LIMIT 1",
                    (f"Postmortem action [%] {a['subject']}:%", QUEUE_OPEN))
        r = cur.fetchone()
        if r:
            return r[0]
    _ensure_session(cur)
    cur.execute("INSERT INTO claude_queue (session_id, created_at, updated_at, status, priority, description, context) "
                "VALUES (%s, now(), now(), 'queued', %s, %s, %s) RETURNING id",
                (SESSION, 4 if a["kind"] == "prevent" else 5, a["queue_desc"],
                 f"From blameless postmortem '{pm_title}' (nova_sre_postmortem.py, table postmortems). "
                 f"Owner: {a['owner']}. Kind: {a['kind']}. Mark this item done when the change has landed; the "
                 f"nightly --sync closes the action and finalizes the postmortem."))
    return cur.fetchone()[0]


def build(cur, inc: dict, reason: str, n: int, use_llm: bool = False, existing_actions=None,
          key: str | None = None) -> dict:
    key = key or pm_key(inc["id"], reason, inc["recurrence_key"], inc["opened_at"])
    group = _group_incidents(cur, key, reason, inc)
    primary = group[-1]
    if primary.get("root_cat") is None:
        primary["root_cat"] = inc.get("root_cat")
    events, rem, pages = _enrich(cur, group)
    subj = subject_of(primary)
    facts = {"problem": _problem_for(cur, primary.get("recurrence_key")),
             "failed_runbooks": [r["action"] for r in rem if r.get("status") == "failed"],
             "noisy": _noisy_detector(cur, primary.get("first_source")),
             "detect_s": time_to_detect(events),
             "change": _recent_change(cur, group[0]["opened_at"])}
    acts = specific_actions(primary, facts)
    for a in acts:
        a.setdefault("subject", subj)     # a detector-tuning action is per detector, shared across problems
        a["queue_desc"] = queue_desc(a["subject"], a["kind"], a["description"])
        prev = (existing_actions or {}).get(a["kind"])
        if prev:
            a.update(queue_id=prev[0], status=prev[1])
    les = lessons(primary, n, rem, pages)
    budget = _slo_budget(cur, group[0]["opened_at"], max((i.get("resolved_at") or i["opened_at"]) for i in group))
    sev = max((i["severity"] for i in group), key=lambda s: s == "critical")
    extra = {"factors": contributing_factors(primary, n, rem, pages, facts), "budget": budget,
             "detect_s": facts["detect_s"], "halved": detection_halved(facts["detect_s"],
                                                                       _prev_detect(cur, primary.get("recurrence_key"), key)),
             "near_miss": near_miss(pages, budget), "problem": facts["problem"],
             "due_at": due_at(datetime.now(timezone.utc), sev)}
    doc = render(primary, reason, group, events, acts, les, n, rem, pages, None, extra)
    narrative = narrative_llm(doc) if use_llm else None
    if narrative:
        doc = render(primary, reason, group, events, acts, les, n, rem, pages, narrative, extra)
    return {"pm_key": key, "incident_id": primary["id"], "incident_ids": [i["id"] for i in group],
            "recurrence_key": primary.get("recurrence_key"), "trigger_reason": reason, "severity": sev,
            "title": doc.splitlines()[0].lstrip("# "), "doc": doc, "narrative": narrative, "actions": acts,
            "extra": extra, "pages": pages}


def live_key_for(cur, recurrence_key: str | None) -> str | None:
    """The open (draft/reviewed) postmortem of this key, if any — one live document per problem (#40)."""
    if not recurrence_key:
        return None
    cur.execute("SELECT pm_key FROM postmortems WHERE recurrence_key=%s AND status IN %s "
                "ORDER BY created_at DESC LIMIT 1", (recurrence_key, LIVE))
    r = cur.fetchone()
    return r[0] if r else None


def run(cur, hours: int = 48, dry: bool = False, use_llm: bool = False) -> dict:
    if not dry:
        ensure_schema(cur)
        retire_templated(cur)         # before regenerating, so a retired kind is reused in place
    cands = candidates(cur, hours)
    done_keys, stats = set(), {"candidates": len(cands), "written": 0, "skipped_final": 0, "queued": 0,
                               "linked": 0, "skipped_known_error": 0}
    cur.execute("SELECT to_regclass('postmortems')")
    have = bool(cur.fetchone()[0])
    for inc, reason, n in sorted(cands, key=lambda c: c[0]["opened_at"], reverse=True):
        key = pm_key(inc["id"], reason, inc["recurrence_key"], inc["opened_at"])
        if have and key.startswith("rk:"):
            live = live_key_for(cur, inc["recurrence_key"])
            if live:
                key = live
            else:
                prob = _problem_for(cur, inc["recurrence_key"])
                if prob and prob["state"] in ("known_error", "fix_in_change"):
                    stats["skipped_known_error"] += 1     # the problem record is the follow-up
                    continue
        if key in done_keys:
            continue
        done_keys.add(key)
        existing, prev_actions = None, {}
        if have:
            cur.execute("SELECT id, status FROM postmortems WHERE pm_key=%s", (key,))
            existing = cur.fetchone()
            if existing and existing[1] in ("final", "superseded"):
                stats["skipped_final"] += 1
                continue
            if existing:
                cur.execute("SELECT kind, queue_id, status FROM postmortem_actions WHERE postmortem_id=%s "
                            "AND status <> 'cancelled'", (existing[0],))
                prev_actions = {k: (q, s) for k, q, s in cur.fetchall()}
        pm = build(cur, inc, reason, n, use_llm and not dry, prev_actions, key)
        if dry:
            log(f"would write {key} ({pm['trigger_reason']}, incidents {pm['incident_ids']}): {pm['title']} "
                f"[{len(pm['actions'])} action(s)]")
            stats["written"] += 1
            continue
        x = pm["extra"]
        cur.execute(
            "INSERT INTO postmortems (pm_key, incident_id, incident_ids, recurrence_key, trigger_reason, severity, "
            "title, doc, narrative, problem_id, due_at, contributing_factors, slo_budget_consumed, detect_s, "
            "detection_halved, near_miss) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s) "
            "ON CONFLICT (pm_key) DO UPDATE SET incident_id=EXCLUDED.incident_id, incident_ids=EXCLUDED.incident_ids, "
            "trigger_reason=EXCLUDED.trigger_reason, severity=EXCLUDED.severity, title=EXCLUDED.title, "
            "doc=EXCLUDED.doc, narrative=COALESCE(EXCLUDED.narrative, postmortems.narrative), "
            "problem_id=EXCLUDED.problem_id, due_at=COALESCE(postmortems.due_at, EXCLUDED.due_at), "
            "contributing_factors=EXCLUDED.contributing_factors, slo_budget_consumed=EXCLUDED.slo_budget_consumed, "
            "detect_s=EXCLUDED.detect_s, detection_halved=EXCLUDED.detection_halved, near_miss=EXCLUDED.near_miss, "
            "status='draft', updated_at=now() WHERE postmortems.status IN ('draft','reviewed') RETURNING id",
            (pm["pm_key"], pm["incident_id"], pm["incident_ids"], pm["recurrence_key"], pm["trigger_reason"],
             pm["severity"], pm["title"], pm["doc"], pm["narrative"], (x["problem"] or {}).get("id"), x["due_at"],
             json.dumps(x["factors"]), x["budget"], x["detect_s"], x["halved"], x["near_miss"]))
        row = cur.fetchone()
        if not row:
            continue
        pid = row[0]
        for a in pm["actions"]:
            # a kind whose old row was cancelled (e.g. the retired templated triple) is reused in place
            cur.execute("INSERT INTO postmortem_actions (postmortem_id, kind, owner, description, queue_desc) "
                        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (postmortem_id, kind) DO UPDATE SET "
                        "description=EXCLUDED.description, queue_desc=EXCLUDED.queue_desc, status='open', "
                        "queue_id=NULL, done_at=NULL, close_reason=NULL WHERE postmortem_actions.status='cancelled'",
                        (pid, a["kind"], a["owner"], a["description"], a["queue_desc"]))
        stats["written"] += 1
    if not dry:
        stats.update(file_pending(cur))
    log(f"run: {stats}")
    return stats


def file_pending(cur, cap: int = MAX_FILE) -> dict:
    """File actions that have no queue item yet (oldest first, capped); linking an open one is free."""
    cur.execute("SELECT a.id, a.kind, a.owner, a.queue_desc, p.title, coalesce(p.recurrence_key, 'incident #' || "
                "p.incident_id) FROM postmortem_actions a JOIN postmortems p ON p.id = a.postmortem_id "
                "WHERE a.queue_id IS NULL AND a.status='open' ORDER BY a.id")
    queued = linked = 0
    for aid, kind, owner, qd, title, subj in cur.fetchall():
        cur.execute("SELECT id FROM claude_queue WHERE description=%s AND status IN %s LIMIT 1", (qd, QUEUE_OPEN))
        r = cur.fetchone()
        if not r:
            cur.execute("SELECT id FROM claude_queue WHERE description LIKE %s AND status IN %s LIMIT 1",
                        (f"Postmortem action [%] {subj}:%", QUEUE_OPEN))
            r = cur.fetchone()
        if r:
            qid = r[0]
            linked += 1
        elif queued >= cap:
            continue
        else:
            qid = file_action(cur, title, {"kind": kind, "owner": owner, "queue_desc": qd, "subject": subj})
            queued += 1
        cur.execute("UPDATE postmortem_actions SET queue_id=%s WHERE id=%s", (qid, aid))
    return {"queued": queued, "linked": linked}


def retire_templated(cur, dry: bool = False) -> dict:
    """One-time cleanup (#40): cancel the templated triple with a recorded reason, close their queue
    items as superseded, and fold duplicate live postmortems of one key into the newest. Idempotent."""
    cur.execute("SELECT a.id, a.queue_id, a.description FROM postmortem_actions a WHERE a.status='open'")
    rows = [r for r in cur.fetchall() if is_templated(r[2])]
    q_ids = sorted({r[1] for r in rows if r[1]})
    sup = 0
    if not dry:
        for aid, _q, _d in rows:
            cur.execute("UPDATE postmortem_actions SET status='cancelled', done_at=now(), close_reason=%s "
                        "WHERE id=%s AND status='open'", (RETIRED_REASON, aid))
        if q_ids:
            cur.execute("UPDATE claude_queue SET status='superseded', updated_at=now(), outcome=%s "
                        "WHERE id = ANY(%s) AND status IN %s", (RETIRED_REASON, q_ids, QUEUE_OPEN))
        cur.execute("WITH ranked AS (SELECT id, row_number() OVER (PARTITION BY recurrence_key ORDER BY created_at DESC) rn "
                    "FROM postmortems WHERE recurrence_key IS NOT NULL AND status IN %s) "
                    "UPDATE postmortems p SET status='superseded', updated_at=now(), "
                    "review_notes='superseded: one live postmortem per problem key (#40)' "
                    "FROM ranked WHERE ranked.id=p.id AND ranked.rn > 1", (LIVE,))
        sup = cur.rowcount or 0
    return {"actions_retired": len(rows), "queue_superseded": len(q_ids), "postmortems_superseded": sup}


def sync_status(queue_status: str | None) -> str | None:
    """claude_queue status -> postmortem_actions status, or None to leave open. Pure."""
    if queue_status in QUEUE_DONE:
        return "done"
    if queue_status in QUEUE_CANCELLED:
        return "cancelled"
    return None


def review(cur, dry: bool = False, now=None) -> dict:
    """Nova's own review pass (#40): draft -> reviewed when review_gaps() is empty, or when overdue
    (accepted with the gaps written down). Never waits on Jordan."""
    now = now or datetime.now(timezone.utc)
    cur.execute("SELECT p.id, p.contributing_factors, p.slo_budget_consumed, p.near_miss, p.detect_s, p.trigger_reason, "
                "p.problem_id, p.due_at, (SELECT state FROM problems pr WHERE pr.id=p.problem_id), "
                "p.incident_ids FROM postmortems p WHERE p.status='draft'")
    rows = cur.fetchall()
    out = {"reviewed": 0, "overdue_accepted": 0, "held": 0}
    for pid, factors, budget, nm, detect_s, reason, prob_id, due, prob_state, inc_ids in rows:
        cur.execute("SELECT kind, description FROM postmortem_actions WHERE postmortem_id=%s AND status<>'cancelled'",
                    (pid,))
        acts = [{"kind": k, "description": d} for k, d in cur.fetchall()]
        cur.execute("SELECT count(*) FROM telemetry.events WHERE incident_id = ANY(%s) AND channel IS NOT NULL "
                    "AND channel NOT LIKE 'triage%%' AND channel NOT LIKE 'detector%%'", (list(inc_ids or []),))
        pages = (cur.fetchone() or [0])[0]
        gaps = review_gaps({"contributing_factors": factors if isinstance(factors, list) else json.loads(factors or "[]"),
                            "slo_budget_consumed": budget, "near_miss": nm, "detect_s": detect_s, "pages": pages,
                            "actions": acts, "recurring": "recurring" in (reason or ""), "problem_id": prob_id,
                            "problem_state": prob_state})
        overdue = due is not None and now > due
        if gaps and not overdue:
            out["held"] += 1
            note = "Nova review: held — " + "; ".join(gaps)
            if not dry:
                cur.execute("UPDATE postmortems SET review_notes=%s WHERE id=%s", (note[:1000], pid))
            continue
        note = ("Nova review: passed all gates" if not gaps
                else "Nova review: accepted overdue with gaps — " + "; ".join(gaps))
        out["overdue_accepted" if gaps else "reviewed"] += 1
        if not dry:
            cur.execute("UPDATE postmortems SET status='reviewed', reviewed_at=now(), review_notes=%s, "
                        "doc=replace(doc, '**Status:** draft', '**Status:** reviewed') WHERE id=%s AND status='draft'",
                        (note[:1000], pid))
    log(f"review: {out}")
    return out


def sync(cur, dry: bool = False) -> dict:
    """Close actions whose queue item is done; review drafts; finalize reviewed postmortems with no
    open actions."""
    cur.execute("SELECT to_regclass('postmortems')")
    if not cur.fetchone()[0]:
        return {"closed": 0, "finalized": 0}
    if not dry:
        ensure_schema(cur)
        retire_templated(cur)
    cur.execute("SELECT a.id, q.status FROM postmortem_actions a JOIN claude_queue q ON q.id = a.queue_id "
                "WHERE a.status='open'")
    closed = 0
    for aid, qs in cur.fetchall():
        st = sync_status(qs)
        if st:
            closed += 1
            if not dry:
                cur.execute("UPDATE postmortem_actions SET status=%s, done_at=now(), "
                            "close_reason=coalesce(close_reason, 'queue item ' || %s) WHERE id=%s AND status='open'",
                            (st, qs, aid))
    if not dry:
        review(cur)
    fin = 0
    if not dry:
        cur.execute("UPDATE postmortems p SET status='final', finalized_at=now(), "
                    "doc=replace(doc, '**Status:** reviewed', '**Status:** final') WHERE status='reviewed' "
                    "AND NOT EXISTS (SELECT 1 FROM postmortem_actions a WHERE a.postmortem_id=p.id AND a.status='open')")
        fin = cur.rowcount or 0
    log(f"sync: {closed} action(s) closed, {fin} postmortem(s) finalized")
    return {"closed": closed, "finalized": fin}


def selftest() -> int:
    t = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)
    assert trigger_reason("critical", "probe", 1, 3) == "critical"
    assert trigger_reason("warning", "probe", 3, 3) == "recurring"
    assert trigger_reason("critical", "incident_escalation", 9, 3) is None
    assert trigger_reason("warning", "probe", 2, 3) is None
    assert pm_key(7, "recurring", "h:probe", t) == "rk:h:probe:2026-W42"
    assert pm_key(7, "critical", "h:probe", t) == "inc:7"
    inc = {"id": 7, "opened_at": t, "resolved_at": t, "acked_at": None, "severity": "critical", "host": "h",
           "title": "x down", "summary": "s", "member_count": 2, "recurrence_key": "h:probe", "mtta_s": None,
           "mttr_s": 600, "resolution": None, "root_cat": "probe", "root_title": "x", "root_body": None,
           "first_source": "prober", "first_ts": t, "first_level": "critical"}
    acts = specific_actions(inc, {"problem": {"id": 1, "key": "h:probe", "state": "candidate"}})
    assert len(acts) == 1 and not is_templated(acts[0]["description"])
    assert specific_actions(inc, {}) == []
    doc = render(inc, "critical+recurring", [inc], [], acts, lessons(inc, 4, [], 1), 4, [], 1,
                 extra={"factors": ["x"], "budget": 0.02, "detect_s": 60, "halved": None, "near_miss": False,
                        "due_at": due_at(t, "critical"), "problem": {"id": 1, "state": "candidate"}})
    for h in ("## Summary", "## Impact", "## Root causes", "## Trigger", "## Detection", "## Contributing factors",
              "## Resolution", "## Action items", "### What went well", "### What went wrong",
              "### Where we got lucky", "## Timeline", "**Due:** 2026-10-16", "SLO error budget consumed: 2.0%"):
        assert h in doc, h
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="write/refresh postmortems and file action items")
    ap.add_argument("--sync", action="store_true", help="close actions whose queue item is done; finalize")
    ap.add_argument("--review", action="store_true", help="Nova's review pass only (draft -> reviewed)")
    ap.add_argument("--show", type=int, metavar="ID", help="print one postmortem")
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--llm", action="store_true", help="add a local-LLM narrative (fails open)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not (a.run or a.sync or a.show or a.review):
        ap.print_help()
        return 0
    import psycopg2
    import nova_dsn
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"), connect_timeout=8)
    try:
        cur = conn.cursor()
        if a.show:
            cur.execute("SELECT doc FROM postmortems WHERE id=%s", (a.show,))
            r = cur.fetchone()
            print(r[0] if r else "no such postmortem")
            return 0
        if a.run:
            run(cur, a.hours, a.dry_run, a.llm)
        if a.sync:
            sync(cur, a.dry_run)
        if a.review and not a.sync:
            review(cur, a.dry_run)
        if a.dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
