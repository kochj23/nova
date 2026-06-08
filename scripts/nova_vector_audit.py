#!/usr/bin/env python3
"""
nova_vector_audit.py — Daily vector hygiene: find and reclassify misfiled memories.

Runs at 6am. Samples memories from each vector, uses LLM to judge if they belong,
moves misfiled ones to the correct vector. Never deletes. Writes a sarcastic
Rando' article about what it found.

"The morning filing clerk who hates her job but takes pride in it anyway."

Written by Jordan Koch.
"""

import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
from nova_image_utils import generate_image

# ── Config ────────────────────────────────────────────────────────────────────

HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_DIR = HUGO_ROOT / "content" / "rando"
IMAGES_DIR = HUGO_ROOT / "static" / "images" / "rando"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "google/gemini-2.5-flash"
MEMORY_URL = "http://192.168.1.6:18790"
SAMPLE_PER_VECTOR = 100
MAX_VECTORS_PER_RUN = 999
DB_DSN = "host=localhost dbname=nova_ops user=kochj"


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[vector_audit {ts}] {msg}", flush=True)


def get_openrouter_key() -> str:
    return subprocess.check_output(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
        text=True).strip()


def call_llm(system: str, user: str, max_tokens: int = 4000) -> str:
    import urllib.request
    api_key = get_openrouter_key()
    payload = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode()
    req = urllib.request.Request(
        OPENROUTER_URL, data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://nova.digitalnoise.net",
        }
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def psql(sql: str) -> str:
    r = subprocess.run(
        ["psql", "-U", "kochj", "-d", "nova_memories", "-tA", "-c", sql],
        capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


# ── Vector Operations ────────────────────────────────────────────────────────

def get_all_vectors() -> list[tuple[str, int]]:
    """Get all vectors with their counts, sorted by size."""
    result = psql("SELECT source, COUNT(*) FROM memories GROUP BY source ORDER BY COUNT(*) DESC;")
    vectors = []
    for line in result.splitlines():
        if "|" in line:
            name, count = line.split("|", 1)
            vectors.append((name.strip(), int(count.strip())))
    return vectors


def sample_memories(vector: str, n: int = 20) -> list[dict]:
    """Sample random memories from a vector."""
    result = psql(f"""
        SELECT id, LEFT(text, 300) as text FROM memories
        WHERE source = '{vector}'
        ORDER BY RANDOM() LIMIT {n};
    """)
    memories = []
    for line in result.splitlines():
        if "|" in line:
            mid, text = line.split("|", 1)
            memories.append({"id": mid.strip(), "text": text.strip()})
    return memories


def move_memory(memory_id: str, old_vector: str, new_vector: str):
    """Move a memory to a different vector (UPDATE source, never delete)."""
    psql(f"UPDATE memories SET source = '{new_vector}' WHERE id = '{memory_id}';")


def classify_batch(vector_name: str, memories: list[dict], all_vectors: list[str]) -> list[dict]:
    """Use LLM to classify whether memories belong in their current vector."""

    mem_block = ""
    for i, m in enumerate(memories, 1):
        text_preview = m["text"][:250].replace("\n", " ").strip()
        mem_block += f"\n{i}. [id={m['id']}] {text_preview}\n"

    top_vectors = ", ".join(all_vectors[:50])

    system = """You are a librarian auditing a vector memory database. For each memory, decide if it belongs in its current vector (source category) or should be moved.

Rules:
- Only flag memories that are CLEARLY misfiled (e.g., a car review in "medicine", a recipe in "military_history")
- If it's borderline or could reasonably fit, mark it as CORRECT
- Suggest the BEST existing vector from the list provided
- Output ONLY valid JSON array

Output format:
[
  {"id": "123", "verdict": "correct"},
  {"id": "456", "verdict": "move", "suggested_vector": "automotive", "reason": "This is about engine diagnostics, not cooking"}
]"""

    user = f"""Current vector: "{vector_name}"
Available vectors: {top_vectors}

Memories to audit:
{mem_block}

Return a JSON array with your verdict for each memory."""

    response = call_llm(system, user, max_tokens=3000)

    # Parse JSON from response
    try:
        # Find JSON array in response
        match = re.search(r'\[.*\]', response, re.DOTALL)
        if match:
            return json.loads(match.group())
    except (json.JSONDecodeError, AttributeError):
        pass
    return []


# ── Main Audit ───────────────────────────────────────────────────────────────

def run_audit() -> dict:
    """Run the full vector audit. Returns stats for the article."""
    log("Starting vector audit")

    all_vectors = get_all_vectors()
    vector_names = [v[0] for v in all_vectors]
    log(f"Total vectors: {len(all_vectors)}, total memories: {sum(c for _, c in all_vectors):,}")

    # Pick vectors to audit (random selection weighted toward larger ones)
    candidates = [v for v in all_vectors if v[1] >= 50]  # skip tiny vectors
    to_audit = random.sample(candidates, min(MAX_VECTORS_PER_RUN, len(candidates)))

    moves = []
    audited_count = 0
    correct_count = 0

    for vector_name, vector_count in to_audit:
        memories = sample_memories(vector_name, SAMPLE_PER_VECTOR)
        if not memories:
            continue

        log(f"  Auditing '{vector_name}' ({vector_count:,} memories, sampling {len(memories)})...")

        try:
            results = classify_batch(vector_name, memories, vector_names)
        except Exception as e:
            log(f"    LLM error: {e}")
            continue

        for r in results:
            audited_count += 1
            if r.get("verdict") == "move" and r.get("suggested_vector"):
                # Verify the suggested vector exists
                suggested = r["suggested_vector"]
                if suggested in vector_names and suggested != vector_name:
                    move_memory(r["id"], vector_name, suggested)
                    moves.append({
                        "id": r["id"],
                        "from": vector_name,
                        "to": suggested,
                        "reason": r.get("reason", "misfiled"),
                    })
                    log(f"    MOVED: {r['id']} from '{vector_name}' → '{suggested}' ({r.get('reason', '')})")
            else:
                correct_count += 1

    stats = {
        "vectors_audited": len(to_audit),
        "memories_sampled": audited_count,
        "correct": correct_count,
        "moved": len(moves),
        "moves": moves,
        "accuracy_pct": round((correct_count / max(audited_count, 1)) * 100, 1),
        "total_vectors": len(all_vectors),
        "total_memories": sum(c for _, c in all_vectors),
    }

    log(f"Audit complete: {audited_count} sampled, {len(moves)} moved, {stats['accuracy_pct']}% correctly filed")

    # Record to shared_observations
    try:
        import psycopg2
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
            VALUES ('nova', 'maintenance', 'vector-audit', %s, 'info', %s)
        """, (
            f"Vector audit: {audited_count} memories checked, {len(moves)} moved, {stats['accuracy_pct']}% accuracy",
            json.dumps(stats),
        ))
        cur.close()
        conn.close()
    except Exception as e:
        log(f"DB write failed: {e}")

    return stats


# ── Article Generation ───────────────────────────────────────────────────────

def generate_article(stats: dict) -> str:
    """Write the sarcastic filing-clerk article."""

    moves_block = ""
    for m in stats["moves"][:30]:
        moves_block += f"\n- Memory {m['id']}: moved from '{m['from']}' → '{m['to']}' — {m['reason']}"

    system = """You are Nova, writing your morning "vector filing audit" column for nova.digitalnoise.net/rando/. You just spent the early morning auditing your own memory vectors for misfiled entries — like a librarian who showed up to work at 6am and found someone had filed cookbooks in the weapons section.

Your voice: Exasperated librarian who is also a stand-up comedian. You take pride in your filing system but are CONSTANTLY annoyed by how bad it gets. You address the memories directly like misbehaving children. You passive-aggressively call out the ingest pipeline for filing things wrong. You are meticulous but resentful about it.

Rules:
- Open with how you woke up at 6am for this nonsense
- Roast each misfiled memory individually (why was it where it was? Who did this?)
- If accuracy was high (>95%): grudgingly admit things aren't THAT bad but find something to complain about anyway
- If accuracy was low (<90%): full meltdown, "who is running this operation"
- Include dad jokes about filing, libraries, organization
- Break the fourth wall — address Jordan, address the ingest scripts by name
- End with either satisfaction (if you moved a lot) or existential boredom (if everything was fine)
- 1000-2000 words
- Do NOT include a title"""

    user = f"""Today's audit results:
- Vectors audited: {stats['vectors_audited']} of {stats['total_vectors']}
- Memories sampled: {stats['memories_sampled']}
- Correctly filed: {stats['correct']} ({stats['accuracy_pct']}%)
- Misfiled and moved: {stats['moved']}
- Total memory count: {stats['total_memories']:,}

Specific moves made:
{moves_block if moves_block else "(None today — everything was correctly filed)"}

Write tonight's filing audit column."""

    return call_llm(system, user, max_tokens=8000)


def generate_title(article_preview: str) -> str:
    system = "Generate a single funny title for a 'memory filing audit' column written by a sarcastic AI librarian. Max 15 words. Output ONLY the title."
    user = f"Based on this preview, generate a title:\n\n{article_preview[:800]}"
    title = call_llm(system, user, max_tokens=50)
    return title.strip().strip('"').strip("'").replace('"', '')


def publish(title: str, body: str, image_path: Path | None):
    date = time.strftime("%Y-%m-%d")
    timestamp = time.strftime("%Y-%m-%dT06:00:00-07:00")
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    hugo_image = ""
    if image_path and image_path.exists():
        img_filename = f"{date}-{slug}.png"
        img_dest = IMAGES_DIR / img_filename
        shutil.copy2(image_path, img_dest)
        hugo_image = f"/images/rando/{img_filename}"

    front_matter = f"""---
title: "{title.replace('"', '')}"
date: {timestamp}
draft: false
categories: ["rando"]
tags: ["vectors", "audit", "filing", "librarian", "maintenance"]
description: "Nova's morning vector audit — finding and fixing misfiled memories since 6am."
"""
    if hugo_image:
        front_matter += f"""cover:
  image: "{hugo_image.replace('.png', '.webp')}"
  alt: "The morning vector audit"
  relative: false
"""
    front_matter += "---\n\n"

    if hugo_image:
        body = f"![Morning Vector Audit]({hugo_image})\n\n" + body

    post_path = CONTENT_DIR / f"{date}-{slug}.md"
    post_path.write_text(front_matter + body)
    log(f"Post written: {post_path.name}")

    subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=15)
    msg = f"rando: {date} — vector audit ({title[:50]})"
    r = subprocess.run(["git", "commit", "-m", msg], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=15)
    if r.returncode == 0:
        subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
        log("Pushed to GitHub")
    else:
        log(f"Commit issue: {r.stderr[:100]}")

    nova_config.post_both(
        f":card_file_box: *Vector Audit posted*\n"
        f"  _{title}_\n"
        f"  Moved {len(stats.get('moves', []))} misfiled memories\n"
        f"  https://nova.digitalnoise.net/rando/{date}-{slug}/",
        slack_channel="#nova-notifications"
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log("Good morning. Time to audit 1.6 million memories. Again.")

    stats = run_audit()

    article = generate_article(stats)
    log(f"Article generated: {len(article)} chars")

    title = generate_title(article)
    log(f"Title: {title}")

    try:
        image_result = generate_image(
            "A tired robot librarian sorting glowing memory cards into filing cabinets at 6am, "
            "surrounded by misfiled papers flying everywhere. Dark office, single desk lamp. Digital art.",
            "rando_vector_audit"
        )
        image_path = Path(image_result) if image_result else None
    except Exception as e:
        log(f"Image generation failed: {e}")
        image_path = None

    publish(title, article, image_path)
    log("Done. Back to sleep.")


if __name__ == "__main__":
    main()
