#!/usr/bin/env python3
"""
nova_tv_ingest.py — Nightly TV show ingest pipeline.

Scans /Volumes/external/videos/ (all subdirs except other/Other),
finds video files modified in the last 5 days that haven't been ingested,
extracts audio with ffmpeg, transcribes with MLX Whisper large-v3-turbo,
filters garbage transcriptions (music, noise, silence), chunks and stores
into Nova's vector memory, then posts a per-episode + summary notification
to #nova-notifications.

Parallelism: 4 concurrent workers (ffmpeg + whisper each). Safe on M3 Ultra.
No delays between videos — go as fast as possible.

State tracking: ~/.openclaw/workspace/state/tv_ingest_state.json
  - Tracks every file that has been processed (path → metadata)
  - Any file older than 5 days on first-run is marked done without processing

Scheduler: cron 0 23 * * * (11pm daily)  timeout: 28800 (8h)

PRIVACY: All TV transcript data is local-only. Never cloud-routed.

Written by Jordan Koch.
"""

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
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
import nova_media_registry as registry

# ── Config ────────────────────────────────────────────────────────────────────

VIDEO_ROOT      = Path("/Volumes/external/videos")
EXCLUDED_DIRS   = {"other", "Other"}
VIDEO_EXTS      = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v", ".wmv", ".flv"}
STATE_FILE      = Path.home() / ".openclaw/workspace/state/tv_ingest_state.json"
WORK_DIR        = Path("/Volumes/Data/nova-livetv/tv-ingest")
LOG_FILE        = Path.home() / ".openclaw/logs/nova_tv_ingest.log"
MEMORY_URL      = "http://192.168.1.6:18790/remember"
SLACK_CHANNEL   = nova_config.SLACK_NOTIFY

WHISPER_BIN     = "/opt/homebrew/bin/mlx_whisper"
WHISPER_MODEL   = "mlx-community/whisper-large-v3-turbo"
FFMPEG_BIN      = "/opt/homebrew/bin/ffmpeg"

CHUNK_WORDS     = 400           # words per memory chunk
MIN_CHUNK_WORDS = 30            # discard chunks shorter than this
TRASH_RATIO     = 0.6           # if >60% of chunks are garbage → skip whole video
RECENT_DAYS     = 5             # ingest files modified within this window
MAX_AUDIO_SECS  = 7200          # cap at 2h (most episodes ≤ 1h)
MAX_WORKERS     = 4             # parallel whisper+ffmpeg workers (safe on M3 Ultra)

# State lock — multiple workers write to shared state dict
_STATE_LOCK = threading.Lock()

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


# ── State management ──────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {"done": {}, "last_run": None}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def mark_done(state: dict, path: str, metadata: dict):
    with _STATE_LOCK:
        state["done"][path] = {**metadata, "marked_at": datetime.now().isoformat()}


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

def find_videos(cutoff: datetime) -> list[Path]:
    results = []
    for root, dirs, files in os.walk(VIDEO_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for fname in files:
            p = Path(root) / fname
            if p.suffix.lower() not in VIDEO_EXTS:
                continue
            try:
                if datetime.fromtimestamp(p.stat().st_mtime) >= cutoff:
                    results.append(p)
            except OSError:
                pass
    return sorted(results, key=lambda p: p.stat().st_mtime)


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


# ── Transcription ─────────────────────────────────────────────────────────────

def transcribe(wav: Path, out_dir: Path, stem: str) -> str | None:
    cmd = [
        WHISPER_BIN, str(wav),
        "--model", WHISPER_MODEL,
        "--output-format", "txt",
        "--output-dir", str(out_dir),
        "--output-name", stem,
        "--language", "en",
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=MAX_AUDIO_SECS * 2)
        txt_path = out_dir / f"{stem}.txt"
        if txt_path.exists():
            text = txt_path.read_text(encoding="utf-8", errors="ignore").strip()
            txt_path.unlink(missing_ok=True)
            return text if len(text) > 20 else None
    except subprocess.TimeoutExpired:
        log("  Whisper timeout — skipping")
    except Exception as exc:
        log(f"  Whisper error: {exc}")
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

    # Second-layer dedup: check the nova_media DB (survives state file resets)
    if registry.is_done(path_key):
        with _STATE_LOCK:
            state["done"][path_key] = {"status": "registry_done", "marked_at": datetime.now().isoformat()}
        return None

    show_name = show_name_from_path(video)
    title = video.stem

    # Register in the nova_media DB (no-op if already present)
    registry.register_file(path_key, show_name=show_name, title=title,
                           ingest_script="nova_tv_ingest.py")

    # Check nova_memories directly — if memories already exist for this file,
    # mark done silently without transcribing or notifying
    try:
        import psycopg2 as _pg2
        _c = _pg2.connect(dbname="nova_memories")
        _cur2 = _c.cursor()
        _cur2.execute("SELECT COUNT(*) FROM memories WHERE metadata->>'source_file' = %s", (path_key,))
        _existing = _cur2.fetchone()[0]
        _c.close()
        if _existing > 0:
            log(f"  ~ already in nova_memories ({_existing} chunks) — skipping silently")
            mark_done(state, path_key, {"show": show_name, "title": title,
                                        "status": "ingested", "chunks": _existing})
            registry.mark_ingested(path_key, _existing, "")
            return None
    except Exception:
        pass

    # Unique WAV name per video to avoid collisions between parallel workers
    wav_stem = f"{video.stem[:60]}_{abs(hash(path_key)) % 100000}"
    wav = work_dir / f"{wav_stem}.wav"

    log(f"▶ {show_name} — {title[:70]}")

    if not extract_audio(video, wav):
        log(f"  ✗ audio failed: {title[:50]}")
        mark_done(state, path_key, {"show": show_name, "title": title, "status": "audio_failed", "chunks": 0})
        registry.mark_status(path_key, "audio_failed")
        return None

    transcript = transcribe(wav, work_dir, wav_stem)
    wav.unlink(missing_ok=True)

    if not transcript:
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
    cutoff = NOW - timedelta(days=RECENT_DAYS)

    # Discover videos within window
    all_videos = find_videos(cutoff)
    new_videos = [v for v in all_videos if str(v) not in state["done"]]
    log(f"Found {len(all_videos)} videos in last {RECENT_DAYS} days, {len(new_videos)} not yet ingested")

    if not new_videos:
        log("Nothing new to ingest.")
        state["last_run"] = NOW.isoformat()
        save_state(state)
        post_slack(f":tv: *TV Ingest — {TODAY}*\nNo new videos to ingest. All caught up.")
        return

    # Backfill: mark everything older than the window as done (no processing)
    backfill_count = 0
    all_existing = find_videos(datetime(2000, 1, 1))
    for v in all_existing:
        path_key = str(v)
        if path_key not in state["done"]:
            mtime = datetime.fromtimestamp(v.stat().st_mtime)
            if mtime < cutoff:
                state["done"][path_key] = {
                    "show": show_name_from_path(v), "title": v.stem,
                    "status": "backfilled", "marked_at": NOW.isoformat(),
                }
                backfill_count += 1
    if backfill_count:
        log(f"Backfilled {backfill_count:,} older files as done")
        save_state(state)

    # Post start notification
    post_slack(
        f":rocket: *TV Ingest starting — {TODAY}*\n"
        f":film_frames: {len(new_videos):,} videos to process · {MAX_WORKERS} parallel workers\n"
        f":calendar: Window: last {RECENT_DAYS} days"
    )

    results_by_show: dict[str, list[dict]] = {}
    total_chunks = 0
    skipped = 0
    failed = 0
    completed = 0

    # Process in parallel — pass next video title for notification context
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
                    "status": "error", "error": str(exc),
                })

            # Save state every 10 completions to avoid constant I/O
            if completed % 10 == 0:
                with _STATE_LOCK:
                    save_state(state)
                log(f"Progress: {completed}/{len(new_videos)} ({completed*100//len(new_videos)}%)")

    state["last_run"] = NOW.isoformat()
    save_state(state)

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
