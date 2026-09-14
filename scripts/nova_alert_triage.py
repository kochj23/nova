#!/usr/bin/env python3
"""nova_alert_triage.py — AI triage over Nova's own alert stream.

The .10 false-page (a "critical" alarm for an intentionally-fenced replica) is the
motivating example: rules fire without context. This brain gives an alert context
before it pages — retrieving what similar alerts turned out to be, what maintenance
just happened, and what's known-normal — and returns a decision.

SAFETY (non-negotiable): unlike memory recall, a false negative here is dangerous —
a suppressed real outage is worse than a noisy false one. So:
  * HARD-CRITICAL classes (data loss, corruption, security/intrusion, primary down,
    backup failed) ALWAYS page, regardless of what the model concludes.
  * The model may SUPPRESS/DOWNGRADE only info/warning alerts, only at high
    confidence, only into a known-benign class.
  * Anything the model is unsure about PAGES (annotated). Learning trims noise; it
    never hides a fire.

Every alert is also ANNOTATED with likely cause + similar past incidents, so even a
page arrives smarter. Every decision is logged to nova_ops.alert_triage_log for the
feedback loop to grade later.

Used by the nova_notifier daemon (import triage) and runnable as a CLI for testing:
    nova_alert_triage.py "Database replica down" --level critical --category database
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
LLM_MODEL = "qwen3:8b"
OLLAMA_NODES = ["http://192.168.1.251:11434", "http://192.168.1.86:11434",
                "http://192.168.1.252:11434", "http://192.168.1.7:11434",
                "http://192.168.1.6:11434"]

# Hard-critical signatures — these ALWAYS page. Matched against title+body+category.
_HARD_CRITICAL = re.compile(
    r"data ?loss|corrupt|breach|intrusion|ransomware|exfiltrat|"
    r"primary\b.{0,40}\b(down|unreachable|offline|dead|lost|gone)|"
    r"\b(no|without|missing|zero)\b.{0,25}\b(standby|replica)|"
    r"backup.{0,15}fail|fail.{0,15}backup|split.?brain|"
    r"\ball\b.{0,20}\bdown\b|fleet.{0,10}down|"
    r"unauthorized|compromise|breach|exposed secret|leaked", re.I)

_VERDICTS = {"real_actionable", "known_self_healing", "expected_change",
             "duplicate", "learned_normal"}


def _log(m): print(f"[alert-triage {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, max_tokens=400, temperature=0.1):
    body = json.dumps({"model": LLM_MODEL, "stream": False, "think": False,
                       "options": {"temperature": temperature, "num_predict": max_tokens},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    for node in OLLAMA_NODES:
        try:
            req = urllib.request.Request(node + "/api/chat", method="POST",
                                         headers={"Content-Type": "application/json"}, data=body)
            with urllib.request.urlopen(req, timeout=45) as r:
                out = json.load(r).get("message", {}).get("content", "").strip()
            if out:
                return out
        except Exception:
            continue
    return ""


def _recall(q, n=4, source=None):
    u = f"{MEMSRV}/recall?q={urllib.parse.quote(q[:400])}&n={n}&tier=fast"
    if source:
        u += f"&source={source}"
    try:
        with urllib.request.urlopen(u, timeout=8) as r:
            return json.load(r).get("memories", [])
    except Exception:
        return []


def _recent_changes(oc):
    """Recent maintenance/actions — so 'replica down' 5h after a failback reads as
    expected, not novel."""
    try:
        oc.execute("SELECT to_char(ts,'HH24:MI') || ' ' || left(coalesce(description,''),90) "
                   "FROM claude_actions WHERE ts > now() - interval '12 hours' "
                   "AND action_type='command' ORDER BY ts DESC LIMIT 12")
        return [r[0] for r in oc.fetchall()]
    except Exception:
        return []


def triage(title, body="", level="info", category=None, source=None, dedup_key=None):
    """Return a decision dict. Conservative: pages unless confidently benign."""
    text = f"{title}. {body or ''}".strip()
    hard = bool(_HARD_CRITICAL.search(f"{text} {category or ''}"))
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()

    similar = _recall(text, n=4, source="incident")
    baselines = _recall(text, n=3)  # any memory that establishes what's normal here
    changes = _recent_changes(oc)
    sim_ids = [str(m.get("id")) for m in similar]

    annotation, likely, verdict, conf, reason = "", "", "real_actionable", 0.5, ""

    if hard:
        decision, verdict, conf = "page", "real_actionable", 1.0
        reason = "hard-critical signature — always pages"
    else:
        sim_block = "\n".join(f"- {(m.get('text') or '')[:200]}" for m in similar) or "(none)"
        base_block = "\n".join(f"- {(m.get('text') or '')[:160]}" for m in baselines) or "(none)"
        chg_block = "\n".join(f"- {c}" for c in changes) or "(no recent maintenance)"
        raw = llm(
            "You are Nova's alert triage. Classify this alert given context. Output ONLY JSON: "
            '{"verdict":"real_actionable|known_self_healing|expected_change|duplicate|learned_normal",'
            '"confidence":0.0-1.0,"likely_cause":"<one line>","reason":"<one line>"}.\n'
            "verdict meanings: real_actionable = a genuine problem needing a human; "
            "known_self_healing = this pattern recovers on its own (per past incidents); "
            "expected_change = explained by recent maintenance below; "
            "duplicate = same ongoing condition already known; "
            "learned_normal = matches an established normal baseline.\n"
            "Be conservative: if unsure, verdict=real_actionable.\n\n"
            f"ALERT [{level}/{category}]: {text}\n\n"
            f"SIMILAR PAST INCIDENTS (what it turned out to be):\n{sim_block}\n\n"
            f"KNOWN-NORMAL BASELINES:\n{base_block}\n\n"
            f"RECENT MAINTENANCE (last 12h):\n{chg_block}")
        try:
            j = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
            verdict = j.get("verdict") if j.get("verdict") in _VERDICTS else "real_actionable"
            conf = float(j.get("confidence", 0.5))
            likely = (j.get("likely_cause") or "")[:200]
            reason = (j.get("reason") or "")[:200]
        except Exception as e:
            verdict, conf, reason = "real_actionable", 0.5, f"triage parse failed ({e}) — paging"

        # Decision rules — only NON-critical may be suppressed/downgraded, at high confidence.
        benign = verdict in ("known_self_healing", "expected_change", "duplicate", "learned_normal")
        if level == "critical":
            decision = "page"                        # critical always pages (context added)
        elif benign and conf >= 0.75:
            decision = "suppress" if verdict in ("duplicate", "learned_normal") else "downgrade"
        else:
            decision = "page"

    if similar:
        top = (similar[0].get("text") or "")[:160]
        annotation = f"🔎 Similar past incident: {top}"
        if likely:
            annotation = f"🔎 Likely: {likely}\n{annotation}"
    elif likely:
        annotation = f"🔎 Likely: {likely}"

    try:
        oc.execute(
            "INSERT INTO alert_triage_log (title, level, category, source, dedup_key, verdict, "
            "confidence, decision, reason, likely_cause, similar_ids, hard_override) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (title[:300], level, category, source, dedup_key, verdict, conf, decision,
             reason, likely, sim_ids, hard))
    except Exception as e:
        _log(f"log write failed: {e}")

    return {"decision": decision, "verdict": verdict, "confidence": conf,
            "level": ("warning" if decision == "downgrade" and level == "critical" else level),
            "annotation": annotation, "reason": reason, "likely_cause": likely,
            "hard_override": hard, "similar": sim_ids}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("title")
    ap.add_argument("--body", default="")
    ap.add_argument("--level", default="info")
    ap.add_argument("--category", default=None)
    ap.add_argument("--source", default=None)
    r = ap.parse_args()
    out = triage(r.title, r.body, r.level, r.category, r.source)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
