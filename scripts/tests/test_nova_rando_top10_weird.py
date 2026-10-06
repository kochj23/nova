#!/usr/bin/env python3
"""Tests for nova_rando_top10_weird.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_rando_top10_weird.py"
SRC = PATH.read_text()
_TMP = Path(tempfile.mkdtemp(prefix="top10_test_"))

import nova_config as _real_config  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("nova_rando_top10_weird_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rt = _load()
rt.LOG_FILE = _TMP / "top10.log"
rt.nova_config = types.SimpleNamespace(post_both=mock.MagicMock(),
                                       filter_private_memories=_real_config.filter_private_memories)
EMAIL = "kochj23" + "@" + "gmail.com"
NOVA_MAIL = "nova" + "@" + "digitalnoise" + ".net"   # allow-listed address, assembled at runtime
ARTICLE = ("Number ten on the countdown is a memory about a toaster that filed a noise complaint. " * 12).strip()


def _mems(n, source="reddit"):
    return [{"text": f"memory {i} about a goose that learned to open the garage door", "source": source,
             "created_at": "t"} for i in range(n)]


class _Cur:
    def __init__(self, rows=(), total=0, stats=()):
        self.rows, self.total, self.stats, self.calls = list(rows), total, list(stats), []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.stats if "GROUP BY" in self.calls[-1][0] else self.rows

    def fetchone(self):
        return (self.total,)


class _Env(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        rt.nova_config.post_both.reset_mock()
        ps = {"content": mock.patch.object(rt, "CONTENT_DIR", root / "content"),
              "images": mock.patch.object(rt, "IMAGES_DIR", root / "images"),
              "push": mock.patch.object(rt.nj, "git_push"),
              "voice": mock.patch("nova_voice.system_prompt", side_effect=lambda c="", **k: c),
              "run": mock.patch.object(rt.subprocess, "run"),
              "out": mock.patch("sys.stdout", new_callable=io.StringIO)}
        self.m = {k: p.start() for k, p in ps.items()}
        self.root = root
        self.addCleanup(lambda: ([p.stop() for p in ps.values()], self.td.cleanup()))


class TestSecurity(_Env):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/\-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn(EMAIL, SRC)                  # the PII patterns are assembled, never literal

    def test_privacy_gate_drops_private_and_scrubs_pii(self):
        mems = [{"text": f"mail {EMAIL} from {Path.home()}/Documents", "source": "reddit"},
                {"text": "private text", "source": "imessage"},
                {"text": f"write {NOVA_MAIL} or x.y@example.org", "source": "music"}]
        safe = rt.sanitize_memories(mems)
        self.assertEqual(len(safe), 2)
        self.assertNotIn(EMAIL, safe[0]["text"])
        self.assertNotIn(str(Path.home()), safe[0]["text"])
        self.assertEqual(safe[1]["text"], f"write {NOVA_MAIL} or [redacted]")

    def test_sql_parameterized(self):
        cur = _Cur(rows=[("t", "s", "c")])
        conn = mock.Mock(); conn.cursor.return_value = cur
        with mock.patch("psycopg2.connect", return_value=conn):
            rt.get_recent_memories(hours=3, limit=7)
        sql, params = cur.calls[0]
        self.assertIn("LIMIT %s", sql)
        self.assertEqual(params[1], 7)


class TestPerformance(_Env):
    def test_scrub_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            rt.scrub_pii(f"note {i}: reach me at user{i}@example.com about the goose")
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(_Env):
    def test_claude_code_failure_falls_back_to_openrouter(self):
        fake = types.SimpleNamespace(claude_generate=mock.Mock(side_effect=RuntimeError("not logged in")))
        r = mock.Mock(); r.read.return_value = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        self.m["run"].return_value = mock.Mock(stdout="sk-test\n")
        with mock.patch.dict(sys.modules, {"nova_claude_code": fake}), \
                mock.patch("urllib.request.urlopen", return_value=r) as uo:
            self.assertEqual(rt.call_llm("s", "u"), "ok")
        fake.claude_generate.assert_called_once()
        self.assertEqual(uo.call_count, 1)
        self.assertEqual(uo.call_args[0][0].headers["Authorization"], "Bearer sk-test")

    def test_both_llm_paths_down_publishes_nothing(self):
        # RETRY GAP: call_llm — one Claude Code try, one OpenRouter try, then the error propagates;
        # main() aborts before publish(), so nothing is written, pushed or posted.
        fake = types.SimpleNamespace(claude_generate=mock.Mock(side_effect=RuntimeError("x")))
        self.m["run"].return_value = mock.Mock(stdout="")
        with mock.patch.dict(sys.modules, {"nova_claude_code": fake}), \
                mock.patch("urllib.request.urlopen", side_effect=OSError("401")), \
                mock.patch.object(rt, "get_memory_stats", return_value={"total": 900, "sources": {"reddit": 900}}), \
                mock.patch.object(rt, "get_recent_memories", return_value=_mems(20)):
            with self.assertRaises(OSError):
                rt.main()
        self.m["push"].assert_not_called()
        rt.nova_config.post_both.assert_not_called()


class TestUnit(_Env):
    def test_scrub_edges(self):
        self.assertEqual(rt.scrub_pii(""), "")
        self.assertIsNone(rt.scrub_pii(None))
        self.assertEqual(rt.scrub_pii("no pii here"), "no pii here")

    def test_title_cleanup(self):
        with mock.patch.object(rt, "call_llm", return_value=' "The Goose Knows" '):
            self.assertEqual(rt.generate_title("x"), "The Goose Knows")

    def test_memory_stats_shape(self):
        cur = _Cur(total=123, stats=[("reddit", 100), ("music", 23)])
        conn = mock.Mock(); conn.cursor.return_value = cur
        with mock.patch("psycopg2.connect", return_value=conn):
            self.assertEqual(rt.get_memory_stats(), {"sources": {"reddit": 100, "music": 23}, "total": 123})


class TestIntegration(_Env):
    def test_article_prompt_built_from_sanitized_memories(self):
        with mock.patch.object(rt, "call_llm", return_value=ARTICLE) as llm:
            rt.generate_article(_mems(3), {"total": 3, "sources": {"reddit": 3}})
        system, user = llm.call_args[0]
        self.assertIn("GROUNDING (CRITICAL)", system)
        self.assertIn("3 randomly sampled memories", user)
        self.assertIn("1. [reddit] memory 0", user)

    def test_image_goes_through_shared_helper(self):
        with mock.patch.object(rt, "call_llm", return_value="a goose"), \
                mock.patch("nova_image_utils.generate_image", return_value="/x/img.png") as gi:
            self.assertEqual(rt.generate_image_openrouter("art"), Path("/x/img.png"))
        self.assertEqual(gi.call_args[1]["section"], "rando_top10_weird")


class TestFunctional(_Env):
    def _main(self, total=900, mems=None):
        with mock.patch.object(rt, "get_memory_stats", return_value={"total": total, "sources": {"reddit": total}}), \
                mock.patch.object(rt, "get_recent_memories", return_value=mems if mems is not None else _mems(20)), \
                mock.patch.object(rt, "call_llm", side_effect=[ARTICLE, "The Goose Knows", "goose prompt"]), \
                mock.patch.object(rt, "generate_image_openrouter", return_value=None):
            rt.main()

    def test_golden_path_writes_pushes_and_posts(self):
        self._main()
        posts = list((self.root / "content").glob("*.md"))
        self.assertEqual(len(posts), 1)
        self.assertIn('title: "The Goose Knows"', posts[0].read_text())
        self.m["push"].assert_called_once_with("operations", "The Goose Knows")
        self.assertIn("/operations/", rt.nova_config.post_both.call_args[0][0])

    def test_quiet_period_and_grounding_guard_skip(self):
        self._main(total=10)
        self._main(mems=_mems(20, source="imessage"))          # all private -> < 5 real -> refuse
        self.assertFalse((self.root / "content").exists())
        self.m["push"].assert_not_called()

    def test_guard_blocks_failure_stub(self):
        rt.publish("Not logged in", "Not logged in · Please run /login", None)
        self.m["push"].assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_rando_top10_weird"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
