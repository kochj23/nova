#!/bin/zsh
# searxng_start.sh — Start local SearXNG search engine.
# Called by launchd. Written by Jordan Koch.
# Port 8888, loopback only.

export HOME="${HOME:-/Users/$(whoami)}"
export SEARXNG_SETTINGS_PATH="/Volumes/Data/searxng/searx/settings.yml"

cd /Volumes/Data/searxng
exec /Volumes/Data/searxng/venv/bin/python3 -m searx.webapp
