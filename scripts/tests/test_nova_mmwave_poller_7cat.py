#!/usr/bin/env python3
"""7-category tests for nova_mmwave_poller.py (2026-10-08: telemetry.presence only — presence_state is
the engine's; PG writes retried with backoff). Complements test_nova_mmwave_poller.py. psycopg2.connect,
time.sleep and subprocess are mocked; no server is bound. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_mmwave_poller.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="mmwave-7cat-"))

_spec = importlib.util.spec_from_file_location("mmwave_7cat", SCRIPT)
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)
P.LOG_FILE = TMP / "mmwave.log"


class Conn:
    def __init__(self, log, fail=False):
        self.log, self.fail, self.commits = log, fail, 0

    def cursor(self):
        if self.fail:
            raise RuntimeError("db down")
        conn = self

        class Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None): conn.log.append((" ".join(sql.split()), params))
        return Cur()

    def commit(self): self.commits += 1
    def close(self): pass


def handler(path, body=b"", ctype="application/json"):
    h = P.PresenceHandler.__new__(P.PresenceHandler)
    h.path = path
    h.headers = {"Content-Length": str(len(body)), "Content-Type": ctype}
    h.rfile, h.wfile, h.codes = io.BytesIO(body), io.BytesIO(), []
    h.send_response = h.codes.append
    h.send_header = lambda *a: None
    h.end_headers = lambda: None
    return h


class _Base(unittest.TestCase):
    def setUp(self):
        P._last_state.clear()
        P._event_count = 0
        self.sql = []
        p = mock.patch("psycopg2.connect", side_effect=lambda *a, **k: Conn(self.sql))
        self.connect = p.start()
        self.addCleanup(p.stop)
        s = mock.patch.object(P.time, "sleep")
        self.sleep = s.start()
        self.addCleanup(s.stop)


class TestSecurity(_Base):
    def test_never_writes_presence_state(self):
        P.process_presence_update("office", True, source="t")
        tables = " ".join(s for s, _ in self.sql)
        self.assertNotIn("presence_state", tables)
        self.assertIn("telemetry.presence", tables)

    def test_oversized_or_bad_json_rejected(self):
        h = handler("/presence", b"{not json")
        h.do_POST()
        self.assertEqual(h.codes, [400])
        self.assertEqual(self.sql, [])

    def test_hostile_room_is_bound_parameter(self):
        h = handler("/presence", json.dumps({"room": "x'); --", "presence": True}).encode())
        h.do_POST()
        sql, params = self.sql[0]
        self.assertNotIn("x');", sql)
        self.assertEqual(params[0], "x');_--")

    def test_no_credentials_in_dsn(self):
        self.assertNotIn("password", SRC.lower())


class TestPerformance(_Base):
    def test_heartbeat_suppresses_duplicate_writes(self):
        t0 = time.perf_counter()
        for _ in range(5000):
            P.process_presence_update("office", True, source="perf")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(sum("telemetry.presence" in s for s, _ in self.sql), 1)   # bounded: no write storm


class TestRetry(_Base):
    def test_transient_failure_retried_with_backoff_then_succeeds(self):
        attempts = []

        def connect(*a, **k):
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("no route")
            return Conn(self.sql)
        self.connect.side_effect = connect
        P.write_presence_sync("office", True, 0.95)
        self.assertEqual(len(attempts), 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [0.5, 1.0])
        self.assertEqual(P._event_count, 1)
        self.assertEqual(P.LOG_FILE.read_text().count("DB write error (attempt"), 2)   # not silent

    def test_permanent_failure_stops_after_db_attempts(self):
        self.connect.side_effect = lambda *a, **k: Conn(self.sql, fail=True)
        P.write_observation_sync("office", "enter")
        self.assertEqual(self.connect.call_count, P.DB_ATTEMPTS)
        self.assertEqual(self.sleep.call_count, P.DB_ATTEMPTS - 1)

    def test_shortcut_poll_loop_retries_before_disabling(self):
        # run_shortcut_poll is retried by its loop every POLL_INTERVAL; first failure is logged (WARN).
        with mock.patch.object(P, "run_shortcut_poll", return_value=False) as rp, \
             mock.patch.object(P, "_shutdown", False):
            P.shortcut_poll_loop()
        self.assertEqual(rp.call_count, 6)
        self.assertIn("relying on webhooks only", P.LOG_FILE.read_text())


class TestUnit(_Base):
    def test_db_write_returns_status(self):
        self.assertTrue(P._db_write("INSERT 1 %s", (1,), "x"))
        self.connect.side_effect = OSError("down")
        self.assertFalse(P._db_write("INSERT 1 %s", (1,), "x"))

    def test_confidence_mapping(self):
        P.write_presence_sync("office", False, 0.05)
        self.assertEqual(self.sql[0][1][1], 0.05)
        P._last_state.clear(); self.sql.clear()
        P.process_presence_update("office", True)
        self.assertEqual(self.sql[0][1][1], 0.95)


class TestIntegration(_Base):
    def test_rows_match_what_presence_engine_reads(self):
        P.write_presence_sync("office", True, 0.95, metadata={"source": "webhook"})
        sql, params = self.sql[0]
        self.assertIn("'mmwave'", sql)          # engine's get_mmwave_presence filters method='mmwave'
        engine = (SCRIPTS / "nova_presence_engine.py").read_text()
        self.assertIn("method = 'mmwave'", engine)


class TestFunctional(_Base):
    def test_enter_then_leave_golden_path(self):
        for presence in ("detected", "false"):
            h = handler("/presence", f"room=Office&presence={presence}".encode(), ctype="application/x-www-form-urlencoded")
            h.do_POST()
            self.assertEqual(h.codes, [200])
        obs = [p[1] for s, p in self.sql if "shared_observations" in s]
        self.assertEqual(obs, ["mmWave: enter detected in office", "mmWave: leave detected in office"])

    def test_db_outage_still_answers_webhook(self):
        self.connect.side_effect = OSError("down")
        h = handler("/presence", json.dumps({"room": "office", "presence": True}).encode())
        h.do_POST()
        self.assertEqual(h.codes, [200])
        self.assertEqual(P._event_count, 0)


class TestFrame(unittest.TestCase):
    def test_imports_without_side_effects(self):
        code = ("import importlib.util as u;"
                f"s=u.spec_from_file_location('m', {str(SCRIPT)!r}); m=u.module_from_spec(s);"
                "s.loader.exec_module(m); assert callable(m.main) and m.DB_ATTEMPTS == 3; print(m.LISTEN_PORT)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "8089")

    def test_main_exits_cleanly_when_pg_unreachable(self):
        with mock.patch.object(P, "get_sync_conn", side_effect=OSError("down")), \
             mock.patch.object(P.signal, "signal"), self.assertRaises(SystemExit):
            P.main()


if __name__ == "__main__":
    unittest.main()
