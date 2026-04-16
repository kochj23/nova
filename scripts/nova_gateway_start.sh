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

exec /opt/homebrew/opt/node/bin/node \
    /opt/homebrew/lib/node_modules/openclaw/dist/entry.js \
    gateway --port 18789
