#!/usr/bin/env python3
"""Tests for nova_slack_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Slack, PG and the local LLM are all mocked.
Written by Jordan Koch (via Claude)."""
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
SCRIPT = SCRIPTS / "nova_slack_watch.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_slack_watch_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sw = _load()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _resp(obj):
    return _Resp(json.dumps(obj).encode())


class _Base(unittest.TestCase):
    def setUp(self):
        self.ps = [patch.object(sw.nova_config, "slack_bot_token", return_value="xoxb-test"),
                   patch.object(sw.urllib.request, "urlopen", side_effect=OSError("offline")),
                   patch("sys.stderr", new_callable=io.StringIO)]
        _, self.urlopen, self.err = [p.start() for p in self.ps]
        self._r = redirect_stdout(io.StringIO())
        self.out = self._r.__enter__()

    def tearDown(self):
        self._r.__exit__(None, None, None)
        for p in self.ps:
            p.stop()

    def run_watch(self, msgs, assessment=None, seen=False, dry=False):
        conn = MagicMock()
        fetch = lambda cid, wm: [dict(m) for m in msgs.get(cid, [])]  # noqa: E731
        with patch.object(sw, "_db", return_value=conn), patch.object(sw, "get_watermark", return_value=0.0), \
             patch.object(sw, "fetch_new", side_effect=fetch), patch.object(sw, "llm_assess", return_value=assessment), \
             patch.object(sw, "already_reported", return_value=seen) as ar, patch.object(sw, "post_alert") as pa, \
             patch.object(sw, "set_watermark") as swm:
            rc = sw.run(dry=dry)
        return rc, pa, swm, ar


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"xox[bpa]-\d")
        self.assertIn("nova_config.slack_bot_token()", SRC)

    def test_redaction(self):
        raw = ("token xoxb-1234567890-abc sk-ABCDEFGHIJKLMNOP AKIAABCDEFGHIJKLMNOP Bearer abc.def.ghi1 "
               "password=hunter22 " + "a" * 40)
        out = sw._redact(raw)
        for leak in ("xoxb-1234567890", "sk-ABCDEF", "AKIAABCD", "abc.def.ghi1", "hunter22", "a" * 40):
            self.assertNotIn(leak, out)

    def test_refuses_non_loopback_llm(self):
        with patch.object(sw, "OLLAMA_URL", "https://api.example.com/chat"):
            with self.assertRaises(RuntimeError):
                sw.llm_assess([{"channel": "#nova-chat", "text": "x"}])
        self.urlopen.assert_not_called()

    def test_llm_receives_redacted_truncated_text(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = _resp({"message": {"content": '{"notable": false, "items": []}'}})
        sw.llm_assess([{"channel": "#nova-chat", "text": "leak xoxb-99999999999 " + "z" * 900}])
        prompt = json.loads(self.urlopen.call_args[0][0].data)["messages"][0]["content"]
        self.assertNotIn("xoxb-99999999999", prompt)
        self.assertNotIn("z" * 401, prompt)


class TestPerformance(unittest.TestCase):
    def test_heuristic_10k(self):
        msgs = [{"channel": "#nova-info", "text": "routine heartbeat ok"} for i in range(10_000)]
        msgs[5]["text"] = "backup FAILED"
        t0 = time.perf_counter()
        flagged = sw.heuristic_flags(msgs)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(flagged), 1)


class TestRetry(_Base):
    def test_llm_down_falls_back_to_heuristic(self):
        # RETRY GAP: llm_assess — one local call; failure -> None -> deterministic heuristic
        self.assertIsNone(sw.llm_assess([{"channel": "#nova-chat", "text": "x"}]))
        rc, pa, _, _ = self.run_watch({sw.nova_config.SLACK_FEED: [{"ts": 1.0, "text": "disk full on nas"}]})
        self.assertEqual(rc, 0)
        self.assertIn("heuristic scan", pa.call_args[0][0])

    def test_fetch_stops_on_slack_error(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = _resp({"ok": False, "error": "ratelimited"})
        self.assertEqual(sw.fetch_new("C1", 0.0), [])
        self.assertEqual(self.urlopen.call_count, 1)


class TestUnit(_Base):
    def test_fetch_filters_boundary_own_and_joins(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = _resp({"ok": True, "messages": [
            {"ts": "10.0", "text": "boundary"}, {"ts": "12.0", "text": sw.WATCH_MARKER + " digest"},
            {"ts": "13.0", "subtype": "channel_join", "text": "joined"}, {"ts": "14.0", "text": "real"},
            {"ts": "11.0", "text": "older real"}]})
        self.assertEqual([m["text"] for m in sw.fetch_new("C1", 10.0)], ["older real", "real"])

    def test_build_digest(self):
        d = sw.build_digest({"severity": "critical", "headline": "H",
                             "items": [{"channel": "#nova-x", "what": "w", "why": "y"}]})
        self.assertTrue(d.startswith(sw.WATCH_MARKER))
        self.assertIn("• `#nova-x` w — _y_", d)


class TestIntegration(_Base):
    def test_db_state_tables_and_watermark(self):
        cur = MagicMock()
        cur.fetchone.return_value = None
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        wm = sw.get_watermark(conn, "#nova-chat")
        self.assertAlmostEqual(wm, time.time() - sw.LOOKBACK_FIRST_RUN_S, delta=5)
        self.assertFalse(sw.already_reported(conn, "k"))
        self.assertIn("INSERT INTO slack_watch_reports", cur.execute.call_args[0][0])
        self.assertEqual(sw.ALERT_CHANNEL, sw.nova_config.SLACK_BB)


class TestFunctional(_Base):
    def test_golden_path_posts_and_advances(self):
        a = {"notable": True, "severity": "warning", "headline": "PG lag", "items": [{"channel": "#nova-info", "what": "lag"}]}
        rc, pa, swm, _ = self.run_watch({sw.nova_config.SLACK_FEED: [{"ts": 5.0, "text": "pg lag 40s"}]}, a)
        self.assertIn("PG lag", pa.call_args[0][0])
        swm.assert_called_once_with(swm.call_args[0][0], "#nova-info", 5.0)

    def test_duplicate_and_dry_run_do_not_post(self):
        a = {"notable": True, "items": [{"channel": "#nova-info", "what": "lag"}]}
        msgs = {sw.nova_config.SLACK_FEED: [{"ts": 5.0, "text": "x"}]}
        _, pa, _, _ = self.run_watch(msgs, a, seen=True)
        pa.assert_not_called()
        _, pa, swm, ar = self.run_watch(msgs, a, dry=True)
        pa.assert_not_called()
        swm.assert_not_called()
        ar.assert_not_called()

    def test_no_messages(self):
        rc, pa, _, _ = self.run_watch({})
        self.assertEqual(rc, 0)
        pa.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_slack_watch; print('ok')"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
