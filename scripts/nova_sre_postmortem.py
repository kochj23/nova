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

CLI: --run [--hours 48] [--llm] [--dry-run] | --sync [--dry-run] | --show <pm id> | --selftest
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
MAX_FILE = 15
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

def action_items(inc: dict, recur_count: int, runbook: list | None) -> list:
    """Three items (prevent / mitigate / detect), worded per subject so the same item recurs with the
    same text (the queue dedups on it). Pure."""
    subj = subject_of(inc)
    host, cat = inc.get("host") or "unknown", inc.get("root_cat") or "uncategorized"
    if recur_count > 1:
        prevent = (f"Find and fix the underlying cause of {subj} (it keeps reopening); the fix is a code, config "
                   f"or capacity change, not another restart or auto-close.")
        detect = (f"Alert on {subj} as one tracked problem (one ticket per key) instead of a fresh incident per "
                  f"occurrence, and add an SLO/burn-rate signal for it so slow degradation is seen before it pages.")
    else:
        prevent = (f"Confirm the root cause of {subj} ({_clip(inc.get('title'), 80)}) and land a fix or guard "
                   f"so this failure mode cannot recur.")
        detect = (f"Check that {host}/{cat} was detected at the earliest point and at the right severity; add "
                  f"a probe or SLO signal if it was found late or by a downstream symptom.")
    if runbook:
        mitigate = (f"Verify the existing runbook for {host}/{cat} ({', '.join(runbook)}) ran and helped; make it "
                    f"execute automatically if it only proposed.")
    else:
        mitigate = (f"Add a remediation runbook for {host}/{cat} to nova_remediation.RUNBOOKS so recovery is "
                    f"automatic and measured.")
    return [{"kind": "prevent", "owner": OWNER, "description": prevent},
            {"kind": "mitigate", "owner": OWNER, "description": mitigate},
            {"kind": "detect", "owner": OWNER, "description": detect}]


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
           recur_count: int, remediations: list, pages: int, narrative: str | None = None) -> str:
    """Markdown postmortem in the SRE-book template. `inc` is the primary (latest) incident. Pure."""
    subj = subject_of(inc)
    n = len(incidents)
    hosts = sorted({i.get("host") or "unknown" for i in incidents})
    total_mttr = sum((i.get("mttr_s") or 0) for i in incidents)
    title = (f"Postmortem: {subj} ({n} incidents, week {week_key(inc['opened_at'])})" if n > 1
             else f"Postmortem: {_clip(inc.get('title'), 120)}")
    L = [f"# {title}", "",
         f"**Status:** draft · **Trigger:** {reason} · **Incidents:** "
         + ", ".join(f"#{i['id']}" for i in incidents) + " · **Blameless:** yes", ""]
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
          + (f"Acknowledged after {_mins(inc.get('mtta_s'))}." if inc.get("acked_at") else "Never acknowledged."),
          "", "## Resolution"]
    if inc.get("resolution"):
        L.append(_clip(inc["resolution"], 400))
    elif remediations:
        L.append("; ".join(f"{r['action']} {r['status']}" for r in remediations[:6]) + ".")
    else:
        L.append("Auto-closed by nova_incident_lifecycle after its events went quiet; no fix was recorded.")
    L += ["", "## Action items", "| Kind | Owner | Action | Tracking |", "|---|---|---|---|"]
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
    """All resolved incidents covered by this pm_key, oldest first."""
    if key.startswith("rk:"):
        wk = week_key(inc["opened_at"])
        y, w = int(wk[:4]), int(wk[-2:])
        cur.execute(INC_SQL + "WHERE i.recurrence_key=%s AND i.status='resolved' "
                    "AND i.opened_at >= to_date(%s, 'IYYY-IW') AND i.opened_at < to_date(%s, 'IYYY-IW') + 7 "
                    "ORDER BY i.opened_at", (inc["recurrence_key"], f"{y}-{w:02d}", f"{y}-{w:02d}"))
        rows = [dict(zip(INC_COLS, r)) for r in cur.fetchall()]
        return rows or [inc]
    return [inc]


def _enrich(cur, incidents: list):
    ids = [i["id"] for i in incidents]
    cur.execute("SELECT ts, level, source, title, channel, incident_id FROM telemetry.events "
                "WHERE incident_id = ANY(%s) ORDER BY ts", (ids,))
    events = [dict(zip(("ts", "level", "source", "title", "channel", "incident_id"), r)) for r in cur.fetchall()]
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
    return sum(1 for e in events if e.get("channel") and not str(e["channel"]).startswith("triage"))


def _ensure_session(cur) -> None:
    cur.execute("INSERT INTO claude_sessions (session_id, status, project) VALUES (%s, 'active', 'nova-sre') "
                "ON CONFLICT (session_id) DO NOTHING", (SESSION,))


def file_action(cur, pm_title: str, a: dict) -> int | None:
    """Link to an open queue item with the same description, else insert one. -> queue id."""
    cur.execute("SELECT id FROM claude_queue WHERE description=%s AND status IN %s ORDER BY id LIMIT 1",
                (a["queue_desc"], QUEUE_OPEN))
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


def build(cur, inc: dict, reason: str, n: int, use_llm: bool = False, existing_actions=None) -> dict:
    key = pm_key(inc["id"], reason, inc["recurrence_key"], inc["opened_at"])
    group = _group_incidents(cur, key, reason, inc)
    primary = group[-1]
    if primary.get("root_cat") is None:
        primary["root_cat"] = inc.get("root_cat")
    events, rem, pages = _enrich(cur, group)
    rb = _runbooks().get((primary.get("host"), primary.get("root_cat")))
    subj = subject_of(primary)
    acts = action_items(primary, n, rb)
    for a in acts:
        a["queue_desc"] = queue_desc(subj, a["kind"], a["description"])
        prev = (existing_actions or {}).get(a["kind"])
        if prev:
            a.update(queue_id=prev[0], status=prev[1])
    les = lessons(primary, n, rem, pages)
    doc = render(primary, reason, group, events, acts, les, n, rem, pages)
    narrative = narrative_llm(doc) if use_llm else None
    if narrative:
        doc = render(primary, reason, group, events, acts, les, n, rem, pages, narrative)
    return {"pm_key": key, "incident_id": primary["id"], "incident_ids": [i["id"] for i in group],
            "recurrence_key": primary.get("recurrence_key"), "trigger_reason": reason,
            "severity": max((i["severity"] for i in group), key=lambda s: s == "critical"),
            "title": doc.splitlines()[0].lstrip("# "), "doc": doc, "narrative": narrative, "actions": acts}


def run(cur, hours: int = 48, dry: bool = False, use_llm: bool = False) -> dict:
    if not dry:
        ensure_schema(cur)
    cands = candidates(cur, hours)
    done_keys, stats = set(), {"candidates": len(cands), "written": 0, "skipped_final": 0, "queued": 0, "linked": 0}
    cur.execute("SELECT to_regclass('postmortems')")
    have = bool(cur.fetchone()[0])
    for inc, reason, n in sorted(cands, key=lambda c: c[0]["opened_at"], reverse=True):
        key = pm_key(inc["id"], reason, inc["recurrence_key"], inc["opened_at"])
        if key in done_keys:
            continue
        done_keys.add(key)
        existing, prev_actions = None, {}
        if have:
            cur.execute("SELECT id, status FROM postmortems WHERE pm_key=%s", (key,))
            existing = cur.fetchone()
            if existing and existing[1] == "final":
                stats["skipped_final"] += 1
                continue
            if existing:
                cur.execute("SELECT kind, queue_id, status FROM postmortem_actions WHERE postmortem_id=%s",
                            (existing[0],))
                prev_actions = {k: (q, s) for k, q, s in cur.fetchall()}
        pm = build(cur, inc, reason, n, use_llm and not dry, prev_actions)
        if dry:
            log(f"would write {key} ({pm['trigger_reason']}, incidents {pm['incident_ids']}): {pm['title']}")
            stats["written"] += 1
            continue
        cur.execute(
            "INSERT INTO postmortems (pm_key, incident_id, incident_ids, recurrence_key, trigger_reason, severity, "
            "title, doc, narrative) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (pm_key) DO UPDATE SET incident_id=EXCLUDED.incident_id, incident_ids=EXCLUDED.incident_ids, "
            "trigger_reason=EXCLUDED.trigger_reason, severity=EXCLUDED.severity, title=EXCLUDED.title, "
            "doc=EXCLUDED.doc, narrative=COALESCE(EXCLUDED.narrative, postmortems.narrative), updated_at=now() "
            "WHERE postmortems.status='draft' RETURNING id",
            (pm["pm_key"], pm["incident_id"], pm["incident_ids"], pm["recurrence_key"], pm["trigger_reason"],
             pm["severity"], pm["title"], pm["doc"], pm["narrative"]))
        row = cur.fetchone()
        if not row:
            continue
        pid = row[0]
        for a in pm["actions"]:
            cur.execute("INSERT INTO postmortem_actions (postmortem_id, kind, owner, description, queue_desc) "
                        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (postmortem_id, kind) DO NOTHING",
                        (pid, a["kind"], a["owner"], a["description"], a["queue_desc"]))
        stats["written"] += 1
    if not dry:
        stats.update(file_pending(cur))
    log(f"run: {stats}")
    return stats


def file_pending(cur, cap: int = MAX_FILE) -> dict:
    """File actions that have no queue item yet (oldest first, capped); linking an open one is free."""
    cur.execute("SELECT a.id, a.kind, a.owner, a.queue_desc, p.title FROM postmortem_actions a "
                "JOIN postmortems p ON p.id = a.postmortem_id WHERE a.queue_id IS NULL AND a.status='open' "
                "ORDER BY a.id")
    queued = linked = 0
    for aid, kind, owner, qd, title in cur.fetchall():
        cur.execute("SELECT id FROM claude_queue WHERE description=%s AND status IN %s LIMIT 1", (qd, QUEUE_OPEN))
        r = cur.fetchone()
        if r:
            qid = r[0]
            linked += 1
        elif queued >= cap:
            continue
        else:
            qid = file_action(cur, title, {"kind": kind, "owner": owner, "queue_desc": qd})
            queued += 1
        cur.execute("UPDATE postmortem_actions SET queue_id=%s WHERE id=%s", (qid, aid))
    return {"queued": queued, "linked": linked}


def sync_status(queue_status: str | None) -> str | None:
    """claude_queue status -> postmortem_actions status, or None to leave open. Pure."""
    if queue_status in QUEUE_DONE:
        return "done"
    if queue_status in QUEUE_CANCELLED:
        return "cancelled"
    return None


def sync(cur, dry: bool = False) -> dict:
    """Close actions whose queue item is done; finalize postmortems with no open actions."""
    cur.execute("SELECT to_regclass('postmortems')")
    if not cur.fetchone()[0]:
        return {"closed": 0, "finalized": 0}
    cur.execute("SELECT a.id, q.status FROM postmortem_actions a JOIN claude_queue q ON q.id = a.queue_id "
                "WHERE a.status='open'")
    closed = 0
    for aid, qs in cur.fetchall():
        st = sync_status(qs)
        if st:
            closed += 1
            if not dry:
                cur.execute("UPDATE postmortem_actions SET status=%s, done_at=now() WHERE id=%s AND status='open'",
                            (st, aid))
    fin = 0
    if not dry:
        cur.execute("UPDATE postmortems p SET status='final', finalized_at=now(), "
                    "doc=replace(doc, '**Status:** draft', '**Status:** final') WHERE status='draft' "
                    "AND EXISTS (SELECT 1 FROM postmortem_actions a WHERE a.postmortem_id=p.id) "
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
    acts = action_items(inc, 4, None)
    doc = render(inc, "critical+recurring", [inc], [], acts, lessons(inc, 4, [], 1), 4, [], 1)
    for h in ("## Summary", "## Impact", "## Root causes", "## Trigger", "## Detection", "## Resolution",
              "## Action items", "### What went well", "### What went wrong", "### Where we got lucky", "## Timeline"):
        assert h in doc, h
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="write/refresh postmortems and file action items")
    ap.add_argument("--sync", action="store_true", help="close actions whose queue item is done; finalize")
    ap.add_argument("--show", type=int, metavar="ID", help="print one postmortem")
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--llm", action="store_true", help="add a local-LLM narrative (fails open)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not (a.run or a.sync or a.show):
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
        if a.dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
