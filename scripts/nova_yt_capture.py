#!/usr/bin/env python3
"""nova_yt_capture.py <video_id> <vector> [--live] — capture ONE YouTube video/stream
into Nova's vector memory (verbatim): audio transcript + live chat/superchats.

--live : record from the START with yt-dlp --live-from-start until the stream ends
         (can run for hours), so it's saved even if deleted right after it ends.
no flag: download the VOD + its chat replay.

Then: extract audio -> mlx_whisper transcript, parse the live_chat JSON (incl. superchat
amounts), and POST both to the memory service (source=<vector>). Verbatim — the only
scrubbing is the memory service's own PII pass.

Dispatched DETACHED by nova_yt_ingest_watch.py. Status tracked in nova_ops.yt_ingest_seen.
Heavy files live under ~/.openclaw/cache/fishbowl_cap and are deleted after ingest.
"""
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import psycopg2

YT_DLP = "/opt/homebrew/bin/yt-dlp"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
WHISPER = "/opt/homebrew/bin/mlx_whisper"
WMODEL = "mlx-community/whisper-large-v3-turbo"
MEMORY_URL = "http://memory-server.digitalnoise.net:18790/remember"
DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
WORK = Path.home() / ".openclaw/cache/fishbowl_cap"
# Plex YouTube library — fishbowl streams are filed here, grouped by channel, so they appear in
# Plex's YouTube section (Jordan 2026-08-11). Same volume the existing YouTube pipeline uses.
PLEX_YT = Path("/Volumes/external/videos/youtube/Fishbowl")


def file_video_to_plex(stem, channel, title, vid):
    """Move the captured 480p video into the Plex YouTube library. Best-effort: if the external
    volume isn't mounted or there's no video file, just log and move on (the transcript already
    landed; a missing Plex copy is not worth failing the capture over)."""
    import shutil
    try:
        srcs = [f for f in WORK.glob(f"{stem}.*")
                if f.suffix.lower() in (".mp4", ".mkv", ".webm")]
        if not srcs:
            return
        video = max(srcs, key=lambda f: f.stat().st_size)  # the real video, not a fragment
        if not PLEX_YT.parent.parent.exists():   # /Volumes/external/videos present == volume mounted
            log("Plex volume not mounted — keeping transcript only, skipping video file")
            return
        safe_ch = re.sub(r"[^A-Za-z0-9 ._-]", "", channel or "unknown").strip() or "unknown"
        safe_ti = re.sub(r"[^A-Za-z0-9 ._-]", "", title or vid).strip()[:120] or vid
        dest_dir = PLEX_YT / safe_ch
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{safe_ti} [{vid}]{video.suffix.lower()}"
        if dest.exists():
            return  # already filed (idempotent on re-capture)
        shutil.move(str(video), str(dest))
        log(f"Filed to Plex: {dest}")
    except Exception as e:
        log(f"Plex filing failed (non-fatal, transcript kept): {e}")
CHUNK = 1500


def log(m):
    print(f"[yt-capture] {m}", flush=True)


def slack(m):
    try:
        import nova_config
        nova_config.post_both(m, slack_channel=nova_config.SLACK_FEED, discord_channel=None)
    except Exception as e:
        log(f"slack: {e}")


MAX_VOD_ATTEMPTS = 4


def retry_or_empty(vid):
    try:
        c = psycopg2.connect(DSN); c.autocommit = True
        c.cursor().execute("UPDATE yt_ingest_seen SET attempts = attempts + 1, seen_at = now(), "
                           "status = CASE WHEN attempts + 1 >= %s THEN 'empty' ELSE 'vod_pending' END "
                           "WHERE video_id = %s", (MAX_VOD_ATTEMPTS, vid))
        c.close()
    except Exception as e:
        log(f"status update failed: {e}")


def setstatus(vid, status):
    try:
        c = psycopg2.connect(DSN); c.autocommit = True
        c.cursor().execute("UPDATE yt_ingest_seen SET status=%s WHERE video_id=%s", (status, vid))
        c.close()
    except Exception as e:
        log(f"status update failed: {e}")


def ytbase():
    a = [YT_DLP, "--extractor-args", "youtube:player_client=web,default", "--no-playlist", "--no-warnings"]
    cj = Path.home() / ".openclaw/cache/yt_cookies.txt"
    a += ["--cookies", str(cj)] if cj.exists() else ["--cookies-from-browser", "chrome"]
    return a


def meta_of(vid):
    try:
        r = subprocess.run(ytbase() + ["--dump-json", "--no-download",
                                       f"https://www.youtube.com/watch?v={vid}"],
                           capture_output=True, text=True, timeout=120)
        j = json.loads(r.stdout)
        return (j.get("title") or "")[:200], (j.get("channel") or "")
    except Exception:
        return "", ""


def download(url, live, stem):
    WORK.mkdir(parents=True, exist_ok=True)
    out = str(WORK / f"{stem}.%(ext)s")
    # 480p VIDEO (with audio) instead of audio-only — Jordan wants the streams kept in Plex
    # (2026-08-11). We transcribe from this same file (to_wav extracts the audio), so it's one
    # download serving both the transcript AND the Plex archive. 480p is plenty for talking-head
    # streams and keeps multi-hour files ~0.5-1GB. Merge to mp4 for Plex compatibility.
    cmd = ytbase() + ["-f", "bv*[height<=480]+ba/b[height<=480]/best[height<=480]/best",
                      "--merge-output-format", "mp4",
                      "--write-subs", "--sub-langs", "live_chat", "-o", out]
    if live:
        cmd += ["--live-from-start", "--wait-for-video", "0"]
    cmd += [url]
    log(f"{'RECORDING LIVE (from start)' if live else 'downloading VOD'}: {url}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=12 * 3600)
    audio = chat = None
    for f in WORK.glob(f"{stem}.*"):
        if f.name.endswith(".live_chat.json") or f.suffix == ".json":
            chat = f
        elif f.suffix.lower() in (".m4a", ".webm", ".opus", ".mp3", ".mp4", ".mkv", ".wav", ".aac", ".ogg"):
            audio = f
    if not audio:   # was silent for weeks (49 empty Franchise Club streams) — say why
        log(f"no media from yt-dlp (rc={r.returncode}): {(r.stderr or '').strip()[-300:]}")
    return audio, chat


def to_wav(audio, stem):
    wav = WORK / f"{stem}.wav"
    subprocess.run([FFMPEG, "-y", "-i", str(audio), "-vn", "-ac", "1", "-ar", "16000",
                    "-acodec", "pcm_s16le", str(wav)], capture_output=True, timeout=4 * 3600)
    return wav if wav.exists() and wav.stat().st_size > 1000 else None


def _dedupe_loops(text, max_repeat=2):
    """Collapse Whisper repetition-loop runs (a line/sentence repeated over and over —
    "and we will be our hearts" x25) down to at most max_repeat, and drop a chunk
    entirely if after collapsing it's still mostly one phrase (pure hallucination)."""
    if not text:
        return text
    import re as _re
    units = [u for u in _re.split(r"(?<=[.!?])\s+|\n+", text)]
    out, run, last = [], 0, None
    for u in units:
        key = _re.sub(r"[^a-z0-9 ]", "", u.strip().lower())
        if key and key == last:
            run += 1
            if run < max_repeat:
                out.append(u)
        else:
            run = 0; last = key
            out.append(u)
    cleaned = " ".join(x.strip() for x in out if x.strip())
    words = cleaned.lower().split()
    if len(words) > 12 and len(set(words)) <= max(3, len(words) // 8):
        return ""   # degenerate loop -> drop the chunk rather than store garbage
    return cleaned


def transcribe(wav, stem):
    subprocess.run([WHISPER, str(wav), "--model", WMODEL, "--output-format", "txt",
                    "--output-dir", str(WORK), "--output-name", stem, "--language", "en",
                    # anti-hallucination: stop the model conditioning on its own looped output,
                    # reject over-compressible (repetitive) segments, skip silent stretches.
                    "--condition-on-previous-text", "False",
                    "--compression-ratio-threshold", "2.4",
                    "--hallucination-silence-threshold", "2"],
                   capture_output=True, text=True, timeout=8 * 3600)
    t = WORK / f"{stem}.txt"
    raw = t.read_text(errors="ignore").strip() if t.exists() else ""
    return _dedupe_loops(raw)


def parse_chat(chatfile):
    """Best-effort live_chat.json -> 'author: message' lines, superchats flagged.

    Also returns per-commenter tallies (channel_id, name, msg/superchat counts) so
    the channel-discovery pipeline can see who's showing up in the chat — a display
    name alone is useless for finding someone's channel, but authorExternalChannelId
    resolves to an exact, clickable URL."""
    if not chatfile or not chatfile.exists():
        return "", {}
    lines = []
    commenters = {}   # channel_id -> {"name": str, "messages": int, "superchats": int, "superchat_total": float}
    for ln in chatfile.read_text(errors="ignore").splitlines():
        try:
            d = json.loads(ln)
        except Exception:
            continue
        actions = d.get("replayChatItemAction", {}).get("actions", [d]) if "replayChatItemAction" in d else [d]
        for a in actions:
            item = (a.get("addChatItemAction", {}) or {}).get("item", {})
            r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
            if not r:
                continue
            auth = (r.get("authorName", {}) or {}).get("simpleText", "?")
            cid = r.get("authorExternalChannelId", "")
            msg = "".join(run.get("text", "") for run in (r.get("message", {}) or {}).get("runs", []))
            amt = (r.get("purchaseAmountText", {}) or {}).get("simpleText", "")
            if cid:
                c = commenters.setdefault(cid, {"name": auth, "messages": 0, "superchats": 0, "superchat_total": 0.0})
                c["name"] = auth
                if amt:
                    c["superchats"] += 1
                    m = re.search(r"[\d,]+\.?\d*", amt)
                    if m:
                        try:
                            c["superchat_total"] += float(m.group().replace(",", ""))
                        except ValueError:
                            pass
                else:
                    c["messages"] += 1
            if amt:
                lines.append(f"[SUPERCHAT {amt}] {auth}: {msg}".rstrip())
            elif msg:
                lines.append(f"{auth}: {msg}")
    return "\n".join(lines), commenters


def record_commenters(channel, video_id, commenters):
    """Upsert per-stream commenter tallies into nova_ops.fishbowl_commenters —
    the raw material for spotting active-but-untracked channels later."""
    if not commenters:
        return
    try:
        conn = psycopg2.connect(DSN); conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS fishbowl_commenters (
                channel_id text PRIMARY KEY,
                display_name text,
                first_seen timestamptz DEFAULT now(),
                last_seen timestamptz DEFAULT now(),
                message_count integer DEFAULT 0,
                superchat_count integer DEFAULT 0,
                superchat_total real DEFAULT 0,
                source_channels text[] DEFAULT '{}',
                reviewed boolean DEFAULT false)""")
            for cid, c in commenters.items():
                cur.execute("""
                    INSERT INTO fishbowl_commenters
                        (channel_id, display_name, message_count, superchat_count, superchat_total, source_channels)
                    VALUES (%s, %s, %s, %s, %s, ARRAY[%s])
                    ON CONFLICT (channel_id) DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        last_seen = now(),
                        message_count = fishbowl_commenters.message_count + EXCLUDED.message_count,
                        superchat_count = fishbowl_commenters.superchat_count + EXCLUDED.superchat_count,
                        superchat_total = fishbowl_commenters.superchat_total + EXCLUDED.superchat_total,
                        source_channels = CASE WHEN %s = ANY(fishbowl_commenters.source_channels)
                                          THEN fishbowl_commenters.source_channels
                                          ELSE fishbowl_commenters.source_channels || %s END
                """, (cid, c["name"], c["messages"], c["superchats"], c["superchat_total"], channel, channel, channel))
        conn.close()
    except Exception as e:
        log(f"commenter tally failed (non-fatal): {e}")


def chunk(text, size=CHUNK):
    out, cur = [], ""
    for p in re.split(r"\n{2,}", text):
        p = p.strip()
        if not p:
            continue
        while len(p) > size:
            out.append(p[:size]); p = p[size:]
        if len(cur) + len(p) > size:
            if cur:
                out.append(cur)
            cur = p
        else:
            cur = (cur + "\n\n" + p) if cur else p
    if cur:
        out.append(cur)
    return out


def remember(text, meta):
    payload = json.dumps({"text": text, "source": meta["vector"], "tier": "long_term",
                          "metadata": {**meta, "privacy": "private"}}).encode()
    req = urllib.request.Request(MEMORY_URL + "?async=1", data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20):
            return True
    except Exception as e:
        log(f"remember failed: {e}")
        return False


def main():
    if len(sys.argv) < 3:
        print("usage: nova_yt_capture.py <video_id> <vector> [--live]"); sys.exit(1)
    vid, vector = sys.argv[1], sys.argv[2]
    live = "--live" in sys.argv
    url = f"https://www.youtube.com/watch?v={vid}"
    setstatus(vid, "recording" if live else "downloading")
    title, channel = meta_of(vid)
    stem = re.sub(r"[^A-Za-z0-9_-]", "", vid)
    audio, chatf = download(url, live, stem)
    transcript = ""
    if audio:
        wav = to_wav(audio, stem)
        if wav:
            transcript = transcribe(wav, stem)
    chat, commenters = parse_chat(chatf)
    record_commenters(channel, vid, commenters)
    # File the VIDEO into Plex's YouTube library before cleaning up the temp dir, so fishbowl
    # streams show up in Plex (Jordan 2026-08-11). Everything captured, grouped by channel under
    # a "Fishbowl" folder. Then delete the leftover temp files (audio-extract wavs, chat json).
    if vector == "fishbowl":   # watch-news channels (horology, 2026-10-06) are transcript-only, not Plex
        file_video_to_plex(stem, channel, title, vid)
    for f in WORK.glob(f"{stem}.*"):
        try:
            f.unlink()
        except Exception:
            pass
    base = {"vector": vector, "type": "fishbowl_stream", "kind": "live" if live else "vod",
            "video_id": vid, "title": title, "channel": channel, "url": url, "author": "fishbowl"}
    hdr = f"[Fishbowl stream — {channel} — {title}]"
    n = 0
    for i, c in enumerate(chunk(transcript)):
        if remember(f"{hdr} (transcript)\n{c}", {**base, "part": "transcript", "idx": i}):
            n += 1
    for i, c in enumerate(chunk(chat)):
        if remember(f"{hdr} (live chat/superchats)\n{c}", {**base, "part": "chat", "idx": i}):
            n += 1
    if not n and not audio and not live:
        # A long stream's replay is often not downloadable for hours after it ends — the 2026-09/10
        # Franchise Club streams all came back empty this way. Park it; the watcher re-queues
        # vod_pending rows every 2 h, up to 4 tries, before calling it empty.
        retry_or_empty(vid)
    else:
        setstatus(vid, "ingested" if n else "empty")
    log(f"done {vid}: transcript={'y' if transcript else 'n'} chat={'y' if chat else 'n'} chunks={n}")
    # post a sample of the actual memory to #nova-info (alongside progress)
    if n:
        ts = (transcript[:600] + "…") if len(transcript) > 600 else (transcript or "(no transcript)")
        cs = "\n".join(chat.split("\n")[:4]) if chat else "(no chat captured)"
        slack(f":memo: *Fishbowl memory ingested* — {channel} — {title[:90]} "
              f"({'LIVE' if live else 'vod'}, {n} chunks -> source=fishbowl)\n"
              f"*Transcript sample:*\n> {ts.strip()[:600]}\n*Chat/superchats sample:*\n> {cs[:500]}")


if __name__ == "__main__":
    main()
