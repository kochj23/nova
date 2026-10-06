#!/usr/bin/env python3
"""Tests for nova_camera_look.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import base64
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_camera_look.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="camera-look-test-"))

import nova_logger            # noqa: E402  (shared log sink, redirected to TMP for this module)
import nova_protect_monitor   # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cl = _load("camera_look_under_test", SCRIPT)

_PATCHES = [
    patch("urllib.request.urlopen", MagicMock(side_effect=AssertionError("offline: urlopen must be stubbed"))),
    patch.object(nova_protect_monitor, "_get_password", MagicMock(return_value="")),   # never shells out to Keychain
    patch.object(nova_logger, "LOG_DIR", TMP),
    patch.object(nova_logger, "LOG_FILE", TMP / "nova.jsonl"),
    patch.object(cl, "SNAPSHOT_DIR", TMP / "snaps"),
]


def setUpModule():
    for p in _PATCHES:
        p.start()


def tearDownModule():
    for p in reversed(_PATCHES):
        p.stop()


def _cam(name, cid=None, state="CONNECTED"):
    return {"id": cid or (name.lower().replace(" ", "-") + "-0123456789"), "name": name, "state": state}


CAMS = [_cam("Patio"), _cam("Front Door"), _cam("Interior Living Room"), _cam("Driveway", state="DISCONNECTED"), _cam("Back Yard")]


class _Client:
    """ProtectClient stand-in: login result, bootstrap payload, snapshot writer."""
    def __init__(self, login=True, cameras=CAMS, snapshot=True):
        self._login, self._cams, self._snap = login, cameras, snapshot
        self.snapshots = []

    def login(self):
        return self._login

    def get_bootstrap(self):
        return {"cameras": self._cams}

    def get_snapshot(self, camera_id, output_path):
        self.snapshots.append(camera_id)
        if self._snap:
            Path(output_path).write_bytes(b"\xff\xd8jpegbytes")
        return self._snap


def _main(argv, client=None):
    out = io.StringIO()
    with patch.object(cl, "ProtectClient", lambda: client or _Client()), patch.object(sys, "argv", ["nova_camera_look.py", *argv]), \
         redirect_stdout(out):
        try:
            cl.main()
            code = 0
        except SystemExit as e:
            code = e.code
    return code, out.getvalue()


def _vision(text="A cat on the patio."):
    resp = MagicMock(); resp.read.return_value = json.dumps({"response": f"  {text}\n"}).encode()
    return patch.object(cl.urllib.request, "urlopen", return_value=resp)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", SRC.lower())                   # auth lives in ProtectClient (Keychain), not here

    def test_interior_cameras_are_never_listed_matched_or_snapshotted(self):
        cams = cl.get_cameras(_Client())
        self.assertEqual({c["name"] for c in cams}, {"Patio", "Front Door", "Back Yard"})
        self.assertIsNone(cl.fuzzy_match("interior", cams))
        self.assertIsNone(cl.fuzzy_match("living room", cams))
        client = _Client()
        code, out = _main(["--all-exterior"], client)
        self.assertNotIn("Interior", out)
        self.assertFalse(any(cid.startswith("interior") for cid in client.snapshots))
        self.assertIn("STRICT: Only non-Interior cameras", SRC)

    def test_vision_request_is_local_and_the_image_never_leaves_the_box(self):
        self.assertTrue(cl.OLLAMA_URL.startswith("http://127.0.0.1:"))
        self.assertNotIn("slack", SRC.lower())
        self.assertNotIn("psycopg2", SRC)


class TestPerformance(unittest.TestCase):
    def test_filter_and_fuzzy_match_fast_on_10k_cameras(self):
        boot = {"cameras": [_cam(f"Cam {i} {'Interior' if i % 3 else 'Exterior'} zone", cid=f"c{i:06d}xxxxxxxx") for i in range(10_000)]}
        client = MagicMock(); client.get_bootstrap.return_value = boot
        t0 = time.perf_counter()
        cams = cl.get_cameras(client)
        for _ in range(20):
            self.assertIsNone(cl.fuzzy_match("nothing-matches-this", cams))   # worst case: both passes over every camera
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(cams), 10_000)       # "Interior" mid-name is not the prefix; all stay listed


class TestRetry(unittest.TestCase):
    def test_vision_failure_fails_open_with_a_placeholder(self):
        # RETRY GAP: describe_image — one urlopen attempt; any error becomes a "(Vision analysis failed: ...)" string
        img = TMP / "img.jpg"; img.write_bytes(b"\xff\xd8")
        with patch.object(cl.urllib.request, "urlopen", side_effect=OSError("ollama down")) as u:
            self.assertEqual(cl.describe_image(str(img), "Patio"), "(Vision analysis failed: ollama down)")
        self.assertEqual(u.call_count, 1)
        self.assertTrue(cl.describe_image(str(TMP / "missing.jpg"), "Patio").startswith("(Vision analysis failed:"))

    def test_snapshot_failure_fails_open(self):
        # RETRY GAP: take_snapshot — one get_snapshot attempt; False -> None, caller prints "snapshot failed"
        self.assertIsNone(cl.take_snapshot(_Client(snapshot=False), "abcdefgh1234", "Patio"))
        code, out = _main(["--all-exterior"], _Client(snapshot=False))
        self.assertEqual((code, out.count("snapshot failed")), (0, 3))

    def test_protect_session_expiry_is_retried_once_through_get_cameras(self):
        # the only real retry on this path lives in ProtectClient._get: a 401 re-logs-in and re-fetches exactly once
        client = nova_protect_monitor.ProtectClient()
        login_resp = MagicMock(status=200); login_resp.headers = {"X-CSRF-Token": "tok"}
        boot = MagicMock(); boot.read.return_value = json.dumps({"cameras": CAMS}).encode()
        err = urllib.error.HTTPError("https://x", 401, "expired", {}, None)
        opener = MagicMock(); opener.open.side_effect = [login_resp, err, login_resp, boot]
        client._opener = opener
        with patch.object(nova_protect_monitor, "_get_password", return_value="pw"):
            self.assertEqual(len(cl.get_cameras(client)), 3)
        self.assertEqual(opener.open.call_count, 4)


class TestUnit(unittest.TestCase):
    def test_get_cameras_edges(self):
        client = MagicMock(); client.get_bootstrap.return_value = None
        self.assertEqual(cl.get_cameras(client), [])
        client.get_bootstrap.return_value = {}
        self.assertEqual(cl.get_cameras(client), [])
        client.get_bootstrap.return_value = {"cameras": [{"id": "x"}, _cam("Driveway", state="DISCONNECTED")]}
        self.assertEqual(cl.get_cameras(client), [])                # no name / not CONNECTED -> dropped

    def test_fuzzy_match_substring_then_word(self):
        cams = [_cam("Front Door"), _cam("Patio Door"), _cam("Driveway")]
        self.assertEqual(cl.fuzzy_match("DOOR", cams)["name"], "Front Door")      # first substring hit, case-insensitive
        self.assertEqual(cl.fuzzy_match("patio door", cams)["name"], "Patio Door")
        self.assertEqual(cl.fuzzy_match("drive", cams)["name"], "Driveway")
        self.assertIsNone(cl.fuzzy_match("garage", cams))
        self.assertIsNone(cl.fuzzy_match("x", []))

    def test_take_snapshot_names_file_by_camera_id_prefix(self):
        client = _Client()
        path = cl.take_snapshot(client, "0123456789abcdef", "Patio")
        self.assertEqual(Path(path), TMP / "snaps" / "look_01234567.jpg")
        self.assertTrue(Path(path).exists())
        self.assertEqual(client.snapshots, ["0123456789abcdef"])
        Path(path).unlink()

    def test_describe_image_payload_and_response(self):
        img = TMP / "p.jpg"; img.write_bytes(b"\xff\xd8\xff\xe0hello")
        with _vision("Two deer, dusk.") as u:
            self.assertEqual(cl.describe_image(str(img), "Back Yard"), "Two deer, dusk.")
        req = u.call_args[0][0]
        self.assertEqual(req.full_url, cl.OLLAMA_URL)
        body = json.loads(req.data)
        self.assertEqual((body["model"], body["stream"], body["options"]["num_predict"]), (cl.VISION_MODEL, False, 300))
        self.assertEqual(body["images"], [base64.b64encode(b"\xff\xd8\xff\xe0hello").decode()])
        self.assertIn("'Back Yard'", body["prompt"])


class TestIntegration(unittest.TestCase):
    def test_shares_protect_monitor_constants_and_client(self):
        self.assertIs(cl.ProtectClient, nova_protect_monitor.ProtectClient)
        self.assertNotIn("class ProtectClient", SRC)
        self.assertEqual(cl.INTERIOR_PREFIX, nova_protect_monitor.INTERIOR_PREFIX)
        # same snapshot directory as the monitor (SNAPSHOT_DIR is patched to TMP here, so compare the source literal)
        literal = 'Path.home() / ".openclaw/workspace/protect_snapshots"'
        self.assertEqual(SRC.count(literal), 1)
        self.assertIn(literal, (SCRIPTS / "nova_protect_monitor.py").read_text())

    def test_get_cameras_then_fuzzy_match_then_snapshot_chain(self):
        client = _Client()
        cam = cl.fuzzy_match("front", cl.get_cameras(client))
        path = cl.take_snapshot(client, cam["id"], cam["name"])
        self.assertTrue(path.endswith("look_front-do.jpg"))
        self.assertEqual(client.snapshots, [cam["id"]])
        os.unlink(path)


class TestFunctional(unittest.TestCase):
    def test_golden_path_looks_describes_and_cleans_up(self):
        client = _Client()
        with _vision("Jordan waving at the camera.") as u:
            code, out = _main(["front", "door"], client)
        self.assertEqual(code, 0)
        self.assertEqual(out, "Looking at: Front Door\n\nJordan waving at the camera.\n")
        self.assertEqual(client.snapshots, [_cam("Front Door")["id"]])
        self.assertEqual(u.call_count, 1)
        self.assertEqual(list((TMP / "snaps").glob("look_*.jpg")), [])         # snapshot removed after describing

    def test_list_and_all_exterior(self):
        code, out = _main(["--list"])
        self.assertEqual((code, out), (0, "Accessible cameras (3):\n  Back Yard\n  Front Door\n  Patio\n"))
        with _vision("quiet") as u:
            code, out = _main(["--all-exterior"])
        self.assertEqual((code, u.call_count), (0, 3))
        self.assertEqual(out, "\nBack Yard:\n  quiet\n\nFront Door:\n  quiet\n\nPatio:\n  quiet\n")

    def test_error_paths(self):
        code, out = _main([])
        self.assertEqual(code, 1); self.assertIn("Usage:", out)
        code, out = _main(["patio"], _Client(login=False))
        self.assertEqual((code, out), (1, "ERROR: Cannot connect to UniFi Protect\n"))
        code, out = _main(["garage"])
        self.assertEqual(code, 1); self.assertTrue(out.startswith("No camera matching 'garage'. Available:\n  Back Yard"))
        code, out = _main(["patio"], _Client(snapshot=False))
        self.assertEqual((code, out), (1, "Looking at: Patio\nERROR: Could not take snapshot from Patio\n"))


class TestFrame(unittest.TestCase):
    def test_usage_exits_one_and_import_never_runs_main(self):
        # no --help/--selftest; bare invocation prints usage before any Protect login
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage: python3 nova_camera_look.py", r.stdout)
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_camera_look"], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
