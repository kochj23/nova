#!/usr/bin/env python3
"""
nova_package_tracker.py — Dedicated package tracking with carrier status lookups.

Goes beyond the nightly report's basic email parsing:
  1. Extracts tracking numbers from email subjects/bodies
  2. Checks carrier tracking APIs for real-time status
  3. Deduplicates and tracks state changes (shipped → in transit → delivered)
  4. Posts updates to Slack when status changes
  5. Stores tracking history in a local JSON file

Uses free/public carrier tracking endpoints where available.
Falls back to email-subject-based status when APIs aren't available.

Cron: every 2 hours
Written by Jordan Koch.
"""

import json
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

VECTOR_URL = nova_config.VECTOR_URL
NOW = datetime.now()
TODAY = date.today().isoformat()
SCRIPTS = Path.home() / ".openclaw" / "scripts"
DATA_FILE = Path.home() / ".openclaw" / "workspace" / "package_tracking.json"

# ── Carrier regex patterns ───────────────────────────────────────────────────

CARRIER_PATTERNS = {
    "USPS": [
        re.compile(r"\b(9[2345]\d{18,21})\b"),
        re.compile(r"\b(420\d{5}9[2345]\d{18,21})\b"),
        re.compile(r"\b(82\d{8})\b"),
    ],
    "UPS": [
        re.compile(r"\b(1Z[A-Z0-9]{16})\b"),
    ],
    "FedEx": [
        re.compile(r"\b(\d{20})\b"),
        re.compile(r"\b(\d{15})\b"),
        re.compile(r"\b(7489\d{8})\b"),
    ],
    "Amazon": [
        re.compile(r"\b(TBA\d{10,12})\b"),
    ],
}

# Keywords that indicate package emails
PACKAGE_KEYWORDS = [
    "shipped", "shipment", "delivery", "delivering", "delivered",
    "package", "tracking", "arriving", "out for delivery", "in transit",
    "order confirmed", "order shipped", "your order",
]

CARRIER_KEYWORDS = {
    "usps": "USPS",
    "fedex": "FedEx",
    "ups.com": "UPS",
    "united parcel": "UPS",
    "amazon": "Amazon",
    "tougher than tom": "Amazon",
}

STATUS_ORDER = ["ordered", "shipped", "in_transit", "out_for_delivery", "delivered"]


def log(msg):
    print(f"[nova_package_tracker {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text, channel=None):
    nova_config.post_both(text, slack_channel=channel or nova_config.SLACK_NOTIFY)


def vector_remember(text, metadata=None):
    try:
        payload = json.dumps({
            "text": text, "source": "package_tracker", "metadata": metadata or {}
        }).encode()
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ── Data persistence ─────────────────────────────────────────────────────────

def load_tracking_data():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {"packages": {}, "last_scan": ""}


def save_tracking_data(data):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=2))


# ── Email scanning ───────────────────────────────────────────────────────────

def get_mail_data():
    """Get cached mail data from the nightly report's cache."""
    summary_file = Path.home() / ".openclaw/workspace/state/nova_mail_fetch.txt"
    if summary_file.exists():
        age_hours = (time.time() - summary_file.stat().st_mtime) / 3600
        if age_hours < 24:
            return summary_file.read_text(encoding="utf-8")
    # Try refreshing
    try:
        subprocess.run(
            ["python3", str(SCRIPTS / "nova_mail_fetch.py")],
            capture_output=True, text=True, timeout=150
        )
        if summary_file.exists():
            return summary_file.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def extract_tracking_numbers(text):
    """Extract tracking numbers from text, return list of (carrier, number)."""
    found = []
    seen = set()
    for carrier, patterns in CARRIER_PATTERNS.items():
        for pattern in patterns:
            for match in pattern.finditer(text):
                num = match.group(1)
                if num not in seen and len(num) >= 10:
                    seen.add(num)
                    found.append((carrier, num))
    return found


def detect_carrier_from_email(sender, subject):
    """Detect carrier from email sender/subject."""
    combined = (sender + " " + subject).lower()
    for keyword, carrier in CARRIER_KEYWORDS.items():
        if keyword in combined:
            return carrier
    return "Unknown"


def infer_status_from_subject(subject):
    """Infer package status from email subject line."""
    subj = subject.lower()
    if any(w in subj for w in ["delivered", "arrived"]):
        return "delivered"
    elif any(w in subj for w in ["out for delivery", "arriving today"]):
        return "out_for_delivery"
    elif any(w in subj for w in ["in transit", "on the way", "on its way"]):
        return "in_transit"
    elif any(w in subj for w in ["shipped", "shipment", "has shipped"]):
        return "shipped"
    elif any(w in subj for w in ["order confirmed", "order placed"]):
        return "ordered"
    return "shipped"


def scan_emails_for_packages():
    """Scan email data for package-related messages. Returns list of package dicts."""
    content = get_mail_data()
    if not content:
        return []

    packages = []
    current_from = ""
    current_subject = ""

    for line in content.splitlines():
        line = line.strip()
        if ("FROM:" in line):
            current_from = re.sub(r"\[(UN)?READ\]\s*FROM:\s*", "", line).strip()
        elif line.startswith("SUBJ:"):
            current_subject = line[5:].strip()
            combined = (current_from + " " + current_subject).lower()

            is_package = any(kw in combined for kw in PACKAGE_KEYWORDS)
            if is_package and current_subject:
                carrier = detect_carrier_from_email(current_from, current_subject)
                status = infer_status_from_subject(current_subject)

                # Try to extract tracking number
                tracking_nums = extract_tracking_numbers(current_subject + " " + current_from)

                pkg = {
                    "subject": current_subject[:100],
                    "sender": current_from[:80],
                    "carrier": carrier if tracking_nums else detect_carrier_from_email(current_from, current_subject),
                    "status": status,
                    "tracking": tracking_nums[0][1] if tracking_nums else None,
                    "last_seen": NOW.isoformat(),
                }
                if tracking_nums:
                    pkg["carrier"] = tracking_nums[0][0]

                packages.append(pkg)

            current_from = ""
            current_subject = ""

    return packages


# ── Carrier status checking ──────────────────────────────────────────────────

def check_usps_status(tracking_number):
    """Check USPS tracking via their public tools endpoint."""
    try:
        url = f"https://tools.usps.com/go/TrackConfirmAction?tLabels={tracking_number}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="ignore")
            # Look for status in the response
            if "Delivered" in html:
                return "delivered"
            elif "Out for Delivery" in html:
                return "out_for_delivery"
            elif "In Transit" in html:
                return "in_transit"
            elif "Accepted" in html or "Shipped" in html:
                return "shipped"
    except Exception:
        pass
    return None


def check_carrier_status(carrier, tracking_number):
    """Check tracking status with carrier. Returns status string or None."""
    if carrier == "USPS":
        return check_usps_status(tracking_number)
    # Other carriers require API keys or scraping — fall back to email status
    return None


# ── Main logic ───────────────────────────────────────────────────────────────

def status_icon(status):
    return {
        "ordered": "🛒",
        "shipped": "📦",
        "in_transit": "🚚",
        "out_for_delivery": "🏃",
        "delivered": "✅",
    }.get(status, "📦")


def status_advanced(old_status, new_status):
    """Check if status has advanced (progressed forward)."""
    try:
        return STATUS_ORDER.index(new_status) > STATUS_ORDER.index(old_status)
    except ValueError:
        return old_status != new_status


def main():
    log("Scanning for packages...")
    data = load_tracking_data()
    existing = data.get("packages", {})
    email_packages = scan_emails_for_packages()

    updates = []
    new_packages = []

    for pkg in email_packages:
        # Use tracking number as key, or subject hash as fallback
        key = pkg.get("tracking") or str(hash(pkg["subject"]))[:12]

        if key in existing:
            old = existing[key]
            old_status = old.get("status", "")

            # Check carrier API for real status if we have a tracking number
            if pkg.get("tracking"):
                api_status = check_carrier_status(pkg["carrier"], pkg["tracking"])
                if api_status:
                    pkg["status"] = api_status

            if status_advanced(old_status, pkg["status"]):
                updates.append({
                    "key": key,
                    "subject": pkg["subject"],
                    "old_status": old_status,
                    "new_status": pkg["status"],
                    "carrier": pkg["carrier"],
                })
            existing[key] = {**old, **pkg}
        else:
            new_packages.append(pkg)
            existing[key] = pkg

    # Prune packages older than 14 days
    cutoff = (NOW - timedelta(days=14)).isoformat()
    existing = {k: v for k, v in existing.items()
                if v.get("last_seen", "") > cutoff or v.get("status") != "delivered"}

    data["packages"] = existing
    data["last_scan"] = NOW.isoformat()
    save_tracking_data(data)

    # ── Build Slack message if there are updates ─────────────────────────────
    if updates or new_packages:
        lines = [f"*Package Update — {NOW.strftime('%I:%M %p')}*"]

        if new_packages:
            lines.append("")
            lines.append("*New packages detected:*")
            for pkg in new_packages[:8]:
                icon = status_icon(pkg["status"])
                lines.append(f"  {icon} [{pkg['carrier']}] {pkg['subject'][:60]}")

        if updates:
            lines.append("")
            lines.append("*Status changes:*")
            for u in updates:
                old_icon = status_icon(u["old_status"])
                new_icon = status_icon(u["new_status"])
                lines.append(f"  {old_icon} -> {new_icon} [{u['carrier']}] {u['subject'][:50]}")

        slack_post("\n".join(lines))
        log(f"Posted {len(new_packages)} new, {len(updates)} updates")

        # Vector memory
        summary_parts = []
        if new_packages:
            summary_parts.append(f"{len(new_packages)} new packages detected")
        if updates:
            for u in updates:
                summary_parts.append(f"{u['subject'][:40]}: {u['old_status']} -> {u['new_status']}")
        vector_remember(
            f"Package tracking {TODAY}: " + "; ".join(summary_parts),
            {"date": TODAY, "type": "package_update"}
        )
    else:
        log(f"No changes. Tracking {len(existing)} active packages.")


# ── Digest (for manual/morning brief use) ───────────────────────────────────

def digest():
    """Return a text summary of all active packages."""
    data = load_tracking_data()
    packages = data.get("packages", {})

    active = {k: v for k, v in packages.items() if v.get("status") != "delivered"}
    delivered_today = {k: v for k, v in packages.items()
                       if v.get("status") == "delivered" and TODAY in v.get("last_seen", "")}

    lines = [f"*Package Tracker — {len(active)} active, {len(delivered_today)} delivered today*"]

    if not active and not delivered_today:
        lines.append("  _No packages being tracked._")
        return "\n".join(lines)

    for key, pkg in sorted(active.items(), key=lambda x: STATUS_ORDER.index(x[1].get("status", "shipped"))
                           if x[1].get("status") in STATUS_ORDER else 0, reverse=True):
        icon = status_icon(pkg.get("status", "shipped"))
        carrier = pkg.get("carrier", "?")
        subj = pkg.get("subject", "Unknown")[:55]
        lines.append(f"  {icon} [{carrier}] {subj}")

    if delivered_today:
        lines.append("")
        lines.append("*Delivered today:*")
        for key, pkg in delivered_today.items():
            lines.append(f"  ✅ [{pkg.get('carrier', '?')}] {pkg.get('subject', '?')[:55]}")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Package Tracker")
    parser.add_argument("--digest", action="store_true", help="Print package digest")
    parser.add_argument("--scan", action="store_true", help="Scan emails and check carriers (default)")
    parser.add_argument("--reset", action="store_true", help="Clear all tracking data")
    args = parser.parse_args()

    if args.digest:
        print(digest())
    elif args.reset:
        DATA_FILE.unlink(missing_ok=True)
        print("Tracking data cleared.")
    else:
        main()
