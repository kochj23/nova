#!/usr/bin/env python3
"""Nova Shop Assistant — combines Corvette workshop manual with automotive YouTube knowledge.

Usage:
    python3 nova_shop_assistant.py "C5 rear hub torque spec"
    python3 nova_shop_assistant.py --verbose "LS1 oil pressure sensor location"

Tool mode:
    from nova_shop_assistant import ask_shop
    result = await ask_shop("C5 rear hub torque spec")
"""

import argparse
import asyncio
import json
import sys
from typing import Optional

import aiohttp

MEMORY_SERVER = "http://192.168.1.6:18790"
MANUAL_VECTOR = "corvette_workshop_manual"
COMMUNITY_VECTOR = "automotive"
MANUAL_TOP_K = 5
COMMUNITY_TOP_K = 10


async def get_embedding(session: aiohttp.ClientSession, text: str) -> list[float]:
    """Get embedding vector for a query string."""
    async with session.post(f"{MEMORY_SERVER}/embed", json={"text": text}) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data["embedding"]


async def recall(session: aiohttp.ClientSession, query: str, top_k: int, source: str) -> list[dict]:
    """Search a specific vector collection."""
    payload = {"query": query, "top_k": top_k, "source": source}
    async with session.post(f"{MEMORY_SERVER}/recall", json=payload) as resp:
        if resp.status == 200:
            data = await resp.json()
            return data if isinstance(data, list) else data.get("results", [])
        return []


def format_result(result: dict, verbose: bool = False) -> str:
    """Format a single memory result for display."""
    content = result.get("content", result.get("text", ""))
    score = result.get("score", result.get("similarity", 0))
    source = result.get("metadata", {}).get("source", result.get("source", ""))
    title = result.get("metadata", {}).get("title", result.get("title", ""))

    if not verbose and len(content) > 300:
        content = content[:297] + "..."

    header = ""
    if title:
        header = f"  [{title}]"
    elif source:
        header = f"  [{source}]"

    score_str = f" (relevance: {score:.3f})" if score else ""
    return f"  {header}{score_str}\n  {content}\n"


def find_contradictions(manual_results: list[dict], community_results: list[dict]) -> list[str]:
    """Identify potential contradictions between manual and community knowledge."""
    notes = []
    manual_text = " ".join(r.get("content", r.get("text", "")) for r in manual_results).lower()
    for result in community_results:
        text = result.get("content", result.get("text", "")).lower()
        # Flag common contradiction indicators
        if any(phrase in text for phrase in [
            "the manual says", "book says", "spec says",
            "don't follow the manual", "ignore the spec",
            "actually should be", "better than stock",
            "factory spec is wrong", "over-torque", "under-torque"
        ]):
            snippet = result.get("content", result.get("text", ""))[:150]
            title = result.get("metadata", {}).get("title", result.get("title", "unknown"))
            notes.append(f"  [{title}]: {snippet}...")
    return notes


async def ask_shop(query: str, verbose: bool = False) -> str:
    """Main query function — searches both vectors and formats combined answer."""
    async with aiohttp.ClientSession() as session:
        # Search both collections
        manual_results, community_results = await asyncio.gather(
            recall(session, query, MANUAL_TOP_K, MANUAL_VECTOR),
            recall(session, query, COMMUNITY_TOP_K, COMMUNITY_VECTOR),
        )

    # Build output
    sections = []

    if manual_results:
        sections.append("\U0001f4cb From the manual:")
        for r in manual_results:
            sections.append(format_result(r, verbose))
    else:
        sections.append("\U0001f4cb From the manual:\n  No matching entries found in workshop manual.\n")

    if community_results:
        sections.append("\U0001f527 From the community:")
        for r in community_results:
            sections.append(format_result(r, verbose))
    else:
        sections.append("\U0001f527 From the community:\n  No matching automotive content found.\n")

    # Check for contradictions
    if manual_results and community_results:
        contradictions = find_contradictions(manual_results, community_results)
        if contradictions:
            sections.append("⚠️  Community notes that may contradict or supplement the manual:")
            sections.extend(contradictions)

    return "\n".join(sections)


async def main():
    parser = argparse.ArgumentParser(
        description="Nova Shop Assistant — Corvette manual + automotive community knowledge"
    )
    parser.add_argument("query", help="Technical question to search")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show full excerpts")
    args = parser.parse_args()

    if not args.query.strip():
        print("Error: empty query", file=sys.stderr)
        sys.exit(1)

    try:
        result = await ask_shop(args.query, verbose=args.verbose)
        print(f"\n{'='*60}")
        print(f"  Query: {args.query}")
        print(f"{'='*60}\n")
        print(result)
    except aiohttp.ClientError as e:
        print(f"Error connecting to memory server: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
