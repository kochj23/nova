#!/usr/bin/env python3
"""Tests for nova_autonomy_graduation.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_autonomy_graduation.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


grad = _load("grad_under_test", SCRIPT)


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


TRUST = [("observe:draft", 3, 0, True),
         ("restart:nova-soil-monitor", 4, 0, False),
         ("restart:x", 1, 0, False),
         ("observe:note", 3, 1, False)]


def _rules(ce=0.1, hw=None, trust=TRUST):
    return [("FROM turing_scoreboard", (ce,) if ce is not None else None),
            ("key='high_water'", (hw,) if hw is not None else None),
            ("FROM autonomy_trust ORDER BY correct_count", trust)]


def _run_main(cur, argv=(), urlopen=None, post_both=None):
    cfg = types.SimpleNamespace(post_both=post_both or mock.MagicMock(), SLACK_CHAN="C_TEST")
    uo = urlopen or mock.MagicMock(return_value=_Resp({"id": 1}))
    buf = io.StringIO()
    with mock.patch.object(grad.psycopg2, "connect", return_value=_conn(cur)), \
         mock.patch.object(sys, "argv", ["nova_autonomy_graduation.py", *argv]), \
         mock.patch("urllib.request.urlopen", uo), \
         mock.patch.dict(sys.modules, {"nova_config": cfg}), redirect_stdout(buf):
        rc = grad.main()
    return rc, cfg, uo, buf.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertIn("VALUES ('nova_autonomy_graduation','high_water',%s", SRC)

    def test_never_grants_or_touches_trust(self):
        # the hard safety line: this organ only READS autonomy_trust; grants live in nova_autonomy_safety
        self.assertNotRegex(SRC, r"(UPDATE|INSERT INTO|DELETE FROM)\s+autonomy_trust")
        self.assertNotIn("_maybe_grant(", SRC[SRC.index("import argparse"):])   # mentioned in the docstring, never called
        writes = {m.group(1) for m in re.finditer(r"(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"service_config"})


class TestPerformance(unittest.TestCase):
    def test_need_for_fast_on_10k_classes(self):
        classes = [f"restart:svc{i}" if i % 3 else f"observe:draft{i}" for i in range(10_000)]
        t0 = time.perf_counter()
        needs = [grad._need_for(c) for c in classes]
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(needs.count(grad.MIN_CORRECT), 6_666)


class TestRetry(unittest.TestCase):
    def test_memory_write_fails_open(self):
        # RETRY GAP: _remember — one urlopen attempt, failure logged and swallowed
        uo = mock.MagicMock(side_effect=OSError("memory server down"))
        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen", uo), redirect_stdout(buf):
            grad._remember("x")
        self.assertEqual(uo.call_count, 1)
        self.assertIn("memory write skipped", buf.getvalue())

    def test_slack_failure_fails_open(self):
        # RETRY GAP: _notify — one post_both attempt, failure logged and swallowed
        cfg = types.SimpleNamespace(post_both=mock.MagicMock(side_effect=OSError("slack down")), SLACK_CHAN="C")
        buf = io.StringIO()
        with mock.patch.dict(sys.modules, {"nova_config": cfg}), redirect_stdout(buf):
            grad._notify("hi")
        self.assertEqual(cfg.post_both.call_count, 1)
        self.assertIn("slack post skipped", buf.getvalue())


class TestUnit(unittest.TestCase):
    def test_need_for_splits_the_bar_and_falls_back(self):
        self.assertEqual(grad._need_for("restart:nova-soil-monitor"), grad.MIN_CORRECT)
        self.assertEqual(grad._need_for("observe:draft"), grad._safety.MIN_CORRECT_REVERSIBLE)
        with mock.patch.object(grad, "_safety", None):
            self.assertEqual(grad._need_for("observe:draft"), grad.MIN_CORRECT)

    def test_hw_parses_dict_string_and_absent(self):
        self.assertEqual(grad._hw(_Cur([("high_water", ({"announced": ["a"], "near": {}},))]))["announced"], ["a"])
        self.assertEqual(grad._hw(_Cur([("high_water", ('{"announced": [], "near": {"x": 2}}',))]))["near"], {"x": 2})
        self.assertEqual(grad._hw(_Cur()), {"announced": [], "near": {}})
        self.assertEqual(grad._hw(_Cur(raise_on=("service_config",))), {"announced": [], "near": {}})

    def test_calibration_none_on_missing_or_error(self):
        self.assertEqual(grad._calibration(_Cur([("turing_scoreboard", (0.25,))])), 0.25)
        self.assertIsNone(grad._calibration(_Cur()))
        self.assertIsNone(grad._calibration(_Cur(raise_on=("turing_scoreboard",))))


class TestIntegration(unittest.TestCase):
    def test_bar_comes_from_the_shared_safety_module(self):
        import nova_autonomy_safety as S
        self.assertIs(grad._safety, S)
        self.assertEqual(grad.MIN_CORRECT, S.MIN_CORRECT)
        self.assertEqual(grad.MAX_CALIB, S.MAX_CALIB)
        for ac in ("restart:a", "observe:b", "ingest:gutenberg", "observe:adjust the dial"):
            self.assertEqual(grad._need_for(ac), S.min_correct_for(ac))

    def test_high_water_round_trips_through_service_config(self):
        cur = _Cur()
        grad._save_hw(cur, {"announced": ["observe:draft"], "near": {"restart:x": 4}})
        (sql, params), = cur.stmts("INSERT INTO service_config")
        self.assertIn("'nova_autonomy_graduation','high_water'", sql)
        saved = json.loads(params[0])
        cur2 = _Cur([("key='high_water'", (saved,))])
        self.assertEqual(grad._hw(cur2), saved)


class TestFunctional(unittest.TestCase):
    def test_golden_path_announces_surfaces_remembers_and_advances(self):
        cur = _Cur(_rules(hw={"announced": [], "near": {"restart:x": 1}}))
        rc, cfg, uo, out = _run_main(cur)
        self.assertEqual(rc, 0)
        msg = cfg.post_both.call_args[0][0]
        self.assertIn("🎓 *observe:draft*", msg)
        self.assertIn("*restart:nova-soil-monitor* — 4/5 clean and I'm calibrated (0.100)", msg)
        self.assertIn("One more", msg)
        self.assertNotIn("observe:note", msg)                      # a vetoed class is never 'close'
        self.assertEqual(uo.call_count, 1)
        body = json.loads(uo.call_args[0][0].data)
        self.assertIn("standing autonomy for 'observe:draft'", body["text"])
        self.assertEqual(body["metadata"]["organ"], "autonomy_graduation")
        (_, params), = cur.stmts("INSERT INTO service_config")
        hw = json.loads(params[0])
        self.assertEqual(hw["announced"], ["observe:draft"])
        self.assertEqual(hw["near"], {"restart:nova-soil-monitor": 4, "restart:x": 1})

    def test_unchanged_near_class_is_not_nagged(self):
        trust = [("restart:nova-soil-monitor", 4, 0, False)]
        cur = _Cur(_rules(hw={"announced": [], "near": {"restart:nova-soil-monitor": 4}}, trust=trust))
        rc, cfg, uo, out = _run_main(cur)
        self.assertEqual(rc, 0)
        cfg.post_both.assert_not_called()
        self.assertFalse(cur.stmts("INSERT INTO service_config"))
        self.assertIn("no graduations", out)

    def test_weak_calibration_is_said_honestly(self):
        cur = _Cur(_rules(ce=0.5, trust=[("restart:nova-soil-monitor", 4, 0, False)]))
        rc, cfg, uo, out = _run_main(cur)
        self.assertIn("above the 0.2 gate", cfg.post_both.call_args[0][0])

    def test_dry_run_prints_and_writes_nothing(self):
        cur = _Cur(_rules())
        rc, cfg, uo, out = _run_main(cur, argv=["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("Autonomy — where I stand", out)
        cfg.post_both.assert_not_called()
        self.assertFalse(cur.stmts("INSERT INTO service_config"))

    def test_missing_trust_table_is_a_quiet_exit(self):
        cur = _Cur(_rules(), raise_on=("FROM autonomy_trust ORDER BY",))
        rc, cfg, uo, out = _run_main(cur)
        self.assertEqual(rc, 0)
        self.assertIn("nothing to do", out)
        cfg.post_both.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], cwd=SCRIPTS,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertRegex(SRC, r'if __name__ == "__main__":\n\s+sys\.exit\(main\(\)\)')
        with mock.patch("psycopg2.connect", side_effect=AssertionError("import must not connect")):
            _load("grad_frame_probe", SCRIPT)


if __name__ == "__main__":
    unittest.main()
