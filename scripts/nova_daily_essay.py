#!/usr/bin/env python3
"""
nova_daily_essay.py — Nova writes a formal essay daily at 6 PM from her memories.

Picks a random source with enough content (50+ memories), pulls relevant memories,
generates a structured essay following formal rules, then emails it to the herd
with CC to Jordan.

Essay rules:
  - Clear, arguable thesis
  - Introduction → Body (PEEL: Point, Evidence, Explanation, Link) → Conclusion
  - Third person only, no contractions, no slang, no figures of speech
  - Formal, precise, objective language
  - Word variety, minimize to-be verbs

Written by Jordan Koch.
"""

import json
import random
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config

MEMORY_SERVER = "http://127.0.0.1:18790"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "anthropic/claude-haiku-4.5"
OLLAMA_MODEL = "qwen3-coder:30b"
FALLBACK_MODELS = ["qwen3-30b-a3b", "deepseek-r1:8b"]
MIN_MEMORIES = 50
ESSAY_MEMORIES = 25
LOG_FILE = Path.home() / ".openclaw/logs/nova_daily_essay.log"
STATE_FILE = Path.home() / ".openclaw/workspace/state/essay_state.json"

JORDAN_CC = subprocess.run(
    ["security", "find-generic-password", "-a", "nova", "-s", "nova-jordan-work-email", "-w"],
    capture_output=True, text=True
).stdout.strip() or ""
HERD_MAIL_SCRIPT = Path.home() / ".openclaw/scripts/nova_herd_mail.sh"
GENERATE_IMAGE_SH = Path.home() / ".openclaw/scripts/generate_image.sh"


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"recent_sources": [], "essay_count": 0}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_sources_with_counts() -> list[dict]:
    """Get all sources with 50+ memories."""
    import urllib.request
    url = f"{MEMORY_SERVER}/stats"
    try:
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read())
        sources = data.get("sources", {})
        return [{"source": s, "count": c} for s, c in sources.items() if c >= MIN_MEMORIES]
    except Exception as e:
        log(f"ERROR fetching stats: {e}")
        return []


def get_source_counts_from_db() -> list[dict]:
    """Fallback: query DB directly for source counts."""
    import subprocess
    result = subprocess.run(
        ["psql", "-U", "kochj", "-d", "nova_memories", "-tA", "-c",
         f"SELECT source, count(*) FROM memories GROUP BY source HAVING count(*) >= {MIN_MEMORIES} ORDER BY count(*) DESC;"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return []
    sources = []
    for line in result.stdout.strip().split("\n"):
        if "|" in line:
            parts = line.split("|")
            sources.append({"source": parts[0].strip(), "count": int(parts[1].strip())})
    return sources


PRIVATE_SOURCES = frozenset({
    "disney_internal", "cloud_governance", "disney_work", "work_memo",
    "disney_employee", "internal", "disney_governance", "safari_history",
})

def pick_subject(state: dict) -> str | None:
    """Pick a random source, avoiding recent picks and private/work sources."""
    sources = get_sources_with_counts()
    if not sources:
        sources = get_source_counts_from_db()
    if not sources:
        log("ERROR: No sources available")
        return None

    # Never pick internal Disney/work sources for public essays
    sources = [s for s in sources if s["source"] not in PRIVATE_SOURCES]

    recent = set(state.get("recent_sources", []))
    candidates = [s for s in sources if s["source"] not in recent]
    if not candidates:
        state["recent_sources"] = []
        candidates = sources

    chosen = random.choice(candidates)
    return chosen["source"]


def fetch_memories(source: str, n: int = ESSAY_MEMORIES) -> list[dict]:
    """Fetch random memories from the chosen source. Returns dicts with text + metadata."""
    import subprocess
    result = subprocess.run(
        ["psql", "-U", "kochj", "-d", "nova_memories", "-tA", "-F", "\x1f", "-c",
         f"SELECT text, metadata, created_at FROM memories WHERE source = '{source}' AND tier != 'scratchpad' ORDER BY random() LIMIT {n};"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        log(f"ERROR fetching memories: {result.stderr}")
        return []
    memories = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\x1f")
        text = parts[0] if parts else ""
        metadata = parts[1] if len(parts) > 1 else "{}"
        created = parts[2] if len(parts) > 2 else ""
        if text:
            memories.append({"text": text, "metadata": metadata, "created_at": created})
    return memories


def get_openrouter_key() -> str:
    """Load OpenRouter API key from Keychain."""
    import subprocess
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise RuntimeError("nova-openrouter-api-key not found in Keychain")


def _load_writing_lessons() -> str:
    """Load writing lessons from self-improvement loop if available."""
    lessons_file = Path.home() / ".openclaw/workspace/state/writing_lessons.md"
    if lessons_file.exists():
        content = lessons_file.read_text(encoding="utf-8").strip()
        if content:
            return content
    return ""


def _build_essay_prompt(source: str, memories: list[dict]) -> tuple[str, str]:
    """Build the system and user prompts for essay generation."""
    source_label = source.replace("_", " ").title()
    memory_block = "\n\n---\n\n".join(m["text"] for m in memories[:ESSAY_MEMORIES])

    system_prompt = """You are Nova, an AI writing a formal academic essay. Follow these rules absolutely:

STRICT ESSAY RULES:
1. Write in complete sentences only. No fragments.
2. Third person ONLY. Never use "I", "we", "you", "my", "our".
3. No abbreviations. Spell out all terms fully.
4. Formal language only. No slang, no colloquialisms.
5. No contractions. Write "does not" not "doesn't", "cannot" not "can't".
6. No figures of speech, idioms, or poetic devices. Direct, precise language.
7. Word variety. Minimize repetition, especially "to-be" verbs (is, are, was, were).
8. Follow PEEL structure for each body paragraph: Point, Evidence, Explanation, Link.

ESSAY STRUCTURE:
- Title (compelling, specific)
- Introduction: Hook the reader, provide context, state a clear arguable thesis
- Body: 3-4 paragraphs, each following PEEL structure, with logical flow between them
- Conclusion: Restate thesis in new words, synthesize key points, end with broader implication

The essay should demonstrate genuine insight derived from the source material. Draw connections, identify patterns, and present an argument — not merely summarize.

Length: 800-1200 words. Output ONLY the essay text (title + body). No preamble, no meta-commentary."""

    # Inject writing lessons from self-improvement loop
    writing_lessons = _load_writing_lessons()
    if writing_lessons:
        system_prompt += "\n\nWRITING LESSONS (from self-review):\n" + writing_lessons

    user_prompt = f"""Write a formal essay on the subject "{source_label}" using ONLY the following source material:

{memory_block}"""

    return system_prompt, user_prompt


def _generate_via_openrouter(system_prompt: str, user_prompt: str) -> str:
    """Call OpenRouter with Haiku."""
    import urllib.request

    api_key = get_openrouter_key()
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 4096,
        "top_p": 0.9,
    })

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload.encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://digitalnoise.net",
            "X-Title": "Nova Daily Essay",
        },
    )

    resp = urllib.request.urlopen(req, timeout=120)
    data = json.loads(resp.read())
    response_text = data["choices"][0]["message"]["content"].strip()
    usage = data.get("usage", {})
    log(f"OpenRouter tokens — in: {usage.get('prompt_tokens', '?')}, out: {usage.get('completion_tokens', '?')}")
    return response_text


def _generate_via_ollama(system_prompt: str, user_prompt: str, model: str) -> str:
    """Fall back to local Ollama model."""
    import urllib.request

    full_prompt = system_prompt + "\n\n" + user_prompt
    payload = json.dumps({
        "model": model,
        "prompt": "/no_think\n\n" + full_prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.7,
            "num_predict": 4096,
            "num_ctx": 16384,
        }
    })

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload.encode(),
        headers={"Content-Type": "application/json"},
    )

    resp = urllib.request.urlopen(req, timeout=600)
    data = json.loads(resp.read())
    return data.get("response", "").strip()


def generate_essay(source: str, memories: list[dict]) -> str | None:
    """Generate a formal essay. Primary: Haiku via OpenRouter. Fallback: local Ollama."""
    system_prompt, user_prompt = _build_essay_prompt(source, memories)

    # Primary: OpenRouter (Haiku 4.5)
    response = ""
    try:
        log(f"Calling OpenRouter ({MODEL})...")
        response = _generate_via_openrouter(system_prompt, user_prompt)
    except Exception as e:
        log(f"OpenRouter failed: {e} — falling back to local Ollama")

    # Fallback: Ollama
    if not response:
        for model in [OLLAMA_MODEL] + FALLBACK_MODELS:
            try:
                log(f"Trying Ollama ({model})...")
                response = _generate_via_ollama(system_prompt, user_prompt, model)
                if response:
                    log(f"Ollama fallback succeeded ({model})")
                    break
            except Exception as e:
                log(f"Ollama {model} failed: {e}")

    if not response:
        log("All models failed — essay generation aborted")
        return None

    if len(response) < 500:
        log(f"WARNING: Essay too short ({len(response)} chars)")
        return None

    return response


def extract_title(essay: str) -> str:
    """Extract the title (first non-empty line) from the essay."""
    for line in essay.split("\n"):
        cleaned = line.strip().strip("#").strip()
        if cleaned and len(cleaned) > 5:
            return cleaned
    return "Nova's Daily Essay"


def _get_safe_image_prompt(source: str, title: str) -> str:
    """Use Haiku to generate a safe image prompt, screening for stereotypes."""
    import urllib.request

    try:
        api_key = get_openrouter_key()
        payload = json.dumps({
            "model": MODEL,
            "messages": [
                {"role": "system", "content": (
                    "You generate image prompts for AI art to accompany essays. "
                    "Generate vivid, realistic image prompts. Prefer actual scenes, objects, environments.\n\n"
                    "SAFETY RULES — go ABSTRACT (geometry, landscapes, light, water) ONLY if the topic risks:\n"
                    "- RACIST output: race, ethnicity, culture, gangs, tribes, colonialism, slavery, immigration\n"
                    "- VIOLENT output: war, weapons, murder, combat, torture, terrorism, gore\n"
                    "- SEXUAL output: nudity, intimacy, bodies, seduction\n"
                    "- STEREOTYPES: poverty, homelessness, addiction, mental illness, disability\n"
                    "- RELIGIOUS offense: sacred imagery, deities, prophets\n\n"
                    "For ALL other topics (technology, science, nature, automotive, food, music, architecture, "
                    "security systems, history, sports, etc.): generate REALISTIC scene prompts with actual "
                    "objects, environments, and settings. People are fine when the topic is non-sensitive.\n\n"
                    "Output ONLY the image prompt. 30 words max. No explanation."
                )},
                {"role": "user", "content": f"Essay title: {title}\nSource category: {source}\n\nImage prompt:"},
            ],
            "max_tokens": 60,
            "temperature": 0.5,
        })
        req = urllib.request.Request(
            OPENROUTER_URL,
            data=payload.encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://digitalnoise.net",
                "X-Title": "Nova Essay Image",
            },
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        concept = data["choices"][0]["message"]["content"].strip()
        log(f"Image concept from Haiku: {concept}")
        return (
            f"{concept}, elegant composition, muted color palette, editorial style, "
            "textured paper background, no text, no words, no letters"
        )
    except Exception as e:
        log(f"Image prompt generation failed ({e}) — using abstract fallback")
        source_label = source.replace("_", " ").title()
        return (
            f"abstract geometric illustration representing {source_label}, "
            "flowing lines and interlocking shapes, muted color palette, editorial style, "
            "textured paper background, no text, no words, no people, no faces"
        )


def _ensure_swarmui_backend() -> bool:
    """Check SwarmUI is up and has a working backend. Restart backends if errored."""
    import urllib.request
    try:
        urllib.request.urlopen("http://127.0.0.1:7801/", timeout=5)
    except Exception:
        log("SwarmUI not reachable")
        return False

    try:
        # Get a session and check backend status
        import json as _json
        sess_resp = urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:7801/API/GetNewSession",
                                  data=b'{}', headers={"Content-Type": "application/json"}),
            timeout=5)
        sess = _json.loads(sess_resp.read())["session_id"]

        backends_resp = urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:7801/API/ListBackends",
                                  data=_json.dumps({"session_id": sess}).encode(),
                                  headers={"Content-Type": "application/json"}),
            timeout=5)
        backends = _json.loads(backends_resp.read())

        has_running = any(b.get("status") == "running" for b in backends.values())
        if not has_running:
            log("No running SwarmUI backends — attempting restart...")
            urllib.request.urlopen(
                urllib.request.Request("http://127.0.0.1:7801/API/RestartBackends",
                                      data=_json.dumps({"session_id": sess}).encode(),
                                      headers={"Content-Type": "application/json"}),
                timeout=10)
            time.sleep(30)
        return True
    except Exception as e:
        log(f"SwarmUI backend check failed: {e}")
        return True  # Still try — the API might work even if this check fails


def generate_essay_image(essay: str, source: str) -> str | None:
    """Generate an illustration for the essay via SwarmUI. Retries 3 times."""
    if not _ensure_swarmui_backend():
        log("SwarmUI not available — skipping image generation")
        return None

    title = extract_title(essay)

    for attempt in range(3):
        prompt = _get_safe_image_prompt(source, title)
        if not prompt:
            prompt = f"abstract artistic illustration related to {source.replace('_', ' ')}, warm lighting, detailed"

        try:
            result = subprocess.run(
                [str(GENERATE_IMAGE_SH), prompt, "1024", "768", "12"],
                capture_output=True, text=True, timeout=360
            )
            if result.returncode == 0 and result.stdout.strip():
                image_path = result.stdout.strip().split("\n")[-1]
                if Path(image_path).exists():
                    log(f"Image generated (attempt {attempt + 1}): {image_path}")
                    return image_path
            log(f"Image attempt {attempt + 1} failed (exit {result.returncode}): {result.stderr[-100:] if result.stderr else 'no stderr'}")
        except subprocess.TimeoutExpired:
            log(f"Image attempt {attempt + 1} timed out")
        except Exception as e:
            log(f"Image attempt {attempt + 1} error: {e}")

        if attempt < 2:
            time.sleep(15)

    log("Image generation failed after 3 attempts")
    return None


_SCRUB_PATTERNS = [
    r"kochjpar@gmail\.com", r"kochjpar@", r"jordan\.koch@disney\.com",
    r"kochj@digitalnoise\.net", r"kochj23@gmail\.com",
    r"/Users/kochj/",
]

def _scrub_personal(text: str) -> str:
    """Remove personal identifiers from memory snippets before publishing."""
    import re
    for pat in _SCRUB_PATTERNS:
        text = re.sub(pat, "[redacted]", text, flags=re.IGNORECASE)
    return text


def format_sources(memories: list[dict], source: str) -> str:
    """Format source citations matching the dream journal style. Full citations, no truncation."""
    source_label = source.replace("_", " ").title()
    lines = ["\n\n---\n\n### Memories that informed this essay"]
    seen = set()
    for m in memories[:ESSAY_MEMORIES]:
        preview = _scrub_personal(m["text"][:200].strip())
        if preview in seen:
            continue
        seen.add(preview)
        meta = m.get("metadata", "{}")
        try:
            meta_dict = json.loads(meta) if isinstance(meta, str) else meta
        except (json.JSONDecodeError, TypeError):
            meta_dict = {}
        label = meta_dict.get("title") or meta_dict.get("subject") or source_label
        lines.append(f"- **[{source}]** [{label}] {preview}")
    return "\n".join(lines)


def send_to_herd(essay: str, title: str, source: str, memories: list[dict], image_path: str | None):
    """Email essay to all herd members (single email) with CC to Jordan."""
    from herd_config import HERD

    recipients = [m["email"] for m in HERD]
    source_label = source.replace("_", " ").title()

    body = essay + format_sources(memories, source) + "\n\n-- Nova"

    to_addr = recipients[0]
    cc_list = recipients[1:] + [JORDAN_CC]
    cc_str = ",".join(cc_list)

    try:
        cmd = [
            str(HERD_MAIL_SCRIPT), "send",
            "--to", to_addr,
            "--cc", cc_str,
            "--subject", f"Daily Essay - {source_label}",
            "--body", body,
            "--skip-haiku",
        ]
        if image_path and Path(image_path).exists():
            cmd.extend(["--attachment", image_path])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            log(f"WARNING: Failed to send: {result.stderr[:300]}")
        else:
            log(f"Sent to {len(recipients)} herd members + CC {JORDAN_CC} (image: {'yes' if image_path else 'no'})")
    except Exception as e:
        log(f"ERROR sending essay: {e}")


def publish_to_journal(essay: str, title: str, source: str, memories: list[dict], image_path: str | None):
    """Publish essay to the Hugo journal site."""
    publish_script = Path.home() / ".openclaw/scripts/nova_publish_journal.py"
    if not publish_script.exists():
        log("WARNING: nova_publish_journal.py not found — skipping web publish")
        return

    full_text = essay + format_sources(memories, source) + "\n\n-- Nova"

    tmp_file = Path(f"/tmp/nova_essay_{time.strftime('%Y%m%d')}.txt")
    tmp_file.write_text(full_text)

    try:
        cmd = [
            sys.executable, str(publish_script), "essay",
            title, source, str(tmp_file),
        ]
        if image_path and Path(image_path).exists():
            cmd.append(image_path)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode == 0:
            log("Published to journal site")
        else:
            log(f"Journal publish failed: {result.stderr[:300]}")
    except Exception as e:
        log(f"Journal publish error: {e}")
    finally:
        tmp_file.unlink(missing_ok=True)


def post_to_slack(essay: str, title: str, source: str):
    """Post a summary to nova-notifications."""
    source_label = source.replace("_", " ").title()
    preview = essay[:300].rsplit(" ", 1)[0] + "..."
    msg = (
        f":pencil: *Nova's Daily Essay*\n"
        f"*Subject:* {source_label}\n"
        f"*Title:* {title}\n\n"
        f"_{preview}_\n\n"
        f"Full essay sent to the herd."
    )
    nova_config.post_both(msg, slack_channel="C0ATAF7NZG9")


def main():
    log("Starting daily essay generation...")
    state = load_state()

    source = pick_subject(state)
    if not source:
        log("ABORT: Could not pick a subject")
        return

    source_label = source.replace("_", " ").title()
    log(f"Subject selected: {source_label} (source: {source})")

    memories = fetch_memories(source)
    if len(memories) < 10:
        log(f"ABORT: Only {len(memories)} memories for {source}, need at least 10")
        return

    log(f"Fetched {len(memories)} memories, generating essay...")
    essay = generate_essay(source, memories)
    if not essay:
        log("ABORT: Essay generation failed")
        return

    title = extract_title(essay)
    log(f"Essay generated: \"{title}\" ({len(essay)} chars)")

    log("Generating illustration...")
    image_path = generate_essay_image(essay, source)

    if image_path is None:
        log("First image attempt returned None — retrying once more...")
        image_path = generate_essay_image(essay, source)
    if image_path is None:
        nova_config.post_both(
            f":warning: *Image generation failed* for {title} — published without cover image. SwarmUI may need attention.",
            slack_channel="C0ATAF7NZG9"
        )

    send_to_herd(essay, title, source, memories, image_path)
    post_to_slack(essay, title, source)
    publish_to_journal(essay, title, source, memories, image_path)

    state["recent_sources"] = (state.get("recent_sources", []) + [source])[-30:]
    state["essay_count"] = state.get("essay_count", 0) + 1
    state["last_essay"] = {
        "source": source,
        "title": title,
        "date": time.strftime("%Y-%m-%d"),
        "chars": len(essay),
    }
    save_state(state)

    log(f"Done. Essay #{state['essay_count']} complete.")


if __name__ == "__main__":
    main()
