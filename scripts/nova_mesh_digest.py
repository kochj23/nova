#!/usr/bin/env python3
"""nova_mesh_digest.py — surface the neighborhood Meshtastic chatter as something INTERESTING.

Nova already captures the local LoRa mesh community channel (nova_meshtastic_bridge logs every
inbound text to shared_observations), but nothing ever reads it — 550+ real human messages since
2026-07-29 sitting unlooked-at: 'Good morning ☀️', 'The humidity is crazy', 'Rain expected
anywhere today?', '☕☕☕☕☕'. This is not an attack surface. It's the neighbors talking. This
job reads the day's chatter and tells Jordan what the mesh was talking about, warmly — the
interesting-things posture, not the threat-analyst one.

PRIVACY: the mesh is a shared public broadcast channel, but the messages are third-party content,
so (1) summarization runs on the LOCAL Ollama — chatter never leaves the box — and (2) output
goes to Jordan's private Slack, never the public journal. Node IDs are the only identifiers and
they're pseudonymous hex handles, but we still don't publish this.

Run: daily, evening. Manual: `python3 nova_mesh_digest.py [--hours 24] [--dry-run]`.
"""
from __future__ import annotations
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config as nc

DSN = "host=localhost dbname=nova_ops user=kochj"
OLLAMA_URL = "http://192.168.1.6:11434/api/generate"
OLLAMA_MODEL = "qwen3-coder:30b"
# obvious radio-test / keep-alive noise — not conversation
_NOISE = re.compile(r"^\s*(test\w*|ping|ack|hello world|seq \d+|node \w+|\W{0,3})\s*$", re.I)


def fetch_messages(hours: int) -> list:
    import psycopg2
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute(
            "SELECT observed_at, substring(subject from 'from (.*)') AS who, observation "
            "FROM shared_observations WHERE observer='nova_meshtastic_bridge' "
            "AND subject ILIKE 'message from%%' AND observed_at > now() - make_interval(hours => %s) "
            "ORDER BY observed_at", (hours,))
        return [{"t": r[0], "who": (r[1] or "?"), "text": (r[2] or "").strip()} for r in cur.fetchall()]


def _interesting(msgs: list) -> list:
    seen, out = set(), []
    for m in msgs:
        t = m["text"]
        if not t or _NOISE.match(t):
            continue
        k = t.lower()
        if k in seen:      # collapse the exact-duplicate "Good morning" doubles
            continue
        seen.add(k)
        out.append(m)
    return out


def local_llm(system: str, user: str) -> str:
    payload = json.dumps({"model": OLLAMA_MODEL, "prompt": f"/no_think\n\n{system}\n\n{user}",
                          "stream": False, "think": False,
                          "options": {"temperature": 0.7, "num_predict": 700}}).encode()
    try:
        req = urllib.request.Request(OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=120).read())
        txt = (d.get("response") or "").strip()
        return txt.split("</think>", 1)[-1].strip() if "</think>" in txt else txt
    except Exception as e:
        print(f"mesh_digest: local LLM error: {e}", file=sys.stderr)
        return ""


def main() -> int:
    hours = 24
    if "--hours" in sys.argv:
        hours = int(sys.argv[sys.argv.index("--hours") + 1])
    dry = "--dry-run" in sys.argv

    msgs = _interesting(fetch_messages(hours))
    if len(msgs) < 3:
        print(f"mesh_digest: only {len(msgs)} real message(s) in {hours}h — nothing worth a digest")
        return 0

    participants = len({m["who"] for m in msgs})
    transcript = "\n".join(f"- {m['text']}" for m in msgs[:60])

    system = (
        "You are Nova, Jordan's AI, telling him what the neighborhood Meshtastic radio mesh was "
        "chatting about today. This is a shared community LoRa channel — real local people, talking. "
        "Your posture is WARM and CURIOUS, a friend relaying neighborhood gossip — NOT a security "
        "analyst. These are not threats or signals to monitor; they're people. Note the vibe, the "
        "recurring topics (weather, heat, coffee, radio tests), anything funny or sweet, the sense of "
        "a small community keeping in touch over homemade radio. 120-200 words, first person, address "
        "him as 'Little Mister'. Don't list every message — capture the FEELING of the day's chatter. "
        "No security framing whatsoever.")
    user = (f"Today {len(msgs)} messages came across the mesh from about {participants} people. "
            f"Here's the chatter:\n{transcript}\n\nTell me about the neighborhood today.")

    summary = local_llm(system, user)
    if not summary or len(summary.split()) < 20:
        # graceful fallback: just show the highlights, no LLM
        highlights = " · ".join(f"“{m['text']}”" for m in msgs[:6])
        summary = (f"The mesh was alive today — {len(msgs)} messages from ~{participants} neighbors. "
                   f"A taste: {highlights}")

    body = f":radio: *The neighborhood mesh today* ({len(msgs)} msgs, ~{participants} people)\n\n{summary}"
    if dry:
        print(body)
        return 0
    try:
        if not nc.post_both(body, slack_channel=nc.SLACK_CHAN, discord_channel=None):
            raise RuntimeError("post_both delivered nowhere")
        print(f"mesh_digest: posted ({len(msgs)} msgs, {participants} people)")
    except Exception as e:
        print(f"mesh_digest: post failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
