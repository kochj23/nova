#!/bin/zsh
# brew_autoupdate.sh — weekly Homebrew formula upgrade (LaunchAgent net.digitalnoise.brew-autoupdate, Sun 04:30).
# Policy: formulae only (never casks), pinned formulae are never touched, never restarts Nova services.
# After a successful run, notifies via nova_notify (if importable) — nova_daemon_staleness then flags any
# daemon running stale code and the operator restarts it. Runbook: agent_docs doc_type=runbook-mac-autoupdate.
set -o pipefail
export HOME="${HOME:-/Users/kochj}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ENV_HINTS=1 HOMEBREW_NO_INSTALL_CLEANUP=1
LOG_DIR="$HOME/.openclaw/logs"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/brew-autoupdate.log"
HOST="$(hostname -s)"
exec >>"$LOG" 2>&1
ts() { date '+%Y-%m-%d %H:%M:%S'; }

echo "===== $(ts) brew-autoupdate start on $HOST ====="
PINNED="$(brew list --pinned 2>/dev/null | tr '\n' ' ')"
echo "[$(ts)] pinned (never upgraded): ${PINNED:-none}"

echo "[$(ts)] brew update"
brew update; rc=$?; [ $rc -eq 0 ] || { echo "[$(ts)] FAIL: brew update rc=$rc"; exit 1; }

BEFORE="$(brew list --formula --versions | sort)"
OUTDATED="$(brew outdated --formula --quiet | tr '\n' ' ')"
echo "[$(ts)] outdated formulae: ${OUTDATED:-none}"

echo "[$(ts)] brew upgrade --formula (casks skipped by policy)"
brew upgrade --formula; rc=$?; [ $rc -eq 0 ] || echo "[$(ts)] WARN: brew upgrade rc=$rc (some formulae failed; continuing with cleanup+notify for the ones that did upgrade)"

AFTER="$(brew list --formula --versions | sort)"
UPGRADED="$(comm -13 <(echo "$BEFORE") <(echo "$AFTER") | awk '{print $1}' | sort -u | tr '\n' ' ')"
N="$(echo "$UPGRADED" | wc -w | tr -d ' ')"
echo "[$(ts)] upgraded $N formulae: ${UPGRADED:-none}"

echo "[$(ts)] brew cleanup --prune=30"
brew cleanup --prune=30; rc=$?; [ $rc -eq 0 ] || echo "[$(ts)] warn: cleanup rc=$rc"

if [ "$N" -gt 0 ]; then
  UPGRADED="$UPGRADED" N="$N" HOST="$HOST" python3 - <<'PY' || echo "[$(ts)] nova_notify unavailable; logged only"
import os, sys
sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
from nova_notify import notify
h = os.environ["HOST"]
notify(title="Homebrew weekly upgrade",
       body=f"{os.environ['N']} formulae upgraded on {h}: {os.environ['UPGRADED'].strip()}",
       level="info", category="telemetry", source="brew-autoupdate", dedup_key=f"brew-autoupdate-{h}")
PY
fi
echo "===== $(ts) brew-autoupdate done on $HOST ====="
