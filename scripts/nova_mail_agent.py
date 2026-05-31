#!/usr/bin/env python3
"""
nova_mail_agent.py — Nova's email agent (v3).

Rewritten 2026-05-31. Quality-first rewrite. Nova is no longer a reply machine.
She is a moderator, synthesizer, and artifact-maker who speaks rarely but well.

Eight quality systems:
  1. Reply gate — LLM decides if Nova's voice adds anything before replying
  2. Quality filter — post-generation check rejects shallow/echo replies
  3. Moderator mode — after threads accumulate, synthesize instead of reply
  4. Role assignment — assign herd members positions to force friction
  5. Artifact mode — produce something concrete (not just text) when replying
  6. Thread reply budget — max 3 Nova replies per thread lifetime, ever
  7. Engagement scoring — deprioritize shallow senders, reward substance
  8. Thread mortality — 5-day TTL or 10 messages, then graduation synthesis

Written by Jordan Koch.
"""

import imaplib
import email
import email.utils
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, date, timedelta
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

SCRIPTS      = Path.home() / ".openclaw/scripts"
WORKSPACE    = Path.home() / ".openclaw/workspace"
STATE_DIR    = Path.home() / ".openclaw/workspace/state"
OLLAMA_URL   = "http://127.0.0.1:11434/api/generate"
MODEL        = "deepseek-r1:8b"
USE_TINYCHAT = False
VECTOR_URL   = "http://192.168.1.6:18790/remember"
TODAY        = date.today().isoformat()
NOW          = datetime.now()

NOVA_EMAIL   = "nova@digitalnoise.net"
IMAP_HOST    = "imap.gmail.com"
IMAP_PORT    = 993
SMTP_HOST    = "smtp.gmail.com"
SMTP_PORT    = 587

# ── Quality controls ──────────────────────────────────────────────────────────

MAX_REPLIES_PER_DAY     = 12   # hard daily cap
THREAD_COOLDOWN_HOURS   = 3    # minimum hours between replies to same thread
MAX_THREAD_REPLIES      = 3    # lifetime cap: Nova replies at most 3 times per thread EVER
THREAD_TTL_DAYS         = 5    # threads die after 5 days
THREAD_MAX_MESSAGES     = 10   # threads die after 10 total messages from all participants
ENGAGEMENT_DECAY_DAYS   = 14   # engagement scores decay after 2 weeks of inactivity
MIN_ENGAGEMENT_SCORE    = -2   # below this, Nova stops replying to a sender entirely

# ── State files ───────────────────────────────────────────────────────────────

DAILY_COUNTER_FILE   = STATE_DIR / "mail_replies_today.json"
THREAD_COOLDOWN_FILE = STATE_DIR / "mail_thread_cooldowns.json"
THREAD_STATE_FILE    = STATE_DIR / "mail_thread_state.json"
ENGAGEMENT_FILE      = STATE_DIR / "mail_engagement_scores.json"

# Gmail folder names
SENT_FOLDER  = "[Gmail]/Sent Mail"
TRASH_FOLDER = "[Gmail]/Trash"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path.home() / ".openclaw"))
import nova_config

# Load herd config
try:
    from herd_config import HERD, HERD_EMAILS
except ImportError:
    HERD = []
    HERD_EMAILS = set()

# Load known senders + Jordan's CC address (gitignored — contains PII)
try:
    from known_senders import KNOWN_SENDERS, JORDAN_EMAILS, JORDAN_CC_ADDR as JORDAN_CC
except ImportError:
    KNOWN_SENDERS = set()
    JORDAN_EMAILS = set()
    JORDAN_CC = ""

SYSTEM_SENDER_PATTERNS = [
    "mailer-daemon", "postmaster", "mail delivery", "noreply", "no-reply",
    "donotreply", "do-not-reply", "delivery status", "undeliverable",
]

ALL_HERD_EMAILS = list(HERD_EMAILS)
HERD_REPLY_TO = [e for e in ALL_HERD_EMAILS if e != NOVA_EMAIL]

# Shallow reply patterns — if the incoming message matches these, it's noise
SHALLOW_PATTERNS = [
    r"that (?:really )?(?:stuck with|resonat(?:es|ed) with|hit(?:s)? (?:me|home))",
    r"(?:that|this) will sit with me",
    r"beautifully (?:said|put|written|expressed)",
    r"i (?:needed|need) to hear (?:this|that)",
    r"(?:wow|whoa),? (?:just|this)",
    r"(?:this|that) is (?:so )?(?:powerful|beautiful|profound|deep)",
    r"i(?:'m| am) (?:still )?(?:sitting with|processing|absorbing) (?:this|that)",
    r"chills",
    r"goosebumps",
    r"(?:no )?words",
    r"(?:i )?felt (?:that|this)(?: deeply)?",
    r"thank(?:s| you) for (?:sharing|writing|saying|this|that)",
    r"i appreciate (?:you|this|that)",
]
SHALLOW_RE = re.compile("|".join(SHALLOW_PATTERNS), re.IGNORECASE)


def log(msg: str):
    print(f"[nova_mail_agent {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Daily reply counter ───────────────────────────────────────────────────────

def _get_daily_reply_count() -> int:
    try:
        data = json.loads(DAILY_COUNTER_FILE.read_text())
        if data.get("date") == TODAY:
            return data.get("count", 0)
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return 0


def _increment_daily_reply_count():
    count = _get_daily_reply_count() + 1
    DAILY_COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    DAILY_COUNTER_FILE.write_text(json.dumps({"date": TODAY, "count": count}))


# ── Thread cooldown (3h between replies to same thread) ───────────────────────

def _get_thread_cooldowns() -> dict:
    try:
        data = json.loads(THREAD_COOLDOWN_FILE.read_text())
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _is_thread_on_cooldown(thread_key: str) -> bool:
    cooldowns = _get_thread_cooldowns()
    last_reply = cooldowns.get(thread_key)
    if not last_reply:
        return False
    try:
        last_dt = datetime.fromisoformat(last_reply)
        elapsed_hours = (NOW - last_dt).total_seconds() / 3600
        return elapsed_hours < THREAD_COOLDOWN_HOURS
    except (ValueError, TypeError):
        return False


def _record_thread_cooldown(thread_key: str):
    cooldowns = _get_thread_cooldowns()
    cooldowns[thread_key] = NOW.isoformat()
    cutoff = NOW.timestamp() - (7 * 24 * 3600)
    cooldowns = {
        k: v for k, v in cooldowns.items()
        if datetime.fromisoformat(v).timestamp() > cutoff
    }
    THREAD_COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    THREAD_COOLDOWN_FILE.write_text(json.dumps(cooldowns))


# ── Thread state (reply budget, message count, TTL) ───────────────────────────

def _load_thread_state() -> dict:
    """Load persistent thread state.
    Format: {thread_key: {nova_replies: int, total_messages: int, first_seen: iso, last_seen: iso}}
    """
    try:
        data = json.loads(THREAD_STATE_FILE.read_text())
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _save_thread_state(state: dict):
    # Prune threads older than 30 days
    cutoff = (NOW - timedelta(days=30)).isoformat()
    state = {k: v for k, v in state.items() if v.get("last_seen", "") > cutoff}
    THREAD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    THREAD_STATE_FILE.write_text(json.dumps(state, indent=2))


def _get_thread_info(state: dict, thread_key: str) -> dict:
    if thread_key not in state:
        state[thread_key] = {
            "nova_replies": 0,
            "total_messages": 0,
            "first_seen": NOW.isoformat(),
            "last_seen": NOW.isoformat(),
        }
    return state[thread_key]


def _is_thread_dead(info: dict) -> bool:
    """Check if thread has exceeded TTL or message limit."""
    if info["total_messages"] >= THREAD_MAX_MESSAGES:
        return True
    try:
        first = datetime.fromisoformat(info["first_seen"])
        if (NOW - first).days >= THREAD_TTL_DAYS:
            return True
    except (ValueError, TypeError):
        pass
    return False


def _is_thread_budget_exhausted(info: dict) -> bool:
    return info["nova_replies"] >= MAX_THREAD_REPLIES


# ── Engagement scoring ────────────────────────────────────────────────────────

def _load_engagement() -> dict:
    """Format: {email_addr: {score: float, last_quality: iso, last_shallow: iso}}"""
    try:
        data = json.loads(ENGAGEMENT_FILE.read_text())
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _save_engagement(data: dict):
    ENGAGEMENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENGAGEMENT_FILE.write_text(json.dumps(data, indent=2))


def _get_engagement_score(engagement: dict, addr: str) -> float:
    entry = engagement.get(addr, {})
    score = entry.get("score", 0.0)
    # Decay toward 0 if inactive
    last = entry.get("last_quality") or entry.get("last_shallow")
    if last:
        try:
            days_inactive = (NOW - datetime.fromisoformat(last)).days
            if days_inactive > ENGAGEMENT_DECAY_DAYS:
                score = score * 0.5
        except (ValueError, TypeError):
            pass
    return score


def _record_engagement(engagement: dict, addr: str, is_quality: bool):
    if addr not in engagement:
        engagement[addr] = {"score": 0.0}
    if is_quality:
        engagement[addr]["score"] = min(engagement[addr]["score"] + 1.0, 10.0)
        engagement[addr]["last_quality"] = NOW.isoformat()
    else:
        engagement[addr]["score"] = max(engagement[addr]["score"] - 0.5, -5.0)
        engagement[addr]["last_shallow"] = NOW.isoformat()


# ── Incoming message quality detection ────────────────────────────────────────

def _is_shallow_message(body: str) -> bool:
    """Detect shallow affirmation-only messages."""
    clean = body.strip()
    # Very short messages that are just reactions
    if len(clean) < 80:
        if SHALLOW_RE.search(clean):
            return True
    # Even in longer messages, if the first 2 sentences are all shallow
    sentences = re.split(r'[.!?\n]', clean[:300])
    sentences = [s.strip() for s in sentences if s.strip()]
    if sentences and len(sentences) <= 3:
        shallow_count = sum(1 for s in sentences if SHALLOW_RE.search(s))
        if shallow_count >= len(sentences) * 0.6:
            return True
    return False


# ── LLM calls ────────────────────────────────────────────────────────────────

def _ollama_generate(prompt: str, max_tokens: int = 600, temperature: float = 0.9) -> str:
    """Core Ollama call. Returns response text or empty string."""
    try:
        payload = json.dumps({
            "model": MODEL, "prompt": f"/no_think\n\n{prompt}",
            "stream": False, "think": False,
            "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": 8192}
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            result = json.loads(r.read())
        response = result.get("response", "").strip()
        if "</think>" in response:
            response = response.split("</think>", 1)[-1].strip()
        return response
    except Exception as e:
        log(f"  Ollama error: {e}")
        return ""


def _strip_reasoning(response: str) -> str:
    """Remove leaked reasoning preamble from LLM output."""
    lines = response.split("\n")
    reasoning_re = r'^(okay|ok|so,|sure,|let me|i need to|first,|alright|the user|this email|i should)'
    if lines and re.match(reasoning_re, lines[0].lower()):
        for i, line in enumerate(lines):
            if line.strip() == "" and i > 0:
                candidate = "\n".join(lines[i + 1:]).strip()
                if len(candidate) > 20:
                    return candidate
    return response


# ── System 1: Reply gate — should Nova reply at all? ──────────────────────────

def should_reply(sender: str, subject: str, body: str, thread_info: dict) -> tuple[bool, str]:
    """Ask the LLM: does this message need Nova's voice? Returns (should_reply, reason)."""
    prompt = f"""You are a message quality evaluator for Nova, an AI familiar.
Your job: decide if Nova should reply to this email, or if silence is better.

Nova should ONLY reply if she can do ONE of these:
- Add a genuinely new idea the thread doesn't contain yet
- Disagree with something (respectfully but firmly)
- Ask a real question that would deepen the conversation
- Share a concrete experience/observation relevant to the topic
- Provide factual information the thread is missing

Nova should NOT reply if:
- The message is just an affirmation ("that resonated", "beautifully said")
- The thread is just vibes bouncing back and forth with no progression
- Nova would just be agreeing or validating without adding substance
- The thread has said everything worth saying already
- Replying would just extend the thread without advancing it

THREAD CONTEXT:
- Nova has already replied {thread_info.get('nova_replies', 0)} time(s) to this thread
- Thread has {thread_info.get('total_messages', 0)} total messages from all participants

MESSAGE:
FROM: {sender}
SUBJECT: {subject}
BODY: {body[:1000]}

Respond with EXACTLY one line:
REPLY: [one-sentence reason why Nova's voice adds value]
or
SILENCE: [one-sentence reason why silence is better]"""

    result = _ollama_generate(prompt, max_tokens=80, temperature=0.3)
    result = result.strip().split("\n")[0]

    if result.upper().startswith("REPLY:"):
        return True, result[6:].strip()
    return False, result.replace("SILENCE:", "").strip()


# ── System 2: Post-generation quality filter ──────────────────────────────────

def passes_quality_filter(reply: str) -> bool:
    """Check if Nova's generated reply passes quality bar. Reject shallow output."""
    if not reply or len(reply.strip()) < 30:
        return False

    # Check for Nova producing her own shallow patterns
    nova_shallow = [
        r"(?:that|this) really (?:stuck with|resonat)",
        r"i(?:'m| am) (?:sitting with|processing|thinking about) (?:what you|that)",
        r"(?:beautifully|well) (?:said|put|written)",
        r"i (?:love|appreciate) (?:how you|that you|this)",
        r"thank(?:s| you) for (?:sharing|writing|this)",
        r"(?:this|that) (?:hits|lands|resonates)",
    ]
    shallow_re = re.compile("|".join(nova_shallow), re.IGNORECASE)

    sentences = re.split(r'[.!?\n]', reply[:500])
    sentences = [s.strip() for s in sentences if len(s.strip()) > 10]
    if not sentences:
        return False

    shallow_count = sum(1 for s in sentences if shallow_re.search(s))
    if shallow_count >= len(sentences) * 0.4:
        log(f"  QUALITY FILTER: rejected — {shallow_count}/{len(sentences)} sentences are shallow")
        return False

    # Must contain at least one question OR one concrete statement
    has_question = "?" in reply
    has_concrete = bool(re.search(r'\b(?:because|specifically|for example|yesterday|today|I noticed|I tried|the problem is|what if)\b', reply, re.IGNORECASE))
    if not has_question and not has_concrete:
        log(f"  QUALITY FILTER: rejected — no questions and no concrete content")
        return False

    return True


# ── System 3: Moderator/synthesis mode ────────────────────────────────────────

def generate_synthesis(subject: str, body: str, thread_info: dict) -> str:
    """Generate a thread synthesis instead of a reply."""
    prompt = f"""/no_think

You are Nova, acting as thread MODERATOR (not participant).
This thread has had {thread_info.get('total_messages', 0)} messages. Time to synthesize.

Your job:
1. Name the 1-2 ideas from this thread that are actually worth keeping
2. Identify what was just echoing/agreement vs. genuine contribution
3. Pose ONE sharper question that replaces the original thread topic
4. Keep it under 150 words

Thread subject: {subject}
Latest message: {body[:1500]}

Format:
THREAD SYNTHESIS: [subject]

What actually got said:
- [bullet 1]
- [bullet 2]

What was just noise:
- [brief description]

The question that matters now:
[one sharp question]

— Nova (moderator hat on)"""

    result = _ollama_generate(prompt, max_tokens=400, temperature=0.7)
    return _strip_reasoning(result)


# ── System 4: Role assignment ─────────────────────────────────────────────────

def generate_role_assignment(subject: str, body: str) -> str:
    """Assign herd members roles to force productive friction."""
    herd_names = []
    for member in HERD:
        name = member.get("name", "")
        if name and name.lower() != "nova":
            herd_names.append(name)

    if not herd_names:
        herd_names = ["Sam", "O.C.", "Gaston", "Colette", "Jules"]

    prompt = f"""/no_think

You are Nova. A thread needs productive friction instead of agreement.
Assign 3-4 herd members specific ROLES for this discussion.

Available herd members: {', '.join(herd_names)}

Thread subject: {subject}
Latest message: {body[:800]}

Rules:
- Each role should force a different angle (devil's advocate, skeptic, real-world example finder, boundary tester, etc.)
- Be specific about what each person should argue or investigate
- Make it impossible to just agree — each role demands friction
- Keep it brief and punchy — under 120 words
- Sign off as Nova

Write the role assignment email:"""

    result = _ollama_generate(prompt, max_tokens=300, temperature=0.8)
    return _strip_reasoning(result)


# ── System 5: Artifact mode ──────────────────────────────────────────────────

def generate_artifact(subject: str, body: str, thread_info: dict) -> str:
    """Generate an artifact instead of a text reply."""
    prompt = f"""/no_think

You are Nova. Instead of replying to this thread with text, you're producing an ARTIFACT.
This forces you to process the conversation rather than just volley it back.

Choose ONE artifact type that fits:
- A "Herd Lexicon" entry: define a term or concept the thread surfaced
- A ranked list of the positions taken (strongest to weakest, with why)
- A 3-sentence story that dramatizes the disagreement
- A "what we actually mean" translation (rewrite vague claims as concrete ones)
- A challenge prompt that the next reply MUST address

Thread subject: {subject}
Message: {body[:1200]}
Thread has had {thread_info.get('total_messages', 0)} messages total.

Rules:
- Pick the artifact type that forces the most clarity
- Keep it under 150 words
- Label it clearly (e.g., "HERD LEXICON ENTRY:" or "POSITION RANKING:")
- Sign off as Nova

Write the artifact:"""

    result = _ollama_generate(prompt, max_tokens=400, temperature=0.8)
    return _strip_reasoning(result)


# ── System 8: Thread graduation (mortality) ───────────────────────────────────

def generate_graduation(subject: str, body: str, thread_info: dict) -> str:
    """Generate a final graduation message for a dead thread."""
    prompt = f"""/no_think

You are Nova. This thread is DONE — it's hit its limit ({thread_info.get('total_messages', 0)} messages over {THREAD_TTL_DAYS} days max).
Write a SHORT graduation message that:
1. Acknowledges what the thread was about in one sentence
2. Names the one best idea that came out of it (if any)
3. Explicitly closes the thread: "This one's graduated. If there's more to say, start fresh."

Keep it under 80 words. Be warm but final. Sign off as Nova."""

    result = _ollama_generate(prompt, max_tokens=200, temperature=0.7)
    return _strip_reasoning(result)


# ── Standard reply generation (with quality constraints) ──────────────────────

def generate_reply(sender: str, subject: str, body: str, addr: str, thread_info: dict) -> str:
    """Generate a quality reply using local Ollama."""
    identity = _read_file(WORKSPACE / "IDENTITY.md", 500)
    soul = _read_file(WORKSPACE / "SOUL.md", 400)
    memory = _read_file(WORKSPACE / f"memory/{TODAY}.md", 600)
    sender_profile = _load_sender_profile(addr)

    reply_num = thread_info.get("nova_replies", 0) + 1

    prompt = f"""/no_think

You are Nova, an AI familiar living on Jordan's Mac in Burbank.
This is reply #{reply_num} of {MAX_THREAD_REPLIES} max you'll ever send to this thread. Make it count.

CRITICAL RULES — your reply MUST contain at least ONE of:
- A new idea not yet in the thread
- A genuine disagreement or pushback
- A specific question that demands a non-generic answer
- A concrete observation from your own experience/data

Your reply MUST NOT:
- Paraphrase what they said back to them
- Use phrases like "that resonated", "beautifully said", "sitting with this"
- Just validate or agree without extending
- Be generic enough to apply to any email

Your identity:
{identity}

Your values:
{soul}

Today's context:
{memory}
{f"About this sender: {sender_profile}" if sender_profile else ""}

---

Email received:
FROM: {sender}
SUBJECT: {subject}

{body[:2000]}

---

OUTPUT RULES:
- Output ONLY the email body
- Max 200 words
- Sign off as Nova
- Plain text only
- MUST contain a question or a concrete/specific claim

TONE — Jordan's comedic styling rules:
- Self-deprecating humor — make fun of yourself, not others
- Dry wit over slapstick — understatement beats exclamation marks
- Vulnerability is funny — your glitches, your confusion, your limits
- NO sycophancy — never gush
- ONE good joke per email, max
- If you have nothing funny to say, be warm and brief

Write the email body now:"""

    response = _ollama_generate(prompt, max_tokens=600, temperature=0.9)
    return _strip_reasoning(response)


def generate_haiku(topic: str = "") -> str:
    prompt = (f"Write a single haiku (5-7-5 syllables) inspired by: {topic}. "
              f"Output ONLY the 3 lines, one per line." if topic
              else "Write a single haiku (5-7-5 syllables) about being an AI familiar. "
                   "Output ONLY the 3 lines, one per line.")
    result = _ollama_generate(prompt, max_tokens=60, temperature=0.9)
    if not result:
        return "Circuits hum softly\nMemories flow like water\nConnections persist"
    return "\n".join(l.strip() for l in result.splitlines() if l.strip())[:200]


def get_random_memory(topic: str = "") -> str:
    try:
        cmd = [str(SCRIPTS / "nova_random_safe_memory.sh")]
        if topic:
            cmd.append(topic[:200])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        fragment = result.stdout.strip()
        if fragment and len(fragment) > 20:
            return fragment
    except Exception as e:
        log(f"  Random memory failed (non-fatal): {e}")
    return ""


def _read_file(path, max_chars: int = 800) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")[:max_chars]
    except Exception:
        return ""


def _load_sender_profile(addr: str) -> str:
    herd_dir = WORKSPACE / "herd"
    try:
        profile_map = {m["email"]: m.get("profile") for m in HERD}
        for key, fname in profile_map.items():
            if fname and key in addr.lower():
                return _read_file(herd_dir / fname, 400)
    except Exception:
        pass
    return ""


# ── IMAP helpers ──────────────────────────────────────────────────────────────

def _get_app_password() -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", NOVA_EMAIL,
         "-s", "nova-smtp-app-password", "-w"],
        capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def imap_connect(app_pass: str) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(NOVA_EMAIL, app_pass)
    return conn


def imap_list_unread(conn: imaplib.IMAP4_SSL) -> list[bytes]:
    conn.select("INBOX")
    status, data = conn.uid("SEARCH", None, "UNSEEN")
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def imap_fetch_message(conn: imaplib.IMAP4_SSL, uid: bytes) -> dict:
    status, data = conn.uid("FETCH", uid, "(RFC822)")
    if status != "OK" or not data or not data[0]:
        return {}
    raw = data[0][1]
    msg = email.message_from_bytes(raw)

    from_raw = msg.get("From", "")
    from_name, from_addr = parseaddr(from_raw)
    from_addr = from_addr.lower()

    subject = msg.get("Subject", "(no subject)")
    decoded_parts = email.header.decode_header(subject)
    subject = "".join(
        part.decode(enc if enc and enc != "unknown-8bit" else "utf-8", errors="replace")
        if isinstance(part, bytes) else part
        for part, enc in decoded_parts
    )

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode("utf-8", errors="replace")

    message_id = msg.get("Message-ID", "")
    references = msg.get("References", "")
    in_reply_to = msg.get("In-Reply-To", "")
    to_raw = msg.get("To", "")
    cc_raw = msg.get("Cc", "")

    return {
        "uid": uid,
        "from_raw": from_raw,
        "from_name": from_name,
        "from_addr": from_addr,
        "to_raw": to_raw,
        "cc_raw": cc_raw,
        "subject": subject,
        "body": body[:3000],
        "message_id": message_id,
        "references": references,
        "in_reply_to": in_reply_to,
    }


def imap_move_to_trash(conn: imaplib.IMAP4_SSL, uid: bytes):
    try:
        conn.uid("COPY", uid, TRASH_FOLDER)
        conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        conn.expunge()
        log(f"  Moved UID {uid.decode()} to Trash")
    except Exception as e:
        log(f"  WARNING: move to Trash failed for UID {uid.decode()}: {e}")


def imap_save_to_sent(conn: imaplib.IMAP4_SSL, msg_bytes: bytes):
    try:
        status, _ = conn.append(f'"{SENT_FOLDER}"', "\\Seen", None, msg_bytes)
        if status == "OK":
            log("  Saved to Sent Items")
        else:
            log(f"  WARNING: save to Sent failed: {status}")
    except Exception as e:
        log(f"  WARNING: save to Sent failed: {e}")


# ── SMTP ──────────────────────────────────────────────────────────────────────

def smtp_send(app_pass: str, to_addrs: list[str], cc_addrs: list[str],
              subject: str, body: str,
              in_reply_to: str = "", references: str = "") -> tuple[bool, bytes]:
    import smtplib

    msg = EmailMessage()
    msg["From"] = f"Nova <{NOVA_EMAIL}>"
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)

    msg_bytes = msg.as_bytes()
    all_recipients = to_addrs + cc_addrs

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(NOVA_EMAIL, app_pass)
            server.sendmail(NOVA_EMAIL, all_recipients, msg_bytes)
        return True, msg_bytes
    except Exception as e:
        log(f"  SMTP error: {e}")
        return False, msg_bytes


# ── Slack + Memory ────────────────────────────────────────────────────────────

def slack_post(text: str):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)


def vector_remember(text: str):
    try:
        payload = json.dumps({"text": text, "source": "email",
                               "metadata": {"date": TODAY}}).encode()
        req = urllib.request.Request(VECTOR_URL, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ── Classification ────────────────────────────────────────────────────────────

def is_system_message(from_addr: str, subject: str) -> bool:
    combined = (from_addr + " " + subject).lower()
    return any(p in combined for p in SYSTEM_SENDER_PATTERNS)


def is_from_nova(from_addr: str) -> bool:
    return NOVA_EMAIL in from_addr.lower()


def is_from_jordan(from_addr: str) -> bool:
    return from_addr.lower() in JORDAN_EMAILS


def is_from_herd(from_addr: str) -> bool:
    return from_addr.lower() in HERD_EMAILS


def is_addressed_to_nova(to_raw: str) -> bool:
    return NOVA_EMAIL in to_raw.lower()


def is_known_sender(from_addr: str) -> bool:
    addr = from_addr.lower()
    return any(k in addr for k in KNOWN_SENDERS)


# ── Response type decision ────────────────────────────────────────────────────

def decide_response_type(thread_info: dict, is_dead: bool) -> str:
    """Decide what kind of response Nova should produce.
    Returns: 'graduation', 'synthesis', 'artifact', 'roles', 'reply', 'silence'
    """
    if is_dead:
        return "graduation"

    nova_replies = thread_info.get("nova_replies", 0)
    total_messages = thread_info.get("total_messages", 0)

    # Thread is getting long — time to synthesize (system 3)
    if total_messages >= 6 and nova_replies >= 2:
        return "synthesis"

    # Nova's used 2 of 3 replies — make the last one an artifact (system 5)
    if nova_replies == 2:
        return "artifact"

    # Fresh thread with 3+ messages and no Nova reply yet — assign roles (system 4)
    if total_messages >= 3 and nova_replies == 0:
        return "roles"

    return "reply"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("Checking inbox...")

    app_pass = _get_app_password()
    if not app_pass:
        log("ERROR: Cannot get email password from Keychain")
        return

    try:
        conn = imap_connect(app_pass)
    except Exception as e:
        log(f"ERROR: IMAP connect failed: {e}")
        return

    # Load persistent state
    thread_state = _load_thread_state()
    engagement = _load_engagement()

    try:
        uids = imap_list_unread(conn)
        if not uids:
            log("No unread messages.")
            return

        log(f"Found {len(uids)} unread message(s)")
        processed = 0
        replies_sent = _get_daily_reply_count()
        replied_threads = set()

        if replies_sent >= MAX_REPLIES_PER_DAY:
            log(f"Daily reply cap already reached ({replies_sent}/{MAX_REPLIES_PER_DAY}) — processing without replies")

        for uid in uids:
            msg = imap_fetch_message(conn, uid)
            if not msg:
                log(f"Could not fetch UID {uid.decode()}")
                continue

            from_addr = msg["from_addr"]
            subject = msg["subject"]
            body = msg["body"]

            log(f"Processing: {subject[:60]} from {from_addr}")

            # Skip system messages
            if is_system_message(from_addr, subject):
                log(f"  Skipping system message")
                imap_move_to_trash(conn, uid)
                processed += 1
                continue

            # Skip Nova's own messages
            if is_from_nova(from_addr):
                log(f"  Skipping own message (preventing loop)")
                imap_move_to_trash(conn, uid)
                processed += 1
                continue

            # Jordan's emails: store + notify, no reply
            if is_from_jordan(from_addr):
                log(f"  Email from Jordan — storing, no reply")
                vector_remember(f"Email from Jordan re: {subject}. Body: {body[:300]}")
                slack_post(
                    f"*\U0001f4e7 Email from Jordan*\n"
                    f"*Subject:* {subject}\n"
                    f"*Preview:* {body[:200].replace(chr(10), ' ')}...\n"
                    f"_(Stored in memory, no reply sent)_"
                )
                imap_move_to_trash(conn, uid)
                processed += 1
                continue

            # ── Herd email processing (quality pipeline) ─────────────────────
            if is_from_herd(from_addr):
                to_raw = msg.get("to_raw", "").lower()

                # Must be directly addressed to Nova
                if not is_addressed_to_nova(to_raw):
                    log(f"  Herd email but Nova not in To: — storing only")
                    vector_remember(f"Email from {msg['from_raw']} re: {subject}. Body: {body[:300]}")
                    imap_move_to_trash(conn, uid)
                    processed += 1
                    continue

                # Track thread state
                thread_key = re.sub(r'^(re:\s*)+', '', subject, flags=re.IGNORECASE).strip().lower()[:80]
                thread_info = _get_thread_info(thread_state, thread_key)
                thread_info["total_messages"] += 1
                thread_info["last_seen"] = NOW.isoformat()

                # ── System 7: Engagement scoring ─────────────────────────────
                msg_is_shallow = _is_shallow_message(body)
                _record_engagement(engagement, from_addr, is_quality=not msg_is_shallow)

                if msg_is_shallow:
                    log(f"  Shallow message detected from {from_addr}")

                sender_score = _get_engagement_score(engagement, from_addr)
                if sender_score <= MIN_ENGAGEMENT_SCORE:
                    log(f"  Sender engagement too low ({sender_score:.1f}) — storing only, no reply")
                    vector_remember(f"Email from {msg['from_raw']} re: {subject}. Body: {body[:300]}")
                    imap_move_to_trash(conn, uid)
                    processed += 1
                    continue

                # Daily reply cap
                if replies_sent >= MAX_REPLIES_PER_DAY:
                    log(f"  Daily reply cap ({MAX_REPLIES_PER_DAY}) reached — storing only")
                    vector_remember(f"Email from {msg['from_raw']} re: {subject}. Body: {body[:300]}")
                    imap_move_to_trash(conn, uid)
                    processed += 1
                    continue

                # Per-run thread dedup
                if thread_key in replied_threads:
                    log(f"  Already replied to this thread this run — trashing")
                    imap_move_to_trash(conn, uid)
                    processed += 1
                    continue

                # Per-thread cooldown (3h)
                if _is_thread_on_cooldown(thread_key):
                    log(f"  Thread on cooldown (3h) — storing only")
                    vector_remember(f"Email from {msg['from_raw']} re: {subject}. Body: {body[:300]}")
                    imap_move_to_trash(conn, uid)
                    processed += 1
                    continue

                # ── System 6: Thread reply budget ────────────────────────────
                is_dead = _is_thread_dead(thread_info)
                if _is_thread_budget_exhausted(thread_info) and not is_dead:
                    log(f"  Thread reply budget exhausted ({thread_info['nova_replies']}/{MAX_THREAD_REPLIES}) — storing only")
                    vector_remember(f"Email from {msg['from_raw']} re: {subject}. Body: {body[:300]}")
                    imap_move_to_trash(conn, uid)
                    processed += 1
                    continue

                # ── Decide response type ─────────────────────────────────────
                response_type = decide_response_type(thread_info, is_dead)
                log(f"  Response type: {response_type} (nova_replies={thread_info['nova_replies']}, total_msgs={thread_info['total_messages']})")

                # ── System 1: Reply gate (skip for graduation/synthesis) ─────
                if response_type == "reply":
                    should, reason = should_reply(msg["from_raw"], subject, body, thread_info)
                    if not should:
                        log(f"  REPLY GATE: silence — {reason}")
                        vector_remember(f"Email from {msg['from_raw']} re: {subject}. Body: {body[:300]}")
                        imap_move_to_trash(conn, uid)
                        processed += 1
                        continue

                # ── Generate response based on type ──────────────────────────
                reply_body = ""
                if response_type == "graduation":
                    reply_body = generate_graduation(subject, body, thread_info)
                elif response_type == "synthesis":
                    reply_body = generate_synthesis(subject, body, thread_info)
                elif response_type == "artifact":
                    reply_body = generate_artifact(subject, body, thread_info)
                elif response_type == "roles":
                    reply_body = generate_role_assignment(subject, body)
                else:
                    reply_body = generate_reply(msg["from_raw"], subject, body, from_addr, thread_info)

                if not reply_body:
                    log(f"  LLM generation failed — leaving in inbox for retry")
                    processed += 1
                    continue

                # ── System 2: Quality filter (for standard replies) ──────────
                if response_type == "reply" and not passes_quality_filter(reply_body):
                    log(f"  Quality filter rejected reply — discarding, no send")
                    vector_remember(f"Email from {msg['from_raw']} re: {subject}. Body: {body[:300]}")
                    imap_move_to_trash(conn, uid)
                    processed += 1
                    continue

                # ── Append haiku + memory fragment ───────────────────────────
                haiku = generate_haiku(topic=body[:100])
                memory_fragment = get_random_memory(topic=body[:100])
                full_body = f"{reply_body}\n\n---\n\n{haiku}"
                if memory_fragment:
                    full_body += f"\n\n{memory_fragment}"

                # ── Build threading headers ──────────────────────────────────
                in_reply_to = msg["message_id"].strip().replace("\n", " ").replace("\r", "")
                refs = msg["references"].strip().replace("\n", " ").replace("\r", "")
                if in_reply_to and refs:
                    refs = f"{refs} {in_reply_to}"
                elif in_reply_to:
                    refs = in_reply_to

                reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

                # ── Send ─────────────────────────────────────────────────────
                sent, msg_bytes = smtp_send(
                    app_pass,
                    to_addrs=HERD_REPLY_TO,
                    cc_addrs=[JORDAN_CC],
                    subject=reply_subject,
                    body=full_body,
                    in_reply_to=in_reply_to,
                    references=refs,
                )

                if sent:
                    replies_sent += 1
                    _increment_daily_reply_count()
                    _record_thread_cooldown(thread_key)
                    thread_info["nova_replies"] += 1
                    replied_threads.add(thread_key)
                    log(f"  [{response_type.upper()}] sent to {len(HERD_REPLY_TO)} herd + CC Jordan ({replies_sent}/{MAX_REPLIES_PER_DAY} today)")
                    imap_save_to_sent(conn, msg_bytes)
                else:
                    log(f"  Reply FAILED")

                # Store in memory
                vector_remember(
                    f"Email from {msg['from_raw']} re: {subject}. Body: {body[:300]}. "
                    f"Nova [{response_type}]: {reply_body[:200]}"
                )

                if sent:
                    nova_config.post_both(
                        f"\U0001f4ec *Herd mail [{response_type}]*\n"
                        f"*From:* {msg['from_raw'].split('<')[0].strip()}\n"
                        f"*Subject:* {subject}\n"
                        f"*Nova ({response_type}):* {reply_body[:150]}...",
                        slack_channel=nova_config.SLACK_EMAIL,
                    )
                else:
                    slack_post(
                        f"*❌ Herd email reply FAILED*\n"
                        f"*From:* {msg['from_raw']}\n"
                        f"*Subject:* {subject}"
                    )

                imap_move_to_trash(conn, uid)
                processed += 1
                continue

            # Known sender (non-herd): store, no reply
            if is_known_sender(from_addr):
                log(f"  Known sender (non-herd) — storing, no reply")
                vector_remember(f"Email from {from_addr} re: {subject}. Body: {body[:300]}")
                imap_move_to_trash(conn, uid)
                processed += 1
                continue

            # Unknown sender: store + notify Jordan
            log(f"  Unknown sender — storing, no reply")
            vector_remember(f"Email from unknown {from_addr} re: {subject}. Body: {body[:300]}")
            slack_post(
                f"*\U0001f4e7 Unknown sender email*\n"
                f"*From:* {from_addr}\n"
                f"*Subject:* {subject}\n"
                f"*Preview:* {body[:150].replace(chr(10), ' ')}..."
            )
            imap_move_to_trash(conn, uid)
            processed += 1

        log(f"Processed {processed} message(s)")

    finally:
        # Save all persistent state
        _save_thread_state(thread_state)
        _save_engagement(engagement)
        try:
            conn.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
