#!/bin/zsh
# nova_gateway_start.sh — Start OpenClaw gateway with Keychain secrets loaded.
# Called by launchd plist. Loads secrets from Keychain into env vars
# before exec'ing the gateway process.
#
# Written by Jordan Koch.

_kc() { security find-generic-password -a nova -s "$1" -w 2>/dev/null; }

export NOVA_OPENROUTER_API_KEY="$(_kc nova-openrouter-api-key)"
export NOVA_SLACK_BOT_TOKEN="$(_kc nova-slack-bot-token)"
export NOVA_SLACK_APP_TOKEN="$(_kc nova-slack-app-token)"
export NOVA_GATEWAY_AUTH_TOKEN="$(_kc nova-gateway-auth-token)"

# Validate secrets loaded — if Keychain is locked, wait with exponential backoff
MAX_RETRIES=12
RETRIES=0
DELAY=5
while [ -z "$NOVA_SLACK_BOT_TOKEN" ] && [ $RETRIES -lt $MAX_RETRIES ]; do
    echo "[gateway_start] Keychain not ready, retry $((RETRIES+1))/$MAX_RETRIES (wait ${DELAY}s)..." >&2
    sleep $DELAY
    export NOVA_SLACK_BOT_TOKEN="$(_kc nova-slack-bot-token)"
    export NOVA_OPENROUTER_API_KEY="$(_kc nova-openrouter-api-key)"
    export NOVA_SLACK_APP_TOKEN="$(_kc nova-slack-app-token)"
    export NOVA_GATEWAY_AUTH_TOKEN="$(_kc nova-gateway-auth-token)"
    RETRIES=$((RETRIES + 1))
    DELAY=$((DELAY < 30 ? DELAY + 5 : 30))
done

if [ -z "$NOVA_SLACK_BOT_TOKEN" ]; then
    echo "[gateway_start] FATAL: Keychain still locked after $MAX_RETRIES retries (~3 min)" >&2
    exit 1
fi

exec /opt/homebrew/opt/node/bin/node \
    /opt/homebrew/lib/node_modules/openclaw/dist/entry.js \
    gateway --port 18789
