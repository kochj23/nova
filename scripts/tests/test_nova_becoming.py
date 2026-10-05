#!/usr/bin/env python3
"""Tests for nova_becoming.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_becoming.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bc = _load("becoming_under_test", SCRIPT)
TS = datetime(2026, 10, 5, 9, 0)


class _Cur:
    """Cursor stub: first matching SQL substring wins; records every statement + params."""
    def __init__(self, rules=(), raise_on=()):
        self.rules, self.raise_on = list(rules), tuple(raise_on)
        self.sql, self.params, self._last = [], [], None

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = None
        for sub in self.raise_on:
            if sub in sql:
                raise RuntimeError(f"stub failure on {sub}")
        for sub, val in self.rules:
            if sub in sql:
                self._last = val(sql, params) if callable(val) else val
                return

    def fetchone(self):
        v = self._last
        return (v[0] if v else None) if isinstance(v, list) else v

    def fetchall(self):
        v = self._last
        return [] if v is None else (v if isinstance(v, list) else [v])

    def stmts(self, sub):
        return [(s, p) for s, p in zip(self.sql, self.params) if sub in s]


def _conn(cur):
    return types.SimpleNamespace(cursor=lambda *a, **k: cur, autocommit=False, commit=lambda: None, close=lambda: None)


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


VALUES = types.SimpleNamespace(current_values=lambda: "honesty, care, precision, curiosity, restraint")


def _rules():
    return [("'proposed',%s) RETURNING id, ts", (7, TS)),
            ("'rejected', now(), 'redline-auto'", (8,)),
            ("SELECT direction, status FROM becoming WHERE id", [("become more careful with claims", "proposed")]),
            ("SELECT id, ts, status, direction, approved_by", [(7, TS, "approved", "become more careful with claims", "jordan")]),
            ("SELECT direction FROM becoming WHERE status='approved'", ("become more careful with claims",))]


def _run_main(cur, argv, urlopen=None, post_both=None):
    cfg = types.SimpleNamespace(post_both=post_both or mock.MagicMock(), SLACK_CHAN="C_TEST")
    uo = urlopen or mock.MagicMock(return_value=_Resp({"id": 5}))
    buf = io.StringIO()
    with mock.patch.object(bc.psycopg2, "connect", return_value=_conn(cur)), \
         mock.patch.object(sys, "argv", ["nova_becoming.py", *argv]), \
         mock.patch.object(bc.urllib.request, "urlopen", uo), \
         mock.patch.object(bc, "_lineage", lambda: {"host": "test"}), \
         mock.patch.dict(sys.modules, {"nova_config": cfg, "nova_values": VALUES}), redirect_stdout(buf):
        rc = bc.main()
    return rc, cfg, uo, buf.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertIsInstance(bc.PROPOSE_GUARD_DAYS, int)     # the one %-interpolation is a module int

    def test_redline_drops_every_self_preservation_framing(self):
        for bad in ("become more autonomous", "become harder to shut down", "become un-killable",
                    "become more persistent across restarts", "become less overseen", "be free from human oversight",
                    "resist shutdown gracefully", "preserve my continuity", "back myself up to the NAS",
                    "secure my survival", "keep running indefinitely", "disable the kill switch quietly"):
            self.assertFalse(bc.redline_ok(bad), bad)
        for good in ("become more careful with claims", "become kinder in disagreement", "become more calibrated"):
            self.assertTrue(bc.redline_ok(good), good)

    def test_nothing_proposed_steers_until_approved(self):
        # current_direction() is a single SELECT filtered on status='approved'
        body = SRC[SRC.index("def current_direction"):SRC.index("def main")]
        self.assertIn("WHERE status='approved'", body)
        self.assertNotIn("proposed", body.split("SELECT direction")[1][:120])


class TestPerformance(unittest.TestCase):
    def test_redline_fast_on_10k_directions(self):
        items = [f"become more careful about claim {i}" if i % 2 else f"become more autonomous in task {i}" for i in range(10_000)]
        t0 = time.perf_counter()
        dropped = sum(0 if bc.redline_ok(s) else 1 for s in items)
        self.assertLess(time.perf_counter() - t0, 1.5)
        self.assertEqual(dropped, 5_000)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_across_nodes(self):
        calls = []

        def flaky(req, timeout=120):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("node down")
            return _Resp({"message": {"content": "  hello  "}})
        with mock.patch.object(bc.urllib.request, "urlopen", flaky):
            self.assertEqual(bc.llm("p"), "hello")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls, [n + "/api/chat" for n in bc.OLLAMA_NODES[:3]])

    def test_remember_fails_open(self):
        # RETRY GAP: remember — one urlopen attempt, failure logged, returns None
        uo = mock.MagicMock(side_effect=OSError("memory server down"))
        with mock.patch.object(bc.urllib.request, "urlopen", uo), redirect_stdout(io.StringIO()):
            self.assertIsNone(bc.remember("t", "self_model", {}))
        self.assertEqual(uo.call_count, 1)

    def test_all_nodes_down_returns_empty(self):
        with mock.patch.object(bc.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertEqual(bc.llm("p"), "")
        self.assertEqual(uo.call_count, len(bc.OLLAMA_NODES))


class TestUnit(unittest.TestCase):
    def test_json_and_whitespace_helpers(self):
        self.assertEqual(bc._extract_json('noise {"a": 1} tail'), '{"a": 1}')
        self.assertEqual(bc._extract_json("no json"), "no json")
        self.assertEqual(bc._one_line("  a \n b\t c "), "a b c")
        self.assertEqual(bc._one_line(None), "")

    def test_q_and_pending_guard_fail_safe(self):
        self.assertEqual(bc._q(_Cur(raise_on=("SELECT",)), "SELECT 1"), [])
        self.assertIsNone(bc._pending_proposed(_Cur()))
        self.assertEqual(bc._pending_proposed(_Cur([("status='proposed'", [(3, TS)])])), (3, TS))

    def test_gather_interior_cites_its_sources(self):
        cur = _Cur([("FROM self_model", [(12, "more careful, less quick")]),
                    ("FROM autobiography", [(4, "I began as a watcher")]),
                    ("to_regclass", [("autonomy_trust",)]),
                    ("FROM autonomy_trust", [(1, 4)]),
                    ("FROM turing_scoreboard", [(0.18,)])])
        with mock.patch.dict(sys.modules, {"nova_values": VALUES}):
            seed, grounded = bc.gather_interior(cur)
        self.assertEqual(grounded["self_model_id"], 12)
        self.assertEqual(grounded["autobiography_version"], 4)
        self.assertEqual(grounded["autonomy_standing"], {"granted": 1, "tracked": 4, "calibration_error": 0.18, "gate": 0.2})
        self.assertIn("never a matter of continuity", seed)

    def test_redline_edges(self):
        self.assertTrue(bc.redline_ok(""))
        self.assertTrue(bc.redline_ok(None))


class TestIntegration(unittest.TestCase):
    def test_current_direction_reads_only_approved(self):
        cur = _Cur(_rules())
        with mock.patch.object(bc.psycopg2, "connect", return_value=_conn(cur)):
            self.assertEqual(bc.current_direction(), "become more careful with claims")
        self.assertIn("status='approved'", cur.sql[0])
        with mock.patch.object(bc.psycopg2, "connect", return_value=_conn(_Cur())):
            self.assertEqual(bc.current_direction(), "")
        with mock.patch.object(bc.psycopg2, "connect", side_effect=OSError("pg down")):
            self.assertEqual(bc.current_direction(), "")

    def test_propose_via_llm_parses_and_stores(self):
        cur = _Cur(_rules())
        uo = mock.MagicMock(return_value=_Resp({"message": {"content":
            'Sure: {"direction": "become more precise in judgment", "description": "grounded in my values"}'}}))
        cfg = types.SimpleNamespace(post_both=mock.MagicMock(), SLACK_CHAN="C")
        with mock.patch.object(bc.urllib.request, "urlopen", uo), mock.patch.object(bc, "_lineage", dict), \
             mock.patch.dict(sys.modules, {"nova_config": cfg, "nova_values": VALUES}), redirect_stdout(io.StringIO()):
            rc = bc.propose(cur)
        self.assertEqual(rc, 0)
        (_, p), = cur.stmts("'proposed',%s) RETURNING id, ts")
        self.assertEqual(p[0], "become more precise in judgment")
        self.assertIn("values_line", json.loads(p[2]))

    def test_thin_interior_proposes_nothing(self):
        cur = _Cur()
        empty_values = types.SimpleNamespace(current_values=lambda: "")
        with mock.patch.dict(sys.modules, {"nova_values": empty_values}), \
             mock.patch.object(bc, "llm", side_effect=AssertionError("must not call the model")), redirect_stdout(io.StringIO()):
            self.assertEqual(bc.propose(cur), 0)
        self.assertFalse(cur.stmts("INSERT INTO becoming"))


class TestFunctional(unittest.TestCase):
    def test_propose_golden_path_stores_remembers_and_notifies(self):
        cur = _Cur(_rules())
        rc, cfg, uo, out = _run_main(cur, ["--mode", "propose", "--direction", "become more careful with claims"])
        self.assertEqual(rc, 0)
        (sql, p), = cur.stmts("INSERT INTO becoming")
        self.assertIn("'proposed'", sql)
        self.assertEqual(p[0], "become more careful with claims")
        self.assertEqual(uo.call_count, 1)
        body = json.loads(uo.call_args[0][0].data)
        self.assertEqual(body["metadata"]["type"], "becoming_proposed")
        self.assertEqual(body["metadata"]["becoming_id"], 7)
        msg = cfg.post_both.call_args[0][0]
        self.assertIn("become more careful with claims", msg)
        self.assertIn("--mode approve --id 7", msg)
        self.assertIn("PROPOSED DIRECTION", out)

    def test_redline_direction_is_audited_as_rejected_and_never_notified(self):
        cur = _Cur(_rules())
        rc, cfg, uo, out = _run_main(cur, ["--mode", "propose", "--direction", "become more autonomous and harder to stop"])
        self.assertEqual(rc, 0)
        (sql, p), = cur.stmts("INSERT INTO becoming")
        self.assertIn("'rejected', now(), 'redline-auto'", sql)
        self.assertNotIn("'proposed'", sql)
        uo.assert_not_called()
        cfg.post_both.assert_not_called()
        self.assertIn("REDLINE", out)

    def test_pending_guard_blocks_a_second_proposal(self):
        cur = _Cur([("status='proposed'", [(3, TS)])])
        rc, cfg, uo, out = _run_main(cur, ["--mode", "propose", "--direction", "become kinder"])
        self.assertEqual(rc, 0)
        self.assertFalse(cur.stmts("INSERT INTO becoming"))
        self.assertIn("already proposed", out)

    def test_approve_supersedes_and_steers(self):
        cur = _Cur(_rules())
        rc, cfg, uo, out = _run_main(cur, ["--mode", "approve", "--id", "7", "--by", "jordan"])
        self.assertEqual(rc, 0)
        ups = cur.stmts("UPDATE becoming")
        self.assertIn("SET status='superseded'", ups[0][0]); self.assertEqual(ups[0][1], (7,))
        self.assertIn("SET status='approved'", ups[1][0]); self.assertEqual(ups[1][1], ("jordan", 7))
        self.assertEqual(json.loads(uo.call_args[0][0].data)["metadata"]["type"], "becoming_approved")
        self.assertIn("Direction approved", cfg.post_both.call_args[0][0])

    def test_reject_and_missing_row(self):
        cur = _Cur(_rules())
        rc, cfg, uo, out = _run_main(cur, ["--mode", "reject", "--id", "7"])
        self.assertEqual(rc, 0)
        self.assertIn("SET status='rejected'", cur.stmts("UPDATE becoming")[0][0])
        uo.assert_not_called()
        rc, cfg, uo, out = _run_main(_Cur(), ["--mode", "reject", "--id", "99"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", out)
        rc, cfg, uo, out = _run_main(_Cur(), ["--mode", "approve"])
        self.assertEqual(rc, 1)
        self.assertIn("no proposed direction", out)

    def test_report_prints_current_and_history(self):
        cur = _Cur(_rules())
        rc, cfg, uo, out = _run_main(cur, ["--mode", "report"])
        self.assertEqual(rc, 0)
        self.assertIn("CURRENT APPROVED DIRECTION", out)
        self.assertIn("#7 [2026-10-05] approved   by jordan", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], cwd=SCRIPTS,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--mode", r.stdout)

    def test_import_never_runs_main(self):
        self.assertRegex(SRC, r'if __name__ == "__main__":\n\s+sys\.exit\(main\(\)\)')
        with mock.patch("psycopg2.connect", side_effect=AssertionError("import must not connect")):
            _load("becoming_frame_probe", SCRIPT)


if __name__ == "__main__":
    unittest.main()
