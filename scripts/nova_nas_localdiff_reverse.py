#!/usr/bin/env python3
"""nova_nas_localdiff_reverse.py — UNAS (primary) -> Synology (replica) replication.

Topology (confirmed by Jordan 2026-10-17): the UNAS Pro 8 (.69) is PRIMARY, files are written there.
The Synology (.11) is the SECONDARY replica. Everything here READS the UNAS and WRITES only the
Synology; nothing ever writes to, deletes from or renames anything on the UNAS.

  * SOURCE = UNAS, scanned locally over SSH at <share>/.data.
  * DEST   = Synology /volume1/<share>.
  * The copy runs ON the Synology, reading its CIFS mount of the UNAS (/volume1/docker/<share>)
    and writing the local /volume1/<share>.

Three ways in, one copy engine (all share one per-share lock, so they never rsync concurrently):
  --mode full         nightly 05:00. Scans both sides, copies missing/size-differing files.
                      Correctness backstop: catches anything the faster paths missed.
  --mode incremental  every 15 min. `find -newermt/-newerct <marker>` on the UNAS, copies only
                      what changed since the last SUCCESSFUL pass. The marker (PG service_config
                      nas_replicate/marker:<share>) advances only when the copy succeeds.
  copy_batch()        the watcher daemon (nova_nas_watchd.py): settled paths from fanotify on the
                      UNAS, copied seconds-to-a-minute after they land.

Settle window: files modified (mtime or ctime) in the last SETTLE_S (60 s) are skipped and
picked up by the next pass; the watcher re-checks size/mtime/ctime between two looks.

Deletes, backup-safe (--soft-deletes, default OFF): a file on the Synology that is gone from the
UNAS is MOVED on the Synology to /volume1/<share>/#nova_deleted/YYYY-MM-DD/<rel>, never removed.
That folder is excluded from every scan and copy and purged after --soft-delete-days (30).
Anti-wipe guards abort and alert (notify critical, category backup) when the UNAS scan is empty
or short, the CIFS mount is dead, or the moves exceed max(500, 1%) of the share. Without --apply
it only reports. The old hard-delete path (--allow-deletes) stays declared but is never run.

SAFETY: dry-run is the default (pass --apply to copy); resync-gated (refuses to write while the
Synology RAID resyncs, --force overrides).

    nova_nas_localdiff_reverse.py                         # DRY RUN, full, both shares
    nova_nas_localdiff_reverse.py --apply                 # nightly full copy (additive)
    nova_nas_localdiff_reverse.py --mode incremental --apply nas
    nova_nas_localdiff_reverse.py --soft-deletes nas      # report what soft-deletes WOULD move
Exit: 0 ok/deferred, 1 a copy failed, 3 (full mode) another run holds the share lock.
"""
import argparse
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

import psycopg2

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import nova_config

SYNO = "kochj@192.168.1.11"
UNAS = "root@192.168.1.69"
UROOT = "/volume/b37f2e84-517c-4a4f-92f0-4d642527ba17/.srv/.unifi-drive"
import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops", "connect_timeout=10")
TMP = os.path.expanduser("~/.openclaw/workspace/state/nas_localdiff_reverse")

# name, UNAS source (local .data scan), Synology dest (local scan + rsync target),
# Synology-side CIFS mount of the UNAS to READ from during the rsync.
JOBS = [
    ("nas",      f"{UROOT}/nas/.data",      "/volume1/nas",      "/volume1/docker/nas"),
    ("external", f"{UROOT}/External/.data", "/volume1/external", "/volume1/docker/external"),
]
DELETED = "#nova_deleted"
# 2026-09-24: anchor on (^|/) — top-level "#recycle/..." entries (no leading slash) slipped
# through and the reverse rsync failed rc=23 daily since 2026-09-20. 2026-10-17: + #nova_deleted.
EXCL = re.compile(r"@eaDir|(^|/)#recycle|(^|/)#snapshot|(^|/)#nova_deleted(/|$)|\.DS_Store$|\.app/")
PRUNE = r"\( -name '#recycle' -o -name '@eaDir' -o -name '#snapshot' -o -name '#nova_deleted' \) -prune -o"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4"]
SETTLE_S = 60
SOFT_DELETE_DAYS = 30
GUARD_MIN, GUARD_PCT = 500, 1.0     # soft-delete cap: max(500 files, 1% of the share)
SHORT_RATIO = 0.9                   # UNAS scan below 90% of the replica = short/partial scan
LOCK_STALE_S = 6 * 3600             # a lock holder older than this is stuck (rsync timeout is 4h)
ORPHAN_S = 6 * 3600                 # an orphaned Synology rsync older than this is killed
CONFIG_SERVICE = "nas_replicate"
QUIET = {"on": False}               # incremental/watch: Slack only on trouble


class Locked(Exception):
    """Another full/incremental/watch run holds this share."""


def slack(msg, force=False):
    print(msg, flush=True)
    if QUIET["on"] and not force:
        return
    try:
        nova_config.post_both(msg, slack_channel=nova_config.SLACK_FEED, discord_channel=None)
    except Exception as e:
        print(f"slack failed: {e}", flush=True)


def alert(title, body, level="critical", key=None):
    """Event bus (nova_notifier routes it). Never raises."""
    try:
        import nova_notify
        nova_notify.notify(title, body=body, level=level, category="backup",
                           source="nova_nas_localdiff_reverse", dedup_key=key)
    except Exception as e:
        print(f"notify failed: {e}", flush=True)
    slack(f":rotating_light: {title} — {body}", force=True)


def ssh(host, remote, timeout, **kw):
    """Every SSH call goes through here: BatchMode, keepalives and a hard timeout."""
    return subprocess.run(["ssh", *SSH_OPTS, host, remote], timeout=timeout, **kw)


# ── scans ────────────────────────────────────────────────────────────────────

def iter_records(path, chunk=1 << 20):
    """NUL-separated records from a file, decoded losslessly (surrogateescape)."""
    with open(path, "rb") as f:
        tail = b""
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            parts = (tail + buf).split(b"\0")
            tail = parts.pop()
            for p in parts:
                if p:
                    yield p.decode("utf-8", "surrogateescape")
        if tail:
            yield tail.decode("utf-8", "surrogateescape")


def find_to(host, path, outfile, timeout=2400):
    """Scan <path> on <host> into outfile as NUL records 'size\\tmtime\\tctime\\trel'.

    The first record is the host's own clock (so settle cutoffs never depend on clock skew).
    Returns (bytes written, host epoch or None)."""
    remote = (f"date +%s.%N; printf '\\0'; cd {path!r} && {_nice(host)} find . {PRUNE} "
              f"-type f -printf '%s\\t%T@\\t%C@\\t%P\\0'")
    with open(outfile, "wb") as f:
        ssh(host, remote, timeout, stdout=f, stderr=subprocess.DEVNULL)
    size = os.path.getsize(outfile)
    return size, _clock_of(outfile) if size else None


def _nice(host):
    """Lowest CPU (and on the UNAS, idle I/O) priority. The Synology has no ionice."""
    return "nice -n 19 ionice -c3" if host == UNAS else "nice -n 19"


def _clock_of(path):
    for rec in iter_records(path):
        try:
            return float(rec.strip())
        except ValueError:
            return None
    return None


def scan_records(path):
    """Yield (rel, size, mtime, ctime) from a find_to file, skipping the clock and exclusions."""
    first = True
    for rec in iter_records(path):
        if first:
            first = False
            try:
                float(rec.strip())
                continue
            except ValueError:
                pass
        parts = rec.split("\t", 3)
        if len(parts) != 4 or not parts[3] or EXCL.search(parts[3]):
            continue
        try:
            yield parts[3], parts[0], float(parts[1]), float(parts[2])
        except ValueError:
            continue


def load_sizes(path):
    return {rel: size for rel, size, _m, _c in scan_records(path)}


def write_list(path, rels):
    with open(path, "wb") as f:
        for r in rels:
            f.write(r.encode("utf-8", "surrogateescape") + b"\0")


def synology_resyncing():
    """Is the Synology RAID mid-resync? We write to it, so don't during a resync."""
    r = ssh(SYNO, "cat /proc/mdstat", 30, capture_output=True, text=True)
    return bool(re.search(r"(resync|recovery)\s*=", r.stdout))


# ── telemetry + marker (PG) ──────────────────────────────────────────────────

def record(name, rc, files, nbytes=0, elapsed=0, kind="reverse", errors=None):
    ok = rc in (0, 24)
    try:
        c = psycopg2.connect(DSN); c.autocommit = True
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO telemetry.backup_runs (ts,job,rc,elapsed_s,files,bytes,errors,ok) "
                "VALUES (now(),%s,%s,%s,%s,%s,%s,%s)",
                (f"nova-backup:{name}:{kind}", rc, int(elapsed), files, nbytes or 0,
                 (0 if ok else 1) if errors is None else errors, ok))
        c.close()
    except Exception as e:
        print(f"telemetry write failed: {e}", flush=True)


def get_marker(name):
    """Epoch (UNAS clock) up to which the last SUCCESSFUL incremental pass replicated, or None."""
    try:
        c = psycopg2.connect(DSN)
        with c.cursor() as cur:
            cur.execute("SELECT value FROM service_config WHERE service=%s AND key=%s",
                        (CONFIG_SERVICE, f"marker:{name}"))
            row = cur.fetchone()
        c.close()
        return float(row[0]["ts"]) if row else None
    except Exception as e:
        print(f"marker read failed: {e}", flush=True)
        raise


def get_config(key, default=None):
    """service_config nas_replicate/<key> value (jsonb), or default. Fails open."""
    try:
        c = psycopg2.connect(DSN)
        with c.cursor() as cur:
            cur.execute("SELECT value FROM service_config WHERE service=%s AND key=%s",
                        (CONFIG_SERVICE, key))
            row = cur.fetchone()
        c.close()
        return row[0] if row else default
    except Exception as e:
        print(f"config read failed ({key}): {e}", flush=True)
        return default


def put_config(key, value):
    try:
        c = psycopg2.connect(DSN); c.autocommit = True
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO service_config (service,key,value,updated_at,updated_by) "
                "VALUES (%s,%s,%s::jsonb,now(),'nova_nas_localdiff_reverse') "
                "ON CONFLICT (service,key) DO UPDATE SET value=EXCLUDED.value, updated_at=now(), "
                "updated_by=EXCLUDED.updated_by", (CONFIG_SERVICE, key, json.dumps(value)))
        c.close()
    except Exception as e:
        print(f"config write failed ({key}): {e}", flush=True)


def set_marker(name, ts, info):
    c = psycopg2.connect(DSN); c.autocommit = True
    with c.cursor() as cur:
        cur.execute(
            "INSERT INTO service_config (service,key,value,updated_at,updated_by) "
            "VALUES (%s,%s,%s::jsonb,now(),'nova_nas_localdiff_reverse') "
            "ON CONFLICT (service,key) DO UPDATE SET value=EXCLUDED.value, updated_at=now(), "
            "updated_by=EXCLUDED.updated_by",
            (CONFIG_SERVICE, f"marker:{name}", json.dumps({"ts": ts, **info})))
    c.close()


# ── the shared per-share lock ────────────────────────────────────────────────

def _cmdline(pid):
    try:
        return subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


class ShareLock:
    """flock on a local file: released by the kernel when the holder dies, so it cannot go stale
    on a crash. Stale guard for a holder that is alive but stuck: past LOCK_STALE_S the holder is
    TERMed, but only if its command line is one of ours."""

    def __init__(self, name, mode, wait_s=0.0, poll_s=2.0):
        self.name, self.mode, self.wait_s, self.poll_s = name, mode, wait_s, poll_s
        self.path = os.path.join(TMP, f"lock_{name}")
        self.fd = None

    def holder(self):
        try:
            with open(self.path) as f:
                return json.loads(f.read() or "{}")
        except (OSError, ValueError):
            return {}

    def __enter__(self):
        os.makedirs(TMP, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.time() + self.wait_s
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                h = self.holder()
                if h and time.time() - h.get("since", time.time()) > LOCK_STALE_S:
                    cmd = _cmdline(h.get("pid", 0))
                    if "nova_nas_localdiff_reverse" in cmd or "nova_nas_watchd" in cmd:
                        alert(f"NAS replicate: stale {self.name} lock", f"holder {h} stuck "
                              f">{LOCK_STALE_S // 3600}h — terminating it", "warning",
                              key=f"nas-replicate-stale-lock:{self.name}")
                        try:
                            os.kill(int(h["pid"]), 15)
                        except (OSError, ValueError, KeyError):
                            pass
                if time.time() >= deadline:
                    os.close(self.fd); self.fd = None
                    raise Locked(f"{self.name} lock held by {h or 'unknown'}")
                time.sleep(self.poll_s)
        os.ftruncate(self.fd, 0)
        os.write(self.fd, json.dumps({"pid": os.getpid(), "mode": self.mode,
                                      "since": time.time()}).encode())
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                os.ftruncate(self.fd, 0)
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd); self.fd = None
        return False


def remote_guard(name):
    """Remote half of the lock: an rsync from a dropped SSH session may still be running on the
    Synology. Younger than ORPHAN_S -> Locked; older -> killed as stale. Fails open."""
    try:
        r = ssh(SYNO, "ps -eo pid=,etimes=,args=", 30, capture_output=True, text=True)
    except Exception:
        return
    for line in r.stdout.splitlines():
        if f"files-from=/tmp/reverse_{name}_" not in line or "rsync" not in line:
            continue
        pid, etimes = line.split()[:2]
        if int(etimes) > ORPHAN_S:
            ssh(SYNO, f"kill {int(pid)}", 30, capture_output=True)
            print(f"killed orphaned rsync {pid} ({int(etimes) // 3600}h old)", flush=True)
        else:
            raise Locked(f"{name}: an rsync (pid {pid}, {etimes}s) is still running on the Synology")


# ── the copy engine ──────────────────────────────────────────────────────────

def parse_stats(text):
    """(files transferred, bytes transferred) from rsync --stats, or (None, None)."""
    def num(label):
        m = re.search(label + r":\s*([\d,]+)", text or "")
        return int(m.group(1).replace(",", "")) if m else None
    return num("Number of regular files transferred"), num("Total transferred file size")


def rsync_files(name, cifs_src, local_dst, tosync_file, tag="full"):
    """Copy the listed paths UNAS->Synology, run ON the Synology: read the UNAS CIFS mount
    (cifs_src), write the local replica (local_dst). Returns (rc, files, bytes)."""
    remote_list = f"/tmp/reverse_{name}_{tag}_{os.getpid()}.lst"
    with open(tosync_file, "rb") as f:
        ssh(SYNO, f"cat > {remote_list}", 300, stdin=f)
    # -rlt, no perms/owner (CIFS source can't carry them). --from0: NUL-separated list, so names
    # with newlines survive. --ignore-missing-args: a file deleted between scan and copy is noise,
    # not a failed backup (rsync 3.1.2 would otherwise exit 23). NO --delete, ever.
    p = ssh(SYNO,
            f"rsync -rlt --no-perms --no-owner --stats --ignore-missing-args --from0 "
            f"--files-from={remote_list} --exclude='/{DELETED}/' {cifs_src}/ {local_dst}/; "
            f"echo RC=$?; rm -f {remote_list}",
            14400, capture_output=True, text=True)
    m = re.search(r"RC=(\d+)", p.stdout or "")
    files, nbytes = parse_stats(p.stdout)
    return (int(m.group(1)) if m else 99), files, nbytes


def job_of(name):
    for j in JOBS:
        if j[0] == name:
            return j
    raise KeyError(name)


def copy_batch(name, rels, tag="watch", wait_s=600):
    """Targeted copy for the watcher daemon: settled relative paths -> Synology.
    Returns dict(rc, files, bytes, ok, n). Raises Locked if the share stays busy."""
    _name, _usrc, sdst, cifs = job_of(name)
    rels = [r for r in dict.fromkeys(rels) if r and not EXCL.search(r) and not r.startswith("/")
            and "/../" not in f"/{r}/"]
    if not rels:
        return {"rc": 0, "files": 0, "bytes": 0, "ok": True, "n": 0}
    os.makedirs(TMP, exist_ok=True)
    lst = os.path.join(TMP, f"batch_{name}_{tag}_{os.getpid()}.lst")
    write_list(lst, rels)
    try:
        with ShareLock(name, tag, wait_s=wait_s):
            remote_guard(name)
            rc, files, nbytes = rsync_files(name, cifs, sdst, lst, tag)
    finally:
        try:
            os.remove(lst)
        except OSError:
            pass
    return {"rc": rc, "files": files if files is not None else len(rels), "bytes": nbytes or 0,
            "ok": rc in (0, 24), "n": len(rels)}


def missing_dirs(sdst, dirs):
    """Which of these relative dirs do not exist on the Synology yet (new or renamed on the UNAS)?"""
    if not dirs:
        return []
    data = b"".join(d.encode("utf-8", "surrogateescape") + b"\0" for d in dirs)
    r = ssh(SYNO, f"cd {sdst!r} && bash -c 'while IFS= read -r -d \"\" d; do [ -d \"$d\" ] || "
                  f"printf \"%s\\0\" \"$d\"; done'", 300, input=data, capture_output=True)
    if r.returncode != 0:
        return []
    return [x.decode("utf-8", "surrogateescape") for x in r.stdout.split(b"\0") if x]


def run_incremental(name, usrc, sdst, cifs, apply, settle_s=SETTLE_S, lock_wait=300):
    """One incremental pass. Returns (ok, summary dict). The marker advances only on success."""
    t0 = time.time()
    marker = get_marker(name)
    bootstrap = marker is None
    if bootstrap:
        marker = time.time() - 26 * 3600        # first pass: everything since before last nightly
    since = int(marker) - 2                     # small overlap; rsync skips what's already there
    out = os.path.join(TMP, f"incr_{name}.lst")
    os.makedirs(TMP, exist_ok=True)
    remote = (f"date +%s.%N; printf '\\0'; cd {usrc!r} && nice -n 19 ionice -c3 find . {PRUNE} "
              f"\\( -type f -o -type d \\) \\( -newermt @{since} -o -newerct @{since} \\) "
              f"-printf '%y\\t%T@\\t%C@\\t%s\\t%P\\0'")
    with open(out, "wb") as f:
        p = ssh(UNAS, remote, 2400, stdout=f, stderr=subprocess.DEVNULL)
    unas_now = _clock_of(out) if os.path.getsize(out) else None
    if p.returncode != 0 or unas_now is None:
        return False, {"error": f"UNAS scan failed rc={p.returncode}", "marker": marker}
    cutoff = unas_now - settle_s
    files, dirs, deferred, first = [], [], 0, True
    for rec in iter_records(out):
        if first:
            first = False
            continue
        parts = rec.split("\t", 4)
        if len(parts) != 5 or not parts[4] or EXCL.search(parts[4]):
            continue
        kind, mt, ct, _size, rel = parts
        try:
            newest = max(float(mt), float(ct))
        except ValueError:
            continue
        if newest > cutoff:
            deferred += 1                       # still settling: the next pass's window has it
            continue
        (files if kind == "f" else dirs).append(rel)
    scan_s = time.time() - t0
    new_dirs = missing_dirs(sdst, dirs) if dirs else []
    todo = list(dict.fromkeys(files + new_dirs))
    summary = {"marker": marker, "cutoff": cutoff, "files": len(files), "dirs_changed": len(dirs),
               "dirs_new": len(new_dirs), "deferred": deferred, "todo": len(todo),
               "scan_s": round(scan_s, 1), "bootstrap": bootstrap}
    if not apply:
        summary["sample"] = todo[:20]
        return True, summary
    rc, nfiles, nbytes = 0, 0, 0
    if todo:
        tf = os.path.join(TMP, f"incr_to_{name}.lst")
        write_list(tf, todo)
        with ShareLock(name, "incremental", wait_s=lock_wait):
            remote_guard(name)
            rc, nfiles, nbytes = rsync_files(name, cifs, sdst, tf, "incr")
    ok = rc in (0, 24)
    summary.update(rc=rc, copied=nfiles if nfiles is not None else len(todo), bytes=nbytes or 0)
    record(name, rc, summary["copied"], summary["bytes"], time.time() - t0, kind="reverse-incr")
    if ok:
        set_marker(name, cutoff, {"at": datetime.now().isoformat(timespec="seconds"),
                                  "files": summary["copied"], "bytes": summary["bytes"]})
    return ok, summary


# ── soft deletes ─────────────────────────────────────────────────────────────

MOVER = r'''
import os, sys, time
lst, dst, cifs, trash = [a.encode() for a in sys.argv[1:5]]
moved = skipped = missing = errors = 0
for rel in open(lst, "rb").read().split(b"\0"):
    if not rel or rel.startswith(b"/") or b"/../" in b"/" + rel + b"/" or rel.split(b"/")[0] == b"#nova_deleted":
        continue
    if os.path.lexists(os.path.join(cifs, rel)):
        skipped += 1          # still on the UNAS (seen through the live mount): never move it
        continue
    src = os.path.join(dst, rel)
    if not os.path.lexists(src):
        missing += 1
        continue
    tgt = os.path.join(trash, rel)
    try:
        os.makedirs(os.path.dirname(tgt), exist_ok=True)
        if os.path.lexists(tgt):
            tgt += b"." + str(int(time.time())).encode()
        os.rename(src, tgt)
        moved += 1
    except OSError:
        errors += 1
os.remove(lst)
print("MOVED=%d SKIPPED=%d MISSING=%d ERRORS=%d" % (moved, skipped, missing, errors))
'''


def cifs_alive(cifs):
    """The Synology's CIFS view of the UNAS is mounted AND lists non-empty."""
    try:
        r = ssh(SYNO, f"grep -q ' {cifs} cifs ' /proc/mounts && timeout 30 ls -A {cifs!r} | head -3",
                60, capture_output=True, text=True)
    except Exception:
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def soft_delete_guard(nsrc, ndst, counted, mount_ok, prev_src=None):
    """-> None when safe, else the abort reason. 'Short' = below SHORT_RATIO of the previous full
    UNAS scan (PG nas_replicate/srccount:<share>); with no history, below half the replica."""
    if nsrc == 0:
        return "UNAS scan is empty"
    if prev_src and nsrc < SHORT_RATIO * prev_src:
        return f"UNAS scan looks short ({nsrc:,} vs {prev_src:,} last time, < {SHORT_RATIO:.0%})"
    if not prev_src and nsrc < 0.5 * ndst:
        return f"UNAS scan looks short ({nsrc:,} vs {ndst:,} on the replica, no history)"
    if not mount_ok:
        return "the Synology's CIFS mount of the UNAS is dead or empty"
    cap = max(GUARD_MIN, math.ceil(ndst * GUARD_PCT / 100))
    if counted > cap:
        return f"{counted:,} moves exceed the cap max({GUARD_MIN}, {GUARD_PCT:g}%) = {cap:,}"
    return None


def top_dirs(rels, n=8):
    counts = {}
    for r in rels:
        k = "/".join(r.split("/")[:2]) if "/" in r else "(top)"
        counts[k] = counts.get(k, 0) + 1
    return ", ".join(f"{k}={v:,}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:n])


def soft_delete(name, sdst, cifs, replica_only, nsrc, ndst, apply, churn=(), today=None,
                prev_src=None):
    """Move replica-only files into #nova_deleted/<today>/ on the Synology. Returns (status, detail)."""
    churn = tuple(churn)
    counted = [r for r in replica_only if not (churn and r.startswith(churn))]
    why = soft_delete_guard(nsrc, ndst, len(counted), cifs_alive(cifs) if replica_only else True,
                            prev_src)
    detail = f"{len(replica_only):,} replica-only ({top_dirs(replica_only)})"
    if why:
        if apply:
            alert(f"NAS soft-delete ABORTED ({name})", f"{why}; {detail}. Nothing was moved.",
                  key=f"nas-softdelete-abort:{name}")
        return "aborted", f"{why}; {detail}"
    if not replica_only:
        return "nothing", detail
    day = (today or date.today()).isoformat()
    if not apply:
        return "report", f"would move {len(replica_only):,} to {sdst}/{DELETED}/{day}/ — {detail}"
    lst = os.path.join(TMP, f"softdel_{name}.lst")
    write_list(lst, replica_only)
    remote_list = f"/tmp/reverse_softdel_{name}_{os.getpid()}.lst"
    with open(lst, "rb") as f:
        ssh(SYNO, f"cat > {remote_list}", 300, stdin=f)
    trash = f"{sdst}/{DELETED}/{day}"
    with ShareLock(name, "soft-delete", wait_s=1800):
        r = ssh(SYNO, f"python3 - {remote_list} {sdst!r} {cifs!r} {trash!r}", 7200,
                input=MOVER, capture_output=True, text=True)
    m = re.search(r"MOVED=(\d+) SKIPPED=(\d+) MISSING=(\d+) ERRORS=(\d+)", r.stdout or "")
    return ("moved" if m else "error"), (m.group(0) if m else f"mover failed: {r.stderr[-300:]}")


def purge_deleted(sdst, days, apply, today=None):
    """Remove #nova_deleted/YYYY-MM-DD folders older than `days` (Synology only). -> [purged]."""
    r = ssh(SYNO, f"ls -1 {sdst}/{DELETED!r} 2>/dev/null", 60, capture_output=True, text=True)
    limit = (today or date.today()) - timedelta(days=days)
    old = []
    for d in (r.stdout or "").split():
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            try:
                if datetime.strptime(d, "%Y-%m-%d").date() < limit:
                    old.append(d)
            except ValueError:
                pass
    if apply:
        for d in old:
            ssh(SYNO, f"rm -rf -- {sdst}/{DELETED!r}/{d}", 3600, capture_output=True)
    return old


# ── modes ────────────────────────────────────────────────────────────────────

def run_full(name, usrc, sdst, cifs, a):
    """Nightly full diff for one share. Returns True if the copy succeeded (or nothing to do)."""
    t0 = time.time()
    sf, df, tf = (f"{TMP}/rev_{k}_{name}.lst" for k in ("src", "dst", "to"))
    su, unas_now = find_to(UNAS, usrc, sf)
    sd, _ = find_to(SYNO, sdst, df)
    if su == 0 or sd == 0:
        slack(f":warning: {name}: find produced no output on one side — skipping.")
        return True
    dst = load_sizes(df)
    cutoff = (unas_now or time.time()) - a.settle
    src_set, todo, deferred, want_bytes = set(), [], 0, 0
    for rel, size, mt, ct in scan_records(sf):
        src_set.add(rel)
        if dst.get(rel) != size:              # missing on the replica OR size differs
            if max(mt, ct) > cutoff:
                deferred += 1                 # still being written: the next pass gets it
                continue
            todo.append(rel)
            want_bytes += int(size) if size.isdigit() else 0
    write_list(tf, todo)
    nsrc, ndst = len(src_set), len(dst)
    replica_only = [rel for rel in dst if rel not in src_set]
    head = (f"• *{name}*: UNAS={nsrc:,} Synology={ndst:,}, {len(todo):,} to copy "
            f"({want_bytes / 1e9:,.1f} GB), {deferred:,} settling, {len(replica_only):,} replica-only "
            f"(scan {int(time.time() - t0)}s)")
    ok = True
    if not a.apply:
        slack(head + " — DRY RUN, nothing changed.")
        if todo:
            print(f"{name}: first planned copies: {todo[:10]}", flush=True)
    elif not todo:
        slack(head + " — already in sync ✅")
        record(name, 0, 0, 0, time.time() - t0)
    else:
        slack(head + " — copying UNAS→Synology…")
        with ShareLock(name, "full", wait_s=1800):
            remote_guard(name)
            rc, files, nbytes = rsync_files(name, cifs, sdst, tf, "full")
        ok = rc in (0, 24)
        files = files if files is not None else len(todo)
        record(name, rc, files, nbytes or 0, time.time() - t0)
        slack(f"• *{name}*: {'✅' if ok else '❌'} copy rc={rc}: {files:,} files, "
              f"{(nbytes or 0) / 1e9:,.2f} GB ({int(time.time() - t0) // 60}m)")
    prev = get_config(f"srccount:{name}", {}) or {}
    if a.apply:
        put_config(f"srccount:{name}", {"n": nsrc, "at": datetime.now().isoformat(timespec="seconds")})
    if a.soft_deletes:
        status, detail = soft_delete(name, sdst, cifs, replica_only, nsrc, ndst, a.apply,
                                     a.churn_prefix or (), prev_src=prev.get("n"))
        slack(f"• *{name}*: soft-delete {status.upper()}{'' if a.apply else ' (report only)'}: {detail}")
        if a.apply and status == "moved":
            purged = purge_deleted(sdst, a.soft_delete_days, True)
            if purged:
                slack(f"• *{name}*: purged {DELETED}/ folders older than {a.soft_delete_days}d: {purged}")
    elif replica_only:
        slack(f"• *{name}*: 🗂️ {len(replica_only):,} files exist on the Synology replica but not the "
              f"UNAS primary — left in place ({top_dirs(replica_only, 4)}; --soft-deletes is OFF).")
    return ok


def parse_args(argv):
    ap = argparse.ArgumentParser(description="UNAS (primary) -> Synology (replica) replication.")
    ap.add_argument("share", nargs="?", choices=[j[0] for j in JOBS])
    ap.add_argument("--apply", action="store_true", help="actually copy (default: dry run)")
    ap.add_argument("--mode", choices=("full", "incremental"), default="full")
    ap.add_argument("--force", action="store_true", help="ignore the Synology RAID-resync gate")
    ap.add_argument("--allow-deletes", action="store_true",
                    help="legacy hard-delete flag: declared, never executed (use --soft-deletes)")
    ap.add_argument("--soft-deletes", action="store_true",
                    help="move replica-only files to #nova_deleted/<date>/ (report-only without --apply)")
    ap.add_argument("--soft-delete-days", type=int, default=SOFT_DELETE_DAYS)
    ap.add_argument("--churn-prefix", action="append",
                    help="path prefix whose deletes are expected churn (not counted by the cap)")
    ap.add_argument("--settle", type=float, default=SETTLE_S)
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(sys.argv[1:] if argv is None else argv)
    mode = "APPLY" if a.apply else "DRY-RUN"
    incr = a.mode == "incremental"
    QUIET["on"] = incr
    os.makedirs(TMP, exist_ok=True)
    if a.apply and not a.force and synology_resyncing():
        # Clean, EXPECTED skip — not a failure. Return 0 so the scheduler doesn't treat the
        # deferral as a failing job for the (multi-day) duration of a resync.
        slack(":hourglass_flowing_sand: reverse reconcile deferred — Synology RAID is resyncing; "
              "waiting for it to finish before writing (this is normal, not an error). Override: --force.")
        print("deferred cleanly: synology resyncing (exit 0, not a failure)", flush=True)
        return 0
    if not incr:
        slack(f":arrows_counterclockwise: *UNAS->Synology reverse reconcile ({mode})* — "
              f"{'copying' if a.apply else 'reporting only, no changes'}"
              f"{' +soft-deletes' if a.soft_deletes else ''}.")
    if a.allow_deletes:
        print("--allow-deletes is declared but never executed; use --soft-deletes", flush=True)
    failed = locked = False
    for name, usrc, sdst, cifs in JOBS:
        if a.share and name != a.share:
            continue
        try:
            if incr:
                ok, s = run_incremental(name, usrc, sdst, cifs, a.apply, a.settle)
                print(f"{name} incremental {mode}: {json.dumps(s, default=str)}", flush=True)
                if not ok:
                    alert(f"NAS incremental replicate failed ({name})", json.dumps(s, default=str)[:500],
                          "warning", key=f"nas-replicate-incr:{name}")
            else:
                ok = run_full(name, usrc, sdst, cifs, a)
            failed |= not ok
        except Locked as e:
            locked = True
            print(f"LOCKED: {e}", flush=True)
        except Exception as e:                  # one share's failure never skips the other
            failed = True
            alert(f"NAS replicate {a.mode} crashed ({name})", f"{type(e).__name__}: {e}"[:500],
                  "warning", key=f"nas-replicate-crash:{a.mode}:{name}")
    if not incr:
        slack(":white_check_mark: *Reverse reconcile complete.*" if not failed
              else ":x: *Reverse reconcile finished with a failed copy.*")
    # Locked: full -> 3 (nova_selfcheck treats it as "already running"); incremental -> 0, the next
    # 15-min pass covers the same window (the marker did not move), so it is a clean skip.
    return 1 if failed else (3 if locked and not incr else 0)


if __name__ == "__main__":
    sys.exit(main())
