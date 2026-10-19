from __future__ import annotations
"""
nova_image_utils.py — Shared image generation with retry logic, backend health checks, and model rotation.

LOCAL ONLY (2026-10-19, Jordan: "move away from cloud based images"): every Nova image is
rendered by ComfyUI on the Studio's M3 Ultra (FLUX.1 dev BF16). There is NO cloud image
fallback any more — a busy ComfyUI is queued behind and waited out, an unreachable one is
retried with backoff, and if it is truly down generate_image() returns None: the article
publishes without a cover and nova_fix_missing_images.py backfills it locally later.

Used by: nova_daily_essay.py, nova_after_dark.py, nova_research_paper.py,
         nova_art_corner.py, nova_tech_today.py, nova_fix_missing_images.py

Written by Jordan Koch.
"""

import json
import os
import random
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

GENERATE_IMAGE_SH = Path.home() / ".openclaw/scripts/generate_image.sh"
SWARMUI_URL = "http://192.168.1.6:7801"
MAX_RETRIES = 3     # attempts when ComfyUI is unreachable / errors (backoff 15 s, 30 s); never resubmits a slow job
RETRY_DELAY = 15
TIMEOUT = 1200      # max seconds generate_image() waits in total (queue + render) — a busy queue is waited
                    # out, not failed over. Also capped by the calling scheduler task's remaining time.
                    # Callers may lower it (monthly_wrap / weekly_summary set 240).
TASK_RESERVE_S = 180    # leave this much of the scheduler task's timeout for publish + git push
MIN_ATTEMPT_S = 60      # don't start an attempt with less budget than this (FLUX dev warm ~60-110 s)

# ── ComfyUI location (2026-10-06) ─────────────────────────────────────────────
# The Studio's ComfyUI binds 127.0.0.1 + 192.168.1.6 (LAN only, see ~/bin/start-comfyui.sh).
# Every host — the Studio included (2026-10-19) — talks to it with the queue-aware Python
# client below; generate_image.sh stays as Nova's command-line tool and for the dream/essay
# scripts that shell out to it.
# Override order: env NOVA_COMFYUI_URL > service_config(image_gen, comfyui).url > default.
COMFYUI_DEFAULT_URL = "http://192.168.1.6:8188"
COMFYUI_LAUNCHD_LABEL = "com.jordankoch.comfyui"   # Studio LaunchAgent; soft-kicked when unreachable
REMOTE_GEN_TIMEOUT = 900        # per image once executing: FLUX dev ~60-110 s warm; a COLD load after a
                                # ComfyUI restart is ~14 min from /Volumes/Data and must be allowed to finish
REMOTE_HEALTH_TIMEOUT = 3       # /system_stats probe before committing to the LAN path
REMOTE_POLL_S = 3
REMOTE_MAX_POLL_ERRORS = 5      # consecutive /history failures => ComfyUI went away
import nova_dsn as _nova_dsn  # noqa: E402
_PG_DSN = _nova_dsn.pg_dsn("nova_ops", "connect_timeout=3")
_comfyui_url_cache = None

# Section -> legacy cloud tier names. NOT used for generation since 2026-10-19 (local only);
# kept because other scripts/tests import it. Every cover renders on FLUX.1 dev.
SECTION_MODEL_MAP = {
    "art": "premium",
    "after-dark": "cinematic",
    "pilot": "cinematic",
    "burbank-ops": "quality",
    "watches": "quality",
    "security": "quality",
    "research": "quality",
    "tech": "quality",
    "cars": "quality",
    "food": "quality",
    "opinions": "quality",
    # retired names still passed by older callers (nova_sections.SECTION_REDIRECTS)
    "operations": "quality", "local": "quality", "essays": "quality", "synthesis": "premium",
    "digests": "balanced",
    "default": "quality",
}

# ── Local quality settings (2026-10-19) ──────────────────────────────────────
# Covers render on FLUX.1 dev BF16 at >= ~1.2 MP (1280x960 for the 4:3 default), 24 steps,
# FluxGuidance 3.5, euler/simple. Benchmarked on the M3 Ultra: 1024x768@20 ~55-70 s,
# 1280x960@24 ~110 s warm. Publish resizes to 1200 px wide, so a native 1280 render beats an
# upscaler pass (none installed) and costs ~1 GPU-hour/day at 30-40 covers.
COVER_WIDTH = 1280
COVER_HEIGHT = 960
FLUX_MIN_PIXELS = 1280 * 960    # FLUX requests smaller than this are scaled up (aspect kept, /64)
FLUX_GUIDANCE = 3.5
# Style-neutral fidelity suffix for FLUX (T5 follows plain descriptive language well).
IMAGE_QUALITY_SUFFIX = (" Masterful composition, intricate fine detail, tack-sharp focus, rich natural "
                        "color and lighting, high dynamic range. No text, no lettering, no watermark.")

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
        "optimal_steps": 24,
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
    """Is model_file loadable by the ComfyUI we render on? Asks ComfyUI's /models listing
    (diffusion_models + checkpoints; cached per process). SwarmUI is no longer consulted —
    ComfyUI runs standalone (com.jordankoch.comfyui) and a SwarmUI outage must not block it.
    Unreachable -> assume available (generation reports the real error)."""
    global _comfy_models_cache
    if _comfy_models_cache is None:
        base = comfyui_url()
        names = set()
        try:
            for folder in ("diffusion_models", "checkpoints"):
                names.update(_comfy_json(f"{base}/models/{folder}", timeout=5) or [])
        except Exception:
            return True
        _comfy_models_cache = names
    return model_file in _comfy_models_cache


_comfy_models_cache = None


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


def generate_image(prompt: str, width: int = COVER_WIDTH, height: int = COVER_HEIGHT, steps: int = 12,
                    model: str = None, section: str = "default") -> str | None:
    """Generate an image on the Studio's ComfyUI. LOCAL ONLY — no cloud fallback.

    Args:
        prompt: Image generation prompt (no PII — creative/descriptive only)
        width/height: requested size (FLUX renders are scaled up to >= FLUX_MIN_PIXELS)
        steps: sampler steps; the default 12 means "the model's optimal" (FLUX dev: 24)
        model: model key from MODELS (None = FLUX.1 dev; Art Corner rotates)
        section: journal section (logging; "art" selects the rotation)

    A busy ComfyUI is queued behind (single submission, waited out within the budget);
    unreachable / errored attempts are retried with backoff. Returns the PNG path, or None
    when no image could be made in time — the caller publishes without a cover and
    nova_fix_missing_images.py backfills it locally.
    """
    # ── SAFETY: every generated image goes through here, so enforce the appearance
    # policy centrally rather than trusting ~40 individual prompt strings. Harmless for
    # people-free scenes (server rooms, landscapes); decisive when a person is depicted.
    if prompt and IMAGE_QUALITY_SUFFIX.strip() not in prompt:
        prompt = prompt.rstrip() + IMAGE_QUALITY_SUFFIX
    prompt = apply_image_safety(prompt)

    t0 = time.monotonic()
    base = comfyui_url()
    deadline = t0 + _wait_budget()
    for attempt in range(1, MAX_RETRIES + 1):
        left = deadline - time.monotonic()
        if left < MIN_ATTEMPT_S:
            _log(f"image budget exhausted ({left:.0f}s left) before attempt {attempt}")
            break
        result = _comfyui_generate(prompt, width, height, steps, model, section, base, budget=left)
        if result:
            _log(f"backend=comfyui ({base}) produced {Path(result).name} in "
                 f"{time.monotonic() - t0:.0f}s (attempt {attempt})")
            return result
        if attempt < MAX_RETRIES:
            delay = min(RETRY_DELAY * 2 ** (attempt - 1), max(0.0, deadline - time.monotonic() - MIN_ATTEMPT_S))
            if delay <= 0:
                break
            _log(f"attempt {attempt} produced no image — retrying in {delay:.0f}s")
            time.sleep(delay)
    _log(f"no local image after {time.monotonic() - t0:.0f}s — publish without a cover; "
         f"nova_fix_missing_images.py will backfill it (no cloud fallback)")
    return None


def _wait_budget() -> float:
    """Seconds generate_image() may spend: TIMEOUT, capped by the scheduler task's remaining
    time (via nova_journal._task_time_left when the caller imported nova_journal) minus
    TASK_RESERVE_S, so waiting on a busy GPU never gets the whole article killed."""
    budget = float(TIMEOUT)
    nj = sys.modules.get("nova_journal")
    if nj is not None and hasattr(nj, "_task_time_left"):
        try:
            left = nj._task_time_left()
            if left is not None:
                budget = min(budget, left - TASK_RESERVE_S)
        except Exception:
            pass
    return max(0.0, budget)


def _flux_size(width: int, height: int) -> tuple[int, int]:
    """Scale a FLUX request up to >= FLUX_MIN_PIXELS (aspect kept, multiples of 64)."""
    w, h = int(width), int(height)
    if w * h >= FLUX_MIN_PIXELS:
        return w, h
    k = (FLUX_MIN_PIXELS / float(w * h)) ** 0.5
    return int(round(w * k / 64.0)) * 64, int(round(h * k / 64.0)) * 64


def _kick_local_comfyui(base: str) -> None:
    """Studio only: ask launchd to start ComfyUI if it isn't running (no-op when it is)."""
    if sys.platform != "darwin" or not comfyui_is_local(base):
        return
    try:
        subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{COMFYUI_LAUNCHD_LABEL}"],
                       capture_output=True, timeout=10)
        _log(f"ComfyUI unreachable — kickstarted {COMFYUI_LAUNCHD_LABEL}")
    except Exception as e:
        _log(f"ComfyUI kickstart failed: {e}")


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
            "13": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["6", 0], "guidance": FLUX_GUIDANCE}},
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
        _log(f"ComfyUI: leaving running job {prompt_id} to finish (keeps the model warm)")
        return
    try:
        _comfy_json(f"{base}/queue", {"delete": [prompt_id]}, timeout=5)
    except Exception as e:
        _log(f"ComfyUI: queue delete of {prompt_id} failed: {e}")


def _comfyui_generate(prompt: str, width: int = COVER_WIDTH, height: int = COVER_HEIGHT, steps: int = 12,
                      model: str = None, section: str = "default",
                      base: str | None = None, budget: float | None = None) -> str | None:
    """Queue one image on the Studio's ComfyUI (local or over the LAN) and wait for it.

    Queue-aware: ComfyUI runs one job at a time, so a busy queue is waited out (within
    `budget`, default TIMEOUT) instead of being treated as a failure. Once our job starts
    executing it gets min(REMOTE_GEN_TIMEOUT, budget). Single submission, no resubmits —
    retrying a slow job would only flood the Studio's queue. Returns None when ComfyUI is
    unreachable, rejects the graph, errors, or the budget runs out (generate_image retries
    the first three with backoff)."""
    base = (base or comfyui_url()).rstrip("/")
    try:
        _comfy_json(f"{base}/system_stats", timeout=REMOTE_HEALTH_TIMEOUT)
    except Exception as e:
        _log(f"ComfyUI {base} unreachable: {e}")
        _kick_local_comfyui(base)
        return None

    model_info, model_file, actual_steps = _pick_model(model, section, steps)
    if model_file.startswith("flux"):
        width, height = _flux_size(width, height)
    try:
        q = _comfy_json(f"{base}/queue", timeout=5) or {}
        ahead = len(q.get("queue_running", [])) + len(q.get("queue_pending", []))
    except Exception:
        ahead = "?"
    budget = float(TIMEOUT if budget is None else budget)
    gen_cap = min(REMOTE_GEN_TIMEOUT, budget)
    _log(f"ComfyUI {base}: {model_info['name']} ({model_file}), {width}x{height}, {actual_steps} steps, "
         f"{ahead} job(s) ahead, budget {budget:.0f}s (gen cap {gen_cap:.0f}s)")

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
        _log(f"ComfyUI submit failed: {e}{detail}")
        return None
    prompt_id = resp.get("prompt_id")
    if not prompt_id:
        _log(f"ComfyUI: no prompt_id returned ({str(resp)[:200]})")
        return None

    started = None
    errors = 0
    while True:
        now = time.monotonic()
        if now - t0 > budget or (started is not None and now - started > gen_cap):
            phase = "generating" if started is not None else "queued"
            _log(f"ComfyUI: timed out after {now - t0:.0f}s ({phase}), job {prompt_id}")
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
                    _log(f"ComfyUI job {prompt_id} failed: {'; '.join(msgs) or state}")
                    return None
            elif started is None:
                q = _comfy_json(f"{base}/queue", timeout=5) or {}
                if any(len(it) > 1 and it[1] == prompt_id for it in q.get("queue_running", [])):
                    started = time.monotonic()
                    _log(f"ComfyUI: job started after {started - t0:.0f}s in queue")
            errors = 0
        except Exception as e:
            errors += 1
            if errors >= REMOTE_MAX_POLL_ERRORS:
                _log(f"ComfyUI: lost contact ({e}) after {errors} polls")
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
                _log(f"ComfyUI: download of {fname} failed: {e}")
                return None
            if not data:
                _log(f"ComfyUI: empty image {fname}")
                return None
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            gen = f", gen {time.monotonic() - started:.0f}s" if started is not None else ""
            _log(f"ComfyUI generated {dest.name} (total {time.monotonic() - t0:.0f}s{gen})")
            return str(dest)
    _log("ComfyUI: job succeeded but produced no image")
    return None
