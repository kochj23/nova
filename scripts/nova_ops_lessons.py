#!/usr/bin/env python3
"""
nova_ops_lessons.py — Nova learns from her OWN operations, not only from what she reads (2026-10-17).

Before this organ nothing turned her operations into beliefs: the work reader (nova_work_lessons.py) excludes
alerts, streams and her own outputs by design; hotwash writes AARs that only feed weekly co-agency proposals;
postmortems and the weekly retro write documents and claude_queue items; lessons_daily only republishes the work
reader's lessons. So on a day with 100+ AARs, dozens of recurring incidents and decided proposals, she drew zero
lessons from any of it.

Every run (scheduler.yaml task ops_lessons, every 2h) reads the window since the watermark:
  hotwash      after-action reviews (q2 actual / q3 why / q4 fix), grouped by kind, heaviest first
  postmortems  SRE postmortems written or updated (title, narrative, contributing factors)
  incidents    telemetry.incidents recurrence keys that opened >= 3 times in 7 days
  scheduler    tasks that failed >= 2 times (and >= 10% of runs) with their error tails
  claude_queue items finished done/cancelled with an outcome (what was decided and why)
  coagency     her proposals decided (approved/rejected/dropped) with the decision note
  gateway      chat turns that fell to the rescue path or the canned failure reply
Each group goes to a LOCAL model (Ollama qwen3:30b-a3b, think off, format json) which may state up to two general,
durable lessons about how her systems behave, each citing the facts [R n] it rests on, optionally the ONE part of
her (self-map key) the lesson argues should change. Validated lessons feed:
  (a) nova_beliefs.add_candidate(origin='ops', credibility='observed') — the belief promoter ranks, dedupes,
      calibrates and books them (a lesson she keeps re-learning reinforces one belief);
  (b) growth_signals (work_key 'ops:<group>:<window hour>') — 'change' when it argues a change to a real self-map
      key, so nova_growth_loop.py can earn a practice when three separate occasions push the same part of her.
Ledger: nova_ops.ops_lessons (one row per lesson; audit + retry when the belief module is unavailable).
Watermark: service_config ops_lessons/watermark. Kill switch respected; every write fails open.
CLI: --hours N (first-run window), --dry-run, --max-groups N, --selftest.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

SERVICE = "ops_lessons"
MODEL = os.environ.get("NOVA_OPS_LESSONS_MODEL", "qwen3:30b-a3b")
OLLAMA_URLS = [u for u in os.environ.get("NOVA_OPS_LESSONS_OLLAMA",
                                         "http://127.0.0.1:11436,http://127.0.0.1:11434").split(",") if u]
FIRST_HOURS = 24
MAX_WINDOW_H = 72          # a long outage does not turn into one giant prompt
PER_GROUP = 2              # lessons per group
MAX_FACTS = 10             # facts per group
MAX_LESSONS_PER_RUN = 16
GROUP_ORDER = ("coagency", "claude_queue", "scheduler", "gateway", "incidents", "postmortem")  # small, decisive sources first; hotwash after
SELF_PREFIXES = ("organ:", "value:", "rule:", "problem:")
SKIP_CHANNELS = ("hc", "test", "bench", "verify", "unknown")

_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_IDISH = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}|#\d{2,}|\b[0-9a-f]{12,}\b", re.I)


def log(m):
    print(f"[ops-lessons {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── pure helpers ──────────────────────────────────────────────────────────────

def clip(s, n=220) -> str:
    return " ".join(str(s or "").split())[:n]


def window_tag(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def lesson_problem(text: str) -> str | None:
    """Why a lesson is unusable, or None. A lesson is ONE general, durable sentence. Pure."""
    t = (text or "").strip()
    words = t.split()
    if len(words) < 8:
        return "too short"
    if len(words) > 40:
        return "too long"
    if "?" in t:
        return "a question"
    if _IP.search(t) or _IDISH.search(t):
        return "names an address or id (not durable)"
    if re.match(r"(?i)^(i|we|my|our)\b", t):
        return "first-person diary, not a lesson"
    if re.search(r"(?i)\b(today|yesterday|this week|last night|this morning)\b", t):
        return "tied to one day"
    return None


def tidy(text: str) -> str:
    t = re.sub(r"\[R?\d+\]", "", str(text or ""))
    t = re.sub(r"\s+([,.;:!?])", r"\1", " ".join(t.split())).strip()
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    if t and t[-1] not in ".!":
        t += "."
    return t


def resolve_refs(cited, facts: list[tuple[str, str]]) -> list[str]:
    """['R1', 3, 'R9'] -> the evidence refs of the facts that exist. Pure."""
    out = []
    for c in cited if isinstance(cited, list) else [cited]:
        m = re.search(r"\d+", str(c))
        if m and 1 <= int(m.group(0)) <= len(facts):
            ref = facts[int(m.group(0)) - 1][0]
            if ref not in out:
                out.append(ref)
    return out


def resolve_key(key, keys: set[str]) -> str | None:
    k = str(key or "").strip().strip("'\"`")
    if not k or k.lower() in ("null", "none"):
        return None
    if k in keys:
        return k
    hits = [x for x in keys if x.split(":", 1)[-1] == k]
    return hits[0] if len(hits) == 1 else None


def parse_lessons(raw: str, facts: list[tuple[str, str]], keys: set[str], cap: int = PER_GROUP) -> list[dict]:
    """Model JSON -> validated lessons [{lesson, topic, evidence, ref, change}]. Pure."""
    t = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    try:
        obj = json.loads(t[t.find("{"):t.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return []
    out = []
    for x in (obj.get("lessons") or [])[:cap * 2]:
        if not isinstance(x, dict):
            continue
        lesson = tidy(x.get("lesson"))
        if lesson_problem(lesson):
            continue
        ev = resolve_refs(x.get("refs") or x.get("evidence") or [], facts)
        if not ev:
            continue
        ref = resolve_key(x.get("self_key") or x.get("ref"), keys)
        change = clip(x.get("change"), 320) if ref else ""
        if change.lower() in ("", "null", "none"):
            change = ""
        out.append({"lesson": lesson, "topic": clip(x.get("topic"), 60).lower() or None, "evidence": ev,
                    "ref": ref, "change": change})
        if len(out) >= cap:
            break
    return out


def signal_kind(ref: str | None, change: str) -> str | None:
    """Ops signals use the growth loop's own classifier (red lines are always 'keep')."""
    if not ref or not change:
        return None
    try:
        from nova_growth_loop import classify
        return classify(ref, change)
    except Exception:  # noqa: BLE001
        return None


def lesson_prompt(group: dict, selfmap: list[tuple[str, str]]) -> str:
    facts = "\n".join(f"[R{i}] {txt}" for i, (_, txt) in enumerate(group["facts"], 1))
    me = "\n".join(f"{k} — {d}" for k, d in selfmap if k.startswith(SELF_PREFIXES))
    return (
        "You are Nova, a home-and-infrastructure AI, reviewing what actually happened in your OWN operations "
        f"({group['title']}). Facts from your logs:\n{facts}\n\n"
        "What do these facts teach about how your systems, alerts, jobs or decisions really behave? State at most "
        f"{PER_GROUP} lessons. Each lesson is ONE grammatical sentence in plain present tense, general and durable "
        "(still true and useful next month), about a mechanism — e.g. 'Suppressing a detector that is usually right "
        "hides real failures until they recur.' No IP addresses, hostnames, ids, dates or counts; no first person; "
        "not a restatement of a single event; not a platitude. Cite the facts it rests on as refs ['R1', ...]. "
        "If the lesson argues that ONE part of you below should change, give its exact key as self_key and one "
        "sentence change: 'so my <part> should <do X>'. Otherwise self_key null. If the facts teach nothing "
        "general, return no lessons — that is a fine answer.\n\n"
        f"Your parts (key — what it is):\n{me}\n\n"
        'Output ONLY JSON: {"lessons": [{"lesson": "...", "topic": "<3-5 words>", "refs": ["R1"], '
        '"self_key": "<key or null>", "change": "<so my ... should ... or null>"}]}')


# ── gathering (each source fails open) ────────────────────────────────────────

def _safe(oc, sql, params=()):
    try:
        oc.execute(sql, params)
        return oc.fetchall() or []
    except Exception as e:  # noqa: BLE001
        log(f"source unreadable ({str(e).splitlines()[0][:120]})")
        try:
            oc.connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        return []


def gather(oc, since: datetime, until: datetime) -> list[dict]:
    groups = []
    rows = _safe(oc, "SELECT id, kind, subject, q2_actual, q3_why, q4_fix, weight FROM hotwash "
                     "WHERE ts > %s AND ts <= %s ORDER BY weight DESC, ts DESC LIMIT 80", (since, until))
    by_kind = {}
    for hid, kind, subj, q2, q3, q4, w in rows:
        by_kind.setdefault(kind, []).append(
            (f"hotwash:{hid}", f"{kind} ({subj}, weight {w}): {clip(q2, 160)} WHY: {clip(q3, 200)} FIX: {clip(q4, 120)}"))
    for kind, facts in by_kind.items():
        groups.append({"key": f"hotwash_{kind}", "title": f"after-action reviews: {kind.replace('_', ' ')}",
                       "facts": facts[:MAX_FACTS]})
    rows = _safe(oc, "SELECT id, title, coalesce(narrative,''), contributing_factors::text, trigger_reason "
                     "FROM postmortems WHERE updated_at > %s AND updated_at <= %s ORDER BY updated_at DESC LIMIT %s",
                 (since, until, MAX_FACTS))
    if rows:
        groups.append({"key": "postmortem", "title": "postmortems", "facts": [
            (f"postmortem:{i}", f"{clip(t, 120)} ({clip(tr, 60)}): {clip(n, 260)} factors: {clip(cf, 160)}")
            for i, t, n, cf, tr in rows]})
    rows = _safe(oc, "SELECT recurrence_key, count(*), max(id), (array_agg(title ORDER BY id DESC))[1], "
                     "(array_agg(coalesce(summary,'') ORDER BY id DESC))[1], "
                     "(array_agg(coalesce(resolution,'') ORDER BY id DESC))[1] "
                     "FROM telemetry.incidents WHERE opened_at > %s - interval '7 days' AND opened_at <= %s "
                     "AND recurrence_key IS NOT NULL GROUP BY 1 "
                     "HAVING count(*) >= 3 AND max(opened_at) > %s ORDER BY 2 DESC LIMIT %s",
                 (until, until, since, MAX_FACTS))
    if rows:
        groups.append({"key": "incidents", "title": "recurring incidents", "facts": [
            (f"incident:{mid}", f"'{clip(t, 100)}' opened {n} times in 7 days. Correlator: {clip(s, 200)} "
                                f"Resolution: {clip(r, 100) or 'none recorded'}") for _, n, mid, t, s, r in rows]})
    ms = (int(since.timestamp() * 1000), int(until.timestamp() * 1000))
    rows = _safe(oc, "SELECT task_id, count(*) FILTER (WHERE status <> 'success'), count(*), "
                     "(array_agg(coalesce(error_tail, stdout_tail, '') ORDER BY started_at DESC) "
                     " FILTER (WHERE status <> 'success'))[1] "
                     "FROM scheduler_runs WHERE started_at > %s AND started_at <= %s AND status <> 'running' "
                     "GROUP BY 1 HAVING count(*) FILTER (WHERE status <> 'success') >= 2 "
                     "AND count(*) FILTER (WHERE status <> 'success') >= 0.1 * count(*) ORDER BY 2 DESC LIMIT %s",
                 (*ms, MAX_FACTS))
    if rows:
        groups.append({"key": "scheduler", "title": "failing scheduled jobs", "facts": [
            (f"scheduler:{t}", f"job {t} failed {f} of {n} runs; last error: {clip((e or '')[-300:], 220)}")
            for t, f, n, e in rows]})
    rows = _safe(oc, "SELECT id, status, description, outcome FROM claude_queue WHERE completed_at > %s "
                     "AND completed_at <= %s AND status IN ('done','cancelled') AND length(coalesce(outcome,'')) > 40 "
                     "ORDER BY completed_at DESC LIMIT %s", (since, until, MAX_FACTS))
    if rows:
        groups.append({"key": "claude_queue", "title": "work items finished on my behalf", "facts": [
            (f"claude_queue:{i}", f"[{s}] {clip(d, 140)} -> {clip(o, 240)}") for i, s, d, o in rows]})
    rows = _safe(oc, "SELECT id, status, origin, proposed_action, coalesce(decision_note,''), coalesce(outcome,'') "
                     "FROM coagency_proposals WHERE decided_at > %s AND decided_at <= %s "
                     "AND status IN ('approved','rejected','dropped','executed','refused','acknowledged') "
                     "ORDER BY decided_at DESC LIMIT %s", (since, until, MAX_FACTS))
    if rows:
        groups.append({"key": "coagency", "title": "decisions on my own proposals", "facts": [
            (f"coagency:{i}", f"[{s}] ({o}) {clip(a, 140)} — decision: {clip(n, 260)} {clip(oc_, 80)}")
            for i, s, o, a, n, oc_ in rows]})
    rows = _safe(oc, "SELECT trace_id, channel, coalesce(backend_used,''), left(coalesce(user_message,''),120), "
                     "left(coalesce(response,''),160) FROM gateway_traces WHERE created_at > %s AND created_at <= %s "
                     "AND NOT (channel = ANY(%s)) AND (backend_used ILIKE '%%rescue%%' OR response IS NULL "
                     "OR response ILIKE 'Something went wrong%%') ORDER BY created_at DESC LIMIT %s",
                 (since, until, list(SKIP_CHANNELS), MAX_FACTS))
    if len(rows) >= 2:
        groups.append({"key": "gateway", "title": "chat turns that failed or fell to the rescue path", "facts": [
            (f"gateway_trace:{t}", f"{c} via {b or 'none'}: asked '{clip(u, 100)}' -> '{clip(r, 120) or '(no reply)'}'")
            for t, c, b, u, r in rows]})
    return groups


# ── io ────────────────────────────────────────────────────────────────────────

def chat(prompt: str, max_tokens: int = 900) -> str:
    body = json.dumps({"model": MODEL, "stream": False, "think": False, "keep_alive": "30m", "format": "json",
                       "options": {"temperature": 0.2, "num_predict": max_tokens, "num_ctx": 16384},
                       "messages": [{"role": "user", "content": prompt + "\n/no_think"}]}).encode()
    for base in OLLAMA_URLS:
        try:
            req = urllib.request.Request(base.rstrip("/") + "/api/chat", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=240) as r:
                out = json.load(r).get("message", {}).get("content", "")
            if out:
                return out
        except Exception as e:  # noqa: BLE001
            log(f"{base} failed ({e})")
    return ""


def ensure_schema(oc):
    oc.execute("""CREATE TABLE IF NOT EXISTS ops_lessons (
        id serial PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(), window_end timestamptz NOT NULL,
        group_key text NOT NULL, lesson text NOT NULL, topic text, evidence text[] NOT NULL,
        ref text, change text, signal_kind text, candidate_id int, candidate_status text,
        UNIQUE (group_key, window_end, lesson))""")


def cfg_get(oc, key):
    oc.execute("SELECT value #>> '{}' FROM service_config WHERE service=%s AND key=%s", (SERVICE, key))
    r = oc.fetchone()
    return r[0] if r else None


def cfg_set(oc, key, val: str):
    oc.execute("INSERT INTO service_config (service, key, value, updated_by) VALUES (%s,%s,to_jsonb(%s::text),"
               "'nova_ops_lessons') ON CONFLICT (service, key) DO UPDATE SET value=EXCLUDED.value, updated_at=now(), "
               "updated_by=EXCLUDED.updated_by", (SERVICE, key, val))


def to_belief(oc, row_id: int, group: dict, les: dict, window_end: datetime) -> tuple:
    try:
        import nova_beliefs
    except Exception as e:  # noqa: BLE001
        return None, f"beliefs_unavailable ({str(e)[:80]})"
    try:
        cid, status = nova_beliefs.add_candidate(
            oc, "ops", f"ops:{group['key']}:{window_tag(window_end)}", group["title"], les["lesson"],
            les["evidence"], topic=les.get("topic"), credibility="observed", fit=2 if les.get("ref") else 1,
            kind="ops")
        return cid, status
    except Exception as e:  # noqa: BLE001
        return None, f"error: {str(e)[:120]}"


def retry_unbooked(oc) -> int:
    """Lessons stored while the belief module was missing get one more try per run."""
    oc.execute("SELECT id, group_key, lesson, topic, evidence, ref, window_end FROM ops_lessons "
               "WHERE candidate_id IS NULL AND candidate_status LIKE 'beliefs_unavailable%%' "
               "AND created_at > now() - interval '3 days' ORDER BY id LIMIT 30")
    n = 0
    for rid, gk, lesson, topic, ev, ref, we in oc.fetchall():
        cid, st = to_belief(oc, rid, {"key": gk, "title": gk.replace("_", " ")},
                            {"lesson": lesson, "topic": topic, "evidence": list(ev), "ref": ref}, we)
        if st and not st.startswith("beliefs_unavailable"):
            oc.execute("UPDATE ops_lessons SET candidate_id=%s, candidate_status=%s WHERE id=%s", (cid, st, rid))
            n += 1
    return n


def run(hours: int = FIRST_HOURS, dry: bool = False, max_groups: int | None = None) -> int:
    import psycopg2
    import nova_dsn
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"))
    conn.autocommit = True
    oc = conn.cursor()
    try:
        import nova_autonomy_safety
        if nova_autonomy_safety.kill_switch_engaged(oc):
            log("kill switch engaged — standing down")
            return 0
    except Exception as e:  # noqa: BLE001
        log(f"kill switch unreadable ({e}) — standing down")
        return 0
    if (cfg_get(oc, "enabled") or "true").lower() == "false":
        log("service_config ops_lessons/enabled=false — off")
        return 0
    ensure_schema(oc)
    until = datetime.now(timezone.utc)
    wm = cfg_get(oc, "watermark")
    since = datetime.fromisoformat(wm) if wm else until - timedelta(hours=hours)
    since = max(since, until - timedelta(hours=MAX_WINDOW_H))
    if not dry:
        retried = retry_unbooked(oc)
        if retried:
            log(f"re-fed {retried} stored lesson(s) to the belief promoter")
    groups = gather(oc, since, until)
    groups.sort(key=lambda g: GROUP_ORDER.index(g["key"]) if g["key"] in GROUP_ORDER else len(GROUP_ORDER))
    if max_groups:
        groups = groups[:max_groups]
    log(f"window {since:%m-%d %H:%M} -> {until:%m-%d %H:%M} UTC: {len(groups)} group(s): "
        + ", ".join(f"{g['key']}({len(g['facts'])})" for g in groups))
    import nova_work_lessons as wl
    smap = wl.self_map(oc)
    keys = {k for k, _ in smap if k.startswith(SELF_PREFIXES)}
    made = 0
    for g in groups:
        if made >= MAX_LESSONS_PER_RUN:
            break
        lessons = parse_lessons(chat(lesson_prompt(g, smap)), g["facts"], keys)
        if not lessons:
            log(f"{g['key']}: nothing general to learn")
            continue
        for les in lessons[:MAX_LESSONS_PER_RUN - made]:
            kind = signal_kind(les["ref"], les["change"])
            log(f"{g['key']}: {les['lesson']}  [{', '.join(les['evidence'][:3])}]"
                + (f"  -> {les['ref']} ({kind}): {les['change']}" if les["ref"] else ""))
            made += 1
            if dry:
                continue
            oc.execute("INSERT INTO ops_lessons (window_end, group_key, lesson, topic, evidence, ref, change, "
                       "signal_kind) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id",
                       (until, g["key"], les["lesson"], les["topic"], les["evidence"], les["ref"],
                        les["change"] or None, kind))
            r = oc.fetchone()
            if not r:
                continue
            cid, st = to_belief(oc, r[0], g, les, until)
            oc.execute("UPDATE ops_lessons SET candidate_id=%s, candidate_status=%s WHERE id=%s", (cid, st, r[0]))
            if kind:
                try:
                    oc.execute("INSERT INTO growth_signals (work_key, ref, kind, meaning, title) VALUES "
                               "(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                               (f"ops:{g['key']}:{window_tag(until)}", les["ref"], kind, les["change"][:320],
                                f"my operations: {g['title']}"))
                except Exception as e:  # noqa: BLE001
                    log(f"growth signal not written ({e})")
    if not dry:
        cfg_set(oc, "watermark", until.isoformat())
    log(f"ops lessons: {made} lesson(s) from {len(groups)} group(s){' (dry run)' if dry else ''}")
    return 0


def selftest():
    assert lesson_problem("Suppressing a detector that is usually right hides real failures until they recur.") is None
    assert lesson_problem("Too short.") == "too short"
    assert lesson_problem("The probe on 192.168.1.2 fails because the router cannot reach the node.").startswith("names")
    assert lesson_problem("I should restart the poller whenever the GPS tracker goes quiet for a day.").startswith("first")
    assert lesson_problem("Why do jobs that fail twice in a row usually fail a third time as well?") == "a question"
    assert tidy("alerts that page twice lose trust [R1] ") == "Alerts that page twice lose trust."
    facts = [("hotwash:1", "a"), ("hotwash:2", "b")]
    assert resolve_refs(["R2", "R9", 1, "R2"], facts) == ["hotwash:2", "hotwash:1"]
    keys = {"organ:prober", "organ:hold", "value:cost-conscious"}
    assert resolve_key("prober", keys) == "organ:prober" and resolve_key("null", keys) is None
    raw = json.dumps({"lessons": [
        {"lesson": "A job that fails on every run is noise until someone owns its fix", "topic": "Chronic failures",
         "refs": ["R1"], "self_key": "organ:hold", "change": "so my hold should page the owner of chronic jobs"},
        {"lesson": "Bad", "refs": ["R1"]},
        {"lesson": "Retries without a cause analysis repeat the same failure on the next run as well.", "refs": []}]})
    out = parse_lessons(raw, facts, keys)
    assert len(out) == 1 and out[0]["ref"] == "organ:hold" and out[0]["evidence"] == ["hotwash:1"]
    assert out[0]["lesson"].endswith(".") and out[0]["topic"] == "chronic failures"
    assert parse_lessons("garbage", facts, keys) == []
    assert signal_kind(None, "x") is None
    print("selftest OK")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--hours", type=int, default=FIRST_HOURS)
    ap.add_argument("--max-groups", type=int, default=None)
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
        return 0
    return run(a.hours, a.dry_run, a.max_groups)


if __name__ == "__main__":
    sys.exit(main())
