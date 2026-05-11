"""
nova_image_utils.py — Shared image generation with retry logic, backend health checks, and model rotation.

Used by: nova_daily_essay.py, nova_after_dark.py, nova_daily_opinion.py, nova_research_paper.py,
         nova_art_corner.py, nova_tech_today.py, nova_fix_missing_images.py

Written by Jordan Koch.
"""

import json
import random
import subprocess
import time
import urllib.request
from pathlib import Path

GENERATE_IMAGE_SH = Path.home() / ".openclaw/scripts/generate_image.sh"
SWARMUI_URL = "http://127.0.0.1:7801"
MAX_RETRIES = 3
RETRY_DELAY = 15
TIMEOUT = 360

# Available models with their optimal settings
# NOTE: FP8 models (flux1-dev-fp8, flux1-schnell-fp8, ZImage FP8Mix) are BROKEN on
# macOS MPS as of 2026-05-10 — ComfyUI throws:
#   "Trying to convert Float8_e4m3fn to the MPS backend but it does not have support for that dtype"
# BF16 replacements need to be downloaded from HuggingFace (requires login — gated repos).
# Pending download: flux1-dev.safetensors, flux1-schnell.safetensors (BF16, ~23GB each)
# Interim: all slots use Juggernaut or LongCat until BF16 models are downloaded.
MODELS = {
    "juggernaut": {
        "file": "Juggernaut_X_RunDiffusion_Hyper.safetensors",
        "name": "Juggernaut XL v10 Hyper",
        "best_for": "photorealism, textures, fast generation",
        "optimal_steps": 8,
        "max_steps": 15,
    },
    # FP8 — broken on MPS. Will be restored once BF16 file downloaded.
    "zimage": {
        "file": "ZImage/SwarmUI_Z-Image-Turbo-FP8Mix.safetensors",
        "name": "Z-Image Turbo (FP8 — MPS broken, using juggernaut fallback)",
        "best_for": "realism, speed",
        "optimal_steps": 6,
        "max_steps": 12,
    },
    # FP8 — broken on MPS. Pending: flux1-schnell.safetensors (BF16)
    "flux_schnell": {
        "file": "flux1-schnell-fp8.safetensors",
        "name": "FLUX.1 schnell (FP8 — MPS broken, using longcat fallback)",
        "best_for": "quality, prompt adherence, fast",
        "optimal_steps": 4,
        "max_steps": 8,
    },
    # FP8 — broken on MPS. Pending: flux1-dev.safetensors (BF16)
    "flux_dev": {
        "file": "flux1-dev-fp8.safetensors",
        "name": "FLUX.1 dev (FP8 — MPS broken, using juggernaut fallback)",
        "best_for": "top quality, best prompt adherence",
        "optimal_steps": 20,
        "max_steps": 50,
    },
    "longcat": {
        "file": "LongCat-Image.safetensors",
        "name": "LongCat-Image",
        "best_for": "text rendering, complex prompts, watercolor",
        "optimal_steps": 20,
        "max_steps": 40,
    },
}

# Default model for quick generation (covers, thumbnails)
DEFAULT_MODEL = "juggernaut"

# Art Corner rotation — matches day-of-week styles to models
# INTERIM: FP8 slots replaced with working SDXL models until BF16 FLUX downloaded.
# Restore once flux1-dev.safetensors + flux1-schnell.safetensors are in Models dir.
ART_MODEL_ROTATION = {
    0: "juggernaut",    # Monday: Photorealism → Juggernaut (was: flux_dev FP8 broken)
    1: "juggernaut",    # Tuesday: Oil Painting → Juggernaut (textures)
    2: "longcat",       # Wednesday: Cyberpunk → LongCat (complex prompt adherence)
    3: "longcat",       # Thursday: Watercolor → LongCat (complex prompts)
    4: "juggernaut",    # Friday: Art Nouveau → Juggernaut (was: flux_schnell FP8 broken)
    5: "longcat",       # Saturday: Surrealism → LongCat (was: flux_dev FP8 broken)
    6: "juggernaut",    # Sunday: Noir Photography → Juggernaut (was: zimage FP8 broken)
}


def _log(msg):
    print(f"[image_utils] {msg}", flush=True)


def ensure_backend() -> bool:
    """Check SwarmUI is up and has a running backend. Restart if needed."""
    try:
        urllib.request.urlopen(f"{SWARMUI_URL}/", timeout=5)
    except Exception:
        _log("SwarmUI not reachable")
        return False

    try:
        sess_resp = urllib.request.urlopen(
            urllib.request.Request(f"{SWARMUI_URL}/API/GetNewSession",
                                  data=b'{}', headers={"Content-Type": "application/json"}),
            timeout=5)
        sess = json.loads(sess_resp.read())["session_id"]

        backends_resp = urllib.request.urlopen(
            urllib.request.Request(f"{SWARMUI_URL}/API/ListBackends",
                                  data=json.dumps({"session_id": sess}).encode(),
                                  headers={"Content-Type": "application/json"}),
            timeout=5)
        backends = json.loads(backends_resp.read())

        has_running = any(b.get("status") == "running" for b in backends.values())
        if not has_running:
            _log("No running backends — restarting...")
            urllib.request.urlopen(
                urllib.request.Request(f"{SWARMUI_URL}/API/RestartBackends",
                                      data=json.dumps({"session_id": sess}).encode(),
                                      headers={"Content-Type": "application/json"}),
                timeout=10)
            time.sleep(30)
            return True
        return True
    except Exception as e:
        _log(f"Backend check failed: {e}")
        return True  # Still try


def get_model_for_today() -> str:
    """Get the model key for today's day-of-week rotation (Art Corner use)."""
    import datetime
    dow = datetime.datetime.now().weekday()
    return ART_MODEL_ROTATION.get(dow, "flux_dev")


def get_random_model() -> str:
    """Pick a random model from available ones (checks via SwarmUI API)."""
    available = [k for k, v in MODELS.items() if _model_available_via_api(v["file"])]
    return random.choice(available) if available else DEFAULT_MODEL


def _model_available_via_api(model_file: str) -> bool:
    """Check if a model file is available in SwarmUI via the API (works even when /Volumes/Data is TCC-restricted)."""
    try:
        session_resp = urllib.request.urlopen(
            urllib.request.Request(f"{SWARMUI_URL}/API/GetNewSession",
                data=b'{}', headers={"Content-Type": "application/json"}), timeout=5)
        session_id = json.loads(session_resp.read())["session_id"]
        req = urllib.request.Request(
            f"{SWARMUI_URL}/API/ListModels",
            data=json.dumps({"session_id": session_id, "path": "", "depth": 2, "subtype": "Stable-Diffusion"}).encode(),
            headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        files = json.loads(resp.read()).get("files", [])
        available = {f.get("name", "") for f in files}
        return model_file in available
    except Exception:
        # If API unreachable, assume available (generate_image will handle the error)
        return True


def generate_image(prompt: str, width: int = 1024, height: int = 768, steps: int = 12, model: str = None) -> str | None:
    """Generate an image with retry logic. Returns file path or None.

    Args:
        prompt: Image generation prompt
        width: Image width (default 1024)
        height: Image height (default 768)
        steps: Generation steps (default 12, override for quality)
        model: Model key from MODELS dict, or None for default
    """
    if not ensure_backend():
        return None

    # Resolve model file
    model_key = model or DEFAULT_MODEL
    model_info = MODELS.get(model_key, MODELS[DEFAULT_MODEL])
    model_file = model_info["file"]

    # Check if model file exists via SwarmUI API (avoids /Volumes/Data TCC issues)
    if not _model_available_via_api(model_file):
        _log(f"Model {model_file} not found in SwarmUI, falling back to {DEFAULT_MODEL}")
        model_file = MODELS[DEFAULT_MODEL]["file"]

    _log(f"Using model: {model_info['name']} ({model_file}), {steps} steps")

    for attempt in range(MAX_RETRIES):
        try:
            result = subprocess.run(
                [str(GENERATE_IMAGE_SH), prompt, str(width), str(height), str(steps), model_file],
                capture_output=True, text=True, timeout=TIMEOUT,
            )
            if result.returncode == 0 and result.stdout.strip():
                # Parse "Workspace copy: /path/to/file.png" line from output
                image_path = None
                for line in result.stdout.strip().split("\n"):
                    if line.startswith("Workspace copy: "):
                        image_path = line.replace("Workspace copy: ", "").strip()
                        break
                # Fallback: try last non-"Open with" line
                if not image_path:
                    for line in reversed(result.stdout.strip().split("\n")):
                        if not line.startswith("Open with:") and "/" in line:
                            image_path = line.strip()
                            break
                if image_path and Path(image_path).exists():
                    _log(f"Generated (attempt {attempt + 1}): {Path(image_path).name}")
                    return image_path
            _log(f"Attempt {attempt + 1} failed (exit {result.returncode})")
        except subprocess.TimeoutExpired:
            _log(f"Attempt {attempt + 1} timed out ({TIMEOUT}s)")
        except Exception as e:
            _log(f"Attempt {attempt + 1} error: {e}")

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY)

    _log("Image generation failed after all retries")
    return None
