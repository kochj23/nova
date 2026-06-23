#!/bin/zsh
# mlx_server_start.sh — Start MLX LM server with speculative decoding.
# Called by launchd. Lives in ~/.openclaw/scripts/ to avoid TCC/Tahoe issues.
# Updated 2026-04-28: Added draft model for 2-3x speedup on general tasks.
#
# 2026-06-22: MTPLX migration attempted but it crash-loops under launchd (works
#   fine in foreground). Rolled BACK to mlx_lm.server to keep inference up.
#   MTPLX cutover continues as a follow-up — see /Volumes/Data/mlx-models/mtplx
#   and the queued cutover task. To retry: swap the exec to the MTPLX one once
#   the launchd-env startup issue is solved.

export HOME="${HOME:-/Users/$(whoami)}"
export PATH="/opt/homebrew/bin:$PATH"
# The login shell exports PYTHONPATH=/Volumes/Data/AI/python_packages (a Python-3.14
# site) which breaks mlx_lm's imports when this job is bootstrapped from that shell.
# Wipe it so inference loads cleanly regardless of how launchd was started.
unset PYTHONPATH

# Models live on /Volumes/Data — after a reboot the volume may mount late.
# Without this wait the server starts with a missing --draft-model path and
# silently runs degraded (no speculative decoding). Block until it's ready.
source "$HOME/.openclaw/scripts/wait-for-volume.sh"
wait_for_volume "/Volumes/Data" 180 || { echo "[mlx_server_start] FATAL: /Volumes/Data unavailable" >&2; exit 1; }

# Launch with a fully clean env (env -i). Bootstrapping this job from an
# interactive shell otherwise leaks env that hangs the model load under launchd;
# `env -i` with only HOME/PATH matches the invocation that loads in ~15s.
exec env -i HOME=/Users/kochj PATH=/opt/homebrew/bin:/usr/bin:/bin \
    /opt/homebrew/bin/mlx_lm.server \
    --model /Volumes/Data/mlx-models/qwen2.5-32b-4bit \
    --draft-model /Volumes/Data/mlx-models/qwen2.5-0.5b-4bit \
    --num-draft-tokens 6 \
    --host 0.0.0.0 \
    --port 5050
