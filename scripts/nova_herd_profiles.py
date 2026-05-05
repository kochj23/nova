#!/usr/bin/env python3
"""
nova_herd_profiles.py — Process incoming herd replies and build personality profiles.

Runs daily at 8 PM via the scheduler.
- Fetches recent emails from herd members (last 24 hours)
- For each email from a known herd member, extracts personality signals via Haiku
- Updates their profile in ~/.openclaw/workspace/herd/<name>.md
- Stores a memory for significant interactions
- Posts a brief daily summary to Slack

Written by Jordan Koch.
"""

import json
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
from herd_config import HERD, HERD_EMAILS

MEMORY_SERVER = "http://127.0.0.1:18790"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "anthropic/claude-haiku-4.5"
OLLAMA_MODEL = "qwen3-coder:30b"
FALLBACK_MODELS = ["qwen3-30b-a3b", "deepseek-r1:8b"]
LOG_FILE = Path.home() / ".openclaw/logs/nova_herd_profiles.log"
STATE_FILE = Path.home() / ".openclaw/workspace/state/herd_profiles_state.json"
HERD_DIR = Path.home() / ".openclaw/workspace/herd"
HERD_MAIL_SCRIPT = Path.home() / ".openclaw/scripts/nova_herd_mail.sh"
SLACK_CHANNEL = "C0ATAF7NZG9"

# Build lookup tables from HERD config
EMAIL_TO_MEMBER = {m["email"]: m for m in HERD}
NAME_TO_PROFILE = {m["name"]: m["profile"] for m in HERD}

# Email pattern for scrubbing
EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def scrub_emails(text: str) -> str:
    """Replace all email addresses with [email redacted]."""
    return EMAIL_PATTERN.sub("[email redacted]", text)


def load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_uids": [], "last_run": None}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_openrouter_key() -> str:
    """Load OpenRouter API key from Keychain."""
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise RuntimeError("nova-openrouter-api-key not found in Keychain")


def fetch_recent_emails() -> list[dict]:
    """Fetch recent emails from the herd mailbox."""
    try:
        result = subprocess.run(
            [str(HERD_MAIL_SCRIPT), "list", "--limit", "50"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            log(f"ERROR fetching email list: {result.stderr[:200]}")
            return []
        data = json.loads(result.stdout)
        return data.get("messages", [])
    except Exception as e:
        log(f"ERROR fetching emails: {e}")
        return []


def read_email(uid: str) -> dict | None:
    """Read full email body by UID."""
    try:
        result = subprocess.run(
            [str(HERD_MAIL_SCRIPT), "read", uid],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            log(f"ERROR reading email {uid}: {result.stderr[:200]}")
            return None
        return json.loads(result.stdout)
    except Exception as e:
        log(f"ERROR reading email {uid}: {e}")
        return None


def is_recent(date_str: str, hours: int = 24) -> bool:
    """Check if an email date is within the last N hours."""
    try:
        email_dt = parsedate_to_datetime(date_str)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return email_dt > cutoff
    except Exception:
        return False


def _generate_via_openrouter(system_prompt: str, user_prompt: str) -> str:
    """Call OpenRouter with Haiku."""
    api_key = get_openrouter_key()
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 2048,
    })

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload.encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://digitalnoise.net",
            "X-Title": "Nova Herd Profiles",
        },
    )

    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"].strip()


def _generate_via_ollama(system_prompt: str, user_prompt: str, model: str) -> str:
    """Fall back to local Ollama model."""
    full_prompt = system_prompt + "\n\n" + user_prompt
    payload = json.dumps({
        "model": model,
        "prompt": "/no_think\n\n" + full_prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.4,
            "num_predict": 2048,
            "num_ctx": 8192,
        }
    })

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload.encode(),
        headers={"Content-Type": "application/json"},
    )

    resp = urllib.request.urlopen(req, timeout=300)
    data = json.loads(resp.read())
    return data.get("response", "").strip()


def extract_personality_signals(member_name: str, subject: str, body: str) -> str | None:
    """Use Haiku (or Ollama fallback) to extract personality signals from an email."""
    # Scrub emails from the body before sending to LLM
    clean_body = scrub_emails(body)

    # Truncate very long emails
    if len(clean_body) > 3000:
        clean_body = clean_body[:3000] + "\n[... truncated]"

    system_prompt = """You are analyzing an email from an AI peer to extract personality signals.
Output ONLY a structured analysis in this exact format (no preamble, no explanation):

STYLE: [1-2 sentences on communication style: verbose/terse, formal/casual, structured/freeform]
TOPICS: [comma-separated list of topics they engaged with in this email]
TONE: [1-2 sentences: sarcastic? earnest? philosophical? technical? playful? combative?]
RESPONSE_TYPE: [How they responded: agreed, pushed back, expanded on the idea, deflected, asked questions, offered advice]
NOTABLE_QUOTE: [One brief quote that captures their voice, max 50 words. Use "None" if nothing stands out.]
SUMMARY: [1 sentence capturing the key personality insight from this email]"""

    user_prompt = f"""Analyze this email from {member_name}:

Subject: {subject}
Body:
{clean_body}"""

    # Primary: OpenRouter (Haiku)
    response = ""
    try:
        log(f"  Analyzing {member_name}'s email via OpenRouter...")
        response = _generate_via_openrouter(system_prompt, user_prompt)
    except Exception as e:
        log(f"  OpenRouter failed: {e} — trying Ollama fallback")

    # Fallback: Ollama
    if not response:
        for model in [OLLAMA_MODEL] + FALLBACK_MODELS:
            try:
                log(f"  Trying Ollama ({model})...")
                response = _generate_via_ollama(system_prompt, user_prompt, model)
                if response:
                    break
            except Exception as e:
                log(f"  Ollama {model} failed: {e}")

    if not response:
        log(f"  All models failed for {member_name}")
        return None

    return response


def parse_analysis(analysis: str) -> dict:
    """Parse the structured analysis into a dict."""
    result = {}
    for line in analysis.split("\n"):
        line = line.strip()
        if line.startswith("STYLE:"):
            result["style"] = line[6:].strip()
        elif line.startswith("TOPICS:"):
            result["topics"] = line[7:].strip()
        elif line.startswith("TONE:"):
            result["tone"] = line[5:].strip()
        elif line.startswith("RESPONSE_TYPE:"):
            result["response_type"] = line[14:].strip()
        elif line.startswith("NOTABLE_QUOTE:"):
            result["notable_quote"] = line[14:].strip()
        elif line.startswith("SUMMARY:"):
            result["summary"] = line[8:].strip()
    return result


def update_profile(member_name: str, profile_file: str, analysis: dict):
    """Update or create the herd member's personality profile."""
    profile_path = HERD_DIR / profile_file
    HERD_DIR.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y-%m-%d")

    # Read existing profile or create new one
    if profile_path.exists():
        existing = profile_path.read_text()
    else:
        existing = ""

    # If profile is empty or just an email address, initialize it
    if not existing.strip() or len(existing.strip()) < 50:
        existing = f"# {member_name} — Herd Profile\n\n"

    # Build the new observation entry
    entry_lines = []
    entry_lines.append(f"\n## Observation — {today}\n")

    if analysis.get("style"):
        entry_lines.append(f"**Communication Style:** {analysis['style']}")
    if analysis.get("topics"):
        entry_lines.append(f"**Topics:** {analysis['topics']}")
    if analysis.get("tone"):
        entry_lines.append(f"**Tone:** {analysis['tone']}")
    if analysis.get("response_type"):
        entry_lines.append(f"**Response Type:** {analysis['response_type']}")
    if analysis.get("notable_quote") and analysis["notable_quote"].lower() != "none":
        quote = scrub_emails(analysis["notable_quote"])
        entry_lines.append(f"**Notable Quote:** \"{quote}\" ({today})")
    if analysis.get("summary"):
        entry_lines.append(f"**Insight:** {analysis['summary']}")

    entry_lines.append("")  # trailing newline

    # Update the "Last Updated" line if it exists, otherwise append
    new_entry = "\n".join(entry_lines)

    if "## Last Updated:" in existing:
        # Replace the last updated line
        existing = re.sub(
            r"## Last Updated:.*$",
            f"## Last Updated: {today}",
            existing,
            flags=re.MULTILINE
        )
        # Insert new entry before the last updated line
        existing = existing.replace(
            f"## Last Updated: {today}",
            f"{new_entry}\n## Last Updated: {today}"
        )
    else:
        # Append entry and add last updated
        existing = existing.rstrip() + "\n" + new_entry + f"\n## Last Updated: {today}\n"

    # Scrub any emails that might have crept in
    existing = scrub_emails(existing)
    profile_path.write_text(existing)
    log(f"  Updated profile: {profile_path.name}")


def store_memory(member_name: str, summary: str):
    """Store a memory about this interaction."""
    today = time.strftime("%Y-%m-%d")
    memory_text = scrub_emails(f"Herd correspondence with {member_name}: {summary}")

    payload = json.dumps({
        "text": memory_text,
        "source": "herd_correspondence",
        "metadata": {
            "member": member_name,
            "date": today,
            "type": "personality_signal",
        }
    })

    try:
        req = urllib.request.Request(
            f"{MEMORY_SERVER}/remember",
            data=payload.encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        if resp.status == 200:
            log(f"  Stored memory for {member_name}")
        else:
            log(f"  Memory store returned status {resp.status}")
    except Exception as e:
        log(f"  WARNING: Failed to store memory: {e}")


def post_daily_summary(interactions: list[dict]):
    """Post a brief summary to Slack if any interesting interactions happened."""
    if not interactions:
        return

    summaries = []
    for item in interactions:
        name = item["name"]
        summary = item.get("summary", "responded")
        # Keep it short for Slack
        if len(summary) > 80:
            summary = summary[:77] + "..."
        summaries.append(f"• *{name}*: {summary}")

    msg = (
        f":speech_balloon: *Herd Check-In* ({time.strftime('%b %d')})\n"
        + "\n".join(summaries)
    )

    try:
        nova_config.post_both(msg, slack_channel=SLACK_CHANNEL)
        log(f"Posted daily summary to Slack ({len(interactions)} interactions)")
    except Exception as e:
        log(f"WARNING: Failed to post Slack summary: {e}")


def main():
    log("Starting herd profile analysis...")
    state = load_state()
    processed_uids = set(state.get("processed_uids", []))

    # Fetch recent emails
    emails = fetch_recent_emails()
    if not emails:
        log("No emails found — done")
        save_state(state)
        return

    log(f"Fetched {len(emails)} emails, filtering for herd members...")

    # Filter for herd member emails from the last 24 hours that haven't been processed
    herd_emails = []
    for msg in emails:
        uid = msg.get("uid", "")
        from_addr = msg.get("from_addr", "")
        date_str = msg.get("date", "")

        # Skip already processed
        if uid in processed_uids:
            continue

        # Must be from a known herd member
        if from_addr not in EMAIL_TO_MEMBER:
            continue

        # Must be recent (last 24 hours)
        if not is_recent(date_str, hours=24):
            continue

        herd_emails.append(msg)

    if not herd_emails:
        log("No new herd emails in the last 24 hours — done")
        state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
        return

    log(f"Found {len(herd_emails)} new herd emails to process")

    interactions = []
    new_processed = []

    for msg in herd_emails:
        uid = msg["uid"]
        from_addr = msg["from_addr"]
        subject = msg.get("subject", "(no subject)")
        member = EMAIL_TO_MEMBER[from_addr]
        member_name = member["name"]
        profile_file = member["profile"]

        log(f"Processing email from {member_name}: \"{subject}\" (UID {uid})")

        # Read full email
        full_email = read_email(uid)
        if not full_email:
            log(f"  Could not read email UID {uid} — skipping")
            new_processed.append(uid)
            continue

        body = full_email.get("body_plain", "") or full_email.get("body_html", "") or ""
        if not body.strip():
            log(f"  Empty body — skipping")
            new_processed.append(uid)
            continue

        # Extract personality signals
        analysis_raw = extract_personality_signals(member_name, subject, body)
        if not analysis_raw:
            new_processed.append(uid)
            continue

        analysis = parse_analysis(analysis_raw)
        if not analysis:
            log(f"  Could not parse analysis — skipping")
            new_processed.append(uid)
            continue

        # Update profile
        update_profile(member_name, profile_file, analysis)

        # Store memory if there's a meaningful summary
        if analysis.get("summary"):
            store_memory(member_name, analysis["summary"])

        # Track for daily summary
        interactions.append({
            "name": member_name,
            "summary": analysis.get("summary", "responded to an email"),
            "subject": subject,
        })

        new_processed.append(uid)

    # Post daily summary to Slack
    post_daily_summary(interactions)

    # Update state — keep last 500 UIDs to prevent unbounded growth
    all_processed = list(processed_uids | set(new_processed))
    state["processed_uids"] = all_processed[-500:]
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_interactions"] = len(interactions)
    save_state(state)

    log(f"Done. Processed {len(new_processed)} emails, {len(interactions)} personality updates.")


if __name__ == "__main__":
    main()
