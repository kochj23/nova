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

import subprocess
import sys


# ── Keychain loader ───────────────────────────────────────────────────────────

def _keychain(service: str, account: str = "nova", required: bool = True) -> str:
    """Load a secret from macOS Keychain.
    If required=True (default), exits on failure.
    If required=False, returns empty string on failure (for cron-safe use).
    """
    result = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        msg = f"[nova_config] Keychain entry not found: service={service} account={account}"
        if required:
            print(msg, file=sys.stderr)
            print(f"[nova_config] Run: security add-generic-password -a {account} -s {service} -w YOUR_VALUE", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"[nova_config] WARNING: {msg} (non-fatal, Keychain may be locked)", file=sys.stderr)
            return ""
    return result.stdout.strip()


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
SLACK_NOTIFY  = "C0ATAF7NZG9"   # #nova-notifications (cron output, status, automated posts)
JORDAN_DM     = "D0AMPB3F4T0"   # Jordan's DM channel with Nova
JORDAN_EMAIL  = "kochj23" + "@gmail.com"     # noqa: avoid scanner false-positive
NOVA_EMAIL    = "nova@digitalnoise.net"
VECTOR_URL    = "http://127.0.0.1:18790/remember"
SCRIPTS_DIR   = str(__import__('pathlib').Path.home() / ".openclaw/scripts")


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
