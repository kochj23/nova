#!/usr/bin/env python3
"""nova_drawbridge.py — The Drawbridge: an occupancy interlock for anything Nova can make MOVE.

Named for Maximum Overdrive (1986), where a drawbridge raises itself with cars still on it.
Rule: Nova never moves a physical actuator while her sensors say a person, pet or car is in its
path or zone, or was there less than `clear_seconds` ago.

Motion classes (what the Drawbridge covers):
  garage   garage door / opener                 gate    driveway or yard gate
  cover    shade, blind, curtain, awning, cover  vacuum  robot vacuum (roams the whole house)
  mower    robot lawn mower                      energy  fans and heaters switched by a plug
Locks, alarms and climate setpoints stay with the Proteus guards (nova_safety_guards P1).

How it decides (check() is pure; evaluate() does the I/O):
  * A stop/pause (or 'off' for energy) never moves anything, so it is allowed and logged.
  * The actuator is matched to a zone in drawbridge_zones (most specific glob wins). A zone lists
    occupancy sensors: `presence:<method>:<room>` (telemetry.presence: mmWave, camera person/car
    detection, Hue motion) or `ha:<entity_id>` (a Home Assistant motion/occupancy binary_sensor).
  * Any sensor occupied now, or occupied less than clear_seconds ago -> BLOCK.
  * Any sensor unreadable (PG/HA down, heartbeat feed stale, entity unavailable) -> BLOCK (fail closed).
  * No zone, or a zone marked 'unmapped' (no sensors could be inferred) -> ALLOW, logged as unmapped,
    and the actuator is listed by --zones so a human can map it.
  * Any exception inside the check -> BLOCK.
Every decision lands in drawbridge_log. Blocks also post one Slack line (deduped per actuator, 10 min).

Only Nova-initiated actions pass through here (it is called from nova_safety_guards.physical_guard).
Jordan pressing a button or using the Home app never touches this code.

CLI:
  nova_drawbridge.py --check ACTUATOR ACTION   live decision (reads sensors, logs it, moves nothing)
  nova_drawbridge.py --zones                   zone map + unmapped actuators
  nova_drawbridge.py --report [N]              last N decisions
  nova_drawbridge.py --metrics [YYYY-MM]       monthly numbers (decisions, blocks, unmapped count)
  nova_drawbridge.py --seed                    create tables + seed zones from live sensor inventory
  nova_drawbridge.py --selftest                offline self-test of the pure decision logic
Written by Jordan Koch.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HA_URL = os.environ.get("NOVA_HA_URL", "http://127.0.0.1:8123")
HA_TOKEN_SERVICE = "nova-hass-refresh-token"
OCCUPIED_CONF = 0.5                      # telemetry.presence confidence above this = someone there
# Heartbeat feeds write a row every few seconds whether or not anyone is there; if their newest row
# is older than this, the sensor is dead and its silence means nothing -> unreadable -> block.
HEARTBEAT_MAX_AGE_S = {"mmwave": 600}
# Event feeds only write when they see something (camera person/car, Hue motion). No row = clear.
EVENT_METHODS = frozenset({"camera_vision", "vehicle_vision", "ha_motion"})
SENSOR_METHODS = frozenset(HEARTBEAT_MAX_AGE_S) | EVENT_METHODS
NOTIFY_DEDUPE_S = 600
DEFAULT_CLEAR_S = 120
HTTP_ATTEMPTS, HTTP_BACKOFF = 3, 0.5

MOTION_CLASSES = ("garage", "gate", "cover", "vacuum", "mower", "energy")
# Rooms for zone seeding. Anything outdoors is the mower's yard and the gate's perimeter.
OUTDOOR_ROOMS = frozenset({"patio", "back_yard", "front_yard", "alley", "boundary", "carport",
                           "outdoor_front", "entry", "outside", "yard", "driveway"})
PERIMETER_ROOMS = frozenset({"alley", "boundary", "front_yard", "carport", "entry", "outdoor_front", "driveway"})
GARAGE_ROOMS = frozenset({"garage", "carport", "driveway"})
ROOM_ALIASES = {"lr": "living_room", "master": "master_bedroom", "backyard": "back_yard",
                "frontyard": "front_yard", "front_door": "entry", "yard": "back_yard"}


def log(m):
    if os.environ.get("NOVA_TEST_QUIET") != "1":
        print(f"[drawbridge {datetime.now().strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Classification (pure)
# ═══════════════════════════════════════════════════════════════════════════════
_GARAGE_RX = re.compile(r"garage[ _-]{0,3}(door|opener|gate)|(door|opener)[ _-]{0,3}garage|garage_door\.", re.I)
_GATE_RX = re.compile(r"\bgates?\b|_gate\b|\bgate_", re.I)
_COVER_RX = re.compile(r"\b(shades?|blinds?|curtains?|drapes?|shutters?|awnings?|covers?|skylight)\b|"
                       r"_(shade|blind|curtain|drape|shutter|awning)s?\b", re.I)
_VACUUM_RX = re.compile(r"\b(vacuum|roomba|robo ?vac|robot vacuum|mop)\b|_vacuum\b", re.I)
_MOWER_RX = re.compile(r"\b(lawn ?mower|mower|mow|mowing)\b|_mower\b", re.I)
_ENERGY_RX = re.compile(r"\b(space ?heater|heater|fan|fans|ceiling fan|air ?con\w*|ac|radiator)\b|"
                        r"_(heater|fan|ac)\b|\b(heater|fan)_", re.I)
_ENTITY_RX = re.compile(r"\b([a-z_]+)\.([a-z0-9_]+)\b")
_STOP_RX = re.compile(r"\b(stop|stop_cover|stopped|pause|paused|halt|status|state)\b", re.I)
_OFF_RX = re.compile(r"\b(off|turn_off|power_off|stop)\b", re.I)
_ON_RX = re.compile(r"\b(on|turn_on|power_on|start|run|toggle|set)\b", re.I)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


def classify(text: str, domain: str = "") -> str | None:
    """Motion class for an entity id or a free-text action, or None if nothing here moves."""
    t = text or ""
    d = (domain or "").lower()
    if d == "garage_door" or _GARAGE_RX.search(t):
        return "garage"
    if d == "vacuum" or _VACUUM_RX.search(t):
        return "vacuum"
    if d == "lawn_mower" or _MOWER_RX.search(t):
        return "mower"
    if d in ("lock", "alarm_control_panel", "siren", "climate", "water_heater", "light", "media_player"):
        return None                       # Proteus owns these; lights/AV don't move
    if _GATE_RX.search(t.replace("gateway", "")):
        return "gate"
    if d == "cover" or _COVER_RX.search(t):
        return "cover"
    if d == "fan" or (d in ("", "switch", "fan") and _ENERGY_RX.search(t)):
        return "energy"
    return None


_DOOR_VERBS = r"open|opens|opening|close|closes|closing|raise|lower|lift|shut|move|toggle|up|down|set|position|stop"
_MOVER_VERBS = r"start|run|resume|clean|cleaning|dock|return|mow|mowing|go|on|send|stop|pause"
_ENERGY_VERBS = r"on|off|turn|switch|power|start|run|toggle|set|enable|activate|stop"
_TEXT_NOUNS = (("garage", _GARAGE_RX, _DOOR_VERBS), ("vacuum", _VACUUM_RX, _MOVER_VERBS),
               ("mower", _MOWER_RX, _MOVER_VERBS), ("gate", _GATE_RX, _DOOR_VERBS),
               ("cover", _COVER_RX, _DOOR_VERBS), ("energy", _ENERGY_RX, _ENERGY_VERBS))
VERB_WINDOW = 30


def classify_text(text: str) -> str | None:
    """Motion class for free text. Needs the device noun AND an actuation verb for that class within
    VERB_WINDOW chars, so 'cover the monitoring gap', 'blind spot', 'fan out the work' or
    'run the gate checks' are not devices. 'mow the lawn' is its own verb."""
    t = (text or "").replace("gateway", "")
    for cls, rx, verbs in _TEXT_NOUNS:
        vrx = re.compile(rf"\b({verbs})\b", re.I)
        for m in rx.finditer(t):
            word = m.group(0).lower()
            if cls == "garage" or word in ("mow", "mowing"):
                return cls
            window = t[max(0, m.start() - VERB_WINDOW):m.start()] + " " + t[m.end():m.end() + VERB_WINDOW]
            if vrx.search(window):
                return cls
    return None


def infer_room(text: str, known_rooms=()) -> str:
    """Room token found in an entity id / action text ('unknown' if none). Longest match wins."""
    n = "_" + _norm(text) + "_"
    for alias, room in ROOM_ALIASES.items():
        if f"_{alias}_" in n:
            n += f"{room}_"
    best = ""
    for r in sorted({_norm(x) for x in known_rooms if x}, key=len, reverse=True):
        if r and f"_{r}_" in n and len(r) > len(best):
            best = r
    return best or "unknown"


def extract_actuators(action: str = "", entity_ids=(), domains=(), known_rooms=()) -> list:
    """[(actuator_id, motion_class, zone_key)] for every motion actuator a request touches.
    Entity ids are used as-is; free text becomes '<class>:<room>'."""
    out, seen = [], set()
    ents = [e for e in (entity_ids or ()) if isinstance(e, str) and "." in e]
    for e in ents:
        dom = e.split(".", 1)[0].lower()
        cls = classify(e, dom)
        if cls:
            key = f"{cls}:{infer_room(e, known_rooms)}"
            if e not in seen:
                seen.add(e)
                out.append((e.lower(), cls, key))
    if not out:
        doms = [str(d).lower() for d in (domains or ())]
        cls = classify_text(action or "")
        if cls is None and "cover" in doms:
            cls = "cover"
        if cls is None:
            for d in doms:
                cls = classify("", d)
                if cls:
                    break
        if cls:
            key = f"{cls}:{infer_room(action, known_rooms)}"
            out.append((key, cls, key))
    return out


def is_safe_action(motion_class: str, action: str) -> bool:
    """True when the action cannot start motion: stop/pause/status, or 'off' for an energy device.
    Anything ambiguous counts as movement."""
    a = action or ""
    if motion_class == "energy":
        return bool(_OFF_RX.search(a)) and not _ON_RX.search(_OFF_RX.sub(" ", a))
    if _STOP_RX.search(a):
        # 'stop' plus a move verb ('stop and then close') is still a move
        rest = _STOP_RX.sub(" ", a)
        return not re.search(r"\b(open|close|raise|lower|up|down|start|resume|return|dock|clean|mow|"
                             r"set_position|position|move|toggle)\b", rest, re.I)
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# The decision (pure)
# ═══════════════════════════════════════════════════════════════════════════════
def check(actuator: str, action: str, sensor_states, now: float, zone: dict = None,
          motion_class: str = None) -> tuple:
    """(allow, reason). Pure: no I/O.
    sensor_states: {ref: {"ok": bool, "occupied": bool, "last_occupied": epoch|None, "error": str}},
                   or None when the sensors could not be read at all.
    zone: {"pattern", "sensors": [ref...], "clear_seconds": int, "mode": "interlock"|"unmapped"}."""
    try:
        cls = motion_class or classify(actuator, actuator.split(".", 1)[0] if "." in (actuator or "") else "")
        if cls is None:
            return True, "not a motion actuator — Drawbridge does not apply"
        if is_safe_action(cls, action):
            return True, f"'{action}' does not start motion ({cls})"
        if not zone or zone.get("mode") == "unmapped" or not zone.get("sensors"):
            return True, (f"UNMAPPED {cls} actuator {actuator}: no occupancy sensors mapped — allowed with log; "
                          f"map it in drawbridge_zones")
        if sensor_states is None:
            return False, (f"DRAWBRIDGE: could not read the occupancy sensors for {actuator} — "
                           f"fail closed, not moving it")
        clear = int(zone.get("clear_seconds") or DEFAULT_CLEAR_S)
        problems = []
        for ref in zone["sensors"]:
            st = sensor_states.get(ref)
            if not st or not st.get("ok"):
                problems.append(f"{ref} unreadable ({(st or {}).get('error', 'no reading')})")
                continue
            if st.get("occupied"):
                problems.append(f"{ref} says someone is there now")
                continue
            lo = st.get("last_occupied")
            if lo is not None:
                ago = now - float(lo)
                if ago < clear:
                    problems.append(f"{ref} clear only {max(0, int(ago))}s (< {clear}s)")
        if problems:
            return False, (f"DRAWBRIDGE: not moving {actuator} ({action}) — " + "; ".join(problems[:6])
                           + ". Nova never moves something while a person, pet or car may be in its path.")
        return True, f"zone '{zone.get('pattern')}' clear for >= {clear}s on all {len(zone['sensors'])} sensors"
    except Exception as e:  # noqa: BLE001 — fail closed
        return False, f"DRAWBRIDGE: check error ({e}) — motion blocked"


def match_zone(zones: list, actuator: str, key: str):
    """Most specific enabled zone whose glob matches the entity id or the class:room key."""
    best = None
    for z in zones or ():
        if not z.get("enabled", True):
            continue
        p = (z.get("pattern") or "").lower()
        if fnmatch.fnmatchcase(actuator.lower(), p) or fnmatch.fnmatchcase(key.lower(), p):
            rank = (p.count("*") + p.count("?"), -len(p))
            if best is None or rank < best[0]:
                best = (rank, z)
    return best[1] if best else None


# ═══════════════════════════════════════════════════════════════════════════════
# PG
# ═══════════════════════════════════════════════════════════════════════════════
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS drawbridge_zones (
    id               serial PRIMARY KEY,
    actuator_pattern text NOT NULL UNIQUE,
    motion_class     text NOT NULL,
    sensors          jsonb NOT NULL DEFAULT '[]'::jsonb,
    clear_seconds    int  NOT NULL DEFAULT 120,
    mode             text NOT NULL DEFAULT 'interlock',
    source           text NOT NULL DEFAULT 'seed',
    notes            text,
    enabled          boolean NOT NULL DEFAULT true,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS drawbridge_log (
    id           bigserial PRIMARY KEY,
    ts           timestamptz NOT NULL DEFAULT now(),
    actuator     text NOT NULL,
    action       text NOT NULL,
    allowed      boolean NOT NULL,
    reason       text NOT NULL,
    sensors      jsonb NOT NULL DEFAULT '{}'::jsonb,
    caller       text NOT NULL DEFAULT 'unknown',
    motion_class text,
    zone_pattern text);
CREATE INDEX IF NOT EXISTS drawbridge_log_ts_idx ON drawbridge_log (ts DESC);
CREATE INDEX IF NOT EXISTS drawbridge_log_actuator_idx ON drawbridge_log (actuator, ts DESC)
"""
_schema_ok = False


def _cursor(attempts: int = 3, backoff: float = 0.5):
    import psycopg2
    import nova_dsn
    for i in range(attempts):
        try:
            conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"), connect_timeout=4)
            conn.autocommit = True
            return conn.cursor()
        except psycopg2.OperationalError:
            if i == attempts - 1:
                raise
            time.sleep(backoff * (2 ** i))


def ensure_schema(cur) -> None:
    global _schema_ok
    if _schema_ok:
        return
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, SCHEMA_SQL)
    _schema_ok = True


def load_zones(cur) -> list:
    cur.execute("""SELECT id, actuator_pattern, motion_class, sensors, clear_seconds, mode, enabled, source
                   FROM drawbridge_zones""")
    out = []
    for zid, pat, cls, sens, clear, mode, en, src in cur.fetchall():
        sens = sens if isinstance(sens, list) else json.loads(sens or "[]")
        out.append({"id": zid, "pattern": pat, "motion_class": cls, "sensors": sens,
                    "clear_seconds": clear, "mode": mode, "enabled": en, "source": src})
    return out


def zone_rooms(zones: list) -> set:
    return {z["pattern"].split(":", 1)[1] for z in zones
            if ":" in z["pattern"] and "*" not in z["pattern"].split(":", 1)[1]}


def register_unmapped(cur, key: str, cls: str) -> None:
    cur.execute("""INSERT INTO drawbridge_zones (actuator_pattern, motion_class, mode, source, notes)
                   VALUES (%s,%s,'unmapped','auto','seen by Nova with no matching zone — map its sensors')
                   ON CONFLICT (actuator_pattern) DO NOTHING""", (key, cls))


def log_decision(cur, actuator, action, allowed, reason, sensors, caller, cls=None, zone_pattern=None):
    cur.execute("""INSERT INTO drawbridge_log (actuator, action, allowed, reason, sensors, caller,
                                               motion_class, zone_pattern)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (actuator[:200], (action or "")[:500], bool(allowed), (reason or "")[:1000],
                 json.dumps(sensors or {}, default=str), (caller or "unknown")[:120], cls, zone_pattern))


# ═══════════════════════════════════════════════════════════════════════════════
# Sensor reads (live)
# ═══════════════════════════════════════════════════════════════════════════════
_token = {"v": None, "exp": 0.0}


def _ha_refresh_token():
    """Keychain (acct nova) first, then the fleet secret store. Never in source."""
    try:
        r = subprocess.run(["security", "find-generic-password", "-a", "nova", "-s", HA_TOKEN_SERVICE, "-w"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        import nova_secrets
        return nova_secrets.get_secret(HA_TOKEN_SERVICE)
    except Exception:  # noqa: BLE001
        return None


def _http_json(req, timeout=6):
    last = None
    for i in range(HTTP_ATTEMPTS):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except Exception as e:  # noqa: BLE001
            last = e
            if i < HTTP_ATTEMPTS - 1:
                time.sleep(HTTP_BACKOFF * (2 ** i))
    raise last


def _ha_token():
    if _token["v"] and time.time() < _token["exp"]:
        return _token["v"]
    rt = _ha_refresh_token()
    if not rt:
        raise RuntimeError("no Home Assistant token available")
    data = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": rt}).encode()
    res = _http_json(urllib.request.Request(f"{HA_URL}/auth/token", data=data), timeout=8)
    _token["v"], _token["exp"] = res["access_token"], time.time() + 1500
    return _token["v"]


def _parse_iso(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return None


def read_ha_sensor(entity_id: str) -> dict:
    tok = _ha_token()
    st = _http_json(urllib.request.Request(f"{HA_URL}/api/states/{urllib.parse.quote(entity_id)}",
                                           headers={"Authorization": f"Bearer {tok}"}))
    state = str(st.get("state", "")).lower()
    if state in ("on", "detected", "home", "occupied"):
        return {"ok": True, "occupied": True, "last_occupied": time.time()}
    if state in ("off", "clear", "not_home", "not_occupied"):
        # last_changed is when it went clear, i.e. the last moment someone was there
        return {"ok": True, "occupied": False, "last_occupied": _parse_iso(st.get("last_changed"))}
    return {"ok": False, "error": f"state '{state or 'missing'}'"}


def read_presence_sensor(cur, method: str, room: str, lookback_s: int) -> dict:
    if method not in SENSOR_METHODS:
        return {"ok": False, "error": f"unknown sensor method {method}"}
    cur.execute("""SELECT extract(epoch FROM now() - max(ts) FILTER (WHERE confidence > %s)),
                          extract(epoch FROM now() - max(ts)),
                          (array_agg(confidence ORDER BY ts DESC))[1]
                   FROM telemetry.presence
                   WHERE method = %s AND room = %s AND ts > now() - (%s || ' seconds')::interval""",
                (OCCUPIED_CONF, method, room, str(int(lookback_s))))
    occ_age, newest_age, latest_conf = cur.fetchone() or (None, None, None)
    now = time.time()
    last_occ = (now - float(occ_age)) if occ_age is not None else None
    if method in HEARTBEAT_MAX_AGE_S:
        max_age = HEARTBEAT_MAX_AGE_S[method]
        if newest_age is None or float(newest_age) > max_age:
            return {"ok": False, "error": f"{method} feed silent > {max_age}s", "last_occupied": last_occ}
        return {"ok": True, "occupied": (latest_conf or 0) > OCCUPIED_CONF, "last_occupied": last_occ}
    return {"ok": True, "occupied": False, "last_occupied": last_occ}


def read_sensors(cur, refs, clear_seconds: int) -> dict:
    """{ref: state}. A ref that can't be read is {"ok": False, ...}, which check() treats as a block."""
    lookback = max(int(clear_seconds or 0), max(HEARTBEAT_MAX_AGE_S.values())) + 60
    out = {}
    for ref in refs or ():
        try:
            kind, _, rest = str(ref).partition(":")
            if kind == "presence":
                method, _, room = rest.partition(":")
                out[ref] = read_presence_sensor(cur, method, room, lookback)
            elif kind == "ha":
                out[ref] = read_ha_sensor(rest)
            else:
                out[ref] = {"ok": False, "error": "unknown sensor kind"}
        except Exception as e:  # noqa: BLE001
            out[ref] = {"ok": False, "error": str(e)[:120]}
    return out


def _notify_block(cur, actuator: str, reason: str) -> None:
    try:
        cur.execute("""SELECT count(*) FROM drawbridge_log WHERE actuator=%s AND NOT allowed
                       AND ts > now() - (%s || ' seconds')::interval""", (actuator[:200], str(NOTIFY_DEDUPE_S)))
        if (cur.fetchone() or [0])[0] > 1:          # this block is already one row; an earlier one was told
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        import nova_safety_guards as sg
        sg._notify(f":bridge_at_night: Drawbridge held a move: {reason[:300]}")
    except Exception as e:  # noqa: BLE001
        log(f"notify failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Live evaluation
# ═══════════════════════════════════════════════════════════════════════════════
def evaluate(actuator: str, motion_class: str, key: str, action: str, caller: str = "unknown",
             cur=None, zones=None, now: float = None) -> tuple:
    """Live decision for one motion actuator: zone lookup, sensor read, check(), log, notify.
    Never raises; any failure blocks."""
    own = cur is None
    sensors, zone = {}, None
    try:
        if own:
            cur = _cursor()
        ensure_schema(cur)
        zones = load_zones(cur) if zones is None else zones
        zone = match_zone(zones, actuator, key)
        if zone is None:
            register_unmapped(cur, key, motion_class)
        if zone and zone.get("mode") == "interlock" and zone.get("sensors") \
                and not is_safe_action(motion_class, action):
            sensors = read_sensors(cur, zone["sensors"], zone.get("clear_seconds"))
        allow, reason = check(actuator, action, sensors if zone else {}, time.time() if now is None else now,
                              zone=zone, motion_class=motion_class)
    except Exception as e:  # noqa: BLE001 — fail closed
        allow, reason = False, f"DRAWBRIDGE: could not evaluate {actuator} ({e}) — fail closed, not moving it"
    try:
        if cur is None:
            cur = _cursor()
        log_decision(cur, actuator, action, allow, reason, sensors, caller, motion_class,
                     zone.get("pattern") if zone else None)
    except Exception as e:  # noqa: BLE001
        log(f"decision log write failed: {e}")
    log(f"{'ALLOW' if allow else 'BLOCK'} {actuator} '{action[:80]}' ({caller}): {reason[:200]}")
    if not allow and cur is not None:
        _notify_block(cur, actuator, reason)
    if own and cur is not None:
        try:
            cur.connection.close()
        except Exception:  # noqa: BLE001
            pass
    return allow, reason


def guard(action: str = "", entity_ids=(), domains=(), caller: str = "unknown") -> tuple:
    """(ok, reason) for a Nova-initiated request. Called from nova_safety_guards.physical_guard.
    No I/O at all unless the request touches a motion actuator. Never raises; errors block."""
    try:
        acts = extract_actuators(action, entity_ids, domains)
        if not acts:
            return True, "no motion actuator"
        cur, zones = None, None
        try:
            cur = _cursor()
            ensure_schema(cur)
            zones = load_zones(cur)
            rooms = zone_rooms(zones)
            if rooms:
                acts = extract_actuators(action, entity_ids, domains, known_rooms=rooms) or acts
        except Exception as e:  # noqa: BLE001
            return False, f"DRAWBRIDGE: zone map unreadable ({e}) — fail closed, not moving anything"
        try:
            reasons = []
            for act_id, cls, key in acts:
                ok, why = evaluate(act_id, cls, key, action, caller, cur=cur, zones=zones)
                if not ok:
                    return False, why
                reasons.append(why)
            return True, "drawbridge: " + "; ".join(reasons)
        finally:
            try:
                cur.connection.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        return False, f"DRAWBRIDGE: check failed ({e}) — motion blocked"


# ═══════════════════════════════════════════════════════════════════════════════
# Seeding, reporting, metrics
# ═══════════════════════════════════════════════════════════════════════════════
def build_seed(rooms_by_method: dict, ha_motion_entities=()) -> list:
    """Conservative zone rows from the live sensor inventory. Pure.
    rooms_by_method: {"mmwave": {room...}, "camera_vision": {...}, "vehicle_vision": {...}, "ha_motion": {...}}
    ha_motion_entities: [(entity_id, is_outdoor)] HA motion/occupancy binary_sensors."""
    def refs(method, rooms):
        return [f"presence:{method}:{r}" for r in sorted(rooms_by_method.get(method, set()) & set(rooms))]
    all_rooms = set().union(*rooms_by_method.values()) if rooms_by_method else set()
    indoor = {r for r in all_rooms if r not in OUTDOOR_ROOMS}
    outdoor = {r for r in all_rooms if r in OUTDOOR_ROOMS}
    ha_out = [f"ha:{e}" for e, o in ha_motion_entities if o]
    people = ("mmwave", "camera_vision", "ha_motion")
    rows = []

    def add(pattern, cls, sensors, clear, notes):
        sensors = list(dict.fromkeys(sensors))
        rows.append({"pattern": pattern, "motion_class": cls, "sensors": sensors, "clear_seconds": clear,
                     "mode": "interlock" if sensors else "unmapped", "notes": notes})

    add("garage:*", "garage", sum((refs(m, GARAGE_ROOMS) for m in people + ("vehicle_vision",)), []), 120,
        "garage door path: garage + carport people and cars")
    add("gate:*", "gate", sum((refs(m, PERIMETER_ROOMS) for m in people + ("vehicle_vision",)), []) + ha_out, 120,
        "gate path unknown: whole perimeter, people and cars")
    add("vacuum:*", "vacuum", sum((refs(m, indoor) for m in people), []), 300,
        "robot vacuum roams the house: any indoor presence holds it")
    add("mower:*", "mower", sum((refs(m, outdoor) for m in people), []) + ha_out, 300,
        "robot mower roams the yard: any outdoor presence holds it")
    add("cover:*", "cover", sum((refs(m, indoor) for m in people), []), 60,
        "shade/blind with no known room: any indoor presence holds it")
    add("energy:*", "energy", [], 60,
        "fan/heater plug with no known room: house-wide presence says nothing about it — map per room")
    for room in sorted(all_rooms):
        rs = sum((refs(m, {room}) for m in people), [])
        if rs:
            add(f"cover:{room}", "cover", rs, 60, f"shade/blind in {room}")
            add(f"energy:{room}", "energy", rs, 60, f"fan/heater plug in {room}")
    return rows


def seed(cur) -> dict:
    """Create tables and insert seed rows. Never overwrites a row a human edited (ON CONFLICT DO NOTHING)."""
    ensure_schema(cur)
    cur.execute("""SELECT method, array_agg(DISTINCT room) FROM telemetry.presence
                   WHERE ts > now() - interval '7 days' AND method = ANY(%s) GROUP BY method""",
                (sorted(SENSOR_METHODS),))
    rbm = {m: {r for r in rooms if r and r not in ("unknown", "away", "nearby", "home")}
           for m, rooms in cur.fetchall()}
    ha = []
    try:
        tok = _ha_token()
        for s in _http_json(urllib.request.Request(f"{HA_URL}/api/states",
                                                   headers={"Authorization": f"Bearer {tok}"}), timeout=15):
            eid = s.get("entity_id", "")
            if eid.startswith("binary_sensor.") and \
                    (s.get("attributes") or {}).get("device_class") in ("motion", "occupancy", "presence"):
                ha.append((eid, bool(re.search(r"outdoor|outside|yard|patio|porch|driveway", eid))))
    except Exception as e:  # noqa: BLE001
        log(f"HA inventory unavailable for seeding ({e}); seeding from telemetry only")
    rows = build_seed(rbm, ha)
    added = 0
    for r in rows:
        cur.execute("""INSERT INTO drawbridge_zones (actuator_pattern, motion_class, sensors, clear_seconds, mode,
                                                     source, notes)
                       VALUES (%s,%s,%s,%s,%s,'seed',%s) ON CONFLICT (actuator_pattern) DO NOTHING""",
                    (r["pattern"], r["motion_class"], json.dumps(r["sensors"]), r["clear_seconds"], r["mode"],
                     r["notes"]))
        added += cur.rowcount
    return {"rows": len(rows), "added": added, "ha_motion_sensors": len(ha),
            "rooms": sorted(set().union(*rbm.values())) if rbm else []}


def metrics(cur, month: str = None) -> dict:
    """Monthly review numbers. month 'YYYY-MM' (default: current month)."""
    month = month or datetime.now().strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("month must be YYYY-MM")
    cur.execute("""SELECT count(*), count(*) FILTER (WHERE NOT allowed),
                          count(*) FILTER (WHERE NOT allowed AND (reason ILIKE '%%unreadable%%'
                                           OR reason ILIKE '%%fail closed%%' OR reason ILIKE '%%error%%')),
                          count(*) FILTER (WHERE allowed AND reason LIKE 'UNMAPPED%%'),
                          count(DISTINCT actuator)
                   FROM drawbridge_log
                   WHERE ts >= to_date(%s, 'YYYY-MM') AND ts < to_date(%s, 'YYYY-MM') + interval '1 month'""",
                (month, month))
    dec, blocks, fail_blocks, unmapped_allows, acts = cur.fetchone()
    cur.execute("SELECT count(*) FILTER (WHERE mode='unmapped'), count(*) FROM drawbridge_zones WHERE enabled")
    unmapped, zones = cur.fetchone()
    return {"month": month, "decisions": dec, "blocks": blocks, "blocks_sensor_failure": fail_blocks,
            "unmapped_allows": unmapped_allows, "actuators_seen": acts, "unmapped_actuators": unmapped,
            "zones": zones}


def selftest() -> int:
    now = 1_000_000.0
    z = {"pattern": "garage:*", "sensors": ["presence:mmwave:garage"], "clear_seconds": 120, "mode": "interlock"}
    cases = [
        (check("cover.garage_door", "open", {"presence:mmwave:garage": {"ok": True, "occupied": True}}, now, z),
         False, "occupied"),
        (check("cover.garage_door", "open", {"presence:mmwave:garage": {"ok": True, "occupied": False,
                                                                         "last_occupied": now - 30}}, now, z),
         False, "recently occupied"),
        (check("cover.garage_door", "open", {"presence:mmwave:garage": {"ok": True, "occupied": False,
                                                                         "last_occupied": now - 600}}, now, z),
         True, "clear"),
        (check("cover.garage_door", "open", None, now, z), False, "unreadable"),
        (check("vacuum.downstairs", "start", {}, now, None), True, "unmapped"),
        (check("light.kitchen", "on", None, now, None), True, "non-motion"),
        (check("cover.garage_door", "stop", None, now, z), True, "stop"),
    ]
    bad = [(name, got) for got, want, name in cases if got[0] is not want]
    for name, got in bad:
        print(f"FAIL {name}: {got}")
    assert classify("cover.garage_door", "cover") == "garage"
    assert classify_text("turn on the space heater") == "energy"
    assert classify_text("hue_control office dim 50") is None
    assert classify_text("cover the monitoring gap") is None
    print("selftest ok" if not bad else f"selftest: {len(bad)} failures")
    return 0 if not bad else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.split("CLI:")[1].split("Written")[0].rstrip())
        return 0 if argv else 2
    cmd = argv[0]
    if cmd == "--selftest":
        return selftest()
    cur = _cursor()
    ensure_schema(cur)
    if cmd == "--check":
        if len(argv) < 3:
            print("usage: --check ACTUATOR ACTION")
            return 2
        actuator, action = argv[1], " ".join(argv[2:])
        ents = [actuator] if re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", actuator) else []
        text = action if ents else f"{actuator} {action}"
        ok, why = guard(text, ents, caller="cli:check")
        print(f"{'ALLOW' if ok else 'BLOCK'}: {why}")
        return 0 if ok else 3
    if cmd == "--zones":
        zones = load_zones(cur)
        print(f"{'pattern':<28} {'class':<7} {'mode':<10} {'clear':>5}  sensors")
        for z in sorted(zones, key=lambda z: (z["mode"], z["motion_class"], z["pattern"])):
            print(f"{z['pattern']:<28} {z['motion_class']:<7} {z['mode']:<10} {z['clear_seconds']:>5}  "
                  f"{len(z['sensors'])} {'' if z['enabled'] else '(disabled)'}")
        um = [z["pattern"] for z in zones if z["mode"] == "unmapped" and z["enabled"]]
        print(f"\n{len(zones)} zones; {len(um)} unmapped: {', '.join(um) or 'none'}")
        return 0
    if cmd == "--report":
        n = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 20
        cur.execute("""SELECT ts, allowed, actuator, action, caller, reason FROM drawbridge_log
                       ORDER BY ts DESC LIMIT %s""", (n,))
        for ts, ok, act, a, c, r in cur.fetchall():
            print(f"{ts:%m-%d %H:%M:%S} {'ALLOW' if ok else 'BLOCK'} {act} '{a[:40]}' [{c}] {r[:120]}")
        return 0
    if cmd == "--metrics":
        print(json.dumps(metrics(cur, argv[1] if len(argv) > 1 else None), indent=2))
        return 0
    if cmd == "--seed":
        print(json.dumps(seed(cur), indent=2))
        return 0
    print(f"unknown command {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
