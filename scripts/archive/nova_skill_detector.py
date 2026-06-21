#!/usr/bin/env python3
"""
nova_skill_detector.py — Detect repeated tool-call patterns in gateway traces.

Runs daily via scheduler. Mines the last 7 days of gateway_traces for repeated
tool-call sequences. When a pattern appears 3+ times, proposes it as a candidate
for skill generation.

Written by Jordan Koch (via Claude).
"""

import hashlib
import json
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

DB_HOST = "localhost"
DB_NAME = "nova_ops"
DB_USER = "kochj"
THRESHOLD = 3
LOOKBACK_DAYS = 7


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[skill-detector {ts}] {msg}", flush=True)


def db_query(sql: str) -> list:
    result = subprocess.run(
        ["psql", "-h", DB_HOST, "-U", DB_USER, "-d", DB_NAME, "-t", "-A", "-F", "\t", "-c", sql],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return []
    return [line.split("\t") for line in result.stdout.strip().split("\n") if line.strip()]


def db_exec(sql: str):
    subprocess.run(
        ["psql", "-h", DB_HOST, "-U", DB_USER, "-d", DB_NAME, "-c", sql],
        capture_output=True, text=True, timeout=15
    )


def extract_tool_sequences() -> list:
    """Extract tool call sequences from gateway traces in the last N days."""
    rows = db_query(
        f"SELECT trace_id, tool_calls "
        f"FROM gateway_traces "
        f"WHERE created_at > now() - interval '{LOOKBACK_DAYS} days' "
        f"AND tool_calls IS NOT NULL AND tool_calls != '[]' AND tool_calls != 'null' "
        f"ORDER BY created_at DESC LIMIT 5000"
    )

    sequences = []
    for row in rows:
        if len(row) < 2:
            continue
        trace_id = row[0]
        try:
            tools = json.loads(row[1])
            if isinstance(tools, list) and len(tools) >= 2:
                normalized = normalize_sequence(tools)
                if normalized:
                    sequences.append({"trace_id": trace_id, "sequence": normalized})
        except (json.JSONDecodeError, TypeError):
            continue

    return sequences


def normalize_sequence(tools: list) -> list:
    """Normalize a tool sequence for pattern matching.
    Strips variable parameters, keeps structure."""
    normalized = []
    for tool in tools:
        if isinstance(tool, dict):
            name = tool.get("name", tool.get("tool", ""))
            params = tool.get("params", tool.get("arguments", {}))
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except Exception:
                    params = {}
            # Keep param keys but not values (values are variable)
            param_keys = sorted(params.keys()) if isinstance(params, dict) else []
            normalized.append({"tool": name, "param_keys": param_keys})
        elif isinstance(tool, str):
            normalized.append({"tool": tool, "param_keys": []})

    return normalized


def hash_sequence(sequence: list) -> str:
    """Create a stable hash for a tool sequence."""
    canonical = json.dumps(sequence, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def detect_patterns(sequences: list) -> dict:
    """Find repeated patterns. Returns {hash: {sequence, traces, count}}."""
    pattern_map = {}

    for item in sequences:
        h = hash_sequence(item["sequence"])
        if h not in pattern_map:
            pattern_map[h] = {"sequence": item["sequence"], "traces": [], "count": 0}
        pattern_map[h]["count"] += 1
        if len(pattern_map[h]["traces"]) < 5:
            pattern_map[h]["traces"].append(item["trace_id"])

    # Filter to threshold
    return {h: v for h, v in pattern_map.items() if v["count"] >= THRESHOLD}


def upsert_patterns(patterns: dict):
    """Insert or update patterns in skill_patterns table."""
    for pattern_hash, data in patterns.items():
        escaped_seq = json.dumps(data["sequence"]).replace("'", "''")
        escaped_traces = json.dumps(data["traces"]).replace("'", "''")

        db_exec(
            f"INSERT INTO skill_patterns (pattern_hash, tool_sequence, occurrence_count, example_traces) "
            f"VALUES ('{pattern_hash}', '{escaped_seq}'::jsonb, {data['count']}, '{escaped_traces}'::jsonb) "
            f"ON CONFLICT (pattern_hash) DO UPDATE SET "
            f"occurrence_count = GREATEST(skill_patterns.occurrence_count, {data['count']}), "
            f"last_seen = now(), "
            f"example_traces = '{escaped_traces}'::jsonb"
        )


def find_proposable() -> list:
    """Find patterns ready to propose (detected, count >= threshold)."""
    rows = db_query(
        f"SELECT pattern_id, pattern_hash, tool_sequence::text, occurrence_count "
        f"FROM skill_patterns "
        f"WHERE status = 'detected' AND occurrence_count >= {THRESHOLD} "
        f"ORDER BY occurrence_count DESC LIMIT 5"
    )
    results = []
    for row in rows:
        if len(row) >= 4:
            results.append({
                "pattern_id": row[0], "hash": row[1],
                "sequence": json.loads(row[2]), "count": int(row[3])
            })
    return results


def propose_skill(pattern: dict):
    """Post a skill proposal to Slack and update status."""
    tools_str = " → ".join(t["tool"] for t in pattern["sequence"])
    msg = (
        f":brain: *Skill Evolution Candidate*\n"
        f"Pattern detected {pattern['count']} times:\n"
        f"`{tools_str}`\n"
        f"Pattern ID: `{pattern['pattern_id']}`\n\n"
        f"Say `approve skill {pattern['pattern_id']}` to generate, "
        f"or `reject skill {pattern['pattern_id']}` to dismiss."
    )
    nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)
    db_exec(f"UPDATE skill_patterns SET status = 'proposed' WHERE pattern_id = '{pattern['pattern_id']}'")
    log(f"Proposed: {tools_str} ({pattern['count']}x)")


def run():
    log("Starting skill detection...")

    sequences = extract_tool_sequences()
    log(f"Extracted {len(sequences)} tool sequences from last {LOOKBACK_DAYS} days")

    if not sequences:
        log("No tool sequences found — nothing to detect")
        return

    patterns = detect_patterns(sequences)
    log(f"Found {len(patterns)} patterns meeting threshold ({THRESHOLD}+)")

    if patterns:
        upsert_patterns(patterns)

    proposable = find_proposable()
    for p in proposable:
        propose_skill(p)

    if proposable:
        log(f"Proposed {len(proposable)} new skills")
    else:
        log("No new skills to propose")


if __name__ == "__main__":
    run()
