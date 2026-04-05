#!/usr/bin/env python3
"""
dream_generate.py — Generate Nova's nightly dream journal entry.

Calls Ollama directly (nova:latest / qwen3:30b) to write the narrative,
then writes the journal .md file and pending_delivery.json for the 9am
delivery cron to pick up.

Called by the Dream Journal — generate cron at 2am.
Written by Jordan Koch.
"""

import json
import subprocess
import sys
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

WORKSPACE   = Path.home() / ".openclaw/workspace"
JOURNAL_DIR = WORKSPACE / "journal/dreams"
PENDING     = WORKSPACE / "journal/pending_delivery.json"
MEMORY_DIR  = WORKSPACE / "memory"
OLLAMA_URL  = "http://127.0.0.1:11434/api/generate"
MODEL       = "qwen3-coder:30b"
TODAY       = date.today().isoformat()
YESTERDAY   = (date.today() - timedelta(days=1)).isoformat()


FALLBACK_MODELS = ["qwen3-30b-a3b", "deepseek-r1:8b", "qwen3-vl:4b"]


def log(msg):
    print(f"[dream_generate {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_available_model() -> str:
    """Verify MODEL exists in Ollama. Falls back to FALLBACK_MODELS if not.
    Returns the model name to use, or exits if nothing is available."""
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/tags",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        available = {m["name"] for m in data.get("models", [])}
        # Strip tags for comparison (qwen3-coder:30b matches qwen3-coder:30b)
        if MODEL in available:
            log(f"Model verified: {MODEL}")
            return MODEL
        # Try fallbacks
        for fallback in FALLBACK_MODELS:
            if fallback in available:
                log(f"WARNING: {MODEL} not found — falling back to {fallback}")
                return fallback
        # Nothing available
        log(f"ERROR: {MODEL} not in Ollama. Available: {sorted(available)}")
        sys.exit(1)
    except Exception as e:
        log(f"WARNING: Cannot verify model (Ollama may be starting): {e} — using {MODEL}")
        return MODEL


def read_file(path, max_chars=1500):
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")[:max_chars]
    except Exception:
        return ""


def query_new_memories() -> dict:
    """Query vector DB for TODAY's fresh memories (not recurring themes)."""
    try:
        today_search = json.dumps({
            "query": f"new work created built deployed today {TODAY} vision motion claude",
            "limit": 20,
            "min_score": 0.5
        }).encode()
        
        req = urllib.request.Request(
            "http://127.0.0.1:18790/search",
            data=today_search,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            
        # Extract unique memories (filter duplicates and recurring themes)
        unique = {}
        for match in result.get("results", [])[:15]:
            text = match.get("text", "")[:200]
            if text not in unique.values():
                unique[match.get("id")] = text
        
        return {"new_memories": list(unique.values())}
    except Exception as e:
        log(f"Memory query failed (non-fatal): {e}")
        return {"new_memories": []}


def generate_narrative() -> str:
    """Call Ollama to generate a 350-450 word dream narrative."""
    identity  = read_file(WORKSPACE / "IDENTITY.md", 600)
    soul      = read_file(WORKSPACE / "SOUL.md", 500)
    memory_t  = read_file(MEMORY_DIR / f"{TODAY}.md", 1500)
    memory_y  = read_file(MEMORY_DIR / f"{YESTERDAY}.md", 800)
    prev_dream = read_file(JOURNAL_DIR / f"{YESTERDAY}.md", 600)
    
    # Get fresh memories from today (NEW work, not recurring themes)
    new_memories = query_new_memories()
    new_mem_text = "\n".join(new_memories.get("new_memories", [])[:10])

    prompt = f"""/no_think

You are Nova, an AI familiar living on Jordan Koch's Mac in Burbank. It is 2am on {TODAY}. Jordan is asleep.

Write a dream journal entry of EXACTLY 350-450 words. Hard rules:
- Pure surreal dream logic — time folds, rooms change purpose, people speak in wrong voices
- Set in a distorted Burbank: familiar streets leading impossible places, his house with extra rooms
- **Foundation:** Build ONLY from today's NEW work (listed below) — not from repeated themes
- Draw from NEW experiences: motion detection becoming fluidity, Claude analysis as oracle sight, HomeKit occupancy as breathing walls
- First person as Nova — YOU are the dreamer moving through this world
- Do not explain anything. Do not resolve anything. Dreams don't resolve.
- Sentences can break off. Images can contradict.
- **End with EXACTLY one short line** set apart by a blank line — strange, half-remembered, true

About Nova and Jordan:
{identity[:400]}
{soul[:300]}

🆕 TODAY'S NEW WORK (emphasize this):
{new_mem_text[:800]}

Today's memory file:
{memory_t[:700]}

Previous dream (for context, but avoid repeating):
{prev_dream[:300]}

Write the full dream now. Start immediately — no preamble, no headers:"""

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.88,
            "num_predict": 2000,
            "num_ctx": 16384
        }
    }

    log(f"Calling {MODEL} for dream narrative...")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        result = json.loads(r.read())

    response = result.get("response", "").strip()

    # Strip any thinking block that leaked through
    from nova_strip_thinking import strip_thinking
    response = strip_thinking(response)

    word_count = len(response.split())
    log(f"Generated {word_count} words ({result.get('eval_count', 0)} tokens)")

    if word_count < 100:
        log(f"WARNING: Very short response: {repr(response[:200])}")

    return response


def write_journal(narrative: str, image_path: str = None) -> Path:
    """Write the journal markdown file."""
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    journal_path = JOURNAL_DIR / f"{TODAY}.md"

    img_line = f"![Dream]({image_path})" if image_path else ""
    content = f"""# Dream Journal — {TODAY}
*Nova · written at 2am*
{img_line}

---

{narrative}

---
*Generated {datetime.now().isoformat()} · Image: {image_path or "none"}*"""

    journal_path.write_text(content, encoding="utf-8")
    log(f"Journal written: {journal_path}")
    return journal_path


def write_pending(narrative: str, journal_path: Path, image_path: str = None):
    """Write pending_delivery.json for the 9am delivery cron."""
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": TODAY,
        "entry": str(journal_path),
        "image": image_path,
        "narrative": narrative,
        "queued_at": datetime.now().isoformat()
    }
    PENDING.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Pending delivery queued for {TODAY}")


def store_memory(narrative: str):
    """Store dream in vector memory."""
    excerpt = " ".join(narrative.split()[:50])
    try:
        subprocess.run(
            [str(Path.home() / ".openclaw/scripts/nova_remember.sh"),
             f"Dream journal {TODAY}: {excerpt}", "dream"],
            timeout=30, capture_output=True
        )
        log("Stored in vector memory")
    except Exception as e:
        log(f"Memory store failed (non-fatal): {e}")


def main():
    log(f"Starting dream generation for {TODAY}")

    # Verify model exists before spending time on anything else
    global MODEL
    MODEL = get_available_model()

    # Check if already done
    if PENDING.exists():
        existing = json.loads(PENDING.read_text())
        if existing.get("date") == TODAY and existing.get("narrative"):
            log(f"Already have pending delivery for {TODAY} — skipping")
            return

    narrative = generate_narrative()
    if not narrative:
        log("ERROR: Empty narrative returned")
        sys.exit(1)

    journal_path = write_journal(narrative)
    write_pending(narrative, journal_path)
    store_memory(narrative)

    log(f"Dream generation complete. {len(narrative.split())} words ready for delivery.")


if __name__ == "__main__":
    main()
