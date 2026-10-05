#!/usr/bin/env python3
"""Tests for nova_unclaimed_time.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_unclaimed_time.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ut = _load("ut", SCRIPT)
SRC = SCRIPT.read_text()


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Cur:
    """Cursor stub: answers fetchone/fetchall by substring of the last SQL, records every execute."""
    def __init__(self, routes=None):
        self.routes = routes or []; self.sql = []; self.params = []; self._last = ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql
        for needle, val in self.routes:
            if needle in sql and isinstance(val, Exception):
                raise val

    def _route(self, default):
        for needle, val in self.routes:
            if needle in self._last:
                return val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur


NOTE = ("The escapement is the part of the clock that lets time out one tooth at a time; what I notice "
        "today is that my scheduler does the same thing with my own hours, and I had not seen the rhyme.\n"
        "NEXT: read about the deadbeat escapement and whether it maps to group:llm serialization.")


def _net(chat=NOTE, tasks=None, sched_fail=False, chat_fail=0):
    """urlopen stand-in covering the scheduler, ollama and the memory server."""
    calls = []

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        body = None if isinstance(req, str) or req.data is None else json.loads(req.data.decode())
        calls.append((url, body))
        if url.endswith("/tasks"):
            if sched_fail:
                raise OSError("no scheduler")
            return _Resp(tasks or {})
        if url.endswith("/api/chat"):
            if sum(u.endswith("/api/chat") for u, _ in calls) <= chat_fail:
                raise OSError("node down")
            return _Resp({"message": {"content": chat}})
        if url.endswith("/remember"):
            return _Resp({"id": 9})
        if "/recall?" in url:
            return _Resp({"memories": [{"text": "a fragment about escapements", "source": "book"}]})
        raise AssertionError(url)
    return urlopen, calls


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        cur = _Cur()
        evil = "x'); DROP TABLE pursuit_threads; --"
        ut._thread_save(cur, evil, "k", "note", None)
        ut._thread_load(cur, evil)
        for s in cur.sql:
            self.assertNotIn("DROP TABLE", s)
        self.assertEqual(cur.params[0][0], evil)

    def test_writes_are_her_own_tables_only(self):
        writes = set(re.findall(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC))
        self.assertEqual(writes, {"pursuit_threads", "preoccupations"})

    def test_private_outputs_are_marked_private(self):
        for tag in ('"privacy": "private"', '"audience": "none"', "private_notebook"):
            self.assertIn(tag, SRC)
        self.assertIn("scheduler_core", SRC.lower().replace("-", "_"))


class TestPerformance(unittest.TestCase):
    def test_loop_guard_and_yield_fast_on_10k(self):
        recent = [f"I'm keyed-up but even, parsing the Coaxial escapement {i}" for i in range(10_000)]
        tasks = {f"t{i}": {"group": "llm", "enabled": True, "next_run": 10_000 + i} for i in range(10_000)}
        t0 = time.perf_counter()
        looped = ut.looks_looped("I'm keyed-up but even, parsing the Coaxial escapement again", recent)
        fresh = ut.looks_looped("Something entirely different caught me today.", recent)
        tid = ut.should_yield(tasks, now=0, window_s=5)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertTrue(looped); self.assertFalse(fresh); self.assertIsNone(tid)

    def test_is_fizzle_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            ut.is_fizzle("Followed it for a while and honestly it petered out, nothing more to say here today." if i % 2 else "A" * 80)
        self.assertLess(time.perf_counter() - t0, 0.5)


class TestRetry(unittest.TestCase):
    # RETRY GAP: yield_to_scheduled() — one GET to the scheduler; fails open (None = her time is hers).
    def test_yield_fails_open_off_box(self):
        calls = []

        def boom(url, timeout=None):
            calls.append(url); raise OSError("connection refused")
        with mock.patch.object(ut.urllib.request, "urlopen", boom):
            self.assertIsNone(ut.yield_to_scheduled())
        self.assertEqual(calls, [f"{ut.SCHED_URL}/tasks"])

    def test_yield_with_scheduler_mocked(self):
        now = 1_000_000.0
        tasks = {"unclaimed_time": {"group": "llm", "running": True},
                 "digest": {"group": "llm", "enabled": True, "next_run": now + 30}}
        urlopen, _ = _net(tasks=tasks)
        with mock.patch.object(ut.urllib.request, "urlopen", urlopen), mock.patch("time.time", return_value=now):
            self.assertEqual(ut.yield_to_scheduled(), "digest")
        urlopen, _ = _net(tasks={"digest": {"group": "llm", "next_run": now + 9999}})
        with mock.patch.object(ut.urllib.request, "urlopen", urlopen), mock.patch("time.time", return_value=now):
            self.assertIsNone(ut.yield_to_scheduled())

    def test_llm_fails_over_across_nodes_then_succeeds(self):
        urlopen, calls = _net(chat="ok", chat_fail=2)
        with mock.patch.object(ut.urllib.request, "urlopen", urlopen):
            self.assertEqual(ut.llm("p"), "ok")
        self.assertEqual([u for u, _ in calls], [n + "/api/chat" for n in ut.OLLAMA_NODES[:3]])
        urlopen, calls = _net(chat="", chat_fail=99)
        with mock.patch.object(ut.urllib.request, "urlopen", urlopen):
            self.assertEqual(ut.llm("p"), "")
        self.assertEqual(len(calls), len(ut.OLLAMA_NODES))

    # RETRY GAP: remember() — one POST, no backoff and no catch; recall() fails open to [].
    def test_remember_raises_and_recall_fails_open(self):
        def boom(req, timeout=None):
            raise OSError("memory server down")
        with mock.patch.object(ut.urllib.request, "urlopen", boom):
            with self.assertRaises(OSError):
                ut.remember("t", "unclaimed", {})
            self.assertEqual(ut.recall("q"), [])


class TestUnit(unittest.TestCase):
    def test_detect_trigger(self):
        self.assertEqual(ut.detect_trigger([]), "manual")
        self.assertEqual(ut.detect_trigger(["--scheduled"]), "scheduled")
        self.assertEqual(ut.detect_trigger(["--trigger=gravel_resurface"]), "gravel_resurface")
        self.assertEqual(ut.detect_trigger(["--trigger", "demo", "--scheduled"]), "demo")
        self.assertEqual(ut.detect_trigger(["--trigger"]), "manual")

    def test_is_fizzle(self):
        self.assertTrue(ut.is_fizzle("short"))
        self.assertTrue(ut.is_fizzle("I sat with this for a long while and it quietly petered out, as these things do."))
        self.assertTrue(ut.is_fizzle("A" * 70 + " nothing"))
        self.assertFalse(ut.is_fizzle("A" * 70))

    def test_opener_normalises(self):
        self.assertEqual(ut._opener("[Private]  I’m   “here” — now"), "i'm \"here\" - now")
        self.assertEqual(ut._opener(None), "")
        self.assertEqual(len(ut._opener("x" * 100, 10)), 10)

    def test_looks_looped_edges(self):
        self.assertFalse(ut.looks_looped("", ["anything"]))
        self.assertFalse(ut.looks_looped("new", None))
        self.assertTrue(ut.looks_looped("[Private] Same opening sentence, different ending A", ["Same opening sentence, different ending B"]))
        self.assertFalse(ut.looks_looped("Short one", ["Short two"]))

    def test_split_next(self):
        self.assertEqual(ut._split_next("body\nNEXT: do the thing."), ("body", "do the thing"))
        self.assertEqual(ut._split_next("body\nNEXT: nothing"), ("body", None))
        self.assertEqual(ut._split_next("body only"), ("body only", None))
        self.assertEqual(ut._split_next(None), ("", None))
        self.assertEqual(len(ut._split_next("b\nNEXT: " + "x" * 500)[1]), 300)

    def test_should_yield_rules(self):
        now = 100.0
        self.assertIsNone(ut.should_yield({}, now))
        self.assertIsNone(ut.should_yield(None, now))
        self.assertIsNone(ut.should_yield({"unclaimed_time": {"group": "llm", "running": True}}, now))
        self.assertIsNone(ut.should_yield({"x": {"group": "llm", "enabled": False, "running": True}}, now))
        self.assertIsNone(ut.should_yield({"x": {"group": "io", "running": True}}, now))
        self.assertEqual(ut.should_yield({"x": {"gpu_heavy": True, "running": True}}, now), "x")
        self.assertEqual(ut.should_yield({"x": {"group": "llm", "next_run": now + 120}}, now, window_s=120), "x")
        self.assertIsNone(ut.should_yield({"x": {"group": "llm", "next_run": now + 121}}, now, window_s=120))
        self.assertIsNone(ut.should_yield({"x": {"group": "llm", "next_run": now - 1}}, now))   # overdue = not "due soon"

    def test_misc_helpers(self):
        self.assertEqual(ut._extract_json('x {"a": 1} y'), '{"a": 1}')
        self.assertEqual(ut._one_line("  a \n b  "), "a b")
        self.assertEqual(len(ut._one_line("w" * 400)), 240)
        self.assertEqual(ut._cand_label({"mode": "preoccupation", "topic": "horology"}), "horology")
        self.assertEqual(ut._cand_label({"mode": "thread", "src": "fishbowl"}), "thread from fishbowl")
        self.assertEqual(ut._cand_label({"mode": "tangent"}), "tangent from ?")


class TestIntegration(unittest.TestCase):
    def test_budget_module_is_imported_not_reimplemented(self):
        import nova_attention_budget
        self.assertIs(ut.budget, nova_attention_budget)
        self.assertNotIn("def cost_of", SRC)
        self.assertNotIn("def log_volition", SRC)

    def test_thread_round_trip_shape(self):
        cur = _Cur([("SELECT last_note, next_step, wakes FROM pursuit_threads", ("old note", None, 3))])
        self.assertEqual(ut._thread_load(cur, "horology"), {"last_note": "old note", "next_step": "(none set)", "wakes": 3})
        self.assertTrue(cur.sql[0].startswith("CREATE TABLE IF NOT EXISTS pursuit_threads"))
        body, nxt = ut._split_next(NOTE)
        ut._thread_save(cur, "horology", "craft", body, nxt)
        ins = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO pursuit_threads" in s][0]
        self.assertEqual(ins[:2], ("horology", "craft"))
        self.assertTrue(ins[3].startswith("read about the deadbeat escapement"))
        self.assertIsNone(ut._thread_load(_Cur(), "none"))

    def test_pick_pursuit_records_the_trade(self):
        oc = _Cur([("FROM preoccupations", [(5, "horology", "craft", "summary")])])
        mc = _Cur()
        urlopen, calls = _net(chat="It had the grip.")          # no losers -> plain one-sentence defense
        with mock.patch.object(ut.urllib.request, "urlopen", urlopen), \
             mock.patch.object(ut.budget, "spend") as spend, mock.patch.object(ut.budget, "remaining", return_value=11), \
             mock.patch.object(ut.budget, "log_volition") as logv, mock.patch("random.random", return_value=0.5), \
             mock.patch.dict(sys.modules, {"nova_tinkerer": mock.Mock(surface_friction=mock.Mock(return_value=None)),
                                           "nova_aspirations": mock.Mock(surface_aspiration=mock.Mock(return_value=None))}), \
             redirect_stdout(io.StringIO()):
            w = ut.pick_pursuit(oc, mc)
        self.assertEqual(w["mode"], "preoccupation")
        spend.assert_called_once_with(oc, 1)
        kw = logv.call_args.kwargs
        self.assertEqual((kw["chosen"], kw["chosen_mode"], kw["cost"], kw["budget_remaining"], kw["defense"]),
                         ("horology", "preoccupation", 1, 11, "It had the grip."))
        self.assertEqual(kw["alternatives_foreclosed"], [])
        self.assertEqual(kw["lineage"], f"{ut.TRIGGER}@{ut.TODAY}")


class TestFunctional(unittest.TestCase):
    def _main(self, oc, mc, urlopen, rolls):
        stubs = {"nova_tinkerer": mock.Mock(surface_friction=mock.Mock(return_value=None)),
                 "nova_aspirations": mock.Mock(surface_aspiration=mock.Mock(return_value=None))}
        with mock.patch.object(ut.psycopg2, "connect", side_effect=lambda dsn, **k: _Conn(mc if "nova_memories" in dsn else oc)) as pg, \
             mock.patch.object(ut.urllib.request, "urlopen", urlopen), \
             mock.patch("random.random", side_effect=rolls), mock.patch("random.choice", side_effect=lambda xs: xs[0]), \
             mock.patch.object(ut.budget, "spend"), mock.patch.object(ut.budget, "remaining", return_value=10), \
             mock.patch.object(ut.budget, "log_volition"), mock.patch.dict(sys.modules, stubs), \
             redirect_stdout(io.StringIO()) as out:
            rc = ut.main()
        return rc, out.getvalue(), pg

    def test_golden_path_develops_a_preoccupation(self):
        oc = _Cur([("FROM preoccupations", [(5, "horology", "craft", "summary")])])
        mc = _Cur()
        urlopen, calls = _net()
        rc, out, _ = self._main(oc, mc, urlopen, rolls=[0.99, 0.99] + [0.5] * 10)
        self.assertEqual(rc, 0)
        self.assertIn("developed preoccupation: horology (next: read about the deadbeat escapement", out)
        mem = [b for u, b in calls if u.endswith("/remember")]
        self.assertEqual(len(mem), 1)
        self.assertTrue(mem[0]["text"].startswith("[Unclaimed — horology] The escapement"))
        self.assertNotIn("NEXT:", mem[0]["text"])
        self.assertEqual((mem[0]["source"], mem[0]["metadata"]["type"], mem[0]["metadata"]["trigger"]),
                         ("unclaimed", "pursuit", ut.TRIGGER))
        upd = [p for s, p in zip(oc.sql, oc.params) if s.startswith("UPDATE preoccupations SET returns")]
        self.assertEqual(upd[0][1], 5)
        self.assertTrue(any("INSERT INTO pursuit_threads" in s for s in oc.sql))
        self.assertTrue(any("/recall?" in u for u, _ in calls))

    def test_yields_to_due_scheduled_task_before_touching_pg(self):
        now = 5_000.0
        urlopen, calls = _net(tasks={"digest": {"group": "llm", "next_run": now + 10}})
        with mock.patch("time.time", return_value=now):
            rc, out, pg = self._main(_Cur(), _Cur(), urlopen, rolls=[0.5] * 10)
        self.assertEqual(rc, 0)
        self.assertIn("yielding this run to scheduled task 'digest'", out)
        pg.assert_not_called()
        self.assertEqual(len(calls), 1)

    def test_quiet_wake_is_a_first_class_outcome(self):
        urlopen, calls = _net(chat="", sched_fail=True, chat_fail=99)      # nodes down -> canned quiet line
        rc, out, _ = self._main(_Cur(), _Cur(), urlopen, rolls=[0.99, 0.0])
        self.assertEqual(rc, 0)
        self.assertIn("quiet wake", out)
        mem = [b for u, b in calls if u.endswith("/remember")][0]
        self.assertTrue(mem["text"].startswith("[Unclaimed — quiet] "))
        self.assertEqual(mem["metadata"]["type"], "quiet")

    def test_error_path_llm_down_records_nothing(self):
        oc = _Cur([("FROM preoccupations", [(5, "horology", "craft", "summary")])])
        urlopen, calls = _net(chat="", sched_fail=True, chat_fail=99)
        rc, out, _ = self._main(oc, _Cur(), urlopen, rolls=[0.99, 0.99] + [0.5] * 10)
        self.assertEqual(rc, 0)
        self.assertIn("LLM returned nothing", out)
        self.assertFalse(any(u.endswith("/remember") for u, _ in calls))
        self.assertFalse(any(s.startswith("UPDATE") for s in oc.sql))


class TestFrame(unittest.TestCase):
    def test_import_smoke_exits_zero(self):
        # no --help/--selftest: the scheduler entry is positional flags only; import must be side-effect free
        r = subprocess.run([sys.executable, "-c", "import nova_unclaimed_time as u; assert u.TRIGGER == 'manual'"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        with mock.patch.object(ut.psycopg2, "connect", side_effect=AssertionError("main ran")), \
             mock.patch.object(ut.urllib.request, "urlopen", side_effect=AssertionError("network")):
            _load("ut_again", SCRIPT)


if __name__ == "__main__":
    unittest.main()
