#!/usr/bin/env python3
"""Tests for metrics_tracker.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "metrics_tracker.py"
SRC = SCRIPT.read_text()

DF_OUT = ("Filesystem     Size   Used  Avail Capacity iused ifree %iused  Mounted on\n"
          "/dev/disk3s1s1 926Gi   10Gi  500Gi    42%    404k  4.2G    0%   /\n")


def _load():
    spec = importlib.util.spec_from_file_location("metrics_tracker_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mt = _load()


def _cp(stdout="", rc=0):
    return subprocess.CompletedProcess(["df"], rc, stdout=stdout, stderr="")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(api[_-]?key|password|secret|token)\s*=\s*['\"][^'\"]{8,}")

    def test_subprocess_uses_argv_not_shell(self):
        self.assertIn('["df", "-h", "/"]', SRC)
        self.assertNotIn("shell=True", SRC)


class TestPerformance(unittest.TestCase):
    def test_collect_is_cheap(self):
        with patch.object(mt.subprocess, "run", return_value=_cp(DF_OUT)):
            t0 = time.perf_counter()
            for _ in range(10_000):
                mt.collect_metrics()
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_df_failure_fails_open(self):
        # RETRY GAP: collect_metrics — df is called once; on failure the disk block stays empty
        with patch.object(mt.subprocess, "run", side_effect=OSError("no df")) as run:
            m = mt.collect_metrics()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(m["disk"], {})


class TestUnit(unittest.TestCase):
    def test_parses_capacity_percent(self):
        with patch.object(mt.subprocess, "run", return_value=_cp(DF_OUT)):
            self.assertEqual(mt.collect_metrics()["disk"]["root_percent"], 42)

    def test_empty_or_garbage_output(self):
        for out in ("", "header only\n", "h\nshort line\n"):
            with patch.object(mt.subprocess, "run", return_value=_cp(out)):
                m = mt.collect_metrics()
            self.assertNotIn("root_percent", m["disk"])
            self.assertIn("timestamp", m)


class TestIntegration(unittest.TestCase):
    def test_metrics_dir_is_under_workspace(self):
        self.assertEqual(mt.metrics_dir, Path.home() / ".openclaw/workspace/metrics")

    def test_collect_output_is_json_serialisable(self):
        with patch.object(mt.subprocess, "run", return_value=_cp(DF_OUT)):
            blob = json.loads(json.dumps(mt.collect_metrics()))
        self.assertEqual(set(blob), {"timestamp", "disk", "memory"})


class TestFunctional(unittest.TestCase):
    def test_main_writes_daily_file(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "metrics"
            with patch.object(mt, "metrics_dir", d), \
                 patch.object(mt.subprocess, "run", return_value=_cp(DF_OUT)), redirect_stdout(io.StringIO()) as out:
                mt.main()
            files = list(d.glob("metrics-*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text())["disk"]["root_percent"], 42)
            self.assertIn("Metrics collected", out.getvalue())

    def test_main_missing_parent_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(mt, "metrics_dir", Path(td) / "no" / "such" / "metrics"), \
                 self.assertRaises(FileNotFoundError):
                mt.main()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run([sys.executable, "-c", "import metrics_tracker"], cwd=str(SCRIPTS),
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "HOME": td, "NOVA_TEST_QUIET": "1"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "")
            self.assertFalse((Path(td) / ".openclaw").exists())

    def test_script_runs_against_temp_home(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".openclaw/workspace").mkdir(parents=True)
            r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                               env={**os.environ, "HOME": td, "NOVA_TEST_QUIET": "1"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(len(list((Path(td) / ".openclaw/workspace/metrics").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
