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


# ── Spending pattern analysis ────────────────────────────────────────────────

SPENDING_CATEGORIES = {
    "dining":       ["restaurant", "grubhub", "doordash", "uber eats", "starbucks", "coffee",
                     "pizza", "sushi", "taco", "burger", "dine"],
    "shopping":     ["amazon", "target", "walmart", "costco", "best buy", "apple.com",
                     "etsy", "ebay", "wayfair"],
    "subscriptions":["netflix", "hulu", "spotify", "apple music", "youtube premium",
                     "adobe", "github", "openai", "anthropic", "patreon", "subscription"],
    "auto":         ["gas", "fuel", "shell", "chevron", "arco", "car wash", "auto",
                     "parking", "geico", "insurance"],
    "utilities":    ["edison", "water", "power", "electric", "internet", "at&t", "verizon",
                     "t-mobile", "comcast", "spectrum"],
    "health":       ["pharmacy", "cvs", "walgreens", "doctor", "medical", "dental",
                     "copay", "insurance", "health"],
    "home":         ["home depot", "lowes", "hardware", "plumbing", "repair"],
}


def categorize_spending(subject, sender):
    """Auto-categorize a transaction into spending categories."""
    combined = (subject + " " + sender).lower()
    for category, keywords in SPENDING_CATEGORIES.items():
        if any(kw in combined for kw in keywords):
            return category
    return "other"


def spending_analysis(days=30):
    """Analyze spending patterns over N days."""
    data = load_data()
    events = data.get("events", [])
    cutoff = (NOW - timedelta(days=days)).isoformat()[:10]

    charges = [e for e in events
               if e.get("category") == "charge"
               and e.get("amount", 0) > 0
               and e.get("date", "") >= cutoff]

    if not charges:
        return f"*Spending Analysis — Last {days} Days*\n  _No charge data available._"

    # Categorize
    by_category = {}
    by_week = {}
    total = 0

    for c in charges:
        amount = c.get("amount", 0)
        total += amount
        spend_cat = categorize_spending(c.get("subject", ""), c.get("sender", ""))
        by_category.setdefault(spend_cat, []).append(amount)

        # Weekly bucketing
        week = c.get("date", "")[:10]  # Just use date as key for now
        by_week.setdefault(week, 0)
        by_week[week] += amount

    lines = [
        f"*Spending Analysis — Last {days} Days*",
        f"  Total: *${total:,.2f}* across {len(charges)} transactions",
        f"  Daily average: *${total/days:,.2f}*",
        "",
        "*By category:*",
    ]

    for cat, amounts in sorted(by_category.items(), key=lambda x: sum(x[1]), reverse=True):
        cat_total = sum(amounts)
        pct = (cat_total / total * 100) if total > 0 else 0
        lines.append(f"  {cat.title()}: ${cat_total:,.2f} ({pct:.0f}%) — {len(amounts)} txns")

    # Trend: compare first half vs second half
    sorted_dates = sorted(by_week.keys())
    if len(sorted_dates) >= 4:
        mid = len(sorted_dates) // 2
        first_half = sum(by_week[d] for d in sorted_dates[:mid])
        second_half = sum(by_week[d] for d in sorted_dates[mid:])
        if first_half > 0:
            change_pct = ((second_half - first_half) / first_half) * 100
            if change_pct > 10:
                lines.append(f"\n_Spending trending UP {change_pct:.0f}% vs earlier period_")
            elif change_pct < -10:
                lines.append(f"\n_Spending trending DOWN {abs(change_pct):.0f}% vs earlier period_")
            else:
                lines.append(f"\n_Spending is stable_")

    # Anomaly detection: flag transactions > 2x daily average
    daily_avg = total / days if days > 0 else 0
    anomalies = [c for c in charges if c.get("amount", 0) > daily_avg * 3 and c.get("amount", 0) > 100]
    if anomalies:
        lines.append("")
        lines.append("*Unusual charges:*")
        for a in anomalies[:5]:
            lines.append(f"  ⚠️ ${a['amount']:.2f} — {a.get('subject', '')[:50]} [{a.get('institution', '')}]")

    return "\n".join(lines)


def cash_flow_forecast():
    """Simple cash flow forecast based on recurring patterns."""
    data = load_data()
    events = data.get("events", [])

    # Look at last 60 days
    cutoff = (NOW - timedelta(days=60)).isoformat()[:10]
    recent = [e for e in events if e.get("date", "") >= cutoff and e.get("amount", 0) > 0]

    charges = [e for e in recent if e.get("category") == "charge"]
    payments = [e for e in recent if e.get("category") == "payment"]

    if not charges:
        return "*Cash Flow Forecast*\n  _Not enough data yet._"

    # Monthly averages
    total_charges = sum(e.get("amount", 0) for e in charges)
    total_payments = sum(e.get("amount", 0) for e in payments)
    months = 2  # 60 days ≈ 2 months

    avg_monthly_out = total_charges / months
    avg_monthly_in = total_payments / months

    # Detect recurring charges (similar amounts from same institution)
    recurring = {}
    for c in charges:
        key = f"{c.get('institution', '')}_{int(c.get('amount', 0))}"
        recurring.setdefault(key, []).append(c)

    likely_recurring = {k: v for k, v in recurring.items() if len(v) >= 2}
    recurring_total = sum(v[0].get("amount", 0) for v in likely_recurring.values())

    lines = [
        "*Cash Flow Forecast (based on 60-day history)*",
        f"  Avg monthly outflow: *${avg_monthly_out:,.2f}*",
        f"  Avg monthly inflow: *${avg_monthly_in:,.2f}*",
        f"  Net: *${avg_monthly_in - avg_monthly_out:,.2f}*",
        "",
    ]

    if likely_recurring:
        lines.append(f"*Recurring charges detected ({len(likely_recurring)}):*")
        for key, occurrences in sorted(likely_recurring.items(),
                                       key=lambda x: x[1][0].get("amount", 0), reverse=True):
            inst = occurrences[0].get("institution", "?")
            amount = occurrences[0].get("amount", 0)
            lines.append(f"  ${amount:.2f} — {inst} ({len(occurrences)}x in 60 days)")
        lines.append(f"  _Estimated recurring monthly: ${recurring_total:,.2f}_")

    return "\n".join(lines)


def monthly_comparison():
    """Compare this month vs last month."""
    data = load_data()
    events = data.get("events", [])

    this_month = NOW.strftime("%Y-%m")
    last_month = (NOW.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    this_charges = sum(e.get("amount", 0) for e in events
                       if e.get("category") == "charge" and e.get("date", "").startswith(this_month))
    last_charges = sum(e.get("amount", 0) for e in events
                       if e.get("category") == "charge" and e.get("date", "").startswith(last_month))

    lines = [f"*Month-over-Month: {this_month} vs {last_month}*"]
    lines.append(f"  This month: ${this_charges:,.2f}")
    lines.append(f"  Last month: ${last_charges:,.2f}")

    if last_charges > 0:
        change = ((this_charges - last_charges) / last_charges) * 100
        direction = "UP" if change > 0 else "DOWN"
        lines.append(f"  Change: {direction} {abs(change):.1f}%")
    elif this_charges > 0:
        lines.append(f"  _(No data for last month)_")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Finance Monitor")
    parser.add_argument("--scan", action="store_true", help="Scan for new financial emails (default)")
    parser.add_argument("--weekly", action="store_true", help="Post weekly digest to Slack")
    parser.add_argument("--digest", action="store_true", help="Print weekly digest to stdout")
    parser.add_argument("--spending", type=int, nargs="?", const=30, help="Spending analysis (N days, default 30)")
    parser.add_argument("--forecast", action="store_true", help="Cash flow forecast")
    parser.add_argument("--compare", action="store_true", help="Month-over-month comparison")
    args = parser.parse_args()

    if args.weekly:
        text = weekly_digest()
        slack_post(text)
        log("Weekly digest posted.")
    elif args.digest:
        print(weekly_digest())
    elif args.spending is not None:
        print(spending_analysis(args.spending))
    elif args.forecast:
        print(cash_flow_forecast())
    elif args.compare:
        print(monthly_comparison())
    else:
        main()
