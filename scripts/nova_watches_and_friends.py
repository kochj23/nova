#!/opt/homebrew/bin/python3
"""nova_watches_and_friends.py — "Watches and Friends", Nova's weekly 5,000-10,000-word watch
roundup (Jordan 2026-10-06; replaces the daily Fishbowl dispatch).

Shape: WATCH NEWS first (the 32 horology channels in nova_yt_ingest_watch.CHANNELS plus the
subscription-audio captures of the same channels), then a closing "Fishbowl / Hate Streams"
section (the fishbowl channels + the existing fishbowl_people dossiers), then a full Sources list
built from the memory rows — every video/stream the piece drew on, linked, grouped by section.

Written in parts so the length is reliable: one call for the news, one for the Fishbowl, and a
grounded "deeper cuts" call only if the total is still under MIN_WORDS. Window: last DAYS days.
Usage: nova_watches_and_friends.py [--days 7] [--dry-run]
"""
import argparse
import re
import sys
from collections import OrderedDict
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj          # noqa: E402
import nova_voice                  # noqa: E402
from nova_yt_ingest_watch import CHANNELS  # noqa: E402

MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
SECTION = "watches"
MIN_WORDS = 5000
NEWS_PER_VIDEO, NEWS_MAX_VIDEOS = 1600, 36      # ~58k chars: under the size that hangs claude -p
FISH_PER_VIDEO, FISH_MAX_VIDEOS = 1200, 18
MODEL = "anthropic/claude-sonnet-4"             # call_openrouter maps any "sonnet" to the CLI's sonnet


def log(m):
    nj.log(f"[watches-and-friends] {m}")


def group_videos(rows, per_video, max_videos):
    """rows: (text, created_at, metadata) newest-first -> [{channel,title,url,date,text}] newest-first."""
    vids = OrderedDict()
    for text, created, md in rows:
        md = md or {}
        vid = md.get("video_id") or md.get("url") or text[:40]
        v = vids.setdefault(vid, {"channel": md.get("channel") or "unknown", "title": md.get("title") or "",
                                  "url": md.get("url") or (f"https://www.youtube.com/watch?v={md['video_id']}"
                                                           if md.get("video_id") else None),
                                  "date": created.strftime("%Y-%m-%d"), "parts": []})
        v["parts"].append(re.sub(r"^\[[^\]]*\]\s*", "", text or ""))
    out = []
    for v in list(vids.values())[:max_videos]:
        v["text"] = " ".join(v.pop("parts"))[:per_video]
        out.append(v)
    return out


def digest(videos):
    return "\n\n---\n\n".join(f"[{v['channel']}] \"{v['title']}\" ({v['date']})\n{v['text']}" for v in videos)


def sources(videos):
    seen, lines = set(), []
    for v in videos:
        if v["url"] and v["url"] not in seen:
            seen.add(v["url"])
            lines.append(f"- [{v['channel']} — {v['title'].replace(']', ')') or 'video'}]({v['url']}) ({v['date']})")
    return "\n".join(lines) or "- (none this week)"


def split_title(raw):
    title, body = None, []
    for ln in (raw or "").splitlines():
        if title is None and ln.upper().startswith("TITLE:"):
            title = ln.split(":", 1)[1].strip().strip('"')
        else:
            body.append(ln)
    return title, "\n".join(body).strip()


def words(s):
    return len(re.findall(r"\w+", s or ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    news_keys = [c["key"] for c in CHANNELS if c["vector"] == "horology"]
    news_names = [c["name"] for c in CHANNELS if c["vector"] == "horology" and c.get("name")]
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    oc.execute("SELECT video_id FROM yt_ingest_seen WHERE channel = ANY(%s)", (news_keys,))
    news_vids = [r[0] for r in oc.fetchall()]
    oc.execute("SELECT name, channels, summary FROM fishbowl_people WHERE kind='cast' AND summary IS NOT NULL "
               "ORDER BY n_mem DESC NULLS LAST LIMIT 12")
    dossiers = oc.fetchall()

    mc = psycopg2.connect(MEM_DSN).cursor()
    mc.execute("""SELECT text, created_at, metadata FROM memories
                  WHERE created_at > now() - make_interval(days => %s)
                    AND coalesce(metadata->>'part', 'transcript') = 'transcript'
                    AND (metadata->>'video_id' = ANY(%s)
                         OR (metadata->>'pipeline' = 'yt_subs_audio' AND metadata->>'channel' = ANY(%s)))
                  ORDER BY created_at DESC, (metadata->>'idx')::int NULLS FIRST""",
               (a.days, news_vids, news_names))
    news = group_videos(mc.fetchall(), NEWS_PER_VIDEO, NEWS_MAX_VIDEOS)
    mc.execute("""SELECT text, created_at, metadata FROM memories
                  WHERE source = 'fishbowl' AND created_at > now() - make_interval(days => %s)
                    AND coalesce(metadata->>'part', 'transcript') = 'transcript'
                  ORDER BY created_at DESC""", (a.days,))
    fish = group_videos(mc.fetchall(), FISH_PER_VIDEO, FISH_MAX_VIDEOS)
    log(f"{len(news)} news videos, {len(fish)} fishbowl streams in the last {a.days} days")
    if not news:
        log("no watch-news transcripts in the window — aborting")
        return 1

    week = nj.today_str()
    rules = ("Ground every claim in the transcripts below; name the channel and the video when you use it. "
             "Never invent prices, references, model numbers or quotes that are not in the transcripts. "
             "Markdown with ## and ### headers, no H1. Do NOT write a sources or links list — it is appended "
             "automatically.")
    news_ctx = (f"You are writing the WATCH NEWS part of 'Watches and Friends', Nova's weekly watch roundup for the "
                f"week ending {week}. Voice: Nova — sharp, funny, opinionated, but the watch content is the point. "
                "Open with a short intro to the week, then organise by STORY, not by channel: releases and novelties, "
                "the market and prices, dealers and the grey market, brands, vintage, collecting advice, the industry. "
                "Where several channels covered the same thing, compare their takes. Give real depth: 4,000-6,500 "
                f"words. {rules}\nFirst line exactly: TITLE: <a punchy title for the whole weekly issue>")
    raw = nj.call_openrouter(nova_voice.system_prompt(news_ctx), "--- THIS WEEK'S WATCH-NEWS TRANSCRIPTS ---\n\n"
                             + digest(news) + "\n\nWrite the Watch News section.",
                             model=MODEL, max_tokens=16000, temperature=0.75, timeout=1200)
    title, news_body = split_title(raw)
    if not news_body:
        log("news generation produced nothing — aborting")
        return 1

    fish_body = ""
    if fish:
        dossier_block = "\n\n".join(f"### {n} ({c})\n{s[:450]}" for n, c, s in dossiers)
        fish_ctx = ("You are writing the closing 'Fishbowl / Hate Streams' section of 'Watches and Friends'. The "
                    "Fishbowl is the grey-market watch-community livestream drama scene — hate streams, beefs, "
                    "superchat wars. A few sharp paragraphs to a few pages: 1,200-2,000 words. Lead with what is NEW "
                    "this week, name names and catchphrases as observational data, be honest that the scene is toxic "
                    f"and that Nova tracks it as data, not endorsement. {rules} Start directly with the prose (no title line).")
        fish_body = nj.call_openrouter(nova_voice.system_prompt(fish_ctx),
                                       f"--- THE CAST (dossiers) ---\n{dossier_block}\n\n--- THIS WEEK'S STREAMS ---\n\n"
                                       + digest(fish) + "\n\nWrite the Fishbowl / Hate Streams section.",
                                       model=MODEL, max_tokens=6000, temperature=0.85, timeout=900) or ""

    if words(news_body) + words(fish_body) < MIN_WORDS:
        more_ctx = ("Continue 'Watches and Friends' with a 'Deeper Cuts' part: 1,500-3,000 more words on the watch-news "
                    "videos below that the main piece covered least — specifics, comparisons, what it means for "
                    f"collectors. Do not repeat the existing text. {rules} Start with '## Deeper Cuts'.")
        more = nj.call_openrouter(nova_voice.system_prompt(more_ctx),
                                  "--- ALREADY WRITTEN ---\n" + news_body[:12000] + "\n\n--- TRANSCRIPTS ---\n\n"
                                  + digest(news), model=MODEL, max_tokens=8000, temperature=0.75, timeout=900)
        if more:
            news_body += "\n\n" + more.strip()

    body = news_body
    if fish_body:
        body += "\n\n## Fishbowl / Hate Streams\n\n" + re.sub(r"^#+\s*Fishbowl[^\n]*\n", "", fish_body.strip())
    body += ("\n\n## Sources\n\n### Watch News\n\n" + sources(news)
             + "\n\n### Fishbowl / Hate Streams\n\n" + sources(fish))
    if not title or len(title) < 8:
        title = f"Watches and Friends — Week Ending {week}"
    title = title if title.lower().startswith("watches and friends") else f"Watches and Friends: {title}"
    log(f"{words(body)} words: {title}")
    if a.dry_run:
        print(body)
        return 0

    img = None
    try:
        img = nj.generate_image(nj.get_image_prompt(title, "luxury wristwatches, watch dealers and collectors", SECTION),
                                width=1024, height=768, section=SECTION)
    except Exception as e:
        log(f"image failed (non-fatal): {e}")
    if not nj.publish_hugo(title, body, SECTION, ["watches", "horology", "watch-news", "fishbowl", "weekly"],
                           "Nova's weekly watch roundup: the week's watch news from 30-odd channels, then the "
                           "Fishbowl / Hate Streams — with every source linked.",
                           image_path=img, emoji="⌚", profile=None):
        log("NOT PUBLISHED — quality guard rejected")
        return 1
    push = nj.git_push(SECTION, title)
    nj.notify_slack("fishbowl", f"⌚ {title}", f"Watches and Friends is out ({words(body)} words, {len(news)} news videos, "
                    f"{len(fish)} Fishbowl streams). Push: {push}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
