#!/usr/bin/env bash
# nova_remember.sh — Store a memory in Nova's vector DB
# Usage: nova_remember.sh "text to remember" [source] [metadata_json]
# Source defaults to "slack". Returns the memory ID.
# Example: nova_remember.sh "Jordan prefers Slack" "slack" '{"channel":"nova-chat"}'

TEXT="${1:?Usage: nova_remember.sh \"text\" [source] [metadata_json]}"
SOURCE="${2:-slack}"
METADATA="${3}"
[[ -z "$METADATA" ]] && METADATA="{}"

# Write args to temp files to avoid shell quoting issues with JSON
TMPTEXT=$(mktemp)
TMPMETA=$(mktemp)
printf '%s' "$TEXT" > "$TMPTEXT"
printf '%s' "$METADATA" > "$TMPMETA"

/opt/homebrew/bin/python3 - "$TMPTEXT" "$TMPMETA" "$SOURCE" << 'EOF'
import sys, json, urllib.request, os

text_file = sys.argv[1]
meta_file = sys.argv[2]
source    = sys.argv[3]

text = open(text_file).read()
os.unlink(text_file)

try:
    meta = json.loads(open(meta_file).read())
    os.unlink(meta_file)
except json.JSONDecodeError as e:
    print(f"ERROR: metadata is not valid JSON: {e}", file=sys.stderr)
    sys.exit(1)

payload = json.dumps({"text": text, "source": source, "metadata": meta}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:18790/remember",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode())
        print(f"Stored memory: {d['id']} ({d['dims']}d)")
except Exception as e:
    print(f"ERROR: Memory server not reachable: {e}", file=sys.stderr)
    sys.exit(1)
EOF
