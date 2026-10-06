#!/usr/bin/env python3
"""Tests for nova_shop_assistant.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import aiohttp

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_shop_assistant.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sh = _load("sh", SCRIPT)


class _Resp:
    def __init__(self, status, data, exc=None):
        self.status = status; self._data = data; self._exc = exc

    async def __aenter__(self):
        if self._exc:
            raise self._exc
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._data

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)


class _Session:
    """aiohttp.ClientSession stand-in: answers /recall per `source`, records every payload."""
    def __init__(self, by_source=None, status=200, exc=None, embed=None):
        self.by_source = by_source or {}; self.status = status; self.exc = exc; self.embed = embed; self.posts = []

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None):
        self.posts.append((url, json))
        if url.endswith("/embed"):
            return _Resp(self.status, {"embedding": self.embed or [0.1, 0.2]}, self.exc)
        return _Resp(self.status, self.by_source.get(json["source"], []), self.exc)


def _r(content, title=None, score=None, source=None):
    d = {"content": content}
    if title or source:
        d["metadata"] = {k: v for k, v in (("title", title), ("source", source)) if v}
    if score is not None:
        d["score"] = score
    return d


def _ask(session, query="C5 rear hub torque spec", verbose=False):
    with patch.object(sh.aiohttp, "ClientSession", session):
        return asyncio.run(sh.ask_shop(query, verbose=verbose))


MANUAL = [_r("Rear hub nut: 118 lb-ft, use new nut", title="Section 3C", score=0.91)]
COMMUNITY = [_r("The manual says 118 but actually should be 150 with the new hubs", title="Vette Garage", score=0.7),
             _r("Jack the car at the frame rails", source="youtube")]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_lan_memory_server(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertTrue(sh.MEMORY_SERVER.startswith("http://memory-server.digitalnoise.net:"))
        self.assertNotIn("subprocess", SRC); self.assertNotIn("eval(", SRC)

    def test_query_travels_in_the_json_body_not_the_url(self):
        s = _Session()
        _ask(s, query="torque'; DROP TABLE memories; -- ../../x")
        for url, payload in s.posts:
            self.assertEqual(url, f"{sh.MEMORY_SERVER}/recall")
            self.assertEqual(payload["query"], "torque'; DROP TABLE memories; -- ../../x")

    def test_result_text_is_rendered_verbatim_never_formatted(self):
        out = sh.format_result(_r("{0.__class__} %s {{}}", title="t"))
        self.assertIn("{0.__class__} %s {{}}", out)


class TestPerformance(unittest.TestCase):
    def test_contradiction_scan_and_formatting_on_10k_results(self):
        community = [_r(f"tip {i}: " + ("the manual says x" if i % 10 == 0 else "just do it"), title=f"v{i}") for i in range(10_000)]
        t0 = time.perf_counter()
        notes = sh.find_contradictions(MANUAL, community)
        text = "".join(sh.format_result(r) for r in community)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(notes), 1_000)
        self.assertEqual(text.count("[v"), 10_000)


class TestRetry(unittest.TestCase):
    def test_recall_fails_open_to_empty_on_non_200(self):
        # RETRY GAP: recall — one POST per vector; a 500 yields [] and the answer says "No matching entries"
        s = _Session(status=500)
        out = _ask(s)
        self.assertEqual(len(s.posts), 2)
        self.assertIn("No matching entries found in workshop manual.", out)
        self.assertIn("No matching automotive content found.", out)

    def test_embedding_error_escapes(self):
        # RETRY GAP: get_embedding — raise_for_status propagates; no retry
        async def go():
            async with _Session(status=503) as s:
                return await sh.get_embedding(s, "x")
        with self.assertRaises(aiohttp.ClientResponseError):
            asyncio.run(go())

    def test_connection_error_escapes_ask_shop(self):
        # RETRY GAP: ask_shop — a ClientError from the session propagates to main(), which exits 1
        with self.assertRaises(aiohttp.ClientConnectionError):
            _ask(_Session(exc=aiohttp.ClientConnectionError("refused")))


class TestUnit(unittest.TestCase):
    def test_format_result_truncates_at_300_unless_verbose(self):
        long = "x" * 400
        self.assertIn("x" * 297 + "...", sh.format_result(_r(long)))
        self.assertIn(long, sh.format_result(_r(long), verbose=True))
        self.assertNotIn("...", sh.format_result(_r("short")))

    def test_format_result_header_and_score(self):
        self.assertTrue(sh.format_result(_r("c", title="T", source="S", score=0.5)).startswith("    [T] (relevance: 0.500)\n  c\n"))
        self.assertTrue(sh.format_result(_r("c", source="S")).startswith("    [S]\n"))
        self.assertTrue(sh.format_result({"text": "legacy", "similarity": 0.25}).startswith("   (relevance: 0.250)\n  legacy"))
        self.assertEqual(sh.format_result({}), "  \n  \n")

    def test_find_contradictions_edges(self):
        self.assertEqual(sh.find_contradictions([], []), [])
        self.assertEqual(sh.find_contradictions(MANUAL, [_r("nothing to see")]), [])
        notes = sh.find_contradictions(MANUAL, COMMUNITY)
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("  [Vette Garage]: The manual says 118"))
        self.assertTrue(sh.find_contradictions(MANUAL, [_r("Over-Torque it")])[0].startswith("  [unknown]: "))


class TestIntegration(unittest.TestCase):
    def test_ask_shop_queries_both_vectors_with_their_top_k(self):
        s = _Session({sh.MANUAL_VECTOR: MANUAL, sh.COMMUNITY_VECTOR: COMMUNITY})
        _ask(s)
        payloads = {p["source"]: p for _, p in s.posts}
        self.assertEqual(set(payloads), {"corvette_workshop_manual", "automotive"})
        self.assertEqual(payloads["corvette_workshop_manual"]["top_k"], 5)
        self.assertEqual(payloads["automotive"]["top_k"], 10)

    def test_recall_accepts_list_and_wrapped_shapes(self):
        async def go(data):
            class S(_Session):
                def post(self, url, json=None):
                    return _Resp(200, data)
            async with S() as s:
                return await sh.recall(s, "q", 3, "automotive")
        self.assertEqual(asyncio.run(go([{"content": "a"}])), [{"content": "a"}])
        self.assertEqual(asyncio.run(go({"results": [{"content": "b"}]})), [{"content": "b"}])
        self.assertEqual(asyncio.run(go({})), [])

    def test_sections_compose_manual_then_community_then_contradictions(self):
        out = _ask(_Session({sh.MANUAL_VECTOR: MANUAL, sh.COMMUNITY_VECTOR: COMMUNITY}))
        i_m, i_c, i_x = out.index("From the manual:"), out.index("From the community:"), out.index("may contradict")
        self.assertLess(i_m, i_c); self.assertLess(i_c, i_x)
        self.assertIn("[Section 3C] (relevance: 0.910)", out)
        self.assertIn("[youtube]", out)


def _main(argv, session):
    out, err = io.StringIO(), io.StringIO()
    with patch.object(sys, "argv", ["nova_shop_assistant.py", *argv]), patch.object(sh.aiohttp, "ClientSession", session), \
         redirect_stdout(out), redirect_stderr(err):
        try:
            asyncio.run(sh.main())
            code = 0
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class TestFunctional(unittest.TestCase):
    def test_golden_path_prints_the_combined_answer(self):
        code, out, _ = _main(["C5 rear hub torque spec"], _Session({sh.MANUAL_VECTOR: MANUAL, sh.COMMUNITY_VECTOR: COMMUNITY}))
        self.assertEqual(code, 0)
        self.assertIn("Query: C5 rear hub torque spec", out)
        self.assertIn("Rear hub nut: 118 lb-ft", out)
        self.assertIn("Community notes that may contradict", out)

    def test_error_paths_exit_one(self):
        code, _, err = _main(["   "], _Session())
        self.assertEqual((code, err.strip()), (1, "Error: empty query"))
        code, _, err = _main(["q"], _Session(exc=aiohttp.ClientConnectionError("refused")))
        self.assertEqual(code, 1); self.assertIn("Error connecting to memory server", err)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_never_runs_main(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--verbose", r.stdout)
        self.assertIn('if __name__ == "__main__":\n    asyncio.run(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_shop_assistant"], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
