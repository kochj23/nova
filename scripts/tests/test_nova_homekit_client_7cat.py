#!/usr/bin/env python3
"""7-category tests for nova_homekit_client.py — the authenticated NovaHomeKit (:37433) client
introduced when NovaHomeKit 51e7a91 started requiring `Authorization: Bearer <token>` on every
request and POST-only state changes. Also pins that every :37433 client in scripts/ goes through it.

No production calls: the integration tests stand up a throwaway loopback HTTP server that mimics
the bridge's auth contract; Keychain/env/token-file lookups are patched. Written by Jordan Koch (via Claude).
"""
import importlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_homekit_client as hk  # noqa: E402

SRC = (SCRIPTS / "nova_homekit_client.py").read_text()
CLIENTS = ["nova_fp2_presence.py", "nova_homekit_sensors.py", "nova_homekit_outlets.py",
           "nova_eve_energy.py", "nova_homekit_battery_monitor.py", "nova_homekit_redis_bridge.py",
           "nova_automation_engine.py"]
TOKEN = "unit-test-token-not-a-secret"


class _Bridge(BaseHTTPRequestHandler):
    """Mimics NovaHomeKit's auth contract: loopback Host, Bearer token, POST-only power."""
    token = TOKEN
    hits = []
    fail_first = 0

    def log_message(self, *a):
        pass

    def _reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self):
        type(self).hits.append((self.command, self.path, self.headers.get("Authorization")))
        if type(self).fail_first > 0:
            type(self).fail_first -= 1
            return self._reply(503, {"error": "warming up"})
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return self._reply(403, {"error": "forbidden host"})
        if self.headers.get("Authorization") != f"Bearer {type(self).token}":
            return self._reply(401, {"error": "unauthorized"})
        if self.path.startswith("/api/accessories/power"):
            if self.command != "POST":
                return self._reply(404, {"error": "not found"})
            return self._reply(200, {"ok": True})
        if self.path.startswith("/api/accessories"):
            return self._reply(200, [{"name": "Aqara Presence Sensor FP2 3", "room": "Master Bedroom",
                                      "services": [{"characteristics": [
                                          {"type": "Occupancy Detected", "value": 1}]}]}])
        return self._reply(200, {"status": "ok"})

    do_GET = _handle
    do_POST = _handle


def _serve():
    srv = HTTPServer(("127.0.0.1", 0), _Bridge)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class _Base(unittest.TestCase):
    def setUp(self):
        hk._token_cache = None
        _Bridge.hits, _Bridge.fail_first, _Bridge.token = [], 0, TOKEN
        self.env = patch.dict(os.environ, {hk.TOKEN_ENV: TOKEN})
        self.env.start()
        self.sleep = patch.object(hk.time, "sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.env.stop()
        self.sleep.stop()
        hk._token_cache = None


# ── Security ─────────────────────────────────────────────────────────────────
class TestSecurity(_Base):
    def test_no_hardcoded_token_or_user_paths(self):
        for f in ["nova_homekit_client.py"] + CLIENTS:
            src = (SCRIPTS / f).read_text()
            self.assertNotRegex(src, r"Bearer [A-Za-z0-9_\-]{16,}", f)
        self.assertNotRegex(SRC, r"/Users/[a-z]", "no absolute user paths in the client")

    def test_token_never_logged(self):
        srv, base = _serve()
        try:
            with patch.object(hk, "_log") as log:
                _Bridge.token = "different"
                with self.assertRaises(urllib.error.HTTPError):
                    hk.request(base + "/api/status", retries=2)
            for call in log.call_args_list:
                self.assertNotIn(TOKEN, " ".join(map(str, call.args)))
        finally:
            srv.shutdown()

    def test_every_37433_client_sends_auth(self):
        for f in CLIENTS:
            src = (SCRIPTS / f).read_text()
            self.assertTrue("nova_homekit_client" in src, f"{f} does not use nova_homekit_client")

    def test_power_is_post_only(self):
        src = (SCRIPTS / "nova_automation_engine.py").read_text()
        block = src[src.index("def _hk_power"):src.index("async def rule_presence_devices")]
        self.assertIn('method="POST"', block)
        self.assertIn("_hk_auth()", block)

    def test_token_file_written_0600(self):
        with tempfile.TemporaryDirectory() as d:
            tf = Path(d) / "nova" / "novahomekit-token"
            with patch.object(hk, "TOKEN_FILE", tf), \
                 patch("subprocess.run") as run:
                hk._token_cache = None
                os.environ.pop(hk.TOKEN_ENV, None)
                with patch.object(hk, "load_token", side_effect=lambda refresh=False: ""):
                    state = hk.provision()
            self.assertEqual(state, "created")
            self.assertEqual(oct(tf.stat().st_mode & 0o777), "0o600")
            self.assertEqual(oct(tf.parent.stat().st_mode & 0o777), "0o700")
            args = run.call_args[0][0]
            self.assertEqual(args[:2], ["security", "add-generic-password"])
            self.assertIn(hk.TOKEN_SERVICE, args)


# ── Performance ──────────────────────────────────────────────────────────────
class TestPerformance(_Base):
    def test_token_cached_not_reloaded_per_request(self):
        with patch("nova_config._keychain") as kc:
            for _ in range(500):
                hk.auth_headers()
            kc.assert_not_called()          # env hit first, then cache

    def test_get_json_fast_on_loopback(self):
        srv, base = _serve()
        try:
            t = time.perf_counter()
            for _ in range(20):
                hk.get_json(base + "/api/accessories")
            self.assertLess(time.perf_counter() - t, 3.0)
        finally:
            srv.shutdown()


# ── Retry ────────────────────────────────────────────────────────────────────
class TestRetry(_Base):
    def test_retries_5xx_then_succeeds(self):
        srv, base = _serve()
        try:
            _Bridge.fail_first = 2
            self.assertEqual(hk.get_json(base + "/api/status", retries=3), {"status": "ok"})
            self.assertEqual(len(_Bridge.hits), 3)
        finally:
            srv.shutdown()

    def test_exhausted_raises_never_silent(self):
        with patch.object(hk.urllib.request, "urlopen",
                          side_effect=hk.urllib.error.URLError("refused")) as u, \
             patch.object(hk, "_log") as log:
            with self.assertRaises(hk.urllib.error.URLError):
                hk.request("/api/status", retries=3)
        self.assertEqual(u.call_count, 3)
        self.assertIn("failed after 3", log.call_args[0][0])

    def test_backoff_is_exponential(self):
        sleeps = []
        with patch.object(hk.time, "sleep", sleeps.append), \
             patch.object(hk.urllib.request, "urlopen", side_effect=TimeoutError("t")), \
             patch.object(hk, "_log"):
            with self.assertRaises(TimeoutError):
                hk.request("/api/status", retries=3, backoff=1.0)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_401_reloads_token_once(self):
        srv, base = _serve()
        try:
            hk._token_cache = "stale"
            self.assertEqual(hk.get_json(base + "/api/status"), {"status": "ok"})
            self.assertEqual([h[2] for h in _Bridge.hits], ["Bearer stale", f"Bearer {TOKEN}"])
        finally:
            srv.shutdown()

    def test_4xx_not_retried(self):
        srv, base = _serve()
        try:
            _Bridge.token = "rotated-elsewhere"
            with patch.object(hk, "_log"), self.assertRaises(urllib.error.HTTPError) as cm:
                hk.request(base + "/api/status", retries=3)
            self.assertEqual(cm.exception.code, 401)
            self.assertEqual(len(_Bridge.hits), 2)   # original + one reload, no blind retries
        finally:
            srv.shutdown()


# ── Unit ─────────────────────────────────────────────────────────────────────
class TestUnit(_Base):
    def test_token_precedence_env_keychain_file(self):
        self.assertEqual(hk.load_token(refresh=True), TOKEN)
        os.environ.pop(hk.TOKEN_ENV)
        with patch("nova_config._keychain", return_value="from-keychain"):
            self.assertEqual(hk.load_token(refresh=True), "from-keychain")
        with tempfile.TemporaryDirectory() as d:
            tf = Path(d) / "t"
            tf.write_text("from-file\n")
            with patch("nova_config._keychain", return_value=""), patch.object(hk, "TOKEN_FILE", tf):
                self.assertEqual(hk.load_token(refresh=True), "from-file")

    def test_no_token_means_no_auth_header(self):
        os.environ.pop(hk.TOKEN_ENV)
        with patch("nova_config._keychain", return_value=""), \
             patch.object(hk, "TOKEN_FILE", Path("/nonexistent/x")):
            hk.load_token(refresh=True)
            self.assertNotIn("Authorization", hk.auth_headers())

    def test_auth_header_shape(self):
        h = hk.auth_headers({"X-A": "1"})
        self.assertEqual(h["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(h["X-A"], "1")

    def test_url_building(self):
        self.assertEqual(hk._url("/api/status"), "http://127.0.0.1:37433/api/status")
        self.assertEqual(hk._url("api/status"), "http://127.0.0.1:37433/api/status")
        self.assertEqual(hk._url("http://x/y"), "http://x/y")


# ── Integration ──────────────────────────────────────────────────────────────
class TestIntegration(_Base):
    def test_fp2_poller_reads_through_authenticated_bridge(self):
        import nova_fp2_presence as fp2
        srv, base = _serve()
        try:
            with patch.object(fp2, "NOVAHOMEKIT_URL", base + "/api/accessories"):
                self.assertEqual(fp2.fetch_fp2_occupancy(), {"master_bedroom": True})
            self.assertEqual(_Bridge.hits[-1][2], f"Bearer {TOKEN}")
        finally:
            srv.shutdown()

    def test_unauthenticated_request_is_rejected(self):
        srv, base = _serve()
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib_request = hk.urllib.request
                urllib_request.urlopen(base + "/api/status", timeout=3)
            self.assertEqual(cm.exception.code, 401)
        finally:
            srv.shutdown()


# ── Functional ───────────────────────────────────────────────────────────────
class TestFunctional(_Base):
    def test_power_post_with_token_succeeds_get_404(self):
        srv, base = _serve()
        try:
            self.assertEqual(json.loads(hk.request(base + "/api/accessories/power?name=x&on=true",
                                                   method="POST")), {"ok": True})
            with patch.object(hk, "_log"), self.assertRaises(urllib.error.HTTPError) as cm:
                hk.request(base + "/api/accessories/power?name=x&on=true", method="GET", retries=1)
            self.assertEqual(cm.exception.code, 404)
        finally:
            srv.shutdown()

    def test_automation_engine_hk_power_posts_with_bearer(self):
        import nova_automation_engine as ae
        srv, base = _serve()
        try:
            with patch.object(ae, "NOVAHOMEKIT_POWER", base + "/api/accessories/power"), \
                 patch.object(ae, "_actuation_ok", return_value=True):
                self.assertTrue(ae._hk_power("Test Lamp", True))
            method, path, auth = _Bridge.hits[-1]
            self.assertEqual((method, auth), ("POST", f"Bearer {TOKEN}"))
            self.assertIn("name=Test%20Lamp", path)
        finally:
            srv.shutdown()


# ── Frame (smoke) ────────────────────────────────────────────────────────────
class TestFrame(unittest.TestCase):
    def test_imports_and_constants(self):
        m = importlib.reload(hk)
        self.assertEqual(m.HK_BASE, "http://127.0.0.1:37433")
        self.assertTrue(callable(m.get_json) and callable(m.provision))
        self.assertIn('if __name__ == "__main__":', SRC)

    def test_clients_import(self):
        for mod in ["nova_fp2_presence", "nova_homekit_outlets", "nova_eve_energy"]:
            importlib.import_module(mod)


if __name__ == "__main__":
    unittest.main()
