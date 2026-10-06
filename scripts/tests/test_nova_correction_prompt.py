#!/usr/bin/env python3
"""Tests for nova_correction_prompt.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_correction_prompt.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_correction_prompt_t", SCRIPTS / "nova_correction_prompt.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cp = _load()


def _resp(code=200, data=None):
    r = mock.Mock(status_code=code, text="err")
    r.json.return_value = data
    return r


class _TmpCorrections:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        f = Path(self.td.name) / "corrections.json"
        if self.data is not None:
            f.write_text(self.data if isinstance(self.data, str) else json.dumps(self.data))
        self.p = mock.patch.object(cp, "CORRECTIONS_FILE", f)
        self.p.start()
        return f

    def __exit__(self, *a):
        self.p.stop()
        self.td.cleanup()


ROWS = [{"topic": "homekit scenes", "nova_response": "Scene A", "jordan_correction": "It is Scene B"},
        {"topic": "weather", "nova_response": "sunny", "jordan_correction": "rain"}]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_query_passed_as_params_not_concatenated(self):
        with mock.patch.object(cp.requests, "get", return_value=_resp(200, [])) as g:
            cp.search_vector_memory("a&source=secret", 3)
        self.assertNotIn("a&source", g.call_args[0][0])
        self.assertEqual(g.call_args[1]["params"]["source"], "correction")

    def test_read_only_no_sql(self):
        self.assertNotRegex(SRC, r"\b(INSERT INTO|DELETE FROM|UPDATE \w+ SET)\b")


class TestPerformance(unittest.TestCase):
    def test_keyword_search_10k_records(self):
        rows = [{"topic": f"t{i}", "nova_response": "x", "jordan_correction": "y"} for i in range(10_000)]
        with _TmpCorrections(rows):
            t0 = time.perf_counter()
            out = cp.search_local_corrections("t5 x", limit=5)
            self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(out), 5)


class TestRetry(unittest.TestCase):
    def test_vector_failure_fails_open(self):
        # RETRY GAP: search_vector_memory — one requests.get attempt; RequestException -> []
        with mock.patch.object(cp.requests, "get", side_effect=cp.requests.RequestException("down")) as g, \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(cp.search_vector_memory("x"), [])
        self.assertEqual(g.call_count, 1)

    def test_non_200_fails_open(self):
        with mock.patch.object(cp.requests, "get", return_value=_resp(500)), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(cp.search_vector_memory("x"), [])

    def test_requests_missing_fails_open(self):
        with mock.patch.object(cp, "requests", None), mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(cp.search_vector_memory("x"), [])


class TestUnit(unittest.TestCase):
    def test_vector_response_shapes(self):
        with mock.patch.object(cp.requests, "get", return_value=_resp(200, ["a", {"text": "b"}, {"content": "c"}, {}])):
            self.assertEqual(cp.search_vector_memory("q"), ["a", "b", "c"])
        with mock.patch.object(cp.requests, "get", return_value=_resp(200, {"memories": [{"text": "m"}]})):
            self.assertEqual(cp.search_vector_memory("q"), ["m"])

    def test_local_missing_bad_and_nonlist(self):
        with _TmpCorrections(None):
            self.assertEqual(cp.search_local_corrections("x"), [])
        with _TmpCorrections("{not json"):
            self.assertEqual(cp.search_local_corrections("x"), [])
        with _TmpCorrections({"a": 1}):
            self.assertEqual(cp.search_local_corrections("x"), [])

    def test_format_empty_and_dedup(self):
        self.assertEqual(cp.format_corrections_for_prompt([], []), "")
        self.assertEqual(cp.format_corrections_for_prompt(["   "], []), "")
        out = cp.format_corrections_for_prompt(["dup", "dup "], [])
        self.assertEqual(out.count("- dup"), 1)
        self.assertTrue(out.endswith("=== END PRIOR CORRECTIONS ==="))


class TestIntegration(unittest.TestCase):
    def test_local_search_feeds_formatter(self):
        with _TmpCorrections(ROWS):
            local = cp.search_local_corrections("homekit scenes")
        self.assertEqual(local[0]["topic"], "homekit scenes")
        out = cp.format_corrections_for_prompt([], local)
        self.assertIn('CORRECTION [homekit scenes]: Nova said "Scene A" -> Jordan corrected: "It is Scene B"', out)

    def test_uses_correction_source_and_state_path(self):
        self.assertIn('"source": "correction"', SRC)
        self.assertEqual(cp.CORRECTIONS_FILE.name, "corrections.json")


class TestFunctional(unittest.TestCase):
    def _run(self, argv):
        with mock.patch.object(sys, "argv", ["x"] + argv), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            cp.main()
        return out.getvalue()

    def test_main_prints_injection_block(self):
        with _TmpCorrections(ROWS), \
             mock.patch.object(cp.requests, "get", return_value=_resp(200, ["Use Scene B always"])):
            out = self._run(["--query", "homekit", "--limit", "2"])
        self.assertIn("PRIOR CORRECTIONS", out)
        self.assertIn("- Use Scene B always", out)
        self.assertIn("homekit scenes", out)

    def test_main_silent_when_nothing_found(self):
        with _TmpCorrections(None), mock.patch.object(cp.requests, "get", return_value=_resp(200, [])):
            self.assertEqual(self._run(["-q", "nothing"]), "")

    def test_main_requires_query(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as cm:
            self._run([])
        self.assertEqual(cm.exception.code, 2)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_correction_prompt.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--query", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_correction_prompt"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
