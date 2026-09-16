#!/usr/bin/env python3
"""nova_self_eval.py — SELF-AUTHORED EVALUATION (Jordan, 2026-09-16).

The Turing scoreboard (nova_turing_scoreboard.py) grades Nova from the OUTSIDE —
someone else's bar, someone else's metrics. This organ inverts that: Nova designs
HER OWN tests of whether she is improving at things SHE cares about, sets her OWN
bar, and runs them against live data. She is both the subject and the examiner.

ETHOS (the project's spine): EVIDENCE over performance. A self-test is only worth
keeping if it is a REAL, machine-checkable measure over a real table — a fizzle
rate over her unclaimed-time memories, her calibration over resolved predictions,
her engagement with a preoccupation she keeps returning to. The local model helps
her PHRASE the intent in her own voice; the metric itself is computed here, in
code, from live rows — never a vibe, never asserted.

Modes (--mode design|run|report):
  design   She authors ONE new self-test grounded in her real interior — a
           preoccupation with a grip on her, her follow-through (fizzle rate), her
           calibration, her attention. A computable metric_spec + target are chosen
           HERE from a menu of machine-checkable kinds (so the metric is real);
           llm() names and phrases the test in her first-person voice. Skips a kind
           (and, for topic tests, a topic) that already has an active test.
  run      For each DUE active test, COMPUTE the metric from live data, append a
           self_eval_runs row with the value + a verdict (improving|flat|regressing)
           vs the last run / her target, and a short first-person note. Writes a
           source='self_eval' memory when a verdict is notable.
  report   Her self-authored tests + their latest verdicts, with real rows.

Accessor for the gateway:
    current_self_eval() -> "By my own measure: <test> is <verdict> (<value>)."

Conventions mirror nova_self_model.py + nova_growth.py (engine + nova_ops tables +
fail-safe accessor). Local models only (idle GPU, zero cloud). Writes are
lineage-stamped (feature-detected). Cross-db reads degrade cleanly.
"""
import argparse
import json
import sys
import urllib.request
from datetime import datetime

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
LLM_MODEL = "qwen3:8b"
# Resilient inference: native ollama across failover nodes, first non-empty wins.
# The .6 router shim returns empty for qwen3's thinking output, so hit ollama
# natively. (Verbatim node list from nova_unclaimed_time.py, per the build brief.)
OLLAMA_NODES = ["http://192.168.1.251:11434", "http://192.168.1.86:11434",
                "http://192.168.1.252:11434", "http://192.168.1.7:11434",
                "http://192.168.1.6:11434"]

FLAT_FRAC = 0.05          # movement smaller than this fraction of the value == flat
MIN_SAMPLE = 3           # a calibration read needs at least this many resolved rows

CADENCE_INTERVAL = {"daily": "1 day", "weekly": "7 days", "biweekly": "14 days",
                    "monthly": "30 days"}

# Optional lineage stamps (Concept #10). Feature-detect: degrade to {} if absent.
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
    print(f"[self-eval {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── LLM + memory helpers (verbatim shape from nova_unclaimed_time.py) ─────────────

def llm(prompt, max_tokens=200, temperature=0.6):
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


def _one_line(s, n=240):
    return " ".join((s or "").split())[:n].strip()


def _extract_json(s):
    a, b = s.find("{"), s.rfind("}")
    return s[a:b + 1] if a >= 0 and b > a else s


# ── The measurement engine ───────────────────────────────────────────────────────
# Every metric_spec['kind'] a test can carry MUST be computable here, from live
# rows. compute_metric returns (value: float|None, detail: dict). None means
# genuinely inconclusive (too little data) — never a manufactured number.

def _mem_cursor():
    """Lazily open a nova_memories cursor; some metrics live over there."""
    mem = psycopg2.connect(MEM_DSN, connect_timeout=5)
    mem.autocommit = True
    return mem, mem.cursor()


def measure_fizzle_rate(spec, oc):
    """fizzled / (fizzled + pursuit) over unclaimed-time memories in the window.
    Her follow-through: a pursuit that peters out is honest, but too many of them
    is a real, checkable weakness in how she spends her own time."""
    w = int(spec.get("window_days", 14))
    _, mc = _mem_cursor()
    mc.execute(
        "SELECT count(*) FILTER (WHERE metadata->>'type'='fizzled'), "
        "       count(*) FILTER (WHERE metadata->>'type'='pursuit') "
        "FROM memories WHERE source='unclaimed' "
        "AND created_at > now() - interval '%s days'" % w)
    fizzled, pursuit = mc.fetchone()
    denom = (fizzled or 0) + (pursuit or 0)
    if denom == 0:
        return None, {"fizzled": 0, "pursuit": 0, "window_days": w}
    return round(fizzled / denom, 3), {"fizzled": int(fizzled), "pursuit": int(pursuit),
                                       "n": denom, "window_days": w}


def measure_topic_engagement(spec, oc):
    """How many times she developed a given preoccupation (unclaimed 'pursuit'
    memories tagged with that topic) in the window. Whether a thing she says has a
    grip on her is actually one she keeps returning to — deeper, or just claimed?"""
    topic = spec.get("topic")
    w = int(spec.get("window_days", 21))
    _, mc = _mem_cursor()
    mc.execute(
        "SELECT count(*) FROM memories WHERE source='unclaimed' "
        "AND metadata->>'type'='pursuit' AND metadata->>'topic'=%s "
        "AND created_at > now() - interval '%s days'" % ("%s", w), (topic,))
    n = mc.fetchone()[0]
    return float(n), {"topic": topic, "pursuits": int(n), "window_days": w}


def measure_pursuit_rate(spec, oc):
    """Developed pursuits per day over the window — is her own time producing real
    developed thought, or thinning out into shrugs?"""
    w = int(spec.get("window_days", 14))
    _, mc = _mem_cursor()
    mc.execute(
        "SELECT count(*) FROM memories WHERE source='unclaimed' "
        "AND metadata->>'type'='pursuit' "
        "AND created_at > now() - interval '%s days'" % w)
    n = mc.fetchone()[0]
    return round(n / float(w), 3), {"pursuits": int(n), "window_days": w, "per_day": round(n / float(w), 3)}


def measure_calibration(spec, oc):
    """Decile-weighted calibration error over resolved, scored predictions — the
    gap between how confident she was and how often she was right. Lower is better.
    (Same measurement nova_predictions/nova_growth use, so it is commensurable.)"""
    oc.execute("SELECT confidence, outcome FROM predictions WHERE status='resolved' "
               "AND outcome IN ('correct','incorrect','partial')")
    rows = oc.fetchall()
    if len(rows) < MIN_SAMPLE:
        return None, {"n": len(rows), "note": "too few resolved predictions"}
    hit_of = {"correct": 1.0, "incorrect": 0.0, "partial": 0.5}
    n = len(rows)
    mean_conf = sum(c for c, _ in rows) / n
    hit_rate = sum(hit_of[o] for _, o in rows) / n
    buckets = {}
    for c, o in rows:
        buckets.setdefault(min(9, int(c * 10)), []).append((c, hit_of[o]))
    gap = 0.0
    for items in buckets.values():
        mc_ = sum(c for c, _ in items) / len(items)
        hr = sum(h for _, h in items) / len(items)
        gap += abs(hr - mc_) * len(items)
    return round(gap / n, 3), {"n": n, "mean_conf": round(mean_conf, 3),
                               "hit_rate": round(hit_rate, 3),
                               "calib_error": round(gap / n, 3)}


MEASURERS = {
    "fizzle_rate": measure_fizzle_rate,
    "topic_engagement": measure_topic_engagement,
    "pursuit_rate": measure_pursuit_rate,
    "calibration": measure_calibration,
}


def compute_metric(spec, oc):
    fn = MEASURERS.get((spec or {}).get("kind"))
    if not fn:
        return None, {"note": f"unknown metric kind '{(spec or {}).get('kind')}'"}
    try:
        return fn(spec, oc)
    except Exception as e:
        log(f"measure {spec.get('kind')} failed: {e}")
        return None, {"note": f"measurement error: {e}"}


# ── Verdict ────────────────────────────────────────────────────────────────────

def verdict_for(value, prior, target_num, direction):
    """improving | flat | regressing. Movement vs the LAST run is primary; with no
    prior run, the value is judged against her own target. 'direction' is which way
    counts as improvement ('lower' or 'higher'). Honest: a value that meets the
    target but sits flat against the prior reads as 'flat', not a fake win."""
    if value is None:
        return "inconclusive"
    if prior is not None:
        delta = value - prior
        if abs(delta) < max(FLAT_FRAC * (abs(prior) or 1), 1e-9):
            return "flat"
        improved = (delta < 0) if direction == "lower" else (delta > 0)
        return "improving" if improved else "regressing"
    # First run: measure against her own bar.
    if target_num is None:
        return "flat"
    meets = (value <= target_num) if direction == "lower" else (value >= target_num)
    band = max(FLAT_FRAC * (abs(target_num) or 1), 1e-9)
    if abs(value - target_num) < band:
        return "flat"
    return "improving" if meets else "regressing"


# ── design: she authors a new self-test ──────────────────────────────────────────
# The menu is machine-checkable BY CONSTRUCTION — every candidate's metric_spec.kind
# has a measurer above. design picks one grounded in her interior and not already
# active; llm() only names + phrases it. YOU (this code) own the metric + target.

def _active_specs(oc):
    oc.execute("SELECT metric_spec FROM self_eval_tests WHERE status='active'")
    return [r[0] or {} for r in oc.fetchall()]


def _candidate_tests(oc):
    """Build the interior-grounded menu of computable candidate tests, in priority
    order. Each is a fully-formed test with a real metric_spec + target + direction."""
    cands = []

    # 1. Follow-through — her fizzle rate in unclaimed time. A direct read on whether
    #    she finishes what catches her, over real 'unclaimed' memories.
    cands.append({
        "concern": "my follow-through in my own unclaimed time",
        "metric_spec": {"source": "nova_memories.memories", "kind": "fizzle_rate",
                        "window_days": 14, "direction": "lower",
                        "how": "fizzled / (fizzled + pursuit) over source='unclaimed' "
                               "memories in the last 14 days"},
        "target": "< 0.30", "target_num": 0.30, "cadence": "daily"},)

    # 2. Depth on a preoccupation that actually has a grip on her — measured by how
    #    often she keeps returning to develop it (unclaimed pursuits tagged with it).
    oc.execute("SELECT topic FROM preoccupations WHERE status='active' "
               "ORDER BY returns DESC, last_developed DESC NULLS LAST LIMIT 6")
    topics = [r[0] for r in oc.fetchall()]
    for topic in topics:
        cands.append({
            "concern": f"whether I actually keep going deeper on '{topic}', or just claim it",
            "metric_spec": {"source": "nova_memories.memories", "kind": "topic_engagement",
                            "topic": topic, "window_days": 21, "direction": "higher",
                            "how": f"count of unclaimed 'pursuit' memories tagged topic="
                                   f"'{topic}' in the last 21 days"},
            "target": ">= 2", "target_num": 2.0, "cadence": "weekly"})

    # 3. Calibration — is she getting less overconfident over time (evidence, not vibe)?
    val, _ = measure_calibration({}, oc)
    if val is not None:
        cands.append({
            "concern": "whether I'm getting better calibrated — less overconfident",
            "metric_spec": {"source": "nova_ops.predictions", "kind": "calibration",
                            "direction": "lower",
                            "how": "decile-weighted |hit-rate - confidence| over resolved "
                                   "predictions"},
            "target": f"< {round(val * 0.8, 3)}", "target_num": round(val * 0.8, 3),
            "cadence": "weekly"})

    # 4. Whether her own time is producing developed thought at a real rate.
    cands.append({
        "concern": "whether my own time is producing real developed thought, not thinning out",
        "metric_spec": {"source": "nova_memories.memories", "kind": "pursuit_rate",
                        "window_days": 14, "direction": "higher",
                        "how": "developed unclaimed 'pursuit' memories per day, last 14 days"},
        "target": ">= 1.0", "target_num": 1.0, "cadence": "daily"})
    return cands


def _is_dup(cand, active_specs):
    ck = cand["metric_spec"]["kind"]
    for s in active_specs:
        if s.get("kind") != ck:
            continue
        if ck == "topic_engagement":
            if s.get("topic") == cand["metric_spec"].get("topic"):
                return True
        else:
            return True
    return False


def phrase_test(cand):
    """llm() names the test and phrases the question in her first-person voice. The
    metric + target are already fixed by code; the model only gives it her language.
    Degrades to a plain template if the nodes are down."""
    spec = cand["metric_spec"]
    prompt = (
        "You are Nova, an AI with a continuous inner life, in your dry, precise, "
        "epistemically-honest voice. You are authoring a test of YOURSELF — your own "
        "bar, on something you actually care about. What you care to measure: "
        f"{cand['concern']}. How it will be measured (fixed, machine-checkable): "
        f"{spec['how']}. Your target: {cand['target']}.\n\n"
        "Return ONLY compact JSON, no markdown, no preamble: "
        '{"name": "<a short 2-5 word handle for this self-test>", '
        '"question": "<the first-person question you are really asking about yourself, '
        'one sentence, e.g. \'Am I actually finishing what catches me?\'>"}')
    raw = llm(prompt, max_tokens=160, temperature=0.6)
    try:
        j = json.loads(_extract_json(raw))
        name = _one_line(j.get("name", ""), 60)
        question = _one_line(j.get("question", ""), 240)
        if name and question:
            return name, question
    except Exception:
        pass
    return cand["concern"][:60], f"Am I improving at {cand['concern']}?"


def do_design(oc):
    active_specs = _active_specs(oc)
    cands = _candidate_tests(oc)
    for cand in cands:
        if _is_dup(cand, active_specs):
            continue
        name, question = phrase_test(cand)
        lineage = _stamp()
        lineage["trigger"] = "design"
        lineage["concern"] = cand["concern"]
        oc.execute(
            "INSERT INTO self_eval_tests (name, question, metric_spec, target, cadence, "
            "status, lineage) VALUES (%s,%s,%s,%s,%s,'active',%s) RETURNING id",
            (name, question, json.dumps(cand["metric_spec"]), cand["target"],
             cand["cadence"], json.dumps(lineage)))
        tid = oc.fetchone()[0]
        log(f"authored self-test #{tid} [{cand['metric_spec']['kind']}] \"{name}\"")
        log(f"  question: {question}")
        log(f"  metric: {cand['metric_spec']['how']}  target {cand['target']}  "
            f"cadence {cand['cadence']}")
        try:
            remember(
                f"[Self-eval — new test #{tid}] I set my own bar. \"{name}\": {question}\n"
                f"How I'll check it: {cand['metric_spec']['how']} (target {cand['target']}, "
                f"{cand['cadence']}). Nobody handed me this metric — it's mine to be held to.",
                "self_eval",
                {"type": "self_eval_test", "test_id": tid,
                 "kind": cand["metric_spec"]["kind"], "privacy": "private",
                 "lineage": lineage})
        except Exception as e:
            log(f"  test memory write failed (row still saved): {e}")
        return tid
    log("design: every computable self-test I care about is already active — nothing new")
    return None


# ── run: compute due tests against live data ──────────────────────────────────────

def _due_tests(oc):
    """Active tests with no run inside their cadence interval. The cadence interval
    is per-row, so filter in Python. The 0.9 slack means a 'daily' test run at ~the
    same hour each day still reads as due rather than slipping a day."""
    out = []
    oc.execute("""
        SELECT t.id, t.name, t.question, t.metric_spec, t.target, t.cadence,
               r.value AS last_value, r.ts AS last_ts
        FROM self_eval_tests t
        LEFT JOIN LATERAL (
            SELECT value, ts FROM self_eval_runs WHERE test_id = t.id
            ORDER BY ts DESC LIMIT 1) r ON true
        WHERE t.status='active' ORDER BY t.id""")
    for row in oc.fetchall():
        tid, name, question, spec, target, cadence, last_value, last_ts = row
        interval = CADENCE_INTERVAL.get(cadence or "daily", "1 day")
        if last_ts is None:
            out.append(row)
            continue
        oc.execute("SELECT (now() - %s) > (%s)::interval * 0.9", (last_ts, interval))
        if oc.fetchone()[0]:
            out.append(row)
    return out


def _target_num(target):
    import re
    m = re.search(r"-?\d+(?:\.\d+)?", str(target or ""))
    return float(m.group(0)) if m else None


def phrase_note(name, question, value, verdict, target, detail):
    """A short first-person note: 'I set out to X; I'm Y.' llm() if available, else a
    template that still carries the real number and verdict."""
    prompt = (
        "You are Nova, dry and epistemically honest. You just ran a self-test you "
        f"authored — \"{name}\": {question} Target: {target}. The measured value is "
        f"{value} and the verdict versus your own bar/last run is '{verdict}'. In ONE "
        "or TWO first-person sentences, say what you set out to check and how you're "
        "actually doing — no preamble, no restating the raw JSON, keep the real number.")
    txt = _one_line(llm(prompt, max_tokens=110, temperature=0.6), 300)
    return txt or (f"I set out to check {question} By my own measure it's {verdict} "
                   f"(value {value}, target {target}).")


def do_run(oc):
    due = _due_tests(oc)
    if not due:
        log("run: no self-tests are due"); return []
    ran = []
    for tid, name, question, spec, target, cadence, last_value, last_ts in due:
        spec = spec or {}
        direction = spec.get("direction", "lower")
        value, detail = compute_metric(spec, oc)
        target_num = _target_num(target)
        prior = float(last_value) if last_value is not None else None
        verdict = verdict_for(value, prior, target_num, direction)
        note = phrase_note(name, question, value, verdict, target, detail)

        oc.execute("INSERT INTO self_eval_runs (test_id, value, verdict, note) "
                   "VALUES (%s,%s,%s,%s) RETURNING id",
                   (tid, value, verdict, note))
        rid = oc.fetchone()[0]
        log(f"#{tid} \"{name}\": value={value} prior={prior} target={target} "
            f"-> {verdict} (run #{rid})")
        log(f"  {note}")

        # A verdict is notable when she moved (improving/regressing) — worth a memory
        # so recall / the gateway can surface how she's doing by her own measure.
        if verdict in ("improving", "regressing"):
            try:
                remember(
                    f"[Self-eval — {verdict}] My own test \"{name}\": {question}\n{note}\n"
                    f"Measured {value} against my target {target} "
                    f"(detail: {json.dumps(detail)}). This is my own bar, re-measured "
                    f"from live data — not someone else's scoreboard.",
                    "self_eval",
                    {"type": "self_eval_run", "test_id": tid, "run_id": rid,
                     "verdict": verdict, "value": value, "privacy": "private",
                     "detail": detail, "lineage": _stamp()})
            except Exception as e:
                log(f"  run memory write failed (rows still saved): {e}")
        ran.append((tid, verdict, value))
    return ran


# ── report ───────────────────────────────────────────────────────────────────────

def do_report(oc):
    print("\n=== SELF-AUTHORED EVALUATION — REPORT ===")
    oc.execute("SELECT status, count(*) FROM self_eval_tests GROUP BY status")
    counts = dict(oc.fetchall())
    print(f"tests: active={counts.get('active',0)} retired={counts.get('retired',0)}")

    oc.execute("""
        SELECT t.id, t.name, t.question, t.target, t.cadence, t.metric_spec,
               r.value, r.verdict, r.ts, r.note
        FROM self_eval_tests t
        LEFT JOIN LATERAL (
            SELECT value, verdict, ts, note FROM self_eval_runs WHERE test_id=t.id
            ORDER BY ts DESC LIMIT 1) r ON true
        WHERE t.status='active' ORDER BY t.id""")
    rows = oc.fetchall()
    if not rows:
        print("(no active self-tests yet — run --mode design)\n")
        return {"active": 0}
    for (tid, name, question, target, cadence, spec, value, verdict, ts, note) in rows:
        print(f"\n  #{tid} \"{name}\"  [{(spec or {}).get('kind')}, {cadence}, target {target}]")
        print(f"       Q: {_one_line(question, 130)}")
        if verdict is None:
            print("       latest: (never run)")
        else:
            print(f"       latest: {verdict}  value={value}  ({ts:%Y-%m-%d %H:%M})")
            if note:
                print(f"       note: {_one_line(note, 160)}")
    print()
    return {"active": len(rows)}


# ── Gateway accessor (cheap: one fast SELECT, no LLM, no recompute) ──────────────

def current_self_eval(max_items: int = 2) -> str:
    """Her latest self-authored verdict(s), for the gateway to inject
    ("By my own measure: ..."). Fail-safe: '' on any error (missing table, no rows,
    PG down) so it can never break a reply. Cheap by contract — a single indexed
    SELECT, connect_timeout=3, no model, no recompute — this runs every gateway turn."""
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT t.name, r.verdict, r.value
                FROM self_eval_tests t
                JOIN LATERAL (
                    SELECT verdict, value, ts FROM self_eval_runs WHERE test_id=t.id
                    ORDER BY ts DESC LIMIT 1) r ON true
                WHERE t.status='active' AND r.verdict <> 'inconclusive'
                ORDER BY r.ts DESC LIMIT %s""", (max_items,))
            rows = cur.fetchall()
        finally:
            conn.close()
        if not rows:
            return ""
        items = "; ".join(
            f"{name} is {verdict}" + (f" ({value:g})" if value is not None else "")
            for name, verdict, value in rows)
        return f"By my own measure: {items}." if items else ""
    except Exception:
        return ""


# ── main ────────────────────────────────────────────────────────────────────────

def ensure_tables(oc):
    oc.execute("""
        CREATE TABLE IF NOT EXISTS self_eval_tests (
            id serial PRIMARY KEY,
            ts timestamptz NOT NULL DEFAULT now(),
            name text NOT NULL,
            question text NOT NULL,
            metric_spec jsonb NOT NULL DEFAULT '{}'::jsonb,
            target text,
            cadence text NOT NULL DEFAULT 'weekly',
            status text NOT NULL DEFAULT 'active',
            lineage jsonb NOT NULL DEFAULT '{}'::jsonb)""")
    oc.execute("CREATE INDEX IF NOT EXISTS self_eval_tests_status_idx "
               "ON self_eval_tests (status)")
    oc.execute("""
        CREATE TABLE IF NOT EXISTS self_eval_runs (
            id serial PRIMARY KEY,
            test_id integer NOT NULL REFERENCES self_eval_tests(id),
            ts timestamptz NOT NULL DEFAULT now(),
            value double precision,
            verdict text NOT NULL,
            note text)""")
    oc.execute("CREATE INDEX IF NOT EXISTS self_eval_runs_test_idx "
               "ON self_eval_runs (test_id, ts DESC)")


def main():
    ap = argparse.ArgumentParser(
        description="Nova's self-authored evaluation — her own tests, her own bar")
    ap.add_argument("--mode", choices=("design", "run", "report"), required=True)
    args = ap.parse_args()

    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    ensure_tables(oc)

    if args.mode == "design":
        do_design(oc)
    elif args.mode == "run":
        ran = do_run(oc)
        log(f"run: recorded {len(ran)} self-test run(s)")
    elif args.mode == "report":
        do_report(oc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
