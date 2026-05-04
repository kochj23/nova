#!/usr/bin/env python3
"""
nova_film_docs_ingest.py — Ingest Film Documentaries into Nova's vector memory.

Extracts audio from documentary files, transcribes via MLX Whisper,
then chunks transcripts into memories with film/documentary metadata.

20 episodes covering behind-the-scenes of horror/sci-fi classics:
Amityville Horror, Evil Dead, Friday the 13th, Halloween, Jaws, Mad Max, etc.

All processing is 100% local — MLX Whisper on Apple Silicon, no cloud.
Posts per-episode notifications to #nova-notifications with interesting facts extracted.

Usage:
  python3 nova_film_docs_ingest.py

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
MEDIA_PATH = Path("/Volumes/external/videos/TVShows/Film Documentaries")
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".ts", ".m4v", ".mov", ".webm", ".np4"}
STATUS_INTERVAL = 300  # 5 minutes
CHUNK_SIZE = 700  # characters per memory chunk

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
    "recent_facts": [],  # interesting excerpts from recent episodes
}


def log(msg):
    print(f"[film_docs {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)


def vector_remember(text, metadata):
    payload = json.dumps({
        "text": text[:2000],
        "source": "local_knowledge",
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
    """Extract episode number and documentary title from filename."""
    stem = Path(filename).stem
    # Pattern: "Film Documentaries - S01E04 - Dawn Of The Dead - Document Of The Dead"
    match = re.match(r'F[Ii]lm Documentaries\s*-\s*S(\d+)E(\d+)\s*-\s*(.*)', stem)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        title = match.group(3).strip()
        return season, episode, title
    return 1, 0, stem


def extract_audio(video_path, output_dir):
    """Extract 16kHz mono WAV from video file."""
    audio_path = Path(output_dir) / "audio.wav"
    try:
        result = subprocess.run([
            FFMPEG, "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            "-y", str(audio_path)
        ], capture_output=True, timeout=900)
        if audio_path.exists() and audio_path.stat().st_size > 1000:
            return str(audio_path)
        else:
            log(f"  Audio extraction produced empty file (ffmpeg rc={result.returncode})")
    except subprocess.TimeoutExpired:
        log(f"  Audio extraction timed out (900s)")
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


def extract_interesting_facts(transcript, title):
    """Pull out interesting behind-the-scenes facts from documentary transcript."""
    facts = []
    sentences = re.split(r'(?<=[.!?])\s+', transcript)

    # Look for sentences with production/filmmaking keywords
    keywords = [
        r'budget', r'million', r'directed', r'written by', r'producer',
        r'special effect', r'practical effect', r'makeup', r'prosthetic',
        r'box office', r'gross', r'sequel', r'originally', r'first time',
        r'cast', r'audition', r'location', r'shoot', r'filming',
        r'script', r'rewrit', r'studio', r'release', r'premier',
        r'iconic', r'famous', r'inspired', r'influence', r'legacy',
        r'Carpenter', r'Romero', r'Spielberg', r'Craven', r'Raimi',
        r'Cunningham', r'Miller', r'Hooper',
    ]
    pattern = re.compile('|'.join(keywords), re.IGNORECASE)

    for i, sentence in enumerate(sentences):
        if pattern.search(sentence) and len(sentence) > 40 and len(sentence) < 500:
            # Include surrounding context
            context_start = max(0, i - 1)
            context_end = min(len(sentences), i + 2)
            fact_text = " ".join(sentences[context_start:context_end])
            if len(fact_text) > 50:
                facts.append(fact_text[:300])

    # Deduplicate (rough — skip if >70% overlap with existing)
    unique_facts = []
    for f in facts:
        is_dup = False
        for uf in unique_facts:
            overlap = len(set(f.split()) & set(uf.split())) / max(len(f.split()), 1)
            if overlap > 0.7:
                is_dup = True
                break
        if not is_dup:
            unique_facts.append(f)

    return unique_facts[:20]  # cap at 20 best facts per episode


def chunk_transcript(transcript, episode_label):
    """Split transcript into memory-sized chunks."""
    words = transcript.split()
    chunks = []
    current = []
    current_len = 0

    for word in words:
        current.append(word)
        current_len += len(word) + 1
        if current_len >= CHUNK_SIZE:
            chunk_text = " ".join(current)
            chunks.append(f"[Film Documentary: {episode_label}] {chunk_text}")
            current = []
            current_len = 0

    if current:
        chunk_text = " ".join(current)
        if len(chunk_text) > 50:
            chunks.append(f"[Film Documentary: {episode_label}] {chunk_text}")

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

        # Show recent interesting facts
        fact_section = ""
        if stats["recent_facts"]:
            fact_lines = []
            for f in stats["recent_facts"][-3:]:
                fact_lines.append(f"  • _{f[:140]}_")
            fact_section = "\n".join(fact_lines)

        msg = (
            f":movie_camera: *Film Documentaries Ingest Progress*\n"
            f"  Episodes: {stats['processed']}/{stats['total_episodes']} ({pct:.0f}%)\n"
            f"  Chunks stored: {stats['chunks_stored']:,} | Transcript: {stats['total_chars']:,} chars\n"
            f"  Current: _{stats['current_episode']}_\n"
            f"  Elapsed: {str(timedelta(seconds=int(elapsed)))} | ETA: {eta}\n"
            f"  Errors: {stats['errors']}"
        )
        if fact_section:
            msg += f"\n\n  :film_frames: *Recent behind-the-scenes facts:*\n{fact_section}"

        slack_post(msg)
        log(f"Status: {stats['processed']}/{stats['total_episodes']} ({pct:.0f}%), {stats['chunks_stored']} chunks, ETA {eta}")


def discover_episodes():
    """Find all documentary files."""
    episodes = []
    for f in sorted(MEDIA_PATH.iterdir()):
        if f.suffix.lower() in VIDEO_EXTENSIONS and f.is_file():
            season, ep_num, title = parse_episode(f.name)
            episodes.append((season, ep_num, title, f))
    episodes.sort(key=lambda x: (x[0], x[1]))
    return episodes


def ingest_episode(season, episode, title, filepath):
    """Full pipeline for one documentary: extract → transcribe → chunk → store."""
    ep_label = f"{title}"
    stats["current_episode"] = ep_label
    size_mb = filepath.stat().st_size / 1024 / 1024
    log(f"Processing: E{episode:02d} — {title} ({size_mb:.0f} MB)")

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

    # Extract interesting production facts
    facts = extract_interesting_facts(transcript, title)
    if facts:
        stats["recent_facts"] = (stats["recent_facts"] + facts)[-10:]
        log(f"  Extracted {len(facts)} interesting facts")

    # Store episode metadata
    meta_text = (
        f"Film Documentary: \"{title}\" — Behind-the-scenes documentary about the making of {title}. "
        f"Covers production history, special effects, cast and crew interviews, and cultural impact."
    )
    metadata = {
        "type": "film_documentary",
        "show": "Film Documentaries",
        "season": season,
        "episode": episode,
        "title": title,
        "category": "film_history",
        "subcategory": "behind_the_scenes",
        "owner_favorite": True,
    }
    vector_remember(meta_text, metadata)

    # Store extracted facts as dedicated memories
    fact_meta = {
        "type": "film_documentary_fact",
        "show": "Film Documentaries",
        "film": title,
        "category": "film_history",
        "owner_favorite": True,
    }
    for fact in facts:
        fact_text = f"[Documentary: {title}] {fact}"
        vector_remember(fact_text, fact_meta)

    # Chunk and store full transcript
    chunks = chunk_transcript(transcript, ep_label)
    log(f"  Storing {len(chunks)} transcript chunks + {len(facts)} production facts...")

    chunk_meta = {
        "type": "film_documentary_transcript",
        "show": "Film Documentaries",
        "film": title,
        "season": season,
        "episode": episode,
        "category": "film_history",
        "owner_favorite": True,
    }

    for chunk in chunks:
        vector_remember(chunk, chunk_meta)

    stats["processed"] += 1

    # Per-episode notification with extracted facts
    fact_lines = ""
    if facts:
        sample_facts = facts[:3]
        fact_lines = "\n".join(f"  • _{f[:150]}_" for f in sample_facts)

    ep_msg = (
        f":clapper: *Film Documentary Complete: {title}*\n"
        f"  {len(chunks)} transcript chunks + {len(facts)} production facts stored\n"
        f"  Total memories: {stats['chunks_stored']:,}"
    )
    if fact_lines:
        ep_msg += f"\n\n  :film_frames: *Behind-the-scenes:*\n{fact_lines}"
    slack_post(ep_msg)


def main():
    log("Film Documentaries ingest starting...")
    log(f"Source: {MEDIA_PATH}")

    episodes = discover_episodes()
    stats["total_episodes"] = len(episodes)
    stats["start_time"] = time.time()

    if not episodes:
        log("ERROR: No episodes found!")
        slack_post(":x: *Film Documentaries Ingest* — No episodes found at expected path.")
        return

    log(f"Found {len(episodes)} documentaries")

    films_list = ", ".join(ep[2] for ep in episodes[:5]) + "..."
    slack_post(
        f":movie_camera: *Film Documentaries Ingest Started*\n"
        f"  {len(episodes)} documentaries (horror/sci-fi classics)\n"
        f"  Pipeline: ffmpeg audio → MLX Whisper → extract facts → vector memory\n"
        f"  Films: {films_list}\n"
        f"  _Per-episode notifications with behind-the-scenes facts_"
    )

    reporter = Thread(target=status_reporter, daemon=True)
    reporter.start()

    try:
        for season, ep_num, title, filepath in episodes:
            ingest_episode(season, ep_num, title, filepath)
    except KeyboardInterrupt:
        log("Interrupted by user")
    finally:
        shutdown.set()
        reporter.join(timeout=5)

    elapsed = time.time() - stats["start_time"]
    elapsed_str = str(timedelta(seconds=int(elapsed)))

    summary = (
        f":star2: *Film Documentaries Ingest Complete*\n"
        f"  {stats['processed']}/{stats['total_episodes']} documentaries transcribed\n"
        f"  {stats['chunks_stored']:,} total memories stored\n"
        f"  {stats['total_chars']:,} transcript characters\n"
        f"  Errors: {stats['errors']} | Time: {elapsed_str}\n"
        f"  Films covered: Amityville Horror, Evil Dead, Friday the 13th, "
        f"Halloween, Jaws, Mad Max, Nightmare on Elm Street, John Carpenter\n"
        f"  _Nova now has deep behind-the-scenes knowledge of horror/sci-fi classics_"
    )
    slack_post(summary)
    log(summary.replace("*", "").replace("_", "").replace(":star2:", ""))


if __name__ == "__main__":
    main()
