#!/usr/bin/env python3
"""Tests for nova_nas_fanwatch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). The UNAS-side watcher is pure stdlib; macOS has no fanotify, so the
kernel layer is faked with real fanotify_event_metadata byte layouts and real temp files, and the
settle logic runs against a real filesystem. Written by Jordan Koch (via Claude)."""
import ast
import importlib.util
import os
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_nas_fanwatch.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_nas_fanwatch_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fw = _load()
ROOTS = fw.parse_roots(["nas=/v/u/nas/.data", "external=/v/u/External/.data"])


def _meta(mask, fd):
    return fw.META.pack(fw.META.size, 3, 0, fw.META.size, mask, fd, 1234)


def _records(data):
    return [r.decode().split("\t") for r in data.split(b"\0") if r]


class FakeStat:
    """Path -> (size, mtime) table; change it between looks."""
    def __init__(self):
        self.files = {}

    def __call__(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        size, mtime = self.files[path]
        return SimpleNamespace(st_mode=0o100644, st_size=size, st_mtime=mtime, st_ctime=mtime,
                               st_mtime_ns=int(mtime * 1e9), st_ctime_ns=int(mtime * 1e9))


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestSecurity(unittest.TestCase):
    def test_never_writes_the_unas(self):
        """Read-only by construction: no write-mode open, unlink, rename, mkdir, chmod or subprocess."""
        tree = ast.parse(SRC)
        banned = {"remove", "unlink", "rename", "replace", "rmdir", "mkdir", "makedirs", "chmod",
                  "chown", "truncate", "ftruncate", "symlink", "link", "utime", "system", "popen"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, banned, f"line {node.lineno}")
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open":
                self.fail(f"open() at line {node.lineno}")
        self.assertNotIn("subprocess", SRC)
        self.assertNotIn("shutil", SRC)
        self.assertNotIn("O_WRONLY", SRC)
        self.assertNotIn("O_RDWR", SRC)

    def test_stdlib_only_and_memory_capped(self):
        mods = {n.names[0].name.split(".")[0] for n in ast.walk(ast.parse(SRC)) if isinstance(n, ast.Import)}
        self.assertLessEqual(mods, {"ctypes", "errno", "heapq", "os", "select", "stat", "struct", "sys",
                                    "time", "resource"})
        self.assertIn("RLIMIT_AS", SRC)
        self.assertLessEqual(fw.MEM_CAP_BYTES, 512 * 1024 * 1024)

    def test_paths_outside_share_roots_are_ignored(self):
        for p in ("/v/u/nas/secret", "/v/u/nas/.database/x", "/etc/shadow", "/v/u/nas/.data",
                  "/v/u/nas/.data/x (deleted)", None, ""):
            self.assertIsNone(fw.classify(p, ROOTS), p)


class TestPerformance(unittest.TestCase):
    def test_settler_50k_paths(self):
        st, clk = FakeStat(), Clock()
        s = fw.Settler(settle_s=60, gap_s=3, stat=st, clock=clk)
        for i in range(50_000):
            p = f"/v/u/nas/.data/f{i}"
            st.files[p] = (i, 900.0)
            s.add(("nas", f"f{i}", p))
        t0 = time.perf_counter()
        clk.t += 3.1
        self.assertEqual(s.due(), [])            # first look only
        clk.t += 3.1
        self.assertEqual(len(s.due()), 50_000)   # second look: all settled
        self.assertLess(time.perf_counter() - t0, 3.0)

    def test_idle_wakeup_touches_nothing(self):
        st, clk = FakeStat(), Clock()
        s = fw.Settler(stat=MagicMock(side_effect=AssertionError("stat on idle wakeup")), clock=clk)
        for i in range(10_000):
            s.add(("nas", f"f{i}", f"/x/{i}"))
        clk.t += 1.0                             # nothing due yet
        self.assertEqual(s.due(), [])


class TestRetry(unittest.TestCase):
    def test_falls_back_to_classic_when_fid_mode_is_refused(self):
        """5.10 + btrfs subvolume: FID-mode marks return EXDEV; we fall back, not fail."""
        libc = MagicMock()
        libc.fanotify_init.side_effect = [7, 8]
        libc.fanotify_mark.side_effect = [-1, 0]
        closed = []
        orig = fw.os.close
        fw.os.close = closed.append
        try:
            fd, mode, errs = fw.open_fanotify(libc, "/v/u/nas/.data/")
        finally:
            fw.os.close = orig
        self.assertEqual((fd, mode), (8, "classic"))
        self.assertEqual(closed, [7])
        self.assertTrue(errs[0].startswith("dfid_name mark"))

    def test_unsettled_path_is_requeued_until_stable(self):
        st, clk = FakeStat(), Clock()
        p = "/v/u/nas/.data/grow.bin"
        st.files[p] = (1, 990.0)
        s = fw.Settler(settle_s=5, gap_s=1, stat=st, clock=clk)
        s.add(("nas", "grow.bin", p))
        for size in (2, 3, 4):                   # still growing at each look
            clk.t += 6
            st.files[p] = (size, clk.t - 6)
            self.assertEqual(s.due(), [])
        clk.t += 6
        self.assertEqual(len(s.due()), 1)        # unchanged between two looks and old enough

    def test_pending_cap_reports_overflow(self):
        s = fw.Settler(max_pending=2, stat=FakeStat(), clock=Clock())
        self.assertTrue(s.add(("nas", "a", "/a")))
        self.assertTrue(s.add(("nas", "b", "/b")))
        self.assertFalse(s.add(("nas", "c", "/c")))
        self.assertTrue(s.overflowed)
        self.assertEqual(_records(fw.drain(s))[0][0], "O")
        self.assertFalse(s.overflowed)


class TestUnit(unittest.TestCase):
    def test_parse_roots(self):
        self.assertEqual(ROOTS[0], ("external", "/v/u/External/.data/"))
        with self.assertRaises(ValueError):
            fw.parse_roots(["nas"])
        with self.assertRaises(ValueError):
            fw.parse_roots(["nas=relative/path"])

    def test_classify_and_exclusions(self):
        self.assertEqual(fw.classify("/v/u/nas/.data/a/b.txt", ROOTS), ("nas", "a/b.txt"))
        self.assertEqual(fw.classify("/v/u/External/.data/v.mp4", ROOTS), ("external", "v.mp4"))
        for rel in ("#recycle/x", "a/@eaDir/t.jpg", "a/.DS_Store", "#nova_deleted/2026-10-17/x",
                    "Foo.app/Contents/Info.plist", "#snapshot/x"):
            self.assertIsNone(fw.classify("/v/u/nas/.data/" + rel, ROOTS), rel)
        self.assertEqual(fw.classify("/v/u/nas/.data/my.app.notes.txt", ROOTS)[1], "my.app.notes.txt")

    def test_parse_events_classic(self):
        buf = _meta(fw.FAN_CLOSE_WRITE, 5) + _meta(fw.FAN_Q_OVERFLOW, -1) + b"\x00" * 3
        self.assertEqual(fw.parse_events(buf), [(fw.FAN_CLOSE_WRITE, 5), (fw.FAN_Q_OVERFLOW, -1)])
        self.assertEqual(fw.parse_events(b""), [])

    def test_parse_dfid_name_events(self):
        handle = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        name = b"file.txt\0"
        info_len = 4 + 8 + 8 + len(handle) + len(name)
        info = (fw.INFO_HDR.pack(fw.FAN_EVENT_INFO_TYPE_DFID_NAME, 0, info_len) + b"F" * 8 +
                struct.pack("Ii", len(handle), 1) + handle + name)
        ev = fw.META.pack(fw.META.size + len(info), 3, 0, fw.META.size, fw.FAN_CLOSE_WRITE, -1, 1) + info
        (mask, h, htype, fsid, n), = fw.parse_dfid_events(ev)
        self.assertEqual((mask, h, htype, fsid, n), (fw.FAN_CLOSE_WRITE, handle, 1, b"F" * 8, "file.txt"))

    def test_record_is_nul_terminated_and_newline_safe(self):
        r = fw.record("C", "nas", "odd\nname.txt", "1.000", 5)
        self.assertTrue(r.endswith(b"\0"))
        self.assertEqual(r.count(b"\0"), 1)
        self.assertIn(b"odd\nname.txt", r)

    def test_cpu_governor_throttles(self):
        clk = Clock(0.0)
        cpu = [0.0]
        slept = []
        g = fw.CpuGovernor(cap=0.25, window=10, times=lambda: (cpu[0], 0.0), clock=clk,
                           sleep=slept.append)
        clk.t, cpu[0] = 10.0, 5.0                # 50% of a core over 10 s
        self.assertGreater(g.tick(), 0)
        self.assertAlmostEqual(slept[0], 10.0)   # sleep until the average is back at 25%
        clk.t, cpu[0] = 20.1, 5.1
        self.assertEqual(g.tick(), 0.0)


class TestIntegration(unittest.TestCase):
    def test_settler_on_real_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "nas", ".data")
            os.makedirs(root)
            stable, gone = os.path.join(root, "stable.txt"), os.path.join(root, "gone.txt")
            for p in (stable, gone):
                Path(p).write_text("x")
            old = time.time() - 120
            os.utime(stable, (old, old))
            s = fw.Settler(settle_s=0.0, gap_s=0.05)
            s.add(("nas", "stable.txt", stable))
            s.add(("nas", "gone.txt", gone))
            os.remove(gone)
            out = []
            for _ in range(10):
                time.sleep(0.06)
                out += s.due()
            self.assertEqual([k[1] for k, _m, _s in out], ["stable.txt"])
            self.assertEqual(s.pending, {})

    def test_ingest_closes_every_fd(self):
        closed, s = [], fw.Settler(stat=FakeStat(), clock=Clock())
        paths = {5: "/v/u/nas/.data/a.txt", 6: "/etc/passwd", 7: None}
        buf = b"".join(_meta(fw.FAN_CLOSE_WRITE, fd) for fd in paths)
        n = fw.ingest_classic(buf, ROOTS, s, fdpath=paths.get, close=closed.append)
        self.assertEqual(n, 1)
        self.assertEqual(sorted(closed), [5, 6, 7])


class TestFunctional(unittest.TestCase):
    def test_event_to_settled_record(self):
        """fanotify buffer (real fds on real files) -> settle -> 'C' records on stdout."""
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "nas", ".data")
            os.makedirs(os.path.join(root, "frigate"))
            files = {}
            for name in ("frigate/seg1.mp4", "#recycle/junk", "frigate/seg2.mp4"):
                p = os.path.join(root, name)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                Path(p).write_bytes(b"v" * 10)
                old = time.time() - 300
                os.utime(p, (old, old))
                files[os.open(p, os.O_RDONLY)] = p
            roots = fw.parse_roots([f"nas={root}"])
            s = fw.Settler(settle_s=0.0, gap_s=0.05)
            buf = b"".join(_meta(fw.FAN_CLOSE_WRITE, fd) for fd in files)
            self.assertEqual(fw.ingest_classic(buf, roots, s, fdpath=files.get), 2)
            for fd in files:                     # ingest closed them all
                with self.assertRaises(OSError):
                    os.fstat(fd)
            out = b""
            for _ in range(10):
                time.sleep(0.06)
                out += fw.drain(s)
            recs = _records(out)
            self.assertEqual(sorted(r[2] for r in recs), ["frigate/seg1.mp4", "frigate/seg2.mp4"])
            self.assertTrue(all(r[0] == "C" and r[1] == "nas" and r[4] == "10" for r in recs))


class TestFrame(unittest.TestCase):
    def test_help_and_no_args(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True,
                           timeout=30, env=env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage", r.stderr)
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env=env)
        self.assertEqual(r.returncode, 2)

    def test_python39_compatible_syntax(self):
        ast.parse(SRC, feature_version=(3, 9))
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
