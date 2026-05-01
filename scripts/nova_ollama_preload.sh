#!/bin/bash
# nova_ollama_preload.sh — Preload critical Ollama models after boot.
# Ensures first task doesn't wait 30-60s for model loading.
# Called by scheduler every 4h, or manually.
#
# If models are already loaded, the warmup call takes <1s (cheap).
#
# Written by Jordan Koch.

OLLAMA_URL="http://127.0.0.1:11434"

# Wait for Ollama to be ready (up to 60s)
for i in $(seq 1 30); do
    if curl -sf "$OLLAMA_URL/" > /dev/null 2>&1; then
        break
    fi
    sleep 2
done

if ! curl -sf "$OLLAMA_URL/" > /dev/null 2>&1; then
    echo "[ollama_preload] Ollama not reachable after 60s — aborting"
    exit 1
fi

echo "[ollama_preload] Preloading models..."

# Preload nomic-embed-text (needed for all memory operations)
curl -s "$OLLAMA_URL/api/generate" -d '{"model":"nomic-embed-text","prompt":"warmup","stream":false,"options":{"num_predict":1}}' > /dev/null 2>&1
echo "[ollama_preload] nomic-embed-text loaded"

# Preload qwen3-coder:30b (needed for gardener, daily journal, dreams)
curl -s "$OLLAMA_URL/api/generate" -d '{"model":"qwen3-coder:30b","prompt":"warmup","stream":false,"options":{"num_predict":1}}' > /dev/null 2>&1
echo "[ollama_preload] qwen3-coder:30b loaded"

echo "[ollama_preload] Done."
