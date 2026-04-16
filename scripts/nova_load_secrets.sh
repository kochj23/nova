#!/bin/zsh
# nova_load_secrets.sh — Load secrets from macOS Keychain into environment variables.
# Source this script before starting OpenClaw or any Nova service that needs credentials.
#
# Usage:
#   source ~/.openclaw/scripts/nova_load_secrets.sh
#   openclaw agent ...
#
# All secrets are stored in macOS Keychain under account "nova".
# To update a secret:
#   security add-generic-password -a nova -s SERVICE_NAME -w NEW_VALUE -U
#
# Written by Jordan Koch.

_keychain() {
    security find-generic-password -a nova -s "$1" -w 2>/dev/null
}

export NOVA_OPENROUTER_API_KEY="$(_keychain nova-openrouter-api-key)"
export NOVA_SLACK_BOT_TOKEN="$(_keychain nova-slack-bot-token)"
export NOVA_SLACK_APP_TOKEN="$(_keychain nova-slack-app-token)"
export NOVA_GATEWAY_AUTH_TOKEN="$(_keychain nova-gateway-auth-token)"

# Validate — warn on missing secrets but don't abort (cron jobs may run with Keychain locked)
_missing=0
for var in NOVA_OPENROUTER_API_KEY NOVA_SLACK_BOT_TOKEN NOVA_SLACK_APP_TOKEN NOVA_GATEWAY_AUTH_TOKEN; do
    if [ -z "${(P)var}" ]; then
        echo "[nova_load_secrets] WARNING: $var is empty (Keychain locked or entry missing)" >&2
        _missing=$((_missing + 1))
    fi
done

if [ $_missing -eq 0 ]; then
    echo "[nova_load_secrets] All 4 secrets loaded from Keychain" >&2
fi
unset _missing
