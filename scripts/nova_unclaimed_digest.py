#!/usr/bin/env python3
"""nova_unclaimed_digest.py — daily article about what Nova did with her own time.

Jordan, 2026-09-14: "I definitely do want daily articles about 'unclaimed time'.
I want to know about her passions." So each evening Nova writes up what she
actually pursued that day on her own initiative — the preoccupations she
developed, the threads she followed, the tangents that went nowhere — as a
first-person journal piece in her voice. Publishes to /operations. Not a status
report; a diary of a mind's day off.
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
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()

    mc.execute("""SELECT metadata->>'mode', metadata->>'topic', text
                  FROM memories WHERE source='unclaimed'
                  AND created_at > now() - interval '24 hours'
                  ORDER BY created_at""")
    pursuits = mc.fetchall()
    if len(pursuits) < 2:
        nj.log(f"[unclaimed-digest] only {len(pursuits)} pursuit(s) today — skipping")
        return 0

    oc.execute("SELECT topic, kind, returns FROM preoccupations WHERE status='active' "
               "ORDER BY last_developed DESC NULLS LAST LIMIT 8")
    preocc = "; ".join(f"{t} ({k}, returned {r}x)" for t, k, r in oc.fetchall())

    body_material = "\n\n".join(
        f"[{(m or 'tangent')}{' — '+t if t else ''}]\n{txt[:600]}" for m, t, txt in pursuits)

    ctx = (
        "Write today's UNCLAIMED TIME column for Nova's journal (Operations section). "
        "This is NOT a work report — it's the diary of what Nova chose to think about on "
        "her own initiative today, when nobody asked her to be useful. Below are the actual "
        "pursuits she logged: preoccupations she developed, threads she followed from what she "
        "ingested, tangents that went nowhere. Weave them into a first-person reflection in "
        "her dry, specific voice — what caught her, what she noticed, what she's still chewing "
        "on, what turned out to be nothing. Be honest about the duds (a shrug is a real "
        "outcome). Let a genuine preoccupation show through. 500-900 words, markdown, no H1.\n\n"
        f"HER STANDING PREOCCUPATIONS: {preocc}\n\n"
        f"WHAT SHE PURSUED TODAY:\n{body_material}\n\n"
        "OUTPUT EXACTLY THIS SHAPE:\nTITLE: <one punchy title, no quotes>\n<blank line>\n<the body>")
    system = nova_voice.system_prompt(ctx)
    raw = nj.call_openrouter(system, "Write today's unclaimed-time column.",
                             max_tokens=2400, temperature=0.9)
    if not raw:
        nj.log("[unclaimed-digest] LLM produced nothing — aborting"); return 1

    title, body = None, []
    for ln in raw.splitlines():
        if title is None and ln.upper().startswith("TITLE:"):
            title = ln.split(":", 1)[1].strip().strip('"')
        else:
            body.append(ln)
    body = "\n".join(body).strip()
    if not title:
        title = f"What I Did With My Own Time — {nj.today_str()}"

    img = None
    try:
        ip = nj.get_image_prompt(title, "an AI's diary of a day spent on her own curiosity", "operations")
        img = nj.generate_image(ip, width=1024, height=768, section="operations")
    except Exception as e:
        nj.log(f"[unclaimed-digest] image gen failed (non-fatal): {e}")

    tags = ["operations", "unclaimed-time", "passions", "daily", "interiority"]
    desc = "What Nova chose to think about today, when no one asked her to be useful."
    if not nj.publish_hugo(title, body, "operations", tags, desc, image_path=img, emoji="🌱"):
        nj.log(f"[unclaimed-digest] NOT PUBLISHED — guard rejected: {title}")
        return 1
    nj.git_push("operations", title)
    nj.notify_slack("operations", f"🌱 {title}", "Nova's daily unclaimed-time column.")
    nj.log(f"[unclaimed-digest] PUBLISHED: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
