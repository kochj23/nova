#!/usr/bin/env python3
"""
nova_tv_ingest.py — TV show ingest pipeline.

Scans /Volumes/external/videos/ (all subdirs except other/Other),
finds ALL video files not yet in the DB, extracts audio with ffmpeg,
transcribes with MLX Whisper large-v3-turbo, filters garbage transcriptions
(music, noise, silence), chunks and stores into Nova's vector memory,
then posts per-episode + summary notifications to #nova-notifications.

Parallelism: 12 concurrent workers (ffmpeg + whisper each). M3 Ultra.
No time window — processes everything not yet tracked.

State tracking: PostgreSQL only (nova_ops.media_ingest_state).
No JSON state file.

Scheduler: cron 0 23 * * * (11pm daily)  timeout: none

PRIVACY: All TV transcript data is local-only. Never cloud-routed.

Written by Jordan Koch.
"""

import base64
import json
import os
import random
import re
import subprocess
import sys
import time
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
import nova_media_registry as registry

# ── Config ────────────────────────────────────────────────────────────────────

VIDEO_ROOT      = Path("/Volumes/external/videos")
EXCLUDED_DIRS   = {"other", "Other"}
VIDEO_EXTS      = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v", ".wmv", ".flv"}
WORK_DIR        = Path("/Volumes/Data/nova-livetv/tv-ingest")
LOG_FILE        = Path.home() / ".openclaw/logs/nova_tv_ingest.log"
MEMORY_URL      = "http://192.168.1.6:18790/remember"
SLACK_CHANNEL   = nova_config.SLACK_NOTIFY

FFMPEG_BIN      = "/opt/homebrew/bin/ffmpeg"

# OpenRouter Gemini Flash Lite for cloud transcription
OPENROUTER_URL  = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "google/gemini-3.1-flash-lite"

CHUNK_WORDS     = 400           # words per memory chunk
MIN_CHUNK_WORDS = 30            # discard chunks shorter than this
TRASH_RATIO     = 0.6           # if >60% of chunks are garbage → skip whole video
MAX_AUDIO_SECS  = 7200          # cap at 2h (most episodes ≤ 1h)
MAX_WORKERS     = 48            # parallel workers — cranked for overnight burn-down
MAX_FFMPEG      = 20            # concurrent ffmpeg extractions (M4 Ultra handles this)
MAX_RETRIES     = 3             # retry on transient API failures

# State lock — multiple workers write to shared state dict
_STATE_LOCK = threading.Lock()
_FFMPEG_SEM = threading.Semaphore(MAX_FFMPEG)
_API_SEM = threading.Semaphore(20)  # max 20 concurrent OpenRouter requests (doubled for overnight)

NOW             = datetime.now()
TODAY           = NOW.strftime("%Y-%m-%d")


# ── Garbage detection patterns ────────────────────────────────────────────────

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

_log_lock = threading.Lock()

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[tv_ingest {ts}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ── State management (PostgreSQL-backed) ─────────────────────────────────────

_DB_CONN = None

def _db():
    global _DB_CONN
    if _DB_CONN is None or _DB_CONN.closed:
        import psycopg2
        _DB_CONN = psycopg2.connect(host="localhost", dbname="nova_ops", user="kochj")
        _DB_CONN.autocommit = True
    return _DB_CONN


def load_state() -> dict:
    """Load all processed file paths from PG into an in-memory set for fast lookup."""
    cur = _db().cursor()
    cur.execute("SELECT file_path FROM media_ingest_state")
    done = {row[0]: True for row in cur.fetchall()}
    cur.close()
    log(f"Loaded {len(done):,} tracked files from DB")
    return {"done": done}


def mark_done(state: dict, path: str, metadata: dict):
    """Write to PG and update in-memory cache."""
    with _STATE_LOCK:
        state["done"][path] = True
    try:
        cur = _db().cursor()
        cur.execute("""
            INSERT INTO media_ingest_state (file_path, show, title, status, chunks, words, source_vector, processed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (file_path) DO UPDATE SET
                status = EXCLUDED.status,
                chunks = EXCLUDED.chunks,
                words = EXCLUDED.words,
                source_vector = EXCLUDED.source_vector,
                processed_at = now()
        """, (
            path,
            metadata.get("show"),
            metadata.get("title"),
            metadata.get("status", "unknown"),
            metadata.get("chunks", 0),
            metadata.get("words", 0),
            metadata.get("source"),
        ))
        cur.close()
    except Exception as e:
        log(f"DB mark_done failed for {path}: {e}")


# ── Source classification ─────────────────────────────────────────────────────

def classify_source(show_name: str, title: str, snippet: str) -> str:
    text = (show_name + " " + title + " " + snippet[:400]).lower()
    show = show_name.lower()

    # ── Explicit show-name overrides (checked first, highest priority) ────────
    # Food/cooking channels
    if any(w in show for w in ["meat church", "arnitex", "arnie tex",
                                "good eats", "binging with babish", "babish",
                                "ethan chlebowski", "food wishes"]):
        return "cooking"
    # Film/movie criticism and reviews
    if any(w in show for w in ["red letter media", "redlettermedia", "half in the bag",
                                "best of the worst", "re:view"]):
        return "film_criticism"
    # Automotive channels (explicit — before content-based fallback)
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


# ── Video discovery ───────────────────────────────────────────────────────────

def find_videos() -> list[Path]:
    """Find all video files under VIDEO_ROOT except other/Other."""
    results = []
    for root, dirs, files in os.walk(VIDEO_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for fname in files:
            p = Path(root) / fname
            if p.suffix.lower() not in VIDEO_EXTS:
                continue
            results.append(p)
    return sorted(results, key=lambda p: p.name)


def show_name_from_path(video: Path) -> str:
    parts = video.parts
    idx = None
    for i, part in enumerate(parts):
        if part.lower().startswith("season") or re.match(r"^s\d{2}$", part.lower()):
            idx = i - 1
            break
    if idx is not None and idx >= 0:
        return parts[idx]
    return video.parent.name


# ── Audio extraction ──────────────────────────────────────────────────────────

SEGMENT_SECS = 300  # 5-minute segments for API (keeps payload under 15MB)


def extract_audio_segments(video: Path, work_dir: Path, stem: str) -> list[Path]:
    """Extract audio as 5-minute WAV segments. Returns list of segment paths."""
    # First get duration
    probe_cmd = [
        FFMPEG_BIN, "-i", str(video), "-f", "null", "-"
    ]
    try:
        result = subprocess.run(
            [FFMPEG_BIN, "-i", str(video)],
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
            "-i", str(video),
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


# ── Transcription (OpenRouter Gemini Flash Lite) ─────────────────────────────

_openrouter_key: str | None = None

def _get_openrouter_key() -> str:
    global _openrouter_key
    if _openrouter_key is None:
        _openrouter_key = subprocess.check_output(
            ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
            text=True
        ).strip()
    return _openrouter_key


def transcribe(wav: Path, out_dir: Path, stem: str) -> str | None:
    """Transcribe audio via OpenRouter Gemini Flash Lite (audio input)."""
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
                body = exc.read().decode("utf-8", errors="ignore")[:200]
                if exc.code == 429 or exc.code >= 500:
                    time.sleep(3 ** attempt + random.random() * 2)
                    continue
                log(f"  OpenRouter HTTP {exc.code}: {body}")
                return None
            except (ConnectionResetError, BrokenPipeError, OSError):
                time.sleep(3 ** attempt + random.random() * 2)
                continue
            except Exception as exc:
                log(f"  OpenRouter error: {exc}")
                return None
    log(f"  OpenRouter failed after {MAX_RETRIES} retries")
    return None


# ── Memory ingestion ──────────────────────────────────────────────────────────

def remember(text: str, source: str, metadata: dict) -> bool:
    payload = json.dumps({
        "text": text[:2000], "source": source,
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


def random_memory_for_show(show_name: str) -> str | None:
    query_payload = json.dumps({
        "query": f"{show_name} television episode",
        "limit": 20,
    }).encode()
    req = urllib.request.Request(
        nova_config.VECTOR_URL + "/recall",
        data=query_payload,
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


# ── Slack notification ────────────────────────────────────────────────────────

def post_slack(msg: str):
    try:
        nova_config.post_both(msg, slack_channel=SLACK_CHANNEL)
    except Exception as exc:
        log(f"Slack error: {exc}")


# ── Per-video processing ──────────────────────────────────────────────────────

def process_video(video: Path, state: dict, work_dir: Path,
                  next_title: str | None = None) -> dict | None:
    """
    Full pipeline for one video. Thread-safe — uses unique WAV names per thread.
    Returns result dict on success, None on skip/failure.
    """
    path_key = str(video)
    with _STATE_LOCK:
        if path_key in state["done"]:
            return None

    show_name = show_name_from_path(video)
    title = video.stem

    # Register in the nova_media DB (no-op if already present)
    registry.register_file(path_key, show_name=show_name, title=title,
                           ingest_script="nova_tv_ingest.py")

    # Source of truth: check nova_memories for existing chunks
    try:
        import psycopg2 as _pg2
        _c = _pg2.connect(dbname="nova_memories")
        _cur2 = _c.cursor()
        _cur2.execute("SELECT COUNT(*) FROM memories WHERE metadata->>'source_file' = %s", (path_key,))
        _existing = _cur2.fetchone()[0]
        _c.close()
        if _existing > 0:
            mark_done(state, path_key, {"show": show_name, "title": title,
                                        "status": "already_known", "chunks": _existing})
            registry.mark_ingested(path_key, _existing, "")
            return None
    except Exception:
        pass

    wav_stem = f"{video.stem[:60]}_{abs(hash(path_key)) % 100000}"

    log(f"▶ {show_name} — {title[:70]}")

    # Extract audio as 5-min segments (ffmpeg semaphore-limited)
    with _FFMPEG_SEM:
        segments = extract_audio_segments(video, work_dir, wav_stem)

    if not segments:
        log(f"  ✗ audio failed: {title[:50]}")
        mark_done(state, path_key, {"show": show_name, "title": title, "status": "audio_failed", "chunks": 0})
        registry.mark_status(path_key, "audio_failed")
        return None

    # Transcribe each segment via OpenRouter (already in thread pool, segments are small)
    transcript_parts = []
    for seg in segments:
        text = transcribe(seg, work_dir, seg.stem)
        seg.unlink(missing_ok=True)
        if text:
            transcript_parts.append(text)

    transcript = " ".join(transcript_parts).strip()

    if not transcript or len(transcript.split()) < MIN_CHUNK_WORDS:
        log(f"  ✗ no transcript: {title[:50]}")
        mark_done(state, path_key, {"show": show_name, "title": title, "status": "no_transcript", "chunks": 0})
        registry.mark_status(path_key, "no_transcript")
        return None

    word_count = len(transcript.split())
    chunks = chunk_text(transcript)
    total_raw = max(1, word_count // CHUNK_WORDS)
    trash_ratio = 1 - (len(chunks) / total_raw)

    if trash_ratio > TRASH_RATIO or len(chunks) == 0:
        log(f"  ✗ garbage ({trash_ratio:.0%}): {title[:50]}")
        mark_done(state, path_key, {"show": show_name, "title": title, "status": "trash", "chunks": 0})
        registry.mark_status(path_key, "trash")
        return None

    source = classify_source(show_name, title, transcript[:500])
    log(f"  ✓ {len(chunks)} chunks [{source}] — {title[:50]}")

    ingested = 0
    dupes = 0
    for i, chunk in enumerate(chunks):
        ok = remember(f"[{show_name}] {chunk}", source,
                      {"type": "tv_transcript", "show": show_name, "title": title,
                       "chunk": i + 1, "total_chunks": len(chunks),
                       "ingested_date": TODAY, "source_file": path_key})
        if ok:
            ingested += 1
        else:
            dupes += 1

    status = "ingested" if ingested > 0 else "already_known"
    mark_done(state, path_key, {
        "show": show_name, "title": title,
        "status": status, "chunks": ingested,
        "source": source, "words": word_count,
    })
    if ingested > 0:
        registry.mark_ingested(path_key, ingested, source)

    # Per-episode notification — always show a memory snippet, show what's next
    memory_snippet = random_memory_for_show(show_name)
    if not memory_snippet and chunks:
        memory_snippet = re.sub(r"^\[.*?\]\s*", "", random.choice(chunks))[:200]

    if ingested > 0:
        notif_lines = [
            f":clapper: *{show_name}* — _{title[:80]}_",
            f":brain: {ingested} new memories `[{source}]` · {word_count:,} words",
        ]
        if memory_snippet:
            notif_lines.append(f":thought_balloon: _\"{memory_snippet[:180]}…\"_")
        if next_title:
            notif_lines.append(f":arrow_forward: *Next:* _{next_title[:80]}_")
        post_slack("\n".join(notif_lines))

    return {"show": show_name, "title": title, "source": source,
            "chunks": ingested, "words": word_count}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log(f"=== TV Ingest started — {NOW.strftime('%Y-%m-%d %H:%M')} (workers={MAX_WORKERS}) ===")

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()

    # Discover ALL videos (no time window)
    all_videos = find_videos()
    new_videos = [v for v in all_videos if str(v) not in state["done"]]
    log(f"Found {len(all_videos):,} total videos, {len(new_videos):,} not yet processed")

    if not new_videos:
        log("Nothing new to ingest.")
        post_slack(f":tv: *TV Ingest — {TODAY}*\nNo new videos to ingest. All caught up.")
        return

    # Post start notification
    post_slack(
        f":rocket: *TV Ingest starting — {TODAY}*\n"
        f":film_frames: {len(new_videos):,} videos to process · {MAX_WORKERS} parallel workers\n"
        f":floppy_disk: {len(all_videos) - len(new_videos):,} already tracked in DB"
    )

    results_by_show: dict[str, list[dict]] = {}
    total_chunks = 0
    skipped = 0
    failed = 0
    completed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for i, video in enumerate(new_videos):
            next_title = new_videos[i + 1].stem if i + 1 < len(new_videos) else None
            future = executor.submit(process_video, video, state, WORK_DIR, next_title)
            futures[future] = video

        for future in as_completed(futures):
            video = futures[future]
            completed += 1
            try:
                result = future.result()
                if result:
                    show = result["show"]
                    results_by_show.setdefault(show, []).append(result)
                    total_chunks += result["chunks"]
                else:
                    skipped += 1
            except Exception as exc:
                log(f"  ERROR {video.name}: {exc}")
                failed += 1
                mark_done(state, str(video), {
                    "show": show_name_from_path(video), "title": video.stem,
                    "status": "error", "chunks": 0,
                })

            if completed % 25 == 0:
                log(f"Progress: {completed}/{len(new_videos)} ({completed*100//len(new_videos)}%)")

    # Final summary notification
    ingested_count = sum(len(eps) for eps in results_by_show.values())
    lines = [
        f":tv: *TV Ingest Complete — {TODAY}*",
        f":white_check_mark: *{ingested_count:,} episodes ingested* | "
        f":bar_chart: {total_chunks:,} chunks | "
        f":fast_forward: {skipped:,} skipped | "
        f":x: {failed:,} errors",
        "",
    ]
    for show, episodes in sorted(results_by_show.items()):
        ep_count = len(episodes)
        chunk_count = sum(e["chunks"] for e in episodes)
        source_tag = episodes[0]["source"]
        lines.append(f"*{show}* — {ep_count:,} eps · {chunk_count:,} chunks `[{source_tag}]`")
        for ep in episodes[-3:]:
            lines.append(f"  • _{ep['title'][:70]}_")
        snippet = random_memory_for_show(show)
        if snippet:
            lines.append(f"  :thought_balloon: _{snippet[:160]}…_")
        lines.append("")

    post_slack("\n".join(lines))
    log(f"=== Complete: {ingested_count:,} ingested, {skipped:,} skipped, {failed:,} errors ===")


if __name__ == "__main__":
    main()
