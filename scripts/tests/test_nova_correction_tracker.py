#!/usr/bin/env python3
"""Tests for nova_correction_tracker.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ct = _load("nova_correction_tracker_t", SCRIPTS / "nova_correction_tracker.py")
SRC = (SCRIPTS / "nova_correction_tracker.py").read_text()
_TMP = Path(tempfile.mkdtemp())
ct.CORRECTIONS_DIR = _TMP / "state"
ct.CORRECTIONS_FILE = ct.CORRECTIONS_DIR / "corrections.json"
_REAL_REQUESTS = ct.requests
# stub the vector POST at load: nothing reaches the memory server
ct.requests = types.SimpleNamespace(post=MagicMock(return_value=types.SimpleNamespace(status_code=200, text="ok")),
                                    RequestException=getattr(_REAL_REQUESTS, "RequestException", Exception))


def _reset():
    if ct.CORRECTIONS_FILE.exists():
        ct.CORRECTIONS_FILE.unlink()
    ct.requests.post.reset_mock(side_effect=True, return_value=False)
    ct.requests.post.side_effect = None
    ct.requests.post.return_value = types.SimpleNamespace(status_code=200, text="ok")


def _main(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with patch.object(sys, "argv", ["nova_correction_tracker.py", *argv]), redirect_stdout(out), redirect_stderr(err):
        try:
            ct.main()
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class TestSecurity(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_vector_payload_is_marked_local_only(self):
        ct.log_correction("a", "b", "t")
        payload = ct.requests.post.call_args[1]["json"]
        self.assertEqual(payload["metadata"]["privacy"], "local-only")
        self.assertEqual(payload["source"], "correction")


class TestPerformance(unittest.TestCase):
    def test_stats_over_10k_corrections(self):
        _reset()
        rows = [{"id": str(i), "timestamp": "2026-01-01T00:00:00", "topic": f"t{i % 50}",
                 "nova_response": "x", "jordan_correction": "y"} for i in range(10_000)]
        ct.save_corrections(rows)
        t0 = time.perf_counter()
        with redirect_stdout(io.StringIO()) as out:
            ct.show_stats()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertIn("10000 total", out.getvalue())


class TestRetry(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_vector_unreachable_is_one_shot_and_saved_locally(self):
        # RETRY GAP: store_to_vector_memory — one POST; failure keeps the local copy
        ct.requests.post.side_effect = ct.requests.RequestException("down")
        with redirect_stderr(io.StringIO()):
            c, ok = ct.log_correction("a", "b")
        self.assertFalse(ok)
        self.assertEqual(ct.requests.post.call_count, 1)
        self.assertEqual(ct.load_corrections()[0]["id"], c["id"])

    def test_non_200_and_missing_requests_fail_open(self):
        ct.requests.post.return_value = types.SimpleNamespace(status_code=500, text="err")
        rec = {"id": "1", "timestamp": "t", "nova_response": "a", "jordan_correction": "b"}
        with redirect_stderr(io.StringIO()):
            self.assertFalse(ct.store_to_vector_memory(rec))
            with patch.object(ct, "requests", None):
                self.assertFalse(ct.store_to_vector_memory(rec))


class TestUnit(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_load_missing_corrupt_and_non_list(self):
        self.assertEqual(ct.load_corrections(), [])
        ct.CORRECTIONS_DIR.mkdir(parents=True, exist_ok=True)
        ct.CORRECTIONS_FILE.write_text("{broken")
        self.assertEqual(ct.load_corrections(), [])
        ct.CORRECTIONS_FILE.write_text('{"a": 1}')
        self.assertEqual(ct.load_corrections(), [])

    def test_empty_topic_defaults_to_general(self):
        c, _ = ct.log_correction("a", "b", "")
        self.assertEqual(c["topic"], "general")

    def test_list_empty_and_limit(self):
        with redirect_stdout(io.StringIO()) as out:
            ct.list_corrections()
        self.assertIn("No corrections", out.getvalue())
        for i in range(5):
            ct.log_correction(f"n{i}", f"j{i}")
        with redirect_stdout(io.StringIO()) as out:
            ct.list_corrections(limit=2)
        self.assertIn("(2 of 5 total)", out.getvalue())
        self.assertLess(out.getvalue().index("n4"), out.getvalue().index("n3"))   # newest first


class TestIntegration(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_memory_text_and_endpoint(self):
        ct.log_correction("Paris is in Spain", "Paris is in France", "geography")
        args, kw = ct.requests.post.call_args
        self.assertEqual(args[0], ct.VECTOR_API_BASE + "/remember")
        self.assertIn('Nova said: "Paris is in Spain"', kw["json"]["text"])
        self.assertIn("geography", kw["json"]["title"])

    def test_log_then_export_roundtrip(self):
        ct.log_correction("a", "b", "x")
        with redirect_stdout(io.StringIO()) as out:
            ct.export_corrections()
        self.assertEqual(json.loads(out.getvalue())[0]["jordan_correction"], "b")


class TestFunctional(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_main_log_golden_path(self):
        code, out, _ = _main("--log", "wrong", "--correction", "right", "--topic", "ops")
        self.assertEqual(code, 0)
        self.assertIn("stored successfully", out)
        self.assertEqual(ct.load_corrections()[0]["topic"], "ops")

    def test_main_log_without_correction_errors(self):
        code, _, err = _main("--log", "wrong")
        self.assertEqual(code, 1)
        self.assertIn("Both --log and --correction", err)
        self.assertEqual(ct.requests.post.call_count, 0)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_correction_tracker.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--correction", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_correction_tracker"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
