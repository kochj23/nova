#!/usr/bin/env python3
"""Tests for zwave_include_devices.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import websockets
import websockets.exceptions  # the package resolves submodules lazily; the bare attribute raises until imported

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "zwave_include_devices.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zw = _load("zw", SCRIPT)
CLOSED = websockets.exceptions.ConnectionClosedOK(None, None)


class _WS:
    """Z-Wave JS UI socket stand-in. `script` maps device number -> list of events pushed after startInclusion."""
    def __init__(self, script=None, ack_start=None, silent=False):
        self.script = script or {}; self.ack_start = ack_start; self.silent = silent
        self.inbox = asyncio.Queue(); self.sent = []; self.device = 0
        self.inbox.put_nowait('0{"sid":"x"}'); self.inbox.put_nowait("40{}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def recv(self):
        msg = await self.inbox.get()
        if isinstance(msg, Exception):
            raise msg
        return msg

    async def send(self, frame):
        self.sent.append(frame)
        if self.silent or not frame.startswith("42"):
            return
        m = re.match(r"42(\d+)(\[.*\])$", frame, re.S)
        ack_id, (event, data) = m.group(1), json.loads(m.group(2))
        if event == "ZWAVE_API" and data["api"] == "startInclusion":
            self.device += 1
            payload = self.ack_start if self.ack_start is not None else [{"success": True}]
            await self.inbox.put(f"43{ack_id}{json.dumps(payload)}")
            for ev in self.script.get(self.device, []):
                await self.inbox.put("42" + json.dumps(ev))
        else:
            await self.inbox.put(f"43{ack_id}[]")

    def api_calls(self):
        return [json.loads(f[f.index("["):])[1]["api"] for f in self.sent if '"ZWAVE_API"' in f]


_REAL_SLEEP = asyncio.sleep  # captured before patching: zw.asyncio IS the global module, so a patched sleep would call itself


async def _fast_sleep(_secs):
    await _REAL_SLEEP(0)


def _run_main(ws, max_devices=2, timeout=1):
    out = io.StringIO()
    with patch.object(zw.websockets, "connect", lambda *a, **k: ws), patch.object(zw, "MAX_DEVICES", max_devices), \
         patch.object(zw, "TIMEOUT_SECS", timeout), patch.object(zw.asyncio, "sleep", _fast_sleep), \
         patch.object(zw, "_pending_acks", {}), redirect_stdout(out):
        asyncio.run(zw.main())
    return out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_localhost_only(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertTrue(zw.ZWUI_WS.startswith("ws://localhost:8091/"))
        self.assertNotIn("subprocess", SRC); self.assertNotIn("eval(", SRC)

    def test_frames_are_json_encoded_so_payloads_cannot_forge_a_second_event(self):
        async def go():
            ws = _WS(silent=True)
            with patch.object(zw, "_pending_acks", {}):
                await zw.emit_with_ack(ws, 'SUBSCRIBE"],["EVIL', {"x": '"]42["y'}, timeout=0.01)
            return ws.sent[0]
        frame = asyncio.run(go())
        self.assertRegex(frame, r"^42\d+\[")
        self.assertEqual(json.loads(frame[frame.index("["):]), ['SUBSCRIBE"],["EVIL', {"x": '"]42["y'}])

    def test_inclusion_strategy_is_the_insecure_one_the_script_documents(self):
        ws = _WS(script={1: [["INCLUSION_ABORTED", {}]]})
        _run_main(ws)
        start = next(json.loads(f[f.index("["):]) for f in ws.sent if "startInclusion" in f)
        self.assertEqual(start[1]["args"][0], 2)                    # InclusionStrategy.Insecure
        self.assertIn("Insecure=2", SRC)


class TestPerformance(unittest.TestCase):
    def test_recv_loop_routes_10k_acks_and_events_quickly(self):
        async def go():
            ws = _WS(silent=True); q = asyncio.Queue(); pend = {}
            futs = [asyncio.get_event_loop().create_future() for _ in range(5_000)]  # recv_loop pops from pend, so keep our own refs
            with patch.object(zw, "_pending_acks", pend):
                for i, fut in enumerate(futs, start=1):
                    pend[i] = fut
                    await ws.inbox.put(f"43{i}[{i}]")
                    await ws.inbox.put(f'42["NODE_FOUND",{{"n":{i}}}]')
                await ws.inbox.put(CLOSED)
                t0 = time.perf_counter()
                await zw.recv_loop(ws, q)
                dt = time.perf_counter() - t0
            return dt, sum(1 for f in futs if f.done()), q.qsize(), len(pend)
        dt, acked, events, left = asyncio.run(go())
        self.assertLess(dt, 2.0)
        self.assertEqual((acked, events, left), (5_000, 5_000, 0))


class TestRetry(unittest.TestCase):
    def test_emit_without_ack_times_out_to_none_and_is_not_resent(self):
        # RETRY GAP: emit_with_ack — a single send; no ack within `timeout` returns None and clears the pending slot
        async def go():
            ws = _WS(silent=True)
            with patch.object(zw, "_pending_acks", {}) as pend:
                r = await zw.emit_with_ack(ws, "SUBSCRIBE", {}, timeout=0.05)
                return r, len(ws.sent), dict(pend)
        r, sent, pend = asyncio.run(go())
        self.assertIsNone(r); self.assertEqual(sent, 1); self.assertEqual(pend, {})

    def test_connect_failure_escapes_main(self):
        # RETRY GAP: main/websockets.connect — one attempt; the OSError propagates (the __main__ guard prints and exits 1)
        def boom(*a, **k):
            raise OSError("connection refused")
        with patch.object(zw.websockets, "connect", boom), redirect_stdout(io.StringIO()), self.assertRaises(OSError):
            asyncio.run(zw.main())

    def test_no_device_in_time_stops_inclusion_cleanly(self):
        ws = _WS(script={1: []})
        out = _run_main(ws, timeout=0.05)
        self.assertIn("No device responded in 0.05s. Stopping.", out)
        self.assertEqual(ws.api_calls(), ["startInclusion", "stopInclusion", "stopInclusion"])


class TestUnit(unittest.TestCase):
    def test_ack_ids_are_monotonic(self):
        a, b = zw.next_ack_id(), zw.next_ack_id()
        self.assertEqual(b, a + 1)

    def test_recv_loop_pongs_pings_and_parses_acks(self):
        async def go():
            ws = _WS(silent=True); q = asyncio.Queue(); fut = asyncio.get_event_loop().create_future(); empty = asyncio.get_event_loop().create_future()
            with patch.object(zw, "_pending_acks", {7: fut, 8: empty}):
                for m in ("2", '437[{"ok":true}]', "438", '42["NODE_ADDED",{"id":3}]', "42[]", "99junk", CLOSED):
                    await ws.inbox.put(m)
                await zw.recv_loop(ws, q)
            return ws.sent, fut.result(), empty.result(), q.qsize(), q.get_nowait()
        sent, ack, none_ack, n, ev = asyncio.run(go())
        self.assertEqual(sent, ["3"])
        self.assertEqual(ack, [{"ok": True}]); self.assertIsNone(none_ack)
        self.assertEqual((n, ev), (1, ["NODE_ADDED", {"id": 3}]))

    def test_late_ack_for_an_unknown_id_is_ignored(self):
        async def go():
            ws = _WS(silent=True); q = asyncio.Queue()
            with patch.object(zw, "_pending_acks", {}):
                await ws.inbox.put("4399[1]"); await ws.inbox.put(CLOSED)
                await zw.recv_loop(ws, q)
            return q.qsize()
        self.assertEqual(asyncio.run(go()), 0)


class TestIntegration(unittest.TestCase):
    def test_emit_and_recv_loop_complete_a_socketio_round_trip(self):
        async def go():
            ws = _WS(); q = asyncio.Queue()
            with patch.object(zw, "_pending_acks", {}):
                await ws.recv(); await ws.recv()
                task = asyncio.create_task(zw.recv_loop(ws, q))
                r1 = await zw.emit_with_ack(ws, "SUBSCRIBE", {"channels": ["nodes"]}, timeout=1)
                r2 = await zw.emit_with_ack(ws, "ZWAVE_API", {"api": "startInclusion", "args": []}, timeout=1)
                task.cancel()
            return r1, r2, ws.sent
        r1, r2, sent = asyncio.run(go())
        self.assertEqual((r1, r2), ([], [{"success": True}]))
        self.assertTrue(all(re.match(r"^42\d+\[", f) for f in sent))

    def test_security_handshake_events_are_answered_with_the_matching_api(self):
        ws = _WS(script={1: [["GRANT_SECURITY_CLASSES", {"requested": {"s2": True}}], ["VALIDATE_DSK", {"dsk": "12345"}],
                            ["CONTROLLER_CMD", "inclusion started"], ["NODE_ADDED", {"nodeId": 9}]],
                        2: [["INCLUSION_ABORTED", {}]]})
        out = _run_main(ws)
        self.assertEqual(ws.api_calls(), ["startInclusion", "grantSecurityClasses", "validateDSK", "startInclusion", "stopInclusion"])
        grant = next(json.loads(f[f.index("["):])[1] for f in ws.sent if "grantSecurityClasses" in f)
        self.assertEqual(grant["args"], [{"s2": True}])
        self.assertIn("Controller confirmed: inclusion active", out)
        self.assertIn("INCLUDED! Node 9", out)


class TestFunctional(unittest.TestCase):
    def test_golden_path_includes_a_device_then_stops_on_abort(self):
        ws = _WS(script={1: [["NODE_FOUND", {}], ["NODE_ADDED", {"id": 7, "manufacturer": "Zooz", "productDescription": "ZEN32"}]],
                        2: [["INCLUSION_ABORTED", {}]]})
        out = _run_main(ws)
        self.assertIn("Device #1: Starting inclusion", out); self.assertIn("Inclusion mode ACTIVE", out)
        self.assertIn("device found! Interviewing", out)
        self.assertIn("INCLUDED! Node 7: Zooz ZEN32", out)
        self.assertIn("Device #2", out); self.assertIn("Inclusion aborted", out)
        self.assertTrue(out.rstrip().endswith("Done."))
        self.assertEqual(ws.sent[0], "40")
        self.assertIn('"SUBSCRIBE"', ws.sent[1]); self.assertIn('"channels": ["nodes", "controller"]', ws.sent[1])
        self.assertEqual(ws.api_calls()[-1], "stopInclusion")

    def test_error_path_controller_refuses_inclusion(self):
        ws = _WS(ack_start=[{"success": False, "message": "controller busy"}])
        out = _run_main(ws)
        self.assertIn("Error: controller busy", out)
        self.assertEqual(ws.api_calls(), ["startInclusion", "stopInclusion"])
        self.assertNotIn("PRESS THE BUTTON", out)


class TestFrame(unittest.TestCase):
    def test_import_is_silent_and_main_is_guarded(self):
        # no --help; running the script dials the Z-Wave JS UI socket, so the frame check is the import smoke
        self.assertIn('if __name__ == "__main__":\n    try:\n        asyncio.run(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import zwave_include_devices"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
