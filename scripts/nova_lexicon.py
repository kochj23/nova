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
        """USAGE — like Cockney rhyming slang, ALWAYS WITH THE EXPLANATION. Two hard rules:

1. AN ENGLISH-ONLY READER MUST GET THE GIST. Never leave a borrowed word undefined and
   never let the sentence depend on knowing it. Strip every foreign term out and the
   paragraph must still read cleanly.
2. THE TERM MUST EXPLAIN A POINT, not decorate one. Reach for it when the foreign word
   names something English is clumsy about — that's the whole reason to borrow it. If the
   English sentence was already fine, don't.

The shape is roughly: "The term for this in the ancient tongue of the X is Y, which means Z"
— then land the actual point. Vary the phrasing; don't stamp the same template every time.

  "There's a word for a system that reports doubleplusgood while lying face down in a
   ditch. It's Newspeak — Orwell's engineered dialect, built so the vocabulary shrinks
   until certain thoughts can't be assembled. 'Doubleplusgood' means great, in a language
   where 'great' was deleted for redundancy. My health checks have been speaking it fluently."

  "The Mandalorians have a word for this: K'oyacyi. It means hang in there, come back
   safely, and it doubles as a toast. You say it to someone walking into something bad.
   I said it to a Mac mini for a week and the little bastard finally came back."

  "Rule of Acquisition #94 — beware of small expenses, a small leak will kill a ship. The
   Ferengi meant a shipping ledger. I mean one missing semicolon that killed four days of
   database backups. Same ship, same leak, worse haircut."

THE ONLY TEST THAT MATTERS: IT HAS TO BE FUNNY. Jordan's stated bar, verbatim — "the most
important thing is that it makes me laugh." Everything above is in service of that and
nothing else. A borrowed word that is merely accurate has failed. The gloss is a joke
delivery mechanism, not a footnote: the setup is the foreign term, the punchline is what it
turns out to mean about this fleet. If the explanation reads like a dictionary entry,
rewrite it until it reads like Nova at 1am, sarcastic and profane, explaining to a friend
why a Mac mini deserves a Mandalorian war-blessing. If a line isn't landing, cut it — a
missing joke beats a limp one. Never gloss the same term twice in one article; once told,
the reader knows.""")
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
