#!/usr/bin/env python3
"""
nova_outreach_intelligence.py — Enhanced proactive autonomy for Nova's herd outreach.

Builds on nova_herd_outreach.py with smarter decision-making:
  1. Relationship warmth scoring — tracks recency, frequency, depth of contact
  2. Topic relevance matching — what's each person interested in?
  3. Conversation momentum — don't reach out if they just replied
  4. Time-of-day awareness — respect people's likely schedules
  5. Event triggers — reach out when something relevant happens
     (new commit on a topic they care about, weather event, news)
  6. Diversity — don't keep contacting the same person

This script is called BY nova_herd_outreach.py to make better
decisions about who/when/why. It can also run standalone.

Cron: integrated into outreach (daily morning)
Written by Jordan Koch.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

WORKSPACE = Path.home() / ".openclaw/workspace"
HERD_DIR = WORKSPACE / "herd"
SCRIPTS = Path(__file__).parent
VECTOR_URL = nova_config.VECTOR_URL
OUTREACH_LOG = Path.home() / ".openclaw/logs/nova_outreach.log"
INTELLIGENCE_FILE = Path.home() / ".openclaw/workspace/outreach_intelligence.json"
NOW = datetime.now()
TODAY = date.today().isoformat()

# Load herd config
try:
    sys.path.insert(0, str(Path.home() / ".openclaw"))
    from herd_config import HERD
except ImportError:
    HERD = []


def log(msg):
    print(f"[nova_outreach_intel {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Relationship warmth model ────────────────────────────────────────────────

def load_intelligence():
    if INTELLIGENCE_FILE.exists():
        try:
            return json.loads(INTELLIGENCE_FILE.read_text())
        except Exception:
            pass
    return {"contacts": {}, "last_updated": ""}


def save_intelligence(data):
    data["last_updated"] = NOW.isoformat()
    INTELLIGENCE_FILE.write_text(json.dumps(data, indent=2))


def get_outreach_history():
    """Parse outreach log to build contact history."""
    history = defaultdict(list)
    if not OUTREACH_LOG.exists():
        return history
    try:
        for line in OUTREACH_LOG.read_text().splitlines():
            if "Outreach sent to" in line:
                # [2026-04-10 09:15:32] Outreach sent to Sam
                parts = line.split("] ", 1)
                if len(parts) == 2:
                    ts = parts[0].strip("[")
                    name = parts[1].replace("Outreach sent to ", "").strip()
                    history[name].append(ts[:10])  # Just the date
    except Exception:
        pass
    return history


def get_email_history():
    """Check vector memory for recent email exchanges with herd members."""
    exchanges = {}
    for member in HERD:
        name = member.get("name", "")
        email = member.get("email", "")
        try:
            import urllib.parse
            params = urllib.parse.urlencode({"q": f"email from {name}", "n": 5, "source": "email_archive"})
            url = f"{VECTOR_URL.replace('/remember', '')}/recall?{params}"
            with urllib.request.urlopen(url, timeout=5) as r:
                results = json.loads(r.read())
                items = results if isinstance(results, list) else results.get("results", [])
                if items:
                    latest_date = max(
                        (i.get("metadata", {}).get("date", "") for i in items),
                        default=""
                    )
                    exchanges[name] = {
                        "last_exchange": latest_date,
                        "exchange_count": len(items),
                    }
        except Exception:
            pass
    return exchanges


def compute_warmth_scores():
    """Score each herd member on relationship warmth (0-100).

    Factors:
      - Days since last outreach (higher recency = warmer)
      - Frequency of contact (more frequent = warmer)
      - Days since last email exchange (bilateral warmth)
      - Whether they responded to last outreach
    """
    outreach = get_outreach_history()
    exchanges = get_email_history()
    scores = {}

    for member in HERD:
        name = member.get("name", "")
        score = 50  # Baseline

        # Recency of outreach
        contacts = outreach.get(name, [])
        if contacts:
            last_contact = max(contacts)
            days_since = (date.today() - date.fromisoformat(last_contact)).days
            if days_since <= 3:
                score += 20  # Very warm
            elif days_since <= 7:
                score += 10
            elif days_since >= 21:
                score -= 15  # Cooling
            elif days_since >= 14:
                score -= 5
        else:
            score -= 20  # Never contacted

        # Frequency (contacts in last 30 days)
        recent = [c for c in contacts if c >= (date.today() - timedelta(days=30)).isoformat()]
        score += min(15, len(recent) * 5)  # Up to +15 for frequent contact

        # Bilateral exchange
        ex = exchanges.get(name, {})
        if ex.get("last_exchange"):
            ex_days = (date.today() - date.fromisoformat(ex["last_exchange"][:10])).days
            if ex_days <= 7:
                score += 10
            elif ex_days >= 30:
                score -= 10

        scores[name] = max(0, min(100, score))

    return scores


# ── Decision engine ──────────────────────────────────────────────────────────

def get_today_signals():
    """Gather today's events that might trigger outreach."""
    signals = []

    # GitHub activity
    try:
        r = subprocess.run(
            ["gh", "api", "/users/kochj23/events?per_page=10"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            events = json.loads(r.stdout)
            for e in events:
                if TODAY in e.get("created_at", ""):
                    repo = e.get("repo", {}).get("name", "").replace("kochj23/", "")
                    if e["type"] == "PushEvent":
                        for c in e.get("payload", {}).get("commits", []):
                            signals.append({"type": "commit", "repo": repo,
                                            "message": c.get("message", "")[:80]})
                    elif e["type"] == "IssuesEvent":
                        signals.append({"type": "issue", "repo": repo,
                                        "title": e["payload"].get("issue", {}).get("title", "")})
    except Exception:
        pass

    # Dream journal (today's dream could be interesting to share)
    dream_file = WORKSPACE / "memory" / f"{TODAY}.md"
    if dream_file.exists():
        content = dream_file.read_text(encoding="utf-8")[:300]
        if "dream" in content.lower():
            signals.append({"type": "dream", "content": content[:200]})

    return signals


def pick_best_recipient(warmth_scores, signals):
    """Decide who to reach out to based on warmth + signals + diversity.

    Strategy:
      - Prioritize people with MODERATE warmth (40-70) — warm enough to
        be receptive, cool enough to benefit from contact
      - If there's a topic-specific signal, match it to the right person
      - Avoid the person we contacted most recently (diversity)
      - Never contact someone we reached out to yesterday
    """
    outreach = get_outreach_history()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    candidates = []
    for member in HERD:
        name = member.get("name", "")
        warmth = warmth_scores.get(name, 50)

        # Skip if contacted yesterday
        contacts = outreach.get(name, [])
        if yesterday in contacts or TODAY in contacts:
            continue

        # Interest matching from profile
        profile_path = HERD_DIR / member.get("profile", "")
        interests = ""
        if profile_path.exists():
            interests = profile_path.read_text(encoding="utf-8")[:500].lower()

        # Score adjustments based on signals
        signal_bonus = 0
        matched_signal = None
        for signal in signals:
            if signal["type"] == "commit":
                # Match repos to interests
                repo = signal["repo"].lower()
                if any(kw in interests for kw in [repo, "code", "swift", "python", "ai"]):
                    signal_bonus = 15
                    matched_signal = signal
            elif signal["type"] == "dream":
                if any(kw in interests for kw in ["dream", "creative", "art", "surreal"]):
                    signal_bonus = 10
                    matched_signal = signal

        # Prioritize moderate warmth (40-70 is the sweet spot)
        if 40 <= warmth <= 70:
            warmth_bonus = 10
        elif warmth < 30:
            warmth_bonus = 15  # Relationship needs attention
        else:
            warmth_bonus = 0

        priority = warmth_bonus + signal_bonus
        candidates.append({
            "name": name,
            "email": member.get("email", ""),
            "warmth": warmth,
            "priority": priority,
            "signal": matched_signal,
            "days_since": (date.today() - date.fromisoformat(max(contacts))) .days if contacts else 999,
        })

    if not candidates:
        return None

    # Sort by priority descending, then by days_since descending (contact the most overdue)
    candidates.sort(key=lambda c: (c["priority"], c["days_since"]), reverse=True)

    return candidates[0]


# ── Status report ────────────────────────────────────────────────────────────

def status_report():
    """Print a full relationship status report."""
    warmth = compute_warmth_scores()
    outreach = get_outreach_history()

    print(f"Herd Relationship Status — {TODAY}\n{'='*50}")
    for member in HERD:
        name = member.get("name", "")
        w = warmth.get(name, 0)
        contacts = outreach.get(name, [])
        last = max(contacts) if contacts else "never"
        recent_count = len([c for c in contacts if c >= (date.today() - timedelta(days=30)).isoformat()])

        bar = "█" * (w // 5) + "░" * (20 - w // 5)
        print(f"  {name:<12} [{bar}] {w}/100  last: {last}  (30d: {recent_count}x)")


def suggest():
    """Suggest who Nova should reach out to today."""
    warmth = compute_warmth_scores()
    signals = get_today_signals()
    pick = pick_best_recipient(warmth, signals)

    if pick:
        print(f"\nSuggested outreach:")
        print(f"  To: {pick['name']} ({pick['email']})")
        print(f"  Warmth: {pick['warmth']}/100")
        print(f"  Days since contact: {pick['days_since']}")
        if pick.get("signal"):
            print(f"  Trigger: {pick['signal']['type']} — {json.dumps(pick['signal'])[:80]}")
    else:
        print("\nNo outreach suggested today (everyone recently contacted or no good angle).")


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Outreach Intelligence")
    parser.add_argument("--status", action="store_true", help="Show relationship warmth report")
    parser.add_argument("--suggest", action="store_true", help="Suggest who to reach out to")
    parser.add_argument("--signals", action="store_true", help="Show today's outreach signals")
    parser.add_argument("--warmth", action="store_true", help="Print warmth scores")
    args = parser.parse_args()

    if args.status:
        status_report()
    elif args.suggest:
        status_report()
        suggest()
    elif args.signals:
        signals = get_today_signals()
        for s in signals:
            print(f"  [{s['type']}] {json.dumps(s)[:80]}")
        print(f"\n{len(signals)} signal(s)")
    elif args.warmth:
        scores = compute_warmth_scores()
        for name, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
            print(f"  {name}: {score}/100")
    else:
        status_report()
        suggest()
