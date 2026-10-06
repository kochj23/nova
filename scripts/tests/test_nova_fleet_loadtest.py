#!/usr/bin/env python3
"""Tests for nova_fleet_loadtest.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

No request ever leaves the box: urlopen is mocked, and soak()'s wall clock is a fake counter."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_fleet_loadtest.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lt = _load("nova_fleet_loadtest_t", SCRIPT)


def _resp(obj):
    r = MagicMock()
    r.read.return_value = json.dumps(obj).encode()
    return r


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("Authorization", SRC)

    def test_every_target_is_on_prem(self):
        # docstring promise: no cloud spillover — every URL is a LAN address
        for _, url, _, _ in lt.CHAT + lt.EMBED:
            self.assertRegex(url, r"^http://192\.168\.1\.\d+:\d+/")
        self.assertNotIn("openrouter.ai", SRC)


class TestPerformance(unittest.TestCase):
    def test_burst_stats_on_many_requests_fast(self):
        with patch.object(lt, "_one", return_value=(True, 0.01, 10)):
            t0 = time.perf_counter()
            s = lt.burst("u", "m", "chat", 8, 200)
            self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual((s["n"], s["ok"], s["err"]), (1600, 1600, 0))


class TestRetry(unittest.TestCase):
    def test_request_failure_is_counted_not_retried(self):
        # RETRY GAP: _one/urlopen — one attempt per request by design (errors are the measurement)
        with patch.object(lt.urllib.request, "urlopen", side_effect=OSError("refused")) as uo:
            ok, dt, tok = lt._one("http://x", "m", "chat")
        self.assertEqual(uo.call_count, 1)
        self.assertEqual((ok, tok), (False, 0))
        self.assertGreaterEqual(dt, 0)

    def test_saturated_node_stops_ramp(self):
        bursts = MagicMock(return_value={"conc": 1, "n": 4, "ok": 0, "err": 4, "wall": 1.0,
                                         "tok_s": 0, "p50": 0, "p95": 0})
        with patch.object(lt, "burst", bursts), redirect_stdout(io.StringIO()) as out:
            lt.ramp([("n1", "u", "m", "chat")], [1, 2, 4, 8], 1)
        self.assertEqual(bursts.call_count, 1)
        self.assertIn("saturated", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_one_chat_uses_usage_or_max_tok(self):
        with patch.object(lt.urllib.request, "urlopen", return_value=_resp({"usage": {"completion_tokens": 42}})):
            self.assertEqual(lt._one("http://x", "m", "chat")[2], 42)
        with patch.object(lt.urllib.request, "urlopen", return_value=_resp({})):
            self.assertEqual(lt._one("http://x", "m", "chat")[2], lt.MAX_TOK)
        with patch.object(lt.urllib.request, "urlopen", return_value=_resp({"embedding": [0.1]})):
            ok, _, ops = lt._one("http://x", "m", "embed")
        self.assertEqual((ok, ops), (True, 1))

    def test_burst_all_errors(self):
        with patch.object(lt, "_one", return_value=(False, 0.5, 0)):
            s = lt.burst("u", "m", "embed", 2, 2)
        self.assertEqual((s["ok"], s["err"], s["p50"], s["p95"]), (0, 4, 0, 0))

    def test_burst_percentiles(self):
        lats = iter([0.1 * i for i in range(1, 11)])
        with patch.object(lt, "_one", side_effect=lambda *a: (True, next(lats), 1)):
            s = lt.burst("u", "m", "chat", 1, 10)
        self.assertEqual((s["p50"], s["p95"]), (0.6, 1.0))


class TestIntegration(unittest.TestCase):
    def test_request_bodies_match_endpoint_kind(self):
        with patch.object(lt.urllib.request, "urlopen", return_value=_resp({})) as uo:
            lt._one(lt.CHAT[0][1], lt.CHAT[0][2], "chat")
            lt._one(lt.EMBED[0][1], lt.EMBED[0][2], "embed")
        chat = json.loads(uo.call_args_list[0].args[0].data)
        emb = json.loads(uo.call_args_list[1].args[0].data)
        self.assertEqual((chat["model"], chat["max_tokens"], chat["stream"]), ("qwen3:8b", lt.MAX_TOK, False))
        self.assertEqual(set(emb), {"model", "prompt"})
        self.assertEqual(len(lt.EMBED), 6)


class TestFunctional(unittest.TestCase):
    def test_ramp_reports_ceiling(self):
        stats = [{"conc": c, "n": c, "ok": c, "err": 0, "wall": 1.0, "tok_s": 10.0 * c, "p50": 1, "p95": 2}
                 for c in (1, 2)]
        with patch.object(lt, "burst", side_effect=stats), redirect_stdout(io.StringIO()) as out:
            lt.ramp([("node", "u", "m", "chat")], [1, 2], 1)
        self.assertIn("ceiling ≈ 20.0 tok/s", out.getvalue())

    def test_soak_runs_until_fake_clock_expires(self):
        clock = iter(range(0, 10_000, 20))
        s = {"conc": 1, "n": 1, "ok": 1, "err": 0, "wall": 1.0, "tok_s": 5.0, "p50": 1, "p95": 3}
        with patch.object(lt.time, "time", side_effect=lambda: next(clock)), \
                patch.object(lt, "burst", return_value=s) as b, redirect_stdout(io.StringIO()) as out:
            lt.soak([("a node", "u", "m", "chat")], 2, 1)
        self.assertGreater(b.call_count, 0)
        self.assertIn("soak summary", out.getvalue())
        self.assertIn("drift=+0.0%", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--mode", r.stdout)

    def test_import_never_runs(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(callable(lt.ramp))


if __name__ == "__main__":
    unittest.main()
