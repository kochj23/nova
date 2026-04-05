#!/bin/bash
#
# nova_herd_mail.sh — Wrapper for herd-mail that loads credentials from Keychain.
#
# Usage: nova_herd_mail.sh <herd-mail subcommand> [args...]
#   nova_herd_mail.sh check
#   nova_herd_mail.sh list --json
#   nova_herd_mail.sh read --id <id>
#   nova_herd_mail.sh send --to <addr> --subject "..." --body "..." --haiku "line1\nline2\nline3"
#
# --haiku is REQUIRED for all send commands. This is a standing policy from Jordan.
# Use --skip-haiku only for automated/system emails (cron jobs, auto-replies).
#
# Written by Jordan Koch.

set -eo pipefail

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
export PYTHONPATH="/Volumes/Data/AI/python_packages:$PYTHONPATH"

# Only intercept 'send' subcommand for haiku enforcement
SUBCOMMAND="${1:-}"

if [ "$SUBCOMMAND" != "send" ]; then
    # Not a send — pass through as-is
    python3 "$HERD_MAIL" "$@"
    exit $?
fi

# --- send subcommand: enforce haiku ---
shift  # consume 'send'

HAIKU=""
SKIP_HAIKU=false
BODY_ARG=""
BODY_FILE_ARG=""
REMAINING_ARGS=()

# Parse args to extract --haiku, --skip-haiku, and --body / --body-file
while [[ $# -gt 0 ]]; do
    case "$1" in
        --haiku)
            HAIKU="$2"
            shift 2
            ;;
        --skip-haiku)
            SKIP_HAIKU=true
            shift
            ;;
        --body)
            BODY_ARG="$2"
            shift 2
            ;;
        --body-file)
            BODY_FILE_ARG="$2"
            shift 2
            ;;
        *)
            REMAINING_ARGS+=("$1")
            shift
            ;;
    esac
done

# Enforce haiku
if [ "$SKIP_HAIKU" = false ] && [ -z "$HAIKU" ]; then
    echo "ERROR: --haiku is required for all send commands." >&2
    echo "  All emails from Nova must contain a haiku (Jordan's standing policy)." >&2
    echo "  Use: --haiku \"Line one five syllables\\nLine two seven syllables\\nLine three five\"" >&2
    echo "  For automated/system emails only: --skip-haiku" >&2
    exit 2
fi

# Build final body: original body + haiku appended
if [ -n "$BODY_FILE_ARG" ]; then
    ORIGINAL_BODY=$(cat "$BODY_FILE_ARG")
elif [ -n "$BODY_ARG" ]; then
    ORIGINAL_BODY="$BODY_ARG"
else
    ORIGINAL_BODY=""
fi

if [ "$SKIP_HAIKU" = false ] && [ -n "$HAIKU" ]; then
    HAIKU_DECODED=$(printf '%b' "$HAIKU")
    FINAL_BODY="${ORIGINAL_BODY}

---

*${HAIKU_DECODED}*"
else
    FINAL_BODY="$ORIGINAL_BODY"
fi

# Call herd_mail.py with the assembled body (always use --body now, not --body-file)
python3 "$HERD_MAIL" send \
    --body "$FINAL_BODY" \
    "${REMAINING_ARGS[@]}"
