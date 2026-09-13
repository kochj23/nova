#!/usr/bin/env python3
"""nova_fishbowl_daily.py — ONE evergreen "Fishbowl" article, rebuilt day by day.

Publishes to the journal's `fishbowl` section under a FIXED slug (the-fishbowl), so each
run overwrites the same article rather than spawning new ones. Pulls the freshest ingested
watch-community livestream transcripts + the evolving per-person dossiers and has Nova write
a running "state of the Fishbowl" dispatch. As new podcasts are captured/transcribed into the
`fishbowl` vector, the next run reflects them. Scheduled daily via scheduler.yaml.
"""
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj
import nova_voice

MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
STABLE_SLUG = "the-fishbowl"          # evergreen: same file overwritten every run


def main():
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    # The whole roster is unbounded — guest discovery adds every one-off caller it hears
    # (27 cast vs 274 guests as of 2026-07-29), and dumping all of them made the prompt
    # grow without limit. Take the standing cast plus the most-covered guests, which is
    # what "the cast" in the prompt actually means. Same shape as
    # nova_opinion_fishbowl_roster.py, which already did this correctly.
    # WEDGE FIX (2026-08-11): the full roster (28 cast @ ~1.6K chars + 15 guests) built an
    # ~86K-char prompt that hung claude -p for its full 300s timeout every run — the fishbowl
    # flakiness all session. Cap to the most-covered 12 cast + 8 guests; each dossier is
    # truncated to an orientation-sized snippet below (the article needs "who's who", not the
    # whole file). Shrinks the prompt ~6x so generation completes in seconds.
    oc.execute("SELECT name, channels, summary FROM fishbowl_people "
               "WHERE kind='cast' AND summary IS NOT NULL ORDER BY n_mem DESC NULLS LAST LIMIT 12")
    dossiers = oc.fetchall()
    oc.execute("SELECT name, channels, summary FROM fishbowl_people "
               "WHERE kind='guest' AND summary IS NOT NULL ORDER BY n_mem DESC NULLS LAST LIMIT 8")
    dossiers += oc.fetchall()

    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    # freshest transcripts first — this is what makes it a DAILY dispatch, not a static intro
    mc.execute("SELECT text, created_at, metadata FROM memories WHERE source='fishbowl' "
               "AND metadata->>'type'='fishbowl_stream' ORDER BY created_at DESC LIMIT 12")
    rows = mc.fetchall()
    if not rows:
        mc.execute("SELECT text, created_at, metadata FROM memories WHERE source='fishbowl' "
                   "ORDER BY created_at DESC LIMIT 12")
        rows = mc.fetchall()
    samples = [r[0] for r in rows]
    from nova_fishbowl_summaries import source_links
    src_block = source_links(rows)
    # recency stats for an honest "since yesterday" framing
    mc.execute("SELECT count(1) FROM memories WHERE source='fishbowl' "
               "AND created_at > now() - interval '48 hours'")
    fresh_48h = mc.fetchone()[0]
    mc.execute("SELECT count(1) FROM memories WHERE source='fishbowl'")
    total = mc.fetchone()[0]

    if not samples:
        nj.log("[fishbowl-daily] no fishbowl memories yet — aborting"); return 1

    # Truncate each dossier to an orientation snippet (~450 chars) and each transcript to ~500 —
    # the article needs the gist of who's who + the freshest churn, not full files. Keeps the
    # whole prompt well under the size that hangs claude -p.
    dossier_block = "\n\n".join(f"### {n} ({c})\n{s[:450]}" for n, c, s in dossiers) or "(dossiers still building)"
    sample_block = "\n\n---\n\n".join(s[:500] for s in samples)

    ctx = (
        "Write TODAY'S entry in Nova's running 'Fishbowl' dispatch for her journal, in Nova's "
        "OPERATIONS voice — the steward who watches the systems and reports plainly, with dry wit. "
        "The Fishbowl is the online grey-market watch-community livestream drama scene. This is a "
        "STANDING, EVERGREEN article that gets rewritten each day as new streams are captured and "
        "transcribed — so write it as a fresh daily status report, not a one-time introduction:\n"
        "- Open with where things stand RIGHT NOW: what's the latest churn from the freshest streams "
        "(the sample memories are ordered newest-first). Name names and catchphrases; call the beefs, "
        "alliances, and superchat drama as OBSERVATIONAL data.\n"
        "- Keep a short 'the cast' orientation from the dossiers so a new reader isn't lost, but lead "
        "with what's NEW, not the backstory.\n"
        "- Be honest that the community is toxic — slurs, personal attacks, death threats over minor "
        "superchats — and that Nova tracks all of it as data, not endorsement.\n"
        "- End with a one-line 'monitoring' note: how many streams/items Nova ingested in the last 48h "
        f"({fresh_48h}) and the running total in the vector ({total}).\n"
        "- A 'Sources' list of the streams (with links) is appended below the dispatch automatically — "
        "refer to streams by channel/title in the text where it helps, but do not write your own link list.\n"
        "600-1000 words, markdown, section headers welcome, no H1 title (added separately).\n\n"
        "OUTPUT EXACTLY THIS SHAPE:\nTITLE: <one punchy title, no quotes>\n<blank line>\n<the body>")
    try:
        from nova_fishbowl_summaries import KNOWN_FACTS
        ctx = ctx + "\n\n" + KNOWN_FACTS
    except Exception:
        pass
    system = nova_voice.system_prompt(ctx)
    user = (f"--- PER-PERSON DOSSIERS (the cast) ---\n{dossier_block}\n\n"
            f"--- FRESHEST INGESTED STREAMS (newest first) ---\n{sample_block}\n\n"
            f"Write today's Fishbowl dispatch.")
    import nova_article_history
    # 'opinions' now (the dispatch moved sections 2026-08-11) — NOT 'fishbowl'. The old
    # content/fishbowl dir was removed, so passing 'fishbowl' here fell through to _content_dir's
    # last-resort recursive home glob and WEDGED the whole run for minutes (py-spy caught it).
    _h = nova_article_history.recent_articles_context("opinions")
    if _h:
        user = user + "\n\n" + _h
    raw = nj.call_openrouter(system, user, max_tokens=3200, temperature=0.85)
    if not raw:
        nj.log("[fishbowl-daily] LLM produced nothing — aborting"); return 1

    title, body = None, []
    for ln in raw.splitlines():
        if title is None and ln.upper().startswith("TITLE:"):
            title = ln.split(":", 1)[1].strip().strip('"')
        else:
            body.append(ln)
    body = "\n".join(body).strip()

    # Guard against degenerate LLM titles (e.g. "Betty. Betty. Betty. Betty.") — a
    # catchphrase loop. Fall back to a clean default if too short or mostly one token.
    def _degenerate(t):
        toks = [w.strip(".,!?—-:;\"'").lower() for w in (t or "").split()]
        toks = [w for w in toks if w]
        if len((t or "").strip()) < 8 or len(toks) < 2:
            return True
        return len(set(toks)) <= max(1, len(toks) // 4)   # heavy repetition

    if not title or _degenerate(title):
        title = f"The Fishbowl — Daily Dispatch, {nj.today_str()}"

    if src_block:
        body += "\n\n## Sources — what this dispatch is about\n\n" + src_block

    img = None
    try:
        ip = nj.get_image_prompt(title, "the online watch-community livestream drama scene", "fishbowl")
        img = nj.generate_image(ip, width=1024, height=768, section="fishbowl")
    except Exception as e:
        nj.log(f"[fishbowl-daily] image gen failed (non-fatal): {e}")

    tags = ["fishbowl", "watch-community", "drama", "opinions"]
    desc = "Nova's running dispatch from The Fishbowl — the watch-community livestream drama scene she tracks, updated as the cast changes."
    # Publishes to OPINIONS now (Jordan 2026-08-11), still under the same evergreen stable slug so
    # it's ONE living article the cast gets added to/removed from — never a new post per day.
    # A guard rejection must FAIL the run, not log a publish: the evergreen slug means a silent
    # no-op leaves yesterday's article in place, so the site looks current while the job is dead.
    if not nj.publish_hugo(title, body, "opinions", tags, desc, image_path=img, emoji="🐠",
                           stable_slug=STABLE_SLUG):
        nj.log(f"[fishbowl-daily] NOT PUBLISHED — quality guard rejected: {title}")
        return 1
    nj.git_push("opinions", title)
    nj.notify_slack("fishbowl", f"🐠 {title}", "Nova's daily Fishbowl dispatch updated.")
    nj.log(f"[fishbowl-daily] PUBLISHED (evergreen): {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
