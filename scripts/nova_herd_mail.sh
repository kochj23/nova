#!/bin/bash
#
# nova_herd_mail.sh — Wrapper for herd-mail that loads credentials from Keychain.
#
# Usage: nova_herd_mail.sh <herd-mail subcommand> [args...]
#   nova_herd_mail.sh check
#   nova_herd_mail.sh list --json
#   nova_herd_mail.sh read --id <id>
#   nova_herd_mail.sh send --to <addr> --subject "..." --body "..."
#
# Written by Jordan Koch.

set -euo pipefail

SCRIPT_DIR="$HOME/.openclaw/scripts"
HERD_MAIL="$SCRIPT_DIR/herd_mail.py"
APP_PASS=$(security find-generic-password -a "nova@digitalnoise.net" -s "nova-smtp-app-password" -w 2>/dev/null)

if [ -z "$APP_PASS" ]; then
    echo "ERROR: nova-smtp-app-password not found in Keychain" >&2
    exit 2
fi

export WAGGLE_HOST=smtp.gmail.com
export WAGGLE_PORT=587
export WAGGLE_TLS=false
export WAGGLE_USER=nova@digitalnoise.net
export WAGGLE_PASS="$APP_PASS"
export WAGGLE_IMAP_HOST=imap.gmail.com
export WAGGLE_IMAP_PORT=993
export WAGGLE_IMAP_TLS=true
export WAGGLE_FROM="Nova <nova@digitalnoise.net>"

python3 "$HERD_MAIL" "$@"
