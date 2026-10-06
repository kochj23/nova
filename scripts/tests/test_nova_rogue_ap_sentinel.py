#!/usr/bin/env python3
"""Tests for nova_rogue_ap_sentinel.py — the 7 house categories (Security, Performance, Retry, Unit,
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

import psycopg2  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_rogue_ap_sentinel.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="rogue_ap_test_"))

import nova_config  # noqa: E402,F401


def _load():
    spec = importlib.util.spec_from_file_location("nras", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("psycopg2.connect", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


ra = _load()
CFG = types.SimpleNamespace(post_both=MagicMock(name="post_both"), SLACK_ALERTS="#test-alerts")
ra.nova_config = CFG                     # Slack/Discord posting stubbed for every test
ra.STATE_DIR = TMP
ra.STATE_FILE = TMP / "rogue_ap_sentinel.json"
ra.ALLOWLIST_FILE = TMP / "rogue_ap_allowlist.json"

SOUNDBAR_STA = "aa:bb:cc:dd:ee:10"
SOUNDBAR_AP = "aa:bb:cc:dd:ee:12"           # same /40 as the wired MAC


def _pg(lan_rows, ap_rows):
    """connect() returns the device-list conn first, then the wifi_aps conn."""
    def mk(rows):
        cur = MagicMock(); cur.fetchall.return_value = rows
        c = MagicMock(); c.cursor.return_value = cur
        return c, cur
    (c1, cur1), (c2, cur2) = mk(lan_rows), mk(ap_rows)
    return MagicMock(side_effect=[c1, c2]), cur2


def _ap(bssid, ssid="Bose 900", dbm=-50, sec="Open", raw=None):
    return (ssid, bssid, dbm, sec, 6, raw)


def _main(argv, connect=None):
    out = io.StringIO()
    with patch.object(ra.psycopg2, "connect", connect or MagicMock(side_effect=RuntimeError("no pg"))), \
         patch.object(sys, "argv", ["x"] + argv), redirect_stdout(out):
        rc = ra.main()
    return rc, out.getvalue()


def _reset():
    for p in (ra.STATE_FILE, ra.ALLOWLIST_FILE):
        p.unlink(missing_ok=True)
    CFG.post_both.reset_mock()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_window_is_bound_param(self):
        connect, cur = _pg([], [])
        with patch.object(ra.psycopg2, "connect", connect):
            ra.scan_flags(window="5 minutes'; SELECT 1; --")
        sql, params = cur.execute.call_args[0]
        self.assertNotIn("SELECT 1;", sql)
        self.assertEqual(params, ("5 minutes'; SELECT 1; --",))

    def test_allowlist_is_honored(self):
        _reset()
        ra._save(ra.ALLOWLIST_FILE, [SOUNDBAR_AP])
        connect, _ = _pg([(SOUNDBAR_STA, "Soundbar")], [_ap(SOUNDBAR_AP)])
        with patch.object(ra.psycopg2, "connect", connect):
            self.assertEqual(ra.get_current_flags(), [])


class TestPerformance(unittest.TestCase):
    def test_10k_aps_evaluated_fast(self):
        lan = [(f"aa:bb:cc:{i // 256:02x}:{i % 256:02x}:01", f"dev{i}") for i in range(2000)]
        aps = [_ap(f"aa:bb:cc:{i // 256:02x}:{i % 256:02x}:02", dbm=-50 if i % 2 else -90) for i in range(10_000)]
        connect, _ = _pg(lan, aps)
        t0 = time.perf_counter()
        with patch.object(ra.psycopg2, "connect", connect):
            flags = ra.scan_flags()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(flags), 1000)


class TestRetry(unittest.TestCase):
    def test_pg_down_fails_open_to_no_findings(self):
        # RETRY GAP: _lan_prefixes()/scan_flags() — one connect each, no retry; failure yields [] and no alert
        _reset()
        connect = MagicMock(side_effect=psycopg2.OperationalError("down"))
        rc, out = _main([], connect)
        self.assertEqual(rc, 0)
        self.assertEqual(connect.call_count, 2)
        self.assertIn("wifi_aps fetch failed", out)
        CFG.post_both.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_classification(self):
        lan = [(SOUNDBAR_STA, "Soundbar"), ("short", "x")]
        aps = [_ap(SOUNDBAR_AP),                                        # ours, open, strong -> flag
               _ap("aa:bb:cc:dd:ee:99", dbm=-80),                       # ours but weak -> ignore
               _ap("aa:bb:cc:dd:ee:98", sec="WPA2"),                    # ours but encrypted -> ignore
               _ap("11:22:33:44:55:66", sec=None),                      # neighbour open -> ignore
               _ap("77:77:77:77:77:77", sec="WPA2", raw=json.dumps({"is_rogue": True})),
               _ap("88:88:88:88:88:88", sec="WPA2", raw="{not json")]
        connect, _ = _pg(lan, aps)
        with patch.object(ra.psycopg2, "connect", connect):
            flags = ra.scan_flags()
        self.assertEqual([(f["kind"], f["bssid"]) for f in flags],
                         [("YOUR-GEAR-OPEN", SOUNDBAR_AP), ("UNIFI-ROGUE", "77:77:77:77:77:77")])
        self.assertEqual(flags[0]["device"], "Soundbar")

    def test_load_save_roundtrip_and_default(self):
        p = TMP / "x" / "y.json"
        self.assertEqual(ra._load(p, ["d"]), ["d"])
        ra._save(p, [1, 2])
        self.assertEqual(ra._load(p, None), [1, 2])

    def test_ack_adds_to_allowlist(self):
        _reset()
        rc, out = _main(["--ack", SOUNDBAR_AP])
        self.assertEqual(rc, 0)
        self.assertEqual(ra._load(ra.ALLOWLIST_FILE, []), [SOUNDBAR_AP])
        self.assertIn("acknowledged", out)


class TestIntegration(unittest.TestCase):
    def test_reads_known_devices_and_wifi_aps(self):
        self.assertIn("FROM telemetry.known_devices", SRC)
        self.assertIn("FROM wifi_aps", SRC)
        self.assertIn("nova_config.post_both(", SRC)

    def test_daily_report_consumer_uses_public_api(self):
        consumers = [p.name for p in SCRIPTS.glob("*.py") if p.name != SCRIPT.name
                     and "rogue_ap_sentinel" in p.read_text(errors="ignore")]
        for c in consumers:
            self.assertIn("get_current_flags", (SCRIPTS / c).read_text(errors="ignore"), c)


class TestFunctional(unittest.TestCase):
    def test_alerts_once_then_stays_quiet(self):
        _reset()
        rows = ([(SOUNDBAR_STA, "Soundbar")], [_ap(SOUNDBAR_AP)])
        rc, out = _main([], _pg(*rows)[0])
        self.assertEqual(rc, 0)
        self.assertIn("ALERTED: YOUR-GEAR-OPEN", out)
        msg, kw = CFG.post_both.call_args[0][0], CFG.post_both.call_args.kwargs
        self.assertIn(f"--ack {SOUNDBAR_AP}", msg)
        self.assertEqual(kw["slack_channel"], "#test-alerts")
        _, out2 = _main([], _pg(*rows)[0])
        self.assertEqual(CFG.post_both.call_count, 1)                  # alert-on-change only
        self.assertIn("no new findings (1 currently flagged)", out2)

    def test_check_mode_never_alerts_or_writes_state(self):
        _reset()
        rc, out = _main(["--check"], _pg([(SOUNDBAR_STA, "Soundbar")], [_ap(SOUNDBAR_AP)])[0])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)[0]["bssid"], SOUNDBAR_AP)
        CFG.post_both.assert_not_called()
        self.assertFalse(ra.STATE_FILE.exists())


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        # no --help: a bare run scans PG and may alert, so the smoke is an import
        r = subprocess.run([sys.executable, "-c", "import nova_rogue_ap_sentinel"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
