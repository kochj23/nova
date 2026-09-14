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


if __name__ == "__main__":
    unittest.main(verbosity=2)
