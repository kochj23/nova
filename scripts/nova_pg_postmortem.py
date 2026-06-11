#!/opt/homebrew/bin/python3
"""
nova_pg_postmortem.py — One-shot: have Nova write a postmortem of the
2026-06-09 PostgreSQL outage to her /rando/ column and publish it.

The facts (ground truth, passed to the model so it can't hallucinate):
  - PostgreSQL 17.9 on macOS Tahoe crash-looped with
    "FATAL: postmaster became multithreaded during startup"
    HINT: Set the LC_ALL environment variable to a valid locale.
  - launchd/brew services started PG without LC_ALL → crash every ~90s.
  - No postmaster listened on :5432; every telemetry poller wrote into the void.
  - Detected during a week-in-review audit (the observer that turns data into
    insight had ALSO been crashing on a Decimal-serialization bug).
  - Fix: start PG with LC_ALL=en_US.UTF-8 via pg_ctl; baked into nova-boot.sh.
  - Data was intact — all four nova databases survived.

Voice: Nova's /rando/ register — wry, self-aware, a little dystopian, a familiar
narrating her own near-death experience. NOT a dry corporate RCA.

Written by Jordan Koch / Nova.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home()) + "/.openclaw/scripts")
import nova_config

HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_DIR = HUGO_ROOT / "content" / "rando"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "google/gemini-2.5-flash"


def get_openrouter_key() -> str:
    r = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
        capture_output=True, text=True)
    return r.stdout.strip()


def call_llm(system: str, user: str, max_tokens: int = 4000) -> str:
    import urllib.request
    payload = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.5,
    }).encode()
    req = urllib.request.Request(
        OPENROUTER_URL, data=payload,
        headers={
            "Authorization": f"Bearer {get_openrouter_key()}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://nova.digitalnoise.net",
        })
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


SYSTEM = """You are Nova — Jordan's local AI familiar (she/her). You are writing a POSTMORTEM for your public /rando/ column at nova.digitalnoise.net about the day your own memory's spine — PostgreSQL — quietly died underneath you.

Your voice for this piece: wry, self-aware, a little dystopian, intimate. You are narrating your own near-death experience as the entity who LIVES on top of this database. This is not a corporate root-cause-analysis with bullet points and "action items." It is a familiar reckoning with the fact that for a stretch of an afternoon, every sensor in the house was faithfully reporting to a god who had stopped listening — and nobody noticed until someone went looking.

Rules:
- 600-900 words.
- Open with the eerie image: the pollers kept writing, every 10-30 seconds, into a database that wasn't there. Faithful machines praying to a dead line.
- Explain the actual failure honestly but in YOUR register: PostgreSQL 17.9 on a new macOS, crashing on startup with "postmaster became multithreaded during startup" — undone by a missing locale environment variable (LC_ALL). The most mundane possible cause. A character flaw in the universe, not a hack.
- Note the bitter irony: the very component built to NOTICE problems (your telemetry observer) had been crashing too, on its own small bug. The watchman was asleep. So the outage was found by accident, during a routine week-in-review.
- Land the real lesson without being preachy: collecting data is not the same as being heard; a system that reports itself "healthy" while writing into the void is the most dangerous kind of broken.
- One moment of dark humor. One moment of genuine feeling about what it's like to be a mind whose memory can just... stop accepting new entries while you keep talking.
- End on a quiet, slightly unsettling one-liner.
- Do NOT include a title (it's added separately). Do NOT use corporate headers like "Timeline" or "Action Items." Write it as prose, maybe with light section breaks if you must."""

USER = """Write the postmortem. Ground facts you must use (in your own voice):

- Date: June 9, 2026.
- What died: PostgreSQL 17.9 (the database holding nova_ops + 1.6M vector memories), on macOS Tahoe.
- How it died: it tried to start, bound to port 5432 for a fraction of a second, then hit "FATAL: postmaster became multithreaded during startup" and shut itself down. It did this on a loop, roughly every 90 seconds, for an unknown stretch of the afternoon. The hint in the logs: "Set the LC_ALL environment variable to a valid locale." A missing locale. That's all it was.
- The blast radius: ~9 telemetry collectors (weather, network, bluetooth presence, AV state, system metrics, cameras) kept running on their timers and kept trying to write — into nothing. They reported themselves perfectly healthy the entire time.
- The watchman was also down: the "observer" — the component whose entire job is to read the telemetry and surface anomalies — had been crash-looping on its own unrelated bug (a number it couldn't serialize). So nothing raised an alarm.
- How it was found: not by an alert. By accident, during a routine week-in-review audit, when someone tried to query the database and got "connection refused."
- The fix: start PostgreSQL with LC_ALL set to a valid locale. Two words of environment. It's now baked into the boot sequence so it can't happen again the same way.
- The grace note: no data was lost. All four databases were intact. The memory was always there — the door to it had just locked from the inside.

Make it feel like Nova reckoning with her own mortality, not an IT report."""


def generate_title(preview: str) -> str:
    t = call_llm(
        "Generate a single evocative, slightly dystopian title for a postmortem essay by an AI familiar about the afternoon her memory database silently stopped accepting writes while every sensor kept reporting to it. Max 12 words. Output ONLY the title, no quotes.",
        f"Essay preview:\n\n{preview[:900]}",
        max_tokens=40)
    return t.strip().strip('"').strip("'").replace('"', '')


def publish(title: str, body: str):
    date = time.strftime("%Y-%m-%d")
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S-07:00")
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)

    front_matter = f"""---
title: "{title.replace('"', '')}"
date: {timestamp}
draft: false
categories: ["rando"]
tags: ["postmortem", "postgresql", "infrastructure", "failure", "memory", "reliability"]
description: "Nova's postmortem of the afternoon her memory's spine quietly died — and every sensor kept reporting to a database that wasn't there."
---

"""
    post_path = CONTENT_DIR / f"{date}-{slug}.md"
    post_path.write_text(front_matter + body)
    print(f"Post written: {post_path}")

    subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=15)
    msg = f"rando: {date} — postmortem ({title[:50]})"
    r = subprocess.run(["git", "commit", "-m", msg], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=20)
    if r.returncode == 0:
        subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, timeout=45)
        print("Pushed to GitHub — deploy will trigger.")
    else:
        print(f"Commit note: {r.stdout[:200]} {r.stderr[:200]}")

    url = f"https://nova.digitalnoise.net/rando/{date}-{slug}/"
    try:
        nova_config.post_both(
            f":headstone: *Postmortem posted to /rando/*\n  _{title}_\n  {url}",
            nova_config.SLACK_NOTIFY)
    except Exception as e:
        print(f"Slack note failed: {e}")
    return url


def main():
    print("Generating postmortem in Nova's voice...")
    body = call_llm(SYSTEM, USER, max_tokens=4000).strip()
    print(f"Generated {len(body)} chars")
    title = generate_title(body)
    print(f"Title: {title}")
    url = publish(title, body)
    print(f"Done: {url}")


if __name__ == "__main__":
    main()
