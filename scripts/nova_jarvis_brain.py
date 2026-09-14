#!/opt/homebrew/bin/python3
"""
nova_jarvis_brain.py — JARVIS Phases 3, 5, 6 combined.

Phase 3: Continuous Visual Understanding
  - Periodically runs vision model on interior camera frames
  - Writes scene descriptions to environment_state table

Phase 5: Activity Classifier
  - Fuses presence, power, media, lighting, and time signals
  - Infers activity state: deep_work, meeting, entertainment, cooking, break, sleeping, away
  - Writes to telemetry.activity

Phase 6: Environmental State Awareness
  - Correlates all signals into actionable environmental observations
  - Generates contextual suggestions via shared_observations

Runs every 2 minutes. HTTP API on port 37480.

Written by Jordan Koch.
"""

import asyncio
import json
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

try:
    import asyncpg
    from aiohttp import web
except ImportError as e:
    print(f"FATAL: missing dependency: {e}", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))

VERSION = "1.0.0"
DB_DSN = "postgresql://kochj@pg-primary.digitalnoise.net:5432/nova_ops"
HA_URL = "http://127.0.0.1:8123"
OLLAMA_URL = "http://127.0.0.1:11434"
HTTP_PORT = 37480
POLL_INTERVAL = 120
FRAME_DIR = Path.home() / ".openclaw/workspace/camera_frames"
LOG_FILE = Path.home() / ".openclaw/logs/nova_jarvis_brain.log"

INTERIOR_CAMERAS = {
    "interior_living_room_latest.jpg": "living_room",
    "interior_kitchen_alley_latest.jpg": "kitchen",
    "interior_front_door_latest.jpg": "hall",
}

ACTIVITY_STATES = [
    "deep_work", "meeting", "entertainment", "cooking",
    "break", "sleeping", "away", "relaxing",
]

_shutdown = False
_pool = None
_start_time = time.time()
_current_activity = {"state": "unknown", "confidence": 0.0, "since": None, "signals": {}}
_last_activity_write = None  # datetime of last telemetry.activity write (heartbeat tracking)
# Re-record the current activity even when unchanged at least this often, so a stable
# (or stuck) classification never makes the stream look DEAD to freshness monitoring.
# Root cause of the 2026-09-02 -> 2026-09-09 silence: writes were change-only, no heartbeat.
ACTIVITY_HEARTBEAT_SEC = 900  # 15 min
_environment = {}  # room -> latest scene

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[jarvis {ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _keychain(service):
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", service, "-w"],
        capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else None


def get_ha_token():
    refresh_token = _keychain("nova-hass-refresh-token")
    if not refresh_token:
        return None
    form_data = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh_token}).encode()
    try:
        req = urllib.request.Request(f"{HA_URL}/auth/token", data=form_data)
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())["access_token"]
    except Exception:
        return None


def ha_get_states():
    token = get_ha_token()
    if not token:
        return []
    try:
        req = urllib.request.Request(f"{HA_URL}/api/states",
            headers={"Authorization": f"Bearer {token}"})
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except Exception:
        return []


# ── Phase 3: Visual Understanding ───────────────────────────────────────────

# Vision routing: local Ollama (qwen3-vl) is the ONLY path — interior camera
# frames of people are strictly on-box and MUST NEVER leave the machine (hard
# local-only rule, DLP). It shares the GPU with everything else and can stall
# under contention, so a circuit breaker tracks Ollama health: after
# VISION_FAIL_THRESHOLD consecutive failures the circuit opens and we stop
# hammering Ollama for VISION_COOLDOWN seconds (then probe once to recover).
#
# When Ollama is unavailable (failing or circuit open), vision returns None and
# the capability simply goes dark for the cooldown window. There is NO cloud
# fallback: we would rather lose scene descriptions than send a frame of a
# person off-box. Do not reintroduce any remote vision call here.
VISION_TIMEOUT = 45             # local Ollama per-call ceiling (was 60)
VISION_FAIL_THRESHOLD = 3       # consecutive Ollama failures before opening
VISION_COOLDOWN = 900           # seconds the circuit stays open (15 min)
_vision_cb = {"fails": 0, "open_until": 0.0, "logged_open": False}

VISION_PROMPT = (
    "Describe this room scene in 1-2 sentences. "
    "Note: who is present (person/people), what they're doing, "
    "lighting level (bright/dim/dark), and general activity. "
    "Be concise and factual."
)


def _vision_ollama(b64):
    """PRIMARY: local qwen3-vl via Ollama. Returns description or raises."""
    payload = json.dumps({
        "model": "qwen3-vl:4b",
        "messages": [{"role": "user", "content": VISION_PROMPT}],
        "images": [b64],
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 100},
    }).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat",
        data=payload, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=VISION_TIMEOUT)
    content = json.loads(resp.read()).get("message", {}).get("content", "")
    if "<think>" in content:
        content = content.split("</think>")[-1].strip()
    return content


def vision_describe(image_path):
    """Describe a camera frame using LOCAL Ollama ONLY.

    Interior camera frames of people are strictly on-box: if local Ollama is
    unavailable (failing or circuit open) this returns None and the scene is
    simply skipped this cycle. There is deliberately NO cloud fallback — a
    frame of a person must never leave the machine.

    NOTE: this is a blocking call; callers run it in an executor so it never
    freezes the brain's async loop.
    """
    import base64
    now = time.time()
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except Exception as e:
        log(f"Vision: cannot read frame {image_path}: {e}", "ERROR")
        return None

    # Local Ollama is the only path. If its circuit is open, do not describe.
    if now < _vision_cb["open_until"]:
        return None

    try:
        content = _vision_ollama(b64)
        # Success → reset breaker (announce recovery if it had tripped).
        if _vision_cb["logged_open"]:
            log("Vision recovered — local circuit closed")
        _vision_cb["fails"] = 0
        _vision_cb["logged_open"] = False
        return content
    except Exception as e:
        _vision_cb["fails"] += 1
        if _vision_cb["fails"] >= VISION_FAIL_THRESHOLD:
            _vision_cb["open_until"] = time.time() + VISION_COOLDOWN
            if not _vision_cb["logged_open"]:
                log(f"Local vision failing ({_vision_cb['fails']}x, last: {e}) "
                    f"— circuit OPEN {VISION_COOLDOWN // 60}m, vision unavailable "
                    f"(NO cloud fallback — frames stay on-box)", "WARN")
                _vision_cb["logged_open"] = True
        else:
            log(f"Local vision error: {e} — vision unavailable this cycle "
                f"(frames stay on-box, no cloud fallback)", "ERROR")
        return None


async def phase3_visual_understanding(pool):
    """Run vision model on interior cameras and update environment_state."""
    for filename, room in INTERIOR_CAMERAS.items():
        frame_path = FRAME_DIR / filename
        if not frame_path.exists():
            continue
        age = time.time() - frame_path.stat().st_mtime
        if age > 900:
            continue

        # Run the blocking Ollama call in a thread so a slow/stalled vision
        # request can't freeze the brain's async loop.
        loop = asyncio.get_event_loop()
        description = await loop.run_in_executor(
            None, vision_describe, str(frame_path))
        if not description:
            continue

        _environment[room] = {
            "description": description,
            "ts": datetime.now(timezone.utc).isoformat(),
        }

        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO environment_state (camera, object_type, description, confidence)
                VALUES ($1, 'scene', $2, 0.8)
            """, f"interior_{room}", description)

        log(f"Scene [{room}]: {description[:80]}")


# ── Phase 5: Activity Classifier ────────────────────────────────────────────

def classify_activity(states, presence_data):
    """Score each activity state based on current signals."""
    signals = {}
    scores = {s: 0.0 for s in ACTIVITY_STATES}

    # Extract signals from HA states
    hour = datetime.now().hour
    signals["hour"] = hour

    # Light analysis
    office_lights_on = False
    bedroom_lights_on = False
    kitchen_lights_on = False
    living_lights_on = False
    all_lights_off = True

    for s in states:
        eid = s["entity_id"]
        if not eid.startswith("light."):
            continue
        if s["state"] != "on":
            continue
        all_lights_off = False
        name = eid.replace("light.", "")
        if "office" in name:
            office_lights_on = True
        elif "master" in name or "bedroom" in name or "tricia" in name:
            bedroom_lights_on = True
        elif "kitchen" in name:
            kitchen_lights_on = True
        elif "living" in name or "lr_" in name:
            living_lights_on = True

    signals["office_lights"] = office_lights_on
    signals["bedroom_lights"] = bedroom_lights_on
    signals["kitchen_lights"] = kitchen_lights_on
    signals["living_lights"] = living_lights_on
    signals["all_lights_off"] = all_lights_off

    # Media analysis
    media_playing = {}
    for s in states:
        if not s["entity_id"].startswith("media_player."):
            continue
        attrs = s.get("attributes", {})
        if s["state"] == "playing":
            app = attrs.get("app_name", "")
            if "office" in s["entity_id"]:
                media_playing["office"] = app
            elif "living" in s["entity_id"] or "master_bedroom_4" in s["entity_id"]:
                media_playing["living_room"] = app
            elif "bedroom" in s["entity_id"] or "mbath" in s["entity_id"]:
                media_playing["bedroom"] = app

    signals["media_playing"] = media_playing

    # AV receiver
    onkyo_on = False
    for s in states:
        if "tx_nr" in s["entity_id"].lower() or "onkyo" in s["entity_id"].lower():
            if s["state"] in ("on", "playing"):
                onkyo_on = True
    signals["onkyo_on"] = onkyo_on

    # Device tracker
    jordan_home = False
    for s in states:
        if s["entity_id"] == "device_tracker.jordan_s_iphone":
            jordan_home = s["state"] == "home"
    signals["jordan_home"] = jordan_home

    # Presence from telemetry
    signals["presence_room"] = presence_data.get("room", "unknown")

    # ── Scoring ──

    # deep_work: office lights + no media playing in office + work hours
    if office_lights_on and "office" not in media_playing:
        scores["deep_work"] += 0.5
    if 8 <= hour <= 18:
        scores["deep_work"] += 0.1
    if office_lights_on:
        scores["deep_work"] += 0.2

    # meeting: office + media playing (could be video call)
    if office_lights_on and "office" in media_playing:
        scores["meeting"] += 0.4
    if 9 <= hour <= 17:
        scores["meeting"] += 0.1

    # entertainment: living room + onkyo/TV + media playing
    if onkyo_on:
        scores["entertainment"] += 0.4
    if "living_room" in media_playing:
        scores["entertainment"] += 0.4
    if living_lights_on and not office_lights_on:
        scores["entertainment"] += 0.1

    # cooking: kitchen lights on + presence in kitchen
    if kitchen_lights_on and not office_lights_on and not living_lights_on:
        scores["cooking"] += 0.5
    if kitchen_lights_on:
        scores["cooking"] += 0.2

    # sleeping: bedroom lights maybe dim or off + late/early hour + no activity
    if (hour >= 22 or hour <= 6) and not office_lights_on and not living_lights_on:
        scores["sleeping"] += 0.5
    if all_lights_off and (hour >= 23 or hour <= 5):
        scores["sleeping"] += 0.4
    if bedroom_lights_on and hour >= 21:
        scores["sleeping"] += 0.1

    # away: not home
    if not jordan_home:
        scores["away"] = 0.95

    # relaxing: lights on but no focused work signals
    if living_lights_on and not onkyo_on and "living_room" not in media_playing:
        scores["relaxing"] += 0.3
    if bedroom_lights_on and hour >= 19 and not (hour >= 23 or hour <= 5):
        scores["relaxing"] += 0.3

    # break: office lights on but unfocused (short duration would be ideal but we don't track that yet)
    if office_lights_on and "office" in media_playing:
        scores["break"] += 0.2

    # Pick highest
    best_state = max(scores, key=scores.get)
    best_score = scores[best_state]

    # Normalize confidence
    confidence = min(best_score, 0.95)

    return best_state, confidence, signals


async def phase5_activity_classifier(pool, states):
    """Classify current activity and write to telemetry."""
    global _current_activity

    # Get latest presence from engine
    presence_data = {}
    try:
        req = urllib.request.Request("http://127.0.0.1:37465/occupancy")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        if data.get("occupancy", {}).get("jordan"):
            presence_data = data["occupancy"]["jordan"]
    except Exception:
        pass

    global _last_activity_write
    state, confidence, signals = classify_activity(states, presence_data)

    prev_state = _current_activity.get("state")
    changed = state != prev_state
    now = datetime.now(timezone.utc)
    # Write on a state change OR when the heartbeat interval has elapsed. The heartbeat
    # keeps telemetry.activity fresh even during long stable/stuck stretches, so the
    # freshness monitor can tell "classifier alive, state steady" from "classifier dead".
    due = _last_activity_write is None or (now - _last_activity_write).total_seconds() >= ACTIVITY_HEARTBEAT_SEC
    if changed:
        _current_activity = {"state": state, "confidence": confidence,
                             "since": now.isoformat(), "signals": signals}
        log(f"Activity: {state} (confidence={confidence:.2f})")
    else:
        _current_activity["confidence"] = confidence
        _current_activity["signals"] = signals
    if changed or due:
        _last_activity_write = now
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO telemetry.activity (ts, person, state, confidence, signals, metadata)
                VALUES (now(), 'jordan', $1, $2, $3, $4)
            """, state, confidence, json.dumps(signals, default=str),
                json.dumps({"heartbeat": not changed}, default=str))


# ── Phase 6: Environmental Awareness ────────────────────────────────────────

async def phase6_environmental_awareness(pool, states):
    """Generate actionable environmental observations."""
    suggestions = []
    hour = datetime.now().hour

    # Temperature check
    outdoor_temp = None
    for s in states:
        attrs = s.get("attributes", {})
        if attrs.get("device_class") == "temperature" and s["state"] != "unavailable":
            try:
                temp = float(s["state"])
                if temp < 130:  # filter out bogus readings
                    outdoor_temp = temp
            except (ValueError, TypeError):
                pass

    if outdoor_temp and outdoor_temp > 95 and hour >= 10 and hour <= 18:
        # Occupied means a person was DETECTED (motion/occupancy/presence sensor) —
        # not merely that something patio-named is switched on. The old check matched
        # light.patio_* being on, so every hot afternoon with patio lights on produced
        # a "you're outdoors in the heat" nag about... the lights being on.
        patio_occupied = any(
            s["entity_id"].startswith("binary_sensor.")
            and "patio" in s["entity_id"].lower()
            and any(k in s["entity_id"].lower() for k in ("motion", "occupancy", "presence"))
            and s["state"] == "on"
            for s in states)
        if patio_occupied:
            suggestions.append(("patio_heat", f"It's {outdoor_temp:.0f}°F outside and the patio is occupied — very hot to be outdoors"))

    # Late night with office lights still on
    if hour >= 23 or hour <= 1:
        office_on = any(s["state"] == "on" and "office" in s["entity_id"]
                       for s in states if s["entity_id"].startswith("light."))
        if office_on:
            suggestions.append(("office_late", "Past 11pm with office lights still on — consider winding down"))

    # All lights off but someone home and it's not sleep hours
    all_off = not any(s["state"] == "on" for s in states if s["entity_id"].startswith("light."))
    jordan_home = any(s["state"] == "home" for s in states
                     if s["entity_id"] == "device_tracker.jordan_s_iphone")
    if all_off and jordan_home and 7 <= hour <= 21:
        suggestions.append(("lights_off_home", "All lights are off but you're home — everything okay?"))

    # Outdoor light level + indoor activity (sun glare)
    illuminance = None
    for s in states:
        if s.get("attributes", {}).get("device_class") == "illuminance":
            try:
                illuminance = float(s["state"])
            except (ValueError, TypeError):
                pass

    if illuminance and illuminance > 2000 and hour >= 14 and hour <= 17:
        office_on = any(s["state"] == "on" and "office" in s["entity_id"]
                       for s in states if s["entity_id"].startswith("light."))
        if office_on:
            suggestions.append(("sun_glare", f"Bright afternoon sun ({illuminance:.0f} lux) — west-facing windows may cause glare"))

    # Write suggestions — at most one per key per 2h. This loop runs every 2 minutes;
    # without the cooldown a persistent condition wrote the same observation ~30x/hour
    # (patio-heat peaked at 12,538 rows), which then leaked into every generated
    # article/journal that samples shared_observations for local color.
    if suggestions:
        async with pool.acquire() as conn:
            for key, suggestion in suggestions:
                inserted = await conn.execute("""
                    INSERT INTO shared_observations (observer, category, subject, observation, severity)
                    SELECT 'jarvis_brain', 'environmental', $1, $2, 'info'
                    WHERE NOT EXISTS (
                        SELECT 1 FROM shared_observations
                        WHERE observer = 'jarvis_brain' AND subject = $1
                          AND observed_at > now() - interval '2 hours')
                """, key, suggestion)
                if inserted != "INSERT 0 0":
                    log(f"Suggestion: {suggestion}")


# ── Main Loop ───────────────────────────────────────────────────────────────

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)
    return _pool


async def brain_loop():
    """Main JARVIS brain loop — runs all three phases."""
    await asyncio.sleep(5)
    log("JARVIS Brain loop started")

    pool = await get_pool()

    while not _shutdown:
        try:
            states = ha_get_states()
            if not states:
                log("No HA states available, skipping cycle", "WARN")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # Phase 5: Activity (fast, every cycle)
            await phase5_activity_classifier(pool, states)

            # Phase 6: Environmental (fast, every cycle)
            await phase6_environmental_awareness(pool, states)

            # Phase 3: Visual (slow — only every 5 minutes due to LLM cost)
            if int(time.time()) % 300 < POLL_INTERVAL:
                await phase3_visual_understanding(pool)

        except Exception as e:
            log(f"Brain loop error: {e}", "ERROR")

        await asyncio.sleep(POLL_INTERVAL)


# ── HTTP API ────────────────────────────────────────────────────────────────

async def handle_health(request):
    return web.json_response({
        "ok": True,
        "service": "nova_jarvis_brain",
        "version": VERSION,
        "uptime_s": int(time.time() - _start_time),
    })


async def handle_activity(request):
    return web.json_response({
        "ok": True,
        "activity": _current_activity,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


async def handle_environment(request):
    return web.json_response({
        "ok": True,
        "rooms": _environment,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


# ── Lifecycle ───────────────────────────────────────────────────────────────

def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log("Shutdown signal received")


async def main():
    global _shutdown
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log(f"Nova JARVIS Brain v{VERSION} starting...")
    log(f"Phases: 3 (Visual), 5 (Activity), 6 (Environmental)")
    log(f"Poll interval: {POLL_INTERVAL}s, HTTP API on port {HTTP_PORT}")

    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/activity", handle_activity)
    app.router.add_get("/environment", handle_environment)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    log(f"HTTP API listening on 0.0.0.0:{HTTP_PORT}")

    tasks = [asyncio.create_task(brain_loop())]

    while not _shutdown:
        await asyncio.sleep(1)

    log("Shutting down...")
    for task in tasks:
        task.cancel()
    await runner.cleanup()
    if _pool:
        await _pool.close()
    log("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
