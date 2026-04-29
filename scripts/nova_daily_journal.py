#!/usr/bin/env python3

"""
Nova Daily Journal — unified nightly summary posted to Slack at 9 PM PT.

Combines the data-driven daily journal (calendar, infra, security, app health,
local news, dreams, historical tidbits, scheduler health) with the LLM-powered
nightly memory summary (vector recall + Ollama synthesis in Nova's voice).

One Slack message. One script. Runs once at 9pm.

Written by Jordan Koch.
"""

import json
import logging
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.expanduser("~/.openclaw/logs/daily-journal.log")),
        logging.StreamHandler(),
    ],
)

TODAY = date.today().isoformat()
DB = "nova_memories"
STATE_DIR = Path.home() / ".openclaw/workspace/state"
WORKSPACE = Path.home() / ".openclaw/workspace"
MEMORY_DIR = WORKSPACE / "memory"
VECTOR_URL = "http://127.0.0.1:18790"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen3-coder:30b"


# ── Postgres helpers ────────────────────────────────────────────────────────


def _query(sql):
    try:
        result = subprocess.run(
            ["psql", "-U", "kochj", "-d", DB, "-tAc", sql],
            capture_output=True, text=True, timeout=10,
        )
        rows = [r for r in result.stdout.strip().split("\n") if r]
        return rows
    except Exception as e:
        logging.warning(f"DB query failed: {e}")
        return []


def _query_field(sql):
    rows = _query(sql)
    return rows[0] if rows else None


def _load_state(name):
    path = STATE_DIR / name
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


# ── Data-driven sections ───────────────────────────────────────────────────


def section_calendar():
    rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'calendar' AND created_at >= '{TODAY}' "
        f"ORDER BY created_at LIMIT 3"
    )
    if not rows:
        return None
    best = max(rows, key=len)
    if "—" in best:
        events_part = best.split("—", 1)[1].strip()
        events = [e.strip() for e in events_part.split(",")]
        lines = [f"• {e}" for e in events if e]
        return "*Today's Calendar:*\n" + "\n".join(lines)
    return f"*Today's Calendar:*\n{best}"


def section_morning_brief():
    rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'morning_brief' AND created_at >= '{TODAY}' "
        f"ORDER BY created_at DESC LIMIT 1"
    )
    if not rows:
        return None
    brief = rows[0]
    if ":" in brief:
        content = brief.split(":", 1)[1].strip()
        parts = []
        if "°F" in content:
            weather = content.split(".")[0]
            parts.append(f"• Weather: {weather.strip()}")
        if "Meetings:" in content:
            meetings = content.split("Meetings:", 1)[1].strip()
            parts.append(f"• Meetings: {meetings.split('.')[0].strip()}")
        if parts:
            return "*Morning Brief:*\n" + "\n".join(parts)
    return None


def section_infrastructure():
    lines = []

    # NAS state
    nas = _load_state("nova_synology_state.json")
    if nas:
        model = nas.get("model", "NAS")
        cpu = nas.get("cpu_pct", "?")
        ram = nas.get("ram_pct", "?")
        vols = nas.get("volumes", "?")
        problems = nas.get("problem_count", 0)
        status = "all clear" if problems == 0 else f"{problems} problem(s)"
        lines.append(f"• NAS ({model}): CPU {cpu}%, RAM {ram}%, {vols} — {status}")

    # Network
    net_rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'infrastructure' AND text LIKE 'Network health%' "
        f"AND created_at >= '{TODAY}' ORDER BY created_at DESC LIMIT 1"
    )
    if net_rows:
        row = net_rows[0]
        if "WAN" in row:
            content = row[row.index("WAN"):]
            lines.append(f"• Network: {content}")
        elif ":" in row:
            content = row.split(":", 1)[1].strip()
            lines.append(f"• Network: {content}")

    # App outages
    app_rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'app_watchdog' AND created_at >= '{TODAY}' "
        f"ORDER BY created_at"
    )
    if app_rows:
        downs = [r for r in app_rows if "went down" in r]
        recoveries = [r for r in app_rows if "recovered" in r]
        if downs:
            apps_affected = set()
            for d in downs:
                app_name = d.split(" went down")[0].strip()
                apps_affected.add(app_name)
            lines.append(
                f"• App outages: {', '.join(sorted(apps_affected))} "
                f"({len(downs)} down, {len(recoveries)} recovered)"
            )
    else:
        lines.append("• Apps: no outages")

    if not lines:
        return None
    return "*Infrastructure:*\n" + "\n".join(lines)


def section_security():
    count = _query_field(
        f"SELECT count(*) FROM memories "
        f"WHERE source = 'security' AND created_at >= '{TODAY}'"
    )
    if not count or count == "0":
        return None

    cam_rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'security' AND created_at >= '{TODAY}'"
    )
    cameras = {}
    for row in cam_rows:
        if "Protect event on " in row:
            cam = row.split("Protect event on ", 1)[1].split(":")[0].strip()
            cameras[cam] = cameras.get(cam, 0) + 1

    lines = [f"• {int(count)} Protect events across {len(cameras)} cameras"]
    if cameras:
        top = sorted(cameras.items(), key=lambda x: -x[1])[:5]
        for cam, n in top:
            lines.append(f"  - {cam}: {n} events")

    return "*Security Cameras:*\n" + "\n".join(lines)


def section_local_news():
    rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source IN ('local', 'burbank') AND created_at >= '{TODAY}' "
        f"ORDER BY created_at LIMIT 10"
    )
    if not rows:
        return None

    highlights = []
    for row in rows:
        first_line = row.split("\n")[0].strip()
        if "Reddit r/" in first_line:
            after_reddit = first_line.split("Reddit ", 1)[1]
            highlights.append(f"• {after_reddit}")
        if len(highlights) >= 5:
            break

    local_count = len([r for r in rows if "r/glendale" in r.lower()])
    burbank_count = len([r for r in rows if "r/burbank" in r.lower()])

    header = f"*Local News ({local_count} Glendale, {burbank_count} Burbank):*"
    return header + "\n" + "\n".join(highlights)


def section_dream():
    rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'dream' AND created_at >= '{TODAY}' "
        f"ORDER BY created_at LIMIT 1"
    )
    if not rows:
        return None
    text = rows[0]
    if ":" in text:
        text = text.split(":", 1)[1].strip()
    if len(text) > 200:
        text = text[:200].rsplit(" ", 1)[0] + "..."
    return f"*Dream Journal:*\n_{text}_"


def section_this_day():
    rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'history' AND created_at >= '{TODAY}' "
        f"ORDER BY created_at LIMIT 3"
    )
    if not rows:
        return None
    lines = []
    for row in rows:
        if "On this day" in row:
            after = row.split("),", 1)
            if len(after) == 2:
                year_event = after[1].strip()
                if len(year_event) > 120:
                    year_event = year_event[:120].rsplit(" ", 1)[0] + "..."
                lines.append(f"• {year_event}")
        elif "Born on" in row:
            if len(row) > 120:
                row = row[:120].rsplit(" ", 1)[0] + "..."
            lines.append(f"• {row}")
        if len(lines) >= 2:
            break
    if not lines:
        return None
    return "*On This Day:*\n" + "\n".join(lines)


def section_scheduler_health():
    state = _load_state("../config/scheduler_state.json")
    if not state:
        return None

    tasks = state.get("tasks", {})
    failures = {}
    for name, info in tasks.items():
        consec = info.get("consecutive_failures", 0)
        if consec >= 3:
            failures[name] = consec

    total_runs = sum(t.get("run_count", 0) for t in tasks.values())

    if failures:
        lines = [f"• Scheduler: {total_runs} total runs, {len(failures)} tasks struggling:"]
        for name, count in sorted(failures.items(), key=lambda x: -x[1]):
            lines.append(f"  - {name}: {count} consecutive failures")
        return "*Scheduler:*\n" + "\n".join(lines)
    else:
        return f"*Scheduler:*\n• {total_runs} total runs today, all tasks healthy"


def generate_journal_sections():
    """Build the data-driven portion of the journal. Returns assembled text."""
    sections = [
        section_morning_brief(),
        section_calendar(),
        section_infrastructure(),
        section_security(),
        section_scheduler_health(),
        section_local_news(),
        section_dream(),
        section_this_day(),
    ]

    parts = [s for s in sections if s]

    if not parts:
        parts.append("_Quiet day — no significant events recorded._")

    return "\n\n".join(parts)


# ── Vector memory gathering (from nightly_memory_summary) ──────────────────


def get_memory_stats():
    """Get vector memory health stats."""
    try:
        req = urllib.request.Request(f"{VECTOR_URL}/health", headers={})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        logging.warning(f"Memory stats error: {e}")
    return {}


def vector_recall(query, n=8, source=None):
    """Semantic search against vector memory."""
    try:
        params = f"q={urllib.parse.quote(query)}&n={n}"
        if source:
            params += f"&source={source}"
        req = urllib.request.Request(f"{VECTOR_URL}/recall?{params}")
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        return [m.get("text", "")[:300] for m in data.get("memories", [])
                if m.get("score", 0) >= 0.4]
    except Exception as e:
        logging.warning(f"Recall error for '{query[:40]}': {e}")
        return []


def get_today_memory_file():
    """Read today's daily memory file."""
    today_file = MEMORY_DIR / f"{TODAY}.md"
    if today_file.exists():
        try:
            return today_file.read_text(encoding="utf-8")[:4000]
        except Exception as e:
            logging.warning(f"Error reading today's memory: {e}")
    return ""


def gather_today_learnings():
    """
    Gather everything Nova learned, noticed, and processed today from
    the daily memory file and vector memory. Returns a raw text block
    for the LLM to synthesize.
    """
    parts = []

    # Daily memory file
    daily = get_today_memory_file()
    if daily.strip():
        parts.append("[Today's daily memory log]\n" + daily)

    # Vector memory — pull today's ingested content across all sources
    queries = [
        ("GitHub activity commits issues stars", "github"),
        ("email communication action items", "email"),
        ("meeting summary notes action items", "meeting"),
        ("Burbank subreddit news discussion", "nightly"),
        ("home status HomeKit accessories", "homekit"),
        ("Slack messages conversation", None),
        ("memory ingested knowledge learned", None),
    ]
    recalled = []
    seen = set()
    for q, src in queries:
        for chunk in vector_recall(q, n=5, source=src):
            if TODAY in chunk or not recalled:
                key = chunk[:80]
                if key not in seen:
                    seen.add(key)
                    recalled.append(chunk)

    if recalled:
        parts.append("[Recalled from vector memory — today]\n" + "\n---\n".join(recalled[:15]))

    return "\n\n".join(parts)


# ── LLM synthesis ──────────────────────────────────────────────────────────


def synthesize_summary(raw_context):
    """
    Use local Ollama to synthesize a concise, Nova-voiced summary of
    what was learned and noticed today. Returns the summary text.
    """
    if not raw_context.strip():
        return "_Quiet day. Nothing notable crossed my desk._"

    prompt = f"""/no_think

You are Nova, an AI familiar. It's 9pm. Summarize what you learned, noticed, and processed today in 150-250 words.

Rules:
- Write as Nova, first person. Direct and observant. Not a report — a reflection.
- Lead with the most interesting or notable things, not operational stats.
- If Burbank news happened (subreddit, weather, events), mention what stood out.
- If Jordan worked on projects, note what and how it went.
- If emails or meetings happened, capture the gist.
- Compress cron/system activity into one sentence at most.
- End with one line about what's on your mind tonight or what you're watching for tomorrow.
- No headers, no bullet points, no markdown formatting. Just flowing text.
- No filler phrases like "Today was" or "Here's what happened."

Today's data:
{raw_context[:3500]}

Write the summary now:"""

    try:
        payload = {
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {
                "temperature": 0.4,
                "num_predict": 400,
                "num_ctx": 8192,
            }
        }
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            result = json.loads(r.read())
        response = result.get("response", "").strip()

        # Strip thinking blocks if they leak through
        try:
            from nova_strip_thinking import strip_thinking
            response = strip_thinking(response)
        except ImportError:
            pass

        if len(response.split()) < 20:
            logging.warning(f"Very short LLM response ({len(response.split())} words)")
            return response or "_Quiet day. Nothing notable crossed my desk._"

        return response

    except Exception as e:
        logging.error(f"LLM synthesis failed: {e}")
        return _fallback_summary(raw_context)


def _fallback_summary(raw_context):
    """If LLM is down, produce a bare-bones summary from raw data."""
    lines = []
    for line in raw_context.splitlines():
        line = line.strip()
        if line.startswith("•") or line.startswith("\U0001f534") or line.startswith("\U0001f7e1"):
            lines.append(line)
        if "weather" in line.lower() and ("°F" in line or "°C" in line):
            lines.append(line)
    if lines:
        return "LLM unavailable — raw highlights:\n" + "\n".join(lines[:10])
    return "_Quiet day. LLM was unavailable for synthesis._"


# ── Memory storage ─────────────────────────────────────────────────────────


def store_summary_in_memory(summary):
    """Store tonight's summary in vector memory for the dream to use."""
    try:
        subprocess.run(
            [str(Path.home() / ".openclaw/scripts/nova_remember.sh"),
             f"Nova nightly summary {TODAY}: {summary[:500]}", "nightly"],
            timeout=30, capture_output=True
        )
        logging.info("Summary stored in vector memory")
    except Exception as e:
        logging.warning(f"Memory store failed (non-fatal): {e}")


# ── Unified posting ────────────────────────────────────────────────────────


def build_unified_message(journal_text, memory_count, llm_summary):
    """Assemble the single unified Slack message."""
    header = f"*Nova Daily Journal — {TODAY}*"
    separator = "\n────────────────────────────────────────\n"
    memory_header = f"*Nova Nightly Summary — {memory_count} memories indexed*"
    footer = "_— Nova · 9pm_"

    parts = [
        header,
        journal_text,
        separator,
        memory_header,
        llm_summary,
        "",
        footer,
    ]
    return "\n\n".join(parts)


# ── Main ───────────────────────────────────────────────────────────────────


def main():
    logging.info("Starting unified daily journal + nightly summary")

    # Phase 1: Data-driven journal sections
    logging.info("Gathering data-driven sections...")
    journal_text = generate_journal_sections()

    # Phase 2: Vector memory context
    logging.info("Gathering vector memory context...")
    raw_context = gather_today_learnings()
    logging.info(f"Gathered {len(raw_context)} chars of memory context")

    stats = get_memory_stats()
    memory_count = stats.get("count", "unknown")

    # Phase 3: LLM reflection
    logging.info("Synthesizing summary via local LLM (qwen3-coder:30b)...")
    llm_summary = synthesize_summary(raw_context)
    logging.info(f"LLM summary: {len(llm_summary.split())} words")

    # Phase 4: Post unified message
    message = build_unified_message(journal_text, memory_count, llm_summary)
    print(message)
    print()

    try:
        nova_config.post_both(message, slack_channel=nova_config.SLACK_NOTIFY)
        logging.info("Unified journal posted to Slack")
    except Exception as e:
        logging.error(f"Slack post failed: {e}")

    # Phase 5: Store LLM summary in vector memory for dream pickup
    store_summary_in_memory(llm_summary)

    logging.info("Unified daily journal process completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
