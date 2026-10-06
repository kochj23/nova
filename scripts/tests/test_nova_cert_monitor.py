#!/usr/bin/env python3
"""Tests for nova_cert_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_cert_monitor.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cm = _load("cert_monitor_under_test", SCRIPT)
# Module-level stubs: no socket, no openssl shell-out, no PG — ever.
cm.socket = types.SimpleNamespace(create_connection=MagicMock(side_effect=OSError("offline: socket stubbed")))
cm.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=OSError("offline: subprocess stubbed")))
cm.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=OSError("offline: pg stubbed")),
                                    extras=types.SimpleNamespace(execute_batch=MagicMock()))

FUTURE = (cm.NOW + timedelta(days=42)).strftime("%b %d %H:%M:%S %Y GMT")


class _Sock:
    def __init__(self, cert): self.cert = cert
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def getpeercert(self): return self.cert


def _ssl_ctx(cert):
    ctx = MagicMock()
    ctx.wrap_socket.return_value = _Sock(cert)
    return ctx


class _Cur:
    def __init__(self): self.sql = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.sql.append((" ".join(sql.split()), params))


class _Conn:
    def __init__(self, cur): self.cur = cur; self.closed = False; self.autocommit = False
    def cursor(self): return self.cur
    def close(self): self.closed = True


def _openssl_runner(stdout_x509):
    """subprocess.run stub: first call is s_client, second is x509 with the given output."""
    def run(cmd, **kw):
        if cmd[1] == "s_client":
            return types.SimpleNamespace(stdout="-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", returncode=0)
        return types.SimpleNamespace(stdout=stdout_x509, returncode=0)
    return MagicMock(side_effect=run)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", cm.DB_DSN)

    def test_insert_is_parameterized_and_partition_bounds_are_bound(self):
        cur = _Cur()
        cm.ensure_partition(_Conn(cur), datetime(2026, 12, 3, tzinfo=timezone.utc))
        sql, params = cur.sql[0]
        self.assertIn("cert_expiry_202612 PARTITION OF telemetry.cert_expiry FOR VALUES FROM (%s) TO (%s)", sql)
        self.assertEqual(params, (datetime(2026, 12, 1, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc)))
        self.assertIsNone(re.search(r'execute\(\s*f"INSERT', SRC))

    def test_row_values_are_bound_not_interpolated(self):
        cur = _Cur(); conn = _Conn(cur); eb = MagicMock()
        evil = "x'); DROP TABLE telemetry.cert_expiry; --"
        with patch.object(cm.psycopg2, "connect", MagicMock(return_value=conn)), patch.object(cm.psycopg2.extras, "execute_batch", eb):
            n = cm.write_rows([cm._row("wazuh", evil, 443, note=evil)])
        self.assertEqual(n, 1)
        sql, values = eb.call_args[0][1], eb.call_args[0][2]
        self.assertNotIn("DROP", sql)
        self.assertEqual(sql, "INSERT INTO telemetry.cert_expiry (ts, endpoint, host, port, subject, not_after, "
                              "days_until_expiry, note) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)")
        self.assertEqual(values[0][2], evil)

    def test_openssl_invoked_as_argv_list_never_shell(self):
        run = _openssl_runner(f"notAfter={FUTURE}\nsubject=CN=x\n")
        with patch.object(cm.subprocess, "run", run):
            cm.fetch_via_openssl("h'; rm -rf /", 443)
        for call in run.call_args_list:
            self.assertIsInstance(call[0][0], list)
            self.assertFalse(call[1].get("shell", False))
        self.assertEqual(run.call_args_list[0][0][0][3], "h'; rm -rf /:443")   # stays one argv token


class TestPerformance(unittest.TestCase):
    def test_row_building_and_date_parse_fast_on_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            cm._row("ep", "h", i, subject="CN=x", note=None)
            cm._parse_notafter_openssl("Jun 20 12:00:00 2026 GMT")
        self.assertLess(time.perf_counter() - t0, 1.5)

    def test_write_rows_batches_in_one_execute_batch(self):
        cur = _Cur(); eb = MagicMock()
        rows = [cm._row("ep", "h", i) for i in range(10_000)]
        with patch.object(cm.psycopg2, "connect", MagicMock(return_value=_Conn(cur))), patch.object(cm.psycopg2.extras, "execute_batch", eb):
            t0 = time.perf_counter()
            self.assertEqual(cm.write_rows(rows), 10_000)
            self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(eb.call_count, 1)
        self.assertEqual(len(eb.call_args[0][2]), 10_000)


class TestRetry(unittest.TestCase):
    def test_ssl_failure_falls_back_to_openssl_then_records_unreachable(self):
        # The ssl path has a real fallback (openssl); the openssl path itself has no retry.
        # RETRY GAP: fetch_via_openssl — one attempt; a transient failure records an 'unreachable' row, never raises
        run = MagicMock(side_effect=OSError("no openssl"))
        with patch.object(cm.socket, "create_connection", MagicMock(side_effect=OSError("refused"))), \
             patch.object(cm.subprocess, "run", run), redirect_stdout(io.StringIO()):
            r = cm.collect_endpoint("wazuh", "192.0.2.1", 443)
        self.assertEqual(run.call_count, 1)
        self.assertIsNone(r["days_until_expiry"])
        self.assertTrue(r["note"].startswith("unreachable: no openssl"))
        self.assertEqual((r["endpoint"], r["host"], r["port"]), ("wazuh", "192.0.2.1", 443))

    def test_db_connect_fails_open(self):
        # RETRY GAP: write_rows/psycopg2.connect — one attempt; returns 0 instead of raising
        with patch.object(cm.psycopg2, "connect", MagicMock(side_effect=OSError("pg down"))), redirect_stdout(io.StringIO()):
            self.assertEqual(cm.write_rows([cm._row("a", "b", 1)]), 0)

    def test_insert_failure_is_swallowed_and_connection_closed(self):
        # RETRY GAP: write_rows/execute_batch — one attempt; 0 inserted, conn still closed
        cur = _Cur(); conn = _Conn(cur)
        with patch.object(cm.psycopg2, "connect", MagicMock(return_value=conn)), \
             patch.object(cm.psycopg2.extras, "execute_batch", MagicMock(side_effect=RuntimeError("partition missing"))), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(cm.write_rows([cm._row("a", "b", 1)]), 0)
        self.assertTrue(conn.closed)


class TestUnit(unittest.TestCase):
    def test_parse_notafter(self):
        self.assertEqual(cm._parse_notafter_openssl(" Jun 20 12:00:00 2026 GMT\n"),
                         datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            cm._parse_notafter_openssl("not a date")

    def test_row_ignores_unknown_columns(self):
        r = cm._row("ep", "h", 443, subject="CN=a", bogus="x")
        self.assertEqual(list(r), cm.COLUMNS)
        self.assertNotIn("bogus", r)
        self.assertIs(r["ts"], cm.NOW)
        self.assertIsNone(r["not_after"])

    def test_fetch_via_ssl_parses_subject_and_expiry(self):
        cert = {"notAfter": "Jun 20 12:00:00 2026 GMT", "subject": ((("commonName", "nova.digitalnoise.net"),), (("organizationName", "Nova"),))}
        with patch.object(cm.socket, "create_connection", MagicMock(return_value=_Sock(None))), \
             patch.object(cm.ssl, "create_default_context", MagicMock(return_value=_ssl_ctx(cert))):
            subj, na = cm.fetch_via_ssl("nova.digitalnoise.net", 443)
        self.assertEqual(subj, "commonName=nova.digitalnoise.net; organizationName=Nova")
        self.assertEqual(na, datetime(2026, 6, 20, 12, tzinfo=timezone.utc))

    def test_fetch_via_ssl_empty_cert_returns_nones(self):
        with patch.object(cm.socket, "create_connection", MagicMock(return_value=_Sock(None))), \
             patch.object(cm.ssl, "create_default_context", MagicMock(return_value=_ssl_ctx({}))):
            self.assertEqual(cm.fetch_via_ssl("h", 443), (None, None))

    def test_fetch_via_openssl_parses_lines(self):
        run = _openssl_runner(f"notAfter={FUTURE}\nsubject=CN = nova\n")
        with patch.object(cm.subprocess, "run", run):
            subj, na = cm.fetch_via_openssl("h", 8443)
        self.assertEqual(subj, "CN = nova")
        self.assertEqual(na.tzinfo, timezone.utc)
        self.assertEqual(run.call_args_list[1][1]["input"].split("\n")[0], "-----BEGIN CERTIFICATE-----")

    def test_ensure_partition_december_rolls_year_and_errors_are_logged(self):
        bad = types.SimpleNamespace(cursor=MagicMock(side_effect=RuntimeError("no cursor")))
        with redirect_stdout(io.StringIO()) as out:
            cm.ensure_partition(bad, cm.NOW)                 # must not raise
        self.assertIn("partition ensure: no cursor", out.getvalue())


class TestIntegration(unittest.TestCase):
    def test_collect_endpoint_ssl_path_computes_days(self):
        cert = {"notAfter": FUTURE, "subject": ((("commonName", "x"),),)}
        with patch.object(cm.socket, "create_connection", MagicMock(return_value=_Sock(None))), \
             patch.object(cm.ssl, "create_default_context", MagicMock(return_value=_ssl_ctx(cert))):
            r = cm.collect_endpoint("nova", "h", 443)
        self.assertAlmostEqual(r["days_until_expiry"], 42.0, places=1)
        self.assertEqual(r["subject"], "commonName=x")
        self.assertIsNone(r["note"])

    def test_empty_ssl_cert_falls_through_to_openssl(self):
        run = _openssl_runner(f"notAfter={FUTURE}\nsubject=CN=via-openssl\n")
        with patch.object(cm.socket, "create_connection", MagicMock(return_value=_Sock(None))), \
             patch.object(cm.ssl, "create_default_context", MagicMock(return_value=_ssl_ctx({}))), \
             patch.object(cm.subprocess, "run", run):
            r = cm.collect_endpoint("unas", "h", 443)
        self.assertEqual(r["subject"], "CN=via-openssl")
        self.assertAlmostEqual(r["days_until_expiry"], 42.0, places=1)

    def test_no_expiry_anywhere_records_note(self):
        run = _openssl_runner("subject=CN=only\n")
        with patch.object(cm.socket, "create_connection", MagicMock(return_value=_Sock(None))), \
             patch.object(cm.ssl, "create_default_context", MagicMock(return_value=_ssl_ctx({}))), \
             patch.object(cm.subprocess, "run", run):
            r = cm.collect_endpoint("unas", "h", 443)
        self.assertEqual(r["note"], "no expiry parsed")
        self.assertIsNone(r["days_until_expiry"])

    def test_write_rows_targets_telemetry_cert_expiry_with_ddl_and_partition(self):
        cur = _Cur(); eb = MagicMock()
        with patch.object(cm.psycopg2, "connect", MagicMock(return_value=_Conn(cur))), patch.object(cm.psycopg2.extras, "execute_batch", eb):
            cm.write_rows([cm._row("a", "b", 1)])
        self.assertIn("CREATE TABLE IF NOT EXISTS telemetry.cert_expiry", cur.sql[0][0])
        self.assertIn(f"cert_expiry_{cm.NOW.strftime('%Y%m')} PARTITION OF", cur.sql[1][0])
        self.assertEqual(cm.write_rows([]), 0)


class TestFunctional(unittest.TestCase):
    def _main(self, argv, run=None):
        cert = {"notAfter": FUTURE, "subject": ((("commonName", "x"),),)}
        cur = _Cur(); conn = _Conn(cur); eb = MagicMock(); buf = io.StringIO()
        with patch.object(sys, "argv", ["nova_cert_monitor.py", *argv]), \
             patch.object(cm.socket, "create_connection", MagicMock(return_value=_Sock(None))), \
             patch.object(cm.ssl, "create_default_context", MagicMock(return_value=_ssl_ctx(cert))), \
             patch.object(cm.subprocess, "run", run or MagicMock(side_effect=OSError("no shell"))), \
             patch.object(cm.psycopg2, "connect", MagicMock(return_value=conn)) as pg, \
             patch.object(cm.psycopg2.extras, "execute_batch", eb), redirect_stdout(buf):
            cm.main()
        return pg, eb, conn, buf.getvalue()

    def test_golden_path_writes_one_row_per_endpoint(self):
        pg, eb, conn, out = self._main([])
        pg.assert_called_once_with(cm.DB_DSN)
        self.assertTrue(conn.autocommit)
        values = eb.call_args[0][2]
        self.assertEqual([v[1] for v in values], [e[0] for e in cm.ENDPOINTS])
        self.assertEqual(len(values), len(cm.ENDPOINTS))
        self.assertIn(f"inserted {len(cm.ENDPOINTS)} row(s)", out)
        self.assertTrue(conn.closed)

    def test_dry_run_never_touches_pg(self):
        pg, eb, conn, out = self._main(["--dry-run"])
        pg.assert_not_called(); eb.assert_not_called()
        self.assertIn(f"DRY RUN — would insert {len(cm.ENDPOINTS)} row(s)", out)

    def test_unreachable_endpoints_still_produce_rows(self):
        cur = _Cur(); eb = MagicMock(); buf = io.StringIO()
        with patch.object(sys, "argv", ["nova_cert_monitor.py"]), \
             patch.object(cm.socket, "create_connection", MagicMock(side_effect=OSError("refused"))), \
             patch.object(cm.subprocess, "run", MagicMock(side_effect=OSError("no openssl"))), \
             patch.object(cm.psycopg2, "connect", MagicMock(return_value=_Conn(cur))), \
             patch.object(cm.psycopg2.extras, "execute_batch", eb), redirect_stdout(buf):
            cm.main()
        values = eb.call_args[0][2]
        self.assertEqual(len(values), len(cm.ENDPOINTS))
        self.assertTrue(all(v[7].startswith("unreachable") for v in values))
        self.assertIn("no expiry — unreachable", buf.getvalue())


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_never_runs_main(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_cert_monitor"], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
