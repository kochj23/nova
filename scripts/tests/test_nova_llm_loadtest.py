#!/usr/bin/env python3
"""Tests for nova_llm_loadtest.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_llm_loadtest.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lt = _load("llm_loadtest_t", SCRIPT)
SRC = SCRIPT.read_text()
URLOPEN = MagicMock(side_effect=RuntimeError("urlopen not mocked in test"))
lt.urllib = types.SimpleNamespace(request=types.SimpleNamespace(Request=urllib.request.Request, urlopen=URLOPEN))


def _resp(data, backend=""):
    r = MagicMock(); r.read.return_value = json.dumps(data).encode(); r.headers = {"X-Nova-Backend": backend}
    r.__enter__ = lambda s: s; r.__exit__ = lambda *a: False
    return r


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("Authorization", SRC)

    def test_targets_are_lan_only(self):
        for _, url, _ in lt.TARGETS:
            self.assertRegex(url, r"^http://(192\.168\.1\.\d+|127\.0\.0\.1):\d+/v1/chat/completions$")


class TestPerformance(unittest.TestCase):
    def test_bench_bookkeeping_is_cheap(self):
        with patch.object(lt, "call", return_value=(0.5, 50, "192.168.1.7")):
            t0 = time.perf_counter()
            for _ in range(200):
                r = lt.bench("x", "u", "m")
            self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(r["single_tps"], 100.0)


class TestRetry(unittest.TestCase):
    def test_call_fails_open(self):
        # RETRY GAP: call — one HTTP attempt; failure returns (None, err, None), never raises
        URLOPEN.reset_mock(); URLOPEN.side_effect = OSError("connection refused")
        dt, info, be = lt.call("http://x", "m")
        URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")
        self.assertEqual((dt, be), (None, None))
        self.assertIn("connection refused", info)
        self.assertEqual(URLOPEN.call_count, 1)

    def test_failed_warmup_short_circuits(self):
        with patch.object(lt, "call", return_value=(None, "timeout", None)) as c:
            self.assertEqual(lt.bench("L", "u", "m"), {"label": "L", "model": "m", "error": "timeout"})
        self.assertEqual(c.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_call_uses_usage_or_estimates(self):
        URLOPEN.reset_mock()
        URLOPEN.side_effect = None
        URLOPEN.return_value = _resp({"usage": {"completion_tokens": 77}}, "192.168.1.6")
        dt, toks, be = lt.call("http://x", "m")
        self.assertEqual((toks, be), (77, "192.168.1.6"))
        URLOPEN.return_value = _resp({"choices": [{"message": {"content": "x" * 40}}]})
        self.assertEqual(lt.call("http://x", "m")[1], 10)
        body = json.loads(URLOPEN.call_args.args[0].data)
        self.assertEqual((body["max_tokens"], body["stream"]), (lt.MAX_TOKENS, False))
        URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")

    def test_bench_partial_concurrency(self):
        seq = iter([(1, 10, "")] * 5 + [(1, 10, "192.168.1.2"), (None, "e", None), (1, 10, ""), (None, "e", None)])
        with patch.object(lt, "call", side_effect=lambda *a, **k: next(seq)):
            r = lt.bench("L", "u", "m")
        self.assertEqual(r["concurrent_ok"], "2/4")
        self.assertEqual(r["backends"], ".2")


class TestIntegration(unittest.TestCase):
    def test_bench_composes_call_results(self):
        with patch.object(lt, "call", return_value=(2.0, 100, "")) as c:
            r = lt.bench("L", "u", "m")
        self.assertEqual(c.call_count, 1 + 3 + 1 + lt.CONCURRENCY)
        self.assertEqual(c.call_args_list[0].kwargs, {"timeout": 420})
        self.assertEqual((r["single_tps"], r["single_lat"]), (50.0, 2.0))


class TestFunctional(unittest.TestCase):
    def test_main_prints_table_with_errors(self):
        def fake(label, url, model):
            return {"label": label, "model": model, "error": "down"} if "FABRIC" in label else \
                {"label": label, "model": model, "single_tps": 10.0, "single_lat": 1.0, "concurrent_tps": 30.0,
                 "concurrent_ok": "4/4", "speedup": 3.0, "backends": ""}
        with patch.object(lt, "bench", side_effect=fake), redirect_stdout(io.StringIO()) as out:
            lt.main()
        txt = out.getvalue()
        self.assertEqual(txt.count("ERROR: down"), 2)
        self.assertIn("3.0x", txt)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_llm_loadtest as m; print(len(m.TARGETS))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(len(lt.TARGETS)))


if __name__ == "__main__":
    unittest.main()
