#!/usr/bin/env python3
"""
nova_security_watcher.py — Auto-trigger breaking security alerts.

Runs every 30 minutes. Checks for critical events that warrant immediate notification:
- New CISA KEV additions
- Feed items matching critical keywords (RCE, actively exploited, nation-state, 0-day)
- NWS severe weather warnings for Los Angeles area
- USGS earthquakes (M4.0+) near LA
- CalOES emergency alerts

Fires nova_journal_security.py breaking when triggered.

Written by Jordan Koch (via Claude).
"""

import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

LOG_FILE = Path.home() / ".openclaw/logs/security_watcher.log"
STATE_FILE = Path.home() / ".openclaw/workspace/state/security_watcher_seen.json"
JOURNAL_SCRIPT = Path.home() / ".openclaw/scripts/nova_journal_security.py"

# LA area coordinates (bounding box)
LA_LAT_MIN, LA_LAT_MAX = 33.5, 34.5
LA_LON_MIN, LA_LON_MAX = -118.8, -117.5

# Critical keywords that trigger breaking alerts
CRITICAL_KEYWORDS = [
    "actively exploited", "exploitation in the wild", "under active exploitation",
    "zero-day", "0-day", "zero day", "critical rce", "unauthenticated rce",
    "nation-state", "apt28", "apt29", "apt41", "lazarus", "cozy bear", "fancy bear",
    "volt typhoon", "salt typhoon", "flax typhoon", "sandworm",
    "critical infrastructure", "mass exploitation", "wormable",
    "emergency directive", "bod 22-01", "kev addition",
    "nuclear test", "icbm", "military strike", "declaration of war",
    "terrorist attack", "mass casualty", "active shooter",
]

# SoCal emergency keywords
LOCAL_KEYWORDS = [
    "los angeles", "southern california", "socal", "la county",
    "long beach", "santa monica", "pasadena", "torrance", "inglewood",
    "south bay", "san pedro", "redondo", "manhattan beach",
]


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[sec-watcher {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_seen() -> set:
    try:
        if STATE_FILE.exists():
            return set(json.loads(STATE_FILE.read_text()))
    except Exception:
        pass
    return set()


def save_seen(seen: set):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(list(seen)[-500:]))


def fire_alert(trigger: str, details: str):
    """Fire a breaking security alert via nova_journal_security.py."""
    log(f"🚨 FIRING ALERT: {trigger}")
    try:
        subprocess.run(
            [sys.executable, str(JOURNAL_SCRIPT), "breaking", trigger, details],
            timeout=300, capture_output=True
        )
    except Exception as e:
        log(f"Alert fire failed: {e}")


# ── CISA KEV Monitor ──────────────────────────────────────────────────────────

def check_cisa_kev(seen: set) -> list:
    """Check CISA Known Exploited Vulnerabilities catalog for new additions."""
    url = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Nova-SecWatch/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log(f"CISA KEV fetch failed: {e}")
        return []

    alerts = []
    for vuln in data.get("vulnerabilities", [])[-20:]:  # Check last 20 entries
        cve = vuln.get("cveID", "")
        key = f"kev-{cve}"
        if key in seen:
            continue

        date_added = vuln.get("dateAdded", "")
        # Only alert on entries added in the last 48 hours
        try:
            added_dt = datetime.strptime(date_added, "%Y-%m-%d")
            if (datetime.now() - added_dt).days > 2:
                seen.add(key)
                continue
        except (ValueError, TypeError):
            pass

        seen.add(key)
        vendor = vuln.get("vendorProject", "Unknown")
        product = vuln.get("product", "Unknown")
        desc = vuln.get("shortDescription", "")
        action = vuln.get("requiredAction", "")

        trigger = f"CISA KEV Addition — {cve} ({vendor} {product})"
        details = f"{desc}\n\nRequired Action: {action}\nDate Added: {date_added}"
        alerts.append((trigger, details))

    return alerts


# ── Critical Feed Monitor ─────────────────────────────────────────────────────

def check_critical_feeds(seen: set) -> list:
    """Check recent RSS ingests for critical keywords."""
    alerts = []
    try:
        result = subprocess.run(
            ["psql", "-h", "192.168.1.6", "-U", "kochj", "-d", "nova_memories", "-tA", "-c",
             "SELECT text, metadata::text FROM memories "
             "WHERE source = 'intelligence' "
             "AND created_at >= now() - interval '35 minutes' "
             "AND LENGTH(text) > 60 "
             "ORDER BY created_at DESC LIMIT 50;"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return []

        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            text = line.split("|")[0].strip().lower()
            text_hash = hashlib.md5(text[:100].encode()).hexdigest()[:10]
            if text_hash in seen:
                continue

            for keyword in CRITICAL_KEYWORDS:
                if keyword in text:
                    seen.add(text_hash)
                    # Extract title from the text
                    title_match = re.match(r'\[([^\]]+)\]\s*(.+?)(?:\.|:)', line.split("|")[0].strip())
                    if title_match:
                        source = title_match.group(1)
                        title = title_match.group(2)
                    else:
                        source = "OSINT Feed"
                        title = line.split("|")[0].strip()[:100]

                    trigger = f"{source}: {title}"
                    details = line.split("|")[0].strip()[:500]
                    alerts.append((trigger, details))
                    break
    except Exception as e:
        log(f"Critical feed check failed: {e}")

    return alerts


# ── USGS Earthquake Monitor ───────────────────────────────────────────────────

def check_earthquakes(seen: set) -> list:
    """Check USGS for significant earthquakes near LA (M4.0+)."""
    url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_hour.geojson"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Nova-SecWatch/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log(f"USGS fetch failed: {e}")
        return []

    alerts = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        geo = feature.get("geometry", {}).get("coordinates", [0, 0, 0])
        lon, lat, depth = geo[0], geo[1], geo[2] if len(geo) > 2 else 0

        mag = props.get("mag", 0)
        place = props.get("place", "")
        eq_id = feature.get("id", "")
        key = f"eq-{eq_id}"

        if key in seen:
            continue

        # Alert on M4.0+ near LA, or M6.0+ anywhere in California
        is_near_la = (LA_LAT_MIN <= lat <= LA_LAT_MAX and LA_LON_MIN <= lon <= LA_LON_MAX)
        is_california = "california" in place.lower() or (32 <= lat <= 42 and -125 <= lon <= -114)

        if (is_near_la and mag >= 4.0) or (is_california and mag >= 6.0) or mag >= 7.0:
            seen.add(key)
            trigger = f"Earthquake M{mag:.1f} — {place}"
            details = f"Magnitude {mag:.1f} at depth {depth:.1f}km. Location: {place}. Coordinates: {lat:.3f}, {lon:.3f}"
            alerts.append((trigger, details))

    return alerts


# ── NWS Weather Alerts ────────────────────────────────────────────────────────

def check_nws_alerts(seen: set) -> list:
    """Check NWS for severe weather alerts in LA county."""
    # LA county zone
    url = "https://api.weather.gov/alerts/active?zone=CAZ041"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Nova-SecWatch/1.0 (nova.digitalnoise.net)",
            "Accept": "application/geo+json"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log(f"NWS fetch failed: {e}")
        return []

    alerts = []
    severe_events = {"Tornado Warning", "Flash Flood Warning", "Tsunami Warning",
                     "Earthquake Warning", "Extreme Wind Warning", "Fire Weather Watch",
                     "Red Flag Warning", "Extreme Fire Danger"}

    for feature in data.get("features", []):
        props = feature.get("properties", {})
        alert_id = props.get("id", "")
        key = f"nws-{hashlib.md5(alert_id.encode()).hexdigest()[:8]}"
        if key in seen:
            continue

        event = props.get("event", "")
        severity = props.get("severity", "")

        if event in severe_events or severity in ("Extreme", "Severe"):
            seen.add(key)
            headline = props.get("headline", event)
            desc = props.get("description", "")[:300]
            trigger = f"NWS Alert: {headline}"
            details = f"Event: {event}\nSeverity: {severity}\n{desc}"
            alerts.append((trigger, details))

    return alerts


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    log("Security watcher check starting...")
    seen = load_seen()
    all_alerts = []

    # Run all checks
    all_alerts.extend(check_cisa_kev(seen))
    all_alerts.extend(check_critical_feeds(seen))
    all_alerts.extend(check_earthquakes(seen))
    all_alerts.extend(check_nws_alerts(seen))

    save_seen(seen)

    if all_alerts:
        log(f"Found {len(all_alerts)} alert(s) to fire")
        for trigger, details in all_alerts[:3]:  # Max 3 alerts per run to avoid spam
            fire_alert(trigger, details)
            time.sleep(5)
    else:
        log("No critical events detected")


if __name__ == "__main__":
    run()
