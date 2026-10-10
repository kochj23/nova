#!/usr/bin/env python3
"""nova_spatial_query.py — answer 'what's happening near me?' from enriched scanner/fire memories.

answer(text) detects a spatial/proximity question and returns a concise, ready-to-send answer built
from recent distance/bearing-tagged transmissions. Returns None when the text is NOT a spatial
question, so the gateway can fall through to normal chat. Measures against home + any saved anchors.
"""
import re
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))

import nova_dsn as _nova_dsn  # noqa: E402
MEM_DSN = _nova_dsn.pg_dsn("nova_memories")

SPATIAL = re.compile(
    r"(near ?by|near me|around me|closest|how (close|far)|within\s+[\d.]+\s*(mile|mi|block)s?|"
    r"in the (area|neighborhood|hood)|on the scanner|walkable|walk to|block(s)? (away|from)|"
    r"anything (happening|going on) (near|around|close)|whats? (near|around|close))", re.I)
RADIUS_RE = re.compile(r"within\s+(\d+(?:\.\d+)?)\s*(mile|mi|block)s?", re.I)


def is_spatial(text):
    return bool(SPATIAL.search(text or ""))


def answer(text, hours=6):
    """Return a spatial answer string, or None if `text` isn't a proximity question."""
    if not is_spatial(text):
        return None
    m = RADIUS_RE.search(text or "")
    radius = float(m.group(1)) if m else 3.0
    if m and m.group(2).lower().startswith("block"):
        radius = round(radius * 0.06, 2)              # a Burbank block ≈ 0.06 mi
    closest_only = bool(re.search(r"\bclosest\b|how close|nearest\b", text or "", re.I))

    con = psycopg2.connect(MEM_DSN); con.autocommit = True
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT source, text, created_at, (metadata->'geo'->>'nearest_mi')::float mi, "
        "metadata->'geo'->>'nearest_dir' dir FROM memories WHERE source IN ('scanner','fire') "
        "AND created_at > now() - interval '%d hours' "
        "AND (metadata->'geo'->>'nearest_mi')::float <= %f "
        "ORDER BY (metadata->'geo'->>'nearest_mi')::float LIMIT 12" % (hours, radius))
    rows = cur.fetchall(); con.close()

    if not rows:
        return (f"Nothing on the police/fire scanners within {radius:g} mi of home in the last "
                f"{hours}h — quiet out there. (Distances only cover transmissions with a clean address.)")

    def line(r):
        k = "\U0001F692" if r["source"] == "fire" else "\U0001F693"
        snip = re.sub(r"^\[.*?\]\s*", "", (r["text"] or ""))[:95]
        return f"{k} ~{r['mi']} mi {r['dir'] or ''} · {r['created_at'].strftime('%a %H:%M')} · {snip}"

    if closest_only:
        return "Closest recent activity: " + line(rows[0])
    body = "\n".join(line(r) for r in rows[:8])
    return f"Within {radius:g} mi of home, last {hours}h ({len(rows)} located):\n{body}"


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "anything happening near me right now?"
    print(answer(q) or "(not a spatial question)")
