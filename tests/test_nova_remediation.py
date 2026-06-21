"""
test_nova_remediation.py — All 7 test categories for nova_remediation.py

The runbook / auto-remediation engine. These tests are SAFETY-CRITICAL:
they prove the engine PROPOSES but executes NOTHING while the master switch
is off, that impactful actions (reboot) are approval-gated, that only
allowlisted argv can ever run, and that cooldown suppresses duplicate
proposals.

HARD SAFETY RULES enforced here:
  * nova_notify.notify is monkeypatched to a recorder in EVERY test (autouse
    fixture). A real notify() writes to telemetry.events, which the live
    nova-notifier daemon would then POST TO SLACK. We never enqueue a real event.
  * subprocess.run is monkeypatched in EVERY test (autouse). It must NEVER run
    launchctl / reboot / rm. A bare recorder is installed by default; the
    safety-critical tests additionally assert it was never called.
  * DB writes go into a single uncommitted transaction. The module calls
    conn.commit() internally (in ensure_schema/_record/_update); we neutralize
    that by wrapping the live connection so commit() is a no-op, then ROLL BACK
    everything in teardown. Zero rows survive the suite.

Written by Jordan Koch.
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# nova_remediation imports nova_notify at module scope (real psycopg2 DSN inside).
# Load the module by path so we get the genuine source under test.
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_remediation.py"
_spec = importlib.util.spec_from_file_location("nova_remediation", _SCRIPT)
rem = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rem)

import nova_notify  # noqa: E402  (real module; we patch its notify per-test)

try:
    import psycopg2  # noqa: E402
    import psycopg2.extensions  # noqa: E402
    _HAVE_PG = True
except Exception:
    _HAVE_PG = False


# ── DB helpers ────────────────────────────────────────────────────────────────

def _live_conn():
    """Open a real nova_ops connection, or skip if unreachable."""
    if not _HAVE_PG:
        pytest.skip("psycopg2 not installed")
    try:
        return psycopg2.connect(rem.DSN, connect_timeout=5)
    except Exception as e:
        pytest.skip(f"nova_ops not reachable: {e}")


class _NoCommitConn:
    """Proxy a psycopg2 connection whose commit() is a no-op.

    The module commits internally; we want everything to live inside ONE
    transaction we can roll back. rollback()/cursor()/close() pass through.
    """

    def __init__(self, real):
        self._real = real

    def commit(self):  # swallow — keep it all in one rollback-able txn
        pass

    def __getattr__(self, name):
        return getattr(self._real, name)


# ── pytest fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _hard_safety(monkeypatch):
    """SAFETY: stub notify() and subprocess.run for EVERY test.

    notify is replaced with a recorder list so no event ever hits telemetry
    (and thus never reaches Slack). subprocess.run is replaced with a recorder
    that, if called, returns a benign result — tests assert against it.
    """
    notify_calls = []

    def fake_notify(title, **kwargs):
        notify_calls.append({"title": title, **kwargs})
        return True

    # Patch on BOTH the nova_notify module and the binding the module holds.
    monkeypatch.setattr(nova_notify, "notify", fake_notify)
    monkeypatch.setattr(rem.nova_notify, "notify", fake_notify)

    sub_calls = []

    def fake_run(argv, *a, **kw):
        sub_calls.append(argv)
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(rem.subprocess, "run", fake_run)

    rem._test_notify_calls = notify_calls
    rem._test_sub_calls = sub_calls
    yield {"notify": notify_calls, "sub": sub_calls}


@pytest.fixture
def db():
    """A rollback-only connection wrapper + seeded synthetic GPU incident.

    Yields (conn, incident_id, event_id). Everything is rolled back after the
    test, so zero rows leak into telemetry.{events,incidents,remediations}.
    """
    real = _live_conn()
    rem.ensure_schema(real)  # commits the (idempotent) DDL — fine, it's CREATE IF NOT EXISTS
    conn = _NoCommitConn(real)
    cur = real.cursor(cursor_factory=psycopg2.extensions.cursor)

    # Synthetic root event (gpu) + OPEN incident on Office-M4-2 — matches the
    # only registered runbook ("Office-M4-2","gpu").
    cur.execute(
        "INSERT INTO telemetry.events (source, level, category, title, body, meta) "
        "VALUES ('pytest-nova_remediation','critical','gpu',"
        "'[pytest] GPU wedged','synthetic',"
        "'{\"host\":\"Office-M4-2\",\"pytest\":true}'::jsonb) RETURNING id")
    ev_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO telemetry.incidents (status, severity, host, title, root_event, member_count) "
        "VALUES ('open','critical','Office-M4-2','[pytest] GPU wedge',%s,1) RETURNING id",
        (ev_id,))
    inc_id = cur.fetchone()[0]
    # NOTE: do not commit — the whole thing rolls back below.

    try:
        yield conn, inc_id, ev_id
    finally:
        try:
            real.rollback()
        except Exception:
            pass
        try:
            real.close()
        except Exception:
            pass


def _rows_for(conn, incident_id):
    cur = conn.cursor(cursor_factory=psycopg2.extensions.cursor)
    cur.execute(
        "SELECT action, tier, status, dry_run FROM telemetry.remediations "
        "WHERE incident_id=%s ORDER BY id", (incident_id,))
    return cur.fetchall()


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pat, src, f"Credential found: {pat!r}")

    def test_no_shell_true_anywhere(self):
        """The engine must never use a shell. shell=False is non-negotiable."""
        src = _SCRIPT.read_text()
        self.assertNotIn("shell=True", src)
        self.assertIn("shell=False", src)

    def test_allowlist_argv_are_lists_not_strings(self):
        """Every allowlisted action's argv is a list (no shell string interpolation)."""
        for name, spec in rem.ACTIONS.items():
            self.assertIsInstance(spec["argv"], list, f"{name} argv must be a list")
            self.assertTrue(all(isinstance(a, str) for a in spec["argv"]),
                            f"{name} argv elements must be strings")

    def test_reboot_argv_is_harmless_noop(self):
        """The impactful reboot must invoke a benign echo, never a real
        shutdown/reboot binary. We check the EXECUTABLE (argv[0]) — the echoed
        message text is allowed to mention 'reboot'."""
        argv = rem.ACTIONS["reboot_host"]["argv"]
        self.assertEqual(argv[0], "/bin/echo")
        for danger in ("/sbin/shutdown", "/sbin/reboot", "shutdown", "reboot", "halt"):
            self.assertNotEqual(argv[0], danger,
                                f"reboot_host executable must not be {danger!r}")

    def test_no_incident_data_interpolated_into_argv(self):
        """argv values are static constants — no %s / f-string fields."""
        for name, spec in rem.ACTIONS.items():
            for tok in spec["argv"]:
                self.assertNotIn("%s", tok)
                self.assertNotIn("{", tok)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_cooldown_is_30_minutes(self):
        self.assertEqual(rem.COOLDOWN_S, 1800)

    def test_execute_action_uses_timeout(self):
        """subprocess.run must be bounded by a timeout to avoid hangs."""
        with patch.object(rem.subprocess, "run") as mrun:
            mrun.return_value = MagicMock(returncode=0, stdout="", stderr="")
            rem.execute_action("restart_ollama", dry_run=False)
        _, kwargs = mrun.call_args
        self.assertIn("timeout", kwargs)
        self.assertLessEqual(kwargs["timeout"], 120)

    def test_lookup_runbook_is_constant_time(self):
        """Runbook lookup is a dict get — present and missing keys both resolve."""
        self.assertEqual(rem._lookup_runbook("Office-M4-2", "gpu"),
                         ["restart_ollama", "reboot_host"])
        self.assertEqual(rem._lookup_runbook("nope", "nope"), [])


# ===========================================================================
# 3. RETRY / RESILIENCE TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_execute_action_captures_exception_never_raises(self):
        """A subprocess failure is captured into the result dict, not raised."""
        with patch.object(rem.subprocess, "run", side_effect=OSError("boom")):
            res = rem.execute_action("restart_ollama", dry_run=False)
        self.assertFalse(res["ok"])
        self.assertIn("boom", res["error"])

    def test_execute_action_timeout_is_caught(self):
        with patch.object(rem.subprocess, "run",
                          side_effect=rem.subprocess.TimeoutExpired("x", 60)):
            res = rem.execute_action("restart_ollama", dry_run=False)
        self.assertFalse(res["ok"])
        self.assertIsNotNone(res["error"])

    def test_propose_swallows_db_errors(self):
        """propose_for_incident never raises — bad conn -> error in dict."""
        bad = MagicMock()
        bad.cursor.side_effect = Exception("db gone")
        out = rem.propose_for_incident(bad, 123)
        self.assertIn("error", out)
        self.assertEqual(out["incident_id"], 123)

    def test_approve_swallows_db_errors(self):
        bad = MagicMock()
        bad.cursor.side_effect = Exception("db gone")
        out = rem.approve(bad, 999)
        self.assertIn("error", out)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_master_switch_default_off(self):
        """SAFETY: REMEDIATION_ENABLED ships False."""
        self.assertIs(rem.REMEDIATION_ENABLED, False)

    def test_execute_action_rejects_non_allowlisted(self):
        """Allowlist: an unknown action cannot run."""
        res = rem.execute_action("rm_rf_root", dry_run=False)
        self.assertFalse(res["ok"])
        self.assertIn("allowlist", res["error"])
        self.assertIsNone(res["argv"])

    def test_execute_action_rejects_non_allowlisted_even_dry_run(self):
        res = rem.execute_action("definitely_not_real", dry_run=True)
        self.assertFalse(res["ok"])
        self.assertIn("allowlist", res["error"])

    def test_execute_action_dry_run_does_not_call_subprocess(self):
        """dry_run must never touch subprocess.run."""
        with patch.object(rem.subprocess, "run") as mrun:
            res = rem.execute_action("restart_ollama", dry_run=True)
        mrun.assert_not_called()
        self.assertTrue(res["ok"])
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["note"], "dry_run: not executed")

    def test_execute_action_real_invokes_exact_argv(self):
        """A real run passes the EXACT allowlisted argv, shell=False."""
        with patch.object(rem.subprocess, "run") as mrun:
            mrun.return_value = MagicMock(returncode=0, stdout="done", stderr="")
            res = rem.execute_action("restart_ollama", dry_run=False)
        called_argv = mrun.call_args[0][0]
        self.assertEqual(called_argv, rem.ACTIONS["restart_ollama"]["argv"])
        self.assertEqual(mrun.call_args[1].get("shell"), False)
        self.assertTrue(res["ok"])
        self.assertEqual(res["returncode"], 0)

    def test_execute_action_nonzero_rc_is_not_ok(self):
        with patch.object(rem.subprocess, "run") as mrun:
            mrun.return_value = MagicMock(returncode=3, stdout="", stderr="bad")
            res = rem.execute_action("restart_ollama", dry_run=False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["returncode"], 3)

    def test_fmt_result_variants(self):
        self.assertIn("error:", rem._fmt_result({"error": "x"}))
        self.assertEqual(rem._fmt_result({"note": "dry_run: not executed"}),
                         "dry_run: not executed")
        s = rem._fmt_result({"returncode": 0, "stdout": "hi", "stderr": ""})
        self.assertIn("rc=0", s)
        self.assertIn("out=hi", s)

    def test_actions_have_required_tiers(self):
        for name, spec in rem.ACTIONS.items():
            self.assertIn(spec["tier"], (rem.SAFE, rem.IMPACTFUL), name)
        self.assertEqual(rem.ACTIONS["reboot_host"]["tier"], rem.IMPACTFUL)
        self.assertEqual(rem.ACTIONS["restart_ollama"]["tier"], rem.SAFE)


# ===========================================================================
# 5. INTEGRATION TESTS  (live DB, rolled back)
# ===========================================================================

class TestIntegration:

    def test_propose_only_when_disabled_executes_nothing(self, db, _hard_safety):
        """SAFETY-CRITICAL: with REMEDIATION_ENABLED False, every step is
        merely 'proposed' and subprocess.run is NEVER called."""
        conn, inc_id, _ = db
        assert rem.REMEDIATION_ENABLED is False
        out = rem.propose_for_incident(conn, inc_id, dry_run=False)

        assert out["runbook"] == ["restart_ollama", "reboot_host"]
        statuses = {s["action"]: s["status"] for s in out["steps"]}
        assert statuses["restart_ollama"] == "proposed"
        assert statuses["reboot_host"] == "proposed"

        # NOTHING executed.
        assert _hard_safety["sub"] == [], "subprocess.run must never be called"
        rows = _rows_for(conn, inc_id)
        assert all(r[2] == "proposed" for r in rows), rows
        assert not any(r[2] == "executed" for r in rows)

    def test_propose_emits_notify_per_step_no_real_event(self, db, _hard_safety):
        """Each proposed step notifies — but only via the stubbed recorder."""
        conn, inc_id, _ = db
        rem.propose_for_incident(conn, inc_id, dry_run=False)
        titles = [c["title"] for c in _hard_safety["notify"]]
        assert any("Proposed fix" in t for t in titles)
        # Two runbook steps -> two notifications.
        assert len(_hard_safety["notify"]) == 2

    def test_impactful_is_approval_gated_when_enabled(self, db, _hard_safety, monkeypatch):
        """SAFETY-CRITICAL: even with the master switch ON, the impactful
        reboot is NEVER auto-executed — only the safe step runs."""
        conn, inc_id, _ = db
        monkeypatch.setattr(rem, "REMEDIATION_ENABLED", True)
        out = rem.propose_for_incident(conn, inc_id, dry_run=False)

        statuses = {s["action"]: s["status"] for s in out["steps"]}
        # Safe step actually executed (subprocess recorder returns rc=0).
        assert statuses["restart_ollama"] == "executed"
        # Impactful step proposed, NOT executed.
        assert statuses["reboot_host"] == "proposed"

        # Only the SAFE action's argv was ever handed to subprocess.run.
        assert _hard_safety["sub"] == [rem.ACTIONS["restart_ollama"]["argv"]]
        # The reboot argv was never executed.
        assert rem.ACTIONS["reboot_host"]["argv"] not in _hard_safety["sub"]

    def test_cooldown_skips_duplicate_within_30m(self, db, _hard_safety):
        """SAFETY: re-proposing the same incident inside COOLDOWN_S skips both steps."""
        conn, inc_id, _ = db
        rem.propose_for_incident(conn, inc_id, dry_run=False)
        again = rem.propose_for_incident(conn, inc_id, dry_run=False)
        statuses = {s["action"]: s["status"] for s in again["steps"]}
        assert statuses["restart_ollama"] == "skipped"
        assert statuses["reboot_host"] == "skipped"
        assert all(s.get("reason") == "cooldown" for s in again["steps"])

    def test_no_runbook_for_unknown_host(self, db, _hard_safety):
        """An incident with no matching runbook yields no steps, no actions."""
        conn, inc_id, ev_id = db
        cur = conn.cursor(cursor_factory=psycopg2.extensions.cursor)
        cur.execute("UPDATE telemetry.incidents SET host='Unknown-Host' WHERE id=%s", (inc_id,))
        out = rem.propose_for_incident(conn, inc_id)
        assert out["runbook"] == []
        assert out["steps"] == []
        assert "no runbook" in out["error"]
        assert _hard_safety["sub"] == []

    def test_propose_for_closed_incident_is_noop(self, db, _hard_safety):
        conn, inc_id, _ = db
        cur = conn.cursor(cursor_factory=psycopg2.extensions.cursor)
        cur.execute("UPDATE telemetry.incidents SET status='resolved' WHERE id=%s", (inc_id,))
        out = rem.propose_for_incident(conn, inc_id)
        assert "no open incident" in out["error"]
        assert out["steps"] == []
        assert _hard_safety["sub"] == []

    def test_zero_rows_leak_after_rollback(self, db, _hard_safety):
        """The connection wrapper neutralizes commit; rows live only in the txn."""
        conn, inc_id, _ = db
        rem.propose_for_incident(conn, inc_id)
        assert len(_rows_for(conn, inc_id)) == 2  # visible inside the open txn
        # A SEPARATE connection must NOT see them (they were never committed).
        other = _live_conn()
        try:
            c2 = other.cursor()
            c2.execute("SELECT count(*) FROM telemetry.remediations WHERE incident_id=%s",
                       (inc_id,))
            assert c2.fetchone()[0] == 0
        finally:
            other.close()


# ===========================================================================
# 6. FUNCTIONAL TESTS  (approve() path, live DB rolled back)
# ===========================================================================

class TestFunctional:

    def _proposed_reboot_id(self, conn, inc_id):
        out = rem.propose_for_incident(conn, inc_id)
        for s in out["steps"]:
            if s["action"] == "reboot_host":
                return s["remediation_id"]
        raise AssertionError("reboot_host was not proposed")

    def test_approve_executes_gated_action(self, db, _hard_safety):
        """approve() is the ONLY path that runs an impactful action — and it
        runs the harmless echo argv (recorder), recording 'executed'."""
        conn, inc_id, _ = db
        rid = self._proposed_reboot_id(conn, inc_id)
        out = rem.approve(conn, rid, dry_run=False)
        assert out["status"] == "executed"
        # The exact (harmless) reboot argv was run via subprocess.
        assert rem.ACTIONS["reboot_host"]["argv"] in _hard_safety["sub"]

    def test_approve_dry_run_does_not_execute(self, db, _hard_safety):
        """approve(dry_run=True) records but never calls subprocess."""
        conn, inc_id, _ = db
        rid = self._proposed_reboot_id(conn, inc_id)
        # Reset recorder so we only observe approve()'s effect.
        _hard_safety["sub"].clear()
        out = rem.approve(conn, rid, dry_run=True)
        assert out["status"] == "executed"  # dry_run reports ok
        assert _hard_safety["sub"] == [], "dry_run approve must not call subprocess"

    def test_approve_missing_remediation(self, db, _hard_safety):
        conn, _, _ = db
        out = rem.approve(conn, 2_000_000_000)
        assert "not found" in out["error"]

    def test_approve_rejects_already_executed(self, db, _hard_safety):
        """A remediation already 'executed' cannot be re-approved."""
        conn, inc_id, _ = db
        rid = self._proposed_reboot_id(conn, inc_id)
        rem.approve(conn, rid)            # -> executed
        _hard_safety["sub"].clear()
        out = rem.approve(conn, rid)      # second approve must refuse
        assert "not approvable" in out["error"]
        assert _hard_safety["sub"] == []

    def test_approve_rejects_action_removed_from_allowlist(self, db, _hard_safety, monkeypatch):
        """If the action vanished from ACTIONS, approve() fails it, runs nothing."""
        conn, inc_id, _ = db
        rid = self._proposed_reboot_id(conn, inc_id)
        # Drop reboot_host from the allowlist after it was proposed.
        new_actions = dict(rem.ACTIONS)
        del new_actions["reboot_host"]
        monkeypatch.setattr(rem, "ACTIONS", new_actions)
        _hard_safety["sub"].clear()
        out = rem.approve(conn, rid)
        assert "not in allowlist" in out["error"]
        assert _hard_safety["sub"] == []


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

    def test_public_api_present(self):
        for fn in ("propose_for_incident", "approve", "execute_action", "ensure_schema"):
            self.assertTrue(callable(getattr(rem, fn)), fn)

    def test_runbook_steps_all_allowlisted(self):
        """Every action named in any runbook exists in the allowlist."""
        for key, steps in rem.RUNBOOKS.items():
            for action in steps:
                self.assertIn(action, rem.ACTIONS, f"{action} (runbook {key}) not allowlisted")

    def test_runbook_safe_before_impactful(self):
        """Within a runbook, safe steps precede impactful ones."""
        for steps in rem.RUNBOOKS.values():
            tiers = [rem.ACTIONS[a]["tier"] for a in steps]
            seen_impactful = False
            for t in tiers:
                if t == rem.IMPACTFUL:
                    seen_impactful = True
                elif seen_impactful:
                    self.fail("safe step found after an impactful step")


if __name__ == "__main__":
    unittest.main(verbosity=2)
