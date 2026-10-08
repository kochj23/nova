#!/usr/bin/env python3
"""Tests for nova_tma1.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import inspect
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_tma1 as M  # noqa: E402

SRC = (SCRIPTS / "nova_tma1.py").read_text()
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
_PATCHERS = []


def setUpModule():
    # no notification and no Buick 8 write can escape from any test in this module
    for p in (mock.patch("nova_notify.notify", return_value=True),
              mock.patch("nova_buick8_log.log_unexplained", return_value=1)):
        p.start()
        _PATCHERS.append(p)


def tearDownModule():
    for p in reversed(_PATCHERS):
        p.stop()
    _PATCHERS.clear()


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = next((list(v) for k, v in self.routes.items() if k in sql), [])

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def fake_conn(cur):
    c = mock.MagicMock()
    c.cursor.return_value = cur
    return c


REG = [({"table": "legacy_api_keys", "config_service": "legacy_vault", "config_key": "master_api_key"},)]
PEEK = ("42", "nova", 3, "SELECT * FROM legacy_api_keys")


def routes(scans, readers=(PEEK,), prev_scans=4, prev_ts=T0):
    return {
        "FROM service_config": REG,
        "FROM pg_stat_user_tables": [(scans, T0 + timedelta(minutes=5))],
        "to_regclass": [("x",)],
        "FROM pg_stat_statements": list(readers),
        "FROM tma1_checks": [(prev_ts, prev_scans, T0, {})],
    }


class TestSecurity(unittest.TestCase):
    def test_no_secrets_and_sql_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(\w+(\.cursor\(\))?,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret|api_key)\s*=\s*['\"][^'\"]{6,}")
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_detector_never_reads_the_decoy(self):
        # run() must only touch catalog/stat views, never SELECT from the decoy itself
        for fn in (M.run, M.table_stats, M.readers_now, M.last_check, M.registry, M.judge):
            self.assertNotRegex(inspect.getsource(fn), r"(?i)FROM\s+(public\.)?legacy_api_keys")

    def test_decoy_values_are_random_filler(self):
        src = inspect.getsource(M.plant)
        self.assertIn('"tma1-decoy-" + secrets.token_hex(16)', src)

    def test_registry_table_name_validated(self):
        cur = FakeCur({"FROM service_config": [({"table": "x; DROP TABLE claude_queue"},)]})
        self.assertIsNone(M.registry(cur))

    def test_plant_only_via_explicit_flag(self):
        with mock.patch.object(M, "plant") as p, mock.patch.object(M, "run"), \
                mock.patch("argparse.ArgumentParser.print_help"):
            M.main(["--run", "--dry-run"])
            M.main([])
        p.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_delta_and_classify_10k(self):
        prev = {str(i): {"role": "r", "calls": i, "query": "SELECT 1 FROM legacy_api_keys"} for i in range(10000)}
        cur = {k: dict(v, calls=v["calls"] + (1 if int(k) % 10 == 0 else 0)) for k, v in prev.items()}
        t = time.monotonic()
        d = M.delta_readers(prev, cur)
        v = M.classify(d)
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertEqual((len(d), v), (1000, "reader"))


class TestRetry(unittest.TestCase):
    # RETRY GAP: run() — W.connect retries (3x backoff); notify/log_unexplained are single-shot
    def test_connect_retries_with_backoff(self):
        calls, sleeps = {"n": 0}, []

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("pg failover")
            return mock.MagicMock()
        fake_pg = mock.MagicMock(connect=flaky)
        with mock.patch.dict(sys.modules, {"psycopg2": fake_pg}):
            M.W.connect(_sleep=sleeps.append)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [2.0, 4.0])

    def test_pg_errors_fail_open(self):
        with mock.patch("builtins.print"):
            cur = FakeCur(boom=True)
            self.assertIsNone(M.registry(cur))
            self.assertIsNone(M.table_stats(cur, "legacy_api_keys"))
            self.assertEqual(M.readers_now(cur, "legacy_api_keys"), {})
            self.assertEqual(M.last_check(cur, "legacy_api_keys"), (None, {}))


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(M.selftest(), 0)

    def test_classify_edges(self):
        self.assertEqual(M.classify([]), "unattributed")
        self.assertEqual(M.classify([{"query": None}]), "unattributed")
        self.assertEqual(M.classify([{"query": "copy public.legacy_api_keys (a) to stdout"}]), "backup")
        self.assertEqual(M.classify([{"query": "COPY legacy_api_keys TO '/tmp/x'"}]), "reader")

    def test_delta_ignores_unchanged(self):
        r = {"1": {"role": "a", "calls": 2, "query": "q"}}
        self.assertEqual(M.delta_readers(r, r), [])
        self.assertEqual(M.delta_readers({}, {}), [])


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_imported(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("from nova_buick8_log import log_unexplained", SRC)
        self.assertIn("from nova_notify import notify", SRC)
        self.assertIn('W.get_config(cur, "tma1", "decoys")', SRC)

    def test_touch_goes_to_buick8_and_notify(self):
        import nova_buick8_log
        import nova_notify
        with mock.patch.object(nova_buick8_log, "log_unexplained") as lu, \
                mock.patch.object(nova_notify, "notify") as nt:
            M.alert(FakeCur(), "legacy_api_keys", {"scans": 6, "last_scan": T0}, {"scans": 4},
                    [{"role": "nova", "calls": 2, "query": "SELECT 1"}], "reader")
        args, kw = lu.call_args
        self.assertEqual(args[:2], ("tripwire_touched", "pg_table:legacy_api_keys"))
        self.assertEqual(kw["source"], "tma1")
        self.assertNotIn("cause", kw)
        self.assertEqual(nt.call_args.kwargs["dedup_key"], "tma1:legacy_api_keys")
        self.assertEqual(nt.call_args.kwargs["level"], "warning")

    def test_schema_contract(self):
        for col in ("decoy text NOT NULL", "scans bigint", "last_scan timestamptz", "touched boolean",
                    "verdict text", "readers jsonb"):
            self.assertIn(col, M.SCHEMA)


class TestFunctional(unittest.TestCase):
    def _run(self, dry, r):
        cur = FakeCur(r)
        with mock.patch.object(M.W, "connect", return_value=fake_conn(cur)), \
                mock.patch.object(M, "alert") as al, mock.patch("builtins.print"):
            j = M.run(dry=dry)
        return cur, j, al

    def test_read_is_recorded_and_alerted(self):
        cur, j, al = self._run(False, routes(6))
        self.assertEqual((j["touched"], j["verdict"]), (True, "reader"))
        ins = [p for s, p in cur.sql if "INSERT INTO tma1_checks" in s]
        self.assertEqual(len(ins), 1)
        self.assertTrue(ins[0][3])
        al.assert_called_once()

    def test_backup_is_logged_not_alerted(self):
        dump = ("7", "kochj", 1, "COPY public.legacy_api_keys (service) TO stdout;")
        cur, j, al = self._run(False, routes(5, readers=(dump,)))
        self.assertEqual(j["verdict"], "backup")
        al.assert_not_called()

    def test_quiet_check_writes_nothing_until_heartbeat(self):
        cur, j, al = self._run(False, routes(4, prev_ts=datetime.now(timezone.utc)))
        self.assertEqual(j["verdict"], "intact")
        self.assertFalse(any("INSERT" in s for s, _ in cur.sql))
        al.assert_not_called()

    def test_dry_run_writes_nothing(self):
        cur, j, al = self._run(True, routes(9))
        self.assertTrue(j["touched"])
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE")))
        al.assert_not_called()

    def test_unplanted_fails_open(self):
        cur, j, al = self._run(False, {})
        self.assertEqual(j["verdict"], "unplanted")
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT")))
        al.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_tma1.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_tma1.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--plant", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
