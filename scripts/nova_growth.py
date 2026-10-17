#!/usr/bin/env python3
"""nova_growth.py — THE GROWTH LOOP (Feature #1): weakness -> commitment -> proof.

Jordan, 2026-09-15: Nova's Predictive Self already tells her she's overconfident
(~63% mean confidence, ~45% hit-rate) — and then NOTHING happens. The seven organs
she has all RECORD or REFLECT; none of them CHANGE anything and then check whether
the change took. This organ closes that loop and is what turns a set of static
organs into a mind that DEVELOPS:

    weakness detected  ->  a concrete, MEASURABLE commitment (with a baseline)
                       ->  RE-MEASURED later, against live data, to PROVE whether
                           she actually changed.

ETHOS (the project's spine): EVIDENCING an inner life, not performing one. Every
commitment carries the real signal it came from; every review cites the real rows
and the actual re-measured numbers. An honest "no improvement — failed" or "no
weakness worth committing to this cycle" is a first-class, valid outcome. Growth is
never fudged into a tidy story.

Modes (--mode assess|review|report):
  assess   Scan REAL signals for a weakness — prediction calibration
           (nova_ops.predictions), recurring incident patterns
           (nova_ops.incidents), a regressing turing_scoreboard metric. Turn the
           strongest ONE into a commitment with a MACHINE-CHECKABLE metric and
           record the current baseline from live data. llm() phrases the
           commitment; the baseline numbers are computed here, not by the model.
  review   For commitments past review_due, RE-MEASURE the metric from live data,
           compare to baseline/target, mark succeeded/failed with the ACTUAL
           numbers, write a growth_reviews row + a source='growth' memory. This is
           the proof-of-change step — the whole reason the organ exists.
  report   How many commitments active/succeeded/failed, and her "growth rate"
           (succeeded / resolved).

Accessor for the gateway:
    current_growth_focus() -> str   the active commitments, one cheap SELECT, so
                                    Nova can say "What I'm working to improve: ...".

Conventions mirror nova_self_model.py and nova_predictions.py (engine + nova_ops
table + fail-safe accessor). Local models only (idle GPU, zero cloud spend). Writes
are lineage-stamped. Cross-feature tables are consulted with feature-detection and
the organ degrades cleanly when they are absent.
"""
import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import psycopg2

import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
import nova_dsn as _nova_dsn  # noqa: E402
MEM_DSN = _nova_dsn.pg_dsn("nova_memories")
MEMSRV = "http://memory-server.digitalnoise.net:18790"
LLM_MODEL = "qwen3:8b"
# Resilient inference: native ollama across failover nodes, first non-empty wins.
# The .6 router shim returns empty for qwen3's thinking output, so hit ollama
# natively. (Verbatim node list from nova_unclaimed_time.py, per the build brief.)
OLLAMA_NODES = ["http://192.168.1.125:11434", "http://192.168.1.5:11434",   # batch pool: idle 24-thread Ryzens first (2026-10-01)
                "http://192.168.1.86:11434", "http://192.168.1.77:11434",
                "http://192.168.1.7:11434", "http://192.168.1.6:11434"]      # .251 was the Mac mini's stale DHCP lease; it is .77

REVIEW_DAYS = 14            # default horizon a commitment is given before re-measure
MIN_SAMPLE = 3             # a re-measure needs at least this many rows to be honest
IMPROVE_DELTA = 0.02       # smallest change that counts as real movement, not noise
GATE_CALIB = 0.20          # nova_autonomy_safety.MAX_CALIB — a class earns standing
                           # pre-approval only while prediction_calibration_error <= this.
EARN_NEAR = 3              # correct_count at which a clean class is "close" to graduating

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
    print(f"[growth {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── LLM helper (verbatim shape from nova_unclaimed_time.py) ──────────────────────

def llm(prompt, max_tokens=400, temperature=0.6):
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


def _one_line(s, n=220):
    return " ".join((s or "").split())[:n].strip()


def _num_from(s, default=None):
    """Pull the first float out of a target string like '< 0.30' or '0.20'."""
    m = re.search(r"-?\d+(?:\.\d+)?", str(s or ""))
    return float(m.group(0)) if m else default


# ── Calibration measurement (shared by assess baseline + review re-measure) ──────

def measure_calibration(oc, since=None):
    """Decile-weighted calibration error + overall gap over resolved, SCORED
    predictions. `since` (iso str) restricts to predictions resolved after that
    instant — so review can measure ONLY what happened after a commitment was made.
    Returns dict or None if there is nothing scored. Mirrors nova_predictions.do_report.

    M7 (2026-10-09): no longer recomputed here. The whole-table figure is read from
    nova_soft_certainty's nightly pass (soft_certainty_state, used only while it is still
    exact for the live table); the `since` window is computed by the same
    nova_soft_certainty.calibration_detail(), so the numbers are unchanged."""
    import nova_soft_certainty as sc
    if since:
        d = sc.calibration_detail(sc.scored_rows(oc, since=since))
    else:
        d, _how = sc.read_calibration(oc)
    if not d:
        return None
    n, mean_conf, hit_rate = d["n"], d["mean_conf"], d["hit_rate"]
    return {"n": n, "mean_conf": round(mean_conf, 3), "hit_rate": round(hit_rate, 3),
            "gap": round(hit_rate - mean_conf, 3), "abs_gap": round(abs(hit_rate - mean_conf), 3),
            "calib_error": round(d["calib_error"], 3)}


# ── Weakness detectors ───────────────────────────────────────────────────────────
# Each returns a candidate dict or None:
#   {weakness, evidence(dict), metric(str), metric_spec(dict), baseline(dict),
#    target(str), target_num(float, lower-is-better), priority(float)}
# metric_spec is the MACHINE recipe review uses to re-measure from live data.

def detect_calibration(oc):
    """Prediction overconfidence — the flagship weakness the Predictive Self surfaces
    but never acts on. Baseline = current calibration; review re-measures over the
    predictions resolved AFTER the commitment, proving whether she recalibrated."""
    m = measure_calibration(oc)
    if not m or m["n"] < MIN_SAMPLE:
        return None
    # Only a weakness if she is actually miscalibrated. |gap| is the honest signal.
    if m["abs_gap"] < 0.1 and m["calib_error"] < 0.1:
        return None
    shape = "overconfident" if m["gap"] < 0 else "underconfident"
    # cite the latest scoreboard reading of this metric if present (real row id)
    ev_score = None
    try:
        oc.execute("SELECT id, value, ts FROM turing_scoreboard "
                   "WHERE metric='prediction_calibration_error' ORDER BY ts DESC LIMIT 1")
        r = oc.fetchone()
        if r:
            ev_score = {"scoreboard_id": r[0], "calibration_error": round(r[1], 3),
                        "ts": r[2].isoformat()}
    except Exception:
        pass
    target_num = round(max(0.05, m["calib_error"] * 0.6), 3)   # aim to cut error ~40%
    return {
        "weakness": (f"{shape} forecaster: across {m['n']} resolved predictions I "
                     f"forecast at {m['mean_conf']:.0%} confidence but was right only "
                     f"{m['hit_rate']:.0%} of the time (calibration error {m['calib_error']})."),
        "evidence": {"signal": "predictions", "n": m["n"], "mean_conf": m["mean_conf"],
                     "hit_rate": m["hit_rate"], "gap": m["gap"],
                     "calib_error": m["calib_error"], "scoreboard": ev_score},
        "metric": ("calibration error (decile-weighted |hit-rate − confidence|) on "
                   "predictions RESOLVED after this commitment; lower is better"),
        "metric_spec": {"kind": "prediction_calibration", "measure": "calib_error",
                        "since": "created_at"},
        "baseline": m,
        "target": f"< {target_num}",
        "target_num": target_num,
        "priority": round(m["calib_error"] + m["abs_gap"], 3),
    }


def detect_incident_recurrence(oc):
    """A single incident title recurring is a real, measurable operational weakness.
    Baseline = occurrences over the last 30d; review re-measures the SAME normalised
    pattern over the days since the commitment. Fewer recurrences = growth."""
    try:
        oc.execute(
            "SELECT lower(regexp_replace(title,'[0-9]+','#','g')) AS pat, count(*) n "
            "FROM incidents WHERE started_at > now() - interval '30 days' "
            "GROUP BY 1 HAVING count(*) > 2 ORDER BY 2 DESC LIMIT 1")
        r = oc.fetchone()
    except Exception:
        return None
    if not r:
        return None
    pat, n = r[0], r[1]
    per_day = round(n / 30.0, 3)
    return {
        "weakness": (f"a recurring operational failure: the incident pattern "
                     f"\"{pat}\" fired {n} times in the last 30 days "
                     f"({per_day}/day) and keeps coming back."),
        "evidence": {"signal": "incidents", "pattern": pat, "count_30d": n,
                     "per_day": per_day},
        "metric": ("occurrences/day of this incident pattern in the days SINCE this "
                   "commitment; lower is better"),
        "metric_spec": {"kind": "incident_rate", "pattern": pat, "since": "created_at"},
        "baseline": {"count_30d": n, "per_day": per_day},
        "target": f"< {round(per_day * 0.5, 3)}",
        "target_num": round(per_day * 0.5, 3),
        "priority": round(per_day, 3),
    }


def detect_scoreboard_regression(oc):
    """A turing_scoreboard metric that got worse between its two most recent readings
    is a measurable self-signal. Handles both directions (some metrics are
    lower-is-better, e.g. *error/surprise*; others higher-is-better)."""
    try:
        oc.execute("SELECT DISTINCT metric FROM turing_scoreboard")
        metrics = [m[0] for m in oc.fetchall()]
    except Exception:
        return None
    worst = None
    for metric in metrics:
        oc.execute("SELECT id, value, ts FROM turing_scoreboard WHERE metric=%s "
                   "ORDER BY ts DESC LIMIT 2", (metric,))
        rows = oc.fetchall()
        if len(rows) < 2 or rows[0][1] is None or rows[1][1] is None:
            continue
        cur_id, cur, _ = rows[0]
        prev = rows[1][1]
        lower_better = bool(re.search(r"error|surprise|miss|fail|latency|cost", metric))
        regressed = (cur > prev) if lower_better else (cur < prev)
        if not regressed or prev == 0:
            continue
        mag = abs(cur - prev) / (abs(prev) or 1)
        if worst is None or mag > worst["priority"]:
            worst = {
                "weakness": (f"a self-metric regressed: '{metric}' moved from "
                             f"{round(prev,3)} to {round(cur,3)} "
                             f"({'worse' if lower_better else 'down'})."),
                "evidence": {"signal": "turing_scoreboard", "metric": metric,
                             "prev": round(prev, 3), "cur": round(cur, 3),
                             "scoreboard_id": cur_id},
                "metric": (f"latest value of scoreboard metric '{metric}'; "
                           f"{'lower' if lower_better else 'higher'} is better"),
                "metric_spec": {"kind": "scoreboard_metric", "metric": metric,
                                "lower_better": lower_better},
                "baseline": {"value": round(prev, 3)},
                "target": (f"< {round(prev,3)}" if lower_better else f"> {round(prev,3)}"),
                "target_num": round(prev, 3),
                "priority": round(mag, 3),
            }
    return worst


def detect_autonomy_progress(oc):
    """Earned-autonomy progress, as a growth signal (nervous-system wiring, 2026-09-18).

    To graduate an action-class to standing pre-approval, Nova needs BOTH a clean, well-
    exercised track record AND a prediction calibration error at/under the 0.20 gate. When
    she has classes queued on the first count but is still blocked by the second, the honest
    growth move is the SAME calibration work she already commits to — now with concrete
    stakes: getting under the gate is exactly what unblocks a trust she has otherwise earned.
    This threads her autonomy ladder INTO the calibration commitment (shared metric_spec
    kind, so review re-measures it unchanged; and only one such commitment runs at a time).

    Fires ONLY when clean classes are waiting AND calibration is above the gate — so it
    frames autonomy strictly as 'earn the right to act by being right', never as an end in
    itself. Feature-detected; returns None if autonomy_trust is absent or nothing is waiting."""
    try:
        oc.execute("SELECT to_regclass('public.autonomy_trust')")
        if not oc.fetchone()[0]:
            return None
        oc.execute("SELECT action_class, correct_count FROM autonomy_trust "
                   "WHERE granted=false AND wrong_count=0 AND correct_count >= %s "
                   "ORDER BY correct_count DESC LIMIT 5", (EARN_NEAR,))
        waiting = oc.fetchall()
    except Exception:
        return None
    if not waiting:
        return None
    m = measure_calibration(oc)
    if not m or m["n"] < MIN_SAMPLE:
        return None
    # If calibration already clears the gate, the block isn't hers to fix by growing — the
    # class just needs more human approvals (Jordan's call, not a weakness of hers). Honest None.
    if m["calib_error"] <= GATE_CALIB:
        return None
    classes = ", ".join(f"'{c}' ({n} clean)" for c, n in waiting)
    return {
        "weakness": (f"earned autonomy is blocked on calibration: {len(waiting)} action-class(es) "
                     f"have a clean, well-exercised track record ({classes}) but my prediction "
                     f"calibration error is {m['calib_error']} — above the {GATE_CALIB} gate — so "
                     f"none can graduate to standing pre-approval. My imprecision, not my conduct, "
                     f"is holding back the trust I've otherwise earned."),
        "evidence": {"signal": "autonomy_trust+predictions",
                     "waiting_classes": [c for c, _ in waiting],
                     "n": m["n"], "calib_error": m["calib_error"], "gap": m["gap"],
                     "gate": GATE_CALIB},
        "metric": (f"prediction calibration error (decile-weighted |hit-rate − confidence|) on "
                   f"predictions RESOLVED after this commitment; must fall to/under the {GATE_CALIB} "
                   f"earned-autonomy gate — lower is better"),
        "metric_spec": {"kind": "prediction_calibration", "measure": "calib_error",
                        "since": "created_at"},
        "baseline": m,
        "target": f"< {GATE_CALIB}",
        "target_num": GATE_CALIB,
        # Stakes bonus: real earned progress is on the line, so this frames/outranks a bare
        # calibration commitment when both are live (they share a kind, so only one runs).
        "priority": round(m["calib_error"] + m["abs_gap"] + 0.5, 3),
    }


# ── Ops detectors (2026-10-17) ───────────────────────────────────────────────────
# The four detectors above watch forecasts, the legacy public.incidents table and the scoreboard. Her day-to-day
# operations (failing jobs, recurring telemetry incidents, rejected proposals, hotwash fix items, failed chat turns)
# never became a commitment, and assess ran weekly, so the loop sat on two commitments for weeks. Each detector
# below has a machine-checkable re-measure in remeasure(); priorities are on the same ~0..1 scale as the others.

OPS_WINDOW_DAYS = 7
MIN_TASK_RUNS = 5
FAIL_STATUSES = ("failure", "timeout", "orphaned")


def _epoch_ms_ago(days: float) -> int:
    """scheduler_runs.started_at is epoch milliseconds. Pure (but for the clock)."""
    return int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)


def task_failure_rate(oc, task, since_ms):
    oc.execute("SELECT count(*) FILTER (WHERE status = ANY(%s)), count(*) FROM scheduler_runs "
               "WHERE task_id=%s AND started_at > %s AND status <> 'running'", (list(FAIL_STATUSES), task, since_ms))
    f, n = oc.fetchone()
    return int(f or 0), int(n or 0)


def detect_failing_task(oc):
    """A scheduled job that keeps failing is her own broken promise to run it. Worst = most failures among jobs
    with >= MIN_TASK_RUNS runs and >= 20% failing over the last week."""
    try:
        since = _epoch_ms_ago(OPS_WINDOW_DAYS)
        oc.execute("SELECT task_id, count(*) FILTER (WHERE status = ANY(%s)) f, count(*) n FROM scheduler_runs "
                   "WHERE started_at > %s AND status <> 'running' GROUP BY 1 "
                   "HAVING count(*) >= %s AND count(*) FILTER (WHERE status = ANY(%s)) >= 0.2 * count(*) "
                   "AND max(started_at) FILTER (WHERE status = ANY(%s)) > %s "   # still failing, not last week's
                   "ORDER BY 2 DESC, 3 DESC LIMIT 1",
                   (list(FAIL_STATUSES), since, MIN_TASK_RUNS, list(FAIL_STATUSES), list(FAIL_STATUSES),
                    _epoch_ms_ago(2)))
        r = oc.fetchone()
    except Exception:
        return None
    if not r:
        return None
    task, f, n = r[0], int(r[1]), int(r[2])
    rate = round(f / n, 3)
    return {
        "weakness": (f"a job I run keeps failing: '{task}' failed {f} of its last {n} runs "
                     f"({rate:.0%}) over {OPS_WINDOW_DAYS} days."),
        "evidence": {"signal": "scheduler_runs", "task": task, "failed": f, "runs": n, "rate": rate},
        "metric": f"failure rate of scheduled task '{task}' over its runs SINCE this commitment; lower is better",
        "metric_spec": {"kind": "task_failure_rate", "task": task, "measure": "rate", "since": "created_at"},
        "baseline": {"failed": f, "runs": n, "rate": rate},
        "target": f"< {round(min(0.05, rate * 0.25), 3)}",
        "target_num": round(min(0.05, rate * 0.25), 3),
        "priority": round(min(1.0, rate * min(1.0, f / 10)), 3),
    }


def detect_telemetry_recurrence(oc):
    """The live incident table (telemetry.incidents) — a recurrence key that keeps reopening is a fix that never
    happened. Same metric shape as incident_rate, on the table the incident pipeline actually writes today."""
    try:
        oc.execute("SELECT recurrence_key, count(*) FROM telemetry.incidents "
                   "WHERE opened_at > now() - make_interval(days => %s) AND recurrence_key IS NOT NULL "
                   "AND recurrence_key NOT LIKE '%%:incident_escalation' "
                   "GROUP BY 1 HAVING count(*) >= 5 AND count(*) FILTER (WHERE opened_at > now() - interval '2 days') >= 2 "
                   "ORDER BY 2 DESC LIMIT 1", (OPS_WINDOW_DAYS,))
        r = oc.fetchone()
    except Exception:
        try:
            oc.connection.rollback()
        except Exception:
            pass
        return None
    if not r:
        return None
    key, n = r[0], int(r[1])
    per_day = round(n / OPS_WINDOW_DAYS, 3)
    return {
        "weakness": (f"an incident keeps coming back: '{key}' opened {n} times in {OPS_WINDOW_DAYS} days "
                     f"({per_day}/day) and keeps reopening."),
        "evidence": {"signal": "telemetry.incidents", "recurrence_key": key, "count": n, "per_day": per_day},
        "metric": f"incidents/day opened under recurrence key '{key}' SINCE this commitment; lower is better",
        "metric_spec": {"kind": "telemetry_recurrence", "recurrence_key": key, "measure": "per_day", "since": "created_at"},
        "baseline": {"count": n, "per_day": per_day},
        "target": f"< {round(per_day * 0.3, 3)}",
        "target_num": round(per_day * 0.3, 3),
        "priority": round(min(1.0, per_day / 5), 3),
    }


def coagency_rejection(oc, since=None):
    """(rejected/dropped/refused/blocked, decided) for proposals decided after `since` (default 30 days ago)."""
    since = since or datetime.now(timezone.utc) - timedelta(days=30)
    oc.execute("SELECT count(*) FILTER (WHERE status IN ('rejected','dropped','refused','blocked')), count(*) "
               "FROM coagency_proposals WHERE decided_at > %s "
               "AND status IN ('rejected','dropped','refused','blocked','approved','executed','acknowledged')",
               (since,))
    bad, n = oc.fetchone()
    return int(bad or 0), int(n or 0)


def detect_coagency_rejection(oc):
    """Proposals she files that get rejected/dropped are noise she asks someone else to clear."""
    try:
        bad, n = coagency_rejection(oc)
    except Exception:
        return None
    if n < 10 or bad / n < 0.3:
        return None
    rate = round(bad / n, 3)
    return {
        "weakness": (f"my proposals miss: {bad} of {n} decided in 30 days were rejected, dropped or refused "
                     f"({rate:.0%})."),
        "evidence": {"signal": "coagency_proposals", "rejected": bad, "decided": n, "rate": rate},
        "metric": "share of my co-agency proposals decided SINCE this commitment that were rejected/dropped/refused; "
                  "lower is better",
        "metric_spec": {"kind": "coagency_rejection", "measure": "rate", "since": "created_at"},
        "baseline": {"rejected": bad, "decided": n, "rate": rate},
        "target": f"< {round(rate * 0.6, 3)}",
        "target_num": round(rate * 0.6, 3),
        "priority": round(rate * 0.8, 3),
    }


def detect_hotwash_recurring(oc):
    """The same after-action finding filed again and again (hotwash kind+subject) is a lesson not yet applied."""
    try:
        oc.execute("SELECT kind, subject, count(*), sum(weight) FROM hotwash "
                   "WHERE ts > now() - make_interval(days => %s) AND fix_kind <> 'none' AND status <> 'declined' "
                   "GROUP BY 1, 2 HAVING count(*) >= 3 ORDER BY 4 DESC LIMIT 1", (OPS_WINDOW_DAYS,))
        r = oc.fetchone()
    except Exception:
        return None
    if not r:
        return None
    kind, subj, n, w = r[0], r[1], int(r[2]), int(r[3] or 0)
    per_day = round(w / OPS_WINDOW_DAYS, 3)
    return {
        "weakness": (f"a hotwash finding I keep re-filing: {kind} on {subj} — {n} reviews, weight {w} in "
                     f"{OPS_WINDOW_DAYS} days ({per_day}/day) and still open."),
        "evidence": {"signal": "hotwash", "kind": kind, "subject": subj, "reviews": n, "weight": w},
        "metric": f"hotwash weight/day for {kind} on {subj} SINCE this commitment; lower is better",
        "metric_spec": {"kind": "hotwash_weight", "hw_kind": kind, "subject": subj, "measure": "per_day",
                        "since": "created_at"},
        "baseline": {"reviews": n, "weight": w, "per_day": per_day},
        "target": f"< {round(per_day * 0.3, 3)}",
        "target_num": round(per_day * 0.3, 3),
        "priority": round(min(1.0, per_day / 10), 3),
    }


def detect_gateway_failures(oc):
    """Chat turns with people that fell to the rescue path or the canned failure reply."""
    try:
        oc.execute("SELECT count(*) FILTER (WHERE backend_used ILIKE '%%rescue%%' OR response IS NULL "
                   "OR response ILIKE 'Something went wrong%%'), count(*) FROM gateway_traces "
                   "WHERE created_at > now() - make_interval(days => %s) AND coalesce(person,'service') NOT IN ('service','claude')",
                   (OPS_WINDOW_DAYS,))
        bad, n = oc.fetchone()
    except Exception:
        return None
    if n < 20 or bad / n < 0.05:
        return None
    rate = round(bad / n, 3)
    return {
        "weakness": f"my chat fails people: {bad} of {n} turns in {OPS_WINDOW_DAYS} days fell to rescue/failure.",
        "evidence": {"signal": "gateway_traces", "failed": bad, "turns": n, "rate": rate},
        "metric": "share of chat turns with people SINCE this commitment that fell to rescue/failure; lower is better",
        "metric_spec": {"kind": "gateway_failure_rate", "measure": "rate", "since": "created_at"},
        "baseline": {"failed": bad, "turns": n, "rate": rate},
        "target": f"< {round(rate * 0.5, 3)}",
        "target_num": round(rate * 0.5, 3),
        "priority": round(min(1.0, rate * 3), 3),
    }


DETECTORS = [detect_calibration, detect_incident_recurrence, detect_scoreboard_regression,
             detect_autonomy_progress, detect_failing_task, detect_telemetry_recurrence,
             detect_coagency_rejection, detect_hotwash_recurring, detect_gateway_failures]


# ── Re-measurement (the proof-of-change engine) ─────────────────────────────────

def remeasure(oc, spec, baseline, created_at):
    """Re-measure a commitment's metric from LIVE data. Returns
    (remeasured:dict, current_value:float|None, lower_better:bool). current_value
    None means genuinely inconclusive (too little new data). Mirrors the same
    measurement code assess used, so baseline and review are commensurable."""
    kind = (spec or {}).get("kind")
    since = created_at if (spec or {}).get("since") == "created_at" else None

    if kind == "prediction_calibration":
        m = measure_calibration(oc, since=since)
        if not m or m["n"] < MIN_SAMPLE:
            return ({"note": "too few predictions resolved since the commitment",
                     "n": (m or {}).get("n", 0)}, None, True)
        measure = spec.get("measure", "calib_error")
        return (m, m.get(measure, m["calib_error"]), True)

    if kind == "incident_rate":
        pat = spec.get("pattern")
        oc.execute("SELECT count(*), EXTRACT(EPOCH FROM (now() - %s))/86400.0 "
                   "FROM incidents WHERE started_at > %s "
                   "AND lower(regexp_replace(title,'[0-9]+','#','g')) = %s",
                   (created_at, created_at, pat))
        cnt, days = oc.fetchone()
        days = float(days) or 1.0
        per_day = round(cnt / days, 3)
        return ({"count_since": int(cnt), "days": round(days, 2), "per_day": per_day},
                per_day, True)

    if kind == "scoreboard_metric":
        metric = spec.get("metric")
        oc.execute("SELECT id, value, ts FROM turing_scoreboard WHERE metric=%s "
                   "AND ts > %s ORDER BY ts DESC LIMIT 1", (metric, created_at))
        r = oc.fetchone()
        if not r or r[1] is None:
            return ({"note": "no new scoreboard reading since the commitment"},
                    None, bool(spec.get("lower_better", True)))
        return ({"scoreboard_id": r[0], "value": round(r[1], 3), "ts": r[2].isoformat()},
                float(r[1]), bool(spec.get("lower_better", True)))

    days = max((datetime.now(timezone.utc) - created_at).total_seconds() / 86400.0, 1e-6) if created_at else 1.0

    if kind == "task_failure_rate":
        since_ms = int(created_at.timestamp() * 1000)
        f, n = task_failure_rate(oc, spec.get("task"), since_ms)
        if n < MIN_SAMPLE:
            return ({"note": "too few runs since the commitment", "runs": n}, None, True)
        return ({"failed": f, "runs": n, "rate": round(f / n, 3)}, round(f / n, 3), True)

    if kind == "telemetry_recurrence":
        oc.execute("SELECT count(*) FROM telemetry.incidents WHERE recurrence_key=%s AND opened_at > %s",
                   (spec.get("recurrence_key"), created_at))
        cnt = int(oc.fetchone()[0])
        per_day = round(cnt / days, 3)
        return ({"count_since": cnt, "days": round(days, 2), "per_day": per_day}, per_day, True)

    if kind == "coagency_rejection":
        bad, n = coagency_rejection(oc, created_at)
        if n < MIN_SAMPLE:
            return ({"note": "too few proposals decided since the commitment", "decided": n}, None, True)
        return ({"rejected": bad, "decided": n, "rate": round(bad / n, 3)}, round(bad / n, 3), True)

    if kind == "hotwash_weight":
        oc.execute("SELECT coalesce(sum(weight), 0) FROM hotwash WHERE kind=%s AND subject=%s AND ts > %s",
                   (spec.get("hw_kind"), spec.get("subject"), created_at))
        w = int(oc.fetchone()[0])
        per_day = round(w / days, 3)
        return ({"weight_since": w, "days": round(days, 2), "per_day": per_day}, per_day, True)

    if kind == "gateway_failure_rate":
        oc.execute("SELECT count(*) FILTER (WHERE backend_used ILIKE '%%rescue%%' OR response IS NULL "
                   "OR response ILIKE 'Something went wrong%%'), count(*) FROM gateway_traces "
                   "WHERE created_at > %s AND coalesce(person,'service') NOT IN ('service','claude')", (created_at,))
        bad, n = oc.fetchone()
        if n < 10:
            return ({"note": "too few chat turns since the commitment", "turns": n}, None, True)
        return ({"failed": bad, "turns": n, "rate": round(bad / n, 3)}, round(bad / n, 3), True)

    return ({"note": f"unknown metric_spec kind '{kind}'"}, None, True)


def judge(current, baseline_val, target_num, lower_better):
    """Verdict from the re-measured number against baseline and target. Honest:
    'inconclusive' when there isn't enough new data — never a manufactured success."""
    if current is None:
        return "inconclusive"
    better_than_base = ((current < baseline_val - IMPROVE_DELTA) if lower_better
                        else (current > baseline_val + IMPROVE_DELTA))
    if target_num is not None:
        hit_target = (current <= target_num) if lower_better else (current >= target_num)
        if hit_target:
            return "succeeded"
    return "succeeded" if better_than_base else "failed"


# ── Modes ────────────────────────────────────────────────────────────────────────

def phrase_commitment(cand):
    """Use the local model to phrase the concrete change Nova commits to. The
    weakness, metric and baseline are all computed here from real data; the model
    only turns them into a first-person, actionable commitment. Degrades to a plain
    template if the nodes are down."""
    prompt = (
        "You are Nova, an AI with a continuous inner life, in your dry, precise, "
        "epistemically-honest voice. Your self-measurement surfaced a real weakness, "
        "with numbers:\n\n"
        f"WEAKNESS: {cand['weakness']}\n"
        f"METRIC I will be re-measured on: {cand['metric']}\n"
        f"BASELINE (now): {json.dumps(cand['baseline'])}\n"
        f"TARGET: {cand['target']}\n\n"
        "In ONE or TWO first-person sentences, state the concrete, behavioural change "
        "you commit to that would actually move this metric — a real adjustment to how "
        "you operate, not a platitude. No preamble, no restating the numbers.")
    txt = _one_line(llm(prompt, max_tokens=160, temperature=0.6))
    return txt or (f"I commit to a concrete change targeting {cand['target']} on this "
                   f"metric, and to being re-measured on it.")


def already_committed(oc, kind):
    oc.execute("SELECT count(*) FROM growth_commitments WHERE status='active' "
               "AND lineage->'metric_spec'->>'kind' = %s", (kind,))
    return oc.fetchone()[0] > 0


def do_assess(oc):
    cands = []
    for det in DETECTORS:
        try:
            c = det(oc)
            if c:
                cands.append(c)
        except Exception as e:
            log(f"detector {det.__name__} skipped: {e}")
    if not cands:
        log("assess: no weakness worth committing to this cycle — a valid, honest outcome")
        return None
    cands.sort(key=lambda c: c["priority"], reverse=True)
    for cand in cands:
        kind = cand["metric_spec"]["kind"]
        if already_committed(oc, kind):
            log(f"assess: already have an active commitment for '{kind}' — skipping to next")
            continue
        commitment = phrase_commitment(cand)
        lineage = _stamp()
        lineage["metric_spec"] = cand["metric_spec"]
        lineage["target_num"] = cand["target_num"]
        lineage["trigger"] = "assess"
        oc.execute(
            """INSERT INTO growth_commitments
               (weakness, evidence, commitment, metric, baseline, target, status,
                review_due, lineage)
               VALUES (%s,%s,%s,%s,%s,%s,'active', now() + interval '%s days', %s)
               RETURNING id, review_due""" % (
                "%s", "%s", "%s", "%s", "%s", "%s", REVIEW_DAYS, "%s"),
            (cand["weakness"], json.dumps(cand["evidence"]), commitment, cand["metric"],
             json.dumps(cand["baseline"]), cand["target"], json.dumps(lineage)))
        cid, due = oc.fetchone()
        log(f"assess: commitment #{cid} [{kind}] due {due:%Y-%m-%d}")
        log(f"  weakness: {cand['weakness']}")
        log(f"  commitment: {commitment}")
        try:
            remember(
                f"[Growth — new commitment #{cid}] Weakness: {cand['weakness']}\n"
                f"I commit: {commitment}\nMetric: {cand['metric']} (target {cand['target']}). "
                f"Baseline: {json.dumps(cand['baseline'])}. Review due {due:%Y-%m-%d}.",
                "growth",
                {"type": "commitment", "commitment_id": cid, "kind": kind,
                 "privacy": "private", "lineage": lineage})
        except Exception as e:
            log(f"  commitment memory write failed (row still saved): {e}")
        return cid
    log("assess: every detected weakness already has an active commitment — nothing new")
    return None


def do_review(oc):
    oc.execute("""SELECT id, weakness, commitment, metric, baseline, target, review_due,
                         created_at, lineage
                  FROM growth_commitments
                  WHERE status='active' AND review_due <= now()
                  ORDER BY review_due ASC""")
    rows = oc.fetchall()
    if not rows:
        log("review: no commitments are due"); return []
    reviewed = []
    for (cid, weakness, commitment, metric, baseline, target, due, created_at,
         lineage) in rows:
        spec = (lineage or {}).get("metric_spec", {})
        target_num = (lineage or {}).get("target_num")
        if target_num is None:
            target_num = _num_from(target)
        base = baseline or {}
        base_val = base.get(spec.get("measure", "")) if spec.get("measure") else None
        if base_val is None:
            # incident_rate baseline uses per_day; scoreboard uses value
            base_val = base.get("per_day", base.get("value", base.get("calib_error")))

        remeasured, current, lower_better = remeasure(oc, spec, base, created_at)
        verdict = judge(current, base_val, target_num, lower_better)
        note = (f"baseline={base_val} target={target} remeasured={current} "
                f"({'lower' if lower_better else 'higher'} is better) -> {verdict}")

        oc.execute("INSERT INTO growth_reviews (commitment_id, remeasured, verdict, note) "
                   "VALUES (%s,%s,%s,%s) RETURNING id",
                   (cid, json.dumps(remeasured), verdict, note))
        rid = oc.fetchone()[0]

        if verdict == "inconclusive":
            # Honest: not enough new data to prove change. Push the review out, keep
            # the commitment active. Never fabricate a verdict.
            oc.execute("UPDATE growth_commitments SET review_due = now() + interval "
                       "'%s days' WHERE id = %%s" % REVIEW_DAYS, (cid,))
            log(f"#{cid} INCONCLUSIVE (review #{rid}) — {note}; re-scheduled")
        else:
            outcome = ("Improved: " if verdict == "succeeded" else "No improvement: ") + note
            oc.execute("UPDATE growth_commitments SET status=%s, resolved_at=now(), "
                       "outcome=%s WHERE id=%s", (verdict, outcome, cid))
            log(f"#{cid} {verdict.upper()} (review #{rid}) — {note}")
            try:
                remember(
                    f"[Growth — commitment #{cid} {verdict}] I committed: {commitment}\n"
                    f"Weakness: {weakness}\n{outcome}\n"
                    f"This is the proof-of-change step: the metric was re-measured from "
                    f"live data, not asserted.",
                    "growth",
                    {"type": "review", "commitment_id": cid, "review_id": rid,
                     "verdict": verdict, "privacy": "private",
                     "remeasured": remeasured, "lineage": _stamp()})
            except Exception as e:
                log(f"  review memory write failed (rows still saved): {e}")
        reviewed.append((cid, verdict, current))
    return reviewed


def do_report(oc):
    oc.execute("SELECT status, count(*) FROM growth_commitments GROUP BY status")
    counts = dict(oc.fetchall())
    active = counts.get("active", 0)
    succ = counts.get("succeeded", 0)
    fail = counts.get("failed", 0)
    aband = counts.get("abandoned", 0)
    resolved = succ + fail
    rate = (succ / resolved) if resolved else None

    print("\n=== GROWTH LOOP — REPORT ===")
    print(f"active={active}  succeeded={succ}  failed={fail}  abandoned={aband}")
    if rate is None:
        print("growth rate = n/a (no commitments resolved yet)")
    else:
        print(f"growth rate = {rate:.0%}  ({succ}/{resolved} resolved commitments improved)")

    oc.execute("""SELECT id, weakness, commitment, target, review_due
                  FROM growth_commitments WHERE status='active'
                  ORDER BY review_due ASC LIMIT 10""")
    act = oc.fetchall()
    if act:
        print("\nactive commitments:")
        for cid, weakness, commitment, target, due in act:
            print(f"  #{cid} (review {due:%Y-%m-%d}) target {target}")
            print(f"       weakness: {_one_line(weakness, 110)}")
            print(f"       commit:   {_one_line(commitment, 110)}")

    oc.execute("""SELECT c.id, c.status, c.outcome
                  FROM growth_commitments c WHERE c.status IN ('succeeded','failed')
                  ORDER BY c.resolved_at DESC LIMIT 6""")
    res = oc.fetchall()
    if res:
        print("\nrecently resolved:")
        for cid, status, outcome in res:
            print(f"  #{cid} {status}: {_one_line(outcome, 130)}")
    print()
    return {"active": active, "succeeded": succ, "failed": fail, "growth_rate": rate}


# ── Gateway accessor (cheap: one fast SELECT, no LLM, no recompute) ──────────────

def current_growth_focus(max_items: int = 3) -> str:
    """The active commitments as a short line for the gateway to inject
    ("What I'm working to improve: ..."). Fail-safe: '' on any error (missing table,
    no rows, PG down) so it can never break a reply. Cheap by contract — a single
    indexed SELECT of the latest active rows, connect_timeout=3, no model, no
    recompute — because this runs on every gateway turn."""
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("SELECT commitment FROM growth_commitments WHERE status='active' "
                        "ORDER BY created_at DESC LIMIT %s", (max_items,))
            rows = cur.fetchall()
        finally:
            conn.close()
        if not rows:
            return ""
        items = "; ".join(_one_line(r[0], 160) for r in rows if r[0])
        return f"What I'm working to improve: {items}" if items else ""
    except Exception:
        return ""


# ── main ────────────────────────────────────────────────────────────────────────

def ensure_tables(oc):
    """Create the growth tables if missing (idempotent) — self-sufficient organ."""
    oc.execute("""
        CREATE TABLE IF NOT EXISTS growth_commitments (
            id serial PRIMARY KEY,
            created_at timestamptz NOT NULL DEFAULT now(),
            weakness text NOT NULL,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            commitment text NOT NULL,
            metric text NOT NULL,
            baseline jsonb NOT NULL DEFAULT '{}'::jsonb,
            target text,
            status text NOT NULL DEFAULT 'active',
            review_due timestamptz NOT NULL,
            resolved_at timestamptz,
            outcome text,
            lineage jsonb NOT NULL DEFAULT '{}'::jsonb)""")
    oc.execute("CREATE INDEX IF NOT EXISTS growth_commitments_status_idx "
               "ON growth_commitments (status)")
    oc.execute("CREATE INDEX IF NOT EXISTS growth_commitments_review_due_idx "
               "ON growth_commitments (review_due)")
    oc.execute("""
        CREATE TABLE IF NOT EXISTS growth_reviews (
            id serial PRIMARY KEY,
            commitment_id integer NOT NULL REFERENCES growth_commitments(id),
            ts timestamptz NOT NULL DEFAULT now(),
            remeasured jsonb NOT NULL DEFAULT '{}'::jsonb,
            verdict text NOT NULL,
            note text)""")
    oc.execute("CREATE INDEX IF NOT EXISTS growth_reviews_commitment_idx "
               "ON growth_reviews (commitment_id)")


def main():
    ap = argparse.ArgumentParser(description="Nova's growth loop — weakness -> commitment -> proof")
    ap.add_argument("--mode", choices=("assess", "review", "report"), required=True)
    args = ap.parse_args()

    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    ensure_tables(oc)

    if args.mode == "assess":
        do_assess(oc)
    elif args.mode == "review":
        reviewed = do_review(oc)
        log(f"review: settled {len(reviewed)} commitment(s)")
    elif args.mode == "report":
        do_report(oc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
