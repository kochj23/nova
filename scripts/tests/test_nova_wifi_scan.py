#!/usr/bin/env python3
"""Tests for nova_wifi_scan.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_wifi_scan.py"
SRC = SCRIPT.read_text()
_TMP = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("nws_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ws = _load()
# stub every outbound side effect at module load: Keychain, notify; log to a tempdir
ws.get_unifi_key = MagicMock(return_value="test-key")
ws.notify = MagicMock()
ws.LOG_FILE = Path(_TMP.name) / "wifi.log"


class _Cur:
    def __init__(self, known_sec, known):
        self.reads = [list(known_sec.items()), [(b,) for b in known]]
        self.inserts = []; self._last = None

    def execute(self, sql, params=None):
        if sql.startswith("INSERT"):
            self.inserts.append(params)
        else:
            self._last = self.reads.pop(0)

    def fetchall(self):
        return self._last

    def close(self): pass


def _ap(bssid, sec, essid="Net", ch=6):
    return {"bssid": bssid, "essid": essid, "security": sec, "signal": -60, "channel": ch, "radio": "ng"}


def _run(neighbors, ours, known_sec=None, known=()):
    cur = _Cur(known_sec or {}, known)
    conn = MagicMock(); conn.cursor.return_value = cur
    import psycopg2
    with patch.object(ws, "fetch_neighbors", return_value=neighbors), \
         patch.object(ws, "fetch_our_bssids", return_value=ours), \
         patch.object(psycopg2, "connect", return_value=conn):
        rc = ws.main()
    return rc, cur


def _resp(obj):
    r = MagicMock(); r.__enter__.return_value.read.return_value = json.dumps(obj).encode(); return r


class _Base(unittest.TestCase):
    def setUp(self):
        ws.notify.reset_mock(side_effect=True)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('_keychain("nova-unifi-api-key"', SRC)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        rc, cur = _run([_ap("AA:BB'); --", "WPA2")], set())
        self.assertEqual(cur.inserts[0][1], "aa:bb'); --")

    def test_api_key_sent_as_header_not_url(self):
        with patch.object(ws.urllib.request, "urlopen", return_value=_resp({"data": []})) as u:
            ws.fetch_neighbors()
        req = u.call_args[0][0]
        self.assertEqual(req.get_header("X-api-key"), "test-key")
        self.assertNotIn("test-key", req.full_url)


class TestPerformance(_Base):
    def test_10k_aps_fast(self):
        aps = [_ap(f"00:00:00:00:{i // 256:02x}:{i % 256:02x}", "WPA2") for i in range(10_000)]
        t0 = time.perf_counter()
        rc, cur = _run(aps, set())
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(cur.inserts), 10_000)


class TestRetry(_Base):
    def test_unifi_failure_returns_1_without_pg(self):
        # RETRY GAP: fetch_neighbors()/urlopen — one attempt; failure logs and exits 1, PG never opened
        import psycopg2
        with patch.object(ws.urllib.request, "urlopen", side_effect=OSError("controller down")) as u, \
             patch.object(psycopg2, "connect") as pc:
            self.assertEqual(ws.main(), 1)
        self.assertEqual(u.call_count, 1)
        pc.assert_not_called()
        self.assertIn("UniFi fetch failed", ws.LOG_FILE.read_text())

    def test_notify_failure_swallowed(self):
        ws.notify.side_effect = RuntimeError("bus down")
        rc, _ = _run([_ap("aa", "OPEN")], set(), known_sec={"aa": "WPA2"}, known={"aa"})
        self.assertEqual(rc, 0)


class TestUnit(_Base):
    def test_our_bssids_lowercased(self):
        dev = {"data": [{"vap_table": [{"bssid": "AA:BB"}, {"bssid": None}]}, {"vap_table": None}]}
        with patch.object(ws.urllib.request, "urlopen", return_value=_resp(dev)):
            self.assertEqual(ws.fetch_our_bssids(), {"aa:bb"})

    def test_downgrade_rules(self):
        cases = [("WPA3", "WPA2", True), ("WPA2", "OPEN", True), ("WPA2", "WEP", True),
                 ("WPA2", "WPA3", False), ("WPA2", "WPA2", False)]
        for old, new, flagged in cases:
            ws.notify.reset_mock()
            _run([_ap("aa", new)], set(), known_sec={"aa": old}, known={"aa"})
            self.assertEqual(ws.notify.called, flagged, (old, new))


class TestIntegration(_Base):
    def test_ours_tracked_but_never_flagged(self):
        rc, cur = _run([_ap("AA", "OPEN"), _ap("bb", "WPA2")], {"aa"}, known_sec={"aa": "WPA3"}, known={"aa"})
        self.assertEqual([p[6] for p in cur.inserts], [True, False])     # is_ours column
        ws.notify.assert_not_called()

    def test_reads_14_day_history_from_wifi_aps(self):
        self.assertIn("FROM wifi_aps WHERE ts > now() - interval '14 days'", SRC)
        self.assertIn("INSERT INTO wifi_aps", SRC)


class TestFunctional(_Base):
    def test_golden_path_new_and_downgrade(self):
        aps = [_ap("aa", "OPEN", "CoffeeShop"), _ap("cc", "WPA2", "Neighbor"), _ap("", "WPA2")]
        rc, cur = _run(aps, set(), known_sec={"aa": "WPA2"}, known={"aa"})
        self.assertEqual(rc, 0)
        self.assertEqual(len(cur.inserts), 2)                   # blank bssid skipped
        body = ws.notify.call_args.kwargs["body"]
        self.assertIn("CoffeeShop [aa] WPA2 -> OPEN", body)
        self.assertIn("1 new AP(s)", ws.LOG_FILE.read_text())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # any invocation reads the UniFi key from Keychain, so the frame check is an import smoke
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_wifi_scan"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
