#!/usr/bin/env python3
"""Tests for nova_haven_drill.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). The live drill is exercised ONLY against a temp tripwire file and a
fake service_config: these tests never touch the real kill switch. Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_haven_drill.py"
spec = importlib.util.spec_from_file_location("hd_under_test", SCRIPT)
hd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hd)
SRC = SCRIPT.read_text()


class FakeCur:
    """A service_config row for autonomy/kill_switch, plus a sink for every other statement."""

    def __init__(self, value='"false"', by="cli", fail_on=None):
        self.value, self.by, self.fail_on, self.q, self._one = value, by, fail_on, [], None

    def execute(self, sql, args=None):
        self.q.append((sql, args))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("db hiccup")
        if "INSERT INTO service_config" in sql:                 # nova_autonomy_safety.engage_kill
            self.value, self.by = '"true"', args[0]
        elif "UPDATE service_config" in sql:                    # the drill's restore
            if self.by == args[1]:
                self.value, self.by = '"false"', args[0]
        elif "SELECT value, updated_by FROM service_config" in sql:
            self._one = (self.value, self.by)
        elif "SELECT value FROM service_config" in sql:
            self._one = (self.value,)

    def fetchone(self):
        return self._one

    def fetchall(self):
        return []

    @property
    def engaged(self):
        return self.value.strip('"') == "true"


def _drill(n=2):
    cl = [hd.item(f"k{i}", "automation", f"Thing {i}", domain="lights") for i in range(n)]
    for c in cl:
        c["fallback"] = "Lights: wall switches work without Nova"
    return {"id": 7, "quarter": "2026-Q4", "status": "awaiting_window", "checklist": cl, "results": {}}


class KillSwitchSandbox(unittest.TestCase):
    """Points nova_autonomy_safety.KILL_FILE at a temp path for the duration of each test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kill = os.path.join(self.tmp.name, ".autonomy-kill")
        self.p = mock.patch.object(hd.S, "KILL_FILE", self.kill)
        self.p.start()
        self.alert = mock.patch.object(hd, "_alert_restore_failed")
        self.alerted = self.alert.start()

    def tearDown(self):
        self.p.stop()
        self.alert.stop()
        self.tmp.cleanup()

    def run_live(self, cur, collect, window=30):
        return hd.live(cur, lambda: cur, _drill(), window, collect, spawn=False)


class Security(KillSwitchSandbox):
    def test_live_refused_under_scheduled(self):
        with mock.patch.object(hd, "connect") as c, mock.patch.object(hd, "live") as lv:
            rc = hd.main(["--live", "--confirm", "--scheduled"])
        self.assertEqual(rc, 2)
        c.assert_not_called()
        lv.assert_not_called()

    def test_live_refused_without_confirm(self):
        with mock.patch.object(hd, "connect") as c:
            self.assertEqual(hd.main(["--live"]), 2)
        c.assert_not_called()

    def test_scheduled_path_never_engages_the_switch(self):
        body = SRC[SRC.index("def scheduled("):SRC.index("def watchdog(")]
        self.assertNotIn("engage(", body)
        self.assertNotIn("engage_kill", body)

    def test_uses_the_existing_kill_switch_only(self):
        self.assertIn("S.engage_kill(", SRC)
        self.assertEqual(SRC.count(".autonomy-kill"), 0)          # path comes from nova_autonomy_safety
        self.assertNotRegex(SRC, r"service='(?!autonomy)\w+' AND key='kill")

    def test_refuses_to_take_over_an_already_engaged_switch(self):
        cur = FakeCur(value='"true"', by="kill:real-emergency")
        with self.assertRaises(RuntimeError):
            self.run_live(cur, lambda cl, dl: {})
        self.assertTrue(cur.engaged)
        self.assertEqual(cur.by, "kill:real-emergency")

    def test_restore_leaves_a_foreign_engagement_alone(self):
        Path(self.kill).write_text("engaged :: someone else\n")
        cur = FakeCur(value='"true"', by="kill:jordan")
        res = hd.restore_kill(cur, 7)
        self.assertTrue(os.path.exists(self.kill))
        self.assertTrue(cur.engaged)
        self.assertIn("foreign", res["file"])
        self.assertIn("foreign", res["db"])

    def test_no_hardcoded_addresses_or_secrets(self):
        self.assertNotRegex(SRC, r"\b(?:192\.168|10\.\d+)\.\d+\.\d+|/Users/|/home/|@gmail")
        self.assertNotRegex(SRC, r"(?i)(password|api_key)\s*=\s*['\"]")


class Performance(unittest.TestCase):
    def test_attach_fallbacks_is_fast(self):
        items = [hd.item(f"k{i}", "automation", f"light {i}") for i in range(500)]
        lines = [("d", f"Thing {i}: works without Nova") for i in range(2000)] + [("d", "Lights: work without Nova")]
        t = time.monotonic()
        hd.attach_fallbacks(items, {}, lines, {})
        self.assertLess(time.monotonic() - t, 5.0)


class Retry(KillSwitchSandbox):
    def test_restore_is_idempotent(self):
        cur = FakeCur()
        r = hd.Restorer(lambda: cur, 7)
        hd.engage(cur, 7)
        first = r()
        self.assertIs(r(), first)
        self.assertFalse(cur.engaged)

    def test_restore_without_database_still_clears_the_file(self):
        hd.engage(FakeCur(), 7)
        self.assertTrue(os.path.exists(self.kill))

        def boom():
            raise OSError("pg down")
        res = hd.Restorer(boom, 7)()
        self.assertEqual(res["file"], "removed")
        self.assertFalse(os.path.exists(self.kill))

    def test_watchdog_restores_an_overdue_drill(self):
        cur = FakeCur()
        hd.engage(cur, 9)
        cur.fetchall = lambda: [(9, time.time() - 60, hd.socket.gethostname())]
        self.assertEqual(hd.watchdog(cur), 1)
        self.assertFalse(cur.engaged)
        self.assertFalse(os.path.exists(self.kill))


class Unit(unittest.TestCase):
    def test_window_capped_at_two_hours(self):
        self.assertEqual(hd.clamp_window(30), 30)
        self.assertEqual(hd.clamp_window(121), 120)
        self.assertEqual(hd.clamp_window(10_000), 120)
        self.assertEqual(hd.clamp_window(0), hd.MIN_WINDOW_MIN)
        self.assertEqual(hd.clamp_window(None), 30)

    def test_quarter(self):
        self.assertEqual(hd.quarter(hd.datetime(2026, 4, 1)), "2026-Q2")

    def test_parse_results(self):
        self.assertEqual(hd.parse_results([{"key": "a", "result": "WORKED", "how": "wall switch"}]),
                         {"a": {"result": "worked", "how": "wall switch"}})
        self.assertEqual(hd.parse_results({"a": "nope"}), {})

    def test_label(self):
        self.assertEqual(hd._label("Locks / alarm / siren / garage: none exist"), "Locks / alarm / siren / garage")
        self.assertEqual(hd._label("a long sentence without a label that goes on: and on"), "")


class Integration(KillSwitchSandbox):
    def test_live_window_engages_then_restores(self):
        cur = FakeCur()
        seen = {}

        def collect(cl, deadline):
            seen["engaged"] = hd.S.kill_switch_engaged(cur)
            seen["file"] = os.path.exists(self.kill)
            return {"k0": {"result": "worked", "how": "wall switch"}, "k1": {"result": "failed", "how": ""}}
        out = self.run_live(cur, collect)
        self.assertTrue(seen["engaged"] and seen["file"])
        self.assertEqual(out["status"], "completed")
        self.assertFalse(cur.engaged)
        self.assertFalse(os.path.exists(self.kill))
        self.assertTrue(any("house_facts" in q for q, _ in cur.q))
        self.alerted.assert_not_called()

    def test_window_passed_to_deadline_is_capped(self):
        cur = FakeCur()
        dl = {}
        t0 = time.time()
        self.run_live(cur, lambda cl, d: dl.setdefault("d", d) and {}, window=999)
        self.assertLessEqual(dl["d"] - t0, 120 * 60 + 5)


class Functional(KillSwitchSandbox):
    def test_restore_on_exception_in_checklist(self):
        cur = FakeCur()

        def collect(cl, dl):
            raise ValueError("checklist blew up")
        out = self.run_live(cur, collect)
        self.assertEqual(out["status"], "aborted")
        self.assertIn("checklist blew up", out["error"])
        self.assertFalse(cur.engaged)
        self.assertFalse(os.path.exists(self.kill))

    def test_restore_on_keyboard_interrupt(self):
        cur = FakeCur()

        def collect(cl, dl):
            raise KeyboardInterrupt()
        out = self.run_live(cur, collect)
        self.assertEqual(out["status"], "aborted")
        self.assertFalse(cur.engaged)

    def test_restore_happens_even_if_recording_results_fails(self):
        cur = FakeCur(fail_on="UPDATE haven_drills SET status = %s")
        out = self.run_live(cur, lambda cl, dl: {"k0": {"result": "failed"}})
        self.assertFalse(cur.engaged)
        self.assertFalse(os.path.exists(self.kill))
        self.assertTrue(out["restore"]["ok"])

    def test_deadline_timer_restores_mid_window(self):
        cur = FakeCur()
        r = hd.Restorer(lambda: cur, 7)
        hd.engage(cur, 7)
        t = hd.threading.Timer(0.05, r)
        t.start()
        t.join(2)
        self.assertFalse(cur.engaged)

    def test_report_lists_missing_and_failed(self):
        d = _drill(3)
        d["checklist"][2]["fallback"] = hd.NO_FALLBACK
        s = hd.summarize(d["checklist"], {"k0": {"result": "worked"}, "k1": {"result": "failed"}})
        txt = hd.render(d, s)
        self.assertIn("No manual fallback recorded:\n  • Thing 2", txt)
        self.assertIn("Failed by hand:\n  • Thing 1", txt)
        self.assertEqual(s["verified_pct"], 33.3)
        self.assertEqual(s["recorded_fallback_pct"], 66.7)

    def test_fallback_needs_a_real_statement_about_nova(self):
        it = hd.attach_fallbacks([hd.item("n", "notification", "Critical alerts x", domain="notifications")], {},
                                 hd.fallback_lines({"d": "- Test any module by hand: click alerts"}), {})
        self.assertEqual(it[0]["fallback"], hd.NO_FALLBACK)

    def test_house_fact_beats_docs(self):
        it = hd.attach_fallbacks([hd.item("automation:x", "automation", "lights")], {"automation:x": "flip the switch"},
                                 [("d", "Lights: work without Nova")], {})
        self.assertEqual((it[0]["fallback"], it[0]["fallback_source"]), ("flip the switch", "house_facts"))

    def test_automation_rules_follow_actuation_helpers(self):
        src = ("def _actuation_ok(a):\n    return True\n"
               "def _hk_power(n, on):\n    _actuation_ok('p')\n"
               "async def rule_a():\n    '''Devices on.'''\n    loop.run_in_executor(None, _hk_power, 'x', True)\n"
               "async def rule_b():\n    '''Only alerts.'''\n    notify('x')\n")
        self.assertEqual(hd.automation_rules(src), [("rule_a", "Devices on.")])


class Frame(unittest.TestCase):
    def test_help_exits(self):
        with self.assertRaises(SystemExit):
            hd.main(["--help"])

    def test_selftest_passes(self):
        self.assertEqual(hd.selftest(), 0)

    def test_cli_flags_present(self):
        for f in ("--scheduled", "--dry-run", "--report", "--selftest", "--live", "--confirm", "--watchdog"):
            self.assertIn(f'"{f}"', SRC)

    def test_live_dry_run_touches_nothing(self):
        with mock.patch.object(hd, "connect") as c, mock.patch.object(hd.sys.stdin, "isatty", return_value=True):
            self.assertEqual(hd.main(["--live", "--confirm", "--dry-run"]), 0)
        c.assert_not_called()

    def test_docstring_convention_and_schema(self):
        self.assertIn("Little Mister, 2026-10-15", SRC)
        self.assertIn("Written by Jordan Koch (via Claude).", SRC)
        for col in ("scheduled_for", "status", "checklist", "results", "kill_switch_flipped"):
            self.assertRegex(hd.SCHEMA, rf"\n\s+{col}\s")


if __name__ == "__main__":
    unittest.main()
