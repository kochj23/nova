#!/usr/bin/env python3
"""
nova_plex_auto_ingest.py — Auto-detect new Plex content and ingest transcriptions.

Monitors Plex for newly added episodes and movies. When new content appears:
1. Extract audio from the video file
2. Transcribe with MLX Whisper (translate non-English to English)
3. Classify content into an appropriate memory vector
4. Chunk and ingest into Nova's PostgreSQL vector DB

Runs every 30 minutes. Tracks what's been ingested to avoid duplicates.

Written by Jordan Koch.
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

PLEX_URL = "http://192.168.1.10:32400"
MEMORY_URL = "http://127.0.0.1:18790/remember?async=1"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
MLX_WHISPER = "/opt/homebrew/bin/mlx_whisper"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"

WORK_DIR = Path("/Volumes/Data/nova-plex-ingest")
STATE_FILE = WORK_DIR / "ingested_keys.json"
LOG_FILE = "/tmp/nova-plex-auto-ingest.log"
CHUNK_SIZE = 2000
MAX_DURATION_MIN = 180  # skip anything over 3 hours

# Plex library sections to monitor
SECTIONS = {
    "6": "TV Shows",
    "7": "Movies",
    "21": "Documentary",
    "26": "Stand-Up Comedy",
}

# Content classification keywords
VECTOR_MAP = {
    "game_show": ["game show", "jeopardy", "wheel of fortune", "price is right", "family feud", "quiz", "contestant"],
    "comedy": ["comedy", "sitcom", "stand-up", "funny", "sketch", "humor", "laugh"],
    "drama": ["drama", "thriller", "mystery", "crime", "legal"],
    "horror": ["horror", "scary", "slasher", "supernatural", "zombie", "haunted"],
    "documentary": ["documentary", "nature", "science", "history", "biography", "true story", "investigation"],
    "education": ["education", "learning", "lecture", "course", "tutorial", "how to"],
    "action": ["action", "adventure", "fight", "chase", "superhero", "war", "military"],
    "sci_fi": ["sci-fi", "science fiction", "space", "alien", "future", "cyberpunk", "robot"],
    "automotive": ["car", "racing", "engine", "mechanic", "restoration", "garage", "motor"],
    "cooking": ["cooking", "chef", "recipe", "kitchen", "food", "baking", "restaurant"],
    "music": ["music", "concert", "band", "song", "performance", "rock", "jazz"],
    "sports": ["sport", "game", "player", "team", "championship", "league", "match"],
    "home_improvement": ["renovation", "house", "build", "repair", "diy", "workshop", "tool"],
}

# ── Logging ───────────────────────────────────────────────────────────────────

import logging
logging.basicConfig(
    level=logging.INFO,
    format="[plex-ingest %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("plex_ingest")

shutdown = False


def signal_handler(sig, frame):
    global shutdown
    shutdown = True


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def notify(text):
    try:
        nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)
    except Exception:
        pass


# ── Plex API ──────────────────────────────────────────────────────────────────

def plex_token():
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-plex-token", "-w"],
        capture_output=True, text=True,
    )
    token = result.stdout.strip()
    if not token:
        # Try credential exchange
        sys.path.insert(0, str(Path(__file__).parent))
        import nova_plex
        token = nova_plex.token()
    return token


def plex_get(path):
    token = plex_token()
    url = f"{PLEX_URL}{path}{'&' if '?' in path else '?'}X-Plex-Token={token}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def get_recently_added(section_key):
    try:
        data = plex_get(f"/library/sections/{section_key}/recentlyAdded")
        items = data.get("MediaContainer", {}).get("Metadata", [])
        return items
    except Exception as e:
        log.warning(f"Failed to get recently added for section {section_key}: {e}")
        return []


# ── State Management ──────────────────────────────────────────────────────────

def load_state():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"ingested": {}, "last_run": 0}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Audio Extraction & Transcription ─────────────────────────────────────────

def extract_audio(video_path: str) -> Path | None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    wav_path = WORK_DIR / f"temp_audio_{int(time.time())}.wav"

    cmd = [
        FFMPEG, "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(wav_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            log.error(f"ffmpeg failed: {result.stderr[-200:]}")
            return None
    except subprocess.TimeoutExpired:
        log.error("Audio extraction timed out")
        return None

    if wav_path.exists() and wav_path.stat().st_size > 10000:
        return wav_path
    return None


def transcribe(wav_path: Path) -> str:
    out_name = f"plex_{int(time.time())}"
    cmd = [
        MLX_WHISPER, str(wav_path),
        "--model", WHISPER_MODEL,
        "--task", "translate",
        "--output-format", "txt",
        "--output-dir", str(WORK_DIR),
        "--output-name", out_name,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            log.error(f"Whisper failed: {result.stderr[-200:]}")
            return ""
    except subprocess.TimeoutExpired:
        log.error("Transcription timed out (30 min)")
        return ""

    txt_path = WORK_DIR / f"{out_name}.txt"
    if txt_path.exists():
        text = txt_path.read_text().strip()
        txt_path.unlink(missing_ok=True)
        return text
    return ""


# ── Classification ────────────────────────────────────────────────────────────

def classify_content(title: str, show_name: str, genres: list, text: str) -> str:
    combined = f"{title} {show_name} {' '.join(genres)} {text[:1500]}".lower()
    scores = {}
    for vector, keywords in VECTOR_MAP.items():
        score = sum(1 for kw in keywords if kw in combined)
        if score > 0:
            scores[vector] = score

    if not scores:
        return "documentary"
    return max(scores, key=scores.get)


# ── Chunking & Ingestion ──────────────────────────────────────────────────────

def chunk_text(text: str) -> list[str]:
    sentences = text.replace("\n", " ").split(". ")
    chunks = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        candidate = current + ". " + sentence if current else sentence
        if len(candidate) > CHUNK_SIZE:
            if current:
                chunks.append(current.strip() + ".")
            current = sentence
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if len(c) > 50]


def ingest_chunks(chunks: list[str], vector: str, metadata: dict) -> int:
    ingested = 0
    for i, chunk in enumerate(chunks):
        payload = json.dumps({
            "text": chunk,
            "metadata": {
                "source": vector,
                **metadata,
                "chunk_index": i,
                "total_chunks": len(chunks),
                "privacy": "local-only",
            },
        }).encode()
        req = urllib.request.Request(
            MEMORY_URL, data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                ingested += 1
        except Exception:
            pass
    return ingested


# ── Main Processing ───────────────────────────────────────────────────────────

def process_item(item, section_name: str) -> bool:
    item_type = item.get("type", "")
    title = item.get("title", "Unknown")
    show_name = item.get("grandparentTitle", "")
    rating_key = item.get("ratingKey", "")
    duration_ms = int(item.get("duration", 0))
    duration_min = duration_ms // 60000
    genres = [g.get("tag", "") for g in item.get("Genre", [])]

    # Get file path
    file_path = ""
    for media in item.get("Media", []):
        for part in media.get("Part", []):
            file_path = part.get("file", "")
            break

    if not file_path:
        log.warning(f"No file path for {title}")
        return False

    if duration_min > MAX_DURATION_MIN:
        log.info(f"Skipping {title} — too long ({duration_min} min)")
        return False

    if duration_min < 1:
        log.info(f"Skipping {title} — too short ({duration_min} min)")
        return False

    display_name = f"{show_name} — {title}" if show_name else title
    log.info(f"Processing: {display_name} ({duration_min} min)")

    # Extract audio
    wav = extract_audio(file_path)
    if not wav:
        log.error(f"Failed to extract audio: {display_name}")
        return False

    # Transcribe
    text = transcribe(wav)
    wav.unlink(missing_ok=True)

    if not text or len(text) < 100:
        log.warning(f"Transcription too short for {display_name}")
        return False

    # Classify
    vector = classify_content(title, show_name, genres, text)
    log.info(f"Classified as: {vector}")

    # Chunk and ingest
    chunks = chunk_text(text)
    metadata = {
        "title": title,
        "show": show_name,
        "section": section_name,
        "duration_min": duration_min,
        "genres": ", ".join(genres),
        "type": "plex_auto_ingest",
        "ingested_at": datetime.now().isoformat(),
    }
    ingested = ingest_chunks(chunks, vector, metadata)
    log.info(f"Ingested {ingested}/{len(chunks)} chunks → `{vector}`")

    return True


def main():
    log.info("=== Plex Auto-Ingest — Checking for new content ===")
    state = load_state()
    new_count = 0
    total_ingested = 0

    for section_key, section_name in SECTIONS.items():
        if shutdown:
            break

        items = get_recently_added(section_key)
        for item in items:
            if shutdown:
                break

            rating_key = item.get("ratingKey", "")
            if not rating_key or rating_key in state["ingested"]:
                continue

            success = process_item(item, section_name)
            if success:
                new_count += 1
                state["ingested"][rating_key] = {
                    "title": item.get("title", ""),
                    "show": item.get("grandparentTitle", ""),
                    "ingested_at": datetime.now().isoformat(),
                }
                save_state(state)

            # Rate limit between items
            time.sleep(5)

    state["last_run"] = int(time.time())
    save_state(state)

    if new_count > 0:
        notify(
            f":clapper: *Plex Auto-Ingest Complete*\n"
            f"• New items processed: {new_count}\n"
            f"• Sections scanned: {', '.join(SECTIONS.values())}\n"
            f"• All transcribed + classified + ingested"
        )
    log.info(f"Done. Processed {new_count} new items.")


if __name__ == "__main__":
    main()
