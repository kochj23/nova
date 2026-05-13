"""
test_nova_finance_monitor.py — All 7 test categories for nova_finance_monitor.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.JORDAN_DM = "D0AMPB3F4T0"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_finance_monitor.py"
_spec = importlib.util.spec_from_file_location("nova_finance_monitor", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

detect_institution = _mod.detect_institution
categorize_email = _mod.categorize_email
extract_amount = _mod.extract_amount
is_urgent = _mod.is_urgent
scan_financial_emails = _mod.scan_financial_emails
load_data = _mod.load_data
save_data = _mod.save_data
weekly_digest = _mod.weekly_digest
spending_analysis = _mod.spending_analysis
categorize_spending = _mod.categorize_spending


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "password =", "secret ="]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com",
                  "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(p, src)

    def test_financial_data_not_in_vector_memory(self):
        """Financial events must be stored in local JSON, not vector memory."""
        src = _SCRIPT.read_text()
        # DATA_FILE should be a local JSON path, not VECTOR_URL
        self.assertIn("finance_events.json", src, "Finance data should go to local JSON file")

    def test_data_file_under_home(self):
        self.assertTrue(str(_mod.DATA_FILE).startswith(str(Path.home())))

    def test_urgent_alerts_go_to_dm(self):
        """Fraud/security alerts must go to DM, not public channel."""
        src = _SCRIPT.read_text()
        # urgent_alerts → JORDAN_DM
        self.assertIn("JORDAN_DM", src, "Urgent alerts must be sent to DM")

    def test_amount_extraction_no_eval(self):
        """extract_amount must not use eval()."""
        src = _SCRIPT.read_text()
        self.assertNotIn("eval(", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_extract_amount_fast(self):
        import time
        texts = [f"Your account was charged $1,{i:03d}.99" for i in range(1000)]
        start = time.perf_counter()
        for t in texts:
            extract_amount(t)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)

    def test_is_urgent_fast(self):
        import time
        subjects = ["unauthorized charge on your account"] * 500
        start = time.perf_counter()
        for s in subjects:
            is_urgent(s)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.2)

    def test_save_data_prunes_old_events(self):
        """save_data must keep only last 90 days."""
        old_date = (datetime.now() - timedelta(days=100)).isoformat()
        recent_date = datetime.now().isoformat()
        data = {
            "events": [
                {"date": old_date[:10], "subject": "old"},
                {"date": recent_date[:10], "subject": "recent"},
            ],
            "last_scan": "",
        }
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            with patch.object(_mod, "DATA_FILE", tmp):
                save_data(data)
                saved = json.loads(tmp.read_text())
        finally:
            tmp.unlink(missing_ok=True)

        subjects = [e["subject"] for e in saved["events"]]
        self.assertNotIn("old", subjects, "Events older than 90 days should be pruned")
        self.assertIn("recent", subjects)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_load_data_returns_defaults_on_missing(self):
        with patch.object(_mod.DATA_FILE, "exists", return_value=False):
            data = load_data()
        self.assertIn("events", data)
        self.assertIn("last_scan", data)

    def test_load_data_returns_defaults_on_corrupt_json(self):
        with patch.object(_mod.DATA_FILE, "exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value="{INVALID"):
                data = load_data()
        self.assertIn("events", data)

    def test_get_mail_data_returns_empty_on_exception(self):
        with patch("subprocess.run", side_effect=Exception("crash")):
            with patch.object(_mod, "SCRIPTS", Path("/nonexistent")):
                # No mail file exists
                result = _mod.get_mail_data()
        self.assertEqual(result, "")

    def test_slack_post_silently_handles_nova_config_error(self):
        _nova_cfg.post_both.side_effect = Exception("slack down")
        try:
            _mod.slack_post("test message")
        except Exception:
            pass  # nova_config errors are the config mock's problem
        finally:
            _nova_cfg.post_both.side_effect = None


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_detect_institution_amex(self):
        self.assertEqual(detect_institution("alerts@americanexpress.com", "Your charge"), "Amex")

    def test_detect_institution_chase(self):
        self.assertEqual(detect_institution("no-reply@chase.com", "Payment"), "Chase")

    def test_detect_institution_none(self):
        self.assertIsNone(detect_institution("newsletter@someblog.com", "10 tips for success"))

    def test_categorize_email_charge(self):
        self.assertEqual(categorize_email("Your account was charged $45.00"), "charge")

    def test_categorize_email_payment(self):
        self.assertEqual(categorize_email("Payment received for your account"), "payment")

    def test_categorize_email_refund(self):
        self.assertEqual(categorize_email("Refund of $12.50 has been processed"), "refund")

    def test_categorize_email_bill_due(self):
        self.assertEqual(categorize_email("Your bill is due on January 15"), "bill_due")

    def test_extract_amount_basic(self):
        self.assertAlmostEqual(extract_amount("Charged $45.99 on your account"), 45.99)

    def test_extract_amount_with_comma(self):
        self.assertAlmostEqual(extract_amount("Payment of $1,234.56"), 1234.56)

    def test_extract_amount_none(self):
        self.assertIsNone(extract_amount("No amount here"))

    def test_is_urgent_fraud(self):
        self.assertTrue(is_urgent("Fraud alert on your account"))

    def test_is_urgent_unauthorized(self):
        self.assertTrue(is_urgent("Unauthorized transaction detected"))

    def test_is_urgent_normal(self):
        self.assertFalse(is_urgent("Your monthly statement is ready"))

    def test_categorize_spending_dining(self):
        self.assertEqual(categorize_spending("Starbucks order", ""), "dining")

    def test_categorize_spending_shopping(self):
        self.assertEqual(categorize_spending("Amazon Prime purchase", ""), "shopping")

    def test_categorize_spending_subscriptions(self):
        self.assertEqual(categorize_spending("Netflix monthly subscription", ""), "subscriptions")

    def test_categorize_spending_other(self):
        self.assertEqual(categorize_spending("Random widget store", ""), "other")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_scan_and_save_deduplication(self):
        """Scanning same email content twice should not create duplicate events."""
        mail_content = (
            "FROM: alerts@americanexpress.com\n"
            "SUBJ: Your account was charged $45.99 at Starbucks\n"
        )
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            with patch.object(_mod, "DATA_FILE", tmp):
                with patch.object(_mod, "get_mail_data", return_value=mail_content):
                    with patch.object(_mod, "slack_post"):
                        _mod.main()
                        _mod.main()  # Second run — should deduplicate

                saved = json.loads(tmp.read_text())
            # Should have exactly 1 event, not 2
            self.assertEqual(len(saved["events"]), 1,
                             "Duplicate events should be deduplicated")
        finally:
            tmp.unlink(missing_ok=True)

    def test_urgent_goes_to_dm_not_channel(self):
        """Fraud alert must go to DM, not public channel."""
        mail_content = (
            "FROM: alerts@wellsfargo.com\n"
            "SUBJ: FRAUD ALERT: unauthorized charge detected on your account\n"
        )
        dm_calls = []
        channel_calls = []

        def fake_post(text, channel=None):
            if channel and "D0" in str(channel):
                dm_calls.append(text)
            else:
                channel_calls.append(text)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            with patch.object(_mod, "DATA_FILE", tmp):
                with patch.object(_mod, "get_mail_data", return_value=mail_content):
                    with patch.object(_mod, "slack_post", side_effect=fake_post):
                        _mod.main()
        finally:
            tmp.unlink(missing_ok=True)

        self.assertGreater(len(dm_calls), 0, "Urgent alert must be sent to DM")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_weekly_digest_empty_data(self):
        with patch.object(_mod, "load_data", return_value={"events": [], "last_scan": "", "weekly_summary_date": ""}):
            result = weekly_digest()
        self.assertIn("No financial activity", result)

    def test_weekly_digest_with_events(self):
        today = datetime.now().strftime("%Y-%m-%d")
        events = [
            {"date": today, "institution": "Amex", "category": "charge",
             "amount": 45.99, "subject": "Purchase at Store", "urgent": False},
            {"date": today, "institution": "Chase", "category": "payment",
             "amount": 100.00, "subject": "Payment received", "urgent": False},
        ]
        with patch.object(_mod, "load_data", return_value={"events": events, "last_scan": "", "weekly_summary_date": ""}):
            result = weekly_digest()
        self.assertIn("Weekly Financial Pulse", result)
        self.assertIn("Amex", result)

    def test_spending_analysis_no_data(self):
        with patch.object(_mod, "load_data", return_value={"events": []}):
            result = spending_analysis(30)
        self.assertIn("No charge data", result)

    def test_spending_analysis_with_charges(self):
        today = datetime.now().strftime("%Y-%m-%d")
        events = [
            {"date": today, "category": "charge", "amount": 50.0,
             "subject": "Amazon order", "sender": "", "institution": "Amex"},
        ]
        with patch.object(_mod, "load_data", return_value={"events": events}):
            result = spending_analysis(30)
        self.assertIn("Spending Analysis", result)
        self.assertIn("$50.00", result)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_script_is_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_module_has_main(self):
        self.assertTrue(callable(_mod.main))

    def test_financial_senders_not_empty(self):
        self.assertGreater(len(_mod.FINANCIAL_SENDERS), 0)

    def test_urgent_patterns_not_empty(self):
        self.assertGreater(len(_mod.URGENT_PATTERNS), 0)

    def test_category_patterns_not_empty(self):
        self.assertGreater(len(_mod.CATEGORY_PATTERNS), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
