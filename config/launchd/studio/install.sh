#!/bin/bash
# Install the Studio's Nova launchd jobs from these templates (__HOME__ -> this user's home).
# Snapshot taken 2026-10-14 so a macOS reinstall can't lose them. Usage: install.sh [--load]
set -euo pipefail
cd "$(dirname "$0")"
for f in LaunchAgents/*.plist; do sed "s#__HOME__#$HOME#g" "$f" > "$HOME/Library/LaunchAgents/$(basename "$f")"; done
for f in LaunchDaemons/*.plist; do sed "s#__HOME__#$HOME#g" "$f" | sudo tee "/Library/LaunchDaemons/$(basename "$f")" >/dev/null; done
if [ "${1:-}" = "--load" ]; then
  for f in LaunchAgents/*.plist; do launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$(basename "$f")" 2>/dev/null || true; done
  for f in LaunchDaemons/*.plist; do sudo launchctl bootstrap system "/Library/LaunchDaemons/$(basename "$f")" 2>/dev/null || true; done
fi
