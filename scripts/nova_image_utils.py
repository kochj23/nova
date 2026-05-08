"""
nova_image_utils.py — Shared image generation with retry logic and backend health checks.

Used by: nova_daily_essay.py, nova_after_dark.py, nova_daily_opinion.py, nova_research_paper.py

Written by Jordan Koch.
"""

import json
import subprocess
import time
import urllib.request
from pathlib import Path

GENERATE_IMAGE_SH = Path.home() / ".openclaw/scripts/generate_image.sh"
SWARMUI_URL = "http://127.0.0.1:7801"
MAX_RETRIES = 3
RETRY_DELAY = 15
TIMEOUT = 360


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


def generate_image(prompt: str, width: int = 1024, height: int = 768, steps: int = 12) -> str | None:
    """Generate an image with retry logic. Returns file path or None."""
    if not ensure_backend():
        return None

    for attempt in range(MAX_RETRIES):
        try:
            result = subprocess.run(
                [str(GENERATE_IMAGE_SH), prompt, str(width), str(height), str(steps)],
                capture_output=True, text=True, timeout=TIMEOUT,
            )
            if result.returncode == 0 and result.stdout.strip():
                image_path = result.stdout.strip().split("\n")[-1]
                if Path(image_path).exists():
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
