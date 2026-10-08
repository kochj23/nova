#!/usr/bin/env python3
"""7-category gap tests for nova_time_sense.py (presence_state consumer, 2026-10-08 change).

Complements tests/test_nova_time_sense.py: PG connect retry/backoff, the presence freshness
contract (stale > PRESENCE_FRESH_MIN => "can't tell", never "away"), bounded query count.
All PG is stubbed. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_time_sense.py"
SRC = SCRIPT.read_text()
spec = importlib.util.spec_from_file_location("time_sense_7cat", SCRIPT)
ts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts)
NOW = datetime(2026, 10, 6, 15, 0, tzinfo=timezone(timedelta(hours=-7)))


class _Cur:
    """Substring-routed cursor; an Exception value raises on execute. Unrouted SQL -> no rows."""
    def __init__(self, routes=()):
        self.routes = list(routes); self.sql = []; self.params = []; self._last = ""
        self.connection = mock.MagicMock()

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql
        for k, v in self.routes:
            if k in sql and isinstance(v, Exception):
                raise v

    def _r(self, d):
        return next((v for k, v in self.routes if k in self._last), d)

    def fetchone(self):
        return self._r((0,) if "count(*)" in self._last else None)

    def fetchall(self):
        return self._r([])


def _sense(presence):
    cur = _Cur([("FROM presence_state", presence)])
    s = ts.sense(cur, NOW)
    return s, ts.sentence(s, NOW)


class TestSecurity(unittest.TestCase):
    def test_presence_query_is_static_and_jordan_only(self):
        cur = _Cur([("FROM presence_state", ("office", 0.9, 1.0))])
        ts.sense(cur, NOW)
        (q,) = [s for s in cur.sql if "FROM presence_state" in s]
        self.assertIn("WHERE person = 'jordan'", q)        # never reads other residents into her sentence
        self.assertNotIn("%", q)

    def test_room_name_is_rendered_as_text_only(self):
        _, txt = _sense(("living_room", 0.9, 1.0))
        self.assertIn("in the living room", txt)
        _, txt = _sense(("<script>x</script>", 0.9, 1.0))
        self.assertNotIn("\n", txt)                        # one sentence, no line injection into the bootstrap

    def test_connect_has_timeout_and_no_password(self):
        self.assertNotIn("password", ts.DSN)
        c = mock.MagicMock()
        with mock.patch.object(ts.psycopg2, "connect", c):
            ts._connect()
        self.assertEqual(c.call_args.kwargs.get("connect_timeout"), 10)


class TestPerformance(unittest.TestCase):
    def test_sense_issues_a_bounded_number_of_queries(self):
        cur = _Cur([("FROM presence_state", ("office", 0.9, 1.0))])
        ts.sense(cur, NOW)
        self.assertLessEqual(len(cur.sql), 16)

    def test_backoff_total_is_bounded(self):
        sleeps = []
        with mock.patch.object(ts.psycopg2, "connect", side_effect=ts.psycopg2.OperationalError("down")), \
             mock.patch.object(ts.time, "sleep", sleeps.append), redirect_stdout(io.StringIO()):
            with self.assertRaises(ts.psycopg2.OperationalError):
                ts._connect()
        self.assertEqual(sleeps, [2.0, 4.0])

    def test_sentence_is_fast(self):
        s, _ = _sense(("office", 0.9, 1.0))
        t0 = time.perf_counter()
        for _ in range(10000):
            ts.sentence(s, NOW)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_connect_recovers_after_two_failures(self):
        conn = object()
        c = mock.MagicMock(side_effect=[ts.psycopg2.OperationalError("a"), ts.psycopg2.OperationalError("b"), conn])
        with mock.patch.object(ts.psycopg2, "connect", c), mock.patch.object(ts.time, "sleep"), \
             redirect_stdout(io.StringIO()) as out:
            self.assertIs(ts._connect(), conn)
        self.assertEqual(c.call_count, 3)
        self.assertEqual(out.getvalue().count("PG connect attempt"), 2)     # each failure is logged

    def test_presence_read_failure_rolls_back_and_drops_the_clause(self):
        s, txt = _sense(RuntimeError("relation presence_state does not exist"))
        self.assertNotIn("jordan_room", s)
        self.assertNotIn("Little Mister is", txt)


class TestUnit(unittest.TestCase):
    def test_freshness_boundary(self):
        self.assertTrue(_sense(("office", 0.9, float(ts.PRESENCE_FRESH_MIN)))[0]["presence_fresh"])
        self.assertFalse(_sense(("office", 0.9, ts.PRESENCE_FRESH_MIN + 0.1))[0]["presence_fresh"])
        self.assertFalse(_sense(("office", 0.9, None))[0]["presence_fresh"])

    def test_stale_away_is_unknown_not_away(self):
        _, txt = _sense(("away", 0.8, 45.0))
        self.assertIn("I can't tell where Little Mister is", txt)
        self.assertNotIn("away from home", txt)

    def test_no_row_means_no_presence_clause(self):
        s, txt = _sense(None)
        self.assertNotIn("Little Mister is", txt)


class TestIntegration(unittest.TestCase):
    def test_tempo_and_presence_are_separate_clauses(self):
        _, txt = _sense(("office", 0.9, 1.0))
        self.assertIn("my event stream has been", txt)
        self.assertIn("Little Mister is home, in the office", txt)
        self.assertNotIn("the house has been", txt)


class TestFunctional(unittest.TestCase):
    def _main(self, cur, leading_errors=(), argv=()):
        conn = mock.MagicMock(); conn.cursor.return_value = cur
        with mock.patch.object(ts.psycopg2, "connect", side_effect=[*leading_errors, conn]), \
             mock.patch.object(ts.time, "sleep"), mock.patch.object(ts.sys, "argv", ["x", *argv]), \
             redirect_stdout(io.StringIO()) as out:
            ts.main()
        return conn, out.getvalue()

    def test_golden_path_after_a_pg_blip_publishes_presence_sentence(self):
        cur = _Cur([("FROM presence_state", ("kitchen", 0.9, 2.0))])
        conn, out = self._main(cur, [ts.psycopg2.OperationalError("blip")])
        self.assertIn("PG connect attempt 1 failed", out)
        (p,) = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO service_config" in s]
        self.assertIn("Little Mister is home, in the kitchen", json.loads(p[0])["sentence"])
        self.assertEqual(conn.commit.call_count, 2)

    def test_error_path_pg_down_raises(self):
        with mock.patch.object(ts.psycopg2, "connect", side_effect=ts.psycopg2.OperationalError("down")), \
             mock.patch.object(ts.time, "sleep"), mock.patch.object(ts.sys, "argv", ["x"]), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(ts.psycopg2.OperationalError):
                ts.main()


class TestFrame(unittest.TestCase):
    def test_import_smoke(self):
        r = subprocess.run([sys.executable, "-c", "import nova_time_sense as m; assert callable(m._connect); print('ok')"],
                           cwd=SCRIPTS, capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"), r.stderr)
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)


if __name__ == "__main__":
    unittest.main()
