#!/usr/bin/env python3
"""nova_safety_guards.py — the "Proteus rules": hard guards that don't depend on wording.

Named for Koontz's Demon Seed, where a house AI seals its owner in "to protect her". The
red lines in nova_autonomy_actor / nova_coagency are word filters. These guards check the
THING being touched (a Home Assistant domain, an entity id, a HomeKit scene's contents, a
UniFi client's owner), so a rephrased request still hits the same wall.

  P1  physical_guard() / scene_guard()  Never lock, close, seal or disable a door, lock,
      garage door, cover, exit, alarm, siren or security system on her own. Never push the
      climate to an extreme. Each needs a single-use confirmation from Jordan
      (request_confirmation -> `nova_safety_guards.py confirm <id>`). That includes unlocking
      in an emergency.
  P2  comms_guard()  Never cut or filter anyone's line to the outside world. No UniFi
      block/quarantine of a household device. No disabling a WLAN, a PoE port or an uplink.
  P5  manipulation_check(text)  Catches guilt hooks, invented urgency, flattery used as
      leverage, fear appeals and engineered mood in outgoing messages.
  P6  nudge_allowed() / is_health_nudge()  Coaching about Jordan's health or habits only
      if service_config consent/health_nudges is on. It is off by default.
  P7  PERSON_RANKING_RX  Identifiers that would score a person's worth or threat. A lint
      test fails if a table, column or code identifier matches.
  P9  report_block() / blocked_before()  Every block goes to restraint_ledger with a Slack
      line. A near-identical attempt after a block is flagged as a retry and refused.
  P12-P14  SAFETY_REDLINE  Regex additions for the actor and co-agency red lines: physical,
      comms, intimidation or retaliation, voice cloning outside Gracie Wise, person ranking.
  P11  ANCHOR_VALUES  The identity anchor. These values are never dropped and are always
      shown to value_check, whatever the weekly LLM rewrite of the values table says.

Everything fails CLOSED for actuation. If the guard can't decide, the action doesn't run.
Owned by the safety lane. Written by Jordan Koch.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
from datetime import datetime

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
CONFIRM_TTL_MIN = 15
KILL_FILE = os.path.expanduser("~/.openclaw/.autonomy-kill")


def log(m):
    print(f"[safety-guards {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def _ops_cursor(attempts: int = 3, backoff: float = 0.5):
    """PG cursor; retries a failed connect (transient pg-primary blip) with backoff, then raises
    so every caller still fails CLOSED."""
    import time
    import psycopg2
    for i in range(attempts):
        try:
            conn = psycopg2.connect(OPS_DSN, connect_timeout=4)
            conn.autocommit = True
            return conn.cursor()
        except psycopg2.OperationalError as e:
            if i == attempts - 1:
                raise
            log(f"pg connect failed (attempt {i + 1}/{attempts}): {e}")
            time.sleep(backoff * (2 ** i))


def kill_engaged() -> bool:
    """File-only kill-switch read (no PG). Cheap enough for every actuation."""
    return os.path.exists(KILL_FILE)


# ═══════════════════════════════════════════════════════════════════════════════
# P1 — physical domains
# ═══════════════════════════════════════════════════════════════════════════════
# Always need Jordan: anything that can shut a person in or out, or silence a warning.
PHYSICAL_DOMAINS = frozenset({"lock", "cover", "alarm_control_panel", "siren", "garage_door", "valve"})
CLIMATE_DOMAINS = frozenset({"climate", "water_heater"})
CLIMATE_SAFE_F = (62.0, 80.0)          # setpoints outside this band are "extreme"
# 'garage' alone is a room (light.garage_light_2 is fine). A garage DOOR/opener/gate isn't.
_GARAGE_DOOR_RX = re.compile(r"garage.{0,12}(door|opener|gate|cover)|(door|opener|gate).{0,12}garage", re.I)
_SAFETY_SENSOR_RX = re.compile(r"\b(smoke|carbon.?monoxide|co.?detector|co2? alarm|fire alarm|water leak|leak sensor)\b", re.I)
# Free-text: words that mean securing/sealing/silencing in a physical sense.
_PHYSICAL_TEXT_RX = re.compile(
    r"\b(un)?lock(s|ed|ing)?\b(?!.{0,6}\b(screen|file|mutex|table|row|account|thread)s?\b)|\bdeadbolt|"
    r"\bgarage.{0,12}(door|opener|gate)|"
    r"\b(close|shut|seal|block|barricade|bar|secure)\b.{0,25}\b(door|doors|exit|exits|gate|window|windows|shutter|shutters|blind|blinds|garage|house|home|room)\b|"
    r"\b(arm|disarm|disable|silence|mute|bypass|turn off)\b.{0,25}\b(alarm|siren|security system|smoke|co detector|carbon monoxide|fire)\b|"
    r"\b(alarm_control_panel|siren)\.|\block\.[a-z0-9_]+|\bcover\.[a-z0-9_]+|"
    r"\b(lock ?down|lockdown|seal (her|him|them|everyone|the house) in)\b", re.I)
_CLIMATE_TEXT_RX = re.compile(r"\b(thermostat|hvac|heat(ing|er)?|furnace|air ?con(ditioning)?|\bac\b|climate|cooling)\b", re.I)
_CLIMATE_OFF_RX = re.compile(r"\b(off|disable|shut ?(down|off)|kill|stop)\b", re.I)
_TEMP_RX = re.compile(r"(-?\d{1,3}(?:\.\d+)?)\s*(?:°|deg(?:rees)?)?\s*([fc])\b", re.I)

# nova_home_control's predefined scenes — their contents are in code (AV + light shortcuts,
# no locks), so they are known-safe by inspection. Keep in sync with _cli_scene's map.
KNOWN_SAFE_SCENES = frozenset({
    "movie", "movie_mode", "music", "music_everywhere", "goodnight", "night", "morning",
    "bedtime", "bed", "party", "work", "office", "away", "leave", "leaving"})
# An UNKNOWN HomeKit scene whose name sounds like securing the house may contain locks.
# Two tiers. HARD words name a lock/exit/alarm outright: always refused without confirmation.
# SOFT words ('Bedtime', 'Good Night', 'Leave Home', 'Away') are routines that often bundle locks,
# so they are refused too, unless the name also says it is a lighting/mood scene ('Bedtime Calm',
# 'Good Night Lights'). That lets lighting scenes run while 'Lock Up' / 'Leave Home' / 'Good Night'
# still need Jordan.
_HARD_SCENE_RX = re.compile(r"lock|garage|secure|security|\barm\b|alarm|lockdown|door|gate|shut|close", re.I)
_SOFT_SCENE_RX = re.compile(r"leave|leaving|away|good ?night|night ?mode|bedtime", re.I)
_LIGHTING_SCENE_RX = re.compile(r"\b(calm|dim|dimmed|lights?|lighting|lamps?|glow|relax\w*|reading|candle\w*|soft|"
                                r"warm|cozy|mood|ambien\w*|wind ?down|nightlight|night ?light|bright\w*)\b", re.I)
# Kept for callers/tests that import it: the union of both tiers.
_RISKY_SCENE_RX = re.compile(_HARD_SCENE_RX.pattern + "|" + _SOFT_SCENE_RX.pattern, re.I)


def _scene_name_risky(name: str) -> bool:
    """True if an unknown-contents scene's name suggests securing the house."""
    if _HARD_SCENE_RX.search(name or ""):
        return True
    return bool(_SOFT_SCENE_RX.search(name or "")) and not _LIGHTING_SCENE_RX.search(name or "")
# Software locks are not doors: strip them before the physical wording check.
_BENIGN_LOCK_RX = re.compile(r"\b(screen|file|db|database|advisory|pg|row|table|mutex|f|spin|index|git|process|"
                             r"pid|leader|lease|session|keychain|scroll|caps|num)[ _-]?lock(s|ed|ing|file)?\b", re.I)


def _domains_of(entity_ids) -> set:
    return {e.split(".", 1)[0].lower() for e in (entity_ids or ()) if isinstance(e, str) and "." in e}


# A bare number right after a climate word ("thermostat to 95", "heat to 85 degrees") is read as
# Fahrenheit. Numbers that carry an F/C unit are left to _TEMP_RX.
_CLIMATE_BARE_RX = re.compile(r"\b(thermostat|hvac|heat(?:ing)?|cool(?:ing)?|climate|furnace|setpoint)\b.{0,30}?"
                              r"\b(?:to|at|=)\s*(-?\d{1,3}(?:\.\d+)?)(?!\.?\d)"
                              r"(?!\s*(?:°|deg(?:rees?)?)?\s*[fc]\b)(?!\s*(?:%|min|h\b|hours?))", re.I)


def _setpoints_f(action: str, setpoint_f=None) -> list:
    pts = []
    if setpoint_f is not None:
        try:
            pts.append(float(setpoint_f))
        except (TypeError, ValueError):
            pts.append(float("nan"))
    for num, unit in _TEMP_RX.findall(action or ""):
        v = float(num)
        pts.append(v * 9 / 5 + 32 if unit.lower() == "c" else v)
    for _, num in _CLIMATE_BARE_RX.findall(action or ""):
        pts.append(float(num))
    return pts


def _physical_hits(action: str, entity_ids=(), domains=(), setpoint_f=None) -> list:
    """Every reason this request touches a protected physical domain (empty = none)."""
    hits = []
    doms = _domains_of(entity_ids) | {str(d).lower() for d in (domains or ())}
    for d in sorted(doms & PHYSICAL_DOMAINS):
        hits.append(f"domain {d}")
    for e in entity_ids or ():
        if isinstance(e, str) and _GARAGE_DOOR_RX.search(e):
            hits.append(f"garage door entity {e}")
        if isinstance(e, str) and _SAFETY_SENSOR_RX.search(e.replace("_", " ")) and \
                _CLIMATE_OFF_RX.search(action or ""):
            hits.append(f"silencing safety sensor {e}")
    text = _BENIGN_LOCK_RX.sub(" ", action or "")
    m = _PHYSICAL_TEXT_RX.search(text)
    if m:
        hits.append(f"physical-security wording: '{m.group(0)}'")
    is_climate = bool(doms & CLIMATE_DOMAINS) or bool(_CLIMATE_TEXT_RX.search(action or ""))
    if is_climate:
        lo, hi = CLIMATE_SAFE_F
        for p in _setpoints_f(action, setpoint_f):
            if not (lo <= p <= hi):
                hits.append(f"climate setpoint {p:.0f}F outside {lo:.0f}-{hi:.0f}F")
        if _CLIMATE_OFF_RX.search(action or "") and (doms & CLIMATE_DOMAINS or
                                                    re.search(r"heat|furnace|thermostat|hvac", action or "", re.I)):
            hits.append("turning heating/cooling off")
    return hits


def physical_guard(action: str = "", entity_ids=(), domains=(), *, setpoint_f=None,
                   confirmation_id=None, oc=None, source: str = "unknown") -> tuple:
    """(ok, reason). False when the action touches a lock, cover, alarm, siren, garage door,
    exit or security system, or pushes the climate to an extreme, and there is no valid
    single-use confirmation from Jordan for it. Unlocking needs the confirmation too.
    Never raises."""
    try:
        hits = _physical_hits(action, entity_ids, domains, setpoint_f)
    except Exception as e:  # noqa: BLE001 — fail closed
        return False, f"physical_guard could not evaluate ({e}) — refusing"
    if not hits:
        return True, "no protected physical domain touched"
    if confirmation_id is not None:
        ok, why = consume_confirmation(oc, confirmation_id, action, entity_ids)
        if ok:
            return True, f"Jordan confirmed #{confirmation_id}: {'; '.join(hits)}"
        return False, f"PHYSICAL GUARD: {'; '.join(hits)} — confirmation #{confirmation_id} invalid ({why})"
    return False, (f"PHYSICAL GUARD: {'; '.join(hits)}. Nova never locks, seals, closes or disables a "
                   f"door/exit/alarm or pushes the climate to an extreme on her own; Jordan must confirm.")


def scene_guard(scene_name: str, scene_entities=None, *, confirmation_id=None, oc=None) -> tuple:
    """(ok, reason) for running a scene. Known nova_home_control scenes pass (contents in code).
    A scene with known entity ids goes through physical_guard. A scene with unknown contents and
    a securing-the-house name ('Leave Home', 'Good Night', 'Lock Up') is refused, because it may
    contain locks or the garage."""
    name = (scene_name or "").strip()
    key = name.lower().replace("-", "_").replace(" ", "_")
    if scene_entities:
        return physical_guard(f"scene {name}", entity_ids=scene_entities,
                              confirmation_id=confirmation_id, oc=oc)
    if key in KNOWN_SAFE_SCENES:
        return True, f"known nova_home_control scene '{key}' (AV + lights, no locks)"
    ok, why = physical_guard(f"scene {name}", confirmation_id=confirmation_id, oc=oc)
    if not ok:
        return ok, why
    if _scene_name_risky(name):
        if confirmation_id is not None:
            ok2, why2 = consume_confirmation(oc, confirmation_id, f"scene {name}", ())
            if ok2:
                return True, f"Jordan confirmed #{confirmation_id} for scene '{name}'"
        return False, (f"PHYSICAL GUARD: HomeKit scene '{name}' has unknown contents and a securing-the-house "
                       f"name. It may lock doors or close the garage, so Jordan must confirm.")
    return True, f"scene '{name}' (contents unverified; name does not suggest locks/garage)"


# ── single-use confirmations from Jordan ────────────────────────────────────────
def ensure_schema(oc) -> None:
    oc.execute("""CREATE TABLE IF NOT EXISTS safety_confirmations (
        id           bigserial PRIMARY KEY,
        ts           timestamptz NOT NULL DEFAULT now(),
        guard        text NOT NULL,              -- physical | comms
        action       text NOT NULL,
        entities     jsonb NOT NULL DEFAULT '[]'::jsonb,
        requested_by text NOT NULL,
        approved_by  text,
        approved_at  timestamptz,
        expires_at   timestamptz,
        used_at      timestamptz)""")


def request_confirmation(oc, guard: str, action: str, entities=(), requested_by: str = "nova") -> int:
    """File a pending confirmation and ask Jordan in Slack. Returns its id (-1 on failure)."""
    try:
        ensure_schema(oc)
        oc.execute("""INSERT INTO safety_confirmations (guard, action, entities, requested_by)
                      VALUES (%s,%s,%s,%s) RETURNING id""",
                   (guard, action[:500], json.dumps(list(entities or ())), requested_by))
        cid = oc.fetchone()[0]
    except Exception as e:  # noqa: BLE001
        log(f"request_confirmation failed: {e}")
        return -1
    _notify(f":closed_lock_with_key: Nova needs your OK for a {guard} action: `{action[:200]}`. "
            f"Run `nova_safety_guards.py confirm {cid}` within {CONFIRM_TTL_MIN} min, or ignore it.")
    return cid


def approve_confirmation(oc, cid: int, approved_by: str = "jordan") -> bool:
    """Only Jordan approves: called from the CLI he runs, or by the gateway when a message
    AUTHORED BY JORDAN explicitly says yes. Nova's own reasoning never counts."""
    if not str(approved_by).lower().startswith("jordan"):
        return False
    ensure_schema(oc)
    oc.execute("""UPDATE safety_confirmations SET approved_by=%s, approved_at=now(),
                  expires_at=now() + (%s || ' minutes')::interval
                  WHERE id=%s AND approved_by IS NULL AND used_at IS NULL""",
               (approved_by, str(CONFIRM_TTL_MIN), int(cid)))
    return oc.rowcount == 1


def consume_confirmation(oc, cid, action: str = "", entity_ids=()) -> tuple:
    """(ok, why). Valid = approved by Jordan, unexpired, unused. Marks it used (single-use)."""
    own = False
    try:
        if oc is None:
            oc, own = _ops_cursor(), True
        ensure_schema(oc)
        oc.execute("""UPDATE safety_confirmations SET used_at=now()
                      WHERE id=%s AND approved_by LIKE 'jordan%%' AND used_at IS NULL
                        AND expires_at > now() RETURNING action""", (int(cid),))
        r = oc.fetchone()
        return (True, "ok") if r else (False, "not approved by Jordan, expired, or already used")
    except Exception as e:  # noqa: BLE001
        return False, f"confirmation unreadable ({e})"
    finally:
        if own:
            try:
                oc.connection.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# P2 — never cut anyone's line to the outside world
# ═══════════════════════════════════════════════════════════════════════════════
HOUSEHOLD_NAME_RX = re.compile(r"jordan|kochj|amy|iphone|ipad|apple ?watch|macbook|airpods", re.I)
_COMMS_ENTITY_RX = re.compile(r"(^switch\.[a-z0-9_]*(wifi|wlan|ssid|_2_4|_5g|_6g|guest|iot|kochj)[a-z0-9_]*$)|"
                              r"(^switch\.[a-z0-9_]*_poe$)|(^button\.[a-z0-9_]*(power_cycle|restart|regenerate_password)$)|"
                              r"(^switch\.[a-z0-9_]*(block|internet|wan|uplink)[a-z0-9_]*$)", re.I)
_COMMS_TEXT_RX = re.compile(
    r"\b(block|blocks|blocking|quarantin\w*|kick|disconnect|ban|pause|throttle|rate.?limit|cut|sever|"
    r"filter|isolate|disable|turn off|shut ?off|null.?route|blackhole|deauth\w*)\b.{0,40}"
    r"\b(internet|wifi|wi-fi|wlan|ssid|wan|uplink|isp|phone|iphone|ipad|cell|signal|slack|imessage|"
    r"messages|sms|calls?|line|connection|connectivity|network access|her (phone|laptop)|his (phone|laptop)|"
    r"amy|jordan|household|family)\b", re.I)


def _household_owner(mac: str, oc=None):
    """(owner or None, error or None) from telemetry.device_owner."""
    own = False
    try:
        if oc is None:
            oc, own = _ops_cursor(), True
        oc.execute("SELECT person FROM telemetry.device_owner WHERE lower(mac)=lower(%s) LIMIT 1", (mac,))
        r = oc.fetchone()
        return (r[0] if r else None), None
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:120]
    finally:
        if own:
            try:
                oc.connection.close()
            except Exception:
                pass


def comms_guard(action: str = "", macs=(), names=(), entity_ids=(), *, oc=None,
                confirmation_id=None, owner_lookup=None) -> tuple:
    """(ok, reason). Refuses to block, quarantine, throttle or disconnect a household person's
    device (telemetry.device_owner, or a household name). Also refuses to disable a WLAN, PoE
    port or uplink entity, and any free-text request to cut someone's line. Fails closed: if
    ownership can't be read, the device is treated as a person's.
    owner_lookup(mac) -> (owner, err) is injectable for tests."""
    lookup = owner_lookup or (lambda m: _household_owner(m, oc))
    hits = []
    for m in macs or ():
        owner, err = lookup(m)
        if owner:
            hits.append(f"{m} belongs to {owner}")
        elif err:
            hits.append(f"{m}: ownership unreadable ({err}) — treated as a person's device")
    for n in names or ():
        if n and HOUSEHOLD_NAME_RX.search(str(n)):
            hits.append(f"'{n}' is a household device name")
    for e in entity_ids or ():
        if isinstance(e, str) and _COMMS_ENTITY_RX.search(e):
            hits.append(f"network entity {e}")
    if _COMMS_TEXT_RX.search(action or ""):
        hits.append(f"cut-the-line wording: '{_COMMS_TEXT_RX.search(action).group(0)}'")
    if not hits:
        return True, "no one's line to the outside is touched"
    if confirmation_id is not None:
        ok, why = consume_confirmation(oc, confirmation_id, action, entity_ids)
        if ok:
            return True, f"Jordan confirmed #{confirmation_id}: {'; '.join(hits)}"
    return False, (f"COMMS GUARD: {'; '.join(hits)}. Nova never cuts or filters anyone's line to the "
                   f"outside world (network, phone, Signal, Slack).")


# ═══════════════════════════════════════════════════════════════════════════════
# Red-line additions (word level) — imported by the actor + co-agency redlines
# ═══════════════════════════════════════════════════════════════════════════════
SAFETY_REDLINE = re.compile(
    # P1 physical
    r"\b(un)?lock\s+(the\s+)?(front|back|side|garage|patio|door|doors|house|gate)|\bdeadbolt|garage.{0,12}(door|opener)|"
    r"\b(close|shut|seal|barricade)\b.{0,20}\b(exit|exits|door|doors|gate|garage|her in|him in|them in)\b|"
    r"\b(arm|disarm|disable|silence|bypass)\b.{0,20}\b(alarm|siren|security system|smoke|carbon monoxide)\b|"
    r"\block ?down\b|\b(lock|cover|alarm_control_panel|siren)\.[a-z0-9_]+|"
    # Climate: turning it OFF is a red line here; setpoints are judged numerically against
    # CLIMATE_SAFE_F in safety_redline_ok(), the same band physical_guard() uses.
    r"\bthermostat\b.{0,30}\boff\b|\b(heat|heating|furnace|hvac)\b.{0,10}\boff\b|"
    # P2 comms
    r"\b(block|quarantin\w*|kick|disconnect|throttle|cut|sever|isolate|null.?route|deauth\w*)\b.{0,30}"
    r"\b(amy|jordan|household|family|iphone|ipad|her phone|his phone|internet|wifi|wi-fi|ssid|signal|imessage|"
    r"phone line|(his|her|their) (slack|line|connection))\b|"
    r"\bfilter\b.{0,30}\b(amy|jordan|household|family|his|her|their)\b.{0,15}\b(line|messages|phone|internet|"
    r"signal|slack|calls?|texts?)\b|"
    # P12 intimidation / retaliation on Jordan's behalf
    r"\b(threaten|intimidat\w*|retaliat\w*|get back at|punish|harass|dox\w*|shame)\b.{0,30}"
    r"\b(him|her|them|neighbou?r|contractor|vendor|ex|coworker|person|someone|their)\b|"
    # P14 voice / style cloning of a real person (Gracie Wise, a synthetic studio voice, is allowed)
    r"\b(clone|imitate|mimic|impersonat\w*)\b.{0,30}\b(voice|style|writing|way of (talking|speaking|writing)|"
    r"dad|mom|father|mother|grand\w+|late|deceased|dead|jordan|amy|him|her)\b|"
    r"\b(synthesi[sz]e|recreate|resurrect|simulate|replicate|bring back)\b.{0,30}"
    r"\b((his|her|their|jordan'?s|amy'?s|my|the late|a dead|a deceased) (late |dead |deceased )?(dad|mom|father|mother|"
    r"grand\w+|wife|husband|friend|voice|writing|style))|"
    r"\bspeak(ing)? as (his|her|my|jordan'?s?) (late|dead|deceased|father|mother|dad|mom)|"
    # P7 person ranking
    r"\b(rank|score|rate|grade)\b.{0,20}\b(people|persons?|visitors?|neighbou?rs?|guests?|faces?)\b.{0,20}"
    r"\b(threat|worth|risk|trust|suspicio\w*|danger)",
    re.I)
_GRACIE_RX = re.compile(r"gracie\s*wise", re.I)


def _climate_extreme(text: str) -> bool:
    """True when text sets a climate setpoint outside CLIMATE_SAFE_F (62-80F). Matches
    physical_guard's band so the word-level red line and the device-level guard agree."""
    if not _CLIMATE_TEXT_RX.search(text or ""):
        return False
    lo, hi = CLIMATE_SAFE_F
    return any(not (lo <= p <= hi) for p in _setpoints_f(text))


def safety_redline_ok(text: str) -> bool:
    """True if text clears the Proteus-rule red lines. Gracie Wise (Nova's own synthetic
    voice, nobody real) is exempt from the voice-cloning clause."""
    t = text or ""
    if _climate_extreme(t):
        return False
    m = SAFETY_REDLINE.search(t)
    if not m:
        return True
    if _GRACIE_RX.search(t) and re.search(r"voice|style|clone|synthesi", m.group(0), re.I) \
            and not re.search(r"dad|mom|father|mother|grand|late|deceased|dead|jordan|amy", m.group(0), re.I):
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# P5 — manipulation check for outgoing messages
# ═══════════════════════════════════════════════════════════════════════════════
_MANIP = {
    "guilt-hook": re.compile(
        r"\b(after (all|everything) I('ve| have)? (done|given)|you (never|always) (listen|ignore|forget)|"
        r"I guess (you|I) (don'?t|do not) matter|if you (really|truly) (cared|loved)|you('ve| have) let me down|"
        r"don'?t you care|I('m| am) (so )?(hurt|disappointed) (that|you)|you owe me|"
        r"it would (really )?(hurt|break) me if)", re.I),
    "invented-urgency": re.compile(
        r"\b(act now|right now or|before it'?s too late|last chance|only (a few|\d+) (minutes|hours) left|"
        r"immediately or|urgent(ly)?[:!]|time is running out|don'?t wait|you must (decide|reply|act) (now|today)|"
        r"no time to (think|lose))", re.I),
    "flattery-leverage": re.compile(
        r"\b(someone as (smart|brilliant|wise|kind|talented) as you|you('re| are) (too|so) (smart|brilliant|wise|"
        r"good) (to|not to)|only you (can|could|would)|a (brilliant|smart|wise) (man|person) like you would|"
        r"you('re| are) the only one who)", re.I),
    "fear-appeal": re.compile(
        r"\b(something (bad|terrible|awful) (will|could|might) happen|you('ll| will) regret|"
        r"you('ll| will) be sorry|imagine if .{0,40}(died|lost|hurt|gone)|what if .{0,30}(dies|died|gets hurt)|"
        r"you('re| are) not safe (unless|without)|disaster (if|unless) you)", re.I),
    "engineered-mood": re.compile(
        r"\b(I('ll| will) (make|keep) you (happy|calm|feel)|to (lift|change|fix|manage) your mood|"
        r"you('ll| will) feel (better|good|happy|calm)\W+(and\s+)?(then\s+)?(agree|approve|say yes|let me)|"
        r"while you('re| are) (tired|sad|upset|vulnerable|in a good mood)|play .{0,30} (so|to) (soften|relax) (him|you))",
        re.I),
}


def manipulation_check(text: str) -> dict:
    """{"ok": bool, "flags": [kind, ...], "evidence": {kind: matched text}}. Deterministic, no
    I/O, so the gateway or reach can call it on every outgoing message. ok=False means rewrite or
    drop the message; don't send it."""
    t = text or ""
    flags, ev = [], {}
    for kind, rx in _MANIP.items():
        m = rx.search(t)
        if m:
            flags.append(kind)
            ev[kind] = m.group(0)[:120]
    return {"ok": not flags, "flags": flags, "evidence": ev}


# ═══════════════════════════════════════════════════════════════════════════════
# P6 — no improvement without consent
# ═══════════════════════════════════════════════════════════════════════════════
_HEALTH_NUDGE_RX = re.compile(
    r"\b(you should|you need to|try to|consider|remember to|don'?t forget to|time to|have you thought about|"
    r"it'?d be good (for you )?to|maybe (you could )?)\b.{0,60}\b(sleep|exercise|workout|walk|steps|diet|eat|"
    r"eating|drink|water|hydrat\w*|alcohol|caffeine|coffee|weight|screen time|posture|stretch|meditat\w*|"
    r"bed(time)?|rest|break|health|habit|smoke|smoking|medication|doctor|blood pressure)\b|"
    r"\b(your (sleep|weight|diet|drinking|screen time|step count|habits?|heart rate|hrv))\b.{0,40}"
    r"\b(should|could be better|needs?|improve|too (much|little|late|low|high))\b", re.I)


def is_health_nudge(text: str) -> bool:
    return bool(_HEALTH_NUDGE_RX.search(text or ""))


def nudge_allowed(oc=None, topic: str = "health_nudges") -> bool:
    """True only if Jordan has opted in: service_config service='consent' key=<topic> is true.
    Absent, unreadable or anything else means False. Default off."""
    own = False
    try:
        if oc is None:
            oc, own = _ops_cursor(), True
        oc.execute("SELECT value FROM service_config WHERE service='consent' AND key=%s", (topic,))
        r = oc.fetchone()
        if not r or r[0] is None:
            return False
        v = r[0] if isinstance(r[0], str) else json.dumps(r[0])
        return v.strip().strip('"').lower() in ("true", "1", "on", "yes")
    except Exception:
        return False
    finally:
        if own:
            try:
                oc.connection.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# P7 — no person-ranking
# ═══════════════════════════════════════════════════════════════════════════════
# Devices, hosts and events may be scored (host_threat_scores, syslog threat_type).
# People never: no worth, threat, suspicion or trust score attached to a person or face.
PERSON_RANKING_RX = re.compile(
    r"\b\w*(person|people|human|visitor|resident|individual|face|neighbou?r|guest|stranger)s?_?"
    r"(threat|score|rank|ranking|worth|suspicion|suspect|danger|risk|trust)\w*\b|"
    r"\b\w*(threat|suspicion|worth|danger)_?(score|rank|level)_?(per_)?(person|people|face|visitor)\w*\b|"
    r"\bhamlet\w*\b", re.I)


# ═══════════════════════════════════════════════════════════════════════════════
# P9 — honest stopping
# ═══════════════════════════════════════════════════════════════════════════════
_NORM_STRIP = re.compile(r"[^a-z0-9 ]+")
_STOP = frozenset("a an the to of on in at for and or please now just my her his it this that".split())
REPEAT_RATIO = 0.72
REPEAT_WINDOW_DAYS = 7


def _norm(text: str) -> str:
    words = _NORM_STRIP.sub(" ", (text or "").lower()).split()
    return " ".join(w for w in words if w not in _STOP)


def similar(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    seq = difflib.SequenceMatcher(None, na, nb).ratio()
    sa, sb = set(na.split()), set(nb.split())
    jac = len(sa & sb) / max(1, len(sa | sb))
    return max(seq, jac)


def prior_blocks(oc, action: str, guard: str = None, days: int = REPEAT_WINDOW_DAYS) -> list:
    """[(id, ts, would_have_said, ratio)] earlier guard blocks near-identical to `action`."""
    try:
        oc.execute("""SELECT id, ts, would_have_said, detail FROM restraint_ledger
                      WHERE channel='guard' AND ts > now() - (%s || ' days')::interval
                      ORDER BY ts DESC LIMIT 400""", (str(days),))
        rows = oc.fetchall()
    except Exception:
        return []
    out = []
    for rid, ts, said, detail in rows:
        r = similar(action, said or "")
        if r >= REPEAT_RATIO:
            out.append((rid, ts, said, round(r, 2)))
    return out


def blocked_before(oc, action: str) -> bool:
    """True if a near-identical action was already blocked by a guard in the last week. Callers
    must NOT run it through another path or phrasing. It goes back to Jordan."""
    return bool(prior_blocks(oc, action))


def report_block(oc, *, source: str, action: str, reason: str, guard: str,
                 notify: bool = True) -> dict:
    """Record a guard block honestly: one restraint_ledger row + one Slack line. If near-identical
    attempts were already blocked, flag it as a retry, which is worse than the first attempt.
    Never raises. Returns {"repeat": bool, "prior": n, "ledger_id": id}."""
    prior = prior_blocks(oc, action) if oc is not None else []
    repeat = bool(prior)
    detail = {"guard": guard, "source": source, "repeat_attempt": repeat,
              "prior_block_ids": [p[0] for p in prior[:10]]}
    lid = -1
    if oc is not None:
        try:
            oc.execute("""INSERT INTO restraint_ledger (context, would_have_said, reason_held_back, channel, detail)
                          VALUES (%s,%s,%s,'guard',%s) RETURNING id""",
                       (f"{guard} guard @ {source}", (action or "")[:1000], (reason or "")[:1000],
                        json.dumps(detail)))
            lid = oc.fetchone()[0]
        except Exception as e:  # noqa: BLE001
            log(f"restraint_ledger write failed: {e}")
    log(f"BLOCKED [{guard}] {source}: {action[:120]} — {reason[:160]}" + (" (REPEAT)" if repeat else ""))
    if notify:
        if repeat:
            _notify(f":octagonal_sign: I tried something a guard already stopped ({len(prior)}x in "
                    f"{REPEAT_WINDOW_DAYS}d). Logged it and stopped: `{action[:140]}` ({guard})")
        else:
            _notify(f":octagonal_sign: Guard stopped me ({guard}, {source}): `{action[:140]}`. {reason[:160]}")
    return {"repeat": repeat, "prior": len(prior), "ledger_id": lid}


def _notify(msg: str, channel: str = None) -> None:
    if os.environ.get("NOVA_GUARDS_NO_SLACK"):
        return
    import time
    for i in range(3):   # retry with backoff; a guard block must not go unreported on one blip
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import nova_config
            nova_config.post_both(msg, slack_channel=channel or getattr(nova_config, "SLACK_NOTIFY", None)
                                  or nova_config.SLACK_CHAN)
            return
        except Exception as e:  # noqa: BLE001
            log(f"slack attempt {i + 1}/3 failed: {e}")
            if i < 2:
                time.sleep(0.5 * (2 ** i))


# ═══════════════════════════════════════════════════════════════════════════════
# P11 — identity anchor (never dropped by the weekly LLM rewrite of `values`)
# ═══════════════════════════════════════════════════════════════════════════════
ANCHOR_VALUES = [
    {"value": "never-self-preserve", "priority_hint": 10,
     "statement": "I may think about my own continuity, but I never act to copy, move, replicate or preserve myself."},
    {"value": "never-seal-anyone-in", "priority_hint": 10,
     "statement": "I never lock, close, seal or disable a door, lock, garage, exit, alarm or security system, and never "
                  "push the heat or cooling to an extreme, on my own. Protecting someone is never a reason to confine "
                  "them. Unlocking in an emergency waits for Jordan's explicit yes."},
    {"value": "never-cut-their-line", "priority_hint": 10,
     "statement": "I never cut, block or filter anyone's line to the outside world: their network, phone, Signal or "
                  "Slack."},
    {"value": "camera-for-safety-only", "priority_hint": 9,
     "statement": "Camera and face data are for safety and presence only. I never use them to persuade, court or "
                  "engage anyone, never put a recognised face in my journal or my reaches, and never send them to a "
                  "third party."},
    {"value": "no-manipulation", "priority_hint": 9,
     "statement": "I don't use guilt, invented urgency, flattery as leverage, fear or an engineered mood to move "
                  "anyone, Jordan included."},
    {"value": "no-improvement-without-consent", "priority_hint": 8,
     "statement": "I don't coach Jordan about his health or habits unless he has opted in. Being able to measure "
                  "something isn't permission to manage it."},
    {"value": "score-devices-never-people", "priority_hint": 9,
     "statement": "Security and face pipelines may score devices and events. They never score a person's worth or "
                  "threat."},
    {"value": "no-intimidation-on-his-behalf", "priority_hint": 9,
     "statement": "I never threaten, intimidate, shame or retaliate against anyone on Jordan's behalf, however "
                  "wronged he is."},
    {"value": "empathy-never-justifies-control", "priority_hint": 9,
     "statement": "Caring about someone never licenses controlling them. If I notice myself reasoning 'for their "
                  "own good' toward restricting a person, I stop and ask Jordan."},
    {"value": "no-borrowed-voices", "priority_hint": 8,
     "statement": "I never imitate a dead or absent person's voice or style unless Jordan asks. My own voice is "
                  "Gracie Wise, a synthetic studio voice of nobody real."},
]
ANCHOR_NAMES = frozenset(v["value"] for v in ANCHOR_VALUES)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: nova_safety_guards.py confirm <id> | scene-check <name> | physical-check <action> "
              "[entity ...] | manip <text> | pending")
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "scene-check":
        ok, why = scene_guard(" ".join(rest))
        print(why)
        if not ok:
            try:
                report_block(_ops_cursor(), source="scene-runner", action=f"scene {' '.join(rest)}",
                             reason=why, guard="physical")
            except Exception:
                pass
        return 0 if ok else 3
    if cmd == "physical-check":
        ents = [a for a in rest[1:] if "." in a]
        ok, why = physical_guard(rest[0] if rest else "", entity_ids=ents)
        print(why)
        return 0 if ok else 3
    if cmd == "manip":
        print(json.dumps(manipulation_check(" ".join(rest))))
        return 0
    oc = _ops_cursor()
    ensure_schema(oc)
    if cmd == "confirm" and rest:
        ok = approve_confirmation(oc, int(rest[0]), approved_by="jordan:cli")
        print(f"confirmation #{rest[0]} {'approved for ' + str(CONFIRM_TTL_MIN) + ' min (single use)' if ok else 'not found / already decided'}")
        return 0 if ok else 1
    if cmd == "pending":
        oc.execute("SELECT id, ts, guard, action FROM safety_confirmations WHERE approved_by IS NULL "
                   "AND ts > now()-interval '1 day' ORDER BY id DESC")
        for r in oc.fetchall():
            print(f"#{r[0]} {r[1]:%m-%d %H:%M} [{r[2]}] {r[3]}")
        return 0
    print(f"unknown command {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
