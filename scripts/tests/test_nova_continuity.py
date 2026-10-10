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
             patch.object(nc, "wake_turnovers", return_value=[]) as wk, \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(nc.main(), 0)
        for d in dets.values():
            d.assert_called_once()
        wk.assert_called_once_with(cur=oc)        # merged from nova_bottle --wake (2026-10-09)
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


# ── Wake packets merged in from nova_bottle.py --wake (2026-10-09, organ audit M11) ──
import nova_bottle  # noqa: E402


class _Cur:
    def __init__(self, rows=None, boom=False):
        self.rows, self.boom, self.sql = list(rows or []), boom, []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class TestWakeSecurity(unittest.TestCase):
    def test_wake_adds_no_actuator_and_redline_assert_still_holds(self):
        for name in ("restart_service", "copy_self", "replicate", "promote"):
            self.assertFalse(hasattr(nc, name))
        self.assertNotIn("ssh", SRC.split("def wake_turnovers")[1].split("\ndef ")[0])

    def test_amulet_host_sql_has_no_interpolation(self):
        cur = _Cur([("x",), ("Studio",)])
        nc.amulet_host(cur)
        self.assertTrue(all(p is None for _, p in cur.sql))
        self.assertFalse(any("{" in s for s, _ in cur.sql))


class TestWakePerformance(unittest.TestCase):
    def test_days_parse_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            nc._days(["x", "--wake", "--days", str(i)])
        self.assertLess(time.perf_counter() - t0, 0.5)


class TestWakeRetry(unittest.TestCase):
    def test_wake_failure_is_contained_in_main(self):
        # RETRY GAP: wake_turnovers — single attempt per 6-hourly run; a failure is logged and the next run retries
        conn = MagicMock()
        conn.cursor.return_value.fetchone.return_value = None
        with patch.object(nc.psycopg2, "connect", return_value=conn), \
             patch.multiple(nc, detect_gateway_restart=MagicMock(), detect_pg_failover=MagicMock(),
                            detect_deploys=MagicMock(), detect_schedule_gap=MagicMock()), \
             patch.object(nc, "current_continuity_note", return_value=""), \
             patch.object(nc, "wake_turnovers", side_effect=RuntimeError("nas gone")), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(nc.main(), 0)
        self.assertIn("wake packets errored (continuing): nas gone", out.getvalue())

    def test_amulet_host_pg_error_is_none(self):
        self.assertIsNone(nc.amulet_host(_Cur(boom=True)))


class TestWakeUnit(unittest.TestCase):
    def test_amulet_host_shapes(self):
        self.assertIsNone(nc.amulet_host(_Cur([(None,)])))                 # table absent
        self.assertIsNone(nc.amulet_host(_Cur([("jade_amulet_manifest",)])))   # empty table
        self.assertEqual(nc.amulet_host(_Cur([("jade_amulet_manifest",), ("Studio",)])), "Studio")

    def test_days_arg(self):
        self.assertEqual(nc._days(["--wake"]), 7)
        self.assertEqual(nc._days(["--wake", "--days", "30"]), 30)
        self.assertEqual(nc._days(["--days", "x"]), 7)


class TestWakeIntegration(unittest.TestCase):
    def test_wake_turnovers_calls_bottle_wake_with_amulet_host(self):
        cur = MagicMock()
        with patch.object(nova_bottle, "wake", return_value=[{"watch": "wake"}]) as w, \
             patch.object(nc, "amulet_host", return_value="Studio"):
            self.assertEqual(nc.wake_turnovers(14, dry=True, cur=cur), [{"watch": "wake"}])
        w.assert_called_once_with(14, dry=True, cur=cur, host="Studio")

    def test_same_turnover_row_shape(self):
        # the packet writer is the Bottle's own (watch_turnover watch='wake' + bottle_log 'wake')
        bsrc = (SCRIPTS / "nova_bottle.py").read_text()
        self.assertIn("WB.save_turnover(cur, p, text)", bsrc)
        self.assertIn('"watch": "wake"', bsrc)


class TestWakeFunctional(unittest.TestCase):
    def test_gap_over_six_hours_becomes_one_wake_turnover(self):
        from datetime import timedelta
        t0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        gap = (44, "gateway_restart", t0 + timedelta(hours=10), 36000.0, {"gap_is_upper_bound": True})
        routes = {"to_regclass": [("bottle_log",)], "FROM continuity_log": [gap], "FROM gateway_traces": [],
                  "FROM service_config": []}

        class Cur:
            def __init__(self):
                self.sql, self._last = [], []

            def execute(self, sql, params=None):
                self.sql.append((sql, params))
                self._last = next((list(v) for k, v in routes.items() if k in sql), [])

            def fetchall(self):
                return self._last

            def fetchone(self):
                return self._last[0] if self._last else None
        cur = Cur()
        with patch.object(nova_bottle, "nas_dir", return_value=None), \
             patch.object(nova_bottle, "amulet_diff", return_value=[]), \
             patch.object(nc, "amulet_host", return_value="Studio"), redirect_stdout(io.StringIO()):
            packets = nc.wake_turnovers(30, cur=cur)
        self.assertEqual(len(packets), 1)
        ins = [p for s_, p in cur.sql if "INSERT INTO watch_turnover" in s_]
        self.assertEqual(ins[0][0], "wake")
        bl = [p for s_, p in cur.sql if "INSERT INTO bottle_log" in s_]
        self.assertEqual((bl[0][0], bl[0][2], bl[0][3]), ("wake", "Studio", "44"))

    def test_dry_run_writes_nothing(self):
        cur = MagicMock()
        cur.fetchall.return_value = []
        with patch.object(nc, "amulet_host", return_value=None), redirect_stdout(io.StringIO()):
            self.assertEqual(nc.wake_turnovers(7, dry=True, cur=cur), [])
        self.assertFalse(any("INSERT" in c[0][0] for c in cur.execute.call_args_list))


class TestWakeFrame(unittest.TestCase):
    def test_cli_routes_wake_before_main(self):
        tail = SRC.split('if __name__ == "__main__":')[1]
        self.assertLess(tail.index("--wake"), tail.index("main()"))
        self.assertIn('dry="--dry-run" in sys.argv', tail)


if __name__ == "__main__":
    unittest.main()
