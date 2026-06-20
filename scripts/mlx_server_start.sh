#!/bin/zsh
# mlx_server_start.sh — Start MLX LM server with speculative decoding.
# Called by launchd. Lives in ~/.openclaw/scripts/ to avoid TCC/Tahoe issues.
# Updated 2026-04-28: Added draft model for 2-3x speedup on general tasks.

export HOME="${HOME:-/Users/$(whoami)}"
export PATH="/opt/homebrew/bin:$PATH"

# Models live on /Volumes/Data — after a reboot the volume may mount late.
# Without this wait the server starts with a missing --draft-model path and
# silently runs degraded (no speculative decoding). Block until it's ready.
source "$HOME/.openclaw/scripts/wait-for-volume.sh"
wait_for_volume "/Volumes/Data" 180 || { echo "[mlx_server_start] FATAL: /Volumes/Data unavailable" >&2; exit 1; }

exec /opt/homebrew/bin/mlx_lm.server \
    --model /Volumes/Data/mlx-models/qwen2.5-32b-4bit \
    --draft-model /Volumes/Data/mlx-models/qwen2.5-0.5b-4bit \
    --num-draft-tokens 6 \
    --host 0.0.0.0 \
    --port 5050
