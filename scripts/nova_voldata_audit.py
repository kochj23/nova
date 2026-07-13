#!/opt/homebrew/bin/python3
"""nova_voldata_audit.py — FDA inventory of /Volumes/Data + /Volumes/MoreData for the
Phase-2 reclaim audit. Runs under launchd via /opt/homebrew/bin/python3 (which HAS Full
Disk Access — /bin/bash does not). Pure-python so it inherits python3's FDA. Writes
INCREMENTALLY (fast/valuable sections first) so progress is observable and a kill mid-run
still leaves partial results. Skips macOS system dirs (slow, not reclaimable)."""
import os, json

LOG = os.path.expanduser("~/.openclaw/logs/voldata-audit.log")
SKIP = {".Spotlight-V100", ".fseventsd", ".DocumentRevisions-V100", ".Trashes",
        ".TemporaryItems", ".PKInstallSandboxManager", ".AppStoreSandbox", ".DS_Store",
        ".vol", ".com.apple.backupd", ".APDisk"}
_fh = open(LOG, "w")


def w(line=""):
    _fh.write(line + "\n")
    _fh.flush()


def human(n):
    n = float(n)
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}P"


def dirsize(p):
    total = 0
    for root, dirs, files in os.walk(p, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


w("===== voldata audit =====")

# 1. Ollama orphaned blobs — the actual reclaim question, and it's fast.
w("\n##### Ollama models (orphaned = blob on disk with no manifest referencing it) #####")
oll = next((c for c in ("/Volumes/Data/.ollama/models", "/Volumes/Data/ollama-models/models",
                        "/Volumes/Data/ollama/models") if os.path.isdir(c)), None)
if oll:
    bd = os.path.join(oll, "blobs")
    blobs = set(os.listdir(bd)) if os.path.isdir(bd) else set()
    bsz = {}
    for b in blobs:
        try:
            bsz[b] = os.lstat(os.path.join(bd, b)).st_size
        except OSError:
            bsz[b] = 0
    referenced = set()
    for root, _, files in os.walk(os.path.join(oll, "manifests")):
        for f in files:
            try:
                m = json.load(open(os.path.join(root, f)))
                for layer in list(m.get("layers", [])) + ([m["config"]] if "config" in m else []):
                    referenced.add(layer.get("digest", "").replace("sha256:", "sha256-"))
            except Exception:
                pass
    orphaned = blobs - referenced
    w(f"  dir: {oll}")
    w(f"  {len(blobs)} blobs, {human(sum(bsz.values()))} total")
    w(f"  referenced by a model: {len(blobs & referenced)}")
    w(f"  ORPHANED (reclaimable via `ollama rm` leftovers): {len(orphaned)} blobs, {human(sum(bsz[b] for b in orphaned))}")
else:
    w("  ollama models dir not found on /Volumes/Data")

# 2. PG dump backups
w("\n##### PG dump backups (retention 7d) #####")
pgb = "/Volumes/Data/backups/postgres"
w(f"  {human(dirsize(pgb))}, {len(os.listdir(pgb))} entries" if os.path.isdir(pgb) else "  none")

# 3. Top-level du of the real data dirs (system dirs skipped), written as each finishes.
for vol in ("/Volumes/Data", "/Volumes/MoreData"):
    w(f"\n##### {vol} (top-level, >100MB, system dirs skipped) #####")
    try:
        results = []
        with os.scandir(vol) as it:
            for e in it:
                if e.name in SKIP:
                    continue
                try:
                    sz = dirsize(e.path) if e.is_dir(follow_symlinks=False) else e.stat(follow_symlinks=False).st_size
                except OSError:
                    sz = 0
                results.append((sz, e.name))
                if sz > 100 * 1024 * 1024:
                    w(f"  {human(sz):>9}  {e.name}   [running]")
        w("  --- sorted ---")
        for sz, name in sorted(results, reverse=True):
            if sz > 100 * 1024 * 1024:
                w(f"  {human(sz):>9}  {name}")
    except Exception as ex:
        w(f"  ERROR: {ex}")

w("\nAUDIT DONE")
_fh.close()
