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
from nova_notify import notify

# ── Config ────────────────────────────────────────────────────────────────────

HUGO_ROOT = (Path.home() / "nova-journal")
CONTENT_DIR = HUGO_ROOT / "content" / "operations"
IMAGES_DIR = HUGO_ROOT / "static" / "images" / "operations"
# Classification reads RAW memory text from arbitrary vectors (may be private),
# so it runs on LOCAL Ollama ONLY — raw memory samples never reach the cloud.
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "qwen3-coder:30b"
MEMORY_URL = "http://192.168.1.6:18790"
SAMPLE_PER_VECTOR = 100
MAX_VECTORS_PER_RUN = 999
DB_DSN = "host=localhost dbname=nova_ops user=kochj"


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[vector_audit {ts}] {msg}", flush=True)


def call_llm(system: str, user: str, max_tokens: int = 4000) -> str:
    """Classify/summarize on LOCAL Ollama ONLY. Raw memory text (which may be
    private) never leaves the box. Returns text, or '' on failure (callers
    already treat empty/garbage output as 'no verdicts'). Matches the local-call
    idiom in nova_inbox_claude.py."""
    import urllib.request
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": f"/no_think\n\n{system}\n\n{user}",
        "stream": False,
        "think": False,
        "options": {"temperature": 0.3, "num_predict": max_tokens},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
        text = (data.get("response") or "").strip()
        if "</think>" in text:
            text = text.split("</think>", 1)[-1].strip()
        return text
    except Exception as e:
        log(f"Local LLM error: {e} — raw memory samples will NOT be sent to cloud")
        return ""


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
- Be conservative — only move obvious misfiles
- Output ONLY valid JSON array

Output format:
[
  {"id": "123", "verdict": "correct"},
  {"id": "456", "verdict": "move", "suggested_vector": "automotive", "reason": "engine diagnostics, not cooking"}
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

def quality_check_batch(memories: list[dict]) -> dict:
    """Check sampled memories for quality issues (not classification — content quality)."""
    trash = {"repetitive": 0, "near_empty": 0, "garbled": 0, "low_signal": 0, "examples": []}

    for m in memories:
        text = m.get("text", "")

        # Near-empty (< 30 chars of real content)
        if len(text.strip()) < 30:
            trash["near_empty"] += 1
            trash["examples"].append({"id": m["id"], "issue": "near_empty", "preview": text[:60]})
            continue

        # Repetitive (same phrase repeated)
        words = text.split()
        if len(words) > 10:
            unique_ratio = len(set(words)) / len(words)
            if unique_ratio < 0.3:
                trash["repetitive"] += 1
                trash["examples"].append({"id": m["id"], "issue": "repetitive", "preview": text[:60]})
                continue

        # Garbled (high ratio of non-ascii, control chars, or HTML tags)
        non_alpha = sum(1 for c in text if not c.isalnum() and c not in ' .,!?;:\'"()-\n')
        if len(text) > 0 and non_alpha / len(text) > 0.4:
            trash["garbled"] += 1
            trash["examples"].append({"id": m["id"], "issue": "garbled", "preview": text[:60]})
            continue

        # Low-signal (transcription artifacts: just music/applause/unintelligible)
        low_signal_markers = ["[music]", "[applause]", "[unintelligible]", "[silence]",
                              "um ", "uh ", "you know", "like like like"]
        marker_count = sum(text.lower().count(m) for m in low_signal_markers)
        if marker_count > 5 and len(text) < 200:
            trash["low_signal"] += 1
            trash["examples"].append({"id": m["id"], "issue": "low_signal", "preview": text[:60]})

    trash["total_issues"] = trash["repetitive"] + trash["near_empty"] + trash["garbled"] + trash["low_signal"]
    return trash


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
    audited_count = 0          # memories the LLM actually returned a verdict for
    quality_checked_count = 0  # memories actually pulled + quality-scanned (LLM-independent)
    correct_count = 0
    all_quality_issues = {"repetitive": 0, "near_empty": 0, "garbled": 0, "low_signal": 0,
                          "total_issues": 0, "examples": [], "worst_vectors": []}

    for vector_name, vector_count in to_audit:
        memories = sample_memories(vector_name, SAMPLE_PER_VECTOR)
        if not memories:
            log(f"  '{vector_name}' returned 0 sampled rows — skipping")
            continue

        quality_checked_count += len(memories)
        log(f"  Auditing '{vector_name}' ({vector_count:,} memories, sampling {len(memories)})...")

        # Quality check (content quality — is this garbage?)
        quality = quality_check_batch(memories)
        if quality["total_issues"] > 0:
            log(f"    QUALITY: {quality['total_issues']} issues "
                f"(repetitive={quality['repetitive']}, empty={quality['near_empty']}, "
                f"garbled={quality['garbled']}, low_signal={quality['low_signal']})")
            all_quality_issues["repetitive"] += quality["repetitive"]
            all_quality_issues["near_empty"] += quality["near_empty"]
            all_quality_issues["garbled"] += quality["garbled"]
            all_quality_issues["low_signal"] += quality["low_signal"]
            all_quality_issues["total_issues"] += quality["total_issues"]
            all_quality_issues["examples"].extend(quality["examples"][:3])
            if quality["total_issues"] >= 5 and len(memories) > 0:
                issue_pct = round(quality["total_issues"] / len(memories) * 100, 1)
                all_quality_issues["worst_vectors"].append(
                    {"vector": vector_name, "issues": quality["total_issues"],
                     "sampled": len(memories), "issue_pct": issue_pct})

        # Classification check (is this in the right vector?)
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

    # Sort worst vectors by issue percentage
    all_quality_issues["worst_vectors"].sort(key=lambda x: x["issue_pct"], reverse=True)

    # Quality % must be computed against the rows we actually QUALITY-CHECKED, not
    # against the LLM classification count (audited_count). If the LLM errors out or
    # returns nothing, audited_count stays 0 while we may still have scanned hundreds
    # of rows — using it as the denominator produced a bogus report ("0 sampled,
    # 100% clean") and previously risked a divide-by-zero before the max(...,1) band-aid.
    if quality_checked_count > 0:
        quality_pct = round(all_quality_issues["total_issues"] / quality_checked_count * 100, 1)
        accuracy_pct = round((correct_count / audited_count) * 100, 1) if audited_count > 0 else None
    else:
        quality_pct = 0.0
        accuracy_pct = None
        log("No rows sampled — quality report unavailable (check memory DB / source filters)")

    stats = {
        "vectors_audited": len(to_audit),
        "memories_sampled": quality_checked_count,   # rows actually pulled + scanned
        "memories_classified": audited_count,        # rows the LLM returned a verdict for
        "correct": correct_count,
        "moved": len(moves),
        "moves": moves,
        "accuracy_pct": accuracy_pct,
        "total_vectors": len(all_vectors),
        "total_memories": sum(c for _, c in all_vectors),
        # Quality stats (the REAL health indicator)
        "quality": all_quality_issues,
        "quality_issue_pct": quality_pct,
        "quality_clean_pct": round(100 - quality_pct, 1) if quality_checked_count > 0 else None,
    }

    acc_str = f"{accuracy_pct}%" if accuracy_pct is not None else "n/a (no LLM verdicts)"
    log(f"Audit complete: {quality_checked_count} sampled & quality-checked "
        f"({audited_count} LLM-classified), {len(moves)} moved, "
        f"{acc_str} correctly filed, "
        f"{quality_pct}% quality issues found")

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
            f"Vector audit: {quality_checked_count} memories checked, {len(moves)} moved, {acc_str} accuracy",
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

    from nova_voice import system_prompt, CONTEXT_JOURNAL_VECTOR_AUDIT
    system = system_prompt(CONTEXT_JOURNAL_VECTOR_AUDIT + """
ADDITIONAL RULES:
- You check TWO things: CLASSIFICATION (right vector?) and QUALITY (worth keeping?)
- Classification accuracy can be 100% and quality can STILL be terrible. A perfectly-filed pile of garbage is still garbage.
- Keep it 600-1000 words
- Open with a one-liner about the 6am shift
- Report BOTH classification accuracy AND quality findings
- If quality issues are high (>5%): alarm bells, dramatic complaint about memory rot
- Give specific examples of the worst memories found
- Pick 2-3 funniest garbage memories to roast
- End with a one-liner about existential memory hygiene
- Do NOT include a title""")

    quality = stats.get("quality", {})
    quality_block = ""
    if quality.get("total_issues", 0) > 0:
        quality_block = f"""
QUALITY ISSUES FOUND:
- Repetitive (same words repeated): {quality.get('repetitive', 0)}
- Near-empty (< 30 chars): {quality.get('near_empty', 0)}
- Garbled (non-text junk): {quality.get('garbled', 0)}
- Low-signal (transcription noise): {quality.get('low_signal', 0)}
- TOTAL: {quality['total_issues']} issues in {stats['memories_sampled']} sampled = {stats.get('quality_issue_pct', 0)}% garbage rate

Worst vectors:
{json.dumps(quality.get('worst_vectors', [])[:5], indent=2)}

Example garbage memories:
{json.dumps(quality.get('examples', [])[:8], indent=2)}
"""
    else:
        quality_block = "\nQUALITY: No issues found in this sample. (Suspicious.)\n"

    user = f"""Today's audit results:

CLASSIFICATION (is it in the right vector?):
- Vectors audited: {stats['vectors_audited']} of {stats['total_vectors']}
- Memories sampled: {stats['memories_sampled']}
- Correctly filed: {stats['correct']} ({stats['accuracy_pct'] if stats['accuracy_pct'] is not None else 'n/a'}%)
- Misfiled and moved: {stats['moved']}
- Total memory count: {stats['total_memories']:,}

Moves:
{moves_block if moves_block else "(None today — all correctly classified)"}
{quality_block}

Write a filing audit column that covers BOTH classification and quality. Be honest about the garbage."""

    return call_llm(system, user, max_tokens=8000)


def generate_title(article_preview: str) -> str:
    system = "Generate a single funny title for a 'memory filing audit' column written by a sarcastic AI librarian. Max 15 words. Output ONLY the title."
    user = f"Based on this preview, generate a title:\n\n{article_preview[:800]}"
    title = call_llm(system, user, max_tokens=50)
    return title.strip().strip('"').strip("'").replace('"', '')


def publish(title: str, body: str, image_path: Path | None, stats: dict | None = None):
    date = time.strftime("%Y-%m-%d")
    timestamp = time.strftime("%Y-%m-%dT06:00:00-07:00")
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    hugo_image = ""
    if image_path and image_path.exists():
        img_filename = f"{date}-{slug}.webp"
        img_dest = IMAGES_DIR / img_filename
        try:
            subprocess.run(
                ["cwebp", "-q", "82", "-resize", "1200", "0", str(image_path), "-o", str(img_dest)],
                capture_output=True, timeout=30
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            shutil.copy2(image_path, img_dest)
        hugo_image = f"/images/operations/{img_filename}"

    front_matter = f"""---
title: "{title.replace('"', '')}"
date: {timestamp}
draft: false
categories: ["operations"]
tags: ["vectors", "audit", "filing", "librarian", "maintenance"]
description: "Nova's morning vector audit — finding and fixing misfiled memories since 6am."
"""
    if hugo_image:
        front_matter += f"""cover:
  image: "{hugo_image}"
  alt: "The morning vector audit"
  relative: false
"""
    front_matter += "---\n\n"

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

    moves_count = len(stats.get('moves', [])) if stats else 0
    notify(
        "Vector Audit posted",
        body=(
            f"{title}\n"
            f"Moved {moves_count} misfiled memories\n"
            f"https://nova.digitalnoise.net/operations/{date}-{slug}/"
        ),
        level="info",
        category="memory_ingest",
        dedup_key="vector-audit",
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

    publish(title, article, image_path, stats)
    log("Done. Back to sleep.")


if __name__ == "__main__":
    main()
