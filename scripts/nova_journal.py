#!/usr/bin/env python3
"""
nova_journal.py — Nightly journaling reflection prompt.

At 9pm, posts a gentle prompt to Jordan's Slack DM asking how the day went.
When Jordan responds, stores the entry in:
  1. A local markdown journal file (per-month)
  2. Vector memory for semantic recall

The prompt is contextual — it references what Nova observed during the day
(meetings, commits, weather, app activity) to seed the reflection without
being prescriptive.

Also supports manual journal entries at any time.

Cron: 9pm PT (prompt), continuous (response listener handled by Nova agent)
Written by Jordan Koch.
"""

import json
import random
import subprocess
import sys
import urllib.request
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_CHAN
SLACK_API = nova_config.SLACK_API
JORDAN_DM = nova_config.JORDAN_DM
VECTOR_URL = nova_config.VECTOR_URL
NOW = datetime.now()
TODAY = date.today().isoformat()
JOURNAL_DIR = Path.home() / ".openclaw" / "workspace" / "journal"
MEMORY_DIR = Path.home() / ".openclaw" / "workspace" / "memory"

# ── Reflection prompts (rotated randomly) ────────────────────────────────────

BASE_PROMPTS = [
    "How was your day, Little Mister?",
    "What's on your mind tonight?",
    "Anything worth remembering about today?",
    "How are you feeling about how things went?",
    "What was the best part of today?",
    "What's something you noticed today that you want to hold onto?",
    "If today were a chapter title, what would it be?",
    "What would you tell yesterday-you about today?",
    "Was today more about progress or about rest? Both are fine.",
    "What's one thing you'd do differently if you could replay today?",
]

# Context-aware additions
CONTEXT_ADDITIONS = {
    "meetings": [
        "I saw you had meetings today — any good conversations?",
        "Looks like a meeting-heavy day. Anything useful come out of them?",
    ],
    "commits": [
        "You pushed some code today. Proud of anything in particular?",
        "I saw some commits go through. Was it satisfying work?",
    ],
    "no_activity": [
        "Quiet day on GitHub — was that intentional?",
        "Not much on the commit graph today. Recharging?",
    ],
    "weekend": [
        "It's the weekend — did you actually take time off?",
        "Weekend check-in: relaxation or side projects?",
    ],
    "late_night": [
        "It's late — are you taking care of yourself?",
        "You're up late. Everything ok?",
    ],
}


def log(msg):
    print(f"[nova_journal {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


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


def vector_remember(text, metadata=None):
    try:
        payload = json.dumps({
            "text": text, "source": "journal",
            "metadata": metadata or {}
        }).encode()
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ── Context gathering ────────────────────────────────────────────────────────

def get_day_context():
    """Gather what happened today for contextual prompting."""
    context = []

    # Check if it's a weekend
    if NOW.weekday() >= 5:
        context.append("weekend")

    # Check if it's late
    if NOW.hour >= 23:
        context.append("late_night")

    # Check for meetings via OneOnOne
    try:
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", "2",
             "http://127.0.0.1:37421/api/oneonone/meetings?limit=5"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            meetings = data if isinstance(data, list) else data.get("meetings", [])
            today_meetings = [m for m in meetings if TODAY in str(m.get("date", ""))]
            if today_meetings:
                context.append("meetings")
    except Exception:
        pass

    # Check for GitHub activity
    try:
        r = subprocess.run(
            ["gh", "api", "/users/kochj23/events?per_page=10"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            events = json.loads(r.stdout)
            today_events = [e for e in events if TODAY in e.get("created_at", "")]
            if today_events:
                context.append("commits")
            else:
                context.append("no_activity")
    except Exception:
        pass

    # Check today's memory log for activity summary
    mem_file = MEMORY_DIR / f"{TODAY}.md"
    if mem_file.exists():
        content = mem_file.read_text(encoding="utf-8")
        if "cron" in content.lower():
            # Nova was busy today
            pass

    return context


def build_prompt():
    """Build a contextual journal prompt."""
    base = random.choice(BASE_PROMPTS)
    context = get_day_context()

    additions = []
    for ctx in context:
        if ctx in CONTEXT_ADDITIONS:
            additions.append(random.choice(CONTEXT_ADDITIONS[ctx]))

    if additions:
        # Pick one context addition (don't overwhelm)
        addition = random.choice(additions)
        prompt = f"_{base}_\n\n_{addition}_"
    else:
        prompt = f"_{base}_"

    return prompt


# ── Journal storage ──────────────────────────────────────────────────────────

def save_journal_entry(text, prompt_used=None):
    """Save a journal entry to markdown file and vector memory."""
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    month_file = JOURNAL_DIR / f"{NOW.strftime('%Y-%m')}.md"

    entry = f"\n## {NOW.strftime('%A, %B %d %Y — %I:%M %p')}\n"
    if prompt_used:
        entry += f"*Prompt:* {prompt_used}\n\n"
    entry += text.strip() + "\n"

    if month_file.exists():
        existing = month_file.read_text(encoding="utf-8")
    else:
        existing = f"# Journal — {NOW.strftime('%B %Y')}\n"

    month_file.write_text(existing + entry, encoding="utf-8")
    log(f"Journal entry saved to {month_file.name}")

    # Store in vector memory (for semantic recall of moods/themes)
    vector_remember(
        f"Journal entry {TODAY}: {text[:500]}",
        {"date": TODAY, "type": "journal_entry", "day": NOW.strftime("%A")}
    )

    return month_file


# ── Main ─────────────────────────────────────────────────────────────────────

def send_prompt():
    """Send the nightly journal prompt to Jordan's DM."""
    # Check if we already sent a prompt today
    state_file = Path("/tmp/nova_journal_state.json")
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            if state.get("last_prompt_date") == TODAY:
                log("Already sent journal prompt today — skipping.")
                return
        except Exception:
            pass

    prompt = build_prompt()
    slack_post(prompt)
    log("Journal prompt sent to DM.")

    # Save state
    state_file.write_text(json.dumps({
        "last_prompt_date": TODAY,
        "prompt": prompt,
        "sent_at": NOW.isoformat(),
    }))


def manual_entry(text):
    """Save a manual journal entry."""
    save_journal_entry(text)
    slack_post(f"_Journal entry saved._ ({len(text)} chars)", channel=JORDAN_DM)
    log(f"Manual entry saved: {len(text)} chars")


def recent_entries(count=5):
    """Print recent journal entries."""
    files = sorted(JOURNAL_DIR.glob("*.md"), reverse=True)
    if not files:
        print("No journal entries yet.")
        return

    entries_shown = 0
    for f in files:
        content = f.read_text(encoding="utf-8")
        sections = content.split("\n## ")
        for section in reversed(sections[1:]):
            if entries_shown >= count:
                return
            print(f"\n## {section.strip()[:500]}")
            entries_shown += 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Journal")
    parser.add_argument("--prompt", action="store_true", help="Send nightly prompt to DM (default)")
    parser.add_argument("--write", type=str, help="Save a manual journal entry")
    parser.add_argument("--recent", type=int, nargs="?", const=5, help="Show recent entries")
    parser.add_argument("--save-response", type=str, help="Save a response to today's prompt")
    args = parser.parse_args()

    if args.write:
        manual_entry(args.write)
    elif args.recent is not None:
        recent_entries(args.recent)
    elif args.save_response:
        # Load today's prompt for context
        state_file = Path("/tmp/nova_journal_state.json")
        prompt_used = None
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                prompt_used = state.get("prompt")
            except Exception:
                pass
        save_journal_entry(args.save_response, prompt_used)
    else:
        send_prompt()
