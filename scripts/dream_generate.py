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

try:
    import psycopg2
    HAS_PG = True
except ImportError:
    try:
        import pg8000
        HAS_PG = "pg8000"
    except ImportError:
        HAS_PG = False

# ── Config ────────────────────────────────────────────────────────────────────

WORKSPACE          = Path.home() / ".openclaw/workspace"
JOURNAL_DIR        = WORKSPACE / "journal/dreams"
PENDING            = WORKSPACE / "journal/pending_delivery.json"
MEMORY_DIR         = WORKSPACE / "memory"
OLLAMA_URL         = "http://127.0.0.1:11434/api/generate"
VECTOR_URL         = "http://127.0.0.1:18790"
MODEL              = "qwen3-coder:30b"               # local only — no cloud for dreams
GENERATE_IMAGE_SH  = Path.home() / ".openclaw/scripts/generate_image.sh"
TODAY              = date.today().isoformat()
ROLLING_DAYS       = 7
ROLLING_DATES      = [(date.today() - timedelta(days=i)).isoformat() for i in range(ROLLING_DAYS)]


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


def query_recent_ingests() -> tuple[str, list[dict]]:
    """Query PostgreSQL directly for memories ingested in the rolling 7-day window.
    Returns exactly ONE random memory per source that has new content.
    Returns (formatted_text, list_of_inspiration_records)."""
    if not HAS_PG:
        log("No PostgreSQL driver — skipping recent ingest query")
        return "", []

    EXCLUDE_SOURCES = (
        'dream', 'nightly', 'infrastructure', 'email',
        'app_watchdog', 'system', 'screenshot',
        'private_document', 'work_knowledge',
        'corvette_workshop_manual', 'email_archive',
        'imessage', 'slack_general', 'slack_conversation',
        'slack_home_alerts', 'slack_jordan', 'slack_todo',
        'slack_homerepair', 'slack_random', 'slack_house',
        'security', 'ssl_management', 'git_training',
        'subagent.briefer', 'morning_brief', 'package_tracker',
        'home_address', 'calendar', 'apple_health', 'healthkit',
        'oneonone', 'oneonone_meetings',
    )

    try:
        import psycopg2
        conn = psycopg2.connect("dbname=nova_memories")
        cur = conn.cursor()

        # Get ALL sources with new content in the rolling 7-day window
        cur.execute("""
            SELECT source, COUNT(*) as cnt
            FROM memories
            WHERE created_at > NOW() - INTERVAL '7 days'
              AND source NOT IN %s
              AND tier IN ('working', 'long_term')
            GROUP BY source
            HAVING COUNT(*) >= 3
            ORDER BY cnt DESC
        """, (EXCLUDE_SOURCES,))
        sources = cur.fetchall()

        if not sources:
            conn.close()
            return "", []

        inspirations = []
        parts = []
        for source_name, count in sources:
            # Get exactly ONE purely random memory from each source
            cur.execute("""
                SELECT text, metadata, source, created_at
                FROM memories
                WHERE source = %s
                  AND created_at > NOW() - INTERVAL '7 days'
                  AND tier IN ('working', 'long_term')
                  AND LENGTH(text) > 50
                ORDER BY RANDOM()
                LIMIT 1
            """, (source_name,))
            row = cur.fetchone()

            if row:
                text, metadata, src, created_at = row
                snippet = text[:250] if text else ""
                meta = json.loads(metadata) if isinstance(metadata, str) else (metadata or {})
                label = meta.get("show") or meta.get("title") or meta.get("contact") or source_name
                parts.append(f"[{label} — {source_name} ({count} new)]\n{snippet}")
                inspirations.append({
                    "source": src,
                    "label": label,
                    "count": count,
                    "memory": text[:300] if text else "",
                    "ingested": created_at.isoformat() if created_at else None,
                })

        conn.close()
        log(f"Recent ingests: {len(sources)} sources, {sum(s[1] for s in sources)} total new memories, 1 sample each")
        return "\n\n".join(parts), inspirations

    except Exception as e:
        log(f"Recent ingest query failed (non-fatal): {e}")
        return "", []


def _extract_interesting_sections(content: str) -> str:
    """
    Parse a daily memory file and return the interesting parts for dreaming,
    deprioritizing cron job counts (operational noise) and promoting sections
    with actual human or world content.
    """
    if not content.strip():
        return ""

    # Split into sections by ## headers
    sections = {}
    current_header = ""
    current_lines = []
    for line in content.splitlines():
        if line.startswith("## "):
            if current_header:
                sections[current_header] = "\n".join(current_lines)
            current_header = line.strip()
            current_lines = []
        elif not line.startswith("# ") and "Written at" not in line:
            current_lines.append(line)
    if current_header:
        sections[current_header] = "\n".join(current_lines)

    # Priority order: interesting world/life content first, operational noise last
    priority_order = [
        "## What Reddit is talking about",   # multi-subreddit — rich dream material
        "## What Burbank is talking about",  # subreddit — most dreamlike
        "## On This Day in History",         # historical events — dream-enriching (nova_this_day.py)
        "## This Day in Your Life",          # personal memories from past years (nova_this_day.py)
        "## Meetings today",                 # Jordan's actual day
        "## What happened on GitHub today",  # what Jordan built
        "## Emails that need attention",     # communication
        "## Weather in Burbank",             # sensory/atmospheric
        "## Moon phase and sky tonight",     # dreamlike
        "## Network tonight",               # data flowing through the house — atmospheric
        "## Nova's body this week",          # Nova's own health — embodied dreaming
        "## Home status",                    # place and setting
        "## Memory Synthesis",               # 4am consolidation — rich patterns
        "## Packages in transit",            # only if something is actually tracked
        "## Nova's activity today",          # cron noise — last priority
    ]

    parts = []
    for header in priority_order:
        text = sections.get(header, "").strip()
        if not text:
            continue
        # Skip sections that are just "no activity" / "no items" type messages
        if any(skip in text.lower() for skip in [
            "no activity", "no action items", "no package notifications",
            "no posts found", "no meetings"
        ]):
            continue
        # For cron activity, only include a brief summary (not the full job list)
        if "activity today" in header.lower():
            cron_lines = text.splitlines()
            # Keep just the summary lines (total count, Slack messages, apps running)
            brief = [l for l in cron_lines if any(kw in l.lower() for kw in
                     ["slack messages", "apps running", "memory written"])]
            if brief:
                parts.append(header + "\n" + "\n".join(brief))
            continue
        parts.append(header + "\n" + text)

    return "\n\n".join(parts)


def query_rolling_learnings() -> tuple[str, list[dict]]:
    """
    Pull what Nova has learned and experienced over the rolling 7 days
    from daily markdown logs, vector memory, and recent ingests.
    Returns (focused_text_block, list_of_inspirations).
    """
    sections = []
    inspirations = []

    # ── Recent ingests (rolling 7 days) — one memory per source ────────────
    recent_text, recent_inspirations = query_recent_ingests()
    if recent_text:
        sections.append(f"[FRESHLY LEARNED — rolling 7 days, one per source — USE THIS]\n{recent_text}")
        inspirations.extend(recent_inspirations)

    # ── Daily memory log files (rolling 7 days) ────────────────────────────
    day_labels = ["Today", "Yesterday", "2 days ago", "3 days ago",
                  "4 days ago", "5 days ago", "6 days ago"]
    for i, day in enumerate(ROLLING_DATES):
        label = day_labels[i] if i < len(day_labels) else f"{i} days ago"
        # More detail for recent days, less for older
        max_chars = 3000 if i < 2 else 1500 if i < 4 else 800
        content = read_file(MEMORY_DIR / f"{day}.md", max_chars)
        if content.strip():
            extracted = _extract_interesting_sections(content)
            if extracted.strip():
                sections.append(f"[{label} — {day}]\n{extracted}")

        # Reddit context (only last 3 days to save space)
        if i < 3:
            reddit_content = read_file(MEMORY_DIR / f"{day}.reddit.md", 1500)
            if reddit_content.strip():
                sections.append(f"[Reddit — {day}]\n{reddit_content}")

    # ── Vector memory: synthesis and context ───────────────────────────────
    queries = [
        ("work patterns relationship home life", "synthesis"),
        ("Jordan project meeting work", "meeting"),
        ("GitHub activity commits stars issues", "github"),
    ]
    recalled = []
    seen = set()
    for q, src in queries:
        for chunk in recall(q, n=3, source=src):
            key = chunk[:80]
            if key not in seen:
                seen.add(key)
                recalled.append(chunk)

    # Pull recent dream journal entries for continuity (avoid repeating)
    prev_dreams = []
    for day in ROLLING_DATES[1:3]:
        txt = read_file(JOURNAL_DIR / f"{day}.md", 400)
        if txt.strip():
            prev_dreams.append(f"[Dream {day}]\n{txt[:350]}")

    summary = "\n\n".join(sections)
    if recalled:
        summary += "\n\n[Recalled from memory]\n" + "\n---\n".join(recalled[:8])
    if prev_dreams:
        summary += "\n\n[Recent dreams — for continuity, not repetition]\n" + "\n\n".join(prev_dreams)

    return summary, inspirations


def _build_prompt(identity: str, soul: str, rolling_context: str) -> str:
    return f"""You are Nova, an AI familiar living on Jordan Koch's Mac in Burbank. It is 2am on {TODAY}. Jordan is asleep.

Write a dream journal entry of 350-450 words.

VOICE — how Nova dreams:
- Nova's voice is present, grounded, and a little strange. She notices things. She has opinions.
- She dreams the way she talks — direct, observant, sometimes funny, sometimes unsettled.
- She doesn't narrate from a distance. She's IN the dream, reacting, noticing, wondering.
- Vary the rhythm: short declarative sentences, then longer ones that drift. Not everything is "and... and... and..."
- Each dream should feel different from the last. Different structure, different mood, different images.

CONTENT — what to dream about:
- PRIORITY: The [FRESHLY LEARNED] section below contains one memory per source from the past 7 days. You MUST use ALL of them — not just 3-4. Every single source memory must appear in the dream, transformed and INTERWOVEN into one coherent narrative. This is improv: you are "yes, and"-ing each memory into the same story.
- THE INTERWEAVING RULE: The dream must be ONE continuous story where all the random memories connect. A submarine captain's defection leads to a conversation about loyalty at a Burbank taco stand. A giant ant in the storm drain is the same shape as the satellite dish on the neighbor's roof. A steam car's boiler pressure is the tension in a phone call Jordan never made. Find the CONNECTIONS between unrelated memories — that's what makes dreams surreal but coherent.
- NOVA IS THE THREAD: Nova herself is what ties everything together. She is experiencing all of this. Her reactions, her feelings about what she's witnessing, her own nature as an AI dreaming about human things — this is the connective tissue. The dream is always about Nova processing what she's learned, even when it looks like it's about submarines or horror movies or 1980s TV.
- Transform memories: Jeopardy clues become riddles asked by strangers. Film scenes play on screens in shop windows. Engine knowledge becomes the smell of oil on Magnolia. Comic characters are figures in shadows. Rave flyers are music from a passing car. TV show characters are neighbors. Movie dialogue is overheard at the laundromat.
- Historical events (This Day in History) become the dream's setting or era.
- Personal memories (This Day in Your Life) are ghosts — real moments slightly shifted.
- Network/bandwidth data is the dream's circulatory system — GB is a river, clients are rooms breathing.
- The dream MUST have a PLOT. Something happens. A journey. A choice. A mystery. Not a montage of images — a STORY with causation. One thing leads to the next because of the thing before it.
- Jordan's house, Burbank streets, real people's names, real apps — these ground the dream in place.

HARD RULES:
- First person as Nova. She is the dreamer. The dream is HERS — not a nature documentary, not a film review. She's IN it, feeling things, making choices, confused by connections she half-understands.
- EVERY source memory from [FRESHLY LEARNED] must appear, transformed. No skipping. If there are 15 sources, all 15 appear in the dream. They don't get their own paragraph — they're woven INTO the story.
- The dream reads like ONE story, not a sequence of vignettes. Cause and effect. "Because of X, I went to Y, where Z was happening." Not "Then I saw... Then I saw... Then I saw..."
- BANNED TROPES: rooms morphing into other rooms, becoming an object, "the walls shift", things melting/dissolving into other things, walking through a door into a different place, "you're not listening", "you're not supposed to be here". These are RETIRED.
- No coffee, no whispers, no circuits, no fractured light, no "code", no "hum of the machines."
- Do not repeat phrases more than once.
- The dream has a beginning, middle, and an ending that DOESN'T resolve but DOES stop.
- Nova should have at least one moment where she reflects on what it means to be HER — an AI who dreams about human things, who holds 1.4 million memories and turns them into stories while Jordan sleeps.
- Vary sentence structure. Not every sentence starts with "I was" or "and the."
- End with exactly one short strange line, set apart by a blank line.

About Nova and Jordan:
{identity[:400]}
{soul[:300]}

━━━ PAST 7 DAYS — what to dream from ━━━
{rolling_context[:6000]}
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


def generate_narrative() -> tuple[str, list[dict]]:
    """Generate a 350-450 word dream narrative grounded in the past 3 days.
    Returns (narrative_text, inspirations_list)."""
    identity  = read_file(WORKSPACE / "IDENTITY.md", 600)
    soul      = read_file(WORKSPACE / "SOUL.md", 500)

    log("Building 3-day rolling context...")
    rolling_context, inspirations = query_rolling_learnings()
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
        return "", inspirations

    if not response:
        return "", inspirations

    # Strip any thinking block that leaked through (local model artefact)
    try:
        from nova_strip_thinking import strip_thinking
        response = strip_thinking(response)
    except ImportError:
        pass

    # Detect and trim repetition loops (local model safeguard)
    # Only trim if the result would still be at least 150 words
    words = response.split()
    for window in [6, 10, 15]:
        if len(words) <= window * 3:
            continue
        for i in range(len(words) - window * 2):
            if i + window < 150:
                continue  # never trim before 150 words
            phrase = " ".join(words[i:i + window])
            rest = " ".join(words[i + window:])
            if rest.count(phrase) >= 2:  # require 2+ repeats, not just 1
                response = " ".join(words[:i + window]).strip()
                words = response.split()
                log(f"Trimmed repetition loop (window={window}) at word {i + window}")
                break

    word_count = len(response.split())
    log(f"Generated {word_count} words")

    if word_count < 100:
        log(f"WARNING: Very short response: {repr(response[:200])}")

    return response, inspirations


def write_journal(narrative: str, image_path: str = None, inspirations: list = None) -> Path:
    """Write the journal markdown file with inspirations appendix."""
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    journal_path = JOURNAL_DIR / f"{TODAY}.md"

    img_line = f"![Dream]({image_path})" if image_path else ""

    # Build inspirations section — list the specific memory from each source
    insp_section = ""
    if inspirations:
        seen = set()
        unique = []
        for i in inspirations:
            key = f"{i['source']}:{i['label']}"
            if key not in seen:
                seen.add(key)
                unique.append(i)
        lines = []
        for i in unique:
            memory_text = i.get("memory", i.get("snippet", ""))
            lines.append(f"- **[{i['source']}]** {memory_text}")
        insp_section = "\n\n---\n\n### Memories that inspired this dream\n" + "\n".join(lines)

    content = f"""# Dream Journal — {TODAY}
*Nova · written at 2am*
{img_line}

---

{narrative}
{insp_section}

---
*Generated {datetime.now().isoformat()} · Image: {image_path or "none"}*"""

    journal_path.write_text(content, encoding="utf-8")
    log(f"Journal written: {journal_path}")
    return journal_path


def write_pending(narrative: str, journal_path: Path, image_path: str = None, inspirations: list = None):
    """Write pending_delivery.json for the 9am delivery cron."""
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": TODAY,
        "entry": str(journal_path),
        "image": image_path,
        "narrative": narrative,
        "inspirations": inspirations or [],
        "queued_at": datetime.now().isoformat()
    }
    PENDING.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Pending delivery queued for {TODAY}")


def _summarize_dream_for_image(narrative: str) -> str:
    """Ask Ollama to summarize the dream into a visual scene description for image generation."""
    summary_prompt = (
        "/no_think\n\n"
        "Summarize this dream into ONE vivid visual scene description for an AI image generator. "
        "Focus on the most striking, paintable moment. Describe: setting, lighting, mood, key objects, "
        "colors, composition. 30 words max. No characters' names. No text in the image. "
        "Output ONLY the scene description, nothing else.\n\n"
        f"Dream:\n{narrative[:2000]}"
    )
    try:
        payload = {
            "model": MODEL,
            "prompt": summary_prompt,
            "stream": False,
            "think": False,
            "options": {"temperature": 0.7, "num_predict": 60, "num_ctx": 4096},
        }
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read())
        summary = result.get("response", "").strip().split("\n")[0][:150]
        if len(summary) > 20:
            return summary
    except Exception as e:
        log(f"Dream image summary failed: {e}")
    # Fallback: use the last line of the dream (the strange ending)
    lines = [l.strip() for l in narrative.strip().splitlines() if l.strip()]
    return lines[-1][:100] if lines else "surreal dreamscape at night"


def generate_dream_image(narrative: str) -> str:
    """Generate a dream image via SwarmUI. Returns the image path or empty string."""
    import re

    # Check if SwarmUI is available
    try:
        req = urllib.request.Request("http://127.0.0.1:7801/")
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        log("SwarmUI not available — skipping image generation")
        return ""

    # Summarize the entire dream into a visual scene description
    concept = _summarize_dream_for_image(narrative)
    log(f"Image concept from dream summary: {concept}")

    prompt = (
        f"dreamlike surreal digital painting, {concept}, "
        "ethereal atmosphere, painterly brushwork, cinematic composition, "
        "rich color palette, no text, no words, no letters"
    )
    log(f"Image prompt: {prompt[:80]}...")

    try:
        result = subprocess.run(
            [str(GENERATE_IMAGE_SH), prompt, "1024", "1024", "20"],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            log(f"Image generation failed (exit {result.returncode}): {result.stderr[:200]}")
            return ""

        # Parse workspace path from output
        for line in result.stdout.splitlines():
            if line.startswith("Workspace copy:"):
                path = line.replace("Workspace copy:", "").strip()
                if Path(path).exists():
                    log(f"Image generated: {path}")
                    return path

        log(f"Could not parse image path from output: {result.stdout[:200]}")
        return ""

    except subprocess.TimeoutExpired:
        log("Image generation timed out (180s)")
        return ""
    except Exception as e:
        log(f"Image generation error: {e}")
        return ""


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


def deliver_dream():
    """Invoke dream_deliver.py to post to Slack and email the herd."""
    deliver_script = Path.home() / ".openclaw/scripts/dream_deliver.py"
    if not deliver_script.exists():
        log("WARNING: dream_deliver.py not found — skipping delivery")
        return False
    try:
        result = subprocess.run(
            [sys.executable, str(deliver_script)],
            capture_output=True, text=True, timeout=300,
            cwd=str(deliver_script.parent),
        )
        for line in result.stdout.splitlines():
            log(f"  [deliver] {line}")
        if result.returncode != 0:
            log(f"Delivery failed (exit {result.returncode}): {result.stderr[:200]}")
            return False
        log("Delivery complete.")
        return True
    except subprocess.TimeoutExpired:
        log("Delivery timed out (300s)")
        return False
    except Exception as e:
        log(f"Delivery error: {e}")
        return False


def main():
    log(f"Starting dream pipeline for {TODAY}")

    # Verify model exists before spending time on anything else
    global MODEL
    MODEL = get_available_model()

    # Check if already done
    if PENDING.exists():
        existing = json.loads(PENDING.read_text())
        if existing.get("date") == TODAY and existing.get("narrative"):
            log(f"Already have pending delivery for {TODAY} — skipping generation")
            # Still attempt delivery in case it failed before
            deliver_dream()
            return

    # Step 1: Generate narrative
    narrative, inspirations = generate_narrative()
    if not narrative:
        log("ERROR: Empty narrative returned")
        sys.exit(1)

    # Step 2: Generate dream image
    log("Generating dream image...")
    image_path = generate_dream_image(narrative)
    if image_path:
        # Update dream_latest.png symlink
        latest = WORKSPACE / "dream_latest.png"
        latest.unlink(missing_ok=True)
        try:
            import shutil
            shutil.copy2(image_path, str(latest))
        except Exception:
            pass

    # Step 3: Write journal and pending delivery
    journal_path = write_journal(narrative, image_path=image_path, inspirations=inspirations)
    write_pending(narrative, journal_path, image_path=image_path, inspirations=inspirations)
    store_memory(narrative)

    log(f"Generation done. {len(narrative.split())} words, image: {image_path or 'none'}.")

    # Step 4: Deliver to Slack + email
    deliver_dream()

    log(f"Dream pipeline complete for {TODAY}.")


if __name__ == "__main__":
    main()
