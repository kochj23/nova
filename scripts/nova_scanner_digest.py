#!/usr/bin/env python3
"""nova_scanner_digest.py — hourly rollup of raw scanner chatter.

Scanner audio is 35% of Nova's memory intake (72K rows/30d, avg 96 chars —
"4-5-6 roger" at industrial scale) and it floods topical recall: a Burbank
fire query returns eight LAPD radio acks and no fire. Per the never-prune
guardrail, raw rows stay untouched; this job writes ONE digest memory per
talkgroup-hour ON TOP (source='scanner_digest'), linked to its constituent
rows via memory_links(link_type='distilled_from'). Recall then surfaces
digests instead of chatter. Scheduled hourly at :10 on nova-core.
"""
import json
import sys
import urllib.request
from datetime import datetime

import psycopg2

MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
# Resilient native ollama across nodes (router shim unreliable for qwen3; .6 thrashes).
LLM_MODEL = "qwen3:8b"
OLLAMA_NODES = ["http://192.168.1.251:11434", "http://192.168.1.86:11434",
                "http://192.168.1.252:11434", "http://192.168.1.7:11434",
                "http://192.168.1.6:11434"]
MIN_ROWS = 8          # below this, the hour isn't worth a digest


def log(m):
    print(f"[scanner-digest {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, max_tokens=260):
    body = json.dumps({"model": LLM_MODEL, "stream": False, "think": False,
                       "options": {"temperature": 0.3, "num_predict": max_tokens},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    for node in OLLAMA_NODES:
        try:
            req = urllib.request.Request(node + "/api/chat", method="POST",
                                         headers={"Content-Type": "application/json"}, data=body)
            with urllib.request.urlopen(req, timeout=90) as r:
                out = json.load(r).get("message", {}).get("content", "").strip()
            if out:
                return out
        except Exception:
            continue
    return ""


def remember(text, metadata):
    req = urllib.request.Request(
        f"{MEMSRV}/remember", method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"text": text, "source": "scanner_digest",
                         "metadata": metadata}).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("id")


def main():
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    # Previous full hour, grouped by whatever channel identifier the rows carry.
    mc.execute("""
        SELECT coalesce(metadata->>'talkgroup', metadata->>'channel',
                        metadata->>'system', 'unknown') AS grp,
               array_agg(id ORDER BY created_at) AS ids,
               string_agg(left(text, 160), E'\n' ORDER BY created_at) AS blob,
               count(*) AS n
        FROM memories
        WHERE source='scanner'
          AND created_at >= date_trunc('hour', now() - interval '1 hour')
          AND created_at <  date_trunc('hour', now())
        GROUP BY 1 HAVING count(*) >= %s""", (MIN_ROWS,))
    groups = mc.fetchall()
    hour = datetime.now().strftime("%Y-%m-%d %H:00")
    made = 0
    for grp, ids, blob, n in groups:
        try:
            digest = llm(
                "Summarize one hour of public-safety radio traffic into 1-3 factual "
                "sentences: incidents (type + location if stated), volume, anything "
                "unusual. Routine acknowledgments are 'routine traffic'. No preamble.\n\n"
                f"CHANNEL: {grp}\nTRANSMISSIONS ({n}):\n{blob[:6000]}")
        except Exception as e:
            log(f"{grp}: llm failed ({e})"); continue
        if not digest or len(digest) < 20:
            continue
        mid = remember(
            f"[Scanner {grp} — {hour}] {digest}",
            {"type": "scanner_digest", "talkgroup": str(grp), "hour": hour,
             "n_transmissions": n, "privacy": "private",
             "source_ref": f"scanner://{grp}/{hour.replace(' ', 'T')}"})
        if mid:
            made += 1
            try:
                mc.execute(
                    "INSERT INTO memory_links (source_id, target_id, link_type) "
                    "SELECT %s, unnest(%s::text[]), 'distilled_from' "
                    "ON CONFLICT DO NOTHING", (str(mid), ids))
            except Exception as e:
                log(f"{grp}: link insert failed ({e})")
    log(f"{made} digest(s) from {len(groups)} group(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
