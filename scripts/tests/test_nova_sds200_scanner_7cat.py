#!/usr/bin/env python3
"""7-category tests for the 2026-10-08 nova_sds200_scanner fix (telemetry.sds200_calls empty): reachability
probe before RTSP, exponential backoff instead of a 10/20 s hammer, empty MDL treated as offline, up/down
published to health_checks, every sink (memory POST, PG insert, health row) retried 3x with backoff, RTSP
transport alternates udp/tcp after a failure. Security, Performance, Retry, Unit, Integration, Functional,
Frame. No hardware, no production writes (psycopg2 mocked). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import socket
import struct
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_sds200_scanner.py"
SRC = PATH.read_text()
_spec = importlib.util.spec_from_file_location("sds7", PATH)
sd = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(sd)

LOUD = struct.pack("<320h", *([3000] * 320))
QUIET = b"\0" * sd.FRAME_BYTES
TAG = {"system": "Burbank 911", "department": "Verdugo Fire", "channel": "Fire Dispatch", "tgid": 2051,
       "service_type": "Fire Dispatch", "frequency_mhz": 851.0375, "p25_status": "P25"}


def _q():
    return redirect_stdout(io.StringIO())


def _pg():
    stmts = []
    cur = mock.MagicMock(); cur.execute.side_effect = lambda s, p=None: stmts.append((s, p))
    conn = mock.MagicMock(); conn.cursor.return_value = cur
    return conn, stmts


class TestSecurity(unittest.TestCase):
    def test_health_and_insert_sql_parameterized(self):
        conn, stmts = _pg()
        with mock.patch("psycopg2.connect", return_value=conn), _q():
            sd.Health("10.99.0.1").report("rtsp", "down", "x'); DROP TABLE health_checks; --")
        sql, params = stmts[0]
        self.assertNotIn("DROP", sql)
        self.assertIn("DROP", params[4])

    def test_audio_transport_whitelisted(self):
        argv = sd.audio_command("rtsp", "10.1.1.5", transport="udp; rm -rf /")
        self.assertEqual(argv[argv.index("-rtsp_transport") + 1], "udp")
        self.assertIsInstance(argv, list)

    def test_no_user_paths_or_secrets(self):
        self.assertNotRegex(SRC, r"/Users/[a-z]+/")
        self.assertNotRegex(SRC.lower(), r"(password|api_key)\s*=\s*['\"]")


class TestPerformance(unittest.TestCase):
    def test_backoff_caps_and_is_cheap(self):
        t = time.perf_counter()
        vals = [sd.backoff_s(n) for n in range(10000)]
        self.assertLess(time.perf_counter() - t, 0.5)
        self.assertEqual(max(vals), sd.BACKOFF_MAX_S)

    def test_health_rows_rate_limited_when_state_steady(self):
        conn, stmts = _pg()
        h = sd.Health("10.99.0.1")
        with mock.patch("psycopg2.connect", return_value=conn), _q():
            for _ in range(500):
                h.report("rtsp", "down", "host down")
        self.assertEqual(len(stmts), 1)                       # one row, not 500

    def test_probe_timeout_bounded(self):
        self.assertLessEqual(sd.PROBE_TIMEOUT_S, 5)


class TestRetry(unittest.TestCase):
    def test_store_retries_pg_insert_then_succeeds(self):
        conn, stmts = _pg()
        with mock.patch("psycopg2.connect", side_effect=[conn, OSError("a"), OSError("b"), conn]), \
                mock.patch.object(sd.urllib.request, "urlopen"), mock.patch.object(sd.time, "sleep") as sl, _q():
            sd.Store().save("engine 5", TAG, "fire")
        self.assertTrue(any("sds200_calls" in s and "INSERT" in s for s, _ in stmts))
        self.assertEqual([c.args[0] for c in sl.call_args_list], [0.5, 1.0])

    def test_memory_post_retried_3x_then_logged(self):
        with mock.patch.object(sd.urllib.request, "urlopen", side_effect=OSError("down")) as uo, \
                mock.patch("psycopg2.connect", side_effect=OSError("pg")), mock.patch.object(sd.time, "sleep"), \
                _q() as out:
            sd.Store().save("engine 5", TAG, "fire")
        self.assertEqual(uo.call_count, 3)
        self.assertIn("memory post failed after retries", out.getvalue())
        self.assertIn("PG insert failed after retries", out.getvalue())

    def test_health_write_retried_and_logged(self):
        with mock.patch("psycopg2.connect", side_effect=OSError("pg")) as pc, \
                mock.patch.object(sd.time, "sleep"), _q() as out:
            sd.Health("h").report("rtsp", "down")
        self.assertEqual(pc.call_count, 3)
        self.assertIn("health write failed", out.getvalue())

    def test_metadata_backoff_grows(self):
        feed = sd.MetadataFeed("10.99.0.1")
        waits = []

        def sleep(s):
            waits.append(s)
            if len(waits) == 5:
                raise KeyboardInterrupt
        with mock.patch.object(sd, "Scanner", side_effect=OSError("no route")), _q():
            with self.assertRaises(KeyboardInterrupt):
                feed.run_forever(sleep=sleep)
        self.assertEqual(waits, [10, 20, 40, 80, 160])


class TestUnit(unittest.TestCase):
    def test_backoff_sequence(self):
        self.assertEqual([sd.backoff_s(n) for n in range(7)], [0, 10, 20, 40, 80, 160, 300])

    def test_empty_model_is_offline(self):
        sc = mock.MagicMock(); sc.connect.return_value = sc; sc.model.return_value = ""
        with mock.patch.object(sd, "Scanner", return_value=sc):
            with self.assertRaises(ConnectionError):
                sd.MetadataFeed("h").run_once()
        sc.stream.assert_not_called()

    def test_probe_tcp_states(self):
        with mock.patch.object(socket, "create_connection", side_effect=ConnectionRefusedError()):
            self.assertEqual(sd.probe_tcp("h"), "refused")
        with mock.patch.object(socket, "create_connection", side_effect=OSError(64, "Host is down")):
            self.assertEqual(sd.probe_tcp("h"), "down")
        cm = mock.MagicMock()
        with mock.patch.object(socket, "create_connection", return_value=cm):
            self.assertEqual(sd.probe_tcp("h"), "up")

    def test_tcp_transport_argv(self):
        argv = sd.audio_command("rtsp", "10.1.1.5", "tcp")
        self.assertEqual(argv[argv.index("-rtsp_transport") + 1], "tcp")


class TestIntegration(unittest.TestCase):
    def test_health_row_shape(self):
        conn, stmts = _pg()
        with mock.patch("psycopg2.connect", return_value=conn), _q():
            sd.Health("192.0.2.1").report("rtsp", "down", "192.0.2.1:554 down")
        sql, p = stmts[0]
        self.assertIn("INSERT INTO health_checks", sql)
        self.assertEqual(p[:4], ("sds200", "192.0.2.1", "nova_sds200_scanner", "down"))
        conn.commit.assert_called_once(); conn.close.assert_called_once()

    def test_state_change_writes_immediately(self):
        conn, stmts = _pg()
        h = sd.Health("h")
        with mock.patch("psycopg2.connect", return_value=conn), _q():
            h.report("rtsp", "down"); h.report("rtsp", "up")
        self.assertEqual([p[3] for _, p in stmts], ["down", "up"])


class TestFunctional(unittest.TestCase):
    def _run(self, probe, frames=None):
        proc = mock.Mock(); proc.stdout.read.side_effect = frames or [b""]; proc.poll.return_value = None
        model = mock.Mock(); model.transcribe.return_value = ([types.SimpleNamespace(text="Engine 5 respond code 3")], None)
        fw = types.SimpleNamespace(WhisperModel=mock.Mock(return_value=model))
        feed = mock.Mock(); feed.current = dict(TAG)
        store, health = mock.Mock(), mock.Mock()
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            if len(sleeps) >= 3:
                raise KeyboardInterrupt
        with mock.patch.dict(sys.modules, {"faster_whisper": fw}), \
                mock.patch.object(sd, "MetadataFeed", return_value=feed), mock.patch.object(sd.threading, "Thread"), \
                mock.patch.object(sd, "Store", return_value=store), mock.patch.object(sd, "Health", return_value=health), \
                mock.patch.object(sd, "probe_tcp", side_effect=probe), \
                mock.patch.object(sd.subprocess, "Popen", return_value=proc) as po, \
                mock.patch.object(sd.time, "sleep", side_effect=sleep), _q():
            with self.assertRaises(KeyboardInterrupt):
                sd.run_service("192.0.2.1", "rtsp", "base.en")
        return po, store, health, sleeps

    def test_unreachable_scanner_never_spawns_ffmpeg_and_reports_down(self):
        po, store, health, sleeps = self._run(probe=lambda *a: "down")
        po.assert_not_called()
        self.assertEqual(sleeps, [10, 20, 40])
        self.assertEqual(health.report.call_args.args[1], "down")
        self.assertIn("off the LAN", health.report.call_args.args[2])

    def test_reachable_scanner_stores_call_and_reports_up(self):
        frames = [QUIET] * 3 + [LOUD] * 50 + [QUIET] * 40 + [b""]
        po, store, health, _ = self._run(probe=lambda *a: "up", frames=frames)
        store.save.assert_called_once_with("Engine 5 respond code 3", TAG, "fire")
        self.assertIn(mock.call("rtsp", "up"), health.report.call_args_list)
        transports = [c.args[0][c.args[0].index("-rtsp_transport") + 1] for c in po.call_args_list]
        self.assertEqual(transports[:2], ["udp", "tcp"])                   # alternates after a failure


class TestFrame(unittest.TestCase):
    def test_compiles_and_selftest(self):
        subprocess.run([sys.executable, "-m", "py_compile", str(PATH)], check=True)
        r = subprocess.run([sys.executable, str(PATH), "--selftest"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("wiring valid", r.stdout)

    def test_public_symbols(self):
        for n in ("Health", "probe_tcp", "backoff_s", "_retry", "Store", "MetadataFeed", "run_service"):
            self.assertTrue(hasattr(sd, n), n)


if __name__ == "__main__":
    unittest.main()
