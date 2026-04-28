#!/usr/bin/env python3
"""
nova_weather_homekit.py — Weather-aware HomeKit automation.

Fetches weather forecast for Burbank and triggers HomeKit scenes
based on configurable rules:
  - Hot day (>90F) → close blinds, suggest AC scene
  - Cold morning (<50F) → suggest heating scene
  - Rain forecast → alert about open windows/garage
  - UV index high → suggest shade scene

Posts actions taken (or suggested) to Slack.
Uses wttr.in for weather data (no API key needed).

Cron: every 2 hours (or at 7am/2pm for key decision points)
Written by Jordan Koch.
"""

import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

VECTOR_URL = nova_config.VECTOR_URL
NOW = datetime.now()
HOUR = NOW.hour
TODAY = date.today().isoformat()
STATE_FILE = Path.home() / ".openclaw/workspace/state/nova_weather_homekit_state.json"
HOMEKIT_URL = "http://127.0.0.1:37432"

# ── Weather thresholds ───────────────────────────────────────────────────────

RULES = [
    {
        "name": "extreme_heat",
        "condition": lambda w: w.get("temp_f", 0) >= 95,
        "message": "Extreme heat ({temp_f}F) — consider closing blinds and running AC",
        "scene": None,  # Set to a scene name to auto-execute, None = suggest only
        "priority": "high",
        "hours": range(8, 20),  # Only during daytime
    },
    {
        "name": "hot_day",
        "condition": lambda w: 90 <= w.get("temp_f", 0) < 95,
        "message": "Hot day ahead ({temp_f}F forecast high)",
        "scene": None,
        "priority": "medium",
        "hours": range(8, 14),
    },
    {
        "name": "cold_morning",
        "condition": lambda w: w.get("temp_f", 0) <= 50,
        "message": "Cold morning ({temp_f}F) — heating may be needed",
        "scene": None,
        "priority": "medium",
        "hours": range(5, 10),
    },
    {
        "name": "rain_alert",
        "condition": lambda w: w.get("rain_chance", 0) >= 60 or "rain" in w.get("description", "").lower(),
        "message": "Rain likely ({rain_chance}% chance) — check windows and garage",
        "scene": None,
        "priority": "high",
        "hours": range(0, 24),
    },
    {
        "name": "wind_alert",
        "condition": lambda w: w.get("wind_mph", 0) >= 30,
        "message": "High winds ({wind_mph} mph) — secure outdoor items",
        "scene": None,
        "priority": "medium",
        "hours": range(0, 24),
    },
    {
        "name": "pleasant_weather",
        "condition": lambda w: 65 <= w.get("temp_f", 0) <= 78 and w.get("rain_chance", 0) < 20,
        "message": "Beautiful weather ({temp_f}F, {description}) — open the windows!",
        "scene": None,
        "priority": "low",
        "hours": range(8, 18),
    },
]


def log(msg):
    print(f"[nova_weather_homekit {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text, channel=None):
    nova_config.post_both(text, slack_channel=channel or nova_config.SLACK_NOTIFY)


def vector_remember(text, metadata=None):
    try:
        payload = json.dumps({
            "text": text, "source": "weather_homekit", "metadata": metadata or {}
        }).encode()
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


def load_state():
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            # Reset daily
            if state.get("date") != TODAY:
                return {"date": TODAY, "triggered": {}, "scenes_run": []}
            return state
        except Exception:
            pass
    return {"date": TODAY, "triggered": {}, "scenes_run": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Weather data ─────────────────────────────────────────────────────────────

def get_weather():
    """Fetch current weather and forecast from wttr.in."""
    try:
        req = urllib.request.Request(
            "https://wttr.in/burbank,ca?format=j1",
            headers={"User-Agent": "curl/7.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())

        current = data.get("current_condition", [{}])[0]
        forecast = data.get("weather", [{}])[0]
        hourly = forecast.get("hourly", [])

        # Current conditions
        temp_c = int(current.get("temp_C", 0))
        temp_f = int(current.get("temp_F", 0))
        feels_f = int(current.get("FeelsLikeF", temp_f))
        humidity = int(current.get("humidity", 0))
        wind_mph = int(current.get("windspeedMiles", 0))
        description = current.get("weatherDesc", [{}])[0].get("value", "Unknown")
        uv = int(current.get("uvIndex", 0))

        # Forecast high/low
        max_f = int(forecast.get("maxtempF", 0))
        min_f = int(forecast.get("mintempF", 0))

        # Rain chance — max across remaining hours today
        rain_chance = 0
        for h in hourly:
            hour_num = int(h.get("time", "0").rstrip("0") or "0") // 100
            if hour_num >= HOUR:
                chance = int(h.get("chanceofrain", 0))
                rain_chance = max(rain_chance, chance)

        return {
            "temp_f": temp_f,
            "temp_c": temp_c,
            "feels_f": feels_f,
            "humidity": humidity,
            "wind_mph": wind_mph,
            "description": description,
            "uv": uv,
            "max_f": max_f,
            "min_f": min_f,
            "rain_chance": rain_chance,
        }
    except Exception as e:
        log(f"Weather fetch error: {e}")
        return None


# ── HomeKit integration ──────────────────────────────────────────────────────

def execute_scene(scene_name):
    """Execute a HomeKit scene via HomekitControl API."""
    try:
        data = json.dumps({"name": scene_name}).encode()
        req = urllib.request.Request(
            f"{HOMEKIT_URL}/api/scenes/execute", data=data,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            return result.get("success", False)
    except Exception as e:
        log(f"Scene execution error: {e}")
        # Fallback to Shortcuts CLI script
        try:
            result = subprocess.run(
                [str(Path.home() / ".openclaw/scripts/nova_homekit_scene.sh"), scene_name],
                capture_output=True, text=True, timeout=15
            )
            return result.returncode == 0
        except Exception:
            return False


def check_open_contacts():
    """Check HomeKit for open doors/windows (relevant for rain alerts)."""
    try:
        with urllib.request.urlopen(f"{HOMEKIT_URL}/api/accessories", timeout=5) as r:
            accessories = json.loads(r.read())
            if isinstance(accessories, dict):
                accessories = accessories.get("accessories", [])

        open_items = []
        for acc in accessories:
            for svc in acc.get("services", []):
                for char in svc.get("characteristics", []):
                    if "contact" in char.get("type", "").lower() and char.get("value") == 1:
                        open_items.append(acc.get("name", "Unknown"))
        return open_items
    except Exception:
        return []


# ── Main logic ───────────────────────────────────────────────────────────────

def evaluate_rules(weather):
    """Evaluate all weather rules and return triggered ones."""
    triggered = []
    for rule in RULES:
        if HOUR not in rule["hours"]:
            continue
        try:
            if rule["condition"](weather):
                msg = rule["message"].format(**weather)
                triggered.append({
                    "name": rule["name"],
                    "message": msg,
                    "scene": rule["scene"],
                    "priority": rule["priority"],
                })
        except Exception:
            continue
    return triggered


def main():
    log("Checking weather conditions...")
    weather = get_weather()
    if not weather:
        log("Could not fetch weather — skipping.")
        return

    log(f"Current: {weather['temp_f']}F, {weather['description']}, "
        f"rain {weather['rain_chance']}%, wind {weather['wind_mph']}mph")

    state = load_state()
    triggered_today = state.get("triggered", {})
    actions = evaluate_rules(weather)

    new_actions = []
    for action in actions:
        name = action["name"]
        # Only fire each rule once per day (or once per 6 hours for high priority)
        last_fired = triggered_today.get(name, 0)
        cooldown = 21600 if action["priority"] == "high" else 86400  # 6h or 24h
        import time
        if (time.time() - last_fired) < cooldown:
            continue

        new_actions.append(action)
        triggered_today[name] = time.time()

        # Execute scene if configured
        if action["scene"]:
            success = execute_scene(action["scene"])
            action["scene_result"] = "executed" if success else "failed"
            state.setdefault("scenes_run", []).append({
                "scene": action["scene"], "rule": name,
                "time": NOW.strftime("%H:%M"), "success": success
            })

    if new_actions:
        # Check for open contacts if rain is in the alerts
        rain_alert = any(a["name"] in ("rain_alert",) for a in new_actions)
        open_items = check_open_contacts() if rain_alert else []

        lines = [f"*Weather Alert — {NOW.strftime('%I:%M %p')}*",
                 f"_Currently {weather['temp_f']}F ({weather['description']}), "
                 f"high {weather['max_f']}F, rain {weather['rain_chance']}%_",
                 ""]

        for a in new_actions:
            icon = {"high": "!!!", "medium": "!", "low": ""}.get(a["priority"], "")
            line = f"  {icon} {a['message']}"
            if a.get("scene_result"):
                line += f" — scene '{a['scene']}' {a['scene_result']}"
            lines.append(line)

        if open_items:
            lines.append("")
            lines.append(f"*Currently open:* {', '.join(open_items)}")
            lines.append("_Consider closing these before the rain._")

        msg = "\n".join(lines)
        slack_post(msg)

        # Vector memory
        summary = f"Weather automation {TODAY} {NOW.strftime('%H:%M')}: " + \
                  "; ".join(a["message"] for a in new_actions)
        vector_remember(summary, {"date": TODAY, "type": "weather_automation"})
        log(f"Posted {len(new_actions)} weather action(s)")
    else:
        log("No new weather actions triggered.")

    state["triggered"] = triggered_today
    save_state(state)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Weather-HomeKit Bridge")
    parser.add_argument("--status", action="store_true", help="Show current weather + active rules")
    parser.add_argument("--force", action="store_true", help="Ignore cooldowns, fire all matching rules")
    args = parser.parse_args()

    if args.status:
        weather = get_weather()
        if weather:
            print(f"Weather: {weather['temp_f']}F ({weather['description']})")
            print(f"Feels like: {weather['feels_f']}F")
            print(f"High/Low: {weather['max_f']}F / {weather['min_f']}F")
            print(f"Rain: {weather['rain_chance']}%  Wind: {weather['wind_mph']} mph  UV: {weather['uv']}")
            print(f"\nTriggered rules:")
            for a in evaluate_rules(weather):
                print(f"  [{a['priority']}] {a['message']}")
        else:
            print("Could not fetch weather.")
    elif args.force:
        # Reset state to force all rules
        STATE_FILE.unlink(missing_ok=True)
        main()
    else:
        main()
