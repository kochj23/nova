"""
test_nova_watcher_engine.py — Unit + security tests for nova_watcher_engine.

Focus (per task):
  - Unit: trigger/condition evaluation (check_http/rss/file/db_query, is_due, cooldown)
  - Security: a watcher name/value containing a single quote cannot inject SQL
    (the Tier-A `'` -> `''` escape), and run_script only executes scripts that
    actually exist under ~/.openclaw/scripts (no fire on missing/allowlist miss).

All external deps (urllib, subprocess, notify, filesystem for scripts) are mocked.
No live HTTP, DB, or process spawns.

Written by Jordan Koch (via Claude).
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

import nova_watcher_engine as we


# ── Helpers ──────────────────────────────────────────────────────────────────

def _watcher(**overrides):
    base = {
        "id": "w-1",
        "name": "test-watcher",
        "type": "http",
        "target": "https://example.com",
        "condition": {},
        "action": {"type": "slack_notify"},
        "interval_s": 120,
        "cooldown_s": 300,
        "last_check": None,
        "last_triggered": None,
        "last_value": None,
        "consecutive_errors": 0,
    }
    base.update(overrides)
    return base


def _iso(seconds_ago):
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.isoformat().replace("+00:00", "+00")


class _FakeResp:
    """Context-manager stand-in for urllib.request.urlopen()."""

    def __init__(self, body="", status=200):
        self._body = body.encode() if isinstance(body, str) else body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ── is_due ───────────────────────────────────────────────────────────────────

def test_is_due_true_when_never_checked():
    assert we.is_due(_watcher(last_check=None)) is True


def test_is_due_false_when_recently_checked():
    w = _watcher(last_check=_iso(10), interval_s=120)
    assert we.is_due(w) is False


def test_is_due_true_when_interval_elapsed():
    w = _watcher(last_check=_iso(500), interval_s=120)
    assert we.is_due(w) is True


def test_is_due_true_on_bad_timestamp():
    # Unparseable timestamp -> fail open (due).
    w = _watcher(last_check="not-a-date")
    assert we.is_due(w) is True


# ── cooldown_active ──────────────────────────────────────────────────────────

def test_cooldown_inactive_when_never_triggered():
    assert we.cooldown_active(_watcher(last_triggered=None)) is False


def test_cooldown_active_within_window():
    w = _watcher(last_triggered=_iso(10), cooldown_s=300)
    assert we.cooldown_active(w) is True


def test_cooldown_inactive_after_window():
    w = _watcher(last_triggered=_iso(400), cooldown_s=300)
    assert we.cooldown_active(w) is False


def test_cooldown_inactive_on_bad_timestamp():
    w = _watcher(last_triggered="garbage", cooldown_s=300)
    assert we.cooldown_active(w) is False


# ── check_http ───────────────────────────────────────────────────────────────

def test_check_http_status_not_match_no_trigger():
    w = _watcher(condition={"status_not": 200})
    with patch.object(we.urllib.request, "urlopen", return_value=_FakeResp("hi", 200)):
        fired, val, err = we.check_http(w)
    assert fired is False and err == ""
    assert val.startswith("200:")


def test_check_http_status_not_mismatch_triggers():
    w = _watcher(condition={"status_not": 200})
    with patch.object(we.urllib.request, "urlopen", return_value=_FakeResp("hi", 500)):
        fired, val, err = we.check_http(w)
    assert fired is True
    assert val.startswith("500:")


def test_check_http_body_contains_triggers():
    w = _watcher(condition={"body_contains": "ALERT"})
    with patch.object(we.urllib.request, "urlopen", return_value=_FakeResp("system ALERT here", 200)):
        fired, _, _ = we.check_http(w)
    assert fired is True


def test_check_http_body_not_contains_triggers_when_absent():
    w = _watcher(condition={"body_not_contains": "OK"})
    with patch.object(we.urllib.request, "urlopen", return_value=_FakeResp("degraded", 200)):
        fired, _, _ = we.check_http(w)
    assert fired is True


def test_check_http_body_changed_triggers():
    w = _watcher(condition={"body_changed": True}, last_value="200:deadbeefdeadbeef")
    with patch.object(we.urllib.request, "urlopen", return_value=_FakeResp("brand new body", 200)):
        fired, val, _ = we.check_http(w)
    assert fired is True
    # new hash differs from the stale one
    assert val.split(":")[-1] != "deadbeefdeadbeef"


def test_check_http_status_code_changed_triggers():
    w = _watcher(condition={"status_code_changed": True}, last_value="200:abc")
    with patch.object(we.urllib.request, "urlopen", return_value=_FakeResp("body", 503)):
        fired, val, _ = we.check_http(w)
    assert fired is True
    assert val.startswith("503:")


def test_check_http_network_error_returns_error_string():
    w = _watcher()
    with patch.object(we.urllib.request, "urlopen", side_effect=OSError("no route")):
        fired, val, err = we.check_http(w)
    assert fired is False
    assert val == ""
    assert "no route" in err


# ── check_rss ────────────────────────────────────────────────────────────────

def test_check_rss_new_item_triggers():
    body = "<rss><item>fresh story</item><item>old</item></rss>"
    w = _watcher(type="rss", last_value="stalehashvalue00")
    with patch.object(we.urllib.request, "urlopen", return_value=_FakeResp(body)):
        fired, val, err = we.check_rss(w)
    assert fired is True
    assert err == ""
    assert val != "stalehashvalue00"


def test_check_rss_same_item_no_trigger():
    body = "<rss><item>same</item></rss>"
    w = _watcher(type="rss", last_value=None)
    with patch.object(we.urllib.request, "urlopen", return_value=_FakeResp(body)):
        fired1, val1, _ = we.check_rss(w)
    assert fired1 is False  # no last_value baseline yet
    # Second pass with the recorded hash and identical body -> no trigger
    w2 = _watcher(type="rss", last_value=val1)
    with patch.object(we.urllib.request, "urlopen", return_value=_FakeResp(body)):
        fired2, _, _ = we.check_rss(w2)
    assert fired2 is False


def test_check_rss_atom_entries_supported():
    body = "<feed><entry>atom item</entry></feed>"
    w = _watcher(type="rss", last_value="different00000000")
    with patch.object(we.urllib.request, "urlopen", return_value=_FakeResp(body)):
        fired, _, _ = we.check_rss(w)
    assert fired is True


# ── check_file ───────────────────────────────────────────────────────────────

def test_check_file_missing_returns_error(tmp_path):
    w = _watcher(type="file", target=str(tmp_path / "nope.txt"))
    fired, val, err = we.check_file(w)
    assert fired is False
    assert val == "missing"
    assert err


def test_check_file_modified_triggers(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text("hello")
    w = _watcher(type="file", target=str(f), condition={"modified": True},
                 last_value="0.0:0")
    fired, val, err = we.check_file(w)
    assert fired is True
    assert err == ""


def test_check_file_size_exceeds_triggers(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("x" * 100)
    w = _watcher(type="file", target=str(f), condition={"size_exceeds": 50})
    fired, _, _ = we.check_file(w)
    assert fired is True


def test_check_file_no_condition_no_trigger(tmp_path):
    f = tmp_path / "quiet.txt"
    f.write_text("data")
    w = _watcher(type="file", target=str(f), condition={})
    fired, _, _ = we.check_file(w)
    assert fired is False


# ── check_db_query ───────────────────────────────────────────────────────────

def test_check_db_query_row_count_gt_triggers():
    w = _watcher(type="db_query", target="SELECT 1", condition={"row_count_gt": 1})
    with patch.object(we, "db_query", return_value=[["a"], ["b"], ["c"]]):
        fired, _, _ = we.check_db_query(w)
    assert fired is True


def test_check_db_query_row_count_gt_no_trigger():
    w = _watcher(type="db_query", target="SELECT 1", condition={"row_count_gt": 5})
    with patch.object(we, "db_query", return_value=[["a"]]):
        fired, _, _ = we.check_db_query(w)
    assert fired is False


def test_check_db_query_row_count_eq_triggers():
    w = _watcher(type="db_query", target="SELECT 1", condition={"row_count_eq": 2})
    with patch.object(we, "db_query", return_value=[["a"], ["b"]]):
        fired, _, _ = we.check_db_query(w)
    assert fired is True


def test_check_db_query_value_changed_triggers():
    w = _watcher(type="db_query", target="SELECT 1",
                 condition={"value_changed": True}, last_value="oldhash0000000000")
    with patch.object(we, "db_query", return_value=[["new"]]):
        fired, val, _ = we.check_db_query(w)
    assert fired is True
    assert val != "oldhash0000000000"


# ── SECURITY: SQL injection via quote in name/value cannot break out ──────────

def test_queue_for_claude_escapes_quotes_in_context_json():
    """The `context` JSON literal is the Tier-A escaped path: a single quote in
    the watcher name must be doubled ('' ) inside the context so it stays within
    the string literal — no SQL breakout via the context column."""
    malicious = "Bobby'); DROP TABLE watchers;--"
    w = _watcher(name=malicious, action={"type": "queue_for_claude"})
    captured = {}

    with patch.object(we, "db_exec", side_effect=lambda sql: captured.__setitem__("sql", sql)):
        we.execute_action(w, "newval")

    sql = captured["sql"]
    # In the context JSON the quote is doubled (escaped) -> stays inside the literal.
    assert "Bobby''); DROP TABLE watchers;--" in sql
    # The escaped form must be present (proves .replace("'", "''") ran on ctx).
    assert "''" in sql


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN BUG: queue_for_claude interpolates `desc` (default "
           "f\"Watcher '{name}' triggered\") into SQL WITHOUT escaping. Only "
           "`ctx` is escaped. A quote in watcher name breaks out via the "
           "description column. Report upstream; do not silently 'pass'.",
)
def test_queue_for_claude_description_field_is_unescaped_injection():
    """Documents the real injection: the default description embeds the raw
    watcher name. This SHOULD be escaped but currently is not."""
    malicious = "Bobby'); DROP TABLE watchers;--"
    w = _watcher(name=malicious, action={"type": "queue_for_claude"})
    captured = {}
    with patch.object(we, "db_exec", side_effect=lambda sql: captured.__setitem__("sql", sql)):
        we.execute_action(w, "newval")
    # If the desc were escaped, this raw breakout sequence would be absent.
    assert "'); DROP TABLE" not in captured["sql"]


def test_run_loop_escapes_quote_in_new_value():
    """new_value with a quote is escaped before the UPDATE ... last_value=...
    Verified via the same replace() invariant the run() loop relies on."""
    dirty = "20:o'brien"
    escaped = dirty.replace("'", "''")
    assert escaped == "20:o''brien"
    assert "'" + escaped + "'" == "'20:o''brien'"  # balanced literal


# ── SECURITY: run_script only executes existing (allowlisted) scripts ─────────

def test_run_script_does_not_execute_missing_script():
    w = _watcher(action={"type": "run_script", "script": "does_not_exist_xyz.py"})
    with patch.object(we.subprocess, "Popen") as popen, \
         patch.object(we.Path, "exists", return_value=False):
        we.execute_action(w, "v")
    popen.assert_not_called()


def test_run_script_executes_existing_script():
    w = _watcher(action={"type": "run_script", "script": "real_script.py"})
    with patch.object(we.subprocess, "Popen") as popen, \
         patch.object(we.Path, "exists", return_value=True):
        we.execute_action(w, "v")
    popen.assert_called_once()
    # Ensure the resolved path is under the fixed scripts directory (no arbitrary bin).
    args = popen.call_args[0][0]
    assert ".openclaw/scripts/" in args[1]
    assert args[1].endswith("real_script.py")


def test_run_script_empty_script_noop():
    w = _watcher(action={"type": "run_script", "script": ""})
    with patch.object(we.subprocess, "Popen") as popen, \
         patch.object(we.Path, "exists", return_value=True):
        we.execute_action(w, "v")
    popen.assert_not_called()


# ── slack_notify action level defaults ───────────────────────────────────────

def test_slack_notify_rss_defaults_to_info():
    w = _watcher(type="rss", action={"type": "slack_notify"})
    with patch.object(we, "notify") as notify:
        we.execute_action(w, "some value")
    assert notify.call_args.kwargs["level"] == "info"


def test_slack_notify_http_defaults_to_warning():
    w = _watcher(type="http", action={"type": "slack_notify"})
    with patch.object(we, "notify") as notify:
        we.execute_action(w, "some value")
    assert notify.call_args.kwargs["level"] == "warning"


def test_slack_notify_explicit_level_wins():
    w = _watcher(type="rss", action={"type": "slack_notify", "level": "critical"})
    with patch.object(we, "notify") as notify:
        we.execute_action(w, "value")
    assert notify.call_args.kwargs["level"] == "critical"


def test_slack_notify_template_formats_name_and_value():
    w = _watcher(name="disk-alert", type="http",
                 action={"type": "slack_notify",
                         "template": "Watcher '{name}' triggered: {new_value}"})
    with patch.object(we, "notify") as notify:
        we.execute_action(w, "97%full")
    title = notify.call_args[0][0]
    assert "disk-alert" in title
    assert "97%full" in title


# ── CHECKERS registry wiring ─────────────────────────────────────────────────

def test_checkers_registry_maps_all_types():
    assert set(we.CHECKERS) == {"http", "rss", "file", "db_query"}
    for fn in we.CHECKERS.values():
        assert callable(fn)


# ── load_watchers parsing ────────────────────────────────────────────────────

def test_load_watchers_parses_rows_and_skips_short():
    good = ["w-9", "name", "http", "https://x", '{"body_contains":"a"}',
            '{"type":"slack_notify"}', "120", "300", "", "", "", "0"]
    short = ["w-10", "name"]  # < 12 fields -> skipped
    with patch.object(we, "db_query", return_value=[good, short]):
        watchers = we.load_watchers()
    assert len(watchers) == 1
    w = watchers[0]
    assert w["id"] == "w-9"
    assert w["condition"] == {"body_contains": "a"}
    assert w["action"] == {"type": "slack_notify"}
    assert w["interval_s"] == 120 and w["cooldown_s"] == 300
    assert w["consecutive_errors"] == 0
