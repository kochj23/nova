#!/usr/bin/env python3
"""Tests for nova_network_health.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2          # noqa: F401  (real module locked in before any stubbing)
import psycopg2.extras   # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_network_health.py"
SRC = SCRIPT.read_text()


def _stub_modules():
    cfg = types.ModuleType("nova_config")
    cfg.SLACK_ALERTS = "C_ALERTS"
    cfg.post_both = MagicMock()
    nj = types.ModuleType("nova_journal")
    nj.call_openrouter = MagicMock(return_value="body")
    nj.publish_hugo = MagicMock(return_value=True)
    nj.git_push = MagicMock()
    nv = types.ModuleType("nova_voice")
    nv.system_prompt = MagicMock(return_value="SYSTEM")
    iu = types.ModuleType("nova_image_utils")
    iu.generate_image = MagicMock(return_value=None)
    return {"nova_config": cfg, "nova_journal": nj, "nova_voice": nv, "nova_image_utils": iu}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()), patch("psycopg2.connect", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


nh = _load("nh", SCRIPT)


def _metrics(**over):
    m = {
        "tiers": [{"tier": "infra", "n": 12}, {"tier": "camera", "n": 4}, {"tier": "smart_home", "n": 30},
                  {"tier": "coordinator", "n": 2}],
        "worst_devices": [{"name": "nova-core3", "tier": "infra", "pct": 71.5, "n": 400}],
        "feeds": [{"name": "unifi:192.168.1.1", "pct": 100.0, "n": 2016},
                  {"name": "weather:api", "pct": 55.0, "n": 2016},
                  {"name": "cams:rtsp", "pct": 2.0, "n": 2016},
                  {"name": "ha:ws", "pct": 95.0, "n": 2016}],
        "open_problems": [{"entity": "cam-garage", "tier": "camera", "detail": "no reply", "hours_open": 3.5}],
        "resolved": [{"entity": "pi-rack", "tier": "infra", "hours_down": 7.2}],
        "new_devices": [{"name": "new-tv", "tier": "smart_home"}],
        "vanished": [{"name": "old-plug", "tier": "infra", "since": "2026-10-01"}],
    }
    m.update(over)
    return m


class _Cur:
    def __init__(self, rows):
        self.rows = rows; self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.closed = False

    def cursor(self, cursor_factory=None):
        return self.cur

    def close(self):
        self.closed = True


def _run_main():
    nh.nova_config.post_both = MagicMock()
    nh.nj.call_openrouter = MagicMock(side_effect=["article body " * 50, '"Grading My Own Nerves"'])
    nh.nj.publish_hugo = MagicMock(return_value=True)
    nh.nj.git_push = MagicMock()
    hist = types.ModuleType("nova_article_history")
    hist.recent_articles_context = MagicMock(return_value="RECENT: last week's column")
    with patch.object(nh, "gather", return_value=_metrics()), patch.object(nh, "generate_image", return_value="/tmp/x.png"), \
         patch.dict(sys.modules, {"nova_article_history": hist}), redirect_stdout(io.StringIO()) as out:
        rc = nh.main()
    return rc, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", nh.DSN)

    def test_sql_is_constant_and_read_only(self):
        self.assertIsNone(re.search(r'_q\(\s*f"', SRC))
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM|DROP)\b", SRC))
        cur = _Cur([])
        with patch("psycopg2.connect", return_value=_Conn(cur)):
            nh.gather()
        self.assertTrue(all(s.startswith("SELECT") for s in cur.sql))

    def test_public_brief_never_carries_device_names_or_ips(self):
        m = _metrics(feeds=[{"name": "unifi:192.168.1.1", "pct": 40.0, "n": 10}])
        brief = nh._sanitized_brief(m)
        self.assertNotIn("192.168", brief)
        self.assertNotIn("nova-core3", brief)
        self.assertNotIn("cam-garage", brief)
        self.assertNotIn("old-plug", brief)
        self.assertIn("a 'unifi' data feed", brief)

    def test_article_prompt_only_sees_the_sanitized_brief(self):
        nh.nj.call_openrouter = MagicMock(side_effect=["body text", "Title"])
        with patch.dict(sys.modules, {"nova_article_history": types.ModuleType("nova_article_history")}):
            nh.generate_article(_metrics())
        user = nh.nj.call_openrouter.call_args_list[0][0][1]
        self.assertNotIn("nova-core3", user)
        self.assertNotIn("192.168", user)
        self.assertIn("Data feeds tracked: 4", user)


class TestPerformance(unittest.TestCase):
    def test_render_and_brief_fast_on_10k_feeds(self):
        feeds = [{"name": f"feed{i}:x", "pct": float(i % 101), "n": 100} for i in range(10_000)]
        m = _metrics(feeds=feeds)
        t0 = time.perf_counter()
        slack = nh.render_slack(m); brief = nh._sanitized_brief(m)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(slack.count("\n  "), 10_000 + len(m["open_problems"]) + len(m["worst_devices"]))
        self.assertIn("Data feeds tracked: 10000", brief)


class TestRetry(unittest.TestCase):
    def test_query_is_one_shot_and_fails_open(self):
        # RETRY GAP: _q() — a failed connect/query is tried once; returns [] (or {} with one=True)
        with patch("psycopg2.connect", side_effect=RuntimeError("db down")) as c, redirect_stdout(io.StringIO()):
            self.assertEqual(nh._q("SELECT 1"), [])
            self.assertEqual(nh._q("SELECT 1", one=True), {})
        self.assertEqual(c.call_count, 2)
        with patch("psycopg2.connect", side_effect=RuntimeError("db down")), redirect_stdout(io.StringIO()):
            m = nh.gather()
        self.assertEqual(set(m), {"tiers", "worst_devices", "feeds", "open_problems", "resolved", "new_devices", "vanished"})
        self.assertTrue(all(v == [] for v in m.values()))

    def test_main_survives_slack_and_publish_failures(self):
        # RETRY GAP: main()/post_both + publish_hugo — each is one attempt, errors are logged, never raised
        nh.nova_config.post_both = MagicMock(side_effect=RuntimeError("slack down"))
        nh.nj.call_openrouter = MagicMock(side_effect=RuntimeError("llm down"))
        with patch.object(nh, "gather", return_value=_metrics()), redirect_stdout(io.StringIO()) as out:
            rc = nh.main()
        self.assertIsNone(rc)
        self.assertIn("slack post failed: slack down", out.getvalue())
        self.assertIn("article publish failed: llm down", out.getvalue())
        self.assertIn("done", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_render_slack_icons_and_sections(self):
        s = nh.render_slack(_metrics())
        self.assertIn("🟢 unifi:192.168.1.1: 100.0%", s)
        self.assertIn("🟡 ha:ws: 95.0%", s)
        self.assertIn("🔴 weather:api: 55.0%", s)
        self.assertIn("🔴 cam-garage (camera) — no reply [3.5h]", s)
        self.assertIn("nova-core3 (infra): 71.5% up", s)
        self.assertIn("pi-rack 7.2h", s)
        self.assertIn("new-tv(smart_home)", s)
        self.assertIn("old-plug since 2026-10-01", s)

    def test_render_slack_handles_empty_and_null_pct(self):
        s = nh.render_slack({})
        self.assertIn("*Currently open:* 0", s)
        self.assertNotIn("Least-reliable", s)
        s = nh.render_slack({"feeds": [{"name": "f", "pct": None, "n": 0}]})
        self.assertIn("🔴 f: None%", s)

    def test_sanitized_brief_bands_and_fleet_counts(self):
        b = nh._sanitized_brief(_metrics())
        self.assertIn("Rock-solid (>=99% this week): 1. Flaky or dark: 3.", b)
        self.assertIn("'cams' data feed, totally dark (2.0%", b)
        self.assertIn("'weather' data feed, badly flaky (55.0%", b)
        self.assertIn("'ha' data feed, occasionally dropping (95.0%", b)
        self.assertIn("~48 devices (12 infra, 2 hubs, 4 cameras, 30 smart-home)", b)
        self.assertIn("worst had ~7.2 hours of downtime", b)
        self.assertIn("1 monitored device(s) have gone silent", b)

    def test_sanitized_brief_on_empty_metrics(self):
        b = nh._sanitized_brief({})
        self.assertIn("Data feeds tracked: 0", b)
        self.assertIn("~0 devices", b)
        self.assertNotIn("Recovered", b)


class TestIntegration(unittest.TestCase):
    def test_publishing_goes_through_nova_journal_not_a_copy(self):
        self.assertIn("import nova_journal as nj", SRC)
        for fn in ("def publish_hugo", "def git_push", "def call_openrouter"):
            self.assertNotIn(fn, SRC)
        self.assertIn("nj.publish_hugo(", SRC); self.assertIn("nj.git_push(", SRC)
        self.assertIn("nova_config.SLACK_ALERTS", SRC)

    def test_gather_reads_the_watchtower_tables(self):
        cur = _Cur([{"tier": "infra", "n": 1}])
        with patch("psycopg2.connect", return_value=_Conn(cur)):
            m = nh.gather()
        joined = "\n".join(cur.sql)
        for t in ("telemetry.net_inventory", "telemetry.net_liveness", "telemetry.net_problems"):
            self.assertIn(t, joined)
        self.assertEqual(len(cur.sql), 7)
        self.assertEqual(m["tiers"], [{"tier": "infra", "n": 1}])

    def test_generate_article_chains_brief_history_and_two_llm_calls(self):
        nh.nj.call_openrouter = MagicMock(side_effect=["  the body  ", ' "A Title" '])
        nh.nova_voice.system_prompt = MagicMock(return_value="SYS")
        hist = types.ModuleType("nova_article_history")
        hist.recent_articles_context = MagicMock(return_value="RECENT CONTEXT")
        with patch.dict(sys.modules, {"nova_article_history": hist}):
            title, body = nh.generate_article(_metrics())
        self.assertEqual((title, body), ("A Title", "the body"))
        hist.recent_articles_context.assert_called_once_with("operations")
        self.assertEqual(nh.nova_voice.system_prompt.call_args[1]["section"], "operations")
        first, second = nh.nj.call_openrouter.call_args_list
        self.assertEqual(first[0][0], "SYS")
        self.assertTrue(first[0][1].endswith("RECENT CONTEXT"))
        self.assertEqual(second[0][1], "the body"[:800])


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_full_detail_then_publishes_sanitized_column(self):
        rc, out = _run_main()
        self.assertIsNone(rc)
        msg, kw = nh.nova_config.post_both.call_args[0][0], nh.nova_config.post_both.call_args[1]
        self.assertTrue(msg.startswith("*Network Health — 7-day review* (full detail)"))
        self.assertIn("nova-core3", msg)
        self.assertEqual(kw, {"slack_channel": "C_ALERTS", "discord_channel": None})
        args, kw = nh.nj.publish_hugo.call_args
        self.assertEqual(args[0], "Grading My Own Nerves")
        self.assertEqual(args[2], "operations")
        self.assertEqual(args[3], ["ops", "network", "reliability", "uptime", "weekly"])
        src = kw.pop("sources")                          # sanitized brief: grounding for the length policy (2026-10-06)
        self.assertTrue(src)
        self.assertEqual(kw, {"image_path": "/tmp/x.png", "emoji": "📶", "profile": "network-health"})
        nh.nj.git_push.assert_called_once_with("operations", "Grading My Own Nerves")
        self.assertIn("published to /operations (image: yes)", out)

    def test_image_failure_still_publishes_without_cover(self):
        nh.nova_config.post_both = MagicMock()
        nh.nj.call_openrouter = MagicMock(side_effect=["body", "T"])
        nh.nj.publish_hugo = MagicMock(return_value=True); nh.nj.git_push = MagicMock()
        with patch.object(nh, "gather", return_value=_metrics()), \
             patch.object(nh, "generate_image", side_effect=RuntimeError("no gpu")), \
             patch.dict(sys.modules, {"nova_article_history": types.ModuleType("nova_article_history")}), \
             redirect_stdout(io.StringIO()) as out:
            nh.main()
        self.assertIsNone(nh.nj.publish_hugo.call_args[1]["image_path"])
        self.assertIn("image gen failed: no gpu", out.getvalue())
        self.assertIn("(image: none)", out.getvalue())


class TestFrame(unittest.TestCase):
    SHIM = ("import psycopg2, urllib.request, subprocess;"
            "psycopg2.connect=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('offline'));"
            "urllib.request.urlopen=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('offline'));"
            "subprocess.run=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('offline'));")

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", self.SHIM + "import nova_network_health"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("gathering network health", r.stdout)


if __name__ == "__main__":
    unittest.main()
