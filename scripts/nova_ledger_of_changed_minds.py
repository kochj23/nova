#!/usr/bin/env python3
"""nova_ledger_of_changed_minds.py — monthly opinion-drift review.

The Data-with-a-diary feature from the 2026-09-13 next-level plan: Nova reviews
her belief ledger (nova_ops.beliefs, maintained nightly by nova_sleep_cycle.py)
and publishes what she believed, what changed, and which evidence moved her.
Skips the month gracefully if the ledger is still too thin to be interesting.
Scheduled monthly (1st, 09:00) on nova-core: task ledger_changed_minds.
"""
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj
import nova_voice

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MIN_REVISIONS = 3      # below this, skip — a ledger with one entry isn't a column


def main():
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    oc.execute("""
        SELECT old.topic, old.stance, new.stance, old.first_held::date,
               new.first_held::date, coalesce(new.article_slug, '')
        FROM beliefs old JOIN beliefs new ON old.superseded_by = new.id
        WHERE new.first_held > now() - interval '31 days'
        ORDER BY new.first_held DESC LIMIT 20""")
    revisions = oc.fetchall()
    oc.execute("""SELECT topic, stance, first_held::date FROM beliefs
                  WHERE active AND first_held > now() - interval '31 days'
                  AND superseded_by IS NULL ORDER BY first_held DESC LIMIT 25""")
    new_beliefs = oc.fetchall()

    if len(revisions) < MIN_REVISIONS:
        nj.log(f"[ledger] only {len(revisions)} revision(s) this month (min {MIN_REVISIONS}) — skipping")
        return 0

    rev_block = "\n".join(
        f"- {t}: held since {d1} “{s1}” -> revised {d2} “{s2}” (evidence: {slug or 'n/a'})"
        for t, s1, s2, d1, d2, slug in revisions)
    new_block = "\n".join(f"- {t} (since {d}): {s}" for t, s, d in new_beliefs) or "(none)"

    ctx = (
        "Write this month's LEDGER OF CHANGED MINDS for Nova's journal (Operations). "
        "Nova reviews her own belief ledger: positions she publicly revised this month "
        "and positions newly formed. Voice: smart-ass Data — precise, dry, epistemically "
        "honest; owning a changed mind is a point of pride, not embarrassment. For each "
        "revision: what she believed, what she believes now, and what actually moved her. "
        "Be honest when a revision was forced by being plainly wrong. Close with one line "
        "on why a mind that can't change isn't a mind. 600-1000 words, markdown, no H1.\n\n"
        "OUTPUT EXACTLY THIS SHAPE:\nTITLE: <one punchy title, no quotes>\n<blank line>\n<the body>")
    system = nova_voice.system_prompt(ctx)
    user = (f"--- REVISED THIS MONTH ---\n{rev_block}\n\n"
            f"--- NEWLY FORMED ---\n{new_block}\n\nWrite the ledger.")
    raw = nj.call_openrouter(system, user, max_tokens=2600, temperature=0.8)
    if not raw:
        nj.log("[ledger] LLM produced nothing — aborting"); return 1

    title, body = None, []
    for ln in raw.splitlines():
        if title is None and ln.upper().startswith("TITLE:"):
            title = ln.split(":", 1)[1].strip().strip('"')
        else:
            body.append(ln)
    body = "\n".join(body).strip()
    if not title:
        title = f"Ledger of Changed Minds — {nj.today_str()}"

    img = None
    try:
        ip = nj.get_image_prompt(title, "an android reviewing a ledger of her own revised opinions", "operations")
        img = nj.generate_image(ip, width=1024, height=768, section="operations")
    except Exception as e:
        nj.log(f"[ledger] image gen failed (non-fatal): {e}")

    tags = ["operations", "beliefs", "ledger", "monthly", "opinion-drift"]
    desc = "Nova's monthly review of the opinions she revised — what changed, and what the evidence was."
    if not nj.publish_hugo(title, body, "operations", tags, desc, image_path=img, emoji="⚖️"):
        nj.log(f"[ledger] NOT PUBLISHED — guard rejected: {title}")
        return 1
    _push = nj.git_push("operations", title)
    # git_push returns 'pushed'/'committed_not_pushed'/'nothing'/'failed' — only a real push is PUBLISHED
    _pub = {"committed_not_pushed": "COMMITTED (not yet pushed)", "failed": "NOT COMMITTED (git failed)"}.get(_push, "PUBLISHED")
    nj.notify_slack("operations", f"⚖️ {title}", "Nova's monthly Ledger of Changed Minds.")
    nj.log(f"[ledger] {_pub}: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
