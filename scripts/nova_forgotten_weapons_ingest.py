#!/usr/bin/env python3
"""
nova_forgotten_weapons_ingest.py — Ingest all Forgotten Weapons videos into Nova's memory.

Scans /Volumes/external/videos/TVShows/Forgotten Weapons/, transcribes each
episode with MLX Whisper, chunks into vector memory, and posts a per-episode
notification to #nova-notifications with a random memory from prior FW episodes
and what video is next in queue.

State is persisted so restarts resume where they left off. Videos already
processed are skipped immediately without re-ingesting (fixes "already in memory").

Run manually or via scheduler. Safe to interrupt and resume.

Written by Jordan Koch.
"""

import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
import nova_media_registry as registry

# ── Config ────────────────────────────────────────────────────────────────────

FW_DIR        = Path("/Volumes/external/videos/TVShows/Forgotten Weapons")
VIDEO_EXTS    = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v"}
STATE_FILE    = Path.home() / ".openclaw/workspace/state/fw_ingest_state.json"
WORK_DIR      = Path("/Volumes/Data/nova-livetv/fw-ingest")
LOG_FILE      = Path.home() / ".openclaw/logs/nova_fw_ingest.log"

MEMORY_URL    = "http://127.0.0.1:18790/remember"
RECALL_URL    = "http://127.0.0.1:18790/recall"
SLACK_CHANNEL = "#nova-notifications"

WHISPER_BIN   = "/opt/homebrew/bin/mlx_whisper"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
FFMPEG_BIN    = "/opt/homebrew/bin/ffmpeg"

CHUNK_WORDS     = 400
MIN_CHUNK_WORDS = 10   # FW has many short clips (50-90s) — don't discard real content
TRASH_RATIO     = 0.7
MAX_AUDIO_SECS  = 7200

SHOW_NAME = "Forgotten Weapons"
SOURCE    = "military_history"

TODAY = datetime.now().strftime("%Y-%m-%d")

# ── Garbage detection ─────────────────────────────────────────────────────────

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
_MUSIC_PHRASES = ["♪", "♫", "la la la", "da da da", "na na na", "hmm hmm"]


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
    return alpha / max(len(stripped), 1) < 0.5


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[fw_ingest {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {"done": {}, "last_run": None, "total_ingested": 0}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Video discovery ───────────────────────────────────────────────────────────

def find_videos() -> list[Path]:
    results = []
    for root, _, files in os.walk(FW_DIR):
        for fname in files:
            p = Path(root) / fname
            if p.suffix.lower() in VIDEO_EXTS:
                results.append(p)
    # Sort by episode number embedded in filename (SxxExxxx pattern)
    def sort_key(p: Path):
        m = re.search(r"S(\d+)E(\d+)", p.stem, re.IGNORECASE)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        return (0, 0)
    return sorted(results, key=sort_key)


# ── Audio / Transcription ─────────────────────────────────────────────────────

def extract_audio(video: Path, out_wav: Path) -> bool:
    cmd = [
        FFMPEG_BIN, "-y",
        "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000",
        "-acodec", "pcm_s16le",
        "-t", str(MAX_AUDIO_SECS),
        str(out_wav),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=MAX_AUDIO_SECS + 60)
        return out_wav.exists() and out_wav.stat().st_size > 1000
    except Exception as exc:
        log(f"  ffmpeg error: {exc}")
        return False


def transcribe(wav: Path, stem: str) -> str | None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        WHISPER_BIN, str(wav),
        "--model", WHISPER_MODEL,
        "--output-format", "txt",
        "--output-dir", str(WORK_DIR),
        "--output-name", stem,
        "--language", "en",
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=MAX_AUDIO_SECS * 2)
        txt_path = WORK_DIR / f"{stem}.txt"
        if txt_path.exists():
            text = txt_path.read_text(encoding="utf-8", errors="ignore").strip()
            txt_path.unlink(missing_ok=True)
            return text if len(text) > 20 else None
    except subprocess.TimeoutExpired:
        log("  Whisper timeout — skipping")
    except Exception as exc:
        log(f"  Whisper error: {exc}")
    return None


# ── Memory ────────────────────────────────────────────────────────────────────

def remember(text: str, metadata: dict) -> bool:
    payload = json.dumps({
        "text": text[:2000],
        "source": SOURCE,
        "tier": "long_term",
        "privacy": "local-only",
        "metadata": metadata,
    }).encode()
    req = urllib.request.Request(
        MEMORY_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
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


def recall_fw_memory() -> str | None:
    """Pull a random existing Forgotten Weapons memory for the notification."""
    payload = json.dumps({
        "query": "Forgotten Weapons firearm history Ian McCollum",
        "limit": 30,
    }).encode()
    req = urllib.request.Request(
        RECALL_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            memories = data.get("memories", data.get("results", []))
            if memories:
                m = random.choice(memories)
                text = re.sub(r"^\[.*?\]\s*", "", m.get("text", ""))
                return text[:200].strip()
    except Exception:
        pass
    return None


# ── Slack ─────────────────────────────────────────────────────────────────────

def post_slack(msg: str):
    try:
        nova_config.post_both(msg, slack_channel=SLACK_CHANNEL)
    except Exception as exc:
        log(f"Slack error: {exc}")


# ── Per-video pipeline ────────────────────────────────────────────────────────

def process_video(video: Path, state: dict, next_title: str | None) -> bool:
    path_key = str(video)
    title = video.stem

    # Second-layer dedup: check the nova_media DB (survives state file resets)
    if registry.is_done(path_key):
        state["done"][path_key] = {"title": title, "status": "registry_done"}
        save_state(state)
        return False

    # Clean up episode prefix for display: "S01E0001 - Title" → "Title"
    display_title = re.sub(r"^S\d+E\d+\s*-\s*", "", title, flags=re.IGNORECASE).strip() or title

    # Register in the nova_media DB (no-op if already present)
    registry.register_file(path_key, show_name=SHOW_NAME, title=display_title,
                           source_label=SOURCE, ingest_script="nova_forgotten_weapons_ingest.py")

    # Check if memories already exist for this file in nova_memories by querying
    # the source_file metadata — if so, mark ingested silently and skip
    _prior_chunks = registry.get_status(path_key)
    # Also query nova_memories directly for this source_file
    try:
        import psycopg2 as _pg
        _conn = _pg.connect(dbname="nova_memories")
        _cur = _conn.cursor()
        _cur.execute(
            "SELECT COUNT(*) FROM memories WHERE metadata->>'source_file' = %s",
            (path_key,)
        )
        _existing = _cur.fetchone()[0]
        _conn.close()
        if _existing > 0:
            log(f"  ~ already in nova_memories ({_existing} chunks) — marking done silently")
            registry.mark_ingested(path_key, _existing, SOURCE)
            state["done"][path_key] = {"title": title, "status": "ingested", "chunks": _existing}
            save_state(state)
            return True
    except Exception:
        pass

    log(f"▶ {display_title[:80]}")

    wav_stem = f"fw_{abs(hash(path_key)) % 1_000_000:06d}"
    wav = WORK_DIR / f"{wav_stem}.wav"
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    if not extract_audio(video, wav):
        log(f"  ✗ audio failed")
        state["done"][path_key] = {"title": title, "status": "audio_failed", "chunks": 0}
        save_state(state)
        registry.mark_status(path_key, "audio_failed")
        return False

    transcript = transcribe(wav, wav_stem)
    wav.unlink(missing_ok=True)

    if not transcript:
        log(f"  ✗ no transcript")
        state["done"][path_key] = {"title": title, "status": "no_transcript", "chunks": 0}
        save_state(state)
        registry.mark_status(path_key, "no_transcript")
        return False

    word_count = len(transcript.split())
    chunks = chunk_text(transcript)
    total_raw = max(1, word_count // CHUNK_WORDS)
    trash_ratio = 1 - (len(chunks) / total_raw)

    if trash_ratio > TRASH_RATIO or not chunks:
        log(f"  ✗ garbage ({trash_ratio:.0%})")
        state["done"][path_key] = {"title": title, "status": "trash", "chunks": 0}
        save_state(state)
        registry.mark_status(path_key, "trash")
        return False

    log(f"  ✓ {len(chunks)} chunks — {word_count:,} words")

    ingested = 0
    for i, chunk in enumerate(chunks):
        ok = remember(f"[{SHOW_NAME}] {chunk}", {
            "type": "tv_transcript",
            "show": SHOW_NAME,
            "title": display_title,
            "chunk": i + 1,
            "total_chunks": len(chunks),
            "ingested_date": TODAY,
            "source_file": path_key,
        })
        if ok:
            ingested += 1

    state["done"][path_key] = {
        "title": title,
        "status": "ingested",
        "chunks": ingested,
        "words": word_count,
        "ingested_date": TODAY,
    }
    state["total_ingested"] = state.get("total_ingested", 0) + ingested
    save_state(state)
    registry.mark_ingested(path_key, ingested, SOURCE)

    # Only notify when new memories were actually stored — suppress if all chunks
    # were deduplicated (ingested == 0 means the DB already had this content)
    if ingested > 0:
        memory_snippet = recall_fw_memory()
        if not memory_snippet and chunks:
            memory_snippet = re.sub(r"^\[.*?\]\s*", "", random.choice(chunks))[:200]

        lines = [
            f":gun: *Forgotten Weapons* — _{display_title[:80]}_",
            f":brain: {ingested} new memories · {word_count:,} words",
        ]
        if memory_snippet:
            lines.append(f":thought_balloon: _\"{memory_snippet[:180]}…\"_")
        if next_title:
            clean_next = re.sub(r"^S\d+E\d+\s*-\s*", "", next_title, flags=re.IGNORECASE).strip()
            lines.append(f":arrow_forward: *Next:* _{clean_next[:80]}_")

        post_slack("\n".join(lines))

    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log(f"=== Forgotten Weapons ingest started — {TODAY} ===")
    state = load_state()
    done = state.setdefault("done", {})

    videos = find_videos()
    total = len(videos)
    pending = [v for v in videos if str(v) not in done]

    log(f"Total videos: {total} | Already done: {total - len(pending)} | Pending: {len(pending)}")

    if not pending:
        log("All videos already ingested. Nothing to do.")
        post_slack(f":white_check_mark: *Forgotten Weapons* — All {total} videos already in memory.")
        return

    post_slack(
        f":gun: *Forgotten Weapons Ingest Started*\n"
        f"  {len(pending)} videos to process ({total - len(pending)} already done)\n"
        f"  _Notifying after each episode_"
    )

    processed = 0
    succeeded = 0

    for i, video in enumerate(pending):
        next_video = pending[i + 1] if i + 1 < len(pending) else None
        next_title = next_video.stem if next_video else None

        ok = process_video(video, state, next_title)
        processed += 1
        if ok:
            succeeded += 1
        state["last_run"] = datetime.now().isoformat()

    log(f"=== Done: {succeeded}/{processed} videos ingested. Total chunks: {state.get('total_ingested', 0):,} ===")
    post_slack(
        f":white_check_mark: *Forgotten Weapons Ingest Complete*\n"
        f"  Processed: {processed} videos\n"
        f"  Succeeded: {succeeded}\n"
        f"  Total memories: {state.get('total_ingested', 0):,}"
    )


if __name__ == "__main__":
    main()
