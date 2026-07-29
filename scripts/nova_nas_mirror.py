#!/usr/bin/env python3
"""nova_nas_mirror.py — make the UNAS an exact replica of the Synology, from an explicit list.

MODEL (Jordan, 2026-07-29): SYNOLOGY IS MASTER, UNAS IS REPLICA, one direction only. The master
is the statement of intent — including its deletions. A file absent from the Synology is absent
because Jordan removed it, and the replica's job is to agree.

FLOW (Jordan's, and it is the right one):
    1. manifest the master   (path + size)
    2. manifest the replica  (path + size)
    3. diff  -> an explicit COPY list and an explicit DELETE list
    4. execute those lists, and nothing outside them

Why not just `rsync --delete`? Because rsync re-walks both trees over SSH on every run — on 2.3M
files that took longer than the nightly window, and it decides what to destroy in flight where
nobody can inspect it first. Manifests are generated locally on each box (fast), the decision is
written to a file you can read, and the execution is bounded by that file.

A PRIOR VERSION OF THIS WAS WRONG, recorded so it is not reinvented: it had a "rescue" phase that
copied replica-only files back to the master, on the theory they might be failover writes. A
one-way replica cannot tell "written during failover" from "deleted on purpose, not yet caught
up" — and on 2026-07-29 that phase was about to haul 4.94 TB of deliberately-deleted video back
onto the master. Deleted is a fact to replicate, not damage to repair.

GUARDS, both checked before anything is destroyed:
  * master must still hold >= MIN_FILES        — a dropped mount looks exactly like a mass delete
  * delete list must be <= MAX_DELETE_FRACTION — a truncated master manifest would otherwise
                                                 propose erasing most of the replica
"""
import argparse
import os
import subprocess
import sys
import time

UNAS_HOST = "root@192.168.1.69"
UNAS_ROOT = "/volume/b37f2e84-517c-4a4f-92f0-4d642527ba17/.srv/.unifi-drive"
SSH_KEY = "/var/services/homes/kochj/.ssh/id_ed25519"
SSH = ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
       "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
WORK = "/volume1/nas/backups/mirror"
LOG = "/volume1/nas/backups/nas_mirror.log"

SHARES = [
    # name,      master path,          replica subdir
    ("nas",      "/volume1/nas",       "nas"),
    ("external", "/volume1/external",  "External"),
]
PRUNE = r'-name @eaDir -prune -o -name "#recycle" -prune -o -name .snapshot -prune -o'
MIN_FILES = 1000
MAX_DELETE_FRACTION = 0.35     # refuse to delete more than this share of the replica


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run(cmd, timeout=7200):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def manifest_local(root, out):
    """Master manifest, generated on this box."""
    cmd = f'find "{root}" {PRUNE} -type f -printf "%P\\t%s\\n" > "{out}" 2>/dev/null; exit 0'
    run(["sh", "-c", cmd])
    return load(out)


def manifest_remote(root, out):
    """Replica manifest, generated ON the replica then streamed back — never walked over SSH."""
    cmd = f'find "{root}" {PRUNE} -type f -printf "%P\\t%s\\n" 2>/dev/null'
    r = subprocess.run(SSH + [UNAS_HOST, cmd], capture_output=True, text=True, timeout=7200)
    with open(out, "w") as f:
        f.write(r.stdout)
    return load(out)


def load(path):
    d = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                p, _, s = line.rstrip("\n").rpartition("\t")
                if p and s.isdigit():
                    d[p] = int(s)
    except OSError:
        pass
    return d


def sync_share(name, master, subdir, dry, force):
    dst = f"{UNAS_ROOT}/{subdir}/.data"
    os.makedirs(WORK, exist_ok=True)
    mf, rf = f"{WORK}/{name}_master.tsv", f"{WORK}/{name}_replica.tsv"

    if not os.path.isdir(master):
        log(f"[{name}] SKIP — master {master} is not a directory (unmounted?)")
        return 1

    log(f"[{name}] 1/4 manifesting master {master}")
    m = manifest_local(master, mf)
    log(f"[{name}]     master  : {len(m):>9,} files  {sum(m.values())/1e12:6.2f} TB")

    if len(m) < MIN_FILES:
        log(f"[{name}] REFUSING — master has only {len(m)} files (<{MIN_FILES}).")
        log(f"[{name}] A dropped mount looks exactly like a mass delete.")
        return 1

    log(f"[{name}] 2/4 manifesting replica")
    r = manifest_remote(dst, rf)
    log(f"[{name}]     replica : {len(r):>9,} files  {sum(r.values())/1e12:6.2f} TB")
    if not r and len(m):
        log(f"[{name}] replica manifest empty — treating every master file as a copy")

    log(f"[{name}] 3/4 diffing")
    to_copy = [p for p, s in m.items() if p not in r or r[p] != s]
    to_del = [p for p in r if p not in m]
    cbytes = sum(m[p] for p in to_copy)
    dbytes = sum(r[p] for p in to_del)
    log(f"[{name}]     COPY  : {len(to_copy):>9,} files  {cbytes/1e9:8.1f} GB")
    log(f"[{name}]     DELETE: {len(to_del):>9,} files  {dbytes/1e9:8.1f} GB")

    # Write BOTH lists before any guard runs. The whole point of a list-driven mirror is that a
    # human can read the decision — a refusal that also blanks the evidence is useless, which is
    # exactly what the first version did.
    cl, dl = f"{WORK}/{name}_to_copy.txt", f"{WORK}/{name}_to_delete.txt"
    with open(cl, "w", encoding="utf-8") as f:
        f.writelines(p + "\n" for p in sorted(to_copy))
    with open(dl, "w", encoding="utf-8") as f:
        f.writelines(p + "\n" for p in sorted(to_del))
    log(f"[{name}]     lists -> {cl} / {dl}")

    frac = len(to_del) / max(len(r), 1)
    if to_del and frac > MAX_DELETE_FRACTION and not force:
        log(f"[{name}] REFUSING DELETE — {frac:.0%} of the replica ({len(to_del):,} files).")
        log(f"[{name}] That is over the {MAX_DELETE_FRACTION:.0%} ceiling. A truncated master")
        log(f"[{name}] manifest looks just like this. The list is written above — read it, then")
        log(f"[{name}] re-run with --force if it is genuinely right.")
        to_del = []
        if not to_copy:
            return 1

    if dry:
        log(f"[{name}] DRY RUN — executing nothing")
        return 0

    log(f"[{name}] 4/4 executing")
    rc = 0
    if to_copy:
        cp = run(["rsync", "-lt", "--partial", "--files-from", cl,
                  "-e", " ".join(SSH), f"{master}/", f"{UNAS_HOST}:{dst}/"], timeout=21600)
        if cp.returncode not in (0, 24):
            log(f"[{name}]     copy rsync rc={cp.returncode} {cp.stderr.strip()[:160]}")
            rc = 1
        else:
            log(f"[{name}]     copied {len(to_copy):,} files")
    if to_del:
        # Delete by explicit list on the replica; nothing outside the list is touched.
        # Trailing newline is REQUIRED. The remote loop is `while IFS= read -r f`, which
        # drops a final line with no terminator — so the last entry of every delete list
        # was silently skipped. Caught 2026-07-29 by replay: 27,571 of 27,572 deleted, and
        # the predicted survivor matched exactly.
        payload = "\n".join(to_del) + "\n"
        script = (f'cd "{dst}" || exit 1; n=0; '
                  f'while IFS= read -r f; do [ -n "$f" ] && rm -f -- "$f" && n=$((n+1)); done; '
                  f'echo "DELETED=$n"; find . -type d -empty -delete 2>/dev/null; exit 0')
        dp = subprocess.run(SSH + [UNAS_HOST, script], input=payload,
                            capture_output=True, text=True, timeout=21600)
        log(f"[{name}]     {dp.stdout.strip()[:120] or 'delete produced no output'}")
        if dp.returncode != 0:
            rc = 1
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="build the lists, execute nothing")
    ap.add_argument("--force", action="store_true", help="allow a delete list over the ceiling")
    ap.add_argument("--share", help="only this share (nas|external)")
    a = ap.parse_args()

    log(f"=== nas mirror start{' (DRY RUN)' if a.dry_run else ''} — synology MASTER -> unas REPLICA ===")
    rc = 0
    for name, master, sub in SHARES:
        if a.share and a.share != name:
            continue
        rc |= sync_share(name, master, sub, a.dry_run, a.force)
    log(f"=== nas mirror end rc={rc} ===")
    return rc


if __name__ == "__main__":
    sys.exit(main())
