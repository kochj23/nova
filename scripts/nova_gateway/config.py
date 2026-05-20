"""
nova_gateway.config — All constants, URLs, channel IDs, context limits, privacy blocklist,
keychain helpers, and token loading.

Written by Jordan Koch.
"""

import re
import subprocess
from pathlib import Path


# ── Version ──────────────────────────────────────────────────────────────────
VERSION = "2.4.0"

# ── URLs & Endpoints ─────────────────────────────────────────────────────────
PG_DSN       = "postgresql://kochj@192.168.1.6:5432/nova_ops"
OLLAMA_URL   = "http://192.168.1.6:11434"
MLX_URL      = "http://192.168.1.6:5050"
LLAMACPP_URL = "http://192.168.1.6:11435"
OPENROUTER   = "https://openrouter.ai/api/v1"
SIGNAL_URL      = "http://127.0.0.1:8080"   # HTTP for send
SIGNAL_TCP_HOST = "127.0.0.1"
SIGNAL_TCP_PORT = 7583                      # TCP for streaming receive

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPTS_DIR  = Path.home() / ".openclaw/scripts"
LOG_DIR      = Path.home() / ".openclaw/logs"
STATE_DIR    = Path.home() / ".openclaw/workspace/state"

# ── Signal numbers ───────────────────────────────────────────────────────────
NOVA_SIGNAL  = "+1" + "3233645436"
JORDAN_SIGNAL = "+1" + "8187310893"

# ── Token limits per agent ───────────────────────────────────────────────────
CONTEXT_LIMITS = {
    "chat":     8192,
    "research": 65536,
    "home":     16384,
    "main":     32768,
}

# Reserve this many tokens for the response
RESPONSE_RESERVE = 2048
# When context exceeds limit - reserve, summarize oldest turns
COMPACTION_THRESHOLD = 0.85

# ── Channel → agent routing ──────────────────────────────────────────────────
CHANNEL_AGENT = {
    "discord": "chat",
    "slack":   "chat",
    "signal":  "chat",
}

# ── Slack channels ───────────────────────────────────────────────────────────
SLACK_NOTIFY_CHANNEL = "C0ATAF7NZG9"  # #nova-notifications
SLACK_CHAT_CHANNEL   = "C0AMNQ5GX70"  # #nova-chat
SLACK_CLAUDE_CHANNEL = "C0B3RSRR0DD"  # #nova-claude (Claude Code <-> Nova)
JORDAN_DM_CHANNEL    = "D0AMPB3F4T0"  # Jordan DM

# ── Discord ──────────────────────────────────────────────────────────────────
DISCORD_GUILD_ID     = 1496985100657623210
DISCORD_CHAT_CHANNEL = 1496990647062761483   # #nova-chat
DISCORD_NOTIF_CHANNEL = 1496990332250886246  # #nova-notifications

# ── Degraded mode (startup grace period) ─────────────────────────────────────
STARTUP_GRACE = 30  # seconds — during this window, respond without memory/tools

# ── Privacy policy enforcement (hard blocklist — NEVER goes to cloud) ────────
PRIVACY_BLOCKLIST = [
    # Personal identifiers
    r"jordan|koch|kochj|amy|mccain",
    # Home network
    r"192\.168\.|10\.0\.|unifi|synology|nas",
    # Financial
    r"bank|credit card|amex|account number|ssn|salary",
    # Work
    r"work_internal|dtoc|enterprise tech",
    # Health
    r"healthkit|medical|diagnosis|prescription",
    # Credentials
    r"password|token|api.key|secret|keychain",
]

# ── Memory injection timeout ─────────────────────────────────────────────────
MEMORY_TIMEOUT = 5.0

# ── Agent fault isolation ────────────────────────────────────────────────────
CRASH_WINDOW = 300  # 5 minutes
CRASH_THRESHOLD = 3  # 3 crashes in window → disable
DISABLE_DURATION = 300  # disable for 5 minutes

# ── Claude Code bridge ───────────────────────────────────────────────────────
CLAUDE_BRIDGE_SESSION = "claude-bridge-persistent"


def is_private_content(messages: list) -> bool:
    """Hard check: does any message contain blocklisted content?
    This runs BEFORE the intent router and overrides it — if content matches
    any pattern, it NEVER goes to OpenRouter regardless of routing decisions.
    """
    text = " ".join(m.get("content", "") for m in messages).lower()
    return any(re.search(p, text) for p in PRIVACY_BLOCKLIST)


# ── Keychain helpers ─────────────────────────────────────────────────────────

def keychain(service: str, account: str = "nova") -> str:
    r = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def load_tokens() -> dict:
    return {
        "slack_bot":    keychain("nova-slack-bot-token"),
        "slack_app":    keychain("nova-slack-app-token"),
        "discord":      keychain("nova-discord-token"),
        "openrouter":   keychain("nova-openrouter-api-key"),
    }
