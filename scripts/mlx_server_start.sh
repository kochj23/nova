#!/bin/zsh
# mlx_server_start.sh — Start MLX LM server on :5050. Called by launchd at login.
#
# 2026-06-22 incident notes:
#   * --draft-model (qwen2.5-0.5b speculative) now HANGS generation under
#     mlx_lm 0.31.3 — loads /v1/models fine, but chat completions never return.
#     Removed; plain decode generates reliably. Re-add only once the draft path
#     is verified working again.
#   * unset PYTHONPATH: the login shell exports a Python-3.14 site dir that breaks
#     mlx_lm's imports if this job inherits it.
#   * MTPLX migration (Qwen3.6-27B, runs great standalone at ~61 tok/s) is staged
#     but its launchd-spawned process wouldn't bind :5050 from a re-bootstrap.
#     Cutover deferred — see the queued MTPLX task.

export HOME="${HOME:-/Users/$(whoami)}"
export PATH="/opt/homebrew/bin:$PATH"
unset PYTHONPATH

# Models live on /Volumes/Data — after a reboot the volume may mount late.
source "$HOME/.openclaw/scripts/wait-for-volume.sh"
wait_for_volume "/Volumes/Data" 180 || { echo "[mlx_server_start] FATAL: /Volumes/Data unavailable" >&2; exit 1; }

exec /opt/homebrew/bin/mlx_lm.server \
    --model /Volumes/Data/mlx-models/qwen2.5-32b-4bit \
    --host 0.0.0.0 \
    --port 5050
