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

MEM_DSN = "host=localhost dbname=nova_memories user=kochj"
OPS_DSN = "host=localhost dbname=nova_ops user=kochj"


def main():
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    oc.execute("SELECT name, channels, summary FROM fishbowl_people "
               "WHERE summary IS NOT NULL ORDER BY n_mem DESC NULLS LAST")
    dossiers = oc.fetchall()

    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    # ONLY the last ~24-48h — this column is about what's NEW, not the backlog.
    mc.execute("SELECT text, created_at FROM memories WHERE source='fishbowl' "
               "AND created_at > now() - interval '36 hours' ORDER BY created_at DESC LIMIT 30")
    rows = mc.fetchall()
    fresh = len(rows)
    if not rows:
        # nothing new in the window — fall back to freshest available so the column still runs
        mc.execute("SELECT text, created_at FROM memories WHERE source='fishbowl' "
                   "ORDER BY created_at DESC LIMIT 20")
        rows = mc.fetchall()
    samples = [r[0] for r in rows]
    if not samples:
        nj.log("[opinion-fishbowl] no fishbowl memories yet — aborting"); return 1

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
        "500-900 words, markdown, section headers optional, no H1 title (added separately).\n\n"
        "OUTPUT EXACTLY THIS SHAPE:\nTITLE: <one punchy opinion-column title, no quotes>\n<blank line>\n<the body>")
    system = nova_voice.system_prompt(ctx)
    user = (f"--- PER-PERSON DOSSIERS (the cast) ---\n{dossier_block}\n\n"
            f"--- NEWEST FISHBOWL ACTIVITY (newest first) ---\n{sample_block}\n\n"
            f"Write today's opinion column.")
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

    img = None
    try:
        ip = nj.get_image_prompt(title, "the online watch-community livestream drama scene, an opinion column", "opinions")
        img = nj.generate_image(ip, width=1024, height=768, section="opinions")
    except Exception as e:
        nj.log(f"[opinion-fishbowl] image gen failed (non-fatal): {e}")

    tags = ["opinion", "fishbowl", "watch-community", "daily"]
    desc = "Nova's daily opinion column on the latest churn in the Watch Fishbowl."
    nj.publish_hugo(title, body, "opinions", tags, desc, image_path=img, emoji="🗣️")  # dated post
    nj.git_push("opinions", title)
    nj.notify_slack("opinions", f"🗣️ {title}", "Nova's daily Fishbowl opinion column.")
    nj.log(f"[opinion-fishbowl] PUBLISHED: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
