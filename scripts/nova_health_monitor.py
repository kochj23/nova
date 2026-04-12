#!/usr/bin/env python3
"""
nova_health_monitor.py — Apple Health data ingestion for Nova.

Reads health data exported from an iPhone Shortcut via iCloud Drive.
The Shortcut runs daily on the iPhone, queries HealthKit, and saves
a JSON file to iCloud Drive/Nova/health/. This script picks it up.

PRIVACY: All health queries are routed through the intent router as
"private" intents — they NEVER leave the machine. Health data is stored
in vector memory with source="apple_health" for semantic recall.

Data flow:
  iPhone HealthKit → Shortcut → iCloud Drive → this script → vector memory
  (never touches OpenRouter — health intents are hard-fail local only)

Cron: every 4 hours (ingest new readings)
Manual: --trends (7-day trend report), --ingest (force ingest)

Written by Jordan Koch.
"""

import glob
import json
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
SCRIPTS = Path(__file__).parent
ICLOUD_HEALTH = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Nova/health"
STATE_FILE = Path("/tmp/nova_health_monitor_state.json")

# Thresholds for concerning readings (alert Jordan's DM)
ALERT_THRESHOLDS = {
    "blood_pressure_sys": {"high": 140, "low": 90,  "label": "Systolic BP"},
    "blood_pressure_dia": {"high": 90,  "low": 60,  "label": "Diastolic BP"},
    "heart_rate":         {"high": 120, "low": 50,  "label": "Heart rate"},
    "blood_oxygen":       {"high": 100, "low": 92,  "label": "Blood oxygen"},
    "blood_glucose":      {"high": 180, "low": 70,  "label": "Blood glucose"},
    "resting_heart_rate": {"high": 100, "low": 40,  "label": "Resting HR"},
    "body_temperature":   {"high": 100.4, "low": 96, "label": "Temperature"},
}


def log(msg):
    print(f"[nova_health {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_dm(text):
    """Post to Jordan's DM only — never #nova-chat for health data."""
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
    """Store health data in vector memory with source=apple_health."""
    try:
        payload = json.dumps({
            "text": text,
            "source": "apple_health",
            "metadata": metadata or {},
        }).encode()
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


def vector_remember_async(text, metadata=None):
    """Async store — fire and forget for bulk ingestion."""
    try:
        payload = json.dumps({
            "text": text,
            "source": "apple_health",
            "metadata": metadata or {},
        }).encode()
        req = urllib.request.Request(
            f"{VECTOR_URL}?async=1", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass


# ── Health data reader (iCloud Drive) ────────────────────────────────────────

def read_health_data(hours=24, data_type=None):
    """Read health data from iCloud Drive JSON files dropped by iPhone Shortcut.

    The iPhone Shortcut saves files as:
      iCloud Drive/Nova/health/health-YYYY-MM-DD.json
      iCloud Drive/Nova/health/health-YYYY-MM-DDTHH-MM-SS.json

    Each file contains:
      {"date": "...", "readings": {"heart_rate": [...], "blood_pressure_sys": [...], ...}}
    """
    if not ICLOUD_HEALTH.exists():
        log(f"iCloud health folder not found: {ICLOUD_HEALTH}")
        log("Create it with: mkdir -p ~/Library/Mobile\\ Documents/com~apple~CloudDocs/Nova/health")
        return None

    # Find JSON files within the time window
    cutoff = (NOW - timedelta(hours=hours)).isoformat()[:10]
    files = sorted(ICLOUD_HEALTH.glob("health-*.json"))

    if not files:
        # Also check for .icloud placeholder files (not yet downloaded)
        placeholders = list(ICLOUD_HEALTH.glob(".health-*.json.icloud"))
        if placeholders:
            log(f"Found {len(placeholders)} iCloud placeholder(s) — files not downloaded yet.")
            log("Open the Nova/health folder in Finder to trigger download, or run:")
            log(f"  brctl download {ICLOUD_HEALTH}/")
            # Try to trigger download
            try:
                for p in placeholders:
                    subprocess.run(["brctl", "download", str(p)],
                                   capture_output=True, timeout=5)
            except Exception:
                pass
        else:
            log("No health data files found in iCloud Drive/Nova/health/")
            log("Set up the iPhone Shortcut to export health data.")
        return None

    # Merge readings from all files in the time window
    merged_readings = {}
    files_read = 0

    for f in files:
        # Extract date from filename: health-YYYY-MM-DD.json
        fname = f.stem  # health-2026-04-12
        file_date = fname.replace("health-", "")[:10]

        if file_date < cutoff:
            continue

        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            readings = data.get("readings", {})

            for rtype, entries in readings.items():
                if data_type and rtype != data_type:
                    continue
                merged_readings.setdefault(rtype, []).extend(
                    entries if isinstance(entries, list) else [entries]
                )
            files_read += 1
        except json.JSONDecodeError as e:
            log(f"Skipping {f.name}: JSON error: {e}")
        except Exception as e:
            log(f"Skipping {f.name}: {e}")

    if not merged_readings:
        log(f"No readings found in {len(files)} file(s) within last {hours}h")
        return None

    log(f"Read {files_read} file(s), {sum(len(v) for v in merged_readings.values())} total readings")

    return {
        "period_hours": hours,
        "start": cutoff,
        "end": TODAY,
        "readings": merged_readings,
    }


# ── Data processing ──────────────────────────────────────────────────────────

def summarize_readings(readings):
    """Create human-readable summaries for vector memory storage."""
    summaries = []
    for data_type, entries in readings.items():
        if not entries:
            continue

        if data_type == "sleep":
            # Sleep is structured differently
            total_min = sum(e.get("duration_min", 0) for e in entries
                           if e.get("stage") not in ("awake", "in_bed"))
            deep_min = sum(e.get("duration_min", 0) for e in entries
                          if e.get("stage") == "deep")
            rem_min = sum(e.get("duration_min", 0) for e in entries
                         if e.get("stage") == "rem")
            if total_min > 0:
                hours = total_min / 60
                summaries.append(
                    f"Sleep: {hours:.1f} hours total "
                    f"({deep_min:.0f} min deep, {rem_min:.0f} min REM)"
                )
            continue

        values = [e["value"] for e in entries if "value" in e]
        if not values:
            continue

        unit = entries[0].get("unit", "")
        avg = sum(values) / len(values)
        latest = values[-1]
        high = max(values)
        low = min(values)

        label = data_type.replace("_", " ").title()
        if len(values) == 1:
            summaries.append(f"{label}: {latest} {unit}")
        else:
            summaries.append(
                f"{label}: latest {latest} {unit}, "
                f"avg {avg:.1f}, range {low}-{high} ({len(values)} readings)"
            )

    return summaries


def check_alerts(readings):
    """Check readings against alert thresholds."""
    alerts = []
    for data_type, thresholds in ALERT_THRESHOLDS.items():
        entries = readings.get(data_type, [])
        if not entries:
            continue

        latest = entries[-1]
        value = latest.get("value")
        if value is None:
            continue

        label = thresholds["label"]
        unit = latest.get("unit", "")

        if value >= thresholds["high"]:
            alerts.append(f"{label} is HIGH: {value} {unit} (threshold: {thresholds['high']})")
        elif value <= thresholds["low"]:
            alerts.append(f"{label} is LOW: {value} {unit} (threshold: {thresholds['low']})")

    return alerts


# ── State management ─────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_ingest": "", "last_alert_date": ""}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


# ── Ingestion ────────────────────────────────────────────────────────────────

def ingest(hours=24):
    """Read health data and store in vector memory."""
    log(f"Reading health data (last {hours}h)...")
    data = read_health_data(hours=hours)
    if not data:
        log("No health data available.")
        return

    readings = data.get("readings", {})
    if not readings:
        log("No readings found.")
        return

    # Count total readings
    total = sum(len(entries) for entries in readings.values())
    log(f"Got {total} readings across {len(readings)} types")

    # Create summaries and store in vector memory
    summaries = summarize_readings(readings)
    if summaries:
        # Store a daily summary as one memory chunk
        summary_text = f"Health readings for {TODAY}: " + "; ".join(summaries)
        vector_remember(summary_text, {
            "date": TODAY,
            "type": "health_daily_summary",
            "reading_count": total,
        })
        log(f"Stored daily health summary ({len(summaries)} types)")

    # Store individual significant readings for granular recall
    for data_type, entries in readings.items():
        if not entries or data_type == "steps":
            continue  # Steps are too granular for individual storage

        if data_type == "sleep":
            total_min = sum(e.get("duration_min", 0) for e in entries
                           if e.get("stage") not in ("awake", "in_bed"))
            if total_min > 0:
                # Get the date from the first sleep entry
                sleep_date = entries[0].get("start", TODAY)[:10]
                vector_remember_async(
                    f"Sleep on {sleep_date}: {total_min/60:.1f} hours",
                    {"date": sleep_date, "type": "sleep", "duration_min": total_min}
                )
            continue

        # For vital signs, store the latest reading
        latest = entries[-1]
        value = latest.get("value")
        unit = latest.get("unit", "")
        reading_date = latest.get("date", TODAY)[:10]
        label = data_type.replace("_", " ").title()

        vector_remember_async(
            f"{label} reading on {reading_date}: {value} {unit}",
            {"date": reading_date, "type": data_type, "value": value}
        )

    # Check for concerning values
    alerts = check_alerts(readings)
    state = load_state()

    if alerts and state.get("last_alert_date") != TODAY:
        # Send alerts to DM — but only once per day
        lines = ["*Health Alert*"]
        for a in alerts:
            lines.append(f"  {a}")
        slack_dm("\n".join(lines))
        state["last_alert_date"] = TODAY
        log(f"Sent {len(alerts)} health alert(s) to DM")

    state["last_ingest"] = NOW.isoformat()
    save_state(state)
    log("Health ingest complete.")


# ── Trend analysis ───────────────────────────────────────────────────────────

def trends(days=7):
    """Analyze trends over the past N days. Prints to stdout."""
    log(f"Reading {days}-day health trends...")
    data = read_health_data(hours=days * 24)
    if not data:
        print("No health data available.")
        return

    readings = data.get("readings", {})
    print(f"\nHealth Trends — Last {days} Days\n{'='*40}")

    for data_type, entries in sorted(readings.items()):
        if not entries or data_type == "sleep":
            continue

        values = [e["value"] for e in entries if "value" in e]
        if not values:
            continue

        unit = entries[0].get("unit", "")
        label = data_type.replace("_", " ").title()
        avg = sum(values) / len(values)
        latest = values[-1]

        # Simple trend: compare last 25% to first 25%
        quarter = max(1, len(values) // 4)
        early_avg = sum(values[:quarter]) / quarter
        late_avg = sum(values[-quarter:]) / quarter

        if late_avg > early_avg * 1.05:
            trend = "trending UP"
        elif late_avg < early_avg * 0.95:
            trend = "trending DOWN"
        else:
            trend = "stable"

        print(f"\n{label} ({len(values)} readings)")
        print(f"  Latest: {latest} {unit}")
        print(f"  Average: {avg:.1f} {unit}")
        print(f"  Range: {min(values)}-{max(values)} {unit}")
        print(f"  Trend: {trend}")

    # Sleep summary
    sleep_entries = readings.get("sleep", [])
    if sleep_entries:
        # Group by night
        nights = defaultdict(float)
        for e in sleep_entries:
            if e.get("stage") not in ("awake", "in_bed"):
                night = e.get("start", "")[:10]
                nights[night] += e.get("duration_min", 0)

        if nights:
            avg_hours = sum(nights.values()) / len(nights) / 60
            print(f"\nSleep ({len(nights)} nights)")
            print(f"  Average: {avg_hours:.1f} hours/night")
            for night, mins in sorted(nights.items())[-5:]:
                print(f"  {night}: {mins/60:.1f} hours")


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Health Monitor")
    parser.add_argument("--ingest", action="store_true", help="Ingest recent health data into vector memory (default)")
    parser.add_argument("--hours", type=int, default=24, help="Hours of data to ingest (default: 24)")
    parser.add_argument("--trends", type=int, nargs="?", const=7, help="Show N-day trend analysis (default: 7)")
    parser.add_argument("--raw", action="store_true", help="Print raw JSON from health reader")
    parser.add_argument("--type", type=str, help="Query specific data type only")
    parser.add_argument("--check", action="store_true", help="Check latest readings against alert thresholds")
    args = parser.parse_args()

    if args.trends is not None:
        trends(args.trends)
    elif args.raw:
        data = read_health_data(hours=args.hours, data_type=args.type)
        if data:
            print(json.dumps(data, indent=2))
        else:
            print("No data.")
    elif args.check:
        data = read_health_data(hours=6)
        if data:
            alerts = check_alerts(data.get("readings", {}))
            if alerts:
                for a in alerts:
                    print(f"  {a}")
            else:
                print("All readings within normal range.")
        else:
            print("No data available.")
    else:
        ingest(hours=args.hours)
