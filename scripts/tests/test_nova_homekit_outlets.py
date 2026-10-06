#!/usr/bin/env python3
"""Tests for nova_homekit_outlets.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_homekit_outlets.py"
SRC = SCRIPT.read_text()


@contextmanager
def _stub_modules(**mods):
    """Install import stubs and afterwards restore ONLY those keys (a whole-dict restore would drop every module
    the script imported for the first time, leaving e.g. a second `urllib` package without `.request`)."""
    mp = pytest.MonkeyPatch()
    for k, v in mods.items():
        mp.setitem(sys.modules, k, v)
    try:
        yield
    finally:
        mp.undo()


def _load(name, path):
    notify_stub = types.ModuleType("nova_notify"); notify_stub.notify = MagicMock(return_value=True)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with _stub_modules(nova_notify=notify_stub):      # bound at import; only that key restored
        spec.loader.exec_module(mod)
    return mod


hk = _load("hk", SCRIPT)
OUTLET = hk.OUTLET_SERVICE_TYPE


def _acc(room="Office", name="Eve Strip", sockets=(("Lamp", True, None, 1),), extra_service=True):
    services = [{"type": OUTLET, "name": n, "characteristics": [
        {"type": "Power State", "value": p}, {"type": "Outlet In Use", "value": u}, {"type": "Status Active", "value": a}]}
        for n, p, u, a in sockets]
    if extra_service:
        services.append({"type": "00000043-0000-1000-8000-0026BB765291", "name": "Light", "characteristics": []})
    return {"room": room, "name": name, "services": services}


class _Cur:
    def __init__(self, answers=()):
        self.answers = list(answers); self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.answers.pop(0)

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.commits = 0; self.closed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _run_once(accessories, do_alert=False, cur=None):
    cur = cur or _Cur()
    conn = _Conn(cur)
    hk.notify.reset_mock()
    with patch.object(hk, "fetch", return_value=accessories), patch.object(hk.psycopg2, "connect", return_value=conn) as pg, \
         redirect_stdout(io.StringIO()) as out:
        hk.run_once(do_alert)
    return cur, conn, out.getvalue(), pg


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", hk.DSN)

    def test_sql_is_parameterized_and_only_writes_homekit_outlets(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r'execute\([^)]*%\s*\(', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"telemetry.homekit_outlets"})
        inj = "Office'; DR" "OP TABLE telemetry.homekit_outlets; --"
        cur = _Cur()
        hk.ingest(cur, list(hk.outlets_from([_acc(room=inj)])))
        sql, params = cur.ran("INSERT INTO telemetry.homekit_outlets")[0]
        self.assertNotIn("DR" "OP", sql)
        self.assertEqual(params[1], inj); self.assertEqual(params[0], f"{inj}|Eve Strip|Lamp")

    def test_source_is_the_local_homekit_api_only(self):
        self.assertTrue(hk.HK_URL.startswith("http://127.0.0.1:"))
        u = MagicMock(); u.return_value.read.return_value = b"[]"
        with patch.object(hk.urllib.request, "urlopen", u):
            self.assertEqual(hk.fetch(), [])
        req = u.call_args.args[0]
        self.assertEqual(req.full_url, hk.HK_URL); self.assertEqual(u.call_args.kwargs["timeout"], 15)

    def test_alerts_are_off_by_default(self):
        self.assertEqual(hk.LEFT_ON_WATCHLIST, {})
        cur = _Cur()
        self.assertEqual(hk.left_on_alerts(cur), 0)
        self.assertEqual(cur.sql, []); hk.notify.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_10k_accessories_parse_and_ingest_fast(self):
        accs = [_acc(room=f"r{i % 5}", name=f"strip{i}", sockets=tuple((f"s{j}", j % 2 == 0, None, 1) for j in range(4)))
                for i in range(2_500)]
        t0 = time.perf_counter()
        rows = list(hk.outlets_from(accs))
        n = hk.ingest(_Cur(), rows)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual((len(rows), n), (10_000, 10_000))


class TestRetry(unittest.TestCase):
    def test_fetch_is_one_shot_and_fails_closed_before_pg(self):
        # RETRY GAP: fetch() — a single urlopen, no backoff; the exception escapes run_once() so a HomeKit outage
        # never opens a PG connection or writes a partial sample set.
        with patch.object(hk.urllib.request, "urlopen", side_effect=OSError("app down")) as u, \
             patch.object(hk.psycopg2, "connect") as pg:
            with self.assertRaises(OSError):
                hk.run_once(False)
        self.assertEqual(u.call_count, 1); pg.assert_not_called()

    def test_daemon_loop_survives_a_failed_poll(self):
        # RETRY GAP: main(--daemon) — no backoff, but the loop catches the error and re-polls after POLL_INTERVAL
        class _Stop(BaseException):
            pass
        sleeps = []

        def _sleep(s):
            sleeps.append(s)
            if len(sleeps) == 2:
                raise _Stop()
        with patch.object(hk, "run_once", side_effect=[RuntimeError("boom"), None]) as ro, \
             patch.object(hk.time, "sleep", _sleep), redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(_Stop):
                hk.main(["--daemon"])
        self.assertEqual(ro.call_count, 2)
        self.assertEqual(sleeps, [hk.POLL_INTERVAL, hk.POLL_INTERVAL])
        self.assertIn("[hk-outlets] error: boom", out.getvalue())

    def test_notify_is_best_effort(self):
        # the import falls back to a no-op notify when nova_notify is unavailable
        self.assertIn("except Exception:  # notify is best-effort", SRC)
        self.assertIn("def notify(*a, **k):\n        return False", SRC)


class TestUnit(unittest.TestCase):
    def test_tb_normalizes_homekit_values(self):
        self.assertIsNone(hk._tb(None))
        self.assertIs(hk._tb(0), False); self.assertIs(hk._tb(1), True); self.assertIs(hk._tb(True), True)

    def test_outlets_from_skips_non_outlet_services_and_falls_back_to_accessory_name(self):
        acc = _acc(sockets=(("Lamp", 1, 0, True), (None, 0, None, None)))
        rows = list(hk.outlets_from([acc]))
        self.assertEqual(rows, [("Office", "Eve Strip", "Lamp", True, False, True),
                                ("Office", "Eve Strip", "Eve Strip", False, None, None)])
        self.assertEqual(list(hk.outlets_from([])), [])
        self.assertEqual(list(hk.outlets_from([{"room": "x", "name": "y"}])), [])

    def test_ingest_builds_uid_and_counts(self):
        cur = _Cur()
        n = hk.ingest(cur, [("Office", "Strip", "Lamp", True, None, True), ("Rack", "Strip", "Switch", False, False, True)])
        self.assertEqual(n, 2)
        self.assertEqual([p[0] for _, p in cur.sql], ["Office|Strip|Lamp", "Rack|Strip|Switch"])
        self.assertIn("ON CONFLICT (ts, uid) DO NOTHING", cur.sql[0][0])
        self.assertEqual(hk.ingest(_Cur(), []), 0)

    def test_left_on_alert_fires_only_past_the_threshold(self):
        since = datetime.now(timezone.utc) - timedelta(hours=3)
        with patch.object(hk, "LEFT_ON_WATCHLIST", {"Iron": 1.0}):
            hk.notify.reset_mock()
            self.assertEqual(hk.left_on_alerts(_Cur([("Office|Strip|Iron", "Office", True), (since,)])), 1)
            title, kw = hk.notify.call_args.args[0], hk.notify.call_args.kwargs
            self.assertEqual(title, "Outlet left on: Iron (Office)")
            self.assertIn("has been on for 3.0h (threshold 1.0h)", kw["body"])
            self.assertEqual((kw["level"], kw["category"]), ("warning", "home"))
            self.assertTrue(kw["dedup_key"].startswith("hk_left_on:Office|Strip|Iron:"))
            hk.notify.reset_mock()
            recent = datetime.now(timezone.utc) - timedelta(minutes=10)
            self.assertEqual(hk.left_on_alerts(_Cur([("u", "Office", True), (recent,)])), 0)
            self.assertEqual(hk.left_on_alerts(_Cur([("u", "Office", False)])), 0)      # socket is off
            self.assertEqual(hk.left_on_alerts(_Cur([None])), 0)                         # never sampled
            self.assertEqual(hk.left_on_alerts(_Cur([("u", "Office", True), (None,)])), 0)
            hk.notify.assert_not_called()


class TestIntegration(unittest.TestCase):
    def test_notify_is_imported_from_nova_notify_not_reimplemented(self):
        self.assertIn("from nova_notify import notify", SRC)
        self.assertNotIn("INSERT INTO telemetry.events", SRC)

    def test_run_once_chains_ensure_ingest_and_commits(self):
        cur, conn, out, pg = _run_once([_acc(sockets=(("Lamp", 1, 1, 1), ("Fan", 0, None, 1)))])
        pg.assert_called_once_with(hk.DSN)
        self.assertTrue(cur.sql[0][0].startswith("CREATE TABLE IF NOT EXISTS telemetry.homekit_outlets"))
        self.assertEqual(len(cur.ran("INSERT INTO telemetry.homekit_outlets")), 2)
        self.assertEqual(conn.commits, 3); self.assertTrue(conn.closed)
        self.assertIn("ingested 2 sockets (1 with In Use value), 0 left-on alert(s)", out)

    def test_alert_flag_reads_the_same_table_the_ingest_wrote(self):
        since = datetime.now(timezone.utc) - timedelta(hours=5)
        cur = _Cur([("Office|Eve Strip|Lamp", "Office", True), (since,)])
        with patch.object(hk, "LEFT_ON_WATCHLIST", {"Lamp": 2.0}):
            cur, conn, out, pg = _run_once([_acc()], do_alert=True, cur=cur)
        reads = [s for s, _ in cur.sql if s.startswith("SELECT")]
        self.assertTrue(all("telemetry.homekit_outlets" in s for s in reads) and len(reads) == 2)
        self.assertIn("1 left-on alert(s)", out); self.assertEqual(hk.notify.call_count, 1)


class TestFunctional(unittest.TestCase):
    def test_golden_path_main_polls_once(self):
        cur = _Cur(); conn = _Conn(cur)
        with patch.object(hk, "fetch", return_value=[_acc(room="Rack", sockets=(("Switch", 1, None, 1),))]), \
             patch.object(hk.psycopg2, "connect", return_value=conn), redirect_stdout(io.StringIO()) as out:
            hk.main([])
        sql, params = cur.ran("INSERT INTO")[0]
        self.assertEqual(params, ("Rack|Eve Strip|Switch", "Rack", "Eve Strip", "Switch", True, None, True))
        self.assertIn("[hk-outlets] ingested 1 sockets (0 with In Use value), 0 left-on alert(s)", out.getvalue())
        self.assertTrue(conn.closed)

    def test_empty_accessory_list_writes_nothing(self):
        cur, conn, out, pg = _run_once([])
        self.assertEqual(cur.ran("INSERT INTO"), []); self.assertIn("ingested 0 sockets", out)

    def test_error_path_pg_failure_escapes_in_single_shot_mode(self):
        with patch.object(hk, "fetch", return_value=[_acc()]), patch.object(hk.psycopg2, "connect", side_effect=OSError("pg")):
            with self.assertRaises(OSError):
                hk.main([])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--daemon", r.stdout); self.assertIn("--alert", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_homekit_outlets"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
