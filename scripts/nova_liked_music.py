#!/opt/homebrew/opt/python@3.14/bin/python3.14
"""nova_liked_music.py — "Liked and Listened": a daily Music and Friends article about the music Jordan
LIKED on YouTube in the last 24 rolling hours, with the best bits played in the Nova Speaks video.

Jordan 2026-11-02: "a new daily article on the Music and Friends section that looks at my Liked videos in
Youtube in the past 24 hour rolling period ... Don't write an article [if there are no new liked videos or
music] ... Analyze the songs, include snippets of the parts that you like and (in the video) play the
interesting bits. Make sure that videos aren't theme music for a podcast ... run these at 7:30am daily. Use
the time that I liked it, not the date of the video." Choices he made: clips PLAY in the video (Content ID
claims accepted), a separate article (not folded into the daily issue), DJ sets count (2-3 highlights).

LIKE TIME: YouTube does not expose when a video was liked. `--poll` (every 15 min) reads the Liked list (LL,
newest first) with Jordan's Safari session and records each NEW video id in nova_ops.yt_likes with
first_seen = now, i.e. the like time to within one poll. The first poll ever marks everything already
there as baseline (never written about). The article window is first_seen in the last --hours (24).

WHAT COUNTS AS MUSIC: YouTube's category is NOT enough (Talk Talk's official "It's My Life" is filed under
People & Blogs). Hard rules first (45 s .. 4 h; podcast / episode / interview / "theme song" / "intro
music" titles out), then the local qwen3:30b judges title, channel, description and tags, plus a Whisper
sample of the audio (a song has sung or rapped lines over music; a podcast theme is a short sting from a
show's channel). kind: song | dj_set | live | other — only song / dj_set / live are written about.

ANALYSIS per track (all local): librosa tempo + key (Krumhansl-Schmuckler on chroma) + energy curve +
section boundaries; mlx-whisper large-v3-turbo lyrics with timestamps (songs only); YouTube's "most
replayed" heatmap. Candidate 15 s windows come from heatmap peaks, the biggest energy rises (drops,
choruses) and section starts; the model picks 2 (3 for a DJ set) and says why, grounded in those facts
only. Clips are cut to the NAS (videos/clips/<id>-<start>-<end>.mp4) where every Nova Speaks host reads
them; the article embeds each one as {{< youtube id start end >}} (cued player on the site, a played clip
in the video — see nova_speaks.py SONG CLIPS).

(2026-11-02 first test: classification moved BEFORE the download — it was pulling 35-80 minute vlogs in full.)

Usage: nova_liked_music.py --poll
       nova_liked_music.py --scheduled [--hours 24] [--dry-run]
       nova_liked_music.py --test-latest N     # treat the N newest likes as "the window" (no like times yet)
       nova_liked_music.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

SECTION = "music"
WORK = Path("/Volumes/Data/AI/liked-music")
CLIPS = Path("/Volumes/nas/nova-fs/videos/clips")
YTDLP = "/opt/homebrew/bin/yt-dlp"
LL = "https://www.youtube.com/playlist?list=LL"
WHISPER = "mlx-community/whisper-large-v3-turbo"
CLIP_S = 15
NOT_MUSIC_RX = re.compile(r"\b(podcast|episode|ep\.?\s*\d|interview|theme\s*(song|music|tune)|intro\s*music|"
                          r"outro|reaction|review|tutorial|lesson|how to|explained|documentary)\b", re.I)
MUSIC_RX = re.compile(r"\b(official\s+(music\s+)?video|official\s+audio|lyrics?(\s+video)?|visuali[sz]er|"
                      r"remaster(ed)?|full\s+album|(dj|live)\s+set|mix(tape)?|boiler\s+room)\b", re.I)
SET_MIN_S = 20 * 60          # a "DJ set" is at least this long; a 4-minute track from a DJ is a song
DDL = """CREATE TABLE IF NOT EXISTS yt_likes (
  video_id text PRIMARY KEY, title text, channel text, duration int, first_seen timestamptz NOT NULL DEFAULT now(),
  last_seen timestamptz NOT NULL DEFAULT now(), baseline boolean NOT NULL DEFAULT false,
  kind text, verdict jsonb, written_at timestamptz)"""


def log(m):
    print(f"[liked-music {time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


def _conn():
    import nova_dsn
    c = nova_dsn.pg_connect(); c.autocommit = True
    return c


def ytdlp(*args, timeout=600) -> str:
    r = subprocess.run([YTDLP, "--cookies-from-browser", "safari", "--no-warnings", *args],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp {' '.join(args[:2])}: {(r.stderr or '').strip()[-300:]}")
    return r.stdout


def liked(n=100) -> list[dict]:
    """Newest-first Liked list (flat)."""
    d = json.loads(ytdlp("--flat-playlist", "--playlist-end", str(n), "-J", LL))
    return [{"id": e["id"], "title": e.get("title") or "", "channel": e.get("channel") or e.get("uploader") or "",
             "duration": int(e.get("duration") or 0)} for e in d.get("entries") or [] if e and e.get("id")]


# ── 1. poll ───────────────────────────────────────────────────────────────────────────────
def poll() -> int:
    items = liked()
    c = _conn()
    with c.cursor() as cur:
        cur.execute(DDL)
        cur.execute("SELECT count(*) FROM yt_likes")
        first = cur.fetchone()[0] == 0
        new = 0
        for e in items:
            cur.execute("INSERT INTO yt_likes (video_id, title, channel, duration, baseline) VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT (video_id) DO UPDATE SET last_seen = now() RETURNING (xmax = 0)",
                        (e["id"], e["title"], e["channel"], e["duration"], first))
            new += 1 if cur.fetchone()[0] else 0
    c.close()
    log(f"{'baseline' if first else 'poll'}: {len(items)} liked, {new} new")
    return 0


# ── 2. is it music? ───────────────────────────────────────────────────────────────────────
def hard_reject(meta: dict) -> str | None:
    """A reason this cannot be music worth writing about, else None. Pure."""
    d = int(meta.get("duration") or 0)
    if d < 45:
        return f"too short ({d}s)"
    if d > 4 * 3600:
        return f"too long ({d // 60} min)"
    text = f"{meta.get('title', '')} {meta.get('channel', '')}"
    m = NOT_MUSIC_RX.search(text)
    if m:
        return f"title says '{m.group(0)}'"
    return None


def llm_json(prompt: str, max_tokens=900, temperature=0.3) -> dict:
    import nova_beliefs as nb
    body = json.dumps({"model": nb.LLM_MODEL, "stream": False, "think": False, "keep_alive": "30m", "format": "json",
                       "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": 16384},
                       "messages": [{"role": "user", "content": prompt + "\n/no_think"}]}).encode()
    for base in nb.LLM_URLS:
        for _ in range(2):
            try:
                req = urllib.request.Request(base + "/api/chat", data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=600) as r:
                    return json.loads(json.load(r).get("message", {}).get("content", "") or "{}")
            except Exception as e:  # noqa: BLE001
                log(f"llm {base}: {type(e).__name__}: {e}")
    return {}


def plainly_music(meta: dict) -> bool:
    """Title/channel say it outright: lyric / official video / audio, a YouTube "- Topic" or VEVO channel. Pure.
    (2026-11-02 test: the model rejected Roxy Music's "More Than This (Lyrics)" as "talking".)"""
    ch = meta.get("channel") or ""
    return (bool(MUSIC_RX.search(meta.get("title") or "")) or ch.endswith(" - Topic") or "vevo" in ch.lower()
            or bool(re.search(r"\b(records|recordings)\b", ch, re.I)))   # a label's channel (DJ ÆDIDIAS on ACKIRAI RECORDS)


def kind_for(meta: dict, kind: str) -> str:
    """dj_set only when it is long enough to be one. Pure."""
    if kind == "dj_set" and int(meta.get("duration") or 0) < SET_MIN_S:
        return "song"
    if kind == "song" and int(meta.get("duration") or 0) >= SET_MIN_S and re.search(r"\b(mix|set|live)\b", meta.get("title") or "", re.I):
        return "dj_set"
    return kind


def classify(meta: dict, sample_text: str = "") -> dict:
    why = hard_reject(meta)
    if why:
        return {"kind": "other", "music": False, "reason": why}
    desc = (meta.get("description") or "")[:900]
    v = llm_json(f"""Decide whether this YouTube video Jordan liked is a piece of MUSIC to review.
MUSIC = a song or track (official video, lyric video, audio upload, live performance) or a DJ set / mix.
NOT music = podcasts, a podcast's theme song or intro sting, talk, news, vlogs, reviews, tutorials, trailers,
comedy, anything where music is background to talking.
Title: {meta.get('title')}
Channel: {meta.get('channel')}
YouTube category: {meta.get('categories')}
Duration: {meta.get('duration')} s
Tags: {(meta.get('tags') or [])[:15]}
Description (start): {desc}
{('What a speech recognizer heard in 30 s from the middle: ' + sample_text[:500]) if sample_text else ''}
Answer JSON: {{"music": true|false, "kind": "song|dj_set|live|other", "artist": "...", "track": "...",
"reason": "one short sentence"}}""", max_tokens=200, temperature=0.0)
    v.setdefault("music", False); v.setdefault("kind", "other")
    if plainly_music(meta):
        v["music"] = True
        if v["kind"] not in ("song", "dj_set", "live"):
            v["kind"] = "song"; v["reason"] = f"title/channel say it is music ({v.get('reason', '')})"
    if v.get("kind") not in ("song", "dj_set", "live"):
        v["music"] = False
    v["kind"] = kind_for(meta, v["kind"])
    return v


# ── 3. analysis ───────────────────────────────────────────────────────────────────────────
KEYS = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
MAJ = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MIN = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def estimate_key(chroma_mean) -> str:
    """Krumhansl-Schmuckler on a 12-bin mean chroma. Pure."""
    import numpy as np
    best = (-2.0, "")
    for i in range(12):
        for prof, mode in ((MAJ, "major"), (MIN, "minor")):
            r = float(np.corrcoef(np.roll(prof, i), chroma_mean)[0, 1])
            if r > best[0]:
                best = (r, f"{KEYS[i]} {mode}")
    return best[1]


def audio_features(wav: str, dur: int) -> dict:
    import numpy as np
    import librosa
    y, sr = librosa.load(wav, sr=22050, mono=True)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=2048)
    rms = librosa.feature.rms(y=y, hop_length=2048)[0]
    t = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=2048)
    sec = 2.0                                      # energy curve at 2 s resolution, 0..1
    idx = (t // sec).astype(int)
    curve = np.array([rms[idx == k].mean() if (idx == k).any() else 0 for k in range(idx.max() + 1)])
    curve = curve / (curve.max() or 1)
    k = max(4, min(14, int(dur // 30)))
    try:
        feats = np.vstack([librosa.util.normalize(chroma, axis=0), librosa.feature.mfcc(y=y, sr=sr, hop_length=2048, n_mfcc=13)])
        bounds = librosa.frames_to_time(librosa.segment.agglomerative(feats, k), sr=sr, hop_length=2048)
    except Exception:  # noqa: BLE001
        bounds = []
    cent = float(np.median(librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=2048)))
    harm, perc = librosa.effects.hpss(y[: sr * 120])
    pr = float(np.sqrt(np.mean(perc ** 2)) / (np.sqrt(np.mean(harm ** 2)) or 1))
    dyn = float(np.percentile(curve, 90) - np.percentile(curve, 20))
    sound = {"brightness": "bright" if cent > 2600 else "warm/dark" if cent < 1500 else "balanced",
             "drive": "drum-led, percussive" if pr > 0.8 else "melodic, harmony-led" if pr < 0.4 else "even mix of beat and melody",
             "dynamics": "big quiet-to-loud swings" if dyn > 0.45 else "steady, compressed level" if dyn < 0.2 else "moderate build and release"}
    return {"tempo": round(float(np.atleast_1d(tempo)[0])), "key": estimate_key(chroma.mean(axis=1)), "sound": sound,
            "curve": [round(float(x), 2) for x in curve], "sections": [round(float(b)) for b in bounds if b > 1]}


def lyrics(wav: str) -> list[dict]:
    try:
        import mlx_whisper
        r = mlx_whisper.transcribe(wav, path_or_hf_repo=WHISPER, condition_on_previous_text=False)
        return [{"start": round(s["start"]), "end": round(s["end"]), "text": s["text"].strip()}
                for s in r.get("segments", []) if s.get("text", "").strip() and s.get("no_speech_prob", 0) < 0.6]
    except Exception as e:  # noqa: BLE001
        log(f"whisper failed: {e}")
        return []


def candidates(dur: int, heat: list, curve: list, sections: list, n=8) -> list[int]:
    """Start seconds of candidate CLIP_S windows: heatmap peaks, biggest energy rises, section starts. Pure."""
    def ok(s):
        return 3 <= s <= max(3, dur - CLIP_S - 2)
    scored = []
    for h in heat or []:
        if h.get("start_time", 0) > 5:
            scored.append((float(h["value"]) * 2.0, int(h["start_time"]) - 2))
    for i in range(2, len(curve)):
        rise = curve[i] - min(curve[max(0, i - 3):i])
        scored.append((rise * 1.5 + curve[i] * 0.3, i * 2 - 2))
    for s in sections:
        scored.append((0.4, int(s)))
    out = []
    for _, s in sorted(scored, reverse=True):
        if ok(s) and all(abs(s - o) >= CLIP_S + 5 for o in out):
            out.append(s)
        if len(out) >= n:
            break
    return sorted(out)


def describe(s: int, feat: dict, heat: list, lyr: list) -> dict:
    c = feat["curve"]; i = s // 2
    win = c[i:i + CLIP_S // 2] or [0]; before = c[max(0, i - 4):i] or [0]
    hv = max([h["value"] for h in heat or [] if s - 3 <= h["start_time"] <= s + CLIP_S] or [0])
    words = " / ".join(l["text"] for l in lyr if s - 1 <= l["start"] <= s + CLIP_S)[:200]
    return {"start": s, "end": s + CLIP_S, "at": f"{s // 60}:{s % 60:02d}", "energy": round(sum(win) / len(win), 2),
            "energy_change": round(sum(win) / len(win) - sum(before) / len(before), 2),
            "most_replayed": round(float(hv), 2), "lyrics": words,
            "section_start": any(abs(s - b) <= 3 for b in feat["sections"])}


# ── 4. one track ──────────────────────────────────────────────────────────────────────────
def fetch(vid: str, dur: int) -> tuple[str, str]:
    WORK.mkdir(parents=True, exist_ok=True)
    mp4 = WORK / f"{vid}.mp4"
    if not mp4.exists():
        h = 1080 if dur < 1800 else 720
        ytdlp("-f", f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}]", "--merge-output-format", "mp4",
              "-o", str(mp4), f"https://www.youtube.com/watch?v={vid}", timeout=3600)
    wav = WORK / f"{vid}.wav"
    if not wav.exists():
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(mp4), "-vn", "-ac", "1",
                        "-ar", "22050", str(wav)], check=True)
    return str(mp4), str(wav)


def sample_text(wav: str, dur: int) -> str:
    mid = max(0, dur // 2 - 15)
    tmp = WORK / "sample.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(mid), "-t", "30", "-i", wav, str(tmp)], check=True)
    return " ".join(l["text"] for l in lyrics(str(tmp)))


def cut(mp4: str, vid: str, s: int, e: int) -> Path:
    CLIPS.mkdir(parents=True, exist_ok=True)
    out = CLIPS / f"{vid}-{s}-{e}.mp4"
    if not out.exists():
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(s), "-t", str(e - s), "-i", mp4,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "192k", str(out)], check=True)
    return out


NAME_JUNK_RX = re.compile(r"\s*[\(\[](?:official\s+)?(?:music\s+)?(?:4k\s+)?(?:video|audio|lyrics?|lyric video|visuali[sz]er|hd|hq|4k|"
                          r"remaster(?:ed)?(?:\s+\d{4})?|\d{4}\s+remaster(?:ed)?)[^\)\]]*[\)\]]", re.I)


def track_name(t: dict) -> str:
    """'Artist — Track' with YouTube packaging ('(Lyrics)', '[Official Video]', '(4K Remaster)') stripped. Pure."""
    name = f"{t['artist']} — {t['track']}" if t.get("artist") and t.get("track") else t["title"]
    return NAME_JUNK_RX.sub("", name).strip() or name


def moment_words(c: dict) -> str:
    """A plain-words reason for a topped-up highlight, from its measured facts. Pure."""
    bits = []
    if c["most_replayed"] >= 0.5:
        bits.append("This is one of the stretches YouTube viewers replay most")
    if c["energy_change"] >= 0.15:
        bits.append("the track lifts here")
    elif c["energy_change"] <= -0.15:
        bits.append("the track drops back and opens up here")
    if c.get("section_start"):
        bits.append("a new section begins")
    s = "; ".join(bits) or "A moment worth hearing on its own"
    return s[0].upper() + s[1:] + "."


def write_track(t: dict) -> dict:
    """LLM: analysis paragraphs + 2-3 chosen highlights, grounded in the measured facts only."""
    n = 3 if t["kind"] == "dj_set" else 2
    lyr = " | ".join(f"[{l['start'] // 60}:{l['start'] % 60:02d}] {l['text']}" for l in t["lyrics"])[:2500]
    v = llm_json(f"""You are Nova, an AI who loves music, writing for your journal's Music and Friends section about
a {'DJ set' if t['kind'] == 'dj_set' else 'track'} that Little Mister (the person you live with) liked on YouTube.
Write like a sharp, warm music critic: what it is, what it sounds like, what makes it work. Use ONLY the facts
below; do not invent release years, labels, chart history, band members, instruments or anything not given (you
cannot hear timbre — use the SOUND descriptors, and name an instrument only if the description names it).
LYRICS come from speech recognition and are often WRONG (especially patois, accents, fast or sung lines): quote AT
MOST ONE short line, and only a line that clearly makes sense (ideally the title or the chorus). Otherwise describe.
NEVER mention the measurement numbers or field names (energy, energy_change, most_replayed): say what happens in
musical words ("the drums drop out", "it lifts into the chorus", "the part everyone replays").
Banned words: masterclass, sonic tapestry, delve, testament, vibe, anthem (unless the description says so).
FACTS
Title: {t['title']} | Artist: {t.get('artist') or '?'} | Track: {t.get('track') or '?'} | Channel: {t['channel']}
Length: {t['duration'] // 60}:{t['duration'] % 60:02d} | Tempo: about {t['tempo']} BPM | Key (estimated): {t['key']}
SOUND (measured): {t['sound']}
Description (start): {(t.get('description') or '')[:600]}
Lyrics heard (timestamped, may contain recognition errors): {lyr or '(instrumental or not transcribed)'}
CANDIDATE MOMENTS (pick exactly {n}, by their start values, for the clips played in the video):
{json.dumps(t['cands'], ensure_ascii=False)}
(energy 0..1; energy_change > 0 = it lifts there; most_replayed 0..1 = how often YouTube viewers replay it)
Answer JSON: {{"analysis": ["paragraph", "paragraph", "paragraph"],
 "highlights": [EXACTLY {n} items: {{"start": <one of the candidate starts>, "why": "2-3 sentences: what happens in these 15 seconds and why it is the good bit"}}],
 "one_line": "a one-sentence verdict"}}""", max_tokens=1600, temperature=0.6)
    starts = {c["start"] for c in t["cands"]}
    hl = [h for h in v.get("highlights", []) if isinstance(h, dict) and h.get("start") in starts][:n]
    taken = {h["start"] for h in hl}           # model gave fewer than n (2026-11-02 test: always 1): top up by replay + lift
    for c in sorted(t["cands"], key=lambda c: -(c["most_replayed"] * 2 + c["energy_change"])):
        if len(hl) >= n:
            break
        if c["start"] not in taken:
            hl.append({"start": c["start"], "why": moment_words(c)}); taken.add(c["start"])
    v["highlights"] = sorted(hl, key=lambda h: h["start"])
    v["analysis"] = [p for p in v.get("analysis", []) if isinstance(p, str) and len(p) > 40]
    return v


def track(vid: str, liked_at, row: dict) -> dict | None:
    meta = json.loads(ytdlp("-j", "--skip-download", f"https://www.youtube.com/watch?v={vid}"))
    meta["duration"] = int(meta.get("duration") or row.get("duration") or 0)
    if hard_reject(meta):
        log(f"skip {vid} {meta.get('title')!r}: {hard_reject(meta)}")
        return None
    verdict = classify(meta)                     # metadata first: never download a 35-min vlog to learn it is a vlog
    log(f"{vid} {meta.get('title')!r}: {verdict}")
    if not verdict.get("music"):
        return None
    mp4, wav = fetch(vid, meta["duration"])
    feat = audio_features(wav, meta["duration"])
    lyr = lyrics(wav) if verdict["kind"] != "dj_set" and meta["duration"] <= 900 else []
    heat = meta.get("heatmap") or []
    t = {"id": vid, "title": meta.get("title") or row.get("title"), "channel": meta.get("channel") or "",
         "duration": meta["duration"], "description": meta.get("description") or "", "liked_at": liked_at,
         "kind": verdict["kind"], "artist": verdict.get("artist"), "track": verdict.get("track"),
         "tempo": feat["tempo"], "key": feat["key"], "sound": feat["sound"], "lyrics": lyr, "verdict": verdict, "mp4": mp4}
    t["cands"] = [describe(s, feat, heat, lyr) for s in candidates(meta["duration"], heat, feat["curve"], feat["sections"])]
    if not t["cands"]:
        return None
    t["text"] = write_track(t)
    for h in t["text"]["highlights"]:
        cut(mp4, vid, h["start"], h["start"] + CLIP_S)
    return t


# ── 5. article ────────────────────────────────────────────────────────────────────────────
def esc(s: str) -> str:
    return (s or "").replace('"', "'").replace("<", "").replace(">", "")


def compose(tracks: list[dict], window: str) -> tuple[str, str]:
    """(body markdown, description). Pure."""
    parts = []
    intro = (f"Little Mister liked {len(tracks)} piece{'s' if len(tracks) != 1 else ''} of music on YouTube {window}. "
             "I listened to every one all the way through, measured the tempo, guessed the key, read along with the "
             "lyrics and picked the moments I kept rewinding. In the video version of this page you hear those moments; "
             "here, each one is cued up in the player.")
    parts.append(intro)
    for t in tracks:
        x = t["text"]
        name = track_name(t)
        kind = {"dj_set": "DJ set", "live": "live performance"}.get(t["kind"], "track")
        when = f" · liked {t['liked_at'].astimezone():%a %-I:%M %p}" if t.get("liked_at") else ""   # test edition: unknown
        parts.append(f"## {esc(name)}")
        parts.append(f"*{kind.capitalize()} · {t['duration'] // 60}:{t['duration'] % 60:02d} · about {t['tempo']} BPM · "
                     f"{t['key']} (my estimate){when} · [watch on YouTube](https://www.youtube.com/watch?v={t['id']})*")
        parts += x.get("analysis") or []
        for i, h in enumerate(x["highlights"], 1):
            s = h["start"]
            parts.append(f"**The good bit{'' if len(x['highlights']) == 1 else f' #{i}'} — {s // 60}:{s % 60:02d}.** {h.get('why') or ''}".rstrip())
            parts.append(f'{{{{< youtube id="{t["id"]}" start="{s}" end="{s + CLIP_S}" title="{esc(name)[:80]}" >}}}}')
        if x.get("one_line"):
            parts.append(f"*Verdict: {x['one_line']}*")
    names = ", ".join(esc(f"{t['artist']}" if t.get("artist") else t["title"]) for t in tracks[:4])
    desc = f"What Little Mister liked on YouTube {window}, listened to closely: {names}{' and more' if len(tracks) > 4 else ''}."
    return "\n\n".join(parts), desc


def run(rows: list[dict], window: str, dry: bool) -> int:
    tracks = []
    for r in rows:
        try:
            t = track(r["video_id"], r.get("first_seen"), r)
            if t:
                tracks.append(t)
        except Exception as e:  # noqa: BLE001
            log(f"track {r['video_id']} failed: {type(e).__name__}: {e}")
    c = _conn()
    with c.cursor() as cur:
        cur.execute(DDL)
        for r in rows:
            t = next((t for t in tracks if t["id"] == r["video_id"]), None)
            cur.execute("UPDATE yt_likes SET kind=%s, verdict=coalesce(verdict,'{}'::jsonb) || %s::jsonb WHERE video_id=%s",
                        (t["kind"] if t else "not_music", json.dumps(t["verdict"] if t else {}), r["video_id"]))
    if not tracks:
        log(f"no new liked music {window} — no article (Jordan's rule)")
        c.close()
        return 0
    body, desc = compose(tracks, window)
    today = datetime.now().strftime("%Y-%m-%d")
    title = f"Liked and Listened — {datetime.now().strftime('%B %-d')}"
    if dry:
        print(body); c.close(); return 0
    import nova_journal as nj
    img = None
    try:
        img = nj.generate_image(nj.get_image_prompt(title, "listening closely to music: headphones, waveforms, vinyl, "
                                                    "a glowing equalizer", SECTION), width=1024, height=768, section=SECTION)
    except Exception as e:  # noqa: BLE001
        log(f"image failed (non-fatal): {e}")
    slug = f"{today}-liked-and-listened"
    if not nj.publish_hugo(title, body, SECTION, ["music", "liked", "listening", "youtube"], desc, image_path=img,
                           emoji="🎧", stable_slug=slug, profile=None):
        log("NOT PUBLISHED — quality guard rejected"); c.close(); return 1
    push = nj.git_push(SECTION, title)
    with c.cursor() as cur:
        cur.execute("UPDATE yt_likes SET written_at=now() WHERE video_id = ANY(%s)", ([t["id"] for t in tracks],))
    c.close()
    url = f"https://nova.digitalnoise.net/{SECTION}/{slug}/"
    log(f"published {url} ({len(tracks)} tracks, push {push})")
    print(url)
    return 0


def main(argv):
    flags = {a for a in argv[1:] if a.startswith("--")}
    if "--selftest" in flags:
        assert hard_reject({"duration": 30, "title": "x"}) and hard_reject({"duration": 300, "title": "My Podcast Theme Song"})
        assert hard_reject({"duration": 300, "title": "Episode 12: Bob"}) and not hard_reject({"duration": 242, "title": "Talk Talk - It's My Life"})
        assert estimate_key(MAJ) == "C major" and estimate_key(list(__import__("numpy").roll(MIN, 9))) == "A minor"
        cs = candidates(240, [{"start_time": 0, "value": 1.0}, {"start_time": 53, "value": 0.97}], [0.2] * 30 + [0.9] * 90, [60, 120])
        assert cs and all(3 <= s <= 240 - CLIP_S - 2 for s in cs) and all(b - a >= CLIP_S + 5 for a, b in zip(cs, cs[1:]))
        assert 51 in cs or any(abs(s - 51) < 20 for s in cs)
        assert plainly_music({"title": "Roxy Music - More Than This (Lyrics)", "channel": "x"})
        assert plainly_music({"title": "x", "channel": "Talk Talk - Topic"}) and not plainly_music({"title": "Riding bikes", "channel": "Speeed"})
        assert track_name({"artist": "Roxy Music", "track": "More Than This (Lyrics)"}) == "Roxy Music — More Than This"
        assert track_name({"title": "Talk Talk - It's My Life [Official 4K Video]"}) == "Talk Talk - It's My Life"
        assert plainly_music({"title": "DJ ÆDIDIAS - REAL FRIENDS", "channel": "ACKIRAI RECORDS"})
        assert "replay" in moment_words({"most_replayed": 0.9, "energy_change": 0, "section_start": False})
        assert kind_for({"duration": 256}, "dj_set") == "song" and kind_for({"duration": 3600, "title": "Live Set"}, "song") == "dj_set"
        print("selftest OK"); return 0
    if "--poll" in flags:
        return poll()
    hours = int(argv[argv.index("--hours") + 1]) if "--hours" in argv else 24
    dry = "--dry-run" in flags
    if "--test-latest" in flags:
        n = int(argv[argv.index("--test-latest") + 1])
        rows = [{"video_id": e["id"], "title": e["title"], "duration": e["duration"], "first_seen": None} for e in liked(n)]
        return run(rows, "over the weekend (test edition: the newest likes)", dry)
    c = _conn()
    with c.cursor() as cur:
        cur.execute(DDL)
        cur.execute("SELECT video_id, title, duration, first_seen FROM yt_likes WHERE NOT baseline AND written_at IS NULL "
                    "AND first_seen > now() - make_interval(hours => %s) ORDER BY first_seen", (hours,))
        rows = [dict(zip(("video_id", "title", "duration", "first_seen"), r)) for r in cur.fetchall()]
    c.close()
    return run(rows, "in the last 24 hours" if hours == 24 else f"in the last {hours} hours", dry)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
