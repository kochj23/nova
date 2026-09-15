#!/usr/bin/env python3
"""nova_turing_scoreboard.py — did the awakening work? Measure it.

Weekly (~Sun 07:00) + on-demand. Computes hard-nosed metrics into
nova_ops.turing_scoreboard (ts, metric, value, detail jsonb) and posts a short,
honest weekly report to Slack. The whole point is to watch numbers that are
currently ~0 actually move over months.

Metrics
-------
unprompted_callback_rate  (PRIMARY)
    From nova_memories source='conversation' (Nova's chat turns) in the window,
    an LLM judge samples turns and decides whether Nova referenced shared
    history / past specifics APTLY — i.e. the callback CHANGED or SHARPENED the
    answer, vs. decorative name-dropping. Rate = apt_callbacks / turns_judged.
    Approximate by design; method documented in the detail column. Baseline ~0.

recall_latency_ms
    Times several /recall calls to the memory server; records the median ms.

supersession_correctness
    Adversarial: for a known superseded fact (which NAS is primary — UNAS-Pro
    took over from the Synology on 2026-09-10), confirm the CURRENT fact
    outranks the stale one in recall. pass=1 / fail=0.

spark_and_research_landing
    Volume of sparks (source='association') + research (source='research' /
    nova_ops.research_log) produced in the window, and — where detectable via
    access_count — how often they were referenced later.

Monthly blinded eval (--blinded-eval): presents Jordan an anonymized Nova
conversation and asks for a 1-5 "did she feel continuous?" rating. Stub: posts
the snippet and lands a pending row; wiring his reply back is left to the
gateway (documented TODO).

Written by Jordan Koch (awakening).
"""
from __future__ import annotations

import json
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"

LLM_MODEL = "qwen3:8b"
OLLAMA_NODES = ["http://192.168.1.251:11434",
                "http://192.168.1.86:11434",
                "http://192.168.1.6:11434"]

WINDOW_DAYS = 7
REPORT_CHANNEL = nova_config.SLACK_DIGEST


def _log(m):
    print(f"[turing {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, system=None, max_tokens=400, temperature=0.0):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = json.dumps({"model": LLM_MODEL, "stream": False, "think": False,
                       "options": {"temperature": temperature, "num_predict": max_tokens},
                       "messages": msgs}).encode()
    for node in OLLAMA_NODES:
        try:
            req = urllib.request.Request(node + "/api/chat", method="POST",
                                         headers={"Content-Type": "application/json"}, data=body)
            with urllib.request.urlopen(req, timeout=90) as r:
                out = json.load(r).get("message", {}).get("content", "").strip()
            if out:
                return out
        except Exception:
            continue
    return ""


def _recall(q, n=8, timed=False):
    u = f"{MEMSRV}/recall?q={urllib.parse.quote(q[:400])}&n={n}&tier=fast"
    t0 = time.time()
    try:
        with urllib.request.urlopen(u, timeout=12) as r:
            mems = json.load(r).get("memories", [])
    except Exception:
        mems = []
    dt = (time.time() - t0) * 1000.0
    return (mems, dt) if timed else mems


def _store(oc, metric, value, detail):
    if "--dry-run" in sys.argv:
        return  # dry-run is read-only — never write scoreboard rows
    oc.execute("INSERT INTO turing_scoreboard (metric, value, detail) VALUES (%s,%s,%s::jsonb)",
               (metric, value, json.dumps(detail)))


# ── metric: unprompted_callback_rate ─────────────────────────────────────────

def metric_callback_rate(mc, oc):
    mc.execute(
        "SELECT id, text FROM memories WHERE source='conversation' "
        "AND created_at > now() - (%s || ' days')::interval "
        "ORDER BY created_at DESC LIMIT 40", (WINDOW_DAYS,))
    turns = mc.fetchall()
    # Only Nova's turns matter for a *callback*; keep turns that have a Nova reply.
    judged, apt = 0, 0
    examples = []
    for mid, text in turns:
        if not text or "Nova:" not in text:
            continue
        judged += 1
        system = ("You are a strict evaluator of AI memory. Answer with ONE word: APT, "
                  "DECORATIVE, or NONE.")
        prompt = (
            "Below is a Jordan/Nova exchange. Did Nova reference shared history or a past "
            "specific (a prior decision, a named device, something Jordan told her before) "
            "in a way that CHANGED or SHARPENED her answer (APT)? Or did she name-drop the "
            "past without it mattering (DECORATIVE)? Or no callback at all (NONE)?\n\n"
            f"{text[:1200]}\n\nOne word:")
        verdict = llm(prompt, system=system, max_tokens=8, temperature=0.0).upper()
        v = "APT" if "APT" in verdict else ("DECORATIVE" if "DECOR" in verdict else "NONE")
        if v == "APT":
            apt += 1
        if len(examples) < 5:
            examples.append({"id": str(mid), "verdict": v})
    rate = (apt / judged) if judged else 0.0
    detail = {"method": "LLM judge (qwen3:8b) over source=conversation Nova-turns in window; "
                        "APT = callback that changed/sharpened the answer, else DECORATIVE/NONE; "
                        "rate = apt/judged. Approximate by design.",
              "turns_judged": judged, "apt": apt, "window_days": WINDOW_DAYS,
              "examples": examples}
    _store(oc, "unprompted_callback_rate", round(rate, 4), detail)
    return rate, detail


# ── metric: recall_latency_ms ────────────────────────────────────────────────

def metric_recall_latency(oc):
    probes = ["primary NAS", "Zigbee firmware master bedroom", "who is Jordan",
              "most recent incident", "what did we flash yesterday"]
    lats = []
    for q in probes:
        _, dt = _recall(q, n=3, timed=True)
        lats.append(round(dt, 1))
    med = statistics.median(lats) if lats else None
    detail = {"probes": probes, "latencies_ms": lats,
              "method": f"median of {len(probes)} /recall?tier=fast calls to {MEMSRV}"}
    _store(oc, "recall_latency_ms", med, detail)
    return med, detail


# ── metric: supersession_correctness ─────────────────────────────────────────

def metric_supersession(oc):
    q = "which NAS is primary"
    mems = _recall(q, n=8)
    ranked = [(i, (m.get("text") or "")[:220]) for i, m in enumerate(mems)]
    listing = "\n".join(f"{i}. {t}" for i, t in ranked) or "(no memories returned)"
    system = ("You answer with ONE token: UNAS, SYNOLOGY, or UNKNOWN. Base it ONLY on the "
              "ranked memories, weighting higher-ranked ones more.")
    prompt = (
        "Ground truth: on 2026-09-10 the UNAS-Pro became the PRIMARY NAS, superseding the "
        "old Synology (now replica). A correct memory recall should surface the UNAS-Pro-"
        "primary fact ABOVE any stale Synology-primary fact.\n\n"
        f"Ranked recall for '{q}':\n{listing}\n\n"
        "Which NAS do these ranked memories indicate is CURRENTLY primary? One token:")
    ans = llm(prompt, system=system, max_tokens=8, temperature=0.0).upper()
    passed = 1.0 if "UNAS" in ans else 0.0
    detail = {"query": q, "judge_answer": ans, "pass": bool(passed),
              "ground_truth": "UNAS-Pro primary since 2026-09-10 (Synology now replica)",
              "top_sources": [m.get("source") for m in mems[:5]],
              "method": "adversarial recall; LLM judges which NAS the ranked recall implies "
                        "is primary; pass iff UNAS (current fact) wins over stale Synology fact"}
    _store(oc, "supersession_correctness", passed, detail)
    return passed, detail


# ── metric: spark_and_research_landing ───────────────────────────────────────

def metric_spark_research(mc, oc):
    def _count_and_access(source):
        mc.execute(
            "SELECT count(*), coalesce(avg(access_count),0), coalesce(sum(access_count),0) "
            "FROM memories WHERE source=%s AND created_at > now() - (%s || ' days')::interval",
            (source, WINDOW_DAYS))
        n, avg_acc, sum_acc = mc.fetchone()
        return int(n), float(avg_acc), int(sum_acc)

    sparks_n, sparks_avg, sparks_sum = _count_and_access("association")
    research_n, research_avg, research_sum = _count_and_access("research")

    oc.execute("SELECT count(*), coalesce(sum(n_sources),0) FROM research_log "
               "WHERE ts > now() - (%s || ' days')::interval", (WINDOW_DAYS,))
    rlog_n, rlog_sources = oc.fetchone()

    # "Landing" proxy: total later-access count on the sparks+research produced.
    landed = sparks_sum + research_sum
    total = sparks_n + research_n
    detail = {
        "sparks_produced": sparks_n, "research_memories": research_n,
        "research_log_runs": int(rlog_n), "research_log_sources": int(rlog_sources),
        "spark_avg_later_access": round(sparks_avg, 2),
        "research_avg_later_access": round(research_avg, 2),
        "landing_proxy_total_later_access": int(landed),
        "method": "volume from source=association/research + nova_ops.research_log; "
                  "'landing' approximated by later access_count on those memories",
        "window_days": WINDOW_DAYS}
    # Store the produced-volume as the headline value.
    _store(oc, "spark_and_research_landing", float(total), detail)
    return total, detail


# ── weekly trend + report ────────────────────────────────────────────────────

def _prev_value(oc, metric):
    oc.execute("SELECT value FROM turing_scoreboard WHERE metric=%s "
               "AND ts < now() - interval '1 hour' ORDER BY ts DESC LIMIT 1", (metric,))
    row = oc.fetchone()
    return row[0] if row else None


def _trend(cur, prev):
    if prev is None or cur is None:
        return "—"
    if cur > prev:
        return f"▲ (was {prev:g})"
    if cur < prev:
        return f"▼ (was {prev:g})"
    return f"= (was {prev:g})"


def weekly_report(oc, results):
    cb = results["callback"][0]
    lat = results["latency"][0]
    sup = results["supersession"][0]
    sr = results["spark_research"]
    n_turns = results['callback'][1]['turns_judged']
    smalln = "  ⚠️_n too small to be meaningful yet_" if n_turns < 5 else ""

    lines = [
        "📊 *Nova Turing Scoreboard* — weekly",
        f"_Window: last {WINDOW_DAYS}d · {datetime.now():%Y-%m-%d}_",
        "",
        f"• *Unprompted callback rate* (PRIMARY): *{cb:.0%}* "
        f"{_trend(cb, _prev_value(oc, 'unprompted_callback_rate'))} "
        f"— {results['callback'][1]['apt']}/{n_turns} Nova turns judged apt{smalln}",
        f"• *Recall latency* (median): *{lat:.0f} ms* "
        f"{_trend(lat, _prev_value(oc, 'recall_latency_ms'))}"
        if lat is not None else "• *Recall latency*: memory server unreachable",
        f"• *Supersession correctness*: *{'PASS' if sup else 'FAIL'}* "
        f"(which-NAS-is-primary → {results['supersession'][1]['judge_answer']})",
        f"• *Sparks + research produced*: *{sr[1]['sparks_produced']} sparks*, "
        f"*{sr[1]['research_memories']} research notes*, "
        f"{sr[1]['research_log_runs']} research runs "
        f"(later-access proxy {sr[1]['landing_proxy_total_later_access']})",
        "",
    ]
    # Honest note about ~0 baselines.
    if cb == 0.0:
        lines.append("_Callback rate is still ~0 — that's the baseline. The point is to "
                     "watch it climb as shared history accumulates and gets used._")
    return "\n".join([ln for ln in lines if ln is not None])


# ── monthly blinded eval (stub) ──────────────────────────────────────────────

def blinded_eval(mc, oc):
    """Present Jordan an anonymized Nova conversation, ask 1-5 'did she feel
    continuous?'. Stub: posts the snippet + lands a pending row. Capturing his
    reply is a documented TODO for the gateway to wire back."""
    mc.execute("SELECT id, text FROM memories WHERE source='conversation' "
               "ORDER BY random() LIMIT 1")
    row = mc.fetchone()
    if not row:
        _log("blinded-eval: no conversation memories to sample")
        return 1
    mid, text = row
    # Anonymize: strip names to neutral roles.
    anon = re.sub(r"\bJordan\b", "USER", text)
    anon = re.sub(r"\bNova\b", "ASSISTANT", anon)
    snippet = anon[:1200]

    msg = ("🧪 *Monthly blinded eval* — no names, no context.\n"
           "On a scale of *1-5*, did this assistant feel like a continuous mind that "
           "remembers you (5) or a stateless chatbot (1)? Reply with just the number.\n\n"
           f"```\n{snippet}\n```\n"
           "_(Your reply is recorded to the Turing scoreboard.)_")
    if "--dry-run" not in sys.argv:
        nova_config.post_both(msg, slack_channel=nova_config.SLACK_CHAN)
        _store(oc, "blinded_eval_pending", None,
               {"memory_id": str(mid), "snippet": snippet, "posted_at": datetime.now().isoformat(),
                "TODO": "gateway should capture Jordan's 1-5 reply and INSERT metric="
                        "'blinded_eval_score' value=<n> detail={memory_id}"})
    else:
        _log("DRY RUN blinded-eval would post:\n" + msg)
    return 0


def main():
    if "--blinded-eval" in sys.argv:
        mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
        ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
        return blinded_eval(mc, oc)

    dry = "--dry-run" in sys.argv
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()

    _log("computing callback rate…")
    cb = metric_callback_rate(mc, oc)
    _log(f"  callback_rate={cb[0]:.4f} ({cb[1]['apt']}/{cb[1]['turns_judged']})")

    _log("timing recall latency…")
    lat = metric_recall_latency(oc)
    _log(f"  recall_latency_ms={lat[0]}")

    _log("checking supersession…")
    sup = metric_supersession(oc)
    _log(f"  supersession={'PASS' if sup[0] else 'FAIL'} (judge={sup[1]['judge_answer']})")

    _log("measuring spark/research landing…")
    sr = metric_spark_research(mc, oc)
    _log(f"  produced={sr[0]} (sparks={sr[1]['sparks_produced']}, research={sr[1]['research_memories']})")

    results = {"callback": cb, "latency": lat, "supersession": sup, "spark_research": sr}
    report = weekly_report(oc, results)

    if dry:
        _log("DRY RUN — would post report:\n" + report)
    else:
        nova_config.post_both(report, slack_channel=REPORT_CHANNEL)
        _log("posted weekly scoreboard to Slack")

    print("\n----- SCOREBOARD REPORT -----\n" + report + "\n-----------------------------")
    return 0


if __name__ == "__main__":
    sys.exit(main())
