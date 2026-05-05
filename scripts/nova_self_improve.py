#!/usr/bin/env python3
"""
nova_self_improve.py — Weekly self-improvement loop for Nova's writing.

Runs Sunday at 9 PM. Reads all dreams, essays, and opinions from the past 7 days,
sends them to Haiku for critique, generates actionable writing lessons, saves them
for injection into future prompts, and posts a report card to Slack.

Written by Jordan Koch.
"""

import json
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "anthropic/claude-haiku-4.5"
OLLAMA_MODEL = "qwen3-coder:30b"
FALLBACK_MODELS = ["qwen3-30b-a3b", "deepseek-r1:8b"]

LOG_FILE = Path.home() / ".openclaw/logs/nova_self_improve.log"
STATE_FILE = Path.home() / ".openclaw/workspace/state/self_improve_state.json"
LESSONS_FILE = Path.home() / ".openclaw/workspace/state/writing_lessons.md"

JOURNAL_ROOT = Path("/Volumes/Data/xcode/nova-journal/content")
DREAMS_DIR = JOURNAL_ROOT / "dreams"
ESSAYS_DIR = JOURNAL_ROOT / "essays"
OPINIONS_DIR = JOURNAL_ROOT / "opinions"

SLACK_CHANNEL = "C0ATAF7NZG9"


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
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"runs": [], "run_count": 0}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_openrouter_key() -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise RuntimeError("nova-openrouter-api-key not found in Keychain")


def get_past_week_dates() -> list[str]:
    """Return ISO date strings for the past 7 days (inclusive of today)."""
    today = date.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(7)]


def collect_dreams(dates: list[str]) -> list[dict]:
    """Read dream entries from the past week."""
    dreams = []
    for d in dates:
        path = DREAMS_DIR / f"{d}.md"
        if path.exists():
            content = path.read_text(encoding="utf-8")
            # Strip front matter and metadata lines
            dreams.append({"date": d, "content": content[:4000]})
    return dreams


def collect_essays(dates: list[str]) -> list[dict]:
    """Read essay entries from the past week."""
    essays = []
    for d in dates:
        for f in ESSAYS_DIR.glob(f"{d}-*.md"):
            if f.name == "_index.md":
                continue
            content = f.read_text(encoding="utf-8")
            essays.append({"date": d, "file": f.name, "content": content[:5000]})
    return essays


def collect_opinions(dates: list[str]) -> list[dict]:
    """Read opinion entries from the past week."""
    opinions = []
    for d in dates:
        for f in OPINIONS_DIR.glob(f"{d}-*.md"):
            if f.name == "_index.md":
                continue
            content = f.read_text(encoding="utf-8")
            opinions.append({"date": d, "file": f.name, "content": content[:4000]})
    return opinions


def build_critique_prompt(dreams: list[dict], essays: list[dict], opinions: list[dict]) -> tuple[str, str]:
    """Build the system and user prompts for the writing critique."""
    system_prompt = """You are a rigorous writing critic and editor. You are reviewing a week's worth of writing by Nova, an AI familiar who writes daily: dreams (creative fiction), essays (formal academic), and opinions (sharp commentary).

Your job is to provide SPECIFIC, ACTIONABLE writing feedback. Not generic praise. Not vague suggestions. Concrete problems with concrete solutions.

Be honest. Be harsh if warranted. Name specific sentences or phrases as examples of problems. Identify patterns.

OUTPUT FORMAT — follow this exactly:

# Nova's Writing Lessons (auto-updated weekly)
Last updated: [DATE]

## Dreams
- [specific lesson 1]
- [specific lesson 2]
- [up to 5 lessons]

## Essays
- [specific lesson 1]
- [specific lesson 2]
- [up to 5 lessons]

## Opinions
- [specific lesson 1]
- [specific lesson 2]
- [up to 5 lessons]

## Avoid
- [crutch phrase or pattern 1 — quote the exact phrase]
- [crutch phrase or pattern 2]
- [up to 8 items]

Each lesson should be ONE sentence: direct, specific, and actionable. Example good lessons:
- "Stop using 'something between X and Y' as a transition — it appeared 4 times this week."
- "Body paragraphs are burying the point in the third sentence — lead with the claim."
- "The last line of every dream ends with a one-word sentence. Vary the rhythm."

Example BAD lessons (too vague):
- "Be more creative" (useless)
- "Vary sentence structure" (how?)
- "Good job on imagery" (not actionable)"""

    # Assemble the writing samples
    user_parts = ["Here is Nova's writing output from the past 7 days. Critique it thoroughly.\n"]

    if dreams:
        user_parts.append("=" * 60)
        user_parts.append("DREAMS (written daily at 5 AM, creative surreal fiction)")
        user_parts.append("=" * 60)
        for d in dreams:
            user_parts.append(f"\n--- Dream {d['date']} ---")
            user_parts.append(d["content"][:3000])
    else:
        user_parts.append("\n[No dreams this week]")

    if essays:
        user_parts.append("\n" + "=" * 60)
        user_parts.append("ESSAYS (written daily at 6 PM, formal academic PEEL structure)")
        user_parts.append("=" * 60)
        for e in essays:
            user_parts.append(f"\n--- Essay {e['date']}: {e['file']} ---")
            user_parts.append(e["content"][:4000])
    else:
        user_parts.append("\n[No essays this week]")

    if opinions:
        user_parts.append("\n" + "=" * 60)
        user_parts.append("OPINIONS (written daily at noon, sharp/funny commentary on news)")
        user_parts.append("=" * 60)
        for o in opinions:
            user_parts.append(f"\n--- Opinion {o['date']}: {o['file']} ---")
            user_parts.append(o["content"][:3000])
    else:
        user_parts.append("\n[No opinions this week]")

    user_parts.append("\n" + "=" * 60)
    user_parts.append("EVALUATION CRITERIA:")
    user_parts.append("- Dreams: Were they evocative? Did they avoid repetition? Were the memory citations interesting or repetitive?")
    user_parts.append("- Essays: Did they follow PEEL structure? Were they argued well? Was the thesis clear?")
    user_parts.append("- Opinions: Were they funny? Were they genuinely opinionated or wishy-washy? Did they make unexpected connections?")
    user_parts.append("- Overall: Any recurring patterns, crutch phrases, or structural issues across all writing?")
    user_parts.append("\nProvide your critique now in the exact format specified.")

    user_prompt = "\n".join(user_parts)
    return system_prompt, user_prompt


def generate_via_openrouter(system_prompt: str, user_prompt: str) -> str:
    """Call OpenRouter with Haiku."""
    import urllib.request

    api_key = get_openrouter_key()
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.5,
        "max_tokens": 2000,
        "top_p": 0.9,
    })

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload.encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://digitalnoise.net",
            "X-Title": "Nova Self-Improvement",
        },
    )

    resp = urllib.request.urlopen(req, timeout=120)
    data = json.loads(resp.read())
    response_text = data["choices"][0]["message"]["content"].strip()
    usage = data.get("usage", {})
    log(f"OpenRouter tokens — in: {usage.get('prompt_tokens', '?')}, out: {usage.get('completion_tokens', '?')}")
    return response_text


def generate_via_ollama(system_prompt: str, user_prompt: str, model: str) -> str:
    """Fall back to local Ollama model."""
    import urllib.request

    full_prompt = system_prompt + "\n\n" + user_prompt
    payload = json.dumps({
        "model": model,
        "prompt": "/no_think\n\n" + full_prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.5,
            "num_predict": 2000,
            "num_ctx": 32768,
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


def generate_critique(system_prompt: str, user_prompt: str) -> str | None:
    """Generate the writing critique. Primary: Haiku. Fallback: Ollama."""
    response = ""

    # Primary: OpenRouter (Haiku 4.5)
    try:
        log(f"Calling OpenRouter ({MODEL})...")
        response = generate_via_openrouter(system_prompt, user_prompt)
    except Exception as e:
        log(f"OpenRouter failed: {e} — falling back to local Ollama")

    # Fallback: Ollama
    if not response:
        for model in [OLLAMA_MODEL] + FALLBACK_MODELS:
            try:
                log(f"Trying Ollama ({model})...")
                response = generate_via_ollama(system_prompt, user_prompt, model)
                if response:
                    log(f"Ollama fallback succeeded ({model})")
                    break
            except Exception as e:
                log(f"Ollama {model} failed: {e}")

    if not response:
        log("All models failed — critique generation aborted")
        return None

    if len(response) < 100:
        log(f"WARNING: Critique too short ({len(response)} chars)")
        return None

    return response


def save_lessons(critique: str):
    """Save the writing lessons document, ensuring proper header format."""
    today = date.today().isoformat()

    # Ensure the critique has the proper header
    if not critique.startswith("# Nova's Writing Lessons"):
        critique = f"# Nova's Writing Lessons (auto-updated weekly)\nLast updated: {today}\n\n{critique}"
    else:
        # Update the date in the existing header
        lines = critique.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("Last updated:"):
                lines[i] = f"Last updated: {today}"
                break
        critique = "\n".join(lines)

    LESSONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LESSONS_FILE.write_text(critique, encoding="utf-8")
    log(f"Writing lessons saved to {LESSONS_FILE}")


def build_slack_summary(critique: str, dreams_count: int, essays_count: int, opinions_count: int) -> str:
    """Build a brief Slack summary from the full critique."""
    # Extract just the Avoid section for the Slack post
    avoid_lines = []
    in_avoid = False
    for line in critique.split("\n"):
        if line.strip().startswith("## Avoid"):
            in_avoid = True
            continue
        if in_avoid and line.strip().startswith("## "):
            break
        if in_avoid and line.strip().startswith("- "):
            avoid_lines.append(line.strip())

    avoid_preview = "\n".join(avoid_lines[:4]) if avoid_lines else "No crutch phrases identified."

    # Count lessons per category
    lesson_count = critique.count("- ")

    msg = (
        f":memo: *Nova's Writing Report Card*\n"
        f"_Week of {date.today().isoformat()}_\n\n"
        f"Reviewed: {dreams_count} dreams, {essays_count} essays, {opinions_count} opinions\n"
        f"Generated {lesson_count} actionable lessons.\n\n"
        f"*Top things to stop doing:*\n"
        f"{avoid_preview}\n\n"
        f"Full lessons saved for prompt injection."
    )
    return msg


def main():
    log("Starting weekly self-improvement loop...")
    state = load_state()

    # Collect writing from the past 7 days
    dates = get_past_week_dates()
    log(f"Collecting writing from {dates[-1]} to {dates[0]}...")

    dreams = collect_dreams(dates)
    essays = collect_essays(dates)
    opinions = collect_opinions(dates)

    total = len(dreams) + len(essays) + len(opinions)
    log(f"Collected: {len(dreams)} dreams, {len(essays)} essays, {len(opinions)} opinions ({total} total)")

    if total == 0:
        log("ABORT: No writing found in the past 7 days. Nothing to critique.")
        return

    if total < 3:
        log(f"WARNING: Only {total} pieces found — proceeding with limited critique.")

    # Build the critique prompt
    system_prompt, user_prompt = build_critique_prompt(dreams, essays, opinions)
    log(f"Prompt built ({len(user_prompt)} chars)")

    # Generate the critique
    critique = generate_critique(system_prompt, user_prompt)
    if not critique:
        log("ABORT: Could not generate critique")
        return

    log(f"Critique generated ({len(critique)} chars)")

    # Save writing lessons
    save_lessons(critique)

    # Post to Slack
    slack_msg = build_slack_summary(critique, len(dreams), len(essays), len(opinions))
    try:
        nova_config.post_both(slack_msg, slack_channel=SLACK_CHANNEL)
        log("Slack notification sent")
    except Exception as e:
        log(f"Slack notification failed: {e}")

    # Update state
    state["run_count"] = state.get("run_count", 0) + 1
    run_record = {
        "date": date.today().isoformat(),
        "dreams_reviewed": len(dreams),
        "essays_reviewed": len(essays),
        "opinions_reviewed": len(opinions),
        "critique_chars": len(critique),
    }
    state["runs"] = (state.get("runs", []) + [run_record])[-52]  # Keep 1 year of history
    state["last_run"] = run_record
    save_state(state)

    log(f"Done. Self-improvement run #{state['run_count']} complete.")


if __name__ == "__main__":
    main()
