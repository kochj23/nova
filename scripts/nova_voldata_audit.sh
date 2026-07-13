#!/bin/bash
# nova_voldata_audit.sh — inventory /Volumes/Data + /Volumes/MoreData for the Phase-2
# reclaim audit. MUST run under launchd (or an FDA-granted context): these volumes are
# TCC/FDA-blocked from a normal shell. Writes a plain-text report Claude can read from
# ~/.openclaw/logs/ (a non-blocked location).
LOG="$HOME/.openclaw/logs/voldata-audit.log"
PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

{
  echo "===== nova voldata audit — $(date) ====="
  for VOL in /Volumes/Data /Volumes/MoreData; do
    echo ""
    echo "########## $VOL ##########"
    df -h "$VOL" 2>/dev/null | tail -1 | awk '{print "  df: "$3" used, "$4" free ("$5")"}'
    echo "  --- top-level (du -sh, biggest first) ---"
    du -sh "$VOL"/* "$VOL"/.[!.]* 2>/dev/null | sort -rh | head -30 | sed 's/^/    /'
  done

  echo ""
  echo "########## Ollama models (HOT served vs orphaned blobs) ##########"
  OLL=$(ls -d /Volumes/Data/.ollama /Volumes/Data/ollama-models /Volumes/Data/ollama 2>/dev/null | head -1)
  echo "  models dir: ${OLL:-not found}"
  [ -n "$OLL" ] && du -sh "$OLL"/models 2>/dev/null | sed 's/^/    total: /'
  [ -n "$OLL" ] && echo "  blobs: $(du -sh "$OLL"/models/blobs 2>/dev/null | cut -f1), $(ls "$OLL"/models/blobs 2>/dev/null | wc -l | tr -d ' ') blob files"
  [ -n "$OLL" ] && echo "  manifests: $(find "$OLL"/models/manifests -type f 2>/dev/null | wc -l | tr -d ' ')"
  echo "  --- served models (ollama list) ---"
  ollama list 2>/dev/null | sed 's/^/    /' | head -25

  echo ""
  echo "########## PG dump backups (retention=7d) ##########"
  du -sh /Volumes/Data/backups/postgres 2>/dev/null | sed 's/^/  total: /'
  ls -1 /Volumes/Data/backups/postgres 2>/dev/null | wc -l | awk '{print "  snapshot dirs: "$1}'
  ls -1t /Volumes/Data/backups/postgres 2>/dev/null | head -3 | sed 's/^/    newest: /'

  echo ""
  echo "########## HF / whisper model cache ##########"
  du -sh /Volumes/Data/huggingface 2>/dev/null | sed 's/^/  /'

  echo ""
  echo "AUDIT DONE $(date)"
} > "$LOG" 2>&1
