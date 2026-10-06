#!/usr/bin/env python3
"""Tests for nova_ssh_rsync_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Every ssh call, the PG insert and Slack are mocked; the watch loop runs with time.sleep and
time.time driven by the test, so nothing ever waits or reaches the Synology/UNAS."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_ssh_rsync_watch.py"
SRC = PATH.read_text()

import nova_config as _real_config  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("nova_ssh_rsync_watch_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rw = _load()
rw.nova_config = types.SimpleNamespace(post_both=mock.MagicMock(), SLACK_FEED="C_FEED")
TAIL_OK = ("Number of regular files transferred: 1,260,001\n"
           "Total transferred file size: 2,500,000,000 bytes\n")
TAIL_ERR = TAIL_OK + "rsync error: some files/attrs were not transferred (code 23)\n"
TAIL_BAD = "rsync error: error in socket IO (code 10)\n"


class _Clock:
    """Fake time: every sleep() advances the clock."""
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _Env(unittest.TestCase):
    def setUp(self):
        rw.nova_config.post_both.reset_mock(side_effect=True)
        self.clock = _Clock()
        self.pg = mock.MagicMock()
        cur = mock.MagicMock(); cur.__enter__.return_value = cur
        self.pg.cursor.return_value = cur
        self.cur = cur
        ps = {"time": mock.patch.object(rw.time, "time", side_effect=self.clock.time),
              "sleep": mock.patch.object(rw.time, "sleep", side_effect=self.clock.sleep),
              "run": mock.patch.object(rw.subprocess, "run"),
              "pg": mock.patch.object(rw.psycopg2, "connect", return_value=self.pg),
              "out": mock.patch("sys.stdout", new_callable=io.StringIO)}
        self.m = {k: p.start() for k, p in ps.items()}
        self.addCleanup(lambda: [p.stop() for p in ps.values()])

    def posts(self):
        return [c[0][0] for c in rw.nova_config.post_both.call_args_list]


def _ssh(alive_polls, tail, unas="1300000"):
    state = {"polls": 0}
    def run(argv, **k):
        cmd = argv[-1]
        if cmd.startswith("kill -0"):
            state["polls"] += 1
            return mock.Mock(stdout="ALIVE\n" if state["polls"] <= alive_polls else "DEAD\n")
        if cmd.startswith("tail"):
            return mock.Mock(stdout=tail)
        if cmd.startswith("find"):
            return mock.Mock(stdout=f"{unas}\n")
        return mock.Mock(stdout="")
    return run


class TestSecurity(_Env):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('"BatchMode=yes"', SRC)                 # key auth only, never a password prompt

    def test_insert_parameterized_and_read_only_remote(self):
        rw.record(True, 5)
        sql, params = self.cur.execute.call_args[0]
        self.assertEqual(sql.count("%s"), 4)
        self.assertEqual(params, (0, 5, 0, True))
        for cmd in re.findall(r'syn\(f?"([^"]+)"', SRC):
            self.assertNotRegex(cmd.replace("2>/dev/null", ""), r"\brm\b|\bmv\b|>\s*/")        # watcher only reads remote state


class TestPerformance(_Env):
    def test_long_job_polls_bounded_by_max(self):
        self.m["run"].side_effect = _ssh(alive_polls=10**9, tail="")
        t0 = time.perf_counter()
        with mock.patch.object(sys, "argv", ["x", "123"]):
            rw.main()
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(self.m["sleep"].call_count, rw.MAX_S // rw.POLL_S)
        self.assertIn("14h limit", self.posts()[-1])


class TestRetry(_Env):
    def test_ssh_and_pg_failures_fail_open(self):
        # RETRY GAP: syn / unas_files / record / slack — one attempt each, safe defaults, never raise
        self.m["run"].side_effect = subprocess.TimeoutExpired("ssh", 40)
        self.assertEqual(rw.syn("x"), "")
        self.assertIsNone(rw.unas_files())
        self.m["pg"].side_effect = OSError("pg down")
        rw.record(True, 1)
        rw.nova_config.post_both.side_effect = OSError("slack down")
        rw.slack("hi")
        self.assertIn("slack: slack down", self.m["out"].getvalue())


class TestUnit(_Env):
    def test_unas_files_parsing(self):
        self.m["run"].return_value = mock.Mock(stdout="  42\n")
        self.assertEqual(rw.unas_files(), 42)
        self.m["run"].return_value = mock.Mock(stdout="garbage")
        self.assertIsNone(rw.unas_files())

    def test_syn_prefixes_ssh_argv(self):
        self.m["run"].return_value = mock.Mock(stdout="ok")
        rw.syn("uptime")
        self.assertEqual(self.m["run"].call_args[0][0], rw.SYN + ["uptime"])


class TestIntegration(_Env):
    def test_slack_targets_feed_channel_only(self):
        rw.slack("m")
        self.assertEqual(rw.nova_config.post_both.call_args[1], {"slack_channel": "C_FEED", "discord_channel": None})
        self.assertTrue(hasattr(_real_config, "SLACK_FEED"))
        self.assertIn("telemetry.backup_runs", SRC)


class TestFunctional(_Env):
    def _main(self, alive, tail):
        self.m["run"].side_effect = _ssh(alive, tail)
        with mock.patch.object(sys, "argv", ["x", "777"]):
            rw.main()

    def test_completion_code_23_counts_ok_and_reports(self):
        self._main(2, TAIL_ERR)
        posts = self.posts()
        self.assertIn("started", posts[0])
        self.assertIn("SSH backup repair complete* (rsync code 23)", posts[-1])
        self.assertIn("files transferred: 1,260,001 (2.5 GB)", posts[-1])
        self.assertIn("UNAS nas now holds 1,300,000 files", posts[-1])
        self.assertEqual(self.cur.execute.call_args[0][1], (0, 1260001, 0, True))
        kills = [c[0][0][-1] for c in self.m["run"].call_args_list if c[0][0][-1].startswith("kill")]
        self.assertTrue(all("kill -0 777" in k for k in kills))

    def test_hard_error_records_failure_and_heartbeat_fires(self):
        self._main(rw.HEARTBEAT_S // rw.POLL_S + 1, TAIL_BAD)
        posts = self.posts()
        self.assertTrue(any("repair running" in p for p in posts))
        self.assertIn("finished with errors* (rsync code 10)", posts[-1])
        self.assertEqual(self.cur.execute.call_args[0][1], (23, 0, 1, False))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_ssh_rsync_watch"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
