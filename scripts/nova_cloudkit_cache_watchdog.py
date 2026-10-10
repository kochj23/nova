#!/opt/homebrew/bin/python3
"""
nova_cloudkit_cache_watchdog.py — Keep the iCloud Drive (bird) CloudKit asset
cache from filling the boot volume.

Background: ~/Library/Caches/CloudKit/com.apple.bird/<id>/Assets is a
re-downloadable iCloud Drive cache. It has twice ballooned to ~310GB and filled
the primary volume (2026-06-04 and 2026-06-09). The authoritative files live in
~/Library/Mobile Documents and are NOT touched here — only evictable cache.

Behavior: runs hourly via launchd. If the bird cache exceeds THRESHOLD_GB,
purge the Assets dir(s) (bird rebuilds lazily). Logs every run; posts to Slack
#nova-notifications + shared_observations only when it actually purges.

2026-10-08 (third recurrence): the cache stayed small (8GB) while a 128GB 2014
package in iCloud Drive Pictures re-downloaded onto the boot SSD (99% full), so
every run now also evicts the known ghost items back to cloud-only (brctl evict,
non-destructive, a no-op when already evicted) and alerts when the boot data
volume has less than LOW_FREE_GB free, whatever the cause. This job had been
disabled in launchd since 2026-06-12; it was re-enabled the same day.

Written by Jordan Koch / Nova.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
from nova_notify import notify as nova_notify

BIRD_CACHE = Path.home() / "Library/Caches/CloudKit/com.apple.bird"
THRESHOLD_GB = 50          # purge if bird cache exceeds this
LOW_FREE_GB = 50           # alert when the boot data volume has less free than this
ICLOUD_DRIVE = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs"
# Items iCloud keeps re-downloading onto the boot SSD (2026-06-04, 06-09, 10-08).
# ponytail: fixed list; add a path here when brctl status shows another big re-download.
# Both are unnamed copies of one 2014 Aperture library (~128 GB each); iCloud cannot finish
# downloading either (stuck at "99%" for months), so they stay cloud-only.
GHOSTS = ["Pictures/.com-apple-bird-noname-51289994-C7EB-4876-8464-84CB77E838AD.pkg",
          "Pictures/.com-apple-bird-noname-B59493E7-459D-48E6-BC00-26D18FB8BEB1.pkg"]
LOG = Path.home() / ".openclaw/logs/cloudkit_cache_watchdog.log"
import nova_dsn as _nova_dsn  # noqa: E402
DB = _nova_dsn.pg_dsn("nova_ops")


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def dir_size_gb(path: Path) -> float:
    """Fast cache size via du -sk (kilobytes)."""
    try:
        r = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True, timeout=300)
        kb = int(r.stdout.split()[0])
        return kb / (1024 * 1024)
    except Exception as e:
        log(f"du failed: {e}")
        return 0.0


def free_gb(mount="/System/Volumes/Data") -> float:
    try:
        st = os.statvfs(mount)
        return (st.f_bavail * st.f_frsize) / (1024 ** 3)
    except Exception:
        return -1.0


def purge_assets() -> int:
    """Delete Assets/ subdirs under every bird container. Returns count purged."""
    purged = 0
    if not BIRD_CACHE.exists():
        return 0
    for container in BIRD_CACHE.iterdir():
        if not container.is_dir():
            continue
        assets = container / "Assets"
        if assets.exists():
            try:
                shutil.rmtree(assets)
                purged += 1
                log(f"Purged {assets}")
            except Exception as e:
                log(f"Failed to purge {assets}: {e}")
    return purged


def evict_ghosts(root: Path = ICLOUD_DRIVE, ghosts=None) -> int:
    """brctl evict each known ghost item (cloud-only again; no data lost). Returns count evicted."""
    n = 0
    for rel in ghosts if ghosts is not None else GHOSTS:
        p = root / rel
        if not p.exists():
            continue
        try:
            r = subprocess.run(["brctl", "evict", str(p)], capture_output=True, text=True, timeout=300)
            if r.returncode == 0:
                n += 1
            log(f"evict {rel}: rc={r.returncode} {(r.stdout or r.stderr).strip()[:120]}")
        except Exception as e:  # noqa: BLE001 — fail open; the cache check still runs
            log(f"evict {rel} failed: {e}")
    return n


def notify(msg: str, severity: str = "info", dedup_key: str = "cloudkit-cache-watchdog"):
    parts = msg.split("\n", 1)
    title = parts[0].lstrip(": ").replace("floppy_disk:", "").strip()
    body = parts[1] if len(parts) > 1 else None
    nova_notify(title, body=body, level=severity, category="storage",
                dedup_key=dedup_key, meta={"host": "mac-studio"})
    try:
        import psycopg2
        conn = psycopg2.connect(DB)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
                VALUES ('nova', 'storage', 'cloudkit-cache-watchdog', %s, %s, %s)
            """, (msg, severity, json.dumps({"threshold_gb": THRESHOLD_GB})))
        conn.close()
    except Exception as e:
        log(f"PG record failed: {e}")


def main():
    evict_ghosts()
    size = dir_size_gb(BIRD_CACHE)
    free = free_gb()
    log(f"bird cache: {size:.1f}GB | free: {free:.1f}GB | threshold: {THRESHOLD_GB}GB")

    if size >= THRESHOLD_GB:
        log(f"OVER THRESHOLD — purging.")
        n = purge_assets()
        new_size = dir_size_gb(BIRD_CACHE)
        new_free = free_gb()
        reclaimed = size - new_size
        msg = (f":floppy_disk: *iCloud cache watchdog purged {reclaimed:.0f}GB*\n"
               f"bird CloudKit Assets cache hit {size:.0f}GB (limit {THRESHOLD_GB}GB). "
               f"Purged {n} container(s) → now {new_size:.1f}GB. "
               f"Free space: {free:.0f}GB → {new_free:.0f}GB. "
               f"(iCloud Drive rebuilds this cache lazily; no real files lost.)")
        log(msg)
        notify(msg, severity="warning")
    else:
        log("Under threshold — nothing to do.")
    free = free_gb()
    if 0 <= free < LOW_FREE_GB:
        notify(f":floppy_disk: *Studio boot SSD low: {free:.0f}GB free*\n"
               f"/System/Volumes/Data is below {LOW_FREE_GB}GB free. iCloud re-downloads have filled it "
               f"three times (2026-06-04, 06-09, 10-08); check `brctl status` for a large download.",
               severity="warning", dedup_key="studio-boot-ssd-low")


if __name__ == "__main__":
    main()
