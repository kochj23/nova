#!/bin/zsh
# nova_config_validate.sh — Validate openclaw.json before any write operation.
#
# Usage:
#   nova_config_validate.sh check               — validate current file, exit 0 if ok
#   nova_config_validate.sh before-write        — call before modifying openclaw.json;
#                                                  backs up .last-good if currently valid
#
# Written by Jordan Koch.

CONFIG="$HOME/.openclaw/openclaw.json"
BACKUP="$HOME/.openclaw/openclaw.json.last-good"
BACKUPS_DIR="$HOME/.openclaw/backups"

_validate() {
    # 1. Valid JSON
    if ! python3 -c "import json,sys; json.load(open('$CONFIG'))" 2>/dev/null; then
        echo "[config_validate] FAIL: openclaw.json is invalid JSON" >&2
        return 1
    fi

    # 2. Required top-level keys exist
    local missing
    missing=$(python3 -c "
import json
d = json.load(open('$CONFIG'))
required = ['channels', 'gateway', 'agents']
missing = [k for k in required if k not in d]
print(' '.join(missing))
" 2>/dev/null)
    if [[ -n "$missing" ]]; then
        echo "[config_validate] FAIL: missing required keys: $missing" >&2
        return 1
    fi

    # 3. No additional properties in channels.signal (known to break gateway)
    local signal_keys
    signal_keys=$(python3 -c "
import json
d = json.load(open('$CONFIG'))
signal = d.get('channels', {}).get('signal', {})
allowed = {'enabled','account','cliPath','autoStart','dmPolicy','groupPolicy',
           'sendReadReceipts','ignoreStories','ignoreAttachments','allowFrom',
           'receiveMode','httpHost','httpPort'}
extra = [k for k in signal.keys() if k not in allowed]
print(' '.join(extra))
" 2>/dev/null)
    if [[ -n "$signal_keys" ]]; then
        echo "[config_validate] FAIL: unknown signal config keys (will crash gateway): $signal_keys" >&2
        return 1
    fi

    return 0
}

case "${1:-check}" in
    check)
        if _validate; then
            echo "[config_validate] OK: openclaw.json is valid"
            exit 0
        else
            exit 1
        fi
        ;;
    before-write)
        # If the CURRENT config is valid, save it as last-good before we overwrite
        if _validate 2>/dev/null; then
            mkdir -p "$BACKUPS_DIR"
            cp "$CONFIG" "$BACKUP"
            DATED="$BACKUPS_DIR/openclaw.json.$(date +%Y%m%d-%H%M%S)"
            cp "$CONFIG" "$DATED"
            echo "[config_validate] Saved valid config backup to $BACKUP and $DATED"
        fi
        exit 0
        ;;
    *)
        echo "Usage: $0 {check|before-write}"
        exit 1
        ;;
esac
