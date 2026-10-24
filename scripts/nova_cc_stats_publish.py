#!/usr/bin/env python3
"""nova_cc_stats_publish.py — Claude Code usage → Postgres, for the Nova Cluster app.

Jordan, 2026-10-24: Claude Code sessions live on this Mac (.6). The `cctime` CLI already
summarises them per window (sessions, cost, tokens, stuck loops). This organ runs cctime
for the week AND the day, keeps ONLY the compact `summary` block from each, and UPSERTs a
single row into nova_ops.service_config (service='cc_stats', key='latest'):

    {"generated_at": "<iso>", "week": <week summary>, "day": <day summary>}

The minion hub (nova_minion_hub.py) reads that row and folds it into the /cluster snapshot
as `claude_code`, and the NovaCluster app draws a small dashboard card from it.

FAIL-OPEN by design: if cctime is missing, errors, or the DB is unreachable, we log and
exit 0 — a missing/stale row simply means the app shows no CC card, never a broken hub.
Idempotent: one row, replaced in place each run. No flat-file state (house rule).

Cadence: every 10 min from the Nova scheduler (scheduler.yaml task `cc_stats_publish`).

Modes:
  (default)   run week+day, upsert the row
  --once      alias for the default single run (explicit, for the scheduler)
  --selftest  run cctime + build the payload, PRINT it, do NOT touch the DB

Connection follows the house pattern: nova_dsn.pg_connect('nova_ops').
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone

import nova_dsn as _nova_dsn  # noqa: E402

CCTIME = "/opt/homebrew/bin/cctime"
SERVICE = "cc_stats"
KEY = "latest"
# The exact keys cctime's `summary` block carries (ints are ms for the *Ms fields).
SUMMARY_KEYS = (
    "sessionCount", "totalCostUsd", "totalActiveMs", "totalDurationMs",
    "totalTokensIn", "totalTokensOut", "totalTurns", "warmupOverheadUsd",
    "stuckLoopSessions", "stuckLoopTotal", "stuckLoopAvgRetries",
)


def log(m):
    print(f"[cc-stats {datetime.now():%H:%M:%S}] {m}", flush=True)


def _summary_for(window: str) -> dict | None:
    """Run `cctime --<window> --json` and return ONLY its `summary` object (compact), or None."""
    try:
        out = subprocess.run([CCTIME, f"--{window}", "--json"],
                             capture_output=True, text=True, timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f"cctime --{window}: {e.__class__.__name__}: {str(e)[:80]}")
        return None
    if out.returncode != 0:
        log(f"cctime --{window}: exit {out.returncode}: {(out.stderr or '').strip()[:120]}")
        return None
    try:
        summary = (json.loads(out.stdout) or {}).get("summary")
    except json.JSONDecodeError as e:
        log(f"cctime --{window}: bad JSON: {str(e)[:80]}")
        return None
    if not isinstance(summary, dict):
        log(f"cctime --{window}: no summary object")
        return None
    # Keep only the known summary keys, so the stored blob stays small and predictable.
    return {k: summary[k] for k in SUMMARY_KEYS if k in summary}


def build_payload() -> dict | None:
    """The value to store: generated_at + week + day summaries. None if BOTH windows failed."""
    week, day = _summary_for("week"), _summary_for("day")
    if week is None and day is None:
        return None
    payload = {"generated_at": datetime.now(timezone.utc).isoformat()}
    if week is not None:
        payload["week"] = week
    if day is not None:
        payload["day"] = day
    return payload


def upsert(payload: dict) -> None:
    """Replace cc_stats/latest in service_config (pattern from nova_autonomy_memory.save_watermark)."""
    conn = _nova_dsn.pg_connect("nova_ops")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO service_config (service, key, value, updated_at, updated_by)
                   VALUES (%s, %s, %s::jsonb, now(), %s)
                   ON CONFLICT (service, key)
                   DO UPDATE SET value = EXCLUDED.value, updated_at = now(),
                                 updated_by = EXCLUDED.updated_by""",
                (SERVICE, KEY, json.dumps(payload), "nova_cc_stats_publish"))
        conn.commit()
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Publish Claude Code usage stats to service_config.")
    ap.add_argument("--once", action="store_true", help="single run (default behaviour; for the scheduler)")
    ap.add_argument("--selftest", action="store_true", help="build the payload and print it; do NOT touch the DB")
    args = ap.parse_args(argv)

    payload = build_payload()
    if payload is None:
        log("no cctime data (fail-open): leaving any existing row untouched")
        return 0

    if args.selftest:
        print(json.dumps(payload, indent=2))
        return 0

    try:
        upsert(payload)
    except Exception as e:  # noqa: BLE001 — fail-open: a DB blip must never crash the scheduler slot
        log(f"upsert failed (fail-open): {e.__class__.__name__}: {str(e)[:120]}")
        return 0
    w, d = payload.get("week", {}), payload.get("day", {})
    log(f"published cc_stats/latest — week: {w.get('sessionCount', '?')} sessions / "
        f"${w.get('totalCostUsd', '?')}, day: {d.get('sessionCount', '?')} sessions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
