#!/usr/bin/env python3
"""Tests for nova_fp2_presence.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
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

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_fp2_presence.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fp2 = _load("fp2_under_test", SCRIPT)


def _acc(name, room, value, extra_svc=True):
    chars = [{"type": "Status Active", "value": 1}]
    if value is not None:
        chars.append({"type": fp2.OCCUPANCY_TYPE, "value": value})
    return {"name": name, "room": room, "services": [{"characteristics": chars}] + ([{"characteristics": []}] if extra_svc else [])}


ACCS = [
    _acc("FP2 Office", "Office", 1),
    _acc("FP2 Living", "Living Room", 0),
    _acc("FP2 Patio", "Outdoor", True),
    _acc("FP2 Garage", "Garage", 1),            # room not in ROOM_MAP -> dropped
    _acc("Hue Motion", "Office", 1),           # not an FP2 -> dropped
    _acc("FP2 Bedroom", "Master Bedroom", None),   # no fresh reading -> dropped
]


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Cur:
    def __init__(self, exc=None): self.sql = []; self.exc = exc
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        if self.exc:
            raise self.exc
        self.sql.append((" ".join(sql.split()), params))


class _Conn:
    def __init__(self, cur): self.cur = cur; self.commits = 0; self.closed = False
    def cursor(self): return self.cur
    def commit(self): self.commits += 1
    def close(self): self.closed = True


def _run_main(accs=ACCS, cur=None, iterations=1, urlopen_exc=None, connect_fail_after=None):
    """Drive main() for N loop iterations: time.sleep flips the shutdown flag; signal/PG/HTTP are stubbed."""
    cur = cur or _Cur(); conns = []
    fp2._shutdown = False
    ticks = {"n": 0}

    def sleep(s):
        ticks["n"] += 1
        if ticks["n"] >= iterations:
            fp2._shutdown = True

    def connect(dsn):
        conns.append(dsn)
        if connect_fail_after is not None and len(conns) > connect_fail_after:
            raise fp2.psycopg2.OperationalError("still down")
        return _Conn(cur)

    def urlopen(req, timeout=None):
        if urlopen_exc:
            raise urlopen_exc
        return _Resp(accs)
    with patch.object(fp2.signal, "signal", MagicMock()), patch.object(fp2.time, "sleep", sleep), \
         patch.object(fp2.psycopg2, "connect", connect), patch("urllib.request.urlopen", urlopen), \
         redirect_stdout(io.StringIO()) as out:
        fp2.main()
    fp2._shutdown = False
    return cur, conns, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", fp2.PG_DSN)

    def test_insert_is_parameterized_and_only_writes_presence(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(0) for m in re.finditer(r"\b(INSERT INTO|UPDATE|DELETE FROM)\s+[\w.]+", SRC)}
        self.assertEqual(writes, {"INSERT INTO telemetry.presence"})
        cur = _Cur()
        fp2.write_presence(_Conn(cur), {"office'); DROP TABLE telemetry.presence; --": True})
        sql, params = cur.sql[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(params[1], "office'); DROP TABLE telemetry.presence; --")

    def test_room_vocabulary_is_an_allowlist(self):
        # an accessory can only ever write one of the four mapped rooms, never an arbitrary label from the bridge
        with patch("urllib.request.urlopen", lambda r, timeout=None: _Resp([_acc("FP2 X", "<script>", 1)])):
            self.assertEqual(fp2.fetch_fp2_occupancy(), {})
        self.assertEqual(set(fp2.ROOM_MAP.values()), {"office", "living_room", "master_bedroom", "patio"})


class TestPerformance(unittest.TestCase):
    def test_parses_10k_accessories_fast(self):
        rooms = list(fp2.ROOM_MAP)
        accs = [_acc(f"FP2 {i}", rooms[i % 4], i % 2) for i in range(10_000)]
        with patch("urllib.request.urlopen", lambda r, timeout=None: _Resp(accs)):
            t0 = time.perf_counter()
            out = fp2.fetch_fp2_occupancy()
            dt = time.perf_counter() - t0
        self.assertLess(dt, 2.0)
        self.assertEqual(len(out), 4)


class TestRetry(unittest.TestCase):
    def test_db_loss_reconnects_and_keeps_polling(self):
        # a dead handle is the 2026-07 zigbee failure mode: the loop must reconnect, not sit alive at 0% CPU
        cur = _Cur(exc=fp2.psycopg2.OperationalError("server closed the connection"))
        _, conns, out = _run_main(cur=cur, iterations=3)
        self.assertEqual(len(conns), 4)              # initial + one reconnect per failed poll
        self.assertIn("DB connection lost", out)
        self.assertIn("reconnecting", out)

    def test_reconnect_failure_is_logged_and_retried_next_tick(self):
        cur = _Cur(exc=fp2.psycopg2.InterfaceError("gone"))
        _, conns, out = _run_main(cur=cur, iterations=2, connect_fail_after=1)
        self.assertEqual(len(conns), 3)
        self.assertIn("reconnect failed: still down", out)

    def test_bridge_outage_fails_open_and_counts(self):
        # RETRY GAP: fetch_fp2_occupancy()/urlopen — no in-call retry; the loop swallows the error, logs the first 3
        # then every 30th, and simply tries again next POLL_INTERVAL. No row is written, no exception escapes.
        cur = _Cur()
        _, conns, out = _run_main(cur=cur, iterations=4, urlopen_exc=OSError("bridge down"))
        self.assertEqual(cur.sql, [])
        self.assertEqual(out.count("poll error"), 3)
        self.assertIn("poll error (1): bridge down", out)
        self.assertIn("Shutdown complete.", out)


class TestUnit(unittest.TestCase):
    def test_fetch_maps_rooms_and_drops_unknowns(self):
        with patch("urllib.request.urlopen", lambda r, timeout=None: _Resp(ACCS)):
            self.assertEqual(fp2.fetch_fp2_occupancy(), {"office": True, "living_room": False, "patio": True})

    def test_fetch_empty_and_malformed(self):
        with patch("urllib.request.urlopen", lambda r, timeout=None: _Resp([])):
            self.assertEqual(fp2.fetch_fp2_occupancy(), {})
        with patch("urllib.request.urlopen", lambda r, timeout=None: _Resp([{"name": "FP2 Office", "room": "Office"}])):
            self.assertEqual(fp2.fetch_fp2_occupancy(), {})     # no services -> no value -> no assertion

    def test_last_reading_wins_and_room_default_is_empty(self):
        a = _acc("FP2 Office", "Office", 1)
        a["services"].append({"characteristics": [{"type": fp2.OCCUPANCY_TYPE, "value": 0}]})
        with patch("urllib.request.urlopen", lambda r, timeout=None: _Resp([a, {"name": "FP2 nowhere"}])):
            self.assertEqual(fp2.fetch_fp2_occupancy(), {"office": False})

    def test_log_format_and_signal_handler(self):
        with redirect_stdout(io.StringIO()) as out:
            fp2.log("hello", "WARN")
        self.assertRegex(out.getvalue(), r"^\[fp2 \d\d:\d\d:\d\d\] \[WARN\] hello\n$")
        fp2._shutdown = False
        fp2._handle_signal(15, None)
        self.assertTrue(fp2._shutdown)
        fp2._shutdown = False


class TestIntegration(unittest.TestCase):
    def test_write_presence_uses_the_mmwave_method_the_engine_expects(self):
        cur = _Cur(); conn = _Conn(cur)
        fp2.write_presence(conn, {"office": True, "patio": False})
        self.assertEqual(conn.commits, 1)
        (sql, p1), (_, p2) = cur.sql
        self.assertIn("INSERT INTO telemetry.presence (ts, person, room, confidence, method, metadata) VALUES (now(), %s, %s, %s, 'mmwave', %s)", sql)
        self.assertEqual(p1[:3], ("occupant", "office", fp2.CONF_OCCUPIED))
        self.assertEqual(json.loads(p1[3]), {"occupied": True, "source": "fp2/novahomekit"})
        self.assertEqual(p2[2], fp2.CONF_EMPTY)
        self.assertTrue(0 < fp2.CONF_EMPTY < 0.5 < fp2.CONF_OCCUPIED < 1)

    def test_fetch_then_write_round_trip(self):
        cur = _Cur()
        with patch("urllib.request.urlopen", lambda r, timeout=None: _Resp(ACCS)):
            fp2.write_presence(_Conn(cur), fp2.fetch_fp2_occupancy())
        self.assertEqual([p[1] for _, p in cur.sql], ["office", "living_room", "patio"])


class TestFunctional(unittest.TestCase):
    def test_golden_loop_writes_rows_and_shuts_down_cleanly(self):
        cur, conns, out = _run_main()
        self.assertEqual(conns, [fp2.PG_DSN])
        self.assertEqual(len(cur.sql), 3)
        self.assertIn("FP2 presence poller started", out)
        self.assertIn("Health: 3 FP2 reporting, occupied: ['office', 'patio']", out)
        self.assertTrue(out.strip().endswith("Shutdown complete."))

    def test_empty_poll_writes_nothing(self):
        cur, conns, out = _run_main(accs=[])
        self.assertEqual(cur.sql, [])
        self.assertIn("0 FP2 reporting, occupied: none", out)

    def test_error_path_keeps_the_daemon_alive(self):
        cur, conns, out = _run_main(iterations=2, urlopen_exc=ValueError("bad json"))
        self.assertEqual(cur.sql, [])
        self.assertIn("poll error (2): bad json", out)
        self.assertIn("Shutdown complete.", out)


class TestFrame(unittest.TestCase):
    def test_import_never_starts_the_poller(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_fp2_presence; print(nova_fp2_presence.POLL_INTERVAL)"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "10")

    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
