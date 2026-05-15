from __future__ import annotations
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
MAX_RETRIES = 2
RETRY_DELAY = 10
TIMEOUT = 300

# ── OpenRouter Image Models (primary — no PII in prompts) ─────────────────────
# Matched by mood/quality tier. All support text→image generation.
OPENROUTER_MODELS = {
    "fast": {
        "id": "google/gemini-2.5-flash-image",
        "name": "Gemini 2.5 Flash Image",
        "best_for": "thumbnails, covers, quick generation",
        "modalities": ["image", "text"],
    },
    "balanced": {
        "id": "google/gemini-3.1-flash-image-preview",
        "name": "Gemini 3.1 Flash Image",
        "best_for": "daily content, good quality at low cost",
        "modalities": ["image", "text"],
    },
    "quality": {
        "id": "openai/gpt-5-image-mini",
        "name": "GPT-5 Image Mini",
        "best_for": "essays, detailed compositions, prompt adherence",
        "modalities": ["image", "text"],
    },
    "premium": {
        "id": "black-forest-labs/flux.2-pro",
        "name": "FLUX.2 Pro",
        "best_for": "art corner hero pieces, maximum photorealism",
        "modalities": ["image"],
    },
    "cinematic": {
        "id": "google/gemini-3-pro-image-preview",
        "name": "Gemini 3 Pro Image",
        "best_for": "after dark, dreams, dramatic moody scenes",
        "modalities": ["image", "text"],
    },
    "artistic": {
        "id": "recraft/recraft-v4.1-pro",
        "name": "Recraft V4.1 Pro",
        "best_for": "stylized art, illustrations, oil painting, watercolor",
        "modalities": ["image"],
    },
    "flux_fast": {
        "id": "black-forest-labs/flux.2-klein-4b",
        "name": "FLUX.2 Klein 4B",
        "best_for": "fast high-quality generation, versatile",
        "modalities": ["image"],
    },
}

# Map journal sections to preferred OpenRouter model tier
SECTION_MODEL_MAP = {
    "art": "premium",
    "dreams": "cinematic",
    "after-dark": "cinematic",
    "essays": "quality",
    "research": "quality",
    "tech-today": "quality",
    "opinions": "quality",
    "synthesis": "premium",
    "digests": "balanced",
    "default": "quality",
}

# Quality suffix appended to all image prompts for maximum fidelity
IMAGE_QUALITY_SUFFIX = (
    " Ultra-high resolution, 8K UHD, extraordinary detail and depth. "
    "Rich textures, volumetric lighting, ray-traced global illumination. "
    "Professional photography quality, masterful composition, tack-sharp focus. "
    "Cinematic color grading with deep blacks and luminous highlights."
)

OPENROUTER_IMAGE_URL = "https://openrouter.ai/api/v1/chat/completions"

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
        "file": "flux1-schnell.safetensors",
        "name": "FLUX.1 schnell (BF16 — MPS compatible)",
        "best_for": "quality, prompt adherence, fast",
        "optimal_steps": 4,
        "max_steps": 8,
    },
    "flux_dev": {
        "file": "flux1-dev.safetensors",
        "name": "FLUX.1 dev (BF16 — MPS compatible, top quality)",
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
# RESTORED: BF16 FLUX models downloaded 2026-05-10, FP8 models replaced.
ART_MODEL_ROTATION = {
    0: "flux_dev",      # Monday: Photorealism → FLUX.1 dev BF16 (best quality)
    1: "juggernaut",    # Tuesday: Oil Painting → Juggernaut (great textures)
    2: "flux_dev",      # Wednesday: Cyberpunk → FLUX.1 dev BF16 (prompt adherence)
    3: "longcat",       # Thursday: Watercolor → LongCat (complex prompts)
    4: "flux_schnell",  # Friday: Art Nouveau → FLUX.1 schnell BF16 (decorative detail)
    5: "flux_dev",      # Saturday: Surrealism → FLUX.1 dev BF16 (impossible scenes)
    6: "juggernaut",    # Sunday: Noir Photography → Juggernaut (realism, fast)
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


def generate_image(prompt: str, width: int = 1024, height: int = 768, steps: int = 12,
                    model: str = None, section: str = "default") -> str | None:
    """Generate an image. OpenRouter primary, local ComfyUI fallback.

    Args:
        prompt: Image generation prompt (no PII — creative/descriptive only)
        width: Image width (default 1024)
        height: Image height (default 768)
        steps: Generation steps (only used for local fallback)
        model: Local model key from MODELS dict (only for local fallback)
        section: Journal section name for mood-matching ("art", "dreams", "after-dark", etc.)
    """
    # ── Primary: OpenRouter (fast, reliable, no GPU contention) ────────────────
    result = _openrouter_generate(prompt, section)
    if result:
        return result

    # ── Fallback: Local ComfyUI ───────────────────────────────────────────────
    _log("OpenRouter failed — falling back to local ComfyUI...")
    return _local_comfyui_generate(prompt, width, height, steps, model)


def _openrouter_generate(prompt: str, section: str = "default") -> str | None:
    """Generate image via OpenRouter API with mood-matched model selection."""
    import nova_config
    import base64

    try:
        api_key = nova_config.openrouter_api_key()
        if not api_key:
            _log("OpenRouter: no API key available")
            return None

        tier = SECTION_MODEL_MAP.get(section, "balanced")
        model_info = OPENROUTER_MODELS[tier]
        model_id = model_info["id"]
        modalities = model_info.get("modalities", ["image", "text"])
        _log(f"OpenRouter: using {model_info['name']} ({tier} tier) for section={section}")

        enhanced_prompt = prompt.strip() + IMAGE_QUALITY_SUFFIX

        payload = json.dumps({
            "model": model_id,
            "modalities": modalities,
            "messages": [
                {
                    "role": "user",
                    "content": f"Generate an image: {enhanced_prompt}"
                }
            ],
        }).encode()

        req = urllib.request.Request(
            OPENROUTER_IMAGE_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://nova.digitalnoise.net",
                "X-Title": "Nova Journal Art",
            },
        )

        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())

        choices = data.get("choices", [])
        if not choices:
            _log("OpenRouter: no choices in response")
            return None

        message = choices[0].get("message", {})

        # OpenRouter returns images in message.images[] array
        images = message.get("images", [])
        for img in images:
            img_url = ""
            if isinstance(img, dict):
                img_url = img.get("image_url", {}).get("url", "") or img.get("url", "")
            elif isinstance(img, str):
                img_url = img

            if img_url.startswith("data:image"):
                b64 = img_url.split(",", 1)[1]
                output_path = Path.home() / ".openclaw/workspace" / f"or_{int(time.time())}.png"
                output_path.write_bytes(base64.b64decode(b64))
                _log(f"OpenRouter: saved base64 image → {output_path.name}")
                return str(output_path)
            elif img_url.startswith("http"):
                output_path = Path.home() / ".openclaw/workspace" / f"or_{int(time.time())}.png"
                urllib.request.urlretrieve(img_url, str(output_path))
                _log(f"OpenRouter: downloaded image → {output_path.name}")
                return str(output_path)

        # Fallback: check content array (some models use this format)
        content = message.get("content", "")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    img_url = part.get("image_url", {}).get("url", "")
                    if img_url.startswith("data:image"):
                        b64 = img_url.split(",", 1)[1]
                        output_path = Path.home() / ".openclaw/workspace" / f"or_{int(time.time())}.png"
                        output_path.write_bytes(base64.b64decode(b64))
                        _log(f"OpenRouter: saved content image → {output_path.name}")
                        return str(output_path)

        _log(f"OpenRouter: no image found in response (keys: {list(message.keys())})")
        return None

    except Exception as e:
        _log(f"OpenRouter image generation failed: {e}")
        return None


def _local_comfyui_generate(prompt: str, width: int = 1024, height: int = 768,
                             steps: int = 12, model: str = None) -> str | None:
    """Fallback: generate image locally via ComfyUI/SwarmUI."""
    if not ensure_backend():
        _log("Local fallback: SwarmUI not available")
        return None

    if model:
        model_key = model
    else:
        model_key = get_random_model()

    model_info = MODELS.get(model_key, MODELS[DEFAULT_MODEL])
    model_file = model_info["file"]

    if not _model_available_via_api(model_file):
        model_info = MODELS[DEFAULT_MODEL]
        model_file = MODELS[DEFAULT_MODEL]["file"]

    actual_steps = steps if steps != 12 else model_info.get("optimal_steps", steps)
    _log(f"Local fallback: {model_info['name']} ({model_file}), {actual_steps} steps")

    for attempt in range(MAX_RETRIES):
        try:
            result = subprocess.run(
                [str(GENERATE_IMAGE_SH), prompt, str(width), str(height), str(actual_steps), model_file],
                capture_output=True, text=True, timeout=TIMEOUT,
            )
            if result.returncode == 0 and result.stdout.strip():
                image_path = None
                for line in result.stdout.strip().split("\n"):
                    if line.startswith("Workspace copy: "):
                        image_path = line.replace("Workspace copy: ", "").strip()
                        break
                if not image_path:
                    for line in reversed(result.stdout.strip().split("\n")):
                        if not line.startswith("Open with:") and "/" in line:
                            image_path = line.strip()
                            break
                if image_path and Path(image_path).exists():
                    _log(f"Local generated (attempt {attempt + 1}): {Path(image_path).name}")
                    return image_path
            _log(f"Local attempt {attempt + 1} failed (exit {result.returncode})")
        except subprocess.TimeoutExpired:
            _log(f"Local attempt {attempt + 1} timed out ({TIMEOUT}s)")
        except Exception as e:
            _log(f"Local attempt {attempt + 1} error: {e}")

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY)

    _log("Local ComfyUI fallback also failed")
    return None
