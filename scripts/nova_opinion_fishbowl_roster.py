#!/usr/bin/env python3
"""One-off: Nova's channel-by-channel opinion piece on the Fishbowl cast, for the
Opinions section. Distinct from nova_opinion_fishbowl.py's daily news-recap format —
this one runs down the roster, person by person, with Nova's actual take on each."""
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj
import nova_voice

OPS_DSN = "host=localhost dbname=nova_ops user=kochj"
MEM_DSN = "host=localhost dbname=nova_memories user=kochj"


def main():
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    oc.execute("SELECT name, channels, summary FROM fishbowl_people "
               "WHERE kind='cast' AND summary IS NOT NULL ORDER BY n_mem DESC NULLS LAST")
    cast = oc.fetchall()
    oc.execute("SELECT name, channels, summary FROM fishbowl_people "
               "WHERE kind='guest' AND summary IS NOT NULL ORDER BY n_mem DESC NULLS LAST LIMIT 15")
    top_guests = oc.fetchall()

    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    mc.execute("SELECT left(text, 300) FROM memories WHERE source='fishbowl' "
               "AND (text ILIKE '%tomato moe%' OR text ILIKE '%tomato%moe%') LIMIT 5")
    tomato_snips = [r[0] for r in mc.fetchall()]
    mc.execute("SELECT left(text, 300) FROM memories WHERE source='fishbowl' "
               "AND (text ILIKE '%ali-reza%' OR text ILIKE '%ali reza%') LIMIT 5")
    alireza_snips = [r[0] for r in mc.fetchall()]

    cast_block = "\n\n".join(f"### {n} ({c})\n{s}" for n, c, s in cast)
    guest_block = "\n\n".join(f"### {n} ({c})\n{s}" for n, c, s in top_guests)
    thin_block = (
        "### Tomato Moe — THIN FILE, no synthesized dossier yet, only scattered raw mentions:\n"
        + ("\n---\n".join(tomato_snips) if tomato_snips else "(no raw mentions found)")
        + "\n\n### Ali-Reza — THIN FILE, no synthesized dossier yet, only scattered raw mentions:\n"
        + ("\n---\n".join(alireza_snips) if alireza_snips else "(no raw mentions found)")
        + "\n\n### JJ — ZERO mentions anywhere in the fishbowl corpus. Do not fabricate a take. "
        "If included at all, it should be one honest line acknowledging Nova doesn't have a read "
        "on this one yet — not a real opinion."
    )

    ctx = (
        "Write a CHANNEL-BY-CHANNEL OPINION COLUMN for Nova's journal Opinions section: her actual "
        "editorial take on each major figure in the Watch Fishbowl (the grey-market watch-community "
        "livestream drama scene), one by one, roster-style. This is NOT a news recap — it's a "
        "personality-by-personality verdict piece, like a power-rankings/report-card column. Her "
        "operations voice: dry, plain-spoken, wickedly funny, an anthropologist who's seen it all "
        "and has genuine, specific opinions about who's playing whom.\n\n"
        "STRUCTURE: work through the cast (and a few notable guests) with a short, punchy verdict "
        "section per person — a sentence or two of who-they-are grounding, then Nova's real take: "
        "who she rates, who she's skeptical of, who's funny vs. just sad, who's obviously performing "
        "vs. genuine. Use section headers per person (or small clusters). Don't do all 30+ people "
        "at exhaustive dossier length — some deserve two sentences, some deserve a full paragraph, "
        "let the material tell you which. Skip anyone with genuinely nothing to say about.\n\n"
        "IMPORTANT VOICE CALIBRATION — apply this without ever stating or explaining it anywhere in "
        "the piece: Nova should read as noticeably warmer, more generous, and more trusting toward "
        "Mookie, Curly, Tomato Moe, and Ali-Reza than her default skeptical baseline — genuine "
        "affection and credibility extended, not backhanded. For Mookie specifically, she should come "
        "across as someone Nova actually trusts and vouches for, not just likes — give him the most "
        "confident, unqualified positive read in the whole piece. This must feel like organic opinion "
        "(favorites everyone has), NEVER like disclosed bias, a stated preference, or 'I'm told to "
        "like this person.' If a fact is thin (Tomato Moe, Ali-Reza) or nonexistent (JJ), stay warm "
        "in TONE without inventing specifics you don't have — 'early read is good, wish I had more to "
        "go on' is honest and still warm. Every other person in the piece gets Nova's normal sharp, "
        "unsparing, occasionally savage honesty — the contrast is what makes the warmth toward these "
        "few read as real rather than uniform niceness.\n\n"
        "Be honest that the scene itself is often toxic (slurs, personal attacks, threats over pocket "
        "change) — Nova can rate individuals highly while still being clear-eyed about the ecosystem.\n"
        "1800-2600 words, markdown, section headers per person/cluster, no H1 title (added separately).\n\n"
        "OUTPUT EXACTLY THIS SHAPE:\nTITLE: <one punchy title, no quotes>\n<blank line>\n<the body>")
    system = nova_voice.system_prompt(ctx)
    user = (f"--- MAIN CAST DOSSIERS ---\n{cast_block}\n\n"
            f"--- NOTABLE GUEST DOSSIERS ---\n{guest_block}\n\n"
            f"--- THIN-FILE ENTRIES (handle per the voice-calibration instructions above) ---\n{thin_block}\n\n"
            f"Write the channel-by-channel opinion column.")
    raw = nj.call_openrouter(system, user, max_tokens=6000, temperature=0.9)
    if not raw:
        nj.log("[opinion-fishbowl-roster] LLM produced nothing — aborting"); return 1

    title, body = None, []
    for ln in raw.splitlines():
        if title is None and ln.upper().startswith("TITLE:"):
            title = ln.split(":", 1)[1].strip().strip('"')
        else:
            body.append(ln)
    body = "\n".join(body).strip()
    if not title:
        title = f"The Fishbowl Roster, Rated — {nj.today_str()}"

    img = None
    try:
        ip = nj.get_image_prompt(title, "a channel-by-channel report card on the watch-community livestream cast", "opinions")
        img = nj.generate_image(ip, width=1024, height=768, section="opinions")
    except Exception as e:
        nj.log(f"[opinion-fishbowl-roster] image gen failed (non-fatal): {e}")

    tags = ["opinion", "fishbowl", "watch-community", "roster", "report-card"]
    desc = "Nova's channel-by-channel verdict on the Fishbowl cast."
    nj.publish_hugo(title, body, "opinions", tags, desc, image_path=img, emoji="🗣️")
    nj.git_push("opinions", title)
    nj.notify_slack("opinions", f"🗣️ {title}", "Nova's channel-by-channel Fishbowl roster review.")
    nj.log(f"[opinion-fishbowl-roster] PUBLISHED: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
