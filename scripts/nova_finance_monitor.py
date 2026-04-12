#!/usr/bin/env python3
"""
nova_finance_monitor.py — Financial alert monitoring.

Watches email for bank/credit card alerts:
  - Transaction alerts (charges, payments, refunds)
  - Credit score changes
  - Fraud/security alerts (immediate Slack DM)
  - Bill due dates
  - Statement availability

Categorizes transactions, flags unusual activity, and posts a weekly
financial pulse to Slack.

Stores financial events in a local JSON file (NOT in vector memory
to avoid financial data in the search index).

Cron: every 4 hours (scan), Sunday 9am (weekly digest)
Written by Jordan Koch.
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_CHAN
SLACK_API = nova_config.SLACK_API
JORDAN_DM = nova_config.JORDAN_DM
NOW = datetime.now()
TODAY = date.today().isoformat()
SCRIPTS = Path.home() / ".openclaw" / "scripts"
DATA_FILE = Path.home() / ".openclaw" / "workspace" / "finance_events.json"

# ── Financial email patterns ─────────────────────────────────────────────────

FINANCIAL_SENDERS = {
    "americanexpress": "Amex",
    "amex": "Amex",
    "wellsfargo": "Wells Fargo",
    "wells fargo": "Wells Fargo",
    "partnersfcu": "Partners FCU",
    "partners federal": "Partners FCU",
    "chase": "Chase",
    "citi": "Citi",
    "capitalone": "Capital One",
    "capital one": "Capital One",
    "discover": "Discover",
    "venmo": "Venmo",
    "paypal": "PayPal",
    "zelle": "Zelle",
}

# High-priority patterns that get immediate DM alerts
URGENT_PATTERNS = [
    r"fraud",
    r"unauthorized",
    r"suspicious\s+activity",
    r"security\s+alert",
    r"account\s+locked",
    r"unusual\s+activity",
    r"declined.*unusual",
    r"verify\s+your\s+identity",
]

# Transaction amount extraction
AMOUNT_PATTERN = re.compile(r"\$[\d,]+\.?\d{0,2}")

# Subject patterns for categorization
CATEGORY_PATTERNS = {
    "charge": [r"charge", r"transaction", r"purchase", r"payment\s+of\s+\$", r"spent"],
    "payment": [r"payment\s+received", r"payment\s+posted", r"payment\s+confirmed", r"autopay"],
    "refund": [r"refund", r"credit\s+posted", r"cashback"],
    "credit_score": [r"credit\s+score", r"fico", r"credit\s+report"],
    "bill_due": [r"bill\s+is\s+due", r"payment\s+due", r"statement\s+ready", r"minimum\s+payment"],
    "transfer": [r"transfer", r"sent\s+you", r"you\s+sent", r"deposit"],
    "alert": [r"alert", r"notification", r"important\s+update"],
}


def log(msg):
    print(f"[nova_finance {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text, channel=None):
    import urllib.request
    data = json.dumps({
        "channel": channel or SLACK_CHAN, "text": text, "mrkdwn": True
    }).encode()
    req = urllib.request.Request(
        f"{nova_config.SLACK_API}/chat.postMessage", data=data,
        headers={"Authorization": "Bearer " + SLACK_TOKEN,
                 "Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        log(f"Slack error: {e}")


# ── Data persistence ─────────────────────────────────────────────────────────

def load_data():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {"events": [], "last_scan": "", "weekly_summary_date": ""}


def save_data(data):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Keep only last 90 days of events
    cutoff = (NOW - timedelta(days=90)).isoformat()
    data["events"] = [e for e in data.get("events", []) if e.get("date", "") > cutoff]
    DATA_FILE.write_text(json.dumps(data, indent=2))


# ── Email scanning ───────────────────────────────────────────────────────────

def get_mail_data():
    summary_file = Path("/tmp/nova_mail_fetch.txt")
    if summary_file.exists():
        age_hours = (time.time() - summary_file.stat().st_mtime) / 3600
        if age_hours < 24:
            return summary_file.read_text(encoding="utf-8")
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


def detect_institution(sender, subject):
    """Detect financial institution from sender/subject."""
    combined = (sender + " " + subject).lower()
    for keyword, name in FINANCIAL_SENDERS.items():
        if keyword in combined:
            return name
    return None


def categorize_email(subject):
    """Categorize a financial email by its subject."""
    subj_lower = subject.lower()
    for category, patterns in CATEGORY_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, subj_lower):
                return category
    return "other"


def extract_amount(text):
    """Extract dollar amount from text."""
    match = AMOUNT_PATTERN.search(text)
    if match:
        amount_str = match.group().replace("$", "").replace(",", "")
        try:
            return float(amount_str)
        except ValueError:
            pass
    return None


def is_urgent(subject):
    """Check if this is a fraud/security alert requiring immediate attention."""
    subj_lower = subject.lower()
    return any(re.search(pat, subj_lower) for pat in URGENT_PATTERNS)


def scan_financial_emails():
    """Scan emails for financial alerts. Returns list of event dicts."""
    content = get_mail_data()
    if not content:
        return []

    events = []
    current_from = ""
    current_subject = ""

    for line in content.splitlines():
        line = line.strip()
        if "FROM:" in line:
            current_from = re.sub(r"\[(UN)?READ\]\s*FROM:\s*", "", line).strip()
        elif line.startswith("SUBJ:"):
            current_subject = line[5:].strip()

            institution = detect_institution(current_from, current_subject)
            if institution:
                category = categorize_email(current_subject)
                amount = extract_amount(current_subject)
                urgent = is_urgent(current_subject)

                event = {
                    "date": TODAY,
                    "time": NOW.strftime("%H:%M"),
                    "institution": institution,
                    "category": category,
                    "subject": current_subject[:120],
                    "amount": amount,
                    "urgent": urgent,
                    "sender": current_from[:80],
                }
                events.append(event)

            current_from = ""
            current_subject = ""

    return events


# ── Alert handling ───────────────────────────────────────────────────────────

CATEGORY_ICONS = {
    "charge": "💳",
    "payment": "💰",
    "refund": "↩️",
    "credit_score": "📊",
    "bill_due": "📅",
    "transfer": "🔄",
    "alert": "🔔",
    "other": "📋",
}


def main():
    log("Scanning for financial alerts...")
    data = load_data()
    existing_keys = {f"{e['date']}_{e['subject'][:50]}" for e in data.get("events", [])}

    new_events = []
    urgent_alerts = []

    for event in scan_financial_emails():
        key = f"{event['date']}_{event['subject'][:50]}"
        if key in existing_keys:
            continue
        new_events.append(event)
        data.setdefault("events", []).append(event)
        if event["urgent"]:
            urgent_alerts.append(event)

    data["last_scan"] = NOW.isoformat()
    save_data(data)

    # ── Urgent alerts go to DM immediately ───────────────────────────────────
    if urgent_alerts:
        lines = ["*FINANCIAL SECURITY ALERT*"]
        for a in urgent_alerts:
            amount_str = f" (${a['amount']:.2f})" if a.get("amount") else ""
            lines.append(f"  {a['institution']}: *{a['subject'][:80]}*{amount_str}")
        lines.append("\n_Review this immediately._")
        slack_post("\n".join(lines), channel=JORDAN_DM)
        log(f"Sent {len(urgent_alerts)} urgent alert(s) to DM")

    # ── Regular financial activity to #nova-chat ─────────────────────────────
    non_urgent = [e for e in new_events if not e["urgent"]]
    if non_urgent:
        lines = [f"*Financial Activity — {NOW.strftime('%I:%M %p')}*"]
        for event in non_urgent[:10]:
            icon = CATEGORY_ICONS.get(event["category"], "📋")
            amount_str = f" ${event['amount']:.2f}" if event.get("amount") else ""
            lines.append(f"  {icon} [{event['institution']}] {event['subject'][:60]}{amount_str}")
        slack_post("\n".join(lines))
        log(f"Posted {len(non_urgent)} financial event(s)")

    if not new_events:
        log("No new financial events.")


# ── Weekly digest ────────────────────────────────────────────────────────────

def weekly_digest():
    """Generate weekly financial summary."""
    data = load_data()
    events = data.get("events", [])

    # Events from last 7 days
    week_ago = (NOW - timedelta(days=7)).isoformat()
    week_events = [e for e in events if e.get("date", "") >= week_ago[:10]]

    if not week_events:
        return "*Weekly Financial Pulse*\n  _No financial activity this week._"

    # Categorize
    by_institution = {}
    by_category = {}
    total_charges = 0
    total_payments = 0
    total_refunds = 0

    for e in week_events:
        inst = e.get("institution", "Unknown")
        cat = e.get("category", "other")
        amount = e.get("amount", 0)

        by_institution.setdefault(inst, []).append(e)
        by_category.setdefault(cat, []).append(e)

        if cat == "charge" and amount:
            total_charges += amount
        elif cat == "payment" and amount:
            total_payments += amount
        elif cat == "refund" and amount:
            total_refunds += amount

    lines = [
        f"*Weekly Financial Pulse — Week of {(NOW - timedelta(days=7)).strftime('%b %d')}*",
        "",
    ]

    # Summary numbers
    if total_charges > 0:
        lines.append(f"  💳 Total charges: *${total_charges:,.2f}*")
    if total_payments > 0:
        lines.append(f"  💰 Total payments: *${total_payments:,.2f}*")
    if total_refunds > 0:
        lines.append(f"  ↩️ Total refunds: *${total_refunds:,.2f}*")
    lines.append("")

    # By institution
    lines.append("*By institution:*")
    for inst, inst_events in sorted(by_institution.items()):
        inst_total = sum(e.get("amount", 0) for e in inst_events if e.get("amount"))
        count = len(inst_events)
        if inst_total > 0:
            lines.append(f"  {inst}: {count} event(s), ${inst_total:,.2f}")
        else:
            lines.append(f"  {inst}: {count} event(s)")

    # Notable items
    credit_score = [e for e in week_events if e.get("category") == "credit_score"]
    if credit_score:
        lines.append("")
        lines.append("*Credit score updates:*")
        for cs in credit_score:
            lines.append(f"  📊 {cs.get('subject', '')[:70]}")

    bill_due = [e for e in week_events if e.get("category") == "bill_due"]
    if bill_due:
        lines.append("")
        lines.append("*Bills:*")
        for bd in bill_due:
            amount_str = f" ${bd['amount']:.2f}" if bd.get("amount") else ""
            lines.append(f"  📅 [{bd.get('institution', '?')}] {bd.get('subject', '')[:50]}{amount_str}")

    lines.append(f"\n_{len(week_events)} total financial events this week_")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Finance Monitor")
    parser.add_argument("--scan", action="store_true", help="Scan for new financial emails (default)")
    parser.add_argument("--weekly", action="store_true", help="Post weekly digest to Slack")
    parser.add_argument("--digest", action="store_true", help="Print weekly digest to stdout")
    args = parser.parse_args()

    if args.weekly:
        text = weekly_digest()
        slack_post(text)
        log("Weekly digest posted.")
    elif args.digest:
        print(weekly_digest())
    else:
        main()
