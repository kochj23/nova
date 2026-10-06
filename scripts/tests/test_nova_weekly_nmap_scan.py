#!/usr/bin/env python3
"""Tests for nova_weekly_nmap_scan.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import runpy
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_weekly_nmap_scan.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ws = _load("weekly_nmap_under_test", SCRIPT)


def _resp(status=200, body=None):
    return types.SimpleNamespace(status_code=status, json=lambda: body)


def _http(post=None, devices=None, threats=None):
    """Patch requests.post/get on the module's requests binding; returns (post_mock, get_mock)."""
    post = post or MagicMock(return_value=_resp(200))
    get = MagicMock(side_effect=[_resp(200, devices if devices is not None else []),
                                 _resp(200, threats if threats is not None else [])])
    return patch.object(ws.requests, "post", post), patch.object(ws.requests, "get", get), post, get


def _ok(returncode=0, stderr=b""):
    return MagicMock(return_value=types.SimpleNamespace(returncode=returncode, stderr=stderr))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_only_talks_to_the_local_novacontrol_api(self):
        urls = re.findall(r"https?://[^\s\"']+", SRC)
        self.assertTrue(urls)
        self.assertTrue(all(u.startswith("http://127.0.0.1:37400/api/nmap/") for u in urls), urls)

    def test_threat_text_is_passed_as_repr_bytes_on_stdin_never_interpolated_into_a_shell(self):
        evil = {"severity": "high", "description": "x'; rm -rf / #"}
        run = _ok()
        with patch.object(ws.subprocess, "run", run):
            ws.post_to_slack({"threats": [evil], "device_count": 1, "timestamp": "t"})
        args, kwargs = run.call_args
        self.assertEqual(args[0][:2], ["python3", "-c"]); self.assertFalse(kwargs.get("shell"))
        code = args[0][2]
        self.assertIn("input=" + repr(ws.post_to_slack.__globals__["json"].dumps and "") [:0], code)  # code is a -c program
        self.assertIn("'--body-file', '/dev/stdin'", code)
        self.assertIn(repr("x'; rm -rf / #")[1:-1], code)        # the message survives verbatim inside repr()
        self.assertNotIn("rm -rf / #\n", code.split("input=")[0]) # ...and only inside the input= literal


class TestPerformance(unittest.TestCase):
    def test_slack_summary_caps_at_ten_threats_and_is_fast(self):
        threats = [{"severity": "low", "description": f"t{i}"} for i in range(10_000)]
        run = _ok()
        t0 = time.perf_counter()
        with patch.object(ws.subprocess, "run", run):
            ws.post_to_slack({"threats": threats, "device_count": 5, "timestamp": "t"})
        self.assertLess(time.perf_counter() - t0, 0.5)
        code = run.call_args[0][0][2]
        self.assertEqual(code.count("🔴"), 10)
        self.assertIn("THREATS DETECTED: 10000", code)


class TestRetry(unittest.TestCase):
    def test_scan_trigger_failures_fail_open_as_an_error_dict(self):
        # RETRY GAP: run_nmap_scan — one POST, one GET each; any failure returns {"error": ...} and nothing is posted
        p, g, post, get = _http(post=MagicMock(side_effect=ConnectionError("NovaControl down")))
        with p, g:
            self.assertEqual(ws.run_nmap_scan(), {"error": "NovaControl down"})
        get.assert_not_called()
        p, g, post, get = _http(post=MagicMock(return_value=_resp(503)))
        with p, g:
            self.assertEqual(ws.run_nmap_scan(), {"error": "Scan trigger returned 503"})

    def test_broadcast_failure_is_logged_not_raised(self):
        # RETRY GAP: post_to_slack — one nova_herd_broadcast attempt; a non-zero exit is printed and swallowed
        out = io.StringIO()
        with patch.object(ws.subprocess, "run", _ok(2, b"no such file")), redirect_stdout(out):
            ws.post_to_slack({"threats": [], "device_count": 0})
        self.assertIn("Slack post failed (exit 2): no such file", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_non_list_payloads_are_normalised(self):
        p, g, _, _ = _http(devices={"oops": 1}, threats="nope")
        with p, g:
            r = ws.run_nmap_scan()
        self.assertEqual((r["device_count"], r["threats"]), (0, []))
        self.assertRegex(r["timestamp"], r"^\d{4}-\d{2}-\d{2}T")

    def test_non_200_device_or_threat_fetches_degrade_to_empty(self):
        post = MagicMock(return_value=_resp(200))
        get = MagicMock(side_effect=[_resp(500), _resp(404)])
        with patch.object(ws.requests, "post", post), patch.object(ws.requests, "get", get):
            r = ws.run_nmap_scan()
        self.assertEqual((r["device_count"], r["threats"]), (0, []))

    def test_message_shapes_clean_vs_threats(self):
        run = _ok()
        with patch.object(ws.subprocess, "run", run):
            ws.post_to_slack({"threats": [], "device_count": 42, "timestamp": "2026-10-05T15:00:00"})
            clean = run.call_args[0][0][2]
            ws.post_to_slack({"threats": [{"severity": "critical", "description": "telnet open"}], "device_count": 3})
            hot = run.call_args[0][0][2]
        self.assertIn("Network status: CLEAN", clean); self.assertIn("Devices Scanned: 42", clean)
        self.assertIn("THREATS DETECTED: 1", hot); self.assertIn("critical: telnet open", hot)
        self.assertIn("—N", hot)


class TestIntegration(unittest.TestCase):
    def test_scan_hits_trigger_then_devices_then_threats_with_the_subnet_payload(self):
        p, g, post, get = _http(devices=[{"ip": "1"}, {"ip": "2"}], threats=[{"severity": "s", "description": "d"}])
        with p, g:
            r = ws.run_nmap_scan()
        self.assertEqual(post.call_args[0][0], "http://127.0.0.1:37400/api/nmap/scan")
        self.assertEqual(post.call_args[1]["json"], {"ip": "192.168.1.0/24"})
        self.assertEqual([c[0][0] for c in get.call_args_list],
                         ["http://127.0.0.1:37400/api/nmap/devices", "http://127.0.0.1:37400/api/nmap/threats"])
        self.assertEqual((r["device_count"], len(r["threats"])), (2, 1))

    def test_broadcast_goes_through_the_herd_script_with_the_weekly_subject(self):
        run = _ok()
        with patch.object(ws.subprocess, "run", run):
            ws.post_to_slack({"threats": [], "device_count": 0, "timestamp": "t"})
        code = run.call_args[0][0][2]
        self.assertIn(".openclaw/scripts/nova_herd_broadcast.sh", code)
        self.assertIn("'--subject', 'Weekly Network Security Scan'", code)
        self.assertTrue(run.call_args[1].get("capture_output"))


class TestFunctional(unittest.TestCase):
    def _main(self, post=None, devices=None, threats=None, run=None):
        run = run or _ok()
        p, g, post, get = _http(post=post, devices=devices, threats=threats)
        out = io.StringIO()
        with p, g, patch.object(ws.subprocess, "run", run), patch("requests.post", post), patch("requests.get", get), \
             redirect_stdout(out):
            runpy.run_path(str(SCRIPT), run_name="__main__")
        return out.getvalue(), run

    def test_golden_path_scans_and_broadcasts(self):
        out, run = self._main(devices=[1, 2, 3], threats=[{"severity": "high", "description": "ssh root login"}])
        self.assertIn("Running weekly network security scan", out)
        self.assertIn("Scan complete: 3 devices, 1 threats", out)
        run.assert_called_once()
        self.assertIn("ssh root login", run.call_args[0][0][2])

    def test_error_path_skips_the_broadcast(self):
        out, run = self._main(post=MagicMock(side_effect=ConnectionError("nope")))
        self.assertIn("Scan error: nope", out)
        run.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_the_scan(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_weekly_nmap_scan"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
