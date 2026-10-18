#!/usr/bin/env python3
"""nova_nas_watchd.py — near-real-time UNAS (primary) -> Synology (replica) copies.

The feature Jordan liked most in DART: copy a file as soon as it settles, not at 05:00.

    UNAS (.69)                      this Mac (launchd KeepAlive)            Synology (.11)
    nova_nas_fanwatch.py  --ssh-->  nova_nas_watchd.py  --ssh rsync -->   /volume1/<share>
    (fanotify, in memory,           debounce + batch, shared lock,        reads its CIFS mount
     settle check, read-only)       telemetry, reconnect + catch-up       of the UNAS

* Nothing is installed on the UNAS: the watcher source is piped into `python3 -` over SSH on every
  connect (UniFi firmware updates wipe the box). It runs at nice 19 / ionice idle, RLIMIT_AS 256 MB,
  a 25%-of-one-core self-throttle, and a 24 h lifetime (then we reconnect).
* This daemon never touches this Mac's SMB mounts of the NASes (they have hung the kernel):
  it talks to both NASes over SSH only, and runs from a LOCAL install dir (INSTALL_DIR), not from the
  repo on the SMB share. `--install` copies the code there and writes the LaunchDaemon plist.
* Every (re)connect and every watcher overflow triggers a catch-up incremental pass
  (nova_nas_localdiff_reverse.run_incremental), because events may have been missed.
* Copies go through nova_nas_localdiff_reverse.copy_batch: the same per-share lock as the nightly
  full and the 15-min incremental, so they never rsync concurrently.
* Telemetry: one telemetry.backup_runs row per share per 15 min with activity,
  job nova-backup:<share>:reverse-watch (files, bytes, errors).

    nova_nas_watchd.py                 # run (what launchd does)
    nova_nas_watchd.py --dry-run       # watch and log what WOULD be copied, copy nothing
    nova_nas_watchd.py --install       # copy code to INSTALL_DIR + write/load the plist
"""
import argparse
import os
import queue
import random
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import nova_nas_localdiff_reverse as rev  # noqa: E402

LABEL = "net.digitalnoise.daemon.nova-nas-watchd"
INSTALL_DIR = "/Volumes/Data/nova-nas-replicate"
INSTALL_FILES = ("nova_nas_watchd.py", "nova_nas_fanwatch.py", "nova_nas_localdiff_reverse.py",
                 "nova_config.py", "nova_dsn.py", "nova_notify.py", "nova_secrets.py")
PLIST_TEMPLATE = "net.digitalnoise.daemon.nova-nas-watchd.plist"
WATCHER = HERE / "nova_nas_fanwatch.py"

DEBOUNCE_S = 2.0          # a path must be quiet this long in our queue before it is batched
BATCH_WINDOW_S = 5.0      # ...and the oldest ready path at least this old (lets a burst coalesce)
BATCH_MAX = 5000
MAX_ATTEMPTS = 20         # then drop it: the 15-min incremental pass will get it
HB_TIMEOUT_S = 100.0      # watcher heartbeats every 30 s
BACKOFF_MIN, BACKOFF_MAX = 5.0, 300.0
STABLE_S = 600.0          # a session this old resets the backoff
TELEMETRY_S = 900.0
LIFETIME_S = 86400
RESYNC_CHECK_S = 600.0


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def watcher_cmd():
    """Remote command: kill a leftover watcher, then run ours from stdin at the lowest priority."""
    roots = " ".join(f"{name}={usrc}" for name, usrc, _d, _c in rev.JOBS)
    return (f"pkill -x nova-fanwatch; ulimit -v 524288 2>/dev/null; "
            f"exec timeout {LIFETIME_S} nice -n 19 ionice -c3 python3 - {roots}")


def parse_record(rec):
    """b'C\\tnas\\trel\\tmtime\\tsize' -> ('C', [...]); unknown/short -> None."""
    try:
        text = rec.decode("utf-8", "surrogateescape")
    except Exception:
        return None
    parts = text.split("\t")
    kind = parts[0]
    if kind == "C" and len(parts) >= 5:
        # a path may contain tabs: share is field 1, mtime/size are the last two
        try:
            return "C", [parts[1], "\t".join(parts[2:-2]), float(parts[-2]), int(parts[-1])]
        except ValueError:
            return None
    if kind in ("M", "H", "O", "E"):
        return kind, parts[1:]
    return None


def split_records(buf):
    """Accumulated bytes -> (complete NUL records, leftover)."""
    parts = buf.split(b"\0")
    return [p for p in parts[:-1] if p], parts[-1]


class Batcher:
    """Per-share pending paths: debounce, coalesce, re-queue with delay, bounded attempts."""

    def __init__(self, debounce=DEBOUNCE_S, window=BATCH_WINDOW_S, batch_max=BATCH_MAX,
                 max_attempts=MAX_ATTEMPTS):
        self.debounce, self.window, self.batch_max, self.max_attempts = (
            debounce, window, batch_max, max_attempts)
        self.pending = {}      # share -> {rel: [first_seen, last_seen, mtime, attempts, not_before]}
        self.dropped = 0

    def add(self, share, rel, mtime, now):
        slot = self.pending.setdefault(share, {}).get(rel)
        if slot:
            slot[1], slot[2] = now, mtime
        else:
            self.pending[share][rel] = [now, now, mtime, 0, 0.0]

    def size(self):
        return sum(len(v) for v in self.pending.values())

    def ready(self, share, now):
        """Paths of `share` that are ready, or [] if the batch should wait a bit longer."""
        items = self.pending.get(share) or {}
        ok = [(rel, s) for rel, s in items.items() if now - s[1] >= self.debounce and now >= s[4]]
        if not ok:
            return []
        oldest = min(s[0] for _r, s in ok)
        if len(ok) < self.batch_max and now - oldest < self.window:
            return []
        ok.sort(key=lambda rs: rs[1][0])
        batch = ok[:self.batch_max]
        return [(rel, s[2]) for rel, s in batch]

    def take(self, share, rels):
        items = self.pending.get(share) or {}
        return {rel: items.pop(rel) for rel in rels if rel in items}

    def requeue(self, share, slots, now, delay):
        items = self.pending.setdefault(share, {})
        for rel, s in slots.items():
            s[3] += 1
            if s[3] >= self.max_attempts:
                self.dropped += 1
                continue
            s[4] = now + delay
            items.setdefault(rel, s)


class Stats:
    """Per-share counters flushed to telemetry.backup_runs every TELEMETRY_S."""

    def __init__(self, record=None):
        self.record = record or rev.record
        self.reset()

    def reset(self):
        self.t0 = time.time()
        self.data = {}

    def add(self, share, files=0, nbytes=0, errors=0, latencies=()):
        d = self.data.setdefault(share, {"files": 0, "bytes": 0, "errors": 0, "lat": []})
        d["files"] += files
        d["bytes"] += nbytes
        d["errors"] += errors
        d["lat"].extend(latencies)

    def flush(self):
        rows = []
        for share, d in self.data.items():
            if not (d["files"] or d["errors"]):
                continue
            rc = 0 if not d["errors"] else 23
            self.record(share, rc, d["files"], d["bytes"], time.time() - self.t0,
                        kind="reverse-watch", errors=d["errors"])
            lat = d["lat"]
            rows.append((share, d["files"], d["bytes"], d["errors"],
                         statistics.median(lat) if lat else None, max(lat) if lat else None))
        self.reset()
        return rows


class Backoff:
    def __init__(self, lo=BACKOFF_MIN, hi=BACKOFF_MAX, rand=random.random):
        self.lo, self.hi, self.rand, self.n = lo, hi, rand, 0

    def next(self):
        d = min(self.hi, self.lo * (2 ** self.n))
        self.n += 1
        return d * (0.8 + 0.4 * self.rand())

    def reset(self):
        self.n = 0


class ShareWorker(threading.Thread):
    """One per share, so a long nas copy never delays external. Jobs: ('batch', slots) or
    ('catchup', reason); catch-ups normally run on their own thread (Daemon.start_catchup)."""

    def __init__(self, share, daemon_ref):
        super().__init__(name=f"worker-{share}", daemon=True)
        self.share, self.d = share, daemon_ref
        self.q = queue.Queue()
        self.busy = threading.Event()
        self.catchup_needed = threading.Event()

    def run(self):
        while True:
            job = self.q.get()
            if job is None:
                return
            self.busy.set()
            try:
                if job[0] == "batch":
                    self.d.do_batch(self.share, job[1])
                else:
                    self.d.do_catchup(self.share, job[1])
            except Exception as e:                    # never let a worker die
                log(f"{self.share}: worker error {type(e).__name__}: {e}")
            finally:
                self.busy.clear()


class Daemon:
    def __init__(self, dry_run=False, spawn=None, copy=None, incremental=None, resyncing=None,
                 clock=time.time):
        self.dry_run = dry_run
        self.spawn = spawn or self._spawn
        self.copy = copy or rev.copy_batch
        self.incremental = incremental or (lambda share: rev.run_incremental(
            *rev.job_of(share), apply=True))
        self.resyncing = resyncing or rev.synology_resyncing
        self.clock = clock
        self.batcher, self.stats, self.backoff = Batcher(), Stats(), Backoff()
        self.lock = threading.Lock()                  # guards batcher (main + workers)
        self.shares = [j[0] for j in rev.JOBS]
        self.workers = {}
        self.proc = None
        self.inbox = queue.Queue()
        self.last_hb = 0.0
        self.offset = 0.0                             # mac clock - UNAS clock
        self.started = 0.0
        self.mode = None
        self.stop = threading.Event()
        self._resync = (0.0, False)

    # ── plumbing ──
    def _spawn(self):
        f = open(WATCHER, "rb")
        try:
            return subprocess.Popen(["ssh", *rev.SSH_OPTS, rev.UNAS, watcher_cmd()], stdin=f,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        finally:
            f.close()

    def _reader(self, proc):
        buf = b""
        while True:
            chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
            if not chunk:
                break
            recs, buf = split_records(buf + chunk)
            for r in recs:
                self.inbox.put((proc, r))
        self.inbox.put((proc, None))                  # EOF marker

    def connect(self):
        self.proc = self.spawn()
        self.started = self.last_hb = self.clock()
        self.mode = None
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        log(f"connected to watcher (pid {self.proc.pid})")

    def disconnect(self, why):
        if self.proc:
            log(f"watcher session ended: {why}")
            try:
                self.proc.kill()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=10)
            except Exception:
                pass
        self.proc = None
        if self.clock() - self.started > STABLE_S:
            self.backoff.reset()

    def request_catchup(self, reason):
        for share in self.shares:
            self.start_catchup(share, reason)

    def start_catchup(self, share, reason):
        """Catch-up runs in its own thread: its ~5 min UNAS scan holds no lock, so watcher batches
        keep flowing; only its rsync waits on the shared lock."""
        w = self.workers.get(share)
        if not w or w.catchup_needed.is_set():
            return
        w.catchup_needed.set()
        threading.Thread(target=self.do_catchup, args=(share, reason), daemon=True,
                         name=f"catchup-{share}").start()

    def resync_now(self):
        t, v = self._resync
        if self.clock() - t > RESYNC_CHECK_S:
            try:
                v = bool(self.resyncing())
            except Exception:
                v = False
            self._resync = (self.clock(), v)
        return v

    # ── jobs (worker threads) ──
    def do_batch(self, share, slots):
        rels = list(slots)
        if self.dry_run:
            log(f"{share}: DRY-RUN would copy {len(rels)}: {rels[:5]}")
            return
        if self.resync_now():
            with self.lock:
                self.batcher.requeue(share, slots, self.clock(), 300)
            return
        try:
            res = self.copy(share, rels)
        except rev.Locked as e:
            with self.lock:
                self.batcher.requeue(share, slots, self.clock(), 30)
            log(f"{share}: busy ({e}); {len(rels)} re-queued")
            return
        except Exception as e:
            res = {"ok": False, "rc": 99, "files": 0, "bytes": 0, "err": str(e)}
        if not res.get("ok"):
            with self.lock:
                self.batcher.requeue(share, slots, self.clock(), 60)
            self.stats.add(share, errors=1)
            log(f"{share}: batch of {len(rels)} FAILED rc={res.get('rc')} {res.get('err', '')}; re-queued")
            return
        done_unas = self.clock() - self.offset
        lat = [max(0.0, done_unas - s[2]) for s in slots.values() if s[2]]
        self.stats.add(share, res.get("files", 0), res.get("bytes", 0), 0, lat)
        p50 = f"{statistics.median(lat):.0f}s" if lat else "-"
        log(f"{share}: copied batch n={len(rels)} transferred={res.get('files')} "
            f"bytes={res.get('bytes')} latency p50={p50} max={max(lat):.0f}s" if lat else
            f"{share}: copied batch n={len(rels)} transferred={res.get('files')}")

    def do_catchup(self, share, reason):
        w = self.workers[share]
        if self.dry_run:
            ok, s = rev.run_incremental(*rev.job_of(share), apply=False)
            log(f"{share}: catch-up ({reason}) DRY-RUN: {s.get('todo')} to copy")
            w.catchup_needed.clear()
            return
        if self.resync_now():
            log(f"{share}: catch-up deferred, Synology resyncing")
            w.catchup_needed.clear()
            threading.Timer(300, self.start_catchup, args=(share, reason)).start()
            return
        try:
            ok, s = self.incremental(share)
        except rev.Locked as e:
            ok, s = False, {"error": f"locked: {e}"}
        except Exception as e:
            ok, s = False, {"error": f"{type(e).__name__}: {e}"}
        if ok:
            w.catchup_needed.clear()
            log(f"{share}: catch-up ({reason}) ok: {s.get('copied', 0)} copied, "
                f"{s.get('deferred', 0)} settling, scan {s.get('scan_s')}s")
        else:
            log(f"{share}: catch-up ({reason}) failed: {s.get('error') or s}; retry in 60s")
            w.catchup_needed.clear()
            threading.Timer(60, self.start_catchup, args=(share, reason)).start()

    # ── main loop ──
    def handle(self, rec):
        p = parse_record(rec)
        if not p:
            return
        kind, f = p
        now = self.clock()
        if kind == "C":
            with self.lock:
                self.batcher.add(f[0], f[1], f[2], now)
        elif kind == "H":
            self.last_hb = now
            try:
                self.offset = now - float(f[0])
            except (ValueError, IndexError):
                pass
        elif kind == "M":
            self.last_hb, self.mode = now, f[0]
            try:
                self.offset = now - float(f[1])
            except (ValueError, IndexError):
                pass
            log(f"watcher up: mode={f[0]} ({f[2] if len(f) > 2 else ''}); scheduling catch-up")
            self.request_catchup("reconnect")
        elif kind == "O":
            log("watcher reported overflow: events lost, scheduling catch-up")
            self.request_catchup("overflow")
        elif kind == "E":
            log(f"watcher error: {f}")

    def dispatch(self):
        now = self.clock()
        for share, w in self.workers.items():
            if w.busy.is_set() or not w.q.empty():
                continue
            with self.lock:
                ready = self.batcher.ready(share, now)
                if not ready:
                    continue
                slots = self.batcher.take(share, [r for r, _m in ready])
            w.q.put(("batch", slots))

    def step(self, timeout=1.0):
        """One loop iteration: drain records, check liveness, dispatch batches."""
        try:
            proc, rec = self.inbox.get(timeout=timeout)
            while True:
                if proc is self.proc:
                    if rec is None:
                        self.disconnect("EOF")
                    else:
                        self.handle(rec)
                proc, rec = self.inbox.get_nowait()
        except queue.Empty:
            pass
        if self.proc and self.clock() - self.last_hb > HB_TIMEOUT_S:
            self.disconnect(f"no heartbeat for {HB_TIMEOUT_S:.0f}s")
        self.dispatch()

    def run(self):
        for share in self.shares:
            self.workers[share] = ShareWorker(share, self)
            self.workers[share].start()
        last_flush = self.clock()
        next_connect = 0.0
        while not self.stop.is_set():
            if self.proc is None and self.clock() >= next_connect:
                try:
                    self.connect()
                except Exception as e:
                    delay = self.backoff.next()
                    log(f"connect failed: {e}; retry in {delay:.0f}s")
                    next_connect = self.clock() + delay
            had = self.proc
            self.step()
            if had is not None and self.proc is None:      # the session just dropped
                delay = self.backoff.next()
                next_connect = self.clock() + delay
                log(f"reconnecting in {delay:.0f}s")
            if self.clock() - last_flush >= TELEMETRY_S:
                for row in self.stats.flush():
                    log(f"telemetry {row[0]}: files={row[1]} bytes={row[2]} errors={row[3]} "
                        f"latency p50={row[4]} max={row[5]} pending={self.batcher.size()} "
                        f"dropped={self.batcher.dropped}")
                last_flush = self.clock()
        self.disconnect("stopping")
        self.stats.flush()


def install():
    """Copy the code to INSTALL_DIR (local disk) and (re)load the LaunchDaemon."""
    os.makedirs(INSTALL_DIR, exist_ok=True)
    for name in INSTALL_FILES:
        src = HERE / name
        if src.exists():
            shutil.copy2(src, os.path.join(INSTALL_DIR, name))
    plist = (HERE / PLIST_TEMPLATE).read_text().replace("__HOME__", str(Path.home())).replace(
        "__DIR__", INSTALL_DIR)
    tmp = os.path.join(INSTALL_DIR, PLIST_TEMPLATE)
    Path(tmp).write_text(plist)
    dest = f"/Library/LaunchDaemons/{LABEL}.plist"
    for cmd in (["sudo", "-n", "launchctl", "bootout", f"system/{LABEL}"],
                ["sudo", "-n", "install", "-m", "644", "-o", "root", "-g", "wheel", tmp, dest],
                ["sudo", "-n", "launchctl", "bootstrap", "system", dest]):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        print(" ".join(cmd[2:4]), "->", r.returncode, (r.stderr or "").strip()[:200])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="watch and log, copy nothing")
    ap.add_argument("--install", action="store_true", help="install to INSTALL_DIR + load plist")
    a = ap.parse_args(argv)
    if a.install:
        return install()
    rev.QUIET["on"] = True
    d = Daemon(dry_run=a.dry_run)
    signal.signal(signal.SIGTERM, lambda *_: d.stop.set())
    signal.signal(signal.SIGINT, lambda *_: d.stop.set())
    log(f"nova_nas_watchd starting ({'DRY-RUN' if a.dry_run else 'APPLY'}) from {HERE}")
    d.run()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
