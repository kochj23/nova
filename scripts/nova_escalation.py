#!/usr/bin/env python3
"""nova_escalation.py — the Two-Man Rule, MOLINK, and fatigue gating for every escalation path.

The security organ already refuses to page CRITICAL on one witness (quorum 2 of unifi/arp/dhcp).
This generalises that to every place Nova climbs a ladder toward Jordan or the outside world:

TWO-MAN RULE. An irreversible or outward action (a page, a voice announcement in the house,
a message to a third party, a physical actuation) needs TWO INDEPENDENT KEYS:
    reasoning   — Nova's own judgement. Present unless Nova is degraded (see below).
    sensors     — >= 2 independent sources of >= 2 different sensor TYPES (nova_spinnaker:
                  shared upstream collapses, motive and confirmation-of-expectation raise the bar).
    jordan      — Jordan's explicit confirmation (a nova_safety_guards confirmation approved by
                  jordan*, consumed single-use; or a MOLINK reply).
    preconsent  — LIFE-SAFETY ONLY: Jordan's pre-consent is key two (Commander's Intent grant
                  'shine.preconsent', or the_shine/enabled while the intent organ is absent).
  Classes: note (0 keys) < ask (1 key: the MOLINK check itself) < alert / outward / irreversible (2 keys).
  SPINNAKER also caps the rung: an UNCORROBORATED conclusion cannot trigger anything but a note.

MOLINK. Before climbing, the cheapest direct check: one Slack line to Jordan, "reply 'fine' in
thread". Any thread reply or any message from him to Nova answers it. An outward action to a third
party requires a MOLINK that went unanswered (life-safety urgent triggers excepted).

FATIGUE GATING.
  Jordan depleted  — relationship quiet_mode active (hard stretch), late night 23:00-07:00, or
                     sleep evidence (bedroom phone / bedroom mmWave in the last 30 min, ignored if
                     CARDINAL marks that sensor compromise-suspect). Non-urgent asks/alerts are
                     DEFERRED (to the next Watch Bill turnover), never dropped.
  Nova degraded    — the gateway reports degraded or is answering on a fallback model, gateway
                     latency high, memory server unreachable, or the Boiler at/over threshold.
                     Her confidence drops (penalty) and every non-life-safety escalation is BLOCKED
                     (held for the turnover, logged as restraint).

Every decision is written to escalation_log; holds/deferrals also to restraint_ledger
(channel 'two-man'). authorize() never raises: if its own plumbing fails it fails OPEN for
life-safety and CLOSED (held + logged) for everything else.

Library: authorize(oc, source=, kind=, action_class=, item=, life_safety=, urgent=, ...) -> dict
         jordan_state(oc), nova_state(oc), molink_ask(oc, ref, text), molink_status(oc, ref)
CLI:     --status (both states now)   --log [--hours 24]   --selftest   --help
         --feedback LOG_ID --note "unneeded: ..." --by jordan   (feeds the hotwash overreach sweep)
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_spinnaker as SP  # noqa: E402

TZ = ZoneInfo("America/Los_Angeles")
CLASSES = {"note": 0, "ask": 1, "alert": 2, "outward": 2, "irreversible": 2}
SPINNAKER_RUNG = {"note": "journal", "ask": "ask", "alert": "escalate", "outward": "escalate",
                  "irreversible": "act"}
DEFAULTS = {"latency_ms": 45000, "night_start": 23, "night_end": 7, "degraded_penalty": 0.25,
            "gateway_health": "http://nova-core.digitalnoise.net:18792/health",
            "memory_health": "http://memory-server.digitalnoise.net:18790/stats"}
CHANNEL = "two-man"

SCHEMA = """
CREATE TABLE IF NOT EXISTS escalation_log (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  source text NOT NULL,
  kind text NOT NULL,
  action_class text NOT NULL,
  rung text,
  allowed boolean NOT NULL,
  deferred boolean NOT NULL DEFAULT false,
  keys jsonb NOT NULL DEFAULT '[]',
  missing jsonb NOT NULL DEFAULT '[]',
  reason text,
  item jsonb NOT NULL DEFAULT '{}',
  life_safety boolean NOT NULL DEFAULT false,
  jordan_state jsonb, nova_state jsonb);
CREATE INDEX IF NOT EXISTS escalation_log_ts ON escalation_log (ts DESC);
ALTER TABLE escalation_log ADD COLUMN IF NOT EXISTS feedback text;
ALTER TABLE escalation_log ADD COLUMN IF NOT EXISTS feedback_by text;
"""


def log(m: str) -> None:
    print(f"[escalation {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(oc) -> None:
    oc.execute(SCHEMA)


def settings(oc) -> dict:
    s = dict(DEFAULTS)
    try:
        oc.execute("SELECT value FROM service_config WHERE service='escalation' AND key='settings'")
        r = oc.fetchone()
        if r:
            v = r[0] if isinstance(r[0], dict) else json.loads(r[0])
            s.update(v or {})
    except Exception:  # noqa: BLE001
        _rollback(oc)
    return s


def _rollback(oc) -> None:
    try:
        oc.connection.rollback()
    except Exception:  # noqa: BLE001
        pass


def http_ok(url: str, attempts: int = 3, timeout: float = 4.0, _open=None, _sleep=time.sleep):
    """GET with bounded retry + backoff. -> (ok, parsed_json_or_None, error)."""
    last = None
    for i in range(attempts):
        try:
            with (_open or urllib.request.urlopen)(url, timeout=timeout) as r:
                body = r.read()
                try:
                    return True, json.loads(body), None
                except Exception:  # noqa: BLE001
                    return True, None, None
        except Exception as e:  # noqa: BLE001
            last = e
            if i < attempts - 1:
                _sleep(0.5 * (2 ** i))
    return False, None, str(last)


# ── the two states ──────────────────────────────────────────────────────────

def is_late_night(now: datetime, start: int = 23, end: int = 7) -> bool:
    h = now.astimezone(TZ).hour
    return h >= start or h < end


def jordan_state(oc, now: datetime | None = None, s: dict | None = None) -> dict:
    """{'depleted': bool, 'reasons': [...]} — tentative, never a diagnosis."""
    now = now or datetime.now(timezone.utc)
    s = s or settings(oc)
    reasons = []
    try:
        from nova_relationship import quiet_mode
        q = quiet_mode(oc)
        if q.get("active"):
            reasons.append(f"hard-stretch quiet mode (score {q.get('score')})")
    except Exception:  # noqa: BLE001
        _rollback(oc)
    if is_late_night(now, s["night_start"], s["night_end"]):
        reasons.append(f"late night ({now.astimezone(TZ):%H:%M})")
    try:
        suspect = set()
        try:
            from nova_cardinal import load_ledger
            suspect = {k for k, v in load_ledger(oc).items() if v.get("compromise_suspect")}
        except Exception:  # noqa: BLE001
            pass
        h = now.astimezone(TZ).hour
        if h >= 21 or h < 10:      # sleep evidence only counts in plausible sleep hours
            oc.execute("SELECT method, max(ts) FROM telemetry.presence WHERE room='master_bedroom' AND ts > %s "
                       "AND ((person='jordan' AND method IN ('ble_rssi','wifi_rssi')) OR "
                       "(method='mmwave' AND metadata->>'occupied'='true')) GROUP BY 1", (now - timedelta(minutes=30),))
            seen = {m: ts for m, ts in oc.fetchall()
                    if ("presence:mmwave:master_bedroom" if m == "mmwave" else f"presence:{m}") not in suspect}
            phone = [m for m in seen if m != "mmwave"]
            if phone:     # identity needed: mmWave alone can't say WHO is in the bedroom
                ts = max(seen[m] for m in phone)
                reasons.append(f"sleep evidence: his phone in the bedroom at {ts.astimezone(TZ):%H:%M}"
                               + (" (bedroom mmWave occupied)" if "mmwave" in seen else ""))
    except Exception:  # noqa: BLE001
        _rollback(oc)
    return {"depleted": bool(reasons), "reasons": reasons}


def nova_state(oc, s: dict | None = None, _http=None) -> dict:
    """{'degraded': bool, 'penalty': float, 'reasons': [...]}"""
    s = s or settings(oc)
    probe = _http or http_ok
    reasons = []
    ok, h, err = probe(s["gateway_health"])
    if not ok:
        reasons.append(f"gateway health unreachable ({err})")
    elif h:
        if h.get("degraded"):
            reasons.append("gateway reports degraded")
        active = ((h.get("backends") or {}).get("active") or "")
        if active and active != "ollama":
            reasons.append(f"model fallback: answering on {active}")
    try:
        oc.execute("SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY total_ms), count(*), "
                   "count(*) FILTER (WHERE backend_used NOT LIKE '%%ollama%%') FROM gateway_traces "
                   "WHERE created_at > now() - interval '60 minutes' AND total_ms IS NOT NULL")
        med, n, fb = oc.fetchone()
        if n and n >= 3 and med and med > s["latency_ms"]:
            reasons.append(f"gateway latency high (median {med / 1000:.0f}s over {n} replies)")
        if n and n >= 3 and fb and fb / n > 0.5:
            reasons.append(f"model fallback in {fb}/{n} recent replies")
    except Exception:  # noqa: BLE001
        _rollback(oc)
    ok, _j, err = probe(s["memory_health"])
    if not ok:
        reasons.append(f"memory server unreachable ({err})")
    try:
        oc.execute("SELECT pressure, threshold FROM boiler_state WHERE ts > now() - interval '3 hours' "
                   "ORDER BY ts DESC LIMIT 1")
        r = oc.fetchone()
        if r and r[0] is not None and r[1] and r[0] >= r[1]:
            reasons.append(f"Boiler at {r[0]:.0f}/{r[1]:.0f}")
    except Exception:  # noqa: BLE001
        _rollback(oc)
    pen = min(0.5, s["degraded_penalty"] * len(reasons)) if reasons else 0.0
    return {"degraded": bool(reasons), "penalty": round(pen, 2), "reasons": reasons}


def adjusted_confidence(conf: float | None, nova: dict) -> float | None:
    """Nova degraded => her confidence drops by the penalty (never below 0.05)."""
    if conf is None:
        return None
    return round(max(0.05, float(conf) * (1 - nova.get("penalty", 0.0))), 3)


# ── keys and the decision (pure) ────────────────────────────────────────────

def decide(action_class: str, item: dict | None, *, life_safety: bool, urgent: bool,
           jordan: dict, nova: dict, jordan_key: bool, preconsent: bool, molink: str | None) -> dict:
    """Pure two-man decision. -> {allowed, deferred, keys, missing, reason, rung, spinnaker}"""
    action_class = action_class if action_class in CLASSES else "irreversible"
    need = CLASSES[action_class]
    rung = SPINNAKER_RUNG[action_class]
    a = SP.assess(item or {"sources": []})
    keys, missing = [], []
    if not nova.get("degraded") or life_safety:
        keys.append("reasoning")
    else:
        missing.append("reasoning (Nova degraded: " + "; ".join(nova.get("reasons", [])) + ")")
    sensor_key = a["independent"] >= 2 and len(a["independent_types"]) >= 2 and a["verdict"] == "CORROBORATED"
    if sensor_key:
        keys.append("sensors")
    if jordan_key:
        keys.append("jordan")
    if life_safety and preconsent:
        keys.append("preconsent")
    if need >= 2 and not ({"sensors", "jordan", "preconsent"} & set(keys)):
        missing.append("an independent second key (2 sensor types, Jordan's confirmation, or life-safety pre-consent)")
    res = {"keys": keys, "missing": missing, "rung": rung, "spinnaker": a, "deferred": False}
    if need == 0:
        return dict(res, allowed=True, reason="note: no keys needed")
    if not life_safety and not jordan_key:
        ok, _ = SP.may_trigger(item or {"sources": []}, rung)
        if not ok and item:
            return dict(res, allowed=False, reason=f"SPINNAKER {a['verdict']}: cannot trigger '{rung}' "
                                                   f"(max {a['max_rung']})")
    if nova.get("degraded") and not life_safety and need >= 2:
        return dict(res, allowed=False, deferred=True,
                    reason="Nova degraded — non-life-safety escalation held for the next turnover")
    if jordan.get("depleted") and not urgent and not life_safety:
        return dict(res, allowed=False, deferred=True,
                    reason="Jordan depleted (" + "; ".join(jordan.get("reasons", [])) + ") — deferred, not dropped")
    if action_class == "outward" and molink != "unanswered" and not jordan_key and not (life_safety and urgent):
        return dict(res, allowed=False, reason="MOLINK first: ask Jordan directly before reaching anyone else")
    if len(keys) >= need and (need < 2 or {"sensors", "jordan", "preconsent"} & set(keys)):
        return dict(res, allowed=True, reason=f"{len(keys)} key(s): {', '.join(keys)}")
    return dict(res, allowed=False, reason="two-man rule: " + "; ".join(missing or ["not enough keys"]))


def preconsent_active(oc, key: str = "shine.preconsent") -> bool:
    """Life-safety pre-consent: Commander's Intent grant if that organ exists, else the_shine/enabled."""
    try:
        import nova_commanders_intent as CI
        st = CI.grant_status(oc, key)
        if st.get("exists"):
            return bool(st.get("active"))
    except Exception:  # noqa: BLE001
        _rollback(oc)
    try:
        oc.execute("SELECT value FROM service_config WHERE service='the_shine' AND key='enabled'")
        r = oc.fetchone()
        v = r[0] if r else False
        return v is True or str(v).lower() == "true"
    except Exception:  # noqa: BLE001
        _rollback(oc)
        return False


def jordan_confirmation(oc, confirmation_id) -> bool:
    if confirmation_id in (None, "", -1):
        return False
    try:
        from nova_safety_guards import consume_confirmation
        ok, _why = consume_confirmation(oc, confirmation_id)
        return bool(ok)
    except Exception:  # noqa: BLE001
        _rollback(oc)
        return False


def authorize(oc, *, source: str, kind: str, action_class: str, item: dict | None = None,
              life_safety: bool = False, urgent: bool = False, confirmation_id=None,
              jordan_confirmed: bool = False, molink: str | None = None,
              preconsent_key: str = "shine.preconsent", text: str = "", dry: bool = False,
              _jordan: dict | None = None, _nova: dict | None = None) -> dict:
    """The gate. Logs every decision. Never raises."""
    try:
        s = settings(oc)
        jst = _jordan if _jordan is not None else jordan_state(oc, s=s)
        nst = _nova if _nova is not None else nova_state(oc, s=s)
        jk = bool(jordan_confirmed) or jordan_confirmation(oc, confirmation_id) or molink == "answered_confirm"
        pc = preconsent_active(oc, preconsent_key) if life_safety else False
        d = decide(action_class, item, life_safety=life_safety, urgent=urgent, jordan=jst, nova=nst,
                   jordan_key=jk, preconsent=pc, molink=molink)
        d["jordan_state"], d["nova_state"] = jst, nst
    except Exception as e:  # noqa: BLE001
        _rollback(oc)
        d = {"allowed": bool(life_safety), "deferred": False, "keys": [], "missing": [],
             "rung": SPINNAKER_RUNG.get(action_class), "reason": f"escalation gate error ({e}) — "
             + ("failing OPEN for life-safety" if life_safety else "held"), "spinnaker": None}
    if not dry:
        _record(oc, source, kind, action_class, d, item, life_safety, text)
        _reason_from_intent(oc, source, kind, d, life_safety)
    return d


def intent_reading(d: dict, life_safety: bool) -> tuple | None:
    """When fatigue changes what Nova does, which standing order is she reading, and how. Pure.
    -> (intent_key, reading, decision) or None."""
    jst = d.get("jordan_state") or {}
    if not jst.get("depleted"):
        return None
    if life_safety and d.get("allowed"):
        return ("quiet.shine_waking", "Quiet hours protect his rest from the ordinary; a life-safety trigger "
                "is exactly what his pre-consent covers, so the purpose says go.", "proceed")
    if d.get("deferred"):
        return ("quiet.notify_window", "The purpose is to protect his attention; a non-urgent escalation "
                "can wait for the next turnover without losing anything.", "defer")
    return None


def _reason_from_intent(oc, source, kind, d, life_safety) -> None:
    """Commander's Intent: log that the circumstance (Jordan depleted) was reasoned from purpose."""
    r = intent_reading(d, life_safety)
    if not r:
        return
    try:
        import nova_commanders_intent as CI
        CI.reason_from_intent(oc, r[0], f"{source}:{kind} while " + "; ".join((d.get('jordan_state') or {})
                              .get("reasons", [])), reading=r[1], decision=r[2], by="nova_escalation")
    except Exception:  # noqa: BLE001
        _rollback(oc)


def _record(oc, source, kind, action_class, d, item, life_safety, text) -> None:
    try:
        ensure_schema(oc)
        oc.execute("INSERT INTO escalation_log (source, kind, action_class, rung, allowed, deferred, keys, missing, "
                   "reason, item, life_safety, jordan_state, nova_state) VALUES "
                   "(%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s::jsonb,%s,%s::jsonb,%s::jsonb) RETURNING id",
                   (source, kind, action_class, d.get("rung"), bool(d.get("allowed")), bool(d.get("deferred")),
                    json.dumps(d.get("keys") or []), json.dumps(d.get("missing") or []), d.get("reason"),
                    json.dumps(item or {}, default=str), bool(life_safety),
                    json.dumps(d.get("jordan_state") or {}), json.dumps(d.get("nova_state") or {})))
        d["log_id"] = oc.fetchone()[0]
    except Exception as e:  # noqa: BLE001
        _rollback(oc)
        log(f"escalation_log write failed: {e}")
    if not d.get("allowed"):
        try:
            from nova_restraint import record_restraint
            record_restraint(context=f"two-man {source}:{kind}",
                             would_have_said=(text or (item or {}).get("claim") or f"[{kind}]")[:4000],
                             reason=("DEFERRED: " if d.get("deferred") else "HELD: ") + str(d.get("reason")),
                             channel=CHANNEL, detail={"two_man": {k: d.get(k) for k in ("keys", "missing", "rung")},
                                                      "escalation_log_id": d.get("log_id")},
                             conn=oc.connection)
        except Exception:  # noqa: BLE001
            _rollback(oc)


def feedback(oc, log_id: int, note: str, by: str) -> bool:
    """Jordan's verdict on an escalation ('unneeded: ...' / 'right call'). Only jordan* may mark it;
    'unneeded' rows become overreach hotwashes (nova_hotwash --sweep)."""
    if not str(by).lower().startswith("jordan"):
        raise PermissionError("only Jordan marks an escalation")
    ensure_schema(oc)
    oc.execute("UPDATE escalation_log SET feedback=%s, feedback_by=%s WHERE id=%s", (note[:500], by, int(log_id)))
    return oc.rowcount == 1


def deferred_since(oc, since: datetime) -> list:
    """Held/deferred escalations since `since` — the Watch Bill turnover and the PDB read these."""
    try:
        oc.execute("SELECT id, ts, source, kind, action_class, reason, item->>'claim' FROM escalation_log "
                   "WHERE NOT allowed AND ts > %s ORDER BY ts", (since,))
        return [dict(zip(("id", "ts", "source", "kind", "action_class", "reason", "claim"), r)) for r in oc.fetchall()]
    except Exception:  # noqa: BLE001
        _rollback(oc)
        return []


# ── MOLINK ──────────────────────────────────────────────────────────────────

def molink_ask(oc, ref: str, text: str, dry: bool = False) -> dict:
    """The cheapest direct check: one Slack line to Jordan in #nova-chat. Idempotent per ref."""
    msg = f":telephone_receiver: {text} — reply *fine* in this thread (any reply, or any message to me, clears it)."
    try:
        from nova_annie_rule import check
        chk = check(msg, oc)
        if not chk.get("ok", True):
            return {"posted": False, "reason": f"Annie rule: {chk}"}
    except Exception:  # noqa: BLE001
        pass
    try:
        oc.execute("SELECT ts FROM slack_prompts WHERE kind='molink' AND ref_id=%s", (ref,))
        if oc.fetchone():
            return {"posted": False, "reason": "already asked"}
    except Exception:  # noqa: BLE001
        _rollback(oc)
    if dry:
        return {"posted": False, "reason": "dry-run", "text": msg}
    import nova_config
    import nova_slack_answers as SA
    try:
        r = SA.slack("chat.postMessage", channel=nova_config.SLACK_CHAN, text=msg)
    except Exception as e:  # noqa: BLE001
        log(f"MOLINK post failed for {ref}: {e}")
        return {"posted": False, "reason": f"slack failed: {e}"}
    if not r.get("ok"):
        return {"posted": False, "reason": r.get("error")}
    try:
        oc.execute("INSERT INTO slack_prompts (kind, ref_id, channel, ts) VALUES ('molink', %s, %s, %s) "
                   "ON CONFLICT (kind, ref_id) DO NOTHING", (ref, nova_config.SLACK_CHAN, r.get("ts")))
    except Exception:  # noqa: BLE001
        _rollback(oc)
    try:
        from nova_action_audit import record_outbound
        record_outbound("slack", nova_config.SLACK_CHAN, msg, source="nova_escalation.molink")
    except Exception:  # noqa: BLE001
        pass
    return {"posted": True, "ts": r.get("ts")}


def molink_status(oc, ref: str, _read=None) -> str:
    """'answered' | 'unanswered' | 'none'. Answered = a human reply in the thread, or any message
    from Jordan to Nova since the ask."""
    try:
        oc.execute("SELECT channel, ts, posted_at, resolved_at FROM slack_prompts WHERE kind='molink' AND ref_id=%s",
                   (ref,))
        r = oc.fetchone()
    except Exception:  # noqa: BLE001
        _rollback(oc)
        return "none"
    if not r:
        return "none"
    ch, ts, posted, resolved = r
    if resolved:
        return "answered"
    answered = False
    try:
        oc.execute("SELECT 1 FROM gateway_traces WHERE person='jordan' AND created_at > %s LIMIT 1", (posted,))
        answered = bool(oc.fetchone())
    except Exception:  # noqa: BLE001
        _rollback(oc)
    if not answered:
        try:
            if _read is None:
                from nova_slack_answers import read_answer as _read
            txt, _v = _read(ch, ts)
            answered = bool(txt)
        except Exception:  # noqa: BLE001
            pass
    if answered:
        try:
            oc.execute("UPDATE slack_prompts SET resolved_at=now(), result='answered' WHERE kind='molink' AND ref_id=%s",
                       (ref,))
        except Exception:  # noqa: BLE001
            _rollback(oc)
        return "answered"
    return "unanswered"


# ── CLI ─────────────────────────────────────────────────────────────────────

def selftest() -> int:
    calm = {"depleted": False, "reasons": []}
    tired = {"depleted": True, "reasons": ["late night (01:10)"]}
    ok_nova = {"degraded": False, "penalty": 0.0, "reasons": []}
    bad_nova = {"degraded": True, "penalty": 0.25, "reasons": ["memory server unreachable"]}
    two = {"claim": "x", "sources": [{"id": "camera:alley_north"}, {"id": "scanner:Burbank PD"}]}
    one = {"claim": "x", "sources": [{"id": "camera:alley_north"}, {"id": "camera:front_door"}]}
    kw = dict(life_safety=False, urgent=False, jordan_key=False, preconsent=False, molink=None)
    assert decide("alert", two, jordan=calm, nova=ok_nova, **kw)["allowed"]
    assert not decide("alert", one, jordan=calm, nova=ok_nova, **kw)["allowed"]
    assert decide("alert", one, jordan=calm, nova=ok_nova, **dict(kw, jordan_key=True))["allowed"]
    assert decide("alert", two, jordan=tired, nova=ok_nova, **kw)["deferred"]
    assert decide("alert", two, jordan=tired, nova=ok_nova, **dict(kw, urgent=True))["allowed"]
    assert not decide("alert", two, jordan=calm, nova=bad_nova, **kw)["allowed"]
    ls = dict(kw, life_safety=True, preconsent=True)
    assert decide("outward", one, jordan=tired, nova=bad_nova, **dict(ls, molink="unanswered"))["allowed"]
    assert not decide("outward", one, jordan=calm, nova=ok_nova, **dict(ls, preconsent=False, molink="unanswered"))["allowed"]
    assert not decide("outward", two, jordan=calm, nova=ok_nova, **kw)["allowed"]       # MOLINK first
    assert decide("ask", one, jordan=calm, nova=ok_nova, **kw)["allowed"]
    assert not decide("ask", {"sources": [{"id": "nova:reasoning"}]}, jordan=calm, nova=ok_nova, **kw)["allowed"]
    assert adjusted_confidence(0.8, bad_nova) == 0.6
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--log", action="store_true")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--feedback", type=int, metavar="LOG_ID", help="mark an escalation (with --note, --by jordan)")
    ap.add_argument("--note", default="")
    ap.add_argument("--by", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    import nova_watch_common as W
    conn = W.connect()
    oc = conn.cursor()
    if a.feedback:
        print("ok" if feedback(oc, a.feedback, a.note or "unneeded", a.by) else "no such escalation")
        return 0
    if a.log:
        ensure_schema(oc)
        oc.execute("SELECT ts, source, kind, action_class, allowed, deferred, keys, reason FROM escalation_log "
                   "WHERE ts > now() - make_interval(hours => %s) ORDER BY ts", (a.hours,))
        for r in oc.fetchall():
            print(f"{r[0].astimezone(TZ):%m-%d %H:%M} {r[1]}:{r[2]} {r[3]} allowed={r[4]} deferred={r[5]} keys={r[6]} — {r[7]}")
        return 0
    print(json.dumps({"jordan": jordan_state(oc), "nova": nova_state(oc)}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
