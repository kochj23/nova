#!/opt/homebrew/bin/python3
"""nova_yt_subs_audio.py — audio-only transcript ingest for the YouTube subscriptions that no other
pipeline covers (Jordan 2026-10-06: "just download the audio, no video, and only keep the most recent
video for the transcription pipeline").

Each run:
  1. lists Jordan's subscriptions (yt-dlp, Safari-sourced cookie jar — the Chrome jar can't see them),
  2. drops channels already handled by nova_yt_ingest_watch (fishbowl) or nova_yt_new_episodes (TV library),
  3. for each remaining channel, looks at its single most recent upload (/videos tab, not live/upcoming);
     if that video is new (nova_ops.yt_ingest_seen, channel 'sub:<channel_id>') it downloads AUDIO ONLY,
     transcribes it locally (MLX Whisper, no cloud spend), and stores the chunks in Nova's memory
     with nova_ingest's own helpers (vector picked per channel, unknown topics -> youtube_subscriptions).
  4. keeps only that latest audio file per channel under AUDIO_DIR/<channel_id>/ (older ones deleted).

At most --max new videos are transcribed per run (default 25) so the first pass over ~770 channels is
spread out instead of pinning the GPU. Scheduled on the Studio scheduler (MLX Whisper is Apple-only).
Usage: nova_yt_subs_audio.py [--max N] [--dry-run] [--only CHANNEL_ID]
"""
import argparse
import hashlib
import re
import subprocess
import sys
import time
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_ingest as ni  # noqa: E402  (shared transcribe/chunk/remember path)

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
YTDLP = "/opt/homebrew/bin/yt-dlp"
COOKIES = Path.home() / ".openclaw/cache/yt_cookies_youtube.txt"   # Safari jar, kept fresh by nova_speaks_upload
AUDIO_DIR = Path("/Volumes/Data/nova-yt-audio")
FALLBACK_VECTOR = "youtube_subscriptions"
COVERED_BY = ("nova_yt_ingest_watch.py", "nova_yt_new_episodes.py")
LIST_PAUSE_S = 2      # politeness between channel listings
AUDIO_EXTS = (".m4a", ".webm", ".opus", ".mp3", ".ogg")


def log(m):
    print(f"[yt-subs-audio {time.strftime('%H:%M:%S')}] {m}", flush=True)


def _yt(args, timeout=180):
    return subprocess.run([YTDLP, "--no-warnings", "--cookies", str(COOKIES), *args],
                          capture_output=True, text=True, timeout=timeout)


def subscriptions():
    """[(channel_id, handle, name)] of every subscribed channel."""
    r = _yt(["--flat-playlist", "--print", "%(id)s\t%(uploader_id)s\t%(uploader)s",
             "https://www.youtube.com/feed/channels"], timeout=300)
    subs = [tuple(l.split("\t", 2)) for l in r.stdout.splitlines() if l.count("\t") == 2]
    return [s for s in subs if s[0].startswith("UC")]


def covered_keys(src_text):
    """Handles (lowercase, no @) and UC channel ids referenced by the other YouTube pipelines."""
    return ({h.lower() for h in re.findall(r"youtube\.com/@([\w.\-]+)", src_text)},
            set(re.findall(r"(UC[\w-]{22})", src_text)))


def uncovered(subs, handles, ids):
    return [s for s in subs if s[0] not in ids and s[1].lstrip("@").lower() not in handles]


def latest_video(channel_id):
    """(video_id, title) of the newest finished upload, or None."""
    r = _yt(["--flat-playlist", "-I", "1:3", "--print", "%(id)s\t%(live_status)s\t%(title)s",
             f"https://www.youtube.com/channel/{channel_id}/videos"])
    for line in r.stdout.splitlines():
        vid, live, title = (line.split("\t", 2) + ["", ""])[:3]
        if vid and live not in ("is_live", "is_upcoming"):
            return vid, title
    return None


def keep_only(folder: Path, keep: Path):
    for f in folder.glob("*"):
        if f.is_file() and f != keep:
            f.unlink()


def download_audio(channel_id, vid):
    folder = AUDIO_DIR / channel_id
    folder.mkdir(parents=True, exist_ok=True)
    r = _yt(["-f", "bestaudio[ext=m4a]/bestaudio", "--no-playlist", "--no-overwrites",
             "-o", str(folder / f"{vid}.%(ext)s"), f"https://www.youtube.com/watch?v={vid}"], timeout=1800)
    got = [f for f in folder.glob(f"{vid}.*") if f.suffix in AUDIO_EXTS]
    if r.returncode != 0 or not got:
        log(f"  download failed {vid}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else r.returncode}")
        return None
    keep_only(folder, got[0])
    return got[0]


def transcribe_and_remember(audio, name, title, vid, existing, dry_run):
    """-> number of chunks stored (0 = nothing usable)."""
    ni.WORK_DIR.mkdir(parents=True, exist_ok=True)
    wav = ni.WORK_DIR / f"subs_{hashlib.md5(vid.encode()).hexdigest()[:12]}.wav"
    try:
        if not ni._audio(audio, wav):
            return 0
        text = ni._transcribe_dispatch(wav, wav.stem, ni.WORK_DIR, local_only=True)
    finally:
        wav.unlink(missing_ok=True)
    if not text:
        return 0
    text = ni.clean_text(text)
    vector = ni.auto_select_vector(f"{name} {title}", text[:500], existing)
    if vector not in existing:
        vector = FALLBACK_VECTOR   # ponytail: no per-channel vector sprawl; re-home later if a topic grows
    stored, seen_hashes = 0, set()
    for chunk in ni.chunk_words(text):
        if ni.is_garbage(chunk):
            continue
        if ni.remember(chunk, vector, {"title": title, "channel": name, "video_id": vid,
                                       "type": "video_transcript", "platform": "youtube",
                                       "pipeline": "yt_subs_audio"}, seen_hashes, dry_run):
            stored += 1
    log(f"  {stored} chunks -> {vector}")
    return stored


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=25, help="new videos to transcribe this run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="a single channel id")
    a = ap.parse_args()

    subs = subscriptions()
    if not subs:
        log("subscription list empty — Safari cookie jar stale? (nova_speaks_upload refreshes it)")
        sys.exit(1)
    here = Path(__file__).parent
    handles, ids = covered_keys("".join((here / f).read_text() for f in COVERED_BY))
    todo = uncovered(subs, handles, ids)
    if a.only:
        todo = [s for s in todo if s[0] == a.only]
    log(f"{len(subs)} subscriptions, {len(todo)} not covered elsewhere")

    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    existing = ni.get_existing_vectors()
    done = 0
    for cid, handle, name in todo:
        if done >= a.max or ni._shutdown:
            break
        time.sleep(LIST_PAUSE_S)
        latest = latest_video(cid)
        if not latest:
            continue
        vid, title = latest
        cur.execute("SELECT 1 FROM yt_ingest_seen WHERE channel = %s AND video_id = %s", (f"sub:{cid}", vid))
        if cur.fetchone():
            continue
        log(f"{name}: {title[:80]}")
        audio = None if a.dry_run else download_audio(cid, vid)
        stored = transcribe_and_remember(audio, name, title, vid, existing, a.dry_run) if audio else 0
        status = "dry_run" if a.dry_run else ("ingested" if stored else "failed")
        if not a.dry_run:
            cur.execute("INSERT INTO yt_ingest_seen (channel, video_id, title, status) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (channel, video_id) DO UPDATE SET status = excluded.status, seen_at = now()",
                        (f"sub:{cid}", vid, title[:300], status))
        done += 1
    log(f"done: {done} new video(s) processed")


if __name__ == "__main__":
    main()
