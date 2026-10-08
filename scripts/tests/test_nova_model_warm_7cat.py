#!/usr/bin/env python3
"""7-category tests for the 2026-10-07/08 nova_model_warm changes: embed model warmed FIRST (coagency #117),
keep_alive=-1 pinning, and retry/backoff on every Ollama HTTP call and the PG connect.
Security, Performance, Retry, Unit, Integration, Functional, Frame. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import subprocess
import sys
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_model_warm.py"
SRC = SCRIPT.read_text()
_spec = importlib.util.spec_from_file_location("mw7", SCRIPT)
mw = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(mw)


class _Resp:
    def __init__(self, payload): self._b = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


class _Cur:
    def __init__(self, place): self.place = place; self.sql = []
    def execute(self, sql, params=None): self.sql.append((sql, params))
    def fetchone(self): return (self.place,) if self.place else None


def _run_main(place, get, post, argv=("x",)):
    cur = _Cur(place); conn = MagicMock(); conn.cursor.return_value = cur
    with patch.object(mw.psycopg2, "connect", return_value=conn), patch.object(mw, "_get", get), \
            patch.object(mw, "_post", post), patch.object(sys, "argv", list(argv)), redirect_stdout(io.StringIO()) as out:
        rc = mw.main()
    return rc, out.getvalue(), cur


class TestSecurity(unittest.TestCase):
    def test_only_private_lan_ollama_endpoints(self):
        for url in mw.DEFAULT_PLACEMENT:
            self.assertRegex(url, r"^http://192\.168\.1\.\d+:11434$")

    def test_no_user_paths_or_secrets(self):
        self.assertNotRegex(SRC, r"/Users/[a-z]")
        self.assertNotRegex(SRC.lower(), r"(password|api[_-]?key|token)\s*=")

    def test_placement_insert_is_parameterized(self):
        cur = _Cur(None)
        mw.placement(cur)
        sql, params = cur.sql[-1]
        self.assertIn("%s", sql); self.assertEqual(json.loads(params[0]), mw.DEFAULT_PLACEMENT)


class TestPerformance(unittest.TestCase):
    def test_retry_backoff_is_bounded(self):
        sleeps = []
        with patch.object(mw.time, "sleep", side_effect=sleeps.append):
            with self.assertRaises(OSError):
                mw._retry(MagicMock(side_effect=OSError("refused")))
        self.assertEqual(sleeps, [0.5, 1.0])          # 3 attempts, < 2 s total backoff

    def test_warm_is_one_ps_and_one_load_on_success(self):
        get, post = MagicMock(return_value={"models": []}), MagicMock(return_value={})
        with patch.object(mw, "_get", get), patch.object(mw, "_post", post):
            mw.warm("http://h:11434", "qwen3:8b")
        self.assertEqual((get.call_count, post.call_count), (1, 1))


class TestRetry(unittest.TestCase):
    def test_get_retries_connection_errors_then_succeeds(self):
        calls = iter([urllib.error.URLError("refused"), urllib.error.URLError("reset"), _Resp({"models": []})])
        def fake(*a, **k):
            v = next(calls)
            if isinstance(v, Exception): raise v
            return v
        with patch("urllib.request.urlopen", side_effect=fake) as u, patch.object(mw.time, "sleep") as s:
            self.assertEqual(mw._get("http://h:11434/api/ps"), {"models": []})
        self.assertEqual((u.call_count, s.call_count), (3, 2))

    def test_post_retries_and_raises_after_three(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")) as u, patch.object(mw.time, "sleep"):
            with self.assertRaises(urllib.error.URLError):
                mw._post("http://h:11434/api/generate", {"model": "m"})
        self.assertEqual(u.call_count, 3)

    def test_timeout_is_not_retried(self):
        # a 600 s load timeout already spent its budget; tripling it would overrun the 10-min schedule
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(TimeoutError("timed out"))) as u, \
                patch.object(mw.time, "sleep"):
            with self.assertRaises(urllib.error.URLError):
                mw._post("http://h:11434/api/generate", {"model": "m"})
        self.assertEqual(u.call_count, 1)

    def test_pg_connect_retries(self):
        conn = MagicMock(); conn.cursor.return_value = _Cur({})
        with patch.object(mw.psycopg2, "connect", side_effect=[OSError("x"), conn]) as c, patch.object(mw.time, "sleep"), \
                patch.object(sys, "argv", ["x"]), redirect_stdout(io.StringIO()):
            self.assertEqual(mw.main(), 0)
        self.assertEqual(c.call_count, 2)
        self.assertEqual(c.call_args.kwargs.get("connect_timeout"), 5)

    def test_final_failure_is_not_silent(self):
        rc, out, _ = _run_main({"http://h:11434": ["qwen3:8b"]}, MagicMock(return_value={"models": []}),
                               MagicMock(side_effect=OSError("oom")))
        self.assertEqual(rc, 1); self.assertIn("FAILED to pin", out)


class TestUnit(unittest.TestCase):
    def test_embed_model_is_first_wherever_it_is_placed_with_big_models(self):
        for url, models in mw.DEFAULT_PLACEMENT.items():
            if "nomic-embed-text:latest" in models and any("qwen3:30b" in m or "nova:" in m for m in models):
                self.assertEqual(models[0], "nomic-embed-text:latest", url)

    def test_embed_uses_embed_endpoint_and_keep_alive_forever(self):
        post = MagicMock(return_value={})
        with patch.object(mw, "_get", return_value={"models": []}), patch.object(mw, "_post", post):
            mw.warm("http://h:11434", "nomic-embed-text:latest")
            mw.warm("http://h:11434", "qwen3:8b")
        (u1, b1), (u2, b2) = [c.args for c in post.call_args_list]
        self.assertTrue(u1.endswith("/api/embed")); self.assertTrue(u2.endswith("/api/generate"))
        self.assertEqual((b1["keep_alive"], b2["keep_alive"]), (-1, -1))


class TestIntegration(unittest.TestCase):
    def test_warm_through_real_urllib_path_with_one_flaky_ps(self):
        seq = [urllib.error.URLError("reset"), _Resp({"models": [{"name": "qwen3:8b"}]}), _Resp({"done": True})]
        def fake(*a, **k):
            v = seq.pop(0)
            if isinstance(v, Exception): raise v
            return v
        with patch("urllib.request.urlopen", side_effect=fake), patch.object(mw.time, "sleep"):
            self.assertEqual(mw.warm("http://h:11434", "qwen3:8b"), "warm")


class TestFunctional(unittest.TestCase):
    def test_golden_embed_warmed_before_big_models(self):
        order = []
        post = lambda url, body, timeout=600: order.append(body["model"]) or {}
        rc, out, _ = _run_main({"http://h:11434": ["nomic-embed-text:latest", "qwen3:30b-a3b"]},
                               lambda url, timeout=5: {"models": []}, post)
        self.assertEqual(rc, 0); self.assertEqual(order[0], "nomic-embed-text:latest")
        self.assertIn("cold loads this run: 2", out)

    def test_error_one_node_down_others_still_pinned(self):
        def get(url, timeout=5):
            if "dead" in url: raise OSError("down")
            return {"models": [{"name": "qwen3:8b"}]}
        rc, out, _ = _run_main({"http://dead:11434": ["qwen3:8b"], "http://ok:11434": ["qwen3:8b"]}, get,
                               lambda url, body, timeout=600: {})
        self.assertEqual(rc, 1); self.assertIn("ok: qwen3:8b=warm", out); self.assertIn("dead:11434 qwen3:8b (fail: ps", out)


class TestFrame(unittest.TestCase):
    def test_compiles_and_imports_clean(self):
        r = subprocess.run([sys.executable, "-c", "import nova_model_warm as m; assert callable(m.main) and callable(m._retry)"],
                           cwd=SCRIPTS, capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
