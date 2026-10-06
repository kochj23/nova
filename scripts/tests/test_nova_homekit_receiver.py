#!/usr/bin/env python3
"""Tests for nova_homekit_receiver.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_homekit_receiver.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="homekit-rx-test-"))
try:
    import psycopg2  # noqa: F401
except ImportError:
    pass


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hr = _load("homekit_rx_under_test", SCRIPT)
hr.DATA_FILE = TMP / "state" / "homekit_accessories.json"   # never touch ~/.openclaw/workspace
hr.LOG_FILE = TMP / "logs" / "homekit_receiver.log"         # never touch ~/.openclaw/logs


class _Cur:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))


class _Conn:
    def __init__(self):
        self.cur = _Cur(); self.autocommit = False; self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _handle(method, path, body=b"", connect=None):
    """Drive the handler offline: no socket, no server — a bare instance with BytesIO pipes."""
    h = hr.Handler.__new__(hr.Handler)
    h.path, h.command, h.request_version, h.requestline = path, method, "HTTP/1.1", f"{method} {path} HTTP/1.1"
    h.client_address, h.close_connection = ("127.0.0.1", 1), True
    h.headers = {"Content-Length": str(len(body))}
    h.rfile, h.wfile = io.BytesIO(body), io.BytesIO()
    pg = types.SimpleNamespace(connect=connect or MagicMock(return_value=_Conn()))
    with patch.object(hr, "psycopg2", pg), redirect_stdout(io.StringIO()) as out:
        (h.do_POST if method == "POST" else h.do_GET)()
    raw = h.wfile.getvalue().decode()
    head, _, payload = raw.partition("\r\n\r\n")
    code = int(head.split(" ")[1])
    return code, json.loads(payload), head, out.getvalue(), pg


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", hr.DB_DSN)

    def test_scene_insert_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"home_scene_activations"})
        evil = "Bedtime'); DROP TABLE home_scene_activations; --"
        conn = _Conn()
        code, resp, _, _, _ = _handle("POST", "/homekit/scene", json.dumps({"scene": evil}).encode(), connect=MagicMock(return_value=conn))
        self.assertEqual(code, 200)
        sql, params = conn.cur.sql[0]
        self.assertEqual(sql, "INSERT INTO home_scene_activations (scene_name) VALUES (%s)")
        self.assertEqual(params, (evil,))

    def test_malformed_json_is_a_400_not_a_crash(self):
        code, resp, _, out, pg = _handle("POST", "/homekit/update", b"{not json")
        self.assertEqual(code, 400); self.assertIn("error", resp)
        self.assertIn("Error:", out)
        code, resp, _, _, _ = _handle("POST", "/homekit/scene", b"[]")
        self.assertEqual(code, 400)
        pg.connect.assert_not_called()

    def test_unknown_routes_are_404(self):
        self.assertEqual(_handle("POST", "/etc/passwd")[0], 404)
        self.assertEqual(_handle("GET", "/../../x")[0], 404)


class TestPerformance(unittest.TestCase):
    def test_10k_accessory_update_persists_under_2s(self):
        body = json.dumps([{"name": f"acc{i}", "room": "r", "value": i} for i in range(10_000)]).encode()
        t0 = time.perf_counter()
        code, resp, _, _, _ = _handle("POST", "/homekit/update", body)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual((code, resp["count"]), (200, 10_000))
        self.assertEqual(len(json.loads(hr.DATA_FILE.read_text())), 10_000)

    def test_status_endpoint_1k_calls_under_1s(self):
        t0 = time.perf_counter()
        for _ in range(1_000):
            _handle("GET", "/health")
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_scene_pg_failure_is_one_shot_and_fails_open_as_400(self):
        # RETRY GAP: do_POST(/homekit/scene)/psycopg2.connect — one attempt; the Shortcut gets a 400 with the
        # error text and the receiver keeps serving (no exception escapes the handler).
        connect = MagicMock(side_effect=RuntimeError("pg down"))
        code, resp, _, out, _ = _handle("POST", "/homekit/scene", b'{"scene": "Bedtime"}', connect=connect)
        self.assertEqual(code, 400); self.assertEqual(resp, {"error": "pg down"})
        self.assertEqual(connect.call_count, 1)
        self.assertIn("Scene error: pg down", out)

    def test_unwritable_log_never_breaks_a_request(self):
        real = hr.LOG_FILE
        hr.LOG_FILE = Path("/dev/null/impossible/x.log")
        try:
            code, resp, _, _, _ = _handle("POST", "/homekit/update", b"[]")
        finally:
            hr.LOG_FILE = real
        self.assertEqual(code, 200)


class TestUnit(unittest.TestCase):
    def test_log_writes_the_redirected_file(self):
        with redirect_stdout(io.StringIO()) as out:
            hr.log("hello")
        self.assertIn("[homekit-rx ", out.getvalue()); self.assertIn("] hello", out.getvalue())
        self.assertTrue(hr.LOG_FILE.read_text().strip().endswith("] hello"))
        self.assertTrue(str(hr.LOG_FILE).startswith(str(TMP)))

    def test_scene_requires_a_non_blank_name(self):
        for body in (b"{}", b'{"scene": ""}', b'{"scene": "   "}', b'{"scene": null}'):
            code, resp, _, _, pg = _handle("POST", "/homekit/scene", body)
            self.assertEqual((code, resp), (400, {"error": "missing 'scene'"}), body)
            pg.connect.assert_not_called()

    def test_responses_are_json_with_length_and_cors(self):
        code, resp, head, _, _ = _handle("GET", "/homekit/status")
        self.assertIn("Content-Type: application/json", head)
        self.assertIn("Access-Control-Allow-Origin: *", head)
        self.assertIn(f"Content-Length: {len(json.dumps(resp))}", head)
        self.assertEqual(resp["status"], "ok")


class TestIntegration(unittest.TestCase):
    def test_update_then_get_roundtrips_through_cache_and_disk(self):
        data = [{"name": "Lamp", "on": True}, {"name": "Fan", "on": False}]
        code, resp, _, out, _ = _handle("POST", "/homekit/update", json.dumps(data).encode())
        self.assertEqual((code, resp), (200, {"status": "ok", "count": 2}))
        self.assertIn("Received 2 accessories", out)
        self.assertEqual(json.loads(hr.DATA_FILE.read_text()), data)
        self.assertEqual(_handle("GET", "/homekit/accessories")[1], data)
        st = _handle("GET", "/homekit/status")[1]
        self.assertEqual(st["accessories"], 2); self.assertIsInstance(st["age_seconds"], int)
        self.assertGreater(st["last_update"], 0)

    def test_scene_write_uses_autocommit_and_closes(self):
        conn = _Conn()
        code, resp, _, out, pg = _handle("POST", "/homekit/scene", b'{"scene": " Movie Night "}', connect=MagicMock(return_value=conn))
        self.assertEqual((code, resp), (200, {"status": "ok", "scene": "Movie Night"}))
        pg.connect.assert_called_once_with(hr.DB_DSN)
        self.assertTrue(conn.autocommit); self.assertTrue(conn.closed)
        self.assertEqual(conn.cur.sql[0][1], ("Movie Night",))
        self.assertIn("Scene activated: Movie Night", out)


class TestFunctional(unittest.TestCase):
    def test_main_loads_cache_then_serves_forever(self):
        hr.DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        hr.DATA_FILE.write_text(json.dumps([{"name": "Cached"}]))
        server = MagicMock()
        with patch.object(hr, "HTTPServer", MagicMock(return_value=server)) as srv, redirect_stdout(io.StringIO()) as out:
            hr.main()
        srv.assert_called_once_with(("0.0.0.0", hr.PORT), hr.Handler)
        server.serve_forever.assert_called_once()
        self.assertEqual(hr._cached_data, [{"name": "Cached"}])
        self.assertIn("Loaded 1 cached accessories", out.getvalue())
        self.assertIn(f"Listening on 0.0.0.0:{hr.PORT}", out.getvalue())

    def test_main_tolerates_a_corrupt_cache(self):
        hr.DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        hr.DATA_FILE.write_text("{corrupt")
        with patch.object(hr, "HTTPServer", MagicMock(return_value=MagicMock())), redirect_stdout(io.StringIO()) as out:
            hr.main()
        self.assertNotIn("Loaded", out.getvalue())
        self.assertIn("Listening", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_binds_the_port(self):
        # no argparse: --help would bind 0.0.0.0:37432 and serve forever, so the smoke is an import
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_homekit_receiver"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
