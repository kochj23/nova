"""
test_nova_prober_dedup.py — All 7 test categories for the 2026-07-29 change to
nova_prober.py: FAIL and RECOVERED must use DISTINCT dedup keys.
Written by Jordan Koch.

REGRESSION BEING PINNED: both alerts used to share dedup_key=f"probe-{name}".
A flapping probe's "RECOVERED" post consumed the dedup slot, and the very next
genuine "FAIL" was silently suppressed by nova_notifier — observed live on
inference_vantage, 2026-07-29. The keys must now be probe-<name>-fail and
probe-<name>-recovered, and FAILs carry dedup_window_s=21600 (6h) so a flapper
pages at most 4x/day.

Scope note: tests/test_nova_prober.py is the older broad suite. This file is the
hermetic regression companion for the dedup-key contract.

HARD SAFETY: nova_notify is stubbed BEFORE load so the real notification bus can
never be reached; every DB connection is a fake; no probe function here does any
I/O. Probe stubs sleep ~10ms so nova_witness.check_grain (5ms minimum-grain
floor) does not downgrade an intentional pass into a counterfeit failure.
"""

import ast
import re
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub the notification bus BEFORE load — nova_prober does
# `from nova_notify import notify`, so the bound name must already be a stub.
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_prober.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_notify_stub = MagicMock()
_notify_stub.notify = MagicMock(return_value=None)
sys.modules["nova_notify"] = _notify_stub

_mod = load_script_compat(_SCRIPT, "nova_prober")
_SRC = _SCRIPT.read_text()

run_probe = _mod.run_probe
_last_ok = _mod._last_ok
_record = _mod._record


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, fetchone_queue, executed):
        self._one = fetchone_queue
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._one.pop(0) if self._one else None


class FakeConn:
    """fetchone_queue is shared across cursors so _last_ok then _record work."""

    def __init__(self, fetchone_queue=None):
        self._one = list(fetchone_queue or [])
        self.executed = []
        self.commits = 0
        self.closed = False

    def cursor(self, *a, **k):
        return FakeCursor(self._one, self.executed)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _spec(fn, name="inference_vantage", level="critical", category="probe",
          host="192.168.1.6"):
    return {"name": name, "fn": fn, "level_on_fail": level,
            "category": category, "host": host}


def _passing():
    """A probe stub that passes with evidence and survives the grain floor."""
    time.sleep(0.01)
    return True, "200 OK, 4096 bytes of evidence"


def _failing():
    time.sleep(0.01)
    return False, "connection refused"


def _prev(value):
    """fetchone queue for _last_ok: None (no history) / (True,) / (False,)."""
    return [None] if value is None else [(value,)]


class _ProbeCase(unittest.TestCase):
    def setUp(self):
        _notify_stub.notify.reset_mock()
        _notify_stub.notify.side_effect = None

    def notify_kwargs(self):
        return _notify_stub.notify.call_args.kwargs

    def notify_title(self):
        return _notify_stub.notify.call_args.args[0]


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        for pat in (r"xox[baprs]-\d{5,}", r"\bsk-[A-Za-z0-9]{20,}",
                    r"\bghp_[A-Za-z0-9]{20,}", r"\bAKIA[0-9A-Z]{16}\b",
                    r"(?i)password\s*=\s*['\"][^'\"]{4,}"):
            self.assertIsNone(re.search(pat, _SRC),
                              f"possible hardcoded credential matching {pat!r}")

    def test_no_hardcoded_home_path(self):
        self.assertNotIn(str(Path.home()) + "/", _SRC)

    def test_dsns_carry_no_password(self):
        for dsn in (_mod.OPS_DSN, _mod.MEM_DSN):
            self.assertNotIn("password", dsn)

    def test_no_shell_execution(self):
        for bad in ("shell=True", "os.system", "subprocess.call"):
            self.assertNotIn(bad, _SRC)

    def test_probe_detail_is_truncated_before_storage(self):
        """A probe's detail is attacker-adjacent text (remote bodies) — cap it."""
        self.assertIn("[:2000]", _SRC)

    def test_alert_meta_carries_no_secrets(self):
        """meta is only probe/host/latency/detail — never credentials."""
        fn = next(n for n in ast.walk(ast.parse(_SRC))
                  if isinstance(n, ast.FunctionDef) and n.name == "run_probe")
        src = ast.get_source_segment(_SRC, fn)
        self.assertIn('meta = {"probe": name, "host": spec.get("host"),', src)
        self.assertNotIn("api_key", src)
        self.assertNotIn("token", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(_ProbeCase):

    def test_fail_carries_a_six_hour_dedup_window(self):
        conn = FakeConn(_prev(True))
        run_probe(conn, _spec(_failing), quiet=True)
        self.assertEqual(self.notify_kwargs()["meta"]["dedup_window_s"], 21600,
                         "a flapper must page at most 4x/day")

    def test_six_hour_window_is_exactly_four_pages_per_day(self):
        conn = FakeConn(_prev(True))
        run_probe(conn, _spec(_failing), quiet=True)
        window = self.notify_kwargs()["meta"]["dedup_window_s"]
        self.assertEqual(86400 // window, 4)

    def test_recovered_keeps_the_default_window(self):
        conn = FakeConn(_prev(False))
        run_probe(conn, _spec(_passing), quiet=True)
        self.assertNotIn("dedup_window_s", self.notify_kwargs()["meta"],
                         "recoveries must not widen their own window")

    def test_history_lookup_is_a_single_row(self):
        self.assertIn("ORDER BY ts DESC LIMIT 1", _SRC)

    def test_http_probe_timeout_is_short_and_bounded(self):
        self.assertIsInstance(_mod.HTTP_TIMEOUT, int)
        self.assertGreater(_mod.HTTP_TIMEOUT, 0)
        self.assertLessEqual(_mod.HTTP_TIMEOUT, 30, "a hang IS a failure")

    def test_steady_state_costs_zero_notifications(self):
        for prev, fn in ((True, _passing), (False, _failing)):
            _notify_stub.notify.reset_mock()
            run_probe(FakeConn(_prev(prev)), _spec(fn), quiet=True)
            _notify_stub.notify.assert_not_called()


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(_ProbeCase):

    def test_probe_exception_becomes_a_failure_not_a_crash(self):
        def boom():
            raise RuntimeError("socket exploded")
        conn = FakeConn(_prev(True))
        ok = run_probe(conn, _spec(boom), quiet=True)
        self.assertFalse(ok)
        self.assertIn("probe raised RuntimeError", self.notify_kwargs()["body"])
        self.assertEqual(self.notify_kwargs()["dedup_key"],
                         "probe-inference_vantage-fail")

    def test_history_write_failure_does_not_mask_the_result(self):
        conn = FakeConn(_prev(False))

        def bad_record(*a, **k):
            raise RuntimeError("probe_results unavailable")
        with patch.object(_mod, "_record", bad_record):
            ok = run_probe(conn, _spec(_passing), quiet=True)
        self.assertTrue(ok)
        self.assertEqual(self.notify_kwargs()["dedup_key"],
                         "probe-inference_vantage-recovered")

    def test_no_history_is_treated_as_no_previous_state(self):
        conn = FakeConn(_prev(None))
        self.assertIsNone(_last_ok(conn, "whatever"))

    def test_recovery_detection_survives_a_restart(self):
        """State comes from telemetry.probe_results, not process memory."""
        self.assertIn("_last_ok(conn, name)", _SRC)
        self.assertNotIn("global ", _SRC)

    def test_grain_latch_import_failure_is_tolerated(self):
        """nova_witness missing must not break the sweep."""
        conn = FakeConn(_prev(True))
        with patch.dict(sys.modules, {"nova_witness": None}):
            ok = run_probe(conn, _spec(_passing), quiet=True)
        self.assertTrue(ok)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(_ProbeCase):

    def test_last_ok_reads_true_false_none(self):
        self.assertIs(_last_ok(FakeConn([(True,)]), "p"), True)
        self.assertIs(_last_ok(FakeConn([(False,)]), "p"), False)
        self.assertIsNone(_last_ok(FakeConn([None]), "p"))

    def test_fail_dedup_key_shape(self):
        conn = FakeConn(_prev(True))
        run_probe(conn, _spec(_failing, name="postgres"), quiet=True)
        self.assertEqual(self.notify_kwargs()["dedup_key"], "probe-postgres-fail")

    def test_recovered_dedup_key_shape(self):
        conn = FakeConn(_prev(False))
        run_probe(conn, _spec(_passing, name="postgres"), quiet=True)
        self.assertEqual(self.notify_kwargs()["dedup_key"],
                         "probe-postgres-recovered")

    def test_fail_and_recovered_keys_are_distinct(self):
        """THE regression: a shared key let RECOVERED suppress the next FAIL."""
        run_probe(FakeConn(_prev(True)), _spec(_failing, name="x"), quiet=True)
        fail_key = self.notify_kwargs()["dedup_key"]
        _notify_stub.notify.reset_mock()
        run_probe(FakeConn(_prev(False)), _spec(_passing, name="x"), quiet=True)
        recovered_key = self.notify_kwargs()["dedup_key"]
        self.assertNotEqual(fail_key, recovered_key)
        self.assertEqual({fail_key, recovered_key}, {"probe-x-fail", "probe-x-recovered"})

    def test_no_alert_on_steady_state_success(self):
        run_probe(FakeConn(_prev(True)), _spec(_passing), quiet=True)
        _notify_stub.notify.assert_not_called()

    def test_no_alert_on_steady_state_failure(self):
        run_probe(FakeConn(_prev(False)), _spec(_failing), quiet=True)
        _notify_stub.notify.assert_not_called()

    def test_first_run_passing_is_silent(self):
        run_probe(FakeConn(_prev(None)), _spec(_passing), quiet=True)
        _notify_stub.notify.assert_not_called()

    def test_first_run_already_broken_alerts(self):
        run_probe(FakeConn(_prev(None)), _spec(_failing), quiet=True)
        self.assertEqual(self.notify_kwargs()["dedup_key"],
                         "probe-inference_vantage-fail")

    def test_fail_uses_the_probes_configured_level(self):
        run_probe(FakeConn(_prev(True)), _spec(_failing, level="warning"), quiet=True)
        self.assertEqual(self.notify_kwargs()["level"], "warning")

    def test_recovered_is_always_info(self):
        run_probe(FakeConn(_prev(False)), _spec(_passing, level="critical"), quiet=True)
        self.assertEqual(self.notify_kwargs()["level"], "info")

    def test_titles_name_the_host_for_at_a_glance_triage(self):
        run_probe(FakeConn(_prev(True)), _spec(_failing, name="embedding",
                                               host="127.0.0.1"), quiet=True)
        self.assertEqual(self.notify_title(), "PROBE FAIL: embedding @ 127.0.0.1")
        _notify_stub.notify.reset_mock()
        run_probe(FakeConn(_prev(False)), _spec(_passing, name="embedding",
                                                host="127.0.0.1"), quiet=True)
        self.assertEqual(self.notify_title(),
                         "PROBE RECOVERED: embedding @ 127.0.0.1")

    def test_hostless_probe_title_has_no_dangling_at(self):
        run_probe(FakeConn(_prev(True)), _spec(_failing, name="p", host=None),
                  quiet=True)
        self.assertEqual(self.notify_title(), "PROBE FAIL: p")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(_ProbeCase):

    def test_result_row_is_recorded_with_latency(self):
        conn = FakeConn(_prev(True))
        run_probe(conn, _spec(_passing, name="postgres"), quiet=True)
        inserts = [(s, p) for s, p in conn.executed
                   if "INSERT INTO telemetry.probe_results" in s]
        self.assertEqual(len(inserts), 1)
        probe, ok, latency_ms, detail = inserts[0][1]
        self.assertEqual(probe, "postgres")
        self.assertTrue(ok)
        self.assertGreaterEqual(latency_ms, 5)
        self.assertIn("evidence", detail)
        self.assertEqual(conn.commits, 1)

    def test_failure_row_is_recorded_before_alerting(self):
        conn = FakeConn(_prev(True))
        run_probe(conn, _spec(_failing), quiet=True)
        self.assertTrue(any("INSERT INTO telemetry.probe_results" in s
                            for s, p in conn.executed))
        _notify_stub.notify.assert_called_once()

    def test_alert_meta_carries_probe_host_and_latency(self):
        conn = FakeConn(_prev(True))
        run_probe(conn, _spec(_failing, name="cloudflared_tunnel", host="Office-M4-2"),
                  quiet=True)
        meta = self.notify_kwargs()["meta"]
        self.assertEqual(meta["probe"], "cloudflared_tunnel")
        self.assertEqual(meta["host"], "Office-M4-2")
        self.assertIsInstance(meta["latency_ms"], int)
        self.assertIn("refused", meta["detail"])

    def test_alert_is_sourced_to_the_prober(self):
        run_probe(FakeConn(_prev(True)), _spec(_failing), quiet=True)
        self.assertEqual(self.notify_kwargs()["source"], "nova_prober.py")
        self.assertEqual(self.notify_kwargs()["category"], "probe")

    def test_counterfeit_pass_is_downgraded_to_a_failure(self):
        """Minimum grain: an instant, evidence-free 'pass' is not a pass."""
        conn = FakeConn(_prev(True))
        ok = run_probe(conn, _spec(lambda: (True, "")), quiet=True)
        self.assertFalse(ok)
        self.assertIn("COUNTERFEIT", self.notify_kwargs()["body"])
        self.assertEqual(self.notify_kwargs()["dedup_key"],
                         "probe-inference_vantage-fail")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(_ProbeCase):

    def test_flap_cycle_never_reuses_the_recovered_key_for_a_fail(self):
        """fail -> recovered -> fail: the second FAIL must be able to page.

        With the old shared key, the RECOVERED post occupied the dedup slot and
        nova_notifier swallowed this second FAIL entirely.
        """
        keys = []
        for prev, fn in ((True, _failing), (False, _passing), (True, _failing)):
            _notify_stub.notify.reset_mock()
            run_probe(FakeConn(_prev(prev)), _spec(fn, name="inference_vantage"),
                      quiet=True)
            keys.append(_notify_stub.notify.call_args.kwargs["dedup_key"])
        self.assertEqual(keys, ["probe-inference_vantage-fail",
                                "probe-inference_vantage-recovered",
                                "probe-inference_vantage-fail"])
        self.assertNotEqual(keys[1], keys[2],
                            "a recovery must never occupy the FAIL dedup slot")

    def test_sweep_all_pass_is_silent_and_returns_true(self):
        conn = FakeConn([(True,)] * 4)
        with patch.object(_mod, "_ops_conn", lambda: conn), \
             patch.object(_mod, "PROBES", [_spec(_passing, name="a"),
                                           _spec(_passing, name="b")]):
            self.assertTrue(_mod.sweep(quiet=True))
        _notify_stub.notify.assert_not_called()
        self.assertTrue(conn.closed)

    def test_sweep_one_fail_returns_false_and_alerts_once(self):
        conn = FakeConn([(True,), (True,)])
        with patch.object(_mod, "_ops_conn", lambda: conn), \
             patch.object(_mod, "PROBES", [_spec(_passing, name="a"),
                                           _spec(_failing, name="b")]):
            self.assertFalse(_mod.sweep(quiet=True))
        _notify_stub.notify.assert_called_once()
        self.assertEqual(_notify_stub.notify.call_args.kwargs["dedup_key"],
                         "probe-b-fail")

    def test_sweep_closes_the_connection_even_when_a_probe_explodes(self):
        conn = FakeConn([(True,)])

        def boom():
            raise RuntimeError("kaboom")
        with patch.object(_mod, "_ops_conn", lambda: conn), \
             patch.object(_mod, "PROBES", [_spec(boom, name="a")]):
            self.assertFalse(_mod.sweep(quiet=True))
        self.assertTrue(conn.closed)

    def test_notify_failure_does_not_abort_the_rest_of_the_sweep(self):
        """REGRESSION (bug found + fixed 2026-07-29): a notification-bus outage
        used to propagate out of run_probe and abort the sweep, so every probe
        after the failing one never ran or recorded — one bad emit hid the whole
        health picture. notify() failures are now caught and logged."""
        conn = FakeConn([(True,), (True,)])
        _notify_stub.notify.side_effect = RuntimeError("bus down")
        try:
            with patch.object(_mod, "_ops_conn", lambda: conn), \
                 patch.object(_mod, "PROBES", [_spec(_failing, name="a"),
                                               _spec(_passing, name="b")]):
                result = _mod.sweep(quiet=True)   # must NOT raise
        finally:
            _notify_stub.notify.side_effect = None
        # Probe 'a' failed, so the sweep is False -- but probe 'b' still ran.
        self.assertFalse(result)
        self.assertGreaterEqual(_notify_stub.notify.call_count, 1)
        self.assertTrue(conn.closed)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_prober.py has syntax errors: {e}")

    def test_public_callables_present(self):
        for fn in ("run_probe", "sweep", "main", "_last_ok", "_record", "_ops_conn"):
            self.assertTrue(callable(getattr(_mod, fn, None)), f"missing: {fn}")

    def test_every_probe_spec_is_well_formed(self):
        for spec in _mod.PROBES:
            self.assertTrue(callable(spec["fn"]), spec["name"])
            self.assertIn(spec["level_on_fail"], ("warning", "critical"))
            self.assertTrue(spec["category"])

    def test_probe_names_are_unique(self):
        names = [s["name"] for s in _mod.PROBES]
        self.assertEqual(len(names), len(set(names)),
                         "duplicate probe names would collide in the dedup keys")

    def test_dedup_key_regression_is_documented_in_source(self):
        self.assertIn("DISTINCT dedup keys", _SRC)
        self.assertIn("probe-{name}-fail", _SRC)
        self.assertIn("probe-{name}-recovered", _SRC)

    def test_entrypoint_guarded(self):
        self.assertIn('if __name__ == "__main__":', _SRC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
