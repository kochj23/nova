"""
test_nova_incident_lifecycle.py — All 7 test categories for nova_incident_lifecycle.py
Written by Jordan Koch.

SAFETY:
  * The notification bus is ALWAYS mocked. nova_incident_lifecycle emits through an
    internal `_notify` wrapper (which imports `nova_notify.notify`). Every test
    monkeypatches `_mod._notify` to a recorder AND stubs `nova_notify.notify` in
    sys.modules, so a real telemetry.events row can never be enqueued (the live
    nova_notifier daemon would otherwise POST it to Slack).
  * All DB writes happen inside ONE outer transaction per test that is ROLLED BACK
    in teardown. Synthetic incidents/events created with source='pytest-lifecycle'
    never survive the test. The module calls conn.commit()/conn.rollback()
    internally; we wrap the connection so commit() is a no-op and the module's own
    error-path rollback() only unwinds to a per-test SAVEPOINT — the outer
    transaction (and our final cleanup) still rolls back everything.
  * No network / LLM / subprocess is touched by this module or these tests.

Live-DB tests are marked @pytest.mark.integration and skip cleanly when nova_ops
is unreachable, so the suite is deterministic without a database.
"""

import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Load the module under test. It uses `int | None` syntax (3.10+); the repo runs
# 3.10+, so a plain spec load is fine. nova_notify is stubbed before load so the
# internal `from nova_notify import notify` inside _notify() can never reach the
# real bus even if a test forgets to patch _notify.
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_incident_lifecycle.py"

_fake_notify_bus = MagicMock(name="nova_notify_module")
_fake_notify_bus.notify = MagicMock(return_value=True)
sys.modules["nova_notify"] = _fake_notify_bus

_spec = importlib.util.spec_from_file_location("nova_incident_lifecycle", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# Convenience aliases
migrate = _mod.migrate
auto_close_resolved = _mod.auto_close_resolved
acknowledge = _mod.acknowledge
detect_recurrence = _mod.detect_recurrence
stats = _mod.stats
sweep = _mod.sweep

DSN = _mod.DSN


# ---------------------------------------------------------------------------
# DB helpers — one rollback-isolated connection per test.
# ---------------------------------------------------------------------------

def _try_connect():
    try:
        import psycopg2
        return psycopg2.connect(DSN, connect_timeout=5)
    except Exception:
        return None


class _TxConn:
    """Wrap a real psycopg2 connection so the module under test can call
    commit()/rollback() without escaping our test transaction.

    - commit()  -> no-op (data stays visible inside our outer tx, nothing
                   actually lands in the DB permanently)
    - rollback()-> roll back to a SAVEPOINT so the module's error path only
                   unwinds its own partial work, not our synthetic fixtures.
    Everything is wiped by real_rollback() in teardown.
    """

    _SP = "pytest_lifecycle_sp"

    def __init__(self, raw):
        self._raw = raw
        self.commit_calls = 0
        self.rollback_calls = 0
        self._set_savepoint()

    def _set_savepoint(self):
        with self._raw.cursor() as cur:
            cur.execute(f"SAVEPOINT {self._SP}")

    def cursor(self, *a, **kw):
        return self._raw.cursor(*a, **kw)

    def commit(self):
        # Don't really commit. Refresh the savepoint so subsequent rollbacks
        # only unwind work done AFTER this logical commit point.
        self.commit_calls += 1
        with self._raw.cursor() as cur:
            cur.execute(f"RELEASE SAVEPOINT {self._SP}")
            cur.execute(f"SAVEPOINT {self._SP}")

    def rollback(self):
        self.rollback_calls += 1
        with self._raw.cursor() as cur:
            cur.execute(f"ROLLBACK TO SAVEPOINT {self._SP}")
            cur.execute(f"RELEASE SAVEPOINT {self._SP}")
            cur.execute(f"SAVEPOINT {self._SP}")

    def real_rollback(self):
        try:
            self._raw.rollback()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._raw, name)


@pytest.fixture
def db():
    """A rollback-isolated connection, or skip if nova_ops is unreachable."""
    raw = _try_connect()
    if raw is None:
        pytest.skip("nova_ops Postgres not reachable")
    tx = _TxConn(raw)
    try:
        yield tx
    finally:
        tx.real_rollback()
        try:
            raw.close()
        except Exception:
            pass


@pytest.fixture
def notify_spy(monkeypatch):
    """Replace the module's _notify with a recorder. NEVER touches the real bus."""
    calls = []

    def _spy(*a, **kw):
        calls.append((a, kw))
        return True

    monkeypatch.setattr(_mod, "_notify", _spy)
    # belt-and-suspenders: also neuter the underlying bus
    monkeypatch.setattr(_fake_notify_bus, "notify", MagicMock(return_value=True))
    return calls


# --- synthetic-row builders (all tagged pytest-lifecycle, all in the test tx) --

def _mk_event(db, *, ts_sql="now() - interval '90 minutes'", category="gpu",
              incident_id=None, corr_role=None):
    with db.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO telemetry.events
              (ts, source, level, category, title, body, status)
            VALUES ({ts_sql}, 'pytest-lifecycle', 'warning', %s,
                    'pytest synthetic', 'pytest', 'sent')
            RETURNING id
            """,
            (category,),
        )
        ev = cur.fetchone()[0]
        if incident_id is not None:
            cur.execute(
                "UPDATE telemetry.events SET incident_id=%s, corr_role=%s WHERE id=%s",
                (incident_id, corr_role, ev),
            )
    return ev


def _mk_incident(db, *, host="pytest-host", status="open",
                 opened_sql="now() - interval '90 minutes'",
                 updated_sql=None, root_event=None, title="pytest incident",
                 recurrence_key=None):
    updated_sql = updated_sql or opened_sql
    with db.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO telemetry.incidents
              (status, severity, host, title, root_event, member_count,
               opened_at, updated_at, recurrence_key)
            VALUES (%s, 'warning', %s, %s, %s, 1,
                    {opened_sql}, {updated_sql}, %s)
            RETURNING id
            """,
            (status, host, title, root_event, recurrence_key),
        )
        return cur.fetchone()[0]


def _get_incident(db, inc_id):
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, resolved_at, mttr_s, mtta_s, acked_at, acked_by, "
            "recurrence_key FROM telemetry.incidents WHERE id=%s",
            (inc_id,),
        )
        r = cur.fetchone()
    if not r:
        return None
    return dict(zip(
        ["status", "resolved_at", "mttr_s", "mtta_s", "acked_at", "acked_by",
         "recurrence_key"], r))


# ===========================================================================
# 1. SECURITY TESTS  (static source analysis — no DB)
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "xoxb-", "password ="]:
            self.assertNotIn(pat, src, f"Credential pattern leaked: {pat!r}")

    def test_pg_conn_no_password(self):
        src = _SCRIPT.read_text().lower()
        self.assertNotIn("password=", src)

    def test_no_hardcoded_slack(self):
        """Module must emit via nova_notify, never hardcode Slack endpoints/channels."""
        src = _SCRIPT.read_text()
        self.assertNotIn("slack.com", src)
        self.assertNotIn("hooks.slack", src)
        self.assertNotIn("chat.postMessage", src)

    def test_emits_only_through_nova_notify(self):
        src = _SCRIPT.read_text()
        self.assertIn("from nova_notify import notify", src)


# ===========================================================================
# 2. CORE BEHAVIOR — auto_close_resolved  (FOCUS)
# ===========================================================================

@pytest.mark.integration
class TestAutoClose:

    def test_closes_stale_open_incident_and_sets_mttr(self, db, notify_spy):
        """A 90m-old open incident with no fresh events auto-closes; mttr_s stamped."""
        ev = _mk_event(db)
        inc = _mk_incident(db, root_event=ev)
        _mk_event(db, incident_id=inc, corr_role="root")  # only stale members

        closed = auto_close_resolved(db, idle_minutes=30)
        assert closed >= 1

        row = _get_incident(db, inc)
        assert row["status"] == "resolved"
        assert row["resolved_at"] is not None
        assert row["mttr_s"] is not None
        # ~90m old -> well over 80 minutes
        assert row["mttr_s"] >= 80 * 60

        # exactly one notify for our incident, deduped, level=info
        ours = [c for c in notify_spy
                if c[1].get("meta", {}).get("incident_id") == inc]
        assert len(ours) == 1
        assert ours[0][1]["level"] == "info"
        assert ours[0][1]["dedup_key"] == f"incident-resolved-{inc}"
        assert ours[0][1]["category"] == "incident"

    def test_fresh_incident_not_closed(self, db, notify_spy):
        """A recent member event keeps the incident open (idle window not met)."""
        inc = _mk_incident(db, opened_sql="now() - interval '5 minutes'",
                           updated_sql="now()")
        _mk_event(db, ts_sql="now()", incident_id=inc, corr_role="root")

        auto_close_resolved(db, idle_minutes=30)

        row = _get_incident(db, inc)
        assert row["status"] == "open"
        assert row["resolved_at"] is None
        ours = [c for c in notify_spy
                if c[1].get("meta", {}).get("incident_id") == inc]
        assert ours == []

    def test_closes_member_less_incident_via_updated_at(self, db, notify_spy):
        """No member events -> falls back to incident.updated_at for the idle test."""
        inc = _mk_incident(db, root_event=None)  # no member events at all
        closed = auto_close_resolved(db, idle_minutes=30)
        assert closed >= 1
        assert _get_incident(db, inc)["status"] == "resolved"

    def test_already_resolved_incident_untouched(self, db, notify_spy):
        """An incident already resolved is never re-closed and emits nothing."""
        inc = _mk_incident(db, status="resolved")
        auto_close_resolved(db, idle_minutes=30)
        ours = [c for c in notify_spy
                if c[1].get("meta", {}).get("incident_id") == inc]
        assert ours == []

    def test_rerun_does_not_double_close(self, db, notify_spy):
        """Second sweep finds nothing new to close for the same incident."""
        inc = _mk_incident(db)
        first = auto_close_resolved(db, idle_minutes=30)
        assert first >= 1
        before = len(notify_spy)
        # second pass: our incident is already resolved
        auto_close_resolved(db, idle_minutes=30)
        ours_after = [c for c in notify_spy[before:]
                      if c[1].get("meta", {}).get("incident_id") == inc]
        assert ours_after == []


# ===========================================================================
# 3. ACKNOWLEDGE  (FOCUS — sets MTTA)
# ===========================================================================

@pytest.mark.integration
class TestAcknowledge:

    def test_ack_stamps_mtta(self, db, notify_spy):
        inc = _mk_incident(db, opened_sql="now() - interval '12 minutes'")
        ok = acknowledge(db, inc, "oncall-jordan")
        assert ok is True

        row = _get_incident(db, inc)
        assert row["acked_at"] is not None
        assert row["acked_by"] == "oncall-jordan"
        assert row["mtta_s"] is not None
        assert row["mtta_s"] >= 10 * 60  # ~12m

        ours = [c for c in notify_spy
                if c[1].get("meta", {}).get("incident_id") == inc]
        assert len(ours) == 1
        assert ours[0][1]["dedup_key"] == f"incident-acked-{inc}"

    def test_second_ack_is_noop_and_preserves_first(self, db, notify_spy):
        inc = _mk_incident(db, opened_sql="now() - interval '12 minutes'")
        assert acknowledge(db, inc, "first") is True
        first_mtta = _get_incident(db, inc)["mtta_s"]

        # second ack must not overwrite acked_by / mtta
        assert acknowledge(db, inc, "second") is False
        row = _get_incident(db, inc)
        assert row["acked_by"] == "first"
        assert row["mtta_s"] == first_mtta

    def test_ack_missing_incident_returns_false(self, db, notify_spy):
        # huge id that won't exist
        assert acknowledge(db, 2_000_000_111, "nobody") is False
        assert notify_spy == []


# ===========================================================================
# 4. RECURRENCE DETECTION  (FOCUS — flags key seen >=3x/7d)
# ===========================================================================

@pytest.mark.integration
class TestRecurrence:

    def test_builds_key_from_host_and_root_category(self, db, notify_spy):
        ev = _mk_event(db, category="gpu")
        inc = _mk_incident(db, host="wopr", root_event=ev)
        key = detect_recurrence(db, inc)
        assert key == "wopr:gpu"
        assert _get_incident(db, inc)["recurrence_key"] == "wopr:gpu"

    def test_key_defaults_when_host_or_category_missing(self, db, notify_spy):
        inc = _mk_incident(db, host=None, root_event=None)
        key = detect_recurrence(db, inc)
        assert key == "unknown:uncategorized"

    def test_three_occurrences_flag_pattern_and_warn(self, db, notify_spy):
        """>= RECURRENCE_THRESHOLD opens of the same key in 7d -> ONE warning."""
        # Pre-seed two prior incidents already carrying the key, opened in-window.
        for _ in range(2):
            _mk_incident(db, host="gpuhost",
                         opened_sql="now() - interval '2 days'",
                         recurrence_key="gpuhost:gpu")
        ev = _mk_event(db, category="gpu")
        inc = _mk_incident(db, host="gpuhost", root_event=ev)  # the 3rd

        key = detect_recurrence(db, inc)
        assert key == "gpuhost:gpu"

        ours = [c for c in notify_spy
                if c[1].get("meta", {}).get("recurrence_key") == "gpuhost:gpu"]
        assert len(ours) == 1
        warn = ours[0]
        assert warn[1]["level"] == "warning"
        assert warn[1]["category"] == "incident_recurring"
        assert warn[1]["meta"]["count_7d"] >= _mod.RECURRENCE_THRESHOLD
        # dedup is per key per day
        today = date.today().isoformat()
        assert warn[1]["dedup_key"] == f"recurring-gpuhost:gpu-{today}"

    def test_below_threshold_does_not_warn(self, db, notify_spy):
        """Two occurrences (< threshold) stamp the key but raise no pattern warning."""
        _mk_incident(db, host="rarehost", opened_sql="now() - interval '1 day'",
                     recurrence_key="rarehost:net")
        ev = _mk_event(db, category="net")
        inc = _mk_incident(db, host="rarehost", root_event=ev)  # 2nd only
        detect_recurrence(db, inc)
        warns = [c for c in notify_spy
                 if c[1].get("category") == "incident_recurring"
                 and c[1].get("meta", {}).get("recurrence_key") == "rarehost:net"]
        assert warns == []

    def test_old_occurrences_outside_window_dont_count(self, db, notify_spy):
        """Opens older than the 7d lookback don't push a key over threshold."""
        for _ in range(3):
            _mk_incident(db, host="stalehost",
                         opened_sql="now() - interval '30 days'",
                         recurrence_key="stalehost:disk")
        ev = _mk_event(db, category="disk")
        inc = _mk_incident(db, host="stalehost",
                           opened_sql="now() - interval '30 days'",
                           root_event=ev)
        detect_recurrence(db, inc)
        warns = [c for c in notify_spy
                 if c[1].get("meta", {}).get("recurrence_key") == "stalehost:disk"]
        assert warns == []

    def test_missing_incident_returns_none(self, db, notify_spy):
        assert detect_recurrence(db, 2_000_000_222) is None


# ===========================================================================
# 5. MIGRATE IDEMPOTENCY  (FOCUS)
# ===========================================================================

@pytest.mark.integration
class TestMigrate:

    def test_migrate_succeeds(self, db):
        assert migrate(db) is True

    def test_migrate_is_idempotent(self, db):
        """Applying schema twice is a no-op success and leaves columns present."""
        assert migrate(db) is True
        assert migrate(db) is True
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM information_schema.columns
                WHERE table_schema='telemetry' AND table_name='incidents'
                  AND column_name IN ('acked_at','acked_by','resolution',
                                      'recurrence_key','mtta_s','mttr_s')
                """
            )
            assert cur.fetchone()[0] == 6


# ===========================================================================
# 6. SWEEP + STATS  (orchestration)
# ===========================================================================

@pytest.mark.integration
class TestSweepAndStats:

    def test_sweep_scans_recurrence_then_closes(self, db, notify_spy):
        ev = _mk_event(db, category="gpu")
        inc = _mk_incident(db, host="sweephost", root_event=ev)

        summary = sweep(db, idle_minutes=30)
        assert summary["recurrence_scanned"] >= 1
        assert summary["closed"] >= 1

        row = _get_incident(db, inc)
        # recurrence ran first, so the key is stamped even though it then closed
        assert row["recurrence_key"] == "sweephost:gpu"
        assert row["status"] == "resolved"

    def test_stats_returns_rollup_dict(self, db, notify_spy):
        _mk_incident(db, status="open", opened_sql="now() - interval '1 hour'")
        out = stats(db)
        assert isinstance(out, dict)
        for k in ("open_now", "resolved_24h", "opened_24h",
                  "distinct_patterns_7d", "top_recurring"):
            assert k in out
        assert isinstance(out["top_recurring"], list)


# ===========================================================================
# 7. RESILIENCE / SAFETY  (never raises; never spams)
# ===========================================================================

class TestResilience(unittest.TestCase):

    def test_notify_swallows_bus_errors(self):
        """_notify must degrade to False, never raise, if the bus blows up."""
        bad = MagicMock()
        bad.notify = MagicMock(side_effect=RuntimeError("bus down"))
        old = sys.modules.get("nova_notify")
        sys.modules["nova_notify"] = bad
        try:
            self.assertFalse(_mod._notify("x", level="info"))
        finally:
            sys.modules["nova_notify"] = old

    def test_connect_failure_returns_none(self):
        """_connect never raises; returns None on bad DSN."""
        import unittest.mock as m
        with m.patch.object(_mod, "DSN", "host=127.0.0.1 dbname=does_not_exist_xyz "
                                          "user=nobody connect_timeout=1"):
            self.assertIsNone(_mod._connect())

    def test_auto_close_handles_broken_conn(self):
        """A connection whose cursor() raises -> 0 closed, no exception."""
        broken = MagicMock()
        broken.cursor.side_effect = RuntimeError("db gone")
        self.assertEqual(_mod.auto_close_resolved(broken), 0)
        broken.rollback.assert_called()

    def test_acknowledge_handles_broken_conn(self):
        broken = MagicMock()
        broken.cursor.side_effect = RuntimeError("db gone")
        self.assertFalse(_mod.acknowledge(broken, 1, "x"))

    def test_detect_recurrence_handles_broken_conn(self):
        broken = MagicMock()
        broken.cursor.side_effect = RuntimeError("db gone")
        self.assertIsNone(_mod.detect_recurrence(broken, 1))

    def test_stats_handles_broken_conn(self):
        broken = MagicMock()
        broken.cursor.side_effect = RuntimeError("db gone")
        self.assertEqual(_mod.stats(broken), {})


@pytest.mark.integration
class TestNoNotifyLeak:
    """Hard safety contract: tests never enqueue a real telemetry.events row."""

    def test_module_level_bus_is_a_mock(self):
        # nova_notify in sys.modules is our MagicMock, not the real package.
        assert sys.modules["nova_notify"] is _fake_notify_bus

    def test_auto_close_uses_spy_not_real_bus(self, db, notify_spy):
        inc = _mk_incident(db)
        auto_close_resolved(db, idle_minutes=30)
        # the real-ish bus.notify must not have been called; only the spy
        assert _fake_notify_bus.notify.call_count == 0
        assert any(c[1].get("meta", {}).get("incident_id") == inc
                   for c in notify_spy)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
