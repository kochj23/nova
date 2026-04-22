#!/bin/zsh
# openwebui_start.sh — Start OpenWebUI after Ollama is ready.
# Called by launchd. Uses VIRTUAL_ENV + -S to skip site.py venv scan
# which triggers TCC PermissionError on macOS Sequoia for external volumes.
# Written by Jordan Koch.

source "$HOME/.openclaw/scripts/wait-for-port.sh"
wait_for_port 11434 "Ollama" 120 || exit 1

export DATA_DIR=/Volumes/Data/openwebui/data
export OLLAMA_BASE_URL=http://127.0.0.1:11434
export WEBUI_AUTH=false
export HOME="${HOME:-/Users/$(whoami)}"
export FRONTEND_BUILD_DIR=/Volumes/Data/openwebui/venv/lib/python3.12/site-packages/open_webui/frontend

# Use homebrew Python 3.12 directly (not the venv symlink) to avoid
# site.py scanning pyvenv.cfg in /Volumes/Data (blocked by TCC).
# Inject site-packages via sys.path before importing open_webui.
exec /opt/homebrew/bin/python3.12 -S -c "
import sys
sys.path.insert(0, '/Volumes/Data/openwebui/venv/lib/python3.12/site-packages')
import site; site.main()
from open_webui.main import app
import uvicorn
uvicorn.run(app, host='127.0.0.1', port=3000, log_level='info')
"
