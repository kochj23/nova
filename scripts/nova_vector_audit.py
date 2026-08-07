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
from collections import Counter
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
# Explicit inference host (.6's Ollama), NOT 127.0.0.1 — this job is scheduled on nova-core
# (.2) post-migration, which has no local Ollama. 127.0.0.1 there returned empty every run.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://192.168.1.6:11434/api/generate")
OLLAMA_MODEL = "qwen3-coder:30b"
MEMORY_URL = "http://memory-server.digitalnoise.net:18790"
SAMPLE_PER_VECTOR = 100
MAX_VECTORS_PER_RUN = 999
DB_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# Video / spoken-word vectors whose transcripts naturally repeat words (dialogue, chants,
# sports commentary, subtitles). The loose low-unique-ratio "repetitive" signal over-flags
# them (a film can be 59% "repetitive" and be perfectly fine), so that signal is suppressed
# for these — true degeneracy (one token dominating, or almost no distinct words) is STILL caught.
TRANSCRIPT_VECTORS = frozenset({
    "sci_fi", "war_film", "crime_drama", "action", "drama", "comedy", "documentary",
    "game_show", "mystery", "horror", "television", "film_criticism", "blockbuster_films",
    "sports", "personal_videos", "spalding_gray", "livetv_news", "livetv_dream_fuel",
})


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
    # -h pinned to the real primary: with no host, this hit whatever local default the
    # running node had -> after the 2026-07-17 PG move to nova-core it returned 0 rows on
    # some nodes, and the audit then hallucinated a whole "zero vectors" article from nothing.
    # (A bare psql subprocess, so the 2026-07-24 host= DSN sweep didn't touch it.)
    r = subprocess.run(
        ["psql", "-h", "pg-primary.digitalnoise.net", "-U", "kochj", "-d", "nova_memories", "-tA", "-c", sql],
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

def quality_check_batch(memories: list[dict], source: str = None) -> dict:
    """Check sampled memories for quality issues (not classification — content quality).

    source: the vector these memories came from — lets the repetitive check suppress its
    loose signal on TRANSCRIPT_VECTORS (films/sports naturally repeat) while still catching
    true degeneracy everywhere."""
    trash = {"repetitive": 0, "near_empty": 0, "garbled": 0, "low_signal": 0, "examples": []}

    for m in memories:
        text = m.get("text", "")

        # Near-empty (< 30 chars of real content)
        if len(text.strip()) < 30:
            trash["near_empty"] += 1
            trash["examples"].append({"id": m["id"], "issue": "near_empty", "preview": text[:60]})
            continue

        # Repetitive — TRUE degeneracy (one token dominating >50%, or fewer than 5 distinct
        # real words) is always junk (a "smart_detect ×20" loop or a number/symbol dump). The
        # looser low-unique-ratio signal fires naturally on transcript vectors (dialogue,
        # chants, sports commentary), so it's only applied OUTSIDE TRANSCRIPT_VECTORS — otherwise
        # the audit cries wolf on every film.
        words = text.split()
        if len(words) > 10:
            lw = [w.lower() for w in words]
            unique_ratio = len(set(lw)) / len(lw)
            top_freq = Counter(lw).most_common(1)[0][1] / len(lw)
            distinct_real = len(set(re.findall(r"[a-z]{3,}", text.lower())))
            truly_degenerate = top_freq > 0.5 or distinct_real < 5
            if truly_degenerate or (unique_ratio < 0.3 and source not in TRANSCRIPT_VECTORS):
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
    total_mem = sum(c for _, c in all_vectors)
    log(f"Total vectors: {len(all_vectors)}, total memories: {total_mem:,}")

    # Empty-audit guard: if the DB query came back with (almost) nothing, the audit has no
    # real data — DON'T fabricate an article about it (that's how "Zero Vectors, Infinite
    # Regrets" got published with invented example memories). A healthy store has 1.7M+.
    if total_mem < 1000:
        log(f"ABORT: only {total_mem} memories visible — DB unreachable/empty, refusing to "
            f"publish a hallucinated audit. Check pg-primary connectivity.")
        return {"aborted": True, "reason": f"only {total_mem} memories visible"}

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
        quality = quality_check_batch(memories, source=vector_name)
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

# The audit publishes to a PUBLIC blog. The LLM used to be handed the real stats AND told to
# "dramatize / alarm bells", so it invented its OWN false statistics (rhyming counts like
# 191/19,191/1,919,003, and flat falsehoods like "LiveJournal is 100% empty" when it holds
# 5,823 real entries). Fix: the LLM writes VOICE ONLY and is forbidden from stating any
# aggregate number; the true measured numbers are appended deterministically as a ledger.
_PCT_RE = re.compile(r"\b\d{1,3}(?:\.\d+)?\s?%")
# a big count (1,919,003 comma-grouped, or a bare 4+ digit run like 19191) — but NOT a plain
# 4-digit year (1900-2099), which is legitimate prose.
_COUNT_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b(?!(?:19|20)\d{2}\b)\d{4,}\b")


def _has_invented_stats(prose: str) -> bool:
    """True if the prose states aggregate numbers it was told not to (percentages or big
    counts). The ledger owns all real numbers; anything numeric in the prose is suspect."""
    return bool(_PCT_RE.search(prose) or _COUNT_RE.search(prose))


def _scrub_invented_stats(prose: str) -> str:
    """Last-resort safety net: neutralize any percentage or large count the LLM slipped in so
    a fabricated statistic can never reach the public post. A slightly vaguer sentence beats a
    confident false number."""
    prose = _PCT_RE.sub("a share", prose)
    prose = _COUNT_RE.sub("plenty", prose)
    return prose


def _facts_ledger(stats: dict) -> str:
    """Deterministic, code-authored block of the REAL measured numbers. This — not the LLM —
    is the single source of every statistic in the article."""
    q = stats.get("quality", {})
    acc = stats.get("accuracy_pct")
    lines = [
        "\n\n---\n\n### The actual numbers (measured, not editorialized)\n",
        f"- **Memories in the store:** {stats['total_memories']:,} across {stats['total_vectors']} vectors",
        f"- **Audited this run:** {stats['vectors_audited']} vectors, {stats['memories_sampled']} memories sampled and scanned",
    ]
    if acc is not None:
        lines.append(f"- **Correctly filed:** {stats['correct']} of {stats['memories_classified']} classified ({acc}%)")
    lines.append(f"- **Misfiled and moved:** {stats['moved']}")
    ti = q.get("total_issues", 0)
    qp = stats.get("quality_issue_pct", 0)
    lines.append(f"- **Quality issues in the scanned sample:** {ti} ({qp}% of scanned) — "
                 f"repetitive {q.get('repetitive',0)}, near-empty {q.get('near_empty',0)}, "
                 f"garbled {q.get('garbled',0)}, low-signal {q.get('low_signal',0)}")
    lines.append("\n*These figures are computed directly from the database; the commentary above adds no numbers of its own.*")
    return "\n".join(lines)


def generate_article(stats: dict) -> str:
    """Write the sarcastic filing-clerk article — VOICE from the LLM, NUMBERS from code."""

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
- CRITICAL — NUMBERS: You must NOT state ANY statistic, count, total, or percentage. Not the
  memory count, not the number moved, not a garbage rate, nothing numeric. A factual ledger
  with the REAL figures is appended automatically after your text. Inventing numbers (you have
  done this — a false "100%% empty" about a vector that was full) is the one unforgivable sin
  here. Describe findings qualitatively ("a stack of misfiles", "mostly clean", "one vector was
  a disaster") and let the ledger carry every number.
- You MAY quote the specific example memories provided (those are real) and roast them.
- Give specific examples of the worst memories found; pick 2-3 funniest to roast
- End with a one-liner about existential memory hygiene
- Do NOT include a title""")

    quality = stats.get("quality", {})
    # Only REAL, per-row example memories go to the LLM (safe to quote). Aggregate counts are
    # deliberately withheld so it can't parrot or mutate them — the ledger owns those.
    quality_block = ""
    if quality.get("total_issues", 0) > 0:
        quality_block = f"""
QUALITY: issues were found (exact figures are in the auto-appended ledger — do not restate them).
Categories seen: repetitive, near-empty, garbled, low-signal.

Worst vectors (names only, for color):
{json.dumps([w.get('vector') for w in quality.get('worst_vectors', [])[:5]])}

Real example memories you may quote and roast:
{json.dumps(quality.get('examples', [])[:8], indent=2)}
"""
    else:
        quality_block = "\nQUALITY: nothing flagged in this sample. (Suspicious.)\n"

    user = f"""Today's audit (qualitative brief — NO numbers in your prose, the ledger handles those):

CLASSIFICATION: {'some misfiles were found and moved' if stats['moved'] else 'everything sampled was correctly filed'}.
Real moves you may reference by example:
{moves_block if moves_block else "(None today — all correctly classified)"}
{quality_block}

Write the filing-audit column: voice, attitude, roast the real example memories above. State NO
statistics — describe qualitatively and let the appended ledger carry every figure."""

    prose = call_llm(system, user, max_tokens=8000)
    if not prose.strip():
        return ""  # caller's empty-output guard handles this
    # Enforce the no-numbers rule: one stricter retry, then scrub as a hard safety net.
    if _has_invented_stats(prose):
        log("Article prose contained numbers (forbidden) — regenerating once, stricter")
        retry = call_llm(system + "\n\nYOU STATED NUMBERS. Rewrite with ZERO digits in the prose.",
                         user, max_tokens=8000)
        if retry.strip() and not _has_invented_stats(retry):
            prose = retry
        else:
            log("Still numeric after retry — scrubbing invented stats from prose")
            prose = _scrub_invented_stats(prose)
    return prose.rstrip() + _facts_ledger(stats)


def generate_title(article_preview: str) -> str:
    system = "Generate a single funny title for a 'memory filing audit' column written by a sarcastic AI librarian. Max 15 words. Output ONLY the title."
    user = f"Based on this preview, generate a title:\n\n{article_preview[:800]}"
    title = call_llm(system, user, max_tokens=50)
    return title.strip().strip('"').strip("'").replace('"', '').replace('*', '').replace('#', '').strip()


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
        # Rebase onto origin BEFORE pushing so a diverged clone can't silently strand commits.
        pull = subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"],
                              cwd=HUGO_ROOT, capture_output=True, text=True, timeout=180)
        if pull.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
            log(f"Push ABORTED — pull --rebase failed (diverged/conflict): {pull.stderr[:200]}")
        else:
            p = subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30)
            log("Pushed to GitHub" if p.returncode == 0
                else f"Push FAILED (commit NOT on origin): {p.stderr[:200]}")
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

    # Empty-audit guard (DB unreachable/empty -> no real data): skip, don't hallucinate.
    if stats.get("aborted"):
        log(f"ABORT: {stats.get('reason')} — not publishing.")
        try:
            import nova_config
            nova_config.post_both(f":warning: Vector-audit skipped — {stats.get('reason')}. "
                                  "Nothing published (would have been fabricated).",
                                  slack_channel=getattr(nova_config, "SLACK_BB", None))
        except Exception:
            pass
        return 1

    article = generate_article(stats)
    log(f"Article generated: {len(article)} chars")

    title = generate_title(article)
    log(f"Title: {title}")

    # Guard: call_llm returns "" when the inference backend is unreachable/timing out (as it
    # did at 06:00 on 2026-07-15). Never publish an empty-title/empty-body post — that yields a
    # broken /operations/<date>-/ article. Abort loudly instead so the failure is visible.
    if len(article.strip()) < 200 or not title.strip():
        log(f"ABORT: empty/short LLM output (article={len(article.strip())}c, "
            f"title={'EMPTY' if not title.strip() else 'ok'}) — backend likely down; not publishing.")
        try:
            import nova_config
            nova_config.post_both(":warning: Vector-audit article skipped — LLM backend returned "
                                  "empty output (blank article/title). Nothing published.",
                                  slack_channel=nova_config.SLACK_BB)
        except Exception:
            pass
        return 1

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
