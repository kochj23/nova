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
LOG = Path.home() / ".openclaw/logs/cloudkit_cache_watchdog.log"
DB = "host=localhost dbname=nova_ops user=kochj"


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


def notify(msg: str, severity: str = "info"):
    parts = msg.split("\n", 1)
    title = parts[0].lstrip(": ").replace("floppy_disk:", "").strip()
    body = parts[1] if len(parts) > 1 else None
    nova_notify(title, body=body, level=severity, category="storage",
                dedup_key="cloudkit-cache-watchdog", meta={"host": "mac-studio"})
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


if __name__ == "__main__":
    main()
