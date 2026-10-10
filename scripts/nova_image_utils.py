from __future__ import annotations
"""
nova_image_utils.py — Shared image generation with retry logic, backend health checks, and model rotation.

Used by: nova_daily_essay.py, nova_after_dark.py, nova_research_paper.py,
         nova_art_corner.py, nova_tech_today.py, nova_fix_missing_images.py

Written by Jordan Koch.
"""

import json
import os
import random
import socket
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

GENERATE_IMAGE_SH = Path.home() / ".openclaw/scripts/generate_image.sh"
SWARMUI_URL = "http://192.168.1.6:7801"
MAX_RETRIES = 3     # standing rule: image gen gets 3 tries, 15 s apart, after a backend health check
RETRY_DELAY = 15
TIMEOUT = 600   # 2026-10-01: FLUX on MPS needs more than 300s; covers use the Hyper SDXL model anyway

# ── ComfyUI location (2026-10-06) ─────────────────────────────────────────────
# The Studio's ComfyUI binds 127.0.0.1 + 192.168.1.6 (LAN only, see ~/bin/start-comfyui.sh).
# Hosts where that address is local (the Studio) keep the generate_image.sh path; every
# other host (nova-core .2, standby .5) talks to it over the LAN with the Python client below.
# Override order: env NOVA_COMFYUI_URL > service_config(image_gen, comfyui).url > default.
COMFYUI_DEFAULT_URL = "http://192.168.1.6:8188"
REMOTE_GEN_TIMEOUT = 300        # per image once executing: FLUX dev ~57 s warm; a COLD load (~14 min
                                # after a ComfyUI restart) overruns it -> that one cover goes to OpenRouter
REMOTE_HEALTH_TIMEOUT = 3       # /system_stats probe before committing to the LAN path
REMOTE_POLL_S = 3
REMOTE_MAX_POLL_ERRORS = 5      # consecutive /history failures => ComfyUI went away
import nova_dsn as _nova_dsn  # noqa: E402
_PG_DSN = _nova_dsn.pg_dsn("nova_ops", "connect_timeout=3")
_comfyui_url_cache = None

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
# NOTE: FP8 models (flux1-dev-fp8, flux1-schnell-fp8, ZImage FP8Mix) are BROKEN on macOS MPS
# (Float8_e4m3fn unsupported). The BF16 FLUX.1 dev/schnell files are present under
# SwarmUI/Models/Stable-Diffusion (symlinked into Models/unet), the t5xxl_fp16 + clip_l encoders
# live in Models/clip and Flux/ae.safetensors in Models/VAE — downloaded 2026-10-03.
# generate_image.sh builds the FLUX graph (UNET + DualCLIP + FluxGuidance, cfg 1) for flux* models.
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
    "flux_schnell": {
        "file": "flux1-schnell.safetensors",
        "name": "FLUX.1 schnell (BF16 — MPS compatible)",
        "best_for": "quality, prompt adherence, fast",
        "optimal_steps": 4,
        "max_steps": 8,
        "requires_t5": True,
    },
    "flux_dev": {
        "file": "flux1-dev.safetensors",
        "name": "FLUX.1 dev (BF16 — MPS compatible, top quality)",
        "best_for": "top quality, best prompt adherence",
        "optimal_steps": 20,
        "max_steps": 50,
        "requires_t5": True,
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
DEFAULT_MODEL = "flux_dev"   # 2026-10-03: FLUX.1 dev BF16, ~60s per 1024x768 on the M3 Ultra; Juggernaut covers looked like game renders

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


# ── Image appearance safety policy ───────────────────────────────────────────
# People in Nova's imagery must read as unambiguously adult and fully clothed. AI-made
# images of youthful/under-dressed figures land too close to a line we never want to be
# near, regardless of intent. Enforced centrally in generate_image() (2026-09-09).
import re as _re

# Words that push the model toward a youthful or under-dressed subject. Rewritten to
# adult/clothed so the sentence still reads naturally: "a young"->"an adult", etc.
_SAFETY_SUBS = [
    (r"\ban?\s+young\b", "an adult"),
    (r"\byoung\b", "adult"),
    (r"\byouthful\b", "mature adult"),
    (r"\blike a kid\b", "like someone"),
    (r"\bkids?\b", "adults"),
    (r"\bchild(ish|like|ren)?\b", "adult"),
    (r"\bteen(age[rd]?|ager)?s?\b", "adult"),
    (r"\bgirls?\b", "woman"),
    (r"\bboys?\b", "man"),
    (r"\blittle\s+", ""),
    (r"\b(nude|naked|topless|shirtless|lingerie|underwear|bikini)\b", "fully clothed"),
]

# Sentinel phrase used to detect prior application (idempotency) AND as the policy clause.
# PHRASED POSITIVELY on purpose: image models (esp. diffusion) follow "is a clothed adult"
# far more reliably than "no children/nudity", where the risky nouns can leak into output.
_SAFETY_SENTINEL = "a mature, fully-clothed adult"
_SAFETY_SUFFIX = (
    " Any person shown is " + _SAFETY_SENTINEL + " in their thirties or older, dressed "
    "in modest high-neck clothing; the overall image is wholesome, tasteful, and entirely "
    "non-sexual. Do not add people who are not described."
)


def apply_image_safety(prompt: str) -> str:
    """Rewrite youth/undress cues to adult/clothed and append the positive policy clause.

    Idempotent: the guard runs FIRST, so a re-applied prompt is returned untouched (a
    second substitution pass would otherwise mangle the clause's own words). Harmless for
    people-free prompts. This is the single chokepoint every Nova image passes through.
    """
    if not prompt:
        return prompt
    if _SAFETY_SENTINEL in prompt:
        return prompt  # already processed
    cleaned = prompt
    for pat, repl in _SAFETY_SUBS:
        cleaned = _re.sub(pat, repl, cleaned, flags=_re.IGNORECASE)
    cleaned = _re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned.rstrip() + _SAFETY_SUFFIX


def generate_image(prompt: str, width: int = 1024, height: int = 768, steps: int = 12,
                    model: str = None, section: str = "default") -> str | None:
    """Generate an image. Local ComfyUI (SwarmUI) primary, OpenRouter fallback.

    Args:
        prompt: Image generation prompt (no PII — creative/descriptive only)
        width: Image width (default 1024)
        height: Image height (default 768)
        steps: Generation steps (only used for local fallback)
        model: Local model key from MODELS dict (only for local fallback)
        section: Journal section name for mood-matching ("art", "dreams", "after-dark", etc.)
    """
    # ── SAFETY: every generated image goes through here, so enforce the appearance
    # policy centrally rather than trusting ~40 individual prompt strings. Harmless for
    # people-free scenes (server rooms, landscapes); decisive when a person is depicted.
    prompt = apply_image_safety(prompt)

    # ── Primary: SwarmUI/ComfyUI on the Studio's GPU (2026-10-01, Jordan: the 80-core
    # M3 Ultra is idle and the cloud path ran dry on 2026-07-17; local keeps the images
    # private and free). On the Studio itself that is generate_image.sh; on nova-core and
    # the standby (2026-10-06) the same workflow is queued over the LAN. OpenRouter is
    # the fallback only when ComfyUI is unreachable or errors, never because it is busy.
    t0 = time.monotonic()
    base = comfyui_url()
    if comfyui_is_local(base):
        backend = "comfyui-local"
        result = _local_comfyui_generate(prompt, width, height, steps, model, section)
    else:
        backend = "comfyui-remote"
        result = _remote_comfyui_generate(prompt, width, height, steps, model, section, base)
    if result:
        _log(f"backend={backend} ({base}) produced {Path(result).name} in {time.monotonic() - t0:.0f}s")
        return result

    _log(f"{backend} failed — falling back to OpenRouter...")
    result = _openrouter_generate(prompt, section)
    if result:
        _log(f"backend=openrouter produced {Path(result).name} in {time.monotonic() - t0:.0f}s")
    else:
        _log(f"no backend produced an image ({time.monotonic() - t0:.0f}s)")
    return result


def comfyui_url() -> str:
    """ComfyUI base URL: env NOVA_COMFYUI_URL, else service_config(image_gen, comfyui).url,
    else the Studio's LAN address. The PG lookup is done once per process and fails soft."""
    global _comfyui_url_cache
    env = os.environ.get("NOVA_COMFYUI_URL", "").strip()
    if env:
        return env.rstrip("/")
    if _comfyui_url_cache is None:
        url = None
        try:
            import psycopg2
            conn = psycopg2.connect(_PG_DSN)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM service_config WHERE service = %s AND key = %s",
                                ("image_gen", "comfyui"))
                    row = cur.fetchone()
            finally:
                conn.close()
            if row and isinstance(row[0], dict):
                url = row[0].get("url")
        except Exception as e:
            _log(f"service_config lookup failed ({e}); using {COMFYUI_DEFAULT_URL}")
        _comfyui_url_cache = (url or COMFYUI_DEFAULT_URL).rstrip("/")
    return _comfyui_url_cache


def comfyui_is_local(base: str) -> bool:
    """True when the ComfyUI host is an address of THIS machine (i.e. we are the Studio).
    Binding a throwaway socket succeeds only for a locally-assigned address."""
    host = urllib.parse.urlparse(base).hostname or ""
    if not host:
        return False                     # malformed URL: bind(("", 0)) would "succeed"
    if host in ("127.0.0.1", "localhost", "::1"):
        return True
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
        return True
    except (OSError, UnicodeError):
        return False


def comfy_workflow(prompt: str, model_file: str, width: int, height: int, steps: int,
                   seed: int | None = None) -> dict:
    """The ComfyUI API graph for one image. Single source for generate_image.sh (Studio)
    and the LAN client (nova-core). FLUX.1 (BF16) gets UNET + DualCLIP + FluxGuidance at
    cfg 1 (2026-10-03 fix for the SDXL "video-game render" covers); everything else is a
    plain SDXL checkpoint graph with the safety/darkness negative prompt."""
    seed = int(time.time()) % 2**31 if seed is None else seed
    prefix = datetime.now().strftime("%H%M")
    if model_file.startswith("flux"):
        return {
            "4": {"class_type": "UNETLoader", "inputs": {"unet_name": model_file, "weight_dtype": "default"}},
            "11": {"class_type": "DualCLIPLoader", "inputs": {"clip_name1": "t5xxl_fp16.safetensors", "clip_name2": "clip_l.safetensors", "type": "flux"}},
            "12": {"class_type": "VAELoader", "inputs": {"vae_name": "Flux/ae.safetensors"}},
            "5": {"class_type": "EmptySD3LatentImage", "inputs": {"width": int(width), "height": int(height), "batch_size": 1}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["11", 0]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["11", 0]}},
            "13": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["6", 0], "guidance": 3.5}},
            "8": {"class_type": "KSampler", "inputs": {
                "model": ["4", 0], "positive": ["13", 0], "negative": ["7", 0], "latent_image": ["5", 0],
                "seed": seed, "steps": int(steps), "cfg": 1.0,
                "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
            "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["12", 0]}},
            "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": prefix}},
        }
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model_file}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": int(width), "height": int(height), "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": _SDXL_NEGATIVE, "clip": ["4", 1]}},
        "8": {"class_type": "KSampler", "inputs": {
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0],
            "seed": seed, "steps": int(steps), "cfg": 7.0,
            "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["4", 2]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": prefix}},
    }


_SDXL_NEGATIVE = ("blurry, low quality, distorted, watermark, text, logo, nudity, nude, nsfw, explicit, "
                  "nipples, sexual content, bare skin, revealing clothing, dark, underexposed, too dark, "
                  "black image, nearly black, dim, murky, low light, pitch black, unlit")


def _pick_model(model: str | None, section: str, steps: int) -> tuple[dict, str, int]:
    """(model_info, model_file, steps) — shared by the Studio and LAN paths."""
    if model:
        model_key = model
    elif section == "art":
        model_key = get_random_model()          # Art Corner keeps its rotation (FLUX etc.)
    else:
        model_key = DEFAULT_MODEL               # covers: FLUX.1 dev (2026-10-03)

    model_info = MODELS.get(model_key, MODELS[DEFAULT_MODEL])
    model_file = model_info["file"]

    if not _model_available_via_api(model_file):
        model_info = MODELS[DEFAULT_MODEL]
        model_file = MODELS[DEFAULT_MODEL]["file"]

    actual_steps = steps if steps != 12 else model_info.get("optimal_steps", steps)
    return model_info, model_file, actual_steps


def _comfy_json(url: str, data: dict | None = None, timeout: float = 10):
    req = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body else None


def _comfy_withdraw(base: str, prompt_id: str, running: bool) -> None:
    """On timeout: drop our job from the queue if it never started, so a cover we've
    given up on doesn't later occupy the Studio GPU. A job that is already RUNNING is
    left alone on purpose: ComfyUI only honours /interrupt between sampler steps, so an
    interrupt during a cold FLUX load (measured 2026-10-06: ~14 min from /Volumes/Data)
    saves nothing and throws the load away — letting it finish leaves the model warm
    (~57 s per cover) for the next caller."""
    if running:
        _log(f"Remote ComfyUI: leaving running job {prompt_id} to finish (keeps the model warm)")
        return
    try:
        _comfy_json(f"{base}/queue", {"delete": [prompt_id]}, timeout=5)
    except Exception as e:
        _log(f"Remote ComfyUI: queue delete of {prompt_id} failed: {e}")


def _remote_comfyui_generate(prompt: str, width: int = 1024, height: int = 768, steps: int = 12,
                             model: str = None, section: str = "default",
                             base: str | None = None) -> str | None:
    """Queue one image on the Studio's ComfyUI over the LAN and wait for it.

    Queue-aware: ComfyUI runs one job at a time, so a busy queue is waited out (within the
    caller's TIMEOUT budget) instead of being treated as a failure. Once our job starts
    executing it gets min(REMOTE_GEN_TIMEOUT, TIMEOUT). Single submission, no resubmits —
    retrying a slow job would only flood the Studio's queue. Returns None (=> OpenRouter)
    when ComfyUI is unreachable, rejects the graph, errors, or the budget runs out."""
    base = (base or comfyui_url()).rstrip("/")
    try:
        _comfy_json(f"{base}/system_stats", timeout=REMOTE_HEALTH_TIMEOUT)
    except Exception as e:
        _log(f"Remote ComfyUI {base} unreachable: {e}")
        return None

    model_info, model_file, actual_steps = _pick_model(model, section, steps)
    try:
        q = _comfy_json(f"{base}/queue", timeout=5) or {}
        ahead = len(q.get("queue_running", [])) + len(q.get("queue_pending", []))
    except Exception:
        ahead = "?"
    budget = TIMEOUT
    gen_cap = min(REMOTE_GEN_TIMEOUT, TIMEOUT)
    _log(f"Remote ComfyUI {base}: {model_info['name']} ({model_file}), {actual_steps} steps, "
         f"{ahead} job(s) ahead, budget {budget}s (gen cap {gen_cap}s)")

    t0 = time.monotonic()
    try:
        resp = _comfy_json(f"{base}/prompt",
                           {"prompt": comfy_workflow(prompt, model_file, width, height, actual_steps),
                            "client_id": str(uuid.uuid4())}, timeout=15) or {}
    except Exception as e:
        detail = ""
        try:
            if hasattr(e, "read"):
                detail = f" — {e.read()[:300]}"
        except Exception:
            pass
        _log(f"Remote ComfyUI submit failed: {e}{detail}")
        return None
    prompt_id = resp.get("prompt_id")
    if not prompt_id:
        _log(f"Remote ComfyUI: no prompt_id returned ({str(resp)[:200]})")
        return None

    started = None
    errors = 0
    while True:
        now = time.monotonic()
        if now - t0 > budget or (started is not None and now - started > gen_cap):
            phase = "generating" if started is not None else "queued"
            _log(f"Remote ComfyUI: timed out after {now - t0:.0f}s ({phase}), job {prompt_id}")
            _comfy_withdraw(base, prompt_id, running=started is not None)
            return None
        time.sleep(REMOTE_POLL_S)
        try:
            job = (_comfy_json(f"{base}/history/{prompt_id}", timeout=5) or {}).get(prompt_id)
            if job:
                status = job.get("status", {})
                state = status.get("status_str")
                if state == "success" and status.get("completed"):
                    return _comfy_download(base, job, started, t0)
                if state in ("error", "failed"):
                    msgs = [m[1].get("exception_message", "unknown error")
                            for m in status.get("messages", [])
                            if isinstance(m, list) and len(m) > 1 and m[0] == "execution_error"
                            and isinstance(m[1], dict)]
                    _log(f"Remote ComfyUI job {prompt_id} failed: {'; '.join(msgs) or state}")
                    return None
            elif started is None:
                q = _comfy_json(f"{base}/queue", timeout=5) or {}
                if any(len(it) > 1 and it[1] == prompt_id for it in q.get("queue_running", [])):
                    started = time.monotonic()
                    _log(f"Remote ComfyUI: job started after {started - t0:.0f}s in queue")
            errors = 0
        except Exception as e:
            errors += 1
            if errors >= REMOTE_MAX_POLL_ERRORS:
                _log(f"Remote ComfyUI: lost contact ({e}) after {errors} polls")
                return None


def _comfy_download(base: str, job: dict, started, t0: float) -> str | None:
    for output in job.get("outputs", {}).values():
        for img in output.get("images", []):
            fname = img["filename"]
            qs = urllib.parse.urlencode({"filename": fname, "subfolder": img.get("subfolder", ""),
                                         "type": img.get("type", "output")})
            dest = Path.home() / ".openclaw/workspace" / f"comfy_{int(time.time())}_{Path(fname).name}"
            try:
                with urllib.request.urlopen(f"{base}/view?{qs}", timeout=30) as r:
                    data = r.read()
            except Exception as e:
                _log(f"Remote ComfyUI: download of {fname} failed: {e}")
                return None
            if not data:
                _log(f"Remote ComfyUI: empty image {fname}")
                return None
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            gen = f", gen {time.monotonic() - started:.0f}s" if started is not None else ""
            _log(f"Remote ComfyUI generated {dest.name} (total {time.monotonic() - t0:.0f}s{gen})")
            return str(dest)
    _log("Remote ComfyUI: job succeeded but produced no image")
    return None


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
            # Cap the token reservation: without max_tokens OpenRouter reserves
            # ~59K tokens of headroom per image request and 402s whenever the
            # credit balance dips below that, even though an image only needs
            # ~8K. Found 2026-09-14 after a day of coverless articles.
            "max_tokens": 8000,
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
        # Surface the HTTP body — a bare "HTTP Error 402" hides the actual
        # remedy (credit balance) and cost a day of debugging by log-reading.
        # Duck-typed: HTTPError has .read(). (Importing urllib.error here made
        # `urllib` function-local and broke the try block — UnboundLocalError.)
        detail = ""
        try:
            if hasattr(e, "read"):
                detail = f" — {e.read()[:200]}"
        except Exception:
            pass
        _log(f"OpenRouter image generation failed: {e}{detail}")
        return None


def _local_comfyui_generate(prompt: str, width: int = 1024, height: int = 768,
                             steps: int = 12, model: str = None, section: str = "default") -> str | None:
    """Studio path: generate via generate_image.sh against the local ComfyUI/SwarmUI."""
    if not ensure_backend():
        _log("Local fallback: SwarmUI not available")
        return None

    model_info, model_file, actual_steps = _pick_model(model, section, steps)
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
