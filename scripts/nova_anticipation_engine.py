#!/usr/bin/env python3
"""
nova_anticipation_engine.py — Nova's proactive intelligence daemon.

Runs every 60 seconds. Observes state across presence, calendar, patterns,
infrastructure, and environment. Generates contextual suggestions that are
delivered only when Jordan is in an appropriate state to receive them.

This is the brain that makes Nova feel like Jarvis — anticipating needs,
not just reacting to commands.

Written by Jordan Koch.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_DIR = Path.home() / ".openclaw/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [anticipation] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("anticipation")


def log(msg):
    logger.info(msg)

# ── Configuration ────────────────────────────────────────────────────────────

PRESENCE_URL = "http://127.0.0.1:37465/occupancy"
CALENDAR_URL = "http://127.0.0.1:37400/api/oneonone/meetings"
MEMORY_URL = "http://memory-server.digitalnoise.net:18790"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
PG_DSN = "postgresql://kochj@pg-primary.digitalnoise.net:5432/nova_ops"

EVAL_INTERVAL = 60  # seconds between evaluation cycles
DELIVERY_COOLDOWN = 1800  # 30 min default between proactive messages on same topic
# Some topics must NOT re-nag at the default cadence. A stretch-break nudge every
# 30 min is worse than useless — it fired 5x one morning off overnight phone-parking.
TOPIC_COOLDOWN_OVERRIDES = {
    "desk_duration": 3 * 3600,   # nudge to stretch at most once every 3 hours
    # Autonomy self-awareness is context, not a status feed — mention a self-heal or a
    # newly-earned class at most twice a day so it never turns into a running commentary.
    "autonomy_selfheal": 12 * 3600,
    "autonomy_earned": 12 * 3600,
}
MAX_DAILY_PROACTIVE = 12  # don't overwhelm

STATE_FILE = Path.home() / ".openclaw/workspace/state/nova_anticipation_state.json"

# ── Activity States ──────────────────────────────────────────────────────────

ACTIVITY_STATES = {
    "deep_work": {"can_interrupt": False, "deliver_priority": 1},
    "meeting": {"can_interrupt": False, "deliver_priority": 1},
    "break": {"can_interrupt": True, "deliver_priority": 5},
    "available": {"can_interrupt": True, "deliver_priority": 5},
    "planning": {"can_interrupt": True, "deliver_priority": 3},
    "winding_down": {"can_interrupt": True, "deliver_priority": 2},
    "away": {"can_interrupt": False, "deliver_priority": 0},
    "sleeping": {"can_interrupt": False, "deliver_priority": 0},
}


# ── State Management ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "delivered_today": [],
        "hold_queue": [],
        "last_delivery": 0,
        "daily_count": 0,
        "date": date.today().isoformat(),
        "topic_cooldowns": {},
    }


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Reset daily count if new day
    if state.get("date") != date.today().isoformat():
        state["daily_count"] = 0
        state["delivered_today"] = []
        state["date"] = date.today().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ── Signal Collection ────────────────────────────────────────────────────────

def get_presence() -> dict:
    """Get current room occupancy from presence engine."""
    try:
        resp = urllib.request.urlopen(PRESENCE_URL, timeout=5)
        data = json.loads(resp.read())
        jordan = data.get("occupancy", {}).get("jordan", {})
        return {
            "room": jordan.get("room", "unknown"),
            "confidence": jordan.get("confidence", 0),
            "home": jordan.get("home", False),
        }
    except Exception:
        return {"room": "unknown", "confidence": 0, "home": True}


def get_activity_state() -> str:
    """Determine Jordan's current activity state."""
    now = datetime.now()
    hour = now.hour

    # Check macOS Focus mode
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'do shell script "plutil -extract dnd_prefs.userPref.enabled raw '
             '~/Library/DoNotDisturb/DB/Assertions/v1/com.apple.donotdisturb.state.json 2>/dev/null || echo false"'],
            capture_output=True, text=True, timeout=5
        )
        if "true" in r.stdout.lower():
            return "deep_work"
    except Exception:
        pass

    # Check for active meetings via calendar
    try:
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", "2", CALENDAR_URL + "?limit=5"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0 and r.stdout.strip():
            meetings = json.loads(r.stdout)
            if isinstance(meetings, dict):
                meetings = meetings.get("meetings", [])
            today_str = date.today().isoformat()
            for m in meetings:
                if today_str in str(m.get("date", "")):
                    start = m.get("start_time", "")
                    end = m.get("end_time", "")
                    if start and end:
                        try:
                            s = datetime.fromisoformat(start)
                            e = datetime.fromisoformat(end)
                            if s <= now <= e:
                                return "meeting"
                        except Exception:
                            pass
    except Exception:
        pass

    # Check if coding (MLXCode running)
    try:
        urllib.request.urlopen("http://127.0.0.1:37422/api/status", timeout=2)
        return "deep_work"
    except Exception:
        pass

    # Time-based fallbacks
    if hour in range(0, 7):
        return "sleeping"
    if hour in range(21, 24):
        return "winding_down"

    # Check presence for break detection
    presence = get_presence()
    if presence["room"] in ("kitchen", "patio", "living_room") and hour in range(9, 18):
        return "break"

    if hour in list(range(9, 12)) + list(range(14, 17)):
        return "available"

    return "available"


def get_upcoming_meetings(minutes_ahead: int = 30) -> list:
    """Get meetings starting in the next N minutes via ICS feed (Outlook/O365)."""
    try:
        from nova_calendar import get_todays_events
        now = datetime.now()
        cutoff = now + timedelta(minutes=minutes_ahead)
        meetings = []
        for event in get_todays_events():
            start_str = event.get("start", "")
            title = event.get("title", event.get("summary", ""))
            if not start_str or not title:
                continue
            try:
                start = datetime.fromisoformat(start_str.replace("Z", "+00:00")).replace(tzinfo=None)
                if now <= start <= cutoff:
                    meetings.append({"title": title, "start": start_str})
            except (ValueError, TypeError):
                continue
        return meetings
    except Exception:
        pass
    return []


def get_presence_duration() -> float:
    """How long has Jordan been in the current room (minutes)."""
    try:
        import psycopg2
        conn = psycopg2.connect(PG_DSN)
        cur = conn.cursor()
        # Start of the CURRENT unbroken run in the current room. "Unbroken" resets on
        # a room change OR any gap > 15 min in the presence stream (you left the desk,
        # or the BLE tag went quiet overnight). The old query floored the start at
        # now()-8h, so a phone parked on the desk overnight always read ~480 min
        # ("479 straight") whether or not anyone was actually sitting there.
        cur.execute("""
            WITH cur AS (
                SELECT room FROM telemetry.presence
                WHERE person = 'jordan' ORDER BY ts DESC LIMIT 1
            ),
            seq AS (
                SELECT ts, room,
                       ts - lag(ts) OVER (ORDER BY ts) AS gap
                FROM telemetry.presence
                WHERE person = 'jordan' AND method = 'ble_rssi'
                  AND ts > now() - interval '18 hours'
            )
            SELECT MIN(ts) FROM seq
            WHERE room = (SELECT room FROM cur)
              AND ts > COALESCE(
                  (SELECT MAX(ts) FROM seq
                   WHERE room <> (SELECT room FROM cur) OR gap > interval '15 minutes'),
                  now() - interval '18 hours'
              )
        """)
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return (datetime.now(row[0].tzinfo) - row[0]).total_seconds() / 60
    except Exception:
        pass
    return 0


def get_disk_usage() -> dict:
    """Check disk usage on key volumes."""
    results = {}
    try:
        r = subprocess.run(["df", "-P", "/", "/Volumes/Data"],
                          capture_output=True, text=True, timeout=5)
        for line in r.stdout.strip().split("\n")[1:]:
            parts = line.split()
            if len(parts) >= 5:
                pct = int(parts[4].rstrip("%"))
                mount = parts[5]
                results[mount] = pct
    except Exception:
        pass
    return results


# ── Pattern Checks ───────────────────────────────────────────────────────────

def check_meeting_prep() -> list:
    """Surface context for upcoming meetings."""
    observations = []
    meetings = get_upcoming_meetings(minutes_ahead=15)

    for m in meetings:
        title = m["title"]
        # A meeting reminder is worthless once the meeting has started — expire it then.
        try:
            expires_at = datetime.fromisoformat(m["start"].replace("Z", "+00:00")).replace(tzinfo=None).timestamp()
        except (ValueError, TypeError, KeyError):
            expires_at = time.time() + 900
        # Search memory for previous discussions on this topic
        try:
            payload = json.dumps({"query": title, "limit": 3}).encode()
            req = urllib.request.Request(
                f"{MEMORY_URL}/recall", data=payload,
                headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=5)
            results = json.loads(resp.read())
            memories = results.get("results", [])
            if memories:
                context = memories[0].get("text", "")[:200]
                observations.append({
                    "type": "meeting_prep",
                    "priority": 2,
                    "message": f"Meeting '{title}' in 15 min. Last relevant context: {context}",
                    "topic": f"meeting:{title}",
                    "expires_at": expires_at,
                })
        except Exception:
            observations.append({
                "type": "meeting_prep",
                "priority": 3,
                "message": f"Meeting '{title}' starting in 15 minutes.",
                "topic": f"meeting:{title}",
                "expires_at": expires_at,
            })

    return observations


IDLE_RESET_SECS = 900  # 15 min away from the keyboard breaks the "continuous desk" run


def _mac_idle_seconds():
    """Seconds since the last keyboard/mouse input on this workstation (the engine
    runs on .6, where Jordan actually sits). None if unavailable. Far more honest than
    BLE presence, which tracks the PHONE — it reported 'office since 4:21am' while
    Jordan was asleep in the bedroom, because his phone was parked on the desk."""
    try:
        import re
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        if m:
            return int(m.group(1)) / 1_000_000_000.0  # nanoseconds -> seconds
    except Exception:
        pass
    return None


def get_desk_active_minutes(state) -> float:
    """Minutes Jordan has been CONTINUOUSLY active at the workstation, from real input
    activity. A gap of IDLE_RESET_SECS+ (he stepped away) resets the run; brief pauses
    don't. This is what 'at your desk' should have meant all along — a phone parked on
    the desk can't fake it, and it resets the moment he actually gets up."""
    idle = _mac_idle_seconds()
    now = time.time()
    if idle is None or idle >= IDLE_RESET_SECS:
        state["desk_active_since"] = None          # away (or no signal) -> run broken
        return 0.0
    since = state.get("desk_active_since")
    if not since:
        since = now - idle                         # just returned -> start at last activity
        state["desk_active_since"] = since
    return max(0.0, (now - since) / 60.0)


def check_desk_duration(state) -> list:
    """Stretch-break nudge after extended CONTINUOUS active desk time — measured from
    real keyboard/mouse input, not BLE phone location. Re-nag is throttled to 3h by
    TOPIC_COOLDOWN_OVERRIDES, so this only needs the 'is he genuinely still here' test."""
    observations = []
    duration = get_desk_active_minutes(state)
    if duration >= 240:  # 4 hours of actually being at the keyboard
        observations.append({
            "type": "health",
            "priority": 3,
            "message": f"You've been at your desk for {int(duration)} minutes straight. Stretch break?",
            "topic": "desk_duration",
        })
    return observations


def check_infrastructure() -> list:
    """Predict infrastructure issues before they become critical."""
    observations = []

    # Disk usage trending
    disks = get_disk_usage()
    for mount, pct in disks.items():
        if pct >= 85:
            observations.append({
                "type": "infrastructure",
                "priority": 2 if pct >= 90 else 3,
                "message": f"Disk {mount} at {pct}% — getting tight.",
                "topic": f"disk:{mount}",
            })

    return observations


def check_environment() -> list:
    """Check for environmental anomalies (lights, doors, weather)."""
    observations = []

    # Lights on in empty rooms
    try:
        import psycopg2
        conn = psycopg2.connect(PG_DSN)
        cur = conn.cursor()

        # Get rooms with lights on but no presence in 30+ min
        cur.execute("""
            WITH lit_rooms AS (
                SELECT DISTINCT room FROM telemetry.hue_lights
                WHERE state = 'on' AND ts > now() - interval '5 minutes'
            ),
            occupied_rooms AS (
                SELECT DISTINCT room FROM telemetry.presence
                WHERE ts > now() - interval '30 minutes'
                  AND confidence > 0.5
            )
            SELECT room FROM lit_rooms
            WHERE room NOT IN (SELECT room FROM occupied_rooms)
        """)
        empty_lit = [r[0] for r in cur.fetchall()]
        conn.close()

        if empty_lit:
            rooms_str = ", ".join(empty_lit)
            observations.append({
                "type": "environment",
                "priority": 4,
                "message": f"Lights still on in {rooms_str} with nobody there for 30+ minutes.",
                "topic": "lights_empty_room",
            })
    except Exception:
        pass

    return observations


def check_autonomy_state() -> list:
    """Let Nova's proactive reasoning be AWARE of her own autonomy state — she might
    mention having just healed something, or newly earning standing approval for an
    action-class. This is context, not a forced feed: it only fires on genuinely fresh
    (last ~2h) notable activity, at low priority, and surfaces ONLY the curated
    autonomy_status().line plus concise counts — never raw inner-state text. Fail-open.
    """
    observations = []
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import nova_autonomy_safety as autos
    except Exception:
        return observations

    try:
        import psycopg2
        conn = psycopg2.connect(PG_DSN)
        conn.autocommit = True
        cur = conn.cursor()
        status = autos.autonomy_status(cur)  # curated line + counts, own no connection
        # A fresh self-heal (autonomous actor restart) in the last couple of hours.
        cur.execute("""SELECT count(*), max(target) FROM autonomy_ledger
                       WHERE executed AND source = 'actor'
                         AND ts > now() - interval '2 hours'""")
        heal_row = cur.fetchone()
        # A class that graduated to standing approval in the last couple of hours.
        cur.execute("""SELECT action_class FROM autonomy_trust
                       WHERE granted AND granted_at > now() - interval '2 hours'
                       ORDER BY granted_at DESC LIMIT 1""")
        earned_row = cur.fetchone()
        conn.close()
    except Exception:
        return observations

    line = (status or {}).get("line", "").strip()
    heals = heal_row[0] if heal_row else 0

    if earned_row and earned_row[0]:
        msg = f"I just earned standing approval for `{earned_row[0]}`."
        if line:
            msg += f" {line}"
        observations.append({
            "type": "autonomy",
            "priority": 4,
            "message": msg,
            "topic": "autonomy_earned",
        })
    elif heals:
        target = (heal_row[1] or "a service") if heal_row else "a service"
        msg = f"Heads up: I self-healed {target} in the last couple of hours."
        if line:
            msg += f" {line}"
        observations.append({
            "type": "autonomy",
            "priority": 4,
            "message": msg,
            "topic": "autonomy_selfheal",
        })

    return observations


# ── Delivery Logic ───────────────────────────────────────────────────────────

def should_deliver(observation: dict, state: dict, activity: str) -> bool:
    """Decide if an observation should be delivered now."""
    # Check activity state permissions
    activity_config = ACTIVITY_STATES.get(activity, {"can_interrupt": False})
    if not activity_config["can_interrupt"]:
        return False

    # Check priority vs activity
    obs_priority = observation.get("priority", 5)
    if obs_priority > activity_config["deliver_priority"]:
        return False

    # Check daily cap
    if state.get("daily_count", 0) >= MAX_DAILY_PROACTIVE:
        return False

    # Check topic cooldown
    topic = observation.get("topic", "")
    cooldowns = state.get("topic_cooldowns", {})
    if topic in cooldowns:
        last_time = cooldowns[topic]
        cooldown = TOPIC_COOLDOWN_OVERRIDES.get(topic, DELIVERY_COOLDOWN)
        if time.time() - last_time < cooldown:
            return False

    return True


def deliver(observation: dict, state: dict):
    """Deliver a proactive observation to Jordan via Slack DM."""
    message = observation["message"]
    obs_type = observation.get("type", "general")

    # Format with type prefix
    prefix = {
        "meeting_prep": "Meeting heads-up",
        "health": "Gentle nudge",
        "infrastructure": "Infra note",
        "environment": "Around the house",
        "routine": "Pattern noticed",
        "autonomy": "Where I stand",
    }.get(obs_type, "FYI")

    formatted = f"*{prefix}:* {message}"

    try:
        nova_config.post_both(formatted, slack_channel=nova_config.JORDAN_DM)
        log(f"Delivered: [{obs_type}] {message[:80]}")
    except Exception as e:
        log(f"Delivery failed: {e}")
        return

    # Update state
    state["daily_count"] = state.get("daily_count", 0) + 1
    state["last_delivery"] = time.time()
    state["delivered_today"].append({
        "type": obs_type,
        "message": message[:100],
        "ts": datetime.now().isoformat(),
    })
    topic = observation.get("topic", "")
    if topic:
        state.setdefault("topic_cooldowns", {})[topic] = time.time()


def queue_for_later(observation: dict, state: dict):
    """Queue an observation for delivery when Jordan is available."""
    q = state.setdefault("hold_queue", [])
    # Don't pile up duplicates of the same topic — the engine runs every ~1 min, so a
    # meeting reminder in its 15-min window would otherwise queue ~15 copies that then
    # dribble out one per cooldown for hours. One queued item per topic is enough.
    topic = observation.get("topic", "")
    if topic and any(o.get("topic") == topic for o in q):
        return
    q.append({**observation, "queued_at": time.time()})
    # Cap queue size
    if len(q) > 20:
        state["hold_queue"] = q[-20:]


def flush_hold_queue(state: dict, activity: str):
    """Deliver queued items if Jordan is now available."""
    if not state.get("hold_queue"):
        return

    activity_config = ACTIVITY_STATES.get(activity, {})
    if not activity_config.get("can_interrupt"):
        return

    # Deliver up to 3 queued items per flush
    delivered = 0
    remaining = []
    for obs in state["hold_queue"]:
        if delivered >= 3:
            remaining.append(obs)
            continue
        # Skip stale items (> 4 hours old)
        if time.time() - obs.get("queued_at", 0) > 14400:
            continue
        # Drop time-sensitive items whose moment has passed (e.g. meeting already started)
        if obs.get("expires_at") and time.time() > obs["expires_at"]:
            continue
        if should_deliver(obs, state, activity):
            deliver(obs, state)
            delivered += 1
        else:
            remaining.append(obs)

    state["hold_queue"] = remaining
    if delivered:
        log(f"Flushed {delivered} queued items")


# ── Main Evaluation Loop ─────────────────────────────────────────────────────

def evaluate():
    """Single evaluation cycle — collect signals, check patterns, deliver."""
    state = load_state()
    activity = get_activity_state()

    log(f"Cycle: activity={activity}, daily_delivered={state.get('daily_count', 0)}, queue={len(state.get('hold_queue', []))}")

    # Collect all observations
    observations = []
    observations.extend(check_meeting_prep())
    observations.extend(check_desk_duration(state))
    observations.extend(check_infrastructure())
    observations.extend(check_environment())
    observations.extend(check_autonomy_state())

    # Process each observation
    for obs in observations:
        if should_deliver(obs, state, activity):
            deliver(obs, state)
        elif obs.get("priority", 5) <= 3:
            queue_for_later(obs, state)

    # Try to flush held items if we're now available
    if activity in ("available", "break"):
        flush_hold_queue(state, activity)

    save_state(state)


# ── Daemon Mode ──────────────────────────────────────────────────────────────

def main():
    log("Anticipation engine starting")
    log(f"  Eval interval: {EVAL_INTERVAL}s")
    log(f"  Max daily proactive: {MAX_DAILY_PROACTIVE}")
    log(f"  Delivery cooldown: {DELIVERY_COOLDOWN}s")

    while True:
        try:
            evaluate()
        except Exception as e:
            log(f"Evaluation error: {e}")
        time.sleep(EVAL_INTERVAL)


if __name__ == "__main__":
    main()
