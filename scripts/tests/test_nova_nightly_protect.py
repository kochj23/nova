#!/usr/bin/env python3
"""Tests for nova_nightly_protect.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_nightly_protect.py"
SRC = SCRIPT.read_text()
_TMP = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("nnpr_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pr = _load()
# stub every outbound side effect at module load: Slack/Discord, the structured logger, ack file
pr.slack_post = MagicMock()
pr.log = MagicMock()
pr.ACK_PATH = Path(_TMP.name) / "ack.json"


def _now_ms(offset_s=0):
    return int((datetime.now(timezone.utc).timestamp() + offset_s) * 1000)


def _bootstrap():
    return {
        "cameras": [
            {"id": "c1", "name": "Front Door", "state": "CONNECTED", "type": "G4"},
            {"id": "c2", "name": "Alley North", "state": "DISCONNECTED", "type": "G3"},
            {"id": "c3", "name": "Garage", "state": "DISCONNECTED", "type": "G5"},
            {"id": "c9", "name": "Interior Kitchen", "state": "CONNECTED", "type": "G4"},
        ],
        "nvr": {"uptime": 2 * 86400 + 3 * 3600, "firmwareVersion": "4.1",
                "storageInfo": {"totalSize": 5 * 1024**4, "totalCapacity": 10 * 1024**4}},
    }


def _events():
    return [
        {"camera": "c1", "type": "smartDetectZone", "smartDetectTypes": ["person", "vehicle"], "start": _now_ms()},
        {"camera": "c1", "type": "motion", "smartDetectTypes": [], "start": _now_ms()},
        {"camera": "c9", "type": "smartDetectZone", "smartDetectTypes": ["person"], "start": _now_ms()},
        {"camera": "c1", "type": "motion", "start": _now_ms(-3 * 86400)},
    ]


def _client(login=True, boot=True, events=None):
    c = MagicMock()
    c.login.return_value = login
    c.get_bootstrap.return_value = _bootstrap() if boot else None
    c.get_events.return_value = _events() if events is None else events
    return c


class _Base(unittest.TestCase):
    def setUp(self):
        pr.slack_post.reset_mock()
        if pr.ACK_PATH.exists():
            pr.ACK_PATH.unlink()

    def run_main(self, client):
        with patch.object(pr, "ProtectClient", return_value=client), \
             patch.object(pr.urllib.request, "urlopen") as u:
            pr.main()
        return u


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_interior_cameras_never_reported(self):
        self.run_main(_client())
        msg = pr.slack_post.call_args[0][0]
        self.assertNotIn("Interior", msg)
        self.assertIn("2 events on exterior cameras", msg)        # c9 (interior) + 3-day-old event excluded


class TestPerformance(_Base):
    def test_10k_events_fast(self):
        ev = [{"camera": "c1", "type": "motion", "start": _now_ms()} for _ in range(10_000)]
        t0 = time.perf_counter()
        self.run_main(_client(events=ev))
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertIn("Front Door: 10000 events", pr.slack_post.call_args[0][0])


class TestRetry(_Base):
    def test_login_failure_posts_and_stops(self):
        # RETRY GAP: main()/ProtectClient.login — one attempt; a failed login posts a red notice and exits
        c = _client(login=False)
        self.run_main(c)
        self.assertIn("Cannot connect to UNVR", pr.slack_post.call_args[0][0])
        self.assertEqual(c.login.call_count, 1)
        c.get_bootstrap.assert_not_called()

    def test_memory_store_failure_is_swallowed(self):
        # RETRY GAP: vector store urlopen — fire-and-forget, one attempt
        with patch.object(pr, "ProtectClient", return_value=_client()), \
             patch.object(pr.urllib.request, "urlopen", side_effect=OSError("down")) as u:
            pr.main()
        self.assertEqual(u.call_count, 1)


class TestUnit(_Base):
    def test_load_acknowledged(self):
        self.assertEqual(pr.load_acknowledged(), {})
        pr.ACK_PATH.write_text("{not json")
        self.assertEqual(pr.load_acknowledged(), {})
        pr.ACK_PATH.write_text(json.dumps({"cameras_offline": ["Garage"]}))
        self.assertEqual(pr.load_acknowledged()["cameras_offline"], ["Garage"])

    def test_bootstrap_failure(self):
        self.run_main(_client(boot=False))
        self.assertIn("Bootstrap failed", pr.slack_post.call_args[0][0])

    def test_nvr_and_storage_lines(self):
        self.run_main(_client())
        msg = pr.slack_post.call_args[0][0]
        self.assertIn("Firmware 4.1 / Uptime: 2d 3h", msg)
        self.assertIn("█████░░░░░ 50%", msg)


class TestIntegration(_Base):
    def test_slack_post_routes_to_critical_channel(self):
        import nova_config
        with patch.object(nova_config, "post_both") as pb:
            spec = importlib.util.spec_from_file_location("nnpr_slack", SCRIPT)
            mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
            mod.slack_post("hi")
        self.assertEqual(pb.call_args.kwargs["slack_channel"], nova_config.SLACK_BB)

    def test_memory_payload_source_security(self):
        u = self.run_main(_client())
        payload = json.loads(u.call_args[0][0].data)
        self.assertEqual(payload["source"], "security")
        self.assertIn("1/3 cameras online", payload["text"])
        self.assertIn("person:1", payload["text"])


class TestFunctional(_Base):
    def test_golden_path_digest(self):
        pr.ACK_PATH.write_text(json.dumps({"cameras_offline": ["Garage"]}))
        self.run_main(_client())
        msg = pr.slack_post.call_args[0][0]
        self.assertIn("*Cameras:* 1/3 online", msg)
        self.assertIn(":red_circle: Alley North — OFFLINE (G3)", msg)
        self.assertIn(":white_circle: Garage — OFFLINE (known — acknowledged)", msg)
        self.assertIn("Front Door: person:1, vehicle:1", msg)
        self.assertIn("*Top motion cameras:*", msg)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help: any invocation logs into the UNVR and posts, so the frame check is an import smoke
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_nightly_protect"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
