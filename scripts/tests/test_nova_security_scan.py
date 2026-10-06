#!/usr/bin/env python3
"""Tests for nova_security_scan.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Every ssh/sh call (run_on_host) is mocked — no scanner ever runs on a real host."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_security_scan.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nss_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ss = _load()
# stub every outbound side effect at module load: notify bus, PG writes, baseline/deploy lookups
ss.notify = MagicMock()
ss.store_result = MagicMock()
ss.post_observation = MagicMock()
ss._get_baseline_warnings = MagicMock(return_value=set())
ss._had_recent_deploy = MagicMock(return_value=False)

PAD = "\n" + "filler line for a realistic scan length\n" * 12
RK_CLEAN = "Checking system commands...\nSystem checks summary\n" + PAD
RK_BAD = RK_CLEAN + "Warning: Suspicious file /dev/.evil\nWarning: Found preloaded shared library\n"
CHK = "ROOTDIR is `/'\nChecking `ls'... INFECTED\nChecking `sshd'... INFECTED\nChecking `amd'... not found\n" + PAD
AIDE = "AIDE found differences\nSummary:\n  Added: 2\n  Removed: 1\n  Changed: 3\n/etc/passwd : changed\n" + PAD


class _Base(unittest.TestCase):
    def setUp(self):
        # slack_alert() imports nova_config at call time and fires a desktop banner + sound — stub it
        import importlib
        cfg = sys.modules.get("nova_config") or importlib.import_module("nova_config")
        p = patch.object(cfg, "notify_local", create=True); self.local = p.start(); self.addCleanup(p.stop)
        for m in (ss.notify, ss.store_result, ss.post_observation):
            m.reset_mock(side_effect=True)
        ss._had_recent_deploy.return_value = False
        ss._get_baseline_warnings.return_value = set()
        ss._scan_running = False


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_no_shell_true_and_ssh_batch_mode(self):
        self.assertIsNone(re.search(r",\s*shell\s*=\s*True", SRC))      # only mentioned in a comment
        with patch.object(ss.subprocess, "run", return_value=SimpleNamespace(stdout="o", stderr="", returncode=0)) as r:
            ss.run_remote({"ip": "h.example", "user": "u"}, "echo")
        argv = r.call_args[0][0]
        self.assertIn("BatchMode=yes", argv)
        self.assertEqual(argv[-2:], ["u@h.example", "echo"])

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))

    def test_unknown_host_refused(self):
        with patch.object(ss, "run_on_host") as roh:
            self.assertEqual(ss.run_full_scan("evil; rm -rf /"), {"error": "Unknown host: evil; rm -rf /"})
        roh.assert_not_called()
        self.assertFalse(ss._scan_running)


class TestPerformance(unittest.TestCase):
    def test_parse_10k_line_chkrootkit_fast(self):
        out = "ROOTDIR is `/'\n" + "\n".join(f"Checking `x{i}'... not infected" for i in range(10_000))
        t0 = time.perf_counter()
        self.assertEqual(ss.parse_chkrootkit(out, "h"), ("clean", []))
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(_Base):
    def test_remote_failures_fail_open_as_error(self):
        # RETRY GAP: run_remote()/ssh — one attempt per tool; timeouts/errors are recorded as status=error
        with patch.object(ss.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 600)) as r:
            out, rc = ss.run_remote({"ip": "h"}, "x", 600)
        self.assertEqual((rc, r.call_count), (-1, 1))
        self.assertIn("TIMEOUT", out)
        with patch.object(ss, "run_on_host", return_value=(out, rc)):
            res = ss.scan_host({"name": "n", "ip": "h", "os": "linux"}, "sid", tools=["rkhunter"])
        self.assertEqual(res[0]["status"], "error")

    def test_host_exception_does_not_stop_fleet(self):
        fleet = [{"name": "a", "ip": "a", "os": "linux"}, {"name": "b", "ip": "b", "os": "linux"}]
        with patch.object(ss, "FLEET", fleet), \
             patch.object(ss, "scan_host", side_effect=[RuntimeError("boom"), [{"host": "b", "status": "clean"}]]), \
             patch("sys.stderr", new_callable=io.StringIO), patch("builtins.print"):
            r = ss.run_full_scan()
        self.assertEqual([x["status"] for x in r["results"]], ["error", "clean"])


class TestUnit(_Base):
    def test_scan_actually_ran(self):
        self.assertFalse(ss.scan_actually_ran("")[0])
        self.assertIn("never executed", ss.scan_actually_ran("sudo: rkhunter: command not found")[1])
        self.assertIn("too short", ss.scan_actually_ran("ok")[1])
        self.assertIn("never completed", ss.scan_actually_ran("x" * 400, "rkhunter")[1])
        self.assertTrue(ss.scan_actually_ran(RK_CLEAN, "rkhunter")[0])

    def test_rkhunter_whitelist_and_baseline(self):
        status, f = ss.parse_rkhunter(RK_BAD, "h")
        self.assertEqual(status, "critical")
        self.assertEqual([x["detail"] for x in f], ["Warning: Suspicious file /dev/.evil"])
        ss._get_baseline_warnings.return_value = {"Warning: Suspicious file /dev/.evil"}
        self.assertEqual(ss.parse_rkhunter(RK_BAD, "h"), ("clean", []))
        self.assertEqual(ss.parse_rkhunter("tiny", "h")[0], "error")

    def test_chkrootkit_false_positives(self):
        status, f = ss.parse_chkrootkit(CHK, "nova-core")
        self.assertEqual([x["detail"] for x in f], ["Checking `sshd'... INFECTED"])   # uutils `ls` suppressed
        xor = "ROOTDIR is `/'\nSearching for Linux.Xor.DDoS ... INFECTED: /tmp/x.py\n" + PAD
        self.assertEqual(ss.parse_chkrootkit(xor)[0], "clean")
        self.assertEqual(ss.parse_chkrootkit(xor + "/etc/cron.hourly/udev.sh\n")[0], "critical")

    def test_aide_counts_and_deploy_exemption(self):
        status, f = ss.parse_aide(AIDE, "h")
        self.assertEqual((status, f[0]["added"], f[0]["removed"], f[0]["changed"]), ("critical", 2, 1, 3))
        self.assertIn("/etc/passwd", f[0]["files"])
        ss._had_recent_deploy.return_value = True
        self.assertEqual(ss.parse_aide(AIDE, "h")[1][0]["verdict"], "PASS")


class TestIntegration(_Base):
    def test_critical_finding_alerts_and_observes(self):
        with patch.object(ss, "run_on_host", return_value=(CHK, 0)), patch("builtins.print"):
            res = ss.scan_host({"name": "nova-core2", "ip": "x", "os": "linux"}, "sid", tools=["chkrootkit"])
        self.assertEqual(res[0]["status"], "critical")
        self.assertEqual(ss.store_result.call_args.kwargs["scan_type"], "chkrootkit")
        self.assertEqual(ss.notify.call_args.kwargs["level"], "critical")
        self.local.assert_called_once()
        self.assertEqual(ss.post_observation.call_args.kwargs["severity"], "critical")

    def test_macos_hosts_run_nothing(self):
        with patch.object(ss, "run_on_host") as roh:
            self.assertEqual(ss.scan_host({"name": "m", "ip": "x", "os": "macos"}, "sid"), [])
        roh.assert_not_called()


class TestFunctional(_Base):
    def _handler(self, method, path, body=b""):
        h = ss.ScanHandler.__new__(ss.ScanHandler)
        h.path = path; h.headers = {"Content-Length": str(len(body))}
        h.rfile = io.BytesIO(body); h.wfile = io.BytesIO()
        h.sent = []
        h.send_response = lambda code: h.sent.append(code)
        h.send_header = lambda *a: None; h.end_headers = lambda: None
        getattr(h, f"do_{method}")()
        return h.sent[0], json.loads(h.wfile.getvalue())

    def test_full_scan_golden_path(self):
        fleet = [{"name": "lin", "ip": "l", "os": "linux"}]
        outs = {"rkhunter": RK_CLEAN, "chkrootkit": CHK.replace("sshd", "ls"), "aide": "All files match\n" + PAD}
        def fake(host, cmd, timeout):
            tool = next(t for t in outs if t in cmd)
            return outs[tool], 0
        with patch.object(ss, "FLEET", fleet), patch.object(ss, "run_on_host", side_effect=fake), patch("builtins.print"):
            r = ss.run_full_scan()
        self.assertEqual(sorted((x["tool"], x["status"]) for x in r["results"]),
                         [("aide", "clean"), ("chkrootkit", "clean"), ("rkhunter", "clean")])
        self.assertEqual(ss.store_result.call_count, 3)
        ss.notify.assert_not_called()
        self.assertIsNotNone(ss._last_scan_time)

    def test_http_routes(self):
        code, body = self._handler("GET", "/health")
        self.assertEqual((code, body["service"]), (200, "nova-security-scan"))
        self.assertEqual(self._handler("GET", "/nope")[0], 404)
        with patch.object(ss.threading, "Thread") as th:
            code, body = self._handler("POST", "/scan", b'{"host": "nova-core"}')
        self.assertEqual(body, {"status": "scan_started", "target": "nova-core"})
        th.return_value.start.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help: main() binds :37474 and starts the 3am scheduler, so the frame check is an import smoke
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_security_scan"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
