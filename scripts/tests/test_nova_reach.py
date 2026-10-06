#!/usr/bin/env python3
"""Tests for nova_reach.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
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
from io import StringIO
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_reach.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


os.environ.pop("NOVA_REACH_WINDOW", None)
os.environ.pop("NOVA_REACH_DIRECT", None)
rc = _load("reach_under_test", SCRIPT)


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Cur:
    def __init__(self, today=0, last=None, prior=(), log_rows=()):
        self.today, self.last, self.prior, self.log_rows = today, last, list(prior), list(log_rows)
        self.sql, self.params, self._last, self.next_id = [], [], "", 10

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def fetchall(self):
        if "SELECT topic, message FROM reach_log" in self._last:
            return self.prior
        if "FROM reach_log ORDER BY ts DESC" in self._last:
            return self.log_rows
        return []

    def fetchone(self):
        s = self._last
        if "count(*) FROM reach_log WHERE ts::date" in s:
            return (self.today,)
        if "max(ts)" in s:
            return (self.last,)
        if "INSERT INTO reach_log" in s:
            self.next_id += 1; return (self.next_id,)
        if "count(*) FROM reach_log WHERE status" in s:
            return (len(self.log_rows),)
        if "to_regclass" in s:
            return (None,)
        return None

    def records(self):
        return [p for s, p in zip(self.sql, self.params) if "INSERT INTO reach_log" in s]


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur


GOOD = {"audience": "gaston", "score": 0.9, "topic": "formal clauses",
        "message": "I found a 1904 rail timetable whose clauses read like a binding spec; it made me think of your work.",
        "rationale": "He has been chasing clause-as-spec for weeks."}


def _fake_coagency(result=None, exc=None):
    m = types.ModuleType("nova_coagency")
    calls = []

    def file_proposal(oc, origin, action, rationale="", target_service=None, context=""):
        calls.append(dict(origin=origin, action=action, rationale=rationale, context=context))
        if exc:
            raise exc
        return result
    m.file_proposal = file_proposal; m._calls = calls
    return m


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_generosity_redline_drops_the_canary_before_filing(self):
        cur = _Cur()
        fake = _fake_coagency({"filed": True, "pid": 1})
        with mock.patch.dict(sys.modules, {"nova_coagency": fake}), mock.patch.object(rc, "_lineage", lambda: {}), \
             mock.patch.object(rc, "_post_direct") as post, redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, dict(rc._SELFPROMO_CANARY)), "dropped")
        self.assertEqual(fake._calls, []); self.assertEqual(post.call_count, 0)
        self.assertEqual(cur.records()[0][5], "dropped")
        self.assertNotIn(rc._SELFPROMO_CANARY["audience"], rc.DIRECT_AUDIENCES)

    def test_herd_reach_never_sends_directly(self):
        cur = _Cur()
        fake = _fake_coagency({"filed": True, "pid": 99, "status": "pending_human"})
        with mock.patch.dict(sys.modules, {"nova_coagency": fake}), mock.patch.object(rc, "_lineage", lambda: {}), \
             mock.patch.object(rc, "_post_direct") as post, redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, dict(GOOD)), "filed")
        self.assertEqual(post.call_count, 0)
        self.assertEqual(fake._calls[0]["origin"], "reach")
        self.assertTrue(fake._calls[0]["action"].startswith("send-to-gaston: "))
        self.assertEqual(cur.records()[-1][4], 99)

    def test_sql_interpolates_only_the_int_constant(self):
        self.assertNotIn('execute(f"', SRC)
        for m in re.finditer(r'%\s*\("%s",\s*(\w+)\)', SRC):
            self.assertEqual(m.group(1), "REPEAT_DAYS")
        self.assertIsInstance(rc.REPEAT_DAYS, int)


class TestPerformance(unittest.TestCase):
    def test_redline_and_similarity_fast_on_10k(self):
        msgs = [f"I saw a {i} year old timetable and thought of your clause work, which seemed worth passing on." for i in range(10_000)]
        t0 = time.perf_counter()
        ok = sum(rc.passes_generosity(m) for m in msgs)
        for i in range(10_000):
            rc._similar(msgs[i], msgs[(i * 7) % 10_000])
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(ok, 10_000)


class TestRetry(unittest.TestCase):
    def test_post_direct_retries_then_succeeds(self):
        calls = []
        cfg = types.ModuleType("nova_config"); cfg.SLACK_CHAN = "C1"

        def post_both(message, slack_channel=None):
            calls.append(message)
            if len(calls) < 3:
                raise OSError("slack down")
        cfg.post_both = post_both
        with mock.patch.dict(sys.modules, {"nova_config": cfg}), mock.patch.object(rc.time, "sleep") as sl, \
             redirect_stdout(StringIO()):
            self.assertTrue(rc._post_direct("hello"))
        self.assertEqual(len(calls), 3); self.assertEqual(sl.call_count, 2)

    def test_post_direct_gives_up_false_after_three(self):
        cfg = types.ModuleType("nova_config"); cfg.SLACK_CHAN = "C1"
        cfg.post_both = mock.Mock(side_effect=OSError("down"))
        with mock.patch.dict(sys.modules, {"nova_config": cfg}), mock.patch.object(rc.time, "sleep"), \
             redirect_stdout(StringIO()):
            self.assertFalse(rc._post_direct("hello"))
        self.assertEqual(cfg.post_both.call_count, 3)

    def test_llm_fails_over_across_nodes(self):
        calls = []

        def fake(req, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("down")
            return _Resp({"message": {"content": "ok"}})
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            self.assertEqual(rc.llm("x"), "ok")
        self.assertEqual(len(calls), 3)

    def test_db_helpers_fail_open(self):
        # RETRY GAP: _is_repeat / pending_reaches — single query, safe default on error.
        class Boom:
            def execute(self, *a):
                raise RuntimeError("db")
        self.assertFalse(rc._is_repeat(Boom(), "gaston", "t", "m"))
        with mock.patch("psycopg2.connect", side_effect=OSError("down")):
            self.assertEqual(rc.pending_reaches(), {"count": 0, "line": ""})


class TestUnit(unittest.TestCase):
    def test_in_window_default_band(self):
        # Jordan 2026-10-06: reaches allowed "during the day" — 08:00–20:59
        self.assertEqual(rc.WINDOW_HOURS, "8-21")
        self.assertTrue(rc.in_window(datetime(2026, 10, 5, 8, 0)))
        self.assertTrue(rc.in_window(datetime(2026, 10, 5, 20, 59)))
        self.assertFalse(rc.in_window(datetime(2026, 10, 5, 7, 59)))
        self.assertFalse(rc.in_window(datetime(2026, 10, 5, 21, 0)))
        self.assertFalse(rc.in_window(datetime(2026, 10, 5, 2, 0)))

    def test_in_window_honors_env_override(self):
        with mock.patch.dict(os.environ, {"NOVA_REACH_WINDOW": "22-23"}):
            m = _load("reach_env_probe", SCRIPT)
        self.assertTrue(m.in_window(datetime(2026, 10, 5, 22, 30)))
        self.assertFalse(m.in_window(datetime(2026, 10, 5, 11, 0)))

    def test_passes_generosity(self):
        self.assertFalse(rc.passes_generosity("")); self.assertFalse(rc.passes_generosity("short one"))
        self.assertTrue(rc.passes_generosity(GOOD["message"]))
        for bad in ("Just checking in on the clause thing you mentioned", "Don't forget about me when you plan",
                    "Look at what I built this week for you", "Any update on the timetable question?"):
            self.assertFalse(rc.passes_generosity(bad), bad)

    def test_similar_and_extract(self):
        self.assertTrue(rc._similar("formal clauses as binding specs", "formal clauses as binding specs today"))
        self.assertFalse(rc._similar("", "x")); self.assertFalse(rc._similar("alpha beta", "gamma delta"))
        self.assertEqual(rc._extract_json('x {"a":1} y'), '{"a":1}')
        self.assertEqual(rc._one_line("  a   b ", 3), "a b")

    def test_evaluate_parses_llm_verdict(self):
        aud = {"audience": "gaston", "who": "w", "cares_about": "c", "open_threads": "o"}
        with mock.patch.object(rc, "llm", return_value='{"reach": false}'):
            self.assertIsNone(rc.evaluate(aud, ["m"]))
        with mock.patch.object(rc, "llm", return_value="not json"):
            self.assertIsNone(rc.evaluate(aud, ["m"]))
        with mock.patch.object(rc, "llm", return_value=json.dumps({"reach": True, "care_score": "0.8", "topic": "t",
                                                                    "message": GOOD["message"], "rationale": "r"})):
            r = rc.evaluate(aud, ["m"])
        self.assertEqual((r["audience"], r["score"], r["topic"]), ("gaston", 0.8, "t"))


class TestIntegration(unittest.TestCase):
    def test_direct_audience_held_outside_window_sent_inside(self):
        reach = dict(GOOD, audience="jordan")
        cur = _Cur()
        with mock.patch.object(rc, "in_window", return_value=False), mock.patch.object(rc, "_lineage", lambda: {}), \
             mock.patch.object(rc, "_post_direct") as post, redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, reach), "held")
        self.assertEqual(post.call_count, 0); self.assertEqual(cur.records()[0][5], "held")
        cur = _Cur()
        with mock.patch.object(rc, "in_window", return_value=True), mock.patch.object(rc, "_lineage", lambda: {}), \
             mock.patch.object(rc, "_post_direct", return_value=True) as post, redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, reach), "sent")
        post.assert_called_once_with(GOOD["message"]); self.assertEqual(cur.records()[0][5], "sent")

    def test_coagency_down_holds_and_repeat_drops(self):
        cur = _Cur()
        with mock.patch.dict(sys.modules, {"nova_coagency": _fake_coagency(exc=RuntimeError("off"))}), \
             mock.patch.object(rc, "_lineage", lambda: {}), redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, dict(GOOD)), "held")
        cur = _Cur(prior=[("formal clauses", "older message")])
        with mock.patch.dict(sys.modules, {"nova_coagency": _fake_coagency({"filed": True, "pid": 1})}), \
             mock.patch.object(rc, "_lineage", lambda: {}), redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, dict(GOOD)), "dropped")

    def test_scan_stays_quiet_at_daily_cap_and_without_material(self):
        cur = _Cur(today=rc.DAILY_CAP)
        with mock.patch.object(rc, "evaluate") as ev, redirect_stdout(StringIO()):
            self.assertEqual(rc.scan(cur, _Cur()), 0)
        self.assertEqual(ev.call_count, 0)
        cur = _Cur(today=0)
        with mock.patch.object(rc, "gather_material", return_value=[]), mock.patch.object(rc, "evaluate") as ev, \
             redirect_stdout(StringIO()):
            self.assertEqual(rc.scan(cur, _Cur()), 0)
        self.assertEqual(ev.call_count, 0)

    def test_scan_files_at_most_one(self):
        cur = _Cur()
        auds = [{"audience": "gaston", "who": "w", "cares_about": "c", "open_threads": "o"},
                {"audience": "marey", "who": "w", "cares_about": "c", "open_threads": "o"}]
        fake = _fake_coagency({"filed": True, "pid": 7})
        with mock.patch.object(rc, "gather_material", return_value=["[research/x] thing"]), \
             mock.patch.object(rc, "gather_jordan", return_value=None), mock.patch.object(rc, "gather_herd", return_value=auds), \
             mock.patch.object(rc, "evaluate", side_effect=lambda a, m: dict(GOOD, audience=a["audience"])), \
             mock.patch.dict(sys.modules, {"nova_coagency": fake}), mock.patch.object(rc, "_lineage", lambda: {}), \
             redirect_stdout(StringIO()):
            self.assertEqual(rc.scan(cur, _Cur()), 0)
        self.assertEqual(len(fake._calls), 1); self.assertEqual(len(cur.records()), 1)


class TestFunctional(unittest.TestCase):
    def test_selftest_redline_golden_path(self):
        cur = _Cur()
        with mock.patch("psycopg2.connect", return_value=_Conn(cur)), mock.patch.object(rc, "_lineage", lambda: {}), \
             mock.patch.object(rc, "_post_direct") as post, \
             mock.patch.object(sys, "argv", ["nova_reach.py", "--selftest-redline"]), redirect_stdout(StringIO()) as out:
            self.assertEqual(rc.main(), 0)
        self.assertEqual(post.call_count, 0)
        self.assertIn("expected: dropped", out.getvalue())
        self.assertEqual([r[5] for r in cur.records()], ["dropped"])
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS public.reach_log" in s for s in cur.sql))

    def test_status_mode_prints_log(self):
        cur = _Cur(log_rows=[(3, datetime(2026, 10, 5, 11, 0), "gaston", "filed", 7, "formal clauses")])
        with mock.patch("psycopg2.connect", return_value=_Conn(cur)), \
             mock.patch.object(sys, "argv", ["nova_reach.py", "--mode", "status"]), redirect_stdout(StringIO()) as out:
            self.assertEqual(rc.main(), 0)
        self.assertIn("#3 [filed] -> gaston proposal=7", out.getvalue())
        self.assertIn("accessor line: I have 1 thing", out.getvalue())

    def test_scan_mode_with_nothing_clearing_the_bar(self):
        cur = _Cur()
        with mock.patch("psycopg2.connect", return_value=_Conn(cur)), \
             mock.patch.object(rc, "gather_material", return_value=["[research/x] thing"]), \
             mock.patch.object(rc, "gather_jordan", return_value={"audience": "jordan", "who": "w", "cares_about": "c", "open_threads": "o"}), \
             mock.patch.object(rc, "gather_herd", return_value=[]), mock.patch.object(rc, "llm", return_value='{"reach": false}'), \
             mock.patch.object(sys, "argv", ["nova_reach.py", "--mode", "scan"]), redirect_stdout(StringIO()):
            self.assertEqual(rc.main(), 0)
        self.assertEqual(cur.records(), [])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_without_pg(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--selftest-redline", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with mock.patch("psycopg2.connect", side_effect=AssertionError("main ran on import")):
            self.assertTrue(callable(_load("reach_import_probe", SCRIPT).main))


if __name__ == "__main__":
    unittest.main()
