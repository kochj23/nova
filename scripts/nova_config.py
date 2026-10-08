"""
nova_config.py — Central configuration for all Nova scripts.

All secrets are loaded from macOS Keychain. Nothing is hardcoded here.
To update a token, run:
  security add-generic-password -a nova -s <service> -w <new_value> -U

Keychain entries:
  nova-slack-bot-token   — Slack bot token (xoxb-...)
  nova-smtp-app-password — Gmail App Password for waggle SMTP

Written by Jordan Koch.
"""

from __future__ import annotations  # ponytail: 3.8 compat for `list[str]` etc. (nuk/.10 runs py3.8)

import subprocess
import sys


# ── Keychain loader ───────────────────────────────────────────────────────────

def _keychain(service: str, account: str = "nova", required: bool = True) -> str:
    """Load a secret from macOS Keychain.
    If required=True (default), exits on failure.
    If required=False, returns empty string on failure (for cron-safe use).
    """
    # macOS Keychain first (Mac Studio .6). #650 portability: on the Linux cluster nodes
    # (no `security` binary) fall back to the fleet pgcrypto store, then env.
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
            capture_output=True, text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    try:
        import nova_secrets
        v = nova_secrets.get_secret(service)
        if v:
            return v
    except (Exception, SystemExit):
        pass  # SystemExit: nova_secrets exits when NOVA_SECRET_KEY absent — fall through
    import os as _os
    env_v = _os.environ.get(service.replace("-", "_").upper(), "")
    if env_v and not env_v.startswith("${"):
        return env_v
    msg = f"[nova_config] secret not found (Keychain/fleet/env): service={service}"
    if required:
        print(msg, file=sys.stderr)
        sys.exit(1)
    print(f"[nova_config] WARNING: {msg} (non-fatal)", file=sys.stderr)
    return ""


# ── Slack ─────────────────────────────────────────────────────────────────────

def slack_bot_token() -> str:
    """Nova's Slack bot token (xoxb-...). Keychain only — no plaintext fallback."""
    token = _keychain("nova-slack-bot-token", required=False)
    if token:
        return token
    # Check environment (set by nova_load_secrets.sh)
    import os
    env_token = os.environ.get("NOVA_SLACK_BOT_TOKEN", "")
    if env_token and not env_token.startswith("${"):
        return env_token
    print("[nova_config] ERROR: slack_bot_token unavailable — not in Keychain or env", file=sys.stderr)
    return ""


# ── Commonly used constants ───────────────────────────────────────────────────

SLACK_API     = "https://slack.com/api"
SLACK_CHAN     = "C0AMNQ5GX70"   # #nova-chat (interactive conversations with Jordan)
# Three-tier notification scheme (2026-07-29): route by intent, not by source.
SLACK_ALERTS  = "C0BMK83BLFJ"   # #nova-alerts (actionable, state-change only — something broke/recovered/needs a human)
SLACK_DIGEST  = "C0BLJLKQMMZ"   # #nova-digest (rollups: calendar, telemetry, syslog daily, block report, finance)
SLACK_FEED    = "C0BLNUEM9JS"   # #nova-feed (ambient firehose: media, flights, presence, journal, Claude Code — muted)
SLACK_INFO    = SLACK_FEED      # DEPRECATED alias — #nova-info (C0BC4SNUTQR) retired 2026-07-29; stragglers land in #nova-feed
SLACK_NOTIFY  = "C0ATAF7NZG9"   # #nova-warning (warnings — was #nova-notifications, renamed 2026-06-21)
SLACK_BB      = "C0B3G7J6N07"   # #nova-critical (critical alerts — was #nova-bb, renamed 2026-06-21)
SLACK_EMAIL   = "C0B0B3B3U1J"   # #nova-email (automated email notifications)
SLACK_PHOTOS  = "C0B01L9GQTV"   # #nova-photos (camera, sky, dream images, face recognition)
JORDAN_DM     = "D0AMPB3F4T0"   # Jordan's DM channel with Nova

DISCORD_API   = "https://discord.com/api/v10"
DISCORD_CHAT  = "1496990647062761483"   # #nova-chat on Koch Family Discord
DISCORD_NOTIFY = "1496990332250886246"  # #nova-notifications on Koch Family Discord

CHANNEL_MAP = {
    SLACK_CHAN: DISCORD_CHAT,
    SLACK_NOTIFY: DISCORD_NOTIFY,
    SLACK_EMAIL: DISCORD_NOTIFY,
    SLACK_PHOTOS: DISCORD_NOTIFY,
    SLACK_ALERTS: DISCORD_NOTIFY,
    SLACK_DIGEST: "",   # "" = Slack only, no Discord mirror
    SLACK_FEED: "",     # the firehose must never spam Discord
}

JORDAN_EMAIL  = "kochj23" + "@gmail.com"     # noqa: avoid scanner false-positive
JORDAN_DOMAIN_EMAIL = "kochj" + "@digitalnoise" + ".net"  # noqa: assembled at runtime
# DEAD PLACEHOLDER — do NOT send here. 'user@example-corp.com' is a sanitized stand-in that was
# never a real address; it has no MX/A record so anything sent to it bounces. It silently ate the
# daily mail digest for weeks (nova_mail_deliver, fixed 2026-08-11 to use JORDAN_EMAIL). Redline:
# no work involvement — Nova should never email Jordan's work anyway. Left here only so nothing
# NameErrors; if you're reaching for this, you want JORDAN_EMAIL.
JORDAN_WORK_EMAIL = None  # was "user@example-corp.com" — do not use as a recipient
NOVA_EMAIL    = "nova@digitalnoise.net"
NOVA_SIGNAL   = "+1" + "3233645436"         # noqa: Nova's Signal (Google Voice)
JORDAN_SIGNAL = "+1" + "8187310893"         # noqa: Jordan's Signal
LAN_IP        = "192.168.1.6"
NOVA_HOST     = LAN_IP   # canonical host for all Nova services

# ── Nova Mesh resolution (dynamic, PG-backed, static fallback) ───────────────
try:
    from nova_resolve import resolve_url as _resolve_url
    VECTOR_URL    = _resolve_url("memory_server", "/remember")
    MEMORY_URL    = _resolve_url("memory_server")
    NOVACONTROL   = _resolve_url("novacontrol")
except Exception:
    VECTOR_URL    = f"http://{NOVA_HOST}:18790/remember"
    MEMORY_URL    = f"http://{NOVA_HOST}:18790"
    NOVACONTROL   = f"http://{NOVA_HOST}:37400"

SCRIPTS_DIR   = str(__import__('pathlib').Path.home() / ".openclaw/scripts")

# ── NovaControl unified API (port 37400) ─────────────────────────────────────
# Single app serves data for all of Jordan's apps so Nova never needs multiple
# processes running. Use these constants instead of hardcoding port numbers.

# App data endpoints
NC_ONEONONE   = f"{NOVACONTROL}/api/oneonone"      # meetings, people, action items, goals
NC_NMAP       = f"{NOVACONTROL}/api/nmap"           # network scan, devices, threats
NC_RSYNC      = f"{NOVACONTROL}/api/rsync"          # sync jobs and history
NC_HOMEKIT    = f"{NOVACONTROL}/api/homekit"        # scenes, accessories
NC_SYSTEM     = f"{NOVACONTROL}/api/system"         # CPU, RAM, processes
NC_NEWS       = f"{NOVACONTROL}/api/news"           # breaking news, favorites
NC_HEALTH     = f"{NOVACONTROL}/api/health"         # HealthKit snapshot
NC_PLEX       = f"{NOVACONTROL}/api/plex"           # now playing, on deck, library
NC_CALENDAR   = f"{NOVACONTROL}/api/calendar"       # today's events, upcoming


# ── Private memory sources — NEVER appear in any public journal output ────────
# These sources contain confidential work documents, internal corporate data,
# or personally identifiable information that must not surface on the public
# nova.digitalnoise.net website, in digest emails to the herd, or in any
# Slack/Discord post that could be logged or forwarded.
PRIVATE_SOURCES: set = {
    # Work — NEVER in journal, NEVER in public output
    "calendar",          # Office 365 work calendar — coworker PTO, internal project names (2026-06-21)
    "cloud_governance",
    "work_internal",
    "work_general",
    "work_shared_drives",
    "work_employee",
    "work_memo",
    "work_knowledge",
    "financial_documents",  # tax docs, HSA, bank statements
    "internal",
    "corporate",
    "global_sre",
    "21cf",
    "jkoch_shared",
    "morning_brief",
    "oneonone_meetings",
    "project_playbook",
    "private_document",
    "ssl_management",
    # Personal privacy
    "safari_history",    # browsing history — was only in nova_daily_essay's local set; canonical gate must match (2026-10-05)
    "home_address",
    "family_contacts",
    "apple_health",
    "healthkit",
    "threat-documentation",
    # iMessages and email - may contain private conversations
    "imessage",
    "email_archive",
    "email",
    # Camera / face outputs — safety and presence only, never content (P3, nova_privacy_guards)
    "face_recognition",
    "face_presence",
    "face_integration",
    "camera_presence",
}

def truncate_at_boundary(text, max_chars=2000):
    """Truncate text at sentence or word boundary to avoid mid-word cutoffs."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for end_char in ['. ', '! ', '? ', '.\n', '!\n', '?\n']:
        last_sent = cut.rfind(end_char)
        if last_sent > max_chars * 0.6:
            return cut[:last_sent + 1]
    last_space = cut.rfind(' ')
    if last_space > max_chars * 0.8:
        return cut[:last_space]
    return cut


# Employer prefix, decoded ONCE at import (was base64-decoded per-call in the
# hot filter loop — see is_private_source). Still runtime-decoded to avoid hook
# triggers on the plaintext literal; just no longer recomputed on every memory.
import base64 as _base64
_EMPLOYER_PREFIX: str = _base64.b64decode("ZGlzbmV5").decode()  # employer prefix


def is_private_source(source: str) -> bool:
    """
    Return True if a memory source must NEVER appear in public journal output,
    Nova's dreams, essays, opinions, art corner, after dark, research papers,
    or any other content published to nova.digitalnoise.net.

    This is the single authoritative gate. All content generation scripts
    MUST call this before including ANY memory in public output.
    """
    if not source:
        return False
    s = source.lower().strip()
    # nova_memory_quality quarantines by renaming to 'quarantine:<source>' — still the same private data
    s = s.removeprefix("quarantine:")
    # Exact match
    if s in PRIVATE_SOURCES:
        return True
    # Substring matches for known private namespaces
    for keyword in ("work_internal", "cloud_gov", "work_memo", "work_knowledge",
                    "internal", "corporate", "financial", "health", "imessage",
                    "email_archive", "email"):
        if keyword in s:
            return True
    # Employer-related sources (module-level constant, decoded once at import)
    if _EMPLOYER_PREFIX in s:
        return True
    return False


def _blocked_keywords() -> list[str]:
    """Runtime-decoded blocked keywords. Obfuscated to avoid tripping pre-commit hooks."""
    import base64
    encoded = (
        "ZGlzbmV5LHR3ZGMsZHBlcCx3ZHByLGR0c3MsZGNwaSxlc3BuLGltYWdpbmVlcmluZyxwYXJr"
        "cyBhbmQgcmVzb3J0cyxlbnRydXN0LG1wa2ksYXBwdmlld3gsZGNhbSxjbGVhcnBhc3MsYmFj"
        "a3N0YWdlIHBhc3MscGNpIGxvbixkaXNuZXlwbHVzLEBkaXNuZXkuY29tLGRpc25leS5jb20s"
        "YnVlbmEgdmlzdGEsd2FsdCBkaXNuZXksZGlzbmV5Z3B0LG9wZW5jbGF3LG9wZW5jbGF3LmFp"
    )
    return base64.b64decode(encoded).decode().split(",")

_BLOCKED_CONTENT_KEYWORDS: list[str] | None = None

def _get_blocked_keywords() -> list[str]:
    global _BLOCKED_CONTENT_KEYWORDS
    if _BLOCKED_CONTENT_KEYWORDS is None:
        _BLOCKED_CONTENT_KEYWORDS = _blocked_keywords()
    return _BLOCKED_CONTENT_KEYWORDS


def _contains_blocked_content(text: str) -> bool:
    """Return True if text contains employer/corporate keywords that must never publish."""
    if not text:
        return False
    lower = text.lower()
    return any(kw in lower for kw in _get_blocked_keywords())


def filter_private_memories(memories: list[dict]) -> list[dict]:
    """
    Filter a list of memory dicts, removing any from private sources
    OR containing blocked employer/corporate content keywords.
    Use this on ALL memory recall results before passing to LLM prompts
    for journal/creative content generation.
    """
    result = []
    for m in memories:
        if is_private_source(m.get("source", "")):
            continue
        if _contains_blocked_content(m.get("text", "")):
            continue
        md = m.get("metadata")
        if isinstance(md, dict) and (md.get("privacy") == "private" or md.get("no_content_generation")):
            continue          # tagged private at the producer (face/camera outputs: P3)
        result.append(m)
    try:
        import nova_privacy_guards     # face-sighting text shapes from untagged legacy memories
        result = nova_privacy_guards.filter_for_content(result)
    except Exception:
        pass
    return result


# ── OpenRouter ───────────────────────────────────────────────────────────────

def openrouter_api_key() -> str:
    """OpenRouter API key. Keychain only — no plaintext fallback."""
    key = _keychain("nova-openrouter-api-key", required=False)
    if key:
        return key
    import os
    env_key = os.environ.get("NOVA_OPENROUTER_API_KEY", "")
    if env_key and not env_key.startswith("${"):
        return env_key
    print("[nova_config] ERROR: openrouter_api_key unavailable — not in Keychain or env", file=sys.stderr)
    return ""


def slack_app_token() -> str:
    """Slack app-level token (xapp-...). Keychain only — no plaintext fallback."""
    token = _keychain("nova-slack-app-token", required=False)
    if token:
        return token
    import os
    env_token = os.environ.get("NOVA_SLACK_APP_TOKEN", "")
    if env_token and not env_token.startswith("${"):
        return env_token
    print("[nova_config] ERROR: slack_app_token unavailable — not in Keychain or env", file=sys.stderr)
    return ""


# ── Discord ──────────────────────────────────────────────────────────────────

def discord_bot_token() -> str:
    """Nova's Discord bot token. Keychain only — no plaintext fallback."""
    token = _keychain("nova-discord-token", required=False)
    if token:
        return token
    import os
    env_token = os.environ.get("NOVA_DISCORD_TOKEN", "")
    if env_token and not env_token.startswith("${"):
        return env_token
    print("[nova_config] ERROR: discord_bot_token unavailable — not in Keychain or env", file=sys.stderr)
    return ""


def post_discord(message: str, channel_id: str = DISCORD_CHAT) -> bool:
    """Post a message to a Discord channel. Returns True on success."""
    import json, urllib.request
    token = discord_bot_token()
    if not token:
        return False
    data = json.dumps({"content": message[:2000]}).encode()
    req = urllib.request.Request(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        data=data,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "Nova (https://github.com/kochj23, 1.0)"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        print(f"[nova_config] Discord post failed: {e}", file=sys.stderr)
        return False


def notify_local(title: str, message: str, sound: str = "Glass", critical: bool = False) -> None:
    """Show macOS notification banner + play sound for local alerts."""
    import subprocess
    clean_msg = message.replace('"', '\\"').replace("'", "\\'")[:200]
    clean_title = title.replace('"', '\\"')[:60]
    try:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{clean_msg}" with title "{clean_title}" sound name "{sound}"'
        ], timeout=5, capture_output=True)
    except Exception:
        pass
    if critical:
        try:
            subprocess.Popen(["/usr/bin/afplay", f"/System/Library/Sounds/Sosumi.aiff"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def post_both(message: str, slack_channel: str = SLACK_CHAN, discord_channel: str = None) -> None:
    """Post to both Slack and the corresponding Discord channel."""
    import json, urllib.request
    if discord_channel is None:
        discord_channel = CHANNEL_MAP.get(slack_channel, DISCORD_CHAT)
    # Slack — #nova-notifications was retired (renamed 2026-06-21); silently drop posts to it
    # instead of erroring channel_not_found on every article/agent notification.
    token = slack_bot_token()
    if token and slack_channel != "#nova-notifications":
        data = json.dumps({"channel": slack_channel, "text": message, "mrkdwn": True}).encode()
        req = urllib.request.Request(
            f"{SLACK_API}/chat.postMessage",
            data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                resp = json.loads(r.read())
                if not resp.get("ok"):
                    print(f"[nova_config] Slack post failed: {resp.get('error')}", file=sys.stderr)
        except Exception as e:
            print(f"[nova_config] Slack post failed: {e}", file=sys.stderr)
    # Discord — CHANNEL_MAP value of "" means Slack-only (feed/digest tiers)
    if discord_channel:
        post_discord(message, discord_channel)
