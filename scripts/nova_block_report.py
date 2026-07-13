#!/usr/bin/env python3
"""nova_block_report.py — PRIVATE daily 'block report': everything the scanners heard within walking
distance of a saved anchor (home/school/work) in the last 24h, in FULL detail. Private (posts to
Slack, not the public journal), so exact addresses are fine. Uses the distance/bearing enrichment +
the code-reference vectors so Nova narrates it accurately and translates the codes. Scheduled daily.
"""
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj
import nova_voice
from nova_code_reference import code_reference_block
from nova_notify import notify

MEM_DSN = "host=localhost dbname=nova_memories user=kochj"
RADIUS_MI = 2.5


def main():
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True
    mc = mem.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    mc.execute(
        "SELECT source, text, created_at, (metadata->'geo'->>'nearest_mi')::float mi, "
        "metadata->'geo'->>'nearest_dir' dir, metadata->'geo'->'anchor' anchor "
        "FROM memories WHERE source IN ('scanner','fire') "
        "AND created_at > now() - interval '24 hours' "
        "AND (metadata->'geo'->>'nearest_mi')::float <= %s "
        "ORDER BY (metadata->'geo'->>'nearest_mi')::float", (RADIUS_MI,))
    rows = mc.fetchall()
    mem.close()

    if not rows:
        notify("\U0001F3D8️ Block report",
               body=f"Quiet 24h — nothing within {RADIUS_MI} mi of home on the scanners.",
               category="block_report", dedup_key="block-report")
        print("[block-report] nothing close"); return

    lines = []
    for r in rows:
        t = r["created_at"].strftime("%a %H:%M")
        anc = r["anchor"] or {}
        where = f"~{r['mi']} mi {r['dir'] or ''} of home"
        if anc.get("name") and anc["name"] != "home":
            where += f" (~{anc['mi']} mi {anc.get('dir','')} of {anc['name']})"
        kind = "FIRE" if r["source"] == "fire" else "LAPD"
        lines.append(f"[{t}] {kind} — {where}: {r['text'][:220]}")
    block = "\n".join(lines)
    ref = code_reference_block(block, ["police", "fire"])

    system = nova_voice.system_prompt(
        "Write a PRIVATE 'block report' for Jordan: everything the police/fire scanners heard within "
        f"~{RADIUS_MI} miles of home in the last 24h. This is private (goes to Slack, NOT the public "
        "journal), so exact addresses are fine here. LEAD with the closest incident. For each: the "
        "time, what it actually was (translate any radio/penal codes using the reference below), and "
        "where (address + distance + compass direction from home). Flag anything very close (<1 mi). "
        "Group trivial/repeat chatter into a line. Dry, factual, lightly wry. 250-500 words." + ref)
    body = nj.call_openrouter(system, block, max_tokens=1200, temperature=0.6)
    if not body or len(body) < 80:
        print("[block-report] LLM produced too little"); return

    notify(f"\U0001F3D8️ Block report — {len(rows)} calls within {RADIUS_MI} mi",
           body=body[:2800], category="block_report", dedup_key="block-report")
    print(f"[block-report] reported {len(rows)} nearby incidents", flush=True)


if __name__ == "__main__":
    main()
