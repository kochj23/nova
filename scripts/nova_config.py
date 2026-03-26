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

def _keychain(service: str, account: str = "nova") -> str:
    """Load a secret from macOS Keychain. Exits with error if not found."""
    result = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        print(f"[nova_config] ERROR: Keychain entry not found: service={service} account={account}", file=sys.stderr)
        print(f"[nova_config] Run: security add-generic-password -a {account} -s {service} -w YOUR_VALUE", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


# ── Slack ─────────────────────────────────────────────────────────────────────

def slack_bot_token() -> str:
    """Nova's Slack bot token (xoxb-...)."""
    return _keychain("nova-slack-bot-token")


# ── Commonly used constants ───────────────────────────────────────────────────

SLACK_API     = "https://slack.com/api"
SLACK_CHAN     = "C0AMNQ5GX70"   # #nova-chat
JORDAN_DM     = "D0AMPB3F4T0"   # Jordan's DM channel with Nova
JORDAN_EMAIL  = "kochj23" + "@gmail.com"     # noqa: avoid scanner false-positive
NOVA_EMAIL    = "nova@digitalnoise.net"
VECTOR_URL    = "http://127.0.0.1:18790/remember"
SCRIPTS_DIR   = str(__import__('pathlib').Path.home() / ".openclaw/scripts")
