#!/usr/bin/env python3

"""
Nova Correction Prompt — Pre-Response Correction Lookup

Called by Nova BEFORE responding to check if there are relevant prior
corrections for the current question or topic. Searches vector memory
and the local corrections file, then outputs context that can be
injected into Nova's prompt to avoid repeating past mistakes.

Usage:
    nova_correction_prompt.py --query "the current question or topic"
    nova_correction_prompt.py --query "HomeKit scene names" --limit 3

Output format is plain text suitable for injection into a system prompt.

Written by Jordan Koch.
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

VECTOR_API_BASE = "http://127.0.0.1:18790"
CORRECTIONS_FILE = Path.home() / ".openclaw" / "workspace" / "state" / "corrections.json"


def search_vector_memory(query: str, limit: int = 5) -> list:
    """
    Search Nova's vector memory for corrections related to the query.
    Uses the /recall endpoint with source=correction filter.
    Returns a list of matching correction texts.
    """
    if requests is None:
        print("WARNING: requests library not available; skipping vector memory search.", file=sys.stderr)
        return []

    params = {
        "q": query,
        "n": limit,
        "source": "correction",
    }

    try:
        resp = requests.get(f"{VECTOR_API_BASE}/recall", params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # Handle both list-of-strings and list-of-objects response formats
            results = []
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        results.append(item)
                    elif isinstance(item, dict):
                        text = item.get("text", item.get("content", ""))
                        if text:
                            results.append(text)
            elif isinstance(data, dict):
                # Some endpoints wrap results in a key
                for key in ("results", "memories", "data", "items"):
                    if key in data and isinstance(data[key], list):
                        for item in data[key]:
                            if isinstance(item, str):
                                results.append(item)
                            elif isinstance(item, dict):
                                text = item.get("text", item.get("content", ""))
                                if text:
                                    results.append(text)
                        break
            return results
        else:
            print(
                f"WARNING: Vector memory returned {resp.status_code}: {resp.text}",
                file=sys.stderr,
            )
            return []
    except requests.RequestException as e:
        print(f"WARNING: Could not reach vector memory server: {e}", file=sys.stderr)
        return []


def search_local_corrections(query: str, limit: int = 5) -> list:
    """
    Fallback: search the local corrections JSON file using simple keyword matching.
    Returns correction records whose topic, nova_response, or jordan_correction
    contain any of the query terms.
    """
    if not CORRECTIONS_FILE.exists():
        return []

    try:
        with open(CORRECTIONS_FILE, "r") as f:
            corrections = json.load(f)
    except (json.JSONDecodeError, IOError):
        return []

    if not isinstance(corrections, list):
        return []

    query_lower = query.lower()
    query_terms = set(query_lower.split())

    scored = []
    for c in corrections:
        searchable = " ".join([
            c.get("topic", ""),
            c.get("nova_response", ""),
            c.get("jordan_correction", ""),
        ]).lower()

        # Score by number of matching query terms
        score = sum(1 for term in query_terms if term in searchable)
        if score > 0:
            scored.append((score, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:limit]]


def format_corrections_for_prompt(vector_results: list, local_results: list) -> str:
    """
    Format correction results into a prompt-injectable context block.
    """
    lines = []

    if not vector_results and not local_results:
        return ""

    lines.append("=== PRIOR CORRECTIONS (review before responding) ===")
    lines.append("")

    seen_texts = set()

    # Vector memory results (higher quality — semantic match)
    if vector_results:
        for text in vector_results:
            normalized = text.strip()
            if normalized and normalized not in seen_texts:
                seen_texts.add(normalized)
                lines.append(f"- {normalized}")

    # Local file results (keyword fallback — only add if not already covered)
    if local_results:
        for c in local_results:
            topic = c.get("topic", "general")
            nova_said = c.get("nova_response", "")
            jordan_said = c.get("jordan_correction", "")
            summary = (
                f"CORRECTION [{topic}]: Nova said \"{nova_said}\" "
                f"-> Jordan corrected: \"{jordan_said}\""
            )
            if summary not in seen_texts:
                seen_texts.add(summary)
                lines.append(f"- {summary}")

    if len(lines) <= 2:
        # Only header lines, no actual corrections found
        return ""

    lines.append("")
    lines.append("Use these corrections to avoid repeating past mistakes.")
    lines.append("=== END PRIOR CORRECTIONS ===")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Nova Correction Prompt — check for relevant prior corrections before responding"
    )
    parser.add_argument(
        "--query", "-q",
        required=True,
        help="The current question or topic to check corrections for",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=5,
        help="Maximum number of corrections to retrieve (default: 5)",
    )

    args = parser.parse_args()

    # Search both sources
    vector_results = search_vector_memory(args.query, args.limit)
    local_results = search_local_corrections(args.query, args.limit)

    output = format_corrections_for_prompt(vector_results, local_results)

    if output:
        print(output)
    else:
        # Silent when no corrections found — nothing to inject
        pass


if __name__ == "__main__":
    main()
