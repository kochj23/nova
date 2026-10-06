#!/usr/bin/env python3
"""Tests for get_known_senders.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The script is a 10-line module-level printer (no main()), so every test runs it in a subprocess
with HOME pointed at a tempdir holding a fake herd_config.py — the real roster is never read."""
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "get_known_senders.py"
SRC = SCRIPT.read_text()


def _run(herd_src=None, timeout=30):
    """Run the script with a fake HOME; herd_src=None means no herd_config.py at all."""
    with tempfile.TemporaryDirectory() as home:
        oc = Path(home) / ".openclaw"
        oc.mkdir()
        if herd_src is not None:
            (oc / "herd_config.py").write_text(herd_src)
        env = {**os.environ, "HOME": home, "NOVA_TEST_QUIET": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("PYTHONPATH", None)
        return subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True,
                              timeout=timeout, env=env, cwd=home)


HERD2 = 'HERD = [{"name": "a", "email": "a@example.com"}, {"name": "b", "email": "b@example.org"}]\n'


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_addresses_or_credentials(self):
        self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", SRC))
        self.assertIsNone(re.search(r"(password|secret|token)\s*=", SRC, re.I))

    def test_roster_comes_from_home_config_not_source(self):
        self.assertIn('Path.home() / ".openclaw"', SRC)
        self.assertIn("from herd_config import HERD", SRC)


class TestPerformance(unittest.TestCase):
    def test_large_roster_is_fast(self):
        herd = "HERD = [{'email': f'm{i}@example.com'} for i in range(10000)]\n"
        t0 = time.perf_counter()
        r = _run(herd)
        self.assertLess(time.perf_counter() - t0, 10.0)
        self.assertEqual(len(r.stdout.strip().split(",")), 10000)


class TestRetry(unittest.TestCase):
    def test_missing_config_fails_open_to_empty_line(self):
        # RETRY GAP: module-level import of herd_config — no retry; ImportError prints "" and exits 0
        r = _run(None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "\n")


class TestUnit(unittest.TestCase):
    def test_empty_roster_prints_empty(self):
        r = _run("HERD = []\n")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "\n")

    def test_single_member_has_no_comma(self):
        r = _run("HERD = [{'email': 'solo@example.com'}]\n")
        self.assertEqual(r.stdout.strip(), "solo@example.com")

    def test_member_without_email_is_a_hard_error(self):
        # documents current behaviour: a malformed entry raises KeyError (not an ImportError)
        r = _run("HERD = [{'name': 'x'}]\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("KeyError", r.stderr)


class TestIntegration(unittest.TestCase):
    def test_reads_the_shared_herd_config_shape(self):
        r = _run(HERD2)
        self.assertEqual(r.stdout.strip().split(","), ["a@example.com", "b@example.org"])

    def test_herd_config_import_error_inside_config_is_swallowed(self):
        r = _run("import module_that_does_not_exist_xyz\nHERD = []\n")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "\n")


class TestFunctional(unittest.TestCase):
    def test_golden_path_preserves_order(self):
        r = _run(HERD2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "a@example.com,b@example.org\n")
        self.assertEqual(r.stderr, "")


class TestFrame(unittest.TestCase):
    def test_runs_and_exits_zero(self):
        r = _run(HERD2)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_compiles(self):
        compile(SRC, str(SCRIPT), "exec")
        self.assertTrue(SRC.startswith("#!/usr/bin/env python3"))


if __name__ == "__main__":
    unittest.main()
