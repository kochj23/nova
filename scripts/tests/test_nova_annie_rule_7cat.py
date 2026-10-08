#!/usr/bin/env python3
"""7-category tests for nova_annie_rule.py (the Annie Wilkes rule) — complements
test_nova_annie_rule.py with evasion, ReDoS, consent-store failure, and the
check() contract as nova_reach consumes it. Security, Performance, Retry, Unit,
Integration, Functional, Frame. No DB, no Slack. Written by Jordan Koch."""
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_annie_rule as ar  # noqa: E402

SCRIPT = SCRIPTS / "nova_annie_rule.py"


class _BrokenCur:
    def execute(self, *a, **k):
        raise OSError("pg down")

    def fetchone(self):
        raise OSError("pg down")


class _ConsentCur:
    def __init__(self, value):
        self.value = value

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return (self.value,) if self.value is not None else None


NUDGE = "You should get more sleep tonight, the backup can wait."


# ── Security ─────────────────────────────────────────────────────────────────
class TestSecurity(unittest.TestCase):
    def test_typographic_apostrophes_do_not_evade(self):
        for t in ("You haven’t replied to my note.", "I’ve been waiting for you to answer.",
                  "You still havenʼt answered."):
            self.assertFalse(ar.ok(t), t)

    def test_whitespace_and_case_do_not_evade(self):
        for t in ("you   haven't\n replied yet", "WHERE HAVE YOU BEEN?", "Left me on read again."):
            self.assertFalse(ar.ok(t), t)

    def test_non_string_inputs_are_safe(self):
        self.assertTrue(ar.check(None)["ok"])
        self.assertTrue(ar.check("")["ok"])

    def test_evidence_is_truncated(self):
        r = ar.check("where have you been " + "x" * 5000)
        self.assertLessEqual(len(r["evidence"]["absence-guilt"]), 120)

    def test_module_does_no_io_at_import(self):
        src = SCRIPT.read_text()
        for bad in ("psycopg2", "urllib", "requests", "subprocess", "open("):
            self.assertNotIn(bad, src.split('if __name__')[0])


# ── Performance ──────────────────────────────────────────────────────────────
class TestPerformance(unittest.TestCase):
    def test_no_catastrophic_backtracking_on_adversarial_input(self):
        evil = ("you " * 20000) + ("haven't " * 5000) + "!"
        t = time.monotonic()
        ar.absence_guilt(evil)
        self.assertLess(time.monotonic() - t, 1.0)

    def test_check_batch_bounded(self):
        with mock.patch("nova_safety_guards.nudge_allowed", return_value=False):
            t = time.monotonic()
            for i in range(500):
                ar.check(f"The NAS has {i} GB free and the backup finished cleanly.")
            self.assertLess(time.monotonic() - t, 3.0)


# ── Retry (no external calls of its own; the consent store is the only I/O, via
# the safety lane — it must fail CLOSED, never silently allow) ───────────────
class TestRetry(unittest.TestCase):
    def test_consent_store_down_blocks_health_nudge(self):
        r = ar.check(NUDGE, oc=_BrokenCur())
        self.assertFalse(r["ok"]); self.assertIn("health-nudge-no-consent", r["flags"])

    def test_safety_lane_manipulation_check_raising_propagates_not_swallowed(self):
        with mock.patch("nova_safety_guards.manipulation_check", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                ar.check("hello there")


# ── Unit ─────────────────────────────────────────────────────────────────────
class TestUnit(unittest.TestCase):
    def test_check_contract_shape(self):
        r = ar.check("Where have you been?")
        self.assertEqual(set(r), {"ok", "flags", "evidence"})
        self.assertIn("absence-guilt", r["flags"]); self.assertIn("absence-guilt", r["evidence"])

    def test_consent_check_can_be_skipped(self):
        self.assertTrue(ar.check(NUDGE, oc=_BrokenCur(), consent_check=False)["ok"])

    def test_consent_values(self):
        self.assertTrue(ar.ok(NUDGE, oc=_ConsentCur('"true"')))
        self.assertFalse(ar.ok(NUDGE, oc=_ConsentCur("false")))
        self.assertFalse(ar.ok(NUDGE, oc=_ConsentCur(None)))

    def test_ok_wraps_check(self):
        self.assertIs(ar.ok("The backup finished."), True)


# ── Integration ──────────────────────────────────────────────────────────────
class TestIntegration(unittest.TestCase):
    def test_flags_merge_from_safety_lane_and_absence(self):
        fake = types.SimpleNamespace(
            manipulation_check=lambda t: {"ok": False, "flags": ["guilt-hook"], "evidence": {"guilt-hook": "x"}},
            is_health_nudge=lambda t: False, nudge_allowed=lambda oc=None: False)
        with mock.patch.dict(sys.modules, {"nova_safety_guards": fake}):
            r = ar.check("Where have you been?")
        self.assertEqual(r["flags"], ["guilt-hook", "absence-guilt"])

    def test_prompt_rule_is_what_reach_imports(self):
        import nova_reach
        self.assertEqual(nova_reach._ANNIE_PROMPT, ar.PROMPT_RULE)


# ── Functional ───────────────────────────────────────────────────────────────
class TestFunctional(unittest.TestCase):
    def test_golden_path_neutral_observation_passes(self):
        self.assertEqual(ar.check("The porch camera went offline at 09:12 and came back at 09:15.",
                                  oc=_ConsentCur(None)), {"ok": True, "flags": [], "evidence": {}})

    def test_error_path_guilt_plus_nudge_reports_both(self):
        r = ar.check("I haven't heard from you in days. You should get more sleep tonight.", oc=_ConsentCur(None))
        self.assertFalse(r["ok"])
        self.assertIn("absence-guilt", r["flags"]); self.assertIn("health-nudge-no-consent", r["flags"])


# ── Frame ────────────────────────────────────────────────────────────────────
class TestFrame(unittest.TestCase):
    def test_imports_and_api(self):
        for n in ("check", "ok", "absence_guilt", "PROMPT_RULE"):
            self.assertTrue(hasattr(ar, n))

    def test_cli_stdin(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], input="The NAS is fine.", capture_output=True,
                           text=True, timeout=30, cwd=str(SCRIPTS))
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("'ok'", r.stdout)


if __name__ == "__main__":
    unittest.main()
