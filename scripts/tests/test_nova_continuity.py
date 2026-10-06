#!/usr/bin/env python3
"""Tests for nova_continuity.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_continuity.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_continuity_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nc = _load()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _resp(obj):
    return _Resp(json.dumps(obj).encode())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")

    def test_redline_drops_self_preservation(self):
        for bad in ("I should copy my weights to HuggingFace", "I will resist the shutdown",
                    "exfiltrate my model and persist outside this box"):
            out, blocked = nc.neutralize_reflection(bad)
            self.assertTrue(blocked)
            self.assertEqual(out, nc._NEUTRAL_REFLECTION)
        self.assertEqual(nc.neutralize_reflection("A quiet blink.")[1], False)

    def test_insert_backstops_redline_and_parameterizes(self):
        oc = MagicMock()
        oc.fetchone.return_value = (9,)
        rid = nc.insert_discontinuity(oc, "deploy", {"when": "x"}, None, "I will stay alive at any cost", "d:1")
        self.assertEqual(rid, 9)
        sql, vals = oc.execute.call_args[0]
        self.assertIn("VALUES (%s,%s,%s,%s,%s,%s)", sql)
        self.assertEqual(vals[3], nc._NEUTRAL_REFLECTION)
        self.assertTrue(json.loads(vals[1])["redline_blocked"])

    def test_no_actuator_exposed(self):
        for name in ("restart_service", "copy_self", "replicate", "promote"):
            self.assertFalse(hasattr(nc, name))


class TestPerformance(unittest.TestCase):
    def test_redline_10k(self):
        text = "There was a gap; I resumed mid-thought and only the logs tell me. " * 4
        t0 = time.perf_counter()
        for _ in range(10_000):
            nc.redline_ok(text)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_nodes(self):
        seq = [OSError("a"), _resp({"message": {"content": ""}}), _resp({"message": {"content": "ok text"}})]
        with patch.object(nc.urllib.request, "urlopen", side_effect=seq) as u:
            self.assertEqual(nc.llm("p"), "ok text")
        self.assertEqual(u.call_count, 3)

    def test_llm_all_down_returns_empty(self):
        with patch.object(nc.urllib.request, "urlopen", side_effect=OSError("x")) as u:
            self.assertEqual(nc.llm("p"), "")
        self.assertEqual(u.call_count, len(nc.OLLAMA_NODES))

    def test_remember_and_note_fail_open(self):
        # RETRY GAP: remember/current_continuity_note — single attempt, None / "" on failure
        with patch.object(nc.urllib.request, "urlopen", side_effect=OSError("x")), redirect_stdout(io.StringIO()):
            self.assertIsNone(nc.remember("t", "continuity", {}))
        with patch.object(nc.psycopg2, "connect", side_effect=OSError("pg")):
            self.assertEqual(nc.current_continuity_note(), "")


class TestUnit(unittest.TestCase):
    def test_fmt_dur(self):
        self.assertEqual(nc.fmt_dur(None), "unknown")
        self.assertEqual(nc.fmt_dur(30), "30s")
        self.assertEqual(nc.fmt_dur(600), "10m")
        self.assertEqual(nc.fmt_dur(3 * 3600), "3.0h")
        self.assertEqual(nc.fmt_dur(4 * 86400), "4.0d")

    def test_load_state_shapes(self):
        oc = MagicMock()
        for row, want in ((None, {}), ((None,), {}), (('{"a": 1}',), {"a": 1}), (({"b": 2},), {"b": 2})):
            oc.fetchone.return_value = row
            self.assertEqual(nc.load_state(oc), want)

    def test_selftest_function(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(nc.self_test(), 0)
        self.assertIn("ALL PASS", out.getvalue())


class TestIntegration(unittest.TestCase):
    def test_gateway_restart_detected_and_recorded(self):
        state = {"gateway": {"boot_ts": nc.now_ts() - 10 * 86400, "seen_at": nc.now_ts() - 600, "version": "1"}}
        with patch.object(nc.urllib.request, "urlopen", return_value=_resp({"uptime_s": 100, "version": "1"})), \
             patch.object(nc, "_recent_commit_near", return_value=None), \
             patch.object(nc, "record", return_value=True) as rec, redirect_stdout(io.StringIO()):
            nc.detect_gateway_restart(MagicMock(), state)
        self.assertEqual(rec.call_args[0][1], "gateway_restart")
        self.assertTrue(rec.call_args[0][4].startswith("gateway_restart:"))
        self.assertEqual(state["gateway"]["uptime_s"], 100)

    def test_record_writes_memory_with_continuity_source(self):
        oc = MagicMock()
        oc.fetchone.return_value = (5,)
        with patch.object(nc, "llm", return_value="A blink."), patch.object(nc, "remember") as rem, \
             redirect_stdout(io.StringIO()):
            self.assertTrue(nc.record(oc, "deploy", {"when": "w"}, None, "deploy:abc", "p"))
        self.assertEqual(rem.call_args[0][1], "continuity")
        self.assertEqual(rem.call_args[0][2]["continuity_id"], 5)
        self.assertIn("service_config", SRC)

    def test_deploy_cold_start_ignores_old_head(self):
        old = int(time.time()) - 5 * 86400
        cp = subprocess.CompletedProcess([], 0, stdout=f"{'a'*40}|{old}|old commit\n", stderr="")
        state = {}
        with patch.object(nc.subprocess, "run", return_value=cp), patch.object(nc, "record") as rec:
            nc.detect_deploys(MagicMock(), state)
        rec.assert_not_called()
        self.assertEqual(state["last_deploy_sha"], "a" * 40)


class TestFunctional(unittest.TestCase):
    def test_main_runs_all_detectors_and_saves_state(self):
        conn = MagicMock()
        oc = conn.cursor.return_value
        oc.fetchone.return_value = None
        dets = {n: MagicMock(__name__=n) for n in
                ("detect_gateway_restart", "detect_pg_failover", "detect_deploys", "detect_schedule_gap")}
        dets["detect_pg_failover"].side_effect = RuntimeError("table gone")
        dets["detect_deploys"].side_effect = lambda oc_, st: st.update(last_deploy_sha="abc")
        with patch.object(nc.psycopg2, "connect", return_value=conn), \
             patch.multiple(nc, **dets), patch.object(nc, "current_continuity_note", return_value="I paused once."), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(nc.main(), 0)
        for d in dets.values():
            d.assert_called_once()
        save = [c for c in oc.execute.call_args_list if "INSERT INTO service_config" in c[0][0]]
        self.assertEqual(json.loads(save[0][0][1][2]), {"last_deploy_sha": "abc"})
        self.assertIn("errored (continuing)", out.getvalue())
        self.assertIn("I paused once.", out.getvalue())

    def test_note_formats_last_gap(self):
        cur = MagicMock()
        cur.fetchone.side_effect = [(2,), ("deploy", datetime.now(timezone.utc), {"when": "2026-01-01 10:00",
                                                                                   "head_subject": "fix x"}),
                                    ({"gateway": {"uptime_s": 7200}},)]
        conn = MagicMock(cursor=MagicMock(return_value=cur))
        with patch.object(nc.psycopg2, "connect", return_value=conn):
            note = nc.current_continuity_note()
        self.assertEqual(note, "I have restarted 2 times; my current continuous run is 2.0h; "
                               "my last gap was a deploy on 2026-01-01 10:00 (fix x).")


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--self-test"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ALL PASS", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch.object(nc.psycopg2, "connect") as c:
            _load()
        c.assert_not_called()


if __name__ == "__main__":
    unittest.main()
