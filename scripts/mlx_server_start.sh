#!/bin/zsh
# mlx_server_start.sh — Start MLX LM server with speculative decoding.
# Called by launchd. Lives in ~/.openclaw/scripts/ to avoid TCC/Tahoe issues.
# Updated 2026-04-28: Added draft model for 2-3x speedup on general tasks.

export HOME="${HOME:-/Users/$(whoami)}"
export PATH="/opt/homebrew/bin:$PATH"

exec /opt/homebrew/bin/mlx_lm.server \
    --model /Volumes/Data/mlx-models/qwen2.5-32b-4bit \
    --draft-model /Volumes/Data/mlx-models/qwen2.5-0.5b-4bit \
    --num-draft-tokens 6 \
    --host 192.168.1.6 \
    --port 5050
