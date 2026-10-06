#!/usr/bin/env python3
"""Tests for nova_wan_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_wan_monitor.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wm = _load("wm_mod", SCRIPT)
_TMP = tempfile.TemporaryDirectory()
wm.GATE_FILE = Path(_TMP.name) / ".wan_speedtest_last"     # never touch ~/.openclaw

PING_OUT = ("PING 1.1.1.1: 56 data bytes\n"
            "5 packets transmitted, 4 packets received, 20.0% packet loss\n"
            "round-trip min/avg/max/stddev = 10.1/12.5/15.0/1.2 ms\n")


def _ps(stdout="", rc=0):
    return types.SimpleNamespace(stdout=stdout, returncode=rc)


class _Cur:
    def __init__(self):
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.closed = False; self.autocommit = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", wm.DB_DSN)

    def test_subprocess_argv_lists_no_shell(self):
        self.assertNotIn("shell=True", SRC)
        with patch.object(wm.subprocess, "run", return_value=_ps(PING_OUT)) as sp:
            wm.ping("1.1.1.1; reboot")
        argv = sp.call_args[0][0]
        self.assertEqual(argv[0], "ping"); self.assertEqual(argv[-1], "1.1.1.1; reboot")   # one argv token, never a shell

    def test_insert_values_are_bound_and_partition_name_is_derived_from_time(self):
        cur = _Cur(); conn = _Conn(cur)
        rows = [wm._row("ping", "x'; select 1; --", latency_ms=1.0)]
        with patch.object(wm.psycopg2, "connect", return_value=conn), patch.object(wm.psycopg2.extras, "execute_batch") as eb, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(wm.write_rows(rows), 1)
        sql, values = eb.call_args[0][1], eb.call_args[0][2]
        self.assertEqual(sql.count("%s"), len(wm.COLUMNS)); self.assertIn("x'; select 1; --", values[0])
        part = [s for s, _ in cur.sql if "PARTITION OF" in s][0]
        self.assertRegex(part, r"telemetry\.wan_quality_\d{6} PARTITION OF")


class TestPerformance(unittest.TestCase):
    def test_row_building_10k_under_bound(self):
        t0 = time.perf_counter()
        rows = [wm._row("ping", f"10.0.{i // 256}.{i % 256}", latency_ms=i * 0.1, packet_loss_pct=0.0, bogus=1) for i in range(10_000)]
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(rows), 10_000)
        self.assertNotIn("bogus", rows[0]); self.assertEqual(list(rows[0]), wm.COLUMNS)


class TestRetry(unittest.TestCase):
    def test_ping_error_fails_open_as_100pct_loss_row(self):
        # RETRY GAP: ping()/subprocess.run — single attempt per anchor; an exception becomes a loss=100 row
        attempts = []

        def boom(*a, **k):
            attempts.append(1); raise subprocess.TimeoutExpired("ping", 30)
        with patch.object(wm.subprocess, "run", side_effect=boom), redirect_stdout(io.StringIO()):
            r = wm.ping("8.8.8.8")
        self.assertEqual(len(attempts), 1)
        self.assertEqual((r["kind"], r["target"], r["packet_loss_pct"], r["latency_ms"]), ("ping", "8.8.8.8", 100.0, None))
        self.assertIn("ping error", r["note"])

    def test_speedtest_chain_falls_through_ookla_cli_fallback(self):
        # RETRY GAP: run_speedtest() — each backend is tried once; failure falls to the next, finally None
        with patch.object(wm.shutil, "which", side_effect=lambda n: f"/bin/{n}"), \
             patch.object(wm.subprocess, "run", return_value=_ps("not json")), \
             patch.object(wm, "speedtest_fallback", return_value=None) as fb, redirect_stdout(io.StringIO()):
            self.assertIsNone(wm.run_speedtest())
        fb.assert_called_once()

    def test_fallback_download_error_returns_none(self):
        import urllib.request
        with patch.object(urllib.request, "urlopen", side_effect=OSError("offline")), redirect_stdout(io.StringIO()):
            self.assertIsNone(wm.speedtest_fallback())

    def test_db_connect_failure_fails_open_zero(self):
        # RETRY GAP: write_rows()/psycopg2.connect — one attempt; failure logs and returns 0 (rows for this 5-min tick are lost)
        with patch.object(wm.psycopg2, "connect", side_effect=OSError("pg down")), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wm.write_rows([wm._row("ping", "x")]), 0)
        self.assertIn("DB connect failed", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_ping_parses_macos_output_and_platform_flags(self):
        with patch.object(wm.subprocess, "run", return_value=_ps(PING_OUT)) as sp, patch("platform.system", return_value="Darwin"):
            r = wm.ping("1.1.1.1")
        self.assertEqual((r["latency_ms"], r["packet_loss_pct"]), (12.5, 20.0))
        self.assertIn("-t", sp.call_args[0][0]); self.assertNotIn("-W", sp.call_args[0][0])
        with patch.object(wm.subprocess, "run", return_value=_ps("")) as sp, patch("platform.system", return_value="Linux"):
            r = wm.ping("1.1.1.1")
        self.assertIn("-W", sp.call_args[0][0]); self.assertEqual((r["latency_ms"], r["packet_loss_pct"]), (None, None))

    def test_speedtest_parsers(self):
        ookla = json.dumps({"download": {"bandwidth": 12_500_000}, "upload": {"bandwidth": 2_500_000}, "ping": {"latency": 9.5}, "server": {"name": "LA"}})
        with patch.object(wm.subprocess, "run", return_value=_ps(ookla)):
            r = wm.speedtest_ookla("/bin/speedtest")
        self.assertEqual((r["down_mbps"], r["up_mbps"], r["latency_ms"], r["target"]), (100.0, 20.0, 9.5, "LA"))
        cli = json.dumps({"download": 50_000_000, "upload": 10_000_000, "ping": 20, "server": {"sponsor": "Spectrum"}})
        with patch.object(wm.subprocess, "run", return_value=_ps(cli)):
            r = wm.speedtest_cli("/bin/speedtest-cli")
        self.assertEqual((r["down_mbps"], r["up_mbps"], r["target"], r["note"]), (50.0, 10.0, "Spectrum", "speedtest-cli"))

    def test_gate_logic(self):
        if wm.GATE_FILE.exists():
            wm.GATE_FILE.unlink()
        self.assertFalse(wm._should_speedtest(force=True, disable=True))
        self.assertTrue(wm._should_speedtest(force=True, disable=False))
        self.assertTrue(wm._should_speedtest(False, False))          # no gate file yet
        wm._touch_gate()
        self.assertTrue(wm.GATE_FILE.exists())
        fresh = wm.NOW - timedelta(minutes=5)
        os.utime(wm.GATE_FILE, (fresh.timestamp(), fresh.timestamp()))
        self.assertFalse(wm._should_speedtest(False, False))
        old = wm.NOW - timedelta(hours=2)
        os.utime(wm.GATE_FILE, (old.timestamp(), old.timestamp()))
        self.assertTrue(wm._should_speedtest(False, False))

    def test_ensure_partition_month_rollover(self):
        from datetime import datetime, timezone
        cur = _Cur()
        wm.ensure_partition(_Conn(cur), datetime(2026, 12, 15, tzinfo=timezone.utc))
        sql, (first, nxt) = cur.sql[0]
        self.assertIn("wan_quality_202612", sql)
        self.assertEqual((first.year, first.month, first.day), (2026, 12, 1)); self.assertEqual((nxt.year, nxt.month), (2027, 1))


class TestIntegration(unittest.TestCase):
    def test_write_rows_creates_schema_partition_then_batch_inserts(self):
        cur = _Cur(); conn = _Conn(cur)
        rows = [wm.ping_row for _ in ()] or [wm._row("ping", "1.1.1.1", latency_ms=1), wm._row("speedtest", "LA", down_mbps=2)]
        with patch.object(wm.psycopg2, "connect", return_value=conn), patch.object(wm.psycopg2.extras, "execute_batch") as eb, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(wm.write_rows(rows), 2)
        self.assertIn("CREATE TABLE IF NOT EXISTS telemetry.wan_quality", cur.sql[0][0])
        self.assertIn("PARTITION OF telemetry.wan_quality", cur.sql[1][0])
        self.assertTrue(eb.call_args[0][1].startswith("INSERT INTO telemetry.wan_quality (ts, kind, target"))
        self.assertEqual(len(eb.call_args[0][2]), 2); self.assertTrue(conn.closed and conn.autocommit)

    def test_empty_rows_skip_db(self):
        with patch.object(wm.psycopg2, "connect") as pg, redirect_stdout(io.StringIO()):
            self.assertEqual(wm.write_rows([]), 0)
        pg.assert_not_called()


class TestFunctional(unittest.TestCase):
    def test_golden_path_pings_speedtests_writes_and_touches_gate(self):
        if wm.GATE_FILE.exists():
            wm.GATE_FILE.unlink()
        st = wm._row("speedtest", "LA", down_mbps=100.0, up_mbps=20.0, note="ookla speedtest CLI")
        with patch.object(wm.subprocess, "run", return_value=_ps(PING_OUT)), patch.object(wm, "run_speedtest", return_value=st), \
             patch.object(wm, "write_rows", return_value=3) as wr, patch.object(sys, "argv", ["nova_wan_monitor.py", "--speedtest"]), \
             redirect_stdout(io.StringIO()) as out:
            wm.main()
        rows = wr.call_args[0][0]
        self.assertEqual([r["target"] for r in rows], ["1.1.1.1", "8.8.8.8", "LA"])
        self.assertTrue(wm.GATE_FILE.exists())
        self.assertIn("inserted 3 row(s)", out.getvalue())

    def test_dry_run_writes_nothing_and_leaves_gate_alone(self):
        if wm.GATE_FILE.exists():
            wm.GATE_FILE.unlink()
        with patch.object(wm.subprocess, "run", return_value=_ps(PING_OUT)), patch.object(wm, "run_speedtest", return_value=None), \
             patch.object(wm, "write_rows") as wr, patch.object(wm.psycopg2, "connect") as pg, \
             patch.object(sys, "argv", ["nova_wan_monitor.py", "--dry-run", "--speedtest"]), redirect_stdout(io.StringIO()) as out:
            wm.main()
        wr.assert_not_called(); pg.assert_not_called()
        self.assertFalse(wm.GATE_FILE.exists())
        self.assertIn("speedtest produced no row", out.getvalue()); self.assertIn("DRY RUN — would insert 2 row(s)", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_never_runs_main(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("--no-speedtest", r.stdout)
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        snippet = ("import subprocess; subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError('ping at import'))\n"
                   "import nova_wan_monitor as m; assert m.PING_COUNT == 5")
        r = subprocess.run([sys.executable, "-c", snippet], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
