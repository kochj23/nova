#!/usr/bin/env python3
"""Tests for nova_prober.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_prober.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("npr_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pr = _load()
pr.notify = MagicMock()          # stub the outbound alert bus at module load


def _resp(body, status=200):
    r = MagicMock()
    r.__enter__.return_value.read.return_value = body if isinstance(body, bytes) else body.encode()
    r.__enter__.return_value.getcode.return_value = status
    r.__enter__.return_value.status = status
    return r


class _Conn:
    """Fake ops connection: answers _last_ok from `prev`, records inserts."""
    def __init__(self, prev=None, fail_insert=False):
        self.prev = prev; self.inserts = []; self.fail_insert = fail_insert; self.closed = False

    def cursor(self, cursor_factory=None):
        outer = self
        class C:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, sql, params=None):
                if sql.startswith("INSERT"):
                    if outer.fail_insert:
                        raise RuntimeError("pg write failed")
                    outer.inserts.append(params)
                s.sql = sql
            def fetchone(s):
                return None if outer.prev is None else (outer.prev,)
        return C()

    def commit(self): pass
    def close(self): self.closed = True


def _spec(fn, name="p1"):
    return {"name": name, "fn": fn, "level_on_fail": "critical", "category": "probe", "host": "h"}


def _slow_clock():
    seq = iter([100.0, 100.5])            # 500 ms latency: above the counterfeit floor
    return patch.object(pr.time, "time", side_effect=lambda: next(seq))


class _Base(unittest.TestCase):
    def setUp(self):
        pr.notify.reset_mock(side_effect=True)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", pr.OPS_DSN + pr.MEM_DSN)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        self.assertIn('"DELETE FROM public.memories WHERE id = %s", (mem_id,)', SRC)

    def test_roundtrip_always_cleans_up(self):
        conns = [MagicMock(), MagicMock()]
        cur = conns[0].__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None
        with patch.object(pr.urllib.request, "urlopen", return_value=_resp('{"id": "m1", "status": "stored"}')), \
             patch.object(pr, "_mem_conn", side_effect=conns):
            ok, detail = pr.probe_memory_roundtrip()
        self.assertFalse(ok)
        del_cur = conns[1].__enter__.return_value.cursor.return_value.__enter__.return_value
        self.assertIn("DELETE", del_cur.execute.call_args[0][0])
        self.assertEqual(del_cur.execute.call_args[0][1], ("m1",))


class TestPerformance(_Base):
    def test_sweep_of_many_probes_bounded(self):
        many = [_spec(lambda: (True, "fine"), f"p{i}") for i in range(2000)]
        conn = _Conn(prev=True)
        with patch.object(pr, "PROBES", many), patch.object(pr, "_ops_conn", return_value=conn):
            t0 = time.perf_counter()
            pr.sweep(quiet=True)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(conn.inserts), 2000)
        self.assertTrue(conn.closed)

    def test_detail_capped(self):
        conn = _Conn()
        with _slow_clock():
            pr.run_probe(conn, _spec(lambda: (False, "x" * 10_000)), quiet=True)
        self.assertEqual(len(conn.inserts[0][3]), 2000)


class TestRetry(_Base):
    def test_probes_are_single_shot_and_never_raise(self):
        # RETRY GAP: probe_http/probe_embedding/probe_inference_vantage — one attempt per sweep by design
        # (a hang IS a failure); errors come back as (False, detail), never as exceptions.
        with patch.object(pr.urllib.request, "urlopen", side_effect=OSError("down")) as u:
            self.assertFalse(pr.probe_http()[0])
            self.assertFalse(pr.probe_embedding()[0])
            self.assertFalse(pr.probe_inference_vantage()[0])
            self.assertFalse(pr.probe_memory_roundtrip()[0])
        self.assertEqual(u.call_count, len(pr.HTTP_CHECKS) + 3)

    def test_notify_and_record_failures_do_not_abort(self):
        pr.notify.side_effect = RuntimeError("bus down")
        conn = _Conn(prev=True, fail_insert=True)
        with _slow_clock(), patch("sys.stderr", new_callable=io.StringIO):
            self.assertFalse(pr.run_probe(conn, _spec(lambda: (False, "broke")), quiet=True))

    def test_raising_probe_is_a_failure(self):
        conn = _Conn()
        with _slow_clock():
            self.assertFalse(pr.run_probe(conn, _spec(lambda: 1 / 0), quiet=True))
        self.assertIn("probe raised ZeroDivisionError", conn.inserts[0][3])


class TestUnit(_Base):
    def test_http_checks_status_and_content(self):
        def fake(req, timeout=None):
            url = req.full_url
            if "nova." in url:
                return _resp("<html>ok")
            if url.startswith("https://digitalnoise"):
                return _resp("blank page")
            raise urllib.error.HTTPError(url, 503, "x", {}, None)
        with patch.object(pr.urllib.request, "urlopen", side_effect=fake):
            ok, detail = pr.probe_http()
        self.assertFalse(ok)
        self.assertIn("200 but missing 'digitalnoise.net'", detail)
        self.assertIn("HTTP 503", detail)

    def test_embedding_dims(self):
        with patch.object(pr.urllib.request, "urlopen", return_value=_resp(json.dumps({"embedding": [0.1] * 768}))):
            self.assertTrue(pr.probe_embedding()[0])
        with patch.object(pr.urllib.request, "urlopen", return_value=_resp(json.dumps({"embedding": [0.1] * 3}))):
            self.assertIn("expected 768", pr.probe_embedding()[1])

    def test_vantage_gap_detected(self):
        pool = {"backends": {"10.0.0.5:11434": {"healthy": False}, "10.0.0.6:5050": {"healthy": True}}}
        with patch.object(pr.urllib.request, "urlopen", side_effect=[_resp(json.dumps(pool)), _resp("ok")]):
            ok, detail = pr.probe_inference_vantage()
        self.assertFalse(ok)
        self.assertIn("VANTAGE GAP: 10.0.0.5:11434", detail)

    def test_cloudflared_connector_count(self):
        out = "CONNECTOR ID  ...\nabc linux_amd64\ndef darwin_arm64\n"
        with patch("subprocess.run", return_value=SimpleNamespace(stdout=out)):
            self.assertEqual(pr.probe_cloudflared(), (True, "tunnel up, 2 connector(s) active"))


class TestIntegration(_Base):
    def test_counterfeit_fast_pass_downgraded_by_witness(self):
        conn = _Conn(prev=True)
        seq = iter([100.0, 100.0])                          # 0 ms: faster than physics
        with patch.object(pr.time, "time", side_effect=lambda: next(seq)):
            self.assertFalse(pr.run_probe(conn, _spec(lambda: (True, "ok")), quiet=True))
        self.assertIn("COUNTERFEIT", conn.inserts[0][3])

    def test_state_change_alerts_only(self):
        with _slow_clock():
            pr.run_probe(_Conn(prev=True), _spec(lambda: (True, "fine")), quiet=True)
        pr.notify.assert_not_called()                       # steady success is silent
        with _slow_clock():
            pr.run_probe(_Conn(prev=True), _spec(lambda: (False, "dead")), quiet=True)
        self.assertEqual(pr.notify.call_args.kwargs["dedup_key"], "probe-p1-fail")
        with _slow_clock():
            pr.run_probe(_Conn(prev=False), _spec(lambda: (True, "back")), quiet=True)
        self.assertTrue(pr.notify.call_args[0][0].startswith("PROBE RECOVERED: p1 @ h"))
        pr.notify.reset_mock()
        with _slow_clock():
            pr.run_probe(_Conn(prev=False), _spec(lambda: (False, "still dead")), quiet=True)
        pr.notify.assert_not_called()                       # still failing: no re-page


class TestFunctional(_Base):
    def test_main_sweep_exit_codes(self):
        specs = [_spec(lambda: (True, "a"), "good"), _spec(lambda: (False, "b"), "bad")]
        clock = iter([float(i) for i in range(10)])
        with patch.object(pr, "PROBES", specs), patch.object(pr, "_ops_conn", return_value=_Conn(prev=True)), \
             patch.object(pr.time, "time", side_effect=lambda: next(clock)), \
             patch.object(sys, "argv", ["nova_prober.py", "--quiet"]):
            self.assertEqual(pr.main(), 1)
        self.assertEqual(pr.notify.call_args[0][0], "PROBE FAIL: bad @ h")
        with patch.object(pr, "PROBES", specs[:1]), patch.object(pr, "_ops_conn", return_value=_Conn(prev=True)), \
             patch.object(pr.time, "time", side_effect=iter([0.0, 1.0]).__next__), \
             patch.object(sys, "argv", ["nova_prober.py", "--quiet"]):
            self.assertEqual(pr.main(), 0)


class TestFrame(unittest.TestCase):
    def test_list_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--list"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("memory_roundtrip", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_prober"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
