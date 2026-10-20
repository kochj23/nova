#!/usr/bin/env python3
"""7-category tests for the 2026-10-20 Grafana snapshot fix: nova_journal.grafana_panel_image must
skip the embed when the dashboard/panel doesn't exist (Grafana renders that as a 200 PNG of its error
page, which shipped on every ops/security article Oct 13-20), and every caller must point at a live
consolidated dashboard (nova-sec / nova-fleet). Grafana is mocked except in the Integration test.
Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_grafana_panel_guard_7cat.py
"""
import io
import json
import re
import sys
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_journal as nj  # noqa: E402

DASH = {"dashboard": {"panels": [{"id": 1, "type": "row", "panels": [{"id": 33, "type": "timeseries"}]},
                                 {"id": 7, "type": "timeseries"}]}}
CALLERS = ["nova_daily_ops_log.py", "nova_journal_security.py", "nova_operations_security.py",
           "nova_postmortem.py", "nova_rando_daily_ops.py"]
LIVE = {"nova-sec", "nova-fleet"}


def _resp(obj):
    return io.BytesIO(json.dumps(obj).encode())


def _urlopen_dash(url, timeout=None):
    return _resp(DASH)


class TestSecurity(unittest.TestCase):
    def test_embeds_are_timeseries_not_tables(self):
        # Tables (Top Source IPs, recent events) would leak IPs onto the public site.
        for f in CALLERS:
            for uid, pid in re.findall(r'grafana_panel_image\("([\w-]+)", (\d+)', (SCRIPTS / f).read_text()):
                self.assertIn((uid, int(pid)), {("nova-sec", 33), ("nova-sec", 7), ("nova-fleet", 30), ("nova-fleet", 59)}, f)


class TestPerformance(unittest.TestCase):
    def test_existence_check_is_fast(self):
        with patch.object(nj.urllib.request, "urlopen", _urlopen_dash):
            t = time.time()
            for _ in range(200):
                nj._grafana_panel_exists("nova-sec", 33)
            self.assertLess(time.time() - t, 1.0)


class TestRetry(unittest.TestCase):
    def test_grafana_down_skips_embed_without_raising(self):
        with patch.object(nj.urllib.request, "urlopen", side_effect=OSError("refused")):
            self.assertIsNone(nj.grafana_panel_image("nova-sec", 33, "operations", "x"))


class TestUnit(unittest.TestCase):
    def test_finds_top_level_and_collapsed_row_panels(self):
        with patch.object(nj.urllib.request, "urlopen", _urlopen_dash):
            self.assertTrue(nj._grafana_panel_exists("nova-sec", 7))
            self.assertTrue(nj._grafana_panel_exists("nova-sec", 33))
            self.assertFalse(nj._grafana_panel_exists("nova-sec", 99))

    def test_missing_dashboard_is_false(self):
        with patch.object(nj.urllib.request, "urlopen", lambda u, timeout=None: _resp({"message": "Dashboard not found"})):
            self.assertFalse(nj._grafana_panel_exists("security-posture", 5))


class TestIntegration(unittest.TestCase):
    def test_live_grafana_has_every_caller_panel(self):
        try:
            urllib.request.urlopen(nj.resolve_url("grafana", "/api/health"), timeout=5)
        except Exception:
            self.skipTest("grafana unreachable")
        for f in CALLERS:
            for uid, pid in re.findall(r'grafana_panel_image\("([\w-]+)", (\d+)', (SCRIPTS / f).read_text()):
                self.assertTrue(nj._grafana_panel_exists(uid, int(pid)), f"{f}: {uid}/{pid}")


class TestFunctional(unittest.TestCase):
    def test_missing_panel_never_renders(self):
        calls = []

        def fake(url, timeout=None):
            calls.append(url)
            return _resp({"message": "not found"})
        with patch.object(nj.urllib.request, "urlopen", fake):
            self.assertIsNone(nj.grafana_panel_image("security-posture", 5, "operations", "daily-briefing-posture"))
        self.assertFalse(any("/render/" in u for u in calls))


class TestFrame(unittest.TestCase):
    def test_callers_use_live_dashboards_only(self):
        for f in CALLERS:
            uids = set(re.findall(r'grafana_panel_image\("([\w-]+)"', (SCRIPTS / f).read_text()))
            self.assertTrue(uids and uids <= LIVE, f"{f}: {uids}")


if __name__ == "__main__":
    unittest.main()
