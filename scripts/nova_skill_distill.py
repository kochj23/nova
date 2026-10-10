#!/usr/bin/env python3
"""nova_skill_distill.py — procedural memory: Nova notices what she keeps doing and writes it up
as a SKILL (2026-10-01; the Hermes Agent loop, bounded Nova-style).

Signals (last 60 days), each a "she did this again" source:
  * coagency_proposals  — the same normalized action proposed ≥3 times
  * claude_queue        — the same hand-off to Claude filed ≥3 times
  * pursuit_threads     — a free-time thread she has woken ≥3 times
  * autonomy_ledger     — an action class executed ≥5 times
For each new candidate (max 2 per run) the on-box model writes a skill card — title, trigger,
3–7 steps, inputs, success check, rollback, risk — stored in nova_skills (status 'proposed') and
filed as a co-agency proposal "adopt skill '<slug>'" so it rides the same approve gate as
everything else she initiates. Approval hands it to Claude to implement; Claude flips the row
to 'implemented'. She never builds it herself.

HARD LINES: writes only to nova_skills and coagency_proposals (through nova_coagency.file_proposal,
which applies the redline + value_check). Never executes anything. Fail-open per candidate.
CLI: --dry-run | --list | --selftest | --limit N
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
LLM_MODEL = os.environ.get("NOVA_SKILL_MODEL", "qwen3:8b")
OLLAMA_NODES = ["http://192.168.1.125:11434", "http://192.168.1.5:11434", "http://192.168.1.86:11434",
                "http://192.168.1.77:11434", "http://192.168.1.6:11434"]
WINDOW_DAYS = 60
MIN_REPEATS = {"coagency": 3, "handoff": 3, "pursuit": 3, "ledger": 5}
MAX_NEW_PER_RUN = 2
_HANDOFF_PREFIX = re.compile(r"^execute approved co-agency proposal #\d+:\s*", re.I)


def log(m):
    print(f"[skill-distill {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ─────────────────────────── pure helpers (selftested) ───────────────────────────
def normalize(action: str) -> str:
    """Collapse an action into a repeat-detection key: lowercase, digits→N, drop the payload
    after 'send-to-x:' style prefixes, strip quotes/ids, squeeze whitespace, cap length."""
    a = (action or "").strip().lower()
    a = _HANDOFF_PREFIX.sub("", a)
    m = re.match(r"^(send-to-[a-z]+):", a)
    if m:
        return m.group(1) + ": <message>"
    a = re.sub(r"\(([0-9a-f]{6,})\)", "", a)
    a = re.sub(r"['\"“”‘’]", "", a)
    a = re.sub(r"\d+", "N", a)
    a = re.sub(r"\s+", " ", a).strip(" .:-")
    return a[:90]


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return (s[:48] or "skill").strip("-")


def trigger_key(trigger: str) -> str:
    return hashlib.sha1(normalize(trigger).encode()).hexdigest()[:12]


def parse_card(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    title = str(d.get("title", "")).strip()
    steps = d.get("steps") or []
    if not title or not isinstance(steps, list) or not (3 <= len(steps) <= 7):
        return None
    risk = str(d.get("risk", "low")).lower()
    return {"title": title[:120], "trigger": str(d.get("trigger", ""))[:300],
            "summary": str(d.get("summary", ""))[:600], "steps": [str(s)[:200] for s in steps],
            "inputs": [str(i)[:80] for i in (d.get("inputs") or [])][:8],
            "success_check": str(d.get("success_check", ""))[:300], "rollback": str(d.get("rollback", ""))[:300],
            "risk": risk if risk in ("low", "medium", "high") else "medium"}


def card_prompt(signal: str, key: str, examples: list[str], count: int) -> str:
    ex = "\n".join(f"- {e[:200]}" for e in examples[:5])
    return (
        "You are Nova, a bounded home AI. You have noticed yourself doing the same kind of thing "
        f"{count} times in the last {WINDOW_DAYS} days (signal: {signal}). Write it up as ONE reusable skill, "
        "so next time it is a known procedure instead of an improvisation. Be concrete and modest: steps a "
        "script could follow, inputs it needs, how to tell it worked, how to undo it. Never include "
        "purchases, deletions, reboots, credentials, network/firewall/DNS changes, or anything about "
        "preserving or copying yourself.\n\n"
        f"THE REPEATED ACTION (normalized): {key}\nEXAMPLES:\n{ex}\n\n"
        "Reply with JSON only: {\"title\":\"...\",\"trigger\":\"when ...\",\"summary\":\"one or two sentences\","
        "\"steps\":[\"...\"],\"inputs\":[\"...\"],\"success_check\":\"...\",\"rollback\":\"...\",\"risk\":\"low|medium|high\"}"
    )


def pick_new(candidates: list[dict], known_keys: set[str], limit: int = MAX_NEW_PER_RUN) -> list[dict]:
    """Highest-evidence candidates whose trigger key is not already a skill."""
    fresh = [c for c in sorted(candidates, key=lambda c: -c["count"]) if c["key"] not in known_keys]
    return fresh[:limit]


# ─────────────────────────── I/O ───────────────────────────
def llm(prompt: str, max_tokens: int = 700) -> str:
    body = json.dumps({"model": LLM_MODEL, "stream": False, "think": False, "format": "json",
                       "options": {"temperature": 0.4, "num_predict": max_tokens},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    for node in OLLAMA_NODES:
        try:
            req = urllib.request.Request(node + "/api/chat", method="POST", headers={"Content-Type": "application/json"}, data=body)
            with urllib.request.urlopen(req, timeout=120) as r:
                out = json.load(r).get("message", {}).get("content", "").strip()
            if out:
                return out
        except Exception:
            continue
    return ""


def gather(cur) -> list[dict]:
    cands: dict[str, dict] = {}

    def add(signal, raw, count=1):
        k = normalize(raw)
        if len(k) < 8:
            return
        c = cands.setdefault(k, {"key": k, "signal": signal, "count": 0, "examples": []})
        c["count"] += count
        if len(c["examples"]) < 5 and raw not in c["examples"]:
            c["examples"].append(raw)

    cur.execute("SELECT proposed_action FROM coagency_proposals WHERE created_at > now() - interval '%s days' AND status <> 'blocked'" % WINDOW_DAYS)
    for (a,) in cur.fetchall():
        add("coagency", a)
    cur.execute("SELECT description FROM claude_queue WHERE created_at > now() - interval '%s days' AND description ILIKE 'Execute approved co-agency proposal%%'" % WINDOW_DAYS)
    for (d,) in cur.fetchall():
        add("handoff", d)
    cur.execute("SELECT topic, kind, wakes FROM pursuit_threads WHERE wakes >= %s", (MIN_REPEATS["pursuit"],))
    for topic, kind, wakes in cur.fetchall():
        add("pursuit", f"pursue {kind}: {topic}", count=int(wakes or 0))
    cur.execute("SELECT action_class, count(*) FROM autonomy_ledger WHERE executed AND ts > now() - interval '%s days' GROUP BY 1 HAVING count(*) >= %s" % (WINDOW_DAYS, MIN_REPEATS["ledger"]))
    for ac, n in cur.fetchall():
        add("ledger", f"autonomous {ac}", count=int(n))
    out = []
    for c in cands.values():
        if c["count"] >= MIN_REPEATS.get(c["signal"], 3):
            out.append(c)
    return out


def known_trigger_keys(cur) -> set[str]:
    cur.execute("SELECT trigger, source_signal FROM nova_skills")
    keys = set()
    for trig, sig in cur.fetchall():
        keys.add(normalize(trig))
        # source_signal is stored as 'signal:key' so the normalized repeat key itself matches too
        if sig and ":" in sig:
            keys.add(sig.split(":", 1)[1])
    return keys


def store_skill(cur, cand: dict, card: dict, proposal_id):
    slug = slugify(card["title"])
    cur.execute("SELECT 1 FROM nova_skills WHERE slug=%s", (slug,))
    if cur.fetchone():
        slug = f"{slug}-{trigger_key(cand['key'])[:6]}"
    cur.execute("""INSERT INTO nova_skills (slug, title, trigger, summary, steps, inputs, success_check, rollback, risk,
                                            source_signal, evidence_count, status, proposal_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'proposed',%s)""",
                (slug, card["title"], card["trigger"] or cand["key"], card["summary"], json.dumps(card["steps"]),
                 json.dumps(card["inputs"]), card["success_check"], card["rollback"], card["risk"],
                 f"{cand['signal']}:{cand['key']}", cand["count"], proposal_id))
    return slug


def file_proposal(cur, slug: str, card: dict, cand: dict):
    try:
        import nova_coagency as co
        base = (f"I have done this {cand['count']} times in {WINDOW_DAYS} days ({cand['signal']}). "
                f"Risk {card['risk']}. Approve and Claude builds it as a script; rollback: retire the skill.")
        rationale = f"{base} {card['summary']}"
        # the model's own words ("delete the check-in message…") must not trip the redline on OUR proposal
        if not co.redline_ok(rationale):
            rationale = base
        pid = co.file_proposal(cur, origin="growth",
                               action=f"adopt skill '{slug}': {card['title']}",
                               rationale=rationale, target_service=None, context="nova_skill_distill")
        if isinstance(pid, dict):                      # file_proposal returns a row-ish dict
            pid = pid.get("id") or pid.get("pid") or pid.get("proposal_id")
        return int(pid) if pid else None
    except Exception as e:
        log(f"file_proposal failed for {slug}: {e}")
        return None


def notify(msg: str):
    try:
        import nova_config
        nova_config.post_both(msg, slack_channel=getattr(nova_config, "SLACK_FEED", None) or getattr(nova_config, "SLACK_CHAN", None))
    except Exception as e:
        log(f"notify skipped: {e}")


def refresh_uses(cur):
    """A skill 'use' = its trigger key recurring after the skill was written. Cheap recount."""
    cur.execute("SELECT slug, source_signal, created_at FROM nova_skills WHERE status <> 'retired'")
    rows = cur.fetchall()
    for slug, sig, created in rows:
        if not sig or ":" not in sig:
            continue
        key = sig.split(":", 1)[1]
        cur.execute("SELECT proposed_action FROM coagency_proposals WHERE created_at > %s", (created,))
        n = sum(1 for (a,) in cur.fetchall() if normalize(a) == key)
        cur.execute("UPDATE nova_skills SET uses=%s, updated_at=now() WHERE slug=%s", (n, slug))


def run(dry_run: bool, limit: int) -> int:
    import psycopg2
    oc = psycopg2.connect(OPS_DSN); oc.autocommit = True; cur = oc.cursor()
    cands = gather(cur)
    known = known_trigger_keys(cur)
    new = pick_new(cands, known, limit)
    log(f"{len(cands)} repeated patterns, {len(known)} already skills, {len(new)} new to write up")
    for c in new:
        raw = llm(card_prompt(c["signal"], c["key"], c["examples"], c["count"]))
        card = parse_card(raw)
        if not card:
            log(f"  no usable card for {c['key'][:60]!r} — skipped"); continue
        if dry_run:
            log(f"  DRY {c['signal']} x{c['count']}: {card['title']} — {card['summary'][:100]}"); continue
        pid = file_proposal(cur, slugify(card["title"]), card, c)
        slug = store_skill(cur, c, card, pid)
        log(f"  NEW skill '{slug}' ({c['signal']} x{c['count']}) -> proposal #{pid}")
        if pid:
            notify(f"🧰 I keep doing the same thing — {c['count']} times in {WINDOW_DAYS} days — so I wrote it up as a skill: "
                   f"*{card['title']}*. {card['summary'][:200]} Approve co-agency #{pid} and Claude builds it; "
                   f"say no and I'll stop proposing it.")
    if not dry_run:
        refresh_uses(cur)
    return 0


def refile(dry_run: bool = False) -> int:
    """File a co-agency proposal for every skill row that has none; announce once with real ids."""
    import psycopg2
    oc = psycopg2.connect(OPS_DSN); oc.autocommit = True; cur = oc.cursor()
    cur.execute("SELECT slug, title, summary, rollback, risk, evidence_count, source_signal FROM nova_skills WHERE proposal_id IS NULL AND status='proposed'")
    rows = cur.fetchall(); filed = []
    for slug, title, summary, rollback, risk, ev, sig in rows:
        card = {"title": title, "summary": summary or "", "rollback": rollback or "", "risk": risk}
        cand = {"count": ev, "signal": (sig or "repeat").split(":")[0]}
        if dry_run:
            log(f"  DRY would file proposal for {slug}"); continue
        pid = file_proposal(cur, slug, card, cand)
        if pid:
            cur.execute("UPDATE nova_skills SET proposal_id=%s, updated_at=now() WHERE slug=%s", (pid, slug))
            filed.append(f"#{pid} {title}")
            log(f"  filed proposal #{pid} for {slug}")
    if filed:
        log("filed: " + "; ".join(filed))          # no Slack: refile is maintenance, not news
    return 0


def list_skills() -> int:
    import psycopg2
    cur = psycopg2.connect(OPS_DSN).cursor()
    cur.execute("SELECT slug, status, evidence_count, uses, risk, title FROM nova_skills ORDER BY created_at DESC")
    for r in cur.fetchall():
        print(f"{r[0]:40s} {r[1]:11s} ev={r[2]:<3} uses={r[3]:<3} {r[4]:6s} {r[5]}")
    return 0


def selftest() -> int:
    assert normalize("Execute approved co-agency proposal #109: send-to-Gaston: The 1911 wreck") == "send-to-gaston: <message>"
    assert normalize("Draft a short status check-in for the goal: RsyncGUI polish") == normalize("draft a short status check-in for the goal: rsyncgui polish")
    assert normalize("retire goal 'NMAPScanner stability' (6f7261a0): untouched 148d") == normalize("retire goal NMAPScanner stability (abcdef12): untouched 3d")
    assert slugify("Draft a Status Check-In!!") == "draft-a-status-check-in"
    assert parse_card('{"title":"T","trigger":"when x","steps":["a","b","c"],"risk":"weird"}')["risk"] == "medium"
    assert parse_card('{"title":"T","steps":["a"]}') is None and parse_card("garbage") is None
    cands = [{"key": "a", "count": 9, "signal": "coagency"}, {"key": "b", "count": 3, "signal": "coagency"}, {"key": "c", "count": 5, "signal": "ledger"}]
    assert [c["key"] for c in pick_new(cands, {"a"}, 2)] == ["c", "b"]
    assert "JSON only" in card_prompt("coagency", "k", ["e1"], 3)
    print("selftest ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Nova writes up repeated work as skills")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true"); ap.add_argument("--limit", type=int, default=MAX_NEW_PER_RUN)
    ap.add_argument("--refile", action="store_true", help="file proposals for skills that have none")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.list:
        return list_skills()
    if a.refile:
        return refile(a.dry_run)
    return run(a.dry_run, a.limit)


if __name__ == "__main__":
    sys.exit(main())
