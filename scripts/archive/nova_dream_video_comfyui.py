#!/usr/bin/env python3
"""
nova_dream_video_comfyui.py — Generate dream video via SwarmUI.

Previously called ComfyUI directly on ports 7823/7824.
Now routes through SwarmUI (port 7801) which manages its own ComfyUI backend.
SwarmUI is stable and handles all image/video generation.

Written by Jordan Koch.
"""

import json
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

SWARMUI_URL = "http://localhost:7801"
SCRIPTS     = Path(__file__).parent
WORKSPACE   = Path.home() / ".openclaw/workspace"
DREAM_DIR   = WORKSPACE / "dream_videos"
DREAM_DIR.mkdir(exist_ok=True)


def log(msg: str):
    print(f"[nova_dream_video {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_session() -> str:
    req = urllib.request.Request(
        f"{SWARMUI_URL}/API/GetNewSession",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["session_id"]


def generate_frame(session: str, prompt: str, frame_num: int,
                   width: int = 1024, height: int = 576,
                   steps: int = 15, model: str = "") -> str | None:
    """Generate one frame via SwarmUI. Returns workspace path or None."""
    payload = {
        "session_id": session,
        "images":     1,
        "prompt":     prompt,
        "width":      width,
        "height":     height,
        "steps":      steps,
        "cfgscale":   2,
        "seed":       -1,
    }
    if model:
        payload["model"] = model

    try:
        req = urllib.request.Request(
            f"{SWARMUI_URL}/API/GenerateText2Image",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            result = json.loads(r.read())

        if "error" in result:
            log(f"Frame {frame_num} error: {result['error']}")
            return None

        images = result.get("images", [])
        if not images:
            log(f"Frame {frame_num}: no images in response")
            return None

        # images[0] is a relative path like View/local/raw/2026-04-05/file.png
        rel_path = images[0].split("/", 3)[-1].strip()
        full_path = Path.home() / "AI/SwarmUI/Output/local/raw" / rel_path

        if not full_path.exists():
            log(f"Frame {frame_num}: file not found at {full_path}")
            return None

        # Copy to workspace
        dest = WORKSPACE / full_path.name
        dest.write_bytes(full_path.read_bytes())
        log(f"Frame {frame_num}: {full_path.name}")
        return str(dest)

    except Exception as e:
        log(f"Frame {frame_num} failed: {e}")
        return None


def frames_to_video(frame_paths: list, output_path: str, fps: int = 2) -> bool:
    """Combine frames into a video with ffmpeg."""
    if not frame_paths:
        return False
    try:
        list_file = DREAM_DIR / "frames.txt"
        with open(list_file, "w") as f:
            for p in frame_paths:
                f.write(f"file '{p}'\n")
                f.write(f"duration {1/fps}\n")

        result = subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-vf", "scale=1024:576,format=yuv420p",
            "-c:v", "libx264", "-crf", "23",
            output_path,
        ], capture_output=True, text=True, timeout=60)

        list_file.unlink(missing_ok=True)

        if result.returncode == 0:
            log(f"Video created: {output_path}")
            return True
        else:
            log(f"ffmpeg failed: {result.stderr[:200]}")
            return False
    except Exception as e:
        log(f"Video assembly failed: {e}")
        return False


def generate_dream_video(prompt: str, num_frames: int = 5, model: str = "") -> str | None:
    """Generate a dream video from a prompt. Returns path to video or None."""
    log(f"Connecting to SwarmUI at {SWARMUI_URL}")

    try:
        session = get_session()
    except Exception as e:
        log(f"Cannot connect to SwarmUI: {e}")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = str(DREAM_DIR / f"dream_{timestamp}.mp4")

    log(f"Generating {num_frames} frames...")
    frames = []
    for i in range(num_frames):
        frame_prompt = f"{prompt}, cinematic frame {i + 1}/{num_frames}"
        path = generate_frame(session, frame_prompt, i + 1, model=model)
        if path:
            frames.append(path)

    if not frames:
        log("No frames generated")
        return None

    log(f"Assembling {len(frames)} frames into video")
    if frames_to_video(frames, output_path):
        return output_path
    return None


def main():
    prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "surreal dream landscape"
    result = generate_dream_video(prompt)
    if result:
        print(f"Video: {result}")
    else:
        print("Video generation failed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
