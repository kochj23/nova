#!/usr/bin/env python3
"""Tests for nova_local_burbank.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). PG, the memory server, Claude Code / OpenRouter, image generation,
Hugo writes (tempdir), git push and Slack are all mocked. Written by Jordan Koch (via Claude)."""
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
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_local_burbank.py").read_text()
_TMP = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("localburbank", SCRIPTS / "nova_local_burbank.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lb = _load()
lb.LOG_FILE = Path(_TMP.name) / "burbank.log"
lb.CONTENT_DIR = Path(_TMP.name) / "content/local"
lb.IMAGES_DIR = Path(_TMP.name) / "static/images/local"
lb.print = lambda *a, **k: None

_PATCHES = []
POST = MagicMock()
PUSH = MagicMock()


def setUpModule():
    # every outbound side effect is stubbed for the whole file
    for p in (patch.object(lb.nova_config, "post_both", POST), patch.object(lb.nj, "git_push", PUSH),
              patch.object(lb.urllib.request, "urlopen", side_effect=OSError("offline"))):
        p.start(); _PATCHES.append(p)


def tearDownModule():
    for p in reversed(_PATCHES):
        p.stop()
    _PATCHES.clear()


class _Cur:
    def __init__(self, c):
        self.c = c

    def execute(self, sql, params=None):
        self.c.sql.append((sql, params))

    def fetchall(self):
        return self.c.all.pop(0) if self.c.all else []

    def fetchone(self):
        return self.c.one.pop(0) if self.c.one else None

    def close(self):
        pass


class _Conn:
    def __init__(self, all_=None, one=None):
        self.all = list(all_ or []); self.one = list(one or []); self.sql = []

    def cursor(self):
        return _Cur(self)

    def close(self):
        pass


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body


def _scanner_rows(n):
    rows = []
    for i in range(n):
        geo = {"nearest_mi": 1.0 + (i % 5), "locations": [{"addr": "123 Elm St", "mi": 0.4},
                                                          {"addr": "Olive Ave & Glenoaks Blvd", "mi": 0.9}]}
        rows.append((["scanner", "fire", "rail"][i % 3], f"[Ch] traffic stop medical signal clear {i}", geo))
    return rows


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"sk-or-v1-[0-9a-f]{8}")

    def test_openrouter_key_from_keychain(self):
        with patch.object(lb.subprocess, "run", return_value=types.SimpleNamespace(stdout="k\n")) as r:
            self.assertEqual(lb.get_openrouter_key(), "k")
        self.assertEqual(r.call_args[0][0][:2], ["security", "find-generic-password"])

    def test_house_numbers_never_leave_blotter(self):
        with patch("psycopg2.connect", return_value=_Conn(all_=[_scanner_rows(9)])):
            out = lb.get_scanner_blotter()
        blob = json.dumps(out)
        self.assertNotIn("123 Elm", blob)
        self.assertIn("Olive Ave & Glenoaks Blvd", blob)
        self.assertNotIn("traffic stop medical", blob)        # raw transcripts never returned

    def test_guard_blocks_publish(self):
        PUSH.reset_mock()
        guard = types.SimpleNamespace(is_publishable=lambda t, b: (False, "auth stub"))
        with patch.dict(sys.modules, {"nova_journal_guard": guard}):
            lb.publish("Not logged in", "Please run /login", None)
        PUSH.assert_not_called()
        self.assertFalse(lb.CONTENT_DIR.exists() and any(lb.CONTENT_DIR.glob("*Not*")))


class TestPerformance(unittest.TestCase):
    def test_blotter_on_10k_rows_fast(self):
        with patch("psycopg2.connect", return_value=_Conn(all_=[_scanner_rows(10_000)])):
            t0 = time.perf_counter()
            out = lb.get_scanner_blotter()
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(sum(d["calls"] for d in out.values()), 10_000)
        self.assertLessEqual(len(out["police"]["near_events"]), 6)


class TestRetry(unittest.TestCase):
    def test_auth_stub_alerts_and_falls_back_to_openrouter(self):
        POST.reset_mock()
        cc = types.SimpleNamespace(claude_generate=lambda u, system=None: "Not logged in · Please run /login")
        body = json.dumps({"choices": [{"message": {"content": "real article"}}]}).encode()
        with patch.dict(sys.modules, {"nova_claude_code": cc}), patch.object(lb, "get_openrouter_key", return_value="k"), \
             patch.object(lb.urllib.request, "urlopen", return_value=_Resp(body)) as uo:
            self.assertEqual(lb.call_llm("s", "u"), "real article")
        self.assertEqual(uo.call_count, 1)
        self.assertIn("auth stub", POST.call_args[0][0])

    def test_feeds_fail_open(self):
        # RETRY GAP: the PG / memory-server feeds are one-shot; each returns None/[] instead of raising
        with patch("psycopg2.connect", side_effect=RuntimeError("pg down")):
            self.assertIsNone(lb.get_scanner_blotter())
            self.assertIsNone(lb.get_overhead_flights())
            self.assertIsNone(lb.get_wifi_ble_summary())
            self.assertIsNone(lb.get_lora_summary())
            self.assertIsNone(lb.get_bluetooth_patterns())
        self.assertEqual(lb.get_burbank_search(), [])          # urlopen is offline for the whole file


class TestUnit(unittest.TestCase):
    def test_burbank_search_drops_stale_and_undated(self):
        now = datetime.now().astimezone()
        mems = [{"text": "fresh", "created_at": now.isoformat()},
                {"text": "old", "created_at": (now - timedelta(days=5)).isoformat()},
                {"text": "undated"}]
        with patch.object(lb.urllib.request, "urlopen", return_value=_Resp(json.dumps({"memories": mems}).encode())):
            self.assertEqual([m["text"] for m in lb.get_burbank_search()], ["fresh"])

    def test_flights_dedup_and_notable(self):
        ts = datetime(2024, 1, 1, 14, 5)
        rows = [("h1", "EC135", "LAPD", "N1", None, True, 900, 0.5, "N", "1200", ts),
                ("h2", "A320", None, None, "SWA1", False, 9000, 4.0, "E", "7700", ts),
                ("h3", "B738", None, None, None, False, 12000, 6.0, "W", "1200", ts)]
        with patch("psycopg2.connect", return_value=_Conn(all_=[rows])):
            f = lb.get_overhead_flights()
        self.assertEqual((f["total"], f["helicopter_count"], f["emergency_count"]), (3, 1, 1))
        self.assertEqual(len(f["notable"]), 2)                  # emergency + low-pass helicopter, once each
        self.assertTrue(f["notable"][0].startswith("A320"))

    def test_bluetooth_pattern_classification(self):
        rows = [("a", 5, 20.0, 3.0, 0.5), ("b", 4, 2.0, 7.0, 0.5), ("c", 3, 2.0, 15.0, 4.0)]
        with patch("psycopg2.connect", return_value=_Conn(all_=[rows], one=[(2,), (1,)])):
            p = lb.get_bluetooth_patterns()
        self.assertEqual((p["resident"], p["transient_random"], p["new_brief"], p["trackers"]), (1, 1, 2, 1))
        self.assertEqual(p["transient_patterned"], [{"distinct_days": 4, "start": "~7 AM"}])


class TestIntegration(unittest.TestCase):
    def test_local_news_reads_memories_with_bound_params(self):
        conn = _Conn(all_=[[("text " * 30, "local_burbank", "2024-01-01", '{"url": "u"}')]])
        with patch("psycopg2.connect", return_value=conn) as c:
            items = lb.get_local_news(hours=24, limit=7)
        self.assertIn("dbname=nova_memories", c.call_args[0][0])
        self.assertEqual(conn.sql[0][1][1], 7)
        self.assertEqual(items[0]["metadata"], {"url": "u"})

    def test_arrests_dedup_by_url_and_parse_array(self):
        news = [{"text": "Burbank Police Log", "metadata": {"url": "https://x/log"}},
                {"text": "burbank police log again", "metadata": {"url": "https://x/log"}}]
        llm = MagicMock(return_value='[{"name": "A", "charges": "x"}] trailing note')
        with patch.object(lb.urllib.request, "urlopen", return_value=_Resp(b"<p>arrest text</p>")) as uo, \
             patch.object(lb, "call_llm", llm):
            arrests = lb.get_myburbank_arrests(news)
        self.assertEqual(uo.call_count, 1)
        self.assertEqual(arrests, [{"name": "A", "charges": "x"}])


class TestFunctional(unittest.TestCase):
    def _feeds(self):
        return [patch.object(lb, n, return_value=v) for n, v in (
            ("get_local_news", [{"text": "Burbank news item", "source": "local_news", "created_at": "", "metadata": {}}]),
            ("get_burbank_search", []), ("get_scanner_blotter", None), ("get_overhead_flights", None),
            ("get_wifi_ble_summary", None), ("get_bluetooth_patterns", None), ("get_myburbank_arrests", []),
            ("get_lora_summary", None), ("generate_article", "ARTICLE BODY"))]

    def test_dry_run_publishes_nothing(self):
        ps = self._feeds()
        for p in ps:
            p.start()
        try:
            with patch.object(lb, "publish") as pub, patch.object(sys, "argv", ["x", "--dry-run"]):
                lb.main()
        finally:
            for p in ps:
                p.stop()
        pub.assert_not_called()

    def test_main_publishes_and_writes_post(self):
        PUSH.reset_mock(); POST.reset_mock()
        ps = self._feeds()
        for p in ps:
            p.start()
        guard = types.SimpleNamespace(is_publishable=lambda t, b: (True, ""))
        try:
            with patch.object(lb, "generate_title", return_value="Burbank Does A Thing"), \
                 patch.object(lb, "generate_image", return_value=None), patch.object(sys, "argv", ["x"]), \
                 patch.dict(sys.modules, {"nova_journal_guard": guard,
                                          "nova_weather_blurb": types.SimpleNamespace(weather_dateline_line=lambda: "WX\n")}):
                lb.main()
        finally:
            for p in ps:
                p.stop()
        post = next(lb.CONTENT_DIR.glob("*burbank-does-a-thing.md"))
        text = post.read_text()
        self.assertIn('title: "Burbank Does A Thing"', text)
        self.assertIn("WX\nARTICLE BODY", text)
        PUSH.assert_called_once_with("local", "Burbank Does A Thing")
        self.assertIn("/local/", POST.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # main() reads PG, calls the LLM and git-pushes, so the smoke is an import only
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_local_burbank"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
