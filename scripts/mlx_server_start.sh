#!/bin/zsh
# mlx_server_start.sh — Start MLX LM server after boot.
# Called by launchd. Lives in ~/.openclaw/scripts/ to avoid TCC/Tahoe issues.

export HOME="${HOME:-/Users/$(whoami)}"
export PATH="/opt/homebrew/bin:$PATH"

exec /opt/homebrew/bin/mlx_lm.server \
    --model /Volumes/Data/mlx-models/qwen2.5-32b-4bit \
    --host 192.168.1.6 \
    --port 5050
