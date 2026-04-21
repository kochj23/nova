#!/usr/bin/env python3
"""
nova_health_correlation.py — Cross-reference health data with calendar, email,
and coding activity to detect lifestyle-health correlations.

Data sources:
  - Health: ~/.openclaw/private/health/ (daily JSON files)
  - Calendar: Vector memory recall (source="calendar")
  - Email volume: Vector memory recall (source="email_archive")
  - Coding activity: Vector memory recall (source="github")

Correlations detected:
  - Sleep vs meeting density
  - Resting HR vs meeting-heavy days (stress indicator)
  - HRV weekend vs weekday (recovery pattern)
  - Steps vs coding days (inverse relationship)
  - Active energy vs calendar event count

Output:
  - Weekly/monthly health correlation report
  - Posts findings to Slack #nova-notifications
  - Stores insights in vector memory (source="health_correlation", privacy="local-only")

Usage:
  ./nova_health_correlation.py --weekly     # Last 7 days (default)
  ./nova_health_correlation.py --monthly    # Last 30 days

PRIVACY: All health data stays local. Never touches cloud APIs.
Written by Jordan Koch.
"""

import argparse
import json
import statistics
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Configuration ────────────────────────────────────────────────────────────

HEALTH_DIR = Path.home() / ".openclaw/private/health"
VECTOR_RECALL = "http://127.0.0.1:18790/recall"
VECTOR_REMEMBER = "http://127.0.0.1:18790/remember"
SLACK_API = nova_config.SLACK_API
SLACK_CHANNEL = nova_config.SLACK_NOTIFY  # #nova-notifications (C0ATAF7NZG9)
NOW = datetime.now()
TODAY = date.today()

HEALTH_FIELDS = ["sleep_hours", "resting_heart_rate", "hrv", "steps", "active_energy"]


# ── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[health_correlation {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Health data loading ──────────────────────────────────────────────────────

def load_health_data(days: int) -> dict:
    """Load daily health JSON files for the last N days.

    Returns: {date_str: {field: value}} for each day with data.
    """
    if not HEALTH_DIR.exists():
        log(f"Health directory not found: {HEALTH_DIR}")
        return {}

    cutoff = TODAY - timedelta(days=days)
    daily = {}

    for json_file in sorted(HEALTH_DIR.glob("*.json")):
        fname = json_file.stem
        if fname == "latest":
            continue

        # Parse date from filename (e.g., "2026-04-20")
        try:
            file_date = date.fromisoformat(fname[:10])
        except ValueError:
            continue

        if file_date < cutoff:
            continue

        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        day_record = {}
        for field in HEALTH_FIELDS:
            if field in data and isinstance(data[field], (int, float)):
                day_record[field] = data[field]

        if day_record:
            daily[file_date.isoformat()] = day_record

    log(f"Loaded health data for {len(daily)} day(s) (lookback={days}d)")
    return daily


# ── Vector memory queries ────────────────────────────────────────────────────

VECTOR_MAX_N = 50  # API rejects n > 50


def _recall(query: str, source: str, n: int = 50) -> list:
    """Query vector memory for recall results (max 50 per request)."""
    n = min(n, VECTOR_MAX_N)
    params = urllib.parse.urlencode({"q": query, "n": n, "source": source})
    url = f"{VECTOR_RECALL}?{params}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            if isinstance(body, list):
                return body
            return body.get("memories", body.get("results", []))
    except Exception as exc:
        log(f"Vector recall failed (source={source}): {exc}")
        return []


def get_calendar_events_by_date(days: int) -> dict:
    """Count calendar events per date from vector memory.

    Returns: {date_str: event_count}
    """
    results = _recall("meeting calendar event", source="calendar", n=50)
    cutoff = (TODAY - timedelta(days=days)).isoformat()
    counts = defaultdict(int)

    for item in results:
        meta = item.get("metadata", {})
        d = meta.get("date", "")[:10]
        if not d or d < cutoff:
            # Try extracting date from the text itself
            text = item.get("text", "")
            for token in text.split():
                try:
                    date.fromisoformat(token[:10])
                    d = token[:10]
                    break
                except ValueError:
                    continue
        if d and d >= cutoff:
            counts[d] += 1

    log(f"Calendar: {sum(counts.values())} events across {len(counts)} day(s)")
    return dict(counts)


def get_email_volume_by_date(days: int) -> dict:
    """Count email_archive memories per date from vector memory.

    Returns: {date_str: email_count}
    """
    results = _recall("email message", source="email_archive", n=50)
    cutoff = (TODAY - timedelta(days=days)).isoformat()
    counts = defaultdict(int)

    for item in results:
        meta = item.get("metadata", {})
        d = meta.get("date", meta.get("received", ""))[:10]
        if not d or d < cutoff:
            text = item.get("text", "")
            for token in text.split():
                try:
                    date.fromisoformat(token[:10])
                    d = token[:10]
                    break
                except ValueError:
                    continue
        if d and d >= cutoff:
            counts[d] += 1

    log(f"Email: {sum(counts.values())} messages across {len(counts)} day(s)")
    return dict(counts)


def get_coding_activity_by_date(days: int) -> dict:
    """Count github memories per date from vector memory.

    Returns: {date_str: activity_count}
    """
    results = _recall("commit push code", source="github", n=50)
    cutoff = (TODAY - timedelta(days=days)).isoformat()
    counts = defaultdict(int)

    for item in results:
        meta = item.get("metadata", {})
        d = meta.get("date", "")[:10]
        if not d or d < cutoff:
            text = item.get("text", "")
            for token in text.split():
                try:
                    date.fromisoformat(token[:10])
                    d = token[:10]
                    break
                except ValueError:
                    continue
        if d and d >= cutoff:
            counts[d] += 1

    log(f"Coding: {sum(counts.values())} activities across {len(counts)} day(s)")
    return dict(counts)


# ── Correlation analysis ─────────────────────────────────────────────────────

def _safe_avg(values: list) -> float:
    """Return mean of values, or 0.0 if empty."""
    return statistics.mean(values) if values else 0.0


def _classify_day(day_str: str) -> str:
    """Return 'weekend' or 'weekday'."""
    return "weekend" if date.fromisoformat(day_str).weekday() >= 5 else "weekday"


def correlate_sleep_vs_meetings(health: dict, calendar: dict) -> dict | None:
    """Sleep vs meeting density: more meetings = less sleep?"""
    high_meeting_sleep = []
    low_meeting_sleep = []

    if not calendar:
        return None

    # Determine meeting-heavy vs light days (split at median)
    meeting_counts = list(calendar.values())
    if len(meeting_counts) < 2:
        return None
    median_meetings = statistics.median(meeting_counts)

    for day, metrics in health.items():
        if "sleep_hours" not in metrics:
            continue
        sleep = metrics["sleep_hours"]
        meetings = calendar.get(day, 0)

        if meetings > median_meetings:
            high_meeting_sleep.append(sleep)
        else:
            low_meeting_sleep.append(sleep)

    if not high_meeting_sleep or not low_meeting_sleep:
        return None

    avg_high = _safe_avg(high_meeting_sleep)
    avg_low = _safe_avg(low_meeting_sleep)
    diff = avg_low - avg_high

    if abs(diff) < 0.3:
        return None

    direction = "less" if diff > 0 else "more"
    return {
        "title": "Sleep vs Meeting Density",
        "finding": (
            f"On meeting-heavy days (>{median_meetings:.0f} events), "
            f"you sleep *{abs(diff):.1f}h {direction}* "
            f"({avg_high:.1f}h vs {avg_low:.1f}h on lighter days)"
        ),
        "high_meeting_avg": round(avg_high, 1),
        "low_meeting_avg": round(avg_low, 1),
        "diff_hours": round(diff, 1),
        "n_high": len(high_meeting_sleep),
        "n_low": len(low_meeting_sleep),
    }


def correlate_hr_vs_meetings(health: dict, calendar: dict) -> dict | None:
    """Resting HR vs meeting days: stress indicator."""
    meeting_hr = []
    no_meeting_hr = []

    for day, metrics in health.items():
        if "resting_heart_rate" not in metrics:
            continue
        hr = metrics["resting_heart_rate"]
        meetings = calendar.get(day, 0)

        if meetings > 0:
            meeting_hr.append(hr)
        else:
            no_meeting_hr.append(hr)

    if not meeting_hr or not no_meeting_hr:
        return None

    avg_meeting = _safe_avg(meeting_hr)
    avg_none = _safe_avg(no_meeting_hr)
    diff = avg_meeting - avg_none

    if abs(diff) < 2.0:
        return None

    direction = "higher" if diff > 0 else "lower"
    return {
        "title": "Resting HR vs Meeting Days",
        "finding": (
            f"Resting HR is *{abs(diff):.1f} bpm {direction}* on meeting days "
            f"({avg_meeting:.0f} vs {avg_none:.0f} bpm) — "
            f"{'possible stress signal' if diff > 0 else 'meetings may not be stressful'}"
        ),
        "meeting_avg": round(avg_meeting, 1),
        "no_meeting_avg": round(avg_none, 1),
        "diff_bpm": round(diff, 1),
        "n_meeting": len(meeting_hr),
        "n_no_meeting": len(no_meeting_hr),
    }


def correlate_hrv_weekday_weekend(health: dict) -> dict | None:
    """HRV weekend vs weekday: recovery pattern."""
    weekend_hrv = []
    weekday_hrv = []

    for day, metrics in health.items():
        if "hrv" not in metrics:
            continue
        hrv_val = metrics["hrv"]

        if _classify_day(day) == "weekend":
            weekend_hrv.append(hrv_val)
        else:
            weekday_hrv.append(hrv_val)

    if not weekend_hrv or not weekday_hrv:
        return None

    avg_weekend = _safe_avg(weekend_hrv)
    avg_weekday = _safe_avg(weekday_hrv)
    diff = avg_weekend - avg_weekday

    if abs(diff) < 2.0:
        return None

    recovery = "better" if diff > 0 else "worse"
    return {
        "title": "HRV: Weekend vs Weekday",
        "finding": (
            f"HRV is *{abs(diff):.1f} ms {recovery}* on weekends "
            f"({avg_weekend:.0f} ms vs {avg_weekday:.0f} ms weekdays) — "
            f"{'weekends aid recovery' if diff > 0 else 'weekends may not be restful'}"
        ),
        "weekend_avg": round(avg_weekend, 1),
        "weekday_avg": round(avg_weekday, 1),
        "diff_ms": round(diff, 1),
        "n_weekend": len(weekend_hrv),
        "n_weekday": len(weekday_hrv),
    }


def correlate_steps_vs_coding(health: dict, coding: dict) -> dict | None:
    """Steps vs coding days: inverse relationship?"""
    coding_steps = []
    non_coding_steps = []

    for day, metrics in health.items():
        if "steps" not in metrics:
            continue
        steps = metrics["steps"]
        code_count = coding.get(day, 0)

        if code_count > 0:
            coding_steps.append(steps)
        else:
            non_coding_steps.append(steps)

    if not coding_steps or not non_coding_steps:
        return None

    avg_coding = _safe_avg(coding_steps)
    avg_non = _safe_avg(non_coding_steps)
    diff = avg_coding - avg_non

    if abs(diff) < 500:
        return None

    direction = "more" if diff > 0 else "fewer"
    return {
        "title": "Steps vs Coding Days",
        "finding": (
            f"On coding days you take *{abs(diff):.0f} {direction} steps* "
            f"({avg_coding:.0f} vs {avg_non:.0f} on non-coding days)"
            f"{' — get up and walk!' if diff < -1000 else ''}"
        ),
        "coding_avg": round(avg_coding),
        "non_coding_avg": round(avg_non),
        "diff_steps": round(diff),
        "n_coding": len(coding_steps),
        "n_non_coding": len(non_coding_steps),
    }


def correlate_energy_vs_events(health: dict, calendar: dict) -> dict | None:
    """Active energy vs calendar event count."""
    if not calendar:
        return None

    busy_energy = []
    light_energy = []

    meeting_counts = list(calendar.values())
    if len(meeting_counts) < 2:
        return None
    median_events = statistics.median(meeting_counts)

    for day, metrics in health.items():
        if "active_energy" not in metrics:
            continue
        energy = metrics["active_energy"]
        events = calendar.get(day, 0)

        if events > median_events:
            busy_energy.append(energy)
        else:
            light_energy.append(energy)

    if not busy_energy or not light_energy:
        return None

    avg_busy = _safe_avg(busy_energy)
    avg_light = _safe_avg(light_energy)
    diff = avg_busy - avg_light

    if abs(diff) < 30:
        return None

    direction = "more" if diff > 0 else "less"
    return {
        "title": "Active Energy vs Calendar Density",
        "finding": (
            f"On event-heavy days (>{median_events:.0f} events), you burn "
            f"*{abs(diff):.0f} kcal {direction}* active energy "
            f"({avg_busy:.0f} vs {avg_light:.0f} kcal)"
        ),
        "busy_avg": round(avg_busy),
        "light_avg": round(avg_light),
        "diff_kcal": round(diff),
        "n_busy": len(busy_energy),
        "n_light": len(light_energy),
    }


# ── Summary statistics ───────────────────────────────────────────────────────

def compute_summaries(health: dict) -> list:
    """Compute summary stats for each health metric across the period."""
    summaries = []
    for field in HEALTH_FIELDS:
        values = [m[field] for m in health.values() if field in m]
        if not values:
            continue

        label = field.replace("_", " ").title()
        avg = _safe_avg(values)
        lo = min(values)
        hi = max(values)

        # Trend: compare first half vs second half
        half = len(values) // 2
        if half >= 2:
            first_half = _safe_avg(values[:half])
            second_half = _safe_avg(values[half:])
            pct_change = ((second_half - first_half) / first_half * 100) if first_half else 0
            if pct_change > 3:
                arrow = " (trending up)"
            elif pct_change < -3:
                arrow = " (trending down)"
            else:
                arrow = " (stable)"
        else:
            arrow = ""

        units = {
            "sleep_hours": "h",
            "resting_heart_rate": "bpm",
            "hrv": "ms",
            "steps": "",
            "active_energy": "kcal",
        }
        unit = units.get(field, "")
        summaries.append(f"  {label}: avg {avg:.1f}{unit}, range {lo:.0f}-{hi:.0f}{unit}{arrow}")

    return summaries


# ── Report generation ────────────────────────────────────────────────────────

def generate_report(days: int) -> str:
    """Generate the full correlation report and return formatted text."""
    period = "Weekly" if days <= 7 else "Monthly"
    header = f"*Health Correlation Report ({period} -- {TODAY.isoformat()})*"

    health = load_health_data(days)
    if not health:
        return f"{header}\n\n_No health data available for the last {days} days._"

    # Fetch activity data
    calendar = get_calendar_events_by_date(days)
    email = get_email_volume_by_date(days)
    coding = get_coding_activity_by_date(days)

    lines = [header, ""]

    # Summaries
    summaries = compute_summaries(health)
    if summaries:
        lines.append(f"*Vitals Summary ({len(health)} days):*")
        lines.extend(summaries)
        lines.append("")

    # Activity context
    activity_lines = []
    if calendar:
        total_events = sum(calendar.values())
        activity_lines.append(f"  Calendar: {total_events} events across {len(calendar)} day(s)")
    if email:
        total_emails = sum(email.values())
        activity_lines.append(f"  Email: {total_emails} messages across {len(email)} day(s)")
    if coding:
        total_commits = sum(coding.values())
        activity_lines.append(f"  Coding: {total_commits} activities across {len(coding)} day(s)")

    if activity_lines:
        lines.append("*Activity Context:*")
        lines.extend(activity_lines)
        lines.append("")

    # Run correlations
    correlations = []
    finders = [
        lambda: correlate_sleep_vs_meetings(health, calendar),
        lambda: correlate_hr_vs_meetings(health, calendar),
        lambda: correlate_hrv_weekday_weekend(health),
        lambda: correlate_steps_vs_coding(health, coding),
        lambda: correlate_energy_vs_events(health, calendar),
    ]

    for finder in finders:
        try:
            result = finder()
            if result:
                correlations.append(result)
        except Exception as exc:
            log(f"Correlation error: {exc}")

    if correlations:
        lines.append("*Correlations Found:*")
        for c in correlations:
            lines.append(f"  *{c['title']}*")
            lines.append(f"    {c['finding']}")
        lines.append("")
    else:
        lines.append("_No significant correlations detected this period._")
        if len(health) < 5:
            lines.append(f"_Only {len(health)} day(s) of data -- need more for patterns._")
        lines.append("")

    lines.append(f"_Report covers {TODAY - timedelta(days=days)} to {TODAY} "
                 f"({len(health)} day(s) with health data)_")

    return "\n".join(lines), correlations


# ── Slack posting ────────────────────────────────────────────────────────────

def post_to_slack(text: str) -> bool:
    """Post report to Slack #nova-notifications."""
    token = nova_config.slack_bot_token()
    if not token:
        log("No Slack token available -- skipping post")
        return False

    data = json.dumps({
        "channel": SLACK_CHANNEL,
        "text": text,
        "mrkdwn": True,
    }).encode()
    req = urllib.request.Request(
        f"{SLACK_API}/chat.postMessage",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                log("Report posted to Slack #nova-notifications")
                return True
            log(f"Slack error: {result.get('error', 'unknown')}")
            return False
    except Exception as exc:
        log(f"Slack post failed: {exc}")
        return False


# ── Vector memory storage ────────────────────────────────────────────────────

def store_insights(correlations: list, days: int) -> None:
    """Store correlation insights in vector memory."""
    if not correlations:
        return

    period = "weekly" if days <= 7 else "monthly"
    findings = "; ".join(c["finding"].replace("*", "") for c in correlations)
    text = f"Health correlation {period} report ({TODAY.isoformat()}): {findings}"

    payload = json.dumps({
        "text": text,
        "source": "health_correlation",
        "metadata": {
            "privacy": "local-only",
            "date": TODAY.isoformat(),
            "period": period,
            "days_analyzed": days,
            "correlations_found": len(correlations),
            "correlation_titles": [c["title"] for c in correlations],
        },
    }).encode()

    req = urllib.request.Request(
        VECTOR_REMEMBER,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log(f"Stored {len(correlations)} insight(s) in vector memory")
    except Exception as exc:
        log(f"Vector store failed: {exc}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Health Trend Correlation -- cross-reference health with activity data"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--weekly", action="store_true", default=True,
        help="Analyze last 7 days (default)",
    )
    group.add_argument(
        "--monthly", action="store_true",
        help="Analyze last 30 days",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print report to stdout without posting to Slack or storing",
    )
    args = parser.parse_args()

    days = 30 if args.monthly else 7

    log(f"Running {'monthly' if days == 30 else 'weekly'} health correlation analysis...")
    report_text, correlations = generate_report(days)

    print(report_text)

    if not args.dry_run:
        post_to_slack(report_text)
        store_insights(correlations, days)

    log("Done.")


if __name__ == "__main__":
    main()
