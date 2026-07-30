"""
test_nova_purple_team.py — All 7 test categories for nova_purple_team.py
Written by Jordan Koch.

nova_purple_team.py is a detection-validation harness. It fires crafted RFC3164
syslog lines (over UDP) matching known attacker signatures at the Nova syslog
server, then queries telemetry.events to confirm the matching detection landed
inside a window, scoring caught/missed into a coverage scorecard.

HARD SAFETY (nothing here may touch the real world):
  * nova_notify is stubbed BEFORE load, so the scorecard can never page the bus.
  * nova_maintenance is stubbed, so no real maintenance window is ever opened.
  * psycopg2 is stubbed, so _conn() can never dial the real database — every DB
    test hands in a fake conn/cursor.
  * socket.socket is mocked in every fire test — NEVER a real UDP datagram.
  * time.sleep / time.time are patched so the poll loops resolve instantly.
"""

import ast
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub dependencies BEFORE loading the module
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_purple_team.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_notify_stub = MagicMock()
_notify_stub.notify = MagicMock(return_value=None)
sys.modules["nova_notify"] = _notify_stub

_maint_stub = MagicMock()
sys.modules["nova_maintenance"] = _maint_stub

_pg_stub = MagicMock(name="psycopg2")
_pg_stub.extras = MagicMock(name="psycopg2.extras")
sys.modules["psycopg2"] = _pg_stub
sys.modules["psycopg2.extras"] = _pg_stub.extras

_mod = load_script_compat(_SCRIPT, "nova_purple_team")
_SRC = _SCRIPT.read_text()

# Never write the real purple-team log; keep a recorder.
_LOGS = []
_mod.log = lambda msg: _LOGS.append(msg)

SIM_HOST = _mod.SIM_HOST
TESTNET_IP = _mod.TESTNET_IP
SYSLOG_PORT = _mod.SYSLOG_PORT

_FIRE_FNS = [
    _mod._fire_auth_brute_force,
    _mod._fire_sensitive_path,
    _mod._fire_suspicious_dns,
    _mod._fire_off_hours_auth,
]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeTS:
    """Stands in for a telemetry.events `ts` column (has .timestamp())."""

    def __init__(self, epoch):
        self._e = epoch

    def timestamp(self):
        return self._e


class FakeCursor:
    def __init__(self, rows, executed):
        self._rows = rows
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def close(self):
        pass


class FakeConn:
    """Hands out a cursor that pops from a shared row list. Empty list => None
    forever (models "no detection landed yet")."""

    def __init__(self, rows=None):
        self.rows = list(rows) if rows is not None else []
        self.executed = []
        self.closed = False

    def cursor(self, *a, **k):
        return FakeCursor(self.rows, self.executed)

    def close(self):
        self.closed = True


def _capture_fires():
    """Run every _fire_* with a mocked socket; return list of (bytes, addr)
    datagrams and the list of close() calls. NEVER touches the network."""
    sent, closes = [], []

    def factory(*a, **k):
        s = MagicMock()
        s.sendto.side_effect = lambda data, addr: sent.append((data, addr))
        s.close.side_effect = lambda: closes.append(True)
        return s

    with patch.object(_mod.socket, "socket", side_effect=factory), \
         patch.object(_mod.time, "sleep", lambda *_a, **_k: None):
        for fn in _FIRE_FNS:
            fn("127.0.0.1")
    return sent, closes


def _decoded_lines(sent):
    return [d.decode() for d, _ in sent]


def _time_seq(values):
    """A time.time() replacement that walks `values`, then repeats the last."""
    it = iter(values)
    last = [values[-1]]

    def f():
        try:
            return next(it)
        except StopIteration:
            return last[0]

    return f


def _tech(tid="t1", category="auth_failure", fire=None, window_s=90, **extra):
    d = {
        "id": tid,
        "attack": "T0000 Test",
        "desc": "test technique",
        "fire": fire if fire is not None else (lambda target: None),
        "category": category,
        "window_s": window_s,
    }
    d.update(extra)
    return d


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_fired_traffic_only_uses_synthetic_host(self):
        """Every fired datagram must stamp the synthetic SIM host — never a real one."""
        sent, _ = _capture_fires()
        self.assertTrue(sent, "no datagrams captured")
        for line in _decoded_lines(sent):
            self.assertIn(SIM_HOST, line, f"line missing synthetic host: {line!r}")

    def test_fired_traffic_only_uses_testnet_source_ip(self):
        """Any IPv4 that appears in a fired line must be the RFC5737 TEST-NET IP."""
        sent, _ = _capture_fires()
        ip_re = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        for line in _decoded_lines(sent):
            for ip in ip_re.findall(line):
                self.assertEqual(ip, TESTNET_IP,
                                 f"non-TEST-NET IP in fired traffic: {ip} ({line!r})")

    def test_no_real_host_or_private_ip_leaks(self):
        """No private/real-network markers may ride along in fired traffic."""
        sent, _ = _capture_fires()
        blob = "\n".join(_decoded_lines(sent))
        for bad in ("192.168.", "10.0.", "172.16.", "127.0.0.1", "digitalnoise", "kochj"):
            self.assertNotIn(bad, blob, f"real-world marker leaked into fire: {bad}")

    def test_testnet_ip_is_rfc5737_documentation_range(self):
        self.assertEqual(TESTNET_IP, "203.0.113.66")
        self.assertTrue(TESTNET_IP.startswith("203.0.113."),
                        "TEST-NET-3 documentation range only")

    def test_detected_since_is_scoped_to_the_sim_markers(self):
        """The detection query must be scoped to SIM_HOST/TESTNET_IP so it can
        never credit an unrelated, real detection."""
        conn = FakeConn(rows=[])
        _mod._detected_since(conn, "auth_failure", 1000.0)
        self.assertTrue(conn.executed, "no SQL executed")
        sql, params = conn.executed[-1]
        param_blob = " ".join(str(p) for p in params)
        self.assertIn(SIM_HOST, param_blob,
                      "query params must include the SIM host marker")
        self.assertIn(TESTNET_IP, param_blob,
                      "query params must include the TEST-NET IP marker")
        # category and epoch must also be bound as parameters (not interpolated).
        self.assertEqual(params[0], "auth_failure")
        self.assertEqual(params[1], 1000.0)

    def test_no_hardcoded_credentials(self):
        forbidden = ["sk-", "ghp_", "AKIA", "password =", "password=", "token ="]
        for pattern in forbidden:
            self.assertNotIn(pattern, _SRC, f"possible credential: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        self.assertNotIn(str(Path.home()) + "/", _SRC,
                         "use Path.home(), never a literal home path")

    def test_targets_loopback_by_default(self):
        self.assertEqual(_mod.DEFAULT_TARGET, "127.0.0.1")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_detection_poll_is_bounded_by_a_deadline(self):
        """run() must stop polling at t0 + window_s — no unbounded wait."""
        self.assertIn("t0 + t[\"window_s\"]", _SRC)
        self.assertIn("while time.time() < deadline", _SRC)

    def test_db_connect_has_a_bounded_timeout(self):
        self.assertIn("connect_timeout=5", _SRC)
        self.assertNotIn("psycopg2.connect(DSN)", _SRC)

    def test_catalog_lookup_is_trivial_linear_scan(self):
        """Technique selection is an O(n) comprehension over a small catalog."""
        self.assertIn('[t for t in CATALOG if t["id"] == a.technique]', _SRC)
        self.assertLessEqual(len(_mod.CATALOG), 100, "catalog must stay small")

    def test_poll_backoff_sleep_is_bounded(self):
        """The poll loop sleeps a fixed small interval, not zero (no busy-spin)."""
        self.assertIn("time.sleep(3)", _SRC)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def setUp(self):
        _maint_stub.reset_mock()
        _notify_stub.notify.reset_mock()

    def test_poll_retries_then_gives_up_cleanly_as_missed(self):
        """A cursor that never returns a row must MISS after the deadline, not hang."""
        conn = FakeConn(rows=[])  # fetchone -> None forever
        tech = _tech(tid="misser", category="auth_failure", window_s=90)
        # t0=1000; three in-window polls (retries) then jump past the 1090 deadline.
        times = _time_seq([1000, 1000, 1000, 1000, 9999])
        with patch.object(_mod, "_conn", lambda: conn), \
             patch.object(_mod.time, "time", side_effect=times), \
             patch.object(_mod.time, "sleep") as slept:
            results = _mod.run([tech], "127.0.0.1", use_maintenance=False)
        self.assertEqual(results[0]["outcome"], "MISSED")
        self.assertGreaterEqual(slept.call_count, 1, "must have retried the poll")

    def test_fire_exception_is_caught_and_recorded_as_error(self):
        def boom(target):
            raise RuntimeError("socket blew up")

        tech = _tech(tid="boomer", fire=boom)
        with patch.object(_mod, "_conn", lambda: FakeConn(rows=[])):
            results = _mod.run([tech], "127.0.0.1", use_maintenance=False)
        self.assertEqual(results[0]["outcome"], "error")
        self.assertIn("fire failed", results[0]["detail"])

    def test_notify_failure_in_scorecard_is_swallowed(self):
        _notify_stub.notify.side_effect = RuntimeError("bus down")
        try:
            tech = _tech(tid="ok", window_s=90)
            times = _time_seq([1000, 9999])
            with patch.object(_mod, "_conn", lambda: FakeConn(rows=[])), \
                 patch.object(_mod.time, "time", side_effect=times), \
                 patch.object(_mod.time, "sleep"):
                _mod.run([tech], "127.0.0.1", use_maintenance=False)  # must not raise
        finally:
            _notify_stub.notify.side_effect = None


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    RFC3164 = re.compile(r"^<\d+>.+ " + re.escape(SIM_HOST) + r" \S+\[\d+\]: .+")

    def _fire_lines(self, fn):
        sent, _ = self._capture_one(fn)
        return _decoded_lines(sent)

    def _capture_one(self, fn):
        sent, closes = [], []

        def factory(*a, **k):
            s = MagicMock()
            s.sendto.side_effect = lambda data, addr: sent.append((data, addr))
            s.close.side_effect = lambda: closes.append(True)
            return s

        with patch.object(_mod.socket, "socket", side_effect=factory), \
             patch.object(_mod.time, "sleep", lambda *_a, **_k: None):
            fn("127.0.0.1")
        return sent, closes

    def test_every_fire_line_is_valid_rfc3164(self):
        for line in _decoded_lines(_capture_fires()[0]):
            self.assertRegex(line, self.RFC3164, f"not RFC3164: {line!r}")

    def test_auth_brute_force_signature(self):
        lines = self._fire_lines(_mod._fire_auth_brute_force)
        self.assertGreaterEqual(len(lines), 5, "brute force must exceed the threshold")
        for line in lines:
            self.assertIn("Failed password", line)
            self.assertIn(f"from {TESTNET_IP}", line)

    def test_sensitive_path_signature(self):
        lines = self._fire_lines(_mod._fire_sensitive_path)
        blob = "\n".join(lines)
        self.assertIn("/etc/shadow", blob)
        self.assertIn("id_rsa", blob)
        self.assertIn("sudoers", blob)

    def test_suspicious_dns_signature(self):
        lines = self._fire_lines(_mod._fire_suspicious_dns)
        self.assertEqual(len(lines), 1)
        self.assertIn(".xyz", lines[0])
        self.assertIn("query", lines[0])

    def test_syslog_opens_and_closes_the_socket(self):
        sent, closes = self._capture_one(_mod._fire_suspicious_dns)
        self.assertEqual(len(sent), 1, "one datagram sent")
        _, addr = sent[0]
        self.assertEqual(addr, ("127.0.0.1", SYSLOG_PORT))
        self.assertEqual(len(closes), 1, "socket must be closed")

    def test_off_hours_gate_true_only_between_1_and_5(self):
        gate = next(t["time_gated"] for t in _mod.CATALOG if t["id"] == "off_hours_auth")
        for hour in range(24):
            with patch.object(_mod, "datetime") as dt:
                dt.now.return_value.hour = hour
                expected = 1 <= hour <= 5
                self.assertEqual(gate(), expected, f"hour {hour}")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def setUp(self):
        _maint_stub.reset_mock()
        _notify_stub.notify.reset_mock()

    def test_matching_event_is_scored_caught_with_latency(self):
        event = {"id": 4242, "ts": FakeTS(1002.0),
                 "title": "brute-force from 203.0.113.66", "meta": {}}
        conn = FakeConn(rows=[event])
        tech = _tech(tid="brute", category="auth_failure", window_s=90)
        with patch.object(_mod, "_conn", lambda: conn), \
             patch.object(_mod.time, "time", lambda: 1000.0), \
             patch.object(_mod.time, "sleep"):
            results = _mod.run([tech], "127.0.0.1", use_maintenance=False)
        self.assertEqual(results[0]["outcome"], "CAUGHT")
        self.assertEqual(results[0]["latency_s"], 2.0)
        self.assertTrue(conn.closed, "conn must be closed in finally")

    def test_maintenance_window_is_opened_and_closed(self):
        tech = _tech(tid="ok", window_s=90)
        times = _time_seq([1000, 9999])
        with patch.object(_mod, "_conn", lambda: FakeConn(rows=[])), \
             patch.object(_mod.time, "time", side_effect=times), \
             patch.object(_mod.time, "sleep"):
            _mod.run([tech], "127.0.0.1", use_maintenance=True)
        _maint_stub.start.assert_called_once()
        _maint_stub.stop.assert_called_once()

    def test_maintenance_window_is_closed_even_when_a_technique_errors(self):
        def boom(target):
            raise RuntimeError("kaboom")

        tech = _tech(tid="boomer", fire=boom)
        with patch.object(_mod, "_conn", lambda: FakeConn(rows=[])):
            results = _mod.run([tech], "127.0.0.1", use_maintenance=True)
        self.assertEqual(results[0]["outcome"], "error")
        _maint_stub.start.assert_called_once()
        _maint_stub.stop.assert_called_once()  # finally must still close it

    def test_no_maintenance_flag_skips_the_window(self):
        tech = _tech(tid="ok", window_s=90)
        times = _time_seq([1000, 9999])
        with patch.object(_mod, "_conn", lambda: FakeConn(rows=[])), \
             patch.object(_mod.time, "time", side_effect=times), \
             patch.object(_mod.time, "sleep"):
            _mod.run([tech], "127.0.0.1", use_maintenance=False)
        _maint_stub.start.assert_not_called()
        _maint_stub.stop.assert_not_called()


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def setUp(self):
        _maint_stub.reset_mock()
        _notify_stub.notify.reset_mock()

    def test_full_run_all_caught_scores_1of1_and_notifies_info(self):
        event = {"id": 1, "ts": FakeTS(1001.0), "title": "caught it", "meta": {}}
        conn = FakeConn(rows=[event])
        tech = _tech(tid="brute", category="auth_failure", window_s=90)
        with patch.object(_mod, "_conn", lambda: conn), \
             patch.object(_mod.time, "time", lambda: 1000.0), \
             patch.object(_mod.time, "sleep"):
            results = _mod.run([tech], "127.0.0.1", use_maintenance=False)
        caught = [r for r in results if r["outcome"] == "CAUGHT"]
        self.assertEqual(len(caught), 1)
        _notify_stub.notify.assert_called_once()
        self.assertEqual(_notify_stub.notify.call_args.kwargs["level"], "info")

    def test_full_run_miss_notifies_warning(self):
        """A miss is the actionable signal — it must page at 'warning'."""
        tech = _tech(tid="brute", category="auth_failure", window_s=90)
        times = _time_seq([1000, 9999])
        with patch.object(_mod, "_conn", lambda: FakeConn(rows=[])), \
             patch.object(_mod.time, "time", side_effect=times), \
             patch.object(_mod.time, "sleep"):
            results = _mod.run([tech], "127.0.0.1", use_maintenance=False)
        self.assertEqual(results[0]["outcome"], "MISSED")
        _notify_stub.notify.assert_called_once()
        self.assertEqual(_notify_stub.notify.call_args.kwargs["level"], "warning")

    def test_time_gated_technique_out_of_window_is_skipped_and_unscored(self):
        event = {"id": 9, "ts": FakeTS(1001.0), "title": "x", "meta": {}}
        conn = FakeConn(rows=[event])
        caught_tech = _tech(tid="brute", category="auth_failure", window_s=90)
        gated_tech = _tech(tid="offhours", category="off_hours_auth",
                           time_gated=lambda: False)
        with patch.object(_mod, "_conn", lambda: conn), \
             patch.object(_mod.time, "time", lambda: 1000.0), \
             patch.object(_mod.time, "sleep"):
            results = _mod.run([caught_tech, gated_tech], "127.0.0.1",
                               use_maintenance=False)
        gated = next(r for r in results if r["id"] == "offhours")
        self.assertEqual(gated["outcome"], "skipped")
        scored = [r for r in results if r["outcome"] in ("CAUGHT", "MISSED")]
        self.assertEqual(len(scored), 1, "skipped technique excluded from denominator")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_purple_team.py has syntax errors: {e}")

    def test_shebang_and_docstring(self):
        self.assertTrue(_SRC.startswith("#!/usr/bin/env python3"))
        doc = ast.get_docstring(ast.parse(_SRC))
        self.assertIn("nova_purple_team.py", doc)

    def test_catalog_is_nonempty_list_of_well_formed_dicts(self):
        self.assertIsInstance(_mod.CATALOG, list)
        self.assertGreater(len(_mod.CATALOG), 0)
        for t in _mod.CATALOG:
            self.assertIsInstance(t, dict)
            for key in ("id", "attack", "fire", "category", "window_s"):
                self.assertIn(key, t, f"technique missing {key!r}: {t.get('id')}")
            self.assertTrue(callable(t["fire"]))

    def test_every_fire_function_is_callable(self):
        for fn in _FIRE_FNS:
            self.assertTrue(callable(fn))

    def test_main_list_prints_catalog_and_returns_zero(self):
        import contextlib
        import io
        buf = io.StringIO()
        with patch.object(sys, "argv", ["nova_purple_team.py", "--list"]), \
             contextlib.redirect_stdout(buf):
            rc = _mod.main()
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("catalog", out.lower())
        for t in _mod.CATALOG:
            self.assertIn(t["id"], out)

    def test_main_does_not_run_on_import(self):
        self.assertIn('if __name__ == "__main__":', _SRC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
