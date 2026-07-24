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

import psycopg2

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import nova_config

SYNO = "kochj@192.168.1.11"
UNAS = "root@192.168.1.69"
UROOT = "/volume/b37f2e84-517c-4a4f-92f0-4d642527ba17/.srv/.unifi-drive"
DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
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
EXCL = re.compile(r"@eaDir|/#recycle|/#snapshot|\.DS_Store$|\.app/|GoogleDriveBackups/Pics/Pictures/\.com-apple-bird-noname-")


def slack(msg):
    try:
        nova_config.post_both(msg, slack_channel=nova_config.SLACK_INFO, discord_channel=None)
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
    d = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            rel, _, size = line.rstrip("\n").rpartition("\t")
            if rel and not EXCL.search(rel):
                d[rel] = size
    return d


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


def main():
    os.makedirs(TMP, exist_ok=True)
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
        dst = load_sizes(df)
        src_set = set()
        nto = 0
        with open(sf, encoding="utf-8", errors="replace") as s, open(tf, "w") as out:
            for line in s:
                rel, _, size = line.rstrip("\n").rpartition("\t")
                if not rel or EXCL.search(rel):
                    continue
                src_set.add(rel)
                if dst.get(rel) != size:        # missing on dest OR size differs
                    out.write(rel + "\n"); nto += 1
        nsrc = len(src_set)
        ndst = len(dst)
        orphans = [rel for rel in dst if rel not in src_set]  # on UNAS, gone from Synology
        if orphans:
            orphan_bytes = sum(int(dst[rel]) for rel in orphans)
            rep = f"{TMP}/ld_orphans_{name}.lst"
            with open(rep, "w") as f:
                for rel in sorted(orphans, key=lambda r: -int(dst[r])):
                    f.write(f"{dst[rel]}\t{rel}\n")
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
    main()
