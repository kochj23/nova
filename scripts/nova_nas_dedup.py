#!/usr/bin/env python3
"""
nova_nas_dedup.py — find and (after approval) delete EXACT duplicate files on /Volumes/NAS.

Pattern (same as the media gardener / backup reaper): scan -> propose -> you approve
-> apply, with telemetry + Slack. EXACT duplicates only (content hash); never guesses.

SAFETY:
  * GoogleDriveBackups / Google-Drive-kochjpar are PROTECTED — deletes there sync to
    Google, so files inside them are NEVER deletion targets. When a dup set spans a
    protected folder and elsewhere, we KEEP the protected copy and delete the other.
  * A dup set that lives ENTIRELY inside protected folders is recorded as
    'manual_review' (reported, never auto-deleted — resolve it in Google Drive).
  * apply() deletes only status='approved' rows, and re-checks protection per file.
  * Deletions are recoverable for 15 days via the UNAS backup (the reaper holds them).

Subcommands: scan | propose | apply | report
"""
import hashlib
import os
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
try:
    from nova_notify import notify
except Exception:
    notify = None

DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"
ROOT = "/Volumes/NAS"
PROTECTED_DIRS = {"GoogleDriveBackups", "Google-Drive-kochjpar",
                  "Shared Google Drives",   # work data + Google-synced
                  "iTunes"}                 # central iTunes library (.7) — NEVER delete (Jordan, 2026-06-23)
# Absolute-path prefixes that must NEVER be a deletion target, no matter what.
# Hard, unconditional guard — checked first in protected().
NEVER_DELETE_PREFIXES = ("/Volumes/NAS/iTunes",)
SKIP_DIRS = {"#recycle", "@eaDir", ".Trash", "#snapshot", ".TemporaryItems", "#sharesnap"}
MIN_SIZE = 1_048_576          # ignore <1MB — focus on space; skips tiny-file noise
CHUNK = 65536


def protected(path: str) -> bool:
    # Unconditional absolute-prefix guard first (iTunes etc.) — cannot be overridden.
    if any(path.startswith(p) for p in NEVER_DELETE_PREFIXES):
        return True
    return any(seg in PROTECTED_DIRS for seg in path.split(os.sep))


def partial_hash(path: str, size: int) -> str:
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        h.update(f.read(CHUNK))
        if size > 2 * CHUNK:
            f.seek(-CHUNK, os.SEEK_END)
            h.update(f.read(CHUNK))
    return h.hexdigest()


def full_hash(path: str) -> str:
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _db():
    c = psycopg2.connect(DSN)
    c.autocommit = True
    return c


def scan():
    """Inventory the NAS, then hash only size-collision candidates (two-stage)."""
    conn = _db(); cur = conn.cursor()
    seen = 0
    batch = []
    cur.execute("SELECT now()"); scan_start = cur.fetchone()[0]
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            p = os.path.join(dirpath, fn)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not os.path.isfile(p) or os.path.islink(p) or st.st_size < MIN_SIZE:
                continue
            batch.append((p, st.st_size, st.st_mtime))
            seen += 1
            if len(batch) >= 1000:
                _flush(cur, batch); batch = []
    if batch:
        _flush(cur, batch)
    # drop rows for files that vanished since this scan
    cur.execute("DELETE FROM nas_files WHERE seen_at < %s", (scan_start,))

    # Stage 2: hash candidates. Only files whose size is shared by >1 file.
    cur.execute("""UPDATE nas_files SET partial_hash=NULL, content_hash=NULL
                   WHERE size_bytes IN (SELECT size_bytes FROM nas_files GROUP BY size_bytes HAVING count(*)=1)""")
    cur.execute("""SELECT path,size_bytes FROM nas_files
                   WHERE partial_hash IS NULL AND size_bytes IN
                   (SELECT size_bytes FROM nas_files GROUP BY size_bytes HAVING count(*)>1)""")
    todo = cur.fetchall(); ph = 0
    for p, sz in todo:
        try:
            cur.execute("UPDATE nas_files SET partial_hash=%s WHERE path=%s", (partial_hash(p, sz), p)); ph += 1
        except OSError:
            cur.execute("DELETE FROM nas_files WHERE path=%s", (p,))
    # full-hash only (size, partial_hash) collisions
    cur.execute("""SELECT path FROM nas_files WHERE content_hash IS NULL AND partial_hash IS NOT NULL
                   AND (size_bytes,partial_hash) IN
                   (SELECT size_bytes,partial_hash FROM nas_files WHERE partial_hash IS NOT NULL
                    GROUP BY size_bytes,partial_hash HAVING count(*)>1)""")
    fh = 0
    for (p,) in cur.fetchall():
        try:
            cur.execute("UPDATE nas_files SET content_hash=%s, hashed_at=now() WHERE path=%s", (full_hash(p), p)); fh += 1
        except OSError:
            cur.execute("DELETE FROM nas_files WHERE path=%s", (p,))
    cur.execute("INSERT INTO nas_dedup_runs (mode,files_seen,hashed) VALUES ('scan',%s,%s)", (seen, fh))
    print(f"[dedup] scanned {seen} files; partial-hashed {ph}, full-hashed {fh} candidates.", flush=True)
    conn.close()


def _flush(cur, batch):
    cur.executemany(
        "INSERT INTO nas_files (path,size_bytes,mtime,seen_at) VALUES (%s,%s,%s,now()) "
        "ON CONFLICT (path) DO UPDATE SET seen_at=now(), "
        "partial_hash = CASE WHEN nas_files.mtime = EXCLUDED.mtime AND nas_files.size_bytes = EXCLUDED.size_bytes "
        "                     THEN nas_files.partial_hash ELSE NULL END, "
        "content_hash = CASE WHEN nas_files.mtime = EXCLUDED.mtime AND nas_files.size_bytes = EXCLUDED.size_bytes "
        "                     THEN nas_files.content_hash ELSE NULL END, "
        "size_bytes=EXCLUDED.size_bytes, mtime=EXCLUDED.mtime", batch)


def _keeper(paths, mtimes):
    """Pick which copy to keep: prefer a non-'copy' name, then shortest path, then oldest."""
    def score(i):
        name = os.path.basename(paths[i]).lower()
        bad = any(t in name for t in ("copy", "(1)", "(2)", "(3)", " 2.", " 3.", "~", "conflict", "duplicate"))
        return (bad, len(paths[i]), mtimes[i])
    return paths[min(range(len(paths)), key=score)]


def propose():
    conn = _db(); cur = conn.cursor()
    cur.execute("DELETE FROM nas_dedup_proposals WHERE status IN ('proposed','manual_review')")
    cur.execute("""SELECT content_hash, array_agg(path), array_agg(mtime), max(size_bytes), count(*)
                   FROM nas_files WHERE content_hash IS NOT NULL
                   GROUP BY content_hash HAVING count(*)>1""")
    sets = cur.fetchall()
    proposed = manual = 0; reclaim = 0
    for chash, paths, mtimes, size, n in sets:
        prot = [p for p in paths if protected(p)]
        unprot = [p for p in paths if not protected(p)]
        if prot and unprot:                       # span Google + elsewhere -> keep Google, delete the rest
            keeper, delete, status = prot[0], unprot, "proposed"
        elif unprot:                              # all outside Google -> normal dedup
            keeper = _keeper(unprot, [mtimes[paths.index(p)] for p in unprot])
            delete, status = [p for p in unprot if p != keeper], "proposed"
        else:                                     # entirely inside Google -> manual review only
            keeper, delete, status = paths[0], paths[1:], "manual_review"
        for p in delete:
            cur.execute("INSERT INTO nas_dedup_proposals (path,keeper_path,content_hash,size_bytes,set_size,status) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (path) DO UPDATE SET status=EXCLUDED.status",
                        (p, keeper, chash, size, n, status))
            if status == "proposed":
                proposed += 1; reclaim += size
            else:
                manual += 1
    gb = round(reclaim / 1e9, 1)
    cur.execute("INSERT INTO nas_dedup_runs (mode,dup_sets,reclaimable_gb) VALUES ('propose',%s,%s)", (len(sets), gb))
    if notify:
        notify(f"NAS dedup: {proposed} exact duplicates (~{gb} GB) ready to approve",
               body=f"{len(sets)} duplicate sets found. {proposed} safe deletions proposed (~{gb} GB). "
                    f"{manual} copies are inside GoogleDriveBackups — manual review, not auto-deleted.",
               level="info", category="dedup", source="nova_nas_dedup.py")
    print(f"[dedup] {len(sets)} dup sets: {proposed} proposed (~{gb} GB), {manual} manual-review (Google).", flush=True)
    conn.close()


def apply():
    conn = _db(); cur = conn.cursor()
    cur.execute("SELECT path,size_bytes FROM nas_dedup_proposals WHERE status='approved'")
    rows = cur.fetchall()
    if not rows:
        print("[dedup] nothing approved — nothing deleted (safe)."); conn.close(); return
    freed = done = 0
    for p, size in rows:
        if protected(p):                          # belt-and-suspenders: never touch Google
            cur.execute("UPDATE nas_dedup_proposals SET status='skipped_protected' WHERE path=%s", (p,)); continue
        try:
            os.remove(p); freed += (size or 0); done += 1
            cur.execute("UPDATE nas_dedup_proposals SET status='deleted' WHERE path=%s", (p,))
            cur.execute("DELETE FROM nas_files WHERE path=%s", (p,))
        except FileNotFoundError:
            cur.execute("UPDATE nas_dedup_proposals SET status='deleted' WHERE path=%s", (p,))
        except Exception as e:
            print(f"[dedup] could not delete {p}: {e}")
    gb = round(freed / 1e9, 1)
    cur.execute("INSERT INTO nas_dedup_runs (mode,deleted,freed_gb) VALUES ('apply',%s,%s)", (done, gb))
    if notify:
        notify(f"NAS dedup: deleted {done} duplicates, freed ~{gb} GB",
               body=f"Removed {done} approved duplicate files (~{gb} GB). Recoverable from the UNAS backup for 15 days.",
               level="info", category="dedup", source="nova_nas_dedup.py")
    print(f"[dedup] deleted {done} files, freed ~{gb} GB.", flush=True)
    conn.close()


def report():
    conn = _db(); cur = conn.cursor()
    cur.execute("SELECT count(*), round(sum(size_bytes)/1e9,1) FROM nas_dedup_proposals WHERE status='proposed'")
    pc, pg = cur.fetchone()
    cur.execute("SELECT count(*) FROM nas_dedup_proposals WHERE status='manual_review'")
    mc = cur.fetchone()[0]
    print(f"Proposed (safe to approve): {pc or 0} files, ~{pg or 0} GB")
    print(f"Manual review (inside GoogleDriveBackups): {mc or 0} files")
    cur.execute("SELECT size_bytes, keeper_path, path FROM nas_dedup_proposals WHERE status='proposed' "
                "ORDER BY size_bytes DESC LIMIT 15")
    print("\nBiggest reclaimable duplicates:")
    for sz, keep, dup in cur.fetchall():
        print(f"  {sz/1e6:8.1f} MB  DELETE {dup}\n             KEEP   {keep}")
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    {"scan": scan, "propose": propose, "apply": apply, "report": report}.get(cmd, scan)()
