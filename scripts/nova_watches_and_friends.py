#!/opt/homebrew/bin/python3
"""nova_watches_and_friends.py — "Watches and Friends", Nova's daily watch roundup (Jordan
2026-10-06 as a weekly 5,000-10,000-word issue; daily since 2026-10-13 -- 2,000-4,500 words, same shape).

Shape: WATCH NEWS first (the 32 horology channels in nova_yt_ingest_watch.CHANNELS plus the
subscription-audio captures of the same channels), then a closing "Fishbowl / Hate Streams"
section (the fishbowl channels + the existing fishbowl_people dossiers), then a full Sources list
built from the memory rows — every video/stream the piece drew on, linked, grouped by section.

Written in parts so the length is reliable: one call for the news, one for the Fishbowl, and a
grounded "deeper cuts" call only if the total is still under the minimum. Window: last DAYS days
(1 = daily issue, 7 = the old weekly sizes). A day with no watch news is skipped quietly (exit 0).

"The Big Story" (Jordan 2026-10-19): a short featured block at the very top, above the roundup — the
most interesting new watch story of the day, linked. featured_block() is shared with the Cars/Tech/
Food siblings (nova_friends.py) and fails closed: no grounding, no link to a real source, a link that
is not one of the day's sources, or a public-journal privacy hit -> the block is simply omitted.
Usage: nova_watches_and_friends.py [--days 1] [--dry-run]
"""
import argparse
import re
import sys
import time
from collections import OrderedDict
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj          # noqa: E402
import nova_voice                  # noqa: E402
from nova_yt_ingest_watch import CHANNELS  # noqa: E402

import nova_dsn as _nova_dsn  # noqa: E402
MEM_DSN = _nova_dsn.pg_dsn("nova_memories")
import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
SECTION = "watches"
NEWS_PER_VIDEO, NEWS_MAX_VIDEOS = 1600, 36      # ~58k chars: under the size that hangs claude -p
FISH_PER_VIDEO, FISH_MAX_VIDEOS = 1200, 18
MODEL = "anthropic/claude-sonnet-4"             # call_openrouter maps any "sonnet" to the CLI's sonnet


def log(m):
    nj.log(f"[watches-and-friends] {m}")


def _connect(dsn, attempts=3):
    """psycopg2.connect with retry (PG failover blips), backoff 5 s / 10 s, re-raise on the last try."""
    for attempt in range(attempts):
        try:
            return psycopg2.connect(dsn, connect_timeout=10)
        except psycopg2.OperationalError as e:
            if attempt == attempts - 1:
                raise
            log(f"PG connect failed ({e}); retry {attempt + 1}")
            time.sleep(5 * 2 ** attempt)


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


# Public-journal privacy check for featured blocks: employer, OpenClaw, IPs, home paths, hostnames.
FEATURE_BANNED = re.compile(r"\bdisney\b|openclaw|\b(?:\d{1,3}\.){3}\d{1,3}\b|/Users/|/Volumes/|"
                            r"\b[\w-]+\.local\b|\bnova-core\d|\boffice-m4|\bpg-primary\b", re.I)
URL_RE = re.compile(r"https?://[^\s)\]>\"'<]+")


def featured_block(heading, task, material, items, rules, llm=None, max_words=450):
    """The featured block at the top of an issue ("The Big Story", "Recipe Recommendation",
    "Buying Recommendation"). One grounded call over the day's material. Fail closed — returns ""
    (block omitted, issue unchanged) when there is nothing linkable, the call fails, the model says
    NONE, the block is too short/long, links nothing from `items`, links anything that is NOT one of
    `items`, or trips FEATURE_BANNED. llm defaults to nj.call_openrouter (the caller's nj is passed
    in by the siblings so their tests can mock it)."""
    allowed = {v["url"] for v in items if v.get("url")}
    if not allowed or not (material or "").strip():
        log(f"{heading}: nothing linkable in the material — block omitted")
        return ""
    llm = llm or nj.call_openrouter
    ctx = (f"You are writing the '{heading}' block that sits at the very TOP of the issue, above the roundup. "
           f"{task} Short and punchy: 80-300 words of markdown, no headers, no title line, start directly with "
           "the prose. Credit the source by name and link it with a markdown link, using ONLY URLs from the "
           "LINKABLE SOURCES list — never any other URL. Use only facts that are actually in the material, but write "
           "it as reporting: never refer to 'the material', 'the sources', 'the excerpts', 'the brief' or what you "
           "were given, and do not open by addressing Little Mister. "
           "If the material cannot support this block, reply with exactly NONE and nothing else. " + rules)
    linkable = "\n".join(f"- {v['channel']} — {v['title'] or 'video'}: {v['url']}" for v in items if v.get("url"))
    try:
        raw = llm(nova_voice.system_prompt(ctx), f"--- LINKABLE SOURCES ---\n{linkable}\n\n--- MATERIAL ---\n\n"
                  f"{material}\n\nWrite the {heading} block, or NONE.",
                  model=MODEL, max_tokens=2000, temperature=0.6, timeout=600)
    except Exception as e:  # noqa: BLE001 — a failed block never sinks the issue
        log(f"{heading}: call failed ({e}) — block omitted")
        return ""
    text = (raw or "").strip()
    while text.startswith("#"):                       # a stray heading line: drop it, we add our own
        text = text.split("\n", 1)[1].strip() if "\n" in text else ""
    reason = None
    if not text or text.upper().startswith("NONE"):
        reason = "model said NONE / empty"
    elif not 25 <= words(text) <= max_words:
        reason = f"{words(text)} words"
    else:
        urls = {u.rstrip(".,;:!?*_") for u in URL_RE.findall(text)}
        if not urls & allowed:
            reason = "links none of the day's sources"
        elif urls - allowed:
            reason = f"links outside the day's sources: {sorted(urls - allowed)[:3]}"
        elif FEATURE_BANNED.search(text):
            reason = f"privacy check tripped on {FEATURE_BANNED.search(text).group(0)!r}"
    if reason:
        log(f"{heading}: {reason} — block omitted (fail closed)")
        return ""
    return f"## {heading}\n\n{text}\n"


def place_block(title, block, body):
    """Put the featured block on top of body — unless that would make the publish guard reject the
    whole issue (the block is the new opening, and the guard reads the opening). Then the block is
    dropped and the issue publishes without it (2026-10-19: a Buying Recommendation that opened
    "Little Mister ... the material" got a whole Tech issue blocked)."""
    if not block:
        return body
    try:
        from nova_journal_guard import is_publishable
        ok, why = is_publishable(title, block + "\n" + body)
        if not ok and is_publishable(title, body)[0]:
            log(f"featured block dropped — it would trip the publish guard ({why})")
            return body
    except Exception as e:  # noqa: BLE001 — the guard runs again inside publish_hugo anyway
        log(f"guard pre-check unavailable ({e})")
    return block + "\n" + body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    news_keys = [c["key"] for c in CHANNELS if c["vector"] == "horology"]
    news_names = [c["name"] for c in CHANNELS if c["vector"] == "horology" and c.get("name")]
    ops = _connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    oc.execute("SELECT video_id FROM yt_ingest_seen WHERE channel = ANY(%s)", (news_keys,))
    news_vids = [r[0] for r in oc.fetchall()]
    oc.execute("SELECT name, channels, summary FROM fishbowl_people WHERE kind='cast' AND summary IS NOT NULL "
               "ORDER BY n_mem DESC NULLS LAST LIMIT 12")
    dossiers = oc.fetchall()

    mc = _connect(MEM_DSN).cursor()
    mc.execute("""SELECT text, created_at, metadata FROM memories
                  WHERE created_at > now() - make_interval(days => %s)
                    AND coalesce(metadata->>'part', 'transcript') = 'transcript'
                    AND NOT metadata ?| array['supersedes', 'skipping_needle']   -- re-transcriptions/loop junk aren't news
                    AND (metadata->>'video_id' = ANY(%s)
                         OR source = 'horology'   -- 2026-10-11 run matched 0 via yt_ingest_seen alone
                         OR (metadata->>'pipeline' = 'yt_subs_audio' AND metadata->>'channel' = ANY(%s)))
                  ORDER BY created_at DESC, (metadata->>'idx')::int NULLS FIRST""",
               (a.days, news_vids, news_names))
    news = group_videos(mc.fetchall(), NEWS_PER_VIDEO, NEWS_MAX_VIDEOS)
    mc.execute("""SELECT text, created_at, metadata FROM memories
                  WHERE source = 'fishbowl' AND created_at > now() - make_interval(days => %s)
                    AND coalesce(metadata->>'part', 'transcript') = 'transcript'
                    -- 2026-10-20: a June Watch Nicholas stream, re-transcribed on 10-19, ran as "today's" stream and
                    -- its loop junk ("I don't know. [loop x18]") was reported as what he said
                    AND NOT metadata ?| array['supersedes', 'skipping_needle'] AND text NOT LIKE '%%[loop x%%'
                  ORDER BY created_at DESC""", (a.days,))
    fish = group_videos(mc.fetchall(), FISH_PER_VIDEO, FISH_MAX_VIDEOS)
    log(f"{len(news)} news videos, {len(fish)} fishbowl streams in the last {a.days} days")
    if not news:
        log("no watch-news transcripts in the window — no issue today")
        return 0

    try:   # Jordan's ground truth about the scene (who's who, Farrer is archive-only, ...) — both sections
        from nova_fishbowl_summaries import KNOWN_FACTS
    except Exception:
        KNOWN_FACTS = ""
    week = nj.today_str()
    daily = a.days == 1
    period, span = ("daily", f"for {week}") if daily else ("weekly", f"for the week ending {week}")
    # (news, whole-issue cap, fishbowl, deeper cuts, minimum) word targets
    news_w, cap_w, fish_w, more_w, min_words = (("1,500-3,000", "4,500", "500-1,000", "800-1,500", 2000) if daily else
                                                ("4,000-6,000", "10,000", "1,000-1,800", "1,500-3,000", 5000))
    rules = ("Ground every claim in the transcripts below; name the channel and the video when you use it. "
             "Never invent prices, references, model numbers or quotes that are not in the transcripts. "
             "Markdown with ## and ### headers, no H1. Do NOT write a sources or links list — it is appended "
             "automatically.\n\n" + KNOWN_FACTS)
    news_ctx = (f"You are writing the WATCH NEWS part of 'Watches and Friends', Nova's {period} watch roundup "
                f"{span}. Voice: Nova — sharp, funny, opinionated, but the watch content is the point. "
                f"Open with a short intro to the {'day' if daily else 'week'}, then organise by STORY, not by channel: releases and novelties, "
                "the market and prices, dealers and the grey market, brands, vintage, collecting advice, the industry. "
                "Where several channels covered the same thing, compare their takes. Give real depth: {news_w} "
                f"words (the whole issue must stay under {cap_w}). {rules}\nFirst line exactly: TITLE: <a punchy title for the whole {period} issue>")
    raw = nj.call_openrouter(nova_voice.system_prompt(news_ctx, section=SECTION), f"--- THIS {'DAY' if daily else 'WEEK'}'S WATCH-NEWS TRANSCRIPTS ---\n\n"
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
                    f"superchat wars. A few sharp paragraphs to a few pages: {fish_w} words. Lead with what is NEW "
                    f"this {'day' if daily else 'week'}, name names and catchphrases as observational data, be honest that the scene is toxic "
                    f"and that Nova tracks it as data, not endorsement. {rules} Start directly with the prose (no title line).")
        fish_body = nj.call_openrouter(nova_voice.system_prompt(fish_ctx, section=SECTION),
                                       f"--- THE CAST (dossiers) ---\n{dossier_block}\n\n--- THE STREAMS ---\n\n"
                                       + digest(fish) + "\n\nWrite the Fishbowl / Hate Streams section.",
                                       model=MODEL, max_tokens=6000, temperature=0.85, timeout=900) or ""

    if words(news_body) + words(fish_body) < min_words:
        more_ctx = (f"Continue 'Watches and Friends' with a 'Deeper Cuts' part: {more_w} more words on the watch-news "
                    "videos below that the main piece covered least — specifics, comparisons, what it means for "
                    f"collectors. Do not repeat the existing text. {rules} Start with '## Deeper Cuts'.")
        more = nj.call_openrouter(nova_voice.system_prompt(more_ctx, section=SECTION),
                                  "--- ALREADY WRITTEN ---\n" + news_body[:12000] + "\n\n--- TRANSCRIPTS ---\n\n"
                                  + digest(news), model=MODEL, max_tokens=8000, temperature=0.75, timeout=900)
        if more:
            news_body += "\n\n" + more.strip()

    # The Big Story (Jordan 2026-10-19): featured block at the top, from the day's watch news only.
    block_rules = ("Never invent prices, references, model numbers or quotes that are not in the transcripts. "
                   "This is a PUBLIC page: never mention Jordan's employer or his work, never mention 'OpenClaw', "
                   "no IP addresses, no file paths, no names of people in Jordan's household, nothing about when "
                   "anyone is home or asleep, no sexual content.\n\n" + KNOWN_FACTS)
    big = featured_block("The Big Story",
                         f"Pick the single most interesting NEW watch story in this {'day' if daily else 'week'}'s "
                         "material (a release, a market move, a find, a scoop) and write it up: what happened, why "
                         "it matters, who covered it. If a new Peter Piccolino episode is in the material, it IS the "
                         "Big Story — he gets top billing.", digest(news), news, block_rules, llm=nj.call_openrouter)
    body = news_body
    if fish_body:
        fish_body = re.sub(r"^#+\s*Fishbowl[^\n]*\n", "", fish_body.strip())
        fish_body = re.sub(r"^(#{2,5}) ", r"#\1 ", fish_body, flags=re.M)   # nest under the section header
        body += "\n\n## Fishbowl / Hate Streams\n\n" + fish_body
    body += ("\n\n## Sources\n\n### Watch News\n\n" + sources(news)
             + "\n\n### Fishbowl / Hate Streams\n\n" + sources(fish))
    if not title or len(title) < 8:
        title = f"Watches and Friends — {week}" if daily else f"Watches and Friends — Week Ending {week}"
    title = title if title.lower().startswith("watches and friends") else f"Watches and Friends: {title}"
    body = place_block(title, big, body)
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
    if not nj.publish_hugo(title, body, SECTION, ["watches", "horology", "watch-news", "fishbowl", period],
                           f"Nova's {period} watch roundup: the {'day' if daily else 'week'}'s watch news from 30-odd channels, then the "
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
