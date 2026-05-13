#!/bin/bash
# nova_ollama_preload.sh — Preload critical Ollama models after boot.
# Ensures first task doesn't wait 30-60s for model loading.
# Called by scheduler every 4h, or manually.
#
# Written by Jordan Koch.

# Ollama.app only binds to localhost regardless of system LAN config
OLLAMA_URL="http://127.0.0.1:11434"

# Wait up to 60s for Ollama to be reachable
for i in $(seq 1 30); do
    if curl -sf "$OLLAMA_URL/api/version" > /dev/null 2>&1; then break; fi
    sleep 2
done

if ! curl -sf "$OLLAMA_URL/api/version" > /dev/null 2>&1; then
    echo "[ollama_preload] Ollama not reachable after 60s — aborting"
    exit 1
fi

echo "[ollama_preload] Preloading models..."

# Skip models already warm — avoids unnecessarily evicting other models
warm_models=$(curl -sf "$OLLAMA_URL/api/ps" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(' '.join(m['name'] for m in d.get('models', [])))
" 2>/dev/null)

# nomic-embed-text: embedding only — warm fast (~2s)
if echo "$warm_models" | grep -q "nomic-embed-text"; then
    echo "[ollama_preload] nomic-embed-text already warm — skipping"
else
    curl -sf "$OLLAMA_URL/api/embed" \
        -d '{"model":"nomic-embed-text","input":"warmup"}' > /dev/null 2>&1
    echo "[ollama_preload] nomic-embed-text loaded"
fi

# qwen3-coder:30b: ~7.5 min cold load — load with generous per-model timeout
# deepseek-r1:8b: ~30s cold load
declare -A MODEL_TIMEOUT=( ["qwen3-coder:30b"]="600" ["deepseek-r1:8b"]="120" )

for model in "qwen3-coder:30b" "deepseek-r1:8b"; do
    if echo "$warm_models" | grep -q "$model"; then
        echo "[ollama_preload] $model already warm — skipping"
        continue
    fi
    timeout_s="${MODEL_TIMEOUT[$model]:-300}"
    echo "[ollama_preload] Loading $model (timeout ${timeout_s}s)..."
    curl -sf --max-time "$timeout_s" "$OLLAMA_URL/api/generate" \
        -d "{\"model\":\"$model\",\"prompt\":\"hi\",\"stream\":false,\"options\":{\"num_predict\":1}}" \
        > /dev/null 2>&1
    rc=$?
    if [ $rc -eq 0 ]; then
        echo "[ollama_preload] $model loaded"
    elif [ $rc -eq 28 ]; then
        echo "[ollama_preload] $model timed out after ${timeout_s}s — still loading in background"
    else
        echo "[ollama_preload] $model failed (curl exit $rc)"
    fi
done

echo "[ollama_preload] Done."
