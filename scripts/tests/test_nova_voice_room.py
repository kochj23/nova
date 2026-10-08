"""Tests for nova_voice_room.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
SRC = SCRIPTS / "nova_voice_room.py"
_spec = importlib.util.spec_from_file_location("nova_voice_room", SRC)
vr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vr)
vr.LOG = Path(os.devnull)  # never touch ~/.openclaw/logs

SMOKE = {"id": 1, "source": "ha", "level": "critical", "category": "smoke", "title": "Smoke: hallway"}
ORGAN = {"id": 2, "source": "nova_security_organ", "level": "critical", "category": "security",
         "title": "NEW DEVICE on the network"}


class TestSecurity(unittest.TestCase):
    def test_no_secrets_or_user_paths(self):
        src = SRC.read_text()
        self.assertNotRegex(src, r"(?i)(password|token|api_key)\s*=\s*['\"][^'\"]{6,}")
        self.assertNotIn("/Users/", src)

    def test_sql_parameterized(self):
        src = SRC.read_text()
        self.assertFalse(re.search(r"execute\(f[\"']", src))

    def test_test_alerts_and_noise_never_speak(self):
        self.assertIsNone(vr.classify(dict(ORGAN, title="[TEST] NEW DEVICE")))
        self.assertIsNone(vr.classify({"source": "nova_traffic_watch.py", "level": "critical",
                                       "category": "traffic_watch", "title": "SMOKE — US-101"}))


class TestPerformance(unittest.TestCase):
    def test_classify_10k_fast(self):
        evs = [SMOKE, ORGAN, {"level": "critical", "category": "backup", "title": "x"}] * 3400
        t = time.time()
        for e in evs:
            vr.classify(e)
        self.assertLess(time.time() - t, 1.0)


class TestRetry(unittest.TestCase):
    def test_play_retries_then_succeeds(self):
        calls = {"n": 0}

        def fake_run(coro):
            coro.close()
            calls["n"] += 1
            if calls["n"] < 2:
                raise OSError("unreachable")
            return "ok"
        with mock.patch.object(vr.asyncio, "run", fake_run), mock.patch.object(vr.time, "sleep"):
            self.assertEqual(vr.play(Path("x.wav"), vr.DEFAULT_SPEAKER, 20), "ok")
        self.assertEqual(calls["n"], 2)

    def test_state_read_fails_open(self):
        with mock.patch.object(vr, "_conn", side_effect=Exception("pg down")):
            enabled, speaker, same, hour = vr.load_state("k", "smoke")
        self.assertTrue(enabled)
        self.assertEqual(speaker["name"], "OfficePod")


class TestUnit(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(vr.classify(SMOKE), ("smoke", True))
        self.assertEqual(vr.classify(dict(SMOKE, category="carbon_monoxide")), ("co", True))
        self.assertEqual(vr.classify({"level": "info", "meta": '{"voice": "water_leak"}'}), ("water_leak", True))
        self.assertEqual(vr.classify(ORGAN), ("intrusion", False))
        self.assertIsNone(vr.classify(dict(SMOKE, level="info")))
        self.assertIsNone(vr.classify({}))

    def test_gate(self):
        self.assertEqual(vr.gate("smoke", True, 12, False, False, 0), "disabled")
        self.assertIsNone(vr.gate("smoke", True, 3, True, False, 99))           # life-safety at 3am
        self.assertEqual(vr.gate("intrusion", False, 23, True, False, 0), "quiet_hours")
        self.assertEqual(vr.gate("intrusion", False, 6, True, False, 0), "quiet_hours")
        self.assertEqual(vr.gate("intrusion", False, 12, True, True, 0), "dedup")
        self.assertEqual(vr.gate("intrusion", False, 12, True, False, 4), "rate_limited")
        self.assertIsNone(vr.gate("intrusion", False, 7, True, False, 3))

    def test_every_sentence_addresses_little_mister(self):
        for text in vr.SENTENCES.values():
            self.assertTrue(text.startswith("Little Mister,"))
            self.assertLess(len(text), 110)

    def test_clip_ok_rejects_babble(self):
        norm = lambda s: re.sub(r"[^a-z ]", " ", s.lower()).split()
        wer = lambda r, h: 0.0 if norm(r) == norm(h) else 0.5
        t = vr.SENTENCES["smoke"]
        self.assertTrue(vr.clip_ok(t, t, wer, norm))
        self.assertFalse(vr.clip_ok(t, t + " Gile.", wer, norm))


class TestIntegration(unittest.TestCase):
    def test_uses_service_config_kill_switch_and_log_table(self):
        src = SRC.read_text()
        self.assertIn("service='nova_voice_room'", src)
        self.assertIn("nova_voice_room_log", src)

    def test_notifier_calls_dispatch_before_correlate(self):
        n = (SCRIPTS / "nova_notifier.py").read_text()
        self.assertLess(n.index("nova_voice_room.dispatch"), n.index("nova_correlator.correlate("))


class TestFunctional(unittest.TestCase):
    def test_speak_golden_path(self):
        with mock.patch.object(vr, "load_state", return_value=(True, vr.DEFAULT_SPEAKER, False, 0)), \
             mock.patch.object(vr, "render", return_value=(Path("c.wav"), "xtts")), \
             mock.patch.object(vr, "play", return_value="OfficePod played") as p, \
             mock.patch.object(vr, "record") as rec, mock.patch.object(vr, "LOG", Path(os.devnull)):
            self.assertEqual(vr.speak("smoke", True, event_id=1, dedup_key="k", hour=3), "spoken")
        self.assertEqual(p.call_args[0][2], vr.VOLUME["life_safety"])
        self.assertEqual(rec.call_args[0][6], "spoken")

    def test_speak_kill_switch_records_skip(self):
        with mock.patch.object(vr, "load_state", return_value=(False, vr.DEFAULT_SPEAKER, False, 0)), \
             mock.patch.object(vr, "play") as p, mock.patch.object(vr, "record") as rec, \
             mock.patch.object(vr, "LOG", Path(os.devnull)):
            self.assertEqual(vr.speak("smoke", True, hour=12), "disabled")
        p.assert_not_called()
        self.assertEqual(rec.call_args[0][6], "skipped:disabled")

    def test_dispatch_ignores_noise_and_spawns_for_smoke(self):
        with mock.patch.object(vr.subprocess, "Popen") as po, mock.patch.object(vr, "LOG", Path(os.devnull)):
            self.assertFalse(vr.dispatch({"id": 9, "level": "critical", "category": "backup"}))
            po.assert_not_called()
            self.assertTrue(vr.dispatch(SMOKE))
            self.assertIn("--event", po.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SRC), "--help"], capture_output=True, timeout=30,
                           env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0)
        self.assertIn("--test", r.stdout.decode())


if __name__ == "__main__":
    unittest.main()
