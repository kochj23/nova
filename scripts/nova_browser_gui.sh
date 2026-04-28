#!/bin/zsh
# nova_browser_gui.sh — Run nova_browser.py in the GUI user session.
# Use this wrapper when Nova needs to browse from launchd context.
# It uses a temp file to preserve output formatting across the osascript boundary.
#
# Usage: nova_browser_gui.sh --fetch "https://example.com"
#        nova_browser_gui.sh --screenshot "https://example.com"

export HOME="${HOME:-/Users/$(whoami)}"

TMPOUT=$(mktemp /tmp/nova_browser_XXXXXX.txt)

/usr/bin/osascript -e "
do shell script \"export HOME=$HOME && export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin && export PYTHONPATH= && /opt/homebrew/bin/python3 $HOME/.openclaw/scripts/nova_browser.py $* > $TMPOUT 2>&1\"
"

cat "$TMPOUT"
rm -f "$TMPOUT"
