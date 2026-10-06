#!/usr/bin/env python3
"""Tests for nova_request_router.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
import urllib.request  # noqa: F401
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2         # noqa: F401
import psycopg2.extras  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_request_router.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nrr", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("psycopg2.connect", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


rr = _load()


def _pg(queue_id=77):
    cur = MagicMock(); cur.fetchone.return_value = (queue_id,)
    conn = MagicMock(); conn.cursor.return_value = cur
    return conn, cur


def _resp(payload):
    r = MagicMock(); r.read.return_value = json.dumps(payload).encode()
    r.__enter__ = lambda s: s; r.__exit__ = lambda s, *a: False
    return r


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", rr.DB_DSN)

    def test_request_text_is_bound_not_interpolated(self):
        evil = "fix x'); UPDATE claude_queue SET status='done'; --"
        conn, cur = _pg()
        with patch.object(rr.psycopg2, "connect", return_value=conn):
            rr.route_to_claude(evil)
        sql, params = cur.execute.call_args[0]
        self.assertNotIn(evil, sql)
        self.assertEqual(params[0], evil)
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')

    def test_server_only_starts_under_main(self):
        # binding the port must never happen on import
        self.assertLess(SRC.index('if __name__ == "__main__":'), SRC.index("serve_forever"))


class TestPerformance(unittest.TestCase):
    def test_classify_10k_requests_fast(self):
        msgs = ["fix the python traceback in deploy script", "what's the weather tomorrow",
                "investigate why is plex down", "hello"] * 2_500
        t0 = time.perf_counter()
        for m in msgs:
            rr.classify_request(m)
        self.assertLess(time.perf_counter() - t0, 5.0)


class TestRetry(unittest.TestCase):
    def test_gateway_down_fails_open(self):
        # RETRY GAP: route_to_nova() — one POST, no retry; failure becomes {"error": ...} not an exception
        op = MagicMock(side_effect=OSError("connection refused"))
        with patch("urllib.request.urlopen", op):
            out = rr.route_to_nova("what time is it")
        self.assertEqual(out, {"error": "connection refused"})
        self.assertEqual(op.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_targets(self):
        self.assertEqual(rr.classify_request("fix the python traceback in the deploy script")["target"], "claude")
        self.assertEqual(rr.classify_request("what's the weather and turn on the light")["target"], "nova")
        self.assertEqual(rr.classify_request("troubleshoot it, figure out the cause")["target"], "both")

    def test_default_and_boosts(self):
        self.assertEqual(rr.classify_request("zzz qqq"), {"target": "nova", "confidence": 0.5, "reason": "default_to_nova"})
        self.assertEqual(rr.classify_request("/foo")["target"], "nova")
        self.assertEqual(rr.classify_request("```\nx = 1\n```")["target"], "claude")

    def test_confidence_is_a_fraction(self):
        for m in ("fix bug", "what is up", "investigate"):
            c = rr.classify_request(m)["confidence"]
            self.assertTrue(0 < c <= 1, (m, c))


class TestIntegration(unittest.TestCase):
    def test_claude_route_writes_claude_queue(self):
        conn, cur = _pg(5)
        with patch.object(rr.psycopg2, "connect", return_value=conn):
            self.assertEqual(rr.route_to_claude("t", {"a": 1}), 5)
        sql, params = cur.execute.call_args[0]
        self.assertIn("INSERT INTO claude_queue", sql)
        self.assertEqual(json.loads(params[1]), {"a": 1})
        conn.close.assert_called_once()

    def test_nova_route_posts_json(self):
        seen = {}

        def op(req, timeout=None):
            seen["url"] = req.full_url; seen["body"] = json.loads(req.data)
            return _resp({"ok": True})
        with patch("urllib.request.urlopen", side_effect=op):
            self.assertEqual(rr.route_to_nova("hi", {"c": 2}), {"ok": True})
        self.assertTrue(seen["url"].startswith("http://127.0.0.1:"))
        self.assertEqual(seen["body"], {"type": "request", "text": "hi", "source": "router", "context": {"c": 2}})


class TestFunctional(unittest.TestCase):
    def test_route_request_both_logs_and_dispatches_twice(self):
        conn, cur = _pg(9)
        with patch.object(rr.psycopg2, "connect", return_value=conn), \
             patch.object(rr, "route_to_nova", return_value={"ok": 1}) as nova:
            out = rr.route_request("investigate why is plex down", source="slack")
        self.assertEqual(out["action"], "Sent to both (Claude #9)")
        self.assertEqual(out["nova_response"], {"ok": 1})
        nova.assert_called_once()
        obs = [c for c in cur.execute.call_args_list if "shared_observations" in c[0][0]][0][0][1]
        self.assertEqual(obs[0], "both")
        self.assertTrue(obs[1].startswith("Routed from slack: investigate"))

    def test_route_request_claude_only(self):
        conn, _ = _pg(3)
        with patch.object(rr.psycopg2, "connect", return_value=conn), patch.object(rr, "route_to_nova") as nova:
            out = rr.route_request("refactor the swift class")
        self.assertEqual(out["action"], "Queued for Claude (#3)")
        nova.assert_not_called()

    def test_pg_down_raises_before_dispatch(self):
        with patch.object(rr.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")), \
             patch.object(rr, "route_to_nova") as nova:
            with self.assertRaises(psycopg2.OperationalError):
                rr.route_request("what is up")
        nova.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        # a bare run binds port 37473 forever, so the smoke is an import
        r = subprocess.run([sys.executable, "-c", "import nova_request_router"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
