#!/usr/bin/env python3
"""
nova_scanner_backfill.py — one-shot: LLM-correct EXISTING scanner/fire/rail memories.

Walks nova_memories for every scanner/fire/rail transcript not yet corrected, runs each
through nova_scanner_correct.correct() (fleet fast pool), and updates the row in place:
  text            -> corrected (prefix "[label] " preserved)
  metadata.corrected              = true
  metadata.correction_confidence  = <0-1 or null>
  metadata.raw_transcript         = <original>   # SAFETY NET for the bulk rewrite (see note)

NOTE on raw_transcript: the live pipeline discards the raw per Jordan's "corrected only"
choice. For this BACKFILL only we keep the raw in metadata — 45k irreversible historical
rewrites are higher-risk than a single live call, and the raw is the sole record of what the
radio actually said. Strip later with one UPDATE if unwanted; costs nothing to keep.

Embeddings are left as-is (corrected text is near-identical semantically; re-embedding 45k
rows is a separate heavier job). Resumable: re-running skips already-corrected rows.

Run:  nohup python3 nova_scanner_backfill.py > ~/.openclaw/logs/scanner_backfill.log 2>&1 &
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
from nova_scanner_correct import correct
import nova_config

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_memories")
SOURCES = ("scanner", "fire", "rail")
BATCH = 120            # rows per round — small so progress commits often & a kill loses little
WORKERS = 6            # concurrent router calls — fast pool is 3 nodes, one shared w/ SDR; stay gentle
PREFIX = re.compile(r"^(\[[^\]]*\]\s*)(.*)$", re.DOTALL)
LOG = Path.home() / ".openclaw/logs/scanner_backfill.log"


def log(m):
    line = f"[scanner-backfill {time.strftime('%H:%M:%S')}] {m}"
    print(line, flush=True)


def _correct_row(row):
    mid, text, source, meta = row
    m = PREFIX.match(text or "")
    prefix, body = (m.group(1), m.group(2)) if m else ("", text or "")
    corrected, conf = correct(body, source)
    if not isinstance(meta, dict):
        meta = json.loads(meta) if meta else {}
    meta = {**meta, "corrected": True, "correction_confidence": conf, "raw_transcript": body}
    return mid, prefix + corrected, json.dumps(meta)


def main():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    total_done = 0
    t0 = time.time()
    last_post = 0.0

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memories WHERE source = ANY(%s) "
                    "AND (metadata->>'corrected') IS NULL", (list(SOURCES),))
        remaining = cur.fetchone()[0]
    log(f"start — {remaining} transcripts to correct across {SOURCES}")
    nova_config.post_both(f":gear: Scanner backfill started — {remaining:,} transcripts to LLM-correct "
                          f"(scanner/fire/rail). Will report progress here.",
                          slack_channel=nova_config.SLACK_FEED)

    while True:
        with conn.cursor() as cur:
            cur.execute("SELECT id, text, source, metadata FROM memories "
                        "WHERE source = ANY(%s) AND (metadata->>'corrected') IS NULL "
                        "ORDER BY created_at LIMIT %s", (list(SOURCES), BATCH))
            rows = cur.fetchall()
        if not rows:
            break

        results = []
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for fut in as_completed([ex.submit(_correct_row, r) for r in rows]):
                try:
                    results.append(fut.result())
                except Exception as e:
                    log(f"row error: {e}")

        with conn.cursor() as cur:
            for mid, new_text, new_meta in results:
                cur.execute("UPDATE memories SET text=%s, metadata=%s WHERE id=%s",
                            (new_text, new_meta, mid))
        total_done += len(results)

        rate = total_done / max(time.time() - t0, 1)
        eta_min = (remaining - total_done) / max(rate, 0.01) / 60
        log(f"{total_done}/{remaining} done ({rate:.1f}/s, ETA {eta_min:.0f}m)")

        # progress to #nova-info every ~10 min
        if time.time() - last_post > 600:
            nova_config.post_both(
                f":gear: Scanner backfill: {total_done:,}/{remaining:,} corrected "
                f"({100*total_done/max(remaining,1):.0f}%), ~{eta_min:.0f} min left.",
                slack_channel=nova_config.SLACK_FEED)
            last_post = time.time()

    mins = (time.time() - t0) / 60
    log(f"DONE — {total_done} corrected in {mins:.1f}m")
    nova_config.post_both(
        f":white_check_mark: Scanner backfill complete — {total_done:,} transcripts LLM-corrected "
        f"in {mins:.0f} min. New dispatch memories are corrected automatically going forward.",
        slack_channel=nova_config.SLACK_FEED)
    conn.close()


if __name__ == "__main__":
    main()
