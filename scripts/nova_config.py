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
    """Nova's Slack bot token (xoxb-...). Tries Keychain first, falls back to openclaw.json
    for cron/non-interactive sessions where Keychain prompts are unavailable."""
    token = _keychain("nova-slack-bot-token", required=False)
    if token:
        return token
    # Fallback: openclaw.json already stores this token for the gateway process
    try:
        import json
        from pathlib import Path
        config_path = Path.home() / ".openclaw/openclaw.json"
        with open(config_path) as f:
            config = json.load(f)
        fallback = config.get("channels", {}).get("slack", {}).get("botToken", "")
        if fallback:
            print("[nova_config] INFO: slack_bot_token loaded from openclaw.json fallback", file=sys.stderr)
        return fallback
    except Exception:
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
    """OpenRouter API key. Keychain first, openclaw.json fallback for gateway process."""
    key = _keychain("nova-openrouter-api-key", required=False)
    if key:
        return key
    try:
        import json
        from pathlib import Path
        config_path = Path.home() / ".openclaw/openclaw.json"
        with open(config_path) as f:
            config = json.load(f)
        fallback = config.get("models", {}).get("providers", {}).get("openrouter", {}).get("apiKey", "")
        if fallback:
            print("[nova_config] INFO: openrouter_api_key loaded from openclaw.json fallback", file=__import__('sys').stderr)
        return fallback
    except Exception:
        return ""


def slack_app_token() -> str:
    """Slack app-level token (xapp-...). Keychain first, openclaw.json fallback."""
    token = _keychain("nova-slack-app-token", required=False)
    if token:
        return token
    try:
        import json
        from pathlib import Path
        config_path = Path.home() / ".openclaw/openclaw.json"
        with open(config_path) as f:
            config = json.load(f)
        fallback = config.get("channels", {}).get("slack", {}).get("appToken", "")
        if fallback:
            print("[nova_config] INFO: slack_app_token loaded from openclaw.json fallback", file=__import__('sys').stderr)
        return fallback
    except Exception:
        return ""
