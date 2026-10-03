#!/bin/bash
# nova_claude_config_sync.sh — make every nova-core node's Claude Code look like .6's.
# Syncs ~/.claude/{CLAUDE.md,hooks,mcp-servers,skills,commands,plugins} + a settings.json
# with the local HOME rewritten to the remote $HOME and .6-only MCP servers (metasploit) dropped.
# Credentials are NOT handled here — nova_claude_cred_sync.py owns that. Run on .6.
# ponytail: plain rsync + sed; no templating until a node needs a genuinely different config.
set -u
NODES=${NODES:-"192.168.1.2 192.168.1.86 192.168.1.5 192.168.1.250 192.168.1.10 192.168.1.125 192.168.1.77 192.168.1.7 192.168.1.252"}
SRC="$HOME/.claude"
SSH="ssh -o ConnectTimeout=6 -o BatchMode=yes"
ok=0; fail=0
for ip in $NODES; do
  rhome=$($SSH "kochj@$ip" 'echo $HOME' 2>/dev/null) || { echo "FAIL $ip ssh"; fail=$((fail+1)); continue; }
  $SSH "kochj@$ip" 'mkdir -p ~/.claude ~/.local/bin; [ -x ~/.local/bin/uv ] || { u=$(command -v uv 2>/dev/null || ls /opt/homebrew/bin/uv 2>/dev/null); [ -n "$u" ] && ln -sf "$u" ~/.local/bin/uv; }; true'
  for d in hooks mcp-servers skills commands plugins; do
    rsync -aq --delete --exclude '__pycache__' -e "$SSH" "$SRC/$d/" "kochj@$ip:~/.claude/$d/" || echo "WARN $ip rsync $d"
  done
  rsync -aq -e "$SSH" "$SRC/CLAUDE.md" "kochj@$ip:~/.claude/CLAUDE.md"
  python3 - "$rhome" <<'PY' | $SSH "kochj@$ip" 'cat > ~/.claude/settings.json'
import json, sys, os
rhome = sys.argv[1]; home = os.path.expanduser("~")
d = json.load(open(os.path.join(home, ".claude/settings.json")))
d.get("mcpServers", {}).pop("metasploit", None)          # .6-only (security-scans venv)
d.pop("feedbackSurveyState", None)
print(json.dumps(d, indent=2).replace(home, rhome))
PY
  echo "ok   $ip ($rhome)"; ok=$((ok+1))
done
echo "claude-config-sync: $ok ok, $fail failed"
