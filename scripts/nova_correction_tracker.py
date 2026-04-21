#!/usr/bin/env python3

"""
Nova Correction Tracker — Response Accuracy Tracking System

Logs corrections when Jordan corrects Nova's responses. Stores correction
pairs (what Nova said vs what was correct) in both a local JSON file and
Nova's vector memory for future retrieval and self-improvement.

Usage:
    # Log a correction
    nova_correction_tracker.py --log "Nova's wrong response" \
        --correction "Jordan's correction" \
        --topic "optional topic tag"

    # List recent corrections
    nova_correction_tracker.py --list [--limit N]

    # Show correction statistics by topic
    nova_correction_tracker.py --stats

    # Export all corrections as JSON to stdout
    nova_correction_tracker.py --export

Written by Jordan Koch.
"""

import argparse
import json
import sys
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

VECTOR_API_BASE = "http://127.0.0.1:18790"
CORRECTIONS_DIR = Path.home() / ".openclaw" / "workspace" / "state"
CORRECTIONS_FILE = CORRECTIONS_DIR / "corrections.json"


def load_corrections() -> list:
    """Load existing corrections from the JSON file."""
    if not CORRECTIONS_FILE.exists():
        return []
    try:
        with open(CORRECTIONS_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, IOError):
        return []


def save_corrections(corrections: list) -> None:
    """Save corrections to the JSON file."""
    CORRECTIONS_DIR.mkdir(parents=True, exist_ok=True)
    with open(CORRECTIONS_FILE, "w") as f:
        json.dump(corrections, f, indent=2)


def store_to_vector_memory(correction: dict) -> bool:
    """
    Store the correction in Nova's vector memory for future retrieval.
    Returns True on success, False on failure.
    """
    if requests is None:
        print("WARNING: requests library not available; skipping vector memory storage.", file=sys.stderr)
        return False

    topic = correction.get("topic", "general")
    nova_said = correction["nova_response"]
    jordan_said = correction["jordan_correction"]

    memory_text = (
        f"CORRECTION: When asked about {topic}, Nova said: \"{nova_said}\". "
        f"Jordan corrected: \"{jordan_said}\". Remember this."
    )

    payload = {
        "text": memory_text,
        "source": "correction",
        "title": f"Correction — {topic}",
        "metadata": {
            "privacy": "local-only",
            "correction_id": correction["id"],
            "topic": topic,
            "timestamp": correction["timestamp"],
        },
    }

    try:
        resp = requests.post(f"{VECTOR_API_BASE}/remember", json=payload, timeout=10)
        if resp.status_code == 200:
            return True
        else:
            print(
                f"WARNING: Vector memory returned {resp.status_code}: {resp.text}",
                file=sys.stderr,
            )
            return False
    except requests.RequestException as e:
        print(f"WARNING: Could not reach vector memory server: {e}", file=sys.stderr)
        return False


def log_correction(nova_response: str, jordan_correction: str, topic: str = "general") -> dict:
    """Create a correction record, store it locally and in vector memory."""
    correction = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now().isoformat(),
        "nova_response": nova_response,
        "jordan_correction": jordan_correction,
        "topic": topic or "general",
        "context": {},
    }

    corrections = load_corrections()
    corrections.append(correction)
    save_corrections(corrections)

    vector_ok = store_to_vector_memory(correction)

    return correction, vector_ok


def list_corrections(limit: int = 10) -> None:
    """Print recent corrections."""
    corrections = load_corrections()
    if not corrections:
        print("No corrections recorded yet.")
        return

    recent = corrections[-limit:]
    recent.reverse()

    print(f"=== Recent Corrections ({len(recent)} of {len(corrections)} total) ===\n")
    for c in recent:
        ts = c.get("timestamp", "unknown")[:19].replace("T", " ")
        topic = c.get("topic", "general")
        print(f"[{ts}]  Topic: {topic}")
        print(f"  Nova said:     {c.get('nova_response', '(missing)')}")
        print(f"  Correction:    {c.get('jordan_correction', '(missing)')}")
        print(f"  ID: {c.get('id', '?')}")
        print()


def show_stats() -> None:
    """Print correction statistics by topic."""
    corrections = load_corrections()
    if not corrections:
        print("No corrections recorded yet.")
        return

    topic_counts = Counter(c.get("topic", "general") for c in corrections)
    total = len(corrections)

    print(f"=== Correction Statistics ({total} total) ===\n")
    print(f"{'Topic':<30} {'Count':>6} {'Pct':>7}")
    print("-" * 45)
    for topic, count in topic_counts.most_common():
        pct = (count / total) * 100
        print(f"{topic:<30} {count:>6} {pct:>6.1f}%")

    if corrections:
        first_ts = corrections[0].get("timestamp", "")[:10]
        last_ts = corrections[-1].get("timestamp", "")[:10]
        print(f"\nDate range: {first_ts} to {last_ts}")


def export_corrections() -> None:
    """Dump all corrections as JSON to stdout."""
    corrections = load_corrections()
    json.dump(corrections, sys.stdout, indent=2)
    print()  # trailing newline


def main():
    parser = argparse.ArgumentParser(
        description="Nova Correction Tracker — log and query response corrections"
    )

    # Logging mode
    parser.add_argument("--log", metavar="RESPONSE", help="Nova's incorrect response to log")
    parser.add_argument("--correction", metavar="FIX", help="Jordan's correction")
    parser.add_argument("--topic", metavar="TAG", default="general", help="Topic tag (default: general)")

    # Query modes
    parser.add_argument("--list", action="store_true", help="List recent corrections")
    parser.add_argument("--limit", type=int, default=10, help="Number of corrections to show (default: 10)")
    parser.add_argument("--stats", action="store_true", help="Show correction statistics by topic")
    parser.add_argument("--export", action="store_true", help="Export all corrections as JSON")

    args = parser.parse_args()

    if args.list:
        list_corrections(args.limit)
    elif args.stats:
        show_stats()
    elif args.export:
        export_corrections()
    elif args.log and args.correction:
        correction, vector_ok = log_correction(args.log, args.correction, args.topic)
        print(f"Correction logged: {correction['id']}")
        print(f"  Topic:      {correction['topic']}")
        print(f"  Timestamp:  {correction['timestamp']}")
        print(f"  Local file: {CORRECTIONS_FILE}")
        if vector_ok:
            print("  Vector memory: stored successfully")
        else:
            print("  Vector memory: storage failed (correction saved locally only)")
    elif args.log or args.correction:
        print("ERROR: Both --log and --correction are required to record a correction.", file=sys.stderr)
        sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
