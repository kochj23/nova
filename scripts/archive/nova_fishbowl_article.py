#!/usr/bin/env python3
"""nova_fishbowl_article.py — Nova writes a re-introduction to "The Fishbowl" (the
watch-community livestream drama scene) in her OPERATIONS voice, from her fishbowl
memories + the per-person dossiers, with a cover image. Publishes to the journal
operations section. Run via launchd (needs FDA for /Volumes/Data + git push).
"""
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj
import nova_voice

MEM_DSN = "host=localhost dbname=nova_memories user=kochj"
OPS_DSN = "host=localhost dbname=nova_ops user=kochj"


def main():
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    oc.execute("SELECT name, channels, summary FROM fishbowl_people "
               "WHERE summary IS NOT NULL ORDER BY n_mem DESC NULLS LAST")
    dossiers = oc.fetchall()
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    mc.execute("SELECT text FROM memories WHERE source='fishbowl' "
               "AND metadata->>'type'='fishbowl_stream' ORDER BY created_at DESC LIMIT 12")
    samples = [r[0] for r in mc.fetchall()]
    if not samples:
        mc.execute("SELECT text FROM memories WHERE source='fishbowl' ORDER BY created_at DESC LIMIT 12")
        samples = [r[0] for r in mc.fetchall()]

    dossier_block = "\n\n".join(f"### {n} ({c})\n{s}" for n, c, s in dossiers) or "(dossiers still building)"
    sample_block = "\n\n---\n\n".join(s[:800] for s in samples)

    ctx = (
        "Write a re-introduction to 'The Fishbowl' for Nova's operations journal, in Nova's "
        "OPERATIONS voice — the steward who runs systems and reports plainly, with dry wit. "
        "Explain what the Fishbowl is: the online grey-market watch-community livestream drama "
        "scene. Introduce the main players using the dossiers. Describe how Nova now ingests and "
        "tracks it — YouTube live-capture (recording streams from the start before they're deleted), "
        "transcripts + live chat/superchats, and the r/TheTpGentleman subreddit, all in her "
        "'fishbowl' memory vector, with per-person dossiers kept up to date and guests caught by name "
        "and catchphrase (e.g., one host's relentless 'It's Hard'). Be honest that the community is "
        "toxic — constant slurs, personal attacks, and death threats traded over minor superchats — "
        "and that Nova treats all of it as OBSERVATIONAL data, not endorsed. 700-1100 words, markdown, "
        "section headers welcome, no H1 title (added separately).\n\n"
        "OUTPUT EXACTLY THIS SHAPE:\nTITLE: <one punchy title, no quotes>\n<blank line>\n<the body>")
    system = nova_voice.system_prompt(ctx)
    user = (f"--- PER-PERSON DOSSIERS ---\n{dossier_block}\n\n"
            f"--- SAMPLE MEMORIES (transcripts/chat/reddit) ---\n{sample_block}\n\n"
            f"Write the re-introduction to The Fishbowl.")
    raw = nj.call_openrouter(system, user, max_tokens=3200, temperature=0.7)
    if not raw:
        nj.log("[fishbowl-article] LLM produced nothing — aborting"); return 1

    title, body = None, []
    for ln in raw.splitlines():
        if title is None and ln.upper().startswith("TITLE:"):
            title = ln.split(":", 1)[1].strip().strip('"')
        else:
            body.append(ln)
    title = title or "The Fishbowl, Re-Introduced: A Field Guide to the Watch World's Loudest Room"
    body = "\n".join(body).strip()

    img = None
    try:
        ip = nj.get_image_prompt(title, "the online watch-community livestream drama scene", "operations")
        img = nj.generate_image(ip, width=1024, height=768, section="operations")
    except Exception as e:
        nj.log(f"[fishbowl-article] image gen failed (non-fatal): {e}")

    tags = ["fishbowl", "watch-community", "drama", "operations"]
    desc = "Nova re-introduces The Fishbowl — the watch-community livestream drama scene she now ingests and tracks."
    nj.publish_hugo(title, body, "operations", tags, desc, image_path=img, emoji="🐠")
    nj.git_push("operations", title)
    nj.notify_slack("operations", f"🐠 {title}", "Nova re-introduces The Fishbowl.")
    nj.log(f"[fishbowl-article] PUBLISHED: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
