"""Tests for nova_finance_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Original focus (kept intact below the pytest-style classes):

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


# ════════════════════════════════════════════════════════════════════════════
# The 7 house categories — self-contained unittest classes (no pytest fixtures),
# so `python3 tests/test_nova_finance_monitor.py` is green too.
# ════════════════════════════════════════════════════════════════════════════
import importlib.util
import io
import os
import subprocess
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

SRC = MODULE_PATH.read_text()


def _stub_modules():
    cfg = types.ModuleType("nova_config")
    cfg.JORDAN_DM = "D_TEST_DM"; cfg.SLACK_NOTIFY = "C_TEST_NOTIFY"; cfg.NOVA_HOST = "127.0.0.1"
    cfg.post_both = MagicMock()
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock()
    return {"nova_config": cfg, "nova_notify": nn}


def _load7(name):
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()):
        spec.loader.exec_module(mod)
    return mod


F7 = _load7("finance_monitor_7cat")
TMP7 = Path(tempfile.mkdtemp(prefix="finance-7cat-"))
F7.DATA_FILE = TMP7 / "finance_events.json"                 # never the real workspace file
F7.SCRIPTS = TMP7                                            # never the real nova_mail_fetch.py
F7.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=OSError("offline: subprocess stubbed")))
F7.notify = MagicMock()
F7.NOW = datetime(2026, 10, 5, 9, 30, 0); F7.TODAY = "2026-10-05"

MAIL = ("[UNREAD] FROM: alerts@americanexpress.com\nSUBJ: A $123.45 charge was made at Starbucks\n"
        "[READ] FROM: fraud@chase.com\nSUBJ: Fraud alert: unauthorized charge of $500.00\n"
        "[READ] FROM: friend@gmail.com\nSUBJ: lunch tomorrow?\n")


def _ev(**kw):
    e = {"date": "2026-10-05", "time": "09:30", "institution": "Amex", "category": "charge",
         "subject": "Charge of $10.00 at Starbucks", "amount": 10.0, "urgent": False, "sender": "alerts@amex.com"}
    e.update(kw); return e


def _seed(events):
    F7.DATA_FILE.write_text(json.dumps({"events": events, "last_scan": "", "weekly_summary_date": ""}))


def _main(mail=MAIL):
    F7.nova_config.post_both = MagicMock(); F7.notify = MagicMock()
    with patch.object(F7, "get_mail_data", lambda: mail), redirect_stdout(io.StringIO()) as out:
        F7.main()
    return json.loads(F7.DATA_FILE.read_text()), out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_no_sql_no_vector_sink_local_file_only(self):
        self.assertNotIn("psycopg2", SRC); self.assertNotIn("execute(", SRC)
        self.assertNotIn("/remember", SRC); self.assertNotIn("urlopen", SRC)
        self.assertTrue(str(F7.DATA_FILE).startswith(str(TMP7)))

    def test_mail_fetch_is_argv_list_never_shell_and_never_a_mailbox(self):
        self.assertIn('["python3", str(SCRIPTS / "nova_mail_fetch.py")]', SRC)
        self.assertNotIn("shell=True", SRC); self.assertNotIn("Library/Mail", SRC); self.assertNotIn("osascript", SRC)

    def test_subjects_are_truncated_before_persisting_and_posting(self):
        _seed([])
        long_subj = "Charge of $5.00 " + "x" * 500
        data, _ = _main(f"FROM: alerts@chase.com\nSUBJ: {long_subj}\n")
        self.assertEqual(len(data["events"][0]["subject"]), 120)
        body = F7.notify.call_args[1]["body"]
        self.assertLess(len(body), 120)


class TestPerformance(unittest.TestCase):
    def test_classifiers_fast_on_10k_subjects(self):
        subjects = [f"Your card was charged ${i}.00 at Starbucks fraud alert {i}" for i in range(10_000)]
        t0 = time.perf_counter()
        for s in subjects:
            F7.categorize_email(s); F7.extract_amount(s); F7.is_urgent(s); F7.detect_institution("x@chase.com", s)
            F7.categorize_spending(s, "")
        self.assertLess(time.perf_counter() - t0, 3.0)

    def test_analysis_over_10k_events_fast(self):
        _seed([_ev(date=f"2026-{9 + i % 2:02d}-{1 + i % 28:02d}", amount=float(i % 300), subject=f"Charge {i} at Amazon") for i in range(10_000)])
        t0 = time.perf_counter()
        F7.spending_analysis(60); F7.cash_flow_forecast(); F7.weekly_digest(); F7.monthly_comparison()
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_mail_fetch_fails_open_to_empty(self):
        # RETRY GAP: get_mail_data — one subprocess attempt; failure returns "" and the scan reports nothing
        run = MagicMock(side_effect=subprocess.TimeoutExpired("x", 150))
        with patch.object(F7.subprocess, "run", run), patch.object(F7.Path, "home", classmethod(lambda c: TMP7)):
            self.assertEqual(F7.get_mail_data(), "")
            self.assertEqual(F7.scan_financial_emails(), [])
        self.assertEqual(run.call_count, 2)

    def test_fresh_summary_file_skips_the_subprocess(self):
        home = TMP7 / "home"; sf = home / ".openclaw/workspace/state/nova_mail_fetch.txt"
        sf.parent.mkdir(parents=True, exist_ok=True); sf.write_text("FROM: a@chase.com\nSUBJ: x\n")
        run = MagicMock()
        with patch.object(F7.subprocess, "run", run), patch.object(F7.Path, "home", classmethod(lambda c: home)):
            self.assertEqual(F7.get_mail_data(), "FROM: a@chase.com\nSUBJ: x\n")
        run.assert_not_called()

    def test_corrupt_data_file_fails_open_to_empty_state(self):
        # RETRY GAP: load_data — a corrupt JSON file is silently replaced by an empty state (no retry, no backup)
        F7.DATA_FILE.write_text("{not json")
        self.assertEqual(F7.load_data(), {"events": [], "last_scan": "", "weekly_summary_date": ""})
        # notify/post_both failures are NOT caught by main(): one attempt, exception escapes
        _seed([]); F7.notify = MagicMock(side_effect=OSError("slack down")); F7.nova_config.post_both = MagicMock()
        with patch.object(F7, "get_mail_data", lambda: "FROM: a@amex.com\nSUBJ: charge $1.00\n"), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                F7.main()
        self.assertEqual(len(json.loads(F7.DATA_FILE.read_text())["events"]), 1)   # saved before the post


class TestUnit(unittest.TestCase):
    def test_parsers_edges(self):
        self.assertIsNone(F7.extract_amount("")); self.assertEqual(F7.extract_amount("$1,000"), 1000.0)
        self.assertEqual(F7.categorize_email(""), "other"); self.assertFalse(F7.is_urgent(""))
        self.assertIsNone(F7.detect_institution("", ""))
        self.assertEqual(F7.categorize_spending("", ""), "other")

    def test_save_data_prunes_90_days_and_creates_parent(self):
        f = TMP7 / "deep/finance.json"
        with patch.object(F7, "DATA_FILE", f):
            F7.save_data({"events": [_ev(date="2026-01-01"), _ev(date="2026-10-04")]})
            self.assertEqual([e["date"] for e in F7.load_data()["events"]], ["2026-10-04"])

    def test_weekly_digest_and_analysis_text(self):
        _seed([_ev(amount=50.0), _ev(category="payment", amount=20.0, subject="Payment received"),
               _ev(category="credit_score", subject="Your FICO score changed", amount=None),
               _ev(category="bill_due", subject="Payment due", amount=99.0, institution="Chase")])
        d = F7.weekly_digest()
        self.assertIn("Total charges: *$50.00*", d); self.assertIn("Total payments: *$20.00*", d)
        self.assertIn("Amex: 3 event(s), $70.00", d); self.assertIn("📊 Your FICO score changed", d)
        self.assertIn("📅 [Chase] Payment due $99.00", d); self.assertIn("4 total financial events", d)
        _seed([])
        self.assertIn("No financial activity", F7.weekly_digest())
        self.assertIn("No charge data", F7.spending_analysis()); self.assertIn("Not enough data", F7.cash_flow_forecast())

    def test_spending_analysis_trend_and_anomaly(self):
        evs = [_ev(date=f"2026-10-0{i}", amount=10.0, subject=f"Starbucks {i}") for i in range(1, 5)]
        evs += [_ev(date="2026-10-05", amount=900.0, subject="Big charge at Jeweler")]
        _seed(evs)
        s = F7.spending_analysis(30)
        self.assertIn("Total: *$940.00* across 5 transactions", s)
        self.assertIn("Dining: $40.00", s); self.assertIn("trending UP", s)
        self.assertIn("⚠️ $900.00 — Big charge at Jeweler [Amex]", s)

    def test_cash_flow_and_monthly_comparison(self):
        _seed([_ev(amount=15.99, subject="Netflix"), _ev(date="2026-09-20", amount=15.99, subject="Netflix"),
               _ev(category="payment", amount=100.0), _ev(date="2026-09-10", amount=200.0, subject="x")])
        c = F7.cash_flow_forecast()
        self.assertIn("Avg monthly outflow: *$115.99*", c); self.assertIn("Avg monthly inflow: *$50.00*", c)
        self.assertIn("$15.99 — Amex (2x in 60 days)", c); self.assertIn("Estimated recurring monthly: $15.99", c)
        m = F7.monthly_comparison()
        self.assertIn("2026-10 vs 2026-09", m); self.assertIn("This month: $15.99", m); self.assertIn("Last month: $215.99", m)
        self.assertIn("Change: DOWN 92.6%", m)


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_used_not_reimplemented(self):
        self.assertIn("from nova_notify import notify", SRC); self.assertIn("nova_config.post_both(", SRC)
        self.assertNotIn("def notify", SRC); self.assertNotIn("def post_both", SRC)
        self.assertEqual(F7.JORDAN_DM, "D_TEST_DM")

    def test_scan_then_dedup_chain(self):
        _seed([])
        data, _ = _main()
        self.assertEqual([e["institution"] for e in data["events"]], ["Amex", "Chase"])
        data2, out = _main()                                          # same mail again -> nothing new
        self.assertEqual(len(data2["events"]), 2); self.assertIn("No new financial events", out)
        F7.notify.assert_not_called(); F7.nova_config.post_both.assert_not_called()

    def test_slack_post_routes_to_default_channel(self):
        F7.nova_config.post_both = MagicMock()
        F7.slack_post("hi"); F7.nova_config.post_both.assert_called_with("hi", slack_channel="C_TEST_NOTIFY")


class TestFunctional(unittest.TestCase):
    def test_golden_path_urgent_to_dm_and_activity_to_bus(self):
        _seed([])
        data, out = _main()
        msg, kw = F7.nova_config.post_both.call_args[0][0], F7.nova_config.post_both.call_args[1]
        self.assertTrue(msg.startswith("*FINANCIAL SECURITY ALERT*")); self.assertIn("Chase: *Fraud alert", msg)
        self.assertIn("($500.00)", msg); self.assertEqual(kw["slack_channel"], "D_TEST_DM")
        title = F7.notify.call_args[0][0]; kw2 = F7.notify.call_args[1]
        self.assertEqual(title, "Financial Activity — 09:30 AM")
        self.assertIn("💳 [Amex] A $123.45 charge was made at Starbucks $123.45", kw2["body"])
        self.assertEqual((kw2["category"], kw2["dedup_key"]), ("finance", "finance-activity"))
        self.assertEqual(data["last_scan"], "2026-10-05T09:30:00")
        self.assertIn("Sent 1 urgent alert(s) to DM", out); self.assertIn("Posted 1 financial event(s)", out)

    def test_empty_mail_posts_nothing(self):
        _seed([])
        data, out = _main("")
        F7.notify.assert_not_called(); F7.nova_config.post_both.assert_not_called()
        self.assertEqual(data["events"], []); self.assertIn("No new financial events", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_never_runs_main(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(MODULE_PATH), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("--weekly", r.stdout)
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_finance_monitor as m; print('IMPORT-OK')"],
                           cwd=str(SCRIPTS_DIR), capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(r.stdout.strip(), "IMPORT-OK")


if __name__ == "__main__":
    unittest.main()
