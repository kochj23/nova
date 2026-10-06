#!/usr/bin/env python3
"""Tests for nova_code_reference.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_code_reference.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _load("coderef_under_test", SCRIPT)


def _resp(memories):
    r = mock.MagicMock()
    r.read.return_value = json.dumps({"memories": memories}).encode()
    return r


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_query_is_url_encoded_so_it_cannot_smuggle_parameters(self):
        with mock.patch.object(R.urllib.request, "urlopen", return_value=_resp([])) as uo:
            R._recall("211&n=999&source=secrets", "police_codes", 3)
        url = uo.call_args[0][0]
        self.assertEqual(url.count("&source="), 1)
        self.assertTrue(url.endswith("&n=3&source=police_codes"))
        self.assertIn("%26n%3D999", url)

    def test_recall_endpoint_is_internal_and_prompt_forbids_guessing(self):
        self.assertTrue(R.RECALL.startswith("http://memory-server.digitalnoise.net:18790/"))
        self.assertIn("NEVER invent", SRC)


class TestPerformance(unittest.TestCase):
    def test_block_over_10k_definitions_is_fast(self):
        defs = [f"Code {i}: meaning number {i} for the dispatcher" for i in range(10_000)]
        with mock.patch.object(R, "_recall", return_value=defs):
            t0 = time.perf_counter()
            block = R.code_reference_block("q", ["police", "fire", "aviation"])
            dt = time.perf_counter() - t0
        self.assertLess(dt, 1.0)
        self.assertEqual(block.count("\n- "), 24)


class TestRetry(unittest.TestCase):
    def test_recall_fails_open_to_empty_on_one_attempt(self):
        # RETRY GAP: _recall — a single urlopen per vector; any error yields [] so the caller still gets "".
        with mock.patch.object(R.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertEqual(R._recall("x", "police_codes", 5), [])
            self.assertEqual(R.code_reference_block("x", ["police", "fire"]), "")
        self.assertEqual(uo.call_count, 3)                      # 1 + 2 domains, no retries

    def test_bad_json_fails_open(self):
        bad = mock.MagicMock(); bad.read.return_value = b"not json"
        with mock.patch.object(R.urllib.request, "urlopen", return_value=bad):
            self.assertEqual(R._recall("x", "fire_ops", 5), [])


class TestUnit(unittest.TestCase):
    def test_recall_strips_and_drops_empty_texts(self):
        with mock.patch.object(R.urllib.request, "urlopen", return_value=_resp([{"text": "  a  "}, {"text": ""}, {}])) as uo:
            self.assertEqual(R._recall("q", "police_codes", 2), ["a"])
        self.assertEqual(uo.call_args[1]["timeout"], 15)

    def test_query_is_truncated_to_400_chars(self):
        with mock.patch.object(R.urllib.request, "urlopen", return_value=_resp([])) as uo:
            R._recall("z" * 1000, "police_codes", 1)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(uo.call_args[0][0]).query)["q"][0]
        self.assertEqual(len(q), 400)

    def test_block_dedups_on_first_80_chars_case_insensitively(self):
        head = "x" * 80
        with mock.patch.object(R, "_recall", return_value=[head + " one", head.upper() + " two", "other"]):
            block = R.code_reference_block("q", ["police"])
        self.assertEqual(block.count("\n- "), 2)

    def test_block_truncates_each_definition_to_300(self):
        with mock.patch.object(R, "_recall", return_value=["y" * 500]):
            block = R.code_reference_block("q", ["fire"])
        self.assertIn("- " + "y" * 300 + "\n", block)
        self.assertNotIn("y" * 301, block)

    def test_unknown_domain_is_skipped_and_empty_returns_empty_string(self):
        with mock.patch.object(R, "_recall") as rc:
            self.assertEqual(R.code_reference_block("q", ["sports"]), "")
        rc.assert_not_called()
        self.assertEqual(R.code_reference_block("q", []), "")


class TestIntegration(unittest.TestCase):
    def test_domain_vector_map_names_the_reference_vectors(self):
        self.assertEqual(R.DOMAIN_VECTOR, {"police": "police_codes", "fire": "fire_ops", "aviation": "aviation_ref"})

    def test_recall_to_block_chain_queries_each_vector(self):
        seen = []

        def uo(url, timeout):
            seen.append(url)
            return _resp([{"text": f"def from {url.rsplit('=', 1)[-1]}"}])

        with mock.patch.object(R.urllib.request, "urlopen", side_effect=uo):
            block = R.code_reference_block("211 robbery", ["police", "aviation"])
        self.assertEqual([u.rsplit("=", 1)[-1] for u in seen], ["police_codes", "aviation_ref"])
        self.assertIn("- def from police_codes", block)
        self.assertIn("- def from aviation_ref", block)


class TestFunctional(unittest.TestCase):
    def test_golden_path_builds_prompt_block(self):
        with mock.patch.object(R.urllib.request, "urlopen", return_value=_resp([{"text": "Code 3 = lights and siren"}])):
            block = R.code_reference_block("code 3 pursuit", ["police"], n=4)
        self.assertTrue(block.startswith("\n\n[VERIFIED CODE/TERM REFERENCE"))
        self.assertIn("- Code 3 = lights and siren\n", block)
        self.assertTrue(block.endswith("\n"))

    def test_error_path_degrades_to_empty(self):
        with mock.patch.object(R.urllib.request, "urlopen", side_effect=TimeoutError("slow")):
            self.assertEqual(R.code_reference_block("x", ["police", "fire", "aviation"]), "")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_the_smoke_query(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_code_reference"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
