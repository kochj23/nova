#!/usr/bin/env python3
"""7-category tests for the 2026-10-07 nova_llm_ping changes: page only after ALERT_AFTER_RUNS (2) consecutive bad
runs, recovery only after a warning (coagency #145), plus retry/backoff on every HTTP probe call and the PG connect.
Security, Performance, Retry, Unit, Integration, Functional, Frame. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import subprocess
import sys
import time
import types
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_llm_ping.py"
SRC = SCRIPT.read_text()
_spec = importlib.util.spec_from_file_location("lp7", SCRIPT)
lp = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(lp)
lp.log = lambda m: None


class _Resp:
    def __init__(self, payload): self._b = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


def R(node, status, lat=100, kind="ollama"):
    return {"node": node, "kind": kind, "url": f"http://{node}", "status": status, "latency_ms": lat,
            "has_chat_model": True, "model": "m", "loaded": [], "ok": status != "down", "error": ""}


def _main(results, prev=None, connect=None):
    stmts = []
    cur = MagicMock(); cur.execute.side_effect = lambda s, p=None: stmts.append((s, p))
    cur.fetchone.return_value = (prev,) if prev is not None else None
    conn = MagicMock(); conn.cursor.return_value = cur
    nn = types.SimpleNamespace(notify=MagicMock())
    seq = iter(results)
    with patch.object(lp, "probe", side_effect=lambda ep: next(seq)), patch.object(lp, "ENDPOINTS", [None] * len(results)), \
            patch.object(sys, "argv", ["x"]), patch("psycopg2.connect", connect or MagicMock(return_value=conn)), \
            patch.object(lp.time, "sleep"), patch.dict(sys.modules, {"nova_notify": nn}), redirect_stdout(io.StringIO()):
        rc = lp.main()
    ranking = next((json.loads(p[1]) for s, p in stmts if "INSERT INTO service_config" in s), None)
    return rc, ranking, [c.kwargs for c in nn.notify.call_args_list]


class TestSecurity(unittest.TestCase):
    def test_endpoints_are_private_lan_http(self):
        for _, kind, url in lp.ENDPOINTS:
            self.assertRegex(url, r"^http://192\.168\.1\.\d+:\d+$")
            self.assertIn(kind, ("ollama", "mlx", "llamacpp"))

    def test_no_user_paths_no_secrets(self):
        self.assertNotRegex(SRC, r"/Users/[a-z]")
        self.assertNotRegex(SRC.lower(), r"(password|api[_-]?key|token)\s*=\s*['\"]")

    def test_error_text_in_alerts_is_truncated(self):
        with patch.object(lp, "_get", side_effect=OSError("x" * 5000)):
            r = lp.probe(("n", "mlx", "http://n"))
        self.assertLessEqual(len(r["error"]), 160)


class TestPerformance(unittest.TestCase):
    def test_probe_retry_budget_fits_the_5min_schedule(self):
        # worst case per endpoint: tags (3x5s + 1.5s backoff) + ps + gen timeout + fallback gen, endpoints in parallel
        worst = 2 * (3 * lp.TAGS_TIMEOUT + 1.5) + 2 * lp.GEN_TIMEOUT
        self.assertLess(worst, 300)

    def test_debounce_bookkeeping_10k_nodes(self):
        res = [R(f"n{i}", "slow", 9000) for i in range(10000)]
        t0 = time.perf_counter(); lp.rank(res); self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_get_retries_twice_then_succeeds(self):
        seq = [urllib.error.URLError("refused"), urllib.error.URLError("reset"), _Resp({"data": []})]
        def fake(*a, **k):
            v = seq.pop(0)
            if isinstance(v, Exception): raise v
            return v
        with patch("urllib.request.urlopen", side_effect=fake) as u, patch.object(lp.time, "sleep") as s:
            self.assertEqual(lp._get("http://n/v1/models", 5), {"data": []})
        self.assertEqual((u.call_count, [c.args[0] for c in s.call_args_list]), (3, [0.5, 1.0]))

    def test_generation_timeout_not_retried(self):
        # a 35 s timeout is the measurement itself — don't triple it, the next run (and the 2-run debounce) re-checks
        with patch("urllib.request.urlopen", side_effect=TimeoutError("slow")) as u, patch.object(lp.time, "sleep"):
            with self.assertRaises(TimeoutError):
                lp._post("http://n/api/generate", {}, 35)
        self.assertEqual(u.call_count, 1)

    def test_pg_connect_retried(self):
        conn = MagicMock(); conn.cursor.return_value.fetchone.return_value = None
        connect = MagicMock(side_effect=[OSError("pg"), conn])
        rc, _, _ = _main([R("a", "up")], connect=connect)
        self.assertEqual((rc, connect.call_count), (0, 2))

    def test_persistent_failure_is_reported_down_not_silent(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")), patch.object(lp.time, "sleep"):
            r = lp.probe(("n", "ollama", "http://n"))
        self.assertEqual(r["status"], "down"); self.assertIn("refused", r["error"])


class TestUnit(unittest.TestCase):
    def test_alert_threshold_is_two(self):
        self.assertEqual(lp.ALERT_AFTER_RUNS, 2)

    def test_bad_runs_counter(self):
        _, rk, _ = _main([R("a", "down", None)], prev=json.dumps({"ollama": [{"node": "a", "status": "down", "bad_runs": 4}]}))
        self.assertEqual(rk["ollama"][0]["bad_runs"], 5)
        _, rk, _ = _main([R("a", "up")], prev=json.dumps({"ollama": [{"node": "a", "status": "down", "bad_runs": 4}]}))
        self.assertEqual(rk["ollama"][0]["bad_runs"], 0)


class TestIntegration(unittest.TestCase):
    def test_probe_through_urllib_with_a_flaky_tags_call(self):
        def fake(req, timeout=0):
            url = req.full_url
            if url.endswith("/api/tags") and not fake.flaked:
                fake.flaked = True; raise urllib.error.URLError("reset")
            if url.endswith("/api/tags"): return _Resp({"models": [{"name": lp.CHAT_MODEL, "size": 5}]})
            if url.endswith("/api/ps"): return _Resp({"models": []})
            return _Resp({"response": "p"})
        fake.flaked = False
        with patch("urllib.request.urlopen", side_effect=fake), patch.object(lp.time, "sleep"):
            r = lp.probe(("n", "ollama", "http://n"))
        self.assertEqual((r["status"], r["model"]), ("up", lp.CHAT_MODEL))


class TestFunctional(unittest.TestCase):
    def test_golden_two_bad_runs_page_once_then_recover(self):
        _, rk, n1 = _main([R("a", "down", None)])
        self.assertEqual(n1, [])                                   # run 1: silent
        _, rk, n2 = _main([R("a", "down", None)], prev=json.dumps(rk))
        self.assertEqual([x["level"] for x in n2], ["warning"])   # run 2: warning
        self.assertEqual(n2[0]["dedup_key"], "llm-ping:ollama:a")
        _, _, n3 = _main([R("a", "up")], prev=json.dumps(rk))
        self.assertEqual([x["title"] for x in n3], ["LLM recovered: ollama on a"])

    def test_error_path_notify_unavailable_still_writes_ranking(self):
        bad = types.SimpleNamespace(notify=MagicMock(side_effect=RuntimeError("bus down")))
        conn = MagicMock(); conn.cursor.return_value.fetchone.return_value = (json.dumps({"ollama": [{"node": "a", "bad_runs": 3}]}),)
        with patch.object(lp, "probe", return_value=R("a", "down", None)), patch.object(lp, "ENDPOINTS", [None]), \
                patch.object(sys, "argv", ["x"]), patch("psycopg2.connect", return_value=conn), \
                patch.dict(sys.modules, {"nova_notify": bad}), redirect_stdout(io.StringIO()):
            self.assertEqual(lp.main(), 0)
        self.assertTrue(any("service_config" in c.args[0] for c in conn.cursor.return_value.execute.call_args_list))


class TestFrame(unittest.TestCase):
    def test_import_and_selftest(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([sys.executable, "-c", "import nova_llm_ping as m; assert callable(m._retry)"], cwd=SCRIPTS,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
