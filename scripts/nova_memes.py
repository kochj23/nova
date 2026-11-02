#!/opt/homebrew/opt/python@3.14/bin/python3.14
"""nova_memes.py — "Memes and Friends": a daily article on the public memes that arrived over iMessage in the
last 24 hours, each one linked, analysed, and PLAYED in the Nova Speaks video (meme 1 -> analysis -> meme 2 ...).

Jordan 2026-11-02: "Amy and I are constantly sending text messages to each other and 90%+ of the time, it is just
a funny dog video or some other dumb joke. Without sending who sent what to who, when you see a imessage come in
that mentions any of the popular meme sites (IG, FB, whatever), analyze it in a new daily article ... linking to
it in the article with a analysis and playing it in the video with analysis after." Then: "I am cool with any
imessage as long as A) names are not revealed or any other personal details and B) you are sure it is a public meme."

PRIVACY (rule A): only the URL and the message time ever leave chat.db — never the handle, chat, sender
direction or message text. Nothing about who sent it reaches PG, the prompt or the article.

PUBLIC (rule B), all checked with NO login and NO cookies:
  * Instagram: the anonymous /embed/captioned/ page must render the post (private accounts' posts don't) and
    show at least MIN_LIKES likes. Reels carry a video_url there; /p/ posts are images.
  * Facebook / TikTok / YouTube Shorts: anonymous `yt-dlp -J` must succeed with >= MIN_VIEWS views (or
    >= MIN_LIKES likes).
  * Then the vision model looks at it and must call it a meme / creator post — not a private person's own
    family photo — and it must not be sexual (absolute rule). When unsure: skipped.
Only the PUBLIC creator handle is credited (it's their work).

VIDEO: each meme is cut to videos/clips/meme-<platform>-<code>-<a>-<b>.mp4 on the NAS — the whole thing when it
is <= FULL_MAX_S, else the best CLIP_S seconds; a still meme is held for STILL_S over silence. The article embeds
it as {{< meme id start end title >}}: the platform's own embed on the site (nothing rehosted), a played clip in
the video (nova_speaks.py MEMES).

Usage: nova_memes.py --scheduled [--hours 24] [--dry-run]
       nova_memes.py --selftest
"""
from __future__ import annotations

import base64
import html
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

SECTION = "memes"
WORK = Path("/Volumes/Data/AI/memes")
CLIPS = Path("/Volumes/nas/nova-fs/videos/clips")
CHAT_DB = Path.home() / "Library/Messages/chat.db"
YTDLP = "/opt/homebrew/bin/yt-dlp"
VLM = "qwen3-vl:30b"
UA = "Mozilla/5.0 (Macintosh)"     # a full Safari UA gets the JS-only shell (no post) from the IG embed endpoint
MIN_LIKES, MIN_VIEWS = 1000, 10_000
FULL_MAX_S, CLIP_S, STILL_S = 60, 20, 6
MAX_MEMES = 12
APPLE_EPOCH = 978307200
URL_RX = re.compile(r"https?://[^\s<>\"'\x00-\x1f]+")
DDL = """CREATE TABLE IF NOT EXISTS meme_links (
  key text PRIMARY KEY, platform text NOT NULL, url text NOT NULL, seen_at timestamptz NOT NULL,
  verdict jsonb, status text, written_at timestamptz)"""


def log(m):
    print(f"[memes {time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


def _conn():
    import nova_dsn
    c = nova_dsn.pg_connect(); c.autocommit = True
    return c


# ── harvest: URLs + time only ───────────────────────────────────────────────────────────
def key_for(url: str) -> tuple[str, str] | None:
    """URL -> (platform, provisional key) for meme platforms, else None. Facebook share links resolve later. Pure."""
    u = url.rstrip(").,!?;:'\"")
    m = re.search(r"instagram\.com/(?:[\w.]+/)?(?:p|reels?|tv)/([\w-]{5,})", u)
    if m:
        return "ig", m.group(1)
    if re.search(r"(?:^|[/.])(?:facebook\.com|fb\.watch)/", u):
        m = re.search(r"[?&]v=(\d+)|/videos/(?:[\w.-]+/)?(\d+)|/reel/(\d+)", u)
        return "fb", next((g for g in m.groups() if g), None) if m else None
    m = re.search(r"tiktok\.com/(?:@[\w.-]+/video/|v/|embed/v2/)(\d+)", u)
    if m:
        return "tt", m.group(1)
    if re.search(r"(?:vm|vt)\.tiktok\.com/\w+", u):
        return "tt", None
    m = re.search(r"youtube\.com/shorts/([\w-]{11})", u)
    if m:
        return "yt", m.group(1)
    return None


def harvest(hours: int) -> list[dict]:
    """[{url, platform, code, seen_at}] from every chat in the window, oldest first, one per meme. Reads ONLY
    message.text / attributedBody (for URLs) and message.date — never handle, chat or direction."""
    since = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()) - APPLE_EPOCH
    db = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    rows = db.execute("SELECT text, attributedBody, date FROM message WHERE date/1000000000 > ? ORDER BY date",
                      (since,)).fetchall()
    db.close()
    out, seen = [], set()
    for text, blob, d in rows:
        src = text or (blob.decode("utf-8", "ignore") if blob else "")
        for url in URL_RX.findall(src):
            k = key_for(url)
            if not k:
                continue
            ident = (k[0], k[1] or url)
            if ident in seen:
                continue
            seen.add(ident)
            out.append({"url": url.rstrip(").,!?;:'\""), "platform": k[0], "code": k[1],
                        "seen_at": datetime.fromtimestamp(d / 1e9 + APPLE_EPOCH, timezone.utc)})
    return out


# ── public check (no login, no cookies) ─────────────────────────────────────────────────
def _get(url: str, timeout=20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def count_of(s: str | None) -> int:
    """'83,416' / '1.2M' / '9.6K' -> int. Pure."""
    if not s:
        return 0
    s = s.replace(",", "").strip()
    mult = {"k": 1_000, "m": 1_000_000}.get(s[-1:].lower(), 1)
    try:
        return int(float(s[:-1] if mult > 1 else s) * mult)
    except ValueError:
        return 0


def parse_ig_embed(page: str) -> dict:
    """The anonymous Instagram embed page -> {creator, image, video, caption, likes}. Pure."""
    def unesc(v):
        return html.unescape(v.replace("\\\\/", "/").replace("\\/", "/").replace("\\u0026", "&"))
    u = re.search(r'class="Username"[^>]*>(?:<[^>]+>)*([^<]+)', page)
    img = re.search(r'class="EmbeddedMediaImage"[^>]*src="([^"]+)"', page)
    vid = re.search(r'\\*"video_url\\*"\s*:\s*\\*"(https?:[^"]+?)\\*"', page)
    cap = re.search(r'class="Caption"[^>]*>(.*?)</div>', page, re.S)
    caption = ""
    if cap:
        c = re.sub(r'<a class="CaptionUsername".*?</a>', "", cap.group(1), flags=re.S)
        c = re.sub(r'<div class="CaptionComments".*', "", c, flags=re.S)
        caption = html.unescape(re.sub(r"<[^>]+>", " ", c))
        caption = re.sub(r"\s+", " ", caption).strip()
    likes = re.search(r"([\d,.]+[KkMm]?) likes", page)
    return {"creator": html.unescape(u.group(1)).strip() if u else "", "image": unesc(img.group(1)) if img else "",
            "video": unesc(vid.group(1)) if vid else "", "caption": caption[:600],
            "likes": count_of(likes.group(1)) if likes else 0}


def ytdlp_json(url: str) -> dict:
    r = subprocess.run([YTDLP, "-J", "--no-warnings", "--no-playlist", "--ignore-config", url],
                       capture_output=True, text=True, timeout=90)
    try:
        return json.loads(r.stdout) if r.returncode == 0 else {}
    except json.JSONDecodeError:
        return {}


def public_meta(item: dict) -> dict | None:
    """Fetch anonymously; None unless clearly public with real reach. Fills key/creator/reach/media fields."""
    p = item["platform"]
    if p == "ig":
        try:
            e = parse_ig_embed(_get(f"https://www.instagram.com/p/{item['code']}/embed/captioned/"))
        except Exception as ex:  # noqa: BLE001
            log(f"ig {item['code']}: embed fetch failed ({type(ex).__name__}) — skipped"); return None
        if not e["creator"] or not (e["image"] or e["video"]):
            log(f"ig {item['code']}: not publicly embeddable — skipped"); return None
        if e["likes"] < MIN_LIKES:
            log(f"ig {item['code']}: {e['likes']} likes < {MIN_LIKES} — not clearly a public meme, skipped"); return None
        return {**item, "key": f"meme-ig-{item['code']}", "creator": "@" + e["creator"], "reach": f"{e['likes']:,} likes",
                "caption": e["caption"], "image": e["image"], "video": e["video"], "duration": None,
                "source": f"https://www.instagram.com/p/{item['code']}/", "platform_name": "Instagram"}
    j = ytdlp_json(item["url"])
    if not j or not j.get("id"):
        log(f"{p} {item['url'][:60]}: not fetchable without login — skipped"); return None
    views, likes = j.get("view_count") or 0, j.get("like_count") or 0
    if views < MIN_VIEWS and likes < MIN_LIKES:
        log(f"{p} {j['id']}: {views} views / {likes} likes — not clearly a public meme, skipped"); return None
    name = {"fb": "Facebook", "tt": "TikTok", "yt": "YouTube Shorts"}[p]
    creator = j.get("uploader") or j.get("channel") or ""
    if p == "tt" and j.get("uploader_id"):
        creator = "@" + str(j["uploader"] or j["uploader_id"]).lstrip("@")
    reach = f"{views:,} views" if views else f"{likes:,} likes"
    return {**item, "code": str(j["id"]), "key": f"meme-{p}-{j['id']}", "creator": creator, "reach": reach,
            "caption": (j.get("title") or "") + " " + (j.get("description") or "")[:400], "image": j.get("thumbnail") or "",
            "video": "ytdlp", "duration": j.get("duration"), "source": j.get("webpage_url") or item["url"],
            "platform_name": name}


# ── media ───────────────────────────────────────────────────────────────────────────────
def ffprobe_dur(path: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def fetch_media(m: dict, d: Path) -> dict:
    """Download (anonymously) into d: mp4 for videos, jpg for stills. Adds 'mp4' or 'jpg' and 'duration'."""
    d.mkdir(parents=True, exist_ok=True)
    if m["video"] == "ytdlp":
        out = d / "src.mp4"
        subprocess.run([YTDLP, "--no-warnings", "--no-playlist", "--ignore-config", "-f", "b[height<=1080]/bv*[height<=1080]+ba/b",
                        "--merge-output-format", "mp4", "-o", str(out), m["source"]], capture_output=True, timeout=600)
        if out.exists():
            return {**m, "mp4": str(out), "duration": ffprobe_dur(str(out))}
    elif m["video"]:
        out = d / "src.mp4"
        req = urllib.request.Request(m["video"], headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r:
            out.write_bytes(r.read())
        if ffprobe_dur(str(out)) > 0:
            return {**m, "mp4": str(out), "duration": ffprobe_dur(str(out))}
    if m["image"]:
        out = d / "src.jpg"
        req = urllib.request.Request(m["image"], headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            out.write_bytes(r.read())
        return {**m, "jpg": str(out), "duration": None}
    raise RuntimeError("no media")


def frames(m: dict, d: Path, n=4) -> list[str]:
    if m.get("jpg"):
        return [m["jpg"]]
    dur = m["duration"] or 1
    out = []
    for i in range(n):
        f = d / f"f{i}.jpg"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{dur * (i + 0.5) / n:.2f}", "-i", m["mp4"],
                        "-frames:v", "1", "-vf", "scale=768:-2", str(f)], check=False)
        if f.exists():
            out.append(str(f))
    return out


def speech(m: dict, d: Path) -> str:
    if not m.get("mp4"):
        return ""
    wav = d / "a.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", m["mp4"], "-vn", "-ac", "1", "-ar", "16000",
                    "-t", "180", str(wav)], check=False)
    if not wav.exists() or wav.stat().st_size < 20_000:
        return ""
    try:
        import mlx_whisper
        r = mlx_whisper.transcribe(str(wav), path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
                                   condition_on_previous_text=False, no_speech_threshold=0.6)
        return " ".join(s["text"].strip() for s in r.get("segments", []) if s.get("no_speech_prob", 0) < 0.6)[:1500]
    except Exception as e:  # noqa: BLE001
        log(f"whisper failed (non-fatal): {e}")
        return ""


def window(dur: float | None, best: int | None) -> tuple[int, int]:
    """(start, end) seconds to play: a still -> (0, STILL_S); a short video -> whole; a long one -> best CLIP_S. Pure."""
    if not dur:
        return 0, STILL_S
    if dur <= FULL_MAX_S:
        return 0, max(1, int(round(dur)))
    s = max(0, min(int(best or 0), int(dur) - CLIP_S))
    return s, s + CLIP_S


def cut(m: dict, a: int, b: int) -> Path:
    CLIPS.mkdir(parents=True, exist_ok=True)
    out = CLIPS / f"{m['key']}-{a}-{b}.mp4"
    if out.exists():
        return out
    tmp = out.with_suffix(".part.mp4")
    if m.get("jpg"):        # a still meme: held frame over silence
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-i", m["jpg"], "-f", "lavfi", "-i",
               "anullsrc=r=48000:cl=stereo", "-t", str(b - a), "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
               "-r", "25", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-shortest", str(tmp)]
    else:
        # silent memes have no audio stream: use theirs when present, else a silent track (the renderer needs one)
        has_audio = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                                    "-of", "csv=p=0", m["mp4"]], capture_output=True, text=True).stdout.strip()
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(a), "-i", m["mp4"], "-f", "lavfi", "-i",
               "anullsrc=r=48000:cl=stereo", "-t", str(b - a), "-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a",
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p", "-r", "25", "-c:v", "libx264", "-preset",
               "veryfast", "-crf", "20", "-c:a", "aac", "-ar", "48000", "-ac", "2", str(tmp)]
    subprocess.run(cmd, check=True, timeout=300)
    tmp.rename(out)
    return out


# ── the vision model ────────────────────────────────────────────────────────────────────
def vlm_json(prompt: str, images: list[str], max_tokens=4000) -> dict:
    """qwen3-vl sometimes thinks anyway (think=False ignored) and spends the whole budget in `thinking` with empty
    content (2026-11-02 test) — so /no_think, a large budget, and a retry per host when content comes back empty."""
    import nova_beliefs as nb
    imgs = [base64.b64encode(Path(p).read_bytes()).decode() for p in images[:4]]
    body = json.dumps({"model": VLM, "stream": False, "think": False, "keep_alive": "30m", "format": "json",
                       "options": {"temperature": 0.5, "num_predict": max_tokens, "num_ctx": 16384},
                       "messages": [{"role": "user", "content": prompt + "\n/no_think", "images": imgs}]}).encode()
    for base in nb.LLM_URLS:
        for attempt in range(2):
            try:
                req = urllib.request.Request(base + "/api/chat", data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=300) as r:   # healthy ~30 s; 11436 hung 15 min (2026-11-02)
                    out = (json.load(r).get("message", {}).get("content", "") or "").strip()
                if out:
                    return json.loads(out)
                log(f"vlm {base}: empty content (thought instead), retry {attempt + 1}")
            except Exception as e:  # noqa: BLE001
                log(f"vlm {base}: {type(e).__name__}: {e}")
    return {}


def judge_and_write(m: dict, imgs: list[str], said: str) -> dict:
    kind = "still image" if m.get("jpg") else f"{m['duration']:.0f}-second video"
    long_note = (f"\nIt is longer than {FULL_MAX_S}s, so give best_start: the second where the funniest {CLIP_S}s begins."
                 if m.get("duration") and m["duration"] > FULL_MAX_S else "")
    prompt = f"""You are Nova, writing "Memes and Friends", a warm, funny daily column on the internet memes going
around the family group chats. You are looking at one post: a {kind} from {m['platform_name']} by the public
account {m['creator']} ({m['reach']}). The images are {'the post' if m.get('jpg') else 'frames from the video, in order'}.
POST CAPTION (from the creator): {m['caption'][:500] or '(none)'}
SPEECH IN THE VIDEO (speech recognition, may be wrong): {said[:1200] or '(none)'}{long_note}

First JUDGE it, strictly:
- is_meme: true only if this is public entertainment content — a meme, joke, funny animal clip, skit, creator
  post, viral video. false if it looks like a private person's own family/personal photo or video, a news
  article, an ad, a scam, or anything you can't make sense of.
- sexual: true if it contains nudity or sexual content or innuendo of any kind.
- personal_info: true if it shows a private person's identifying details (an address, phone number, a
  document, a non-public person's full name).
Then WRITE (only if is_meme and not sexual and not personal_info):
- title: a short, funny headline for this meme (max 8 words), NOT the creator's caption.
- what: 1-2 sentences, plainly: what happens / what it shows. Only what you can see or hear.
- analysis: 2 short paragraphs: why it's funny (the setup, the turn, the timing, the expression on the dog's
  face), and what kind of meme it is / why people send it to each other. Warm, dry, a little mock-serious.
- rating: an integer 1-10, and rating_line: one sentence justifying it.
Rules: describe ONLY what is in the post. Never invent names, places, or backstory. Never mention who sent it,
group chats' members, or any person by name except the public creator account. No hashtags. No emoji.
Banned words: delve, testament, tapestry, masterclass, vibe, iconic, hilarious.
Return JSON: {{"is_meme": bool, "sexual": bool, "personal_info": bool, "why_not": "...", "title": "...",
"what": "...", "analysis": ["...", "..."], "rating": 7, "rating_line": "...", "best_start": 0}}"""
    return vlm_json(prompt, imgs)


def meme(item: dict) -> dict | None:
    m = public_meta(item)
    if not m:
        return None
    d = WORK / m["key"]
    m = fetch_media(m, d)
    imgs = frames(m, d)
    if not imgs:
        log(f"{m['key']}: no frames — skipped"); return None
    said = speech(m, d)
    v = judge_and_write(m, imgs, said)
    m["verdict"] = {k: v.get(k) for k in ("is_meme", "sexual", "personal_info", "why_not")}
    if not v.get("is_meme") or v.get("sexual") or v.get("personal_info") or not v.get("analysis"):
        log(f"{m['key']}: rejected {m['verdict']}"); m["rejected"] = True; return m
    a, b = window(m.get("duration"), v.get("best_start"))
    cut(m, a, b)
    m.update(text=v, a=a, b=b)
    log(f"{m['key']}: OK '{v.get('title')}' {m['platform_name']} {m['creator']} {m['reach']} play {a}-{b}s")
    return m


# ── article ─────────────────────────────────────────────────────────────────────────────
def esc(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace('"', "'")).strip()


PII_RX = re.compile(r"\+?\d[\d\s().-]{8,}\d|[\w.+-]+@[\w-]+\.[\w.]+")


def compose(memes: list[dict], window_txt: str) -> tuple[str, str]:
    """(body markdown, description). Each meme: heading, credit, the meme (played in the video), then the
    analysis — the order Jordan asked for. Pure."""
    n = len(memes)
    parts = [f"{n} meme{'s' if n != 1 else ''} went around the family group chats {window_txt}. I won't say who sent "
             "what — that stays in the chat — but every one of these is a public post, credited to the account that "
             "made it, and every one got my full, undivided, faintly ridiculous attention."]
    for i, m in enumerate(memes, 1):
        x = m["text"]
        title = esc(x.get("title") or f"Meme {i}")
        parts.append(f"## {i}. {title}")
        kind = "photo" if m.get("jpg") else (f"{m['duration']:.0f}s video" if m.get("duration") else "video")
        parts.append(f"*{m['platform_name']} · {esc(m['creator'])} · {m['reach']} · {kind} · "
                     f"[see the original]({m['source']})*")
        if x.get("what"):
            parts.append(esc(x["what"]))
        credit = f"{m['creator']} on {m['platform_name']}"
        parts.append(f'{{{{< meme id="{m["key"]}" start="{m["a"]}" end="{m["b"]}" title="{esc(credit)[:80]}" >}}}}')
        parts += [esc(p) for p in (x.get("analysis") or [])][:3]
        if x.get("rating"):
            parts.append(f"*Rating: {int(x['rating'])}/10. {esc(x.get('rating_line') or '')}*".replace(" *", "*"))
    body = PII_RX.sub("[removed]", "\n\n".join(parts))
    best = max(memes, key=lambda m: m["text"].get("rating") or 0)
    desc = f"{n} public meme{'s' if n != 1 else ''} from the family group chats, rated and taken far too seriously — " \
           f"today's best: {esc(best['text'].get('title'))}."
    return body, desc


def run(hours: int, dry: bool, window_txt: str) -> int:
    items = harvest(hours)
    log(f"{len(items)} meme link(s) in the last {hours}h")
    c = _conn()
    with c.cursor() as cur:
        cur.execute(DDL)
        cur.execute("SELECT key FROM meme_links WHERE written_at IS NOT NULL")
        done = {r[0] for r in cur.fetchall()}
    good, keys = [], set()
    for it in items:
        if len(good) >= MAX_MEMES:
            break
        try:
            m = meme(it)
        except Exception as e:  # noqa: BLE001
            log(f"{it['platform']} {it.get('code')}: failed {type(e).__name__}: {e}"); continue
        if not m or m["key"] in keys:
            continue
        keys.add(m["key"])
        with c.cursor() as cur:
            cur.execute("INSERT INTO meme_links (key, platform, url, seen_at, verdict, status) VALUES (%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (key) DO UPDATE SET verdict=EXCLUDED.verdict, status=EXCLUDED.status",
                        (m["key"], m["platform"], m["source"], m["seen_at"], json.dumps(m.get("verdict") or {}),
                         "rejected" if m.get("rejected") else "ok"))
        if not m.get("rejected") and m["key"] not in done:
            good.append(m)
    if not good:
        log("no public memes in the window — no article"); c.close(); return 0
    body, desc = compose(good, window_txt)
    today = datetime.now().strftime("%Y-%m-%d")
    title = f"Memes and Friends — {datetime.now().strftime('%B %-d')}"
    if dry:
        print(body); c.close(); return 0
    import nova_journal as nj
    img = None
    try:
        img = nj.generate_image(nj.get_image_prompt(title, "a cheerful dog staring at a phone full of memes, laughing "
                                                    "emoji energy, cozy living room", SECTION), width=1024, height=768, section=SECTION)
    except Exception as e:  # noqa: BLE001
        log(f"image failed (non-fatal): {e}")
    slug = f"{today}-memes-and-friends"
    if not nj.publish_hugo(title, body, SECTION, ["memes", "funny", "dogs", "internet"], desc, image_path=img,
                           emoji="🐶", stable_slug=slug, profile=None):
        log("NOT PUBLISHED — quality guard rejected"); c.close(); return 1
    push = nj.git_push(SECTION, title)
    with c.cursor() as cur:
        cur.execute("UPDATE meme_links SET written_at=now() WHERE key = ANY(%s)", ([m["key"] for m in good],))
    c.close()
    url = f"https://nova.digitalnoise.net/{SECTION}/{slug}/"
    log(f"published {url} ({len(good)} memes, push {push})")
    print(url)
    return 0


def selftest() -> int:
    assert key_for("https://www.instagram.com/reel/DQx1abcDEF/?igsh=xyz") == ("ig", "DQx1abcDEF")
    assert key_for("https://www.instagram.com/p/De-yut7xkhO/") == ("ig", "De-yut7xkhO")
    assert key_for("https://www.facebook.com/watch/?v=123456789") == ("fb", "123456789")
    assert key_for("https://www.facebook.com/share/r/1AbCd/") == ("fb", None)
    assert key_for("https://www.tiktok.com/@dogs/video/7312345678901234567") == ("tt", "7312345678901234567")
    assert key_for("https://youtube.com/shorts/abcdefghijk") == ("yt", "abcdefghijk")
    assert key_for("https://votenoon40.org/x") is None and key_for("https://ask234.net/a") is None
    assert count_of("83,416") == 83416 and count_of("1.2M") == 1_200_000 and count_of("9.6K") == 9600
    assert window(None, None) == (0, STILL_S) and window(32.2, None) == (0, 32) and window(200, 150) == (150, 170)
    assert window(200, 999) == (180, 200)
    page = ('<span class="Username"><span>dog.memes</span></span><img class="EmbeddedMediaImage" src="https://x/a.jpg?a=1&amp;b=2">'
            '<div class="Caption"><a class="CaptionUsername">dog.memes</a> when the zoomies hit<br></div> 12.5K likes')
    e = parse_ig_embed(page)
    assert e["creator"] == "dog.memes" and e["image"] == "https://x/a.jpg?a=1&b=2" and e["likes"] == 12500
    assert e["caption"] == "when the zoomies hit"
    m = {"key": "meme-ig-ABC", "platform_name": "Instagram", "creator": "@dog.memes", "reach": "12,500 likes",
         "source": "https://www.instagram.com/p/ABC/", "jpg": "x.jpg", "a": 0, "b": 6,
         "text": {"title": "Zoomies", "what": "A dog. Call 818-555-1212.", "analysis": ["p1", "p2"], "rating": 8, "rating_line": "Fast."}}
    body, desc = compose([m], "in the last 24 hours")
    assert '{{< meme id="meme-ig-ABC" start="0" end="6" title="@dog.memes on Instagram" >}}' in body
    assert body.index("{{< meme") < body.index("p1") and "818-555" not in body and "Zoomies" in desc
    print("selftest OK")
    return 0


def main(argv):
    if "--selftest" in argv:
        return selftest()
    hours = int(argv[argv.index("--hours") + 1]) if "--hours" in argv else 24
    txt = "in the last 24 hours" if hours == 24 else f"in the last {hours} hours"
    return run(hours, "--dry-run" in argv, txt)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
