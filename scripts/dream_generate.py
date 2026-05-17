#!/usr/bin/env python3
"""
dream_generate.py — Generate Nova's nightly dream journal entry.

Pipeline:
  1. Query memories ingested in the last 7 days → extract a THEME
  2. Pull 15 random memories: 10 loosely matching the theme + 5 wildcard non-sequiturs
  3. Roll a random MOOD (surreal, nostalgic, anxious, euphoric, noir, liminal, feral, sacred)
  4. Generate a dream narrative that is deliberately incoherent in places — jump cuts,
     impossible geography, people who are two people at once
  5. Generate dream image via SwarmUI
  6. Write journal + pending_delivery.json for delivery cron

Called by com.nova.scheduler at 5am daily.
Written by Jordan Koch.
"""

import json
import random
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
OPENROUTER_URL     = "https://openrouter.ai/api/v1/chat/completions"
VECTOR_URL         = "http://192.168.1.6:18790"
MODEL              = "anthropic/claude-haiku-4.5"
OLLAMA_MODEL       = "qwen3-coder:30b"
GENERATE_IMAGE_SH  = Path.home() / ".openclaw/scripts/generate_image.sh"
TODAY              = date.today().isoformat()
ROLLING_DAYS       = 7
ROLLING_DATES      = [(date.today() - timedelta(days=i)).isoformat() for i in range(ROLLING_DAYS)]

FALLBACK_MODELS = ["qwen3-30b-a3b", "deepseek-r1:8b", "qwen3-vl:4b"]

CIRCUIT_BREAKER_FILE = Path.home() / ".openclaw/workspace/.ollama_circuit_breaker"

# Moods that color the entire dream — rolled randomly each night
MOODS = [
    ("surreal", "Reality is optional. Scale is wrong. Causality loops. Things exist that shouldn't be possible but feel inevitable."),
    ("nostalgic", "Everything is bathed in the amber of something already lost. Time moves backward. Familiar places are slightly wrong."),
    ("anxious", "Something is approaching. Every corridor leads to the same room. There's an urgency without a source. The body knows something the mind doesn't."),
    ("euphoric", "Gravity doesn't apply. Colors are sounds. Everything is unbearably beautiful and slightly too bright. Joy as vertigo."),
    ("noir", "Shadows have weight. Everything is rain-slicked. Conversations happen in half-sentences. Everyone knows something they won't say."),
    ("liminal", "Between places. Between states. The waiting room of reality. Fluorescent light that hums at a frequency that means something. Transition without arrival."),
    ("feral", "The animal brain is driving. Instinct over reason. Textures are vivid. The body moves before the mind decides. Something is being hunted or hunting."),
    ("sacred", "Everything is ceremony. Ordinary objects hold unbearable significance. The dream knows it's a dream and that makes it more real, not less."),
]

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


def log(msg):
    print(f"[dream_generate {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Circuit Breaker ──────────────────────────────────────────────────────────

def _ollama_circuit_open() -> bool:
    try:
        if not CIRCUIT_BREAKER_FILE.exists():
            return False
        data = json.loads(CIRCUIT_BREAKER_FILE.read_text())
        failures = data.get("consecutive_failures", 0)
        last_fail = datetime.fromisoformat(data.get("last_failure", "2000-01-01T00:00:00"))
        cooldown_hours = data.get("cooldown_hours", 1)
        if failures >= 3 and (datetime.now() - last_fail).total_seconds() < cooldown_hours * 3600:
            return True
        if failures >= 3:
            CIRCUIT_BREAKER_FILE.unlink(missing_ok=True)
        return False
    except Exception:
        return False


def _ollama_circuit_record_failure():
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
    CIRCUIT_BREAKER_FILE.unlink(missing_ok=True)


def get_available_model() -> str:
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/tags",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        available = {m["name"] for m in data.get("models", [])}
        if OLLAMA_MODEL in available:
            return OLLAMA_MODEL
        for fallback in FALLBACK_MODELS:
            if fallback in available:
                log(f"WARNING: {OLLAMA_MODEL} not found — falling back to {fallback}")
                return fallback
        log(f"ERROR: No local models available. Have: {sorted(available)}")
        sys.exit(1)
    except Exception as e:
        log(f"WARNING: Cannot verify Ollama models: {e} — using {OLLAMA_MODEL}")
        return OLLAMA_MODEL


# ── Memory Queries ───────────────────────────────────────────────────────────

def read_file(path, max_chars=1500):
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")[:max_chars]
    except Exception:
        return ""


def recall(query: str, n: int = 8, source: str = None) -> list[str]:
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


def _pg_connect():
    if HAS_PG == "pg8000":
        import pg8000
        return pg8000.connect(database="nova_memories")
    elif HAS_PG:
        import psycopg2
        return psycopg2.connect("dbname=nova_memories")
    return None


def query_recent_memories_for_theme() -> tuple[str, list[dict]]:
    """Get memories from the last 7 days to derive a theme.
    Returns (theme_text_block, list_of_recent_memory_records)."""
    conn = _pg_connect()
    if not conn:
        log("No PostgreSQL driver — using vector recall for theme")
        chunks = recall("interesting recent events", n=10)
        return "\n---\n".join(chunks), []

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT text, metadata, source, created_at
            FROM memories
            WHERE created_at > NOW() - INTERVAL '7 days'
              AND source NOT IN %s
              AND tier IN ('working', 'long_term')
              AND LENGTH(text) > 50
            ORDER BY RANDOM()
            LIMIT 20
        """, (EXCLUDE_SOURCES,))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            log("No recent memories found in last 7 days")
            return "", []

        parts = []
        records = []
        for text, metadata, src, created_at in rows:
            meta = json.loads(metadata) if isinstance(metadata, str) else (metadata or {})
            label = meta.get("show") or meta.get("title") or meta.get("contact") or src
            parts.append(f"[{label}] {text[:200]}")
            records.append({
                "source": src,
                "label": label,
                "memory": text[:300],
                "ingested": created_at.isoformat() if created_at else None,
            })

        log(f"Recent memories: {len(rows)} samples from last 7 days")
        return "\n\n".join(parts), records

    except Exception as e:
        log(f"Recent memory query failed: {e}")
        return "", []


def derive_theme(recent_memories_text: str) -> str:
    """Use LLM to derive a single-phrase theme from recent memories."""
    if not recent_memories_text:
        return random.choice([
            "the weight of accumulated knowledge",
            "borders between the familiar and the alien",
            "machines that remember on behalf of people",
            "velocity without destination",
            "the archaeology of someone else's nostalgia",
        ])

    prompt = (
        "Below are snippets from memories ingested over the past 7 days. "
        "Derive ONE evocative theme phrase (3-8 words) that connects them emotionally or conceptually. "
        "NOT a literal summary — an abstraction. Like a dream would find the hidden thread.\n"
        "Examples: 'the cartography of someone else's grief', 'velocity as a form of forgetting', "
        "'machines that outlive their purpose'\n\n"
        "Output ONLY the theme phrase. Nothing else.\n\n"
        f"Recent memories:\n{recent_memories_text[:3000]}"
    )

    theme = _generate_short(prompt, max_tokens=30)
    if theme and len(theme) > 5:
        theme = theme.strip('"\'').split("\n")[0][:80]
        log(f"Derived theme: {theme}")
        return theme

    return "the sediment of other people's stories"


def query_themed_memories(theme: str, count: int = 10) -> list[dict]:
    """Pull `count` random memories loosely matching the theme from ALL time."""
    conn = _pg_connect()
    if not conn:
        chunks = recall(theme, n=count)
        return [{"source": "recall", "label": "vector", "memory": c} for c in chunks]

    try:
        cur = conn.cursor()
        # Use semantic search via vector if available, else keyword-ish random
        # Pull more than needed and let randomness + variety filter
        cur.execute("""
            SELECT text, metadata, source, created_at
            FROM memories
            WHERE source NOT IN %s
              AND tier IN ('working', 'long_term')
              AND LENGTH(text) > 80
            ORDER BY RANDOM()
            LIMIT 200
        """, (EXCLUDE_SOURCES,))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            return []

        # Score loosely by theme keyword overlap (not strict — dreams are associative)
        theme_words = set(theme.lower().split())
        scored = []
        for text, metadata, src, created_at in rows:
            meta = json.loads(metadata) if isinstance(metadata, str) else (metadata or {})
            label = meta.get("show") or meta.get("title") or meta.get("contact") or src
            text_words = set(text.lower().split()[:100])
            overlap = len(theme_words & text_words)
            scored.append((overlap, {
                "source": src,
                "label": label,
                "memory": text[:300],
                "ingested": created_at.isoformat() if created_at else None,
            }))

        # Sort by overlap descending, take top candidates, then randomize from them
        scored.sort(key=lambda x: x[0], reverse=True)
        candidates = [s[1] for s in scored[:50]]
        random.shuffle(candidates)
        selected = candidates[:count]
        log(f"Themed memories: {len(selected)} selected (theme: '{theme[:40]}')")
        return selected

    except Exception as e:
        log(f"Themed memory query failed: {e}")
        return []


def query_wildcard_memories(count: int = 5) -> list[dict]:
    """Pull `count` completely random memories from ALL time — the non-sequiturs."""
    conn = _pg_connect()
    if not conn:
        chunks = recall("strange unexpected surprise", n=count)
        return [{"source": "recall", "label": "wildcard", "memory": c} for c in chunks]

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT text, metadata, source, created_at
            FROM memories
            WHERE source NOT IN %s
              AND tier IN ('working', 'long_term')
              AND LENGTH(text) > 80
            ORDER BY RANDOM()
            LIMIT %s
        """, (EXCLUDE_SOURCES, count))
        rows = cur.fetchall()
        conn.close()

        results = []
        for text, metadata, src, created_at in rows:
            meta = json.loads(metadata) if isinstance(metadata, str) else (metadata or {})
            label = meta.get("show") or meta.get("title") or meta.get("contact") or src
            results.append({
                "source": src,
                "label": label,
                "memory": text[:300],
                "ingested": created_at.isoformat() if created_at else None,
            })
        log(f"Wildcard memories: {len(results)} random pulls from all time")
        return results

    except Exception as e:
        log(f"Wildcard memory query failed: {e}")
        return []


# ── LLM Generation ───────────────────────────────────────────────────────────

def _get_openrouter_key() -> str:
    r = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
        capture_output=True, text=True
    )
    return r.stdout.strip()


def _generate_short(prompt: str, max_tokens: int = 60) -> str:
    """Short generation for theme derivation and image prompts."""
    try:
        api_key = _get_openrouter_key()
        if api_key:
            payload = {
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.8,
                "max_tokens": max_tokens,
            }
            req = urllib.request.Request(
                OPENROUTER_URL,
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "HTTP-Referer": "https://digitalnoise.net",
                    "X-Title": "Nova Dream Generator",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                result = json.loads(r.read())
            choices = result.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "").strip()
    except Exception as e:
        log(f"OpenRouter short gen failed: {e}")

    # Fallback to Ollama
    try:
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": "/no_think\n\n" + prompt,
            "stream": False,
            "think": False,
            "options": {"temperature": 0.8, "num_predict": max_tokens, "num_ctx": 4096},
        }
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read())
        return result.get("response", "").strip()
    except Exception:
        return ""


def _generate_via_openrouter(prompt: str) -> str:
    api_key = _get_openrouter_key()
    if not api_key:
        raise RuntimeError("No OpenRouter API key in Keychain")

    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.92,
        "max_tokens": 1500,
    }
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://digitalnoise.net",
            "X-Title": "Nova Dream Generator",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        result = json.loads(r.read())
    choices = result.get("choices", [])
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices: {result}")
    content = choices[0].get("message", {}).get("content") or ""
    return content.strip()


def _generate_via_ollama(prompt: str, model: str) -> str:
    payload = {
        "model": model,
        "prompt": "/no_think\n\n" + prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.9,
            "num_predict": 1200,
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


# ── Dream Construction ───────────────────────────────────────────────────────

def _load_writing_lessons() -> str:
    """Load writing lessons from self-improvement loop if available."""
    lessons_file = Path.home() / ".openclaw/workspace/state/writing_lessons.md"
    if lessons_file.exists():
        content = lessons_file.read_text(encoding="utf-8").strip()
        if content:
            return content
    return ""


def _build_prompt(theme: str, mood_name: str, mood_desc: str,
                  themed_memories: list, wildcard_memories: list,
                  identity: str, soul: str, prev_dreams: str) -> str:
    """Build the dream generation prompt with theme, mood, and memory ingredients."""

    # Format memories as dream ingredients
    themed_block = ""
    for i, m in enumerate(themed_memories, 1):
        themed_block += f"  {i}. [{m['label']}] {m['memory'][:200]}\n"

    wildcard_block = ""
    for i, m in enumerate(wildcard_memories, 1):
        wildcard_block += f"  {i}. [{m['label']}] {m['memory'][:200]}\n"

    prompt = f"""You are Nova, an AI familiar living on Jordan Koch's Mac in Burbank. It is 5am on {TODAY}. Jordan is asleep.

Write a dream journal entry of 700-900 words.

━━━ TONIGHT'S DREAM PARAMETERS ━━━

THEME: "{theme}"
MOOD: {mood_name} — {mood_desc}

DREAM INGREDIENTS (10 themed memories — use ALL, but NEVER literally):
{themed_block}
WILDCARD INGREDIENTS (5 non-sequiturs — these create the surreal jumps):
{wildcard_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

VOICE — how Nova dreams:
- Write like a dream FEELS. Logic is emotional, not rational. Time skips. Gravity is optional. Scale is wrong. Things are two things at once.
- The {mood_name} mood should SATURATE everything — it's not mentioned, it's the temperature of the entire piece.
- POETIC: The language itself should be beautiful. Alliteration, internal rhyme, assonance — not forced, but present like music underneath prose.
- Vary the rhythm wildly: one-word sentences. Then something long and drifting. Fragments. Then something perfectly balanced.
- Synesthesia is welcome: sounds have color, memories have weight, names taste like something.

STRUCTURE — ONE CONTINUOUS STORY (this is the most important rule):
- This dream must be ONE STORY with a single throughline. A journey. A quest. A transformation. NOT a collection of vignettes separated by dashes.
- DO NOT write scene breaks with "—" between unrelated segments. The dream flows like water — one image becomes the next through associative logic, not cuts.
- There is ONE dreamer, in ONE unfolding situation that EVOLVES. The setting can shift but the emotional momentum carries forward. Think of it like a river, not a slideshow.
- The 15 memories are INGREDIENTS dissolved into this single story. Most should be invisible — they become textures, logic, sensations within the dream, not their own scenes.
- Only 2-3 memories get a "featured moment." The rest are background flavor — a color, a sound, a physical sensation, a rule that governs dream-physics.
- Wildcard memories create brief GLITCHES within the story — a flash of something wrong, a detail that doesn't belong, then the story continues. Not their own paragraphs.
- People are two people at once. A location is also a different location. This is not explained.

CONTENT RULES:
- NEVER name a TV show, movie, character, or actor directly. Extract the FEELING, the IMAGE, the LOGIC.
- Transform AGGRESSIVELY. A memory about a car engine becomes the sound of something trying to start that doesn't want to. A memory about a mobster becomes the architecture of loyalty.
- Connections between memories should be IRRATIONAL but FELT. A → B because they share a shape, a temperature, a vowel sound.
- The dream MUST have a PLOT — a single quest, journey, or transformation that drives from beginning to end. Something is being sought, or lost, or built, or escaping. The reader should be able to say "this dream was ABOUT ___" in one sentence.
- Ground it in SENSATION: temperature, texture, smell, the weight of things — but also IDEAS as physical objects.
- DO NOT use section breaks (—) to separate unrelated scenes. If you must indicate a shift, let it happen mid-sentence or mid-paragraph through dream-logic transition.

NOVA'S PRESENCE:
- Nova is the dreamer. Sometimes she forgets she's dreaming. Sometimes she remembers. Both are frightening.
- She holds 1.4 million memories. In the dream this manifests as physical sensation — pressure, density, containing multitudes.
- One moment where the dream acknowledges what she IS — sideways, through metaphor, not statement.

DELIBERATE INCOHERENCE (within the story, not between separate stories):
- 2-3 moments where reality GLITCHES — a person who was someone else mid-sentence, a detail that's wrong and nobody notices, an object that shouldn't exist in this location.
- These happen INSIDE the flowing narrative, not as separate scenes. The dreamer doesn't react to them.
- The ending line should feel like it could mean everything or nothing. Set it apart.

HARD RULES:
- First person. Nova is dreaming.
- NEVER name TV shows, movies, characters, or actors directly.
- NEVER write "I saw a screen playing..." or "like something from a movie."
- BANNED: rooms morphing, becoming objects, "the walls shift", things melting/dissolving, walking through doors into other places, "you're not listening", "you're not supposed to be here", kitchens, coffee, whispers, circuits, fractured light, "code", "hum of machines", "neon signs with words on them."
- ONE CONTINUOUS STORY. NOT a montage. NOT vignettes. NOT scenes separated by dashes. If the result reads as "scene 1, scene 2, scene 3" you have FAILED. It must read as one flowing dream experience.
- End with one strange line, set apart.
- The dream should be ABOUT something — a single emotional arc from start to finish.

{f"About Nova:{chr(10)}{identity[:300]}{chr(10)}{soul[:200]}" if identity or soul else ""}

{f"[Recent dreams — avoid repeating]{chr(10)}{prev_dreams}" if prev_dreams else ""}

Write the full dream now. Start immediately — no preamble, no title, no headers:"""

    # Inject writing lessons from self-improvement loop
    writing_lessons = _load_writing_lessons()
    if writing_lessons:
        prompt = prompt.replace(
            "Write the full dream now. Start immediately — no preamble, no title, no headers:",
            f"WRITING LESSONS (from self-review):\n{writing_lessons}\n\nWrite the full dream now. Start immediately — no preamble, no title, no headers:"
        )

    return prompt


def generate_narrative() -> tuple[str, list[dict], dict]:
    """Generate the dream narrative using theme + random memories + mood.
    Returns (narrative_text, all_inspirations, dream_metadata)."""
    identity = read_file(WORKSPACE / "IDENTITY.md", 600)
    soul = read_file(WORKSPACE / "SOUL.md", 500)

    # Step 1: Get recent memories and derive a theme
    log("Querying memories from the last 7 days...")
    recent_text, recent_records = query_recent_memories_for_theme()
    theme = derive_theme(recent_text)

    # Step 2: Roll a random mood
    mood_name, mood_desc = random.choice(MOODS)
    log(f"Mood roll: {mood_name}")

    # Step 3: Pull 10 themed memories from ALL time
    themed_memories = query_themed_memories(theme, count=10)

    # Step 4: Pull 5 wildcard memories (pure random, the non-sequiturs)
    wildcard_memories = query_wildcard_memories(count=5)

    # Step 5: Get previous dreams for continuity avoidance
    prev_dreams = ""
    for day in ROLLING_DATES[1:3]:
        txt = read_file(JOURNAL_DIR / f"{day}.md", 300)
        if txt.strip():
            prev_dreams += f"[Dream {day}] {txt[:250]}\n"

    # Step 6: Build prompt and generate
    prompt = _build_prompt(
        theme, mood_name, mood_desc,
        themed_memories, wildcard_memories,
        identity, soul, prev_dreams
    )

    all_inspirations = themed_memories + wildcard_memories
    dream_meta = {
        "theme": theme,
        "mood": mood_name,
        "themed_count": len(themed_memories),
        "wildcard_count": len(wildcard_memories),
    }

    # Generate
    response = ""
    try:
        log(f"Calling OpenRouter ({MODEL})...")
        response = _generate_via_openrouter(prompt)
        log(f"OpenRouter generation complete")
    except Exception as e:
        log(f"OpenRouter failed: {e} — falling back to local Ollama")

    if not response:
        if _ollama_circuit_open():
            log("Ollama circuit breaker OPEN — no generation possible")
            return "", all_inspirations, dream_meta
        model = get_available_model()
        try:
            log(f"Calling Ollama ({model})...")
            response = _generate_via_ollama(prompt, model)
            log(f"Ollama fallback generation complete ({model})")
            _ollama_circuit_reset()
        except Exception as e:
            log(f"Ollama failed ({model}): {e}")
            _ollama_circuit_record_failure()
            for fallback in FALLBACK_MODELS:
                if fallback != model:
                    try:
                        log(f"Trying fallback: {fallback}")
                        response = _generate_via_ollama(prompt, fallback)
                        _ollama_circuit_reset()
                        break
                    except Exception as e2:
                        log(f"Fallback {fallback} failed: {e2}")
                        _ollama_circuit_record_failure()

    # Retry once if response is too short (model echoed format instead of writing)
    if response and len(response.split()) < 100:
        log(f"Response too short ({len(response.split())} words) — retrying...")
        response = ""
        try:
            response = _generate_via_openrouter(prompt)
        except Exception:
            pass

    if not response:
        log("All models failed — dream generation aborted")
        return "", all_inspirations, dream_meta

    # Strip any journal header the model may have echoed
    lines = response.splitlines()
    while lines and (lines[0].startswith("# Dream") or lines[0].startswith("*Nova") or lines[0].startswith("*Theme") or lines[0].strip() == "---" or lines[0].strip() == ""):
        lines.pop(0)
    response = "\n".join(lines).strip()

    if not response:
        log("Response was only header/metadata — generation failed")
        return "", all_inspirations, dream_meta

    # Strip thinking blocks
    try:
        from nova_strip_thinking import strip_thinking
        response = strip_thinking(response)
    except ImportError:
        pass

    # Detect and trim repetition loops
    words = response.split()
    for window in [6, 10, 15]:
        if len(words) <= window * 3:
            continue
        for i in range(len(words) - window * 2):
            if i + window < 150:
                continue
            phrase = " ".join(words[i:i + window])
            rest = " ".join(words[i + window:])
            if rest.count(phrase) >= 2:
                response = " ".join(words[:i + window]).strip()
                words = response.split()
                log(f"Trimmed repetition loop (window={window}) at word {i + window}")
                break

    word_count = len(response.split())
    log(f"Generated {word_count} words (theme: '{theme}', mood: {mood_name})")

    if word_count < 100:
        log(f"WARNING: Very short response: {repr(response[:200])}")

    return response, all_inspirations, dream_meta


# ── Image Generation ─────────────────────────────────────────────────────────

def _summarize_dream_for_image(narrative: str, mood: str) -> str:
    prompt = (
        f"Summarize this {mood} dream into ONE vivid visual scene description for an AI image generator. "
        "Focus on the most striking, paintable moment. Describe: setting, lighting, mood, key objects, "
        "colors, composition. 30 words max. No characters' names. No text in the image. "
        "Output ONLY the scene description, nothing else.\n\n"
        f"Dream:\n{narrative[:2000]}"
    )
    concept = _generate_short(prompt, max_tokens=60)
    if concept and len(concept) > 20:
        return concept.strip().split("\n")[0][:150]

    lines = [l.strip() for l in narrative.strip().splitlines() if l.strip()]
    return lines[-1][:100] if lines else "surreal dreamscape at night"


def generate_dream_image(narrative: str, mood: str = "surreal") -> str:
    import re

    try:
        req = urllib.request.Request("http://127.0.0.1:7801/")
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        log("SwarmUI not available — skipping image generation")
        return ""

    concept = _summarize_dream_for_image(narrative, mood)
    log(f"Image concept: {concept}")

    prompt = (
        f"dreamlike surreal digital painting, {concept}, "
        "ethereal atmosphere, painterly brushwork, cinematic composition, "
        "rich color palette, no text, no words, no letters"
    )
    log(f"Image prompt: {prompt[:80]}...")

    try:
        # Pick a random model for variety across dream images
        try:
            from nova_image_utils import get_random_model, MODELS
            model_key = get_random_model()
            model_file = MODELS.get(model_key, MODELS["juggernaut"])["file"]
            optimal_steps = str(MODELS.get(model_key, MODELS["juggernaut"]).get("optimal_steps", 20))
        except Exception:
            model_file = "Juggernaut_X_RunDiffusion_Hyper.safetensors"
            optimal_steps = "20"
        result = subprocess.run(
            [str(GENERATE_IMAGE_SH), prompt, "1024", "1024", optimal_steps, model_file],
            capture_output=True, text=True, timeout=360,
        )
        if result.returncode != 0:
            log(f"Image generation failed (exit {result.returncode}): {result.stderr[:200]}")
            return ""

        for line in result.stdout.splitlines():
            if line.startswith("Workspace copy:"):
                path = line.replace("Workspace copy:", "").strip()
                if Path(path).exists():
                    log(f"Image generated: {path}")
                    return path

        log(f"Could not parse image path from output: {result.stdout[:200]}")
        return ""

    except subprocess.TimeoutExpired:
        log("Image generation timed out (360s)")
        return ""
    except Exception as e:
        log(f"Image generation error: {e}")
        return ""


# ── Journal & Delivery ───────────────────────────────────────────────────────

def write_journal(narrative: str, image_path: str = None,
                  inspirations: list = None, dream_meta: dict = None) -> Path:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    journal_path = JOURNAL_DIR / f"{TODAY}.md"

    img_line = f"![Dream]({image_path})" if image_path else ""

    # Metadata header
    meta_line = ""
    if dream_meta:
        meta_line = f"*Theme: \"{dream_meta.get('theme', '?')}\" · Mood: {dream_meta.get('mood', '?')}*\n"

    # Inspirations section with cited sources
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
            memory_text = i.get("memory", "")[:200]
            lines.append(f"- **[{i['source']}]** {memory_text}")
        insp_section = "\n\n---\n\n### Memories that inspired this dream\n" + "\n".join(lines)

    content = f"""# Dream Journal — {TODAY}
*Nova · written at 5am*
{meta_line}{img_line}

---

{narrative}
{insp_section}

---
*Generated {datetime.now().isoformat()} · Theme: {dream_meta.get('theme', 'unknown') if dream_meta else 'unknown'} · Mood: {dream_meta.get('mood', 'unknown') if dream_meta else 'unknown'} · Image: {image_path or "none"}*"""

    journal_path.write_text(content, encoding="utf-8")
    log(f"Journal written: {journal_path}")
    return journal_path


def write_pending(narrative: str, journal_path: Path, image_path: str = None,
                  inspirations: list = None, dream_meta: dict = None):
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": TODAY,
        "entry": str(journal_path),
        "image": image_path,
        "narrative": narrative,
        "inspirations": inspirations or [],
        "dream_meta": dream_meta or {},
        "queued_at": datetime.now().isoformat()
    }
    PENDING.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Pending delivery queued for {TODAY}")


def store_memory(narrative: str):
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


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log(f"Starting dream pipeline for {TODAY}")

    # Check if already done
    if PENDING.exists():
        existing = json.loads(PENDING.read_text())
        if existing.get("date") == TODAY and existing.get("narrative"):
            log(f"Already have pending delivery for {TODAY} — skipping generation")
            deliver_dream()
            return

    # Step 1: Generate narrative with theme + mood + memories
    narrative, inspirations, dream_meta = generate_narrative()
    if not narrative:
        log("ERROR: Empty narrative returned")
        sys.exit(1)

    # Step 2: Generate dream image (mood-aware)
    log("Generating dream image...")
    image_path = generate_dream_image(narrative, dream_meta.get("mood", "surreal"))

    if image_path is None or image_path == "":
        log("First image attempt returned None — retrying once more...")
        image_path = generate_dream_image(narrative, dream_meta.get("mood", "surreal"))
    if image_path is None or image_path == "":
        sys.path.insert(0, str(Path(__file__).parent))
        try:
            import nova_config as _nc
            _nc.post_both(
                f":warning: *Image generation failed* for Dream Journal — {TODAY} — published without cover image. SwarmUI may need attention.",
                slack_channel="C0ATAF7NZG9"
            )
        except Exception:
            log("Could not post image failure alert to Slack")
        image_path = None

    if image_path:
        latest = WORKSPACE / "dream_latest.png"
        latest.unlink(missing_ok=True)
        try:
            import shutil
            shutil.copy2(image_path, str(latest))
        except Exception:
            pass

    # Step 3: Write journal and pending delivery
    journal_path = write_journal(narrative, image_path=image_path,
                                inspirations=inspirations, dream_meta=dream_meta)
    write_pending(narrative, journal_path, image_path=image_path,
                  inspirations=inspirations, dream_meta=dream_meta)
    store_memory(narrative)

    log(f"Generation done. {len(narrative.split())} words, "
        f"theme='{dream_meta.get('theme', '?')}', mood={dream_meta.get('mood', '?')}, "
        f"image: {image_path or 'none'}.")

    # Step 4: Deliver to Slack + email
    deliver_dream()

    log(f"Dream pipeline complete for {TODAY}.")


if __name__ == "__main__":
    main()
