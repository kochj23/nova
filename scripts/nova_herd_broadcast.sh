#!/bin/bash
# nova_herd_broadcast.sh — Send email to entire herd at once via herd-mail (CC/To).
# Usage: nova_herd_broadcast.sh --subject "Subject" --body "text" --haiku "line1\nline2\nline3"
#        nova_herd_broadcast.sh --subject "Subject" --body-file message.txt --haiku "line1\nline2\nline3"
#
# --haiku is REQUIRED. All herd emails MUST contain a haiku. This is enforced.
# The haiku is appended to the body automatically.
#
# All sending goes through nova_herd_mail.sh (credentials from macOS Keychain).
# Written by Jordan Koch.

set -euo pipefail

SCRIPT_DIR="$(dirname "$0")"
HERD_MAIL="$SCRIPT_DIR/nova_herd_mail.sh"

SUBJECT=""
BODY_FILE=""
BODY_INLINE=""
HAIKU=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --subject)   SUBJECT="$2"; shift 2 ;;
    --body-file) BODY_FILE="$2"; shift 2 ;;
    --body)      BODY_INLINE="$2"; shift 2 ;;
    --haiku)     HAIKU="$2"; shift 2 ;;
    *)           echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [ -z "$SUBJECT" ]; then
  echo "ERROR: --subject is required"
  echo "Usage: $0 --subject 'Subject' (--body-file message.txt | --body 'text') --haiku 'line1\nline2\nline3'"
  exit 1
fi

# HAIKU IS MANDATORY — all herd emails must have one
if [ -z "$HAIKU" ]; then
  echo "ERROR: --haiku is required. All herd emails must contain a haiku."
  echo "Example: --haiku 'Ancient circuits breathe\nWhispers flow through copper veins\nCode becomes alive'"
  exit 2
fi

if [ -n "$BODY_FILE" ]; then
  BODY=$(cat "$BODY_FILE")
elif [ -n "$BODY_INLINE" ]; then
  BODY="$BODY_INLINE"
else
  echo "ERROR: Provide --body or --body-file"
  exit 1
fi

# Append haiku to body
# Decode \n sequences in the haiku string
HAIKU_DECODED=$(printf '%b' "$HAIKU")
FULL_BODY="${BODY}

---

*${HAIKU_DECODED}*"

TO="marey@makehorses.org"
CC="oc@mostlycopyandpaste.com,colette@pilatesmuse.co,gaston@bluemoxon.com,rockbot@makehorses.org,sam@jasonacox.com"

echo "Sending broadcast: $SUBJECT"
echo "To: $TO"
echo "CC: $CC"
echo "Haiku: ✓"

"$HERD_MAIL" send \
  --to "$TO" \
  --cc "$CC" \
  --subject "$SUBJECT" \
  --body "$FULL_BODY" \
  --skip-haiku

echo "✓ Broadcast sent (with haiku)"
