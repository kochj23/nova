#!/usr/bin/env python3
"""Tests for nova_anticipation_engine.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Loaded with Path.home -> tempdir and logging.basicConfig stubbed. osascript/curl/df/ioreg
(subprocess.run), urlopen, PG and Slack are mocked for the whole file; the daemon loop is
broken out of via a raising time.sleep."""
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_anticipation_engine.py"
SRC = PATH.read_text()
_TD = tempfile.TemporaryDirectory()
_REAL_RUN = subprocess.run


def _load():
    spec = importlib.util.spec_from_file_location("anticipation_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(Path, "home", return_value=Path(_TD.name)), patch.object(logging, "basicConfig"):
        spec.loader.exec_module(mod)
    return mod


ae = _load()
_PATCHES = []


def setUpModule():
    ae.logger.disabled = True
    for p in (patch.object(ae.subprocess, "run", side_effect=OSError("no subprocess in tests")),
              patch.object(ae.urllib.request, "urlopen", side_effect=OSError("offline")),
              patch.object(psycopg2, "connect", side_effect=OSError("no PG in tests")),
              patch.object(ae.nova_config, "post_both", side_effect=AssertionError("unmocked Slack"))):
        p.start(); _PATCHES.append(p)


def tearDownModule():
    ae.logger.disabled = False
    while _PATCHES:
        _PATCHES.pop().stop()


def _state(**kw):
    s = {"delivered_today": [], "hold_queue": [], "last_delivery": 0, "daily_count": 0,
         "date": datetime.now().date().isoformat(), "topic_cooldowns": {}}
    s.update(kw)
    return s


OBS = {"type": "infrastructure", "priority": 2, "message": "Disk / at 91%", "topic": "disk:/"}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))

    def test_delivers_only_to_jordan_dm(self):
        with patch.object(ae.nova_config, "post_both") as post:
            ae.deliver(dict(OBS), _state())
        self.assertEqual(post.call_args.kwargs["slack_channel"], ae.nova_config.JORDAN_DM)
        self.assertTrue(post.call_args.args[0].startswith("*Infra note:*"))

    def test_never_interrupts_meeting_sleep_or_deep_work(self):
        for act in ("meeting", "sleeping", "deep_work", "away", "bogus"):
            self.assertFalse(ae.should_deliver(dict(OBS, priority=1), _state(), act), act)


class TestPerformance(unittest.TestCase):
    def test_should_deliver_10k_fast(self):
        st = _state(topic_cooldowns={f"t{i}": time.time() for i in range(500)})
        t0 = time.perf_counter()
        for i in range(10_000):
            ae.should_deliver({"priority": i % 6, "topic": f"t{i % 1000}"}, st, "available")
        self.assertLess(time.perf_counter() - t0, 1.0)
        st = _state()                                     # and the hold queue is bounded
        for i in range(50):
            ae.queue_for_later({"topic": f"t{i}", "priority": 2}, st)
        self.assertEqual(len(st["hold_queue"]), 20)


class TestRetry(unittest.TestCase):
    def test_signals_fail_open(self):
        # RETRY GAP: get_presence/get_upcoming_meetings/get_disk_usage/check_environment — one attempt each,
        # every failure degrades to a safe default and the next 60s cycle retries
        self.assertEqual(ae.get_presence(), {"room": "unknown", "confidence": 0, "home": True})
        self.assertEqual(ae.get_disk_usage(), {})
        self.assertEqual(ae.get_presence_duration(), 0)
        self.assertEqual(ae.check_environment(), [])
        self.assertIsNone(ae._mac_idle_seconds())

    def test_delivery_failure_leaves_state_untouched(self):
        st = _state()
        with patch.object(ae.nova_config, "post_both", side_effect=RuntimeError("slack 500")):
            ae.deliver(dict(OBS), st)
        self.assertEqual((st["daily_count"], st["topic_cooldowns"]), (0, {}))

    def test_daemon_loop_survives_cycle_errors(self):
        class Stop(Exception):
            pass
        with patch.object(ae, "evaluate", side_effect=[RuntimeError("boom"), None]) as ev, \
                patch.object(ae.time, "sleep", side_effect=[None, Stop()]):
            with self.assertRaises(Stop):
                ae.main()
        self.assertEqual(ev.call_count, 2)


class TestUnit(unittest.TestCase):
    def test_should_deliver_rules(self):
        self.assertTrue(ae.should_deliver(dict(OBS), _state(), "available"))
        self.assertFalse(ae.should_deliver(dict(OBS, priority=4), _state(), "planning"))
        self.assertFalse(ae.should_deliver(dict(OBS), _state(daily_count=ae.MAX_DAILY_PROACTIVE), "available"))
        self.assertFalse(ae.should_deliver(dict(OBS), _state(topic_cooldowns={"disk:/": time.time() - 60}), "available"))
        st = _state(topic_cooldowns={"desk_duration": time.time() - 2 * 3600})
        self.assertFalse(ae.should_deliver({"priority": 3, "topic": "desk_duration"}, st, "available"))   # 3h override

    def test_desk_minutes_from_idle(self):
        st = {}
        with patch.object(ae, "_mac_idle_seconds", return_value=30):
            self.assertLess(ae.get_desk_active_minutes(st), 1.0)
            st["desk_active_since"] = time.time() - 250 * 60
            self.assertEqual(ae.check_desk_duration(st)[0]["topic"], "desk_duration")
        with patch.object(ae, "_mac_idle_seconds", return_value=ae.IDLE_RESET_SECS):
            self.assertEqual(ae.get_desk_active_minutes(st), 0.0)
        self.assertIsNone(st["desk_active_since"])

    def test_disk_parse_and_infra_threshold(self):
        df = "Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/a 1 1 1 91% /\n/dev/b 1 1 1 40% /Volumes/Data\n"
        with patch.object(ae.subprocess, "run", return_value=types.SimpleNamespace(stdout=df)):
            obs = ae.check_infrastructure()
        self.assertEqual([(o["topic"], o["priority"]) for o in obs], [("disk:/", 2)])

    def test_activity_time_fallbacks(self):
        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 1, 5, 3, 0)
        with patch.object(ae, "datetime", FakeDT):
            self.assertEqual(ae.get_activity_state(), "sleeping")


class TestIntegration(unittest.TestCase):
    def test_meeting_prep_uses_memory_recall_and_expires_at_start(self):
        start = (datetime.now() + timedelta(minutes=10)).isoformat()
        r = MagicMock(); r.read.return_value = json.dumps({"results": [{"text": "last sync notes"}]}).encode()
        with patch.object(ae, "get_upcoming_meetings", return_value=[{"title": "SRE sync", "start": start}]), \
                patch.object(ae.urllib.request, "urlopen", return_value=r) as u:
            obs = ae.check_meeting_prep()
        self.assertTrue(u.call_args.args[0].full_url.endswith("/recall"))
        self.assertIn("last sync notes", obs[0]["message"])
        self.assertAlmostEqual(obs[0]["expires_at"], datetime.fromisoformat(start).timestamp(), delta=1)

    def test_autonomy_state_uses_curated_line(self):
        autos = types.ModuleType("nova_autonomy_safety")
        autos.autonomy_status = lambda cur: {"line": "3 classes earned."}
        cur = MagicMock(); cur.fetchone.side_effect = [(1, "ha-core"), None]
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.dict(sys.modules, {"nova_autonomy_safety": autos}), patch.object(psycopg2, "connect", return_value=conn):
            obs = ae.check_autonomy_state()
        self.assertEqual(obs[0]["topic"], "autonomy_selfheal")
        self.assertIn("self-healed ha-core", obs[0]["message"])
        self.assertTrue(obs[0]["message"].endswith("3 classes earned."))


class TestFunctional(unittest.TestCase):
    def test_evaluate_delivers_queues_and_persists(self):
        st_file = Path(_TD.name) / "ant_state.json"
        hi = dict(OBS); lo = {"type": "environment", "priority": 4, "message": "lights", "topic": "lights"}
        held = {"type": "meeting_prep", "priority": 2, "message": "old", "topic": "m", "queued_at": time.time(),
                "expires_at": time.time() - 5}
        st_file.write_text(json.dumps(_state(hold_queue=[held])))
        with patch.object(ae, "STATE_FILE", st_file), patch.object(ae, "get_activity_state", return_value="planning"), \
                patch.object(ae, "check_meeting_prep", return_value=[]), patch.object(ae, "check_desk_duration", return_value=[]), \
                patch.object(ae, "check_infrastructure", return_value=[hi]), patch.object(ae, "check_environment", return_value=[lo]), \
                patch.object(ae, "check_autonomy_state", return_value=[]), patch.object(ae.nova_config, "post_both") as post:
            ae.evaluate()
        self.assertEqual(post.call_count, 1)                          # priority 2 <= planning's 3; lights (4) dropped
        saved = json.loads(st_file.read_text())
        self.assertEqual(saved["daily_count"], 1)
        self.assertIn("disk:/", saved["topic_cooldowns"])
        self.assertEqual(len(saved["hold_queue"]), 1)                 # planning never flushes the queue

    def test_flush_drops_expired_and_stale(self):
        now = time.time()
        st = _state(hold_queue=[{**OBS, "topic": "a", "queued_at": now - 20000},
                                {**OBS, "topic": "b", "queued_at": now, "expires_at": now - 1},
                                {**OBS, "topic": "c", "queued_at": now}])
        with patch.object(ae.nova_config, "post_both") as post:
            ae.flush_hold_queue(st, "available")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(st["hold_queue"], [])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as home:
            r = _REAL_RUN([sys.executable, "-c", "import nova_anticipation_engine"], cwd=str(SCRIPTS),
                          capture_output=True, text=True, timeout=30,
                          env={**os.environ, "HOME": home, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)
        self.assertNotIn("Anticipation engine starting", r.stderr)


if __name__ == "__main__":
    unittest.main()
