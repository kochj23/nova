"""nova_dials.py (show / parse_set / set_dial / reset / CLI) — 7-category supplement (Security,
Performance, Retry, Unit, Integration, Functional, Frame). PG is always mocked.
Written by Jordan Koch (via Claude)."""
import io
import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import psycopg2  # noqa: E402
import nova_dials as nd  # noqa: E402
import nova_voice as nv  # noqa: E402

D = nv.DIAL_DEFAULTS


def _conn():
    cur = mock.MagicMock()
    cur.__enter__.return_value = cur
    conn = mock.MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cur
    return conn, cur


def _quiet(fn, *a):
    with redirect_stdout(io.StringIO()) as o, redirect_stderr(io.StringIO()) as e:
        rc = fn(*a)
    return rc, o.getvalue(), e.getvalue()


class TestSecurity(unittest.TestCase):
    def test_injection_names_rejected_before_db(self):
        with mock.patch("psycopg2.connect") as c:
            for name in ("humor'; DROP TABLE service_config;--", "../x", ""):
                rc, _, err = _quiet(nd.main, ["set", name, "5"])
                self.assertEqual(rc, 2)
                self.assertIn("unknown dial", err)
            rc, _, _ = _quiet(nd.main, ["reset", "x; DELETE"])
            self.assertEqual(rc, 2)
        c.assert_not_called()

    def test_values_written_as_json_params(self):
        conn, cur = _conn()
        with mock.patch("psycopg2.connect", return_value=conn):
            nd.set_dial("profanity", False, by="test")
        sql, params = cur.execute.call_args[0]
        self.assertIn("%s::jsonb", sql)
        self.assertEqual(params, ("nova_dials", "profanity", "false", "test"))

    def test_reset_scoped_to_service(self):
        conn, cur = _conn()
        with mock.patch("psycopg2.connect", return_value=conn):
            nd.reset()
        sql, params = cur.execute.call_args[0]
        self.assertIn("WHERE service = %s", sql)
        self.assertEqual(params, ("nova_dials",))


class TestPerformance(unittest.TestCase):
    def test_parse_and_show_fast(self):
        t = time.perf_counter()
        for i in range(20_000):
            nd.parse_set("humor", str(i % 101))
        for _ in range(2_000):
            nd.show(dict(D))
        self.assertLess(time.perf_counter() - t, 1.0)


class TestRetry(unittest.TestCase):
    def test_connect_retries_operational_error(self):
        conn, _ = _conn()
        with mock.patch("psycopg2.connect", side_effect=[psycopg2.OperationalError("blip"), conn]) as c, \
             mock.patch("time.sleep") as sl:
            self.assertIs(nd._connect(), conn)
        self.assertEqual(c.call_count, 2)
        sl.assert_called_once_with(0.5)

    def test_connect_gives_up_loudly_after_three(self):
        with mock.patch("psycopg2.connect", side_effect=psycopg2.OperationalError("down")) as c, \
             mock.patch("time.sleep") as sl:
            with self.assertRaises(psycopg2.OperationalError):
                nd._connect()
        self.assertEqual(c.call_count, 3)
        self.assertEqual([x[0][0] for x in sl.call_args_list], [0.5, 1.0])

    def test_non_transient_error_not_retried(self):
        with mock.patch("psycopg2.connect", side_effect=ValueError("bad dsn")) as c:
            with self.assertRaises(ValueError):
                nd._connect()
        self.assertEqual(c.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_parse_set_matrix(self):
        self.assertEqual(nd.parse_set("humor", "0"), 0)
        self.assertEqual(nd.parse_set("humor", "100"), 100)
        self.assertIs(nd.parse_set("profanity", "YES"), True)
        self.assertIs(nd.parse_set("profanity", "0"), False)
        for bad in (("humor", "-1"), ("humor", "50.5"), ("humor", "loud"), ("profanity", "2")):
            with self.assertRaises(ValueError):
                nd.parse_set(*bad)

    def test_show_marks_off_default(self):
        out = nd.show(dict(D, humor=60, profanity=False))
        self.assertIn("humor         60/100   (default 90)", out)
        self.assertIn("profanity    off   (default on)", out)
        self.assertNotIn("(default", out.splitlines()[1])   # snark at default


class TestIntegration(unittest.TestCase):
    def test_set_then_show_reads_fresh_values(self):
        conn, cur = _conn()
        cur.fetchall.return_value = [("humor", 33)]
        with mock.patch("psycopg2.connect", return_value=conn):
            rc, out, _ = _quiet(nd.main, ["set", "humor", "33"])
        self.assertEqual(rc, 0)
        self.assertIn("humor         33/100", out)
        nv._DIAL_CACHE.update(at=0.0, vals=None)


class TestFunctional(unittest.TestCase):
    def tearDown(self):
        nv._DIAL_CACHE.update(at=0.0, vals=None)

    def test_reset_one_and_all(self):
        conn, cur = _conn()
        cur.fetchall.return_value = []
        with mock.patch("psycopg2.connect", return_value=conn):
            self.assertEqual(_quiet(nd.main, ["reset", "humor"])[0], 0)
            self.assertEqual(cur.execute.call_args_list[0][0][1], ("nova_dials", "humor"))
            self.assertEqual(_quiet(nd.main, ["reset"])[0], 0)

    def test_bad_usage_prints_doc(self):
        rc, _, err = _quiet(nd.main, ["frobnicate"])
        self.assertEqual(rc, 2)
        self.assertIn("nova_dials.py show", err)

    def test_db_down_on_set_is_loud(self):
        with mock.patch("psycopg2.connect", side_effect=psycopg2.OperationalError("down")), mock.patch("time.sleep"):
            with self.assertRaises(psycopg2.OperationalError):
                nd.main(["set", "humor", "10"])


class TestFrame(unittest.TestCase):
    def test_selftest_subprocess(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_dials.py"), "--selftest"], capture_output=True,
                           text=True, timeout=30, cwd=SCRIPTS, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest ok", r.stdout)


if __name__ == "__main__":
    unittest.main()
