#!/usr/bin/env python3
"""Tests for nova_vault7_ttp.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Read-only detection rules over a fake PG connection; notify/triage/maintenance are mocked file-wide."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_vault7_ttp.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("vault7_ttp_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v7 = _load()
_PATCHES = []
_QUIET = lambda m: None  # noqa: E731


def setUpModule():
    maint = MagicMock(); maint.is_active.return_value = False
    for p in (patch.object(v7, "notify", side_effect=AssertionError("unmocked notify")),
              patch.object(v7, "triage", None), patch.object(v7, "nova_maintenance", maint),
              patch.object(v7.psycopg2, "connect", side_effect=AssertionError("unmocked PG"))):
        p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


class _Cur:
    def __init__(self, routes, log):
        self.routes, self.log, self._rows = routes, log, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        self._rows = []
        for needle, rows in self.routes:
            if needle in sql:
                self._rows = rows(params) if callable(rows) else rows
                return

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _Conn:
    """routes: list of (sql-substring, rows-or-callable); first match wins."""
    def __init__(self, routes=()):
        self.routes, self.sql = list(routes), []

    def cursor(self, cursor_factory=None):
        return _Cur(self.routes, self.sql)

    def close(self):
        pass


def _ev(agent="nova-core", desc="", log="", groups=("syscheck",), src_ip=None):
    return {"id": 1, "agent_name": agent, "rule_description": desc, "full_log": log, "rule_groups": list(groups),
            "src_ip": src_ip, "ts": datetime(2026, 1, 1)}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_read_only_no_writes(self):
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE\s+\w+\s+SET|DELETE FROM|DROP\s)", SRC))
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))

    def test_maintenance_window_suppresses_scan(self):
        maint = MagicMock(); maint.is_active.return_value = True
        conn = _Conn()
        with patch.object(v7, "nova_maintenance", maint):
            self.assertEqual(v7.scan(conn, do_alert=True, logger=_QUIET), [])
        self.assertEqual(conn.sql, [])

    def test_dedup_lookup_parameterized(self):
        conn = _Conn([("telemetry.events", [(1,)])])
        self.assertTrue(v7._recently_alerted(conn, "vault7:x:'; SELECT 1;--"))
        sql, params = conn.sql[0]
        self.assertNotIn("SELECT 1;--", sql)
        self.assertEqual(params[1], 24)


class TestPerformance(unittest.TestCase):
    def test_classify_10k_names_fast(self):
        names = ["Samsung-TV", "nest-cam-front", "MacBook-Pro", "esp32-plug", "bambu-p1s", "mystery"] * 1700
        t0 = time.perf_counter()
        out = [v7.classify_device(n) for n in names]
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(out[:6], ["smart-tv-av", "camera", "trusted", "iot", "printer", "unknown"])


class TestRetry(unittest.TestCase):
    def test_failing_rule_skipped_others_still_run(self):
        # RETRY GAP: scan() — each rule queries once; a failing rule is logged + skipped, the rest still run
        msgs = []
        boom = MagicMock(side_effect=RuntimeError("pg timeout")); boom.__name__ = "rule_boom"
        ok = MagicMock(return_value=[{"rule": "r", "host": "H 1", "level": "info", "title": "t", "detail": "d", "why": "w"}])
        with patch.object(v7, "RULES", [boom, ok]):
            out = v7.scan(_Conn(), logger=msgs.append)
        self.assertEqual(boom.call_count, 1)
        self.assertEqual(out[0]["dedup_key"], "vault7:r:h_1")
        self.assertIn("rule_boom failed (skipped)", msgs[0])

    def test_triage_failure_still_emits(self):
        f = {"rule": "r", "host": "h", "level": "warning", "title": "t", "detail": "d", "why": "w"}
        with patch.object(v7, "RULES", [lambda c: [dict(f)]]), patch.object(v7, "triage", side_effect=RuntimeError("llm")), \
                patch.object(v7, "notify") as n:
            v7.scan(_Conn(), do_alert=True, logger=_QUIET)
        self.assertEqual(n.call_args.kwargs["level"], "warning")


class TestUnit(unittest.TestCase):
    def test_classify_edges(self):
        self.assertEqual(v7.classify_device(""), "unknown")
        self.assertEqual(v7.classify_device("Unknown"), "unknown")
        self.assertEqual(v7.classify_device("nova-core4"), "trusted")
        self.assertEqual(v7.classify_device("Living-Room-Sonos"), "smart-tv-av")

    def test_firmware_rule_dpkg_explains_fim_but_not_rootkit(self):
        fim = _ev(desc="Integrity checksum changed", log="/boot/vmlinuz-6.8")
        rk = _ev(desc="rootkit file found", log="/lib/modules/x.ko", groups=("rootcheck",))
        conn = _Conn([("rule_groups && ARRAY['dpkg'", [(1,)]), ("syscheck", [fim, rk])])
        out = v7.rule_firmware_tamper(conn)
        self.assertEqual([f["level"] for f in out], ["warning"])
        self.assertIn("(rootcheck)", out[0]["title"])

    def test_antiforensic_regex(self):
        conn = _Conn([("security_events", [_ev(desc="auditd service stopped"), _ev(desc="user logged in")])])
        out = v7.rule_antiforensic_gap(conn)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["rule"], "v7_antiforensic_gap")


class TestIntegration(unittest.TestCase):
    def test_beacon_joins_network_class_with_events(self):
        conn = _Conn([("telemetry.network", [{"ip": "10.0.0.9", "client_name": "esp32-plug"},
                                             {"ip": "10.0.0.2", "client_name": "MacBook"}]),
                      ("security_events", [_ev(desc="Suspicious DNS query evil.xyz", src_ip="10.0.0.9"),
                                           _ev(desc="Suspicious DNS query evil.xyz", src_ip="10.0.0.2")])])
        out = v7.rule_implant_beacon(conn)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["host"], "esp32-plug (10.0.0.9)")

    def test_fakeoff_only_av_and_persistence_trusted_on_iot(self):
        tv = {"client_mac": "aa", "name": "Samsung-TV", "rtx": 100 * 2**20, "tx_sum": 1, "rx_sum": 1, "nights": 2, "btx": 4 * 2**20}
        cam = {**tv, "name": "nest-cam"}
        self.assertEqual([f["host"] for f in v7.rule_smart_tv_fakeoff(_Conn([("WITH q AS", [tv, cam])]))], ["Samsung-TV"])
        conn = _Conn([("security_events", [_ev(desc="New systemd service unit added")]),
                      ("essid = ANY", lambda p: [{"client_name": "MacBook-Pro", "essid": p[0][0], "ip": "1.2.3.4"},
                                                 {"client_name": "hue-bridge", "essid": "KOCH-IOT", "ip": "1.2.3.5"}])])
        out = v7.rule_rogue_persistence(conn)
        self.assertEqual(len(out), 2)
        self.assertIn("MacBook-Pro @ KOCH-IOT", out[1]["title"])


class TestFunctional(unittest.TestCase):
    def test_alert_path_dedups_and_notifies(self):
        f = {"rule": "v7_x", "host": "core", "level": "info", "title": "T", "detail": "D", "why": "W"}
        conn = _Conn([("telemetry.events", lambda p: [(1,)] if p[0] == "vault7:v7_x:old" else [])])
        triage = MagicMock(return_value={"annotation": "triaged", "level": "warning"})
        with patch.object(v7, "RULES", [lambda c: [dict(f), {**f, "host": "old"}]]), patch.object(v7, "triage", triage), \
                patch.object(v7, "notify") as n:
            out = v7.scan(conn, do_alert=True, logger=_QUIET)
        self.assertEqual(len(out), 2)
        self.assertEqual(n.call_count, 1)                      # 'old' already alerted in 24h
        kw = n.call_args.kwargs
        self.assertEqual((kw["level"], kw["category"], kw["dedup_key"]), ("warning", "security", "vault7:v7_x:core"))
        self.assertTrue(kw["body"].endswith("triaged"))

    def test_scan_without_alert_never_notifies(self):
        with patch.object(v7, "RULES", [lambda c: [{"rule": "r", "host": "h", "level": "info", "title": "t", "detail": "d", "why": "w"}]]), \
                patch.object(v7, "notify") as n:
            self.assertEqual(len(v7.scan(_Conn(), logger=_QUIET)), 1)
        n.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_list_exits_zero(self):
        r = subprocess.run([sys.executable, str(PATH), "--list"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.count("v7_"), 5)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_vault7_ttp"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
