#!/usr/bin/env python3
"""Tests for nova_healthkit_receiver.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
The handler is driven in-process over BytesIO (no port is bound); HEALTH_DIR is a tempdir and the
memory server is mocked — no real health data is read or written."""
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_healthkit_receiver.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="healthkit-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("healthkit_receiver_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(Path, "home", classmethod(lambda c: TMP)):   # import mkdirs HEALTH_DIR
        spec.loader.exec_module(mod)
    return mod


hk = _load()
assert str(hk.HEALTH_DIR).startswith(str(TMP))
# module-level stub: no memory-server writes from any test
hk.urllib = types.SimpleNamespace(request=types.SimpleNamespace(
    Request=hk.urllib.request.Request, urlopen=MagicMock(side_effect=OSError("offline"))))


def _req(method, body=None, path="/health"):
    """Drive HealthHandler without a socket; return (status, json-or-text)."""
    raw = json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or b"")
    h = hk.HealthHandler.__new__(hk.HealthHandler)
    h.rfile, h.wfile = io.BytesIO(raw), io.BytesIO()
    h.headers = {"Content-Length": str(len(raw))}
    h.path, h.command, h.request_version = path, method, "HTTP/1.1"
    h.requestline = f"{method} {path} HTTP/1.1"
    h.client_address = ("192.0.2.1", 0)
    getattr(h, f"do_{method}")()
    out = h.wfile.getvalue().decode()
    status = int(out.split(" ", 2)[1])
    payload = out.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in out else ""
    return status, payload


def _clean():
    for f in hk.HEALTH_DIR.glob("*.json"):
        f.unlink()
    hk.urllib.request.urlopen.reset_mock()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_date_path_traversal_rejected(self):
        # regression for the fix: body "date" is a filename and used to accept ../
        _clean()
        for bad in ("../../escape", "2026-01-01/../../x", 20260101, "2026-1-1"):
            status, _ = _req("POST", {"date": bad, "steps": 1})
            self.assertEqual(status, 400, bad)
        self.assertFalse((TMP / ".openclaw" / "escape.json").exists())
        self.assertEqual(list(hk.HEALTH_DIR.glob("*.json")), [])

    def test_files_private_and_tagged_local_only(self):
        _clean()
        _req("POST", {"date": "2026-01-02", "steps": 100})
        mode = stat.S_IMODE((hk.HEALTH_DIR / "2026-01-02.json").stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(stat.S_IMODE(hk.HEALTH_DIR.stat().st_mode), 0o700)
        body = json.loads(hk.urllib.request.urlopen.call_args[0][0].data)
        self.assertEqual(body["metadata"]["privacy"], "local-only")


class TestPerformance(unittest.TestCase):
    def test_large_payload_fast(self):
        _clean()
        big = {"date": "2026-01-03", **{f"metric_{i}": i + 1 for i in range(10_000)}}
        t0 = time.perf_counter()
        status, _ = _req("POST", big)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(status, 200)


class TestRetry(unittest.TestCase):
    def test_memory_failure_still_returns_ok(self):
        # RETRY GAP: do_POST memory write — one attempt; failure is logged, data is still on disk and 200 returned
        _clean()
        status, payload = _req("POST", {"date": "2026-01-04", "hr": 60})
        self.assertEqual(status, 200)
        self.assertEqual(hk.urllib.request.urlopen.call_count, 1)
        self.assertTrue((hk.HEALTH_DIR / "2026-01-04.json").exists())


class TestUnit(unittest.TestCase):
    def test_bad_json_and_unknown_paths(self):
        self.assertEqual(_req("POST", b"{not json")[0], 400)
        self.assertEqual(_req("POST", {"a": 1}, path="/other")[0], 404)
        self.assertEqual(_req("GET", path="/nope")[0], 404)

    def test_get_without_data(self):
        _clean()
        self.assertEqual(_req("GET"), (200, '{"status":"no data yet"}'))

    def test_zero_and_meta_keys_excluded_from_summary(self):
        _clean()
        _req("POST", {"date": "2026-01-05", "steps": 0, "hr": 61, "sample_count": 9})
        text = json.loads(hk.urllib.request.urlopen.call_args[0][0].data)["text"]
        self.assertEqual(text, "HealthKit data for 2026-01-05: hr=61")


class TestIntegration(unittest.TestCase):
    def test_history_merges_without_touching_latest(self):
        _clean()
        _req("POST", {"date": "2026-01-06", "steps": 500, "hr": 70})
        latest_before = (hk.HEALTH_DIR / "latest.json").read_text()
        _req("POST", {"date": "2026-01-06", "source": "healthkit_history", "sleep_h": 7, "hr": 0})
        merged = json.loads((hk.HEALTH_DIR / "2026-01-06.json").read_text())
        self.assertEqual((merged["steps"], merged["hr"], merged["sleep_h"]), (500, 70, 7))
        self.assertEqual((hk.HEALTH_DIR / "latest.json").read_text(), latest_before)
        meta = json.loads(hk.urllib.request.urlopen.call_args[0][0].data)["metadata"]
        self.assertEqual(meta["origin"], "healthkit_history")


class TestFunctional(unittest.TestCase):
    def test_live_post_then_get_roundtrip(self):
        _clean()
        status, payload = _req("POST", {"date": "2026-01-07", "steps": 1234})
        self.assertEqual((status, json.loads(payload)), (200, {"status": "ok", "date": "2026-01-07"}))
        status, payload = _req("GET")
        self.assertEqual(json.loads(payload)["steps"], 1234)
        req = hk.urllib.request.urlopen.call_args[0][0]
        self.assertTrue(req.full_url.endswith("/remember?async=1"))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help/--selftest: main() binds :37450 and serves forever, so import is the smoke test
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import importlib.util as u, sys; sp=u.spec_from_file_location('h', sys.argv[1]);"
                "m=u.module_from_spec(sp); sp.loader.exec_module(m); print('ok')")
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
