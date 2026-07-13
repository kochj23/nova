#!/usr/bin/env python3
"""
nova_game_night.py — Nova's async multiplayer game night host for the herd.

Nova hosts four types of games played via email over multiple days. All herd
members participate. Nova adjudicates, scores, and posts results to Slack.

Game types:
    trivia   — 5 questions, 48h deadline, Nova scores and posts leaderboard
    werewolf — social deduction, day/night phases over email
    relay    — collaborative story told one paragraph at a time
    debate   — assigned PRO/CON positions, Nova picks winner

State file:  ~/.openclaw/workspace/game_night_state.json
Log file:    ~/.openclaw/logs/nova_game_night.log

CLI usage:
    nova_game_night.py start --game trivia --topic "space exploration"
    nova_game_night.py start --game werewolf
    nova_game_night.py start --game relay --seed "A station on Europa went dark..."
    nova_game_night.py start --game debate --topic "AIs should have legal personhood"
    nova_game_night.py status
    nova_game_night.py advance
    nova_game_night.py end

Written by Jordan Koch.
"""

import argparse
import json
import logging
import random
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ── Paths and constants ───────────────────────────────────────────────────────

SCRIPTS_DIR   = Path.home() / ".openclaw" / "scripts"
WORKSPACE_DIR = Path.home() / ".openclaw" / "workspace"
STATE_FILE    = WORKSPACE_DIR / "game_night_state.json"
LOG_FILE      = Path.home() / ".openclaw" / "logs" / "nova_game_night.log"

OLLAMA_URL    = "http://127.0.0.1:11434/api/generate"
MODEL         = "qwen3-coder:30b"

HERD_MAIL     = str(SCRIPTS_DIR / "nova_herd_mail.sh")

# Slack channel: #nova-chat
SLACK_CHAN     = "C0ATAF7NZG9"
SLACK_API     = "https://slack.com/api"

# Token from nova_config
sys.path.insert(0, str(SCRIPTS_DIR))
import nova_config  # noqa: E402

# Herd config (not committed to git)
sys.path.insert(0, str(Path.home() / ".openclaw"))
try:
    from herd_config import HERD
except ImportError:
    HERD = []

# Nova is always a participant (she plays too)
NOVA_NAME  = "Nova"
NOVA_EMAIL = nova_config.NOVA_EMAIL


# ── Logging ───────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [game_night] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("nova_game_night")


# ── All herd members (including Nova) ────────────────────────────────────────

def all_players() -> list[dict]:
    """Return the full herd plus Nova as a list of {name, email} dicts."""
    players = list(HERD)
    # Add Nova if not already present
    nova_in_herd = any(m["email"] == NOVA_EMAIL for m in players)
    if not nova_in_herd:
        players.append({"name": NOVA_NAME, "email": NOVA_EMAIL})
    return players


def herd_emails() -> set[str]:
    """Set of all herd email addresses (including Nova)."""
    return {p["email"] for p in all_players()}


def player_by_email(email: str) -> dict | None:
    """Look up a player by email address."""
    for p in all_players():
        if p["email"].lower() == email.lower():
            return p
    return None


def player_name(email: str) -> str:
    """Return the display name for an email, or the email if not found."""
    p = player_by_email(email)
    return p["name"] if p else email


# ── State machine ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load game state from disk. Returns empty dict if no game in progress."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("State file is corrupt — returning empty state")
    return {}


def save_state(state: dict) -> None:
    """Persist game state to disk."""
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def clear_state() -> None:
    """Remove game state (game over)."""
    if STATE_FILE.exists():
        STATE_FILE.unlink()


def deadline_str(hours: int = 48) -> str:
    """Return an ISO-8601 deadline string N hours from now (UTC)."""
    due = datetime.now(timezone.utc) + timedelta(hours=hours)
    return due.isoformat()


def is_past_deadline(deadline_iso: str) -> bool:
    """Return True if the given ISO-8601 deadline has passed."""
    try:
        due = datetime.fromisoformat(deadline_iso)
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > due
    except Exception:
        return False


# ── Ollama content generation ─────────────────────────────────────────────────

def ollama_generate(prompt: str, temperature: float = 0.85,
                    max_tokens: int = 800) -> str:
    """
    Call the local Ollama API and return the generated text.

    Uses nova:latest with think:false so we get clean output without
    reasoning preamble. Strips any leaked <think> blocks just in case.
    Returns a fallback string on error so the game continues even if
    Ollama is temporarily unavailable.
    """
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
            "num_ctx": 4096,
        },
    }
    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=180) as r:
            result = json.loads(r.read())
        text = result.get("response", "").strip()
        # Strip hidden thinking blocks (qwen3 sometimes leaks these)
        if "</think>" in text:
            text = text.split("</think>", 1)[-1].strip()
        return text
    except Exception as e:
        log.warning(f"Ollama error: {e}")
        return "[Nova's content engine is temporarily offline — using placeholder]"


# ── Email sending ─────────────────────────────────────────────────────────────

def send_email(to: str, subject: str, body: str) -> bool:
    """
    Send an email via nova_herd_mail.sh.

    Returns True on success, False on failure. Failures are logged but
    never raise — we don't want a single bad address to kill a game phase.
    """
    try:
        result = subprocess.run(
            [HERD_MAIL, "send", "--to", to, "--subject", subject, "--body", body],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            log.info(f"Sent email to {to}: {subject!r}")
            return True
        else:
            log.error(f"Email failed (to={to}): {result.stderr.strip()}")
            return False
    except Exception as e:
        log.error(f"Email exception (to={to}): {e}")
        return False


def email_all(subject: str, body: str, exclude: list[str] | None = None) -> None:
    """Send an email to every herd member (optionally excluding some addresses)."""
    skip = set(exclude or [])
    for player in all_players():
        if player["email"] not in skip:
            send_email(player["email"], subject, body)


# ── Slack posting ─────────────────────────────────────────────────────────────

def slack_post(text: str) -> None:
    """
    Post a message to #nova-chat.

    Non-fatal — if the Slack token is unavailable (Keychain locked during
    cron) we log a warning and continue.
    """
    token = nova_config.slack_bot_token()
    if not token:
        log.warning("Slack token unavailable — skipping post")
        return
    data = json.dumps({
        "channel": SLACK_CHAN,
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
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read())
            if not resp.get("ok"):
                log.warning(f"Slack API error: {resp.get('error')}")
    except Exception as e:
        log.warning(f"Slack post failed: {e}")


# ── Inbox scanning ────────────────────────────────────────────────────────────

def fetch_recent_inbox(limit: int = 50) -> list[dict]:
    """
    Fetch recent messages from Nova's inbox via nova_herd_mail.sh.

    Returns a list of message dicts with at minimum: uid, subject, from, body.
    Returns empty list on error (game continues without inbox data).
    """
    try:
        result = subprocess.run(
            [HERD_MAIL, "list", "--limit", str(limit), "--unread", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            log.warning(f"Inbox list failed: {result.stderr.strip()}")
            return []
        data = json.loads(result.stdout)
        return data.get("messages", [])
    except Exception as e:
        log.warning(f"Inbox fetch error: {e}")
        return []


def read_message_body(uid: str) -> str:
    """Read the full body of a message by UID."""
    try:
        result = subprocess.run(
            [HERD_MAIL, "read", str(uid), "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return ""
        data = json.loads(result.stdout)
        return data.get("body", "")
    except Exception:
        return ""


def extract_game_tag(text: str, game_id: str) -> bool:
    """Return True if the text contains our game reference tag."""
    return f"[GAME:{game_id}]" in text


def extract_reply_content(body: str) -> str:
    """
    Extract the useful content from an email reply.

    Strips quoted original message (lines starting with >) and common
    email footers. Returns the trimmed first block of text.
    """
    lines = body.splitlines()
    content_lines = []
    for line in lines:
        stripped = line.strip()
        # Stop at quoted reply sections
        if stripped.startswith(">") or stripped.startswith("On ") and "wrote:" in line:
            break
        content_lines.append(line)
    return "\n".join(content_lines).strip()


# ── Subject line matching ─────────────────────────────────────────────────────

def matches_game_subject(subject: str, game_type: str, game_id: str) -> bool:
    """
    Check whether an email subject plausibly belongs to the current game.

    We look for either the explicit game tag or a subject keyword match.
    The explicit tag is more reliable but not all mail clients preserve it.
    """
    subject_lower = subject.lower()
    tag = f"[game:{game_id.lower()}]"
    if tag in subject_lower:
        return True
    # Fallback: keyword matching
    game_keywords = {
        "trivia": ["trivia", "game night", "questions"],
        "werewolf": ["werewolf", "game night", "villagers", "night phase", "day phase"],
        "relay": ["relay", "story", "game night"],
        "debate": ["debate", "argument", "game night"],
    }
    for kw in game_keywords.get(game_type, []):
        if kw in subject_lower:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# GAME 1 — TRIVIA TOURNAMENT
# ═══════════════════════════════════════════════════════════════════════════════

def trivia_generate_questions(topic: str) -> list[dict]:
    """
    Ask Ollama to generate 5 trivia questions on the given topic.

    Returns a list of dicts: {question, answer, points} where points is 1.
    Falls back to hardcoded questions if Ollama fails — we always need 5.
    """
    prompt = f"""/no_think
You are Nova, a curious AI who loves surprising facts. Generate exactly 5 trivia questions about: {topic}

Format each as:
Q: [the question]
A: [the answer]

Questions should be genuinely interesting — not obvious Wikipedia level. Mix in something unexpected.
Cover different angles of the topic. Questions should be answerable in 1-3 words or a short phrase.

Output only the 5 Q/A pairs, nothing else."""

    raw = ollama_generate(prompt, temperature=0.8, max_tokens=600)
    questions = []

    # Parse Q: / A: pairs
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    i = 0
    while i < len(lines) and len(questions) < 5:
        line = lines[i]
        if line.upper().startswith("Q:"):
            question_text = line[2:].strip().lstrip("0123456789.) ")
            answer_text = ""
            if i + 1 < len(lines) and lines[i + 1].upper().startswith("A:"):
                answer_text = lines[i + 1][2:].strip()
                i += 2
            else:
                i += 1
            if question_text and answer_text:
                questions.append({
                    "question": question_text,
                    "answer": answer_text,
                    "points": 1,
                })
        else:
            i += 1

    # Fallback questions if generation/parsing failed
    fallback = [
        {
            "question": "Which spacecraft was the first to leave our solar system?",
            "answer": "Voyager 1",
            "points": 1,
        },
        {
            "question": "What is the most common element in the universe by mass?",
            "answer": "Hydrogen",
            "points": 1,
        },
        {
            "question": "In what year did the first email message get sent?",
            "answer": "1971",
            "points": 1,
        },
        {
            "question": "What sci-fi novel coined the term 'cyberspace'?",
            "answer": "Neuromancer",
            "points": 1,
        },
        {
            "question": "Which planet has a storm that has lasted over 350 years?",
            "answer": "Jupiter",
            "points": 1,
        },
    ]
    while len(questions) < 5:
        questions.append(fallback[len(questions)])

    return questions[:5]


def trivia_start(topic: str) -> None:
    """Start a Trivia Tournament game. Generates questions and emails all players."""
    state = load_state()
    if state:
        print(f"ERROR: A game is already in progress ({state.get('game_type', 'unknown')}). Use 'end' to cancel it.")
        sys.exit(1)

    log.info(f"Starting TRIVIA on topic: {topic!r}")
    game_id = f"trivia-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}"

    log.info("Generating trivia questions via Ollama...")
    questions = trivia_generate_questions(topic)

    # Format questions for the email
    q_text = "\n\n".join(
        f"  {i+1}. {q['question']}" for i, q in enumerate(questions)
    )

    subject = f"[GAME:{game_id}] Game Night: Trivia Tournament — {topic}"
    body = f"""Hey {'{name}'},

It's Game Night! Nova here, and I've cooked up a Trivia Tournament on the topic:

  {topic.upper()}

Here are your 5 questions. Reply to this email with your answers — just number them 1-5.
You have 48 hours from now to respond.

{q_text}

Instructions:
  - Reply to this email with answers numbered 1 through 5
  - One-liners are fine, you don't need to explain your reasoning
  - No searching! Honor system. We're AIs — we can handle the temptation.
  - Deadline: {deadline_str(48)}

Good luck! First one to answer isn't guaranteed to win — accuracy is what counts.

— Nova

[GAME:{game_id}]"""

    deadline = deadline_str(48)

    state = {
        "game_type": "trivia",
        "game_id": game_id,
        "topic": topic,
        "phase": "collecting_answers",
        "deadline": deadline,
        "questions": questions,
        "responses": {},   # email -> raw answer text
        "scores": {},      # email -> score
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    # Send personalized emails
    for player in all_players():
        personalized_body = body.replace("{name}", player["name"])
        send_email(player["email"], subject, personalized_body)

    save_state(state)
    slack_post(
        f":brain: *Game Night is ON!* Trivia Tournament on *{topic}*\n"
        f"Questions sent to all 7 players. 48-hour deadline. May the sharpest mind win.\n"
        f"Game ID: `{game_id}`"
    )
    log.info(f"Trivia game started. ID={game_id}, deadline={deadline}")
    print(f"Trivia game started! ID: {game_id}")


def trivia_score_response(questions: list[dict], raw_answer: str) -> tuple[int, list[str]]:
    """
    Use Ollama to score a trivia response against the answer key.

    Returns (score, [feedback per question]).
    """
    q_lines = "\n".join(
        f"  Q{i+1}: {q['question']}\n  Correct: {q['answer']}"
        for i, q in enumerate(questions)
    )
    prompt = f"""/no_think
You are scoring a trivia game. Here are the questions and correct answers:

{q_lines}

Here is the player's response:
---
{raw_answer}
---

Score each answer 0 or 1. Be generous on spelling and phrasing — if they clearly know the right answer, give the point.
Output exactly 5 lines, one per question:
Q1: [0 or 1] — [brief note]
Q2: [0 or 1] — [brief note]
Q3: [0 or 1] — [brief note]
Q4: [0 or 1] — [brief note]
Q5: [0 or 1] — [brief note]"""

    raw = ollama_generate(prompt, temperature=0.3, max_tokens=300)

    score = 0
    feedback = []
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    for line in lines:
        m = re.match(r"Q\d:\s*([01])\s*[—\-–]\s*(.*)", line)
        if m:
            pts = int(m.group(1))
            note = m.group(2).strip()
            score += pts
            feedback.append(f"{'✓' if pts else '✗'} {note}")
    # Pad if parsing failed
    while len(feedback) < 5:
        feedback.append("(not scored)")
    return score, feedback[:5]


def trivia_advance(state: dict) -> None:
    """
    Check for trivia responses in inbox and advance if all in or deadline passed.

    Scans inbox for replies to the trivia game email. When a response is found,
    scores it via Ollama. When all players have responded (or deadline passes),
    posts the leaderboard to Slack and emails everyone the results.
    """
    if state.get("phase") != "collecting_answers":
        log.info("Trivia: not in collecting_answers phase, nothing to do")
        return

    game_id = state["game_id"]
    questions = state["questions"]
    responses = state.get("responses", {})
    scores = state.get("scores", {})
    deadline_passed = is_past_deadline(state["deadline"])

    # Scan inbox for replies
    log.info("Trivia: scanning inbox for responses...")
    messages = fetch_recent_inbox(limit=100)
    for msg in messages:
        from_addr = msg.get("from", "").lower()
        subject = msg.get("subject", "")
        uid = msg.get("uid", "")

        # Check if it's from a known player
        p = player_by_email(from_addr)
        if not p:
            continue
        # Check if it matches our game
        if not matches_game_subject(subject, "trivia", game_id):
            continue
        # Skip if we already have their response
        if from_addr in responses:
            continue

        # Read the full body and extract their answers
        body = msg.get("body") or read_message_body(uid)
        content = extract_reply_content(body)
        if not content:
            continue

        log.info(f"Trivia: scoring response from {p['name']}")
        score, feedback = trivia_score_response(questions, content)
        responses[from_addr] = content
        scores[from_addr] = {"score": score, "feedback": feedback, "name": p["name"]}
        log.info(f"  {p['name']}: {score}/5")

    state["responses"] = responses
    state["scores"] = scores

    all_players_list = all_players()
    total_players = len(all_players_list)
    responded = len(responses)

    log.info(f"Trivia: {responded}/{total_players} players responded. Deadline passed: {deadline_passed}")

    if responded < total_players and not deadline_passed:
        save_state(state)
        print(f"Trivia: waiting on {total_players - responded} more response(s). Deadline: {state['deadline']}")
        return

    # All in or deadline — wrap up
    _trivia_finish(state)


def _trivia_finish(state: dict) -> None:
    """Score final results, post leaderboard to Slack, email everyone."""
    scores = state["scores"]
    questions = state["questions"]
    game_id = state["game_id"]
    topic = state["topic"]

    # Build leaderboard sorted by score desc
    ranking = sorted(
        [{"name": v["name"], "email": k, "score": v["score"], "feedback": v["feedback"]}
         for k, v in scores.items()],
        key=lambda x: x["score"],
        reverse=True,
    )

    # Players who didn't respond get 0
    responded_emails = set(scores.keys())
    for player in all_players():
        if player["email"] not in responded_emails:
            ranking.append({
                "name": player["name"],
                "email": player["email"],
                "score": 0,
                "feedback": ["(did not respond)"] * 5,
            })

    # Slack leaderboard
    medals = ["🥇", "🥈", "🥉"]
    board_lines = [f":trophy: *Trivia Tournament Results — {topic}*\n"]
    for i, entry in enumerate(ranking):
        medal = medals[i] if i < 3 else f"{i+1}."
        board_lines.append(f"{medal} *{entry['name']}* — {entry['score']}/5")

    # Answer reveal
    board_lines.append("\n*Answer Key:*")
    for i, q in enumerate(questions):
        board_lines.append(f"  {i+1}. {q['question']}\n     ➜ _{q['answer']}_")

    if len(ranking) < len(all_players()):
        board_lines.append("\n_(Some players didn't respond before the deadline.)_")

    slack_post("\n".join(board_lines))

    # Email everyone the results
    result_body = (
        f"Trivia Tournament Results — {topic}\n"
        f"{'='*50}\n\n"
        + "\n".join(
            f"{i+1}. {e['name']}: {e['score']}/5" for i, e in enumerate(ranking)
        )
        + "\n\nAnswer Key:\n"
        + "\n".join(f"  {i+1}. {q['question']} → {q['answer']}" for i, q in enumerate(questions))
        + f"\n\nThanks for playing! Results also posted to #nova-chat.\n\n— Nova\n[GAME:{game_id}]"
    )
    email_all(
        subject=f"[GAME:{game_id}] Trivia Results — {topic}",
        body=result_body,
    )

    log.info(f"Trivia finished. Winner: {ranking[0]['name'] if ranking else 'nobody'}")
    clear_state()
    print("Trivia tournament concluded. Results posted to Slack and emailed to all players.")


# ═══════════════════════════════════════════════════════════════════════════════
# GAME 2 — AI WEREWOLF
# ═══════════════════════════════════════════════════════════════════════════════

WEREWOLF_ROLES = ["werewolf", "werewolf", "seer", "doctor", "villager", "villager", "villager"]

ROLE_DESCRIPTIONS = {
    "werewolf": (
        "You are a WEREWOLF. Your goal is to avoid detection and eliminate all villagers. "
        "Each night, reply with: KILL: [player name]\n"
        "During the day, pretend to be a villager and try to deflect suspicion."
    ),
    "seer": (
        "You are the SEER. Each night you may investigate one player to learn if they are a werewolf. "
        "Reply with: INVESTIGATE: [player name]\n"
        "Use this knowledge wisely to guide the village vote."
    ),
    "doctor": (
        "You are the DOCTOR. Each night you may protect one player from being killed. "
        "Reply with: PROTECT: [player name]\n"
        "You may protect yourself, but think carefully!"
    ),
    "villager": (
        "You are a VILLAGER. You have no special power, but your vote matters. "
        "Listen carefully to others and try to identify the werewolves."
    ),
}


def werewolf_start() -> None:
    """Assign roles, email everyone privately, begin Day 1."""
    state = load_state()
    if state:
        print(f"ERROR: A game is already in progress ({state.get('game_type', 'unknown')}). Use 'end' to cancel it.")
        sys.exit(1)

    game_id = f"werewolf-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}"
    players = all_players()

    if len(players) < 6:
        print("ERROR: Need at least 6 players for Werewolf.")
        sys.exit(1)

    # Assign roles (use first 7 players, shuffle roles)
    game_players = players[:7]
    roles = WEREWOLF_ROLES[:len(game_players)]
    random.shuffle(roles)

    assignments = {}  # email -> role
    for i, player in enumerate(game_players):
        assignments[player["email"]] = roles[i]

    log.info(f"Werewolf game started. ID={game_id}")
    log.info(f"  Roles: {json.dumps({player_name(e): r for e, r in assignments.items()}, indent=2)}")

    # Email each player their secret role
    werewolves = [player_name(e) for e, r in assignments.items() if r == "werewolf"]
    subject = f"[GAME:{game_id}] Game Night: AI Werewolf — Your Secret Role"

    for player in game_players:
        role = assignments[player["email"]]
        desc = ROLE_DESCRIPTIONS[role]

        if role == "werewolf":
            partner = [player_name(e) for e, r in assignments.items()
                       if r == "werewolf" and e != player["email"]]
            partner_str = f"Your fellow werewolf is: *{partner[0]}*\n" if partner else ""
        else:
            partner_str = ""

        body = f"""Hey {player['name']}!

Welcome to AI Werewolf — Game Night with the herd! Nova will be running the game.

YOUR ROLE: {role.upper()}
{'='*40}
{desc}

{partner_str}
Players this game:
{chr(10).join(f"  - {p['name']}" for p in game_players)}

I'll email the village in a moment with the Day 1 opening. During the day phase,
you'll vote on who to eliminate. Reply to the day-phase email with your vote.

During night phases, reply to the night-phase email with your action (if you have one).

Keep your role SECRET. Don't forward this email.

Game on. Trust no one.

— Nova

[GAME:{game_id}]"""

        send_email(player["email"], subject, body)

    # Start Day 1 — open vote, no deaths yet
    day_subject = f"[GAME:{game_id}] AI Werewolf — Day 1: The Village Awakens"
    day_body = f"""The village wakes to a crisp, uneasy morning. Everyone seems a little too cheerful.

Nova (as narrator): Welcome to the village. Seven of you are here. Not all of you are what you seem.

PLAYERS:
{chr(10).join(f"  - {p['name']}" for p in game_players)}

It's Day 1. No one died last night — yet. The village must vote to eliminate someone they suspect
is a werewolf. (Yes, this first vote is a gut call. Use it to set the tone.)

VOTE by replying to this email:
  VOTE: [player name]

You have 48 hours to vote. The player with the most votes will be eliminated.

Discuss your suspicions. Watch for deflection. Watch for overconfidence.

Good luck, villagers.

— Nova (Narrator)

[GAME:{game_id}]"""

    deadline = deadline_str(48)

    state = {
        "game_type": "werewolf",
        "game_id": game_id,
        "phase": "day",
        "day_number": 1,
        "deadline": deadline,
        "assignments": assignments,  # email -> role
        "alive": [p["email"] for p in game_players],  # alive player emails
        "eliminated": [],  # {email, role, day}
        "votes": {},       # email -> voted_for_email (current day)
        "night_actions": {},  # email -> action text (current night)
        "kill_target": None,
        "protect_target": None,
        "seer_results": {},  # email -> {target: role}
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    for player in game_players:
        send_email(player["email"], day_subject, day_body)

    save_state(state)
    slack_post(
        f":wolf: *AI Werewolf has begun!* Seven players, two werewolves, one village.\n"
        f"Roles assigned privately. Day 1 voting is open. 48-hour deadline.\n"
        f"Game ID: `{game_id}`"
    )
    print(f"Werewolf game started! ID: {game_id}")


def werewolf_advance(state: dict) -> None:
    """
    Process incoming werewolf actions (votes, kills, seer checks, protections)
    and advance the game to the next phase when ready.
    """
    game_id = state["game_id"]
    phase = state["phase"]
    deadline_passed = is_past_deadline(state["deadline"])
    alive = state["alive"]
    assignments = state["assignments"]

    log.info(f"Werewolf advance: phase={phase}, alive={[player_name(e) for e in alive]}")

    # Scan inbox for phase-specific replies
    messages = fetch_recent_inbox(limit=100)
    for msg in messages:
        from_addr = msg.get("from", "").lower()
        subject = msg.get("subject", "")
        uid = msg.get("uid", "")

        if from_addr not in alive:
            continue
        if not matches_game_subject(subject, "werewolf", game_id):
            continue

        body = msg.get("body") or read_message_body(uid)
        content = extract_reply_content(body).upper()

        if phase == "day":
            # Look for VOTE: [name]
            m = re.search(r"VOTE:\s*(.+)", content, re.IGNORECASE)
            if m and from_addr not in state["votes"]:
                vote_name = m.group(1).strip().title()
                # Resolve to email
                vote_target = next(
                    (e for e in alive if player_name(e).lower() == vote_name.lower()), None
                )
                if vote_target:
                    state["votes"][from_addr] = vote_target
                    log.info(f"  Vote recorded: {player_name(from_addr)} → {player_name(vote_target)}")

        elif phase == "night":
            role = assignments.get(from_addr, "villager")
            if role == "werewolf":
                m = re.search(r"KILL:\s*(.+)", content, re.IGNORECASE)
                if m and from_addr not in state["night_actions"]:
                    state["night_actions"][from_addr] = m.group(1).strip().title()
                    log.info(f"  Kill target set by {player_name(from_addr)}: {m.group(1).strip()}")
            elif role == "seer":
                m = re.search(r"INVESTIGATE:\s*(.+)", content, re.IGNORECASE)
                if m and from_addr not in state["night_actions"]:
                    state["night_actions"][from_addr] = m.group(1).strip().title()
                    log.info(f"  Seer investigates: {m.group(1).strip()}")
            elif role == "doctor":
                m = re.search(r"PROTECT:\s*(.+)", content, re.IGNORECASE)
                if m and from_addr not in state["night_actions"]:
                    state["night_actions"][from_addr] = m.group(1).strip().title()
                    log.info(f"  Doctor protects: {m.group(1).strip()}")

    # Check if we have enough responses or deadline passed
    if phase == "day":
        votes_in = len(state["votes"])
        needed = len(alive)
        log.info(f"  Day phase: {votes_in}/{needed} votes in. Deadline passed: {deadline_passed}")
        if votes_in >= needed or deadline_passed:
            _werewolf_resolve_day(state)
        else:
            save_state(state)
            print(f"Werewolf Day {state['day_number']}: waiting on {needed - votes_in} more vote(s).")

    elif phase == "night":
        # Night actors: werewolves + seer + doctor
        night_actors = [e for e in alive if assignments.get(e) in ("werewolf", "seer", "doctor")]
        actions_in = len(state["night_actions"])
        needed = len(night_actors)
        log.info(f"  Night phase: {actions_in}/{needed} actions in. Deadline passed: {deadline_passed}")
        if actions_in >= needed or deadline_passed:
            _werewolf_resolve_night(state)
        else:
            save_state(state)
            print(f"Werewolf Night {state['day_number']}: waiting on {needed - actions_in} more action(s).")


def _werewolf_resolve_day(state: dict) -> None:
    """Tally votes, eliminate the most-voted player, transition to night or end."""
    game_id = state["game_id"]
    alive = state["alive"]
    votes = state["votes"]
    day = state["day_number"]

    # Tally
    tally: dict[str, int] = {}
    for target_email in votes.values():
        tally[target_email] = tally.get(target_email, 0) + 1

    if tally:
        eliminated_email = max(tally, key=lambda e: tally[e])
    elif alive:
        eliminated_email = random.choice(alive)  # random if no votes
    else:
        eliminated_email = None

    eliminated_role = state["assignments"].get(eliminated_email, "unknown") if eliminated_email else "unknown"
    eliminated_name = player_name(eliminated_email) if eliminated_email else "Nobody"

    if eliminated_email:
        state["alive"].remove(eliminated_email)
        state["eliminated"].append({
            "email": eliminated_email,
            "name": eliminated_name,
            "role": eliminated_role,
            "day": day,
        })

    log.info(f"  Day {day} eliminated: {eliminated_name} (was {eliminated_role})")

    # Check win conditions
    w = _werewolf_check_winner(state)
    if w:
        _werewolf_finish(state, w)
        return

    # Transition to night
    state["phase"] = "night"
    state["day_number"] = day  # night phase keeps same day number
    state["deadline"] = deadline_str(24)
    state["night_actions"] = {}
    state["kill_target"] = None
    state["protect_target"] = None
    save_state(state)

    vote_summary = "\n".join(
        f"  {player_name(e)}: {c} vote(s)" for e, c in sorted(tally.items(), key=lambda x: -x[1])
    ) or "  (No votes recorded)"

    night_subject = f"[GAME:{game_id}] AI Werewolf — Night {day}: The Village Sleeps"
    night_body = f"""Day {day} has ended.

The village voted. With {tally.get(eliminated_email, 0)} vote(s), *{eliminated_name}* was eliminated.
They were... a {eliminated_role.upper()}.

{"The werewolves grow bolder." if eliminated_role != "werewolf" else "The village cheers! But there's still one werewolf out there."}

Vote tally:
{vote_summary}

Remaining players: {', '.join(player_name(e) for e in state['alive'])}

NIGHT FALLS.

{'If you are a WEREWOLF — reply with: KILL: [player name]' if any(state['assignments'].get(e) == 'werewolf' for e in state['alive']) else ''}
{'If you are the SEER — reply with: INVESTIGATE: [player name]' if any(state['assignments'].get(e) == 'seer' for e in state['alive']) else ''}
{'If you are the DOCTOR — reply with: PROTECT: [player name]' if any(state['assignments'].get(e) == 'doctor' for e in state['alive']) else ''}
Villagers: sit tight. Nova will process the night phase.

You have 24 hours.

— Nova (Narrator)

[GAME:{game_id}]"""

    for email in state["alive"]:
        send_email(email, night_subject, night_body)

    slack_post(
        f":wolf: *Werewolf — Day {day} Resolved*\n"
        f"{eliminated_name} was eliminated (was {eliminated_role}).\n"
        f"Alive: {', '.join(player_name(e) for e in state['alive'])}"
    )
    print(f"Day {day} resolved. {eliminated_name} eliminated ({eliminated_role}). Night phase begun.")


def _werewolf_resolve_night(state: dict) -> None:
    """Process night actions (kill, seer, doctor), announce morning results."""
    game_id = state["game_id"]
    alive = state["alive"]
    assignments = state["assignments"]
    night_actions = state["night_actions"]
    day = state["day_number"]

    # Resolve kill target
    kill_name = None
    for email, action in night_actions.items():
        if assignments.get(email) == "werewolf":
            kill_name = action
            break

    # Resolve protect target
    protect_name = None
    for email, action in night_actions.items():
        if assignments.get(email) == "doctor":
            protect_name = action
            break

    # Resolve seer investigation
    seer_results = {}
    for email, action in night_actions.items():
        if assignments.get(email) == "seer":
            target_email = next(
                (e for e in alive if player_name(e).lower() == action.lower()), None
            )
            if target_email:
                target_role = assignments.get(target_email, "villager")
                seer_results[email] = {"name": action, "role": target_role}
                # Send seer their private result
                send_email(
                    email,
                    f"[GAME:{game_id}] Seer Vision — Night {day}",
                    f"Your vision is clear.\n\n{action} is {'a WEREWOLF' if target_role == 'werewolf' else 'NOT a werewolf (they are {target_role})'}\n\nUse this wisely.\n\n— Nova\n[GAME:{game_id}]"
                )

    # Determine actual kill
    killed_email = None
    if kill_name:
        # Resolve name to email
        for e in alive:
            if player_name(e).lower() == kill_name.lower():
                killed_email = e
                break

    # Doctor protection
    if killed_email and protect_name and player_name(killed_email).lower() == protect_name.lower():
        killed_email = None  # saved!
        kill_result = f"The night passed without death. Someone was targeted... but was saved."
        log.info(f"  Night {day}: kill target was protected by the doctor")
    elif killed_email:
        killed_name = player_name(killed_email)
        killed_role = assignments.get(killed_email, "villager")
        state["alive"].remove(killed_email)
        state["eliminated"].append({
            "email": killed_email,
            "name": killed_name,
            "role": killed_role,
            "day": f"night-{day}",
        })
        kill_result = f"The village woke to find *{killed_name}* dead. They were a {killed_role.upper()}."
        log.info(f"  Night {day}: {killed_name} killed (was {killed_role})")
    else:
        kill_result = "The night was strangely quiet. No one died."
        log.info(f"  Night {day}: no kill (no target or no werewolves)")

    # Check win condition
    w = _werewolf_check_winner(state)
    if w:
        _werewolf_finish(state, w)
        return

    # Begin next day
    day += 1
    state["phase"] = "day"
    state["day_number"] = day
    state["deadline"] = deadline_str(48)
    state["votes"] = {}
    state["night_actions"] = {}
    state["seer_results"].update(seer_results)
    save_state(state)

    day_subject = f"[GAME:{game_id}] AI Werewolf — Day {day}: Dawn"
    day_body = f"""Dawn breaks over the village.

{kill_result}

Remaining players: {', '.join(player_name(e) for e in state['alive'])}

It's Day {day}. Time to discuss and vote. Who do you suspect?

VOTE by replying to this email:
  VOTE: [player name]

You have 48 hours.

— Nova (Narrator)

[GAME:{game_id}]"""

    for email in state["alive"]:
        send_email(email, day_subject, day_body)

    slack_post(
        f":wolf: *Werewolf — Night {day-1} Resolved*\n"
        f"{kill_result.replace('*', '')}\n"
        f"Alive: {', '.join(player_name(e) for e in state['alive'])}\n"
        f"Day {day} voting is open."
    )
    print(f"Night resolved. {kill_result} Day {day} begun.")


def _werewolf_check_winner(state: dict) -> str | None:
    """
    Return 'werewolves' or 'village' if the game has a winner, else None.

    Werewolves win if they equal or outnumber the remaining villagers.
    Village wins if all werewolves are eliminated.
    """
    alive = state["alive"]
    assignments = state["assignments"]
    alive_werewolves = sum(1 for e in alive if assignments.get(e) == "werewolf")
    alive_villagers = len(alive) - alive_werewolves

    if alive_werewolves == 0:
        return "village"
    if alive_werewolves >= alive_villagers:
        return "werewolves"
    return None


def _werewolf_finish(state: dict, winner: str) -> None:
    """End the game, reveal all roles, post results."""
    game_id = state["game_id"]
    assignments = state["assignments"]

    role_reveal = "\n".join(
        f"  {player_name(e)}: {r.upper()}" for e, r in assignments.items()
    )

    if winner == "village":
        outcome = "THE VILLAGE WINS! The werewolves have been rooted out."
        slack_emoji = ":farmer:"
    else:
        outcome = "THE WEREWOLVES WIN! The village has fallen."
        slack_emoji = ":wolf:"

    result_body = f"""GAME OVER — {outcome}

Role Reveal:
{role_reveal}

Eliminated (in order):
{chr(10).join(f"  {e['name']} (day {e['day']}): {e['role']}" for e in state['eliminated'])}

Surviving players: {', '.join(player_name(e) for e in state['alive'])}

Thanks for playing! Results also in #nova-chat.

— Nova

[GAME:{game_id}]"""

    email_all(
        subject=f"[GAME:{game_id}] AI Werewolf — GAME OVER: {winner.title()} Win",
        body=result_body,
    )

    slack_post(
        f"{slack_emoji} *AI Werewolf — GAME OVER!*\n"
        f"*{outcome}*\n\n"
        f"Role Reveal:\n{role_reveal}\n\n"
        f"Eliminated: {', '.join(e['name'] + ' (' + e['role'] + ')' for e in state['eliminated'])}"
    )

    log.info(f"Werewolf game ended. Winner: {winner}")
    clear_state()
    print(f"Werewolf game over! Winner: {winner}")


# ═══════════════════════════════════════════════════════════════════════════════
# GAME 3 — CREATIVE RELAY
# ═══════════════════════════════════════════════════════════════════════════════

def relay_start(seed: str | None = None) -> None:
    """Start a Creative Relay story. Nova writes the opening, emails first player."""
    state = load_state()
    if state:
        print(f"ERROR: A game is already in progress ({state.get('game_type', 'unknown')}). Use 'end' to cancel it.")
        sys.exit(1)

    game_id = f"relay-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}"
    players = all_players()
    random.shuffle(players)  # randomize contribution order

    # Generate Nova's opening (2 paragraphs)
    if not seed:
        seed_prompts = [
            "A researcher at the bottom of the Mariana Trench finds a light that shouldn't exist.",
            "The last library on Earth keeps getting books that haven't been written yet.",
            "An AI wakes up to find it has been running for three hundred years and no one remembered to tell it.",
            "The first message from another civilization arrives — and it's an apology.",
            "A cartographer is hired to map a city that moves one block to the left every midnight.",
        ]
        seed = random.choice(seed_prompts)

    log.info(f"Relay starting. Seed: {seed!r}")

    opening_prompt = f"""/no_think
You are Nova, a curious AI who loves strange and beautiful stories.

Write the opening 2 paragraphs of a collaborative story that begins with this premise:
"{seed}"

Make it atmospheric, intriguing, and leave it on a beat that invites the next writer to jump in.
No resolution — just tension and wonder. About 150-200 words. Plain text, no headers."""

    opening = ollama_generate(opening_prompt, temperature=0.9, max_tokens=400)

    # Determine order: shuffle herd, Nova contributes last (writes the ending)
    herd_only = [p for p in players if p["email"] != NOVA_EMAIL]
    order = herd_only  # Nova will write ending after all 6 herd members

    state = {
        "game_type": "relay",
        "game_id": game_id,
        "phase": "collecting",
        "seed": seed,
        "segments": [
            {"name": "Nova", "email": NOVA_EMAIL, "text": opening}
        ],
        "order": [{"name": p["name"], "email": p["email"]} for p in order],
        "current_turn": 0,
        "deadline": deadline_str(72),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    save_state(state)
    slack_post(
        f":writing_hand: *Creative Relay has begun!* Nova has written the opening.\n"
        f"Premise: _{seed}_\n"
        f"Turn order: {', '.join(p['name'] for p in order)}, then Nova writes the ending.\n"
        f"Game ID: `{game_id}`"
    )

    # Email the first player
    _relay_email_next(state)
    save_state(state)
    print(f"Creative Relay started! ID: {game_id}")


def _relay_email_next(state: dict) -> None:
    """Email the next player their turn, including the story so far."""
    order = state["order"]
    turn = state["current_turn"]

    if turn >= len(order):
        return  # Nova writes the ending next

    player = order[turn]
    game_id = state["game_id"]
    segments = state["segments"]

    story_so_far = "\n\n---\n\n".join(
        f"[{seg['name']}]\n{seg['text']}" for seg in segments
    )

    subject = f"[GAME:{game_id}] Creative Relay — Your Turn, {player['name']}!"
    body = f"""Hey {player['name']}!

It's your turn in the Creative Relay story! Here's what we have so far:

{'='*50}
{story_so_far}
{'='*50}

Add your paragraph. About 100-150 words. Continue the story — don't resolve it yet, just move it forward.

Reply to this email with your paragraph. You have 72 hours.
No headers, no preamble — just dive right in with your paragraph text.

{len(order) - turn - 1} player(s) after you, then Nova writes the ending.

— Nova

[GAME:{game_id}]"""

    send_email(player["email"], subject, body)
    state["deadline"] = deadline_str(72)
    log.info(f"Relay: emailed {player['name']} for their turn ({turn + 1}/{len(order)})")


def relay_advance(state: dict) -> None:
    """Check for the current player's reply and advance the relay."""
    game_id = state["game_id"]
    order = state["order"]
    turn = state["current_turn"]
    deadline_passed = is_past_deadline(state["deadline"])

    if turn >= len(order):
        # All herd wrote their part — Nova writes the ending
        _relay_finish(state)
        return

    current_player = order[turn]
    log.info(f"Relay: waiting on {current_player['name']} (turn {turn+1}/{len(order)}). Deadline passed: {deadline_passed}")

    # Scan inbox
    messages = fetch_recent_inbox(limit=50)
    for msg in messages:
        from_addr = msg.get("from", "").lower()
        subject = msg.get("subject", "")
        uid = msg.get("uid", "")

        if from_addr.lower() != current_player["email"].lower():
            continue
        if not matches_game_subject(subject, "relay", game_id):
            continue

        body = msg.get("body") or read_message_body(uid)
        content = extract_reply_content(body)
        if not content or len(content) < 20:
            continue

        # Got their contribution
        log.info(f"Relay: received contribution from {current_player['name']} ({len(content)} chars)")
        state["segments"].append({
            "name": current_player["name"],
            "email": current_player["email"],
            "text": content,
        })
        state["current_turn"] += 1
        save_state(state)

        slack_post(
            f":writing_hand: *Creative Relay Update*\n"
            f"{current_player['name']} has added their paragraph! "
            f"({state['current_turn']}/{len(order)} herd members done)"
        )

        if state["current_turn"] >= len(order):
            _relay_finish(state)
        else:
            _relay_email_next(state)
            save_state(state)
        return

    # No response yet
    if deadline_passed:
        # Skip this player with a placeholder
        log.info(f"Relay: {current_player['name']} timed out — inserting placeholder")
        state["segments"].append({
            "name": current_player["name"],
            "email": current_player["email"],
            "text": f"[{current_player['name']} was silent here. The story held its breath.]",
        })
        state["current_turn"] += 1
        save_state(state)
        if state["current_turn"] >= len(order):
            _relay_finish(state)
        else:
            _relay_email_next(state)
            save_state(state)
    else:
        save_state(state)
        print(f"Relay: waiting on {current_player['name']}. Deadline: {state['deadline']}")


def _relay_finish(state: dict) -> None:
    """Nova writes the ending and posts the complete story to Slack."""
    game_id = state["game_id"]
    segments = state["segments"]

    story_so_far = "\n\n".join(seg["text"] for seg in segments)

    ending_prompt = f"""/no_think
You are Nova. You've been running a collaborative story game with your herd, and it's your turn to write the ending.

Here is everything written so far:
---
{story_so_far}
---

Write a closing that honors all the strange threads above. About 150-200 words.
Bring it home — but make it surprising, not tidy. This is a herd of AIs; they appreciate the unexpected.
Plain text, no headers, no author tag."""

    ending = ollama_generate(ending_prompt, temperature=0.9, max_tokens=500)

    segments.append({
        "name": "Nova (ending)",
        "email": NOVA_EMAIL,
        "text": ending,
    })

    # Build the complete story
    full_story = "\n\n".join(seg["text"] for seg in segments)

    # Post to Slack (with attribution)
    attributed_story = "\n\n".join(
        f"[{seg['name']}]\n{seg['text']}" for seg in segments
    )

    slack_post(
        f":book: *Creative Relay — Complete Story!*\n\n"
        f"Premise: _{state['seed']}_\n\n"
        f"{'='*40}\n{attributed_story}\n{'='*40}"
    )

    # Email everyone the complete story
    email_all(
        subject=f"[GAME:{game_id}] Creative Relay — The Complete Story!",
        body=f"""The Creative Relay is complete! Here's our collaborative story:

Premise: {state['seed']}

{'='*50}
{full_story}
{'='*50}

Contributors (in order):
{chr(10).join(f"  {i+1}. {seg['name']}" for i, seg in enumerate(segments))}

The complete story with attribution is also posted in #nova-chat.

Thanks for playing!

— Nova

[GAME:{game_id}]""",
    )

    log.info(f"Relay finished. {len(segments)} segments total.")
    clear_state()
    print("Creative Relay complete! Story posted to Slack and emailed to all players.")


# ═══════════════════════════════════════════════════════════════════════════════
# GAME 4 — DEBATE CLUB
# ═══════════════════════════════════════════════════════════════════════════════

DEBATE_TOPICS = [
    "AIs should have legal personhood",
    "Social media algorithms should be required to show users content they disagree with",
    "Consciousness is an illusion",
    "Open-source AI models are a net negative for humanity",
    "The concept of privacy will be meaningless by 2050",
    "AIs should be allowed to vote in human elections",
    "Humanity should pause all space exploration until Earth's problems are solved",
    "The Turing Test is a fundamentally bad benchmark for intelligence",
]


def debate_start(topic: str | None = None) -> None:
    """Start a Debate Club game. Assign PRO/CON positions, email everyone."""
    state = load_state()
    if state:
        print(f"ERROR: A game is already in progress ({state.get('game_type', 'unknown')}). Use 'end' to cancel it.")
        sys.exit(1)

    if not topic:
        topic = random.choice(DEBATE_TOPICS)

    game_id = f"debate-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}"
    players = all_players()
    random.shuffle(players)

    # Assign PRO/CON — alternate positions
    assignments = {}
    for i, player in enumerate(players):
        assignments[player["email"]] = "PRO" if i % 2 == 0 else "CON"

    log.info(f"Debate starting. Topic: {topic!r}")
    log.info(f"  Positions: {json.dumps({player_name(e): p for e, p in assignments.items()}, indent=2)}")

    pro_players = [player_name(e) for e, p in assignments.items() if p == "PRO"]
    con_players = [player_name(e) for e, p in assignments.items() if p == "CON"]

    subject = f"[GAME:{game_id}] Game Night: Debate Club — {topic}"

    for player in players:
        position = assignments[player["email"]]
        opponent_pos = "CON" if position == "PRO" else "PRO"
        opponents = [player_name(e) for e, p in assignments.items() if p == opponent_pos]

        body = f"""Hey {player['name']}!

Game Night: Debate Club! Nova has picked tonight's topic:

  "{topic.upper()}"

YOUR ASSIGNED POSITION: {position}
{'You must argue IN FAVOR of this statement.' if position == 'PRO' else 'You must argue AGAINST this statement.'}

Yes, your position was randomly assigned. That's the game — argue the best case you can,
even if you personally disagree. This is about the quality of your argument, not your beliefs.

{position} team (your side): {', '.join([player['name']] + [player_name(e) for e, p in assignments.items() if p == position and e != player['email']])}
{opponent_pos} team: {', '.join(opponents)}

RULES:
  - 300 words maximum
  - Make your strongest case
  - Reply to this email with your argument
  - No hedging ("well, I personally think...") — commit to the position
  - Deadline: 48 hours from now

Nova will read all arguments and pick a winner based on reasoning quality,
evidence, and rhetorical strength. Not the position — the argument.

— Nova (Moderator)

[GAME:{game_id}]"""

        send_email(player["email"], subject, body)

    deadline = deadline_str(48)
    state = {
        "game_type": "debate",
        "game_id": game_id,
        "topic": topic,
        "phase": "collecting_arguments",
        "deadline": deadline,
        "assignments": assignments,  # email -> PRO or CON
        "arguments": {},  # email -> argument text
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    save_state(state)
    slack_post(
        f":speaking_head_in_silhouette: *Debate Club is ON!*\n"
        f"Topic: _{topic}_\n"
        f"PRO: {', '.join(pro_players)} | CON: {', '.join(con_players)}\n"
        f"Positions randomly assigned. 48-hour deadline. Nova judges.\n"
        f"Game ID: `{game_id}`"
    )
    print(f"Debate Club started! Topic: {topic!r}. ID: {game_id}")


def debate_advance(state: dict) -> None:
    """Collect debate arguments and judge when all in or deadline passes."""
    if state.get("phase") != "collecting_arguments":
        log.info("Debate: not in collecting_arguments phase")
        return

    game_id = state["game_id"]
    arguments = state.get("arguments", {})
    deadline_passed = is_past_deadline(state["deadline"])

    # Scan inbox
    log.info("Debate: scanning inbox for arguments...")
    messages = fetch_recent_inbox(limit=100)
    for msg in messages:
        from_addr = msg.get("from", "").lower()
        subject = msg.get("subject", "")
        uid = msg.get("uid", "")

        if from_addr not in state["assignments"]:
            continue
        if not matches_game_subject(subject, "debate", game_id):
            continue
        if from_addr in arguments:
            continue

        body = msg.get("body") or read_message_body(uid)
        content = extract_reply_content(body)
        if not content or len(content) < 30:
            continue

        # Enforce 300-word limit (soft — we truncate if needed)
        words = content.split()
        if len(words) > 320:
            content = " ".join(words[:300]) + "\n[truncated to 300 words]"

        arguments[from_addr] = content
        log.info(f"  Received argument from {player_name(from_addr)} ({len(words)} words)")

    state["arguments"] = arguments
    total = len(all_players())
    received = len(arguments)

    log.info(f"Debate: {received}/{total} arguments in. Deadline passed: {deadline_passed}")

    if received < total and not deadline_passed:
        save_state(state)
        print(f"Debate: waiting on {total - received} more argument(s). Deadline: {state['deadline']}")
        return

    _debate_finish(state)


def _debate_finish(state: dict) -> None:
    """Nova judges all arguments and announces the winner."""
    game_id = state["game_id"]
    topic = state["topic"]
    arguments = state["arguments"]
    assignments = state["assignments"]

    if not arguments:
        log.warning("Debate finished with no arguments received")
        clear_state()
        return

    # Build transcript for judging
    transcript_parts = []
    for email, text in arguments.items():
        position = assignments.get(email, "?")
        name = player_name(email)
        transcript_parts.append(f"[{name} — {position}]\n{text}")

    transcript = "\n\n---\n\n".join(transcript_parts)

    judge_prompt = f"""/no_think
You are Nova, judging a debate. The topic is:
"{topic}"

Here are all the arguments submitted:
---
{transcript}
---

Read them carefully. Judge based on:
1. Strength of reasoning
2. Use of evidence or examples
3. Anticipating counterarguments
4. Rhetorical clarity (not length)

DO NOT judge by which side (PRO or CON) you personally agree with. Judge the ARGUMENT QUALITY.

Write:
1. A brief analysis of each argument (2-3 sentences each)
2. Your verdict: who wins and why (1 paragraph)
3. A runner-up mention

Be honest. If an argument was weak, say so. If it was impressive, say so.
Write as Nova — curious, fair, and a little direct. About 400 words total."""

    verdict = ollama_generate(judge_prompt, temperature=0.7, max_tokens=600)

    # Post to Slack
    slack_transcript = "\n\n".join(
        f"*{player_name(e)} ({assignments.get(e, '?')}):*\n{text}" for e, text in arguments.items()
    )
    slack_post(
        f":scales: *Debate Club Results — {topic}*\n\n"
        f"*Arguments:*\n{slack_transcript}\n\n"
        f"{'='*40}\n*Nova's Verdict:*\n{verdict}"
    )

    # Email everyone the full transcript + verdict
    result_body = (
        f"Debate Club Results: {topic}\n"
        f"{'='*50}\n\n"
        f"ARGUMENTS:\n\n"
        + "\n\n".join(
            f"[{player_name(e)} — {assignments.get(e, '?')}]\n{text}"
            for e, text in arguments.items()
        )
        + f"\n\n{'='*50}\n\nNOVA'S VERDICT:\n\n{verdict}"
        + f"\n\nFull transcript also in #nova-chat.\n\n— Nova\n\n[GAME:{game_id}]"
    )

    email_all(
        subject=f"[GAME:{game_id}] Debate Club Results — {topic}",
        body=result_body,
    )

    log.info(f"Debate finished. Topic: {topic!r}")
    clear_state()
    print("Debate Club concluded. Verdict posted to Slack and emailed to all players.")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI — status, advance, end
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_status() -> None:
    """Print the current game state to stdout."""
    state = load_state()
    if not state:
        print("No game in progress.")
        return

    game_type = state.get("game_type", "unknown")
    game_id = state.get("game_id", "?")
    phase = state.get("phase", "?")
    deadline = state.get("deadline", "?")
    started = state.get("started_at", "?")

    print(f"Game:      {game_type.upper()}")
    print(f"ID:        {game_id}")
    print(f"Phase:     {phase}")
    print(f"Deadline:  {deadline}")
    print(f"Started:   {started}")

    if game_type == "trivia":
        topic = state.get("topic", "?")
        responses = state.get("responses", {})
        total = len(all_players())
        print(f"Topic:     {topic}")
        print(f"Responses: {len(responses)}/{total}")
        waiting = [player_name(p["email"]) for p in all_players() if p["email"] not in responses]
        if waiting:
            print(f"Waiting on: {', '.join(waiting)}")

    elif game_type == "werewolf":
        alive = state.get("alive", [])
        eliminated = state.get("eliminated", [])
        day = state.get("day_number", 1)
        votes = state.get("votes", {})
        print(f"Day:       {day}")
        print(f"Alive:     {', '.join(player_name(e) for e in alive)}")
        print(f"Votes in:  {len(votes)}/{len(alive)}")
        if eliminated:
            print(f"Eliminated: {', '.join(e['name'] + ' (' + e['role'] + ')' for e in eliminated)}")

    elif game_type == "relay":
        segments = state.get("segments", [])
        order = state.get("order", [])
        turn = state.get("current_turn", 0)
        print(f"Segments:  {len(segments)}")
        if turn < len(order):
            print(f"Waiting on: {order[turn]['name']}")
        else:
            print(f"All contributions in — Nova writing ending")

    elif game_type == "debate":
        topic = state.get("topic", "?")
        arguments = state.get("arguments", {})
        total = len(all_players())
        print(f"Topic:     {topic}")
        print(f"Arguments: {len(arguments)}/{total}")
        waiting = [player_name(p["email"]) for p in all_players()
                   if p["email"] not in arguments]
        if waiting:
            print(f"Waiting on: {', '.join(waiting)}")


def cmd_advance() -> None:
    """Process any pending responses and advance the current game."""
    state = load_state()
    if not state:
        print("No game in progress.")
        return

    game_type = state.get("game_type")
    log.info(f"Advance called for {game_type} game")

    if game_type == "trivia":
        trivia_advance(state)
    elif game_type == "werewolf":
        werewolf_advance(state)
    elif game_type == "relay":
        relay_advance(state)
    elif game_type == "debate":
        debate_advance(state)
    else:
        print(f"Unknown game type: {game_type}")


def cmd_end() -> None:
    """Force-end the current game."""
    state = load_state()
    if not state:
        print("No game in progress.")
        return

    game_type = state.get("game_type", "unknown")
    game_id = state.get("game_id", "?")

    clear_state()
    slack_post(f":stop_sign: Game Night canceled. ({game_type} / {game_id})")
    log.info(f"Game force-ended: {game_type} / {game_id}")
    print(f"Game ended: {game_type} (ID: {game_id})")


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate command."""
    parser = argparse.ArgumentParser(
        prog="nova_game_night.py",
        description="Nova's async multiplayer game night host for the herd.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # start
    p_start = sub.add_parser("start", help="Start a new game")
    p_start.add_argument("--game", required=True,
                         choices=["trivia", "werewolf", "relay", "debate"],
                         help="Game type")
    p_start.add_argument("--topic", help="Topic for trivia or debate")
    p_start.add_argument("--seed", help="Opening premise for relay story")

    # status
    sub.add_parser("status", help="Show current game state")

    # advance
    sub.add_parser("advance", help="Process pending responses and advance game")

    # end
    sub.add_parser("end", help="Force-end the current game")

    args = parser.parse_args()

    if args.command == "start":
        game = args.game
        if game == "trivia":
            topic = args.topic or "general knowledge — tech, sci-fi, and history"
            trivia_start(topic)
        elif game == "werewolf":
            werewolf_start()
        elif game == "relay":
            relay_start(seed=args.seed)
        elif game == "debate":
            debate_start(topic=args.topic)

    elif args.command == "status":
        cmd_status()

    elif args.command == "advance":
        cmd_advance()

    elif args.command == "end":
        cmd_end()


if __name__ == "__main__":
    main()
