#!/usr/bin/env bash
# nova-node-join.sh — turn an idle laptop/desktop into a Nova inference node. Run ON the machine.
#
#   curl -fsSL <this file> | bash          # or: bash nova-node-join.sh [model]
#
# Works the same on NVIDIA (CUDA), AMD (ROCm), Apple Silicon (Metal), Intel/other GPUs (Vulkan) and plain CPU,
# because it uses Ollama — one binary that picks the right accelerator itself. Then, from any Nova host:
#   python3 ~/.openclaw/scripts/nova_rosetta.py --join <this machine's IP>
# and nova_rosetta/nova_lb route canonical-model work to it within a minute. Windows: install Ollama from
# ollama.com, set OLLAMA_HOST=0.0.0.0 and OLLAMA_KEEP_ALIVE=-1 as user env vars, `ollama pull <model>`, then --join.
set -euo pipefail

MODEL="${1:-}"
say() { printf '\033[1m[nova-join]\033[0m %s\n' "$*"; }

os="$(uname -s)"
if ! command -v ollama >/dev/null 2>&1; then
  say "installing Ollama (official installer)"
  if [ "$os" = "Darwin" ]; then
    command -v brew >/dev/null || { say "install Homebrew or Ollama.app first"; exit 1; }
    brew install ollama
  else
    curl -fsSL https://ollama.com/install.sh | sh     # sets up CUDA/ROCm runtimes itself
  fi
fi

# Size the default model to the memory this machine actually has (GPU VRAM if we can see it, else RAM).
mem_gb=0
if command -v nvidia-smi >/dev/null 2>&1; then
  mem_gb=$(( $(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1) / 1024 ))
elif [ "$os" = "Darwin" ]; then
  mem_gb=$(( $(sysctl -n hw.memsize) / 1073741824 * 2 / 3 ))      # unified memory, leave room for macOS
else
  mem_gb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1048576 / 2 ))
fi
if [ -z "$MODEL" ]; then
  if   [ "$mem_gb" -ge 24 ]; then MODEL="qwen3:30b-a3b"
  elif [ "$mem_gb" -ge 10 ]; then MODEL="qwen3:14b"
  elif [ "$mem_gb" -ge 6 ];  then MODEL="qwen3:8b"
  else                            MODEL="llama3.2:3b"; fi
fi
say "usable memory ~${mem_gb} GB -> model ${MODEL}"

# Listen on the LAN, keep the model resident (no cold loads), one context size for every client.
if [ "$os" = "Darwin" ]; then
  launchctl setenv OLLAMA_HOST 0.0.0.0
  launchctl setenv OLLAMA_KEEP_ALIVE -1
  launchctl setenv OLLAMA_CONTEXT_LENGTH 16384
  if brew services list 2>/dev/null | grep -q '^ollama'; then brew services restart ollama; else (ollama serve >/tmp/ollama.log 2>&1 &); fi
else
  sudo mkdir -p /etc/systemd/system/ollama.service.d
  printf '[Service]\nEnvironment=OLLAMA_HOST=0.0.0.0\nEnvironment=OLLAMA_KEEP_ALIVE=-1\nEnvironment=OLLAMA_CONTEXT_LENGTH=16384\n' \
    | sudo tee /etc/systemd/system/ollama.service.d/20-nova.conf >/dev/null
  sudo systemctl daemon-reload && sudo systemctl enable --now ollama && sudo systemctl restart ollama
fi

for _ in $(seq 1 30); do curl -fs http://127.0.0.1:11434/api/version >/dev/null && break; sleep 1; done
say "pulling ${MODEL}"
ollama pull "$MODEL"
say "loading it into memory"
curl -fs http://127.0.0.1:11434/api/generate -d "{\"model\":\"${MODEL}\",\"keep_alive\":-1}" >/dev/null

ip="$( (ipconfig getifaddr en0 || ipconfig getifaddr en1 || hostname -I | awk '{print $1}') 2>/dev/null | head -1)"
say "ready. From a Nova host run:"
echo "    python3 ~/.openclaw/scripts/nova_rosetta.py --join ${ip:-<this-ip>}"
