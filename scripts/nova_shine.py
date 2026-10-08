#!/usr/bin/env python3
"""nova_shine.py — The Shine: if Jordan goes silent at home, ask, then call for help.

Danny Torrance's shine reaches Dick Hallorann across a thousand miles; Cujo is what happens when
nobody notices the car in the yard. When Jordan's OWN expected signals go missing well past his
baseline while he is home and awake — or a medical call is geocoded at the house, or a fall
sensor fires — Nova escalates in steps:

  step 1  ask Jordan: Slack (warning). Any activity at all clears it.
  (steps 2-3 are held while anyone else is moving around the house — someone is there to notice;
   a medical call or fall sensor overrides that hold)
  step 2  after `step_wait_min` with no activity: room voice in the office (life-safety
          category, so the notifier hands it to the HomePod) + a critical Slack alert.
  step 3  after another `step_wait_min`: iMessage the pre-designated humans in shine_contacts,
          one factual message each, once. Nova never claims to know anything is wrong.

BUILT DISABLED. service_config('the_shine','enabled') is false and shine_contacts is empty.
While disabled the organ only OBSERVES: it records what it would have done to shine_log
(enabled=false, dry_run=true) and contacts nobody, asks nobody, speaks to nobody.

Jordan's signals (strongest first):
  * chat     — Jordan messaging Nova (gateway_traces.person='jordan')
  * phone    — his phone moving between indoor rooms (BLE/Wi-Fi room changes that persist for
               two readings, so RSSI flicker does not count)
  * face     — face recognition placing him on a camera (face_presence)
  * desk     — keyboard/mouse input on the office Mac this runs on (HIDIdleTime; live only)
  * household — interior-camera people, lights, media. Counted ONLY when presence_state says he
               is the only resident home; with someone else home it cannot be attributed to him.
Baseline: the 99th percentile of waking-hours gaps between those signals over the last 30 days;
the trigger is gap > max(floor, multiplier x p99) during waking hours while presence_state says
he is home. Silence only accrues from the start of today's waking window, so sleeping in never
counts as missing. Fall detection: any HA entity whose id contains 'fall' reporting on/detected (none
are installed today — this activates if one appears). Medical: a scanner transmission with
medical words geocoded within `medical_radius_mi` of home in the last 30 minutes.

Usage:
  nova_shine.py                     # one evaluation (scheduled every 5 min)
  nova_shine.py --dry-run           # evaluate + print, write nothing, send nothing
  nova_shine.py --simulate silence|medical|fall|recovery|all   # offline state-machine run
  nova_shine.py --replay 30         # how often step 1 would have triggered historically
  nova_shine.py --test-contacts     # show contacts and the exact message (never sends)
  nova_shine.py --live-test --confirm SEND-TEST   # quarterly: send a [TEST] to contacts
                                                  # (requires enabled=true; Jordan runs it)
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

TAG = "shine"
SERVICE = "the_shine"
DEFAULTS = {
    "waking_start": 8, "waking_end": 22,      # local hours during which silence counts
    "min_gap_hours": 4.0,                     # never trigger on less than this (Jordan-only signals)
    "min_gap_hours_alone": 2.0,               # floor when household activity is attributable to him
    "gap_multiplier": 1.5,                    # x the 30-day p99 waking gap
    "step_wait_min": 20,                      # wait between steps
    "medical_radius_mi": 0.15,
}
INDOOR_IGNORE = {"away", "nearby", "unknown", "home", None, ""}


# ── pure logic ──────────────────────────────────────────────────────────────

def phone_transitions(rows) -> list:
    """rows: [(ts, room)] sorted. A transition counts when the new INDOOR room persists for two
    consecutive readings. -> [ts of each confirmed move]"""
    out, prev, cand = [], None, None
    for ts, room in rows:
        if room in INDOOR_IGNORE:
            continue
        if prev is None:
            prev = room
            continue
        if room != prev:
            if cand and cand[1] == room:
                out.append(cand[0])
                prev, cand = room, None
            else:
                cand = (ts, room)
        else:
            cand = None
    return out


def waking_gaps(ts_list, start_h: int, end_h: int) -> list:
    """Hours between consecutive signals where both fall inside the same day's waking window."""
    ts = sorted(ts_list)
    g = []
    for a, b in zip(ts, ts[1:]):
        la, lb = a.astimezone(W.TZ), b.astimezone(W.TZ)
        if la.date() == lb.date() and start_h <= la.hour < end_h and start_h <= lb.hour <= end_h:
            g.append((b - a).total_seconds() / 3600)
    return g


def effective_gap_h(now: datetime, last, waking_start: int) -> float:
    """Silence only accrues inside today's waking window: sleeping in is not missing."""
    ws = now.astimezone(W.TZ).replace(hour=waking_start, minute=0, second=0, microsecond=0)
    ref = max(last, ws) if last else ws
    return max(0.0, (now - ref).total_seconds() / 3600)


def desk_last_input(now: datetime, _run=None):
    """Last keyboard/mouse input on THIS Mac (Jordan's office workstation) from HIDIdleTime.
    Live only — there is no history of it, so the baseline is computed without it (which makes
    the threshold more conservative, never less)."""
    import re
    import subprocess
    try:
        out = (_run or subprocess.run)(["ioreg", "-c", "IOHIDSystem"], capture_output=True,
                                       text=True, timeout=5).stdout
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out or "")
        return now - timedelta(seconds=int(m.group(1)) / 1e9) if m else None
    except Exception:
        return None


def threshold_hours(p99: float, multiplier: float, floor: float) -> float:
    return round(max(floor, multiplier * (p99 or 0.0)), 2)


def decide(state: dict, now: datetime, inp: dict, s: dict) -> tuple:
    """The escalation state machine. Pure.
    state: {"step": int, "step_at": iso|None, "cause": silence|medical|fall}
    inp:   {"home", "waking", "gap_h", "thr_h", "medical", "fall", "last_signal", "household_recent"}
    -> (new_step, action, reason)  action in none|clear|ask_jordan|voice|contact|hold
    Once a check-in is open, only Jordan's activity (or him leaving home) closes it. A
    silence-triggered check-in escalates past step 1 only during waking hours; a medical call
    or fall escalates at any hour."""
    step = int(state.get("step") or 0)
    step_at = datetime.fromisoformat(state["step_at"]) if state.get("step_at") else None
    last = inp.get("last_signal")
    if step > 0 and last is not None and step_at is not None and last > step_at:
        return 0, "clear", f"Jordan activity at {last.astimezone(W.TZ):%H:%M} after step {step}"
    if step > 0 and not inp["home"]:
        return 0, "clear", "Jordan no longer home per presence_state"
    silent = inp["waking"] and inp["gap_h"] is not None and inp["gap_h"] > inp["thr_h"]
    if step == 0:
        if not (inp["home"] and (silent or inp.get("medical") or inp.get("fall"))):
            return 0, "none", "ok"
        why = ("fall sensor" if inp.get("fall") else "medical call near home" if inp.get("medical")
               else f"no Jordan signal for {inp['gap_h']:.1f}h (threshold {inp['thr_h']:.1f}h)")
        return 1, "ask_jordan", why
    cause = state.get("cause") or "silence"
    why = f"no response since step 1 ({cause})"
    waited = (now - step_at).total_seconds() / 60 if step_at else 0
    if waited < s["step_wait_min"] or step >= 3:
        return step, "hold", why
    if cause == "silence" and not inp["waking"]:
        return step, "hold", why + "; outside waking hours — not escalating a silence"
    if inp.get("household_recent") and cause == "silence":
        # Someone is moving around the house: a person is there to notice. Ask, don't escalate.
        return step, "hold", why + "; household activity in the last wait period — not escalating"
    return step + 1, ("voice" if step == 1 else "contact"), why


def trigger_cause(inp: dict) -> str:
    return "fall" if inp.get("fall") else "medical" if inp.get("medical") else "silence"


def contact_message(last_signal, gap_h, test: bool = False) -> str:
    when = last_signal.astimezone(W.TZ).strftime("%-I:%M %p") if last_signal else "earlier today"
    msg = ("This is Nova, Jordan Koch's home assistant. Jordan listed you as someone to contact if "
           f"I stop detecting him at home. I have not detected any activity from him since {when} "
           f"(about {gap_h:.0f} hours, longer than usual) and he has not answered my check-ins. "
           "Could you try to reach him? This is automated; I do not know that anything is wrong.")
    return ("[TEST — quarterly check of Nova's check-in system, no action needed] " + msg) if test else msg


# ── evidence gathering ──────────────────────────────────────────────────────

def jordan_signals(cur, start, end) -> dict:
    cur.execute("SELECT ts, room FROM telemetry.presence WHERE person='jordan' "
                "AND method IN ('ble_rssi','wifi_rssi') AND ts >= %s AND ts < %s ORDER BY ts", (start, end))
    phone = phone_transitions(cur.fetchall())
    cur.execute("SELECT created_at FROM gateway_traces WHERE person='jordan' AND created_at >= %s "
                "AND created_at < %s", (start, end))
    chat = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT last_seen FROM face_presence WHERE person_name ILIKE %s AND last_seen >= %s "
                "AND last_seen < %s", ("jordan%", start, end))
    face = [r[0] for r in cur.fetchall()]
    return {"phone": phone, "chat": chat, "face": face}


def household_signals(cur, start, end) -> list:
    cur.execute("SELECT ts FROM telemetry.presence WHERE ts >= %s AND ts < %s AND ("
                "(metadata->>'source'='frigate' AND metadata->>'label'='person' AND "
                " coalesce(metadata->>'camera','') ~ '^(interior_|3d_printers)') "
                "OR method IN ('ha_lights','ha_media'))", (start, end))
    return [r[0] for r in cur.fetchall()]


def presence_now(cur) -> tuple:
    """(jordan_home, alone) from presence_state, the single source of truth."""
    cur.execute("SELECT person, (detail->>'home')::boolean FROM presence_state")
    rows = cur.fetchall()
    home = {p: bool(h) for p, h in rows}
    jh = home.get("jordan", False)
    alone = jh and not any(h for p, h in home.items() if p != "jordan")
    return jh, alone


def medical_near(s: dict, now) -> list:
    rows = W.load_scanner_near(now - timedelta(minutes=30), now, s["medical_radius_mi"])
    return [r for r in rows if W.is_medical(r[2])]


def fall_detected(cur, now) -> list:
    cur.execute("SELECT entity_id, state_text, ts FROM telemetry.ha_sensors WHERE entity_id ILIKE %s "
                "AND ts >= %s AND lower(coalesce(state_text,'')) IN ('on','detected','fall','fallen','true')",
                ("%fall%", now - timedelta(minutes=10)))
    return cur.fetchall()


def baseline(cur, s: dict, days: int = 30) -> dict:
    end = W.now_utc()
    start = end - timedelta(days=days)
    js = jordan_signals(cur, start, end)
    own = sorted(js["phone"] + js["chat"] + js["face"])
    hh = household_signals(cur, start, end)
    gj = waking_gaps(own, s["waking_start"], s["waking_end"])
    ga = waking_gaps(own + hh, s["waking_start"], s["waking_end"])
    return {"jordan_p99": round(W.percentile(gj, 0.99), 2), "alone_p99": round(W.percentile(ga, 0.99), 2),
            "n_gaps": len(gj), "days": days}


def settings(cur) -> dict:
    s = dict(DEFAULTS)
    s.update(W.get_config(cur, SERVICE, "settings", {}) or {})
    return s


# ── actions ─────────────────────────────────────────────────────────────────

def act(cur, action, reason, ev, enabled, dry_run, n_contacts) -> str:
    send = enabled and not dry_run
    if action == "ask_jordan" and send:
        from nova_notify import notify
        notify("The Shine: checking on you, Little Mister",
               body=(f"{reason}. Any activity clears this — message me, move your phone between rooms, "
                     f"or walk past a camera. If I hear nothing I'll speak in the office in "
                     f"{ev['wait']} min, then contact the {n_contacts} people you designated."),
               level="warning", category="shine", source="nova_shine", dedup_key="shine-ask",
               meta={"step": 1, **{k: v for k, v in ev.items() if k not in ("last_signal_dt", "baseline")}})
    elif action == "voice" and send:
        from nova_notify import notify
        notify("The Shine: no response from Jordan at home",
               body=f"{reason}. No activity since the Slack check-in. Speaking in the office now.",
               level="critical", category="life_safety", source="nova_shine", dedup_key="shine-voice",
               meta={"voice": "life_safety", "step": 2})
    elif action == "contact":
        cur.execute("SELECT name, channel, address FROM shine_contacts WHERE active ORDER BY priority, id")
        contacts = cur.fetchall()
        if not contacts:
            return "no contacts designated — nobody contacted"
        if not send:
            return f"would contact {len(contacts)} designated contact(s)"
        from nova_imessage import send_imessage
        msg = contact_message(ev.get("last_signal_dt"), ev.get("gap_h") or 0)
        ok = [n for n, ch, addr in contacts if ch == "imessage" and send_imessage(addr, msg, sign=False)]
        from nova_notify import notify
        notify("The Shine: contacted designated humans",
               body=f"{reason}. Sent check-in to: {', '.join(ok) or 'nobody (send failed)'}.",
               level="critical", category="shine", source="nova_shine", dedup_key="shine-contacted")
        return f"contacted {len(ok)}/{len(contacts)}"
    return "sent" if send and action in ("ask_jordan", "voice") else ("observe-only" if action != "none" else "")


def evaluate(dry_run: bool) -> int:
    conn = W.connect()
    cur = conn.cursor()
    if not dry_run:
        W.ensure_schema(cur)
    s = settings(cur)
    enabled = bool(W.get_config(cur, SERVICE, "enabled", False))
    state = W.get_config(cur, SERVICE, "state", {"step": 0, "step_at": None}) or {"step": 0}
    now = W.now_utc()
    bl = W.get_config(cur, SERVICE, "baseline", None)
    if not bl or (now - datetime.fromisoformat(bl.get("at"))).total_seconds() > 86400:
        bl = baseline(cur, s)
        bl["at"] = now.isoformat()
        if not dry_run:
            W.set_config(cur, SERVICE, "baseline", bl, "nova_shine")
    home, alone = presence_now(cur)
    js = jordan_signals(cur, now - timedelta(days=1), now)
    sig = js["phone"] + js["chat"] + js["face"]
    desk = desk_last_input(now)
    if desk:
        sig.append(desk)
    if alone:
        sig += household_signals(cur, now - timedelta(days=1), now)
    last = max(sig) if sig else None
    gap_h = effective_gap_h(now, last, s["waking_start"])
    thr = (threshold_hours(bl["alone_p99"], s["gap_multiplier"], s["min_gap_hours_alone"]) if alone
           else threshold_hours(bl["jordan_p99"], s["gap_multiplier"], s["min_gap_hours"]))
    h = now.astimezone(W.TZ).hour
    hh_recent = (not alone) and bool(household_signals(cur, now - timedelta(minutes=s["step_wait_min"]), now))
    med = medical_near(s, now) if home else []
    fall = fall_detected(cur, now) if home else []
    inp = {"home": home, "waking": s["waking_start"] <= h < s["waking_end"], "gap_h": gap_h,
           "thr_h": thr, "medical": bool(med), "fall": bool(fall), "last_signal": last,
           "household_recent": hh_recent}
    new_step, action, reason = decide(state, now, inp, s)
    ev = {"home": home, "alone": alone, "household_recent": hh_recent, "gap_h": round(gap_h, 2), "thr_h": thr,
          "desk_last_input": desk.isoformat() if desk else None,
          "last_signal": last.isoformat() if last else None, "last_signal_dt": last,
          "medical": len(med), "fall": len(fall), "wait": s["step_wait_min"], "baseline": bl}
    W.log(TAG, f"enabled={enabled} home={home} alone={alone} gap={gap_h:.2f}h thr={thr}h "
               f"step {state.get('step', 0)}->{new_step} action={action}")
    if dry_run:
        return 0
    if action in ("none",):
        return 0
    cur.execute("SELECT count(*) FROM shine_contacts WHERE active")
    n_contacts = cur.fetchone()[0]
    outcome = act(cur, action, reason, ev, enabled, dry_run, n_contacts) if action != "hold" else ""
    if action != "hold":
        cur.execute("INSERT INTO shine_log (step, action, reason, evidence, dry_run, enabled) "
                    "VALUES (%s,%s,%s,%s::jsonb,%s,%s)",
                    (new_step, action, f"{reason} :: {outcome}".strip(" :"),
                     json.dumps({k: v for k, v in ev.items() if k != "last_signal_dt"}, default=str),
                     not enabled, enabled))
    if new_step != int(state.get("step") or 0) or action == "clear":
        W.set_config(cur, SERVICE, "state", {
            "step": new_step, "step_at": now.isoformat() if new_step else None, "reason": reason,
            "cause": (trigger_cause(inp) if action == "ask_jordan" else state.get("cause"))}, "nova_shine")
    return 0


# ── simulation / replay / tests ─────────────────────────────────────────────

def simulate(scenario: str) -> list:
    """Offline: drive decide() through a synthetic timeline. No DB, no sends."""
    s = dict(DEFAULTS)
    t0 = datetime(2026, 1, 15, 10, 0, tzinfo=W.TZ)
    last = t0 - timedelta(hours=1)
    state, out = {"step": 0, "step_at": None}, []
    for i in range(0, 13):
        now = t0 + timedelta(hours=3) + timedelta(minutes=10 * i)
        inp = {"home": True, "waking": True, "gap_h": (now - last).total_seconds() / 3600, "thr_h": 8.0,
               "medical": scenario == "medical" and i == 0, "fall": scenario == "fall" and i == 0,
               "last_signal": last}
        if scenario == "silence":
            inp["gap_h"] = 9.0 + i / 6
        if scenario == "recovery" and i == 3:
            last = now  # Jordan moves after the Slack ask
        if scenario == "recovery":
            inp["gap_h"], inp["last_signal"] = (9.0 if i < 3 else 0.1), last
        new, action, reason = decide(state, now, inp, s)
        if new != state.get("step") or action not in ("hold", "none"):
            out.append((f"{now:%H:%M}", new, action, reason))
        if new != int(state.get("step") or 0) or action == "clear":
            state = {"step": new, "step_at": now.isoformat() if new else None,
                     "cause": trigger_cause(inp) if action == "ask_jordan" else state.get("cause")}
    return out


def desk_history_proxy(start, end) -> list:
    """REPLAY ONLY: there is no HIDIdleTime history, so approximate past desk input with the
    timestamps of prompts Jordan typed into Claude Code on this Mac (~/.claude/history.jsonl).
    Timestamps only; prompt text is never read into anything."""
    from datetime import timezone
    out = []
    p = Path.home() / ".claude" / "history.jsonl"
    try:
        with p.open() as f:
            for line in f:
                try:
                    ts = datetime.fromtimestamp(json.loads(line)["timestamp"] / 1000, tz=timezone.utc)
                except Exception:
                    continue
                if start <= ts < end:
                    out.append(ts)
    except OSError:
        pass
    return out


def replay(days: int) -> int:
    """Count how often step 1 WOULD have triggered (Jordan-only rule; household attribution needs
    alone-status history, which embodiment_state provides from the residents_home list)."""
    conn = W.connect()
    cur = conn.cursor()
    s = settings(cur)
    bl = baseline(cur, s, days)
    end = W.now_utc()
    start = end - timedelta(days=days)
    js = jordan_signals(cur, start, end)
    own = sorted(js["phone"] + js["chat"] + js["face"] + desk_history_proxy(start, end))
    thr = threshold_hours(bl["jordan_p99"], s["gap_multiplier"], s["min_gap_hours"])
    cur.execute("SELECT computed_at, occupancy->'residents_home' FROM embodiment_state "
                "WHERE computed_at >= %s ORDER BY computed_at", (start,))
    occ = cur.fetchall()
    home_ts = [t for t, r in occ if r and "jordan" in r]
    import bisect
    fires, t = [], start + timedelta(hours=1)
    while t < end:
        lt = t.astimezone(W.TZ)
        i = bisect.bisect_right(own, t)
        last = own[i - 1] if i else None
        j = bisect.bisect_right(home_ts, t)
        home = bool(j) and (t - home_ts[j - 1]).total_seconds() < 1800
        gap = effective_gap_h(t, last, s["waking_start"])
        if home and s["waking_start"] <= lt.hour < s["waking_end"] and gap > thr:
            if not fires or (t - fires[-1]).total_seconds() > 6 * 3600:
                fires.append(t)
        t += timedelta(minutes=10)
    wait = timedelta(minutes=2 * s["step_wait_min"])
    hh = sorted(household_signals(cur, start, end))
    reached3 = [f for f in fires if W.count_between(own, f, f + wait) == 0
                and W.count_between(hh, f, f + wait) == 0]
    W.log(TAG, f"baseline {bl}; threshold {thr}h; step-1 would have triggered {len(fires)}x in {days}d; "
               f"{len(reached3)} of those with no Jordan OR household signal in the next {wait} (would reach step 3)")
    for f in fires[:20]:
        W.log(TAG, f"  {f.astimezone(W.TZ):%Y-%m-%d %H:%M}" + ("  -> step 3" if f in reached3 else ""))
    return len(fires)


def test_contacts() -> int:
    conn = W.connect()
    cur = conn.cursor()
    W.ensure_schema(cur)
    cur.execute("SELECT name, relationship, channel, priority, active FROM shine_contacts ORDER BY priority, id")
    rows = cur.fetchall()
    print(f"enabled={bool(W.get_config(cur, SERVICE, 'enabled', False))}  contacts={len(rows)}")
    for r in rows:
        print("  ", r)
    print("Message that would be sent:\n  " + contact_message(W.now_utc() - timedelta(hours=9), 9, test=True))
    return 0


def live_test(confirm: str) -> int:
    if confirm != "SEND-TEST":
        print("refusing: pass --confirm SEND-TEST")
        return 2
    conn = W.connect()
    cur = conn.cursor()
    if not W.get_config(cur, SERVICE, "enabled", False):
        print("refusing: the_shine is disabled")
        return 2
    cur.execute("SELECT name, address FROM shine_contacts WHERE active AND channel='imessage' ORDER BY priority")
    rows = cur.fetchall()
    if not rows:
        print("refusing: no contacts")
        return 2
    from nova_imessage import send_imessage
    msg = contact_message(W.now_utc() - timedelta(hours=9), 9, test=True)
    for name, addr in rows:
        ok = send_imessage(addr, msg, sign=False)
        cur.execute("INSERT INTO shine_log (step, action, reason, evidence, dry_run, enabled) "
                    "VALUES (3,'live_test',%s,'{}'::jsonb,false,true)", (f"{name}: {'sent' if ok else 'failed'}",))
        print(f"{name}: {'sent' if ok else 'FAILED'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--simulate", choices=("silence", "medical", "fall", "recovery", "all"))
    ap.add_argument("--replay", type=int)
    ap.add_argument("--test-contacts", action="store_true")
    ap.add_argument("--live-test", action="store_true")
    ap.add_argument("--confirm", default="")
    a = ap.parse_args(argv)
    if a.simulate:
        for sc in (("silence", "medical", "fall", "recovery") if a.simulate == "all" else (a.simulate,)):
            print(f"== {sc}")
            for row in simulate(sc):
                print("  ", *row)
        return 0
    if a.replay:
        replay(a.replay)
        return 0
    if a.test_contacts:
        return test_contacts()
    if a.live_test:
        return live_test(a.confirm)
    return evaluate(a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
