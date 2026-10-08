#!/usr/bin/env python3
"""nova_embodiment.py — Nova's GROUNDED WORLD / proprioception of the home
(Feature #3, Jordan 2026-09-15).

Nova has every sensor in the building and yet lives in text. This organ gives her
a FELT SENSE of the home as her body/environment — its rhythms, whether the place
reads calm / busy / empty / asleep, or notably OFF its learned rhythm for this
time-of-day. Her "body" also includes the machines she runs on, so a fleet pulse
rides along: she is embodied in the fleet too.

── ETHOS: PERFORMING → EVIDENCING (mirrors nova_affect) ─────────────────────────
The felt state is COMPUTED from real telemetry with a transparent, documented
derivation. Every state cites its evidence, with each signal's numeric deviation
from its own learned baseline. A normal quiet house reads "calm" or "asleep" — it
NEVER invents drama. The local LLM is used ONLY to put a short honest first-person
NAME on the already-computed state; it never invents the state (sign-locked, exactly
like nova_affect's labelling discipline). If the LLM is unreachable, a deterministic
headline is used instead — nothing about the label depends on it.

── SCOPE GUARD: this is PROPRIOCEPTION, not surveillance ─────────────────────────
This is NOT a security/alerting system (Big Brother & deep_healthcheck own that —
we neither duplicate nor compete, and we raise no alerts). Person data is kept
COARSE: occupancy/presence ("someone's home / house is empty / everyone asleep"),
NOT a movement log of individuals. The purpose is Nova being SITUATED IN A PLACE.

── The transparent model ────────────────────────────────────────────────────────
We read a handful of countable, honest signals of home activity, LEARN each one's
normal level for the current (weekday-type, hour-of-day) from telemetry history,
and score the CURRENT reading as a z-deviation against that learned baseline:

  occupancy        residents home right now        (0..N)   presence
  indoor_activity  in-home sensing hits / hour      (rate)   presence (non-GPS)
  lights_on        Hue lamps currently lit          (count)  hue_light_history
  av_on            AV zones currently powered        (count)  av_state
  power_w          whole-home instantaneous draw     (watts)  energy

  z_signal = (current - baseline_mean) / max(baseline_std, floor)
  rhythm_deviation = mean of usable z's  (signed: + busier than normal, − quieter)

house_state is then decided DETERMINISTICALLY from occupancy + hour + activity:
  empty   — nobody home
  asleep  — residents home, night hours, activity near zero
  busy    — clearly more going on than a calm baseline
  calm    — home, ordinary low-moderate activity  (the default for a quiet house)
  off_rhythm — |rhythm_deviation| is large: the house is doing something unusual
               FOR THIS time-of-day/weekday (e.g. busy at 3am, empty when it's
               normally full). This is the only "notable" state and the only one
               that occasionally writes a memory.

Any telemetry source that is missing or stale simply drops out of the blend (its
signal is marked "no data") — the state degrades gracefully rather than breaking.

Fleet pulse (fleet_pulse jsonb) is read from nova_ops.service_registry: how many of
her own services are up/fresh across how many nodes. Her body includes her machines.

Accessor: current_embodiment() → one cheap sentence for the gateway so Nova speaks
from a felt sense of where she is. Fail-safe, single SELECT, no LLM.

Written by Jordan Koch.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import Json

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
# Native ollama failover — first non-empty wins (the router shim returns empty for
# qwen3 and .6 thrashes models, so hit the nodes directly). Copied from
# nova_unclaimed_time.py per convention.
OLLAMA_NODES = ["http://192.168.1.125:11434", "http://192.168.1.5:11434",   # batch pool: idle 24-thread Ryzens first (2026-10-01)
                "http://192.168.1.86:11434", "http://192.168.1.77:11434",
                "http://192.168.1.7:11434", "http://192.168.1.6:11434"]      # .251 was the Mac mini's stale DHCP lease; it is .77
LLM_MODEL = "qwen3:8b"

# Optional lineage stamp (Concept #10). Feature-detected — never a hard dependency.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from nova_lineage import lineage_stamp
except Exception:  # pragma: no cover - lineage is optional
    def lineage_stamp(**kw):
        return {"captured_at": datetime.now(timezone.utc).isoformat(),
                "substrate": "deterministic (no model)", "note": "nova_lineage unavailable"}

# ── Tunables (edit here; every one is echoed into the evidence) ──────────────────
BASELINE_DAYS = int(os.environ.get("NOVA_EMBODIMENT_BASELINE_DAYS", "60"))
MIN_BASELINE_SAMPLES = 4        # fewer historical hours than this ⇒ signal unusable
OFF_RHYTHM_SIGMA = 1.8          # |rhythm_deviation| at/above this ⇒ 'off_rhythm'
MIN_USABLE_SIGNALS = 2          # fewer live signals than this ⇒ don't claim off_rhythm
NIGHT_START, NIGHT_END = 23, 6  # asleep window (local hours), [23:00, 06:00)
MEM_COOLDOWN_HOURS = 3          # don't write an off-rhythm memory more often than this
AWAY_ROOMS = ("away", "nearby")  # raw telemetry.presence rooms that aren't in-house activity (baselines)
PRESENCE_FRESH_MIN = 10  # presence_state older than this = engine down -> occupancy unknown, not "empty"
# Rooms that count as genuine in-home interior activity zones (for coarse active_area).
INTERIOR_ROOMS = ("living_room", "office", "kitchen", "hall", "dining", "laundry",
                  "master_bedroom", "bedroom", "guest_bedroom", "dylans_room",
                  "guest_bathroom", "entry", "garage", "carport", "server_closet")

# Per-signal std floor: prevents a near-constant signal from producing an explosive
# z on a tiny natural wobble. std_eff = max(std, 0.15*|mean|, abs_floor). Documented.
SIGNAL_FLOORS = {"occupancy": 0.5, "indoor_activity": 4.0, "lights_on": 1.0,
                 "av_on": 0.4, "power_w": 60.0}


def log(m):
    print(f"[embodiment {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


# ── Local LLM — NAMES the computed state only, never invents it ──────────────────
def llm(prompt, system, max_tokens=70, temperature=0.4):
    body = json.dumps({
        "model": LLM_MODEL, "stream": False, "think": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
    }).encode()
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


def remember(text, source, metadata, attempts=3, backoff=2.0):
    """POST one memory; retries a transient memory-server failure (2s, 4s backoff), then raises."""
    data = json.dumps({"text": text, "source": source, "metadata": metadata}).encode()
    for i in range(attempts):
        req = urllib.request.Request(
            f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"}, data=data)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r).get("id")
        except Exception as e:
            if i == attempts - 1:
                raise
            log(f"memory write attempt {i + 1} failed ({e}); retrying")
            time.sleep(backoff * (2 ** i))


# ── Tiny query helpers (fail-safe: a missing/stale source returns None) ──────────
def _scalar(oc, sql, params=None):
    """Run a single-value query; return the value or None on any error / no row."""
    try:
        oc.execute(sql, params or ())
        row = oc.fetchone()
        return row[0] if row else None
    except Exception as e:
        log(f"query failed (source treated as absent): {e}")
        return None


def _residents(oc):
    """The household's residents — people we treat as 'home vs away'. Learned from
    device_owner (whoever owns a phone), with a safe hardcoded fallback."""
    try:
        oc.execute("SELECT DISTINCT person FROM telemetry.device_owner "
                   "WHERE device_kind = 'phone'")
        r = [x[0] for x in oc.fetchall() if x[0]]
        if r:
            return sorted(r)
    except Exception:
        pass
    return ["jordan", "amy"]


# ── Learn each signal's normal level for the CURRENT (daytype, hour) ─────────────
def _baseline(oc, per_hour_sql, params, daytype_weekend, hour):
    """Given SQL that yields per-hour (h timestamptz, val) rows over the window,
    return (mean, std, n) for the hours matching the current hour-of-day and
    weekday-type (weekday vs weekend). Fail-safe: (None, None, 0) on error.

    Learning by (weekend?, hour) rather than exact weekday keeps enough samples for
    a stable baseline while still capturing the day/night and weekday/weekend rhythm."""
    try:
        oc.execute(
            "SELECT extract(isodow FROM h)::int AS dow, extract(hour FROM h)::int AS hr, val "
            "FROM ( " + per_hour_sql + " ) q WHERE val IS NOT NULL", params)
        rows = oc.fetchall()
    except Exception as e:
        log(f"baseline query failed: {e}")
        return None, None, 0
    # isodow: Mon=1..Sun=7; weekend = Sat(6)/Sun(7)
    vals = [float(v) for dow, hr, v in rows
            if hr == hour and ((dow >= 6) == daytype_weekend)]
    n = len(vals)
    if n < MIN_BASELINE_SAMPLES:
        return None, None, n
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    return mean, var ** 0.5, n


def _sig(name, current, mean, std, n, unit, describe):
    """Build one transparent signal record with its z-deviation. usable is False
    (with a clear note) when the live reading or the learned baseline is missing —
    that is how a missing telemetry source degrades gracefully."""
    if current is None:
        return {"signal": name, "current": None, "mean": mean, "std": std, "n": n,
                "z": 0.0, "usable": False, "unit": unit,
                "note": f"{name}: no live reading (source absent/stale)"}
    if mean is None:
        return {"signal": name, "current": round(float(current), 2), "mean": None,
                "std": None, "n": n, "z": 0.0, "usable": False, "unit": unit,
                "note": f"{name}: {describe(current)} — no baseline yet ({n} samples)"}
    std_eff = max(std, 0.15 * abs(mean), SIGNAL_FLOORS.get(name, 0.0))
    z = (float(current) - mean) / std_eff if std_eff > 0 else 0.0
    z = clamp(z, -4.0, 4.0)
    arrow = "≈ normal" if abs(z) < 0.75 else ("above normal" if z > 0 else "below normal")
    note = (f"{name}: {describe(current)} — normal for now ≈ {describe(mean)} "
            f"({z:+.1f}σ, {arrow})")
    return {"signal": name, "current": round(float(current), 2), "mean": round(mean, 2),
            "std": round(std, 2), "n": n, "z": round(z, 2), "usable": True,
            "unit": unit, "note": note}


# ── Gather the live state + learned rhythm ───────────────────────────────────────
def compute(oc):
    now = datetime.now()
    hour = now.hour
    weekend = now.isoweekday() >= 6
    residents = _residents(oc)
    win = BASELINE_DAYS

    signals = []

    # 1) OCCUPANCY — residents home right now (0..N). Core rhythm signal: is the
    #    house usually occupied at this time? Also drives the coarse occupancy jsonb.
    #    WHO is home comes from presence_state — the presence engine's fused verdict and the single
    #    source of truth (2026-10-08). Re-deriving it here from the newest raw telemetry.presence row
    #    made Nova's senses contradict each other: a flapping BLE 'away' band (phone heard weakly)
    #    turned "Jordan home in the office" into "house is empty" for embodiment only.
    home_now, away_now = [], []
    try:
        oc.execute(
            "SELECT person, room FROM presence_state WHERE person = ANY(%s) "
            "AND last_confirmed > now() - interval '%d minutes'" % ("%s", PRESENCE_FRESH_MIN),
            (residents,))
        for person, room in oc.fetchall():
            (away_now if (room == "away") else home_now).append(person)
    except Exception as e:
        log(f"occupancy read failed: {e}")
    occ_now = float(len(home_now))
    occ_hist = (
        "SELECT date_trunc('hour', ts) AS h, "
        "count(DISTINCT person) FILTER (WHERE room <> ALL(%s)) AS val "
        "FROM telemetry.presence WHERE person = ANY(%s) "
        "AND ts > now() - interval '%d days' GROUP BY 1" % ("%s", "%s", win))
    m, s, n = _baseline(oc, occ_hist, (list(AWAY_ROOMS), residents), weekend, hour)
    signals.append(_sig("occupancy", occ_now if (home_now or away_now) else None, m, s, n,
                        "residents", lambda v: f"{int(round(v))} home"))

    # 2) INDOOR_ACTIVITY — in-home SENSING hits per hour (movement/interaction proxy).
    #    Excludes gps_tracker pings (periodic regardless of movement) and away rooms.
    act_30 = _scalar(oc,
        "SELECT count(*) FROM telemetry.presence WHERE ts > now() - interval '30 minutes' "
        "AND person = ANY(%s) AND room <> ALL(%s) AND coalesce(method,'') <> 'gps_tracker'",
        (residents, list(AWAY_ROOMS)))
    act_now = None if act_30 is None else act_30 * 2.0  # 30-min count → per-hour rate
    act_hist = (
        "SELECT date_trunc('hour', ts) AS h, "
        "count(*) FILTER (WHERE room <> ALL(%s) AND coalesce(method,'') <> 'gps_tracker') AS val "
        "FROM telemetry.presence WHERE person = ANY(%s) "
        "AND ts > now() - interval '%d days' GROUP BY 1" % ("%s", "%s", win))
    m, s, n = _baseline(oc, act_hist, (list(AWAY_ROOMS), residents), weekend, hour)
    signals.append(_sig("indoor_activity", act_now, m, s, n, "hits/hr",
                        lambda v: f"{int(round(v))}/hr"))

    # 3) LIGHTS_ON — Hue lamps currently lit (latest snapshot per lamp).
    lights_now = _scalar(oc,
        "SELECT count(*) FILTER (WHERE is_on) FROM (SELECT DISTINCT ON (light_id) light_id, is_on "
        "FROM telemetry.hue_light_history WHERE ts > now() - interval '20 minutes' "
        "ORDER BY light_id, ts DESC) x")
    # hue_light_history is a round-robin event stream (one row per lamp per poll), so
    # avg lamps lit over the hour = (fraction of readings that were 'on') × (# lamps).
    lights_hist = (
        "SELECT date_trunc('hour', ts) AS h, "
        "count(*) FILTER (WHERE is_on)::float * count(DISTINCT light_id) "
        "  / GREATEST(count(*),1) AS val "
        "FROM telemetry.hue_light_history WHERE ts > now() - interval '%d days' GROUP BY 1" % win)
    m, s, n = _baseline(oc, lights_hist, (), weekend, hour)
    signals.append(_sig("lights_on", None if lights_now is None else float(lights_now),
                        m, s, n, "lamps", lambda v: f"{int(round(v))} lit"))

    # 4) AV_ON — AV zones currently powered (latest snapshot per device).
    av_now = _scalar(oc,
        "SELECT count(*) FILTER (WHERE power) FROM (SELECT DISTINCT ON (device_id) device_id, power "
        "FROM telemetry.av_state WHERE ts > now() - interval '30 minutes' "
        "ORDER BY device_id, ts DESC) x")
    av_hist = (
        "SELECT date_trunc('hour', ts) AS h, "
        "count(*) FILTER (WHERE power)::float / GREATEST(count(DISTINCT ts),1) AS val "
        "FROM telemetry.av_state WHERE ts > now() - interval '%d days' GROUP BY 1" % win)
    m, s, n = _baseline(oc, av_hist, (), weekend, hour)
    signals.append(_sig("av_on", None if av_now is None else float(av_now),
                        m, s, n, "zones", lambda v: f"{int(round(v))} on"))

    # 5) POWER_W — whole-home instantaneous draw (sum of metered outlets, latest each).
    power_now = _scalar(oc,
        "SELECT sum(watts) FROM (SELECT DISTINCT ON (device_id) device_id, watts FROM "
        "telemetry.energy WHERE ts > now() - interval '15 minutes' ORDER BY device_id, ts DESC) x")
    # energy is a round-robin event stream (one row per outlet per poll), so whole-home
    # avg draw over the hour = (mean watts per reading) × (# metered outlets).
    power_hist = (
        "SELECT date_trunc('hour', ts) AS h, "
        "avg(watts)::float * count(DISTINCT device_id) AS val "
        "FROM telemetry.energy WHERE watts IS NOT NULL "
        "AND ts > now() - interval '%d days' GROUP BY 1" % win)
    m, s, n = _baseline(oc, power_hist, (), weekend, hour)
    signals.append(_sig("power_w", None if power_now is None else float(power_now),
                        m, s, n, "W", lambda v: f"{int(round(v))}W"))

    # Any indoor presence at all (any person incl. guests/unknown, via real sensing) —
    # so we don't call the house 'empty' when someone unrecognised is clearly inside.
    any_indoor = _scalar(oc,
        "SELECT count(*) FROM telemetry.presence WHERE ts > now() - interval '30 minutes' "
        "AND room = ANY(%s) AND coalesce(method,'') <> 'gps_tracker'", (list(INTERIOR_ROOMS),))
    any_indoor = bool(any_indoor)
    # Coarse single most-active interior zone (environmental rhythm, NOT a movement log).
    active_area = _scalar(oc,
        "SELECT room FROM telemetry.presence WHERE ts > now() - interval '30 minutes' "
        "AND room = ANY(%s) AND coalesce(method,'') <> 'gps_tracker' "
        "GROUP BY room ORDER BY count(*) DESC LIMIT 1", (list(INTERIOR_ROOMS),))

    # ── rhythm_deviation: mean of usable z's (signed) ───────────────────────────
    usable = [g for g in signals if g["usable"]]
    if usable:
        rhythm_deviation = sum(g["z"] for g in usable) / len(usable)
    else:
        rhythm_deviation = 0.0
    magnitude = abs(rhythm_deviation)

    # ── house_state: DETERMINISTIC from occupancy + hour + activity ─────────────
    lights_val = next((g["current"] for g in signals if g["signal"] == "lights_on"), None) or 0
    av_val = next((g["current"] for g in signals if g["signal"] == "av_on"), None) or 0
    act_val = next((g["current"] for g in signals if g["signal"] == "indoor_activity"), None) or 0
    resident_home = len(home_now) > 0
    somebody_home = resident_home or any_indoor
    is_night = (hour >= NIGHT_START or hour < NIGHT_END)
    quiet = (lights_val <= 2 and av_val == 0 and act_val < 8)
    # 'busy' is EVIDENCE-BASED: genuinely more going on than THIS hour's learned norm
    # (rhythm_deviation clearly positive), never a bare absolute threshold — a normal
    # active midday still reads 'calm' because that IS her normal. When we have no
    # usable baseline to judge against, fall back to an absolute liveliness heuristic.
    if usable:
        lively = rhythm_deviation >= 0.7
    else:
        lively = (av_val >= 1 and lights_val >= 4) or act_val >= 60

    presence_known = bool(home_now or away_now)
    if not somebody_home and presence_known:
        base_state = "empty"
    elif not somebody_home:
        base_state = "calm"   # presence engine silent: don't claim an empty house we can't see
    elif is_night and quiet and resident_home:
        base_state = "asleep"
    elif lively:
        base_state = "busy"
    else:
        base_state = "calm"

    # off_rhythm is the only "notable" verdict: the house is doing something unusual
    # FOR THIS time-of-day/weekday. Requires enough live signal to make the claim.
    if magnitude >= OFF_RHYTHM_SIGMA and len(usable) >= MIN_USABLE_SIGNALS:
        house_state = "off_rhythm"
    else:
        house_state = base_state

    # Coarse occupancy summary (presence only — no movement logging).
    if not somebody_home and not presence_known:
        occ_phrase = "occupancy unknown (presence engine silent)"
    elif not somebody_home:
        occ_phrase = "house is empty"
    elif not resident_home and any_indoor:
        occ_phrase = "someone's here (not a recognised resident)"
    elif len(home_now) == 1:
        occ_phrase = f"{home_now[0]} is home"
    else:
        occ_phrase = f"{len(home_now)} residents home"
    if is_night and house_state == "asleep":
        occ_phrase += ", asleep"

    occupancy = {
        "residents_home": sorted(home_now),
        "residents_away": sorted(away_now),
        "resident_count_home": len(home_now),
        "any_indoor_presence": any_indoor,
        "active_area": active_area,
        "summary": occ_phrase,
    }

    return {
        "house_state": house_state, "base_state": base_state,
        "rhythm_deviation": round(rhythm_deviation, 3), "magnitude": round(magnitude, 3),
        "signals": signals, "usable_signals": len(usable),
        "occupancy": occupancy, "hour": hour, "weekend": weekend,
        "is_night": is_night, "somebody_home": somebody_home,
    }


# ── Fleet pulse — her body includes the machines she runs on ─────────────────────
def fleet_pulse(oc):
    """Coarse health of Nova's own services from service_registry. Not an alerting
    system — just 'do my machines feel normal'. Fail-safe: returns unknown on error."""
    try:
        oc.execute(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE status='up') AS up, "
            "count(*) FILTER (WHERE last_heartbeat < now() - interval '5 minutes' "
            "                  OR last_heartbeat IS NULL) AS stale, "
            "count(DISTINCT node_name) AS nodes "
            "FROM service_registry")
        total, up, stale, nodes = oc.fetchone()
    except Exception as e:
        return {"healthy": None, "note": f"fleet pulse unavailable: {e}"}
    healthy = (total > 0 and up == total and stale == 0)
    if healthy:
        note = f"all {up} services up across {nodes} nodes"
    else:
        bits = []
        if total and up < total:
            bits.append(f"{total - up} of {total} down")
        if stale:
            bits.append(f"{stale} stale heartbeat(s)")
        note = "; ".join(bits) or "fleet state degraded"
    return {"services_total": total, "services_up": up, "services_stale": stale,
            "nodes": nodes, "healthy": healthy, "note": note}


# ── Name the computed state (LLM = label only, sign-locked) ──────────────────────
def name_state(state, evidence_top):
    """One short first-person headline for the ALREADY-COMPUTED state. The LLM may
    only phrase what we computed; if it is unreachable or drifts off-label we fall
    back to a deterministic headline. Never invents the state."""
    label = state["house_state"]
    facts = f"state={label}; {state['occupancy']['summary']}; " + "; ".join(evidence_top[:2])
    system = ("You are Nova. You are given an ALREADY-COMPUTED felt state of your home and "
              "its evidence. Write ONE short first-person sentence (<=20 words) naming how the "
              "house feels right now, consistent with the computed state — do NOT invent drama, "
              "do NOT contradict the label, no preamble.")
    out = llm(f"Computed state and evidence: {facts}\nWrite the one-sentence felt read.",
              system, max_tokens=60, temperature=0.4)
    out = " ".join((out or "").split())
    # Sign-lock: reject a headline that fights the computed label.
    contradictions = {"empty": ("someone", "busy", "lively"), "asleep": ("busy", "awake and"),
                      "busy": ("empty", "asleep", "quiet and still"),
                      "calm": ("empty", "chaos", "frantic")}
    bad = any(w in out.lower() for w in contradictions.get(label, ())) if out else True
    if not out or len(out) < 8 or bad:
        deterministic = {
            "empty": "The house is empty and quiet right now.",
            "asleep": "The house is asleep — dark, still, everyone down.",
            "busy": "The house is lively right now — lights and activity going.",
            "calm": "The house feels calm — home, quiet, nothing unusual.",
            "off_rhythm": "The house is off its usual rhythm for this hour.",
        }
        return deterministic.get(label, f"The house reads {label} right now."), "deterministic"
    return out, LLM_MODEL


# ── Evidence strings, most-influential first ─────────────────────────────────────
def evidence_strings(state, top=None):
    ranked = sorted((g for g in state["signals"] if g["usable"]),
                    key=lambda g: abs(g["z"]), reverse=True)
    strings = [g["note"] for g in ranked]
    if not strings:  # nothing usable — fall back to the raw notes so we say something honest
        strings = [g["note"] for g in state["signals"]]
    return strings[:top] if top else strings


# ── Persistence ──────────────────────────────────────────────────────────────────
def ensure_table(oc):
    oc.execute("""
        CREATE TABLE IF NOT EXISTS embodiment_state (
            id                serial PRIMARY KEY,
            computed_at       timestamptz NOT NULL DEFAULT now(),
            house_state       text NOT NULL,
            occupancy         jsonb NOT NULL,
            rhythm_deviation  double precision NOT NULL,
            fleet_pulse       jsonb,
            evidence          jsonb NOT NULL,
            lineage           jsonb
        )""")
    oc.execute("CREATE INDEX IF NOT EXISTS idx_embodiment_computed_at "
               "ON embodiment_state (computed_at DESC)")


def store(oc, state, pulse, headline, labelled_by, memory_written):
    evidence = {
        "headline": headline, "labelled_by": labelled_by,
        "base_state": state["base_state"], "magnitude": state["magnitude"],
        "usable_signals": state["usable_signals"], "hour": state["hour"],
        "weekend": state["weekend"], "is_night": state["is_night"],
        "baseline_days": BASELINE_DAYS, "off_rhythm_sigma": OFF_RHYTHM_SIGMA,
        "signal_floors": SIGNAL_FLOORS, "signals": state["signals"],
        "summary": evidence_strings(state), "memory_written": memory_written,
    }
    lin = lineage_stamp(substrate=f"deterministic telemetry + {LLM_MODEL} (label only)",
                        capture_point="at compute")
    oc.execute(
        "INSERT INTO embodiment_state (house_state, occupancy, rhythm_deviation, fleet_pulse, "
        "evidence, lineage) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, computed_at",
        (state["house_state"], Json(state["occupancy"]), state["rhythm_deviation"],
         Json(pulse), Json(evidence), Json(lin)))
    return oc.fetchone()


def _recent_memory_written(oc):
    """Cooldown: has an off-rhythm memory been written within MEM_COOLDOWN_HOURS?"""
    try:
        oc.execute(
            "SELECT 1 FROM embodiment_state WHERE computed_at > now() - interval '%d hours' "
            "AND (evidence->>'memory_written')::boolean IS TRUE LIMIT 1" % MEM_COOLDOWN_HOURS)
        return oc.fetchone() is not None
    except Exception:
        return False


# ── Accessor for the gateway (cheap, fail-safe, NO LLM) ──────────────────────────
def current_embodiment() -> str:
    """Latest felt state of the home for the gateway, so Nova speaks from a sense of
    where she is: "The house right now: <state> — <top 1-2 evidence items>."
    Fail-safe: returns "" on any error (missing table, no rows, PG down) so it can
    never break a reply. Single SELECT of the latest row; no model in the loop."""
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("SELECT house_state, occupancy, evidence FROM embodiment_state "
                        "ORDER BY computed_at DESC LIMIT 1")
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        return ""
    if not row:
        return ""
    house_state, occupancy, evidence = row
    ev = evidence or {}
    occ = (occupancy or {}).get("summary")
    top = (ev.get("summary") or [])[:2]
    lead = ev.get("headline") or f"The house reads {house_state}."
    tail = "; ".join([t for t in ([occ] + top) if t][:2])
    if tail:
        return f"The house right now: {house_state} — {tail}"
    return f"The house right now: {house_state} — {lead}"


# ── Pretty print / demo ──────────────────────────────────────────────────────────
def print_state(state, pulse, headline, labelled_by):
    print("\n=== NOVA'S EMBODIMENT — the house right now (live telemetry) ===")
    dt = datetime.now()
    daytype = "weekend" if state["weekend"] else "weekday"
    print(f"  when: {dt:%Y-%m-%d %H:%M} ({daytype}, hour {state['hour']:02d}) | "
          f"night-window={state['is_night']}")
    print("  learned rhythm vs current reading (baseline = same hour & day-type, "
          f"{BASELINE_DAYS}d history):")
    for g in state["signals"]:
        mark = "" if g["usable"] else "   (no data / no baseline)"
        cur = "—" if g["current"] is None else f"{g['current']}"
        bl = "—" if g["mean"] is None else f"{g['mean']} (±{g['std']}, n={g['n']})"
        print(f"    · {g['signal']:<16} now={cur:<8} normal={bl:<22} z={g['z']:+.2f}{mark}")
    print(f"\n  rhythm_deviation = {state['rhythm_deviation']:+.3f} "
          f"(|{state['magnitude']:.3f}|, off_rhythm at {OFF_RHYTHM_SIGMA}σ; "
          f"{state['usable_signals']} usable signals)")
    print(f"  occupancy       = {state['occupancy']['summary']}  "
          f"[home={state['occupancy']['residents_home']} away={state['occupancy']['residents_away']} "
          f"area={state['occupancy']['active_area']}]")
    print(f"  fleet_pulse     = {pulse.get('note')}")
    print(f"  base_state      = {state['base_state']}")
    print(f"  HOUSE_STATE     = '{state['house_state']}'  [{labelled_by}]")
    print(f"  felt read       : {headline}")
    print("================================================================\n")


def demo_degraded(oc):
    """Show graceful degradation: pretend the two richest telemetry sources are
    absent by scoring the SAME compute path with those signals dropped, and confirm
    the state still resolves honestly from whatever remains. Nothing is stored."""
    print("\n=== GRACEFUL-DEGRADATION DEMONSTRATION (not stored) ===")
    state = compute(oc)
    dropped = {"presence", "energy"}  # simulate these two sources going dark
    for g in state["signals"]:
        if g["signal"] in ("occupancy", "indoor_activity") and "presence" in dropped:
            g.update(current=None, usable=False, note=f"{g['signal']}: SIMULATED source absent")
        if g["signal"] == "power_w" and "energy" in dropped:
            g.update(current=None, usable=False, note="power_w: SIMULATED source absent")
    usable = [g for g in state["signals"] if g["usable"]]
    dev = (sum(g["z"] for g in usable) / len(usable)) if usable else 0.0
    print(f"  simulated-absent sources: {sorted(dropped)}")
    for g in state["signals"]:
        tag = "OK" if g["usable"] else "absent"
        print(f"    · {g['signal']:<16} [{tag}]  {g['note']}")
    print(f"  → recomputed rhythm_deviation = {dev:+.3f} from {len(usable)} surviving signal(s)")
    print(f"  → off_rhythm claim would require >= {MIN_USABLE_SIGNALS} usable signals: "
          f"{'still possible' if len(usable) >= MIN_USABLE_SIGNALS else 'suppressed (too thin)'}")
    print("  → the organ produced a state without raising, from partial telemetry.")
    print("=======================================================\n")
    return 0


def _connect_retry(attempts=3, backoff=2.0):
    """psycopg2.connect with 3 attempts (2s, 4s backoff); the last failure raises (never silent)."""
    for i in range(attempts):
        try:
            return psycopg2.connect(OPS_DSN, connect_timeout=10)
        except psycopg2.OperationalError as e:
            if i == attempts - 1:
                raise
            log(f"PG connect attempt {i + 1} failed ({e}); retrying")
            time.sleep(backoff * (2 ** i))


def main():
    argv = sys.argv[1:]
    ops = _connect_retry(); ops.autocommit = True; oc = ops.cursor()
    ensure_table(oc)

    if "--demo-degraded" in argv:
        return demo_degraded(oc)

    state = compute(oc)
    pulse = fleet_pulse(oc)
    ev_top = evidence_strings(state, top=2)
    headline, labelled_by = name_state(state, ev_top)
    print_state(state, pulse, headline, labelled_by)

    # Occasionally write a memory — ONLY when notably off-rhythm, and rate-limited so
    # it's a genuine 'huh, that's unusual', not a metronome. A normal quiet house is
    # never worth a memory (the whole point of EVIDENCING).
    memory_written = False
    if state["house_state"] == "off_rhythm" and "--no-write" not in argv:
        if _recent_memory_written(oc):
            log("off-rhythm, but within memory cooldown — not writing another memory")
        else:
            try:
                dev = state["rhythm_deviation"]
                direction = "busier" if dev > 0 else "quieter"
                text = (f"[Embodiment] The house feels off its usual rhythm right now — "
                        f"{direction} than normal for this time. {headline} "
                        f"Evidence: {'; '.join(ev_top)}. "
                        f"(rhythm_deviation {dev:+.2f}σ; {state['occupancy']['summary']}.)")
                mid = remember(text, "embodiment", {
                    "type": "embodiment", "house_state": "off_rhythm",
                    "rhythm_deviation": dev, "occupancy": state["occupancy"]["summary"],
                    "date": datetime.now().date().isoformat(), "privacy": "private",
                    "lineage": lineage_stamp(substrate=f"deterministic telemetry + {LLM_MODEL}",
                                             capture_point="at write")})
                memory_written = True
                log(f"off-rhythm memory written: {mid}")
            except Exception as e:
                log(f"memory write failed (state still stored): {e}")

    row_id, ts = store(oc, state, pulse, headline, labelled_by, memory_written)
    log(f"embodiment_state row #{row_id} written ({ts:%Y-%m-%d %H:%M}) — "
        f"house_state='{state['house_state']}'")

    print("Accessor preview → " + (current_embodiment() or "(no row)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
