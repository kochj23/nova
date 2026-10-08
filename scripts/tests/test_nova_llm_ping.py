#!/usr/bin/env python3
"""Tests for nova_llm_ping.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_llm_ping.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lp = _load("llm_ping_t", SCRIPT)
SRC = SCRIPT.read_text()
lp._get = MagicMock(side_effect=RuntimeError("_get not mocked in test"))
lp._post = MagicMock(side_effect=RuntimeError("_post not mocked in test"))
lp.log = lambda m: None


def _ollama(tags, ps=(), post=None):
    def get(url, timeout):
        if url.endswith("/api/tags"):
            return {"models": list(tags)}
        return {"models": list(ps)}
    return patch.object(lp, "_get", side_effect=get), patch.object(lp, "_post", side_effect=post or (lambda *a: {}))


class _Cur:
    def __init__(self, prev=None):
        self.prev = prev; self.stmts = []

    def execute(self, sql, params=None):
        self.stmts.append((sql, params))

    def fetchone(self):
        return (self.prev,) if self.prev is not None else None


def R(node, kind, status, lat=100, chat=True):
    return {"node": node, "kind": kind, "url": f"http://{node}", "status": status, "latency_ms": lat,
            "has_chat_model": chat, "model": "m", "loaded": [], "ok": status != "down", "error": ""}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_param_sql(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", lp.OPS_DSN)
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))

    def test_embedding_models_never_pinged(self):
        self.assertIsNone(lp.pick_model([{"name": "nomic-embed-text", "size": 1}], [{"name": "nomic-embed-text"}]))
        self.assertIsNone(lp.smallest_model([{"name": "mxbai-embed", "size": 1}]))


class TestPerformance(unittest.TestCase):
    def test_rank_10k(self):
        res = [R(f"n{i}", "ollama" if i % 2 else "mlx", ("up", "slow", "down")[i % 3], i) for i in range(10_000)]
        t0 = time.perf_counter()
        rk = lp.rank(res)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(rk["ollama"][0]["status"], "up")


class TestRetry(unittest.TestCase):
    def test_chat_model_failure_falls_back_to_smallest(self):
        calls = []
        def post(url, payload, timeout):
            calls.append(payload["model"])
            if payload["model"] == lp.CHAT_MODEL:
                raise TimeoutError("OOM on 16 GB box")
            return {}
        g, p = _ollama([{"name": lp.CHAT_MODEL, "size": 5}, {"name": "tiny", "size": 1}], post=post)
        with g, p:
            r = lp.probe(("n", "ollama", "http://n"))
        self.assertEqual(calls, [lp.CHAT_MODEL, "tiny"])
        self.assertEqual((r["status"], r["model"], r["has_chat_model"]), ("up", "tiny", False))

    def test_probe_fails_open_to_down(self):
        # probe: _get retries internally (see test_nova_llm_ping_7cat); a final failure -> status down, never raises
        with patch.object(lp, "_get", side_effect=OSError("refused")) as g:
            r = lp.probe(("n", "mlx", "http://n"))
        self.assertEqual((r["status"], g.call_count), ("down", 1))
        self.assertIn("refused", r["error"])


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with redirect_stdout(io.StringIO()) as out:
            lp.demo()
        self.assertIn("all llm-ping assertions passed", out.getvalue())

    def test_classify_edges(self):
        self.assertEqual(lp.classify(True, lp.SLOW_MS - 1), "up")
        self.assertEqual(lp.classify(True, lp.SLOW_MS), "slow")
        self.assertEqual(lp.classify(True, lp.DEAD_MS), "down")
        self.assertEqual(lp.classify(True, None), "down")

    def test_mlx_probe_picks_started_model(self):
        with patch.object(lp, "_get", return_value={"data": [{"id": "/hf/other"}, {"id": "/Vol/qwen2.5-32b-4bit"}]}), \
                patch.object(lp, "_post", return_value={}) as p:
            r = lp.probe(("n", "mlx", "http://n"))
        self.assertEqual(r["model"], "/Vol/qwen2.5-32b-4bit")
        self.assertEqual(p.call_args.args[1]["max_tokens"], 1)


class TestIntegration(unittest.TestCase):
    def test_ranking_matches_router_contract(self):
        # nova_gateway/router.py::_best_url reads service_config(nova_llm_ping, ranking) and these row keys
        router_src = (SCRIPTS / "nova_gateway" / "router.py").read_text()
        self.assertIn("service='nova_llm_ping' AND key='ranking'", router_src)
        row = lp.rank([R("a", "ollama", "up")])["ollama"][0]
        for k in ("url", "status", "has_chat_model", "loaded"):
            self.assertIn(k, row)
            self.assertIn(f'"{k}"', router_src)


class TestFunctional(unittest.TestCase):
    def _main(self, results, prev=None, argv=("x",)):
        cur = _Cur(prev)
        conn = MagicMock(); conn.cursor.return_value = cur
        nn = types.SimpleNamespace(notify=MagicMock())
        seq = iter(results)
        with patch.object(lp, "probe", side_effect=lambda ep: next(seq, R("z", "llamacpp", "up"))), \
                patch.object(lp, "ENDPOINTS", [None] * len(results)), patch.object(sys, "argv", list(argv)), \
                patch("psycopg2.connect", return_value=conn), patch.dict(sys.modules, {"nova_notify": nn}), \
                redirect_stdout(io.StringIO()) as out:
            rc = lp.main()
        return rc, cur, nn.notify, out.getvalue()

    def test_writes_health_rows_ranking_and_alerts(self):
        # a: bad last run too (pre-#145 row without bad_runs) -> sustained -> alert; b: alerted outage ended -> recovered
        prev = json.dumps({"ollama": [{"node": "a", "status": "slow"}, {"node": "b", "status": "down", "bad_runs": 3}]})
        rc, cur, notify, _ = self._main([R("a", "ollama", "down", None), R("b", "ollama", "up")], prev=prev)
        self.assertEqual(rc, 0)
        hc = [p for s, p in cur.stmts if "INSERT INTO health_checks" in s]
        self.assertEqual([p[:4] for p in hc], [("llm:ollama", "a", "nova_llm_ping", "down"), ("llm:ollama", "b", "nova_llm_ping", "up")])
        ranking = json.loads([p for s, p in cur.stmts if "INSERT INTO service_config" in s][0][1])
        self.assertEqual(ranking["ollama"][0]["node"], "b")
        titles = [c.kwargs["title"] for c in notify.call_args_list]
        self.assertEqual(titles, ["LLM DOWN: ollama on a", "LLM recovered: ollama on b"])

    def test_single_blip_is_silent_both_ways(self):
        # coagency #145: one slow run then a fast one must page NOTHING (was SLOW + "recovered" every time)
        rc, cur, notify, _ = self._main([R("a", "ollama", "slow", 12000)], prev=None)
        notify.assert_not_called()
        ranking = json.loads([p for s, p in cur.stmts if "INSERT INTO service_config" in s][0][1])
        self.assertEqual(ranking["ollama"][0]["bad_runs"], 1)
        _, _, notify, _ = self._main([R("a", "ollama", "up")], prev=json.dumps(ranking))
        notify.assert_not_called()

    def test_sustained_then_recovery(self):
        prev = json.dumps({"ollama": [{"node": "a", "status": "slow", "bad_runs": 1}]})
        _, cur, notify, _ = self._main([R("a", "ollama", "slow", 12000)], prev=prev)
        self.assertEqual([c.kwargs["title"] for c in notify.call_args_list], ["LLM SLOW: ollama on a"])
        ranking = json.loads([p for s, p in cur.stmts if "INSERT INTO service_config" in s][0][1])
        _, _, notify, _ = self._main([R("a", "ollama", "up")], prev=json.dumps(ranking))
        self.assertEqual([c.kwargs["title"] for c in notify.call_args_list], ["LLM recovered: ollama on a"])

    def test_dry_run_writes_nothing(self):
        with patch("psycopg2.connect") as pc, patch.object(lp, "probe", return_value=R("a", "mlx", "up")), \
                patch.object(lp, "ENDPOINTS", [None]), patch.object(sys, "argv", ["x", "--dry-run"]), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(lp.main(), 0)
        pc.assert_not_called()
        self.assertIn('"chat_model"', out.getvalue())


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all llm-ping assertions passed", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_llm_ping"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
