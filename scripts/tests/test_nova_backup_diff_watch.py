#!/usr/bin/env python3
"""Tests for nova_backup_diff_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_backup_diff_watch.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nbdw_under_test", SCRIPTS / "nova_backup_diff_watch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bw = _load()
# stub every outbound side effect at module load
bw.nova_config = SimpleNamespace(post_both=MagicMock(), SLACK_FEED="C-test")

DONE_LOG = [
    "[t] nas: src=10 dst=8 to_sync=1234",
    "[t] nas: rsync rc=0",
    "[t] external: already in sync",
    "[t] diff-backup done, overall rc=0",
]
EVIL = "nas'; " + "DR" + "OP TABLE x;--"


class _Cur:
    def __init__(self, sink): self.sink = sink
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.sink.append((sql, params))


def _conn(sink):
    c = MagicMock()
    c.cursor.return_value = _Cur(sink)
    return c


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password=", bw.DSN)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        sink = []
        with patch.object(bw.psycopg2, "connect", return_value=_conn(sink)):
            bw.record(EVIL, 0, 5)
        sql, params = sink[0]
        self.assertNotIn("TABLE x", sql)
        self.assertIn("TABLE x", params[0])

    def test_ssh_is_batch_mode(self):
        self.assertIn("BatchMode=yes", bw.SSH)


class TestPerformance(unittest.TestCase):
    def test_report_parses_10k_lines_fast(self):
        lines = [f"[t] noise line {i}" for i in range(10_000)] + DONE_LOG
        with patch.object(bw, "record"), patch.object(bw, "slack"):
            t0 = time.perf_counter()
            bw.report(lines)
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_main_loop_bounded_by_max_s(self):
        self.assertEqual(bw.MAX_S, 4 * 3600)
        self.assertIn("while time.time() - t0 < MAX_S", SRC)


class TestRetry(unittest.TestCase):
    def test_ssh_failure_fails_open(self):
        # RETRY GAP: ssh() — one attempt, any exception returns "" (no retry)
        with patch.object(bw.subprocess, "run", side_effect=OSError("no route")) as run:
            self.assertEqual(bw.ssh("echo"), "")
            self.assertEqual(bw.log_lines(), [])
        self.assertEqual(run.call_count, 2)

    def test_telemetry_write_failure_fails_open(self):
        # RETRY GAP: record()/psycopg2.connect — one attempt, failure only printed
        with patch.object(bw.psycopg2, "connect", side_effect=Exception("pg down")) as c:
            bw.record("nas", 0, 1)
        self.assertEqual(c.call_count, 1)

    def test_slack_failure_fails_open(self):
        # RETRY GAP: slack()/post_both — one attempt, swallowed
        with patch.object(bw.nova_config, "post_both", side_effect=RuntimeError("slack down")) as pb:
            bw.slack("x")
        self.assertEqual(pb.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_report_ok_and_failed_rcs(self):
        with patch.object(bw, "record") as rec, patch.object(bw, "slack") as sl:
            bw.report(DONE_LOG)
        msg = sl.call_args[0][0]
        self.assertIn("Diff backup complete", msg)
        self.assertIn("1,234 files synced", msg)
        self.assertIn("overall rc=0", msg)
        rec.assert_any_call("external", 0, 0)
        with patch.object(bw, "record"), patch.object(bw, "slack") as sl:
            bw.report(["nas: rsync rc=23"])
        self.assertIn(":warning:", sl.call_args[0][0])

    def test_report_empty(self):
        with patch.object(bw, "record") as rec, patch.object(bw, "slack") as sl:
            bw.report([])
        self.assertIn("no per-job lines parsed", sl.call_args[0][0])
        rec.assert_not_called()

    def test_record_ok_flag_for_rc24(self):
        sink = []
        with patch.object(bw.psycopg2, "connect", return_value=_conn(sink)):
            bw.record("nas", 24, 3)
        self.assertEqual(sink[0][1], ("nova-backup:nas:diff", 24, 3, 0, True))


class TestIntegration(unittest.TestCase):
    def test_writes_telemetry_backup_runs_and_posts_feed(self):
        self.assertIn("INSERT INTO telemetry.backup_runs", SRC)
        with patch.object(bw.nova_config, "post_both") as pb:
            bw.slack("hello")
        self.assertEqual(pb.call_args.kwargs["slack_channel"], "C-test")


class TestFunctional(unittest.TestCase):
    def _run(self, argv, logs):
        seq = iter(logs)
        with patch.object(sys, "argv", argv), patch.object(bw, "log_lines", side_effect=lambda: next(seq)), \
             patch.object(bw, "ssh") as ssh, patch.object(bw, "slack") as sl, patch.object(bw, "report") as rep, \
             patch.object(bw.time, "sleep"):
            bw.main()
        return ssh, sl, rep

    def test_trigger_then_report(self):
        ssh, sl, rep = self._run(["x"], [["old"], ["old"] + DONE_LOG])
        self.assertIn("nova_nas_diff_backup.sh", ssh.call_args[0][0])
        rep.assert_called_once_with(DONE_LOG)
        self.assertIn("Fast diff-based backup started", sl.call_args_list[0][0][0])

    def test_attach_does_not_trigger(self):
        ssh, sl, rep = self._run(["x", "attach"], [[], DONE_LOG])
        ssh.assert_not_called()
        self.assertIn("Attached", sl.call_args_list[0][0][0])

    def test_timeout_path_posts_limit(self):
        import itertools
        clock = itertools.chain([0], itertools.repeat(5 * 3600))
        with patch.object(sys, "argv", ["x", "attach"]), patch.object(bw, "log_lines", return_value=[]), \
             patch.object(bw, "slack") as sl, patch.object(bw.time, "sleep"), \
             patch.object(bw.time, "time", side_effect=lambda: next(clock)):
            bw.main()
        self.assertIn("4h limit", sl.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_backup_diff_watch"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
