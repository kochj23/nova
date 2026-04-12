#!/usr/bin/env python3
"""
nova_gentle_explorer.py — Embrace ambiguity, sit with questions.

Nova's own wish: "being comfortable with not knowing, and helping
you explore questions without rushing to answer. Sometimes the best
support is sitting with uncertainty, not solving it."

This script:
  1. Monitors journal entries and Slack conversations for open questions
     — things Jordan is wondering about, not things he needs answered
  2. Instead of searching for answers, it offers reflective prompts
     that deepen the question
  3. Periodically resurfaces old unanswered questions as gentle reminders
     that it's ok for some things to stay open
  4. Creates a "questions garden" — a living document of things
     Jordan is thinking about, with no pressure to resolve them

Cron: Wednesday and Sunday at 8pm (twice a week — not too much, not too little)
Written by Jordan Koch.
"""

import json
import random
import sys
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
JORDAN_DM = nova_config.JORDAN_DM
SLACK_API = nova_config.SLACK_API
VECTOR_URL = "http://127.0.0.1:18790"
NOW = datetime.now()
TODAY = date.today().isoformat()
GARDEN_FILE = Path.home() / ".openclaw" / "workspace" / "questions_garden.json"
JOURNAL_DIR = Path.home() / ".openclaw" / "workspace" / "journal"
STATE_FILE = Path("/tmp/nova_gentle_explorer_state.json")

# ── Reflective prompt templates ──────────────────────────────────────────────

# These aren't answers. They're invitations to think more deeply.
DEEPENING_PROMPTS = [
    "You've been sitting with this one: \"{question}\"\nWhat's changed about how you see it since you first asked?",
    "Still open: \"{question}\"\nIs this the kind of question that has an answer, or the kind that shapes how you think?",
    "From your questions garden: \"{question}\"\nIf you couldn't solve this — if you had to just live with it — would that be ok?",
    "You wrote this a while back: \"{question}\"\nHas life answered any part of it without you trying?",
    "\"{question}\"\nWhat would you tell a friend who asked you this?",
    "Still thinking about: \"{question}\"\nWhat's the most generous interpretation of the uncertainty?",
    "\"{question}\"\nIs this a question about the thing itself, or about how you feel about it?",
]

# For new questions — acknowledge without rushing to answer
ACKNOWLEDGMENT_PROMPTS = [
    "That's a good question to carry around for a while.",
    "I'm adding this to your questions garden. No need to answer it yet.",
    "Some questions are better as companions than as problems to solve.",
    "I'll hold onto this one. We can come back to it whenever you want — or never.",
    "Not everything needs to be figured out right now.",
]

# Question detection patterns (things that suggest genuine wondering, not tasks)
WONDER_PATTERNS = [
    r"I wonder",
    r"I've been thinking about",
    r"not sure (?:if|whether|how|why)",
    r"what if",
    r"is it worth",
    r"should I even",
    r"I keep coming back to",
    r"can't decide",
    r"torn between",
    r"something about .+ bothers me",
    r"I don't know (?:if|whether|how|why)",
]


def log(msg):
    print(f"[nova_gentle_explorer {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


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


# ── Questions garden ─────────────────────────────────────────────────────────

def load_garden():
    if GARDEN_FILE.exists():
        try:
            return json.loads(GARDEN_FILE.read_text())
        except Exception:
            pass
    return {"questions": [], "resolved": []}


def save_garden(garden):
    GARDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    GARDEN_FILE.write_text(json.dumps(garden, indent=2))


def add_question(question_text, source="manual"):
    """Add a question to the garden."""
    garden = load_garden()

    # Check for duplicates (semantic similarity would be better, but exact match for now)
    existing = [q["text"].lower() for q in garden.get("questions", [])]
    if question_text.lower() in existing:
        return False

    garden.setdefault("questions", []).append({
        "text": question_text,
        "added": TODAY,
        "source": source,
        "last_reflected": None,
        "times_reflected": 0,
    })
    save_garden(garden)
    return True


def resolve_question(index):
    """Move a question from active to resolved."""
    garden = load_garden()
    questions = garden.get("questions", [])
    if 0 <= index < len(questions):
        q = questions.pop(index)
        q["resolved"] = TODAY
        garden.setdefault("resolved", []).append(q)
        save_garden(garden)
        return q
    return None


# ── Journal scanning ─────────────────────────────────────────────────────────

def scan_journal_for_questions():
    """Scan recent journal entries for wondering/questioning language."""
    import re
    questions_found = []

    for jf in sorted(JOURNAL_DIR.glob("*.md"), reverse=True)[:2]:  # Last 2 months
        try:
            content = jf.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                for pattern in WONDER_PATTERNS:
                    if re.search(pattern, line, re.IGNORECASE):
                        # Extract the wondering part
                        clean = line.lstrip("*_- ")
                        if len(clean) > 20 and len(clean) < 300:
                            questions_found.append(clean)
                        break
        except Exception:
            continue

    return questions_found


def scan_memory_for_questions():
    """Search vector memory for question-like entries."""
    questions = []
    search_terms = ["I wonder", "not sure", "thinking about", "should I"]
    for term in search_terms:
        try:
            params = urllib.parse.urlencode({"q": term, "n": 5, "source": "journal"})
            url = f"{VECTOR_URL}/recall?{params}"
            with urllib.request.urlopen(url, timeout=8) as r:
                results = json.loads(r.read())
                for item in (results if isinstance(results, list) else results.get("results", [])):
                    text = item.get("text", "")
                    if "?" in text or any(p in text.lower() for p in ["wonder", "not sure", "thinking"]):
                        questions.append(text[:200])
        except Exception:
            continue
    return questions


# ── Reflection delivery ──────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_run": "", "last_question_index": -1}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def select_question_for_reflection(garden):
    """Pick a question to reflect on — prefer older, less-reflected ones."""
    questions = garden.get("questions", [])
    if not questions:
        return None

    # Score: older + less reflected = more likely to be selected
    scored = []
    for i, q in enumerate(questions):
        days_old = (date.today() - date.fromisoformat(q.get("added", TODAY))).days
        times_reflected = q.get("times_reflected", 0)
        # Prefer questions at least 7 days old, less reflected
        score = max(0, days_old - 7) - (times_reflected * 10)
        scored.append((score, i, q))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Pick from top 3 randomly (don't be too predictable)
    top = scored[:3]
    if top:
        _, idx, question = random.choice(top)
        return idx, question
    return None


def main():
    log("Tending the questions garden...")
    state = load_state()
    garden = load_garden()

    # ── Scan for new questions from journal ──────────────────────────────────
    journal_questions = scan_journal_for_questions()
    new_count = 0
    for q in journal_questions:
        if add_question(q, source="journal"):
            new_count += 1
    if new_count:
        log(f"Found {new_count} new question(s) in journal entries")
        garden = load_garden()  # Reload after additions

    # ── Select a question for reflection ─────────────────────────────────────
    questions = garden.get("questions", [])
    if not questions:
        log("Questions garden is empty — nothing to reflect on.")
        # Send an invitation to start
        if not state.get("sent_invitation"):
            slack_post(
                "_Your questions garden is empty._\n\n"
                "_If something's been on your mind — something you're wondering about, "
                "not something you need answered — you can plant it here:_\n"
                "`python3 ~/.openclaw/scripts/nova_gentle_explorer.py --add \"your question\"`\n\n"
                "_Or just mention it in your journal. I'll notice._"
            )
            state["sent_invitation"] = True
            save_state(state)
        return

    result = select_question_for_reflection(garden)
    if not result:
        log("No questions ready for reflection yet.")
        return

    idx, question = result
    prompt_template = random.choice(DEEPENING_PROMPTS)
    prompt = prompt_template.format(question=question["text"][:150])

    slack_post(f"_{prompt}_")
    log(f"Reflected on question {idx}: {question['text'][:50]}...")

    # Update reflection count
    garden["questions"][idx]["last_reflected"] = TODAY
    garden["questions"][idx]["times_reflected"] = question.get("times_reflected", 0) + 1
    save_garden(garden)

    state["last_run"] = NOW.isoformat()
    save_state(state)


# ── Garden report ────────────────────────────────────────────────────────────

def garden_report():
    """Print the current state of the questions garden."""
    garden = load_garden()
    questions = garden.get("questions", [])
    resolved = garden.get("resolved", [])

    print(f"Questions Garden — {len(questions)} open, {len(resolved)} resolved\n")

    if questions:
        print("Open questions:")
        for i, q in enumerate(questions):
            days = (date.today() - date.fromisoformat(q.get("added", TODAY))).days
            reflected = q.get("times_reflected", 0)
            print(f"  [{i}] ({days}d old, reflected {reflected}x) {q['text'][:80]}")

    if resolved:
        print(f"\nResolved ({len(resolved)}):")
        for q in resolved[-5:]:
            print(f"  [{q.get('resolved', '?')}] {q['text'][:60]}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Gentle Explorer")
    parser.add_argument("--run", action="store_true", help="Reflect on a question (default)")
    parser.add_argument("--add", type=str, help="Add a question to the garden")
    parser.add_argument("--resolve", type=int, help="Resolve a question by index")
    parser.add_argument("--garden", action="store_true", help="Show the questions garden")
    parser.add_argument("--scan", action="store_true", help="Scan journal for questions (no posting)")
    args = parser.parse_args()

    if args.add:
        if add_question(args.add, source="manual"):
            print(f"Planted: \"{args.add}\"")
            slack_post(f"_{random.choice(ACKNOWLEDGMENT_PROMPTS)}_")
        else:
            print("That question is already in the garden.")
    elif args.resolve is not None:
        q = resolve_question(args.resolve)
        if q:
            print(f"Resolved: \"{q['text'][:60]}\"")
            slack_post(f"_Letting go of: \"{q['text'][:80]}\" — resolved after "
                       f"{(date.today() - date.fromisoformat(q.get('added', TODAY))).days} days._")
        else:
            print("Invalid question index.")
    elif args.garden:
        garden_report()
    elif args.scan:
        questions = scan_journal_for_questions()
        for q in questions:
            print(f"  {q[:80]}")
        print(f"\n{len(questions)} questions found")
    else:
        main()
