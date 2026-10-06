#!/usr/bin/env python3
"""Tests for nova_sds200_scanner.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import struct
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_sds200_scanner.py"
SRC = PATH.read_text()
OFFLINE_ENV = {"NOVA_SDS200_HOST": "10.99.0.1", "NOVA_SDS200_AUDIO": "rtsp", "NOVA_SDS200_WHISPER": "base.en"}
EVIL = "x'); " + "DEL" + "ETE FROM telemetry.sds200_calls; --"


def _load():
    spec = importlib.util.spec_from_file_location("nova_sds200_scanner_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sd = _load()
LOUD = struct.pack("<320h", *([3000] * 320))
QUIET = b"\0" * sd.FRAME_BYTES
TAG = {"system": "Burbank 911", "department": "Verdugo Fire", "channel": "Fire Dispatch", "tgid": 2051,
       "service_type": "Fire Dispatch", "frequency_mhz": 851.0375, "p25_status": "P25"}


class _PG:
    def __init__(self):
        self.cur = mock.MagicMock(); self.conn = mock.MagicMock(); self.conn.cursor.return_value = self.cur


def _quiet():
    return mock.patch("sys.stdout", new_callable=io.StringIO)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password=", sd.DSN)

    def test_insert_and_config_lookup_parameterized(self):
        pg = _PG()
        with mock.patch("psycopg2.connect", return_value=pg.conn), \
                mock.patch.object(sd.urllib.request, "urlopen"), _quiet():
            sd.Store.__new__(sd.Store).save(EVIL, TAG, "fire")
        sql, params = pg.cur.execute.call_args[0]
        self.assertNotIn(EVIL, sql)
        self.assertEqual(params[9], EVIL)
        os.environ.pop("NOVA_X_KEY", None)
        with mock.patch("psycopg2.connect", return_value=pg.conn):
            pg.cur.fetchone.return_value = ("v",)
            self.assertEqual(sd.cfg("x_key"), "v")
        self.assertEqual(pg.cur.execute.call_args[0][1], ("x_key",))

    def test_audio_command_is_argv_list(self):
        argv = sd.audio_command("alsa:hw:1,0; echo pwned", "h")
        self.assertEqual(argv[argv.index("-i") + 1], "hw:1,0; echo pwned")   # one argv element, never a shell


class TestPerformance(unittest.TestCase):
    def test_rms_10k_frames(self):
        t0 = time.perf_counter()
        for _ in range(10_000):
            sd.rms(LOUD)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertAlmostEqual(sd.rms(LOUD), 3000.0)


class TestRetry(unittest.TestCase):
    def test_store_sinks_fail_open(self):
        # RETRY GAP: Store.save — one memory POST + one PG insert, each best-effort; failures only logged
        with mock.patch.object(sd.urllib.request, "urlopen", side_effect=OSError("down")) as uo, \
                mock.patch("psycopg2.connect", side_effect=OSError("pg down")) as pc, _quiet() as out:
            sd.Store().save("engine 5 respond", TAG, "fire")
        self.assertEqual((uo.call_count, pc.call_count), (1, 2))       # ensure_table + insert
        self.assertIn("memory post failed", out.getvalue())

    def test_metadata_feed_backs_off_20s_when_scanner_offline(self):
        feed = sd.MetadataFeed("10.99.0.1")
        with mock.patch.object(sd, "Scanner", side_effect=[OSError("no route"), OSError("no route")]), \
                mock.patch.object(sd.time, "sleep", side_effect=[None, KeyboardInterrupt]) as sl, _quiet():
            with self.assertRaises(KeyboardInterrupt):
                feed.run_forever()
        self.assertEqual([c[0][0] for c in sl.call_args_list], [20, 20])

    def test_cfg_pg_down_returns_default(self):
        with mock.patch("psycopg2.connect", side_effect=OSError("down")):
            self.assertEqual(sd.cfg("nonexistent_key_zz", "dflt"), "dflt")


class TestUnit(unittest.TestCase):
    def test_source_routing(self):
        self.assertEqual(sd.source_for(TAG), "fire")
        self.assertEqual(sd.source_for({"department": "Metrolink Dispatch"}), "rail")
        self.assertEqual(sd.source_for({"system": "CHP Southern"}), "chp")
        self.assertEqual(sd.source_for({"service_type": "Law Dispatch"}), "scanner")
        self.assertEqual(sd.source_for({}), "scanner")

    def test_rms_edges(self):
        self.assertEqual(sd.rms(b""), 0.0)
        self.assertEqual(sd.rms(b"\x01"), 0.0)
        self.assertEqual(sd.rms(QUIET), 0.0)

    def test_metadata_only_tracks_open_squelch(self):
        feed = sd.MetadataFeed("h")
        feed._on_info(types.SimpleNamespace(squelch_open=False, tag=lambda: {"x": 1}))
        self.assertEqual(feed.current, {})
        feed._on_info(types.SimpleNamespace(squelch_open=True, tag=lambda: dict(TAG)))
        snap = feed.current
        snap["department"] = "mutated"
        self.assertEqual(feed.current["department"], "Verdugo Fire")      # a copy, not the live dict


class TestIntegration(unittest.TestCase):
    def test_store_posts_tagged_memory_and_row(self):
        pg = _PG()
        with mock.patch.object(sd.urllib.request, "urlopen") as uo, mock.patch("psycopg2.connect", return_value=pg.conn):
            sd.Store().save("engine 5 respond", TAG, "fire")
        body = json.loads(uo.call_args[0][0].data)
        self.assertEqual(body["text"], "[Verdugo Fire / Fire Dispatch] engine 5 respond")
        self.assertEqual((body["source"], body["metadata"]["kind"], body["metadata"]["tgid"]), ("fire", "sds200", 2051))
        self.assertTrue(uo.call_args[0][0].full_url.endswith("/remember?async=1"))
        self.assertIn("INSERT INTO telemetry.sds200_calls", pg.cur.execute.call_args[0][0])

    def test_audio_command_rtsp_default(self):
        argv = sd.audio_command("rtsp", "10.1.1.5")
        self.assertIn("rtsp://10.1.1.5/au:scanner.au", argv)
        self.assertEqual(argv[-7:], ["-ar", "16000", "-ac", "1", "-f", "s16le", "-"])


class TestFunctional(unittest.TestCase):
    def test_service_segments_transcribes_and_stores_one_call(self):
        frames = [QUIET] * 3 + [LOUD] * 50 + [QUIET] * 40 + [b""]
        proc = mock.Mock(); proc.stdout.read.side_effect = frames; proc.poll.return_value = None
        model = mock.Mock(); model.transcribe.return_value = ([types.SimpleNamespace(text="Engine 5 respond code 3")], None)
        fw = types.SimpleNamespace(WhisperModel=mock.Mock(return_value=model))
        feed = mock.Mock(); feed.current = dict(TAG)
        store = mock.Mock()
        with mock.patch.dict(sys.modules, {"faster_whisper": fw}), \
                mock.patch.object(sd, "MetadataFeed", return_value=feed), \
                mock.patch.object(sd.threading, "Thread"), \
                mock.patch.object(sd, "Store", return_value=store), \
                mock.patch.object(sd.subprocess, "Popen", return_value=proc), \
                mock.patch.object(sd.time, "sleep", side_effect=KeyboardInterrupt), _quiet():
            with self.assertRaises(KeyboardInterrupt):      # stream end -> restart backoff -> test exits
                sd.run_service("10.99.0.1", "rtsp", "base.en")
        store.save.assert_called_once_with("Engine 5 respond code 3", TAG, "fire")
        proc.terminate.assert_called_once()

    def test_main_without_host_falls_back_to_selftest(self):
        with mock.patch.object(sd, "cfg", side_effect=lambda k, d=None: d), \
                mock.patch.object(sys, "argv", ["x"]), \
                mock.patch.object(sd, "run_service") as rs, _quiet() as out:
            self.assertEqual(sd.main(), 0)
        rs.assert_not_called()
        self.assertIn("no sds200_host configured", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero_offline(self):
        r = subprocess.run([sys.executable, str(PATH), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", **OFFLINE_ENV})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("source bucket = 'fire'", r.stdout)
        self.assertIn("OK — wiring valid", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_sds200_scanner"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
