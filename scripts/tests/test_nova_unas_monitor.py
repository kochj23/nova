#!/usr/bin/env python3
"""Tests for nova_unas_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

State/status/snapshot/log files go to a tempdir; the UNAS client, notify and urlopen are mocked."""
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_unas_monitor.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("unas_monitor_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


um = _load()
_TD = tempfile.TemporaryDirectory()
_PATCHES = []


def setUpModule():
    d = Path(_TD.name)
    for p in (patch.object(um, "STATE_DIR", d), patch.object(um, "STATE_FILE", d / "state.json"),
              patch.object(um, "STATUS_FILE", d / "status.json"), patch.object(um, "SNAPSHOT_FILE", d / "snaps.json"),
              patch.object(um, "LOG_FILE", d / "logs" / "unas.log"),
              patch.object(um, "notify", side_effect=AssertionError("unmocked notify")),
              patch.object(um.client, "health_snapshot", side_effect=AssertionError("unmocked UNAS")),
              patch("urllib.request.urlopen", side_effect=OSError("offline"))):
        p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


def _snap(free_tb=8.0, status="healthy", state="setup", shares=(("Media", "active"),), more=False):
    return {"device": {"name": "UNAS", "model": "UNASPRO8", "state": state},
            "storage": {"status": status, "free_tb": free_tb, "used_pct": 70.0, "total_tb": 30.0,
                        "used_bytes": 21 * 10**12, "needs_more_disk": more},
            "shares": [{"name": n, "status": s, "used_tb": 1.0, "encryption": "unencrypted"} for n, s in shares]}


def _main(argv, snap, state=None):
    if state is None:
        um.STATE_FILE.unlink(missing_ok=True)
    else:
        um.STATE_FILE.write_text(json.dumps(state))
    with patch.object(um.client, "health_snapshot", return_value=snap), patch.object(um, "notify") as n, \
            patch.object(um, "_ingest_memory") as mem, patch.object(sys, "argv", ["x", *argv]), \
            redirect_stdout(io.StringIO()) as out:
        um.main()
    return n, mem, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_memory_ingest_marked_local_only(self):
        r = MagicMock(); r.__enter__.return_value = r
        with patch("urllib.request.urlopen", return_value=r) as u:
            um._ingest_memory("status text")
        body = json.loads(u.call_args.args[0].data)
        self.assertEqual(body["privacy"], "local-only")
        self.assertTrue(u.call_args.args[0].full_url.endswith("/remember"))

    def test_files_written_only_under_redirected_dir(self):
        _main([], _snap())
        self.assertTrue(um.STATUS_FILE.exists())
        self.assertTrue(str(um.STATUS_FILE).startswith(_TD.name))


class TestPerformance(unittest.TestCase):
    def test_checks_on_10k_shares_fast(self):
        snap = _snap(shares=[(f"s{i}", "degraded" if i % 2 else "active") for i in range(10_000)])
        t0 = time.perf_counter()
        probs = um.check_device(snap) + um.check_storage(snap) + um.check_shares(snap)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(probs), 5000)


class TestRetry(unittest.TestCase):
    def test_unas_error_exits_1(self):
        # retries live in nova_unas_client._request (3 attempts w/ backoff); here the final UNASError exits 1
        with patch.object(um.client, "health_snapshot", side_effect=um.UNASError("after 3 attempts")), \
                patch.object(sys, "argv", ["x"]), redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as cm:
                um.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("ERROR: after 3 attempts", out.getvalue())

    def test_notify_and_memory_failures_swallowed(self):
        with patch.object(um, "notify", side_effect=RuntimeError("bus")), redirect_stdout(io.StringIO()) as out:
            um.post_slack("⚠️ *Title*\nbody")
        self.assertIn("notify failed", out.getvalue())
        um._ingest_memory("x")   # urlopen raises OSError file-wide; must not escape


class TestUnit(unittest.TestCase):
    def test_storage_thresholds(self):
        self.assertEqual(um.check_storage(_snap(free_tb=8)), [])
        self.assertIn("warning", um.check_storage(_snap(free_tb=2.5))[0])
        self.assertIn("CRITICAL", um.check_storage(_snap(free_tb=1.0))[0])
        self.assertEqual(len(um.check_storage(_snap(status="degraded", more=True))), 2)

    def test_device_and_shares(self):
        self.assertEqual(um.check_device(_snap(state="production (local-managed)")), [])
        self.assertEqual(um.check_device(_snap(state="offline")), ["UNAS device state: offline"])
        self.assertEqual(um.check_shares(_snap(shares=[("A", "active"), ("B", ""), ("C", "error")])),
                         ["Share 'C' status: error"])

    def test_fmt_bytes(self):
        self.assertEqual(um._fmt_bytes(2 * 10**12), "2.00 TB")
        self.assertEqual(um._fmt_bytes(3 * 10**9), "3.00 GB")
        self.assertEqual(um._fmt_bytes(5 * 10**6), "5.0 MB")

    def test_snapshot_keeps_90(self):
        um.SNAPSHOT_FILE.write_text(json.dumps([{"i": i} for i in range(95)]))
        with redirect_stdout(io.StringIO()):
            um.save_snapshot({"x": 1})
        data = json.loads(um.SNAPSHOT_FILE.read_text())
        self.assertEqual((len(data), data[-1]["x"]), (90, 1))


class TestIntegration(unittest.TestCase):
    def test_post_slack_routes_through_notify(self):
        with patch.object(um, "notify") as n:
            um.post_slack("⚠️ *UNAS Pro 8 — New Problems*\n• a")
        self.assertEqual(n.call_args.args[0], "UNAS Pro 8 — New Problems")
        kw = n.call_args.kwargs
        self.assertEqual((kw["category"], kw["body"], kw["dedup_key"]), ("storage", "• a", "unas-storage"))
        self.assertIs(um.UNASClient, sys.modules["nova_unas_client"].UNASClient)


class TestFunctional(unittest.TestCase):
    def test_new_problem_alerts_once_then_resolves(self):
        n, mem, out = _main([], _snap(free_tb=1.0))
        self.assertEqual(n.call_count, 1)
        self.assertEqual(json.loads(um.STATUS_FILE.read_text())["device"]["state"], "production (local-managed)")
        mem.assert_called_once()
        state = json.loads(um.STATE_FILE.read_text())
        n, mem, _ = _main([], _snap(free_tb=1.0), state=state)
        n.assert_not_called(); mem.assert_not_called()           # same problem, same day
        n, _, _ = _main([], _snap(free_tb=9.0), state=json.loads(um.STATE_FILE.read_text()))
        self.assertEqual(n.call_args.kwargs["level"], "info")
        self.assertIn("resolved", n.call_args.args[0])

    def test_json_mode_prints_and_never_alerts(self):
        n, mem, out = _main(["--json"], _snap(free_tb=0.5))
        n.assert_not_called()
        self.assertEqual(json.loads(out)["storage"]["free_tb"], 0.5)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(PATH), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--snapshot", r.stdout)


if __name__ == "__main__":
    unittest.main()
