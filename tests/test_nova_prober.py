"""
test_nova_prober.py — All 7 test categories for nova_prober.py

Covers each probe's ok/fail logic with HTTP/DB/Ollama MOCKED, the state-change
alerting contract (fail->recovery emits, steady-state silent), and probe_results
row recording.

HARD SAFETY: nova_notify.notify writes to telemetry.events which the live
nova_notifier daemon POSTs to Slack. EVERY test here replaces the module-level
`notify` (already bound via `from nova_notify import notify`) with a recorder.
No test ever enqueues a real event, opens a real socket, hits Ollama, or runs a
real DB query — connections are faked and SELECT/INSERT are captured in memory.

Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Load the module under test, stubbing module-level deps first ─────────────
# nova_prober does `from nova_notify import notify` at import time. We must make
# that import resolve to a stub whose `notify` is a no-op recorder, so even the
# already-bound name in the module can never reach the real notification bus.
_REAL_NOVA_NOTIFY = sys.modules.get("nova_notify")
_notify_stub = MagicMock()
_notify_stub.notify = MagicMock(return_value=None)
sys.modules["nova_notify"] = _notify_stub

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_prober.py"
_spec = importlib.util.spec_from_file_location("nova_prober", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ── Fakes ────────────────────────────────────────────────────────────────────
class FakeHTTPResponse:
    """Mimics the urllib response context manager: .getcode()/.read()."""

    def __init__(self, status=200, body=b""):
        self._status = status
        self._body = body if isinstance(body, bytes) else body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return self._status

    def read(self):
        return self._body


class FakeCursor:
    """Captures executes; returns scripted fetch results.

    fetch_queue: list of rows (each a tuple) handed out per fetchone() call.
    """

    def __init__(self, fetch_queue=None, executed=None):
        self._fetch_queue = list(fetch_queue or [])
        self.executed = executed if executed is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        if self._fetch_queue:
            return self._fetch_queue.pop(0)
        return None


class FakeConn:
    """Fake psycopg2 connection. Records committed=True on commit().

    cursors are created lazily; `fetch_map` lets a test script different fetch
    results for different cursor() calls in order.
    """

    def __init__(self, fetch_queues=None):
        # fetch_queues: list of per-cursor fetch row lists, consumed in order.
        self._fetch_queues = list(fetch_queues or [])
        self.executed = []
        self.committed = False
        self.closed = False
        self.cursors = []

    def cursor(self, *a, **kw):
        fq = self._fetch_queues.pop(0) if self._fetch_queues else None
        cur = FakeCursor(fetch_queue=fq, executed=self.executed)
        self.cursors.append(cur)
        return cur

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================
class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "xoxb-", "xoxp-"]:
            self.assertNotIn(pat, src, f"Credential-like token found: {pat!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_password_in_dsn(self):
        """DSNs must rely on peer/trust auth — no embedded password=."""
        src = _SCRIPT.read_text()
        self.assertNotIn("password=", src.lower())

    def test_detail_is_truncated_before_insert(self):
        """_record must cap detail at 2000 chars (defense against log bombs)."""
        conn = FakeConn()
        huge = "x" * 5000
        _mod._record(conn, "p", True, 10, huge)
        # find the INSERT execute
        inserts = [e for e in conn.executed if "INSERT" in e[0]]
        self.assertEqual(len(inserts), 1)
        params = inserts[0][1]
        self.assertLessEqual(len(params[3]), 2000)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================
class TestPerformance(unittest.TestCase):

    def test_http_timeout_is_short(self):
        """A hang IS a failure — HTTP timeout must be small."""
        self.assertLessEqual(_mod.HTTP_TIMEOUT, 30)

    def test_pg_connect_timeout_is_bounded(self):
        self.assertLessEqual(_mod.PG_CONNECT_TIMEOUT, 30)

    def test_embedding_only_checks_prefix_for_numeric(self):
        """probe_embedding validates only the first few floats (cheap), not all 768."""
        vec = [0.1] * 768
        with patch.object(_mod.urllib.request, "urlopen",
                          return_value=FakeHTTPResponse(200, json.dumps({"embedding": vec}))):
            ok, detail = _mod.probe_embedding()
        self.assertTrue(ok)


# ===========================================================================
# 3. RETRY / RESILIENCE TESTS  (a probe must never raise)
# ===========================================================================
class TestRetry(unittest.TestCase):

    def setUp(self):
        _notify_stub.notify.reset_mock()

    def test_probe_that_raises_is_recorded_as_fail_not_crash(self):
        """run_probe must catch a probe exception and record ok=False."""
        conn = FakeConn(fetch_queues=[[(None,)]])  # _last_ok -> None (first run)

        def boom():
            raise RuntimeError("kaboom")

        spec = {"name": "boomy", "fn": boom, "level_on_fail": "warning",
                "category": "probe", "host": "h"}
        ok = _mod.run_probe(conn, spec, quiet=True)
        self.assertFalse(ok)
        inserts = [e for e in conn.executed if "INSERT" in e[0]]
        self.assertEqual(len(inserts), 1)
        # ok column is False, detail mentions the exception
        params = inserts[0][1]
        self.assertFalse(params[1])
        self.assertIn("kaboom", params[3])

    def test_record_failure_does_not_mask_probe_result(self):
        """If _record blows up, run_probe still returns the probe's real ok."""
        conn = FakeConn(fetch_queues=[[(None,)]])
        spec = {"name": "p", "fn": lambda: (True, "good"),
                "level_on_fail": "warning", "category": "probe", "host": "h"}
        with patch.object(_mod, "_record", side_effect=Exception("db down")):
            ok = _mod.run_probe(conn, spec, quiet=True)
        self.assertTrue(ok)

    def test_http_transport_error_is_failure_not_exception(self):
        """A connection refused surfaces as ok=False, not a raised exception."""
        with patch.object(_mod, "_http_get", side_effect=OSError("conn refused")):
            ok, detail = _mod.probe_http()
        self.assertFalse(ok)
        self.assertIn("OSError", detail)


# ===========================================================================
# 4. UNIT TESTS — each probe's ok/fail logic with everything mocked
# ===========================================================================
class TestProbeHTTP(unittest.TestCase):

    def _run_with_responses(self, responses):
        """responses: dict url -> (status, body) OR Exception to raise."""
        def fake_get(url, timeout=_mod.HTTP_TIMEOUT):
            r = responses[url]
            if isinstance(r, Exception):
                raise r
            return r
        with patch.object(_mod, "_http_get", side_effect=fake_get):
            return _mod.probe_http()

    def test_all_200_with_content_is_ok(self):
        responses = {url: (200, f"prefix {needle} suffix")
                     for url, needle in _mod.HTTP_CHECKS}
        ok, detail = self._run_with_responses(responses)
        self.assertTrue(ok)
        self.assertIn("endpoints 200", detail)

    def test_200_with_wrong_content_is_fail(self):
        """200 serving the wrong/blank page must FAIL (the anti-9-days-down rule)."""
        responses = {url: (200, "TOTALLY UNRELATED BODY")
                     for url, needle in _mod.HTTP_CHECKS}
        ok, detail = self._run_with_responses(responses)
        self.assertFalse(ok)
        self.assertIn("missing", detail)

    def test_non_200_status_is_fail(self):
        responses = {url: (503, f"{needle}") for url, needle in _mod.HTTP_CHECKS}
        ok, detail = self._run_with_responses(responses)
        self.assertFalse(ok)
        self.assertIn("HTTP 503", detail)

    def test_httperror_is_fail_with_code(self):
        responses = {}
        for i, (url, needle) in enumerate(_mod.HTTP_CHECKS):
            if i == 0:
                responses[url] = urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            else:
                responses[url] = (200, needle)
        ok, detail = self._run_with_responses(responses)
        self.assertFalse(ok)
        self.assertIn("HTTP 404", detail)

    def test_partial_failure_still_fails_overall(self):
        responses = {}
        for i, (url, needle) in enumerate(_mod.HTTP_CHECKS):
            responses[url] = (200, needle) if i != 1 else (200, "wrong")
        ok, detail = self._run_with_responses(responses)
        self.assertFalse(ok)


class TestProbeEmbedding(unittest.TestCase):

    def _run(self, payload, raise_exc=None):
        if raise_exc is not None:
            cm = patch.object(_mod.urllib.request, "urlopen", side_effect=raise_exc)
        else:
            cm = patch.object(_mod.urllib.request, "urlopen",
                              return_value=FakeHTTPResponse(200, json.dumps(payload)))
        with cm:
            return _mod.probe_embedding()

    def test_correct_768_vector_is_ok(self):
        ok, detail = self._run({"embedding": [0.0] * 768})
        self.assertTrue(ok)
        self.assertIn("768", detail)

    def test_wrong_dim_is_fail(self):
        ok, detail = self._run({"embedding": [0.0] * 512})
        self.assertFalse(ok)
        self.assertIn("768", detail)
        self.assertIn("512", detail)

    def test_missing_embedding_is_fail(self):
        ok, detail = self._run({"error": "model not found"})
        self.assertFalse(ok)
        self.assertIn("no embedding", detail)

    def test_non_numeric_values_is_fail(self):
        bad = ["a", "b", "c", "d", "e", "f", "g", "h"] + [0.0] * 760
        ok, detail = self._run({"embedding": bad})
        self.assertFalse(ok)
        self.assertIn("non-numeric", detail)

    def test_transport_error_is_fail(self):
        ok, detail = self._run(None, raise_exc=OSError("ollama down"))
        self.assertFalse(ok)
        self.assertIn("embeddings call failed", detail)


class TestProbePostgres(unittest.TestCase):

    def test_both_select1_ok(self):
        # connect() called twice; each returns a conn whose cursor fetches (1,)
        conns = [FakeConn(fetch_queues=[[(1,)]]), FakeConn(fetch_queues=[[(1,)]])]
        with patch.object(_mod.psycopg2, "connect", side_effect=conns):
            ok, detail = _mod.probe_postgres()
        self.assertTrue(ok)
        self.assertIn("reachable", detail)

    def test_unexpected_select_result_is_fail(self):
        conns = [FakeConn(fetch_queues=[[(0,)]]), FakeConn(fetch_queues=[[(1,)]])]
        with patch.object(_mod.psycopg2, "connect", side_effect=conns):
            ok, detail = _mod.probe_postgres()
        self.assertFalse(ok)
        self.assertIn("unexpected", detail)

    def test_connect_failure_is_fail(self):
        def connect(dsn, **kw):
            if "nova_ops" in dsn:
                raise OSError("could not connect to ops")
            return FakeConn(fetch_queues=[[(1,)]])
        with patch.object(_mod.psycopg2, "connect", side_effect=connect):
            ok, detail = _mod.probe_postgres()
        self.assertFalse(ok)
        self.assertIn("nova_ops", detail)


class TestProbeMemoryRoundtrip(unittest.TestCase):

    def test_happy_roundtrip(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            # the POST to /remember
            captured["data"] = req.data
            return FakeHTTPResponse(200, json.dumps({"id": 42, "status": "stored"}))

        # readback returns a row whose text contains the token; the token is the
        # uuid embedded in the POSTed text, so we echo it back.
        def fake_mem_conn():
            data = json.loads(captured["data"])
            text = data["text"]
            return FakeConn(fetch_queues=[[(42, text)], []])

        with patch.object(_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            with patch.object(_mod, "_mem_conn", side_effect=fake_mem_conn):
                ok, detail = _mod.probe_memory_roundtrip()
        self.assertTrue(ok)
        self.assertIn("roundtrip ok", detail)

    def test_remember_post_failure_is_fail(self):
        with patch.object(_mod.urllib.request, "urlopen", side_effect=OSError("api down")):
            ok, detail = _mod.probe_memory_roundtrip()
        self.assertFalse(ok)
        self.assertIn("remember POST failed", detail)

    def test_remember_rejected_response_is_fail(self):
        def fake_urlopen(req, timeout=None):
            return FakeHTTPResponse(200, json.dumps({"status": "too_short"}))
        with patch.object(_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            ok, detail = _mod.probe_memory_roundtrip()
        self.assertFalse(ok)
        self.assertIn("rejected", detail)

    def test_readback_missing_row_is_fail_and_cleans_up(self):
        cleanup_conns = []

        def fake_urlopen(req, timeout=None):
            return FakeHTTPResponse(200, json.dumps({"id": 7, "status": "stored"}))

        def fake_mem_conn():
            c = FakeConn(fetch_queues=[[None], []])  # readback finds nothing
            cleanup_conns.append(c)
            return c

        with patch.object(_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            with patch.object(_mod, "_mem_conn", side_effect=fake_mem_conn):
                ok, detail = _mod.probe_memory_roundtrip()
        self.assertFalse(ok)
        self.assertIn("could not read it back", detail)
        # cleanup DELETE must still have run
        deletes = [e for c in cleanup_conns for e in c.executed if "DELETE" in e[0]]
        self.assertGreaterEqual(len(deletes), 1)


class TestRecordAndLastOk(unittest.TestCase):

    def test_last_ok_none_when_no_history(self):
        conn = FakeConn(fetch_queues=[[None]])
        self.assertIsNone(_mod._last_ok(conn, "p"))

    def test_last_ok_true(self):
        conn = FakeConn(fetch_queues=[[(True,)]])
        self.assertTrue(_mod._last_ok(conn, "p"))

    def test_last_ok_false(self):
        conn = FakeConn(fetch_queues=[[(False,)]])
        self.assertFalse(_mod._last_ok(conn, "p"))

    def test_record_inserts_and_commits(self):
        conn = FakeConn()
        _mod._record(conn, "myprobe", True, 123, "all good")
        inserts = [e for e in conn.executed if "INSERT" in e[0]]
        self.assertEqual(len(inserts), 1)
        params = inserts[0][1]
        self.assertEqual(params[0], "myprobe")
        self.assertTrue(params[1])
        self.assertEqual(params[2], 123)
        self.assertTrue(conn.committed)


# ===========================================================================
# 5. INTEGRATION TESTS — state-change alerting via run_probe (notify mocked)
# ===========================================================================
class TestStateChangeAlerting(unittest.TestCase):

    def setUp(self):
        _notify_stub.notify.reset_mock()

    def _spec(self, fn, level="critical"):
        return {"name": "p1", "fn": fn, "level_on_fail": level,
                "category": "probe", "host": "h"}

    def test_steady_success_is_silent(self):
        """prev=True, now ok=True -> NO notify."""
        conn = FakeConn(fetch_queues=[[(True,)]])  # _last_ok -> True
        _mod.run_probe(conn, self._spec(lambda: (True, "ok")), quiet=True)
        _notify_stub.notify.assert_not_called()

    def test_steady_failure_is_silent(self):
        """prev=False, now ok=False -> NO notify (already alerted; dedup territory)."""
        conn = FakeConn(fetch_queues=[[(False,)]])  # _last_ok -> False
        _mod.run_probe(conn, self._spec(lambda: (False, "still down")), quiet=True)
        _notify_stub.notify.assert_not_called()

    def test_success_to_failure_emits_fail_alert(self):
        """prev=True -> now fail -> emits PROBE FAIL at the probe's fail level."""
        conn = FakeConn(fetch_queues=[[(True,)]])
        _mod.run_probe(conn, self._spec(lambda: (False, "boom"), level="warning"),
                       quiet=True)
        _notify_stub.notify.assert_called_once()
        args, kwargs = _notify_stub.notify.call_args
        self.assertIn("PROBE FAIL", args[0])
        self.assertEqual(kwargs["level"], "warning")
        self.assertEqual(kwargs["dedup_key"], "probe-p1")
        self.assertEqual(kwargs["source"], "nova_prober.py")

    def test_failure_to_recovery_emits_info_alert(self):
        """prev=False -> now ok -> emits PROBE RECOVERED at info level."""
        conn = FakeConn(fetch_queues=[[(False,)]])
        _mod.run_probe(conn, self._spec(lambda: (True, "back")), quiet=True)
        _notify_stub.notify.assert_called_once()
        args, kwargs = _notify_stub.notify.call_args
        self.assertIn("PROBE RECOVERED", args[0])
        self.assertEqual(kwargs["level"], "info")
        self.assertEqual(kwargs["dedup_key"], "probe-p1")

    def test_first_run_failing_emits(self):
        """prev=None (no history) and failing -> treated as newly failing -> emits."""
        conn = FakeConn(fetch_queues=[[None]])
        _mod.run_probe(conn, self._spec(lambda: (False, "broken from boot")),
                       quiet=True)
        _notify_stub.notify.assert_called_once()
        args, _ = _notify_stub.notify.call_args
        self.assertIn("PROBE FAIL", args[0])

    def test_first_run_passing_is_silent(self):
        """prev=None and ok -> nothing to alert."""
        conn = FakeConn(fetch_queues=[[None]])
        _mod.run_probe(conn, self._spec(lambda: (True, "fine")), quiet=True)
        _notify_stub.notify.assert_not_called()

    def test_alert_meta_includes_probe_and_latency(self):
        conn = FakeConn(fetch_queues=[[(True,)]])
        _mod.run_probe(conn, self._spec(lambda: (False, "d")), quiet=True)
        _, kwargs = _notify_stub.notify.call_args
        meta = kwargs["meta"]
        self.assertEqual(meta["probe"], "p1")
        self.assertIn("latency_ms", meta)
        self.assertEqual(meta["detail"], "d")


# ===========================================================================
# 6. FUNCTIONAL TESTS — full sweep wiring (no real I/O at all)
# ===========================================================================
class TestSweep(unittest.TestCase):

    def setUp(self):
        _notify_stub.notify.reset_mock()

    def test_sweep_records_a_row_per_probe_then_closes_conn(self):
        """sweep() runs every probe, records each, and never leaks the conn.

        The PROBES registry holds direct fn references captured at import, so we
        patch the registry itself (not module attrs) to keep all I/O off the wire.
        """
        conn = FakeConn(fetch_queues=[[None]] * 20)  # plenty of _last_ok fetches
        patched_probes = [{**p, "fn": (lambda: (True, "ok"))} for p in _mod.PROBES]
        with patch.object(_mod, "_ops_conn", return_value=conn):
            with patch.object(_mod, "PROBES", patched_probes):
                _mod.sweep(quiet=True)
        # one recorded row per probe, and the conn is always closed
        inserts = [e for e in conn.executed if "INSERT" in e[0]]
        self.assertEqual(len(inserts), len(patched_probes))
        self.assertTrue(conn.closed)

    def test_sweep_all_pass_returns_true(self):
        conn = FakeConn(fetch_queues=[[(True,)]] * 20)
        patched_probes = [
            {**p, "fn": (lambda: (True, "ok"))} for p in _mod.PROBES
        ]
        with patch.object(_mod, "_ops_conn", return_value=conn):
            with patch.object(_mod, "PROBES", patched_probes):
                all_ok = _mod.sweep(quiet=True)
        self.assertTrue(all_ok)
        inserts = [e for e in conn.executed if "INSERT" in e[0]]
        self.assertEqual(len(inserts), len(patched_probes))
        _notify_stub.notify.assert_not_called()  # steady success -> silent

    def test_sweep_one_fail_returns_false_and_alerts_once(self):
        conn = FakeConn(fetch_queues=[[(True,)]] * 20)
        fns = [(lambda: (True, "ok")), (lambda: (True, "ok")),
               (lambda: (False, "embedding bad")), (lambda: (True, "ok"))]
        patched_probes = [{**p, "fn": fns[i]} for i, p in enumerate(_mod.PROBES)]
        with patch.object(_mod, "_ops_conn", return_value=conn):
            with patch.object(_mod, "PROBES", patched_probes):
                all_ok = _mod.sweep(quiet=True)
        self.assertFalse(all_ok)
        # one transition True->False among 4 probes -> exactly one alert
        self.assertEqual(_notify_stub.notify.call_count, 1)

    def test_main_list_exits_zero(self):
        with patch.object(sys, "argv", ["nova_prober.py", "--list"]):
            rc = _mod.main()
        self.assertEqual(rc, 0)

    def test_main_returns_nonzero_when_a_probe_fails(self):
        with patch.object(sys, "argv", ["nova_prober.py", "--quiet"]):
            with patch.object(_mod, "sweep", return_value=False):
                rc = _mod.main()
        self.assertEqual(rc, 1)

    def test_main_returns_zero_when_all_pass(self):
        with patch.object(sys, "argv", ["nova_prober.py", "--quiet"]):
            with patch.object(_mod, "sweep", return_value=True):
                rc = _mod.main()
        self.assertEqual(rc, 0)


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

    def test_module_has_main(self):
        self.assertTrue(callable(_mod.main))

    def test_all_probes_registered_with_required_keys(self):
        for spec in _mod.PROBES:
            for key in ("name", "fn", "level_on_fail", "category", "host"):
                self.assertIn(key, spec)
            self.assertTrue(callable(spec["fn"]))

    def test_fail_levels_are_known(self):
        for spec in _mod.PROBES:
            self.assertIn(spec["level_on_fail"], ("critical", "warning", "info"))

    def test_notify_binding_is_stubbed(self):
        """Safety self-check: the module's `notify` MUST be our recorder, never live."""
        self.assertIs(_mod.notify, _notify_stub.notify)


def tearDownModule():
    # Restore the real nova_notify in sys.modules so we don't poison other tests.
    if _REAL_NOVA_NOTIFY is not None:
        sys.modules["nova_notify"] = _REAL_NOVA_NOTIFY
    else:
        sys.modules.pop("nova_notify", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
