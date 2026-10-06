#!/usr/bin/env python3
"""Tests for nova_daemon_staleness — the "daemon running stale code" detector.

Seven categories:
 1. plist-parse         — label + main script extracted from a real plist
 2. script-selection    — interpreter/flags skipped, .py/.sh picked (zsh wrapper too)
 3. is-stale-true       — on-disk newer than the process by > grace -> stale
 4. is-stale-false      — within grace, and process-newer-than-disk -> not stale
 5. label-scope         — only managed nova namespaces are watched
 6. report-only         — a stale daemon yields a report; NEVER a restart call
 7. not-running/absent  — no PID, or a script missing on disk -> skipped, no crash

Filesystem/launchctl/ps are all faked; nothing on the real system is touched.
"""
import os
import sys
import unittest
import plistlib
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nova_daemon_staleness as d

NOW = 1_000_000_000.0  # fixed epoch seconds


def _write_plist(tmp, label, args):
    path = os.path.join(tmp, f"{label}.plist")
    with open(path, "wb") as f:
        plistlib.dump({"Label": label, "ProgramArguments": args}, f)
    return path


class TestParse(unittest.TestCase):

    def test_1_plist_parse_extracts_label_and_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_plist(tmp, "net.digitalnoise.nova-foo",
                             ["/opt/homebrew/bin/python3",
                              "/opt/nova/scripts/nova_foo.py"])
            info = d.parse_plist(p)
        self.assertEqual(info["label"], "net.digitalnoise.nova-foo")
        self.assertEqual(info["script"], "/opt/nova/scripts/nova_foo.py")

    def test_2_script_selection_skips_interpreter_and_flags(self):
        # python with a -u flag, then the script
        self.assertEqual(
            d.script_path_from_args(["/usr/bin/python3", "-u", "/x/nova_bar.py"]),
            "/x/nova_bar.py")
        # a zsh wrapper (the gateway shape) -> the .sh is the main script
        self.assertEqual(
            d.script_path_from_args(["/bin/zsh", "/x/nova_gateway_start.sh"]),
            "/x/nova_gateway_start.sh")
        # bare interpreter only -> no script
        self.assertEqual(d.script_path_from_args(["/bin/sh"]), None)
        # BSD `ps -o etime` parsing (macOS has no etimes): MM:SS / HH:MM:SS / DD-HH:MM:SS
        self.assertEqual(d.parse_etime("05:23"), 5 * 60 + 23)
        self.assertEqual(d.parse_etime("01:05:23"), 3600 + 5 * 60 + 23)
        self.assertEqual(d.parse_etime("127-03:15:22"), 127 * 86400 + 3 * 3600 + 15 * 60 + 22)
        self.assertIsNone(d.parse_etime(""))


class TestStaleness(unittest.TestCase):

    def test_3_is_stale_true_when_disk_newer(self):
        # script written 1h after the process started -> stale (grace 600s)
        self.assertTrue(d.is_stale(NOW, NOW - 3600, grace_s=600))

    def test_4_is_stale_false_within_grace_and_when_proc_newer(self):
        # disk only 5 min newer than proc, grace 10 min -> NOT stale
        self.assertFalse(d.is_stale(NOW, NOW - 300, grace_s=600))
        # process started AFTER the file was last written -> NOT stale
        self.assertFalse(d.is_stale(NOW - 3600, NOW, grace_s=600))

    def test_5_label_scope_managed_only(self):
        self.assertTrue(d.label_is_managed("net.digitalnoise.nova-x"))
        self.assertTrue(d.label_is_managed("com.nova.scheduler"))
        self.assertTrue(d.label_is_managed("com.digitalnoise.nova.general-monitor"))
        self.assertFalse(d.label_is_managed("com.apple.something"))
        self.assertFalse(d.label_is_managed("com.google.keystone"))
        self.assertFalse(d.label_is_managed(""))


class TestCheckDaemon(unittest.TestCase):

    def _info(self):
        return {"label": "net.digitalnoise.nova-foo", "script": "/x/nova_foo.py"}

    def test_6_stale_daemon_reports_and_never_restarts(self):
        info = self._info()
        with patch("subprocess.run") as run:  # guard: no launchctl kickstart / restart
            rep = d.check_daemon(
                info, now_s=NOW,
                pid_fn=lambda label: 4242,
                start_fn=lambda pid, now=None: NOW - 4 * 3600,   # started 4h ago
                mtime_fn=lambda path: NOW)                        # edited just now
            self.assertIsNotNone(rep)
            self.assertEqual(rep["pid"], 4242)
            self.assertAlmostEqual(rep["newer_by_h"], 4.0, delta=0.01)
            # report-only: check_daemon must not shell out to restart anything
            run.assert_not_called()

    def test_7_not_running_or_missing_script_is_skipped(self):
        info = self._info()
        # no live PID -> skipped
        self.assertIsNone(d.check_daemon(
            info, now_s=NOW, pid_fn=lambda label: None,
            start_fn=lambda pid, now=None: NOW, mtime_fn=lambda path: NOW))

        # script path absent on disk -> getmtime raises -> skipped, no crash
        def _missing(path):
            raise OSError("no such file")
        self.assertIsNone(d.check_daemon(
            info, now_s=NOW, pid_fn=lambda label: 5,
            start_fn=lambda pid, now=None: NOW, mtime_fn=_missing))

        # up-to-date (proc newer than disk) -> no report
        self.assertIsNone(d.check_daemon(
            info, now_s=NOW, pid_fn=lambda label: 5,
            start_fn=lambda pid, now=None: NOW, mtime_fn=lambda path: NOW - 3600))


# ── house categories added 2026-10-05 — the 7 house categories (Security, Performance, Retry, Unit,
# Integration, Functional, Frame). Written by Jordan Koch (via Claude).
# launchctl/ps (subprocess.run), notify and psycopg2 are mocked; plists live in a tempdir.

import re  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nova_daemon_staleness.py")
SRC = open(SCRIPT).read()
STALE = {"label": "net.digitalnoise.nova-x", "script": "/x/nova_x.py", "pid": 7, "script_mtime_s": NOW,
         "proc_start_s": NOW - 7200, "newer_by_s": 7200.0, "newer_by_h": 2.0}


class TestSecurity(unittest.TestCase):
    def test_no_credentials_and_parameterized_sql(self):
        self.assertIsNone(re.search(r"(password|token|secret)\s*=\s*['\"]", SRC, re.I))
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))
        self.assertIn("VALUES (%s,'active')", SRC)

    def test_never_restarts_anything(self):
        self.assertNotIn('"kickstart"', SRC)
        self.assertNotIn('"bootout"', SRC)
        with patch.object(d, "discover_daemons", return_value=[{"label": "net.digitalnoise.nova-x", "script": "/x"}]), \
             patch.object(d, "check_daemon", return_value=STALE), patch.object(d, "_notify_stale") as n, \
             patch("subprocess.run") as run:
            d.run_once()
        run.assert_not_called()
        n.assert_called_once_with(STALE)

    def test_non_managed_plists_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_plist(tmp, "com.apple.evil", ["/bin/sh", "/x/evil.sh"])
            _write_plist(tmp, "net.digitalnoise.nova-ok", ["/usr/bin/python3", "/x/ok.py"])
            with open(os.path.join(tmp, "broken.plist"), "w") as f:
                f.write("not a plist")
            self.assertEqual([i["label"] for i in d.discover_daemons(tmp)], ["net.digitalnoise.nova-ok"])


class TestPerformance(unittest.TestCase):
    def test_10k_checks_fast(self):
        info = {"label": "net.digitalnoise.nova-x", "script": "/x.py"}
        t0 = time.perf_counter()
        for i in range(10_000):
            d.check_daemon(info, now_s=NOW, pid_fn=lambda l: 1, start_fn=lambda p, n=None: NOW - i,
                           mtime_fn=lambda p: NOW)
            d.parse_etime(f"{i % 99}-01:02:03")
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_launchctl_and_ps_fail_open(self):
        # RETRY GAP: launchctl_pid()/process_start_s() — one subprocess call each; failure -> None
        with patch.object(d.subprocess, "run", side_effect=subprocess.TimeoutExpired("launchctl", 10)) as run:
            self.assertIsNone(d.launchctl_pid("net.digitalnoise.x"))
            self.assertIsNone(d.process_start_s(1, NOW))
        self.assertEqual(run.call_count, 2)

    def test_db_unavailable_still_sweeps(self):
        with patch("psycopg2.connect", side_effect=RuntimeError("pg down")), \
             patch.object(d, "run_once", return_value=[]) as ro, patch("builtins.print"):
            self.assertEqual(d.main(), 0)
        ro.assert_called_once_with(None)

    def test_ensure_session_rolls_back_on_error(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("x")
        d.ensure_session(conn)
        conn.rollback.assert_called_once()


class TestUnit(unittest.TestCase):
    def test_launchctl_pid_parse(self):
        out = '{\n\t"Label" = "x";\n\t"PID" = 22008;\n};'
        with patch.object(d.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=out)):
            self.assertEqual(d.launchctl_pid("x"), 22008)
        with patch.object(d.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout='"Label" = "x";')):
            self.assertIsNone(d.launchctl_pid("x"))
        with patch.object(d.subprocess, "run", return_value=SimpleNamespace(returncode=113, stdout="")):
            self.assertIsNone(d.launchctl_pid("x"))

    def test_process_start_and_bad_etime(self):
        with patch.object(d.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=" 01:00\n")):
            self.assertEqual(d.process_start_s(5, NOW), NOW - 60)
        self.assertIsNone(d.parse_etime("x-01:00"))
        self.assertIsNone(d.parse_etime("1:2:3:4"))
        self.assertEqual(d.parse_etime("42"), 42)

    def test_accepts_two(self):
        self.assertTrue(d._accepts_two(lambda a, b: 0))
        self.assertFalse(d._accepts_two(lambda a: 0))


class TestIntegration(unittest.TestCase):
    def test_notify_payload_is_deduped_per_label(self):
        import nova_notify
        with patch.object(nova_notify, "notify") as n:
            d._notify_stale(STALE)
        kw = n.call_args.kwargs
        self.assertEqual(kw["dedup_key"], "stale-code:net.digitalnoise.nova-x")
        self.assertEqual(kw["category"], "stale-code")
        self.assertIn("launchctl kickstart -k", kw["body"])     # advice only, never executed

    def test_run_once_logs_to_claude_tables(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        with patch.object(d, "discover_daemons", return_value=[{"label": "a", "script": "/a"}]), \
             patch.object(d, "check_daemon", return_value=STALE), patch.object(d, "_notify_stale"):
            d.run_once(conn)
        sql = " ".join(c[0][0] for c in cur.execute.call_args_list)
        self.assertIn("claude_sessions", sql)
        self.assertIn("claude_actions", sql)
        self.assertEqual(cur.execute.call_args_list[-2][0][1][2], "stale=1")


class TestFunctional(unittest.TestCase):
    def test_main_reports_stale(self):
        conn = MagicMock()
        with patch("psycopg2.connect", return_value=conn), patch.object(d, "run_once", return_value=[STALE]), \
             patch("builtins.print") as p:
            self.assertEqual(d.main(), 0)
        conn.close.assert_called_once()
        self.assertIn("1 daemon(s) running STALE", p.call_args_list[0][0][0])

    def test_main_sweep_failure_returns_1(self):
        with patch("psycopg2.connect", side_effect=RuntimeError("x")), \
             patch.object(d, "run_once", side_effect=RuntimeError("boom")), patch("builtins.print"):
            self.assertEqual(d.main(), 1)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_daemon_staleness"], cwd=os.path.dirname(SCRIPT),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
