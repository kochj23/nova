#!/usr/bin/env python3
"""
nova_tv_retry_failed.py — Retry transcription for files that have audio but failed.

Queries media_ingest_state for files with status 'no_transcript' or 'audio_failed'
that still exist on disk AND have a valid audio stream. Re-processes them using
OpenRouter Gemini Flash Lite (same as nova_tv_ingest.py).

Usage:
    python3 nova_tv_retry_failed.py [--limit N] [--dry-run]

Written by Jordan Koch.
"""

import argparse
import base64
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

WORK_DIR        = Path("/Volumes/Data/nova-livetv/tv-retry")
LOG_FILE        = Path.home() / ".openclaw/logs/nova_tv_retry.log"
MEMORY_URL      = "http://192.168.1.6:18790/remember"
FFMPEG_BIN      = "/opt/homebrew/bin/ffmpeg"
FFPROBE_BIN     = "/opt/homebrew/bin/ffprobe"

OPENROUTER_URL   = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "google/gemini-3.1-flash-lite"

CHUNK_WORDS     = 400
MIN_CHUNK_WORDS = 30
TRASH_RATIO     = 0.6
MAX_AUDIO_SECS  = 7200
MAX_WORKERS     = 24
MAX_FFMPEG      = 12
MAX_RETRIES     = 3
SEGMENT_SECS    = 300

TODAY = datetime.now().strftime("%Y-%m-%d")

_log_lock   = threading.Lock()
_FFMPEG_SEM = threading.Semaphore(MAX_FFMPEG)
_API_SEM    = threading.Semaphore(20)

# ── Garbage detection (same as nova_tv_ingest.py) ─────────────────────────────

_TRASH_PATTERNS = [
    re.compile(r"[♪♫♬♩]"),
    re.compile(r"\b(\w+)\s+\1\s+\1\s+\1", re.IGNORECASE),
    re.compile(r"^[A-Z\s\W]{20,}$"),
    re.compile(r"^[^aeiouAEIOU\s]{8,}$"),
    re.compile(r"^[\W\d\s]+$"),
    re.compile(r"subtitles?\s+by|transcribed\s+by|closed\s+caption", re.IGNORECASE),
    re.compile(r"^\[?\s*(silence|music|applause|laughter|cheering|crowd|♪)\s*\]?$", re.IGNORECASE),
    re.compile(r"(.{5,}?)(\s+\1){4,}"),
]
_MUSIC_PHRASES = ["♪", "♫", "la la la", "da da da", "na na na", "hmm hmm", "mmm mmm", "woo woo"]


def is_trash_chunk(text: str) -> bool:
    stripped = text.strip()
    if len(stripped.split()) < MIN_CHUNK_WORDS:
        return True
    for pat in _TRASH_PATTERNS:
        if pat.search(stripped):
            return True
    lower = stripped.lower()
    for phrase in _MUSIC_PHRASES:
        if lower.count(phrase) >= 3:
            return True
    alpha = sum(c.isalpha() for c in stripped)
    if alpha / max(len(stripped), 1) < 0.5:
        return True
    return False


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[tv_retry {ts}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ── DB ────────────────────────────────────────────────────────────────────────

def get_retriable_files(limit: int = 0) -> list[dict]:
    """Get files that failed transcription from the DB."""
    import psycopg2
    conn = psycopg2.connect(host="localhost", dbname="nova_ops", user="kochj")
    cur = conn.cursor()
    query = """
        SELECT id, file_path, show, title
        FROM media_ingest_state
        WHERE status IN ('no_transcript', 'audio_failed')
        ORDER BY id
    """
    if limit > 0:
        query += f" LIMIT {limit}"
    cur.execute(query)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{"id": r[0], "file_path": r[1], "show": r[2], "title": r[3]} for r in rows]


def update_status(file_path: str, status: str, chunks: int, words: int, source_vector: str):
    """Update a file's status in the DB."""
    import psycopg2
    conn = psycopg2.connect(host="localhost", dbname="nova_ops", user="kochj")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        UPDATE media_ingest_state
        SET status = %s, chunks = %s, words = %s, source_vector = %s, processed_at = now()
        WHERE file_path = %s
    """, (status, chunks, words, source_vector, file_path))
    cur.close()
    conn.close()


# ── File validation ───────────────────────────────────────────────────────────

def file_has_audio(path: str) -> bool:
    """Check if file exists and has an audio stream."""
    if not os.path.isfile(path):
        return False
    try:
        result = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


# ── Audio extraction ──────────────────────────────────────────────────────────

def extract_audio_segments(video_path: str, work_dir: Path, stem: str) -> list[Path]:
    """Extract audio as 5-minute WAV segments."""
    try:
        result = subprocess.run(
            [FFMPEG_BIN, "-i", video_path],
            capture_output=True, text=True, timeout=30
        )
        duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+)", result.stderr)
        if duration_match:
            h, m, s = int(duration_match.group(1)), int(duration_match.group(2)), int(duration_match.group(3))
            total_secs = min(h * 3600 + m * 60 + s, MAX_AUDIO_SECS)
        else:
            total_secs = MAX_AUDIO_SECS
    except Exception:
        total_secs = MAX_AUDIO_SECS

    segments = []
    for start in range(0, total_secs, SEGMENT_SECS):
        seg_path = work_dir / f"{stem}_seg{start:05d}.wav"
        cmd = [
            FFMPEG_BIN, "-y",
            "-ss", str(start),
            "-i", video_path,
            "-t", str(SEGMENT_SECS),
            "-vn", "-ac", "1", "-ar", "16000",
            "-acodec", "pcm_s16le",
            str(seg_path),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=120)
            if seg_path.exists() and seg_path.stat().st_size > 1000:
                segments.append(seg_path)
        except Exception:
            pass

    return segments


# ── Transcription (OpenRouter) ────────────────────────────────────────────────

_openrouter_key: str | None = None


def _get_openrouter_key() -> str:
    global _openrouter_key
    if _openrouter_key is None:
        _openrouter_key = subprocess.check_output(
            ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
            text=True
        ).strip()
    return _openrouter_key


def transcribe_segment(wav: Path) -> str | None:
    """Transcribe a WAV segment via OpenRouter Gemini Flash Lite."""
    try:
        audio_b64 = base64.b64encode(wav.read_bytes()).decode("ascii")
    except Exception as exc:
        log(f"  Failed to read WAV: {exc}")
        return None

    payload = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": audio_b64,
                            "format": "wav"
                        }
                    },
                    {
                        "type": "text",
                        "text": "Transcribe this audio verbatim. Output ONLY the spoken words, no timestamps, no speaker labels, no descriptions of sounds or music. If there is no speech, respond with EMPTY."
                    }
                ]
            }
        ],
        "max_tokens": 16000,
        "temperature": 0.0,
    }).encode()

    for attempt in range(MAX_RETRIES):
        with _API_SEM:
            try:
                req = urllib.request.Request(
                    OPENROUTER_URL, data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {_get_openrouter_key()}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=300) as resp:
                    data = json.loads(resp.read())
                    text = data["choices"][0]["message"]["content"].strip()
                    if text == "EMPTY" or len(text) < 20:
                        return None
                    return text
            except urllib.error.HTTPError as exc:
                if exc.code == 429 or exc.code >= 500:
                    time.sleep(3 ** attempt + random.random() * 2)
                    continue
                body = exc.read().decode("utf-8", errors="ignore")[:200]
                log(f"  OpenRouter HTTP {exc.code}: {body}")
                return None
            except (ConnectionResetError, BrokenPipeError, OSError):
                time.sleep(3 ** attempt + random.random() * 2)
                continue
            except Exception as exc:
                log(f"  OpenRouter error: {exc}")
                return None
    return None


# ── Source classification (same as nova_tv_ingest.py) ─────────────────────────

def classify_source(show_name: str, title: str, snippet: str) -> str:
    text = (show_name + " " + title + " " + snippet[:400]).lower()
    show = show_name.lower()

    if any(w in show for w in ["meat church", "arnitex", "arnie tex",
                                "good eats", "binging with babish", "babish",
                                "ethan chlebowski", "food wishes"]):
        return "cooking"
    if any(w in show for w in ["red letter media", "redlettermedia", "half in the bag",
                                "best of the worst", "re:view"]):
        return "film_criticism"
    if any(w in show for w in ["vin_tra", "vin tra", "rob dahm", "jason cammisa",
                                "jay leno", "jasoncommisa"]):
        return "automotive"
    if any(w in show for w in ["forgotten weapon", "forbidden weapon"]):
        return "military_history"
    if any(w in show for w in ["jeopardy", "wheel of fortune", "game show", "price is right"]):
        return "game_show"
    if any(w in show for w in ["crash course", "crashcourse"]):
        return "education"
    if any(w in show for w in ["documentary", "biography", "civilizations", "connections",
                                "nova ", "frontline", "american experience"]):
        return "documentary"
    if any(w in show for w in ["car", "auto", "garage", "engine", "motor", "mustang",
                                "corvette", "racing", "drift", "truck", "wheels", "horsepower",
                                "finnegan", "car wizard", "chasing classic", "dream car",
                                "build or bust", "car craft"]):
        return "automotive"
    if any(w in show for w in ["combat", "war", "battle", "military", "bonanza", "western",
                                "cannon", "batman", "21 jump"]):
        return "crime_drama"
    if any(w in show for w in ["cooking", "pepin", "kitchen", "chef", "recipe", "food"]):
        return "education"
    if any(w in show for w in ["louis ck", "comedy", "standup", "stand-up", "chug"]):
        return "comedy"

    if any(w in text for w in ["firearm", "rifle", "pistol", "shotgun", "cartridge",
                                "caliber", "ammunition", "magazine", "barrel", "trigger"]):
        return "military_history"
    if any(w in text for w in ["horsepower", "torque", "carburetor", "engine", "transmission",
                                "differential", "chassis", "dyno", "lap time", "drag strip"]):
        return "automotive"
    if any(w in text for w in ["history", "war", "battle", "ancient", "civilization", "empire",
                                "century", "dynasty", "revolution"]):
        return "documentary"
    if any(w in text for w in ["joke", "laugh", "funny", "comedian", "crowd", "audience"]):
        return "comedy"
    return "television"


# ── Memory ingestion ──────────────────────────────────────────────────────────

def remember(text: str, source: str, metadata: dict) -> bool:
    payload = json.dumps({
        "text": nova_config.truncate_at_boundary(text), "source": source,
        "tier": "long_term", "privacy": "local-only",
        "metadata": metadata,
    }).encode()
    req = urllib.request.Request(
        MEMORY_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception:
        return False


def chunk_text(text: str) -> list[str]:
    words = text.split()
    chunks = []
    for i in range(0, len(words), CHUNK_WORDS):
        chunk = " ".join(words[i:i + CHUNK_WORDS])
        if not is_trash_chunk(chunk):
            chunks.append(chunk)
    return chunks


# ── Per-file processing ───────────────────────────────────────────────────────

def process_file(entry: dict, work_dir: Path) -> dict:
    """Process a single retriable file. Returns result dict."""
    file_path = entry["file_path"]
    show_name = entry["show"] or Path(file_path).parent.name
    title = entry["title"] or Path(file_path).stem

    if not file_has_audio(file_path):
        return {"path": file_path, "status": "skip", "reason": "missing_or_no_audio"}

    wav_stem = f"retry_{abs(hash(file_path)) % 100000}"
    log(f"▶ {show_name} — {title[:70]}")

    with _FFMPEG_SEM:
        segments = extract_audio_segments(file_path, work_dir, wav_stem)

    if not segments:
        log(f"  ✗ audio extraction failed: {title[:50]}")
        return {"path": file_path, "status": "audio_failed", "reason": "extraction_failed"}

    transcript_parts = []
    for seg in segments:
        text = transcribe_segment(seg)
        seg.unlink(missing_ok=True)
        if text:
            transcript_parts.append(text)

    transcript = " ".join(transcript_parts).strip()

    if not transcript or len(transcript.split()) < MIN_CHUNK_WORDS:
        log(f"  ✗ no transcript after retry: {title[:50]}")
        update_status(file_path, "no_transcript", 0, 0, "")
        return {"path": file_path, "status": "no_transcript", "reason": "empty_after_retry"}

    word_count = len(transcript.split())
    chunks = chunk_text(transcript)
    total_raw = max(1, word_count // CHUNK_WORDS)
    trash_ratio = 1 - (len(chunks) / total_raw)

    if trash_ratio > TRASH_RATIO or len(chunks) == 0:
        log(f"  ✗ garbage ({trash_ratio:.0%}): {title[:50]}")
        update_status(file_path, "trash", 0, word_count, "")
        return {"path": file_path, "status": "trash", "reason": f"garbage_{trash_ratio:.0%}"}

    source = classify_source(show_name, title, transcript[:500])
    log(f"  ✓ {len(chunks)} chunks [{source}] — {title[:50]}")

    ingested = 0
    for i, chunk in enumerate(chunks):
        ok = remember(f"[{show_name}] {chunk}", source,
                      {"type": "tv_transcript", "show": show_name, "title": title,
                       "chunk": i + 1, "total_chunks": len(chunks),
                       "ingested_date": TODAY, "source_file": file_path})
        if ok:
            ingested += 1

    update_status(file_path, "ingested", ingested, word_count, source)
    return {"path": file_path, "status": "ingested", "chunks": ingested, "words": word_count}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Retry failed transcriptions via OpenRouter")
    parser.add_argument("--limit", type=int, default=0, help="Max files to process (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="Just show what would be retried")
    args = parser.parse_args()

    log("=" * 60)
    log("Nova TV Retry — re-processing failed transcriptions via OpenRouter")
    log("=" * 60)

    files = get_retriable_files(args.limit)
    log(f"Found {len(files)} retriable files in DB")

    if args.dry_run:
        existing = 0
        for f in files:
            if file_has_audio(f["file_path"]):
                existing += 1
        log(f"Dry run: {existing} files exist with audio on disk")
        return

    WORK_DIR.mkdir(parents=True, exist_ok=True)

    results = {"ingested": 0, "no_transcript": 0, "trash": 0, "audio_failed": 0, "skip": 0}
    total_chunks = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process_file, entry, WORK_DIR): entry for entry in files}

        for future in as_completed(futures):
            try:
                result = future.result()
                status = result["status"]
                results[status] = results.get(status, 0) + 1
                if status == "ingested":
                    total_chunks += result.get("chunks", 0)
            except Exception as exc:
                log(f"Worker error: {exc}")
                results["skip"] = results.get("skip", 0) + 1

    log("=" * 60)
    log(f"DONE. Results:")
    log(f"  Ingested:       {results['ingested']} ({total_chunks} chunks)")
    log(f"  No transcript:  {results['no_transcript']}")
    log(f"  Trash:          {results['trash']}")
    log(f"  Audio failed:   {results['audio_failed']}")
    log(f"  Skipped:        {results['skip']}")
    log("=" * 60)

    nova_config.post_both(
        f"*TV Retry Complete*\n"
        f"• Ingested: {results['ingested']} files ({total_chunks} chunks)\n"
        f"• Still failed: {results['no_transcript']} no transcript, {results['trash']} trash\n"
        f"• Skipped: {results['skip']} (missing/no audio)",
        slack_channel=nova_config.SLACK_NOTIFY,
    )


if __name__ == "__main__":
    main()
