#!/usr/bin/env python3
"""nova_nas_localdiff.py — FAST Synology→UNAS reconcile that finds BOTH sides
locally (no CIFS scan), orchestrated from .6.

The old diff scanned the dest over CIFS — brutal for the 3M-file `nas` tree
(never finished). Now that .6 has shell on both the Synology (source) and the
UNAS (dest), we `find` each side on its own local disk (~90s each), compare by
path+size here, then rsync ONLY the differing files via the Synology's existing
CIFS mount. Result: minutes instead of hours.

Reports per job to #nova-info and writes telemetry.backup_runs
(nova-backup:<name>:localdiff) so the backup monitor reflects the reconcile.
Written by Jordan Koch (via Claude).
"""
import os
import re
import subprocess
import sys
import time
from collections import Counter

import psycopg2

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import nova_config

SYNO = "kochj@192.168.1.11"
UNAS = "root@192.168.1.69"
UROOT = "/volume/b37f2e84-517c-4a4f-92f0-4d642527ba17/.srv/.unifi-drive"
import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
TMP = os.path.expanduser("~/.openclaw/workspace/state/nas_localdiff")

# name, synology source, unas dest (local), synology CIFS dest mount (for the rsync)
# NOTE: UniFi UNAS stores each share's contents under <share>/.data/ (owner
# unifi-drive). The dest scan MUST point at .data/ so relative paths line up with
# the Synology source — otherwise every file looks "missing". (Writes still go via
# the CIFS mount, which lands in .data/ correctly.)
JOBS = [
    ("nas",      "/volume1/nas",      f"{UROOT}/nas/.data",      "/volume1/docker/nas"),
    ("external", "/volume1/external", f"{UROOT}/External/.data", "/volume1/docker/external"),
]
# NOTE 2026-09-10: `find` emits RELATIVE paths (no leading slash), so the old `/#recycle`
# and `/#snapshot` never matched top-level Recycle Bin / snapshot dirs — the Synology
# recycle bin (deleted backups) counted as "differing" on every run, so the nas share
# NEVER reached parity (ok=false forever). Anchor with (^|/) and also drop DSM index
# sidecars (@SynoEAStream / SYNOINDEX_*), which are regenerable metadata, never user data.
EXCL = re.compile(r"@eaDir|@Syno|SYNOINDEX|(^|/)#recycle|(^|/)#snapshot|\.DS_Store$|\.app/|GoogleDriveBackups/Pics/Pictures/\.com-apple-bird-noname-")


def slack(msg):
    try:
        nova_config.post_both(msg, slack_channel=nova_config.SLACK_FEED, discord_channel=None)
    except Exception as e:
        print(f"slack failed: {e}", flush=True)


def find_to(host, path, outfile):
    """Stream `find <path> -type f -printf '%P\\t%s\\n'` on <host> into outfile.
    Returns bytes written. NOTE: find exits non-zero on benign permission warnings
    even when it lists everything, so we gauge success by output size, not rc."""
    with open(outfile, "w") as f:
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host,
             f"cd {path!r} && find . -type f -printf '%P\\t%s\\n'"],
            stdout=f, stderr=subprocess.DEVNULL, timeout=2400)
    return os.path.getsize(outfile)


def load_sizes(path):
    # Counter of (rel, size) PAIRS, not a rel->size dict. Some Synology entries share
    # a relative path but differ in size (media-index siblings, pathological duplicate
    # names like the "spicy pot roast …).jpg" recipe thumbs). A plain dict kept only
    # the LAST size per rel, so every other same-rel file looked "missing" forever —
    # the recurring nas:localdiff ok=false false positive that never reconciled even
    # though the bytes were already on the UNAS. Comparing as a multiset fixes it. 2026-09-10.
    c = Counter()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            rel, _, size = line.rstrip("\n").rpartition("\t")
            if rel and not EXCL.search(rel):
                c[(rel, size)] += 1
    return c


def record(name, rc, files):
    ok = rc in (0, 24)
    try:
        c = psycopg2.connect(DSN); c.autocommit = True
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO telemetry.backup_runs (ts,job,rc,elapsed_s,files,bytes,errors,ok) "
                "VALUES (now(),%s,%s,0,%s,0,%s,%s)",
                (f"nova-backup:{name}:localdiff", rc, files, 0 if ok else 1, ok))
        c.close()
    except Exception as e:
        print(f"telemetry write failed: {e}", flush=True)


STATE = os.path.expanduser("~/.openclaw/.localdiff_srccount.json")


def _last_counts():
    import json
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def _save_count(name, n):
    import json
    d = _last_counts(); d[name] = n
    try:
        json.dump(d, open(STATE, "w"))
    except Exception as e:
        print(f"state save failed: {e}", flush=True)


def prune_orphans(name, udst, orphans):
    """Delete UNAS-only files (on dest, absent from source). UNAS ONLY — the
    Synology source is never touched here. Caller applies the safety gates."""
    safe = [r for r in orphans if not r.startswith("/") and "../" not in r]
    if not safe:
        return 0, 0
    lst = f"{TMP}/ld_orphans_{name}.lst"
    with open(lst, "w") as f:
        f.write("\n".join(safe) + "\n")
    remote = f"/tmp/ld_orphans_{name}.lst"
    with open(lst) as f:
        subprocess.run(["ssh", "-o", "BatchMode=yes", UNAS, f"cat > {remote}"],
                       stdin=f, timeout=600)
    # cd into the dest then delete RELATIVE paths (null-delimited → handles spaces/odd bytes)
    p = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", UNAS,
         f"cd {udst!r} && tr '\\n' '\\0' < {remote} | xargs -0 rm -f -- ; echo RC=$?"],
        capture_output=True, text=True, timeout=14400)
    m = re.search(r"RC=(\d+)", p.stdout)
    return len(safe), (int(m.group(1)) if m else 99)


def rsync_files(name, src, cifs_dst, tosync_file):
    """Push the to_sync list to the Synology and rsync only those via its CIFS mount."""
    remote_list = f"/tmp/localdiff_{name}.lst"
    with open(tosync_file) as f:
        subprocess.run(["ssh", "-o", "BatchMode=yes", SYNO, f"cat > {remote_list}"],
                       stdin=f, timeout=120)
    p = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", SYNO,
         f"rsync -rlt --no-perms --no-owner --stats --files-from={remote_list} {src}/ {cifs_dst}/; echo RC=$?"],
        capture_output=True, text=True, timeout=14400)
    m = re.search(r"RC=(\d+)", p.stdout)
    return int(m.group(1)) if m else 99


def synology_resyncing():
    """Is the Synology RAID mid-resync/recovery? The reconcile adds scan+rsync load,
    so we skip during a rebuild (the nightly self-defers; the resync-done watcher
    fires us once it clears). Override with NOVA_LOCALDIFF_FORCE=1."""
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", SYNO, "cat /proc/mdstat"],
                       capture_output=True, text=True, timeout=30)
    return bool(re.search(r"(resync|recovery)\s*=", r.stdout))


def source_reachable():
    """Preflight: the Synology (source of truth) must be up before we reconcile.
    Without this, an unreachable source made find_to() return an empty listing, the
    loop 'skipped' every share, and main() still returned 0 — so a DEAD Synology
    looked like a successful sync. That masked the ~40h 2026-09-05/06 outage: the
    UNAS replica silently went stale and nothing alarmed. Fail LOUD instead."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", SYNO, "echo ok"],
            capture_output=True, text=True, timeout=20)
        return r.returncode == 0 and "ok" in r.stdout
    except Exception:
        return False


def ensure_mounts():
    """Self-heal: bring the Synology's CIFS mounts of the UNAS back up if they dropped
    (e.g. after a reboot) before we rsync into them. The mountpoints are chattr +i, so
    even if this fails, a write can't fill local disk — this just restores the mirror.
    See memory: unas-mirror-cifs-mount-and-immutable-fix."""
    cmd = ('for m in nas:nas external:External; do mp=/volume1/docker/${m%%:*}; '
           'unc=//192.168.1.69/${m#*:}; grep -q " $mp cifs " /proc/mounts || '
           'sudo -n mount -t cifs "$unc" "$mp" -o credentials=/root/.unascreds,iocharset=utf8,vers=3.0 2>/dev/null; done')
    subprocess.run(["ssh", "-o", "BatchMode=yes", SYNO, cmd], timeout=60)


def main():
    os.makedirs(TMP, exist_ok=True)
    # Source-down guard (must run FIRST): a dead Synology used to look like success.
    if not source_reachable():
        msg = ("Synology source (192.168.1.11) unreachable — reconcile ABORTED. "
               "The UNAS replica is NOT being updated and is going stale.")
        print(msg, flush=True)
        slack(f":rotating_light: *NAS reconcile FAILED* — {msg}")
        record("nas", 99, 0)   # write a failed backup_run so the backup monitor catches it
        return 1
    if synology_resyncing() and os.environ.get("NOVA_LOCALDIFF_FORCE") != "1":
        print("synology RAID resyncing — deferring reconcile (set NOVA_LOCALDIFF_FORCE=1 to override)", flush=True)
        return 0
    ensure_mounts()
    slack(":rocket: *Local-find reconcile started* (Synology↔UNAS, both sides scanned locally — no CIFS crawl). "
          "Reporting per-share results here.")
    only = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("nas", "external") else None
    for name, src, udst, cifs in JOBS:
        if only and name != only:
            continue
        t0 = time.time()
        sf, df, tf = f"{TMP}/ld_src_{name}.lst", f"{TMP}/ld_dst_{name}.lst", f"{TMP}/ld_to_{name}.lst"
        if find_to(SYNO, src, sf) == 0 or find_to(UNAS, udst, df) == 0:
            slack(f":warning: {name}: find produced no output on one side — skipping."); continue
        dst = load_sizes(df)                    # Counter[(rel, size)] -> count
        remaining = Counter(dst)                # consumed as each source file matches a UNAS copy
        dst_rels = {rel for (rel, _sz) in dst}
        src_set = set()
        nto = 0
        with open(sf, encoding="utf-8", errors="replace") as s, open(tf, "w") as out:
            for line in s:
                rel, _, size = line.rstrip("\n").rpartition("\t")
                if not rel or EXCL.search(rel):
                    continue
                src_set.add(rel)
                if remaining[(rel, size)] > 0:  # this exact (rel,size) already on the UNAS
                    remaining[(rel, size)] -= 1
                else:                           # missing on dest OR size differs
                    out.write(rel + "\n"); nto += 1
        nsrc = len(src_set)
        ndst = sum(dst.values())
        orphans = [rel for rel in dst_rels if rel not in src_set]  # on UNAS, gone from Synology
        if orphans:
            osz = {}                            # orphan rel -> total bytes across its (rel,size) entries
            for (rel, size), cnt in dst.items():
                if rel not in src_set:
                    osz[rel] = osz.get(rel, 0) + int(size) * cnt
            orphan_bytes = sum(osz.values())
            rep = f"{TMP}/ld_orphans_{name}.lst"
            with open(rep, "w") as f:
                for rel in sorted(orphans, key=lambda r: -osz.get(r, 0)):
                    f.write(f"{osz.get(rel, 0)}\t{rel}\n")
            slack(f"• *{name}*: \U0001f5c2️ {len(orphans):,} UNAS-only files not on Synology "
                  f"(~{orphan_bytes/1e9:.1f} GB) — suggested for deletion, see {rep} "
                  f"(not deleted; requires NOVA_LOCALDIFF_PRUNE=1)")
        scan_s = int(time.time() - t0)

        # --- 1) push differing files (copy) ---
        if nto == 0:
            slack(f"• *{name}*: ✅ already in sync — src={nsrc:,} dst={ndst:,} (scanned in {scan_s}s)")
            record(name, 0, 0)
        else:
            slack(f"• *{name}*: {nto:,} files differ (src={nsrc:,} dst={ndst:,}, scan {scan_s}s) — rsyncing only those…")
            rc = rsync_files(name, src, cifs, tf)
            ok = rc in (0, 24)
            record(name, rc, nto)
            slack(f"• *{name}*: {'✅' if ok else '❌'} rsync rc={rc} on {nto:,} files "
                  f"(total {int(time.time()-t0)//60}m)")

        # --- 2) prune UNAS-only orphans so the backup mirrors source (UNAS ONLY) ---
        # DISABLED 2026-06-30: UNAS stores the share under nas/.data/ (UniFi-managed);
        # the raw-path comparison wrongly flags .data/ as orphans. Off until the
        # .data/ vs root:root duplication is reconciled. Opt-in via NOVA_LOCALDIFF_PRUNE=1.
        norph = len(orphans)
        if norph and os.environ.get("NOVA_LOCALDIFF_PRUNE") == "1":
            last = _last_counts().get(name, 0)
            # GATES: source enumeration must look complete, and the prune can't be absurd.
            if nsrc < 1000:
                slack(f"• *{name}*: ⚠️ prune skipped — source scan too small (src={nsrc:,})")
            elif last and nsrc < int(last * 0.7):
                slack(f"• *{name}*: ⚠️ prune skipped — source shrank vs last run ({nsrc:,} < 0.7×{last:,}); investigate before deleting")
            elif norph > int(ndst * 0.7):
                slack(f"• *{name}*: ⚠️ prune skipped — orphans {norph:,} > 70% of dst ({ndst:,}); suspicious")
            else:
                n, prc = prune_orphans(name, udst, orphans)
                slack(f"• *{name}*: 🧹 pruned {n:,} UNAS-only orphans (rc={prc}) — Synology untouched")
        if nsrc >= 1000:
            _save_count(name, nsrc)
    slack(":white_check_mark: *Local-find reconcile complete.*")


if __name__ == "__main__":
    # sys.exit(main()) so main()'s return code becomes the process exit code — the
    # scheduler was previously blind to failures because main() was called bare and
    # the process always exited 0 regardless of what happened inside.
    sys.exit(main())
