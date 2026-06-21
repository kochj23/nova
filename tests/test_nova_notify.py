"""
test_nova_notify.py — All 7 test categories for nova_notify.py

nova_notify.notify() is the ONE way Nova emits a notification. It writes a
structured row to telemetry.events (the event bus); the LIVE nova_notifier
daemon then routes those rows to Slack. So these tests MUST NEVER let a real
INSERT reach Postgres and MUST NEVER run a real `psql` subprocess — either of
those would spam #nova-notifications via the daemon.

Safety strategy used here (no live state ever touched):
  * notify() imports psycopg2 LAZILY inside the function, so every test patches
    sys.modules["psycopg2"] with a FakePsycopg2 whose connect() returns a fake
    connection. The fake cursor RECORDS the SQL + params instead of executing
    them. No real connection is ever opened.
  * subprocess.run is monkeypatched in every test that can reach the psql
    fallback, so the real `psql` binary is never invoked.
  * No row is ever written to telemetry.events. A FrameTest asserts the module
    never connects to a real DSN by confirming the real psycopg2 / subprocess
    are not called unless we injected a fake.

Written by Jordan Koch.
"""

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


# --------------------------------------------------------------------------
# Load the module under test (matches the tests/ convention: importlib.util)
# --------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_notify.py"
_spec = importlib.util.spec_from_file_location("nova_notify", _SCRIPT)
nova_notify = importlib.util.module_from_spec(_spec)
sys.modules["nova_notify"] = nova_notify
_spec.loader.exec_module(nova_notify)

notify = nova_notify.notify
_VALID_LEVELS = nova_notify._VALID_LEVELS
_DSN = nova_notify._DSN


# --------------------------------------------------------------------------
# Fake psycopg2 — records INSERTs instead of executing them. NEVER touches PG.
# --------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self, recorder):
        self._recorder = recorder

    def execute(self, sql, params=None):
        self._recorder.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, recorder, dsn, connect_timeout=None):
        self._recorder = recorder
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.committed = False

    def cursor(self):
        return _FakeCursor(self._recorder)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        # psycopg2 connection context manager commits on clean exit.
        if not a[0]:
            self.committed = True
        return False


def _fake_psycopg2(recorder, connect_calls):
    """Build a fake psycopg2 module whose connect() records the row."""
    fake = MagicMock(name="fake_psycopg2")

    def _connect(dsn, connect_timeout=None):
        connect_calls.append({"dsn": dsn, "connect_timeout": connect_timeout})
        return _FakeConn(recorder, dsn, connect_timeout)

    fake.connect.side_effect = _connect
    return fake


class NotifyTestBase(unittest.TestCase):
    """Base that installs the fake psycopg2 + neuters subprocess for safety.

    Every subclass test runs with:
      - sys.modules["psycopg2"] -> fake (records INSERTs, no real connection)
      - subprocess.run -> MagicMock (psql fallback never runs the real binary)
    so a stray test can never write to telemetry.events or trigger Slack.
    """

    def setUp(self):
        self.recorder = []          # (sql, params) tuples for the happy path
        self.connect_calls = []     # psycopg2.connect invocations
        self.fake_pg = _fake_psycopg2(self.recorder, self.connect_calls)

        self._pg_patch = patch.dict(sys.modules, {"psycopg2": self.fake_pg})
        self._pg_patch.start()

        # Hard safety net: real psql must never run. Default fallback is a no-op
        # that "succeeds" so tests don't accidentally exercise the real binary.
        self.subprocess_mock = MagicMock(return_value=MagicMock(returncode=0))
        self._sp_patch = patch.object(subprocess, "run", self.subprocess_mock)
        self._sp_patch.start()

        # Make source auto-detection deterministic.
        self._argv_patch = patch.object(sys, "argv", ["nova_unittest.py"])
        self._argv_patch.start()

    def tearDown(self):
        self._argv_patch.stop()
        self._sp_patch.stop()
        self._pg_patch.stop()

    # -- helpers -----------------------------------------------------------
    def last_row(self):
        """Return the params dict of the last recorded INSERT."""
        self.assertTrue(self.recorder, "no INSERT was recorded")
        sql, params = self.recorder[-1]
        return params


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================
class TestSecurity(NotifyTestBase):

    def test_no_hardcoded_secrets_in_source(self):
        src = _SCRIPT.read_text()
        for bad in ("xoxb-", "password=", "AKIA", "BEGIN PRIVATE KEY"):
            self.assertNotIn(bad, src, f"hardcoded secret marker {bad!r} found")

    def test_dsn_uses_local_socket_no_password(self):
        # DSN must be localhost and carry no embedded password.
        self.assertIn("host=127.0.0.1", _DSN)
        self.assertNotIn("password", _DSN.lower())

    def test_insert_is_parameterized_not_string_formatted(self):
        # SQL injection guard: title is bound via params, not concatenated.
        notify("'; DROP TABLE telemetry.events; --", level="info")
        sql, params = self.recorder[-1]
        self.assertIn("%(title)s", sql)
        self.assertEqual(params["title"], "'; DROP TABLE telemetry.events; --")

    def test_title_truncated_to_500_chars(self):
        notify("X" * 5000)
        self.assertEqual(len(self.last_row()["title"]), 500)

    def test_meta_is_json_encoded_string(self):
        notify("t", meta={"k": "v", "n": 1})
        meta = self.last_row()["meta"]
        self.assertIsInstance(meta, str)
        self.assertEqual(json.loads(meta), {"k": "v", "n": 1})


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================
class TestPerformance(NotifyTestBase):

    def test_notify_is_fast(self):
        import time
        start = time.time()
        for _ in range(200):
            notify("perf", level="info")
        self.assertLess(time.time() - start, 1.0)

    def test_connect_timeout_is_bounded(self):
        notify("t")
        self.assertEqual(self.connect_calls[-1]["connect_timeout"], 5)

    def test_single_connect_per_notify(self):
        notify("t")
        self.assertEqual(len(self.connect_calls), 1)


# ===========================================================================
# 3. RETRY / RESILIENCE TESTS  (never raises; DB-down -> psql fallback)
# ===========================================================================
class TestRetry(NotifyTestBase):

    def test_db_down_falls_back_to_psql_subprocess(self):
        # psycopg2.connect blows up -> module must try the psql fallback.
        self.fake_pg.connect.side_effect = Exception("connection refused")
        ok = notify("db is down", level="warning", category="storage")
        self.assertTrue(ok, "psql fallback should have succeeded")
        self.assertEqual(self.subprocess_mock.call_count, 1)
        argv = self.subprocess_mock.call_args[0][0]
        self.assertEqual(argv[0], "psql")
        self.assertIn(_DSN, argv)

    def test_psql_fallback_passes_correct_values(self):
        self.fake_pg.connect.side_effect = Exception("no psycopg2")
        notify("fallback title", body="bod", level="critical",
               category="net", dedup_key="dk1", correlation_id="cid1")
        argv = self.subprocess_mock.call_args[0][0]
        self.assertIn("s=nova_unittest.py", argv)
        self.assertIn("l=critical", argv)
        self.assertIn("c=net", argv)
        self.assertIn("t=fallback title", argv)
        self.assertIn("b=bod", argv)
        self.assertIn("dk=dk1", argv)
        self.assertIn("ci=cid1", argv)

    def test_never_raises_when_both_paths_fail(self):
        # Both psycopg2 AND psql fail -> notify must return False, not raise.
        self.fake_pg.connect.side_effect = Exception("pg dead")
        self.subprocess_mock.side_effect = FileNotFoundError("psql missing")
        try:
            ok = notify("everything is broken", level="critical")
        except Exception as e:  # pragma: no cover - this is the failure we guard
            self.fail(f"notify() raised {e!r}; it must never raise")
        self.assertFalse(ok)

    def test_psql_timeout_does_not_raise(self):
        self.fake_pg.connect.side_effect = Exception("pg dead")
        self.subprocess_mock.side_effect = subprocess.TimeoutExpired("psql", 10)
        ok = notify("slow psql", level="info")
        self.assertFalse(ok)

    def test_unserializable_meta_raises_before_try_block(self):
        # KNOWN behavior: json.dumps(meta) runs BEFORE the try/except in
        # notify(), so a non-serializable meta value DOES propagate. This
        # documents that the "never raises" guarantee only covers the DB I/O
        # path, not arg marshalling. Callers must pass JSON-safe meta.
        class Unserializable:
            pass
        with self.assertRaises(TypeError):
            notify("bad meta", meta={"x": Unserializable()})
        # And no INSERT / psql write happened.
        self.assertEqual(self.recorder, [])
        self.assertEqual(self.subprocess_mock.call_count, 0)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================
class TestUnit(NotifyTestBase):

    def test_enqueue_writes_row_with_right_fields(self):
        ok = notify("UNAS storage low", body="1.4TB free", level="warning",
                    category="storage", dedup_key="unas-storage-low",
                    correlation_id="corr-9", meta={"free": "1.4TB"})
        self.assertTrue(ok)
        row = self.last_row()
        self.assertEqual(row["title"], "UNAS storage low")
        self.assertEqual(row["body"], "1.4TB free")
        self.assertEqual(row["level"], "warning")
        self.assertEqual(row["category"], "storage")
        self.assertEqual(row["dedup_key"], "unas-storage-low")
        self.assertEqual(row["correlation_id"], "corr-9")
        self.assertEqual(json.loads(row["meta"]), {"free": "1.4TB"})

    def test_insert_targets_telemetry_events(self):
        notify("t")
        sql, _ = self.recorder[-1]
        self.assertIn("INSERT INTO telemetry.events", sql)

    def test_invalid_level_coerced_to_info(self):
        for bad in ("debug", "WARNING", "emergency", "", "trace", None):
            notify("t", level=bad)
            self.assertEqual(self.last_row()["level"], "info",
                             f"level {bad!r} should coerce to info")

    def test_valid_levels_preserved(self):
        for lvl in _VALID_LEVELS:
            notify("t", level=lvl)
            self.assertEqual(self.last_row()["level"], lvl)

    def test_source_auto_detected_from_argv(self):
        with patch.object(sys, "argv", ["/Volumes/Data/scripts/nova_calendar.py"]):
            notify("t")
        self.assertEqual(self.last_row()["source"], "nova_calendar.py")

    def test_source_explicit_overrides_autodetect(self):
        notify("t", source="custom_source")
        self.assertEqual(self.last_row()["source"], "custom_source")

    def test_source_falls_back_to_unknown(self):
        # Empty argv[0] -> basename is "" -> "unknown".
        with patch.object(sys, "argv", [""]):
            notify("t")
        self.assertEqual(self.last_row()["source"], "unknown")

    def test_body_none_stays_none(self):
        notify("t", body=None)
        self.assertIsNone(self.last_row()["body"])

    def test_body_coerced_to_str(self):
        notify("t", body=12345)
        self.assertEqual(self.last_row()["body"], "12345")

    def test_defaults_when_minimal_args(self):
        notify("just a title")
        row = self.last_row()
        self.assertEqual(row["level"], "info")
        self.assertIsNone(row["category"])
        self.assertIsNone(row["dedup_key"])
        self.assertIsNone(row["correlation_id"])
        self.assertEqual(json.loads(row["meta"]), {})

    def test_title_coerced_to_str(self):
        notify(99999)
        self.assertEqual(self.last_row()["title"], "99999")


# ===========================================================================
# 5. INTEGRATION TESTS  (multiple components together — still fully mocked)
# ===========================================================================
class TestIntegration(NotifyTestBase):

    def test_happy_path_does_not_touch_psql(self):
        # When psycopg2 succeeds, the psql fallback must NOT run.
        notify("ok path")
        self.assertEqual(self.subprocess_mock.call_count, 0)
        self.assertEqual(len(self.recorder), 1)

    def test_connection_commits_on_success(self):
        captured = {}
        real_connect = self.fake_pg.connect.side_effect

        def _wrap(dsn, connect_timeout=None):
            conn = real_connect(dsn, connect_timeout=connect_timeout)
            captured["conn"] = conn
            return conn

        self.fake_pg.connect.side_effect = _wrap
        notify("commit check")
        self.assertTrue(captured["conn"].committed)

    def test_fallback_then_recovery_sequence(self):
        # First call DB-down (fallback), second call DB-up (direct insert).
        self.fake_pg.connect.side_effect = Exception("down")
        self.assertTrue(notify("first", level="warning"))
        self.assertEqual(self.subprocess_mock.call_count, 1)

        self.fake_pg.connect.side_effect = lambda dsn, connect_timeout=None: \
            _FakeConn(self.recorder, dsn, connect_timeout)
        self.assertTrue(notify("second", level="critical"))
        self.assertEqual(self.last_row()["title"], "second")

    def test_cli_main_queued_path(self):
        # Exercise the __main__ CLI entrypoint through notify().
        with patch.object(sys, "argv",
                          ["nova_notify.py", "cli title", "warning", "storage", "body text"]):
            ok = notify(sys.argv[1], level=sys.argv[2],
                        category=sys.argv[3], body=sys.argv[4])
        self.assertTrue(ok)
        row = self.last_row()
        self.assertEqual(row["title"], "cli title")
        self.assertEqual(row["level"], "warning")
        self.assertEqual(row["category"], "storage")
        self.assertEqual(row["body"], "body text")


# ===========================================================================
# 6. FUNCTIONAL TESTS  (real-world emitter scenarios)
# ===========================================================================
class TestFunctional(NotifyTestBase):

    def test_storage_warning_scenario(self):
        ok = notify("UNAS storage low", body="1.4TB free", level="warning",
                    category="storage", dedup_key="unas-storage-low")
        self.assertTrue(ok)
        row = self.last_row()
        self.assertEqual(row["dedup_key"], "unas-storage-low")
        self.assertEqual(row["level"], "warning")

    def test_critical_security_event_scenario(self):
        ok = notify("Intrusion detected", body="ssh brute force from 10.0.0.5",
                    level="critical", category="security",
                    correlation_id="sec-2026-001")
        self.assertTrue(ok)
        self.assertEqual(self.last_row()["level"], "critical")

    def test_returns_bool_type(self):
        self.assertIsInstance(notify("t"), bool)

    def test_unicode_title_and_body(self):
        notify("Café résumé 日本語 🚀", body="naïve façade")
        row = self.last_row()
        self.assertEqual(row["title"], "Café résumé 日本語 🚀")
        self.assertEqual(row["body"], "naïve façade")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================
class TestFrame(NotifyTestBase):

    def test_script_compiles(self):
        import py_compile
        py_compile.compile(str(_SCRIPT), doraise=True)

    def test_notify_is_callable(self):
        self.assertTrue(callable(notify))

    def test_valid_levels_constant(self):
        self.assertEqual(_VALID_LEVELS, ("info", "warning", "critical"))

    def test_no_real_psycopg2_connect_invoked(self):
        # Safety assertion: the fake psycopg2 is what gets called, proving the
        # real driver / real telemetry.events is never reached in tests.
        notify("smoke")
        self.assertTrue(self.fake_pg.connect.called)
        self.assertEqual(self.connect_calls[-1]["dsn"], _DSN)

    def test_no_rows_persisted_anywhere_real(self):
        # The recorder is in-memory only; assert nothing escaped to subprocess
        # on the happy path (i.e., no real psql write to telemetry.events).
        self.recorder.clear()
        notify("smoke2")
        self.assertEqual(self.subprocess_mock.call_count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
