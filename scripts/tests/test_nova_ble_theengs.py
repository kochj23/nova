#!/usr/bin/env python3
"""Tests for nova_ble_theengs.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

bleak and TheengsDecoder are stubbed for the import only (keys restored afterwards), sys.argv is pinned
so the module-level SCAN_S parse never sees pytest's argv, and psql / BLE scanning are always mocked."""
import asyncio
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
SCRIPT = SCRIPTS / "nova_ble_theengs.py"
SRC = SCRIPT.read_text()


def _stubs():
    bleak = types.ModuleType("bleak")
    bleak.BleakScanner = MagicMock()
    td = types.ModuleType("TheengsDecoder")
    td.decodeBLE = MagicMock(return_value=None)
    return {"bleak": bleak, "TheengsDecoder": td}


def _load():
    stubs = _stubs()
    saved = {k: sys.modules.get(k) for k in stubs}
    try:
        with patch.dict(sys.modules, stubs), patch.object(sys, "argv", ["nova_ble_theengs.py", "3"]):
            spec = importlib.util.spec_from_file_location("nova_ble_theengs_t", SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v  # restore the exact original object
    return mod


bt = _load()


def _adv(rssi=-60, name=None, mfr=None, svc=None):
    return types.SimpleNamespace(rssi=rssi, local_name=name, manufacturer_data=mfr or {}, service_data=svc or {})


def _discover(found):
    async def fake(**kw):
        return found
    return fake


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_literal_escaping_neutralizes_injection(self):
        # values are inlined into psql -c, so _s() must double every quote
        self.assertEqual(bt._s("x' OR '1'='1"), "'x'' OR ''1''=''1'")
        self.assertEqual(bt._s(None), "NULL")

    def test_psql_invoked_as_argv_list_not_shell(self):
        self.assertNotIn("shell=True", SRC)


class TestPerformance(unittest.TestCase):
    def test_input_builder_10k_fast(self):
        a = _adv(name="n", mfr={0x004C: b"\x02\x15"}, svc={"0000feaa-0000-1000-8000-00805f9b34fb": b"\x01"})
        t0 = time.perf_counter()
        for i in range(10_000):
            bt._theengs_input(f"AA:{i}", a)
            bt._s(f"o'{i}")
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_psql_failure_is_one_shot_and_returns_1(self):
        # RETRY GAP: main()/subprocess.run(psql) — one attempt; failure is printed and exit code 1, no raise
        err = subprocess.CalledProcessError(1, "psql", stderr="conn refused")
        with patch.object(bt.BleakScanner, "discover", _discover({"AA": (None, _adv())})), \
                patch.object(bt.subprocess, "run", side_effect=err) as run, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(bt.main(), 1)
        self.assertEqual(run.call_count, 1)
        self.assertIn("insert failed: conn refused", out.getvalue())

    def test_decoder_exception_fails_open(self):
        with patch.object(bt, "decodeBLE", side_effect=RuntimeError("bad adv")):
            self.assertEqual(bt._decode({"id": "x"}), {})


class TestUnit(unittest.TestCase):
    def test_theengs_input_encodes_company_id_little_endian(self):
        d = bt._theengs_input("AA", _adv(rssi=None, name="Tag", mfr={0x004C: b"\x12\x19"}))
        self.assertEqual(d["rssi"], -100)
        self.assertEqual(d["name"], "Tag")
        self.assertEqual(d["manufacturerdata"], "4c001219")

    def test_service_uuid_shortened(self):
        d = bt._theengs_input("AA", _adv(svc={"0000feaa-0000-1000-8000-00805f9b34fb": b"\xab"}))
        self.assertEqual((d["servicedatauuid"], d["servicedata"]), ("feaa", "ab"))
        self.assertEqual(bt._theengs_input("AA", _adv(svc={"fe": b""}))["servicedatauuid"], "fe")

    def test_decode_empty_and_json(self):
        with patch.object(bt, "decodeBLE", return_value=None):
            self.assertEqual(bt._decode({}), {})
        with patch.object(bt, "decodeBLE", return_value='{"brand":"Apple"}'):
            self.assertEqual(bt._decode({}), {"brand": "Apple"})

    def test_scan_seconds_from_argv(self):
        self.assertEqual(bt.SCAN_S, 3)


class TestIntegration(unittest.TestCase):
    def test_scan_tags_trackers_and_identified(self):
        found = {"T1": (None, _adv()), "U1": (None, _adv(name="thing"))}
        dec = {"T1": '{"brand":"Tile","model":"Mate","type":"TRACK","track":true}', "U1": None}
        with patch.object(bt.BleakScanner, "discover", _discover(found)), \
                patch.object(bt, "decodeBLE", side_effect=lambda s: dec[json.loads(s)["id"]]):
            rows = {r[0]: r for r in asyncio.run(bt.scan())}
        self.assertEqual(rows["T1"][3:6], ("TRACK", "Tile", True))
        self.assertEqual(rows["U1"][1], "thing")
        self.assertEqual(rows["U1"][3], "ble")
        self.assertEqual(rows["T1"][6]["scanner"], "bleak+theengs")


class TestFunctional(unittest.TestCase):
    def test_golden_path_inserts_into_telemetry_bluetooth(self):
        with patch.object(bt.BleakScanner, "discover", _discover({"AA": (None, _adv(name="O'Brien"))})), \
                patch.object(bt, "decodeBLE", return_value='{"brand":"Apple","track":false}'), \
                patch.object(bt.subprocess, "run") as run, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(bt.main(), 0)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "psql")
        self.assertIn("INSERT INTO telemetry.bluetooth", argv[-1])
        self.assertIn("'O''Brien'", argv[-1])
        self.assertIn("1 devices, 1 identified, 0 TRACKERS", out.getvalue())

    def test_no_devices_skips_insert(self):
        with patch.object(bt.BleakScanner, "discover", _discover({})), \
                patch.object(bt.subprocess, "run") as run, redirect_stdout(io.StringIO()):
            self.assertEqual(bt.main(), 0)
        run.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_with_stubs_never_runs_main(self):
        # no --help (argv[1] is scan seconds); smoke-import in a child with bleak/Theengs stubbed
        code = ("import sys,types,importlib.util\n"
                "b=types.ModuleType('bleak'); b.BleakScanner=object\n"
                "t=types.ModuleType('TheengsDecoder'); t.decodeBLE=lambda s: None\n"
                "sys.modules.update({'bleak': b, 'TheengsDecoder': t})\n"
                f"s=importlib.util.spec_from_file_location('m', {str(SCRIPT)!r})\n"
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print('SCAN', m.SCAN_S)\n")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "SCAN 25")
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
