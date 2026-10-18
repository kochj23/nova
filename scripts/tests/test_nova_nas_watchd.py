#!/usr/bin/env python3
"""Tests for nova_nas_watchd.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). No SSH, NAS, PG or Slack: the UNAS watcher is replaced by a local
subprocess that writes the same NUL record stream over a real pipe, and copies/incrementals are
mocked. Proves debounce/batching, re-queue on busy/failure, reconnect -> catch-up, overflow ->
catch-up, telemetry and that the daemon never touches the SMB mounts.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_nas_watchd.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_nas_watchd_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wd = _load()
# Nothing may reach Slack, PG or the notifier. Patched for the whole module, restored afterwards
# (rev is the shared nova_nas_localdiff_reverse module object).
_STUBS = [patch.object(wd.rev.nova_config, "post_both"), patch.object(wd.rev, "record"),
          patch.object(wd.rev, "alert")]
for _p in _STUBS:
    _p.start()


def tearDownModule():
    for p in _STUBS:
        p.stop()


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def fake_watcher(records, linger=0.0):
    """A local process that speaks the watcher protocol over a real pipe."""
    payload = repr(b"".join(r.encode() + b"\0" for r in records))
    code = (f"import sys,time; sys.stdout.buffer.write({payload}); sys.stdout.flush(); "
            f"time.sleep({linger})")
    return lambda: subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)


def wait_for(cond, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")

    def test_never_touches_the_smb_mounts(self):
        """The Mac's /Volumes/nas, /Volumes/external, /Volumes/NAS mounts have hung the kernel."""
        self.assertIsNone(re.search(r"/Volumes/(nas|external|NAS)\b", SRC))
        self.assertTrue(wd.INSTALL_DIR.startswith("/Volumes/Data/"))

    def test_watcher_runs_capped_and_ssh_is_batchmode(self):
        cmd = wd.watcher_cmd()
        for piece in ("pkill -x nova-fanwatch", "ulimit -v", "timeout 86400", "nice -n 19",
                      "ionice -c3", "python3 -"):
            self.assertIn(piece, cmd)
        self.assertIn("BatchMode=yes", wd.rev.SSH_OPTS)
        self.assertNotRegex(cmd, r"\b(rm|mv|cp|tee|mkdir)\b")


class TestPerformance(unittest.TestCase):
    def test_batcher_50k(self):
        b, t0 = wd.Batcher(), time.perf_counter()
        for i in range(50_000):
            b.add("nas", f"d/{i}", 1.0, 100.0)
        ready = b.ready("nas", 200.0)
        self.assertEqual(len(ready), wd.BATCH_MAX)
        b.take("nas", [r for r, _m in ready])
        self.assertEqual(b.size(), 50_000 - wd.BATCH_MAX)
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_split_records_streaming(self):
        data = b"".join(f"C\tnas\tf{i}\t1.0\t1\0".encode() for i in range(20_000))
        t0, buf, n = time.perf_counter(), b"", 0
        for i in range(0, len(data), 4096):
            recs, buf = wd.split_records(buf + data[i:i + 4096])
            n += len(recs)
        self.assertEqual((n, buf), (20_000, b""))
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_backoff_grows_and_caps(self):
        b = wd.Backoff(lo=5, hi=300, rand=lambda: 0.5)
        self.assertEqual([round(b.next()) for _ in range(8)], [5, 10, 20, 40, 80, 160, 300, 300])
        b.reset()
        self.assertEqual(round(b.next()), 5)

    def test_reconnect_triggers_catch_up_each_time(self):
        """Session 1 dies (EOF); session 2 comes up: catch-up must run after EVERY connect."""
        sessions = [fake_watcher(["M\tclassic\t1000.0\t-"]),
                    fake_watcher(["M\tclassic\t1000.0\t-", "H\t1001.0\t0\t0.0"], linger=5)]
        spawn = MagicMock(side_effect=[s() for s in sessions])
        inc = MagicMock(return_value=(True, {"copied": 0}))
        d = wd.Daemon(spawn=spawn, copy=MagicMock(), incremental=inc, resyncing=lambda: False)
        d.backoff = wd.Backoff(lo=0.05, hi=0.1)
        with redirect_stdout(io.StringIO()) as out:
            t = threading.Thread(target=d.run, daemon=True)
            t.start()
            self.assertTrue(wait_for(lambda: spawn.call_count == 2 and inc.call_count >= 4))
            d.stop.set()
            t.join(5)
        shares = sorted(c[0][0] for c in inc.call_args_list)
        self.assertEqual(shares[:4], ["external", "external", "nas", "nas"])
        self.assertIn("watcher session ended: EOF", out.getvalue())

    def test_busy_share_requeues_then_drops_after_max_attempts(self):
        clk = Clock()
        copy = MagicMock(side_effect=wd.rev.Locked("full run holds nas"))
        d = wd.Daemon(copy=copy, resyncing=lambda: False, clock=clk)
        d.batcher.max_attempts = 3
        slots = {"a.txt": [0, 0, 999.0, 0, 0.0]}
        with redirect_stdout(io.StringIO()):
            for _ in range(3):
                d.do_batch("nas", d.batcher.take("nas", ["a.txt"]) or slots)
        self.assertEqual(d.batcher.dropped, 1)
        self.assertEqual(copy.call_count, 3)

    def test_failed_copy_counts_error_and_requeues(self):
        clk = Clock()
        d = wd.Daemon(copy=MagicMock(return_value={"ok": False, "rc": 23}), resyncing=lambda: False,
                      clock=clk)
        with redirect_stdout(io.StringIO()):
            d.do_batch("nas", {"a.txt": [0, 0, 999.0, 0, 0.0]})
        self.assertIn("a.txt", d.batcher.pending["nas"])
        self.assertEqual(d.batcher.pending["nas"]["a.txt"][4], clk.t + 60)
        self.assertEqual(d.stats.data["nas"]["errors"], 1)

    def test_failed_catch_up_is_retried(self):
        d = wd.Daemon(incremental=MagicMock(return_value=(False, {"error": "scan failed"})),
                      resyncing=lambda: False)
        d.workers = {"nas": wd.ShareWorker("nas", d)}
        with patch.object(wd.threading, "Timer") as timer, redirect_stdout(io.StringIO()):
            d.do_catchup("nas", "reconnect")
        timer.assert_called_once()
        self.assertEqual(timer.call_args[0][0], 60)


class TestUnit(unittest.TestCase):
    def test_parse_record(self):
        self.assertEqual(wd.parse_record(b"C\tnas\ta\tb.txt\t12.5\t7"), ("C", ["nas", "a\tb.txt", 12.5, 7]))
        self.assertEqual(wd.parse_record(b"H\t1.0\t3\t0.01"), ("H", ["1.0", "3", "0.01"]))
        self.assertEqual(wd.parse_record(b"O\t5.0")[0], "O")
        for bad in (b"C\tnas\tonly", b"X\t1", b"C\tnas\tf\tnot-a-float\t1"):
            self.assertIsNone(wd.parse_record(bad), bad)

    def test_split_records_keeps_partial_tail(self):
        self.assertEqual(wd.split_records(b"a\0b\0par"), ([b"a", b"b"], b"par"))
        self.assertEqual(wd.split_records(b"\0\0"), ([], b""))

    def test_debounce_and_window(self):
        b = wd.Batcher(debounce=2, window=5)
        b.add("nas", "x", 1.0, 100.0)
        self.assertEqual(b.ready("nas", 101.0), [])     # not quiet for 2 s yet
        self.assertEqual(b.ready("nas", 103.0), [])     # quiet, but the batch window is still open
        b.add("nas", "x", 2.0, 103.0)                   # touched again: debounce restarts
        self.assertEqual(b.ready("nas", 104.0), [])
        self.assertEqual(b.ready("nas", 105.5), [("x", 2.0)])

    def test_batch_max_flushes_early(self):
        b = wd.Batcher(debounce=0, window=60, batch_max=3)
        for i in range(5):
            b.add("nas", f"f{i}", 1.0, 100.0)
        self.assertEqual(len(b.ready("nas", 100.0)), 3)

    def test_requeue_not_before(self):
        b = wd.Batcher(debounce=0, window=0)
        b.add("nas", "f", 1.0, 100.0)
        b.requeue("nas", b.take("nas", ["f"]), 100.0, 30)
        self.assertEqual(b.ready("nas", 120.0), [])
        self.assertEqual(b.ready("nas", 131.0), [("f", 1.0)])


class TestIntegration(unittest.TestCase):
    def test_defaults_use_the_shared_engine(self):
        d = wd.Daemon()
        self.assertIs(d.copy, wd.rev.copy_batch)
        self.assertIs(d.resyncing, wd.rev.synology_resyncing)
        self.assertEqual(d.shares, ["nas", "external"])

    def test_stats_flush_writes_reverse_watch_rows(self):
        rec = MagicMock()
        s = wd.Stats(record=rec)
        s.add("nas", files=3, nbytes=300, latencies=[60.0, 62.0, 70.0])
        s.add("external", errors=1)
        s.add("idle")
        rows = s.flush()
        kinds = {c.kwargs["kind"] for c in rec.call_args_list}
        self.assertEqual(kinds, {"reverse-watch"})
        self.assertEqual(sorted(c[0][0] for c in rec.call_args_list), ["external", "nas"])
        nas = [r for r in rows if r[0] == "nas"][0]
        self.assertEqual(nas[1:5], (3, 300, 0, 62.0))
        self.assertEqual(s.data, {})

    def test_install_files_cover_imports(self):
        for name in ("nova_nas_fanwatch.py", "nova_nas_localdiff_reverse.py", "nova_dsn.py",
                     "nova_config.py", "nova_notify.py"):
            self.assertIn(name, wd.INSTALL_FILES)
        plist = (SCRIPTS / wd.PLIST_TEMPLATE).read_text()
        self.assertIn(wd.LABEL, plist)
        self.assertIn("<key>KeepAlive</key>", plist)
        self.assertIn("__DIR__/nova_nas_watchd.py", plist)


class TestFunctional(unittest.TestCase):
    def test_events_over_a_pipe_become_one_batched_copy(self):
        recs = ["M\tclassic\t1000.0\tdfid_name mark EXDEV", "H\t1000.0\t0\t0.0",
                "C\tnas\tfrigate/a.mp4\t990.0\t10", "C\tnas\tfrigate/b.mp4\t991.0\t20",
                "C\tnas\tfrigate/a.mp4\t992.0\t10", "C\texternal\tv.ts\t993.0\t5", "O\t1001.0"]
        copy = MagicMock(return_value={"ok": True, "rc": 0, "files": 2, "bytes": 30, "n": 2})
        inc = MagicMock(return_value=(True, {"copied": 0}))
        d = wd.Daemon(spawn=fake_watcher(recs, linger=10), copy=copy, incremental=inc,
                      resyncing=lambda: False)
        d.batcher = wd.Batcher(debounce=0.05, window=0.1)
        with redirect_stdout(io.StringIO()) as out:
            t = threading.Thread(target=d.run, daemon=True)
            t.start()
            self.assertTrue(wait_for(lambda: copy.call_count >= 2))
            d.stop.set()
            t.join(5)
        batches = {c[0][0]: sorted(c[0][1]) for c in copy.call_args_list}
        self.assertEqual(batches, {"nas": ["frigate/a.mp4", "frigate/b.mp4"], "external": ["v.ts"]})
        self.assertGreaterEqual(inc.call_count, 2)         # connect + overflow -> catch-up
        self.assertIn("mode=classic", out.getvalue())
        self.assertEqual(d.stats.data.get("nas", {}).get("files", 2), 2)

    def test_batches_keep_flowing_during_a_slow_catch_up(self):
        release = threading.Event()
        inc = MagicMock(side_effect=lambda share: (release.wait(10), (True, {}))[1])
        copy = MagicMock(return_value={"ok": True, "rc": 0, "files": 1, "bytes": 1, "n": 1})
        recs = ["M\tclassic\t1000.0\t-", "C\tnas\tlive.mp4\t990.0\t1"]
        d = wd.Daemon(spawn=fake_watcher(recs, linger=10), copy=copy, incremental=inc,
                      resyncing=lambda: False)
        d.batcher = wd.Batcher(debounce=0.05, window=0.1)
        with redirect_stdout(io.StringIO()):
            t = threading.Thread(target=d.run, daemon=True)
            t.start()
            self.assertTrue(wait_for(lambda: copy.call_count >= 1))   # while catch-up still blocked
            self.assertFalse(release.is_set())
            release.set()
            d.stop.set()
            t.join(5)
        self.assertEqual(copy.call_args[0][:2], ("nas", ["live.mp4"]))

    def test_dry_run_copies_nothing(self):
        copy = MagicMock()
        d = wd.Daemon(dry_run=True, copy=copy, resyncing=lambda: False)
        with redirect_stdout(io.StringIO()) as out:
            d.do_batch("nas", {"a.txt": [0, 0, 1.0, 0, 0.0]})
        copy.assert_not_called()
        self.assertIn("DRY-RUN would copy 1", out.getvalue())

    def test_resync_defers_copy(self):
        copy = MagicMock()
        d = wd.Daemon(copy=copy, resyncing=lambda: True)
        d.do_batch("nas", {"a.txt": [0, 0, 1.0, 0, 0.0]})
        copy.assert_not_called()
        self.assertIn("a.txt", d.batcher.pending["nas"])

    def test_heartbeat_timeout_drops_session(self):
        clk = Clock()
        d = wd.Daemon(spawn=fake_watcher([], linger=10), clock=clk)
        with redirect_stdout(io.StringIO()) as out:
            d.connect()
            clk.t += wd.HB_TIMEOUT_S + 1
            d.step(timeout=0.05)
        self.assertIsNone(d.proc)
        self.assertIn("no heartbeat", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"}, cwd=str(SCRIPTS))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--install", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_nas_watchd; print('ok')"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.stdout.strip(), "ok", r.stderr)


if __name__ == "__main__":
    unittest.main()
