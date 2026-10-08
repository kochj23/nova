#!/usr/bin/env python3
"""Tests for nova_model_warm.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_model_warm.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mw = _load("mw", SCRIPT)


class _Cur:
    def __init__(self, placement="missing"):
        self.placement = placement; self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def fetchone(self):
        return None if self.placement == "missing" else (self.placement,)

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


PLACE = {"http://h:11434": ["qwen3:8b", "nomic-embed-text:latest"]}


def _ps(*names, exp="2319-01-01T00:00:00Z"):
    return {"models": [{"name": n, "expires_at": exp} for n in names]}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_auth_headers(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("Authorization", SRC)
        self.assertTrue(all(u.startswith("http://192.168.1.") for u in mw.DEFAULT_PLACEMENT))   # LAN only

    def test_sql_is_parameterized_and_writes_only_its_config_row(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = re.findall(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)
        self.assertEqual(writes, ["service_config"])
        cur = _Cur()
        mw.placement(cur)
        sql, params = cur.ran("INSERT INTO service_config")[0]
        self.assertIn("ON CONFLICT DO NOTHING", sql)                   # never overwrites a human-edited placement
        self.assertEqual(json.loads(params[0]), mw.DEFAULT_PLACEMENT)


class TestPerformance(unittest.TestCase):
    def test_status_fast_on_10k_models(self):
        names = [f"m{i}" for i in range(10_000)]
        place = {"http://h:11434": names[:5_000] + ["ghost"]}
        with patch.object(mw, "_get", lambda url, timeout=5: _ps(*names)), redirect_stdout(io.StringIO()) as out:
            t0 = time.perf_counter()
            mw.status(place)
            self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertIn("missing=['ghost']", out.getvalue())


class TestRetry(unittest.TestCase):
    def test_warm_fails_open_per_model(self):
        # warm: _get/_post retry inside (see test_nova_model_warm_7cat); a final failure is reported, never raised
        with patch.object(mw, "_get", MagicMock(side_effect=OSError("refused"))):
            self.assertTrue(mw.warm("http://h:11434", "qwen3:8b").startswith("fail: ps"))
        with patch.object(mw, "_get", lambda url, timeout=5: _ps()), patch.object(mw, "_post", MagicMock(side_effect=OSError("x" * 200))):
            r = mw.warm("http://h:11434", "qwen3:8b")
            self.assertTrue(r.startswith("fail: ")); self.assertLessEqual(len(r), 86)

    def test_status_skips_an_unreachable_node(self):
        with patch.object(mw, "_get", MagicMock(side_effect=[OSError("down"), _ps("qwen3:8b")])), redirect_stdout(io.StringIO()) as out:
            mw.status({"http://a:11434": ["qwen3:8b"], "http://b:11434": ["qwen3:8b"]})
        self.assertIn("http://a:11434: unreachable", out.getvalue()); self.assertIn("http://b:11434: loaded=['qwen3:8b']", out.getvalue())

    def test_pg_down_is_retried_then_raised(self):
        # main (psycopg2.connect) retries 3x with backoff; a lasting PG outage still escapes to the scheduler log
        with patch.object(mw.psycopg2, "connect", side_effect=OSError("no pg")) as c, patch.object(mw.time, "sleep"), \
                patch.object(sys, "argv", ["x"]):
            with self.assertRaises(OSError):
                mw.main()
        self.assertEqual(c.call_count, 3)


class TestUnit(unittest.TestCase):
    def test_warm_distinguishes_resident_from_cold_and_picks_the_endpoint(self):
        posts = []
        with patch.object(mw, "_get", lambda url, timeout=5: _ps("qwen3:8b")), patch.object(mw, "_post", lambda url, body, timeout=600: posts.append((url, body))):
            self.assertEqual(mw.warm("http://h:11434", "qwen3:8b"), "warm")
            self.assertRegex(mw.warm("http://h:11434", "nomic-embed-text:latest"), r"^loaded in \d+s$")
        self.assertEqual(posts[0], ("http://h:11434/api/generate", {"model": "qwen3:8b", "prompt": "", "keep_alive": -1}))
        self.assertEqual(posts[1], ("http://h:11434/api/embed", {"model": "nomic-embed-text:latest", "input": "warm", "keep_alive": -1}))

    def test_placement_prefers_the_stored_row(self):
        cur = _Cur(placement=PLACE)
        self.assertEqual(mw.placement(cur), PLACE)
        self.assertEqual(cur.ran("INSERT"), [])
        self.assertIn("service='nova_model_warm' AND key='placement'", cur.sql[0][0])

    def test_status_reports_missing_unpinned_and_extra(self):
        ps = {"models": [{"name": "qwen3:8b", "expires_at": "2026-10-05T10:00:00Z"}, {"name": "llama3.2:3b", "expires_at": "2319-01-01T00:00:00Z"}]}
        with patch.object(mw, "_get", lambda url, timeout=5: ps), redirect_stdout(io.StringIO()) as out:
            mw.status({"http://h:11434": ["qwen3:8b", "nomic-embed-text:latest"]})
        self.assertIn("missing=['nomic-embed-text:latest'] unpinned=['qwen3:8b'] extra=['llama3.2:3b']", out.getvalue())


class TestIntegration(unittest.TestCase):
    def test_http_helpers_use_urllib_and_send_json(self):
        seen = []

        def fake(req, timeout=0):
            seen.append(req)
            return _Resp(_ps() if isinstance(req, str) else {"done": True})
        with patch("urllib.request.urlopen", side_effect=fake):
            self.assertTrue(mw.warm("http://h:11434", "qwen3:8b").startswith("loaded in"))
        self.assertEqual(seen[0], "http://h:11434/api/ps")
        req = seen[1]
        self.assertEqual((req.full_url, req.get_header("Content-type")), ("http://h:11434/api/generate", "application/json"))
        self.assertEqual(json.loads(req.data)["keep_alive"], -1)

    def test_gateway_router_reads_the_same_residency_signal(self):
        # the warmer pins; the gateway's router sticks to nodes where the chat model is 'loaded'
        router = (SCRIPTS / "nova_gateway" / "router.py").read_text()
        self.assertIn('"loaded"', router)
        self.assertIn("/api/ps", SRC)


class TestFunctional(unittest.TestCase):
    def _main(self, argv, get, post=None):
        cur = _Cur(placement=PLACE)
        out = io.StringIO()
        with patch.object(mw.psycopg2, "connect", return_value=_Conn(cur)), patch.object(mw, "_get", get), \
             patch.object(mw, "_post", post or (lambda url, body, timeout=600: {"done": True})), patch.object(sys, "argv", argv), redirect_stdout(out):
            rc = mw.main()
        return rc, out.getvalue(), cur

    def test_golden_path_pins_every_model_and_logs_cold_loads(self):
        rc, out, cur = self._main(["nova_model_warm.py"], lambda url, timeout=5: _ps("qwen3:8b"))
        self.assertEqual(rc, 0)
        self.assertIn("h: qwen3:8b=warm, nomic-embed-text:latest=loaded in", out)
        self.assertIn("cold loads this run: 1 — http://h:11434 nomic-embed-text:latest", out)
        self.assertEqual(cur.ran("INSERT"), [])

    def test_error_path_a_failed_pin_fails_the_run(self):
        rc, out, _ = self._main(["nova_model_warm.py"], lambda url, timeout=5: _ps(), post=MagicMock(side_effect=OSError("oom")))
        self.assertEqual(rc, 1)
        self.assertIn("FAILED to pin: 2", out)

    def test_status_flag_is_read_only(self):
        rc, out, _ = self._main(["nova_model_warm.py", "--status"], lambda url, timeout=5: _ps("qwen3:8b", "nomic-embed-text:latest"))
        self.assertEqual(rc, 0)
        self.assertIn("missing=[] unpinned=[] extra=[]", out)
        self.assertNotIn("cold loads", out)


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free_and_main_is_guarded(self):
        # no argparse: --help would run main() against PG, so the frame check is a clean import + guard
        r = subprocess.run([sys.executable, "-c", "import nova_model_warm"], cwd=SCRIPTS, capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)


if __name__ == "__main__":
    unittest.main()
