#!/opt/homebrew/bin/python3
"""nova_directive_conflicts.py — flags pairs of standing feedback rules that make opposite demands on the same
subject, so a human can resolve them instead of Nova picking one silently.

This is a keyword detector, not a judgement. It finds candidates: a rule that forbids something and another
rule that requires the same thing, sharing subject words. Each candidate goes to a person to decide. The
detector never edits a rule or chooses between them.

Usage: nova_directive_conflicts.py [--dry-run] [--notify]
Written by Jordan Koch (via Claude).
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn()
MIN_SHARED = 2
STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "is", "it", "be", "this", "that", "with",
        "any", "all", "not", "no", "never", "always", "must", "do", "don", "t", "i", "you", "they", "their",
        "when", "if", "as", "by", "at", "from", "use", "should", "will", "can", "each", "every", "his", "her",
        "nova", "little", "mister", "jordan", "one", "more", "than", "into", "about", "only"}

# (family, demand-to-require pattern, demand-to-forbid pattern). A rule matching one side and another rule
# matching the other side, with shared subject words, is a candidate conflict.
FAMILIES = [
    ("ask-before-acting",
     re.compile(r"\b(always|must|should) (ask|confirm|get approval|check with)\b"),
     re.compile(r"\b(never ask|don't ask|do not ask|without asking|just execute|no permission|never confirm)\b")),
    ("publish",
     re.compile(r"\b(always|must) (publish|post|send)\b"),
     re.compile(r"\b(never (publish|post|send)|do not (publish|post|send)|must not (appear|be) (public|published))\b")),
    ("alerts",
     re.compile(r"\b(always|must) (alert|ping|notify|page)\b"),
     re.compile(r"\b(never (alert|ping|notify|page)|don't (alert|ping|notify|page)|do not (alert|ping|notify|page)|just don't set off alerts)\b")),
    ("logging",
     re.compile(r"\b(always|must) (log|record|audit)\b"),
     re.compile(r"\b(never (log|record)|do not (log|record)|don't (log|record))\b")),
]


def words(text: str) -> set:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2 and w not in STOP}


def candidates(rules: list) -> list:
    """rules: [(name, text)] -> [(family, name_a, name_b, shared_words)]. Pure."""
    out = []
    for family, require, forbid in FAMILIES:
        req = [(n, t) for n, t in rules if require.search(t.lower())]
        fb = [(n, t) for n, t in rules if forbid.search(t.lower())]
        for na, ta in req:
            for nb, tb in fb:
                if na == nb:
                    continue
                shared = words(ta) & words(tb)
                if len(shared) >= MIN_SHARED:
                    out.append((family, na, nb, sorted(shared)))
    return out


def load_rules(conn) -> list:
    cur = conn.cursor()
    cur.execute("SELECT name, content FROM claude_memories WHERE type = 'feedback' ORDER BY name")
    return cur.fetchall()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--notify", action="store_true", help="post the candidates to the event bus")
    a = ap.parse_args(argv)
    import psycopg2
    conn = _nova_dsn.pg_connect()
    try:
        found = candidates(load_rules(conn))
    finally:
        conn.close()
    if not found:
        print("no candidate conflicts")
        return 0
    for family, na, nb, shared in found:
        print(f"[{family}] {na}  <->  {nb}   shared: {', '.join(shared)}")
    if a.notify and not a.dry_run:
        import nova_notify
        nova_notify.notify(
            f"{len(found)} candidate rule conflict(s) for review",
            "\n".join(f"{f}: {x} <-> {y}" for f, x, y, _ in found),
            level="info", category="governance", source="nova_directive_conflicts",
            dedup_key="directive-conflicts-" + "|".join(sorted(f"{x}{y}" for _, x, y, _ in found)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
