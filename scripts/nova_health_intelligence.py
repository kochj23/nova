#!/usr/bin/env python3
"""
nova_health_intelligence.py — Proactive health trend analysis and cross-referencing.

Colette's suggestion: "not just 'here's your data' but 'your resting heart rate
has been trending up for 5 days, maybe check in with your doctor'" and
"cross-referencing health patterns with calendar/life events — that's where
the real value is."

This script:
  1. Reads health data from the iCloud Drive JSON files
  2. Computes multi-day trends for key vitals (rolling averages, direction)
  3. Detects concerning patterns (not just single readings, but TRENDS)
  4. Cross-references with calendar events, GitHub activity, and sleep
     to find correlations:
       - "You sleep 1.2 hours less on nights before meetings"
       - "Your resting HR rises after 3+ consecutive coding days"
       - "Blood pressure is lower on weekends"
  5. Posts proactive alerts to Jordan's DM when patterns are concerning
  6. Weekly health intelligence report with correlations

PRIVACY: All health intents are PRIVATE in the intent router — hard-fail
if local models are down. Never touches OpenRouter.

Cron: daily at 8am (after health ingest), weekly Sunday at 10am
Written by Jordan Koch.
"""

import json
import math
import subprocess
import sys
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
JORDAN_DM = nova_config.JORDAN_DM
SLACK_API = nova_config.SLACK_API
VECTOR_URL = nova_config.VECTOR_URL
NOW = datetime.now()
TODAY = date.today().isoformat()

ICLOUD_HEALTH = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Nova/health"
STATE_FILE = Path("/tmp/nova_health_intelligence_state.json")

# ── Trend thresholds ─────────────────────────────────────────────────────────
# These define when a TREND (not a single reading) becomes concerning.
# A single high reading is a blip. Five days trending up is a pattern.

TREND_ALERTS = {
    "resting_heart_rate": {
        "window_days": 5,
        "rising_threshold": 8,    # bpm increase over window
        "high_avg_threshold": 85, # average above this = concern
        "label": "Resting heart rate",
        "unit": "bpm",
        "advice": "Consider checking in with your doctor if this continues.",
    },
    "blood_pressure_sys": {
        "window_days": 5,
        "rising_threshold": 10,
        "high_avg_threshold": 135,
        "label": "Systolic blood pressure",
        "unit": "mmHg",
        "advice": "Persistent elevation may need medical attention.",
    },
    "blood_pressure_dia": {
        "window_days": 5,
        "rising_threshold": 8,
        "high_avg_threshold": 85,
        "label": "Diastolic blood pressure",
        "unit": "mmHg",
        "advice": "Persistent elevation may need medical attention.",
    },
    "heart_rate": {
        "window_days": 3,
        "rising_threshold": 15,
        "high_avg_threshold": 95,
        "label": "Heart rate",
        "unit": "bpm",
        "advice": "Elevated resting HR can indicate stress, dehydration, or illness.",
    },
    "hrv": {
        "window_days": 5,
        "falling_threshold": -10,  # HRV dropping is bad (opposite direction)
        "low_avg_threshold": 25,
        "label": "Heart rate variability",
        "unit": "ms",
        "advice": "Declining HRV may indicate stress or insufficient recovery.",
    },
    "blood_oxygen": {
        "window_days": 3,
        "falling_threshold": -2,
        "low_avg_threshold": 94,
        "label": "Blood oxygen",
        "unit": "%",
        "advice": "Persistent low SpO2 should be evaluated by a doctor.",
    },
    "weight": {
        "window_days": 14,
        "rising_threshold": 5,  # lbs
        "falling_threshold": -5,
        "label": "Weight",
        "unit": "lbs",
        "advice": "Unexpected changes may warrant attention.",
    },
}


def log(msg):
    print(f"[nova_health_intel {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_dm(text):
    data = json.dumps({
        "channel": JORDAN_DM, "text": text, "mrkdwn": True
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


def vector_remember(text, metadata=None):
    try:
        payload = json.dumps({
            "text": text, "source": "health_intelligence", "metadata": metadata or {}
        }).encode()
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ── Health data loading ──────────────────────────────────────────────────────

def load_health_days(days=14):
    """Load health data from the last N days of iCloud JSON files."""
    if not ICLOUD_HEALTH.exists():
        return {}

    daily_data = {}  # {date_str: {type: [values]}}

    for f in sorted(ICLOUD_HEALTH.glob("health-*.json")):
        fname = f.stem
        file_date = fname.replace("health-", "")[:10]

        cutoff = (date.today() - timedelta(days=days)).isoformat()
        if file_date < cutoff:
            continue

        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            readings = data.get("readings", {})

            for rtype, entries in readings.items():
                if not isinstance(entries, list):
                    entries = [entries]
                for entry in entries:
                    entry_date = entry.get("date", file_date)[:10]
                    daily_data.setdefault(entry_date, {}).setdefault(rtype, [])
                    if "value" in entry:
                        daily_data[entry_date][rtype].append(entry["value"])
        except Exception:
            continue

    return daily_data


def daily_averages(daily_data, data_type):
    """Compute daily averages for a data type across all days."""
    avgs = {}
    for day, types in sorted(daily_data.items()):
        values = types.get(data_type, [])
        if values:
            avgs[day] = sum(values) / len(values)
    return avgs


# ── Trend detection ──────────────────────────────────────────────────────────

def detect_trends(daily_data):
    """Analyze multi-day trends for all vital types."""
    alerts = []

    for data_type, config in TREND_ALERTS.items():
        avgs = daily_averages(daily_data, data_type)
        if len(avgs) < 3:
            continue

        window = config["window_days"]
        label = config["label"]
        unit = config["unit"]

        # Get the last N days of averages
        sorted_days = sorted(avgs.keys())
        recent_days = sorted_days[-window:] if len(sorted_days) >= window else sorted_days
        recent_values = [avgs[d] for d in recent_days]

        if len(recent_values) < 3:
            continue

        # Compute trend direction (linear regression slope)
        n = len(recent_values)
        x_mean = (n - 1) / 2
        y_mean = sum(recent_values) / n
        numerator = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(recent_values))
        denominator = sum((i - x_mean) ** 2 for i in range(n))
        slope = numerator / denominator if denominator != 0 else 0
        total_change = slope * (n - 1)

        current_avg = sum(recent_values[-3:]) / min(3, len(recent_values[-3:]))
        overall_avg = y_mean

        # Check rising threshold
        rising = config.get("rising_threshold")
        if rising and total_change >= rising:
            alerts.append({
                "type": data_type,
                "pattern": "rising",
                "label": label,
                "message": (
                    f"*{label}* has been trending UP over the last {len(recent_days)} days "
                    f"(+{total_change:.1f} {unit}, current avg: {current_avg:.1f} {unit})"
                ),
                "advice": config["advice"],
                "severity": "warning",
                "change": total_change,
                "current_avg": current_avg,
            })

        # Check falling threshold (for HRV, SpO2 where dropping is bad)
        falling = config.get("falling_threshold")
        if falling and total_change <= falling:
            alerts.append({
                "type": data_type,
                "pattern": "falling",
                "label": label,
                "message": (
                    f"*{label}* has been trending DOWN over the last {len(recent_days)} days "
                    f"({total_change:.1f} {unit}, current avg: {current_avg:.1f} {unit})"
                ),
                "advice": config["advice"],
                "severity": "warning",
                "change": total_change,
                "current_avg": current_avg,
            })

        # Check sustained high/low average
        high_thresh = config.get("high_avg_threshold")
        if high_thresh and current_avg >= high_thresh:
            alerts.append({
                "type": data_type,
                "pattern": "sustained_high",
                "label": label,
                "message": (
                    f"*{label}* has averaged {current_avg:.1f} {unit} "
                    f"over the last {len(recent_days)} days (threshold: {high_thresh})"
                ),
                "advice": config["advice"],
                "severity": "concern",
                "current_avg": current_avg,
            })

        low_thresh = config.get("low_avg_threshold")
        if low_thresh and current_avg <= low_thresh:
            alerts.append({
                "type": data_type,
                "pattern": "sustained_low",
                "label": label,
                "message": (
                    f"*{label}* has averaged {current_avg:.1f} {unit} "
                    f"over the last {len(recent_days)} days (threshold: {low_thresh})"
                ),
                "advice": config["advice"],
                "severity": "concern",
                "current_avg": current_avg,
            })

    return alerts


# ── Cross-referencing with life events ───────────────────────────────────────

def get_calendar_days():
    """Get days that had calendar events."""
    try:
        from nova_calendar import get_todays_events
        # For historical cross-referencing, we'd need past calendar data
        # For now, use vector memory to recall meeting-heavy days
        import urllib.parse
        params = urllib.parse.urlencode({"q": "meeting calendar", "n": 20, "source": "calendar"})
        url = f"http://127.0.0.1:18790/recall?{params}"
        with urllib.request.urlopen(url, timeout=5) as r:
            results = json.loads(r.read())
            items = results if isinstance(results, list) else results.get("results", [])
            meeting_days = set()
            for item in items:
                d = item.get("metadata", {}).get("date", "")[:10]
                if d:
                    meeting_days.add(d)
            return meeting_days
    except Exception:
        return set()


def get_coding_days():
    """Get days with GitHub commit activity."""
    try:
        r = subprocess.run(
            ["gh", "api", "/users/kochj23/events?per_page=100"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode != 0:
            return set()
        events = json.loads(r.stdout)
        coding_days = set()
        for e in events:
            if e["type"] == "PushEvent":
                coding_days.add(e["created_at"][:10])
        return coding_days
    except Exception:
        return set()


def get_weekend_days(days=30):
    """Get weekend dates in the last N days."""
    weekends = set()
    for i in range(days):
        d = date.today() - timedelta(days=i)
        if d.weekday() >= 5:
            weekends.add(d.isoformat())
    return weekends


def cross_reference(daily_data):
    """Find correlations between health metrics and life events."""
    correlations = []

    meeting_days = get_calendar_days()
    coding_days = get_coding_days()
    weekends = get_weekend_days()

    # ── Sleep vs meetings ────────────────────────────────────────────────
    sleep_data = {}
    for day, types in daily_data.items():
        sleep_entries = types.get("sleep", [])
        if sleep_entries and isinstance(sleep_entries[0], (int, float)):
            sleep_data[day] = sum(sleep_entries)
        # Sleep might be stored as duration_min in metadata
        # Try to get from the raw data
        elif "sleep_duration" in types:
            sleep_data[day] = sum(types["sleep_duration"]) / 60  # convert to hours

    if sleep_data and meeting_days:
        # Check sleep on nights BEFORE meeting days
        meeting_sleep = []
        non_meeting_sleep = []
        for day, hours in sleep_data.items():
            # The night before = the day before
            next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
            if next_day in meeting_days:
                meeting_sleep.append(hours)
            else:
                non_meeting_sleep.append(hours)

        if meeting_sleep and non_meeting_sleep:
            avg_meeting = sum(meeting_sleep) / len(meeting_sleep)
            avg_non = sum(non_meeting_sleep) / len(non_meeting_sleep)
            diff = avg_non - avg_meeting
            if abs(diff) > 0.5:  # More than 30 min difference
                direction = "less" if diff > 0 else "more"
                correlations.append(
                    f"You sleep *{abs(diff):.1f} hours {direction}* on nights before meeting days "
                    f"({avg_meeting:.1f}h vs {avg_non:.1f}h)"
                )

    # ── Heart rate vs coding marathons ───────────────────────────────────
    hr_avgs = daily_averages(daily_data, "resting_heart_rate")
    if hr_avgs and coding_days:
        coding_hrs = [hr_avgs[d] for d in hr_avgs if d in coding_days]
        non_coding_hrs = [hr_avgs[d] for d in hr_avgs if d not in coding_days]
        if coding_hrs and non_coding_hrs:
            avg_coding = sum(coding_hrs) / len(coding_hrs)
            avg_non = sum(non_coding_hrs) / len(non_coding_hrs)
            diff = avg_coding - avg_non
            if abs(diff) > 3:
                direction = "higher" if diff > 0 else "lower"
                correlations.append(
                    f"Resting HR is *{abs(diff):.1f} bpm {direction}* on coding days "
                    f"({avg_coding:.0f} vs {avg_non:.0f} bpm)"
                )

    # ── Blood pressure on weekends vs weekdays ───────────────────────────
    bp_avgs = daily_averages(daily_data, "blood_pressure_sys")
    if bp_avgs and weekends:
        weekend_bp = [bp_avgs[d] for d in bp_avgs if d in weekends]
        weekday_bp = [bp_avgs[d] for d in bp_avgs if d not in weekends]
        if weekend_bp and weekday_bp:
            avg_wkend = sum(weekend_bp) / len(weekend_bp)
            avg_wkday = sum(weekday_bp) / len(weekday_bp)
            diff = avg_wkday - avg_wkend
            if abs(diff) > 5:
                direction = "higher" if diff > 0 else "lower"
                correlations.append(
                    f"Blood pressure is *{abs(diff):.0f} mmHg {direction}* on weekdays vs weekends "
                    f"({avg_wkday:.0f} vs {avg_wkend:.0f})"
                )

    # ── Steps vs sleep quality ───────────────────────────────────────────
    steps_avgs = daily_averages(daily_data, "steps")
    if steps_avgs and sleep_data:
        # More steps → better sleep?
        active_days = {d: s for d, s in steps_avgs.items() if s > 8000}
        sedentary_days = {d: s for d, s in steps_avgs.items() if s < 4000}

        active_sleep = [sleep_data[d] for d in active_days if d in sleep_data]
        sedentary_sleep = [sleep_data[d] for d in sedentary_days if d in sleep_data]

        if active_sleep and sedentary_sleep:
            avg_active = sum(active_sleep) / len(active_sleep)
            avg_sed = sum(sedentary_sleep) / len(sedentary_sleep)
            diff = avg_active - avg_sed
            if abs(diff) > 0.3:
                better = "better" if diff > 0 else "worse"
                correlations.append(
                    f"You sleep *{abs(diff):.1f} hours {better}* on active days (8K+ steps) "
                    f"vs sedentary days (<4K steps)"
                )

    return correlations


# ── Main ─────────────────────────────────────────────────────────────────────

def daily_analysis():
    """Run daily health intelligence analysis."""
    log("Running health intelligence analysis...")
    daily_data = load_health_days(days=14)

    if not daily_data:
        log("No health data available.")
        return

    log(f"Loaded {len(daily_data)} days of health data")

    # Detect trends
    alerts = detect_trends(daily_data)
    state = load_state()

    # Filter already-sent alerts (once per type per day)
    new_alerts = []
    for a in alerts:
        key = f"{TODAY}_{a['type']}_{a['pattern']}"
        if key not in state.get("sent_alerts", set()):
            new_alerts.append(a)
            state.setdefault("sent_alerts", set()).add(key)

    if new_alerts:
        lines = ["*Health Intelligence*"]
        for a in new_alerts:
            icon = "!!" if a["severity"] == "concern" else "!"
            lines.append(f"  {icon} {a['message']}")
            lines.append(f"    _{a['advice']}_")
        slack_dm("\n".join(lines))
        log(f"Sent {len(new_alerts)} trend alert(s)")

        for a in new_alerts:
            vector_remember(
                f"Health trend alert {TODAY}: {a['label']} {a['pattern']} "
                f"(avg {a.get('current_avg', '?')})",
                {"date": TODAY, "type": "health_trend_alert", "vital": a["type"]}
            )

    save_state(state)


def weekly_intelligence():
    """Generate weekly health intelligence report with cross-references."""
    log("Generating weekly health intelligence report...")
    daily_data = load_health_days(days=30)

    if not daily_data:
        log("Not enough data for weekly report.")
        return

    # Trends
    alerts = detect_trends(daily_data)

    # Cross-references
    correlations = cross_reference(daily_data)

    lines = [f"*Weekly Health Intelligence — {NOW.strftime('%B %d')}*"]

    # Vital summaries
    lines.append("")
    lines.append("*14-Day Vital Trends:*")
    for data_type in ["resting_heart_rate", "blood_pressure_sys", "blood_pressure_dia",
                       "blood_oxygen", "hrv", "weight"]:
        avgs = daily_averages(daily_data, data_type)
        if not avgs:
            continue
        recent = list(avgs.values())[-7:]
        if not recent:
            continue
        avg = sum(recent) / len(recent)
        label = data_type.replace("_", " ").title()

        # Trend direction
        if len(recent) >= 3:
            early = sum(recent[:len(recent)//2]) / max(1, len(recent)//2)
            late = sum(recent[len(recent)//2:]) / max(1, len(recent) - len(recent)//2)
            if late > early * 1.03:
                trend = " ↑"
            elif late < early * 0.97:
                trend = " ↓"
            else:
                trend = " →"
        else:
            trend = ""

        lines.append(f"  {label}: {avg:.1f}{trend} ({len(avgs)} days of data)")

    # Concerning trends
    if alerts:
        lines.append("")
        lines.append("*Patterns to Watch:*")
        for a in alerts:
            lines.append(f"  {a['message']}")

    # Correlations
    if correlations:
        lines.append("")
        lines.append("*Life-Health Connections:*")
        for c in correlations:
            lines.append(f"  {c}")

    if not alerts and not correlations:
        lines.append("")
        lines.append("_All vitals stable. No concerning patterns detected._")

    lines.append(f"\n_Based on {len(daily_data)} days of health data_")

    slack_dm("\n".join(lines))

    vector_remember(
        f"Weekly health intelligence {TODAY}: {len(alerts)} trend alerts, "
        f"{len(correlations)} correlations found",
        {"date": TODAY, "type": "weekly_health_report"}
    )

    log(f"Weekly report posted: {len(alerts)} alerts, {len(correlations)} correlations")


# ── State ────────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            # Convert sent_alerts back to set
            state["sent_alerts"] = set(state.get("sent_alerts", []))
            return state
        except Exception:
            pass
    return {"sent_alerts": set(), "last_daily": "", "last_weekly": ""}


def save_state(state):
    # Convert set to list for JSON
    state_copy = {**state, "sent_alerts": list(state.get("sent_alerts", set()))}
    STATE_FILE.write_text(json.dumps(state_copy))


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Health Intelligence")
    parser.add_argument("--daily", action="store_true", help="Run daily trend analysis (default)")
    parser.add_argument("--weekly", action="store_true", help="Generate weekly intelligence report")
    parser.add_argument("--correlations", action="store_true", help="Show life-health correlations")
    parser.add_argument("--trends", action="store_true", help="Show current vital trends")
    args = parser.parse_args()

    if args.weekly:
        weekly_intelligence()
    elif args.correlations:
        daily_data = load_health_days(days=30)
        correlations = cross_reference(daily_data)
        if correlations:
            print("Life-Health Correlations:")
            for c in correlations:
                print(f"  {c}")
        else:
            print("Not enough data for correlations yet.")
    elif args.trends:
        daily_data = load_health_days(days=14)
        alerts = detect_trends(daily_data)
        if alerts:
            print("Current Trend Alerts:")
            for a in alerts:
                print(f"  [{a['severity']}] {a['message']}")
                print(f"    {a['advice']}")
        else:
            print("No concerning trends detected.")
    else:
        daily_analysis()
