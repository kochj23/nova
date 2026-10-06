#!/usr/bin/env python3
"""Tests for nova_nas_localdiff_reverse.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). ssh/find/rsync, PG and Slack are all mocked: no NAS is
touched, and the dry-run / resync-refusal / additive-only paths are proven.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_nas_localdiff_reverse.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_nas_localdiff_reverse_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nr = _load()
UNAS_FILES = "a.txt\t10\nb.txt\t20\n#recycle/old\t5\nnew.txt\t7\n"
SYNO_FILES = "a.txt\t10\nb.txt\t99\nstale.txt\t3\n"


class _Fake:
    """Answers every ssh call by content; records the commands."""
    def __init__(self, resync=False, rsync_rc=0):
        self.calls, self.resync, self.rsync_rc = [], resync, rsync_rc

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        remote = cmd[-1]
        if "find ." in remote:
            kw["stdout"].write(UNAS_FILES if nr.UNAS in cmd else SYNO_FILES)
            kw["stdout"].flush()
            return subprocess.CompletedProcess(cmd, 0)
        if "mdstat" in remote:
            return subprocess.CompletedProcess(cmd, 0, stdout="md2 : active\n [>....] resync = 3.2%" if self.resync else "md2 : active", stderr="")
        if remote.startswith("rsync"):
            return subprocess.CompletedProcess(cmd, 0, stdout=f"stats\nRC={self.rsync_rc}\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def rsyncs(self):
        return [c for c in self.calls if c[-1].startswith("rsync")]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ps = [patch.object(nr, "TMP", self.tmp.name), patch.object(nr.nova_config, "post_both"),
                   patch.object(nr.psycopg2, "connect")]
        _, self.post, self.pg = [p.start() for p in self.ps]
        self._r = redirect_stdout(io.StringIO())
        self.out = self._r.__enter__()

    def tearDown(self):
        self._r.__exit__(None, None, None)
        for p in self.ps:
            p.stop()
        self.tmp.cleanup()

    def run_main(self, argv, fake):
        with patch.object(sys, "argv", ["x"] + argv), patch.object(nr.subprocess, "run", side_effect=fake):
            rc = nr.main()
        return rc, [c[0][0] for c in self.post.call_args_list]


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")
        self.assertIn("BatchMode=yes", SRC)

    def test_default_is_dry_run_no_rsync(self):
        fake = _Fake()
        rc, posts = self.run_main([], fake)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.rsyncs(), [])
        self.assertTrue(any("DRY RUN, nothing changed" in p for p in posts))
        self.pg.assert_not_called()

    def test_resync_refuses_to_write(self):
        fake = _Fake(resync=True)
        rc, posts = self.run_main(["--apply"], fake)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.rsyncs(), [])
        self.assertIn("deferred", posts[0])

    def test_rsync_never_deletes(self):
        fake = _Fake()
        self.run_main(["--apply", "--allow-deletes", "nas"], fake)
        self.assertEqual(len(fake.rsyncs()), 1)
        self.assertNotIn("--delete", fake.rsyncs()[0][-1])


class TestPerformance(_Base):
    def test_load_sizes_100k(self):
        p = Path(self.tmp.name) / "big.lst"
        p.write_text("".join(f"dir{i % 100}/file{i}.bin\t{i}\n" for i in range(100_000)))
        t0 = time.perf_counter()
        d = nr.load_sizes(str(p))
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(d), 100_000)


class TestRetry(_Base):
    def test_record_fails_open(self):
        # RETRY GAP: record()/psycopg2.connect — single attempt; telemetry failure never breaks the run
        self.pg.side_effect = OSError("pg down")
        nr.record("nas", 0, 3)
        self.assertIn("telemetry write failed", self.out.getvalue())

    def test_rsync_without_rc_marker_is_99(self):
        # RETRY GAP: rsync_files — one attempt; unparseable output -> rc 99, reported not retried
        lst = Path(self.tmp.name) / "to.lst"
        lst.write_text("a\n")
        with patch.object(nr.subprocess, "run", return_value=subprocess.CompletedProcess([], 255, stdout="", stderr="")) as run:
            self.assertEqual(nr.rsync_files("nas", "/src", "/dst", str(lst), False), 99)
        self.assertEqual(run.call_count, 2)


class TestUnit(_Base):
    def test_exclusions(self):
        for bad in ("#recycle/x", "a/#recycle/x", "x/@eaDir/y", "a/.DS_Store", "Foo.app/Contents/x", "#snapshot/1"):
            self.assertTrue(nr.EXCL.search(bad), bad)
        self.assertIsNone(nr.EXCL.search("photos/recycle_bin.jpg"))

    def test_load_sizes_handles_tabs_in_name(self):
        p = Path(self.tmp.name) / "s.lst"
        p.write_text("weird\tname.txt\t42\n#recycle/x\t1\n")
        self.assertEqual(nr.load_sizes(str(p)), {"weird\tname.txt": "42"})


class TestIntegration(_Base):
    def test_record_writes_backup_runs_parameterized(self):
        cur = MagicMock()
        self.pg.return_value.cursor.return_value.__enter__.return_value = cur
        nr.record("nas", 23, 5)
        sql, params = cur.execute.call_args[0]
        self.assertIn("INSERT INTO telemetry.backup_runs", sql)
        self.assertEqual(params, ("nova-backup:nas:reverse", 23, 5, 1, False))

    def test_slack_goes_to_feed(self):
        nr.slack("hi")
        self.assertEqual(self.post.call_args.kwargs["slack_channel"], nr.nova_config.SLACK_FEED)


class TestFunctional(_Base):
    def test_apply_copies_only_delta(self):
        fake = _Fake()
        rc, posts = self.run_main(["--apply", "nas"], fake)
        self.assertEqual(rc, 0)
        delta = (Path(self.tmp.name) / "rev_to_nas.lst").read_text().split()
        self.assertEqual(sorted(delta), ["b.txt", "new.txt"])
        self.assertTrue(any("2 to copy, 1 replica-only" in p for p in posts))
        self.assertTrue(any("left in place" in p for p in posts))
        self.assertTrue(posts[-1].startswith(":white_check_mark:"))

    def test_empty_find_skips_job(self):
        def fake(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        rc, posts = self.run_main(["external"], fake)
        self.assertEqual(rc, 0)
        self.assertTrue(any("find produced no output" in p for p in posts))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_nas_localdiff_reverse; print('ok')"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
