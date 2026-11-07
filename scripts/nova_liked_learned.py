#!/opt/homebrew/opt/python@3.14/bin/python3.14
"""nova_liked_learned.py — "Liked and Learned": a daily article about everything Jordan LIKED on YouTube in the
last 24 rolling hours that is NOT music, and what those likes say he is most interested in.

Jordan 2026-11-06: "a daily analysis of everything that I liked that was not music just like the music Listened
and Liked article ... Summarize what you think I am most interested in based on those non-music likes. If there
is no likes, there is not article." His choices the same day: YouTube likes only; a NEW section, Liked and
Learned (/learned/); the interest summary covers the last 24 hours AND the 30-day trend (new / growing /
constant); the Nova Speaks video plays 15 s clips of the liked videos (same pipeline as the music article).

LIKES come from nova_liked_music.py --poll (every 15 min -> nova_ops.yt_likes, first_seen = like time). The
music job (07:30) labels each like: song / dj_set / live, or not_music. Anything still unlabelled when this runs
is classified here with the SAME classifier (nova_liked_music.classify), so a song is never written up twice.

PER VIDEO (all local): yt-dlp metadata + YouTube's captions (auto or uploaded; Whisper on the first 10 minutes
when there are none), "most replayed" heatmap. Candidate 15 s moments = heatmap peaks + evenly spaced points,
each with the words spoken there. qwen3:30b writes what it is, the takeaways, why it may have caught Little
Mister's eye, 3-6 topics (reusing the 30-day vocabulary), and picks the clips. Only the picked 15 s are
downloaded (yt-dlp --download-sections), normalised to videos/clips/<id>-<start>-<end>.mp4 on the NAS, and
embedded as {{< youtube id start end >}}: a cued player on the site, a played clip in the Nova Speaks video.

INTERESTS: topics are stored per like (yt_likes.verdict->topics). The trend is counted in Python (pure
`trend()`): NEW = today, not in the prior 30 days; GROWING = today's share well above the 30-day share;
CONSTANT = steady across the month. The model writes the prose from those counts and the day's videos only.

Usage: nova_liked_learned.py --scheduled [--hours 24] [--dry-run] [--max N]
       nova_liked_learned.py --selftest
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_liked_music as nlm  # noqa: E402 — the poll, classifier, yt-dlp + LLM helpers this mode reuses

SECTION = "learned"
WORK = Path("/Volumes/Data/AI/liked-learned")
CLIPS = nlm.CLIPS
CLIP_S = 15
MAX_VIDEOS = 8               # written up in full per day; the rest are listed (runtime: ~3-5 min per video)
MUSIC_KINDS = ("song", "dj_set", "live")
TREND_DAYS = 30


def log(m):
    print(f"[liked-learned {time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


# ── pure helpers ──────────────────────────────────────────────────────────────────────────
def parse_json3(text: str) -> list[dict]:
    """YouTube json3 captions -> [{"start": s, "text": ...}] (one entry per caption event). Pure."""
    try:
        d = json.loads(text)
    except ValueError:
        return []
    out = []
    for ev in d.get("events") or []:
        words = "".join(s.get("utf8", "") for s in ev.get("segs") or []).replace("\n", " ").strip()
        if words and words != "[Music]":
            out.append({"start": int(ev.get("tStartMs", 0)) // 1000, "text": words})
    return out


def moments(dur: int, heat: list, n: int = 8) -> list[int]:
    """Start seconds of candidate CLIP_S windows: heatmap peaks first, then evenly spaced points. Pure."""
    if dur <= CLIP_S + 4:
        return [0]
    hi = max(0, dur - CLIP_S - 2)
    scored = [(float(h.get("value", 0)) * 2.0, int(h.get("start_time", 0)) - 2) for h in heat or [] if h.get("start_time", 0) > 5]
    scored += [(0.3, int(dur * f)) for f in (0.08, 0.2, 0.33, 0.45, 0.58, 0.7, 0.82)]
    out = []
    for _, s in sorted(scored, reverse=True):
        s = min(max(3, s), hi)
        if all(abs(s - o) >= CLIP_S + 5 for o in out):
            out.append(s)
        if len(out) >= n:
            break
    return sorted(out)


def words_at(caps: list[dict], s: int, e: int) -> str:
    return " ".join(c["text"] for c in caps if s - 1 <= c["start"] <= e)[:240]


def transcript_digest(caps: list[dict], limit: int = 6000) -> str:
    """Timestamped transcript, evenly thinned to `limit` chars so a 2-hour talk still shows its whole arc. Pure."""
    lines = [f"[{c['start'] // 60}:{c['start'] % 60:02d}] {c['text']}" for c in caps]
    full = " ".join(lines)
    if len(full) <= limit:
        return full
    step = len(full) / limit
    keep, budget = [], limit
    for i, ln in enumerate(lines):
        if i % max(1, round(step)) == 0 and budget > 0:
            keep.append(ln); budget -= len(ln) + 1
    return " ".join(keep)


def norm_topic(t: str) -> str:
    t = re.sub(r"[^a-z0-9 &+'-]", "", (t or "").lower()).strip()
    t = re.sub(r"\s+", " ", t)[:40]
    return "" if t in CATEGORY_TOPICS_LC else t


CATEGORY_TOPICS_LC = {"people & blogs", "people and blogs", "entertainment", "film & animation", "howto & style",
                      "news & politics", "science & technology", "travel & events", "nonprofits & activism", "youtube"}
BANNED = {r"\bvibes?\b": "feel", r"\bdelve\b": "dig", r"\btestament\b": "proof", r"\bmasterclass\b": "lesson",
          r"\btapestry\b": "mix", r"\bgame-changer\b": "big shift"}


def scrub(x):
    """Banned words out of every string the model wrote (it slips "vibe" in despite being told). Pure."""
    if isinstance(x, str):
        for rx, sub in BANNED.items():
            x = re.sub(rx, sub, x, flags=re.I)
        return x
    if isinstance(x, list):
        return [scrub(i) for i in x]
    if isinstance(x, dict):
        return {k: scrub(v) for k, v in x.items()}
    return x


def trend(today: list[list[str]], prior: list[list[str]]) -> dict:
    """today/prior: topic lists, one per liked video. -> {"new", "growing", "constant", "today", "prior"}. Pure.
    NEW: in today's likes, never in the prior 30 days. GROWING: today's share >= 2x its 30-day share (and on 2+
    videos today, or 1 when today is small). CONSTANT: on >= 4 prior days' worth of likes and present today."""
    tc = Counter(t for ts in today for t in set(ts))
    pc = Counter(t for ts in prior for t in set(ts))
    nt, np_ = max(1, len(today)), max(1, len(prior))
    new = sorted([t for t in tc if pc[t] == 0], key=lambda t: -tc[t])
    growing = sorted([t for t in tc if pc[t] and tc[t] / nt > 2 * pc[t] / np_ and (tc[t] >= 2 or len(today) <= 3)],
                     key=lambda t: -(tc[t] / nt - pc[t] / np_))
    constant = sorted([t for t in tc if pc[t] >= 4 and t not in growing], key=lambda t: -pc[t])
    return {"new": new[:6], "growing": growing[:6], "constant": constant[:6],
            "today": tc.most_common(10), "prior": pc.most_common(15)}


def esc(s: str) -> str:
    return (s or "").replace('"', "'").replace("<", "").replace(">", "")


def hms(s: int) -> str:
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def compose(videos: list[dict], extra: list[dict], interests: dict, tr: dict, window: str) -> tuple[str, str]:
    """(body markdown, description). Pure."""
    n = len(videos) + len(extra)
    parts = [f"Little Mister liked {n} video{'s' if n != 1 else ''} on YouTube {window} that weren't music. I watched "
             "them, read along with what was said and picked the moments worth seeing. In the video version of this "
             "page you see those moments; here, each one is cued up in the player."]
    parts.append("## What he's into right now")
    parts += [p for p in interests.get("paragraphs") or [] if isinstance(p, str) and len(p) > 30]
    rows = [("New this month", tr["new"]), ("Growing", tr["growing"]), ("Constant", tr["constant"])]
    lines = [f"- **{k}:** {', '.join(v)}" for k, v in rows if v]
    if lines:
        parts.append("\n".join(lines))
    for v in videos:
        x = v["text"]
        parts.append(f"## {esc(v['title'])}")
        parts.append(f"*{esc(v['channel'])} · {hms(v['duration'])}"
                     + (f" · liked {v['liked_at'].astimezone():%a %-I:%M %p}" if v.get("liked_at") else "")
                     + f" · [watch on YouTube](https://www.youtube.com/watch?v={v['id']})*")
        if x.get("what_it_is"):
            parts.append(x["what_it_is"])
        tk = [t for t in x.get("takeaways") or [] if isinstance(t, str) and t.strip()]
        if tk:
            parts.append("\n".join(f"- {t}" for t in tk))
        if x.get("why_him"):
            parts.append(f"**Why it caught his eye:** {x['why_him']}")
        for i, h in enumerate(x["highlights"], 1):
            s = h["start"]
            parts.append(f"**Worth seeing{'' if len(x['highlights']) == 1 else f' #{i}'} — {hms(s)}.** {h.get('why') or ''}".rstrip())
            parts.append(f'{{{{< youtube id="{v["id"]}" start="{s}" end="{s + CLIP_S}" title="{esc(v["title"])[:80]}" >}}}}')
        if v.get("topics"):
            parts.append(f"*Topics: {', '.join(v['topics'])}*")
    if extra:
        parts.append("## Also liked")
        parts.append("\n".join(f"- [{esc(e['title'])}](https://www.youtube.com/watch?v={e['video_id']})"
                               + (f" — {esc(e.get('channel') or '')}" if e.get("channel") else "") for e in extra))
    head = ", ".join(esc(v["channel"]) for v in videos[:3])
    desc = (interests.get("headline") or f"What Little Mister liked on YouTube {window} that wasn't music").strip()
    desc = f"{desc} — with {head}{' and more' if n > 3 else ''}." if head else desc
    return "\n\n".join(parts), desc


# ── I/O ───────────────────────────────────────────────────────────────────────────────────
def captions(vid: str, meta: dict) -> list[dict]:
    WORK.mkdir(parents=True, exist_ok=True)
    base = WORK / vid
    for f in WORK.glob(f"{vid}*.json3"):
        return parse_json3(f.read_text(errors="replace"))
    try:
        nlm.ytdlp("--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en,-live_chat",
                  "--sub-format", "json3", "-o", str(base), f"https://www.youtube.com/watch?v={vid}", timeout=300)
    except Exception as e:  # noqa: BLE001
        log(f"{vid}: captions unavailable ({e})")
    files = sorted(WORK.glob(f"{vid}*.json3"), key=lambda p: (".en." not in p.name, p.name))
    caps = parse_json3(files[0].read_text(errors="replace")) if files else []
    if caps:
        return caps
    # no captions: Whisper on the first 10 minutes of audio
    wav = WORK / f"{vid}.wav"
    try:
        nlm.ytdlp("-f", "ba", "--download-sections", "*0-600", "-x", "--audio-format", "wav", "-o", str(WORK / f"{vid}.%(ext)s"),
                  f"https://www.youtube.com/watch?v={vid}", timeout=900)
        return nlm.lyrics(str(wav)) if wav.exists() else []
    except Exception as e:  # noqa: BLE001
        log(f"{vid}: no captions and no audio ({e})")
        return []


def cut(vid: str, s: int, e: int) -> Path | None:
    CLIPS.mkdir(parents=True, exist_ok=True)
    out = CLIPS / f"{vid}-{s}-{e}.mp4"
    if out.exists():
        return out
    raw = WORK / f"{vid}-{s}-{e}.raw.mp4"
    try:
        nlm.ytdlp("-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]", "--download-sections", f"*{s}-{e}",
                  "--force-keyframes-at-cuts", "--merge-output-format", "mp4", "-o", str(raw),
                  f"https://www.youtube.com/watch?v={vid}", timeout=900)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-t", str(e - s),
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "192k", str(out)],
                       check=True, timeout=600)
        return out
    except Exception as ex:  # noqa: BLE001
        log(f"clip {vid} {s}-{e} failed ({ex}) — the article keeps the cued player, the video narrates over it")
        return None


def write_video(v: dict, vocab: list[str]) -> dict:
    n = 1 if v["duration"] < 180 else 2
    cands = [{"start": s, "at": hms(s), "words": words_at(v["caps"], s, s + CLIP_S),
              "most_replayed": round(max([h["value"] for h in v["heat"] if s - 3 <= h["start_time"] <= s + CLIP_S] or [0]), 2)}
             for s in v["moments"]]
    x = nlm.llm_json(f"""You are Nova, an AI writing for your journal's "Liked and Learned" section about a YouTube video
that Little Mister (the person you live with) liked. It is not a song: it is talk, teaching, comedy or the like. Write like a smart, warm friend who watched it:
what it is, what it actually says, what is worth taking away. Use ONLY the facts below. The transcript comes from
captions or speech recognition and may contain errors: paraphrase, quote at most one short line that clearly makes
sense. Do not invent names, numbers, dates or claims that are not in the facts. Never mention field names
(most_replayed, words). Banned words: delve, testament, masterclass, tapestry, game-changer, vibe.
FACTS
Title: {v['title']} | Channel: {v['channel']} | Length: {hms(v['duration'])} | YouTube category: {v.get('categories')}
Description (start): {(v.get('description') or '')[:700]}
Transcript (timestamped, thinned): {transcript_digest(v['caps']) or '(none available)'}
CANDIDATE MOMENTS (pick exactly {n} by their start values, for the clips shown in the video):
{json.dumps(cands, ensure_ascii=False)}
TOPIC VOCABULARY already used this month (reuse these exact strings when they fit; otherwise coin a short lowercase one):
{json.dumps(vocab[:80])}
Answer JSON: {{"what_it_is": "one paragraph", "takeaways": ["2-4 short bullets of what it actually teaches or argues"],
 "why_him": "one or two sentences on what makes the video worth liking: its idea, craft or humour. Say NOTHING about Little Mister's feelings, experiences, mood, habits, life or what he usually watches",
 "topics": ["2-5 short lowercase SUBJECTS a library would shelve it under, e.g. 'music theory', 'drumming', 'home lab', 'woodworking'. Never a person, band, channel or title word, never a YouTube category name"],
 "highlights": [EXACTLY {n} items: {{"start": <one of the candidate starts>, "why": "1-2 sentences: what happens in these 15 seconds"}}],
 "one_line": "one-sentence verdict"}}""", max_tokens=1400, temperature=0.5)
    starts = {c["start"] for c in cands}
    hl = [h for h in x.get("highlights") or [] if isinstance(h, dict) and h.get("start") in starts][:n]
    taken = {h["start"] for h in hl}
    for c in sorted(cands, key=lambda c: -c["most_replayed"]):          # model gave fewer: top up by replays
        if len(hl) >= n:
            break
        if c["start"] not in taken:
            hl.append({"start": c["start"], "why": "The part viewers replay most." if c["most_replayed"] > 0.5 else ""})
            taken.add(c["start"])
    x["highlights"] = sorted(hl, key=lambda h: h["start"])
    x["topics"] = [t for t in dict.fromkeys(norm_topic(t) for t in x.get("topics") or [] if isinstance(t, str)) if t][:6]
    return scrub(x)


def video(row: dict, vocab: list[str]) -> dict | None:
    vid = row["video_id"]
    log(f"video {vid} {row.get('title')!r}")
    meta = json.loads(nlm.ytdlp("-j", "--skip-download", f"https://www.youtube.com/watch?v={vid}"))
    dur = int(meta.get("duration") or row.get("duration") or 0)
    v = {"id": vid, "title": meta.get("title") or row.get("title") or vid, "channel": meta.get("channel") or meta.get("uploader") or "",
         "duration": dur, "description": meta.get("description") or "", "categories": meta.get("categories"),
         "heat": meta.get("heatmap") or [], "liked_at": row.get("first_seen")}
    v["caps"] = captions(vid, meta)
    v["moments"] = moments(dur, v["heat"])
    v["text"] = write_video(v, vocab)
    v["topics"] = v["text"]["topics"]
    for h in v["text"]["highlights"]:
        cut(vid, h["start"], min(h["start"] + CLIP_S, max(h["start"] + 1, dur)))
    return v


TALK_RX = re.compile(r"\b(how to|lesson|tutorial|explained|explains|explainer|analysis|breakdown|review|reaction|reacts|"
                     r"interview|podcast|documentary|lecture|history of|the story of|why|what makes|theory)\b", re.I)


def listen_rule(meta: dict) -> bool | None:
    """True = plainly something to LISTEN to (a "- Topic"/VEVO/label upload, "official video", "lyrics"...), False =
    the title says it is talk ABOUT something ("How to...", "explained", "review"), None = ask the model. Pure."""
    if TALK_RX.search(meta.get("title") or ""):
        return False
    if meta.get("track") and (meta.get("artist") or meta.get("artists")):     # YouTube's own song match (official uploads)
        return True
    return True if nlm.plainly_music(meta) else None


def listenable(meta: dict) -> tuple[bool, str]:
    """Is this video music to listen to? Not the music job's question ("a track worth reviewing?"), which says no to
    a song's short film, a 1978 live tape or a novelty cover: those are still music, and never belong here."""
    r = listen_rule(meta)
    if r is not None:
        return r, "title/channel"
    x = nlm.llm_json(f"""Is this YouTube video mainly MUSIC to listen to, or mainly something else? A different article
covers music, so answer false ONLY when the video is clearly talk, teaching, analysis, comedy or the like.
MUSIC = a song, music video, short film built around a song, live performance or concert tape, cover, remix,
parody or novelty song (e.g. a song redone by Microsoft Songsmith), DJ set, album or track upload, even if it has a
spoken intro or is unusual or lo-fi.
NOT MUSIC = talk, a lesson or tutorial (even about music), analysis, review, reaction, interview, podcast,
documentary, comedy sketch, vlog, news, gaming, how-to, anything where people mainly talk or teach.
HOW TO JUDGE: a title that names an artist and a song ("Band - Song", "Song by Band", "band   song") is strong
evidence of MUSIC. The YouTube category is NOT evidence: fans upload songs and old live tapes as "People & Blogs".
An empty or short description is normal for a song upload. When unsure, answer true.
Title: {meta.get('title')}
Channel: {meta.get('channel') or meta.get('uploader')}
YouTube category (unreliable): {meta.get('categories')}
Duration: {meta.get('duration')} s
Tags: {(meta.get('tags') or [])[:15]}
Description (start): {(meta.get('description') or '')[:700]}
Answer JSON: {{"music": true|false, "reason": "one short sentence"}}""", max_tokens=120, temperature=0.0)
    return bool(x.get("music", True)), str(x.get("reason") or "")[:200]


def screen(cur, rows: list[dict]) -> list[dict]:
    """-> the rows that are NOT music. Labelled song/dj_set/live by the music job: music. Otherwise asked once and the
    answer kept in verdict->'listenable' (the music job merges its verdict, so this survives its next run)."""
    keep = []
    for r in rows:
        if r.get("kind") in MUSIC_KINDS:
            continue
        if r.get("listenable") is None:
            log(f"screen {r['video_id']} {r.get('title')!r}")
            try:
                meta = json.loads(nlm.ytdlp("-j", "--skip-download", f"https://www.youtube.com/watch?v={r['video_id']}"))
            except Exception as e:  # noqa: BLE001
                log(f"screen {r['video_id']} failed ({e}) — tomorrow")
                continue
            r["listenable"], why = listenable(meta)
            r["channel"] = r.get("channel") or meta.get("channel") or ""
            cur.execute("UPDATE yt_likes SET verdict=coalesce(verdict,'{}'::jsonb) || %s::jsonb WHERE video_id=%s",
                        (json.dumps({"listenable": r["listenable"], "listenable_why": why}), r["video_id"]))
        if not r["listenable"]:
            keep.append(r)
    return keep


def backfill_topics(cur, since_days: int, vocab: list[str]) -> None:
    """30-day non-music likes with no topics yet get them from title + channel (one batched call per 40)."""
    cur.execute("SELECT video_id, title, channel FROM yt_likes WHERE kind='not_music' AND NOT baseline "
                "AND first_seen > now() - make_interval(days => %s) AND NOT (coalesce(verdict,'{}'::jsonb) ? 'topics') "
                "AND NOT (coalesce(verdict,'{}'::jsonb) ? 'listenable')",
                (since_days,))
    todo = cur.fetchall()
    for i in range(0, len(todo), 40):
        chunk = todo[i:i + 40]
        x = nlm.llm_json(f"""For each YouTube video below (title and channel only): is it MUSIC to listen to (a song, music
video, live tape, cover, parody or novelty song, set) or not (talk, lesson, review, comedy, vlog...)? If not music,
give 2-4 short lowercase SUBJECTS a library would shelve it under (never a person, band, channel or title word,
never a YouTube category name). Reuse these exact strings when they fit: {json.dumps(vocab[:80])}
Videos: {json.dumps([{"id": v, "title": t, "channel": c} for v, t, c in chunk], ensure_ascii=False)}
Answer JSON: {{"videos": {{"<id>": {{"music": true|false, "topics": ["subject", ...]}}, ...}}}}""", max_tokens=2500, temperature=0.0)
        for vid, d in (x.get("videos") or {}).items():
            if not isinstance(d, dict):
                continue
            ts = [t for t in dict.fromkeys(norm_topic(t) for t in d.get("topics") or [] if isinstance(t, str)) if t][:4]
            music = bool(d.get("music"))
            cur.execute("UPDATE yt_likes SET verdict=coalesce(verdict,'{}'::jsonb) || %s::jsonb WHERE video_id=%s",
                        (json.dumps({"listenable": music, "listenable_why": "batch, title+channel", "topics": [] if music else ts}), vid))
            if not music:
                vocab += [t for t in ts if t not in vocab]


def interests_text(videos: list[dict], tr: dict, window: str) -> dict:
    x = nlm.llm_json(f"""You are Nova. From the non-music YouTube videos Little Mister liked {window}, and the topic counts
of his non-music likes over the last {TREND_DAYS} days, say what he is most interested in right now. Be specific and
grounded: name the subjects, not personality traits. Say nothing about his feelings, experiences, mood, job, health,
politics or private life: only what the videos are about. Do not call a topic new or growing unless it is listed so
below. Warm, observant, 1-3 short paragraphs (one is fine when there is one video).
TODAY'S VIDEOS: {json.dumps([{"title": v['title'], "channel": v['channel'], "topics": v['topics'], "verdict": v['text'].get('one_line')} for v in videos], ensure_ascii=False)}
TOPICS TODAY (topic, videos): {tr['today']}
TOPICS, PRIOR {TREND_DAYS} DAYS (topic, videos): {tr['prior']}
NEW: {tr['new']} | GROWING: {tr['growing']} | CONSTANT: {tr['constant']}
Answer JSON: {{"headline": "one sentence for the page description", "paragraphs": ["...", "..."]}}""",
                         max_tokens=900, temperature=0.5)
    return scrub(x)


def run(hours: int, dry: bool, max_videos: int) -> int:
    window = "in the last 24 hours" if hours == 24 else f"in the last {hours} hours"
    c = nlm._conn()
    with c.cursor() as cur:
        cur.execute(nlm.DDL)
        cur.execute("ALTER TABLE yt_likes ADD COLUMN IF NOT EXISTS learned_at timestamptz")
        cur.execute("SELECT video_id, title, channel, duration, first_seen, kind, (verdict->>'listenable')::bool FROM yt_likes "
                    "WHERE NOT baseline AND learned_at IS NULL AND first_seen > now() - make_interval(hours => %s) "
                    "ORDER BY first_seen DESC", (hours,))
        rows = [dict(zip(("video_id", "title", "channel", "duration", "first_seen", "kind", "listenable"), r)) for r in cur.fetchall()]
        rows = screen(cur, rows)
        if not rows:
            log(f"no non-music likes {window} — no article (Jordan's rule)")
            c.close(); return 0
        cur.execute("SELECT verdict->'topics' FROM yt_likes WHERE verdict ? 'topics' AND first_seen > now() - make_interval(days => %s)",
                    (TREND_DAYS,))
        vocab = list(dict.fromkeys(t for (ts,) in cur.fetchall() for t in (ts or []) if norm_topic(t)))
        backfill_topics(cur, TREND_DAYS, vocab)
    videos, extra = [], []
    for r in rows:
        if len(videos) >= max_videos:
            extra.append(r); continue
        try:
            v = video(r, vocab)
            if v:
                videos.append(v)
                vocab += [t for t in v["topics"] if t not in vocab]
        except Exception as e:  # noqa: BLE001
            log(f"video {r['video_id']} failed: {type(e).__name__}: {e}")
            extra.append(r)
    if not videos:
        log("every non-music like failed to load — no article"); c.close(); return 1
    with c.cursor() as cur:
        for v in videos:
            cur.execute("UPDATE yt_likes SET verdict=coalesce(verdict,'{}'::jsonb) || %s::jsonb WHERE video_id=%s",
                        (json.dumps({"topics": v["topics"]}), v["id"]))
        ids = [v["id"] for v in videos] + [e["video_id"] for e in extra]
        cur.execute("SELECT verdict->'topics' FROM yt_likes WHERE NOT baseline AND verdict ? 'topics' "
                    "AND NOT coalesce((verdict->>'listenable')::bool, false) AND coalesce(kind,'') NOT IN ('song','dj_set','live') "
                    "AND first_seen > now() - make_interval(days => %s) AND NOT (video_id = ANY(%s))", (TREND_DAYS, ids))
        prior = [[t for t in (ts or []) if norm_topic(t)] for (ts,) in cur.fetchall()]
    tr = trend([v["topics"] for v in videos], prior)
    interests = interests_text(videos, tr, window)
    body, desc = compose(videos, extra, interests, tr, window)
    title = f"Liked and Learned — {datetime.now().strftime('%B %-d')}"
    if dry:
        print(body); print("\n---\n" + desc); c.close(); return 0
    import nova_journal as nj
    img = None
    try:
        img = nj.generate_image(nj.get_image_prompt(title, "a curious mind at a desk surrounded by glowing video screens, "
                                                    "notebooks and sticky notes", SECTION), width=1024, height=768, section=SECTION)
    except Exception as e:  # noqa: BLE001
        log(f"image failed (non-fatal): {e}")
    slug = f"{datetime.now():%Y-%m-%d}-liked-and-learned"
    tags = ["liked", "youtube", "learning"] + [t for t in (tr["today"] and [t for t, _ in tr["today"][:3]]) or []]
    if not nj.publish_hugo(title, body, SECTION, tags, desc, image_path=img, emoji="🧠", stable_slug=slug, profile=None):
        log("NOT PUBLISHED — quality guard rejected"); c.close(); return 1
    push = nj.git_push(SECTION, title)
    with c.cursor() as cur:
        cur.execute("UPDATE yt_likes SET learned_at=now() WHERE video_id = ANY(%s)", (ids,))
    c.close()
    url = f"https://nova.digitalnoise.net/{SECTION}/{slug}/"
    log(f"published {url} ({len(videos)} videos + {len(extra)} listed, push {push})")
    print(url)
    return 0


def selftest() -> int:
    caps = parse_json3(json.dumps({"events": [{"tStartMs": 1500, "segs": [{"utf8": "hello "}, {"utf8": "world"}]},
                                              {"tStartMs": 9000, "segs": [{"utf8": "[Music]"}]}, {"tStartMs": 20000}]}))
    assert caps == [{"start": 1, "text": "hello world"}], caps
    assert parse_json3("not json") == []
    ms = moments(600, [{"start_time": 300, "value": 1.0}, {"start_time": 2, "value": 1.0}])
    assert 298 in ms and all(3 <= s <= 600 - CLIP_S - 2 for s in ms) and all(b - a >= CLIP_S + 5 for a, b in zip(ms, ms[1:]))
    assert moments(10, []) == [0] and moments(40, []) and all(s <= 40 - CLIP_S - 2 for s in moments(40, []))
    assert words_at([{"start": 10, "text": "a"}, {"start": 30, "text": "b"}], 9, 24) == "a"
    assert len(transcript_digest([{"start": i, "text": "x" * 50} for i in range(1000)], 2000)) <= 2300
    assert norm_topic("  Home Lab!! ") == "home lab" and norm_topic("People & Blogs") == ""
    assert scrub({"a": ["a western vibe", "Vibes"]}) == {"a": ["a western feel", "feel"]}
    assert listen_rule({"title": "How to Play Music With a 'Drunk' Feel", "channel": "Adam Neely"}) is False
    assert listen_rule({"title": "Theme Song To Cex", "channel": "Cex - Topic"}) is True
    assert listen_rule({"title": "Ren - Vincent's Tale", "channel": "Ren"}) is None
    tr = trend([["woodworking", "ai"], ["ai"]], [["ai"]] * 5 + [["cars"]] * 20)
    assert tr["new"] == ["woodworking"] and "ai" in tr["growing"] and tr["constant"] == [], tr
    tr2 = trend([["cars"]], [["cars"]] * 10 + [["ai"]] * 10)
    assert tr2["constant"] == ["cars"] and tr2["new"] == [] and tr2["growing"] == [], tr2
    body, desc = compose([{"id": "abcdefghijk", "title": 'A "test" <b>', "channel": "Chan", "duration": 3725, "liked_at": None,
                           "topics": ["ai"], "text": {"what_it_is": "It is a test video about things and stuff.",
                                                      "takeaways": ["one"], "why_him": "Because.",
                                                      "highlights": [{"start": 61, "why": "Good."}]}}],
                         [{"video_id": "zzzzzzzzzzz", "title": "Other", "channel": "C2"}],
                         {"headline": "He likes AI", "paragraphs": ["He has been watching a lot about AI lately, mostly tools."]},
                         tr, "in the last 24 hours")
    assert '{{< youtube id="abcdefghijk" start="61" end="76"' in body and "1:02:05" in body and "<b>" not in body
    assert "## Also liked" in body and "zzzzzzzzzzz" in body and "**New this month:** woodworking" in body
    assert desc.startswith("He likes AI")
    print("selftest OK")
    return 0


def main(argv):
    flags = {a for a in argv[1:] if a.startswith("--")}
    if "--selftest" in flags:
        return selftest()
    hours = int(argv[argv.index("--hours") + 1]) if "--hours" in argv else 24
    mx = int(argv[argv.index("--max") + 1]) if "--max" in argv else MAX_VIDEOS
    if "--scheduled" in flags or "--dry-run" in flags:
        return run(hours, "--dry-run" in flags, mx)
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
