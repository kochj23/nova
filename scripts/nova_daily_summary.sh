#!/bin/bash
# Nova Daily Summary — Post to Slack at 11:59pm
# Posts: "Here's everything I learned today"

TODAY=$(date +%Y-%m-%d)
MEMORY_FILE="$HOME/.openclaw/workspace/memory/${TODAY}.md"

if [ ! -f "$MEMORY_FILE" ]; then
  echo "No memories recorded today."
  exit 0
fi

# Build summary from memory file
SUMMARY=$(cat "$MEMORY_FILE" | head -100)

# Format for Slack (using message tool)
python3 << PYSCRIPT
import subprocess
import os

today = "$TODAY"
summary = """$SUMMARY"""

message = f"""📖 DAILY LEARNING SUMMARY — {today}

{summary}

---
Full details stored in vector memory system.
—N"""

# Post via OpenClaw message tool
subprocess.run([
  "python3", "-m", "openclaw.tools.message",
  "--action", "send",
  "--channel", "slack", 
  "--target", "C0AMNQ5GX70",
  "--message", message
], capture_output=True)

print(f"✓ Posted daily summary to Slack")
PYSCRIPT
