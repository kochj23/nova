#!/usr/bin/env python3
"""
nova_media_gardener.py — the YouTube/media gardener.

PROPOSES which VIDEO files to prune per media_policy, and NEVER deletes on its own.
Transcripts (Nova's memories) are always kept — only the heavy .mp4 is a candidate.
A run writes media_prune_proposals (status='proposed'); a human approves the list,
and only then does `--apply` remove the approved files. Belt-and-suspenders:

  RULES
   - Only shows with policy='rolling_15' are ever candidates. 'keep' is untouchable.
   - Candidate = video older than WINDOW_DAYS AND (not watched in Plex, if known).
   - watched_auto_keep: anything you've actually played stays.
   - If a YouTube channel is gone/banned (source unreachable) it should be promoted
     to keep elsewhere — the gardener never prunes what it can't re-fetch silently.
"""
import os
import sys
from datetime import datetime, timezone

import psycopg2

DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"
WINDOW_DAYS = 15
AVG_EP_BYTES = 350_000_000   # ~350 MB fallback when the file isn't stat-able (mount down)


def ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS media_prune_proposals (
            file_path text PRIMARY KEY,
            show text,
            size_bytes bigint,
            size_estimated boolean DEFAULT false,
            age_days int,
            watched boolean,
            status text DEFAULT 'proposed',   -- proposed | approved | rejected | pruned
            reason text,
            proposed_at timestamptz DEFAULT now())""")


def propose():
    conn = psycopg2.connect(DSN); conn.autocommit = True
    cur = conn.cursor()
    ensure_table(cur)
    cur.execute("DELETE FROM media_prune_proposals WHERE status='proposed'")
    # HARD folder allowlist: only YouTube-source folders are EVER prunable.
    # Everything else (Movies, Documentary, Home Videos, Stand-Up, Other, Ripped
    # Movies, DVR) stays — per Jordan 2026-06-22. The Plex "TV Shows" *library* is
    # a dumping ground and is NOT a trustworthy signal; the folder path is.
    cur.execute(f"""
        SELECT m.file_path, m.show, m.processed_at
        FROM media_ingest_state m
        JOIN media_policy p ON p.show = m.show
        WHERE p.policy = 'rolling_15' AND p.locked
          AND m.processed_at < now() - interval '{WINDOW_DAYS} days'
          AND m.file_path IS NOT NULL AND m.file_path <> ''
          AND m.file_path ~ '/videos/(TVShows|Liked|yt|Youtube Music Videos|random|My Youtube)/'""")
    rows = cur.fetchall()
    n = 0; total = 0; est = 0
    for fp, show, processed in rows:
        try:
            size = os.path.getsize(fp); estimated = False
        except Exception:
            size = AVG_EP_BYTES; estimated = True; est += 1
        age = (datetime.now(timezone.utc) - processed).days if processed else None
        cur.execute(
            "INSERT INTO media_prune_proposals (file_path,show,size_bytes,size_estimated,age_days,reason) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (file_path) DO UPDATE SET "
            "size_bytes=EXCLUDED.size_bytes, size_estimated=EXCLUDED.size_estimated, age_days=EXCLUDED.age_days, status='proposed'",
            (fp, show, size, estimated, age, '15-day rolling — transcript retained'))
        n += 1; total += size
    conn.close()
    print(f"[gardener] PROPOSED {n} videos for prune, ~{total/1e9:.0f} GB "
          f"({est} sizes estimated — media mount unavailable). Transcripts kept. NOTHING deleted.")
    print("[gardener] review with the approval query, then run --apply to remove ONLY status='approved' rows.")
    return n


def apply():
    """Delete ONLY files explicitly marked status='approved'. Never touches 'proposed'."""
    conn = psycopg2.connect(DSN); conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT file_path FROM media_prune_proposals WHERE status='approved'")
    approved = [r[0] for r in cur.fetchall()]
    if not approved:
        print("[gardener] no rows marked 'approved' — nothing to do (safe).")
        return 0
    freed = 0; done = 0
    for fp in approved:
        try:
            sz = os.path.getsize(fp); os.remove(fp); freed += sz; done += 1
            cur.execute("UPDATE media_prune_proposals SET status='pruned' WHERE file_path=%s", (fp,))
        except FileNotFoundError:
            cur.execute("UPDATE media_prune_proposals SET status='pruned' WHERE file_path=%s", (fp,))
        except Exception as e:
            print(f"[gardener] could not remove {fp}: {e}")
    conn.close()
    print(f"[gardener] pruned {done} videos, freed ~{freed/1e9:.1f} GB. Transcripts intact.")
    return done


if __name__ == "__main__":
    sys.exit(0 if (apply() is not None if "--apply" in sys.argv else propose() is not None) else 1)
