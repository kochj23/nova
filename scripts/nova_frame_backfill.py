#!/usr/bin/env python3
"""nova_frame_backfill.py — frame-index EXISTING videos into Nova memory, scopably.

Walks the TVShows library, skips videos already frame-indexed, and indexes the rest via
nova_frame_index. Resumable (re-run picks up where it left off). Scope it — do NOT run the
whole 7.9 TB / ~19.7k-video library blind (~300+ GPU-hours).

Usage:
  nova_frame_backfill.py --show "NBC4"          # one show / substring match
  nova_frame_backfill.py --recent-days 30       # videos modified in the last N days
  nova_frame_backfill.py --limit 200            # cap the number of videos this run
  nova_frame_backfill.py --frames 16            # frames per video (default 24)
  (combine filters; --dry-run to preview the work-list)
"""
import nova_dsn as _nova_dsn  # noqa: E402
import os, sys, time, argparse, subprocess
import psycopg2
sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
from nova_frame_index import index_video, show_from_path

ROOT = "/Volumes/external/videos/TVShows"
EXTS = (".mp4", ".mkv", ".ts", ".m4v", ".avi", ".mov", ".wmv")


def already_indexed():
    conn = psycopg2.connect(_nova_dsn.pg_dsn("nova_memories"))
    with conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT metadata->>'video' FROM memories WHERE source='frame_vision'")
        return {r[0] for r in cur.fetchall() if r[0]}


def find_videos(show, recent_days):
    cutoff = time.time() - recent_days * 86400 if recent_days else None
    for dirpath, _, files in os.walk(ROOT):
        if show and show.lower() not in dirpath.lower():
            continue
        for f in files:
            if f.lower().endswith(EXTS):
                fp = os.path.join(dirpath, f)
                if cutoff:
                    try:
                        if os.path.getmtime(fp) < cutoff:
                            continue
                    except OSError:
                        continue
                yield fp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", default=None)
    ap.add_argument("--recent-days", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--frames", type=int, default=24)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    done = already_indexed()
    todo = [fp for fp in find_videos(a.show, a.recent_days) if os.path.basename(fp) not in done]
    if a.limit:
        todo = todo[:a.limit]
    print(f"backfill scope: {len(todo)} videos to index (skipped {len(done)} already done), "
          f"{a.frames} frames each ≈ {len(todo)*a.frames} VLM calls", flush=True)
    if a.dry_run:
        for fp in todo[:40]:
            print(f"  would index: {fp}")
        return
    for i, fp in enumerate(todo, 1):
        show = show_from_path(fp)
        try:
            n = index_video(fp, show, a.frames)
            print(f"[{i}/{len(todo)}] {show}: {n} frames — {os.path.basename(fp)[:60]}", flush=True)
        except Exception as e:
            print(f"[{i}/{len(todo)}] FAILED {os.path.basename(fp)[:50]}: {e}", flush=True)
    print("backfill complete.", flush=True)


if __name__ == "__main__":
    main()
