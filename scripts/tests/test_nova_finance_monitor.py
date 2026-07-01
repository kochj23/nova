"""test_nova_finance_monitor.py — Tests for nova_finance_monitor.py.

Focus (per task):
  - Unit: extract_amount / categorize_email parsers + the 90-day retention
    cutoff (Tier-A off-by-one on the date boundary).
  - Security invariant: financial PII is kept OUT of the vector / any
    cloud-bound search sink on purpose. The module persists to a LOCAL JSON
    file only; assert no vector/remember/embedding sink exists or is invoked.

External deps (nova_config, nova_notify, subprocess, filesystem) are mocked;
no live service or DB is touched. There is no real retry loop in this module
(get_mail_data does a single subprocess call), so no retry test is written —
noted in the task return.

Written by Jordan Koch.
"""

import importlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

MODULE_NAME = "nova_finance_monitor"
MODULE_PATH = SCRIPTS_DIR / "nova_finance_monitor.py"


@pytest.fixture
def fin(mock_nova_config, monkeypatch):
    """Import nova_finance_monitor with mocked nova_config + nova_notify."""
    mock_notify = MagicMock()
    mock_notify.notify = MagicMock()
    monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
    monkeypatch.setitem(sys.modules, "nova_notify", mock_notify)
    if MODULE_NAME in sys.modules:
        del sys.modules[MODULE_NAME]
    mod = importlib.import_module(MODULE_NAME)
    mod._mock_notify = mock_notify  # stash for assertions
    return mod


# ────────────────────────────────────────────────────────────────────────────
# Unit: extract_amount
# ────────────────────────────────────────────────────────────────────────────

class TestExtractAmount:
    @pytest.mark.parametrize("text,expected", [
        ("$1,234.56", 1234.56),
        ("Payment of $1,000.00 posted", 1000.00),
        ("$50", 50.0),
        ("You saved $5 today", 5.0),
        ("$0.99 charge", 0.99),
        ("Total: $1,234,567.89", 1234567.89),
    ])
    def test_extracts_dollar_amounts(self, fin, text, expected):
        assert fin.extract_amount(text) == expected

    def test_no_amount_returns_none(self, fin):
        assert fin.extract_amount("no money mentioned here") is None

    def test_empty_string_returns_none(self, fin):
        assert fin.extract_amount("") is None

    def test_first_amount_wins(self, fin):
        # AMOUNT_PATTERN.search returns the first match in the string.
        assert fin.extract_amount("charged $10.00 then refunded $99.99") == 10.00

    def test_bare_dollar_sign_returns_none(self, fin):
        # "$" with no digits should not match the [\d,]+ requirement.
        assert fin.extract_amount("cost is $ unknown") is None


# ────────────────────────────────────────────────────────────────────────────
# Unit: categorize_email
# ────────────────────────────────────────────────────────────────────────────

class TestCategorizeEmail:
    @pytest.mark.parametrize("subject,expected", [
        ("Your card was charged $50 at Target", "charge"),
        ("You spent $12 at the store", "charge"),
        ("Payment received — thank you", "payment"),
        ("Autopay confirmed", "payment"),
        ("Refund issued to your account", "refund"),
        ("Your FICO score changed", "credit_score"),
        ("Your credit score updated", "credit_score"),
        ("Your bill is due tomorrow", "bill_due"),
        ("Statement ready to view", "bill_due"),
        ("You sent $20 to a friend", "transfer"),
        ("Important account notification", "alert"),
        ("Hello from a friend", "other"),
    ])
    def test_categorization(self, fin, subject, expected):
        assert fin.categorize_email(subject) == expected

    def test_is_case_insensitive(self, fin):
        assert fin.categorize_email("REFUND POSTED") == "refund"

    def test_category_priority_charge_before_transfer(self, fin):
        # "purchase" (charge) should win even if other words present.
        assert fin.categorize_email("purchase transfer notice") == "charge"


# ────────────────────────────────────────────────────────────────────────────
# Unit: detect_institution / is_urgent / categorize_spending
# ────────────────────────────────────────────────────────────────────────────

class TestDetectInstitution:
    @pytest.mark.parametrize("sender,subject,expected", [
        ("alerts@americanexpress.com", "A charge", "Amex"),
        ("no-reply@chase.com", "", "Chase"),
        ("service@capitalone.com", "", "Capital One"),
        ("notify@venmo.com", "You got paid", "Venmo"),
        ("", "Your Wells Fargo statement", "Wells Fargo"),
    ])
    def test_detects_known_institutions(self, fin, sender, subject, expected):
        assert fin.detect_institution(sender, subject) == expected

    def test_unknown_returns_none(self, fin):
        assert fin.detect_institution("friend@gmail.com", "lunch plans") is None


class TestIsUrgent:
    @pytest.mark.parametrize("subject", [
        "Fraud alert on your account",
        "Unauthorized transaction detected",
        "Suspicious activity on your card",
        "Security alert: sign in",
        "Your account locked",
        "Please verify your identity",
    ])
    def test_urgent_true(self, fin, subject):
        assert fin.is_urgent(subject) is True

    @pytest.mark.parametrize("subject", [
        "Your payment posted",
        "Statement ready",
        "You spent $5 at coffee shop",
    ])
    def test_urgent_false(self, fin, subject):
        assert fin.is_urgent(subject) is False


class TestCategorizeSpending:
    @pytest.mark.parametrize("subject,sender,expected", [
        ("Starbucks purchase", "", "dining"),
        ("AMAZON.COM order", "", "shopping"),
        ("Netflix subscription renewal", "", "subscriptions"),
        ("Shell gas station", "", "auto"),
        ("Edison electric bill", "", "utilities"),
        ("CVS pharmacy pickup", "", "health"),
        ("Home Depot receipt", "", "home"),
        ("Mystery merchant xyz", "", "other"),
    ])
    def test_spending_categories(self, fin, subject, sender, expected):
        assert fin.categorize_spending(subject, sender) == expected


# ────────────────────────────────────────────────────────────────────────────
# Unit: 90-day retention cutoff — Tier-A off-by-one on the date boundary
# ────────────────────────────────────────────────────────────────────────────

class TestRetentionCutoff:
    def _run_save(self, fin, tmp_path, now, events):
        fin.NOW = now
        fin.DATA_FILE = tmp_path / "finance_events.json"
        data = {"events": list(events), "last_scan": "", "weekly_summary_date": ""}
        fin.save_data(data)
        return json.loads(fin.DATA_FILE.read_text())

    def test_boundary_uses_date_only_cutoff(self, fin, tmp_path):
        # NOW fixed; cutoff = (NOW - 90d).date() -> keep events strictly newer.
        now = datetime(2026, 6, 26, 12, 0, 0)
        cutoff_date = (now - timedelta(days=90)).date().isoformat()  # 2026-03-28
        assert cutoff_date == "2026-03-28"

        events = [
            {"date": cutoff_date, "subject": "on the cutoff day"},          # pruned (> is strict)
            {"date": "2026-03-29", "subject": "one day inside window"},     # kept
            {"date": "2026-06-26", "subject": "today"},                     # kept
            {"date": "2026-01-01", "subject": "way too old"},               # pruned
        ]
        out = self._run_save(fin, tmp_path, now, events)
        kept = {e["subject"] for e in out["events"]}
        assert kept == {"one day inside window", "today"}

    def test_date_only_cutoff_not_pruned_early_by_timestamp(self, fin, tmp_path):
        # Regression for the documented Tier-A bug: the cutoff must be a
        # DATE-only string (10 chars), never a full ISO timestamp. A day-89
        # event that shares the cutoff's calendar day but has NO time component
        # would sort *before* a "...T12:00:00" timestamp cutoff and be pruned
        # a day early. The date-only cutoff keeps it.
        now = datetime(2026, 6, 26, 23, 59, 59)
        eighty_nine = (now - timedelta(days=89)).date().isoformat()  # 2026-03-29
        events = [{"date": eighty_nine, "subject": "89 days ago"}]
        out = self._run_save(fin, tmp_path, now, events)
        assert [e["subject"] for e in out["events"]] == ["89 days ago"]

        # The buggy variant compares the event date against the cutoff's *own*
        # calendar day carrying a time suffix — a date-only string always sorts
        # before that suffix, so the same-day event would be wrongly pruned.
        buggy_cutoff = eighty_nine + "T00:00:01"   # timestamp on the event's day
        assert not (eighty_nine > buggy_cutoff)     # date-only < timestamp -> pruned early
        # The real (date-only) cutoff is a bare 10-char date, so no early prune.
        real_cutoff = (now - timedelta(days=90)).date().isoformat()
        assert len(real_cutoff) == 10 and "T" not in real_cutoff

    def test_missing_date_field_is_pruned(self, fin, tmp_path):
        now = datetime(2026, 6, 26, 12, 0, 0)
        events = [{"subject": "no date field"}, {"date": "2026-06-25", "subject": "recent"}]
        out = self._run_save(fin, tmp_path, now, events)
        assert [e["subject"] for e in out["events"]] == ["recent"]


# ────────────────────────────────────────────────────────────────────────────
# scan_financial_emails — end-to-end parse of the mail summary format
# ────────────────────────────────────────────────────────────────────────────

class TestScanFinancialEmails:
    def test_parses_financial_event(self, fin, monkeypatch):
        content = (
            "[UNREAD] FROM: alerts@americanexpress.com\n"
            "SUBJ: A $123.45 charge was made\n"
            "[READ] FROM: friend@gmail.com\n"
            "SUBJ: lunch tomorrow?\n"
        )
        monkeypatch.setattr(fin, "get_mail_data", lambda: content)
        events = fin.scan_financial_emails()
        assert len(events) == 1
        e = events[0]
        assert e["institution"] == "Amex"
        assert e["category"] == "charge"
        assert e["amount"] == 123.45
        assert e["urgent"] is False

    def test_flags_urgent_fraud_email(self, fin, monkeypatch):
        content = (
            "FROM: fraud@chase.com\n"
            "SUBJ: Fraud alert: unauthorized charge of $500.00\n"
        )
        monkeypatch.setattr(fin, "get_mail_data", lambda: content)
        events = fin.scan_financial_emails()
        assert len(events) == 1
        assert events[0]["urgent"] is True
        assert events[0]["amount"] == 500.00

    def test_ignores_non_financial(self, fin, monkeypatch):
        content = "FROM: newsletter@example.com\nSUBJ: Weekly digest of cat photos\n"
        monkeypatch.setattr(fin, "get_mail_data", lambda: content)
        assert fin.scan_financial_emails() == []

    def test_empty_mail_returns_empty(self, fin, monkeypatch):
        monkeypatch.setattr(fin, "get_mail_data", lambda: "")
        assert fin.scan_financial_emails() == []


# ────────────────────────────────────────────────────────────────────────────
# SECURITY INVARIANT: financial PII never reaches a vector / cloud search sink
# ────────────────────────────────────────────────────────────────────────────

class TestNoVectorSink:
    def test_module_has_no_vector_remember_references(self, fin):
        """The module must not import or reference any vector/embedding sink.

        This is the core design invariant: financial events are stored in a
        LOCAL JSON file, deliberately kept out of the searchable vector memory.
        """
        src = MODULE_PATH.read_text()
        forbidden = ["remember(", "VECTOR_URL", "/remember", "embedding",
                     "qdrant", "memory_remember", "vector_memory", "upsert("]
        hits = [tok for tok in forbidden if tok in src]
        assert hits == [], f"finance module references a vector sink: {hits}"

    def test_module_exposes_no_remember_symbol(self, fin):
        assert not hasattr(fin, "remember")
        assert not hasattr(fin, "memory_remember")

    def test_data_file_is_local_path_not_url(self, fin):
        p = str(fin.DATA_FILE)
        assert not p.startswith("http")
        assert "workspace" in p and p.endswith("finance_events.json")

    def test_pii_amount_stays_in_local_file_only(self, fin, tmp_path, monkeypatch):
        """A charge with a dollar amount is persisted locally and the ONLY
        outbound sinks are Slack/notify — never a vector remember call."""
        fin.NOW = datetime(2026, 6, 26, 12, 0, 0)
        fin.TODAY = "2026-06-26"
        fin.DATA_FILE = tmp_path / "finance_events.json"

        secret_event = {
            "date": "2026-06-26", "time": "12:00", "institution": "Amex",
            "category": "charge", "subject": "Charge of $4,321.00 at Jeweler",
            "amount": 4321.00, "urgent": False, "sender": "alerts@amex.com",
        }
        monkeypatch.setattr(fin, "scan_financial_emails", lambda: [secret_event])

        # Give nova_config a vector-style sink that must NEVER be called.
        fin.nova_config.remember = MagicMock()
        fin.nova_config.post_both = MagicMock()

        fin.main()

        # PII landed in the local file...
        saved = json.loads(fin.DATA_FILE.read_text())
        assert any(e["amount"] == 4321.00 for e in saved["events"])
        # ...and the vector sink was never touched.
        fin.nova_config.remember.assert_not_called()

    def test_urgent_pii_goes_to_dm_not_vector(self, fin, tmp_path, monkeypatch):
        fin.NOW = datetime(2026, 6, 26, 12, 0, 0)
        fin.TODAY = "2026-06-26"
        fin.DATA_FILE = tmp_path / "finance_events.json"
        urgent = {
            "date": "2026-06-26", "time": "12:00", "institution": "Chase",
            "category": "alert", "subject": "Fraud: unauthorized $999.00",
            "amount": 999.00, "urgent": True, "sender": "fraud@chase.com",
        }
        monkeypatch.setattr(fin, "scan_financial_emails", lambda: [urgent])
        fin.nova_config.remember = MagicMock()
        fin.nova_config.post_both = MagicMock()

        fin.main()

        # Urgent alert posted to Slack DM (post_both), not to any vector store.
        assert fin.nova_config.post_both.called
        fin.nova_config.remember.assert_not_called()
        # Notify (regular activity channel) not used for the urgent-only batch.
        # Local file still holds the PII.
        saved = json.loads(fin.DATA_FILE.read_text())
        assert any(e["amount"] == 999.00 for e in saved["events"])


# ────────────────────────────────────────────────────────────────────────────
# Dedup: main() must not re-add an already-seen event
# ────────────────────────────────────────────────────────────────────────────

class TestDedup:
    def test_existing_event_not_duplicated(self, fin, tmp_path, monkeypatch):
        fin.NOW = datetime(2026, 6, 26, 12, 0, 0)
        fin.TODAY = "2026-06-26"
        fin.DATA_FILE = tmp_path / "finance_events.json"
        ev = {
            "date": "2026-06-26", "time": "12:00", "institution": "Amex",
            "category": "charge", "subject": "Charge of $10.00", "amount": 10.0,
            "urgent": False, "sender": "alerts@amex.com",
        }
        # Pre-seed the data file with the same event.
        fin.DATA_FILE.write_text(json.dumps(
            {"events": [ev], "last_scan": "", "weekly_summary_date": ""}))
        monkeypatch.setattr(fin, "scan_financial_emails", lambda: [dict(ev)])
        fin.nova_config.post_both = MagicMock()

        fin.main()
        saved = json.loads(fin.DATA_FILE.read_text())
        assert len(saved["events"]) == 1
