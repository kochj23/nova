#!/bin/bash
#
# dream_add_image.sh — Generate the dream image and inject it into pending_delivery.json
#
# Runs at 2:05am after dream.py (or Nova's cron) has written the narrative.
# Reads pending_delivery.json, generates an image from the first sentence of the
# narrative, and updates the json with the image path.
#
# Usage: dream_add_image.sh
# Author: Jordan Koch

set -euo pipefail

PENDING="$HOME/.openclaw/workspace/journal/pending_delivery.json"
SCRIPTS="$HOME/.openclaw/scripts"

log() { echo "[dream_add_image.sh $(date +%H:%M:%S)] $*"; }

if [ ! -f "$PENDING" ]; then
    log "No pending_delivery.json found — nothing to do."
    exit 0
fi

# Check if image is already set
CURRENT_IMAGE=$(python3 -c "
import json, sys
d = json.load(open('$PENDING'))
img = d.get('image')
print('null' if img is None or img == '' else img)
")

if [ "$CURRENT_IMAGE" != "null" ]; then
    log "Image already set: $CURRENT_IMAGE — skipping."
    exit 0
fi

log "No image found in pending delivery — generating now..."

# Extract first sentence from narrative to build image prompt
IMAGE_CONCEPT=$(python3 -c "
import json, re
d = json.load(open('$PENDING'))
narrative = d.get('narrative', '')
# Get first sentence
sentences = re.split(r'(?<=[.!?])\s+', narrative.strip())
first = sentences[0][:120] if sentences else 'surreal digital dreamscape'
# Trim to ~60 chars for a clean concept phrase
words = first.split()
concept = ' '.join(words[:12])
print(concept)
")

PROMPT="dreamlike surreal digital painting, ${IMAGE_CONCEPT}, deep navy and indigo, soft amber light, ethereal atmosphere, painterly brushwork, cinematic wide shot, no text"

log "Image prompt: ${PROMPT:0:100}..."

# Generate image
OUTPUT=$("$SCRIPTS/generate_image.sh" "$PROMPT" 1024 1024 20 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    log "Image generation failed (exit $EXIT_CODE): $OUTPUT"
    exit 0
fi

# Parse workspace path from output
IMAGE_PATH=$(echo "$OUTPUT" | grep "^Workspace copy:" | sed 's/^Workspace copy: //')

if [ -z "$IMAGE_PATH" ] || [ ! -f "$IMAGE_PATH" ]; then
    log "Could not parse image path from output: $OUTPUT"
    exit 0
fi

log "Image generated: $IMAGE_PATH"

# Update pending_delivery.json with image path
python3 -c "
import json
path = '$IMAGE_PATH'
pending_file = '$PENDING'
d = json.load(open(pending_file))
d['image'] = path
with open(pending_file, 'w') as f:
    json.dump(d, f, indent=2)
print('Updated pending_delivery.json with image:', path)
"

# Also update the journal .md file to include the image
ENTRY_FILE=$(python3 -c "import json; print(json.load(open('$PENDING')).get('entry',''))")
if [ -n "$ENTRY_FILE" ] && [ -f "$ENTRY_FILE" ]; then
    python3 -c "
import re
entry_file = '$ENTRY_FILE'
image_path = '$IMAGE_PATH'
content = open(entry_file).read()
# Replace empty ![Dream]() or missing image line
if '![Dream]()' in content:
    content = content.replace('![Dream]()', '![Dream](' + image_path + ')')
elif '*Nova · written at 2am*' in content and '![Dream]' not in content:
    content = content.replace('*Nova · written at 2am*', '*Nova · written at 2am*\n![Dream](' + image_path + ')')
# Update the Image: none footer
content = content.replace('Image: none', 'Image: ' + image_path)
content = content.replace('Image: null', 'Image: ' + image_path)
open(entry_file, 'w').write(content)
print('Updated journal entry with image.')
"
fi

log "Done."
