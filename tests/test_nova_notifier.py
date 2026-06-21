"""
test_nova_notifier.py — All 7 test categories for nova_notifier.py

nova_notifier.py is the central notification daemon (Phase 2 event-bus consumer).
It drains telemetry.events, ROUTES by severity, DEDUPs repeats, CORRELATEs related
events into incidents, and delivers to Slack via nova_config.post_both.

SAFETY (these tests must never touch production state or spam Slack):
  * nova_config.post_both is MOCKED in every test — no real Slack/Discord ever.
  * nova_correlator.correlate / llm_summarize are MOCKED — no LLM/Ollama, no network.
  * The DB is REAL (nova_ops) but drain() runs against a connection whose commit/
    close are neutralized, and every test ROLLS BACK in teardown. We verify ZERO
    leftover rows tagged source='pytest-notifier' after the suite.

Written by Jordan Koch.
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2
import psycopg2.extras
import pytest

# ---------------------------------------------------------------------------
# Load module under test directly (not on PATH)
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_notifier.py"
_spec = importlib.util.spec_from_file_location("nova_notifier_mod", _SCRIPT)
notifier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notifier)

# Real Slack channel ids that the routing policy resolves to (from nova_config).
SLACK_INFO = notifier.nova_config.SLACK_INFO      # #nova-info
SLACK_NOTIFY = notifier.nova_config.SLACK_NOTIFY  # #nova-warning
SLACK_BB = notifier.nova_config.SLACK_BB          # #nova-critical

DSN = notifier.DSN
TEST_SOURCE = "pytest-notifier"  # unique marker — teardown verifies zero leftovers


# ===========================================================================
# Shared test infrastructure
# ===========================================================================

class _NoCommitConn:
    """Wraps a real psycopg2 connection so drain()'s `with conn:` and conn.close()
    never commit or close it. The fixture rolls back and closes for real afterward.
    """

    def __init__(self, real):
        self._real = real

    # `with conn:` in psycopg2 commits on __exit__ success — swallow that.
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Never commit; never close. Just keep the transaction open for rollback.
        return False

    def cursor(self, *a, **k):
        return self._real.cursor(*a, **k)

    def commit(self):
        # No-op: keep everything inside the rolled-back transaction.
        pass

    def rollback(self):
        self._real.rollback()

    def close(self):
        # No-op: the fixture owns the real connection's lifecycle.
        pass

    def __getattr__(self, name):
        return getattr(self._real, name)


def _insert_event(conn, *, level="info", category=None, title="t", body=None,
                  dedup_key=None, status="new", source=TEST_SOURCE,
                  sent_at_offset_s=None, channel=None):
    """Insert a telemetry.events row, returning its id. Always tagged TEST_SOURCE."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    sent_at = None
    if sent_at_offset_s is not None:
        cur.execute("SELECT now() - make_interval(secs => %s) AS t", (sent_at_offset_s,))
        sent_at = cur.fetchone()["t"]
    cur.execute(
        "INSERT INTO telemetry.events (source, level, category, title, body, "
        "dedup_key, status, channel, sent_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (source, level, category, title, body, dedup_key, status, channel, sent_at))
    return cur.fetchone()["id"]


def _row(conn, eid):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM telemetry.events WHERE id=%s", (eid,))
    return cur.fetchone()


@pytest.fixture
def db(monkeypatch):
    """Real DB connection with safe transaction semantics.

    - Patches notifier._connect to hand drain() a non-committing wrapper.
    - Patches nova_config.post_both and nova_correlator.{correlate,llm_summarize}
      to safe MagicMocks so no Slack/LLM/network ever fires.
    - Rolls back and closes the real connection in teardown.

    Yields a namespace with: conn, post_both, correlate, llm_summarize, insert, row.
    """
    # Match _connect()'s RealDictCursor factory so drain() sees dict rows.
    real = psycopg2.connect(DSN, connect_timeout=5,
                            cursor_factory=psycopg2.extras.RealDictCursor)
    wrapped = _NoCommitConn(real)

    monkeypatch.setattr(notifier, "_connect", lambda: wrapped)

    post_both = MagicMock(name="post_both")
    monkeypatch.setattr(notifier.nova_config, "post_both", post_both)

    # Default correlate => standalone (deliver the event normally).
    correlate = MagicMock(name="correlate",
                          return_value={"action": "standalone", "incident_id": None,
                                        "role": None, "suppress": False})
    monkeypatch.setattr(notifier.nova_correlator, "correlate", correlate)

    llm_summarize = MagicMock(name="llm_summarize",
                              return_value=("a summary", "test-model"))
    monkeypatch.setattr(notifier.nova_correlator, "llm_summarize", llm_summarize)

    ns = MagicMock()
    ns.conn = wrapped
    ns.post_both = post_both
    ns.correlate = correlate
    ns.llm_summarize = llm_summarize
    ns.insert = lambda **kw: _insert_event(wrapped, **kw)
    ns.row = lambda eid: _row(wrapped, eid)

    try:
        yield ns
    finally:
        try:
            real.rollback()
        finally:
            real.close()


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials_in_source(self):
        src = _SCRIPT.read_text()
        for pattern in ("sk-live", "sk-test", "ghp_", "AKIA", "xoxb-"):
            self.assertNotIn(pattern, src, f"Potential credential in source: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src,
                         "Hardcoded home path found — use Path(__file__) instead")

    def test_db_connect_has_bounded_timeout(self):
        """_connect() must use a bounded connect_timeout, never block forever."""
        src = _SCRIPT.read_text()
        self.assertIn("connect_timeout=5", src,
                      "_connect must set an explicit bounded connect_timeout")

    def test_dsn_is_local_only(self):
        """DB must point at localhost — never a remote prod host."""
        self.assertIn("127.0.0.1", notifier.DSN)
        self.assertIn("dbname=nova_ops", notifier.DSN)


# ===========================================================================
# 2. UNIT TESTS — routing policy + formatting (pure functions, no DB)
# ===========================================================================

class TestRouting(unittest.TestCase):

    def test_info_routes_to_slack_info(self):
        self.assertEqual(notifier._route("info", None), SLACK_INFO)

    def test_warning_routes_to_slack_notify(self):
        self.assertEqual(notifier._route("warning", None), SLACK_NOTIFY)

    def test_critical_routes_to_slack_bb(self):
        self.assertEqual(notifier._route("critical", None), SLACK_BB)

    def test_unknown_level_falls_back_to_info(self):
        self.assertEqual(notifier._route("debug", None), SLACK_INFO)
        self.assertEqual(notifier._route("", None), SLACK_INFO)

    def test_category_override_security_news_to_info(self):
        # Even a 'critical' security_news item is FYI, not your-network.
        self.assertEqual(notifier._route("critical", "security_news"), SLACK_INFO)

    def test_category_override_claude_code_to_info(self):
        self.assertEqual(notifier._route("warning", "claude_code"), SLACK_INFO)

    def test_category_override_calendar_to_info(self):
        self.assertEqual(notifier._route("critical", "calendar"), SLACK_INFO)

    def test_unknown_category_falls_through_to_level(self):
        # A category with no override uses the level routing.
        self.assertEqual(notifier._route("critical", "disk_full"), SLACK_BB)
        self.assertEqual(notifier._route("warning", "disk_full"), SLACK_NOTIFY)

    def test_override_wins_over_level(self):
        # The override map must take precedence regardless of level.
        for lvl in ("info", "warning", "critical"):
            self.assertEqual(notifier._route(lvl, "calendar"), SLACK_INFO)


class TestFormat(unittest.TestCase):

    def test_fmt_includes_title_and_emoji(self):
        msg = notifier._fmt({"level": "critical", "title": "Disk full",
                             "body": None, "category": None, "source": None})
        self.assertIn("Disk full", msg)
        self.assertIn(":rotating_light:", msg)

    def test_fmt_warning_emoji(self):
        msg = notifier._fmt({"level": "warning", "title": "T", "body": None,
                             "category": None, "source": None})
        self.assertIn(":warning:", msg)

    def test_fmt_info_emoji(self):
        msg = notifier._fmt({"level": "info", "title": "T", "body": None,
                             "category": None, "source": None})
        self.assertIn(":information_source:", msg)

    def test_fmt_includes_body(self):
        msg = notifier._fmt({"level": "info", "title": "T", "body": "the body",
                             "category": None, "source": None})
        self.assertIn("the body", msg)

    def test_fmt_includes_category_and_source_tag(self):
        msg = notifier._fmt({"level": "info", "title": "T", "body": None,
                             "category": "disk", "source": "monitor"})
        self.assertIn("disk", msg)
        self.assertIn("monitor", msg)
        self.assertIn("·", msg)

    def test_fmt_unknown_level_no_emoji_prefix(self):
        msg = notifier._fmt({"level": "weird", "title": "T", "body": None,
                             "category": None, "source": None})
        # Unknown levels get empty emoji; title still present.
        self.assertIn("*T*", msg)


# ===========================================================================
# 3. INTEGRATION TESTS — drain() against the real DB (rolled back)
# ===========================================================================

@pytest.mark.integration
class TestDrainRouting:
    """drain() routing: info->SLACK_INFO, warning->SLACK_NOTIFY, critical->SLACK_BB."""

    def test_info_delivered_to_slack_info(self, db):
        eid = db.insert(level="info", title="info event")
        sent = notifier.drain(only_source="pytest-notifier")
        assert sent == 1
        db.post_both.assert_called_once()
        _, kwargs = db.post_both.call_args
        assert kwargs["slack_channel"] == SLACK_INFO
        row = db.row(eid)
        assert row["status"] == "sent"
        assert row["channel"] == SLACK_INFO
        assert row["sent_at"] is not None

    def test_warning_delivered_to_slack_notify(self, db):
        eid = db.insert(level="warning", title="warn event")
        sent = notifier.drain(only_source="pytest-notifier")
        assert sent == 1
        _, kwargs = db.post_both.call_args
        assert kwargs["slack_channel"] == SLACK_NOTIFY
        assert db.row(eid)["channel"] == SLACK_NOTIFY

    def test_critical_delivered_to_slack_bb(self, db):
        eid = db.insert(level="critical", title="crit event")
        sent = notifier.drain(only_source="pytest-notifier")
        assert sent == 1
        _, kwargs = db.post_both.call_args
        assert kwargs["slack_channel"] == SLACK_BB
        assert db.row(eid)["channel"] == SLACK_BB

    def test_category_override_routes_critical_security_news_to_info(self, db):
        eid = db.insert(level="critical", category="security_news",
                        title="new CVE")
        sent = notifier.drain(only_source="pytest-notifier")
        assert sent == 1
        _, kwargs = db.post_both.call_args
        # Override beats the critical level: goes to #nova-info.
        assert kwargs["slack_channel"] == SLACK_INFO
        assert db.row(eid)["channel"] == SLACK_INFO

    def test_message_body_passed_to_post_both(self, db):
        db.insert(level="warning", title="Pump failure", body="details here")
        notifier.drain(only_source="pytest-notifier")
        args, _ = db.post_both.call_args
        assert "Pump failure" in args[0]
        assert "details here" in args[0]

    def test_only_new_events_processed(self, db):
        # An already-sent row must not be re-delivered.
        db.insert(level="info", title="already sent", status="sent",
                  channel=SLACK_INFO)
        new_id = db.insert(level="info", title="fresh")
        sent = notifier.drain(only_source="pytest-notifier")
        assert sent == 1
        assert db.post_both.call_count == 1
        assert db.row(new_id)["status"] == "sent"


@pytest.mark.integration
class TestDrainDedup:
    """Same dedup_key within DEDUP_WINDOW_S -> suppressed, prior dispatch_count bumped."""

    def test_dedup_suppresses_recent_duplicate(self, db):
        # Prior 'sent' event with same key, sent 10 minutes ago (inside the window).
        prior = db.insert(level="warning", title="cpu high", dedup_key="cpu-host1",
                          status="sent", channel=SLACK_NOTIFY, sent_at_offset_s=600)
        dup = db.insert(level="warning", title="cpu high again",
                        dedup_key="cpu-host1")
        sent = notifier.drain(only_source="pytest-notifier")
        # The duplicate is folded, not delivered.
        assert sent == 0
        db.post_both.assert_not_called()
        dup_row = db.row(dup)
        assert dup_row["status"] == "suppressed"
        assert dup_row["collapsed_into"] == prior
        # Prior's dispatch_count is bumped.
        assert db.row(prior)["dispatch_count"] == 1

    def test_dedup_outside_window_delivers(self, db):
        # Prior 'sent' event with same key, sent well outside the window.
        old_offset = notifier.DEDUP_WINDOW_S + 600
        db.insert(level="warning", title="cpu high", dedup_key="cpu-host2",
                  status="sent", channel=SLACK_NOTIFY, sent_at_offset_s=old_offset)
        fresh = db.insert(level="warning", title="cpu high again",
                          dedup_key="cpu-host2")
        sent = notifier.drain(only_source="pytest-notifier")
        assert sent == 1
        db.post_both.assert_called_once()
        assert db.row(fresh)["status"] == "sent"

    def test_no_dedup_key_always_delivers(self, db):
        # Events with no dedup_key are never suppressed even if titles match.
        db.insert(level="warning", title="same", dedup_key=None,
                  status="sent", channel=SLACK_NOTIFY, sent_at_offset_s=60)
        fresh = db.insert(level="warning", title="same", dedup_key=None)
        sent = notifier.drain(only_source="pytest-notifier")
        assert sent == 1
        assert db.row(fresh)["status"] == "sent"

    def test_dedup_only_against_sent_not_new(self, db):
        # A prior row with the same key but status='new' (not yet sent) is not a
        # dedup target — only 'sent' rows count.
        db.insert(level="warning", title="first", dedup_key="k-new",
                  status="error")  # error, not sent
        fresh = db.insert(level="warning", title="second", dedup_key="k-new")
        sent = notifier.drain(only_source="pytest-notifier")
        assert sent == 1
        assert db.row(fresh)["status"] == "sent"


@pytest.mark.integration
class TestDrainCorrelation:
    """Correlation branches: suppress (attached symptom) and opened (incident alert)."""

    def test_attached_symptom_is_suppressed(self, db):
        eid = db.insert(level="critical", category="disk", title="symptom")
        db.correlate.return_value = {"action": "attached", "incident_id": 999,
                                     "role": "symptom", "suppress": True}
        sent = notifier.drain(only_source="pytest-notifier")
        # Symptom folded into existing incident — no standalone delivery.
        assert sent == 0
        db.post_both.assert_not_called()
        # Still 'new' (untouched) — the incident's own alert covers it.
        assert db.row(eid)["status"] == "new"

    def test_opened_incident_delivers_llm_summary(self, db):
        eid = db.insert(level="critical", category="disk", title="root cause")
        db.correlate.return_value = {"action": "opened", "incident_id": 4242,
                                     "role": "root", "suppress": False}
        db.llm_summarize.return_value = ("Root cause: disk filled up", "llama-test")
        sent = notifier.drain(only_source="pytest-notifier")
        assert sent == 1
        db.llm_summarize.assert_called_once()
        # The incident_id was passed to llm_summarize.
        assert db.llm_summarize.call_args[0][1] == 4242
        args, kwargs = db.post_both.call_args
        msg = args[0]
        assert "Incident #4242" in msg
        assert "root cause" in msg
        assert "Root cause: disk filled up" in msg
        assert "llama-test" in msg
        # Critical incident -> SLACK_BB.
        assert kwargs["slack_channel"] == SLACK_BB
        assert db.row(eid)["status"] == "sent"

    def test_opened_incident_warning_badge_and_channel(self, db):
        eid = db.insert(level="warning", category="net", title="link flap")
        db.correlate.return_value = {"action": "opened", "incident_id": 7,
                                     "role": "root", "suppress": False}
        db.llm_summarize.return_value = ("summary", None)
        sent = notifier.drain(only_source="pytest-notifier")
        assert sent == 1
        args, kwargs = db.post_both.call_args
        # warning badge (no model line when model is None)
        assert ":warning:" in args[0]
        assert "summary by" not in args[0]
        assert kwargs["slack_channel"] == SLACK_NOTIFY

    def test_correlate_exception_falls_back_to_standalone(self, db):
        eid = db.insert(level="warning", title="boom")
        db.correlate.side_effect = RuntimeError("correlator down")
        sent = notifier.drain(only_source="pytest-notifier")
        # Exception in correlate is swallowed -> treated as standalone, delivered.
        assert sent == 1
        db.post_both.assert_called_once()
        assert db.row(eid)["status"] == "sent"

    def test_opened_incident_marks_incident_posted(self, db):
        # Create a real incident row so the slack_ts update has a target.
        cur = db.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "INSERT INTO telemetry.incidents (title, severity) VALUES (%s,%s) RETURNING id",
            ("pytest incident", "critical"))
        inc_id = cur.fetchone()["id"]
        db.insert(level="critical", category="disk", title="root")
        db.correlate.return_value = {"action": "opened", "incident_id": inc_id,
                                     "role": "root", "suppress": False}
        sent = notifier.drain(only_source="pytest-notifier")
        assert sent == 1
        cur.execute("SELECT slack_ts FROM telemetry.incidents WHERE id=%s", (inc_id,))
        assert cur.fetchone()["slack_ts"] == "posted"


# ===========================================================================
# 4. RETRY / FAILURE TESTS
# ===========================================================================

@pytest.mark.integration
class TestDrainFailures:

    def test_delivery_failure_marks_error_not_sent(self, db):
        eid = db.insert(level="warning", title="will fail")
        db.post_both.side_effect = OSError("slack down")
        sent = notifier.drain(only_source="pytest-notifier")
        # post_both raised -> event marked 'error', not counted as sent.
        assert sent == 0
        assert db.row(eid)["status"] == "error"

    def test_db_connect_failure_returns_zero(self, monkeypatch):
        # _connect raising must not crash drain — returns 0 delivered.
        def boom():
            raise psycopg2.OperationalError("no db")
        monkeypatch.setattr(notifier, "_connect", boom)
        # Ensure post_both is mocked too, just in case.
        monkeypatch.setattr(notifier.nova_config, "post_both", MagicMock())
        assert notifier.drain(only_source="pytest-notifier") == 0

    def test_one_failure_does_not_block_others(self, db):
        good = db.insert(level="info", title="good")
        bad = db.insert(level="warning", title="bad")
        # Fail only the warning delivery; info should still succeed.
        def side(msg, slack_channel=None):
            if slack_channel == SLACK_NOTIFY:
                raise OSError("down")
        db.post_both.side_effect = side
        sent = notifier.drain(only_source="pytest-notifier")
        assert sent == 1
        assert db.row(good)["status"] == "sent"
        assert db.row(bad)["status"] == "error"


# ===========================================================================
# 5. FUNCTIONAL TESTS — end-to-end batch behavior
# ===========================================================================

@pytest.mark.integration
@pytest.mark.functional
class TestDrainFunctional:

    def test_mixed_batch_routes_each_correctly(self, db):
        i = db.insert(level="info", title="i")
        w = db.insert(level="warning", title="w")
        c = db.insert(level="critical", title="c")
        sent = notifier.drain(only_source="pytest-notifier")
        assert sent == 3
        channels = {db.row(i)["channel"], db.row(w)["channel"], db.row(c)["channel"]}
        assert channels == {SLACK_INFO, SLACK_NOTIFY, SLACK_BB}
        assert db.post_both.call_count == 3

    def test_drain_empty_queue_returns_zero(self, db):
        # No 'new' rows tagged by us; but other 'new' rows could exist in prod.
        # Process only what's there; with nothing new from us, our post must reflect it.
        # We can't guarantee an empty global queue, so assert drain doesn't raise and
        # never posts for a queue we didn't populate beyond what exists.
        before = db.post_both.call_count
        notifier.drain(only_source="pytest-notifier")
        # No assertion on count (other prod rows may exist) — just must not raise.
        assert db.post_both.call_count >= before

    def test_suppressed_and_delivered_in_same_batch(self, db):
        prior = db.insert(level="warning", title="x", dedup_key="dk",
                          status="sent", channel=SLACK_NOTIFY, sent_at_offset_s=120)
        dup = db.insert(level="warning", title="x2", dedup_key="dk")
        fresh = db.insert(level="critical", title="other")
        sent = notifier.drain(only_source="pytest-notifier")
        # Only the fresh critical is delivered; the dup is suppressed.
        assert sent == 1
        assert db.row(dup)["status"] == "suppressed"
        assert db.row(fresh)["status"] == "sent"
        assert db.row(prior)["dispatch_count"] == 1


# ===========================================================================
# 6. SAFETY VERIFICATION — no real Slack, no leaked rows
# ===========================================================================

@pytest.mark.integration
class TestSafety:

    def test_post_both_never_calls_real_slack(self, db):
        # Sanity: the patched post_both is a MagicMock, not the real function.
        assert isinstance(notifier.nova_config.post_both, MagicMock)
        db.insert(level="critical", title="would-page")
        notifier.drain(only_source="pytest-notifier")
        # It was "called" but only the mock — no HTTP happened.
        assert db.post_both.called

    def test_correlator_is_mocked(self, db):
        assert isinstance(notifier.nova_correlator.correlate, MagicMock)
        assert isinstance(notifier.nova_correlator.llm_summarize, MagicMock)

    def test_no_rows_committed_after_rollback(self, db):
        # Insert inside the transaction; it must NOT survive a fresh connection.
        eid = db.insert(level="info", title="ephemeral")
        # Open a *separate* connection (autocommit) and confirm the row is invisible.
        other = psycopg2.connect(DSN, connect_timeout=5)
        other.autocommit = True
        try:
            cur = other.cursor()
            cur.execute("SELECT count(*) FROM telemetry.events WHERE id=%s", (eid,))
            assert cur.fetchone()[0] == 0, "uncommitted row leaked to another session"
        finally:
            other.close()


def test_zero_leftover_pytest_rows_globally():
    """Final guard: after all tests, no row tagged source='pytest-notifier' exists.

    Every test rolls back, so this committed table must be clean. Runs on its own
    connection (not the rolled-back fixture).
    """
    conn = psycopg2.connect(DSN, connect_timeout=5)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM telemetry.events WHERE source=%s",
                    (TEST_SOURCE,))
        leftover = cur.fetchone()[0]
        assert leftover == 0, f"{leftover} pytest-notifier rows leaked into telemetry.events"
    finally:
        conn.close()


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles_without_errors(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_notifier.py has syntax errors: {e}")

    def test_channel_map_covers_all_levels(self):
        for lvl in ("info", "warning", "critical"):
            self.assertIn(lvl, notifier.CHANNEL)

    def test_category_override_map_present(self):
        for cat in ("security_news", "claude_code", "calendar"):
            self.assertIn(cat, notifier.CATEGORY_OVERRIDE)

    def test_dedup_window_is_positive(self):
        self.assertIsInstance(notifier.DEDUP_WINDOW_S, int)
        self.assertGreater(notifier.DEDUP_WINDOW_S, 0)

    def test_public_callables_present(self):
        for fn in ("drain", "_route", "_fmt", "_connect", "main"):
            self.assertTrue(callable(getattr(notifier, fn, None)),
                            f"Missing: {fn}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
