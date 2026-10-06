#!/usr/bin/env python3
"""Tests for nova_reddit_backfill.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
Reddit, the memory server and time.sleep are mocked at module load: no network, no waiting."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_reddit_backfill.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("reddit_backfill_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rb = _load()
# module-level stubs: no reddit / memory HTTP, no sleeping, quiet log
rb.urllib = types.SimpleNamespace(request=types.SimpleNamespace(
    Request=rb.urllib.request.Request, urlopen=MagicMock(side_effect=OSError("offline"))))
rb.time = types.SimpleNamespace(sleep=MagicMock())
rb.log = MagicMock()


def _cm(obj):
    r = MagicMock(); r.__enter__.return_value.read.return_value = json.dumps(obj).encode(); return r


def _listing(ids, after=None):
    return {"data": {"after": after, "children": [
        {"data": {"id": i, "title": f"T{i}", "selftext": "body", "author": "a", "score": 3}} for i in ids]}}


def _comments(*bodies):
    nested = {"data": {"children": [{"data": {"author": "r", "body": "reply"}}]}}
    return [{}, {"data": {"children": [{"data": {"author": "c", "body": b, "replies": nested if j == 0 else ""}}
                                       for j, b in enumerate(bodies)]}}]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("Authorization", SRC)                 # public .json only, no auth

    def test_memories_marked_private_and_author_masked(self):
        with patch.object(rb.urllib.request, "urlopen") as m:
            rb.remember("t", {"vector": "fishbowl", "author": "fishbowl"})
        body = json.loads(m.call_args[0][0].data)
        self.assertEqual(body["metadata"]["privacy"], "private")
        self.assertEqual(body["source"], "fishbowl")

    def test_only_reddit_hosts_are_fetched(self):
        urls = re.findall(r'f"(https://[^/"]+)', SRC)
        self.assertTrue(urls)
        self.assertTrue(all(u == "https://www.reddit.com" for u in urls))


class TestPerformance(unittest.TestCase):
    def test_pagination_bounded_and_chunking_fast(self):
        self.assertLessEqual(rb.PAGES_PER_SORT * 5 * 100, 6000)   # hard cap on listing pages
        long_post = _listing(["p1"]); long_post["data"]["children"][0]["data"]["selftext"] = "x" * 150_000
        calls = []
        t0 = time.perf_counter()
        with patch.object(rb, "get", side_effect=lambda u: long_post if "/new.json" in u else None), \
             patch.object(rb, "remember", side_effect=lambda t, m: calls.append(len(t)) or True), \
             patch.object(sys, "argv", ["x", "r/test", "vec"]):
            rb.main()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertTrue(all(n <= rb.CHUNK for n in calls))
        self.assertEqual(len(calls), -(-len("[r/test post by u/a (3 pts)] Tp1\n" + "x" * 150_000) // rb.CHUNK))


class TestRetry(unittest.TestCase):
    def test_get_retries_then_succeeds(self):
        rb.time.sleep.reset_mock()
        m = MagicMock(side_effect=[OSError("429"), OSError("429"), _cm({"ok": 1})])
        with patch.object(rb.urllib.request, "urlopen", m):
            self.assertEqual(rb.get("https://www.reddit.com/r/x/new.json"), {"ok": 1})
        self.assertEqual(m.call_count, 3)
        self.assertEqual([c.args[0] for c in rb.time.sleep.call_args_list], [6, 6])

    def test_get_gives_up_after_four(self):
        m = MagicMock(side_effect=OSError("down"))
        with patch.object(rb.urllib.request, "urlopen", m):
            self.assertIsNone(rb.get("https://www.reddit.com/r/x/new.json"))
        self.assertEqual(m.call_count, 4)
        self.assertFalse(rb.remember("t", {"vector": "v"}))    # remember fails open (single attempt)


class TestUnit(unittest.TestCase):
    def test_comments_walk_nested(self):
        with patch.object(rb, "get", return_value=_comments("first", "second")):
            self.assertEqual(rb.comments_text("s", "p"), "u/c: first\nu/r: reply\nu/c: second")
        with patch.object(rb, "get", return_value=None):
            self.assertEqual(rb.comments_text("s", "p"), "")
        with patch.object(rb, "get", return_value=[{}]):
            self.assertEqual(rb.comments_text("s", "p"), "")

    def test_subreddit_arg_forms(self):
        for arg in ("https://www.reddit.com/r/Fishbowl/", "r/Fishbowl", "Fishbowl"):
            seen = []
            with patch.object(rb, "get", side_effect=lambda u: seen.append(u)), \
                 patch.object(sys, "argv", ["x", arg, "vec"]):
                rb.main()
            self.assertIn("/r/Fishbowl/new.json", seen[0], arg)


class TestIntegration(unittest.TestCase):
    def test_post_and_comments_chained_into_memory(self):
        stored = []
        def fake_get(u):
            if "/comments/" in u:
                return _comments("nice")
            return _listing(["a1"]) if "/new.json" in u else None
        with patch.object(rb, "get", side_effect=fake_get), \
             patch.object(rb, "remember", side_effect=lambda t, m: stored.append((t, m)) or True), \
             patch.object(sys, "argv", ["x", "r/sub", "fishbowl"]):
            rb.main()
        text, meta = stored[0]
        self.assertIn("--- comments ---\nu/c: nice", text)
        self.assertEqual((meta["subreddit"], meta["post_id"], meta["vector"], meta["idx"]), ("sub", "a1", "fishbowl", 0))


class TestFunctional(unittest.TestCase):
    def test_dedups_across_sorts_and_follows_after(self):
        stored = []
        pages = {"new": [_listing(["p1", "p2"], after="t3_p2"), _listing(["p3"])], "hot": [_listing(["p1", "p4"])]}
        def fake_get(u):
            if "/comments/" in u:
                return None
            for sort, lst in pages.items():
                if f"/{sort}.json" in u and lst:
                    if "after=t3_p2" in u or sort != "new" or len(lst) == 2:
                        return lst.pop(0)
            return None
        with patch.object(rb, "get", side_effect=fake_get), \
             patch.object(rb, "remember", side_effect=lambda t, m: stored.append(m["post_id"]) or True), \
             patch.object(sys, "argv", ["x", "sub", "v"]):
            rb.main()
        self.assertEqual(stored, ["p1", "p2", "p3", "p4"])

    def test_missing_args_fail_fast(self):
        with patch.object(sys, "argv", ["x"]), patch.object(rb, "get") as g:
            with self.assertRaises(IndexError):
                rb.main()
        g.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help/--selftest (positional args only; a run crawls reddit), so import is the smoke test
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_reddit_backfill as m; print(callable(m.main))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
