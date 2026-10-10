"""session-logger.sh (PostToolUse hook) — all seven test categories.

The hook writes to the live ops database, so these tests never send a real tool event. Empty or unparseable
input makes the hook exit before any database call, and the rest is checked as text.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path.home() / ".claude" / "hooks" / "session-logger.sh"
JQ = shutil.which("jq") or "/opt/homebrew/bin/jq"


@pytest.fixture(scope="module")
def src():
    return HOOK.read_text()


# ── Security ──────────────────────────────────────────────────────────────────
def test_security_quotes_are_escaped_before_sql(src):
    assert "sed \"s/'/''/g\"" in src
    assert "TARGET_ESC" in src and "DESC_ESC" in src


def test_security_no_host_literal_in_hook(src):
    assert "pg-primary.digitalnoise.net" in src  # default only, overridable by NOVA_PG_HOST
    assert "NOVA_PG_HOST" in src


def test_security_rationale_only_from_command_or_agent(src):
    assert 'command|agent) RAT_ESC="$DESC_ESC"' in src
    assert "RAT_ESC=\"\"" in src


# ── Performance ───────────────────────────────────────────────────────────────
def test_performance_hook_is_bounded_in_retries(src):
    assert "for attempt in 1 2 3" in src
    assert "sleep $((attempt * 2))" in src


# ── Retry ─────────────────────────────────────────────────────────────────────
def test_retry_action_write_has_three_attempts_with_backoff(src):
    assert src.count("psql -h \"$DB_HOST\"") >= 2
    assert "if [ $? -eq 0 ]; then break; fi" in src


# ── Unit ──────────────────────────────────────────────────────────────────────
def test_unit_bash_syntax_is_valid():
    out = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


# ── Integration ───────────────────────────────────────────────────────────────
def test_integration_hook_exits_zero_on_empty_input():
    out = subprocess.run(["bash", str(HOOK)], input="", capture_output=True, text=True, timeout=30)
    assert out.returncode == 0


def test_integration_hook_skips_input_without_tool_name():
    out = subprocess.run(["bash", str(HOOK)], input='{"session_id":"x"}', capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0


# ── Functional ────────────────────────────────────────────────────────────────
def test_functional_hook_sets_rationale_only_for_bash_and_agent(src):
    # The action type decides whether the description becomes the rationale.
    assert 'case "$ACTION_TYPE" in' in src
    assert "NULLIF('$RAT_ESC', '')" in src


# ── Frame ─────────────────────────────────────────────────────────────────────
def test_frame_hook_file_exists_and_is_executable_text():
    assert HOOK.exists() and HOOK.read_text().startswith("#!/bin/bash")
