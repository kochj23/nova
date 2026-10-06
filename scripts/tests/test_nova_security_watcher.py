#!/usr/bin/env python3
"""Tests for nova_security_watcher.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import hashlib
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
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_security_watcher.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    cfg = types.ModuleType("nova_config"); cfg.SLACK_NOTIFY = "C_TEST"; cfg.post_both = mock.MagicMock()
    with mock.patch.dict(sys.modules, {"nova_config": cfg}):
        spec.loader.exec_module(mod)
    return mod


SW = _load("security_watcher_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="secwatch-test-"))
SW.LOG_FILE = TMP / "security_watcher.log"
SW.STATE_FILE = TMP / "state" / "seen.json"
SW.JOURNAL_SCRIPT = TMP / "nova_journal_security.py"


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _today(offset_days=0):
    return (datetime.now() - timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _quake(eid, mag, lat, lon, place):
    return {"id": eid, "properties": {"mag": mag, "place": place}, "geometry": {"coordinates": [lon, lat, 10.0]}}


class _Quiet(unittest.TestCase):
    def setUp(self):
        self._buf = io.StringIO(); self._rs = redirect_stdout(self._buf); self._rs.__enter__()

    def tearDown(self):
        self._rs.__exit__(None, None, None)


class TestSecurity(_Quiet):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_psql_sql_is_static_and_argv_only(self):
        self.assertNotIn("shell=True", SRC)
        m = re.search(r'\["psql".*?\]', SRC, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn("{", m.group(0))                      # no interpolation into the SQL
        self.assertIn("source = 'intelligence'", m.group(0))

    def test_fire_alert_passes_untrusted_text_as_argv(self):
        with mock.patch.object(SW.subprocess, "run") as sp:
            SW.fire_alert("x; rm -rf /", "$(whoami) `id`")
        cmd = sp.call_args[0][0]
        self.assertEqual(cmd[:3], [sys.executable, str(SW.JOURNAL_SCRIPT), "breaking"])
        self.assertEqual(cmd[3:], ["x; rm -rf /", "$(whoami) `id`"])
        self.assertNotIn("shell", sp.call_args.kwargs)

    def test_outbound_requests_identify_themselves(self):
        self.assertEqual(SRC.count('"User-Agent": "Nova-SecWatch/1.0'), 3)


class TestPerformance(_Quiet):
    def test_feed_scan_over_10k_lines(self):
        lines = [f"[Feed{i}] Story {i}: nothing to see here at all|{{}}" for i in range(10_000)]
        lines[7777] = "[CISA] Actively exploited bug: zero-day in widget|{}"
        out = "\n".join(lines)
        seen = set()
        with mock.patch.object(SW.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout=out)):
            t0 = time.perf_counter()
            alerts = SW.check_critical_feeds(seen)
            dt = time.perf_counter() - t0
        self.assertLess(dt, 3.0)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(len(seen), 1)


class TestRetry(_Quiet):
    def test_each_feed_is_one_shot_and_fails_open(self):
        # RETRY GAP: check_cisa_kev / check_earthquakes / check_nws_alerts — one urlopen each; failure -> []
        with mock.patch.object(SW.urllib.request, "urlopen", side_effect=OSError("tls")) as uo:
            self.assertEqual(SW.check_cisa_kev(set()), [])
            self.assertEqual(SW.check_earthquakes(set()), [])
            self.assertEqual(SW.check_nws_alerts(set()), [])
        self.assertEqual(uo.call_count, 3)
        self.assertIn("fetch failed", self._buf.getvalue())

    def test_feed_psql_failure_fails_open(self):
        # RETRY GAP: check_critical_feeds — psql tried once; rc!=0 or exception -> []
        with mock.patch.object(SW.subprocess, "run", return_value=types.SimpleNamespace(returncode=2, stdout="")):
            self.assertEqual(SW.check_critical_feeds(set()), [])
        with mock.patch.object(SW.subprocess, "run", side_effect=subprocess.TimeoutExpired("psql", 15)):
            self.assertEqual(SW.check_critical_feeds(set()), [])

    def test_fire_alert_failure_is_logged_not_raised(self):
        # RETRY GAP: fire_alert — a failed journal subprocess is logged and the run continues
        with mock.patch.object(SW.subprocess, "run", side_effect=OSError("boom")):
            SW.fire_alert("t", "d")
        self.assertIn("Alert fire failed", self._buf.getvalue())


class TestUnit(_Quiet):
    def test_cisa_kev_recent_only_last_20(self):
        vulns = [{"cveID": f"CVE-0-{i}", "dateAdded": _today(30)} for i in range(25)]
        vulns.append({"cveID": "CVE-NEW", "dateAdded": _today(1), "vendorProject": "V", "product": "P",
                      "shortDescription": "d", "requiredAction": "patch"})
        vulns.append({"cveID": "CVE-BAD-DATE", "dateAdded": "nope", "vendorProject": "V", "product": "P"})
        seen = set()
        with mock.patch.object(SW.urllib.request, "urlopen", return_value=_Resp({"vulnerabilities": vulns})):
            alerts = SW.check_cisa_kev(seen)
        self.assertEqual([a[0] for a in alerts], ["CISA KEV Addition — CVE-NEW (V P)", "CISA KEV Addition — CVE-BAD-DATE (V P)"])
        self.assertIn("Required Action: patch", alerts[0][1])
        self.assertEqual(len(seen), 20)                          # only the tail is examined, all of it marked seen
        self.assertNotIn("kev-CVE-0-0", seen)
        with mock.patch.object(SW.urllib.request, "urlopen", return_value=_Resp({"vulnerabilities": vulns})):
            self.assertEqual(SW.check_cisa_kev(seen), [])        # dedup on the second pass

    def test_earthquake_thresholds(self):
        feats = [_quake("la4", 4.1, 34.0, -118.2, "Los Angeles"), _quake("la3", 3.9, 34.0, -118.2, "LA"),
                 _quake("ca6", 6.2, 37.7, -122.4, "San Francisco, California"), _quake("ca5", 5.5, 37.7, -122.4, "CA"),
                 _quake("jp7", 7.1, 36.0, 140.0, "Japan")]
        seen = set()
        with mock.patch.object(SW.urllib.request, "urlopen", return_value=_Resp({"features": feats})):
            alerts = SW.check_earthquakes(seen)
        self.assertEqual(sorted(a[0].split(" — ")[1] for a in alerts), ["Japan", "Los Angeles", "San Francisco, California"])
        self.assertEqual(seen, {"eq-la4", "eq-ca6", "eq-jp7"})
        self.assertIn("depth 10.0km", alerts[0][1])

    def test_nws_severe_filter(self):
        feats = [{"properties": {"id": "a", "event": "Red Flag Warning", "severity": "Moderate", "headline": "RFW", "description": "dry"}},
                 {"properties": {"id": "b", "event": "Small Craft Advisory", "severity": "Minor"}},
                 {"properties": {"id": "c", "event": "Heat Advisory", "severity": "Extreme", "description": "x" * 400}}]
        seen = set()
        with mock.patch.object(SW.urllib.request, "urlopen", return_value=_Resp({"features": feats})):
            alerts = SW.check_nws_alerts(seen)
        self.assertEqual([a[0] for a in alerts], ["NWS Alert: RFW", "NWS Alert: Heat Advisory"])
        self.assertLess(len(alerts[1][1]), 400)                  # description capped at 300
        self.assertEqual(len(seen), 2)

    def test_feed_title_extraction_and_hash_dedup(self):
        line = "[BleepingComputer] Zero-day in Widget: actively exploited by APT28.|{\"url\":\"x\"}"
        seen = set()
        with mock.patch.object(SW.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout=line + "\n\n")):
            alerts = SW.check_critical_feeds(seen)
            again = SW.check_critical_feeds(seen)
        self.assertEqual(alerts[0][0], "BleepingComputer: Zero-day in Widget")
        self.assertEqual(again, [])
        h = hashlib.md5(line.split("|")[0].strip().lower()[:100].encode()).hexdigest()[:10]
        self.assertEqual(seen, {h})

    def test_seen_state_round_trip_and_cap(self):
        self.assertEqual(SW.load_seen(), set())
        SW.save_seen({f"k{i}" for i in range(600)})
        self.assertEqual(len(SW.load_seen()), 500)
        SW.STATE_FILE.write_text("{broken")
        self.assertEqual(SW.load_seen(), set())


class TestIntegration(_Quiet):
    def test_run_chains_checks_into_state_and_alerts(self):
        SW.STATE_FILE.unlink(missing_ok=True)
        def _kev(seen): seen.add("kev-X"); return [("KEV X", "d1")]
        def _eq(seen): seen.add("eq-Y"); return [("EQ Y", "d2")]
        with mock.patch.object(SW, "check_cisa_kev", side_effect=_kev), mock.patch.object(SW, "check_critical_feeds", return_value=[]), \
             mock.patch.object(SW, "check_earthquakes", side_effect=_eq), mock.patch.object(SW, "check_nws_alerts", return_value=[]), \
             mock.patch.object(SW, "fire_alert") as fa, mock.patch.object(SW.time, "sleep") as sl:
            SW.run()
        self.assertEqual([c[0][0] for c in fa.call_args_list], ["KEV X", "EQ Y"])
        self.assertEqual(sl.call_count, 2)
        self.assertEqual(SW.load_seen(), {"kev-X", "eq-Y"})

    def test_la_bounding_box_matches_the_checks(self):
        self.assertTrue(SW.LA_LAT_MIN < 34.05 < SW.LA_LAT_MAX and SW.LA_LON_MIN < -118.25 < SW.LA_LON_MAX)
        self.assertIn("zone=CAZ041", SRC)                        # NWS zone is LA county


class TestFunctional(_Quiet):
    def test_run_caps_at_three_alerts(self):
        many = [(f"T{i}", f"D{i}") for i in range(6)]
        with mock.patch.object(SW, "check_cisa_kev", return_value=many), mock.patch.object(SW, "check_critical_feeds", return_value=[]), \
             mock.patch.object(SW, "check_earthquakes", return_value=[]), mock.patch.object(SW, "check_nws_alerts", return_value=[]), \
             mock.patch.object(SW.subprocess, "run") as sp, mock.patch.object(SW.time, "sleep"):
            SW.run()
        self.assertEqual(sp.call_count, 3)
        self.assertEqual([c[0][0][3] for c in sp.call_args_list], ["T0", "T1", "T2"])
        self.assertIn("Found 6 alert(s)", self._buf.getvalue())
        self.assertIn("FIRING ALERT: T0", SW.LOG_FILE.read_text())

    def test_run_quiet_when_everything_is_down(self):
        with mock.patch.object(SW.urllib.request, "urlopen", side_effect=OSError("offline")), \
             mock.patch.object(SW.subprocess, "run", side_effect=OSError("no psql")) as sp:
            SW.run()
        self.assertIn("No critical events detected", self._buf.getvalue())
        self.assertFalse(any("breaking" in str(c) for c in sp.call_args_list))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_security_watcher"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
