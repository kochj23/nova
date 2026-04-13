#!/usr/bin/env python3
"""
nova_sky_watcher.py — Automated sky photography and golden hour capture.

Inspired by Sam's sky-watcher (9,000+ frames). Nova's version:

  1. Calculates sunrise/sunset for Burbank daily using solar position math
  2. During golden hour (±45 min around sunrise/sunset), captures a frame
     every 5 minutes from the best sky-facing camera
  3. Archives all golden hour frames with timestamps
  4. At the end of each golden hour session, picks the "best" frame
     (highest color variance = most dramatic sky) and posts it to Slack
  5. Weekly: generates a timelapse GIF from the week's best shots
  6. Stores sky observations in vector memory for seasonal awareness

Sky archive: /Volumes/Data/nova-sky/YYYY/MM/DD/
Best shots:  /Volumes/Data/nova-sky/best/

Cron: every 5 min (only captures during golden hours, sleeps otherwise)
Written by Jordan Koch.
"""

import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_CHAN
SLACK_API = nova_config.SLACK_API
VECTOR_URL = nova_config.VECTOR_URL
NOW = datetime.now()
TODAY = date.today().isoformat()

# ── Configuration ────────────────────────────────────────────────────────────

# Burbank, CA
LATITUDE = 34.1808
LONGITUDE = -118.3090
TIMEZONE_OFFSET = -7  # PDT (adjust for DST manually or use tz)

# Sky-facing cameras (ordered by preference — front yard has the widest sky view)
# URLs loaded from camera_config.py (gitignored)
try:
    from camera_config import CAMERAS as _ALL_CAMERAS
    SKY_CAMERAS = [
        (name, _ALL_CAMERAS[name]) for name in ["front_yard", "front_yard_alt", "back_patio"]
        if name in _ALL_CAMERAS
    ]
except ImportError:
    SKY_CAMERAS = []

# Golden hour window: capture from this many minutes before to after
GOLDEN_BEFORE = 45  # minutes before sunrise/sunset
GOLDEN_AFTER = 45   # minutes after sunrise/sunset

# Capture settings
CAPTURE_INTERVAL = 300  # 5 minutes between captures during golden hour
FRAME_RESOLUTION = "1920x1080"  # Request higher res for sky shots

# Storage
SKY_ARCHIVE = Path("/Volumes/Data/nova-sky")
BEST_DIR = SKY_ARCHIVE / "best"
STATE_FILE = Path.home() / ".openclaw/workspace/state/nova_sky_watcher_state.json"
CAMERA_FRAMES = Path.home() / ".openclaw/workspace/camera_frames"


def log(msg):
    print(f"[nova_sky {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text, channel=None):
    data = json.dumps({
        "channel": channel or SLACK_CHAN, "text": text, "mrkdwn": True
    }).encode()
    req = urllib.request.Request(
        f"{SLACK_API}/chat.postMessage", data=data,
        headers={"Authorization": "Bearer " + SLACK_TOKEN,
                 "Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        log(f"Slack error: {e}")


def slack_upload(filepath, comment="", channel=None):
    """Upload image to Slack."""
    try:
        cmd = [
            "curl", "-s", "-F", f"file=@{filepath}",
            "-F", f"channels={channel or SLACK_CHAN}",
            "-F", f"initial_comment={comment}",
            "-H", f"Authorization: Bearer {SLACK_TOKEN}",
            "https://slack.com/api/files.upload"
        ]
        subprocess.run(cmd, capture_output=True, timeout=15)
    except Exception as e:
        log(f"Slack upload error: {e}")


def vector_remember(text, metadata=None):
    try:
        payload = json.dumps({
            "text": text, "source": "sky_watcher", "metadata": metadata or {}
        }).encode()
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ── Solar position calculator ────────────────────────────────────────────────
# Based on NOAA Solar Calculator — no external dependencies

def solar_times(dt, lat, lon):
    """Calculate sunrise, sunset, and solar noon for a given date and location.

    Returns (sunrise_dt, sunset_dt, solar_noon_dt) as datetime objects.
    Uses the NOAA simplified solar position algorithm.
    """
    # Day of year
    n = dt.timetuple().tm_yday

    # Solar declination (radians)
    declination = math.radians(-23.45 * math.cos(math.radians(360 / 365 * (n + 10))))

    # Equation of time (minutes) — simplified
    B = math.radians(360 / 365 * (n - 81))
    eot = 9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)

    # Solar noon (local standard time)
    # Time offset from UTC in hours
    try:
        tz_offset = dt.astimezone().utcoffset().total_seconds() / 3600
    except Exception:
        tz_offset = TIMEZONE_OFFSET
    solar_noon_minutes = 720 - 4 * lon - eot + tz_offset * 60

    # Hour angle for sunrise/sunset
    lat_rad = math.radians(lat)
    cos_hour_angle = (
        math.cos(math.radians(90.833)) /
        (math.cos(lat_rad) * math.cos(declination))
        - math.tan(lat_rad) * math.tan(declination)
    )

    # Clamp for polar regions (shouldn't matter for Burbank)
    cos_hour_angle = max(-1, min(1, cos_hour_angle))
    hour_angle = math.degrees(math.acos(cos_hour_angle))

    # Sunrise and sunset in minutes from midnight
    sunrise_minutes = solar_noon_minutes - hour_angle * 4
    sunset_minutes = solar_noon_minutes + hour_angle * 4

    base = datetime(dt.year, dt.month, dt.day)
    sunrise = base + timedelta(minutes=sunrise_minutes)
    sunset = base + timedelta(minutes=sunset_minutes)
    solar_noon = base + timedelta(minutes=solar_noon_minutes)

    return sunrise, sunset, solar_noon


def get_golden_hours():
    """Get today's golden hour windows as (start, end) datetime pairs."""
    sunrise, sunset, _ = solar_times(NOW, LATITUDE, LONGITUDE)

    golden_sunrise = (
        sunrise - timedelta(minutes=GOLDEN_BEFORE),
        sunrise + timedelta(minutes=GOLDEN_AFTER),
    )
    golden_sunset = (
        sunset - timedelta(minutes=GOLDEN_BEFORE),
        sunset + timedelta(minutes=GOLDEN_AFTER),
    )

    return golden_sunrise, golden_sunset, sunrise, sunset


def is_golden_hour():
    """Check if we're currently in a golden hour window."""
    gs, gset, _, _ = get_golden_hours()
    return (gs[0] <= NOW <= gs[1]) or (gset[0] <= NOW <= gset[1])


def current_session():
    """Return which golden hour session we're in, or None."""
    gs, gset, sunrise, sunset = get_golden_hours()
    if gs[0] <= NOW <= gs[1]:
        return "sunrise", sunrise
    if gset[0] <= NOW <= gset[1]:
        return "sunset", sunset
    return None, None


# ── Camera capture ───────────────────────────────────────────────────────────

def capture_frame(camera_name, rtsp_url, output_path):
    """Capture a single high-res frame from an RTSP camera."""
    try:
        cmd = [
            "ffmpeg", "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-frames:v", "1",
            "-q:v", "2",  # High quality JPEG
            "-update", "1",
            "-y", str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        if result.returncode == 0 and output_path.exists():
            return True
        return False
    except subprocess.TimeoutExpired:
        log(f"Capture timeout on {camera_name}")
        return False
    except Exception as e:
        log(f"Capture error on {camera_name}: {e}")
        return False


def capture_sky_frame():
    """Capture a frame from the best available sky camera."""
    today_dir = SKY_ARCHIVE / NOW.strftime("%Y/%m/%d")
    today_dir.mkdir(parents=True, exist_ok=True)

    timestamp = NOW.strftime("%H%M%S")
    session, _ = current_session()
    prefix = session or "sky"

    for camera_name, rtsp_url in SKY_CAMERAS:
        filename = f"{prefix}_{camera_name}_{timestamp}.jpg"
        output_path = today_dir / filename

        if capture_frame(camera_name, rtsp_url, output_path):
            log(f"Captured: {filename} ({output_path.stat().st_size // 1024}KB)")
            return output_path, camera_name

    log("All sky cameras failed")
    return None, None


# ── Image analysis ───────────────────────────────────────────────────────────

def frame_color_score(image_path):
    """Score a frame by color variance — more variance = more dramatic sky.

    Uses ffprobe to get histogram data, or falls back to file size as proxy.
    Dramatic skies have more color variation (oranges, pinks, purples) vs
    flat grey/blue.
    """
    try:
        # Use sips to get basic stats, or PIL if available
        from PIL import Image
        import numpy as np

        img = Image.open(str(image_path))
        arr = np.array(img, dtype=np.float32)

        # Score based on:
        # 1. Color standard deviation (more = more dramatic)
        # 2. Warm channel presence (red/orange in sky)
        # 3. Overall brightness (not too dark, not blown out)
        color_std = arr.std()
        red_ratio = arr[:, :, 0].mean() / (arr.mean() + 1)
        brightness = arr.mean()

        # Sweet spot: bright enough to see, warm colors, high variance
        score = color_std * 0.5
        if red_ratio > 1.1:  # Warm sky
            score *= 1.5
        if 80 < brightness < 200:  # Good exposure
            score *= 1.2
        elif brightness < 40 or brightness > 240:
            score *= 0.5

        return score
    except ImportError:
        # Fallback: use file size as proxy (bigger = more detail = more interesting)
        try:
            return image_path.stat().st_size / 1024
        except Exception:
            return 0
    except Exception:
        return 0


def pick_best_frame(directory, session_prefix=""):
    """Pick the best frame from a directory based on color scoring."""
    frames = sorted(directory.glob(f"{session_prefix}*.jpg"))
    if not frames:
        return None

    best = None
    best_score = 0

    for frame in frames:
        score = frame_color_score(frame)
        if score > best_score:
            best_score = score
            best = frame

    return best


# ── State management ─────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_capture": "", "sessions_today": [], "frames_today": 0,
            "last_best_posted": "", "total_frames": 0}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Session management ───────────────────────────────────────────────────────

def post_session_best(session_name):
    """At the end of a golden hour session, pick and post the best shot."""
    today_dir = SKY_ARCHIVE / NOW.strftime("%Y/%m/%d")
    if not today_dir.exists():
        return

    best = pick_best_frame(today_dir, session_name)
    if not best:
        log(f"No frames found for {session_name} session")
        return

    # Copy to best/ archive
    BEST_DIR.mkdir(parents=True, exist_ok=True)
    best_dest = BEST_DIR / f"{TODAY}_{session_name}.jpg"
    shutil.copy2(str(best), str(best_dest))

    # Count today's frames
    frame_count = len(list(today_dir.glob(f"{session_name}*.jpg")))

    # Post to Slack
    sunrise, sunset, _, _ = get_golden_hours()
    caption = (
        f"*{session_name.title()} — {NOW.strftime('%B %d')}*\n"
        f"Best of {frame_count} frames captured during golden hour\n"
        f"_Camera: {best.stem.split('_')[1] if '_' in best.stem else 'sky'}_"
    )
    slack_upload(str(best_dest), caption)

    # Store in vector memory
    vector_remember(
        f"{session_name.title()} sky on {TODAY}: captured {frame_count} frames during golden hour. "
        f"Best shot saved to {best_dest.name}",
        {"date": TODAY, "type": f"sky_{session_name}", "frames": frame_count}
    )

    log(f"Posted best {session_name} frame: {best_dest.name} (from {frame_count} captures)")


# ── Timelapse generation ─────────────────────────────────────────────────────

def generate_timelapse(days=7):
    """Generate a timelapse GIF from the best shots of the last N days."""
    frames = sorted(BEST_DIR.glob("*.jpg"))
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    recent = [f for f in frames if f.stem[:10] >= cutoff]

    if len(recent) < 4:
        log(f"Not enough frames for timelapse ({len(recent)} < 4)")
        return None

    output = SKY_ARCHIVE / f"timelapse_{TODAY}_last{days}d.gif"

    # Use ffmpeg to create GIF
    # Create a temp file list
    list_file = Path.home() / ".openclaw/workspace/state/nova_sky_frames.txt"
    with open(list_file, "w") as f:
        for frame in recent:
            f.write(f"file '{frame}'\n")
            f.write(f"duration 0.5\n")  # 0.5s per frame

    try:
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-vf", f"scale=800:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
            "-loop", "0",
            str(output),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode == 0 and output.exists():
            log(f"Timelapse generated: {output.name} ({output.stat().st_size // 1024}KB)")
            return output
        log(f"ffmpeg timelapse error: {result.stderr[:200]}")
        return None
    except Exception as e:
        log(f"Timelapse error: {e}")
        return None
    finally:
        list_file.unlink(missing_ok=True)


def post_weekly_timelapse():
    """Generate and post a weekly timelapse."""
    output = generate_timelapse(days=7)
    if output:
        frame_count = len(list(BEST_DIR.glob("*.jpg")))
        caption = (
            f"*Weekly Sky Timelapse — {(date.today() - timedelta(days=7)).strftime('%b %d')} to {NOW.strftime('%b %d')}*\n"
            f"_{frame_count} total best shots in the archive_"
        )
        slack_upload(str(output), caption)
        vector_remember(
            f"Weekly sky timelapse generated on {TODAY}: {frame_count} frames in archive",
            {"date": TODAY, "type": "sky_timelapse"}
        )


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    state = load_state()
    session, event_time = current_session()

    if not session:
        # Not in golden hour — check if we just finished a session
        gs, gset, sunrise, sunset = get_golden_hours()

        # If sunrise golden hour just ended (within last 10 min)
        if (NOW - gs[1]).total_seconds() < 600 and "sunrise_posted" not in state.get("sessions_today", []):
            post_session_best("sunrise")
            state.setdefault("sessions_today", []).append("sunrise_posted")
            save_state(state)

        # If sunset golden hour just ended
        if (NOW - gset[1]).total_seconds() < 600 and "sunset_posted" not in state.get("sessions_today", []):
            post_session_best("sunset")
            state.setdefault("sessions_today", []).append("sunset_posted")
            save_state(state)

        log(f"Not golden hour. Sunrise: {sunrise.strftime('%I:%M %p')}, Sunset: {sunset.strftime('%I:%M %p')}")
        return

    # We're in golden hour — capture!
    last = state.get("last_capture", "")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            seconds_since = (NOW - last_dt).total_seconds()
            if seconds_since < CAPTURE_INTERVAL - 30:  # Allow 30s tolerance
                log(f"Last capture {int(seconds_since)}s ago, waiting...")
                return
        except Exception:
            pass

    log(f"Golden hour ({session}) — capturing...")
    frame_path, camera = capture_sky_frame()

    if frame_path:
        state["last_capture"] = NOW.isoformat()
        state["frames_today"] = state.get("frames_today", 0) + 1
        state["total_frames"] = state.get("total_frames", 0) + 1
        save_state(state)

        # Log every 3rd frame to avoid noise
        if state["frames_today"] % 3 == 1:
            log(f"Frame #{state['frames_today']} today (#{state['total_frames']} total)")


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Sky Watcher")
    parser.add_argument("--capture", action="store_true", help="Capture now (even outside golden hour)")
    parser.add_argument("--status", action="store_true", help="Show sky watcher status")
    parser.add_argument("--best", action="store_true", help="Post today's best frame")
    parser.add_argument("--timelapse", type=int, nargs="?", const=7, help="Generate timelapse (N days)")
    parser.add_argument("--solar", action="store_true", help="Show today's solar times")
    parser.add_argument("--archive-stats", action="store_true", help="Show archive statistics")
    args = parser.parse_args()

    if args.solar:
        sunrise, sunset, noon = solar_times(NOW, LATITUDE, LONGITUDE)
        gs, gset, _, _ = get_golden_hours()
        print(f"Solar Times for Burbank — {NOW.strftime('%A, %B %d %Y')}")
        print(f"  Sunrise:       {sunrise.strftime('%I:%M %p')}")
        print(f"  Solar noon:    {noon.strftime('%I:%M %p')}")
        print(f"  Sunset:        {sunset.strftime('%I:%M %p')}")
        print(f"  Golden AM:     {gs[0].strftime('%I:%M')} - {gs[1].strftime('%I:%M %p')}")
        print(f"  Golden PM:     {gset[0].strftime('%I:%M')} - {gset[1].strftime('%I:%M %p')}")
        print(f"  Currently:     {'GOLDEN HOUR' if is_golden_hour() else 'normal'}")
        session, _ = current_session()
        if session:
            print(f"  Session:       {session}")

    elif args.capture:
        log("Manual capture...")
        frame, cam = capture_sky_frame()
        if frame:
            print(f"Captured: {frame}")
        else:
            print("Capture failed.")

    elif args.best:
        today_dir = SKY_ARCHIVE / NOW.strftime("%Y/%m/%d")
        if today_dir.exists():
            for prefix in ["sunrise", "sunset"]:
                best = pick_best_frame(today_dir, prefix)
                if best:
                    print(f"Best {prefix}: {best}")
                    post_session_best(prefix)
        else:
            print("No captures today.")

    elif args.timelapse is not None:
        output = generate_timelapse(args.timelapse)
        if output:
            print(f"Timelapse: {output}")
        else:
            print("Not enough frames for timelapse.")

    elif args.archive_stats:
        if SKY_ARCHIVE.exists():
            total = sum(1 for _ in SKY_ARCHIVE.rglob("*.jpg"))
            best_count = len(list(BEST_DIR.glob("*.jpg"))) if BEST_DIR.exists() else 0
            days = len(list(SKY_ARCHIVE.glob("????/??/??")))
            size_mb = sum(f.stat().st_size for f in SKY_ARCHIVE.rglob("*.jpg")) / 1024 / 1024
            print(f"Sky Archive Statistics")
            print(f"  Total frames:  {total}")
            print(f"  Best shots:    {best_count}")
            print(f"  Days captured: {days}")
            print(f"  Archive size:  {size_mb:.1f} MB")
            print(f"  Location:      {SKY_ARCHIVE}")
        else:
            print("No archive yet. First capture will create it.")

    elif args.status:
        state = load_state()
        sunrise, sunset, _ = solar_times(NOW, LATITUDE, LONGITUDE)
        gs, gset, _, _ = get_golden_hours()
        print(f"Sky Watcher Status — {NOW.strftime('%I:%M %p')}")
        print(f"  Golden AM:     {gs[0].strftime('%I:%M')} - {gs[1].strftime('%I:%M %p')}")
        print(f"  Golden PM:     {gset[0].strftime('%I:%M')} - {gset[1].strftime('%I:%M %p')}")
        print(f"  In golden hour: {'YES' if is_golden_hour() else 'No'}")
        print(f"  Frames today:  {state.get('frames_today', 0)}")
        print(f"  Total frames:  {state.get('total_frames', 0)}")
        print(f"  Last capture:  {state.get('last_capture', 'never')}")

    else:
        main()
