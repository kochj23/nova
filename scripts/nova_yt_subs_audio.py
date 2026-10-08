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
       nova_yt_subs_audio.py --baseline   # one pass: latest audio of every never-downloaded channel,
         <= RATE_PER_HOUR downloads (rolling hour, counted in PG so restarts keep the pace), status to
         #nova-info every 30 min, sets service_config yt_subs_baseline/done when finished (later runs exit).
"""
import argparse
import hashlib
import os
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
AUDIO_DIR = Path("/Volumes/external/nova-yt-audio")   # UNAS External share; not Plex (Jordan 2026-10-06)
INFO_CHANNEL = "C0BC4SNUTQR"   # #nova-info — Jordan asked for baseline status here
RATE_PER_HOUR = 30            # max audio downloads in any rolling hour
STATUS_EVERY_S = 1800
DONE_KEY = ("yt_subs_baseline", "done")
FALLBACK_VECTOR = "youtube_subscriptions"
COVERED_BY = ("nova_yt_ingest_watch.py", "nova_yt_new_episodes.py")
LIST_PAUSE_S = 2      # politeness between channel listings
AUDIO_EXTS = (".m4a", ".webm", ".opus", ".mp3", ".ogg")


def log(m):
    print(f"[yt-subs-audio {time.strftime('%H:%M:%S')}] {m}", flush=True)


def _yt(args, timeout=180, cookies=True):
    """yt-dlp with a 30 s socket timeout. A stalled run (2026-10-07: one download hung the 55-min slice
    past its scheduler timeout) comes back as a normal failure instead of raising out of the run."""
    try:
        return subprocess.run([YTDLP, "--no-warnings", "--socket-timeout", "30",
                               *(["--cookies", str(COOKIES)] if cookies else []), *args],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 1, "", f"yt-dlp timed out after {timeout}s")


def subscriptions():
    """[(channel_id, handle, name)] of every subscribed channel."""
    r = _yt(["--flat-playlist", "--print", "%(id)s\t%(uploader_id)s\t%(uploader)s",
             "https://www.youtube.com/feed/channels"], timeout=300)
    subs = [tuple(l.split("\t", 2)) for l in r.stdout.splitlines() if l.count("\t") == 2]
    subs = [s for s in subs if s[0].startswith("UC")]
    import json
    c = psycopg2.connect(DSN); c.autocommit = True; cur = c.cursor()
    if subs:   # remember the last good list: a logged-out cookie jar must not stop the baseline
        cur.execute("INSERT INTO service_config (service, key, value) VALUES ('yt_subs_audio', 'subscriptions', %s) "
                    "ON CONFLICT (service, key) DO UPDATE SET value = excluded.value", (json.dumps(subs),))
    else:
        cur.execute("SELECT value FROM service_config WHERE service = 'yt_subs_audio' AND key = 'subscriptions'")
        row = cur.fetchone()
        subs = [tuple(x) for x in row[0]] if row else []
        log(f"live subscription fetch failed — using the cached list ({len(subs)} channels)")
    c.close()
    return subs


def covered_keys(src_text):
    """Handles (lowercase, no @) and UC channel ids referenced by the other YouTube pipelines."""
    return ({h.lower() for h in re.findall(r"youtube\.com/@([\w.\-]+)", src_text)},
            set(re.findall(r"(UC[\w-]{22})", src_text)))


def uncovered(subs, handles, ids):
    return [s for s in subs if s[0] not in ids and s[1].lstrip("@").lower() not in handles]


MAX_SECONDS = 3 * 3600   # 7-hour auction livestream replays: hours of download, 2 h transcribed at most


def latest_video(channel_id):
    """(video_id, title) of the newest finished upload under MAX_SECONDS, or None."""
    for attempt in range(3):   # transient yt-dlp errors must not count a channel as "no video"
        r = _yt(["--flat-playlist", "-I", "1:5", "--print", "%(id)s\t%(live_status)s\t%(duration)s\t%(title)s",
                 f"https://www.youtube.com/channel/{channel_id}/videos"])
        if r.returncode == 0:
            break
        if attempt < 2:
            time.sleep(5 * (attempt + 1))
    else:
        log(f"  listing {channel_id} failed after 3 tries: {(r.stderr or '').strip()[-200:]}")
    for line in r.stdout.splitlines():
        vid, live, dur, title = (line.split("\t", 3) + ["", "", ""])[:4]
        too_long = dur.replace(".", "", 1).isdigit() and float(dur) > MAX_SECONDS
        if vid and live not in ("is_live", "is_upcoming") and not too_long:
            return vid, title
    return None


def keep_only(folder: Path, keep: Path):
    """Delete older finished audio only. Never touch SMB temp files (.smbdelete*), dotfiles or
    yt-dlp .part files: unlinking an open .smbdelete raised EBUSY and killed the first baseline run."""
    for f in folder.glob("*"):
        if f != keep and f.suffix in AUDIO_EXTS and not f.name.startswith("."):
            try:
                f.unlink()
            except OSError as e:
                log(f"  could not remove {f.name}: {e}")


def channel_folder(channel_id, name):
    """'<Channel Name> [UC...]' — readable, still unique. Renames an older id-only folder in place."""
    safe = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]+', " ", name or "")).strip(" .")[:80] or "channel"
    folder = AUDIO_DIR / f"{safe} [{channel_id}]"
    legacy = AUDIO_DIR / channel_id
    if legacy.is_dir() and not folder.exists():
        legacy.rename(folder)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def download_audio(channel_id, vid, name=""):
    folder = channel_folder(channel_id, name)
    args = ["-f", "bestaudio[ext=m4a]/bestaudio", "--no-playlist", "--no-overwrites",
            "--windows-filenames",   # no : ? * etc. on the SMB share
            "-o", str(folder / "%(title).80B [%(id)s].%(ext)s"), f"https://www.youtube.com/watch?v={vid}"]
    # Anonymous first: on 2026-10-07 YouTube started 403ing media for the logged-in jar while the same
    # video downloaded fine without it. Cookies are the fallback (members-only / age-gated videos).
    r = _yt(args, timeout=900, cookies=False)
    if r.returncode != 0:
        r = _yt(args, timeout=900)
    got = [f for f in folder.iterdir() if f"[{vid}]" in f.name and f.suffix in AUDIO_EXTS]
    if r.returncode != 0 or not got:
        log(f"  download failed {vid}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else r.returncode}")
        return None
    keep_only(folder, got[0])
    return got[0]


# Fixed topic list (Jordan 2026-10-07: "Seattle travel -> travel, Omega James Bond -> horology,
# Beatles -> music, truck build -> automotive"). A local LLM picks one label from channel + title;
# anything else, or any error, goes to FALLBACK_VECTOR. Replaced nearest-memory voting, which
# filed videos into whatever odd vector their neighbours happened to sit in.
TOPICS = {"automotive": "automotive", "horology": "horology", "music": "music", "travel": "travel",
          "news": "news", "geopolitics": "geopolitics", "history": "history", "computing": "computing",
          "cooking": "cooking", "television": "television", "film": "film", "comedy": "comedy",
          "science": "science"}
CLASSIFIER_URL = "http://127.0.0.1:11434/api/chat"
CLASSIFIER_MODEL = "qwen3:30b-a3b"


def classify(channel, title):
    import json
    import urllib.request
    prompt = (f"Pick exactly one topic label for this YouTube video from: {', '.join(TOPICS)}, other. "
              f"Answer with the label only.\nChannel: {channel}\nTitle: {title}")
    body = {"model": CLASSIFIER_MODEL, "think": False, "stream": False, "options": {"temperature": 0},
            "messages": [{"role": "user", "content": prompt}]}
    for attempt in range(3):   # Ollama mid-model-swap: retry with backoff before the fallback vector
        try:
            req = urllib.request.Request(CLASSIFIER_URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
            out = json.loads(urllib.request.urlopen(req, timeout=120).read())["message"]["content"]
            label = re.sub(r"[^a-z]", " ", out.rsplit("</think>", 1)[-1].lower()).split()  # qwen3 may still think aloud
            return TOPICS.get(label[0] if label else "", FALLBACK_VECTOR)
        except Exception as e:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            log(f"  classifier failed after 3 tries ({e}) -> {FALLBACK_VECTOR}")
    return FALLBACK_VECTOR


def transcribe_and_remember(audio, name, title, vid, existing, dry_run):
    """-> number of chunks stored (0 = nothing usable)."""
    ni.WORK_DIR.mkdir(parents=True, exist_ok=True)
    wav = ni.WORK_DIR / f"subs_{hashlib.md5(vid.encode()).hexdigest()[:12]}.wav"
    try:
        if not ni._audio(audio, wav):
            return 0
        from nova_yt_capture import whisper_lock   # shared GPU lock with the capture runner
        with whisper_lock():
            text = ni._transcribe_dispatch(wav, wav.stem, ni.WORK_DIR, local_only=True)
    finally:
        wav.unlink(missing_ok=True)
    if not text:
        return None   # no speech (music video, silent cut) — not a failure, never retried
    text = ni.clean_text(text)
    vector = classify(name, title)
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


def post_info(msg):
    try:
        import nova_config
        nova_config.post_both(msg, slack_channel=INFO_CHANNEL, discord_channel="")
    except Exception as e:
        log(f"status post failed: {e}")


def wait_for_rate(cur):
    """Block until fewer than RATE_PER_HOUR sub: downloads happened in the last hour."""
    while not ni._shutdown:
        cur.execute("SELECT count(*), min(seen_at) FROM yt_ingest_seen WHERE channel LIKE 'sub:%%' "
                    "AND status IN ('ingested', 'failed') AND seen_at > now() - interval '1 hour'")
        n, oldest = cur.fetchone()
        if n < RATE_PER_HOUR:
            return
        cur.execute("SELECT greatest(5, extract(epoch FROM %s + interval '1 hour' - now()))::int", (oldest,))
        time.sleep(min(cur.fetchone()[0] + 1, 300))


def _laps(pending, failed_now, retry_lap):
    """Yield every pending channel, then (once) every channel that failed during the first lap."""
    yield from pending
    retry_lap.extend(failed_now)
    if retry_lap:
        log(f"retry lap: {len(retry_lap)} channel(s) that failed this run")
    yield from list(retry_lap)


def baseline(cur, todo, existing, max_minutes=0):
    cur.execute("SELECT 1 FROM service_config WHERE service = %s AND key = %s", DONE_KEY)
    if cur.fetchone():
        log("baseline already complete — nothing to do")
        return
    cur.execute("SELECT DISTINCT substr(channel, 5) FROM yt_ingest_seen WHERE channel LIKE 'sub:%%' "
                "AND (status IN ('ingested', 'no_speech') OR (status = 'failed' AND attempts >= 3))")
    # ^ failed ones get retried, up to 3 attempts
    had = {r[0] for r in cur.fetchall()}
    pending = [s for s in todo if s[0] not in had]
    total, start = len(todo), time.time()
    stats = {"ingested": 0, "failed": 0, "no_speech": 0, "skipped": 0, "chunks": 0}
    failed_now = []
    if not had:   # announce once; hourly resumed slices stay quiet apart from the 30-min progress line
        post_info(f":headphones: *YouTube subscriptions baseline started* — "
                  f"{len(pending)} of {total} uncovered channels still need their latest video. Audio only "
                  f"-> `{AUDIO_DIR}`, max {RATE_PER_HOUR}/hour, local Whisper -> Nova memory. "
                  f"ETA ~{len(pending) / RATE_PER_HOUR:.0f} h. Updates every 30 min.")
    started = time.time()
    last_status = time.time()
    # One retry lap for this run's failures: the 2026-10-07 00:00-04:00 cluster (memory server /
    # PG briefly unreachable) failed 60+ perfectly good videos that transcribe fine on a re-run.
    retry_lap = []
    for i, (cid, handle, name) in enumerate(_laps(pending, failed_now, retry_lap), 1):
        if ni._shutdown:
            break
        if max_minutes and time.time() - started > max_minutes * 60:
            log(f"time slice of {max_minutes} min used — exiting cleanly; the hourly kick resumes")
            return   # ponytail: hourly slices so the scheduler sees successes (one 24 h run read as failing)
        latest = latest_video(cid)
        if not latest:
            stats["skipped"] += 1
            continue
        vid, title = latest
        wait_for_rate(cur)
        log(f"[{i}/{len(pending)}] {name}: {title[:80]}")
        audio = download_audio(cid, vid, name)
        stored = transcribe_and_remember(audio, name, title, vid, existing, False) if audio else 0
        status = "no_speech" if stored is None else ("ingested" if stored else "failed")
        stats[status] += 1
        stats["chunks"] += stored or 0
        if status == "failed" and (cid, handle, name) not in retry_lap:
            failed_now.append((cid, handle, name))
        cur.execute("INSERT INTO yt_ingest_seen (channel, video_id, title, status, attempts) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (channel, video_id) DO UPDATE SET status = excluded.status, seen_at = now(), "
                    "attempts = yt_ingest_seen.attempts + excluded.attempts",
                    (f"sub:{cid}", vid, title[:300], status, int(status == "failed")))
        if time.time() - last_status >= STATUS_EVERY_S:
            last_status = time.time()
            left = len(pending) - i
            post_info(f":headphones: YT baseline: {i}/{len(pending)} this pass — {stats['ingested']} ingested "
                      f"({stats['chunks']} chunks), {stats['failed']} failed, {stats['skipped']} no video. "
                      f"{left} left, ETA ~{left / RATE_PER_HOUR:.1f} h. Latest: {name} — {title[:60]}")
    if ni._shutdown:
        post_info(f":pause_button: YT baseline paused (process stopped) after {stats['ingested']} ingested; "
                  f"the hourly scheduler kick resumes it.")
        return
    cur.execute("INSERT INTO service_config (service, key, value) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (*DONE_KEY, '{"finished": "' + time.strftime("%Y-%m-%dT%H:%M:%S") + '"}'))
    cur.execute("SELECT count(*) FILTER (WHERE status = 'ingested'), count(*) FILTER (WHERE status = 'failed') "
                "FROM yt_ingest_seen WHERE channel LIKE 'sub:%%'")
    ok, bad = cur.fetchone()
    post_info(f":white_check_mark: *YouTube subscriptions baseline done* — {ok} channels ingested, {bad} failed "
              f"(members-only/age-gated/removed), {stats['skipped']} with no finished upload. This pass: "
              f"{stats['chunks']} memory chunks in {(time.time() - start) / 3600:.1f} h. "
              f"Recurring updates stay paused until Jordan decides the cadence.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=25, help="new videos to transcribe this run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="a single channel id")
    ap.add_argument("--baseline", action="store_true", help="one rate-limited pass over never-downloaded channels")
    ap.add_argument("--max-minutes", type=int, default=0, help="baseline: exit cleanly after this many minutes")
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
    if a.baseline:
        if not os.path.ismount(AUDIO_DIR.parent):   # unmounted share = a local folder on the boot SSD
            log(f"{AUDIO_DIR.parent} is not mounted — not starting")
            sys.exit(1)
        baseline(cur, todo, existing, a.max_minutes)
        return
    cur.execute("SELECT 1 FROM service_config WHERE service = %s AND key = %s", DONE_KEY)
    if not cur.fetchone() and not a.only:   # the daily update starts once the baseline pass has finished
        log("baseline not finished yet — daily update waits")
        return
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
        if not a.dry_run:
            wait_for_rate(cur)   # same 30/h ceiling as the baseline
        audio = None if a.dry_run else download_audio(cid, vid, name)
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
