#!/usr/bin/env python3
"""Tests for nova_career_narrative.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
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
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_career_narrative.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("career_narr", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.notify = MagicMock()
    mod.CACHE_FILE = Path(tempfile.mkdtemp(prefix="career_")) / "career_narrative.json"
    return mod


cn = _load()


class _Resp:
    def __init__(self, status, data): self.status_code = status; self._d = data
    def json(self): return self._d


class _Client:
    """Fake httpx.AsyncClient: /recall answers from `recall`, ollama POST answers from `chat`."""
    def __init__(self, recall=None, chat="Jordan ran Sun boxes.", fail_get=False, fail_post=False):
        self.recall = recall if recall is not None else [{"text": "ran Solaris", "source": "computing_sun"}]
        self.chat = chat; self.fail_get = fail_get; self.fail_post = fail_post
        self.gets = []; self.posts = []

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get(self, url, params=None, timeout=None):
        self.gets.append((url, params))
        if self.fail_get:
            raise ConnectionError("memory server down")
        return _Resp(200, {"memories": self.recall})

    async def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        if self.fail_post:
            raise ConnectionError("ollama down")
        return _Resp(200, {"message": {"content": "<think>hmm</think>" + self.chat}})


def _quiet(fn, *a, **k):
    with redirect_stderr(io.StringIO()):
        return fn(*a, **k)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_prompt_excerpts_bounded(self):
        c = _Client()
        mems = [{"text": "Z" * 2000, "source": "s"} for _ in range(50)]
        asyncio.run(cn.synthesize_narrative(c, "litton", mems))
        user = c.posts[0][1]["messages"][1]["content"]
        self.assertEqual(user.count("Z"), 20 * 500)          # 20 excerpts, 500 chars each
        self.assertNotIn("[21]", user)

    def test_employer_is_anonymized(self):
        self.assertNotIn("Disney", cn.CAREER_ERAS["disney"]["title"] + cn.CAREER_ERAS["disney"]["context"])


class TestPerformance(unittest.TestCase):
    def test_dedup_of_10k_recalled_memories(self):
        c = _Client(recall=[{"text": f"m{i % 500}", "source": "x"} for i in range(10_000)])
        t0 = time.perf_counter()
        mems = asyncio.run(cn.gather_era_memories(c, "disney"))
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(len(mems), 500)


class TestRetry(unittest.TestCase):
    def test_recall_failure_is_one_shot_and_fails_open(self):
        # RETRY GAP: recall_memories/httpx GET — one attempt, [] on failure
        c = _Client(fail_get=True)
        self.assertEqual(_quiet(asyncio.run, cn.recall_memories(c, "q", source="s")), [])
        self.assertEqual(len(c.gets), 1)

    def test_synthesis_failure_returns_placeholder(self):
        # RETRY GAP: synthesize_narrative/ollama POST — one attempt, placeholder text on failure
        c = _Client(fail_post=True)
        out = _quiet(asyncio.run, cn.synthesize_narrative(c, "litton", [{"text": "t"}]))
        self.assertTrue(out.startswith("[Narrative generation failed"))
        self.assertEqual(len(c.posts), 1)


class TestUnit(unittest.TestCase):
    def test_recall_shapes(self):
        class C(_Client):
            async def get(self, url, params=None, timeout=None):
                return _Resp(200, [{"text": "listform"}])
        self.assertEqual(asyncio.run(cn.recall_memories(C(), "q")), [{"text": "listform"}])

        class D(_Client):
            async def get(self, url, params=None, timeout=None):
                return _Resp(500, {})
        self.assertEqual(asyncio.run(cn.recall_memories(D(), "q")), [])

    def test_era_with_no_memories(self):
        r = _quiet(asyncio.run, cn.generate_era(_Client(recall=[]), "northstar"))
        self.assertEqual(r["source_count"], 0)
        self.assertIn("No primary source", r["narrative"])

    def test_unknown_era_error(self):
        with patch("httpx.AsyncClient", return_value=_Client()):
            r = asyncio.run(cn.generate_career_narrative(era="nope"))
        self.assertIn("Unknown era", r["error"])

    def test_post_to_slack_chunks(self):
        cn.notify.reset_mock()
        with patch.object(cn.time, "sleep"):
            cn.post_to_slack("x" * 8000)
        self.assertEqual(cn.notify.call_count, 3)
        self.assertEqual(cn.notify.call_args_list[0].args[0], "Career Narrative (1/3)")
        cn.notify.reset_mock()
        cn.post_to_slack("short")
        cn.notify.assert_called_once_with("Career Narrative", body="short", level="info", category="journal")


class TestIntegration(unittest.TestCase):
    def test_recall_queries_every_vector_and_source(self):
        c = _Client()
        asyncio.run(cn.gather_era_memories(c, "litton"))
        era = cn.CAREER_ERAS["litton"]
        self.assertEqual(len(c.gets), len(era["vectors"]) * len(era["queries"]) + 2)
        self.assertTrue(all(u == f"{cn.MEMORY_SERVER}/recall" for u, _ in c.gets))
        self.assertEqual({p.get("source") for _, p in c.gets}, set(era["vectors"]) | {None})

    def test_fresh_cache_short_circuits(self):
        cn.CACHE_FILE.write_text(json.dumps({"generated_at": datetime.now().isoformat(), "full_text": "cached"}))
        try:
            with patch("httpx.AsyncClient", side_effect=AssertionError("network")):
                r = _quiet(asyncio.run, cn.generate_career_narrative())
            self.assertEqual(r["full_text"], "cached")
        finally:
            cn.CACHE_FILE.unlink()


class TestFunctional(unittest.TestCase):
    def test_full_generation_writes_cache(self):
        old = (datetime.now() - timedelta(days=30)).isoformat()
        cn.CACHE_FILE.write_text(json.dumps({"generated_at": old, "full_text": "stale"}))
        c = _Client()
        with patch("httpx.AsyncClient", return_value=c):
            r = _quiet(asyncio.run, cn.generate_career_narrative())
        self.assertEqual([e["era"] for e in r["eras"]], cn.ERA_ORDER)
        self.assertTrue(r["full_text"].startswith("# Jordan Koch — Career Narrative"))
        self.assertNotIn("<think>", r["full_text"])
        self.assertEqual(json.loads(cn.CACHE_FILE.read_text())["full_text"], r["full_text"])
        self.assertEqual(len(c.posts), 4)

    def test_main_single_era_posts(self):
        cn.notify.reset_mock()
        out = io.StringIO()
        with patch("httpx.AsyncClient", return_value=_Client()), patch.object(sys, "argv", ["x", "--era", "litton", "--post"]), \
             redirect_stdout(out), redirect_stderr(io.StringIO()):
            cn.main()
        self.assertIn("## Litton Guidance", out.getvalue())
        cn.notify.assert_called_once()

    def test_main_bad_era_exits_1(self):
        with patch("httpx.AsyncClient", return_value=_Client()), patch.object(sys, "argv", ["x", "--era", "bogus"]), \
             redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            cn.main()
        self.assertEqual(cm.exception.code, 1)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--era", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        self.assertTrue(callable(cn.generate_career_narrative))


if __name__ == "__main__":
    unittest.main()
