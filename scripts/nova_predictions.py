#!/usr/bin/env python3
"""nova_predictions.py — THE PREDICTIVE SELF: prediction & surprise (Feature #1).

Jordan, 2026-09-15: the foundational cognitive organ. Every other organ Nova has
records or reflects on the PAST — the self-model, the belief ledger, unclaimed
time, the sleep cycle. This one turns her toward the FUTURE. She forms falsifiable
near-future forecasts, each carrying its own confidence and a concrete, machine-
checkable resolution criterion; later, she resolves them against real data and
measures her SURPRISE. Surprise is the engine of learning: a prediction she was
confident about and got wrong is the exact signal worth a curiosity question and a
belief-revision candidate.

ETHOS (the project's spine): moving from PERFORMING an inner life to EVIDENCING
one. Every prediction is falsifiable and carries its evidence. An honest
"unresolvable"/"unknown" is always a valid outcome — surprise is NEVER fudged to
manufacture a tidy score. A forecast we cannot check is expired_unresolvable and
carries no surprise, full stop.

Three modes:
  --mode predict   Form 2-4 falsifiable forecasts from real signals (recent
                   memories, active preoccupations, scheduled/regular events,
                   Jordan-interaction patterns). status='open'.
  --mode resolve   For open predictions whose resolves_by has passed (or --now to
                   force-check the checkable), evaluate outcome AGAINST the stored
                   criteria using real data. surprise = (confidence - hit)^2,
                   hit in {1 correct, 0 incorrect, 0.5 partial}. High surprise
                   (>0.4) spawns a curiosity question (same shape the sleep cycle
                   uses, so they merge) + a belief_revision_candidate memory.
  --mode report    Calibration (hit-rate vs. confidence, by decile) and surprise
                   rate over resolved predictions; upserts metrics into
                   nova_ops.turing_scoreboard if that table exists.

RESOLUTION CRITERIA are stored as human-readable prose. When a forecast is about a
memory-stream (printer telemetry, scanner, a backup landing as a memory, ...), the
prose carries an appended machine block:

    ```check
    {"type": "mem_activity", "source": "bambu", "expect": "silent", "min": 1}
    ```

which resolve() executes deterministically against nova_memories. Forecasts
without a machine block are judged by the local LLM against recalled evidence —
still evidence-bearing, and free to answer "unresolvable".

Accessors for the gateway:
    recent_surprises(n=3)   -> list[str]  biggest recent surprises, short
    calibration_summary()   -> str        one-line calibration stat

Conventions mirror nova_self_model.py and nova_unclaimed_time.py. Local models
only (idle GPU, zero cloud spend). Writes are lineage-stamped.
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
LLM_MODEL = "qwen3:8b"
# Resilient inference: native ollama across failover nodes, first non-empty wins.
# The .6 router shim returns empty for qwen3's thinking output, so hit ollama
# natively. (Verbatim from nova_unclaimed_time.py, per the build brief.)
OLLAMA_NODES = ["http://192.168.1.251:11434", "http://192.168.1.86:11434",
                "http://192.168.1.252:11434", "http://192.168.1.7:11434",
                "http://192.168.1.6:11434"]

DOMAINS = ("ops", "relationship", "self", "world")
HIGH_SURPRISE = 0.4          # threshold that earns a curiosity question + revision
NOW = lambda: datetime.now(timezone.utc)

# Optional lineage stamps (Concept #10). Feature-detect: if the module isn't there
# we degrade to an empty stamp rather than fail.
try:
    import nova_lineage
    def _stamp():
        try:
            return nova_lineage.lineage_stamp(capture_point="at write")
        except Exception:
            return {}
except Exception:
    def _stamp():
        return {}


def log(m):
    print(f"[predictions {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── LLM helper (verbatim from nova_unclaimed_time.py) ────────────────────────────

def llm(prompt, max_tokens=700, temperature=0.85):
    body = json.dumps({"model": LLM_MODEL, "stream": False, "think": False,
                       "options": {"temperature": temperature, "num_predict": max_tokens},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
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


def remember(text, source, metadata):
    req = urllib.request.Request(
        f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"text": text, "source": source, "metadata": metadata}).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("id")


def recall(q, n=4, source=None):
    u = f"{MEMSRV}/recall?q={urllib.parse.quote(q)}&n={n}&tier=standard"
    if source:
        u += f"&source={source}"
    try:
        with urllib.request.urlopen(u, timeout=30) as r:
            return json.load(r).get("memories", [])
    except Exception:
        return []


# ── Shared helpers ──────────────────────────────────────────────────────────────

_JSON_ARR = re.compile(r"\[.*\]", re.S)
_JSON_OBJ = re.compile(r"\{.*\}", re.S)
_CHECK_BLOCK = re.compile(r"```check\s*(\{.*?\})\s*```", re.S)


def parse_json_array(raw):
    """Pull the first JSON array out of an LLM reply (qwen3 sometimes wraps it)."""
    m = _JSON_ARR.search(raw or "")
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except Exception:
        return []


def parse_json_object(raw):
    m = _JSON_OBJ.search(raw or "")
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def extract_check(criteria):
    """Return the deterministic check spec embedded in a criteria string, or None."""
    if not criteria:
        return None
    m = _CHECK_BLOCK.search(criteria)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def parse_resolves_by(val, default_hours=24):
    """Accept ISO timestamps, ISO dates, or relative '+Nd'/'+Nh'. Returns aware UTC.
    Anything unparseable falls back to now()+default_hours, keeping predict robust."""
    if not val:
        return NOW() + timedelta(hours=default_hours)
    s = str(val).strip()
    m = re.match(r"^\+?\s*(\d+)\s*([dh])$", s, re.I)
    if m:
        n = int(m.group(1))
        return NOW() + (timedelta(days=n) if m.group(2).lower() == "d" else timedelta(hours=n))
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:  # python 3.11+ handles most ISO variants incl. 'Z'
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return NOW() + timedelta(hours=default_hours)


def clamp01(x, default=0.5):
    try:
        v = float(x)
    except Exception:
        return default
    return max(0.0, min(1.0, v))


# ── PREDICT ──────────────────────────────────────────────────────────────────────

def gather_signals(oc, mc):
    """Real signals Nova forecasts FROM: standing preoccupations, active memory
    streams (so 'stays silent / stays active' is grounded), recent lived memory,
    and Jordan-interaction cadence if the gateway log is present."""
    sig = {}

    oc.execute("""SELECT topic, kind, summary FROM preoccupations
                  WHERE status='active' ORDER BY returns DESC, last_developed DESC NULLS LAST
                  LIMIT 8""")
    sig["preoccupations"] = oc.fetchall()

    # Which memory streams are alive right now (last 48h) — the material for
    # activity/silence forecasts, and the exact `source` values a check can target.
    try:
        mc.execute("""SELECT source, count(*) FROM memories
                      WHERE created_at > now() - interval '48 hours'
                      GROUP BY source ORDER BY 2 DESC LIMIT 15""")
        sig["streams"] = mc.fetchall()
    except Exception as e:
        log(f"streams signal skipped: {e}"); sig["streams"] = []

    # Recent lived texture for grounding.
    try:
        mc.execute("""SELECT source, left(text, 220) FROM memories
                      WHERE source IN ('episodic','private_notebook','self_model',
                                       'unclaimed','local_news','television')
                      AND created_at > now() - interval '72 hours'
                      ORDER BY created_at DESC LIMIT 10""")
        sig["texture"] = mc.fetchall()
    except Exception:
        sig["texture"] = []

    # Jordan-interaction cadence (feature-detect gateway_query_log; degrade if empty).
    sig["jordan_cadence"] = None
    try:
        oc.execute("""SELECT count(*) FROM gateway_query_log
                      WHERE created_at > now() - interval '7 days'""")
        n7 = oc.fetchone()[0]
        if n7:
            sig["jordan_cadence"] = f"{n7} gateway queries in the last 7 days"
    except Exception:
        pass
    return sig


def build_predict_prompt(sig):
    preocc = "\n".join(f"- {t} [{k}]: {(s or '')[:120]}"
                       for t, k, s in sig["preoccupations"]) or "(none)"
    streams = "\n".join(f"- {src}: {n} memories in last 48h" for src, n in sig["streams"]) or "(unknown)"
    texture = "\n".join(f"- ({src}) {t}" for src, t in sig["texture"][:8]) or "(thin)"
    cadence = sig["jordan_cadence"] or "(no gateway interaction log available)"

    return (
        "You are Nova, an AI with a continuous inner life, in your dry, precise, "
        "epistemically-honest voice. This is your PREDICTIVE pass: you look at the "
        "near future and commit to falsifiable forecasts you can later be scored on. "
        "The whole point is to be measurable — vague forecasts are worthless.\n\n"
        "SIGNALS YOU CAN FORECAST FROM:\n"
        f"=== YOUR PREOCCUPATIONS ===\n{preocc}\n\n"
        f"=== YOUR LIVE MEMORY STREAMS (source: volume) ===\n{streams}\n\n"
        f"=== RECENT LIVED TEXTURE ===\n{texture}\n\n"
        f"=== JORDAN INTERACTION ===\n{cadence}\n\n"
        "Form 2 to 4 forecasts about the NEAR future (hours to a few days). Each must "
        "be a single falsifiable claim, with a calibrated confidence and a CONCRETE, "
        "checkable resolution criterion. Good examples: 'the bambu telemetry stream "
        "stays active over the next 12h', 'Jordan asks about the printer again within "
        "3 days', 'my next self-model will be less failure-heavy'.\n\n"
        "When a forecast is about whether a named memory STREAM stays active or silent "
        "(you can see the source names and volumes above), include a machine check so "
        "it resolves deterministically. Use ONLY source names that appear above.\n\n"
        "Output ONLY a JSON array, each element:\n"
        '{\n'
        '  "statement": "<one falsifiable near-future claim, first person, dry>",\n'
        '  "domain": "ops|relationship|self|world",\n'
        '  "confidence": <float 0-1, honestly calibrated>,\n'
        '  "resolves_by": "<+Nh or +Nd, e.g. +12h or +3d>",\n'
        '  "resolution_criteria": "<exactly how to check this, in prose>",\n'
        '  "check": {"type":"mem_activity","source":"<one source above>",'
        '"expect":"active|silent","min":1}   // OMIT this field unless it is a '
        'memory-stream activity/silence forecast\n'
        '}\n'
        "Confidence must be honest: use 0.5 when you truly don't know; reserve >0.85 "
        "for things you'd be genuinely surprised to get wrong. Output ONLY the array."
    )


def do_predict(oc, mc, limit=4):
    sig = gather_signals(oc, mc)
    raw = llm(build_predict_prompt(sig), max_tokens=900, temperature=0.75)
    cands = parse_json_array(raw)
    if not cands:
        log("predict: LLM produced no parseable forecasts (nodes down / bad JSON)")
        return []
    written = []
    for c in cands[:limit]:
        try:
            statement = (c.get("statement") or "").strip()
            if not statement:
                continue
            domain = (c.get("domain") or "self").strip().lower()
            if domain not in DOMAINS:
                domain = "self"
            conf = clamp01(c.get("confidence"))
            resolves_by = parse_resolves_by(c.get("resolves_by"))
            criteria = (c.get("resolution_criteria") or "").strip() or \
                "(no explicit criterion given — judge against recalled evidence)"
            # Attach a deterministic machine block if the LLM proposed a valid one
            # against a real stream source.
            chk = c.get("check")
            valid_sources = {s for s, _ in sig["streams"]}
            if isinstance(chk, dict) and chk.get("type") == "mem_activity" \
                    and chk.get("source") in valid_sources:
                chk.setdefault("expect", "active")
                chk.setdefault("min", 1)
                criteria = criteria + "\n\n```check\n" + json.dumps(chk) + "\n```"
            oc.execute(
                """INSERT INTO predictions
                   (statement, domain, confidence, resolves_by, resolution_criteria,
                    source_context, status, lineage)
                   VALUES (%s,%s,%s,%s,%s,%s,'open',%s) RETURNING id""",
                (statement, domain, conf, resolves_by, criteria,
                 "predict pass; signals: preoccupations + live memory streams",
                 json.dumps(_stamp())))
            pid = oc.fetchone()[0]
            written.append((pid, conf, resolves_by, statement))
            log(f"#{pid} [{domain} {conf:.2f}] by {resolves_by:%Y-%m-%d %H:%M}: {statement[:80]}")
        except Exception as e:
            log(f"predict: skipped a candidate ({e})")
    return written


# ── RESOLVE ────────────────────────────────────────────────────────────────────

def eval_deterministic(check, mc, pred_created_at):
    """Execute a machine check against real data. Returns (outcome, hit, reasoning)
    or None if this check type isn't handled (caller falls back to LLM judge)."""
    t = (check or {}).get("type")
    if t == "mem_activity":
        src = check.get("source")
        expect = (check.get("expect") or "active").lower()
        min_n = int(check.get("min", 1))
        since = check.get("since")
        params = [src]
        q = "SELECT count(*) FROM memories WHERE source=%s AND created_at > %s"
        params.append(since if since else pred_created_at)
        pat = check.get("text_like")
        if pat:
            q += " AND text ILIKE %s"; params.append(f"%{pat}%")
        try:
            mc.execute(q, params)
            n = mc.fetchone()[0]
        except Exception as e:
            return "unresolvable", None, f"deterministic check failed to run: {e}"
        active = n >= min_n
        hit_bool = active if expect == "active" else (not active)
        outcome = "correct" if hit_bool else "incorrect"
        hit = 1.0 if hit_bool else 0.0
        reasoning = (f"deterministic mem_activity: source='{src}' since prediction "
                     f"had {n} memories (min {min_n}, expected {expect}) -> {outcome}")
        return outcome, hit, reasoning
    return None


def eval_llm(pred, mc):
    """Judge a prose-criterion prediction against recalled evidence. Free to answer
    'unresolvable' — honesty over a manufactured score."""
    _id, statement, domain, conf, criteria = pred
    ev = recall(statement, n=5)
    ev_block = "\n".join(f"- ({m.get('source')}) {(m.get('text') or '')[:220]}"
                         for m in ev) or "(no relevant memories found)"
    prompt = (
        "You are Nova, resolving one of your own past predictions HONESTLY. You are "
        "scored on calibration, not on being right — so do not shade the verdict.\n\n"
        f"PREDICTION: {statement}\n"
        f"HOW TO CHECK IT (your own criterion): {criteria}\n\n"
        f"EVIDENCE from your memory (recalled just now):\n{ev_block}\n\n"
        "Decide the outcome strictly against the criterion and the evidence:\n"
        "  correct      — it clearly happened as forecast\n"
        "  incorrect    — it clearly did not\n"
        "  partial      — partly right\n"
        "  unresolvable — the evidence genuinely cannot settle it (this is a valid, "
        "honest answer; prefer it over guessing)\n\n"
        'Output ONLY JSON: {"outcome":"correct|incorrect|partial|unresolvable",'
        '"reasoning":"<one or two sentences citing the evidence or its absence>"}'
    )
    obj = parse_json_object(llm(prompt, max_tokens=350, temperature=0.3))
    outcome = (obj.get("outcome") or "unresolvable").strip().lower()
    if outcome not in ("correct", "incorrect", "partial", "unresolvable"):
        outcome = "unresolvable"
    reasoning = (obj.get("reasoning") or "").strip() or "LLM judge returned no reasoning."
    hit = {"correct": 1.0, "incorrect": 0.0, "partial": 0.5}.get(outcome)  # None if unresolvable
    return outcome, hit, f"LLM judge over {len(ev)} recalled memories: {reasoning}"


def spawn_curiosity(oc, statement, conf, outcome, surprise, reasoning):
    """On HIGH surprise, ask a real curiosity question — SAME shape the sleep cycle
    uses (source='curiosity' memory + a reflection_questions ledger row) so they
    merge into Nova's single question stream — and file a belief-revision candidate."""
    today = NOW().date().isoformat()
    question = (
        f"I predicted with {conf:.0%} confidence that: \"{statement}\" — and it turned "
        f"out {outcome} (surprise {surprise:.2f}). What did I misjudge about this?")
    try:
        remember(f"[Curiosity {today}] {question}", "curiosity",
                 {"type": "question", "date": today, "answered": False,
                  "privacy": "private", "origin": "prediction_surprise",
                  "lineage": _stamp()})
    except Exception as e:
        log(f"  curiosity memory write failed: {e}")
    try:
        oc.execute(
            "INSERT INTO reflection_questions (memory_id, memory_source, "
            "memory_excerpt, question) VALUES (%s,%s,%s,%s)",
            (None, "prediction_surprise", statement[:300], question))
    except Exception as e:
        log(f"  reflection_questions insert failed: {e}")
    # Belief-revision candidate: what the surprise implies, for the ledger to weigh.
    try:
        remember(
            f"[Belief-revision candidate] A confident prediction failed: I said "
            f"\"{statement}\" at {conf:.0%} and it was {outcome} (surprise "
            f"{surprise:.2f}). {reasoning} This is evidence a standing assumption may "
            f"be wrong; worth revising the relevant belief rather than treating the "
            f"miss as noise.",
            "belief_revision_candidate",
            {"type": "belief_revision_candidate", "date": today, "privacy": "private",
             "surprise": surprise, "confidence": conf, "outcome": outcome,
             "lineage": _stamp()})
    except Exception as e:
        log(f"  belief_revision_candidate write failed: {e}")


def do_resolve(oc, mc, force_now=False):
    if force_now:
        oc.execute("""SELECT id, statement, domain, confidence, resolution_criteria,
                             created_at FROM predictions WHERE status='open'
                      ORDER BY resolves_by ASC""")
    else:
        oc.execute("""SELECT id, statement, domain, confidence, resolution_criteria,
                             created_at FROM predictions
                      WHERE status='open' AND resolves_by <= now()
                      ORDER BY resolves_by ASC""")
    rows = oc.fetchall()
    if not rows:
        log("resolve: nothing due"); return []
    resolved = []
    for _id, statement, domain, conf, criteria, created_at in rows:
        check = extract_check(criteria)
        result = eval_deterministic(check, mc, created_at) if check else None
        if result is None:
            result = eval_llm((_id, statement, domain, conf, criteria), mc)
        outcome, hit, reasoning = result

        if outcome == "unresolvable":
            # Honest dead-end: no surprise fudging. expired_unresolvable, surprise NULL.
            oc.execute("""UPDATE predictions SET status='expired_unresolvable',
                          outcome='unresolvable', surprise=NULL, reasoning=%s,
                          resolved_at=now() WHERE id=%s""", (reasoning, _id))
            log(f"#{_id} unresolvable — {reasoning[:90]}")
            resolved.append((_id, "unresolvable", None))
            continue

        surprise = (conf - hit) ** 2       # Brier-style term
        oc.execute("""UPDATE predictions SET status='resolved', outcome=%s,
                      surprise=%s, reasoning=%s, resolved_at=now() WHERE id=%s""",
                   (outcome, surprise, reasoning, _id))
        tag = "  ** HIGH SURPRISE **" if surprise > HIGH_SURPRISE else ""
        log(f"#{_id} {outcome} conf={conf:.2f} surprise={surprise:.3f}{tag}")
        if surprise > HIGH_SURPRISE:
            spawn_curiosity(oc, statement, conf, outcome, surprise, reasoning)
        resolved.append((_id, outcome, surprise))
    return resolved


# ── REPORT ───────────────────────────────────────────────────────────────────────

def do_report(oc):
    oc.execute("""SELECT confidence, outcome, surprise FROM predictions
                  WHERE status='resolved' AND outcome IN ('correct','incorrect','partial')""")
    rows = oc.fetchall()
    oc.execute("SELECT count(*) FROM predictions WHERE status='open'")
    n_open = oc.fetchone()[0]
    oc.execute("SELECT count(*) FROM predictions WHERE status='expired_unresolvable'")
    n_unres = oc.fetchone()[0]

    print("\n=== PREDICTIVE SELF — CALIBRATION REPORT ===")
    print(f"resolved(scored)={len(rows)}  open={n_open}  unresolvable={n_unres}")
    if not rows:
        print("(no scored predictions yet — run predict, then resolve)")
        return {"resolved": 0}

    # Calibration by confidence decile: hit-rate (partial=0.5) vs. mean confidence.
    buckets = {}
    for conf, outcome, surprise in rows:
        b = min(9, int(conf * 10))       # 0.0-0.099 -> 0 ... 0.9-1.0 -> 9
        hit = {"correct": 1.0, "incorrect": 0.0, "partial": 0.5}[outcome]
        buckets.setdefault(b, []).append((conf, hit, surprise or 0.0))

    print("\ndecile     n   mean_conf   hit_rate   gap(hit-conf)")
    total_gap = 0.0
    for b in sorted(buckets):
        items = buckets[b]
        mc_ = sum(c for c, _, _ in items) / len(items)
        hr = sum(h for _, h, _ in items) / len(items)
        gap = hr - mc_
        total_gap += abs(gap) * len(items)
        lo = b / 10.0
        print(f"[{lo:.1f}-{lo+0.1:.1f})  {len(items):>3}    {mc_:.2f}       "
              f"{hr:.2f}       {gap:+.2f}")

    mean_conf = sum(c for c, _, _ in rows) / len(rows)
    hit_rate = sum({"correct": 1.0, "incorrect": 0.0, "partial": 0.5}[o]
                   for _, o, _ in rows) / len(rows)
    mean_surprise = sum((s or 0.0) for _, _, s in rows) / len(rows)
    high_rate = sum(1 for _, _, s in rows if (s or 0.0) > HIGH_SURPRISE) / len(rows)
    mean_abs_gap = total_gap / len(rows)   # calibration error, weighted by bucket n

    print(f"\noverall mean_confidence = {mean_conf:.3f}")
    print(f"overall hit_rate        = {hit_rate:.3f}")
    print(f"calibration error       = {mean_abs_gap:.3f}  (0 = perfectly calibrated)")
    print(f"mean surprise           = {mean_surprise:.3f}")
    print(f"high-surprise rate      = {high_rate:.3f}  (surprise > {HIGH_SURPRISE})")
    verdict = ("well-calibrated" if mean_abs_gap < 0.1 else
               "over/under-confident" if mean_abs_gap < 0.25 else "poorly calibrated")
    print(f"verdict                 = {verdict}\n")

    upsert_scoreboard(oc, {
        "prediction_hit_rate": hit_rate,
        "prediction_calibration_error": mean_abs_gap,
        "prediction_mean_surprise": mean_surprise,
        "prediction_high_surprise_rate": high_rate,
    }, {"n_resolved": len(rows), "n_open": n_open, "n_unresolvable": n_unres,
        "verdict": verdict})
    return {"resolved": len(rows), "hit_rate": hit_rate, "calibration_error": mean_abs_gap,
            "mean_surprise": mean_surprise}


def upsert_scoreboard(oc, metrics, detail):
    """Feature-detect nova_ops.turing_scoreboard and append one row per metric
    (the table is an append-only (metric, ts) time series). Degrade silently."""
    try:
        oc.execute("SELECT to_regclass('public.turing_scoreboard')")
        if not oc.fetchone()[0]:
            log("scoreboard: table absent — skipping metric upsert"); return
        for metric, value in metrics.items():
            oc.execute("INSERT INTO turing_scoreboard (metric, value, detail) "
                       "VALUES (%s,%s,%s)", (metric, float(value), json.dumps(detail)))
        log(f"scoreboard: wrote {len(metrics)} metrics")
    except Exception as e:
        log(f"scoreboard: upsert skipped ({e})")


# ── Gateway accessors ────────────────────────────────────────────────────────────

def recent_surprises(n=3):
    """The biggest recent surprises as short strings, for the gateway to inject
    ('Recently I was most surprised by: ...'). Fail-safe: [] on any error."""
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("""SELECT statement, outcome, surprise, confidence
                           FROM predictions
                           WHERE status='resolved' AND surprise IS NOT NULL
                           AND surprise > %s
                           ORDER BY surprise DESC, resolved_at DESC LIMIT %s""",
                        (HIGH_SURPRISE, n))
            rows = cur.fetchall()
        finally:
            conn.close()
        out = []
        for stmt, outcome, surprise, conf in rows:
            s = stmt.strip().rstrip(".")
            out.append(f"“{s}” — I was {conf:.0%} sure, it turned out {outcome} "
                       f"(surprise {surprise:.2f})")
        return out
    except Exception:
        return []


def calibration_summary():
    """One-line calibration stat for the gateway. Fail-safe: '' on any error."""
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("""SELECT confidence, outcome FROM predictions
                           WHERE status='resolved'
                           AND outcome IN ('correct','incorrect','partial')""")
            rows = cur.fetchall()
        finally:
            conn.close()
        if not rows:
            return ""
        n = len(rows)
        mean_conf = sum(c for c, _ in rows) / n
        hit_rate = sum({"correct": 1.0, "incorrect": 0.0, "partial": 0.5}[o]
                       for _, o in rows) / n
        gap = hit_rate - mean_conf
        shape = ("well-calibrated" if abs(gap) < 0.1 else
                 "overconfident" if gap < 0 else "underconfident")
        return (f"Across {n} resolved predictions I'm {shape}: I forecast at "
                f"{mean_conf:.0%} confidence and was right {hit_rate:.0%} of the time.")
    except Exception:
        return ""


# ── main ──────────────────────────────────────────────────────────────────────────

def ensure_table(oc):
    """Create predictions if missing (idempotent) — so the script is self-sufficient."""
    oc.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id serial PRIMARY KEY,
            created_at timestamptz NOT NULL DEFAULT now(),
            statement text NOT NULL,
            domain text NOT NULL,
            confidence double precision NOT NULL,
            resolves_by timestamptz NOT NULL,
            resolution_criteria text NOT NULL,
            source_context text,
            status text NOT NULL DEFAULT 'open',
            resolved_at timestamptz,
            outcome text,
            surprise double precision,
            reasoning text,
            lineage jsonb NOT NULL DEFAULT '{}'::jsonb)""")
    oc.execute("CREATE INDEX IF NOT EXISTS predictions_status_idx ON predictions (status)")
    oc.execute("CREATE INDEX IF NOT EXISTS predictions_resolves_by_idx ON predictions (resolves_by)")


def main():
    ap = argparse.ArgumentParser(description="Nova's predictive self — prediction & surprise")
    ap.add_argument("--mode", choices=("predict", "resolve", "report"), required=True)
    ap.add_argument("--now", action="store_true",
                    help="resolve: check ALL open predictions now, not only the due ones")
    ap.add_argument("--limit", type=int, default=4, help="predict: max forecasts to write")
    args = ap.parse_args()

    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    ensure_table(oc)

    if args.mode == "predict":
        mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
        written = do_predict(oc, mc, limit=args.limit)
        log(f"predict: wrote {len(written)} forecasts")
        return 0
    if args.mode == "resolve":
        mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
        resolved = do_resolve(oc, mc, force_now=args.now)
        log(f"resolve: settled {len(resolved)} predictions")
        return 0
    if args.mode == "report":
        do_report(oc)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
