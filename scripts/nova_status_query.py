#!/usr/bin/env python3
"""nova_status_query.py — answer 'what changed / what did you do today?' from REAL data, not vibes.

Detects a status/change question and returns a grounded summary pulled from claude_actions (today's
logged work), the journal git log, and today's memory ingestion. Returns None when the text is NOT a
status question, so the gateway falls through to normal chat. This is the fix for Nova confabulating
"nothing changed" — she reads the receipts instead of guessing.
"""
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"

_PATTS = [
    r"chang(e|es|ed).{0,30}(today|occurred|happened|made|ops)",
    r"(today'?s?|recent|latest).{0,20}chang",
    r"what'?s?\s+(new|different|up|happening|going on)",
    r"what\s+(has|have|did|are|you)\b.{0,25}\b(chang|happen|do|been|built|ship|work|accomplish)",
    r"what\s+(you\s+)?(been\s+)?(doing|working on|up to)",
    r"any\s+(changes|updates)",
    r"recent(ly)?\s+(changes?|added|built|shipped)",
    r"look\s+(in|into|at)\s+the\s+ops",
    r"ops\s?d(b|atabase)",
    r"status\s+(report|update)",
    r"what\s+did\s+you\s+(do|build|change|ship|accomplish)",
]
STATUS = re.compile("|".join(_PATTS), re.I)


def is_status(text):
    return bool(STATUS.search(text or ""))


def _q(dsn, sql):
    c = psycopg2.connect(dsn); c.autocommit = True
    cur = c.cursor(); cur.execute(sql); r = cur.fetchall(); c.close()
    return r


def _voice(facts):
    """Reword the EXACT facts in Nova's voice — snark added, facts unchanged. Falls back to None."""
    try:
        import nova_journal as nj
        import nova_voice
        system = (nova_voice.NOVA_VOICE_SHORT + "\n\nBelow are the EXACT, VERIFIED facts about what "
                  "changed today (straight from the databases). Rewrite them as a short Slack reply in "
                  "your voice — snarky, dry, put-upon, a little proud despite yourself. HARD RULES: keep "
                  "EVERY number, script name, and fact EXACTLY as given; invent NOTHING; add no new "
                  "claims or events; you may reorder and pile on attitude/commentary, but never facts. "
                  "Tight — under ~140 words. No preamble, no bullet headers, just talk.")
        out = nj.call_openrouter(system, facts, max_tokens=420, temperature=0.8)
        return out.strip() if out and len(out.strip()) > 40 else None
    except Exception:
        return None


def answer(text, voiced=True):
    """Return a grounded 'what changed today' summary, or None if not a status question.
    voiced=True reruns the facts through Nova's voice (snark on, facts unchanged), with fallback."""
    if not is_status(text):
        return None
    day = datetime.now().strftime("%A, %B %-d")

    acts = _q(OPS_DSN, "SELECT action_type, count(1) FROM claude_actions "
                       "WHERE ts::date=current_date GROUP BY 1 ORDER BY 2 DESC")
    total = sum(c for _, c in acts)
    breakdown = ", ".join(f"{c} {t.replace('_', ' ')}" for t, c in acts)

    newf = _q(OPS_DSN, "SELECT DISTINCT target FROM claude_actions WHERE ts::date=current_date "
                       "AND action_type='file_write' AND target LIKE '%.py' ORDER BY target")
    files = sorted({Path(f[0]).name for f in newf if f[0]})

    try:
        jl = subprocess.run(["git", "-C", str(Path.home() / "nova-journal"), "log", "--oneline",
                             "--since=midnight"], capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
    except Exception:
        jl = []

    mem = _q(MEM_DSN, "SELECT source, count(1) FROM memories WHERE created_at::date=current_date "
                      "GROUP BY 1 ORDER BY 2 DESC LIMIT 6")
    memtxt = ", ".join(f"{s} ({c})" for s, c in mem)

    out = [f"*What actually changed today ({day})* — reading the receipts:",
           f"• {total} logged actions: {breakdown}."]
    if files:
        out.append(f"• New scripts written ({len(files)}): {', '.join(files[:14])}")
    if jl:
        latest = re.sub(r"[*_`]", "", jl[0][8:66]).strip()   # strip markdown so titles don't leak **bold**
        out.append(f"• Journal: {len(jl)} commits (latest: {latest})")
    if memtxt:
        out.append(f"• Memory ingested today: {memtxt}")
    out.append("_Straight from claude_actions + git + the memory DB. Not a vibe._")
    raw = "\n".join(out)
    return (_voice(raw) or raw) if voiced else raw


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "what changed today?"
    print(answer(q) or "(not a status question)")
