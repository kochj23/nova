#!/bin/bash
#
# generate_image.sh — Generate an image via SwarmUI for Nova
#
# TOOL FOR NOVA: Use this script to generate images from text prompts.
# SwarmUI runs locally at http://localhost:7801 and uses Stable Diffusion SDXL.
#
# Usage: generate_image.sh "your prompt here" [width] [height] [steps] [model]
#
# Arguments:
#   $1  prompt  (required) — describe the image you want
#   $2  width   (optional, default 1024)
#   $3  height  (optional, default 1024)
#   $4  steps   (optional, default 8 — range 4-30, higher = more detail, slower)
#   $5  model   (optional, default Juggernaut_X_RunDiffusion_Hyper.safetensors)
#
# Examples:
#   generate_image.sh "a sunset over mountains, oil painting style"
#   generate_image.sh "portrait of a robot, detailed, cinematic lighting" 1024 1024 20
#
# Output: prints the full file path to the generated PNG image
# Author: Jordan Koch

set -euo pipefail

PROMPT="${1:-}"
WIDTH="${2:-1024}"
HEIGHT="${3:-1024}"
STEPS="${4:-8}"
MODEL="${5:-Juggernaut_X_RunDiffusion_Hyper.safetensors}"
SWARM_URL="http://localhost:7801"
OUTPUT_BASE="$HOME/AI/SwarmUI/Output/local/raw"

if [ -z "$PROMPT" ]; then
    echo "ERROR: No prompt provided." >&2
    echo "Usage: generate_image.sh \"your prompt here\" [width] [height] [steps] [model]" >&2
    exit 1
fi

# Get a session
SESSION=$(curl -sf -X POST "$SWARM_URL/API/GetNewSession" \
    -H "Content-Type: application/json" \
    -d '{}' | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")

if [ -z "$SESSION" ]; then
    echo "ERROR: Could not connect to SwarmUI at $SWARM_URL" >&2
    echo "Is SwarmUI running? Check: launchctl list | grep swarmui" >&2
    exit 1
fi

# Generate the image
PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'session_id': sys.argv[1],
    'images': 1,
    'prompt': sys.argv[2],
    'model': sys.argv[3],
    'width': int(sys.argv[4]),
    'height': int(sys.argv[5]),
    'steps': int(sys.argv[6]),
    'cfgscale': 2,
    'seed': -1
}))
" "$SESSION" "$PROMPT" "$MODEL" "$WIDTH" "$HEIGHT" "$STEPS")

RESPONSE=$(curl -sf -X POST "$SWARM_URL/API/GenerateText2Image" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")

# Extract the relative image path from the response
REL_PATH=$(echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if 'images' in data and data['images']:
    # Strip the 'View/local/raw/' prefix to get the date/filename part
    path = data['images'][0]
    # path looks like: View/local/raw/2026-03-19/filename.png
    parts = path.split('/', 3)
    print(parts[3].strip() if len(parts) == 4 else path.strip())
elif 'error' in data:
    print('ERROR: ' + data['error'], file=sys.stderr)
    sys.exit(1)
else:
    print('ERROR: Unexpected response: ' + str(data), file=sys.stderr)
    sys.exit(1)
" 2>/dev/null)
REL_PATH="${REL_PATH//[$'\t\r\n']}"

FULL_PATH="$OUTPUT_BASE/$REL_PATH"

if [ ! -f "$FULL_PATH" ]; then
    echo "ERROR: Image was reported generated but file not found at: $FULL_PATH" >&2
    exit 1
fi

# Copy to Nova's workspace for easy access
WORKSPACE="$HOME/.openclaw/workspace"
DEST="$WORKSPACE/$(basename "$FULL_PATH")"
cp "$FULL_PATH" "$DEST"

echo "Image generated successfully."
echo "SwarmUI path: $FULL_PATH"
echo "Workspace copy: $DEST"
echo "Open with: open \"$DEST\""
