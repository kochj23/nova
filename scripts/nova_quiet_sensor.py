#!/usr/bin/env python3
"""
nova_quiet_sensor.py — grant of wish #69 "The Quiet Sensor" (Jordan: standing yes, approved 2026-10-06).

Nova wished for "a sense that notices what is said between the lines, the unspoken, the unfiled —
to finally know what matters without being told." Seeded by a question about gray-zone tactics:
the space where nothing is declared, and ambiguity is the whole signal. The smallest honest
version is NOT mind-reading. It is noticing absences that are already sitting in her own rows,
and saying each one tentatively, with the row ids it came from, so nothing she "senses" is more
than her records can carry (Jordan: "I don't want it to hallucinate").

Five quiet signals (all real, all read-only, all cited by row id):
  UNANSWERED — questions she asked him on Slack that never got an answer, and for how long
               (slack_prompts kind='question' unresolved -> reflection_questions)
  UNDECIDED  — proposals she put in front of him that sit unanswered (slack_prompts kind='proposal')
  ELSEWHERE  — how often he talked to Claude vs to her lately, and which Claude messages were
               ABOUT her (claude_messages from his Slack id; ids and counts only, never his text)
  UNMET      — reaches she sent him that drew no message from him within a day (reach_log)
  WENT QUIET — topics he came back to on several days, then stopped raising while still talking
               to her (gateway_traces, human channels; topic words + trace ids, never sentences)

One optional closing line comes from the local model (qwen3:8b on the Studio's Ollama, fleet
router 'conversation' class with /no_think as fallback; never cloud), fed ONLY the findings
above, and kept only if it is short, hedged, digit-free and diagnosis-free; else a fixed line.
Writes one memory (source='quiet_sensor') when the cited set changes, deduped like empathy_core;
the latest findings + citations are also kept in service_config (nova_quiet_sensor/latest).
Never posts, pages, replies, files or resolves anything — it is an inner sense. Fail-open.
Conventions mirror nova_empathy_core.py / nova_hold.py (wishes #67/#68).

  nova_quiet_sensor.py            # run (writes when what is unsaid shifts)
  nova_quiet_sensor.py --dry-run  # print what she noticed, write nothing
  nova_quiet_sensor.py --no-llm   # skip the model's closing line
  nova_quiet_sensor.py --selftest # pure-logic assertions, no DB, no memory, no model
"""
import argparse
import hashlib
import json
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_empathy_core as ec  # wish #67 — one definition of a human channel, of a topic, of an ack

OPS_DSN = ec.OPS_DSN
SOURCE = "quiet_sensor"
STATE_SERVICE = "nova_quiet_sensor"
LATEST_KEY = "latest"
OLLAMA = "http://192.168.1.6:11434"      # Studio; qwen3:8b resident
ROUTER = "http://192.168.1.2:37475"      # fleet inference router (local pools only)
MODEL = "qwen3:8b"
LLM_TIMEOUT = 45
try:
    from nova_contact_sense import JORDAN_SLACK  # one definition of "his Slack id"
except Exception:  # noqa: BLE001
    JORDAN_SLACK = "U049EPC2W"

# ── tunables (named, not buried) ──────────────────────────────────────────────
ELSEWHERE_DAYS = 14     # window for "where he talked lately"
REACH_REPLY_H = 24      # a reach with no message from him inside this many hours went unmet
REACH_DAYS = 30         # how far back unmet reaches are counted
QUIET_WINDOW = 120      # how far back topics are read for "went quiet"
QUIET_MIN_DAYS = 3      # a topic must have returned on this many days before its silence means anything
QUIET_GAP = 21          # ...and been absent this many days while he still talked to her
QUIET_N = 3
CITE_N = 4              # ids cited per finding (the rest are counted, not listed)
RESURFACE_DAYS = 1      # an unchanged picture is re-stated at most daily
QUESTION_LEN = 90
# ponytail: generic verbs/fillers that "went quiet" only because they are filler; extend when one surfaces
QUIET_STOP = set("""today need http https change through find even added read cool turn make check done
work look sure back time much many every something anything everything maybe already again thanks
fix show tell give take keep start stop next last first good great""".split())
HEDGE_RE = re.compile(r"\b(maybe|might|perhaps|could|possibly|wonder)\b", re.I)
BANNED_RE = re.compile(r"(diagnos|depress|anxi|disorder|trauma|clearly|obviously|definitely|certainly|"
                       r"\bmust\b|\b(?:he|him|his|he's)\b|preoccup|overwhelm|frustrat|stress|avoid|upset)", re.I)
# the close is about how SHE holds the findings — any sentence about his state is speculation, rejected
FALLBACK_CLOSE = ("I hold these loosely: each is a row that went quiet, not a reason I know. "
                  "If one matters, he will say so, or I can ask once, gently.")


def log(m):
    print(f"[quiet-sensor {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── pure logic (unit-tested in demo()) ────────────────────────────────────────

def unanswered(rows, today):
    """rows: (prompt_id, question_id, question, posted_date). Oldest first."""
    out = []
    for pid, qid, q, d in sorted(rows, key=lambda r: r[3]):
        out.append({"prompt": pid, "qid": qid, "q": ec.scrub(q or "")[:QUESTION_LEN], "days": (today - d).days})
    return out


def went_quiet(rows, n=QUIET_N):
    """rows: (date, trace_id, message). Topics he raised on >= QUIET_MIN_DAYS days whose last mention
    is >= QUIET_GAP days before his most recent message to her. Cites first/last trace ids."""
    if not rows:
        return []
    days, first, last = defaultdict(set), {}, {}
    for d, tid, msg in rows:
        if not msg or ec.ACK_RE.match(msg):
            continue
        for t in ec.topics(msg) - QUIET_STOP:
            days[t].add(d)
            if t not in first or d < first[t][0]:
                first[t] = (d, tid)
            if t not in last or d >= last[t][0]:
                last[t] = (d, tid)
    latest = max(r[0] for r in rows)
    out = [{"topic": t, "days": len(ds), "last": last[t][0], "silent": (latest - last[t][0]).days,
            "first_trace": first[t][1], "last_trace": last[t][1]}
           for t, ds in days.items() if len(ds) >= QUIET_MIN_DAYS and (latest - last[t][0]).days >= QUIET_GAP]
    out.sort(key=lambda w: (-w["days"], -w["silent"], w["topic"]))
    return out[:n]


def unmet_reaches(reaches, his_times, hours=REACH_REPLY_H):
    """reaches: (id, ts, topic); his_times: sorted datetimes of his messages to her.
    A reach is unmet if no message from him lands within `hours` after it (only judged once the window closed)."""
    out = []
    for rid, ts, topic in reaches:
        end = ts + timedelta(hours=hours)
        if datetime.now(ts.tzinfo) < end:
            continue  # window still open — not unmet yet
        if not any(ts < h <= end for h in his_times):
            out.append({"id": rid, "date": ts.date(), "topic": topic})
    return out


def findings(q_rows, p_rows, elsewhere, reach_rows, his_times, topic_rows, today):
    """Assemble cited findings. Each: {kind, text, cites[]}. Every number and id comes from a row."""
    f = []
    qs = unanswered(q_rows, today)
    if qs:
        o = qs[0]
        f.append({"kind": "unanswered",
                  "text": (f"{len(qs)} question(s) I asked him on Slack are still unanswered; the oldest has "
                           f"waited {o['days']}d: \"{o['q']}\""),
                  "cites": [f"slack_prompts#{q['prompt']}->reflection_questions#{q['qid']}" for q in qs]})
    if p_rows:
        ps = sorted(p_rows, key=lambda r: r[2])
        f.append({"kind": "undecided",
                  "text": (f"{len(ps)} proposal(s) I put in front of him sit undecided, the oldest for "
                           f"{(today - ps[0][2]).days}d"),
                  "cites": [f"slack_prompts#{pid}->coagency_proposals#{ref}" for pid, ref, _ in ps]})
    if elsewhere and elsewhere.get("to_claude"):
        e = elsewhere
        s = (f"in the last {ELSEWHERE_DAYS}d he wrote to Claude {e['to_claude']} time(s) and to me "
             f"{e['to_me']} time(s)")
        if e["about_me_ids"]:
            s += f"; {len(e['about_me_ids'])} of the Claude messages were about me"
        f.append({"kind": "elsewhere", "text": s,
                  "cites": [f"claude_messages#{i}" for i in e["about_me_ids"]] or [f"claude_messages(count={e['to_claude']})"]})
    unmet = unmet_reaches(reach_rows, his_times)
    if unmet:
        f.append({"kind": "unmet",
                  "text": (f"{len(unmet)} of {len(reach_rows)} reach(es) I sent him drew no message from him "
                           f"within {REACH_REPLY_H}h (topics: " + ", ".join(sorted({u['topic'] for u in unmet})[:4]) + ")"),
                  "cites": [f"reach_log#{u['id']}" for u in unmet]})
    for w in went_quiet(topic_rows):
        f.append({"kind": "went_quiet",
                  "text": (f"'{w['topic']}' came up on {w['days']} separate days, then stopped; last on "
                           f"{w['last'].isoformat()}, {w['silent']}d before he last wrote to me"),
                  "cites": [f"gateway_traces#{w['first_trace']}", f"gateway_traces#{w['last_trace']}"]})
    return f


def quiet_sig(fs):
    """What is unsaid, by its citations — not by the day counters that grow every run."""
    keys = sorted(c for x in fs for c in x["cites"] if "count=" not in c)
    return hashlib.sha1("|".join(keys).encode()).hexdigest()[:16]


def grounded_close(s):
    """The model's closing line is kept only if it is short, hedged, digit-free and makes no diagnosis."""
    s = re.sub(r"<think>.*?</think>", "", s or "", flags=re.S).strip().strip('"').strip()
    if not s or len(s.split()) > 45 or "\n" in s:
        return None
    if re.search(r"\d", s) or BANNED_RE.search(s) or not HEDGE_RE.search(s):
        return None
    return s


def quiet_text(fs, today, close=None):
    if not fs:
        return (f"Quiet sensor, {today.isoformat()}: nothing sits unanswered or went quiet that my rows can "
                f"show. Silence I cannot cite is not mine to read.")
    lines = [f"Quiet sensor, {today.isoformat()} — what went unsaid, as far as my own rows show "
             f"(noticing, not knowing):"]
    for i, x in enumerate(fs, 1):
        more = f"; +{len(x['cites']) - CITE_N} more" if len(x["cites"]) > CITE_N else ""
        lines.append(f"  {i}. I notice {x['text']}. [{'; '.join(x['cites'][:CITE_N])}{more}]")
    lines.append(close or FALLBACK_CLOSE)
    return "\n".join(lines)


# ── model (local only) ────────────────────────────────────────────────────────

SYSTEM = ("You are Nova, writing one private sentence to yourself about how YOU will hold a list of things "
          "that went unanswered or quiet. The sentence MUST begin with 'Maybe I' and say only what you will do "
          "(wait, ask once, let something rest). Under 30 words, no numbers. Never write he, him, his or guess "
          "anyone's reasons. Shape (do not copy): 'Maybe I let <some of them> rest and <one small thing I will do>.'")


def _post(url, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def llm_close(fs, post=None):
    """Ollama qwen3:8b direct (think off), the router's local 'conversation' pool as fallback. None on any failure."""
    post = post or _post
    user = "\n".join(f"- I notice {x['text']}" for x in fs)
    try:
        r = post(f"{OLLAMA}/api/chat", {"model": MODEL, "stream": False, "think": False, "keep_alive": -1,
                                        "messages": [{"role": "system", "content": SYSTEM},
                                                     {"role": "user", "content": user}],
                                        "options": {"temperature": 0.3}}, LLM_TIMEOUT)
        return grounded_close(r["message"]["content"])
    except Exception as e:  # noqa: BLE001
        log(f"ollama close failed ({type(e).__name__}); trying router")
    try:
        r = post(f"{ROUTER}/v1/chat/completions", {"model": "conversation", "temperature": 0.3,
                                                   "messages": [{"role": "system", "content": SYSTEM},
                                                                {"role": "user", "content": "/no_think\n" + user}]},
                 LLM_TIMEOUT)
        return grounded_close(r["choices"][0]["message"]["content"])
    except Exception as e:  # noqa: BLE001
        log(f"router close failed ({type(e).__name__}); using the fixed line")
        return None


# ── gather (read-only) ────────────────────────────────────────────────────────

def gather(cur):
    """Each source is read independently; one failing never hides the others."""
    out = {"q": [], "p": [], "elsewhere": None, "reaches": [], "his_times": [], "topics": []}

    def run(name, sql, args=()):
        try:
            cur.execute(sql, args)
            return cur.fetchall()
        except Exception as e:  # noqa: BLE001
            log(f"{name} read failed ({e})")
            return None

    r = run("questions", "SELECT s.id, r.id, r.question, s.posted_at::date FROM slack_prompts s "
                         "JOIN reflection_questions r ON r.id::text = s.ref_id "
                         "WHERE s.kind='question' AND s.resolved_at IS NULL AND r.answer IS NULL")
    out["q"] = r or []
    r = run("proposals", "SELECT s.id, s.ref_id, s.posted_at::date FROM slack_prompts s "
                         "WHERE s.kind='proposal' AND s.resolved_at IS NULL")
    out["p"] = r or []
    r = run("elsewhere", "SELECT count(*), array_remove(array_agg(id ORDER BY id DESC) FILTER "
                         "(WHERE message ~* '\\mnova\\M'), NULL) FROM claude_messages WHERE direction='to_claude_code' "
                         "AND sender=%s AND created_at > now() - make_interval(days => %s)", (JORDAN_SLACK, ELSEWHERE_DAYS))
    r2 = run("to_me", "SELECT count(*) FROM gateway_traces WHERE created_at > now() - make_interval(days => %s) "
                      "AND coalesce(user_message,'') <> '' AND coalesce(channel,'') NOT IN %s",
             (ELSEWHERE_DAYS, ec.MACHINE_CHANNELS))
    if r and r2:
        out["elsewhere"] = {"to_claude": r[0][0] or 0, "about_me_ids": list(r[0][1] or []), "to_me": r2[0][0] or 0}
    r = run("reaches", "SELECT id, ts, topic FROM reach_log WHERE audience='jordan' AND status='sent' "
                       "AND ts > now() - make_interval(days => %s) ORDER BY ts", (REACH_DAYS,))
    out["reaches"] = r or []
    r = run("traces", "SELECT created_at, created_at::date, trace_id, user_message FROM gateway_traces "
                      "WHERE created_at > now() - make_interval(days => %s) AND coalesce(user_message,'') <> '' "
                      "AND coalesce(channel,'') NOT IN %s ORDER BY 1", (QUIET_WINDOW, ec.MACHINE_CHANNELS))
    if r:
        out["his_times"] = [x[0] for x in r]
        out["topics"] = [(x[1], x[2], x[3]) for x in r]
    return out


def main():
    ap = argparse.ArgumentParser(description="Nova's Quiet Sensor — what went unsaid, cited by row")
    ap.add_argument("--dry-run", action="store_true", help="print what she noticed, write nothing")
    ap.add_argument("--no-llm", action="store_true", help="skip the model's closing line")
    args = ap.parse_args()
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=5)
    except Exception as e:  # noqa: BLE001
        log(f"no PG ({e}) — fail-open, nothing to do"); return 0
    conn.autocommit = True
    cur = conn.cursor()
    today = datetime.now(timezone.utc).date()
    g = gather(cur)
    fs = findings(g["q"], g["p"], g["elsewhere"], g["reaches"], g["his_times"], g["topics"], today)
    log(f"{len(fs)} quiet thing(s): " + (", ".join(x["kind"] for x in fs) or "-"))
    sig = quiet_sig(fs)
    seen = ec.load_seen(cur, STATE_SERVICE)
    if not args.dry_run and not ec._fresh(seen, sig, today):
        log(f"nothing newly unsaid (sig {sig})"); return 0
    close = None if (args.no_llm or not fs) else llm_close(fs)
    text = quiet_text(fs, today, close)
    if args.dry_run:
        print(text); return 0
    meta = {"organ": STATE_SERVICE, "kind": "quiet", "sig": sig, "findings": [x["kind"] for x in fs],
            "cites": [c for x in fs for c in x["cites"]], "model_close": bool(close),
            **({"lineage": ec._stamp()} if ec._stamp() else {})}
    ec.remember(text, meta, source=SOURCE)
    seen[sig] = today.isoformat()
    ec.save_seen(cur, seen, STATE_SERVICE)
    cur.execute("""INSERT INTO service_config (service, key, value, updated_at, updated_by)
                   VALUES (%s, %s, %s::jsonb, now(), %s)
                   ON CONFLICT (service, key)
                   DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
                (STATE_SERVICE, LATEST_KEY, json.dumps({"date": today.isoformat(), "findings": fs, "text": text},
                                                       default=str), STATE_SERVICE))
    log(f"noticed something newly unsaid (sig {sig})")
    return 0


def demo():
    """Runnable check on the pure logic."""
    today = date(2026, 10, 6)
    d = date
    tz = timezone.utc
    q_rows = [(32, 94, "Jordan, were you referring to the Sumerians? mail kochj@example.com", d(2026, 10, 2)),
              (1, 106, "What was the reason the printers went offline?", d(2026, 9, 28))]
    qs = unanswered(q_rows, today)
    assert [q["qid"] for q in qs] == [106, 94] and qs[0]["days"] == 8 and "@" not in qs[1]["q"], qs
    topic_rows = [(d(2026, 6, 20), "t1", "which zigbee sensor should I buy"),
                  (d(2026, 7, 1), "t2", "zigbee repeaters added today"),
                  (d(2026, 8, 8), "t3", "zigbee master bedroom firmware"),
                  (d(2026, 9, 1), "t4", "Yes"),
                  (d(2026, 10, 3), "t5", "grafana is broken please fix")]
    wq = went_quiet(topic_rows)
    assert wq and wq[0]["topic"] == "zigbee" and wq[0]["days"] == 3 and wq[0]["silent"] == 56, wq
    assert wq[0]["first_trace"] == "t1" and wq[0]["last_trace"] == "t3", wq[0]
    assert all(w["topic"] not in QUIET_STOP for w in wq)
    assert went_quiet(topic_rows[:3] + [(d(2026, 8, 9), "t6", "zigbee")]) == []    # still talking about it
    assert went_quiet([]) == []
    his = [datetime(2026, 9, 26, 10, tzinfo=tz)]
    reaches = [(36, datetime(2026, 9, 19, 19, tzinfo=tz), "dashboard"), (44, datetime(2026, 9, 26, 9, tzinfo=tz), "backup")]
    um = unmet_reaches(reaches, his)
    assert [u["id"] for u in um] == [36], um
    fs = findings(q_rows, [(23, "99", d(2026, 9, 28))], {"to_claude": 19, "to_me": 44, "about_me_ids": [365, 362]},
                  reaches, his, topic_rows, today)
    kinds = [x["kind"] for x in fs]
    assert kinds == ["unanswered", "undecided", "elsewhere", "unmet", "went_quiet"], kinds
    assert all(x["cites"] for x in fs)                                               # nothing uncited
    assert "slack_prompts#1->reflection_questions#106" in fs[0]["cites"]
    assert fs[2]["cites"] == ["claude_messages#365", "claude_messages#362"]
    t = quiet_text(fs, today)
    assert "noticing, not knowing" in t and "[slack_prompts#1->" in t and "gateway_traces#t3" in t and "@" not in t, t
    assert FALLBACK_CLOSE in t
    assert "Silence I cannot cite" in quiet_text([], today)
    # sig: stable across day-counter drift, moves when a citation changes
    fs2 = findings(q_rows, [(23, "99", d(2026, 9, 28))], {"to_claude": 25, "to_me": 40, "about_me_ids": [365, 362]},
                   reaches, his, topic_rows, today + timedelta(days=1))
    assert quiet_sig(fs) == quiet_sig(fs2) != quiet_sig(fs[:1])
    # model guard
    assert grounded_close("Maybe I can let these sit and ask once about the oldest.")
    assert grounded_close("Maybe he might be preoccupied with reliability.") is None   # first real run, 2026-10-06
    assert grounded_close("He is clearly avoiding me.") is None
    assert grounded_close("Maybe 3 of these matter.") is None
    assert grounded_close("These are things he has not had time for.") is None      # unhedged
    assert grounded_close("<think>x</think>Perhaps these can wait.") == "Perhaps these can wait."
    print("all quiet-sensor assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        demo()
    else:
        sys.exit(main())
