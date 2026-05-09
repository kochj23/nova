#!/bin/bash
# nova_ollama_preload.sh — Preload critical Ollama models after boot.
# Ensures first task doesn't wait 30-60s for model loading.
# Called by scheduler every 4h, or manually.
#
# Written by Jordan Koch.

OLLAMA_URL="http://127.0.0.1:11434"

for i in $(seq 1 30); do
    if curl -sf "$OLLAMA_URL/api/version" > /dev/null 2>&1; then break; fi
    sleep 2
done

if ! curl -sf "$OLLAMA_URL/api/version" > /dev/null 2>&1; then
    echo "[ollama_preload] Ollama not reachable after 60s — aborting"
    exit 1
fi

echo "[ollama_preload] Preloading models..."

# nomic-embed-text is embedding-only — use /api/embed not /api/generate
curl -sf "$OLLAMA_URL/api/embed" \
    -d '{"model":"nomic-embed-text","input":"warmup"}' > /dev/null 2>&1
echo "[ollama_preload] nomic-embed-text loaded"

# LLMs use /api/generate
for model in "qwen3-coder:30b" "deepseek-r1:8b"; do
    curl -sf "$OLLAMA_URL/api/generate" \
        -d "{\"model\":\"$model\",\"prompt\":\"hi\",\"stream\":false,\"options\":{\"num_predict\":1}}" \
        > /dev/null 2>&1
    echo "[ollama_preload] $model loaded"
done

echo "[ollama_preload] Done."
