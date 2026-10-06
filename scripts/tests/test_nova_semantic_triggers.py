#!/usr/bin/env python3
"""Tests for nova_semantic_triggers.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg2  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_semantic_triggers.py"
SRC = SCRIPT.read_text()

import nova_config  # noqa: E402,F401
import nova_notify  # noqa: E402,F401


def _load():
    spec = importlib.util.spec_from_file_location("nst", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("psycopg2.connect", side_effect=RuntimeError("offline")), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


st = _load()
st.notify = MagicMock(name="notify")                 # never reach Slack
st.subprocess = MagicMock(name="subprocess")          # run_script action never launches anything
st.subprocess.DEVNULL = subprocess.DEVNULL


def _trig(tid=1, name="disk-full", thr=0.8, cooldown=3600, last=None, action="slack_notify", cfg=None):
    return {"id": tid, "name": name, "reference_text": f"ref {name}", "threshold": thr, "cooldown_s": cooldown,
            "last_fired_at": last, "action_type": action, "action_config": cfg or {}}


def _req(payload=None, bad=False):
    r = MagicMock()
    r.json = AsyncMock(side_effect=ValueError("bad")) if bad else AsyncMock(return_value=payload)
    return r


def _body(resp):
    return json.loads(resp.body.decode() if isinstance(resp.body, bytes) else resp.text)


def _quiet():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(st.OPS_DSN, r"//[^/@]+:[^/@]+@")

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'_db_(query|exec)\(\s*f["\']')
        with patch.object(st, "_db_exec") as ex, _quiet():
            asyncio.run(st.handle_create(_req({"name": "x'; --", "reference_text": "r"})))
        sql, params = ex.call_args[0]
        self.assertNotIn("x'; --", sql)
        self.assertEqual(params[0], "x'; --")

    def test_run_script_only_existing_path_via_argv(self):
        st.subprocess.Popen.reset_mock()
        res = {"action_type": "run_script", "name": "n", "similarity": 0.9,
               "action_config": {"script": "/nonexistent/$(reboot).py"}}
        with _quiet():
            st.execute_action(res, "t")
        st.subprocess.Popen.assert_not_called()
        real = tempfile.NamedTemporaryFile(suffix=".py", delete=False).name
        res["action_config"] = {"script": real}
        try:
            with _quiet():
                st.execute_action(res, "t")
        finally:
            os.unlink(real)
        argv = st.subprocess.Popen.call_args[0][0]
        self.assertEqual(argv[1], real)
        self.assertNotIn("shell", st.subprocess.Popen.call_args.kwargs)


class TestPerformance(unittest.TestCase):
    def test_cosine_10k_vectors_bounded(self):
        a = [0.1] * 64
        vecs = [[(i % 7) * 0.1 + 0.01] * 64 for i in range(10_000)]
        t0 = time.perf_counter()
        sims = [st.cosine_similarity(a, v) for v in vecs]
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertAlmostEqual(sims[0], 1.0, places=6)


class TestRetry(unittest.TestCase):
    def test_embed_down_fails_open_no_fire(self):
        # RETRY GAP: get_embedding() — one POST, no retry; failure returns [] and evaluate_triggers fires nothing
        op = MagicMock(side_effect=OSError("memory server down"))
        with patch.object(st.urllib.request, "urlopen", op), patch.object(st, "load_active_triggers") as lt:
            self.assertEqual(st.evaluate_triggers("disk is full"), [])
        self.assertEqual(op.call_count, 1)
        lt.assert_not_called()

    def test_db_errors_fail_open(self):
        # RETRY GAP: _db_query()/_db_exec() — one connect, no retry; errors are logged and [] returned
        with patch.object(st.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")) as c, _quiet():
            self.assertEqual(st._db_query("SELECT 1"), [])
            st._db_exec("SELECT 1")
        self.assertEqual(c.call_count, 2)


class TestUnit(unittest.TestCase):
    def test_cosine_edges(self):
        self.assertEqual(st.cosine_similarity([], [1]), 0.0)
        self.assertEqual(st.cosine_similarity([1, 2], [1]), 0.0)
        self.assertEqual(st.cosine_similarity([0, 0], [1, 1]), 0.0)
        self.assertAlmostEqual(st.cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_threshold_and_cooldown(self):
        now = datetime.now()
        trigs = [_trig(1, "fresh"), _trig(2, "cooling", last=now - timedelta(seconds=10)),
                 _trig(3, "cooled", last=now - timedelta(hours=2)), _trig(4, "high-bar", thr=1.01)]
        with patch.object(st, "load_active_triggers", return_value=trigs), \
             patch.object(st, "get_embedding", return_value=[1.0, 0.0]), patch.object(st, "_db_exec") as ex:
            fired = st.evaluate_triggers("x", [1.0, 0.0])
        self.assertEqual([f["name"] for f in fired], ["fresh", "cooled"])
        self.assertEqual([c[0][1] for c in ex.call_args_list], [(1,), (3,)])

    def test_create_validation(self):
        self.assertEqual(asyncio.run(st.handle_create(_req(bad=True))).status, 400)
        self.assertEqual(asyncio.run(st.handle_create(_req({"name": " "}))).status, 400)
        self.assertEqual(asyncio.run(st.handle_test(_req({"text": ""}))).status, 400)


class TestIntegration(unittest.TestCase):
    def test_list_serialises_datetime_rows(self):
        # regression: json_response(default=str) was a TypeError -> /triggers/list always 500'd
        with patch.object(st, "_db_query", return_value=[{"name": "n", "created_at": datetime(2026, 1, 2, 3, 4)}]):
            resp = asyncio.run(st.handle_list(_req()))
        self.assertEqual(_body(resp)["triggers"][0]["created_at"], "2026-01-02 03:04:00")

    def test_actions_route_to_notify_queue_and_memory(self):
        st.notify.reset_mock()
        base = {"name": "n", "similarity": 0.91}
        with patch.object(st, "_db_exec") as ex, patch.object(st.urllib.request, "urlopen") as op, _quiet():
            st.execute_action({**base, "action_type": "slack_notify", "action_config": None}, "text")
            st.execute_action({**base, "action_type": "queue_for_claude", "action_config": {"priority": 1}}, "text")
            st.execute_action({**base, "action_type": "save_to_memory", "action_config": {}}, "text")
        self.assertEqual(st.notify.call_args.kwargs["dedup_key"], "semantic-trigger-n")
        self.assertIn("INSERT INTO claude_queue", ex.call_args[0][0])
        self.assertEqual(ex.call_args[0][1][1], 1)
        self.assertIn("/remember?async=1", op.call_args[0][0].full_url)


class TestFunctional(unittest.TestCase):
    def test_subscriber_evaluates_and_executes(self):
        msgs = [{"type": "subscribe", "data": 1},
                {"type": "message", "data": json.dumps({"text": "", "embedding": []})},
                {"type": "message", "data": "{not json"},
                {"type": "message", "data": json.dumps({"text": "disk full on nas", "embedding": [1, 0]})}]
        pubsub = MagicMock(); pubsub.listen.return_value = iter(msgs)
        rc = MagicMock(); rc.pubsub.return_value = pubsub
        fired = [{"name": "disk-full", "similarity": 0.9, "action_type": "slack_notify", "action_config": {}}]
        with patch.object(st.redis, "from_url", return_value=rc), \
             patch.object(st, "evaluate_triggers", return_value=fired) as ev, \
             patch.object(st, "execute_action") as ea, _quiet():
            st.subscriber_loop()
        pubsub.subscribe.assert_called_once_with("nova:memory:new")
        ev.assert_called_once_with("disk full on nas", [1, 0])
        ea.assert_called_once_with(fired[0], "disk full on nas")

    def test_test_endpoint_never_fires(self):
        with patch.object(st, "get_embedding", return_value=[1.0, 0.0]), \
             patch.object(st, "load_active_triggers", return_value=[_trig(1, "a"), _trig(2, "b", thr=1.5)]), \
             patch.object(st, "_db_exec") as ex:
            out = _body(asyncio.run(st.handle_test(_req({"text": "x"}))))
        self.assertEqual([(r["name"], r["would_fire"]) for r in out["results"]], [("a", True), ("b", False)])
        ex.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        # a bare run binds :37472 and subscribes to Redis forever, so the smoke is an import
        r = subprocess.run([sys.executable, "-c", "import nova_semantic_triggers"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
