#!/usr/bin/env python3
"""Tests for nova_ble_phy_collector.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ble_phy_collector.py"
SRC = SCRIPT.read_text()


def _stub_modules():
    cfg = types.ModuleType("nova_config"); cfg.NOVA_HOST = "127.0.0.1"; cfg.post_both = MagicMock()
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock()
    return {"nova_config": cfg, "nova_notify": nn}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()), patch.dict(os.environ, {"NOVA_BLE_OBSERVER": "test-phy"}):
        spec.loader.exec_module(mod)        # nova_ble_monitor (real) binds the stubbed config/notify; restored after
    return mod


bp = _load("ble_phy_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="ble-phy-test-"))
FAKE_UBERTOOTH = TMP / "ubertooth-btle"; FAKE_UBERTOOTH.write_text("#!/bin/sh\n")
bp.UBERTOOTH = str(FAKE_UBERTOOTH)
# Module-level stubs: no radio process, no PG.
bp.subprocess = types.SimpleNamespace(Popen=MagicMock(side_effect=OSError("offline: Popen stubbed")),
                                      PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL)
bp.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=OSError("offline: pg stubbed")),
                                    extras=types.SimpleNamespace(execute_values=MagicMock(), Json=lambda d: ("JSON", d)))


def _pkt_block(mac, advdata_hex, rssi=-60, chan=37, ts=1000, addr="random"):
    return (f"systime={ts} freq=2402 addr=8e89bed6 delta_t=1.0 ms rssi={rssi}\n"
            f"    Channel Index: {chan}\n"
            f"    AdvA:  {mac} ({addr})\n"
            f"    AdvData: {advdata_hex}\n")


# Flags(02 01 06) + complete name "HomePod"(08 09 ...) + Apple mfg 0x004C subtype 0x12 len 0x02 (owner nearby)
NAME_HOMEPOD = "02 01 06 08 09 48 6f 6d 65 50 6f 64 "
APPLE_NEARBY = "07 ff 4c 00 12 02 00 00 "
APPLE_SEPARATED = "1e ff 4c 00 12 19 " + "aa " * 25
UUID16_HR = "03 03 0d 18 "            # 16-bit Heart Rate 0x180D
TXPOWER = "02 0a fb "                 # -5 dBm
NO_FIELDS = "02 01 06 "


class _Cur:
    def __init__(self): self.sql, self.params = [], []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.sql.append(" ".join(sql.split())); self.params.append(params)


class _Conn:
    def __init__(self, cur): self.cur = cur; self.closed = False; self.autocommit = False
    def cursor(self): return self.cur
    def close(self): self.closed = True


class _Clock:
    """time.time() stand-in that replays an explicit schedule (first value seeds last_flush, one per packet
    after that) and then holds the last value — no module-level clock."""
    def __init__(self, seq): self.seq = list(seq)
    def __call__(self): return self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]


def _flush_after(n_packets):
    """Schedule where the first n-1 packets stay inside FLUSH_SECONDS and packet n trips the flush."""
    return _Clock([1000.0] + [1000.0 + i for i in range(1, n_packets)] + [1000.0 + bp.FLUSH_SECONDS + 1])


def _run_main(stream_text, clock=None, connect_exc=None, ev_exc=None, shutdown=False):
    cur = _Cur(); conn = _Conn(cur)
    proc = types.SimpleNamespace(stdout=io.StringIO(stream_text), terminate=MagicMock())
    ev = MagicMock(side_effect=ev_exc)
    fake_time = types.SimpleNamespace(time=clock or _flush_after(1), strftime=time.strftime)
    buf = io.StringIO()
    with patch.object(bp.psycopg2, "connect", MagicMock(return_value=conn, side_effect=connect_exc)) as pg, \
         patch.object(bp.psycopg2.extras, "execute_values", ev), patch.object(bp.subprocess, "Popen", MagicMock(return_value=proc)) as po, \
         patch.object(bp, "time", fake_time), patch.object(bp, "_shutdown", shutdown), redirect_stdout(buf):
        rc = bp.main()
    return rc, cur, conn, proc, ev, po, buf.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", bp.DSN)

    def test_sql_is_parameterized_and_only_telemetry_bluetooth_is_written(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"telemetry.bluetooth"})
        self.assertIn('template="(NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)"', SRC)

    def test_control_characters_in_radio_names_are_stripped(self):
        # NUL/garbage inside a "name" killed the collector mid-insert once; it must never reach a row
        name, *_ = bp.decode_advdata(bytes.fromhex("0a09") + b"Ho\x00me\x01Pod\x7f")
        self.assertEqual(name, "HomePod")
        self.assertNotIn("\x00", name)

    def test_radio_binary_is_launched_as_argv_list(self):
        rc, cur, conn, proc, ev, po, out = _run_main(_pkt_block("AA:BB:CC:DD:EE:01", NAME_HOMEPOD))
        argv = po.call_args[0][0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[-2:], [str(FAKE_UBERTOOTH), "-n"])
        self.assertFalse(po.call_args[1].get("shell", False))


class TestPerformance(unittest.TestCase):
    def test_decode_and_classify_fast_on_10k_packets(self):
        adv = bytes.fromhex((NAME_HOMEPOD + APPLE_SEPARATED + UUID16_HR + TXPOWER).replace(" ", ""))
        t0 = time.perf_counter()
        for _ in range(10_000):
            bp.decode_advdata(adv); bp.classify_apple(adv)
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_parse_packets_streams_10k_blocks_linearly(self):
        text = "".join(_pkt_block(f"AA:BB:CC:{i >> 8:02X}:{i & 255:02X}:00", NO_FIELDS, ts=i) for i in range(10_000))
        t0 = time.perf_counter()
        n = sum(1 for _ in bp.parse_packets(io.StringIO(text)))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(n, 10_000)


class TestRetry(unittest.TestCase):
    def test_pg_connect_failure_propagates_before_the_radio_starts(self):
        # RETRY GAP: main/psycopg2.connect — one attempt; the exception escapes (launchd/systemd restarts)
        with self.assertRaises(OSError):
            _run_main("", connect_exc=OSError("pg down"))
        self.assertEqual(bp.subprocess.Popen.call_count, 0)              # the module-level stub was never reached

    def test_insert_failure_propagates_but_radio_and_conn_are_cleaned_up(self):
        # RETRY GAP: execute_values flush — no retry; the finally still terminates ubertooth and closes PG
        stream = _pkt_block("AA:BB:CC:DD:EE:01", NAME_HOMEPOD) * 3
        with self.assertRaises(RuntimeError):
            _run_main(stream, clock=_flush_after(3), ev_exc=RuntimeError("partition missing"))

    def test_insert_failure_cleanup_details(self):
        cur = _Cur(); conn = _Conn(cur)
        proc = types.SimpleNamespace(stdout=io.StringIO(_pkt_block("AA:BB:CC:DD:EE:01", NAME_HOMEPOD) * 3), terminate=MagicMock())
        fake_time = types.SimpleNamespace(time=_flush_after(3), strftime=time.strftime)
        with patch.object(bp.psycopg2, "connect", MagicMock(return_value=conn)), \
             patch.object(bp.psycopg2.extras, "execute_values", MagicMock(side_effect=RuntimeError("x"))), \
             patch.object(bp.subprocess, "Popen", MagicMock(return_value=proc)), patch.object(bp, "time", fake_time), redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                bp.main()
        proc.terminate.assert_called_once(); self.assertTrue(conn.closed)


class TestUnit(unittest.TestCase):
    def test_decode_advdata_shapes_match_bleak(self):
        adv = bytes.fromhex((NAME_HOMEPOD + APPLE_NEARBY + UUID16_HR + TXPOWER).replace(" ", ""))
        name, uuids, cids, tx = bp.decode_advdata(adv)
        self.assertEqual(name, "HomePod")
        self.assertEqual(uuids, ["0000180d-0000-1000-8000-00805f9b34fb"])
        self.assertEqual(cids, [0x004C]); self.assertEqual(tx, -5)
        self.assertEqual(bp.decode_advdata(b""), (None, [], [], None))
        self.assertEqual(bp.decode_advdata(bytes.fromhex("ff09")), (None, [], [], None))   # truncated tail stops, no raise
        self.assertEqual(bp.decode_advdata(bytes.fromhex("00000000")), (None, [], [], None))

    def test_uuid_expansion_is_little_endian(self):
        self.assertEqual(bp._uuid16(bytes.fromhex("0d18")), "0000180d-0000-1000-8000-00805f9b34fb")
        self.assertEqual(bp._uuid32(bytes.fromhex("78563412")), "12345678-0000-1000-8000-00805f9b34fb")
        u = bytes.fromhex("fb349b5f80000080001000000d180000")
        self.assertEqual(bp._uuid128(u), "0000180d-0000-1000-8000-00805f9b34fb")
        _, uuids, _, _ = bp.decode_advdata(bytes([17, 0x07]) + u)
        self.assertEqual(uuids, ["0000180d-0000-1000-8000-00805f9b34fb"])

    def test_classify_apple(self):
        self.assertEqual(bp.classify_apple(bytes.fromhex(APPLE_NEARBY.replace(" ", ""))), ("findmy", "owner_nearby"))
        self.assertEqual(bp.classify_apple(bytes.fromhex(APPLE_SEPARATED.replace(" ", ""))), ("findmy", "separated"))
        self.assertEqual(bp.classify_apple(bytes.fromhex("04ff4c0010")), ("nearby", None))
        self.assertEqual(bp.classify_apple(bytes.fromhex("04ff4c00ee")), (None, None))        # unknown subtype
        self.assertEqual(bp.classify_apple(bytes.fromhex("04ffe00001")), (None, None))        # not Apple
        self.assertEqual(bp.classify_apple(b""), (None, None))

    def test_parse_packets_tolerates_garbage_and_headerless_lines(self):
        text = "garbage before any header\n" + _pkt_block("aa:bb:cc:dd:ee:01", NO_FIELDS, rssi=-70, chan=38, addr="public") \
               + "systime=5 freq=2426 addr=x delta_t=2 ms rssi=-50\n    no AdvA here\n" + _pkt_block("AA:BB:CC:DD:EE:02", NO_FIELDS)
        pk = list(bp.parse_packets(io.StringIO(text)))
        self.assertEqual([p["mac"] for p in pk], ["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"])
        self.assertEqual((pk[0]["rssi"], pk[0]["channel"], pk[0]["addr_type"], pk[0]["freq"]), (-70, 38, "public", 2402))
        self.assertEqual(list(bp.parse_packets(io.StringIO(""))), [])

    def test_enrich_handles_bad_hex(self):
        p = {"raw": ["    AdvData: zz zz "], "mac": "X"}
        p = bp.enrich(p)
        self.assertEqual((p["name"], p["uuids"], p["company_ids"], p["fingerprint"]), (None, [], [], None))


class TestIntegration(unittest.TestCase):
    def test_fingerprint_comes_from_nova_ble_monitor_verbatim(self):
        self.assertIn("from nova_ble_monitor import (compute_ble_fingerprint", SRC)
        self.assertNotIn("def compute_ble_fingerprint", SRC)
        self.assertEqual(Path(bp.compute_ble_fingerprint.__code__.co_filename).name, "nova_ble_monitor.py")
        self.assertEqual(Path(bp.compute_cross_observer_fingerprint.__code__.co_filename).name, "nova_ble_monitor.py")
        p = bp.enrich({"raw": [f"    AdvData: {NAME_HOMEPOD}{APPLE_NEARBY}"], "mac": "M"})
        self.assertEqual(p["fingerprint"], bp.compute_ble_fingerprint("HomePod", [], [0x004C], None))
        self.assertIsNotNone(p["fingerprint"])
        self.assertEqual(p["apple_subtype"], "findmy"); self.assertEqual(p["findmy_state"], "owner_nearby")

    def test_observer_tag_defaults_to_host_phy(self):
        self.assertEqual(bp.OBSERVER, "test-phy")
        self.assertIn('f"{socket.gethostname().split(\'.\')[0].lower()}-phy"', SRC)

    def test_consensus_buffer_lets_the_modal_decode_win(self):
        good = _pkt_block("AA:BB:CC:DD:EE:01", NAME_HOMEPOD + APPLE_NEARBY, rssi=-60)
        bad = _pkt_block("AA:BB:CC:DD:EE:01", "08 09 47 41 52 42 41 47 45 ", rssi=-90)   # "GARBAGE" bit-error decode
        rc, cur, conn, proc, ev, po, out = _run_main(good * 3 + bad, clock=_flush_after(4))
        rows = ev.call_args[0][2]
        self.assertEqual(len(rows), 1)
        mac, name, rssi, batt, dtype, connected, meta, fp, observer = rows[0]
        self.assertEqual((mac, name, rssi, dtype, connected, observer), ("AA:BB:CC:DD:EE:01", "HomePod", -60, "ble_phy", False, "test-phy"))
        self.assertEqual(meta[1]["consensus"], 3); self.assertEqual(meta[1]["packets"], 4)
        self.assertEqual(meta[1]["company_ids"], ["0x004c"]); self.assertEqual(meta[1]["findmy_state"], "owner_nearby")
        self.assertFalse(meta[1]["single_packet"])


class TestFunctional(unittest.TestCase):
    def test_golden_path_flushes_consensus_rows_and_anonymous_rollup(self):
        stream = _pkt_block("AA:BB:CC:DD:EE:01", NAME_HOMEPOD) + _pkt_block("AA:BB:CC:DD:EE:02", NO_FIELDS, rssi=-80) \
                 + _pkt_block("AA:BB:CC:DD:EE:03", NO_FIELDS, rssi=-70) + _pkt_block("AA:BB:CC:DD:EE:04", UUID16_HR)
        rc, cur, conn, proc, ev, po, out = _run_main(stream, clock=_flush_after(4))
        self.assertEqual(rc, 0)
        self.assertTrue(conn.autocommit)
        self.assertEqual(ev.call_count, 1)
        self.assertIn("INSERT INTO telemetry.bluetooth (ts, device_mac, device_name, rssi, battery_pct, device_type, is_connected, metadata, fingerprint, observer) VALUES %s",
                      " ".join(ev.call_args[0][1].split()))
        anon = [p for s, p in zip(cur.sql, cur.params) if "ble_phy_anon" in str(p)]
        self.assertEqual(anon[0][0], "--:--:--:--:--:--"); self.assertEqual(anon[0][1], "anonymous x2")
        self.assertEqual(anon[0][2], -70)                                         # median of [-80, -70] (upper)
        self.assertEqual(anon[0][4][1]["count"], 2)
        self.assertEqual([r[0] for r in ev.call_args[0][2]], ["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:04"])
        self.assertIn("flushed 2 consensus devices (total 2) + 2 anonymous rolled up", out)
        self.assertIn("starting PHY capture as observer=test-phy", out)
        proc.terminate.assert_called_once(); self.assertTrue(conn.closed)
        self.assertTrue(out.rstrip().endswith("stopped"))

    def test_missing_radio_binary_returns_2_without_pg(self):
        with patch.object(bp, "UBERTOOTH", str(TMP / "absent")), patch.object(bp.psycopg2, "connect", MagicMock()) as pg, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(bp.main(), 2)
        pg.assert_not_called()
        self.assertIn("FATAL", out.getvalue())

    def test_shutdown_flag_stops_before_processing(self):
        rc, cur, conn, proc, ev, po, out = _run_main(_pkt_block("AA:BB:CC:DD:EE:01", NAME_HOMEPOD) * 5, shutdown=True)
        self.assertEqual(rc, 0)
        ev.assert_not_called(); proc.terminate.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_ble_phy_collector as m; print('IMPORT-OK', m.FLUSH_SECONDS)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "NOVA_BLE_OBSERVER": "frame-phy"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "IMPORT-OK 30")


if __name__ == "__main__":
    unittest.main()
