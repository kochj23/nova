#!/usr/bin/env python3
"""Tests for nova_rerank.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The module creates its log dir and a FileHandler at import, so it is loaded with Path.home ->
tempdir and logging.basicConfig stubbed. Handlers are called directly — no port is ever bound."""
import asyncio
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_rerank.py"
SRC = PATH.read_text()
_TD = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("rerank_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(Path, "home", return_value=Path(_TD.name)), patch.object(logging, "basicConfig"):
        spec.loader.exec_module(mod)
    return mod


rr = _load()


def setUpModule():
    rr.log.disabled = True


def tearDownModule():
    rr.log.disabled = False


class _Req:
    def __init__(self, body=None, bad=False):
        self.body, self.bad = body, bad

    async def json(self):
        if self.bad:
            raise ValueError("bad json")
        return self.body


def _call(handler, req):
    resp = asyncio.run(handler(req))
    return resp.status, json.loads(resp.text)


class _Backend:
    def __init__(self, backend, model=None):
        self.backend, self.model = backend, model

    def __enter__(self):
        self.old = (rr.reranker_backend, rr.reranker_model)
        rr.reranker_backend, rr.reranker_model = self.backend, self.model

    def __exit__(self, *a):
        rr.reranker_backend, rr.reranker_model = self.old


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_input_validation(self):
        self.assertEqual(_call(rr.handle_rerank, _Req(bad=True))[0], 400)
        self.assertEqual(_call(rr.handle_rerank, _Req({"query": "  ", "candidates": ["a"]}))[0], 400)
        self.assertEqual(_call(rr.handle_rerank, _Req({"query": "q", "candidates": "not-a-list"}))[0], 400)
        self.assertEqual(_call(rr.handle_rerank, _Req({"query": "q", "candidates": []}))[0], 400)

    def test_top_k_clamped(self):
        with _Backend("tfidf"):
            st, body = _call(rr.handle_rerank, _Req({"query": "q", "candidates": ["q", "x"], "top_k": 10**9}))
            self.assertEqual(len(body["results"]), 2)
            st, body = _call(rr.handle_rerank, _Req({"query": "q", "candidates": ["q"] * 9, "top_k": "all"}))
            self.assertEqual(len(body["results"]), 5)


class TestPerformance(unittest.TestCase):
    def test_tfidf_10k_candidates_fast(self):
        cands = [f"memory {i} about backups and replicas number {i % 97}" for i in range(10_000)]
        t0 = time.perf_counter()
        out = rr.rerank_tfidf("replicas backups 42", cands, 10)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(out), 10)


class TestRetry(unittest.TestCase):
    def test_model_load_failure_falls_back_to_tfidf(self):
        # RETRY GAP: load_cross_encoder() — one load attempt; any failure degrades to the TF-IDF scorer
        fake = types.ModuleType("sentence_transformers")
        fake.CrossEncoder = MagicMock(side_effect=RuntimeError("HF hub offline"))
        with _Backend("none"), patch.dict(sys.modules, {"sentence_transformers": fake}):
            rr.load_cross_encoder()
            self.assertEqual(rr.reranker_backend, "tfidf")
        self.assertEqual(fake.CrossEncoder.call_count, 1)

    def test_missing_library_falls_back(self):
        with _Backend("none"), patch.dict(sys.modules, {"sentence_transformers": None}):
            rr.load_cross_encoder()
            self.assertEqual(rr.reranker_backend, "tfidf")


class TestUnit(unittest.TestCase):
    def test_tokenize_and_idf(self):
        self.assertEqual(rr.tokenize("Hello, World! x_1"), ["hello", "world", "x_1"])
        self.assertEqual(rr.compute_idf([]), {})
        idf = rr.compute_idf(["a b", "a c"])
        self.assertEqual(idf["a"], 1.0)
        self.assertGreater(idf["b"], idf["a"])

    def test_tfidf_score_edges(self):
        self.assertEqual(rr.tfidf_score("", "x", {}), 0.0)
        self.assertEqual(rr.tfidf_score("a b", "a b", {}), 1.0)
        self.assertEqual(rr.tfidf_score("a b", "a", {}), 0.5)

    def test_rerank_tfidf_orders_and_keeps_index(self):
        out = rr.rerank_tfidf("pg replica lag", ["weather today", "replica lag on pg is 5ms"], 2)
        self.assertEqual(out[0]["index"], 1)
        self.assertGreater(out[0]["score"], out[1]["score"])


class TestIntegration(unittest.TestCase):
    def test_cross_encoder_path_uses_model_predict(self):
        model = MagicMock(); model.predict.return_value = [0.1, 0.9]
        with _Backend("cross-encoder", model):
            st, body = _call(rr.handle_rerank, _Req({"query": "q", "candidates": ["a", "b"], "top_k": 1}))
        self.assertEqual(body["backend"], "cross-encoder")
        self.assertEqual(body["results"], [{"text": "b", "score": 0.9, "index": 1}])
        self.assertEqual(model.predict.call_args.args[0], [("q", "a"), ("q", "b")])

    def test_routes_registered(self):
        app = rr.create_app()
        routes = {(r.method, r.resource.canonical) for r in app.router.routes()}
        self.assertIn(("POST", "/rerank"), routes)
        self.assertIn(("GET", "/health"), routes)


class TestFunctional(unittest.TestCase):
    def test_golden_path_tfidf(self):
        with _Backend("tfidf"):
            st, body = _call(rr.handle_rerank, _Req({"query": "test", "candidates": ["hello", "test here"], "top_k": 2}))
        self.assertEqual(st, 200)
        self.assertEqual(body["results"][0]["text"], "test here")
        self.assertIn("elapsed_ms", body)

    def test_health(self):
        with _Backend("tfidf"):
            st, body = _call(rr.handle_health, _Req())
        self.assertEqual((st, body["status"], body["model"], body["port"]), (200, "ok", "tfidf-overlap", 18791))

    def test_main_never_binds_when_run_app_mocked(self):
        with _Backend("none"), patch.object(rr, "load_cross_encoder"), patch.object(rr.web, "run_app") as ra:
            rr.main()
        self.assertEqual(ra.call_args.kwargs["port"], 18791)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, "-c", "import nova_rerank"], cwd=str(SCRIPTS), capture_output=True,
                               text=True, timeout=30, env={**os.environ, "HOME": home, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)
        self.assertNotIn("Starting rerank", r.stderr)


if __name__ == "__main__":
    unittest.main()
