#!/usr/bin/env python3
"""
nova_school_report.py — "How was school today?" digest.

Summarizes TODAY's newly-ingested Nova memories by vector: count + one sample
per vector. Fed to the gateway agent so Nova can narrate her "school day"
(what she learned/ingested today) in her own voice.

Written by Jordan Koch.
"""
import subprocess
import sys

DB = ["psql", "-h", "localhost", "-U", "kochj", "-d", "nova_memories", "-tA", "-F", "\t", "-c"]

COUNTS_SQL = """
SELECT source, count(*) FROM memories
WHERE created_at::date = current_date
GROUP BY source ORDER BY count(*) DESC;
"""

SAMPLE_SQL = r"""
WITH s AS (
  SELECT source, text,
         row_number() OVER (PARTITION BY source ORDER BY created_at DESC) rn
  FROM memories WHERE created_at::date = current_date
)
SELECT source, left(regexp_replace(text, '\s+', ' ', 'g'), 140)
FROM s WHERE rn = 1;
"""


def _psql(sql):
    r = subprocess.run(DB + [sql], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "psql failed")
    return [line.split("\t") for line in r.stdout.strip().splitlines() if line]


def build_report(counts, samples):
    """counts: [(source, count_str)]; samples: {source: sample}. Returns digest text."""
    if not counts:
        return "No new memories today — Nova hasn't ingested anything yet."
    total = sum(int(c) for _, c in counts)
    lines = [f"Nova's school day — {total} new memories across {len(counts)} vectors:"]
    for source, cnt in counts:
        sample = samples.get(source, "").strip()
        lines.append(f"- {source} ({cnt}): {sample}" if sample else f"- {source} ({cnt})")
    return "\n".join(lines)


def main():
    counts = [(row[0], row[1]) for row in _psql(COUNTS_SQL)]
    samples = {row[0]: (row[1] if len(row) > 1 else "") for row in _psql(SAMPLE_SQL)}
    print(build_report(counts, samples))


def _selftest():
    assert "hasn't ingested" in build_report([], {})
    out = build_report([("television", "401"), ("email", "3")], {"television": "World Cup chatter"})
    assert "404 new memories across 2 vectors" in out
    assert "- television (401): World Cup chatter" in out
    assert "- email (3)" in out and "- email (3):" not in out  # no trailing colon when no sample
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
