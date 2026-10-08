"""nova_voice_room.py — 7-category supplement (Security, Performance, Retry, Unit, Integration,
Functional, Frame). Nothing here can make a sound: pyatv, `say`, Popen and PG are all mocked.
Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import os
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
SRC = SCRIPTS / "nova_voice_room.py"
_spec = importlib.util.spec_from_file_location("nova_voice_room_7cat", SRC)
vr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vr)
vr.LOG = Path(os.devnull)

SMOKE = {"id": 7, "source": "ha", "level": "critical", "category": "smoke", "title": "Smoke", "dedup_key": "s"}


def _fake_pyatv(devs_by_host, devs_mcast, stream_exc=None):
    atv = mock.MagicMock()
    atv.audio.set_volume = mock.AsyncMock()
    atv.stream.stream_file = mock.AsyncMock(side_effect=stream_exc)
    calls = []

    async def scan(loop, hosts=None, timeout=5):
        calls.append(hosts)
        return devs_by_host if hosts else devs_mcast

    async def connect(dev, loop):
        return atv
    return types.SimpleNamespace(scan=scan, connect=connect), atv, calls


def _dev(name, addr="192.168.1.91"):
    return types.SimpleNamespace(name=name, address=addr)


class _Cur:
    def __init__(self, rows=None, one=None):
        self.rows, self.one, self.sql = rows or [], one, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.one() if callable(self.one) else self.one


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur


class TestSecurity(unittest.TestCase):
    def test_dispatch_passes_only_int_id_no_shell(self):
        with mock.patch.object(vr.subprocess, "Popen") as po:
            vr.dispatch(dict(SMOKE, id=42))
        argv, kw = po.call_args[0][0], po.call_args[1]
        self.assertIsInstance(argv, list)
        self.assertNotIn("shell", kw)
        self.assertEqual(argv[-2:], ["--event", "42"])
        self.assertTrue(kw["start_new_session"])

    def test_meta_voice_injection_cannot_pick_arbitrary_sentence(self):
        for bad in ("intrusion", "test", "'; DROP TABLE x;--", "../../etc/passwd", 123):
            self.assertIsNone(vr.classify({"id": 1, "level": "info", "meta": {"voice": bad}}))
        self.assertIsNone(vr.classify({"id": 1, "level": "critical", "meta": "{not json"}))

    def test_test_titled_security_and_noise_stay_silent(self):
        self.assertIsNone(vr.classify({"source": "nova_security_organ", "level": "critical",
                                       "category": "security", "title": "[TEST] new device"}))
        self.assertIsNone(vr.classify({"source": "traffic_watch", "level": "critical", "category": "traffic",
                                       "title": "SMOKE"}))

    def test_clip_name_is_hash_inside_cache(self):
        p = vr.clip_path("../../evil")
        self.assertEqual(p.parent, vr.CACHE)
        self.assertRegex(p.name, r"^[0-9a-f]{16}\.wav$")

    def test_say_text_after_double_dash(self):
        with mock.patch.object(vr.subprocess, "run") as run, mock.patch.object(vr, "clip_path",
                                                                                return_value=Path("/nonexistent.wav")):
            vr.render("-v Evil")
        argv = run.call_args[0][0]
        self.assertEqual(argv[-2:], ["--", "-v Evil"])


class TestPerformance(unittest.TestCase):
    def test_gate_and_classify_100k_under_1s(self):
        t = time.time()
        for i in range(50_000):
            vr.gate("smoke", True, i % 24, True, False, i % 6)
            vr.classify({"level": "warning", "category": "leak" if i % 2 else "storage"})
        self.assertLess(time.time() - t, 1.0)

    def test_dispatch_does_not_block_on_noise(self):
        with mock.patch.object(vr.subprocess, "Popen") as po:
            t = time.time()
            for _ in range(10_000):
                vr.dispatch({"id": 1, "level": "info", "category": "backup"})
        po.assert_not_called()
        self.assertLess(time.time() - t, 0.5)

    def test_play_is_bounded_by_wait_for(self):
        self.assertIn("asyncio.wait_for(_play(path, speaker, volume), 60)", SRC.read_text())


class TestRetry(unittest.TestCase):
    def test_event_read_retries_then_succeeds(self):
        n = {"i": 0}

        def conn():
            n["i"] += 1
            if n["i"] < 3:
                raise RuntimeError("pg blip")
            return _Conn(_Cur(one=dict(SMOKE)))
        with mock.patch.object(vr, "_conn", side_effect=conn), mock.patch.object(vr.time, "sleep") as sl:
            self.assertEqual(vr._event(7)["id"], 7)
        self.assertEqual(n["i"], 3)
        self.assertEqual(sl.call_count, 2)

    def test_event_read_raises_after_three(self):
        with mock.patch.object(vr, "_conn", side_effect=RuntimeError("down")) as c, \
             mock.patch.object(vr.time, "sleep"):
            with self.assertRaises(RuntimeError):
                vr._event(7)
        self.assertEqual(c.call_count, 3)

    def test_play_gives_up_loudly_after_retries(self):
        with mock.patch.object(vr, "_play", side_effect=lambda *a: (_ for _ in ()).throw(OSError("x"))), \
             mock.patch.object(vr.asyncio, "run", side_effect=OSError("no route")) as run, \
             mock.patch.object(vr.time, "sleep"):
            with self.assertRaises(RuntimeError):
                vr.play(Path("a.wav"), vr.DEFAULT_SPEAKER, 20)
        self.assertEqual(run.call_count, 2)

    def test_record_failure_never_raises(self):
        with mock.patch.object(vr, "_conn", side_effect=RuntimeError("down")):
            vr.record(1, "k", "smoke", True, "t", "say", "spoken")

    def test_dispatch_never_raises(self):
        with mock.patch.object(vr.subprocess, "Popen", side_effect=OSError("fork failed")):
            self.assertFalse(vr.dispatch(SMOKE))
        self.assertFalse(vr.dispatch({"level": "critical", "category": "smoke"}))  # no id -> KeyError swallowed


class TestUnit(unittest.TestCase):
    def test_quiet_hours_boundaries(self):
        self.assertTrue(vr.in_quiet_hours(22))
        self.assertTrue(vr.in_quiet_hours(6))
        self.assertFalse(vr.in_quiet_hours(7))
        self.assertFalse(vr.in_quiet_hours(21))

    def test_gate_order(self):
        self.assertEqual(vr.gate("test", False, 3, True, True, 99), None)       # test bypasses gates
        self.assertEqual(vr.gate("intrusion", False, 12, True, False, 4), "rate_limited")
        self.assertEqual(vr.gate("smoke", True, 12, True, False, 99), None)     # life-safety not rate limited
        self.assertEqual(vr.gate("smoke", True, 12, True, True, 0), "dedup")

    def test_classify_aliases(self):
        for cat, kind in (("carbon_monoxide", "co"), ("flood", "water_leak"), ("leak", "water_leak")):
            self.assertEqual(vr.classify({"level": "warning", "category": cat}), (kind, True))
        self.assertIsNone(vr.classify({"level": "info", "category": "smoke"}))

    def test_render_prefers_cached_clip(self):
        with mock.patch.object(vr, "CACHE", Path(os.environ.get("TMPDIR", "/tmp"))):
            p = vr.clip_path("hello")
            p.write_bytes(b"0" * 2000)
            try:
                with mock.patch.object(vr.subprocess, "run") as run:
                    self.assertEqual(vr.render("hello"), (p, "xtts"))
                run.assert_not_called()
            finally:
                p.unlink()


class TestIntegration(unittest.TestCase):
    def test_load_state_reads_switch_speaker_and_counters(self):
        counts = iter([{"n": 1}, {"n": 3}])
        cur = _Cur(rows=[{"key": "enabled", "value": False},
                         {"key": "speaker", "value": {"name": "Den", "host": "10.0.0.9"}}],
                   one=lambda: next(counts))
        with mock.patch.object(vr, "_conn", return_value=_Conn(cur)):
            enabled, spk, same, hour = vr.load_state("k", "smoke")
        self.assertEqual((enabled, spk["name"], same, hour), (False, "Den", True, 3))
        self.assertTrue(all("%s" in s or p is None for s, p in cur.sql))

    def test_play_scans_host_then_falls_back_to_multicast(self):
        fake, atv, calls = _fake_pyatv([], [_dev("OfficePod", "192.168.1.50")])
        with mock.patch.dict(sys.modules, {"pyatv": fake}):
            out = asyncio.run(vr._play(Path("x.wav"), vr.DEFAULT_SPEAKER, 33))
        self.assertEqual(calls, [["192.168.1.91"], None])
        atv.audio.set_volume.assert_awaited_with(33)
        atv.close.assert_called_once()
        self.assertIn("192.168.1.50", out)

    def test_play_closes_connection_on_stream_error(self):
        fake, atv, _ = _fake_pyatv([_dev("OfficePod")], [], stream_exc=OSError("broken pipe"))
        with mock.patch.dict(sys.modules, {"pyatv": fake}):
            with self.assertRaises(OSError):
                asyncio.run(vr._play(Path("x.wav"), vr.DEFAULT_SPEAKER, 20))
        atv.close.assert_called_once()

    def test_speaker_not_found_raises(self):
        fake, _, _ = _fake_pyatv([_dev("Kitchen")], [_dev("Bedroom")])
        with mock.patch.dict(sys.modules, {"pyatv": fake}):
            with self.assertRaises(RuntimeError):
                asyncio.run(vr._play(Path("x.wav"), vr.DEFAULT_SPEAKER, 20))


class TestFunctional(unittest.TestCase):
    def test_main_event_speaks_smoke(self):
        with mock.patch.object(vr, "_event", return_value=dict(SMOKE)), \
             mock.patch.object(vr, "speak", return_value="spoken") as sp:
            self.assertEqual(vr.main(["--event", "7"]), 0)
        self.assertEqual(sp.call_args[0][:2], ("smoke", True))
        self.assertEqual(sp.call_args[1]["dedup_key"], "s")

    def test_main_missing_event_returns_1(self):
        with mock.patch.object(vr, "_event", return_value=None), mock.patch.object(vr, "speak") as sp:
            self.assertEqual(vr.main(["--event", "7"]), 1)
        sp.assert_not_called()

    def test_main_classify_is_dry_run(self):
        with mock.patch.object(vr, "_event", return_value=dict(SMOKE)), mock.patch.object(vr, "speak") as sp, \
             mock.patch("builtins.print") as pr:
            self.assertEqual(vr.main(["--classify", "7"]), 0)
        sp.assert_not_called()
        self.assertIn('"smoke"', pr.call_args[0][0])

    def test_speak_error_path_records_error(self):
        with mock.patch.object(vr, "load_state", return_value=(True, vr.DEFAULT_SPEAKER, False, 0)), \
             mock.patch.object(vr, "render", return_value=(Path("c.wav"), "say")), \
             mock.patch.object(vr, "play", side_effect=RuntimeError("play failed")), \
             mock.patch.object(vr, "record") as rec:
            self.assertEqual(vr.speak("smoke", True, event_id=7, hour=12), "error")
        self.assertEqual(rec.call_args[0][6], "error")
        self.assertEqual(rec.call_args[0][5], "say")

    def test_quiet_hours_security_is_silent(self):
        with mock.patch.object(vr, "load_state", return_value=(True, vr.DEFAULT_SPEAKER, False, 0)), \
             mock.patch.object(vr, "play") as p, mock.patch.object(vr, "record"):
            self.assertEqual(vr.speak("intrusion", False, hour=23), "quiet_hours")
        p.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_has_no_side_effects(self):
        self.assertTrue(callable(vr.dispatch) and callable(vr.main))
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC.read_text())

    def test_cli_requires_a_mode(self):
        r = subprocess.run([sys.executable, str(SRC)], capture_output=True, timeout=30,
                           env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
