#!/bin/zsh
# mtplx_server_start.sh — start the MTPLX Qwen3.6-27B inference server on :5050.
#
# #686 Option A — LOGIN-time start (via ~/Library/LaunchAgents): runs inside the
# GUI/Aqua session, so it can read /Volumes/Data and use Metal/GPU WITHOUT a
# boot-time TCC/Full-Disk-Access grant (the boot-LaunchDaemon path needed one).
#
# A 120s settle delay keeps the ~6min 27B model-load Metal freeze from colliding
# with Ollama (the primary chat backend) right at login; Big Brother's
# _legit_inference_load guard already tolerates the load freeze (no kill/thrash).
#
# Written for Jordan (#686).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/wait-for-volume.sh" 2>/dev/null && wait_for_volume /Volumes/Data 180
sleep 120                       # let the system + Ollama settle before the GPU-heavy load
unset PYTHONPATH

exec /opt/homebrew/bin/python3.13 -m mtplx.server.openai \
  --model /Volumes/Data/mlx-models/mtplx/Youssofal--Qwen3.6-27B-MTPLX-Optimized-Speed \
  --backend-id qwen3_next --host 0.0.0.0 --port 5050 --depth 3 \
  --generation-mode mtp --profile sustained --reasoning-mode off --preserve-thinking auto \
  --verify-strategy capture_commit --verify-core linear-gdn-from-conv-tape \
  --draft-lm-head-bits 3 --draft-lm-head-group-size 64 --draft-lm-head-mode affine \
  --rate-limit 0 --stream-interval 1 --scheduler-mode serial --batching-preset latency \
  --warmup-tokens 16 --model-id mtplx-qwen36-27b-optimized-speed \
  --paged-kv-quantization off --fan-mode default --ssd-session-cache off \
  --ssd-session-cache-max-size 100GB --ssd-session-cache-min-prefix-tokens 512 \
  --draft-temperature 0.7 --draft-top-p 0.95 --draft-top-k 20 \
  --tool-prompt-mode hybrid --chat-template-profile local_qwen36 --no-strict-mlx-fork-assert \
  --temperature 0.6 --top-p 0.95 --top-k 20 --no-enable-thinking \
  --reasoning-parser qwen3 --reasoning-effort auto
