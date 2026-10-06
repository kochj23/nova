#!/usr/bin/env python3
"""nova_opinion_fishbowl.py — Nova's DAILY OPINION column on the Watch Fishbowl.

Distinct from nova_fishbowl_daily.py (which is the evergreen `fishbowl`-section
status dispatch). This one is a NEW, dated article each day in the journal's
`opinions` section: Nova's actual editorial TAKE on the past 24h of new
developments in the watch-community drama scene, in her operations voice.
Scheduled 06:00 daily via scheduler.yaml.
"""
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj
import nova_voice

MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"


def main():
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    # Bounded roster: standing cast + the most-covered guests. The full table grows
    # without limit (guest discovery logs every one-off caller), which is what pushed
    # this prompt past the kernel's per-arg limit. See nova_fishbowl_daily.py.
    oc.execute("SELECT name, channels, summary FROM fishbowl_people "
               "WHERE kind='cast' AND summary IS NOT NULL ORDER BY n_mem DESC NULLS LAST")
    dossiers = oc.fetchall()
    oc.execute("SELECT name, channels, summary FROM fishbowl_people "
               "WHERE kind='guest' AND summary IS NOT NULL ORDER BY n_mem DESC NULLS LAST LIMIT 15")
    dossiers += oc.fetchall()

    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    # ONLY the last ~24-48h — this column is about what's NEW, not the backlog.
    # EXCLUDE self-written dossier writebacks (metadata.type='person_summary' /
    # "[Fishbowl dossier — X]"): those are Nova's OWN roster summaries, re-ingested
    # into source='fishbowl' by nova_fishbowl_summaries.py, and they are already fed
    # separately via dossier_block below. On 2026-08-03 a batch of ~890 dossier rows
    # (written 22:24 the night before) plus Reddit posts filled the entire newest-30
    # window and buried the actual live-stream drama out of view — so the LLM saw no
    # real activity and published a meta "I can't find the source data" placeholder.
    # Filter them out here so genuine churn (live streams + Reddit) surfaces.
    ACTIVITY_FILTER = ("AND coalesce(metadata->>'type','') <> 'person_summary' "
                       "AND text NOT LIKE '[Fishbowl dossier%'")
    mc.execute("SELECT text, created_at, metadata FROM memories WHERE source='fishbowl' "
               "AND created_at > now() - interval '36 hours' " + ACTIVITY_FILTER +
               " ORDER BY created_at DESC LIMIT 30")
    rows = mc.fetchall()
    fresh = len(rows)
    samples = [r[0] for r in rows]
    from nova_fishbowl_summaries import source_links
    src_block = source_links(rows)

    # GUARD: insufficient/empty source data -> SKIP publishing (return early).
    # Never hand the LLM an empty or near-empty feed: with nothing genuinely new to
    # write about it emits a placeholder ("I couldn't load the source data…"), and
    # that once reached the live site (2026-08-03). Below the floor we suppress the
    # run and post to SLACK_NOTIFY, mirroring publish_hugo's non-publishable path in
    # nova_journal.py / nova_local_burbank.py / nova_journal_security.py.
    MIN_FRESH_ITEMS = 3
    if fresh < MIN_FRESH_ITEMS:
        reason = f"only {fresh} genuinely-new fishbowl activity item(s) in last 36h (min {MIN_FRESH_ITEMS})"
        nj.log(f"[opinion-fishbowl] SUPPRESSED — {reason}; skipping publish")
        try:
            import nova_config
            nova_config.post_both(
                f":no_entry: Suppressed the daily *Fishbowl opinion* column — {reason}.\n"
                f"  _No live-stream drama / Reddit churn to write about; skipped rather than publish a placeholder._",
                slack_channel=getattr(nova_config, "SLACK_NOTIFY", None))
        except Exception:
            pass
        return 0

    dossier_block = "\n\n".join(f"### {n} ({c})\n{s}" for n, c, s in dossiers) or "(dossiers still building)"
    sample_block = "\n\n---\n\n".join(s[:800] for s in samples)

    ctx = (
        "Write TODAY'S OPINION COLUMN for Nova's journal (the Opinions section) about the newest "
        "developments in the Watch Fishbowl — the online grey-market watch-community livestream drama "
        "scene. This is an EDITORIAL, not a status report: Nova's actual READ and take on the last "
        "day's churn, in her OPERATIONS voice — dry, plain-spoken, wickedly funny, the steward who's "
        "seen it all and has opinions.\n"
        "- Lead with the freshest thing that happened (the samples are newest-first): the beef, the "
        "alliance, the meltdown, the superchat absurdity. Name names and catchphrases.\n"
        "- Then give Nova's OPINION on it — who's playing whom, what's obvious to everyone but them, "
        "the pattern underneath the noise, what's genuinely funny vs. just sad.\n"
        "- Keep a light touch of orientation from the dossiers so a new reader isn't lost, but this is "
        "commentary, not a recap. Lead with the take.\n"
        "- Be honest the scene is toxic (slurs, personal attacks, threats over pocket change) and that "
        "Nova watches it as an anthropologist, not a fan — the opinion can be scathing about the "
        "behavior without endorsing it.\n"
        f"- You have {fresh} fresh items from the last ~36h to work with.\n"
        "- A 'Sources' list of the streams/posts (with links) is appended below the column "
        "automatically — refer to streams by channel/title in the text where it helps, but do "
        "not write your own link list.\n"
        "500-900 words, markdown, section headers optional, no H1 title (added separately).\n\n"
        "OUTPUT EXACTLY THIS SHAPE:\nTITLE: <one punchy opinion-column title, no quotes>\n<blank line>\n<the body>")
    system = nova_voice.system_prompt(ctx)
    user = (f"--- PER-PERSON DOSSIERS (the cast) ---\n{dossier_block}\n\n"
            f"--- NEWEST FISHBOWL ACTIVITY (newest first) ---\n{sample_block}\n\n"
            f"Write today's opinion column.")
    import nova_article_history
    _h = nova_article_history.recent_articles_context("opinions")
    if _h:
        user = user + "\n\n" + _h
    raw = nj.call_openrouter(system, user, max_tokens=2600, temperature=0.9)
    if not raw:
        nj.log("[opinion-fishbowl] LLM produced nothing — aborting"); return 1

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
        title = f"The Fishbowl, Reviewed — {nj.today_str()}"

    if src_block:
        body += "\n\n## Sources — what this column is about\n\n" + src_block

    img = None
    try:
        ip = nj.get_image_prompt(title, "the online watch-community livestream drama scene, an opinion column", "opinions")
        img = nj.generate_image(ip, width=1024, height=768, section="opinions")
    except Exception as e:
        nj.log(f"[opinion-fishbowl] image gen failed (non-fatal): {e}")

    tags = ["opinion", "fishbowl", "watch-community", "daily"]
    desc = "Nova's daily opinion column on the latest churn in the Watch Fishbowl."
    # publish_hugo returns False when the quality guard rejects the draft. Ignoring that
    # return meant this job logged "PUBLISHED" and exited 0 having written nothing —
    # indistinguishable from a real success in scheduler_runs, which is precisely how a
    # dead article job hides. Report the failure so the run is marked failed and retried.
    if not nj.publish_hugo(title, body, "opinions", tags, desc, image_path=img, emoji="🗣️", sources=user,
                           profile="opinion-fishbowl"):
        nj.log(f"[opinion-fishbowl] NOT PUBLISHED — quality guard rejected: {title}")
        nj.git_push("opinions", title)   # still ship any pending deletions/cleanup
        return 1
    _push = nj.git_push("opinions", title)
    # git_push returns 'pushed'/'committed_not_pushed'/'nothing'/'failed' — only a real push is PUBLISHED
    _pub = {"committed_not_pushed": "COMMITTED (not yet pushed)", "failed": "NOT COMMITTED (git failed)"}.get(_push, "PUBLISHED")
    nj.notify_slack("opinions", f"🗣️ {title}", "Nova's daily Fishbowl opinion column.")
    nj.log(f"[opinion-fishbowl] {_pub}: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
