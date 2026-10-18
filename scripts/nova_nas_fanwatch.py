#!/usr/bin/env python3
"""nova_nas_fanwatch.py — the UNAS-side change watcher for UNAS->Synology replication.

NOT installed on the UNAS (a UniFi appliance; firmware updates wipe it). The Mac-side daemon
(nova_nas_watchd.py) streams this file in over SSH and runs it in memory:

    ssh root@192.168.1.69 'nice -n 19 ionice -c3 python3 - <share>=<abs .data root> ...' < nova_nas_fanwatch.py

READ-ONLY on the UNAS: it never creates, writes, renames or deletes anything. It only reads
fanotify events and lstat()s the files they name. Stdlib only, Python 3.9 (Debian 11).

Why fanotify, and which mode (measured on the UNAS, kernel 5.10.216, 2026-10-17):
  * Per-directory inotify is out: `nas` has ~2.29M directories (one watch each, >2 GB kernel memory)
    against max_user_watches=8192.
  * FAN_MARK_FILESYSTEM + FAN_REPORT_DFID_NAME (one mark, names + renames + deletes) fails with
    EXDEV. The share volume is a btrfs SUBVOLUME (subvol=/@unifi, subvolid=257); btrfs gives each
    subvolume its own statfs fsid and 5.10's fanotify_test_fid() refuses FID-mode marks whose fsid
    differs from the superblock root's. Same for FAN_MARK_MOUNT in FID mode.
  * FAN_MARK_FILESYSTEM in CLASSIC (fd) mode with FAN_CLOSE_WRITE works: one mark covers the whole
    filesystem, each event carries an open fd, and readlink(/proc/self/fd/N) gives the path. This
    is what we use. It sees every write-and-close (new files, in-place edits, SMB copies). It does
    NOT see renames or deletes (no dirent events without FID mode); those are caught by the
    15-minute incremental pass (ctime) and the nightly full diff.
  The code still tries DFID_NAME first, so a future kernel that allows it gets renames for free.

Settle window: a path is emitted only when it is at least SETTLE_S old (max of mtime, ctime; ctime
cannot be set by SMB clients) and its (size, mtime, ctime) did not change between two looks
LOOK_GAP_S apart. Otherwise it is re-queued and looked at again later.

Output (stdout, NUL-terminated records, tab-separated fields):
    M\t<mode>\t<unas epoch>            watcher is up (mode: classic | dfid_name)
    H\t<unas epoch>\t<pending>\t<cpu%> heartbeat every HEARTBEAT_S (also detects a dead SSH: EPIPE)
    C\t<share>\t<rel>\t<mtime>\t<size>  a settled changed file
    O\t<unas epoch>                    kernel queue overflow or pending cap hit: events were lost
    E\t<message>                       fatal error, exits non-zero
"""
import ctypes
import errno
import heapq
import os
import select
import stat
import struct
import sys
import time

SETTLE_S = 60.0
LOOK_GAP_S = 3.0
HEARTBEAT_S = 30.0
MAX_PENDING = 200000          # bounded memory: beyond this, drop and report overflow
CPU_CAP = 0.25                # self-throttle above 25% of one core over a window
MEM_CAP_BYTES = 256 * 1024 * 1024
PROC_NAME = b"nova-fanwatch"

# fanotify constants (include/uapi/linux/fanotify.h)
FAN_CLOEXEC, FAN_NONBLOCK = 0x1, 0x2
FAN_REPORT_DIR_FID, FAN_REPORT_NAME = 0x400, 0x800
FAN_MARK_ADD, FAN_MARK_FILESYSTEM = 0x1, 0x100
FAN_CLOSE_WRITE, FAN_MOVED_TO, FAN_CREATE = 0x8, 0x80, 0x100
FAN_Q_OVERFLOW, FAN_ONDIR = 0x4000, 0x40000000
FAN_EVENT_INFO_TYPE_DFID_NAME = 2
AT_FDCWD = -100
O_PATH = 0o10000000
META = struct.Struct("IBBHQii")     # event_len, vers, reserved, metadata_len, mask, fd, pid
INFO_HDR = struct.Struct("BBH")     # info_type, pad, len
EXCLUDE_PARTS = ("@eaDir", "#recycle", "#snapshot", "#nova_deleted", ".DS_Store")


# ── pure logic (unit-tested on the Mac) ──────────────────────────────────────

def parse_roots(argv):
    """['nas=/v/.../nas/.data', ...] -> [('nas', '/v/.../nas/.data/'), ...] longest root first."""
    roots = []
    for a in argv:
        name, sep, root = a.partition("=")
        if not sep or not name or not root.startswith("/"):
            raise ValueError(f"bad root spec {a!r} (want name=/abs/path)")
        roots.append((name, root.rstrip("/") + "/"))
    return sorted(roots, key=lambda r: -len(r[1]))


def excluded(rel):
    parts = rel.split("/")
    if any(p in EXCLUDE_PARTS for p in parts):
        return True
    return any(p.endswith(".app") for p in parts[:-1])


def classify(path, roots):
    """Absolute path -> (share, rel) when it is under a watched .data root and not excluded."""
    if not path or path.endswith(" (deleted)"):
        return None
    for name, root in roots:
        if path.startswith(root):
            rel = path[len(root):]
            if rel and not excluded(rel):
                return name, rel
            return None
    return None


def record(*fields):
    """One NUL-terminated output record (fields tab-joined; NUL/newline-safe for paths)."""
    return ("\t".join(str(f) for f in fields) + "\0").encode("utf-8", "surrogateescape")


def parse_events(buf):
    """Classic-mode buffer -> [(mask, fd)] (fd = -1 for overflow)."""
    out, off = [], 0
    while off + META.size <= len(buf):
        ln, _v, _r, _ml, mask, fd, _pid = META.unpack_from(buf, off)
        if ln < META.size:
            break
        out.append((mask, fd))
        off += ln
    return out


def parse_dfid_events(buf):
    """FID-mode buffer -> [(mask, handle_bytes, handle_type, fsid, name)] (DFID_NAME records)."""
    out, off = [], 0
    while off + META.size <= len(buf):
        ln, _v, _r, ml, mask, _fd, _pid = META.unpack_from(buf, off)
        if ln < META.size:
            break
        i, end = off + ml, off + ln
        while i + INFO_HDR.size <= end:
            itype, _p, ilen = INFO_HDR.unpack_from(buf, i)
            if ilen == 0:
                break
            if itype == FAN_EVENT_INFO_TYPE_DFID_NAME:
                fsid = buf[i + 4:i + 12]
                hbytes, htype = struct.unpack_from("Ii", buf, i + 12)
                handle = buf[i + 20:i + 20 + hbytes]
                name = buf[i + 20 + hbytes:i + ilen].split(b"\0", 1)[0]
                out.append((mask, handle, htype, fsid, name.decode("utf-8", "surrogateescape")))
            i += ilen
        off += ln
    return out


class Settler:
    """Holds changed paths until they have settled; bounded; clock and stat are injectable.

    A min-heap of (next_check, seq, key) means each wakeup only touches the paths that are due.
    """

    def __init__(self, settle_s=SETTLE_S, gap_s=LOOK_GAP_S, max_pending=MAX_PENDING,
                 stat=os.lstat, clock=time.time):
        self.settle_s, self.gap_s, self.max_pending = settle_s, gap_s, max_pending
        self.stat, self.clock = stat, clock
        self.pending = {}          # (share, rel, abspath) -> [next_check, last_look or None]
        self.heap, self.seq = [], 0
        self.overflowed = False

    def _schedule(self, key, when):
        self.pending[key][0] = when
        self.seq += 1
        heapq.heappush(self.heap, (when, self.seq, key))

    def add(self, key):
        now = self.clock()
        if key in self.pending:
            self.pending[key][1] = None              # more activity: start the two looks over
            self._schedule(key, now + self.gap_s)
            return True
        if len(self.pending) >= self.max_pending:
            self.overflowed = True
            return False
        self.pending[key] = [0.0, None]
        self._schedule(key, now + self.gap_s)
        return True

    def due(self):
        now = self.clock()
        ready = []
        while self.heap and self.heap[0][0] <= now:
            when, _seq, key = heapq.heappop(self.heap)
            slot = self.pending.get(key)
            if slot is None or slot[0] != when:
                continue                              # superseded heap entry
            try:
                st = self.stat(key[2])
            except OSError:
                del self.pending[key]                 # gone before it settled
                continue
            if not stat.S_ISREG(st.st_mode):
                del self.pending[key]
                continue
            look = (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            age = now - max(st.st_mtime, st.st_ctime)
            if slot[1] == look and age >= self.settle_s:
                del self.pending[key]
                ready.append((key, st.st_mtime, st.st_size))
                continue
            slot[1] = look                            # first look, or it changed: look again
            self._schedule(key, now + max(self.gap_s, self.settle_s - age))
        return ready

    def next_wakeup(self, default):
        nxt = self.clock() + default
        return min(self.heap[0][0], nxt) if self.heap else nxt


class CpuGovernor:
    """Sleeps when this process has used more than `cap` of one core since the last window."""

    def __init__(self, cap=CPU_CAP, window=10.0, times=os.times, clock=time.monotonic,
                 sleep=time.sleep):
        self.cap, self.window, self.times, self.clock, self.sleep = cap, window, times, clock, sleep
        self.t0, self.c0, self.last_pct = clock(), self._cpu(), 0.0

    def _cpu(self):
        t = self.times()
        return t[0] + t[1]

    def tick(self):
        now = self.clock()
        if now - self.t0 < self.window:
            return 0.0
        used, wall = self._cpu() - self.c0, now - self.t0
        self.last_pct = used / wall if wall > 0 else 0.0
        pause = max(0.0, used / self.cap - wall) if self.last_pct > self.cap else 0.0
        if pause:
            self.sleep(min(pause, 30.0))
        self.t0, self.c0 = self.clock(), self._cpu()
        return pause


# ── fanotify plumbing (UNAS only) ────────────────────────────────────────────

def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.fanotify_init.argtypes = [ctypes.c_uint, ctypes.c_uint]
    libc.fanotify_mark.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_uint64, ctypes.c_int,
                                   ctypes.c_char_p]
    return libc


def open_fanotify(libc, mount_path):
    """-> (fd, mode). Tries DFID_NAME (renames too), falls back to classic CLOSE_WRITE."""
    path = mount_path.encode()
    tries = [
        ("dfid_name", FAN_CLOEXEC | FAN_REPORT_DIR_FID | FAN_REPORT_NAME,
         FAN_CLOSE_WRITE | FAN_MOVED_TO | FAN_CREATE | FAN_ONDIR),
        ("classic", FAN_CLOEXEC, FAN_CLOSE_WRITE),
    ]
    errs = []
    for mode, iflags, mask in tries:
        fd = libc.fanotify_init(iflags, os.O_RDONLY | getattr(os, "O_LARGEFILE", 0) | os.O_CLOEXEC)
        if fd < 0:
            errs.append(f"{mode} init {errno.errorcode.get(ctypes.get_errno(), '?')}")
            continue
        if libc.fanotify_mark(fd, FAN_MARK_ADD | FAN_MARK_FILESYSTEM, mask, AT_FDCWD, path) == 0:
            return fd, mode, errs
        errs.append(f"{mode} mark {errno.errorcode.get(ctypes.get_errno(), '?')}")
        os.close(fd)
    raise OSError("fanotify unavailable: " + "; ".join(errs))


class _Handle(ctypes.Structure):
    _fields_ = [("handle_bytes", ctypes.c_uint), ("handle_type", ctypes.c_int),
                ("f_handle", ctypes.c_char * 128)]


def resolve_dfid(libc, mount_fd, handle, htype, name):
    h = _Handle(len(handle), htype, handle)
    fd = libc.open_by_handle_at(mount_fd, ctypes.byref(h), O_PATH)
    if fd < 0:
        return None
    try:
        return os.path.join(os.readlink(f"/proc/self/fd/{fd}"), name)
    finally:
        os.close(fd)


def fd_path(fd):
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None


def ingest_classic(buf, roots, settler, fdpath=fd_path, close=os.close):
    """Classic-mode events -> settler. Every event fd is closed. Returns paths queued."""
    n = 0
    for mask, fd in parse_events(buf):
        if mask & FAN_Q_OVERFLOW or fd < 0:
            settler.overflowed = True
            continue
        try:
            path = fdpath(fd)
        finally:
            close(fd)
        hit = classify(path, roots)
        if hit and settler.add((hit[0], hit[1], path)):
            n += 1
    return n


def drain(settler):
    """Settled paths (+ an overflow notice) -> output bytes."""
    chunk = [record("C", share, rel, f"{mtime:.3f}", size)
             for (share, rel, _p), mtime, size in settler.due()]
    if settler.overflowed:
        chunk.append(record("O", f"{time.time():.3f}"))
        settler.overflowed = False
    return b"".join(chunk)


def emit(out, data):
    out.write(data)
    out.flush()


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python3 - <share>=<abs .data root> ...  (streamed over ssh by nova_nas_watchd.py)",
              file=sys.stderr)
        return 0 if argv else 2
    roots = parse_roots(argv)
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MEM_CAP_BYTES, MEM_CAP_BYTES))
    except Exception:
        pass
    libc = _libc()
    try:
        libc.prctl(15, PROC_NAME, 0, 0, 0)       # PR_SET_NAME, so the daemon can pkill -x a leftover
    except Exception:
        pass
    out = sys.stdout.buffer
    mount = roots[0][1]
    try:
        fan, mode, errs = open_fanotify(libc, mount)
    except OSError as e:
        emit(out, record("E", e))
        return 2
    mount_fd = os.open(mount, os.O_RDONLY | os.O_DIRECTORY) if mode == "dfid_name" else -1
    emit(out, record("M", mode, f"{time.time():.3f}", " / ".join(errs) or "-"))
    settler, gov = Settler(), CpuGovernor()
    last_hb = 0.0
    while True:
        now = time.time()
        if now - last_hb >= HEARTBEAT_S:
            emit(out, record("H", f"{now:.3f}", len(settler.pending), f"{gov.last_pct:.3f}"))
            last_hb = now
        timeout = max(0.2, min(settler.next_wakeup(HEARTBEAT_S), last_hb + HEARTBEAT_S) - now)
        r, _, _ = select.select([fan], [], [], timeout)
        if r:
            buf = os.read(fan, 262144)
            if mode == "classic":
                ingest_classic(buf, roots, settler)
            else:
                for mask, handle, htype, _fsid, name in parse_dfid_events(buf):
                    if mask & FAN_Q_OVERFLOW:
                        settler.overflowed = True
                        continue
                    if mask & FAN_ONDIR:
                        continue                      # dirs: the incremental pass handles them
                    path = resolve_dfid(libc, mount_fd, handle, htype, name)
                    hit = classify(path, roots)
                    if hit:
                        settler.add((hit[0], hit[1], path))
        data = drain(settler)
        if data:
            emit(out, data)
        gov.tick()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        sys.exit(0)                                   # the Mac side went away: just stop
    except KeyboardInterrupt:
        sys.exit(0)
