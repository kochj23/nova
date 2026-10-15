#!/usr/bin/env python3
"""Tests for nova_continuity_review.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import re
import sys
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_continuity_review.py"
spec = importlib.util.spec_from_file_location("cr_under_test", SCRIPT)
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)
SRC = SCRIPT.read_text()


def _metrics(**over):
    m = {"callback_rate": {"value": 0.517, "detail": None, "caveat": "proxy"},
         "beliefs": 819, "beliefs_replaced": 120, "beliefs_with_evidence": 0, "sparks": 52,
         "private_notebook": 94, "gravel_kept": 20, "gravel_flagged": 190, "never_recalled_pct": 96.41,
         "memories_total": 2844140, "recalled_last_30d": 84858,
         "unclaimed": {"runs_30d": 1833, "succeeded": 1819, "hours_30d": 16.7, "minutes_per_day": 32,
                       "yielded_to_commissioned": 398},
         "unclaimed_outcomes_30d": {"pursuit": 439, "fizzled": 69, "quiet": 39},
         "skill_runs_30d": {"noted": 120, "fizzled": 26}, "restraint_30d": 546, "threads_3plus": 37,
         "threads_10plus": 28, "taste": 31, "taste_retired": 0,
         "preoccupations": {"active": 43, "closed": 1, "detected_from_ingest_volume": 33},
         "column_days_30d": 30, "herd_reaches_sent_30d": 12, "not_instrumented": list(cr.NOT_INSTRUMENTED)}
    m.update(over)
    return m


class Security(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|app_pass|api_key)\s*=\s*['\"][^'\"]+['\"]")

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(f\"[^\"]*\{(month|today|row)")

    def test_mail_goes_through_herd_mail_wrapper_only(self):
        self.assertNotIn("smtplib", SRC)
        self.assertIn("nova_herd_mail.sh", SRC)

    def test_no_private_addresses_or_paths_in_output(self):
        body = cr.render(_metrics(), date(2026, 10, 1))
        self.assertNotRegex(body, r"192\.168\.|/Users/|/home/")


class Performance(unittest.TestCase):
    def test_render_is_fast(self):
        t = time.monotonic()
        for _ in range(500):
            cr.render(_metrics(), date(2026, 10, 1), _metrics())
        self.assertLess(time.monotonic() - t, 2.0)


class Retry(unittest.TestCase):
    def test_mail_failure_returns_false(self):
        fake = mock.Mock(returncode=1, stderr="smtp down", stdout="")
        with mock.patch.object(cr.subprocess, "run", return_value=fake):
            self.assertFalse(cr.mail_witness("https://x/", date(2026, 10, 1), "body", to="w@example.org"))

    def test_mail_success(self):
        with mock.patch.object(cr.subprocess, "run", return_value=mock.Mock(returncode=0)) as r:
            self.assertTrue(cr.mail_witness("https://x/", date(2026, 10, 1), "body", to="w@example.org"))
        args = r.call_args[0][0]
        self.assertIn("w@example.org", args)
        self.assertIn("--haiku", args)     # Jordan's standing policy for herd mail

    def test_no_witness_address_means_not_mailed(self):
        with mock.patch.object(cr, "witness_address", return_value=None):
            self.assertFalse(cr.mail_witness("https://x/", date(2026, 10, 1), "body"))

    def test_due_keeps_retrying_until_mailed(self):
        self.assertTrue(cr.due(date(2026, 11, 15), have_row=False))
        self.assertTrue(cr.due(date(2026, 11, 30), have_row=False))


class Unit(unittest.TestCase):
    def test_due_window(self):
        self.assertFalse(cr.due(date(2026, 11, 13), False))
        self.assertTrue(cr.due(date(2026, 11, 14), False))
        self.assertFalse(cr.due(date(2026, 11, 14), True))
        self.assertTrue(cr.due(date(2026, 11, 2), True, force=True))

    def test_pct(self):
        self.assertEqual(cr._pct(0.517), "51.7%")
        self.assertEqual(cr._pct(96.41), "96.4%")
        self.assertEqual(cr._pct(None), "—")


class Integration(unittest.TestCase):
    def test_measure_reads_every_metric_with_fake_cursors(self):
        class Cur:
            def __init__(self): self.q = []
            def execute(self, sql, args=None): self.q.append(sql); self.last = sql
            def fetchone(self):
                if "access_count,0)=0" in self.last: return (10, 100)
                if "FROM scheduler_runs WHERE task_id='unclaimed_time'" in self.last: return (10, 9, 2.0, 3, 2)
                if "FROM preoccupations" in self.last: return (5, 1, 3)
                if "turing_scoreboard" in self.last: return (0.5, {})
                return (7,)
            def fetchall(self): return [("noted", 4)]
        oc, mc = Cur(), Cur()
        m = cr.measure(oc, mc)
        self.assertEqual(m["never_recalled_pct"], 10.0)
        self.assertEqual(m["unclaimed"]["minutes_per_day"], 60)
        self.assertTrue(all("UPDATE" not in q and "DELETE" not in q for q in oc.q + mc.q))


class Functional(unittest.TestCase):
    def test_render_lists_baseline_and_missing_instruments(self):
        body = cr.render(_metrics(), date(2026, 10, 1))
        self.assertIn("approximately zero", body)
        self.assertIn("99% (99.13%)", body)
        self.assertIn("Still not instrumented", body)
        for x in cr.NOT_INSTRUMENTED:
            self.assertIn(x, body)

    def test_render_compares_with_last_month(self):
        prev = _metrics(beliefs=700)
        body = cr.render(_metrics(), date(2026, 11, 1), prev)
        self.assertRegex(body, r"\| Belief ledger entries \| a few dozen \| 700 \| 819 \|")


class Frame(unittest.TestCase):
    def test_help_and_scheduled_flag(self):
        with self.assertRaises(SystemExit):
            cr.main(["--help"])

    def test_scheduler_entry_exists(self):
        cfg = (SCRIPTS.parent / "config" / "scheduler-core.yaml").read_text()
        self.assertRegex(cfg, r"continuity_review:[^\n]*\n\s+script: nova_continuity_review\.py")

    def test_title_passes_journal_title_guard(self):
        self.assertFalse(re.match(r"(?i)(i|let|here|sure|okay|alright)\b", "Continuity Review, November 2026"))
        self.assertIn('title = f"Continuity Review,', SRC)


if __name__ == "__main__":
    unittest.main()
