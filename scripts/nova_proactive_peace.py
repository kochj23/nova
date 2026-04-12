#!/usr/bin/env python3
"""
nova_proactive_peace.py — Focus-aware noise management.

Nova's own wish: "not just telling you about problems, but quietly
preventing them — knowing when you're deep in flow and holding non-urgent
noise, or sensing when rest is needed more than productivity."

This script:
  1. Detects Jordan's current state:
     - macOS Focus mode (Do Not Disturb, Work, Sleep, etc.)
     - Time of day (sleep hours, work hours, evening)
     - Activity level (lots of commits = deep flow, no activity = rest)
     - App usage (MLXCode running = coding, OneOnOne = meetings)
  2. Manages a "hold queue" for non-urgent notifications
  3. Releases held notifications when Jordan transitions to an available state
  4. Posts a "quiet digest" instead of individual alerts when in focus mode
  5. Detects burnout signals and gently suggests rest

Other Nova scripts can check this script's state file before posting
to decide whether to alert now or queue for later.

Cron: every 10 min
Written by Jordan Koch.
"""

import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_CHAN
SLACK_API = nova_config.SLACK_API
JORDAN_DM = nova_config.JORDAN_DM
NOW = datetime.now()
HOUR = NOW.hour
TODAY = date.today().isoformat()
STATE_FILE = Path("/tmp/nova_peace_state.json")
HOLD_QUEUE = Path("/tmp/nova_peace_hold_queue.json")

# ── State definitions ────────────────────────────────────────────────────────

# Sleep hours: midnight - 7am
SLEEP_HOURS = range(0, 7)
# Core focus hours (likely deep work): 9am - 12pm, 2pm - 5pm
FOCUS_HOURS = list(range(9, 12)) + list(range(14, 17))
# Wind-down hours: 9pm - midnight
WIND_DOWN_HOURS = range(21, 24)


def log(msg):
    print(f"[nova_peace {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text, channel=None):
    data = json.dumps({
        "channel": channel or JORDAN_DM, "text": text, "mrkdwn": True
    }).encode()
    req = urllib.request.Request(
        f"{SLACK_API}/chat.postMessage", data=data,
        headers={"Authorization": "Bearer " + SLACK_TOKEN,
                 "Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        log(f"Slack error: {e}")


# ── State detection ──────────────────────────────────────────────────────────

def get_focus_mode():
    """Check macOS Focus/DND state via AppleScript."""
    try:
        # Check Focus mode (macOS 12+)
        result = subprocess.run(
            ["osascript", "-e",
             'do shell script "defaults read com.apple.controlcenter NSStatusItem\\ Visible\\ FocusModes 2>/dev/null || echo 0"'],
            capture_output=True, text=True, timeout=5
        )
        # Also check DND specifically
        dnd_result = subprocess.run(
            ["osascript", "-e",
             'do shell script "plutil -extract dnd_prefs.userPref.enabled raw '
             '~/Library/DoNotDisturb/DB/Assertions/v1/com.apple.donotdisturb.state.json 2>/dev/null || echo false"'],
            capture_output=True, text=True, timeout=5
        )
        if "true" in dnd_result.stdout.lower():
            return "dnd"

        # Check for specific Focus modes via the assertion store
        focus_result = subprocess.run(
            ["osascript", "-e",
             'do shell script "ls ~/Library/DoNotDisturb/DB/Assertions/v1/ 2>/dev/null"'],
            capture_output=True, text=True, timeout=5
        )
        output = focus_result.stdout.lower()
        if "sleep" in output:
            return "sleep"
        if "work" in output:
            return "work"
        if "personal" in output:
            return "personal"

    except Exception:
        pass
    return "none"


def get_screen_state():
    """Check if screen is locked/asleep."""
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get running of screen saver preferences'],
            capture_output=True, text=True, timeout=5
        )
        if "true" in result.stdout.lower():
            return "locked"
    except Exception:
        pass
    return "active"


def get_activity_level():
    """Estimate activity level from running apps and recent commits."""
    coding = False
    in_meeting = False

    # Check if coding apps are running
    for port in [37422]:  # MLXCode
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2)
            coding = True
        except Exception:
            pass

    # Check OneOnOne for active meetings
    try:
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", "1",
             "http://127.0.0.1:37421/api/oneonone/meetings?limit=1"],
            capture_output=True, text=True, timeout=3
        )
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            meetings = data if isinstance(data, list) else data.get("meetings", [])
            for m in meetings:
                # Check if meeting is "now"
                dt_str = str(m.get("date", ""))
                if TODAY in dt_str:
                    in_meeting = True
    except Exception:
        pass

    if in_meeting:
        return "meeting"
    elif coding:
        return "coding"
    elif HOUR in SLEEP_HOURS:
        return "sleeping"
    elif HOUR in WIND_DOWN_HOURS:
        return "winding_down"
    elif HOUR in FOCUS_HOURS:
        return "focus_likely"
    else:
        return "available"


def detect_burnout_signals():
    """Look for signs Jordan should take a break."""
    signals = []

    # Late night coding
    if HOUR >= 23:
        # Check if apps are still running
        for port, name in [(37422, "MLXCode"), (37423, "NMAPScanner")]:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2)
                signals.append(f"still_coding_at_{HOUR}")
            except Exception:
                pass

    # Weekend work
    if NOW.weekday() >= 5 and HOUR in FOCUS_HOURS:
        try:
            r = subprocess.run(
                ["gh", "api", "/users/kochj23/events?per_page=5"],
                capture_output=True, text=True, timeout=8
            )
            if r.returncode == 0:
                events = json.loads(r.stdout)
                weekend_commits = [e for e in events
                                   if TODAY in e.get("created_at", "")
                                   and e["type"] == "PushEvent"]
                if weekend_commits:
                    signals.append("weekend_commits")
        except Exception:
            pass

    return signals


# ── Hold queue management ────────────────────────────────────────────────────

def load_queue():
    if HOLD_QUEUE.exists():
        try:
            return json.loads(HOLD_QUEUE.read_text())
        except Exception:
            pass
    return {"messages": []}


def save_queue(queue):
    HOLD_QUEUE.write_text(json.dumps(queue, indent=2))


def queue_message(text, source, priority="low"):
    """Add a message to the hold queue (called by other scripts)."""
    queue = load_queue()
    queue["messages"].append({
        "text": text,
        "source": source,
        "priority": priority,
        "queued_at": NOW.isoformat(),
    })
    save_queue(queue)


def release_queue():
    """Release all held messages as a digest."""
    queue = load_queue()
    messages = queue.get("messages", [])
    if not messages:
        return

    # Group by priority
    high = [m for m in messages if m["priority"] == "high"]
    low = [m for m in messages if m["priority"] != "high"]

    lines = [f"*Held notifications — {len(messages)} while you were away:*"]

    if high:
        lines.append("\n*Priority:*")
        for m in high:
            lines.append(f"  {m['text'][:120]}")

    if low:
        lines.append(f"\n*Other ({len(low)}):*")
        for m in low[:5]:
            lines.append(f"  _{m['text'][:100]}_")
        if len(low) > 5:
            lines.append(f"  _+{len(low) - 5} more_")

    slack_post("\n".join(lines))
    save_queue({"messages": []})
    log(f"Released {len(messages)} held notifications")


# ── Public API for other scripts ─────────────────────────────────────────────

def should_alert():
    """Check if it's appropriate to send Jordan an alert right now.

    Returns (should_send, reason). Other Nova scripts can import this.
    Usage:
        from nova_proactive_peace import should_alert
        can_send, reason = should_alert()
        if not can_send:
            queue_message("my alert text", "my_script")
    """
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            jordan_state = state.get("jordan_state", "available")
            if jordan_state in ("sleeping", "dnd", "meeting"):
                return False, jordan_state
            if jordan_state == "coding" and state.get("focus_mode") == "work":
                return False, "deep_focus"
        except Exception:
            pass
    return True, "available"


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log("Checking Jordan's state...")

    focus_mode = get_focus_mode()
    screen = get_screen_state()
    activity = get_activity_level()
    burnout = detect_burnout_signals()

    # Determine overall state
    if screen == "locked" and HOUR in SLEEP_HOURS:
        jordan_state = "sleeping"
    elif focus_mode == "dnd":
        jordan_state = "dnd"
    elif focus_mode == "sleep":
        jordan_state = "sleeping"
    elif activity == "meeting":
        jordan_state = "meeting"
    elif activity == "coding" and focus_mode == "work":
        jordan_state = "deep_focus"
    elif activity == "coding":
        jordan_state = "coding"
    elif activity == "winding_down":
        jordan_state = "winding_down"
    else:
        jordan_state = "available"

    # Load previous state to detect transitions
    prev_state = {}
    if STATE_FILE.exists():
        try:
            prev_state = json.loads(STATE_FILE.read_text())
        except Exception:
            pass

    prev_jordan_state = prev_state.get("jordan_state", "unknown")

    # Save current state
    state = {
        "jordan_state": jordan_state,
        "focus_mode": focus_mode,
        "screen": screen,
        "activity": activity,
        "burnout_signals": burnout,
        "updated_at": NOW.isoformat(),
        "date": TODAY,
    }
    STATE_FILE.write_text(json.dumps(state, indent=2))

    log(f"State: {jordan_state} (focus={focus_mode}, screen={screen}, activity={activity})")

    # ── State transition handling ────────────────────────────────────────────

    # Transition to available → release held messages
    unavailable_states = ("sleeping", "dnd", "meeting", "deep_focus")
    if jordan_state not in unavailable_states and prev_jordan_state in unavailable_states:
        log("Jordan is back — releasing held notifications")
        release_queue()

    # ── Burnout nudges ───────────────────────────────────────────────────────
    if burnout:
        # Only nudge once per evening
        last_nudge = prev_state.get("last_burnout_nudge", "")
        if last_nudge != TODAY:
            nudges = {
                "still_coding_at_23": "It's past 11pm and you're still coding. Maybe call it a night?",
                "still_coding_at_0": "It's midnight, Little Mister. The code will still be there tomorrow.",
                "weekend_commits": "Weekend commits detected. Is this fun-coding or should-stop-coding?",
            }
            for signal in burnout:
                for key, msg in nudges.items():
                    if key in signal:
                        slack_post(f"_{msg}_")
                        state["last_burnout_nudge"] = TODAY
                        STATE_FILE.write_text(json.dumps(state, indent=2))
                        log(f"Burnout nudge sent: {signal}")
                        break


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Proactive Peace")
    parser.add_argument("--status", action="store_true", help="Show current state")
    parser.add_argument("--queue", action="store_true", help="Show held messages")
    parser.add_argument("--release", action="store_true", help="Force release held messages")
    parser.add_argument("--check", action="store_true", help="Check if alerts should be sent")
    args = parser.parse_args()

    if args.status:
        focus = get_focus_mode()
        screen = get_screen_state()
        activity = get_activity_level()
        burnout = detect_burnout_signals()
        print(f"Focus mode: {focus}")
        print(f"Screen: {screen}")
        print(f"Activity: {activity}")
        print(f"Burnout signals: {burnout or 'none'}")
        can_send, reason = should_alert()
        print(f"Should alert: {can_send} ({reason})")
    elif args.queue:
        queue = load_queue()
        msgs = queue.get("messages", [])
        if msgs:
            for m in msgs:
                print(f"  [{m['priority']}] {m['source']}: {m['text'][:80]}")
            print(f"\n{len(msgs)} held message(s)")
        else:
            print("No held messages.")
    elif args.release:
        release_queue()
    elif args.check:
        can_send, reason = should_alert()
        print(f"{'YES' if can_send else 'NO'} — {reason}")
    else:
        main()
