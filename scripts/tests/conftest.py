"""
conftest.py — Shared fixtures, markers, and Slack notification hooks for Nova script tests.
Posts warnings and errors to #nova-notifications on test suite failures.

Written by Jordan Koch.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure the scripts directory is on sys.path for all tests
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

# ── Slack notification on test failures ──────────────────────────────────────

SLACK_NOTIFY_CHANNEL = "C0ATAF7NZG9"  # #nova-notifications
_failures = []
_session_start = 0


def _get_slack_token():
    try:
        import nova_config
        return nova_config.slack_bot_token()
    except Exception:
        return os.environ.get("NOVA_SLACK_BOT_TOKEN", "")


def _slack_post(channel, text):
    token = _get_slack_token()
    if not token:
        return
    payload = json.dumps({"channel": channel, "text": text, "mrkdwn": True}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def pytest_sessionstart(session):
    global _session_start
    _session_start = time.time()


def pytest_runtest_logreport(report):
    if report.when == "call" and report.failed:
        _failures.append(report.nodeid)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if not _failures:
        return
    duration = round(time.time() - _session_start, 1)
    total = session.testscollected
    failed = len(_failures)
    passed = total - failed

    lines = [":test_tube: *Nova Test Suite — Failures Detected*"]
    lines.append(f"Results: {passed}/{total} passed, {failed} failed ({duration}s)")
    lines.append("")
    lines.append(":red_circle: *Failures:*")
    for f in _failures[:20]:
        lines.append(f"  • `{f}`")
    if len(_failures) > 20:
        lines.append(f"  ...and {len(_failures) - 20} more")
    _slack_post(SLACK_NOTIFY_CHANNEL, "\n".join(lines))


# ── Markers ──────────────────────────────────────────────────────────────────

def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "integration: marks tests that hit live services (PostgreSQL, Ollama, etc.)")
    config.addinivalue_line("markers", "functional: marks end-to-end workflow tests")
    config.addinivalue_line("markers", "frame: marks tests that verify HTML/CSS/JS frame rendering")
    config.addinivalue_line("markers", "dashboard: marks dashboard server unit tests")


@pytest.fixture
def mock_nova_config(monkeypatch):
    """Mock nova_config to prevent real Slack/Discord/Keychain calls.

    Not autouse -- tests must opt in so scripts that import nova_config at
    module level can be handled per-test with importlib.reload().
    """
    mock_config = MagicMock()
    mock_config.VECTOR_URL = "http://127.0.0.1:18790/remember"
    mock_config.SLACK_API = "https://slack.com/api"
    mock_config.SLACK_CHAN = "C_TEST_CHAT"
    mock_config.SLACK_NOTIFY = "C_TEST_NOTIFY"
    mock_config.SLACK_EMAIL = "C_TEST_EMAIL"
    mock_config.SLACK_PHOTOS = "C_TEST_PHOTOS"
    mock_config.JORDAN_DM = "D_TEST_DM"
    mock_config.SCRIPTS_DIR = str(SCRIPTS_DIR)
    mock_config.slack_bot_token.return_value = "xoxb-test-token"
    mock_config.post_both = MagicMock()
    mock_config.post_discord = MagicMock(return_value=True)
    monkeypatch.setitem(sys.modules, "nova_config", mock_config)
    return mock_config


@pytest.fixture
def mock_nova_logger(monkeypatch):
    """Mock nova_logger to prevent real file I/O."""
    mock_logger = MagicMock()
    mock_logger.LOG_INFO = "info"
    mock_logger.LOG_ERROR = "error"
    mock_logger.LOG_WARN = "warn"
    mock_logger.LOG_DEBUG = "debug"
    mock_logger.log = MagicMock()
    monkeypatch.setitem(sys.modules, "nova_logger", mock_logger)
    return mock_logger


@pytest.fixture
def tmp_state_dir(tmp_path):
    """Provide a temporary directory for state files."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture
def tmp_logs_dir(tmp_path):
    """Provide a temporary directory for log files."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    return logs_dir
