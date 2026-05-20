#!/usr/bin/env python3
"""
nova_career_narrative.py — Generate Jordan's career narrative from primary sources.

Queries actual memories from the vector DB for each career era, then synthesizes
them into coherent narrative prose via Ollama. Caches the full narrative and
supports single-era generation.

CLI:
  python3 nova_career_narrative.py              — full career narrative
  python3 nova_career_narrative.py --era litton — single era
  python3 nova_career_narrative.py --post       — post result to Slack
  python3 nova_career_narrative.py --refresh    — regenerate even if cache is fresh

Tool mode:
  from nova_career_narrative import generate_career_narrative
  result = asyncio.run(generate_career_narrative(era="litton"))

Written by Jordan Koch.
"""

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config

# ── Configuration ─────────────────────────────────────────────────────────────

MEMORY_SERVER = "http://192.168.1.6:18790"
OLLAMA_URL = "http://192.168.1.6:11434/api/chat"
OLLAMA_MODEL = "qwen3-coder:30b"
CACHE_FILE = Path.home() / ".openclaw/workspace/state/career_narrative.json"
CACHE_MAX_AGE = timedelta(days=7)

# ── Career Eras ───────────────────────────────────────────────────────────────

CAREER_ERAS = {
    "northstar": {
        "title": "North Star Computers — The Beginning",
        "period": "Late 1970s – Early 1980s",
        "vectors": ["computing_northstar", "computing_history_personal"],
        "queries": [
            "North Star Horizon computer support",
            "North Star Computers CP/M S-100",
            "early personal computer support technician",
        ],
        "context": (
            "Jordan's first technology job. Supporting North Star Horizon systems — "
            "S-100 bus, CP/M, 5.25-inch floppies, hard-sectored disks. The era of "
            "hobbyist computing becoming small business computing."
        ),
    },
    "prg_aviation": {
        "title": "PRG Aviation Systems — Y2K and DataFlex",
        "period": "~1998 – 2000",
        "vectors": ["computing_history_personal"],
        "queries": [
            "PRG Aviation Systems FBO fuel management",
            "Y2K programming DataFlex remediation",
            "fixed-base operator aviation software",
        ],
        "context": (
            "Second career role. Y2K-era programming in DataFlex for PRG Aviation "
            "Systems. Building and maintaining FBO (Fixed Base Operator) fuel "
            "management software for small airports."
        ),
    },
    "litton": {
        "title": "Litton Guidance & Control Systems — First UNIX SA",
        "period": "~2000 – 2002",
        "vectors": ["computing_military", "computing_sun"],
        "queries": [
            "Litton Guidance Control Systems UNIX administrator",
            "Sun Microsystems workstation Solaris administration",
            "military defense contractor systems administration",
        ],
        "context": (
            "First UNIX Systems Administrator role. Litton Guidance & Control Systems "
            "(later Northrop Grumman). Managing Sun workstations running Solaris for "
            "military/defense guidance systems development."
        ),
    },
    "disney": {
        "title": "The Walt Disney Company — SRE Leadership",
        "period": "2002 – Present",
        "vectors": ["sre_core", "sre_history", "sre_scaling", "sre_infrastructure"],
        "queries": [
            "site reliability engineering large scale infrastructure",
            "SRE team leadership incident management",
            "infrastructure scaling reliability practices",
            "senior manager operations engineering",
        ],
        "context": (
            "25+ year tenure. Progressed from systems administrator to Senior Manager "
            "of Site Reliability Engineering. Manages large-scale infrastructure, "
            "incident response, and reliability practices for one of the world's "
            "largest media companies."
        ),
    },
}

ERA_ORDER = ["northstar", "prg_aviation", "litton", "disney"]


# ── Memory Recall ─────────────────────────────────────────────────────────────

async def recall_memories(client, query: str, source: str = None, n: int = 10) -> list[dict]:
    """Query the memory server's /recall endpoint."""
    params = {"q": query, "n": n}
    if source:
        params["source"] = source
    try:
        resp = await client.get(f"{MEMORY_SERVER}/recall", params=params, timeout=15.0)
        if resp.status_code == 200:
            data = resp.json()
            memories = data.get("memories", data) if isinstance(data, dict) else data
            return memories if isinstance(memories, list) else []
    except Exception as e:
        print(f"  [recall] Failed ({source}/{query[:40]}): {e}", file=sys.stderr)
    return []


async def gather_era_memories(client, era_key: str) -> list[dict]:
    """Pull all relevant memories for a career era from multiple vectors/queries."""
    era = CAREER_ERAS[era_key]
    tasks = []
    for vector in era["vectors"]:
        for query in era["queries"]:
            tasks.append(recall_memories(client, query, source=vector, n=8))
    # Also do a broad query without source filter for serendipity
    for query in era["queries"][:2]:
        tasks.append(recall_memories(client, query, n=5))

    results = await asyncio.gather(*tasks)
    # Deduplicate by text content
    seen = set()
    unique = []
    for batch in results:
        for mem in batch:
            text = mem.get("text", "").strip()
            if text and text not in seen:
                seen.add(text)
                unique.append(mem)
    return unique


# ── LLM Synthesis ─────────────────────────────────────────────────────────────

async def synthesize_narrative(client, era_key: str, memories: list[dict]) -> str:
    """Use Ollama to synthesize raw memories into narrative prose."""
    era = CAREER_ERAS[era_key]

    # Build the memory excerpts block
    excerpts = []
    for i, mem in enumerate(memories[:20], 1):
        text = mem.get("text", "")[:500]
        source = mem.get("source", "unknown")
        excerpts.append(f"[{i}] (source: {source}) {text}")
    excerpts_block = "\n\n".join(excerpts)

    system_prompt = (
        "You are writing a career narrative for Jordan Koch based on actual primary "
        "source documents from his personal archive. Write in third person, past tense. "
        "Be factual and grounded — only include details supported by the provided excerpts. "
        "Quote or reference specific details from the sources when possible. "
        "Write 2-4 paragraphs of narrative prose. Include the era's time period. "
        "Do not invent facts not in the sources. If sources are sparse, note what is known "
        "and acknowledge gaps. Do not use flowery language or cliches."
    )

    user_prompt = (
        f"Career Era: {era['title']}\n"
        f"Period: {era['period']}\n"
        f"Context: {era['context']}\n\n"
        f"Primary source excerpts from Jordan's archive:\n\n{excerpts_block}\n\n"
        f"Write the narrative section for this era of Jordan's career."
    )

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "/no_think\n\n" + user_prompt},
        ],
        "stream": False,
        "options": {
            "temperature": 0.7,
            "num_predict": 1500,
            "num_ctx": 16384,
        },
    }

    try:
        resp = await client.post(OLLAMA_URL, json=payload, timeout=300.0)
        if resp.status_code == 200:
            data = resp.json()
            text = data.get("message", {}).get("content", "").strip()
            # Strip thinking blocks if present
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            return text
    except Exception as e:
        print(f"  [ollama] Synthesis failed for {era_key}: {e}", file=sys.stderr)
    return f"[Narrative generation failed for {era['title']}]"


# ── Main Generation ───────────────────────────────────────────────────────────

async def generate_era(client, era_key: str) -> dict:
    """Generate narrative for a single career era."""
    era = CAREER_ERAS[era_key]
    print(f"  Recalling memories for: {era['title']}...", file=sys.stderr)
    memories = await gather_era_memories(client, era_key)
    print(f"    Found {len(memories)} unique memories", file=sys.stderr)

    if not memories:
        return {
            "era": era_key,
            "title": era["title"],
            "period": era["period"],
            "narrative": f"No primary source memories found for this era in the vector DB.",
            "source_count": 0,
            "sources_used": [],
        }

    print(f"  Synthesizing narrative...", file=sys.stderr)
    narrative = await synthesize_narrative(client, era_key, memories)
    sources_used = list({m.get("source", "unknown") for m in memories})

    return {
        "era": era_key,
        "title": era["title"],
        "period": era["period"],
        "narrative": narrative,
        "source_count": len(memories),
        "sources_used": sources_used,
    }


async def generate_career_narrative(era: str = None, refresh: bool = False) -> dict:
    """Generate the full career narrative or a single era.

    Args:
        era: If set, generate only this era (e.g. "litton"). None = all eras.
        refresh: If True, ignore cache and regenerate.

    Returns:
        Dict with 'eras' list, 'generated_at' timestamp, 'full_text' string.
    """
    # Check cache for full narrative
    if not era and not refresh and CACHE_FILE.exists():
        try:
            cached = json.loads(CACHE_FILE.read_text())
            generated = datetime.fromisoformat(cached.get("generated_at", "2000-01-01"))
            if datetime.now() - generated < CACHE_MAX_AGE:
                print("  Using cached narrative (less than 7 days old)", file=sys.stderr)
                return cached
        except (json.JSONDecodeError, ValueError):
            pass

    import httpx
    async with httpx.AsyncClient() as client:
        if era:
            if era not in CAREER_ERAS:
                return {"error": f"Unknown era '{era}'. Valid: {', '.join(ERA_ORDER)}"}
            era_result = await generate_era(client, era)
            return {
                "eras": [era_result],
                "generated_at": datetime.now().isoformat(),
                "full_text": f"## {era_result['title']} ({era_result['period']})\n\n{era_result['narrative']}",
            }
        else:
            # Generate all eras sequentially (to avoid overloading Ollama)
            eras_output = []
            for era_key in ERA_ORDER:
                result = await generate_era(client, era_key)
                eras_output.append(result)

            # Compose full text
            sections = []
            for e in eras_output:
                sections.append(f"## {e['title']} ({e['period']})\n\n{e['narrative']}")
            full_text = (
                "# Jordan Koch — Career Narrative\n"
                "*Generated from primary sources in Nova's vector memory*\n\n"
                + "\n\n---\n\n".join(sections)
            )

            output = {
                "eras": eras_output,
                "generated_at": datetime.now().isoformat(),
                "full_text": full_text,
            }

            # Cache the result
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(output, indent=2))
            print(f"  Cached narrative to {CACHE_FILE}", file=sys.stderr)
            return output


# ── Slack Posting ─────────────────────────────────────────────────────────────

def post_to_slack(text: str) -> None:
    """Post the narrative to Slack (truncated if needed)."""
    # Slack message limit is 4000 chars for rich text
    if len(text) > 3900:
        chunks = [text[i:i+3900] for i in range(0, len(text), 3900)]
        for i, chunk in enumerate(chunks):
            prefix = f"*Career Narrative ({i+1}/{len(chunks)})*\n" if len(chunks) > 1 else ""
            nova_config.post_both(prefix + chunk, slack_channel=nova_config.SLACK_NOTIFY)
            time.sleep(1)
    else:
        nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate Jordan's career narrative from vector DB sources")
    parser.add_argument("--era", type=str, default=None,
                        help=f"Focus on one era: {', '.join(ERA_ORDER)}")
    parser.add_argument("--post", action="store_true",
                        help="Post the result to Slack")
    parser.add_argument("--refresh", action="store_true",
                        help="Regenerate even if cache is fresh")
    parser.add_argument("--json", action="store_true",
                        help="Output raw JSON instead of formatted text")
    args = parser.parse_args()

    print("Nova Career Narrative Generator", file=sys.stderr)
    print("=" * 40, file=sys.stderr)

    result = asyncio.run(generate_career_narrative(era=args.era, refresh=args.refresh))

    if "error" in result:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result["full_text"])

    # Stats
    total_sources = sum(e.get("source_count", 0) for e in result.get("eras", []))
    print(f"\n{'=' * 40}", file=sys.stderr)
    print(f"  Eras covered: {len(result.get('eras', []))}", file=sys.stderr)
    print(f"  Total source memories used: {total_sources}", file=sys.stderr)
    print(f"  Generated: {result.get('generated_at', 'unknown')}", file=sys.stderr)

    if args.post:
        print("  Posting to Slack...", file=sys.stderr)
        post_to_slack(result["full_text"])
        print("  Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
