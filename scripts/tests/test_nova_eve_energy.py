#!/usr/bin/env python3
"""Tests for nova_eve_energy.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_eve_energy.py"
SRC = SCRIPT.read_text()
try:
    import psycopg2  # noqa: F401  real module locked in before any stubbing
except ImportError:
    pass


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ee = _load("eve_energy_under_test", SCRIPT)


def _acc(name, services):
    return {"name": name, "services": services}


def _svc(name=None, watts=None, kwh=None, on=None):
    chars = []
    if watts is not None:
        chars.append({"uuid": "E863F10C-079E-48FF-8F27-9C2605A29F52", "value": watts})
    if kwh is not None:
        chars.append({"uuid": "E863F10D-079E-48FF-8F27-9C2605A29F52", "value": kwh})
    if on is not None:
        chars.append({"uuid": "00000025-0000-1000-8000-0026BB765291", "value": on})
    s = {"characteristics": chars}
    if name:
        s["name"] = name
    return s


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
        self.cur = cur; self.autocommit = False; self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _run(payload, connect=None):
    """Run main() with the HomeKit API and PG mocked; returns (rc, cursor, stdout, stderr, urlopen)."""
    cur = _Cur()
    body = io.BytesIO(json.dumps(payload).encode())
    resp = MagicMock()
    resp.__enter__.return_value = body; resp.__exit__.return_value = False
    pg = types.SimpleNamespace(connect=connect or MagicMock(return_value=_Conn(cur)))
    with patch.object(ee.urllib.request, "urlopen", return_value=resp) as u, patch.object(ee, "psycopg2", pg), \
         redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
        rc = ee.main()
    return rc, cur, out.getvalue(), err.getvalue(), u


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", ee.DSN)

    def test_sql_is_parameterized_and_only_writes_energy(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"telemetry.energy"})
        evil = "Strip'; DROP TABLE telemetry.energy; --"
        rc, cur, _, _, _ = _run([_acc(evil, [_svc(watts=12)])])
        self.assertEqual(rc, 0)
        sql, params = cur.sql[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(params[0], f"eve:{evil}")

    def test_source_is_loopback_only(self):
        self.assertTrue(ee.SRC.startswith("http://127.0.0.1:"))


class TestPerformance(unittest.TestCase):
    def test_num_fast_on_10k_values(self):
        vals = [i if i % 3 else str(i) for i in range(10_000)]
        t0 = time.perf_counter()
        out = [ee.num(v) for v in vals]
        self.assertLess(time.perf_counter() - t0, 0.2)
        self.assertEqual(out[0], 0.0); self.assertEqual(out[1], 1.0)

    def test_main_hot_path_10k_services_under_2s(self):
        payload = [_acc(f"Strip {i}", [_svc(watts=i % 600, kwh=i, on=bool(i % 2))]) for i in range(10_000)]
        t0 = time.perf_counter()
        rc, cur, _, _, _ = _run(payload)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(rc, 0)
        self.assertEqual(len(cur.sql), 10_000)


class TestRetry(unittest.TestCase):
    def test_homekit_fetch_is_one_shot_and_never_writes(self):
        # RETRY GAP: main()/urllib.request.urlopen — one attempt; the fetch error escapes to launchd (non-zero
        # exit) and, crucially, nothing is written to telemetry.energy.
        pg = types.SimpleNamespace(connect=MagicMock())
        with patch.object(ee.urllib.request, "urlopen", side_effect=OSError("api down")) as u, patch.object(ee, "psycopg2", pg):
            with self.assertRaises(OSError):
                ee.main()
        self.assertEqual(u.call_count, 1)
        pg.connect.assert_not_called()

    def test_pg_connect_is_one_shot(self):
        # RETRY GAP: main()/psycopg2.connect — one attempt, error escapes; the fetched rows are simply dropped
        body = io.BytesIO(json.dumps([_acc("S", [_svc(watts=5)])]).encode())
        resp = MagicMock(); resp.__enter__.return_value = body; resp.__exit__.return_value = False
        connect = MagicMock(side_effect=RuntimeError("pg down"))
        with patch.object(ee.urllib.request, "urlopen", return_value=resp), patch.object(ee, "psycopg2", types.SimpleNamespace(connect=connect)):
            with self.assertRaises(RuntimeError):
                ee.main()
        self.assertEqual(connect.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_num_edges(self):
        self.assertEqual(ee.num(3), 3.0)
        self.assertEqual(ee.num(2.5), 2.5)
        self.assertEqual(ee.num("552"), 552.0)
        self.assertIsNone(ee.num("abc"))
        self.assertIsNone(ee.num(None))
        self.assertIsNone(ee.num([1]))
        self.assertIsNone(ee.num({}))
        self.assertEqual(ee.num(""), None)

    def test_characteristic_uuids_are_lowercase_prefixes(self):
        for u in (ee.WATT, ee.KWH, ee.ON):
            self.assertEqual(u, u.lower()); self.assertEqual(len(u), 8)
        self.assertEqual(ee.WATT, "e863f10c"); self.assertEqual(ee.KWH, "e863f10d")


class TestIntegration(unittest.TestCase):
    def test_rows_use_service_name_and_derive_on_state(self):
        payload = [_acc("Office Strip", [_svc(name="Outlet 1", watts=120.0, kwh=0, on=None),
                                          _svc(name="Outlet 2", watts=0.1, kwh=3.5, on="0"),
                                          _svc(name="No Power", on=True)])]
        rc, cur, out, _, _ = _run(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(len(cur.sql), 2)                          # the On-only service is skipped
        self.assertEqual(cur.sql[0][1], ("eve:Outlet 1", "Outlet 1", 120.0, None, True))  # kwh 0 -> NULL, on from watts
        self.assertEqual(cur.sql[1][1], ("eve:Outlet 2", "Outlet 2", 0.1, 3.5, False))    # on "0" -> num==1 False
        self.assertIn("wrote 2 Eve services, total 120 W", out)

    def test_accepts_dict_wrapper_and_bool_on(self):
        rc, cur, _, _, _ = _run({"accessories": [_acc("Strip", [_svc(watts="552", on=True)])]})
        self.assertEqual(cur.sql[0][1], ("eve:Strip", "Strip", 552.0, None, True))


class TestFunctional(unittest.TestCase):
    def test_golden_path_inserts_with_autocommit_and_closes(self):
        conn = _Conn(_Cur())
        connect = MagicMock(return_value=conn)
        rc, cur, out, err, u = _run([_acc("Strip", [_svc(watts=552, kwh=1.25, on=True)])], connect=connect)
        self.assertEqual(rc, 0)
        connect.assert_called_once_with(ee.DSN)
        self.assertTrue(conn.autocommit); self.assertTrue(conn.closed)
        req = u.call_args[0][0]                   # NovaHomeKit 51e7a91: Request carrying the Bearer token
        self.assertEqual(req.full_url, ee.SRC)
        self.assertEqual(req.get_method(), "GET")
        sql, params = conn.cur.sql[0]
        self.assertTrue(sql.startswith("INSERT INTO telemetry.energy (ts, device_id, device_name, watts, kwh_total, on_state)"))
        self.assertEqual(params, ("eve:Strip", "Strip", 552.0, 1.25, True))
        self.assertEqual(err, "")

    def test_no_eve_services_returns_1_without_touching_pg(self):
        connect = MagicMock()
        rc, cur, out, err, _ = _run([_acc("Lamp", [_svc(on=True)])], connect=connect)
        self.assertEqual(rc, 1)
        connect.assert_not_called()
        self.assertIn("no Eve power services found", err)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no argparse: --help would run main() against the HomeKit API, so the smoke is an import
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_eve_energy"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
