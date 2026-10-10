#!/usr/bin/env python3
"""Tests for nova_attention_zones.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Since 2026-10-09 (organ audit M10) attention zones is a thin wrapper over
nova_escalation.jordan_state(): no HTTP server, no Redis; these tests prove it delegates."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_attention_zones.py"
SRC = SCRIPT.read_text()
CODE = SRC.split('"""', 2)[2]   # source minus the module docstring


def _load():
    spec = importlib.util.spec_from_file_location("naz_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


az = _load()
CALM = {"depleted": False, "reasons": [], "signals": {"focus": "none", "screen": "unlocked"}, "available": True}
DND = {"depleted": False, "reasons": [], "signals": {"focus": "dnd", "screen": "unlocked"}, "available": False}
LATE = {"depleted": True, "reasons": ["late night (01:10)"], "signals": {"focus": "unknown", "screen": "unknown"},
        "available": False}


class TestSecurity(unittest.TestCase):
    def test_no_server_no_redis_no_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        for banned in ("import redis", "aiohttp", "0.0.0.0", "37471", "192.168."):
            self.assertNotIn(banned, CODE, banned)

    def test_critical_always_breaks_through(self):
        for st in (DND, LATE):
            self.assertTrue(az.should_notify("critical", st))
            self.assertTrue(az.should_notify("EMERGENCY", st))


class TestPerformance(unittest.TestCase):
    def test_zone_mapping_10k(self):
        t = time.monotonic()
        for i in range(10_000):
            az.zone_for((CALM, DND, LATE)[i % 3])
            az._in_hours(i % 24, (22, 8))
        self.assertLess(time.monotonic() - t, 2.0)


class TestRetry(unittest.TestCase):
    def test_state_failure_fails_open_via_peace(self):
        # RETRY GAP: _state — delegates to nova_proactive_peace._state (one PG attempt, then jordan_state without PG)
        with patch("nova_escalation.jordan_state", side_effect=RuntimeError("boom")):
            import nova_proactive_peace as P
            st = P._state(oc=object())
        self.assertTrue(az.should_notify("info", st))


class TestUnit(unittest.TestCase):
    def test_in_hours_wraps_midnight(self):
        self.assertTrue(az._in_hours(23, (22, 8)))
        self.assertTrue(az._in_hours(3, (22, 8)))
        self.assertFalse(az._in_hours(12, (22, 8)))
        self.assertTrue(az._in_hours(9, (8, 18)))

    def test_zone_for(self):
        self.assertEqual(az.zone_for(DND), "focus")
        self.assertEqual(az.zone_for(LATE), "rest")
        self.assertIn(az.zone_for(CALM), ("work", "home"))

    def test_should_notify_follows_available(self):
        self.assertTrue(az.should_notify("info", CALM))
        self.assertFalse(az.should_notify("warning", DND))
        self.assertFalse(az.should_notify("info", LATE))


class TestIntegration(unittest.TestCase):
    def test_delegates_to_jordan_state_via_peace(self):
        self.assertIn("import nova_escalation as E", SRC)
        with patch("nova_proactive_peace._state", return_value=DND) as st:
            self.assertEqual(az.get_active_zone(), "focus")
            self.assertFalse(az.should_notify("warning"))
        self.assertEqual(st.call_count, 2)


class TestFunctional(unittest.TestCase):
    def test_main_logs_merge_and_status_prints_zone(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(az.main([]), 0)
        self.assertIn("merged into nova_escalation.jordan_state() on 2026-10-09", out.getvalue())
        with patch("nova_proactive_peace._state", return_value=LATE), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(az.main(["--status"]), 0)
        self.assertIn('"active_zone": "rest"', out.getvalue())


class TestFrame(unittest.TestCase):
    def test_help_and_plain_run_exit_zero(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        for argv in (["--help"], []):
            r = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with redirect_stdout(io.StringIO()) as out:
            _load()
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
