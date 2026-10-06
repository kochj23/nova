#!/usr/bin/env python3
"""Tests for nova_nightly_synology.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
The NAS socket, the monitor subprocess, post_both, nova_logger and the memory server are all mocked."""
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_nightly_synology.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="synology-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("nightly_synology_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ns = _load()
# module-level stubs: no Slack, no log file writes, no monitor subprocess, ack file in a tempdir
ns.nova_config = types.SimpleNamespace(post_both=MagicMock(), SLACK_BB="C_TEST", VECTOR_URL="http://mem.test/remember")
ns.log = MagicMock()
ns.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=OSError("offline: monitor stubbed")))
ns.ACK_PATH = TMP / "ack.json"

DATA = {
    "status": {"model": "RS1221+", "dsm_version": "7.2", "uptime_seconds": 86400 * 3, "cpu_load": 5,
               "ram_used_percent": 40, "temperature": 41, "overall_status": "normal"},
    "storage": {"volumes": [{"name": "volume1", "total_bytes": 10 * 1024**4, "used_bytes": 7 * 1024**4,
                             "raid_type": "SHR-2", "status": "normal"}]},
    "disks": {"disks": [{"name": "d1", "temperature": 38, "status": "normal"},
                        {"name": "d2", "temperature": 50, "status": "failing", "model": "WD"}]},
    "security": {"failed_logins_24h": 2, "blocked_ips": 1},
    "services": {"packages": [{"name": "Plex", "status": "running"}, {"name": "Bad", "status": "crashed"}]},
    "network": {"interfaces": [{"name": "eth0", "speed": 10000}]},
}


def _main(data, wake=True):
    ns.nova_config.post_both.reset_mock()
    with patch.object(ns, "wake_nas", return_value=wake), \
         patch.object(ns, "run_synology", side_effect=lambda m: data.get(m)), \
         patch.object(urllib.request, "urlopen") as mem:
        ns.main()
    return ns.nova_config.post_both.call_args[0][0], mem


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"(?i)passw(or)?d\s*=")

    def test_monitor_called_with_argv_and_fixed_modes(self):
        self.assertNotIn("shell=True", SRC)
        rec = MagicMock(return_value=types.SimpleNamespace(stdout='{"a": 1}'))
        with patch.object(ns.subprocess, "run", rec):
            self.assertEqual(ns.run_synology("disks"), {"a": 1})
        argv = rec.call_args[0][0]
        self.assertEqual(argv[-2:], ["--disks", "--json"])


class TestPerformance(unittest.TestCase):
    def test_format_bytes_100k(self):
        t0 = time.perf_counter()
        for i in range(100_000):
            ns.format_bytes(i * 1024**2)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_wake_nas_retries_then_succeeds(self):
        socks = [MagicMock(), MagicMock()]
        socks[0].connect.side_effect = OSError("asleep")
        with patch.object(socket, "socket", side_effect=socks), patch.object(time, "sleep") as sl:
            self.assertTrue(ns.wake_nas())
        self.assertEqual(sl.call_count, 1)
        socks[1].connect.assert_called_once_with(("192.168.1.11", 5000))

    def test_wake_nas_gives_up_after_retries(self):
        s = MagicMock(); s.connect.side_effect = OSError("down")
        with patch.object(socket, "socket", return_value=s), patch.object(time, "sleep"):
            self.assertFalse(ns.wake_nas(retries=2))
        self.assertEqual(s.connect.call_count, 2)

    def test_run_synology_failure_is_none(self):
        # RETRY GAP: run_synology — one subprocess attempt, None on failure (section skipped)
        self.assertIsNone(ns.run_synology("status"))


class TestUnit(unittest.TestCase):
    def test_format_bytes(self):
        self.assertEqual(ns.format_bytes(2 * 1024**4), "2.0 TB")
        self.assertEqual(ns.format_bytes(3 * 1024**3), "3.0 GB")
        self.assertEqual(ns.format_bytes(5 * 1024**2), "5 MB")
        self.assertEqual(ns.format_bytes(0), "0 KB")

    def test_load_acknowledged(self):
        if ns.ACK_PATH.exists():
            ns.ACK_PATH.unlink()
        self.assertEqual(ns.load_acknowledged(), {})
        ns.ACK_PATH.write_text("{bad")
        self.assertEqual(ns.load_acknowledged(), {})
        ns.ACK_PATH.write_text('{"nas_unreachable_hours": [1]}')
        self.assertEqual(ns.load_acknowledged()["nas_unreachable_hours"], [1])


class TestIntegration(unittest.TestCase):
    def test_posts_via_shared_post_both_to_bb_channel(self):
        ns.nova_config.post_both.reset_mock()
        ns.slack_post("x")
        ns.nova_config.post_both.assert_called_once_with("x", slack_channel="C_TEST")

    def test_memory_write_uses_infrastructure_vector(self):
        _, mem = _main(DATA)
        body = json.loads(mem.call_args[0][0].data)
        self.assertEqual(body["source"], "infrastructure")
        self.assertEqual(body["metadata"]["type"], "synology_nightly")


class TestFunctional(unittest.TestCase):
    def test_full_report(self):
        msg, _ = _main(DATA)
        for s in ("RS1221+ / DSM 7.2 / Uptime: 3d", "volume1", "70%", "d2: failing", "d2: 50°C",
                  "2 failed login(s)", "Bad: crashed", "eth0: 10Gbps"):
            self.assertIn(s, msg)
        self.assertNotIn("All drives healthy", msg)

    def test_unreachable_nas_reports_red_or_expected_sleep(self):
        ns.ACK_PATH.write_text(json.dumps({"nas_unreachable_hours": []}))
        msg, _ = _main({}, wake=False)
        self.assertIn("Could not reach NAS", msg)
        ns.ACK_PATH.write_text(json.dumps({"nas_unreachable_hours": list(range(24))}))
        msg, _ = _main({}, wake=False)
        self.assertIn("NAS sleeping (expected)", msg)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help/--selftest: running the script probes the NAS and posts to Slack, so import is the smoke test
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_nightly_synology as m; print(callable(m.main))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
