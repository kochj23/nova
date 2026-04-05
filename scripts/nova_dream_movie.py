#!/usr/bin/env python3
"""
nova_dream_movie.py — Generate a narrative-driven dream movie from Nova's journal.

Pipeline:
  1. LLM reads the dream text and extracts 5-7 cinematic scenes with visual
     descriptions, emotional tone, camera direction, and pacing
  2. SwarmUI generates a keyframe image per scene (consistent style)
  3. ffmpeg applies Ken Burns camera moves chosen per scene (not random)
  4. Scenes are assembled with cross-dissolves into a single movie

Upgrade path (when t5xxl_fp8_e4m3fn.safetensors is downloaded to
  /Volumes/Data/AI/SwarmUI/dlbackend/ComfyUI/models/text_encoders/):
  Each keyframe gets fed into LTX-Video image-to-video for actual frame
  animation — same pipeline, just replace the ffmpeg zoompan step.

Written by Jordan Koch.
"""

import json
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SWARMUI_URL  = "http://localhost:7801"
COMFYUI_LTX  = "http://localhost:7824"
LTX_ENCODER  = Path("/Volumes/Data/AI/SwarmUI/dlbackend/ComfyUI/models/text_encoders/t5xxl_fp8_e4m3fn.safetensors")
WORKSPACE    = Path.home() / ".openclaw/workspace"
MOVIE_DIR    = WORKSPACE / "dream_videos"
MOVIE_DIR.mkdir(parents=True, exist_ok=True)
SLACK_TOKEN   = nova_config.slack_bot_token()
SLACK_CHANNEL = "C0AMNQ5GX70"

# Style prefix applied to every scene for visual consistency
DREAM_STYLE = (
    "dreamlike surreal digital painting, deep navy and indigo palette, "
    "soft amber light, painterly brushwork, cinematic composition, "
    "ethereal atmosphere, no text, no watermark"
)

# Camera move → ffmpeg zoompan expression
# (zoom_expr, x_expr, y_expr, description)
CAMERA_MOVES = {
    "push_in":     ("if(lte(on,1),1.0,min(zoom+0.0015,1.4))", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)", "slow push toward center"),
    "pull_back":   ("if(lte(on,1),1.4,max(zoom-0.0015,1.0))", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)", "pull away to reveal"),
    "pan_right":   ("1.1", "if(lte(on,1),0,x+0.3)",           "ih/2-(ih/zoom/2)", "drift right across scene"),
    "pan_left":    ("1.1", "if(lte(on,1),iw*0.1,max(x-0.3,0))", "ih/2-(ih/zoom/2)", "drift left across scene"),
    "drift_up":    ("1.1", "iw/2-(iw/zoom/2)",                 "if(lte(on,1),ih*0.1,max(y-0.2,0))", "slow drift upward"),
    "drift_down":  ("1.1", "iw/2-(iw/zoom/2)",                 "if(lte(on,1),0,min(y+0.2,ih*0.1))", "slow drift downward"),
    "static":      ("1.05", "iw/2-(iw/zoom/2)",                "ih/2-(ih/zoom/2)", "locked frame, slight breathe"),
    "vertiginous": ("if(lte(on,1),1.5,max(zoom-0.003,1.0))",   "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)", "pull back fast — disorienting"),
}


def log(msg: str):
    print(f"[nova_dream_movie {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Scene extraction ──────────────────────────────────────────────────────────

SCENE_PROMPT = """You are a film director. Read this dream and extract exactly 7 scenes that tell its story in sequence — like a short film explaining what happened.

Each scene must cover a specific narrative beat from the dream, in order. Together they should give a viewer who hasn't read the dream a clear sense of what happened and how it felt.

Output ONLY this JSON structure. No markdown. No explanation. No preamble. Start your response with the opening brace.

{{"scenes":[{{"visual":"detailed painting description — specific objects, lighting, space, color","camera":"push_in","title":"short scene title","mood":"one word"}},{{"visual":"...","camera":"pull_back","title":"...","mood":"..."}}]}}

Camera:
push_in=intimacy/dread/revelation  pull_back=reveal/isolation/waking  pan_right=movement/pursuit  pan_left=drifting/searching  drift_up=ascent/escape  drift_down=descent/falling  static=stillness/dread  vertiginous=time fold/disorientation

The dream:
{dream_text}"""


def extract_scenes(dream_text: str) -> list[dict]:
    """Call the LLM to extract narrative scenes from the dream text."""
    try:
        from nova_intent_router import route
        result = route(
            intent="dream_journal",
            prompt=SCENE_PROMPT.format(dream_text=dream_text[:2000]),
            system="You are a film director. Output ONLY the JSON. No markdown. No explanation.",
        )
        if not result.get("success"):
            log(f"LLM failed: {result.get('error')} — using fallback")
            return _fallback_scenes(dream_text)

        response = result["response"].strip()

        # Strip <think>...</think> blocks (Qwen3 chain-of-thought)
        response = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
        # Strip markdown code fences
        response = re.sub(r"```(?:json)?\s*", "", response)
        response = re.sub(r"```\s*", "", response).strip()
        # Strip any preamble before the first {
        brace = response.find("{")
        if brace > 0:
            response = response[brace:]

        # Try to parse JSON — find the outermost { ... } block
        for pattern in [
            r'\{"scenes"\s*:\s*\[.*?\]\s*\}',   # tight match
            r'\{.*?"scenes"\s*:\s*\[.*?\]\s*\}', # with other keys
            r'\{.*\}',                            # anything
        ]:
            match = re.search(pattern, response, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                    scenes = data.get("scenes", [])
                    if scenes and len(scenes) >= 3:
                        log(f"Extracted {len(scenes)} scenes from dream")
                        return scenes[:8]
                except json.JSONDecodeError:
                    continue

        log(f"Could not parse scenes from LLM response — using fallback")
        return _fallback_scenes(dream_text)

    except Exception as e:
        log(f"Scene extraction error: {e} — using fallback")
        return _fallback_scenes(dream_text)


def _fallback_scenes(dream_text: str) -> list[dict]:
    """Split dream into segments when LLM fails."""
    sentences = [s.strip() for s in re.split(r'[.!?—]+', dream_text) if len(s.strip()) > 20]
    # Pick 7 evenly spaced sentences
    step = max(1, len(sentences) // 7)
    picks = sentences[::step][:7]
    moves = ["push_in", "pan_right", "drift_up", "pull_back", "pan_left", "static", "vertiginous"]
    return [
        {"visual": pick[:180], "camera": moves[i % len(moves)], "title": f"Scene {i+1}", "duration": 4, "mood": "dreamlike"}
        for i, pick in enumerate(picks)
    ]


# ── Image generation ──────────────────────────────────────────────────────────

def swarmui_session() -> str:
    req = urllib.request.Request(
        f"{SWARMUI_URL}/API/GetNewSession",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["session_id"]


def generate_keyframe(session: str, scene: dict, scene_num: int,
                      prev_image: str | None = None) -> str | None:
    """Generate scene keyframe. Optionally uses prev_image for visual continuity."""
    prompt = f"{DREAM_STYLE}, {scene['visual']}"
    payload = {
        "session_id": session,
        "images":     1,
        "prompt":     prompt,
        "model":      "Juggernaut_X_RunDiffusion_Hyper.safetensors",
        "width":      1024,
        "height":     576,
        "steps":      20,
        "cfgscale":   5,
        "seed":       -1,
    }
    try:
        req = urllib.request.Request(
            f"{SWARMUI_URL}/API/GenerateText2Image",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            result = json.loads(r.read())

        if "error" in result:
            log(f"Scene {scene_num} error: {result['error']}")
            return None

        images = result.get("images", [])
        if not images:
            return None

        rel_path = images[0].split("/", 3)[-1].strip()
        full_path = Path.home() / "AI/SwarmUI/Output/local/raw" / rel_path
        if not full_path.exists():
            log(f"Scene {scene_num}: file not found")
            return None

        dest = MOVIE_DIR / f"scene_{scene_num:02d}_{full_path.stem}.png"
        dest.write_bytes(full_path.read_bytes())
        log(f"Scene {scene_num} ({scene['mood']}): {full_path.name}")
        return str(dest)

    except Exception as e:
        log(f"Scene {scene_num} generation failed: {e}")
        return None


# ── Ken Burns ffmpeg assembly ─────────────────────────────────────────────────

def build_scene_clip(image_path: str, scene: dict, scene_num: int,
                     fps: int = 24) -> str | None:
    """Apply Ken Burns effect to a single image. Returns path to clip."""
    move = CAMERA_MOVES.get(scene.get("camera", "static"), CAMERA_MOVES["static"])
    zoom_expr, x_expr, y_expr, _ = move
    duration = scene.get("duration", 6)
    total_frames = duration * fps
    output = str(MOVIE_DIR / f"clip_{scene_num:02d}.mp4")

    # zoompan: d=total_frames, fps=fps, s=output_size
    # The output size is 1024x576, zoompan works on the input image
    zoompan = (
        f"zoompan="
        f"z='{zoom_expr}':"
        f"x='{x_expr}':"
        f"y='{y_expr}':"
        f"d={total_frames}:"
        f"s=1024x576:"
        f"fps={fps}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", image_path,
        "-vf", f"{zoompan},format=yuv420p",
        "-c:v", "libx264", "-crf", "20",
        "-t", str(duration),
        "-r", str(fps),
        output,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            log(f"Clip {scene_num}: {duration}s, {scene.get('camera','static')} ({move[3]})")
            return output
        else:
            log(f"Clip {scene_num} ffmpeg error: {result.stderr[-200:]}")
            return None
    except Exception as e:
        log(f"Clip {scene_num} failed: {e}")
        return None


def assemble_movie(clips: list[str], output_path: str,
                   dissolve_frames: int = 24) -> bool:
    """Concatenate clips with cross-dissolves using ffmpeg filter_complex."""
    if not clips:
        return False

    if len(clips) == 1:
        # Single clip — just copy it
        cmd = ["ffmpeg", "-y", "-i", clips[0], "-c", "copy", output_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0

    # Build filter_complex for cross-dissolves between N clips
    inputs = []
    for c in clips:
        inputs += ["-i", c]

    # Chain xfade transitions
    filter_parts = []
    current = "[0:v]"
    for i in range(1, len(clips)):
        # Get duration of the previous clip to calculate offset
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", clips[i - 1]],
            capture_output=True, text=True
        )
        duration = 6.0  # default
        try:
            stream_info = json.loads(probe.stdout)
            duration = float(stream_info["streams"][0].get("duration", 6.0))
        except Exception:
            pass

        offset = max(0.1, duration - dissolve_frames / 24.0)
        output_label = f"[v{i}]" if i < len(clips) - 1 else "[vout]"
        filter_parts.append(
            f"{current}[{i}:v]xfade=transition=dissolve:"
            f"duration={dissolve_frames/24:.2f}:"
            f"offset={offset:.2f}{output_label}"
        )
        current = f"[v{i}]"

    filter_complex = ";".join(filter_parts)

    cmd = (
        ["ffmpeg", "-y"]
        + inputs
        + ["-filter_complex", filter_complex,
           "-map", "[vout]",
           "-c:v", "libx264", "-crf", "20",
           "-r", "24",
           output_path]
    )

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            size_mb = Path(output_path).stat().st_size / 1_048_576
            log(f"Movie assembled: {output_path} ({size_mb:.1f}MB)")
            return True
        else:
            log(f"Assembly error: {result.stderr[-300:]}")
            return False
    except Exception as e:
        log(f"Assembly failed: {e}")
        return False


# ── Slack delivery ────────────────────────────────────────────────────────────

def post_movie_to_slack(movie_path: str, title: str) -> bool:
    """Upload movie to Slack #nova-chat."""
    path = Path(movie_path)
    if not path.exists():
        return False

    file_size = path.stat().st_size
    log(f"Uploading {file_size / 1_048_576:.1f}MB movie to Slack")

    # Step 1: Get upload URL
    try:
        req = urllib.request.Request(
            "https://slack.com/api/files.getUploadURLExternal",
            data=f"filename={path.name}&length={file_size}".encode(),
            headers={"Authorization": f"Bearer {SLACK_TOKEN}"}
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            url_data = json.loads(r.read())
        if not url_data.get("ok"):
            log(f"Upload URL error: {url_data.get('error')}")
            return False
        upload_url = url_data["upload_url"]
        file_id = url_data["file_id"]
    except Exception as e:
        log(f"Upload URL failed: {e}")
        return False

    # Step 2: Upload
    try:
        with open(path, "rb") as f:
            file_bytes = f.read()
        req = urllib.request.Request(upload_url, data=file_bytes, method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=120):
            pass
    except Exception as e:
        log(f"Upload failed: {e}")
        return False

    # Step 3: Complete
    try:
        payload = json.dumps({
            "files":           [{"id": file_id, "title": title}],
            "channel_id":      SLACK_CHANNEL,
            "initial_comment": f"*{title}*",
        }).encode()
        req = urllib.request.Request(
            "https://slack.com/api/files.completeUploadExternal",
            data=payload,
            headers={"Authorization": f"Bearer {SLACK_TOKEN}",
                     "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())
        if result.get("ok"):
            log("Movie posted to Slack")
            return True
        log(f"Complete upload error: {result.get('error')}")
        return False
    except Exception as e:
        log(f"Complete upload failed: {e}")
        return False


# ── LTX-Video animation ───────────────────────────────────────────────────────

LTX_CHECKPOINT = "ltx-video-2b-v0.9.5.safetensors"
LTX_LORA       = "ltx-2-19b-distilled-lora-384.safetensors"
LTX_FRAMES     = 97    # ~4s at 24fps — 7 scenes × 4s + crossfades ≈ 30s total
LTX_FPS        = 24


def ltx_available() -> bool:
    """Returns True if T5 encoder + LTX-Video 2B model are both present."""
    model_path = Path("/Volumes/Data/AI/SwarmUI/dlbackend/ComfyUI/models/checkpoints") / LTX_CHECKPOINT
    return LTX_ENCODER.exists() and model_path.exists()


def animate_with_ltx(image_path: str, prompt: str, scene_num: int) -> str | None:
    """
    Feed a keyframe into LTX-Video image-to-video via ComfyUI on port 7824.
    Returns path to the generated video clip, or None on failure.
    """
    import base64, uuid

    client_id = str(uuid.uuid4())
    output_path = str(MOVIE_DIR / f"ltx_clip_{scene_num:02d}.mp4")

    # Load the image as base64 — ComfyUI accepts this via LoadImage node
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    # Upload image to ComfyUI first
    img_filename = f"dream_scene_{scene_num:02d}.png"
    try:
        import io
        boundary = "----FormBoundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{img_filename}"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode() + Path(image_path).read_bytes() + f"\r\n--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            f"{COMFYUI_LTX}/upload/image",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            upload_result = json.loads(r.read())
        uploaded_name = upload_result.get("name", img_filename)
        log(f"Scene {scene_num}: image uploaded as {uploaded_name}")
    except Exception as e:
        log(f"Scene {scene_num}: image upload failed: {e}")
        return None

    # Build the LTX-Video workflow — matches the official ltxv_image_to_video template
    # Key: SamplerCustom + KSamplerSelect + LTXVScheduler (not KSampler)
    #      strength=0.15 (preserve image, let motion happen)
    #      VAEDecode not VAEDecodeTiled
    prefix = f"dream_ltx_{scene_num:02d}"
    workflow = {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": LTX_CHECKPOINT},
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": "t5xxl_fp8_e4m3fn.safetensors",
                "type": "ltxv",
                "device": "default",
            },
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": f"{DREAM_STYLE}, {prompt}",
                "clip": ["2", 0],
            },
        },
        "4": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": "blurry, distorted, watermark, text, low quality, static, frozen",
                "clip": ["2", 0],
            },
        },
        "5": {
            "class_type": "LTXVConditioning",
            "inputs": {
                "positive": ["3", 0],
                "negative": ["4", 0],
                "frame_rate": float(LTX_FPS),
            },
        },
        "6": {
            "class_type": "LoadImage",
            "inputs": {"image": uploaded_name},
        },
        "7": {
            "class_type": "LTXVImgToVideo",
            "inputs": {
                "positive": ["5", 0],
                "negative": ["5", 1],
                "vae": ["1", 2],
                "image": ["6", 0],
                "width": 768,
                "height": 512,
                "length": LTX_FRAMES,
                "batch_size": 1,
                "strength": 0.15,
            },
        },
        "8": {
            "class_type": "LTXVScheduler",
            "inputs": {
                "steps": 20,
                "max_shift": 2.05,
                "base_shift": 0.95,
                "stretch": True,
                "terminal": 0.1,
            },
        },
        "9": {
            "class_type": "KSamplerSelect",
            "inputs": {"sampler_name": "euler"},
        },
        "10": {
            "class_type": "SamplerCustom",
            "inputs": {
                "model": ["1", 0],
                "add_noise": True,
                "noise_seed": 0,
                "cfg": 3.0,
                "positive": ["7", 0],
                "negative": ["7", 1],
                "sampler": ["9", 0],
                "sigmas": ["8", 0],
                "latent_image": ["7", 2],
            },
        },
        "11": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["10", 0],
                "vae": ["1", 2],
            },
        },
        "12": {
            "class_type": "VHS_VideoCombine",
            "inputs": {
                "images": ["11", 0],
                "frame_rate": LTX_FPS,
                "loop_count": 0,
                "filename_prefix": prefix,
                "format": "video/h264-mp4",
                "pingpong": False,
                "save_output": True,
            },
        },
    }

    try:
        # Queue the workflow
        payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
        req = urllib.request.Request(
            f"{COMFYUI_LTX}/prompt",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            queue_result = json.loads(r.read())

        prompt_id = queue_result.get("prompt_id")
        if not prompt_id:
            log(f"Scene {scene_num}: no prompt_id in queue response")
            return None

        log(f"Scene {scene_num}: LTX queued ({prompt_id[:8]}...) — waiting")

        # Poll for completion (up to 10 minutes — 2B model takes ~2min on MPS)
        for attempt in range(300):
            time.sleep(2)
            req = urllib.request.Request(f"{COMFYUI_LTX}/history/{prompt_id}")
            with urllib.request.urlopen(req, timeout=10) as r:
                history = json.loads(r.read())

            if prompt_id not in history:
                continue

            outputs = history[prompt_id].get("outputs", {})
            # VHS_VideoCombine output is in node "12"
            video_outputs = outputs.get("12", {}).get("gifs", [])
            if not video_outputs:
                video_outputs = outputs.get("12", {}).get("videos", [])

            if video_outputs:
                video_info = video_outputs[0]
                # Use fullpath if available (ComfyUI provides it)
                if video_info.get("fullpath"):
                    src = Path(video_info["fullpath"])
                else:
                    src = Path("/Volumes/Data/AI/SwarmUI/dlbackend/ComfyUI/output") / video_info.get("filename", "")
                if not src.exists():
                    src = Path.home() / "AI/SwarmUI/dlbackend/ComfyUI/output" / video_info.get("filename", "")

                if src.exists():
                    import shutil
                    shutil.copy2(src, output_path)
                    log(f"Scene {scene_num}: LTX animation done ({LTX_FRAMES} frames)")
                    return output_path
                else:
                    log(f"Scene {scene_num}: video file not found at {src}")
                    return None

        log(f"Scene {scene_num}: LTX timed out after 5 minutes")
        return None

    except Exception as e:
        log(f"Scene {scene_num}: LTX error: {e}")
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

def generate_dream_movie(dream_text: str, post_to_slack: bool = True) -> str | None:
    """
    Full pipeline: dream text → movie file.
    Returns path to the generated movie, or None on failure.
    """
    log(f"Starting dream movie — {len(dream_text.split())} word dream")

    mode = "LTX-Video animation" if ltx_available() else "Ken Burns"
    log(f"Mode: {mode}")

    # Step 1: Extract scenes
    log("Extracting narrative scenes...")
    scenes = extract_scenes(dream_text)
    if not scenes:
        log("No scenes extracted")
        return None
    log(f"Scenes: {[(s.get('mood','?'), s.get('camera','?'), s.get('duration',6)) for s in scenes]}")

    # Step 2: Generate keyframes
    log("Generating keyframes via SwarmUI...")
    try:
        session = swarmui_session()
    except Exception as e:
        log(f"Cannot connect to SwarmUI: {e}")
        return None

    images = []
    prev_image = None
    for i, scene in enumerate(scenes):
        img = generate_keyframe(session, scene, i + 1, prev_image)
        if img:
            images.append((img, scene))
            prev_image = img
        else:
            log(f"Scene {i+1} image failed — skipping")

    if len(images) < 2:
        log(f"Only {len(images)} image(s) generated — need at least 2")
        return None

    # Step 3: Build clips — LTX-Video animation or Ken Burns fallback
    use_ltx = ltx_available()
    log(f"Building clips via {'LTX-Video animation' if use_ltx else 'Ken Burns'}...")
    clips = []
    for i, (img, scene) in enumerate(images):
        if use_ltx:
            clip = animate_with_ltx(img, scene.get("visual", ""), i + 1)
            if not clip:
                log(f"Scene {i+1}: LTX failed — falling back to Ken Burns")
                clip = build_scene_clip(img, scene, i + 1)
        else:
            clip = build_scene_clip(img, scene, i + 1)
        if clip:
            clips.append(clip)

    if not clips:
        log("No clips generated")
        return None

    # Step 4: Assemble
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    movie_path = str(MOVIE_DIR / f"dream_movie_{timestamp}.mp4")

    log(f"Assembling {len(clips)} clips into movie...")
    if not assemble_movie(clips, movie_path, dissolve_frames=18):  # 0.75s crossfades
        return None

    # Clean up intermediate clips
    for clip in clips:
        Path(clip).unlink(missing_ok=True)

    total_duration = sum(s.get("duration", 6) for _, s in images)
    log(f"Movie complete: {total_duration}s, {len(images)} scenes")

    # Step 5: Post to Slack
    if post_to_slack:
        today = datetime.now().strftime("%Y-%m-%d")
        title = f"Dream Movie — {today}"
        post_movie_to_slack(movie_path, title)

    return movie_path


def main():
    if len(sys.argv) > 1:
        # Dream text from command line
        dream_text = " ".join(sys.argv[1:])
    else:
        # Read from stdin or pending_delivery.json
        pending = Path.home() / ".openclaw/workspace/journal/pending_delivery.json"
        if pending.exists():
            data = json.loads(pending.read_text())
            dream_text = data.get("narrative", "")
            log(f"Using dream from pending_delivery.json ({len(dream_text.split())} words)")
        elif not sys.stdin.isatty():
            dream_text = sys.stdin.read().strip()
        else:
            print("Usage: nova_dream_movie.py <dream text>", file=sys.stderr)
            print("       nova_dream_movie.py  (reads pending_delivery.json)", file=sys.stderr)
            sys.exit(1)

    if not dream_text:
        log("ERROR: No dream text provided")
        sys.exit(1)

    result = generate_dream_movie(dream_text)
    if result:
        print(f"Movie: {result}")
    else:
        print("Movie generation failed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
