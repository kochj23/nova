#!/bin/bash
#
# generate_image.sh — Generate an image via ComfyUI for Nova
#
# TOOL FOR NOVA: Use this script to generate images from text prompts.
# Calls ComfyUI directly on port 8188 (more reliable than SwarmUI API which requires WebSocket).
#
# Usage: generate_image.sh "your prompt here" [width] [height] [steps] [model]
#
# Arguments:
#   $1  prompt  (required) — describe the image you want
#   $2  width   (optional, default 1024)
#   $3  height  (optional, default 1024)
#   $4  steps   (optional, default 8 — range 4-30, higher = more detail, slower)
#   $5  model   (optional, default Juggernaut_X_RunDiffusion_Hyper.safetensors)
#
# Examples:
#   generate_image.sh "a sunset over mountains, oil painting style"
#   generate_image.sh "portrait of a robot, detailed, cinematic lighting" 1024 1024 20
#
# Output: prints the full file path to the generated PNG image
# Author: Jordan Koch

set -euo pipefail

PROMPT="${1:-}"
WIDTH="${2:-1024}"
HEIGHT="${3:-1024}"
STEPS="${4:-8}"
MODEL="${5:-Juggernaut_X_RunDiffusion_Hyper.safetensors}"
COMFY_URL="http://127.0.0.1:8188"
OUTPUT_BASE="$HOME/AI/SwarmUI/Output/local/raw"
WORKSPACE="$HOME/.openclaw/workspace"
TIMEOUT=600   # 10 min max per generation

if [ -z "$PROMPT" ]; then
    echo "ERROR: No prompt provided." >&2
    echo "Usage: generate_image.sh \"your prompt here\" [width] [height] [steps] [model]" >&2
    exit 1
fi

# Check ComfyUI is running
if ! curl -sf "$COMFY_URL/system_stats" -o /dev/null --max-time 5 2>/dev/null; then
    echo "ERROR: ComfyUI not responding at $COMFY_URL" >&2
    exit 1
fi

# Build ComfyUI workflow and submit, then poll for result
RESULT=$(python3 - "$PROMPT" "$MODEL" "$WIDTH" "$HEIGHT" "$STEPS" "$OUTPUT_BASE" "$WORKSPACE" "$TIMEOUT" <<'PYEOF'
import json, sys, time, uuid, urllib.request, urllib.parse, os
from pathlib import Path
from datetime import datetime

PROMPT, MODEL, WIDTH, HEIGHT, STEPS, OUTPUT_BASE, WORKSPACE, TIMEOUT = sys.argv[1:]
COMFY = "http://127.0.0.1:8188"
client_id = str(uuid.uuid4())

workflow = {
    "4": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": MODEL}
    },
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": int(WIDTH), "height": int(HEIGHT), "batch_size": 1}
    },
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": PROMPT, "clip": ["4", 1]}
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "blurry, low quality, distorted, watermark, text, logo, nudity, nude, nsfw, explicit, nipples, sexual content, bare skin, revealing clothing, dark, underexposed, too dark, black image, nearly black, dim, murky, low light, pitch black, unlit", "clip": ["4", 1]}
    },
    "8": {
        "class_type": "KSampler",
        "inputs": {
            "model": ["4", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
            "seed": int(time.time()) % 2**31,
            "steps": int(STEPS),
            "cfg": 7.0,
            "sampler_name": "euler",
            "scheduler": "normal",
            "denoise": 1.0
        }
    },
    "9": {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["8", 0], "vae": ["4", 2]}
    },
    "10": {
        "class_type": "SaveImage",
        "inputs": {
            "images": ["9", 0],
            "filename_prefix": datetime.now().strftime("%H%M")
        }
    }
}

# Submit to ComfyUI
payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
req = urllib.request.Request(
    f"{COMFY}/prompt", data=payload,
    headers={"Content-Type": "application/json"}
)
with urllib.request.urlopen(req, timeout=15) as r:
    result = json.loads(r.read())
    prompt_id = result.get("prompt_id")

if not prompt_id:
    print("ERROR: No prompt_id returned", file=sys.stderr)
    sys.exit(1)

# Poll for completion
deadline = time.time() + int(TIMEOUT)
while time.time() < deadline:
    time.sleep(3)
    with urllib.request.urlopen(f"{COMFY}/history/{prompt_id}", timeout=5) as r:
        hist = json.loads(r.read())
    if not hist:
        continue
    job = hist.get(prompt_id, {})
    status = job.get("status", {})
    if status.get("status_str") == "success" and status.get("completed"):
        for node_id, output in job.get("outputs", {}).items():
            for img in output.get("images", []):
                fname = img["filename"]
                subdir = img.get("subfolder", "")
                # ComfyUI saves to its output dir
                comfy_output = Path("/Volumes/Data/AI/SwarmUI/dlbackend/ComfyUI/output")
                if subdir:
                    src = comfy_output / subdir / fname
                else:
                    src = comfy_output / fname
                if not src.exists():
                    # Try SwarmUI output dir as fallback
                    today = datetime.now().strftime("%Y-%m-%d")
                    src = Path(OUTPUT_BASE) / today / fname
                                # Download from ComfyUI /view endpoint (most reliable path)
                    dest = Path(WORKSPACE) / fname
                    view_url = f"{COMFY}/view?filename={urllib.parse.quote(fname)}&subfolder={urllib.parse.quote(subdir)}&type=output"
                    try:
                        with urllib.request.urlopen(view_url, timeout=30) as img_resp:
                            dest.write_bytes(img_resp.read())
                        # Also copy to SwarmUI output for compatibility
                        today = datetime.now().strftime("%Y-%m-%d")
                        out_dir = Path(OUTPUT_BASE) / today
                        out_dir.mkdir(parents=True, exist_ok=True)
                        import shutil
                        shutil.copy2(dest, out_dir / fname)
                        swarm_path = out_dir / fname
                        print(f"Image generated successfully.")
                        print(f"SwarmUI path: {swarm_path}")
                        print(f"Workspace copy: {dest}")
                        print(f"Open with: open \"{dest}\"")
                        sys.exit(0)
                    except Exception as e:
                        print(f"ERROR downloading image: {e}", file=sys.stderr)
                        sys.exit(1)
    elif status.get("status_str") in ("error", "failed"):
        msgs = status.get("messages", [])
        for msg in msgs:
            if msg[0] == "execution_error":
                print(f"ERROR: {msg[1].get('exception_message','unknown error')}", file=sys.stderr)
        sys.exit(1)

print("ERROR: Generation timed out", file=sys.stderr)
sys.exit(1)
PYEOF
)

echo "$RESULT"
WORKSPACE="$HOME/.openclaw/workspace"
# Extract workspace path for callers that need just the path
DEST_PATH=$(echo "$RESULT" | grep "^Workspace copy:" | sed 's/Workspace copy: //')
if [ -z "$DEST_PATH" ] || [ ! -f "$DEST_PATH" ]; then
    exit 1
fi
