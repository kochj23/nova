#!/usr/bin/env python3
"""nova_nas_localdiff_reverse.py — the UNAS->Synology reconcile for AFTER cutover.

DRAFT for the UNAS-primary migration (see the migration runbook / memory
unas-primary-migration). This is the mirror-image of nova_nas_localdiff.py:

    localdiff.py  (today):   Synology (primary)  --->  UNAS (replica)
    THIS script   (cutover): UNAS   (primary)    --->  Synology (replica)

Once the UNAS becomes primary, the Synology becomes the replica and this keeps it
current. It reuses the exact same plumbing as localdiff, just swapped:
  * SOURCE (authoritative) = UNAS, scanned locally on the UNAS at <share>/.data.
  * DEST (replica)         = Synology /volume1/<share>.
  * The copy runs ON the Synology, READING from its existing CIFS mount of the UNAS
    (/volume1/docker/<share>) and WRITING to the local /volume1/<share>. Same mount,
    reversed direction — no new plumbing, and it inherits the immutable-mountpoint +
    guard protection (see memory: unas-mirror-cifs-mount-and-immutable-fix).

SAFETY — this is a draft that must be harmless to run by accident:
  * DEFAULT IS DRY-RUN. It scans and reports the delta and changes NOTHING. You must
    pass --apply to actually copy.
  * ADDITIVE ONLY. Files on the Synology that are absent from the UNAS (i.e. deleted
    on the new primary) are REPORTED, never deleted — until parity is proven and you
    explicitly pass --allow-deletes (which also requires --apply).
  * RESYNC-GATED. It writes to the Synology, so it refuses to run while the Synology
    RAID is resyncing (don't pile writes on a resyncing array). Override: --force.

    nova_nas_localdiff_reverse.py                 # DRY RUN, both shares (default)
    nova_nas_localdiff_reverse.py nas             # DRY RUN, nas only
    nova_nas_localdiff_reverse.py --apply         # actually copy UNAS->Synology (additive)
    nova_nas_localdiff_reverse.py --apply --allow-deletes   # + mirror deletes (post-parity only)
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
TMP = os.path.expanduser("~/.openclaw/workspace/state/nas_localdiff_reverse")

# name, UNAS source (local .data scan), Synology dest (local scan + rsync target),
# Synology-side CIFS mount of the UNAS to READ from during the rsync.
JOBS = [
    ("nas",      f"{UROOT}/nas/.data",      "/volume1/nas",      "/volume1/docker/nas"),
    ("external", f"{UROOT}/External/.data", "/volume1/external", "/volume1/docker/external"),
]
# 2026-09-24: anchor on (^|/) — top-level "#recycle/..." entries (no leading slash) slipped
# through and the reverse rsync failed rc=23 daily since 2026-09-20.
EXCL = re.compile(r"@eaDir|(^|/)#recycle|(^|/)#snapshot|\.DS_Store$|\.app/")


def slack(msg):
    try:
        nova_config.post_both(msg, slack_channel=nova_config.SLACK_FEED, discord_channel=None)
    except Exception as e:
        print(f"slack failed: {e}", flush=True)


def find_to(host, path, outfile):
    """find <path> on <host>, stream 'relpath<TAB>size' into outfile; return bytes."""
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


def synology_resyncing():
    """Is the Synology RAID mid-resync? We write to it, so don't during a resync."""
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", SYNO, "cat /proc/mdstat"],
                       capture_output=True, text=True, timeout=30)
    return bool(re.search(r"(resync|recovery)\s*=", r.stdout))


def record(name, rc, files):
    ok = rc in (0, 24)
    try:
        c = psycopg2.connect(DSN); c.autocommit = True
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO telemetry.backup_runs (ts,job,rc,elapsed_s,files,bytes,errors,ok) "
                "VALUES (now(),%s,%s,0,%s,0,%s,%s)",
                (f"nova-backup:{name}:reverse", rc, files, 0 if ok else 1, ok))
        c.close()
    except Exception as e:
        print(f"telemetry write failed: {e}", flush=True)


def rsync_files(name, cifs_src, local_dst, tosync_file, allow_deletes):
    """Copy the differing files UNAS->Synology, run ON the Synology: read from the
    UNAS CIFS mount (cifs_src), write to the local replica (local_dst)."""
    remote_list = f"/tmp/reverse_{name}.lst"
    with open(tosync_file) as f:
        subprocess.run(["ssh", "-o", "BatchMode=yes", SYNO, f"cat > {remote_list}"],
                       stdin=f, timeout=120)
    # -rlt, no perms/owner (CIFS source can't carry them). --files-from limits to the
    # computed delta. NO --delete here regardless — deletes are handled separately and
    # only when explicitly allowed, so an additive run can never remove replica data.
    p = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", SYNO,
         f"rsync -rlt --no-perms --no-owner --stats --files-from={remote_list} "
         f"{cifs_src}/ {local_dst}/; echo RC=$?"],
        capture_output=True, text=True, timeout=14400)
    m = re.search(r"RC=(\d+)", p.stdout)
    return int(m.group(1)) if m else 99


def main():
    args = sys.argv[1:]
    apply = "--apply" in args
    allow_deletes = "--allow-deletes" in args
    force = "--force" in args
    only = next((a for a in args if a in ("nas", "external")), None)
    mode = "APPLY" if apply else "DRY-RUN"

    os.makedirs(TMP, exist_ok=True)
    if apply and not force and synology_resyncing():
        # Clean, EXPECTED skip — not a failure. Return 0 so the scheduler doesn't
        # treat the deferral as a failing job and retry/alert every cycle for the
        # (multi-day) duration of a resync. The reverse mirror simply waits its turn.
        slack(":hourglass_flowing_sand: reverse reconcile deferred — Synology RAID is resyncing; "
              "waiting for it to finish before writing (this is normal, not an error). Override: --force.")
        print("deferred cleanly: synology resyncing (exit 0, not a failure)", flush=True)
        return 0

    slack(f":arrows_counterclockwise: *UNAS->Synology reverse reconcile ({mode})* — "
          f"{'copying' if apply else 'reporting only, no changes'}"
          f"{' +deletes' if (apply and allow_deletes) else ''}.")

    for name, usrc, sdst_local, cifs in JOBS:
        if only and name != only:
            continue
        t0 = time.time()
        sf = f"{TMP}/rev_src_{name}.lst"   # UNAS (source of truth)
        df = f"{TMP}/rev_dst_{name}.lst"   # Synology (replica)
        tf = f"{TMP}/rev_to_{name}.lst"
        if find_to(UNAS, usrc, sf) == 0 or find_to(SYNO, sdst_local, df) == 0:
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
                if dst.get(rel) != size:      # missing on the Synology replica OR size differs
                    out.write(rel + "\n"); nto += 1
        nsrc, ndst = len(src_set), len(dst)
        # Synology-only files: present on the replica, gone from the UNAS primary.
        replica_only = [rel for rel in dst if rel not in src_set]
        scan_s = int(time.time() - t0)

        head = f"• *{name}*: UNAS={nsrc:,} Synology={ndst:,}, {nto:,} to copy, {len(replica_only):,} replica-only (scan {scan_s}s)"
        if not apply:
            slack(head + " — DRY RUN, nothing changed.")
            continue
        if nto == 0:
            slack(head + " — already in sync ✅"); record(name, 0, 0)
        else:
            slack(head + " — copying UNAS→Synology…")
            rc = rsync_files(name, cifs, sdst_local, tf, allow_deletes)
            ok = rc in (0, 24)
            record(name, rc, nto)
            slack(f"• *{name}*: {'✅' if ok else '❌'} copy rc={rc} on {nto:,} files ({int(time.time()-t0)//60}m)")
        if replica_only:
            if allow_deletes:
                slack(f"• *{name}*: ⚠️ {len(replica_only):,} replica-only files — delete-on-replica is "
                      f"declared but NOT auto-run in this draft (wire in only after parity is proven).")
            else:
                slack(f"• *{name}*: 🗂️ {len(replica_only):,} files exist on the Synology replica but not the "
                      f"UNAS primary — left in place (additive; pass --allow-deletes post-parity to prune).")
    slack(":white_check_mark: *Reverse reconcile complete.*")
    return 0


if __name__ == "__main__":
    sys.exit(main())
