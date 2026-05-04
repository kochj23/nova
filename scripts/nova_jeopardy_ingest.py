#!/usr/bin/env python3
"""
nova_jeopardy_ingest.py — Ingest Jeopardy! episodes into Nova's vector memory.

Extracts audio from Jeopardy .ts files, transcribes via MLX Whisper,
then chunks transcripts into trivia-sized memories tagged with episode info.

All processing is 100% local — MLX Whisper on Apple Silicon, no cloud.
Posts 5-minute status updates to #nova-notifications with progress and sample facts.

Usage:
  python3 nova_jeopardy_ingest.py

Written by Jordan Koch.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread, Event

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

FFMPEG = "/opt/homebrew/bin/ffmpeg"
VECTOR_URL = "http://127.0.0.1:18790/remember"
MEDIA_PATH = Path("/Volumes/external/videos/TVShows/Jeopardy (1984)/Season 42")
VIDEO_EXTENSIONS = {".ts", ".mp4", ".mkv", ".avi", ".mov"}
STATUS_INTERVAL = 300  # 5 minutes
CHUNK_SIZE = 600  # characters per memory chunk (shorter = more granular trivia)

shutdown = Event()

stats = {
    "total_episodes": 0,
    "processed": 0,
    "transcribed": 0,
    "chunks_stored": 0,
    "errors": 0,
    "current_episode": "",
    "start_time": 0,
    "total_chars": 0,
    "last_sample": "",
    "recent_qa": [],  # last few Q&A pairs extracted
}


def log(msg):
    print(f"[jeopardy {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)


def vector_remember(text, metadata):
    payload = json.dumps({
        "text": text[:2000],
        "source": "television",
        "metadata": metadata,
    }).encode()
    try:
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
        stats["chunks_stored"] += 1
        return True
    except Exception as e:
        log(f"  Memory write failed: {e}")
        stats["errors"] += 1
        return False


def parse_episode(filename):
    """Extract season, episode number, and title from Jeopardy filename."""
    stem = Path(filename).stem
    # Pattern: "Jeopardy (1984) - S42E49 - Jeopardy " or "...S42E87 - S41 Champions Wildcard"
    match = re.match(r'Jeopardy \(1984\)\s*-\s*S(\d+)E(\d+)\s*-\s*(.*)', stem)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        title = match.group(3).strip().rstrip('.')
        if not title or title.lower() == "jeopardy":
            title = f"Episode {episode}"
        return season, episode, title
    return 42, 0, stem


def extract_audio(video_path, output_dir):
    """Extract 16kHz mono WAV from video file."""
    audio_path = Path(output_dir) / "audio.wav"
    try:
        result = subprocess.run([
            FFMPEG, "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            "-y", str(audio_path)
        ], capture_output=True, timeout=600)
        if audio_path.exists() and audio_path.stat().st_size > 1000:
            return str(audio_path)
        else:
            log(f"  Audio extraction produced empty file (ffmpeg rc={result.returncode})")
    except subprocess.TimeoutExpired:
        log(f"  Audio extraction timed out (600s)")
    except Exception as e:
        log(f"  Audio extraction error: {e}")
    return None


def transcribe(audio_path):
    """Transcribe audio using MLX Whisper large-v3-turbo."""
    try:
        import mlx_whisper
        result = mlx_whisper.transcribe(
            audio_path,
            path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
        )
        return result.get("text", "").strip()
    except ImportError:
        log("ERROR: mlx-whisper not installed. Run: pip3 install mlx-whisper")
        return ""
    except Exception as e:
        log(f"  Transcription error: {e}")
        return ""


def extract_qa_pairs(transcript):
    """Attempt to extract question/answer pairs from Jeopardy transcript.

    Jeopardy clues typically follow patterns like:
    - "This [fact]..." followed by "What is [answer]"
    - Category announcements followed by clue text
    """
    qa_pairs = []

    # Pattern: "What is/are/was..." or "Who is/are/was..."
    # These are the contestant answers in Jeopardy format
    answer_pattern = re.compile(
        r'(what|who|where|when)\s+(is|are|was|were)\s+([^?.]+)[?.]',
        re.IGNORECASE
    )

    # Split into sentences
    sentences = re.split(r'(?<=[.!?])\s+', transcript)

    for i, sentence in enumerate(sentences):
        answer_match = answer_pattern.search(sentence)
        if answer_match:
            # The clue is typically the sentence(s) before the answer
            clue = ""
            if i > 0:
                # Look back 1-2 sentences for the clue
                lookback = sentences[max(0, i-2):i]
                clue = " ".join(lookback).strip()

            answer = answer_match.group(0).strip().rstrip('.')
            if clue and len(clue) > 20 and len(answer) > 5:
                qa_pairs.append({"clue": clue[:200], "answer": answer[:100]})

    return qa_pairs


def chunk_transcript(transcript, episode_label):
    """Split transcript into trivia-sized chunks with episode context."""
    words = transcript.split()
    chunks = []
    current = []
    current_len = 0

    for word in words:
        current.append(word)
        current_len += len(word) + 1
        if current_len >= CHUNK_SIZE:
            chunk_text = " ".join(current)
            chunks.append(f"[Jeopardy! {episode_label}] {chunk_text}")
            current = []
            current_len = 0

    if current:
        chunk_text = " ".join(current)
        if len(chunk_text) > 50:
            chunks.append(f"[Jeopardy! {episode_label}] {chunk_text}")

    return chunks


def status_reporter():
    """Background thread: posts progress to #nova-notifications every 5 minutes."""
    while not shutdown.is_set():
        shutdown.wait(STATUS_INTERVAL)
        if shutdown.is_set():
            break

        elapsed = time.time() - stats["start_time"]
        pct = (stats["processed"] / stats["total_episodes"] * 100) if stats["total_episodes"] else 0
        remaining = stats["total_episodes"] - stats["processed"]

        if stats["processed"] > 0:
            avg_per = elapsed / stats["processed"]
            eta_s = remaining * avg_per
            eta = str(timedelta(seconds=int(eta_s)))
        else:
            eta = "calculating..."

        # Build Q&A sample section
        qa_section = ""
        if stats["recent_qa"]:
            qa_lines = []
            for qa in stats["recent_qa"][-3:]:  # show last 3 Q&A pairs
                qa_lines.append(f"  Q: _{qa['clue'][:100]}_\n  A: *{qa['answer']}*")
            qa_section = "\n".join(qa_lines)

        msg = (
            f":brain: *Jeopardy! Ingest Progress*\n"
            f"  Episodes: {stats['processed']}/{stats['total_episodes']} ({pct:.0f}%)\n"
            f"  Transcribed: {stats['transcribed']} | Chunks stored: {stats['chunks_stored']:,}\n"
            f"  Total transcript: {stats['total_chars']:,} chars\n"
            f"  Current: _{stats['current_episode']}_\n"
            f"  Elapsed: {str(timedelta(seconds=int(elapsed)))} | ETA: {eta}\n"
            f"  Errors: {stats['errors']}"
        )
        if qa_section:
            msg += f"\n\n  :question: *Recent Q&A from episodes:*\n{qa_section}"

        slack_post(msg)
        log(f"Status: {stats['processed']}/{stats['total_episodes']} ({pct:.0f}%), {stats['chunks_stored']} chunks, ETA {eta}")


def discover_episodes():
    """Find all Jeopardy episode files."""
    episodes = []
    for f in sorted(MEDIA_PATH.iterdir()):
        if f.suffix.lower() in VIDEO_EXTENSIONS and f.is_file():
            season, ep_num, title = parse_episode(f.name)
            episodes.append((season, ep_num, title, f))
    episodes.sort(key=lambda x: (x[0], x[1]))
    return episodes


def ingest_episode(season, episode, title, filepath):
    """Full pipeline for one episode: extract → transcribe → chunk → store."""
    ep_label = f"S{season:02d}E{episode:02d} — {title}"
    stats["current_episode"] = ep_label
    log(f"Processing: {ep_label} ({filepath.stat().st_size / 1024 / 1024:.0f} MB)")

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = extract_audio(filepath, tmpdir)
        if not audio_path:
            stats["errors"] += 1
            return

        transcript = transcribe(audio_path)

    if not transcript or len(transcript) < 100:
        log(f"  Transcript too short ({len(transcript) if transcript else 0} chars) — skipping")
        stats["errors"] += 1
        return

    stats["transcribed"] += 1
    stats["total_chars"] += len(transcript)
    log(f"  Transcribed: {len(transcript):,} characters")

    # Extract Q&A pairs for notifications
    qa_pairs = extract_qa_pairs(transcript)
    if qa_pairs:
        stats["recent_qa"] = (stats["recent_qa"] + qa_pairs)[-10:]  # keep last 10
        log(f"  Extracted {len(qa_pairs)} Q&A pairs")

    # Store episode metadata
    meta_text = (
        f"Jeopardy! Season {season}, Episode {episode}: \"{title}\". "
        f"Host: Ken Jennings (Season 42, 2025-2026 season). "
        f"Syndicated quiz show with categories across science, history, literature, pop culture, geography, and more."
    )
    metadata = {
        "type": "jeopardy_episode",
        "show": "Jeopardy!",
        "season": season,
        "episode": episode,
        "title": title,
        "host": "Ken Jennings",
        "category": "trivia",
        "owner_favorite": True,
    }
    vector_remember(meta_text, metadata)

    # Store individual Q&A pairs as dedicated memories
    qa_meta = {
        "type": "jeopardy_qa",
        "show": "Jeopardy!",
        "season": season,
        "episode": episode,
        "title": title,
        "host": "Ken Jennings",
        "category": "trivia",
        "owner_favorite": True,
    }
    for qa in qa_pairs:
        qa_text = f"[Jeopardy! {ep_label}] Clue: {qa['clue']} → Answer: {qa['answer']}"
        vector_remember(qa_text, qa_meta)

    # Chunk and store full transcript
    chunks = chunk_transcript(transcript, ep_label)
    log(f"  Storing {len(chunks)} transcript chunks + {len(qa_pairs)} Q&A pairs...")

    chunk_meta = {
        "type": "jeopardy_transcript",
        "show": "Jeopardy!",
        "season": season,
        "episode": episode,
        "title": title,
        "host": "Ken Jennings",
        "category": "trivia",
        "owner_favorite": True,
    }

    for chunk in chunks:
        vector_remember(chunk, chunk_meta)

    if chunks:
        stats["last_sample"] = chunks[len(chunks) // 2]

    stats["processed"] += 1


def main():
    log("Jeopardy! ingest starting...")
    log(f"Source: {MEDIA_PATH}")

    episodes = discover_episodes()
    stats["total_episodes"] = len(episodes)
    stats["start_time"] = time.time()

    if not episodes:
        log("ERROR: No episodes found!")
        slack_post(":x: *Jeopardy! Ingest* — No episodes found at expected path.")
        return

    log(f"Found {len(episodes)} episodes")

    slack_post(
        f":brain: *Jeopardy! Ingest Started*\n"
        f"  {len(episodes)} episodes (Season 42, Ken Jennings era)\n"
        f"  Pipeline: ffmpeg audio → MLX Whisper → chunk → vector memory\n"
        f"  Expect: thousands of trivia facts across all categories\n"
        f"  _Notifications every 5 minutes_"
    )

    reporter = Thread(target=status_reporter, daemon=True)
    reporter.start()

    try:
        for idx, (season, ep_num, title, filepath) in enumerate(episodes):
            ingest_episode(season, ep_num, title, filepath)

            # Post per-episode completion with Q&A samples
            if stats["recent_qa"] and stats["processed"] > 0:
                ep_qa = stats["recent_qa"][-3:]
                qa_lines = "\n".join(
                    f"  Q: _{qa['clue'][:120]}_\n  A: *{qa['answer']}*"
                    for qa in ep_qa
                )
                ep_msg = (
                    f":white_check_mark: *Jeopardy! S42E{ep_num:02d}* — {title}\n"
                    f"  {stats['chunks_stored']:,} total memories stored\n\n"
                    f"  :question: *Sample Q&A:*\n{qa_lines}"
                )
                slack_post(ep_msg)
    except KeyboardInterrupt:
        log("Interrupted by user")
    finally:
        shutdown.set()
        reporter.join(timeout=5)

    elapsed = time.time() - stats["start_time"]
    elapsed_str = str(timedelta(seconds=int(elapsed)))

    summary = (
        f":trophy: *Jeopardy! Ingest Complete*\n"
        f"  {stats['processed']}/{stats['total_episodes']} episodes transcribed\n"
        f"  {stats['chunks_stored']:,} trivia chunks stored in vector memory\n"
        f"  {stats['total_chars']:,} total transcript characters\n"
        f"  Errors: {stats['errors']} | Time: {elapsed_str}\n"
        f"  _Nova now knows what is... a lot of random trivia_"
    )
    slack_post(summary)
    log(summary.replace("*", "").replace("_", "").replace(":trophy:", ""))


if __name__ == "__main__":
    main()
