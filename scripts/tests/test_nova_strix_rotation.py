#!/usr/bin/env python3
"""Tests for nova_strix_rotation.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_strix_rotation.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("strix_rotation", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sr = _load()


class _FixedDT(datetime):
    wd = 0

    @classmethod
    def now(cls):
        base = datetime(2026, 1, 5)       # a Monday
        return base.fromordinal(base.toordinal() + cls.wd)


def _run_on(weekday):
    _FixedDT.wd = weekday
    run = MagicMock()
    with patch.object(sr, "datetime", _FixedDT), patch.object(sr.subprocess, "run", run), redirect_stdout(io.StringIO()) as out:
        sr.main()
    return run, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_live_zigbee_coordinator_is_excluded(self):
        # the comment promises .23 is never a target; prove no rotation entry lists it
        for g in sr.ROTATION.values():
            self.assertFalse(any(t.endswith(".23") or ".23:" in t or ".23/" in t for t in g["targets"]), g["label"])

    def test_fragile_tiers_are_recon_only(self):
        for day in (5, 6):
            run, _ = _run_on(day)
            self.assertIn("--recon-only", run.call_args.args[0])
            self.assertEqual(sr.ROTATION[day]["mode"], "quick")


class TestPerformance(unittest.TestCase):
    def test_lookup_is_constant_time_over_many_days(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            sr.ROTATION.get(i % 7)
        self.assertLess(time.perf_counter() - t0, 0.5)


class TestRetry(unittest.TestCase):
    def test_single_hand_off_no_retry(self):
        # RETRY GAP: main/subprocess.run — the schedule hands off to nova_strix_run.py exactly once; no retry loop
        run, _ = _run_on(1)
        self.assertEqual(run.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_command_is_assembled_from_the_group(self):
        run, out = _run_on(2)
        cmd = run.call_args.args[0]
        g = sr.ROTATION[2]
        self.assertEqual(cmd[cmd.index("--targets") + 1], ",".join(g["targets"]))
        self.assertEqual(cmd[cmd.index("--label") + 1], g["label"])
        self.assertEqual(cmd[cmd.index("--max-min") + 1], str(g["max_min"]))
        self.assertNotIn("--recon-only", cmd)

    def test_every_rotation_entry_is_well_formed(self):
        self.assertEqual(sorted(sr.ROTATION), list(range(7)))
        for g in sr.ROTATION.values():
            self.assertTrue(g["targets"] and isinstance(g["max_min"], int))
            self.assertIn(g["mode"], ("standard", "quick"))


class TestIntegration(unittest.TestCase):
    def test_hands_off_to_strix_run(self):
        run, _ = _run_on(0)
        self.assertEqual(run.call_args.args[0][1], sr.RUN)
        self.assertTrue(sr.RUN.endswith("nova_strix_run.py"))

    def test_empty_schedule_skips_subprocess(self):
        g = sr.ROTATION[3]
        with patch.dict(sr.ROTATION, {3: {**g, "targets": []}}):
            run, out = _run_on(3)
        run.assert_not_called()
        self.assertIn("nothing scheduled", out)


class TestFunctional(unittest.TestCase):
    def test_robust_weekday_runs_standard_no_recon(self):
        run, out = _run_on(0)
        cmd = run.call_args.args[0]
        self.assertIn("--mode", cmd)
        self.assertEqual(cmd[cmd.index("--mode") + 1], "standard")
        self.assertNotIn("--recon-only", cmd)
        self.assertIn("grafana-2stack", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_strix_rotation as m; print(len(m.ROTATION))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "7")


if __name__ == "__main__":
    unittest.main()
