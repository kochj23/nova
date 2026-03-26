#!/bin/zsh
PHRASE_FILE="$HOME/.openclaw/workspace/spanish_phrases.txt"
if [ ! -f "$PHRASE_FILE" ]; then
  echo "Error: Spanish phrases file not found. Create $PHRASE_FILE with one phrase per line." >&2
  exit 1
fi
PHRASE=$(shuf -n 1 "$PHRASE_FILE")
/opt/homebrew/bin/openclaw message send --target "C0AMNQ5GX70" --message "Daily Spanish phrase: $PHRASE"