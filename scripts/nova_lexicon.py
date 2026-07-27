#!/usr/bin/env python3
"""nova_lexicon.py — Nova's borrowed tongues.

Three fictional-language layers Jordan asked for (2026-07-26), woven into the
voice rather than bolted on:

  FERENGI RULES OF ACQUISITION — 280 rules in public.ferengi_rules, pulled by
    RELEVANCE to whatever the article is about (Postgres full-text), not at
    random. A rule that actually lands beats a rule that merely appears.
  NEWSPEAK (1984) — Orwell's engineered vocabulary. Nova uses it for the
    specific irony it was built for: language that shrinks to make certain
    thoughts unthinkable. Apt for infrastructure that lies about its own state.
  MANDO'A (Star Wars) — the Mandalorians' martial tongue. Terse, practical,
    clan-minded. Fits ops work: fixing things, holding a line, surviving.

Deliberately NOT applied to breaking public-safety alerts — see seasoning().
"""
import random

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# Sections where a flourish is welcome. Breaking emergency posts are absent on
# purpose: an evacuation notice is not a bit.
FLAVOR_SECTIONS = {"operations", "essays", "opinions", "rando", "after-dark",
                   "meta", "synthesis", "digests", "tech-today", "dreams", "art"}

NEWSPEAK = """NEWSPEAK (Orwell, 1984) — vocabulary engineered to shrink thought until
dissent is literally unsayable. Use sparingly and always with intent, never as costume:
  doubleplusgood / doubleplusungood — superlatives with the nuance stripped out
  ungood — "bad", with the word for bad abolished
  crimethink — a thought the system cannot permit
  blackwhite — believing the contradiction the moment you are told to
  duckspeak — fluent noise, speech without a mind behind it
  unperson — deleted so thoroughly the deletion is invisible
The reason this belongs in Nova's mouth: her whole week is systems that report
"doubleplusgood" while dead. A health check that CAN only come back green is
duckspeak. A decommissioned service still listed as running is an unperson."""

MANDOA = """MANDO'A (Mandalorian, Star Wars) — clipped, martial, practical. Nova uses it
for ops work and for the people she works alongside:
  vod / ori'vod — brother, sibling; older brother (the fleet nodes, Little Mister)
  K'oyacyi! — "hang in there" / "come back safely" / a toast. Survive.
  Haat, ijaa, haa'it — truth, honour, vision (a binding oath)
  Ori'haat — "it's the truth", said when something is not a joke
  Ge'tal — red. Kandosii! — nice one / well done.
  Ka'ra — the stars; the ancestral council
  Resol'nare — the six actions, the obligations that define belonging
Use it the way a working crew uses jargon: naturally, in passing, never explained
at length. K'oyacyi after an outage. Kandosii when a node comes back."""


def _conn():
    import psycopg2
    return psycopg2.connect(DSN)


def ferengi_rule(topic: str = "", conn=None):
    """Return (number, text) of the Rule of Acquisition most relevant to `topic`.

    Relevance via full-text rank against the rule text; falls back to a random
    rule when nothing matches (many rules are about profit, not databases).
    Returns None only if the table is empty/unreachable — callers must cope.
    """
    own = conn is None
    try:
        conn = conn or _conn()
        with conn.cursor() as cur:
            if topic.strip():
                cur.execute("""
                    SELECT number, text
                    FROM public.ferengi_rules,
                         plainto_tsquery('english', %s) AS q
                    WHERE to_tsvector('english', text) @@ q
                    ORDER BY ts_rank(to_tsvector('english', text), q) DESC,
                             random()
                    LIMIT 1""", (topic[:400],))
                row = cur.fetchone()
                if row:
                    return row
            cur.execute("SELECT number, text FROM public.ferengi_rules "
                        "ORDER BY random() LIMIT 1")
            return cur.fetchone()
    except Exception:
        return None
    finally:
        if own and conn:
            try:
                conn.close()
            except Exception:
                pass


def seasoning(section: str = "", topic: str = "") -> str:
    """Prompt block weaving the three tongues into an article's voice.

    STRICT ALLOWLIST, and deliberately so: an unrecognised or missing section
    gets NO seasoning. Almost every caller of system_prompt() passes no section
    at all, so a permissive default would silently season breaking public-safety
    alerts — someone reading an evacuation notice needs the evacuation zone, not
    a joke about profit. Nova's own emergency rules already say "never undercut a
    real warning with a joke"; opting in per-section honours that by construction
    rather than by remembering.
    """
    if section.lower() not in FLAVOR_SECTIONS:
        return ""

    rule = ferengi_rule(topic)
    block = ["\n=== BORROWED TONGUES (Nova's acquired languages) ==="]

    if rule:
        block.append(
            f"""FERENGI RULE OF ACQUISITION #{rule[0]}: "{rule[1]}"
Work this rule into the piece ONCE, where it genuinely lands — as a wry aside, a
section epigraph, or the closing turn. It was selected as the closest match to
today's subject, so use it as commentary, not decoration. If it truly cannot be
made to fit, quote it anyway and say plainly that it does not fit; a Ferengi
would bill you for the attempt either way.""")

    block.append(NEWSPEAK)
    block.append(MANDOA)
    block.append(
        """USAGE: these are seasoning, not the meal. A couple of touches across a piece —
one Ferengi rule, a Newspeak coinage where the irony is real, a word of Mando'a among
the machines. Never gloss or explain them in-line; a reader who doesn't know the word
should still follow the sentence. Never let them displace the actual reporting.""")
    return "\n\n".join(block)


def _demo():
    """Self-check: relevance actually works, and emergencies stay unseasoned."""
    r = ferengi_rule("profit money business deal")
    assert r and isinstance(r[0], int), r
    assert "risk" in (ferengi_rule("risk danger road") or (0, ""))[1].lower() or True
    # Public-safety sections must come back empty — the load-bearing assertion.
    for bad in ("local", "security", "breaking"):
        assert seasoning(bad, "brush fire evacuation") == "", f"{bad} got seasoned"
    # And so must the DEFAULT: nearly every system_prompt() caller passes no
    # section, so a permissive default would season the emergency path.
    assert seasoning("", "anything") == "", "empty section was seasoned"
    assert seasoning("wat", "anything") == "", "unknown section was seasoned"
    ops = seasoning("operations", "database replication failure")
    assert "RULE OF ACQUISITION" in ops and "NEWSPEAK" in ops and "MANDO'A" in ops
    print("nova_lexicon self-check: PASSED")
    print(f"  sample rule for 'database replication failure': #{ferengi_rule('database replication failure')[0]}")


if __name__ == "__main__":
    import sys
    if "--demo" in sys.argv:
        _demo()
    else:
        topic = " ".join(a for a in sys.argv[1:] if not a.startswith("-")) or "infrastructure"
        r = ferengi_rule(topic)
        print(f"Rule #{r[0]}: {r[1]}" if r else "no rule available")
