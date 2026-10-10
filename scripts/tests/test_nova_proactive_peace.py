#!/usr/bin/env python3
"""Tests for nova_proactive_peace.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Since 2026-10-09 (organ audit M10) proactive peace is a thin wrapper over
nova_escalation.jordan_state(); these tests prove it delegates, never posts, and writes no state."""
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
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_proactive_peace.py"
SRC = SCRIPT.read_text()
CODE = SRC.split('"""', 2)[2]   # source minus the module docstring


def _load():
    spec = importlib.util.spec_from_file_location("npp_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


npp = _load()
E = npp.E
CALM = {"depleted": False, "reasons": [], "signals": {"focus": "none", "screen": "unlocked"}, "available": True}
DND = {"depleted": False, "reasons": [], "signals": {"focus": "dnd", "screen": "unlocked"}, "available": False}
LATE = {"depleted": True, "reasons": ["late night (01:10)"], "signals": {"focus": "unknown", "screen": "unknown"},
        "available": False}


class TestSecurity(unittest.TestCase):
    def test_no_credentials_no_posting_no_flat_state(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        for banned in ("post_both", "slack_post", "write_text", "urlopen", "subprocess", "nova_peace_state.json"):
            self.assertNotIn(banned, CODE, banned)

    def test_main_never_touches_slack_or_files(self):
        with patch.object(E, "jordan_state", side_effect=AssertionError("main must not even read state")), \
             patch("builtins.open", side_effect=AssertionError("no file writes")), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(npp.main([]), 0)
        self.assertIn("merged into nova_escalation.jordan_state() on 2026-10-09", out.getvalue())


class TestPerformance(unittest.TestCase):
    def test_should_alert_10k_with_state_mocked(self):
        with patch.object(npp, "_state", return_value=CALM):
            t = time.monotonic()
            for _ in range(10_000):
                npp.should_alert()
            self.assertLess(time.monotonic() - t, 2.0)


class TestRetry(unittest.TestCase):
    def test_pg_down_fails_open_to_time_and_signals(self):
        # RETRY GAP: _state — one connect attempt (W.connect(attempts=1)); on failure jordan_state runs without PG
        w = MagicMock()
        w.connect.side_effect = OSError("pg down")
        seen = []
        with patch.dict(sys.modules, {"nova_watch_common": w}), \
             patch.object(E, "jordan_state", side_effect=lambda oc: seen.append(oc) or CALM):
            self.assertEqual(npp._state(), CALM)
        self.assertEqual(seen, [None])
        self.assertEqual(w.connect.call_args.kwargs, {"attempts": 1})

    def test_jordan_state_crash_reads_available(self):
        with patch.object(E, "jordan_state", side_effect=RuntimeError("boom")):
            self.assertTrue(npp._state(oc=object())["available"])


class TestUnit(unittest.TestCase):
    def test_should_alert_maps_jordan_state(self):
        for st, want in ((CALM, (True, "available")), (DND, (False, "macOS Focus: dnd")),
                         (LATE, (False, "late night (01:10)"))):
            with patch.object(npp, "_state", return_value=st):
                self.assertEqual(npp.should_alert(), want)

    def test_focus_and_screen_delegate(self):
        with patch.object(E, "jordan_signals", return_value={"focus": "work", "screen": "locked"}):
            self.assertEqual((npp.get_focus_mode(), npp.get_screen_state()), ("work", "locked"))


class TestIntegration(unittest.TestCase):
    def test_uses_escalation_not_its_own_detection(self):
        self.assertIn("import nova_escalation as E", SRC)
        self.assertNotIn("def detect_burnout_signals", SRC)
        self.assertNotIn("HOLD_QUEUE", SRC)

    def test_state_closes_its_connection(self):
        conn = MagicMock()
        w = MagicMock()
        w.connect.return_value = conn
        with patch.dict(sys.modules, {"nova_watch_common": w}), patch.object(E, "jordan_state", return_value=CALM) as js:
            npp._state()
        self.assertIs(js.call_args.args[0], conn.cursor.return_value)
        conn.close.assert_called_once()


class TestFunctional(unittest.TestCase):
    def test_check_and_status(self):
        with patch.object(npp, "_state", return_value=DND), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(npp.main(["--check"]), 0)
            self.assertEqual(npp.main(["--status"]), 0)
        self.assertIn("NO — macOS Focus: dnd", out.getvalue())
        self.assertIn('"focus": "dnd"', out.getvalue())

    def test_retired_queue_flags_are_harmless(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(npp.main(["--release"]), 0)
        self.assertIn("the hold queue never existed", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_help_and_plain_run_exit_zero(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("merged", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with redirect_stdout(io.StringIO()) as out:
            _load()
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
