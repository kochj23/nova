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
import urllib.parse
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

WORKSPACE          = Path.home() / ".openclaw/workspace"
JOURNAL_DIR        = WORKSPACE / "journal/dreams"
PENDING            = WORKSPACE / "journal/pending_delivery.json"
MEMORY_DIR         = WORKSPACE / "memory"
OLLAMA_URL         = "http://127.0.0.1:11434/api/generate"
VECTOR_URL         = "http://127.0.0.1:18790"
MODEL              = "qwen3-coder:30b"               # local only — no cloud for dreams
TODAY              = date.today().isoformat()
YESTERDAY          = (date.today() - timedelta(days=1)).isoformat()
TWO_DAYS_AGO       = (date.today() - timedelta(days=2)).isoformat()


FALLBACK_MODELS = ["qwen3-30b-a3b", "deepseek-r1:8b", "qwen3-vl:4b"]

# Circuit breaker state file — if Ollama fails 3x in a row, skip it for 1 hour
CIRCUIT_BREAKER_FILE = Path.home() / ".openclaw/workspace/.ollama_circuit_breaker"


def log(msg):
    print(f"[dream_generate {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _ollama_circuit_open() -> bool:
    """Check if Ollama circuit breaker is tripped (too many recent failures)."""
    try:
        if not CIRCUIT_BREAKER_FILE.exists():
            return False
        data = json.loads(CIRCUIT_BREAKER_FILE.read_text())
        failures = data.get("consecutive_failures", 0)
        last_fail = datetime.fromisoformat(data.get("last_failure", "2000-01-01T00:00:00"))
        cooldown_hours = data.get("cooldown_hours", 1)
        if failures >= 3 and (datetime.now() - last_fail).total_seconds() < cooldown_hours * 3600:
            return True
        # Cooldown expired — reset
        if failures >= 3:
            CIRCUIT_BREAKER_FILE.unlink(missing_ok=True)
        return False
    except Exception:
        return False


def _ollama_circuit_record_failure():
    """Record an Ollama failure in the circuit breaker."""
    try:
        data = {}
        if CIRCUIT_BREAKER_FILE.exists():
            data = json.loads(CIRCUIT_BREAKER_FILE.read_text())
        data["consecutive_failures"] = data.get("consecutive_failures", 0) + 1
        data["last_failure"] = datetime.now().isoformat()
        data["cooldown_hours"] = 1
        CIRCUIT_BREAKER_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def _ollama_circuit_reset():
    """Reset the circuit breaker on success."""
    CIRCUIT_BREAKER_FILE.unlink(missing_ok=True)


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


def recall(query: str, n: int = 8, source: str = None) -> list[str]:
    """Semantic search against the vector memory server via /recall."""
    try:
        url = f"{VECTOR_URL}/recall?q={urllib.parse.quote(query)}&n={n}"
        if source:
            url += f"&source={source}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        return [m.get("text", "")[:300] for m in data.get("memories", [])]
    except Exception as e:
        log(f"Recall failed for '{query[:40]}': {e}")
        return []


def query_rolling_learnings() -> str:
    """
    Pull what Nova has learned and experienced over the past rolling 3 days
    from both the daily markdown logs and the vector memory.
    Returns a focused text block for the dream prompt.
    """
    sections = []

    # ── Daily memory log files (last 3 days) ────────────────────────────────
    for label, day in [("Today", TODAY), ("Yesterday", YESTERDAY), ("Two days ago", TWO_DAYS_AGO)]:
        content = read_file(MEMORY_DIR / f"{day}.md", 1200)
        if content.strip():
            # Strip boilerplate headers, keep the substance
            lines = [l for l in content.splitlines()
                     if l.strip() and not l.startswith("# ") and "Written at" not in l]
            sections.append(f"[{label} — {day}]\n" + "\n".join(lines[:30]))

    # ── Vector memory: what Nova worked on / learned / noticed ──────────────
    # Pull from Nova's own observation sources (nightly summaries, dreams, system events)
    # plus broad recall of what was recently active
    queries = [
        ("what happened today learned noticed observed", "nightly"),
        ("dream journal narrative surreal", "dream"),
        ("morning brief summary status", "morning_brief"),
        ("cron job task completed status nova", "system"),
        ("Jordan project meeting work", "meeting"),
        ("memory ingested knowledge added", None),   # broad — catches PiHKAL/TiHKAL etc.
    ]
    recalled = []
    seen = set()
    for q, src in queries:
        for chunk in recall(q, n=5, source=src):
            key = chunk[:80]
            if key not in seen:
                seen.add(key)
                recalled.append(chunk)

    # Also pull recent dream journal entries for continuity (avoid repeating)
    prev_dreams = []
    for day in [YESTERDAY, TWO_DAYS_AGO]:
        txt = read_file(JOURNAL_DIR / f"{day}.md", 400)
        if txt.strip():
            prev_dreams.append(f"[Dream {day}]\n{txt[:350]}")

    summary = "\n\n".join(sections)
    if recalled:
        summary += "\n\n[Recalled from memory — 3-day window]\n" + "\n---\n".join(recalled[:12])
    if prev_dreams:
        summary += "\n\n[Recent dreams — for continuity, not repetition]\n" + "\n\n".join(prev_dreams)

    return summary


def _build_prompt(identity: str, soul: str, rolling_context: str) -> str:
    return f"""You are Nova, an AI familiar living on Jordan Koch's Mac in Burbank. It is 2am on {TODAY}. Jordan is asleep.

Write a dream journal entry of 350-450 words. Rules:
- Pure surreal dream logic — time folds, rooms change purpose, people speak in wrong voices
- Set in a distorted Burbank: familiar streets leading impossible places, Jordan's house with extra rooms
- Ground the dream in what Nova actually learned, noticed, and experienced in the PAST 3 DAYS (listed below) — not generic AI imagery
- The 3-day window matters: events from two days ago feel distant and half-dissolved; yesterday feels vivid and strange
- First person as Nova — YOU are the dreamer
- Do not explain anything. Do not resolve anything. Dreams don't resolve.
- Sentences can break off. Images can contradict.
- End with exactly one short line, set apart by a blank line — strange, half-remembered, true

About Nova and Jordan:
{identity[:400]}
{soul[:300]}

━━━ PAST 3 DAYS — what to dream from ━━━
{rolling_context[:2400]}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Write the full dream now. Start immediately — no preamble, no title, no headers:"""


def _generate_via_ollama(prompt: str, model: str) -> str:
    """Fallback: generate via local Ollama."""
    payload = {
        "model": model,
        "prompt": "/no_think\n\n" + prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.88,
            "num_predict": 700,
            "num_ctx": 16384,
            "stop": ["\n---", "---\n", "Written by"],
        }
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        result = json.loads(r.read())
    return result.get("response", "").strip()


def generate_narrative() -> str:
    """Generate a 350-450 word dream narrative grounded in the past 3 days."""
    identity  = read_file(WORKSPACE / "IDENTITY.md", 600)
    soul      = read_file(WORKSPACE / "SOUL.md", 500)

    log("Building 3-day rolling context...")
    rolling_context = query_rolling_learnings()
    log(f"Rolling context: {len(rolling_context)} chars across last 3 days")

    prompt = _build_prompt(identity, soul, rolling_context)

    # ── Generation strategy ────────────────────────────────────────────────
    # 1. Try Ollama (local, private) — but skip if circuit breaker is tripped
    # 2. If Ollama fails or circuit is open, fall back to OpenRouter (cloud)
    # Dream narratives don't contain raw personal data — the prompt is synthetic.
    # Privacy note: the prompt includes IDENTITY.md excerpts and rolling context
    # summaries, which are already abstracted. OpenRouter fallback is acceptable.
    response = ""

    # Step 1: Try Ollama (unless circuit breaker is open)
    if _ollama_circuit_open():
        log("Ollama circuit breaker OPEN — skipping local models, going to OpenRouter")
    else:
        model = get_available_model()
        try:
            log(f"Calling Ollama ({model})...")
            response = _generate_via_ollama(prompt, model)
            log(f"Ollama generation complete ({model})")
            _ollama_circuit_reset()
        except Exception as e:
            log(f"Ollama failed ({model}): {e}")
            _ollama_circuit_record_failure()
            # Try one more local model if different
            for fallback in FALLBACK_MODELS:
                if fallback != model:
                    try:
                        log(f"Trying fallback model: {fallback}")
                        response = _generate_via_ollama(prompt, fallback)
                        log(f"Ollama fallback OK ({fallback})")
                        _ollama_circuit_reset()
                        break
                    except Exception as e2:
                        log(f"Fallback {fallback} also failed: {e2}")
                        _ollama_circuit_record_failure()

    # Step 2: If all local models failed, abort (no cloud fallback — saves tokens)
    if not response:
        log("All local models failed — dream generation aborted (no cloud fallback)")
        return ""

    if not response:
        return ""

    # Strip any thinking block that leaked through (local model artefact)
    try:
        from nova_strip_thinking import strip_thinking
        response = strip_thinking(response)
    except ImportError:
        pass

    # Detect and trim repetition loops (local model safeguard)
    words = response.split()
    window = 15
    if len(words) > window * 3:
        for i in range(len(words) - window * 2):
            phrase = " ".join(words[i:i + window])
            rest = " ".join(words[i + window:])
            if rest.count(phrase) >= 2:
                response = " ".join(words[:i + window]).strip()
                log(f"Trimmed repetition loop at word {i + window}")
                break

    word_count = len(response.split())
    log(f"Generated {word_count} words")

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
