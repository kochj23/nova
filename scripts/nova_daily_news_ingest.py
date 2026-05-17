#!/usr/bin/env python3
"""
nova_daily_news_ingest.py — Record, transcribe, and ingest KABC nightly news.

Records the KABC (ABC 7) evening news broadcast via HDHomeRun, transcribes
with MLX Whisper, chunks into digestible segments, and ingests into the
'daily_news' vector for Nova's always-current news awareness.

Schedule: Runs at 5:00 PM, 6:00 PM, and 11:00 PM daily (when news airs).
Each run records 30 minutes of audio, transcribes, and ingests.

Written by Jordan Koch.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

HDHR_STREAM = "http://192.168.1.89:5004/auto/v"
CHANNEL = "7.1"
CHANNEL_NAME = "KABC DT (ABC)"
RECORD_DURATION = 1800  # 30 minutes
FFMPEG = "/opt/homebrew/bin/ffmpeg"
MLX_WHISPER = "/opt/homebrew/bin/mlx_whisper"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
MEMORY_URL = "http://192.168.1.6:18790/remember?async=1"
CHUNK_SIZE = 2000

WORK_DIR = Path("/Volumes/Data/nova-livetv/daily-news")
LOG_FILE = "/tmp/nova-daily-news-ingest.log"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[daily-news %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("daily_news")

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


# ── Recording ─────────────────────────────────────────────────────────────────

def record_audio(duration: int) -> Path | None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    filename = f"kabc_news_{now.strftime('%Y%m%d_%H%M')}.wav"
    output = WORK_DIR / filename
    stream_url = f"{HDHR_STREAM}{CHANNEL}"

    log.info(f"Recording {duration}s from {CHANNEL_NAME} (ch {CHANNEL})...")

    cmd = [
        FFMPEG, "-y",
        "-i", stream_url,
        "-t", str(duration),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(output),
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=duration + 60,
        )
        if result.returncode != 0 and not output.exists():
            log.error(f"ffmpeg failed: {result.stderr[-200:]}")
            return None
    except subprocess.TimeoutExpired:
        log.error("ffmpeg recording timed out")
        return None
    except Exception as e:
        log.error(f"Recording failed: {e}")
        return None

    if output.exists() and output.stat().st_size > 100000:
        log.info(f"Recorded: {output.name} ({output.stat().st_size / 1024 / 1024:.1f} MB)")
        return output
    else:
        log.error("Recording too small or missing")
        return None


# ── Transcription ─────────────────────────────────────────────────────────────

def transcribe(wav_path: Path) -> str:
    log.info(f"Transcribing {wav_path.name}...")

    cmd = [
        MLX_WHISPER,
        "--model", WHISPER_MODEL,
        "--language", "en",
        "--output-format", "txt",
        "--output-dir", str(WORK_DIR),
        str(wav_path),
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=600,
        )
        if result.returncode != 0:
            log.error(f"Whisper failed: {result.stderr[-200:]}")
            return ""
    except subprocess.TimeoutExpired:
        log.error("Whisper transcription timed out")
        return ""
    except Exception as e:
        log.error(f"Transcription failed: {e}")
        return ""

    txt_path = wav_path.with_suffix(".txt")
    if txt_path.exists():
        text = txt_path.read_text().strip()
        log.info(f"Transcribed: {len(text)} chars")
        return text
    return ""


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


def ingest_chunks(chunks: list[str], timestamp: str, date_str: str) -> int:
    ingested = 0
    for i, chunk in enumerate(chunks):
        payload = json.dumps({
            "text": chunk,
            "metadata": {
                "source": "daily_news",
                "channel": CHANNEL,
                "channel_name": CHANNEL_NAME,
                "timestamp": timestamp,
                "date": date_str,
                "chunk_index": i,
                "total_chunks": len(chunks),
                "type": "news_broadcast",
                "privacy": "public",
            },
        }).encode()
        req = __import__("urllib.request", fromlist=["Request"]).Request(
            MEMORY_URL, data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with __import__("urllib.request", fromlist=["urlopen"]).urlopen(req, timeout=10):
                ingested += 1
        except Exception:
            pass
    return ingested


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    timestamp = now.isoformat()

    log.info(f"=== Daily News Ingest — {CHANNEL_NAME} — {date_str} {time_str} ===")

    notify(
        f":satellite: *Daily News Recording Started*\n"
        f"• Channel: {CHANNEL_NAME} (ch {CHANNEL})\n"
        f"• Duration: {RECORD_DURATION // 60} minutes\n"
        f"• Time: {time_str}\n"
        f"• Vector: `daily_news`"
    )

    # Record
    wav = record_audio(RECORD_DURATION)
    if not wav:
        notify(f":x: Daily News Recording Failed — could not record from {CHANNEL_NAME}")
        return

    # Transcribe
    text = transcribe(wav)
    if not text:
        notify(f":x: Daily News Transcription Failed — no text produced")
        wav.unlink(missing_ok=True)
        return

    # Chunk and ingest
    chunks = chunk_text(text)
    log.info(f"Split into {len(chunks)} chunks")

    ingested = ingest_chunks(chunks, timestamp, date_str)
    log.info(f"Ingested {ingested}/{len(chunks)} chunks to daily_news vector")

    # Save transcript
    transcript_path = WORK_DIR / f"kabc_news_{now.strftime('%Y%m%d_%H%M')}_transcript.txt"
    transcript_path.write_text(text)

    # Clean up WAV (keep transcript)
    wav.unlink(missing_ok=True)

    # Summarize with Ollama
    summary = summarize_news(text, time_str, date_str)

    # Report
    notify(
        f":newspaper: *KABC News Summary — {time_str}*\n"
        f"_{date_str} · {RECORD_DURATION // 60} min broadcast · {ingested} chunks ingested_\n\n"
        f"{summary}"
    )

    log.info("Done.")


def summarize_news(text: str, time_str: str, date_str: str) -> str:
    prompt = (
        "You are a news editor. Summarize the following TV news broadcast transcript into "
        "a concise bullet-point summary of the top stories. Include: what happened, where, "
        "and any key names or numbers. Use emoji bullets. Keep it under 500 words. "
        "If there are ads or non-news content, skip them.\n\n"
        f"TRANSCRIPT ({date_str} {time_str} KABC):\n{text[:6000]}"
    )
    payload = json.dumps({
        "model": "qwen3-coder:30b",
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": 800, "temperature": 0.3},
    }).encode()
    req = __import__("urllib.request", fromlist=["Request"]).Request(
        "http://127.0.0.1:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with __import__("urllib.request", fromlist=["urlopen"]).urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
            return data.get("response", "Summary unavailable.")
    except Exception as e:
        log.warning(f"Summary generation failed: {e}")
        return f"_Summary unavailable ({e})_"


if __name__ == "__main__":
    main()
