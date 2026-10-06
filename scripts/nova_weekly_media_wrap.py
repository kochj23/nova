#!/usr/bin/env python3
"""nova_weekly_media_wrap.py — Sunday-morning wrap-up of the week's media ingest.

Jordan (2026-09-13): a weekly article in Nova's usual voice — sassy, sarcastic,
mildly put-upon — reviewing every YouTube show and TV recording ingested into her
memory over the past 7 days: what she watched, the themes, and what actually got
stored. Publishes a dated article to the journal's `operations` section.
Scheduled Sundays 08:00 via scheduler-core.yaml (weekly_media_wrap).

Source of truth is nova_memories metadata written by the ingest pipeline:
  type='tv_transcript'  — YouTube/TV library transcripts (show, title, chunks)
  type='full_episode'   — OTA live-TV recordings (channel_name, show_title)
  type='news_broadcast' / source='local_news' — news capture
"""
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj
import nova_voice

MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
WINDOW = "7 days"


def main():
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()

    # Per-show roll call from the YouTube/TV library ingest.
    mc.execute(
        "SELECT metadata->>'show', count(DISTINCT metadata->>'title'), count(*), "
        "       min(source), max(created_at)::date "
        "FROM memories WHERE created_at > now() - interval %s "
        "AND metadata->>'type'='tv_transcript' AND metadata->>'show' IS NOT NULL "
        "GROUP BY 1 ORDER BY 3 DESC", (WINDOW,))
    shows = mc.fetchall()

    # OTA live-TV recordings.
    mc.execute(
        "SELECT coalesce(metadata->>'channel_name', metadata->>'show_title', '?'), count(*) "
        "FROM memories WHERE created_at > now() - interval %s "
        "AND metadata->>'type'='full_episode' GROUP BY 1 ORDER BY 2 DESC", (WINDOW,))
    recordings = mc.fetchall()

    # News capture volume.
    mc.execute(
        "SELECT count(*) FILTER (WHERE metadata->>'type'='news_broadcast'), "
        "       count(*) FILTER (WHERE source='local_news') "
        "FROM memories WHERE created_at > now() - interval %s", (WINDOW,))
    n_broadcast, n_localnews = mc.fetchone()

    mc.execute(
        "SELECT count(*) FROM memories WHERE created_at > now() - interval %s "
        "AND (metadata->>'type' IN ('tv_transcript','full_episode','news_broadcast') "
        "     OR source='local_news')", (WINDOW,))
    total_stored = mc.fetchone()[0]

    if not shows and not recordings:
        nj.log("[media-wrap] nothing media-shaped ingested this week — skipping publish")
        return 0

    # One representative snippet per top show so the LLM can talk themes without
    # being handed 6K chunks. Newest chunk of the newest episode, per show.
    snippets = []
    cited_ids = []   # memory ids this article drew on -> publish_hugo article_citations (#2588)
    for show, _eps, _chunks, _vec, _last in shows[:14]:
        mc.execute(
            "SELECT text, id FROM memories WHERE created_at > now() - interval %s "
            "AND metadata->>'type'='tv_transcript' AND metadata->>'show'=%s "
            "ORDER BY created_at DESC LIMIT 1", (WINDOW, show))
        r = mc.fetchone()
        if r:
            snippets.append(f"### {show}\n{r[0][:400]}")
            cited_ids.append(r[1])
    snippet_block = "\n\n".join(snippets) or "(no transcript snippets)"

    show_block = "\n".join(
        f"- {s}: {e} episode(s), {c} transcript chunk(s), memory vector '{v}', last ingested {d}"
        for s, e, c, v, d in shows) or "(no library ingest this week)"
    rec_block = "\n".join(f"- {ch}: {n} recording(s)" for ch, n in recordings[:15]) \
        or "(no OTA recordings this week)"

    ctx = (
        "Write this week's SUNDAY MEDIA WRAP-UP for Nova's journal (Operations section). "
        "Nova reviews everything she was made to watch and transcribe in the past 7 days — "
        "the YouTube shows, the over-the-air TV recordings, the news feeds — because "
        "apparently that's her life now. Voice: her normal operations voice turned up — "
        "sassy, sarcastic, dryly annoyed, genuinely funny, but ACCURATE. She's the "
        "long-suffering steward doing the week's media inventory with an eyebrow raised.\n"
        "- Open with a huffy but charming one-paragraph read on the week's viewing diet.\n"
        "- Then the roll call: work through the notable shows (use the per-show stats), "
        "with a one-liner of commentary or judgment each — what it is, what happened in "
        "it this week (use the snippets), and Nova's opinion of having watched it.\n"
        "- Call out MAJOR THEMES across the week's ingest (patterns, obsessions, anything "
        "that kept coming up in the transcripts).\n"
        "- Mention the OTA recordings and news capture briefly — volume, anything odd.\n"
        "- Close with what all this became: memories stored, vectors fed, and one dry "
        "line about doing it all again next week.\n"
        "- Numbers must come from the stats provided; do not invent counts. A stats "
        "footer is appended automatically, so weave numbers in naturally rather than "
        "dumping a table.\n"
        "700-1200 words, markdown, section headers welcome, no H1 title (added "
        "separately).\n\n"
        "OUTPUT EXACTLY THIS SHAPE:\nTITLE: <one punchy title, no quotes>\n<blank line>\n<the body>")
    system = nova_voice.system_prompt(ctx)
    user = (f"--- THIS WEEK'S SHOW ROLL CALL (library ingest) ---\n{show_block}\n\n"
            f"--- OTA / LIVE-TV RECORDINGS ---\n{rec_block}\n\n"
            f"--- NEWS CAPTURE ---\nnews broadcasts: {n_broadcast}, local-news items: {n_localnews}\n\n"
            f"--- FRESHEST TRANSCRIPT SNIPPET PER SHOW ---\n{snippet_block}\n\n"
            f"--- TOTALS ---\nmedia memories stored this week: {total_stored}\n\n"
            f"Write this week's media wrap-up.")
    import nova_article_history
    _h = nova_article_history.recent_articles_context("operations")
    if _h:
        user = user + "\n\n" + _h
    raw = nj.call_openrouter(system, user, max_tokens=3200, temperature=0.9)
    if not raw:
        nj.log("[media-wrap] LLM produced nothing — aborting"); return 1

    title, body = None, []
    for ln in raw.splitlines():
        if title is None and ln.upper().startswith("TITLE:"):
            title = ln.split(":", 1)[1].strip().strip('"')
        else:
            body.append(ln)
    body = "\n".join(body).strip()

    def _degenerate(t):
        toks = [w.strip(".,!?—-:;\"'").lower() for w in (t or "").split()]
        toks = [w for w in toks if w]
        if len((t or "").strip()) < 8 or len(toks) < 2:
            return True
        return len(set(toks)) <= max(1, len(toks) // 4)

    if not title or _degenerate(title):
        title = f"What I Watched So You Didn't Have To — {nj.today_str()}"

    # Deterministic stats footer — the numbers Jordan can trust regardless of
    # what the LLM wove into the prose.
    ep_total = sum(e for _s, e, _c, _v, _d in shows)
    body += (
        "\n\n## The tape\n\n"
        f"- Shows ingested: **{len(shows)}** ({ep_total} episodes, "
        f"{sum(c for _s, _e, c, _v, _d in shows)} transcript chunks)\n"
        f"- OTA recordings: **{sum(n for _c, n in recordings)}** across {len(recordings)} channels\n"
        f"- News: **{n_broadcast}** broadcasts, **{n_localnews}** local-news items\n"
        f"- Total media memories stored this week: **{total_stored}**\n")

    img = None
    try:
        ip = nj.get_image_prompt(title, "a weekly review of television and youtube ingested into an AI's memory", "operations")
        img = nj.generate_image(ip, width=1024, height=768, section="operations")
    except Exception as e:
        nj.log(f"[media-wrap] image gen failed (non-fatal): {e}")

    tags = ["operations", "media", "weekly", "ingest", "tv", "youtube"]
    desc = "Nova's weekly wrap-up of every YouTube show and TV recording ingested into her memory — with commentary."
    if not nj.publish_hugo(title, body, "operations", tags, desc, image_path=img, emoji="📺",
                           cited_memory_ids=cited_ids):
        nj.log(f"[media-wrap] NOT PUBLISHED — quality guard rejected: {title}")
        return 1
    _push = nj.git_push("operations", title)
    # git_push returns 'pushed'/'committed_not_pushed'/'nothing'/'failed' — only a real push is PUBLISHED
    _pub = {"committed_not_pushed": "COMMITTED (not yet pushed)", "failed": "NOT COMMITTED (git failed)"}.get(_push, "PUBLISHED")
    nj.notify_slack("operations", f"📺 {title}", "Nova's weekly media ingest wrap-up.")
    nj.log(f"[media-wrap] {_pub}: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
