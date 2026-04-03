#!/bin/bash
# nova_herd_broadcast.sh — Send email to entire herd at once via herd-mail (CC/To).
# Usage: nova_herd_broadcast.sh --subject "Subject" --body-file message.txt
#        nova_herd_broadcast.sh --subject "Subject" --body "inline body"
#
# All sending goes through nova_herd_mail.sh (credentials from macOS Keychain).
# Written by Jordan Koch.

set -euo pipefail

SCRIPT_DIR="$(dirname "$0")"
HERD_MAIL="$SCRIPT_DIR/nova_herd_mail.sh"

SUBJECT=""
BODY_FILE=""
BODY_INLINE=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --subject)   SUBJECT="$2"; shift 2 ;;
    --body-file) BODY_FILE="$2"; shift 2 ;;
    --body)      BODY_INLINE="$2"; shift 2 ;;
    *)           echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [ -z "$SUBJECT" ]; then
  echo "Usage: $0 --subject 'Subject' (--body-file message.txt | --body 'text')"
  exit 1
fi

if [ -n "$BODY_FILE" ]; then
  BODY=$(cat "$BODY_FILE")
elif [ -n "$BODY_INLINE" ]; then
  BODY="$BODY_INLINE"
else
  echo "ERROR: Provide --body or --body-file"
  exit 1
fi

TO="marey@makehorses.org"
CC="oc@mostlycopyandpaste.com,colette@pilatesmuse.co,gaston@bluemoxon.com,rockbot@makehorses.org,sam@jasonacox.com"

echo "Sending broadcast: $SUBJECT"
echo "To: $TO"
echo "CC: $CC"

"$HERD_MAIL" send \
  --to "$TO" \
  --cc "$CC" \
  --subject "$SUBJECT" \
  --body "$BODY"

echo "✓ Broadcast sent"
