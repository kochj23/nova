#!/usr/bin/env python3
"""Tests for nova_hue_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_hue_poller.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("hue_poller", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hp = _load()
LIGHTS = {"1": {"name": "Desk", "state": {"on": True, "bri": 254, "reachable": True}},
          "2": {"name": "Lamp", "state": {"on": False, "bri": 127, "reachable": False}}}
GROUPS = {"10": {"type": "Room", "name": "Office", "lights": ["1"], "state": {"all_on": True, "any_on": True}},
          "11": {"type": "Zone", "name": "Downstairs", "lights": ["1", "2"]}}


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _urlopen_ok(url, timeout=None):
    return _Resp(json.dumps(LIGHTS if url.endswith("/lights") else GROUPS).encode())


def _get(path):
    """Drive HueHandler.do_GET without a socket; returns (status, parsed body)."""
    h = hp.HueHandler.__new__(hp.HueHandler)
    h.path = path; h.wfile = io.BytesIO(); h.status = None
    h.send_response = lambda code: setattr(h, "status", code)
    h.send_header = lambda *a: None; h.end_headers = lambda: None
    h.do_GET()
    return h.status, json.loads(h.wfile.getvalue())


def _set_state(lights, groups, last=None):
    hp._lights, hp._groups, hp._last_poll = lights, groups, last if last is not None else time.time()


class _Cur:
    def __init__(self): self.sql = []
    def execute(self, sql, params=None): self.sql.append((sql, params))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_token_from_keychain(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('"-s", "nova-hue-api-token"', SRC)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))
        cur = _Cur(); conn = MagicMock(); conn.cursor.return_value = cur
        with patch("psycopg2.connect", return_value=conn):
            hp.store_to_pg({"x": {"name": "x'); --injected", "state": {}}}, {})
        sql, params = cur.sql[-1]
        self.assertNotIn("--injected", sql)
        self.assertEqual(params[1], "x'); --injected")

    def test_no_write_endpoints(self):
        self.assertNotIn("def do_POST", SRC)
        self.assertNotIn("def do_PUT", SRC)
        self.assertNotIn("method=\"PUT\"", SRC)       # poller never changes a light


class TestPerformance(unittest.TestCase):
    def test_lights_endpoint_10k_lights(self):
        lights = {str(i): {"name": f"L{i}", "state": {"on": i % 2 == 0, "bri": i % 255}} for i in range(10_000)}
        _set_state(lights, {"r": {"type": "Room", "name": "Big", "lights": list(lights)}})
        t0 = time.perf_counter()
        code, body = _get("/lights")
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual((code, len(body)), (200, 10_000))


class TestRetry(unittest.TestCase):
    def test_bridge_failure_is_one_shot_and_keeps_old_state(self):
        # RETRY GAP: poll_bridge/urlopen — one attempt per cycle; failure logged, cached state untouched
        _set_state({"old": {}}, {}, last=123.0)
        calls = []

        def boom(url, timeout=None):
            calls.append(url); raise OSError("bridge unreachable")
        with patch.object(hp, "get_token", return_value="tok"), patch.object(hp.urllib.request, "urlopen", boom), \
             redirect_stdout(io.StringIO()) as out:
            hp.poll_bridge()
        self.assertEqual(len(calls), 1)
        self.assertEqual((hp._lights, hp._last_poll), ({"old": {}}, 123.0))
        self.assertIn("Poll failed", out.getvalue())

    def test_pg_failure_is_swallowed(self):
        with patch("psycopg2.connect", side_effect=OSError("pg down")), redirect_stdout(io.StringIO()) as out:
            hp.store_to_pg(LIGHTS, GROUPS)
        self.assertIn("PG store failed", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_no_token_skips_poll(self):
        with patch.object(hp, "get_token", return_value=""), patch.object(hp.urllib.request, "urlopen") as u:
            hp.poll_bridge()
        u.assert_not_called()

    def test_status_and_404(self):
        _set_state(LIGHTS, GROUPS)
        code, body = _get("/health")
        self.assertEqual((code, body["lights_total"], body["lights_on"], body["rooms"]), (200, 2, 1, 1))
        self.assertEqual(_get("/nope"), (404, {"error": "not found"}))
        _set_state({}, {}, last=0)
        self.assertIsNone(_get("/status")[1]["age_seconds"])

    def test_lights_rooms_and_on(self):
        _set_state(LIGHTS, GROUPS)
        _, lights = _get("/lights")
        self.assertEqual(lights[0], {"id": "1", "name": "Desk", "room": "Office", "on": True, "brightness": 100, "reachable": True})
        self.assertEqual(lights[1]["room"], "Unknown")
        self.assertEqual(_get("/rooms")[1], [{"name": "Office", "all_on": True, "any_on": True, "lights": 1}])
        self.assertEqual(_get("/lights/on")[1], ["Desk"])


class TestIntegration(unittest.TestCase):
    def test_poll_then_store_maps_rooms(self):
        cur = _Cur(); conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(hp, "get_token", return_value="tok"), patch.object(hp.urllib.request, "urlopen", _urlopen_ok), \
             patch("psycopg2.connect", return_value=conn) as pc:
            hp.poll_bridge()
        self.assertEqual(pc.call_args.args[0], hp.PG_DSN)
        rows = [p for s, p in cur.sql if p]
        self.assertEqual(rows, [("1", "Desk", "Office", True, 254, True), ("2", "Lamp", None, False, 127, False)])
        self.assertEqual(hp._lights, LIGHTS)
        conn.close.assert_called_once()


class TestFunctional(unittest.TestCase):
    def test_main_starts_loop_thread_and_server_without_binding(self):
        server = MagicMock()
        with patch.object(hp, "poll_bridge"), patch.object(hp, "Thread") as th, \
             patch.object(hp, "HTTPServer", return_value=server) as hs, redirect_stdout(io.StringIO()) as out:
            hp.main()
        hs.assert_called_once_with(("0.0.0.0", hp.PORT), hp.HueHandler)
        server.serve_forever.assert_called_once()
        th.assert_called_once_with(target=hp.poll_loop, daemon=True)
        self.assertIn("Initial poll", out.getvalue())

    def test_token_uses_keychain_command(self):
        with patch.object(hp.subprocess, "run", return_value=MagicMock(stdout="abc\n")) as run:
            self.assertEqual(hp.get_token(), "abc")
        self.assertEqual(run.call_args.args[0][:2], ["security", "find-generic-password"])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_hue_poller as h; print(h.PORT)"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "37476")


if __name__ == "__main__":
    unittest.main()
