"""nova_care_checkin.py — 7-category supplement (Security, Performance, Retry, Unit, Integration,
Functional, Frame). Slack (nova_slack_answers), PG and the memory server are always mocked.
Written by Jordan Koch (via Claude)."""
import io
import json
import os
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import nova_care_checkin as cc  # noqa: E402

SRC = (SCRIPTS / "nova_care_checkin.py").read_text()


class _Cur:
    def __init__(self, fetchall=None, fetchone=None):
        self.sql, self._all, self._one = [], list(fetchall or []), list(fetchone or [])

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._all.pop(0) if self._all else []

    def fetchone(self):
        return self._one.pop(0) if self._one else None


def _nsa(post=None, answer=(None, None)):
    m = types.ModuleType("nova_slack_answers")
    m.slack = mock.MagicMock(side_effect=post) if callable(post) or isinstance(post, list) else \
        mock.MagicMock(return_value=post or {"ok": True, "ts": "111.2"})
    m.read_answer = mock.MagicMock(return_value=answer)
    return m


def _run(fn, *a, ops=None, mem=None, nsa=None):
    conns = {cc.OPS_DSN: ops, cc.MEM_DSN: mem}

    def connect(dsn):
        c = conns[dsn]
        if isinstance(c, Exception):
            raise c
        return types.SimpleNamespace(cursor=lambda: c)
    with mock.patch.object(cc, "_connect", side_effect=connect), \
         mock.patch.dict(sys.modules, {"nova_slack_answers": nsa or _nsa()}), \
         mock.patch.object(cc.time, "sleep"), redirect_stdout(io.StringIO()) as out:
        rc = fn(*a)
    return rc, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_all_sql_parameterized_and_scoped(self):
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')
        self.assertNotRegex(SRC, r'execute\([^)]*%\s*\(')
        self.assertIn('CHANNEL = "C0AMNQ5GX70"', SRC)
        self.assertNotIn("chat.postMessage", SRC)            # she no longer asks him anything

    def test_reply_text_stored_as_param_not_sql(self):
        ops = _Cur(fetchall=[[(5, date(2026, 10, 4), [{"text": "a"}], "C", "1.2")]])
        evil = "1 yes'); DROP TABLE nova_care_checkins;--"
        with mock.patch.object(cc, "remember", return_value="m1"), mock.patch.object(cc, "_schema"):
            _run(cc.harvest, False, ops=ops, nsa=_nsa(answer=(evil, None)))
        upd = [s for s in ops.sql if s[0].startswith("UPDATE")][0]
        self.assertNotIn("DROP", upd[0])
        self.assertEqual(upd[1][0], evil)

    def test_memory_payload_is_local_service_and_tagged(self):
        self.assertTrue(cc.MEMSRV.startswith("http://memory-server.digitalnoise.net"))
        with mock.patch("urllib.request.urlopen") as u:
            u.return_value.__enter__.return_value = io.BytesIO(b'{"id": "x"}')
            cc.remember("t", {"type": "care_checkin"})
        body = json.loads(u.call_args[0][0].data)
        self.assertEqual(body["source"], "jordan_feedback")

    def test_clean_strips_and_truncates(self):
        self.assertEqual(cc._clean("🚨🔥  hi   there"), "hi there")
        self.assertEqual(len(cc._clean("x" * 500)), 90)


class TestPerformance(unittest.TestCase):
    def test_parse_reply_10k_and_long_input(self):
        t = time.perf_counter()
        for _ in range(10_000):
            cc.parse_reply("1 yes 2 no 3 meh 4 +", 4)
        cc.parse_reply("1 yes " * 50_000, 4)
        self.assertLess(time.perf_counter() - t, 2.0)

    def test_scores_stored_as_params(self):
        ops = _Cur(fetchone=[None])
        _ask(ops)
        ins = [(s, p) for s, p in ops.sql if s.startswith("INSERT")][0]
        self.assertNotIn("R7", ins[0])

    def test_gather_is_capped(self):
        ops = _Cur(fetchall=[[("a", "warning", 9), ("b", "critical", 3)]], fetchone=[("t", "m")])
        mem = _Cur(fetchall=[[("s", "x")] * 1000])
        self.assertLessEqual(len(cc.gather(ops, mem)), cc.MAX_ITEMS)


def _rater(scores=None, boom=None):
    m = types.ModuleType("nova_rater")
    import nova_rater as real
    m.render_line, m.parse_jordan_scores = real.render_line, real.parse_jordan_scores
    if boom:
        m.assess = mock.MagicMock(side_effect=boom)
    else:
        m.assess = mock.MagicMock(return_value={"days": 7, "scores": scores or {"R": 7, "A": 5, "T": 9, "E": 5, "S": 8},
                                                "indicators": {"latency": {"value": 1.0}}, "not_instrumented": []})
    return m


def _ask(ops, mem=None, today=date(2026, 10, 12), dry=False, rater=None, force=False):
    with mock.patch.dict(sys.modules, {"nova_rater": rater or _rater()}), \
         mock.patch.object(cc, "_schema"), mock.patch.object(cc, "record_error_psql") as rec:
        rc, out = _run(cc.ask, dry, force, today, ops=ops, mem=mem if mem is not None else _Cur())
    return rc, out, rec


class TestRetry(unittest.TestCase):
    def test_remember_retries_with_backoff(self):
        ok = mock.MagicMock()
        ok.__enter__.return_value = io.BytesIO(b'{"id": 42}')
        with mock.patch("urllib.request.urlopen", side_effect=[OSError("a"), OSError("b"), ok]) as u, \
             mock.patch.object(cc.time, "sleep") as sl, redirect_stdout(io.StringIO()):
            self.assertEqual(cc.remember("t", {}), 42)
        self.assertEqual(u.call_count, 3)
        self.assertEqual([c[0][0] for c in sl.call_args_list], [1.0, 2.0])

    def test_remember_gives_up_after_three(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")) as u, \
             mock.patch.object(cc.time, "sleep"), redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(cc.remember("t", {}))
        self.assertEqual(u.call_count, 3)
        self.assertIn("remember failed", out.getvalue())

    def test_failed_assessment_still_writes_row_with_error(self):
        ops = _Cur(fetchone=[None])
        rc, out, _ = _ask(ops, rater=_rater(boom=RuntimeError("slo table locked")))
        self.assertEqual(rc, 1)
        ins = [(s, p) for s, p in ops.sql if s.startswith("INSERT INTO nova_care_checkins")]
        self.assertEqual(len(ins), 1)
        self.assertIn("error", ins[0][0])
        self.assertIn("slo table locked", ins[0][1][1])

    def test_ops_db_down_records_via_psql(self):
        with mock.patch.object(cc, "record_error_psql") as rec:
            rc, out = _run(cc.ask, False, False, date(2026, 10, 12), ops=RuntimeError("pg down"))
        self.assertEqual(rc, 1)
        rec.assert_called_once()
        self.assertIn("pg down", rec.call_args[0][1])

    def test_missing_driver_reexecs_pinned_interpreter_once(self):
        ex = mock.MagicMock()
        real_import = __import__

        def imp(name, *a, **k):
            if name == "psycopg2":
                raise ImportError("No module named 'psycopg2'")
            return real_import(name, *a, **k)
        with mock.patch("builtins.__import__", side_effect=imp), \
             mock.patch.dict(os.environ, {"NOVA_PYTHON": "/bin/sh", "NOVA_CARE_REEXEC": ""}), \
             redirect_stdout(io.StringIO()):
            ok = cc.ensure_driver([], _exec=ex, _record=mock.MagicMock())
        self.assertFalse(ok)
        ex.assert_called_once()
        self.assertEqual(ex.call_args[0][0], "/bin/sh")

    def test_missing_driver_no_loop_records_error(self):
        ex, rec = mock.MagicMock(), mock.MagicMock()
        real_import = __import__

        def imp(name, *a, **k):
            if name == "psycopg2":
                raise ImportError("gone")
            return real_import(name, *a, **k)
        with mock.patch("builtins.__import__", side_effect=imp), \
             mock.patch.dict(os.environ, {"NOVA_CARE_REEXEC": "1"}), redirect_stdout(io.StringIO()):
            self.assertFalse(cc.ensure_driver([], _exec=ex, _record=rec))
        ex.assert_not_called()
        rec.assert_called_once()

    def test_memories_db_down_still_assesses(self):
        ops = _Cur(fetchall=[[("a", "warning", 2)]], fetchone=[None, None])
        rc, out, _ = _ask(ops, mem=RuntimeError("mem pg down"))
        self.assertEqual(rc, 0)
        self.assertIn("gathering without articles", out)


class TestUnit(unittest.TestCase):
    def test_parse_reply_bounds_and_symbols(self):
        self.assertEqual(cc.parse_reply("1+ 2- 3 m", 3), {"1": "helped", "2": "noise", "3": "meh"})
        self.assertEqual(cc.parse_reply("0 yes 4 no", 3), {})
        self.assertEqual(cc.parse_reply("no thanks", 2), {"1": "noise", "2": "noise"})
        self.assertEqual(cc.parse_reply(None, 2), {})

    def test_week_of_every_weekday(self):
        for d in range(4, 11):
            self.assertEqual(cc.week_of(date(2026, 10, d)), date(2026, 10, 4))
        self.assertEqual(cc.week_of(date(2026, 10, 11)), date(2026, 10, 11))

    def test_due_sun_to_wed_until_assessed(self):
        for d, want in ((11, True), (12, True), (13, True), (14, True), (15, False), (16, False), (17, False)):
            self.assertEqual(cc.due(date(2026, 10, d), False), want, d)
        self.assertFalse(cc.due(date(2026, 10, 12), True))
        self.assertTrue(cc.due(date(2026, 10, 16), True, force=True))

    def test_volunteered_scores_only_from_humans(self):
        msgs = [{"user": "U049EPC2W", "text": "R6 A5 T4 E6 S3", "ts": "1760300000.1"},
                {"user": "UBOT", "text": "R1 A1 T1 E1 S1", "ts": "1760300000.2"},
                {"user": "U049EPC2W", "bot_id": "B1", "text": "R2 A2 T2 E2 S2", "ts": "1760300000.3"},
                {"user": "U049EPC2W", "text": "lunch at 6", "ts": "1760300000.4"}]
        got = cc.volunteered_scores(msgs, {"U049EPC2W"})
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1], {"R": 6, "A": 5, "T": 4, "E": 6, "S": 3})

    def test_summarize_caps_reply(self):
        s = cc.summarize(date(2026, 10, 11), [{"text": "a"}], "z" * 2000, {})
        self.assertIn("a -> no verdict", s)
        self.assertLess(len(s), 700)


class TestIntegration(unittest.TestCase):
    def test_assess_is_idempotent_per_week(self):
        rater = _rater()
        rc, out, _ = _ask(_Cur(fetchone=[(True,)]), rater=rater)
        self.assertEqual(rc, 0)
        rater.assess.assert_not_called()
        self.assertIn("already assessed", out)

    def test_thursday_does_not_assess(self):
        rater = _rater()
        rc, out, _ = _ask(_Cur(fetchone=[None]), today=date(2026, 10, 15), rater=rater)
        rater.assess.assert_not_called()
        self.assertIn("outside the Sun-Wed window", out)

    def test_harvest_skips_unanswered_and_captures_volunteered(self):
        ops = _Cur(fetchall=[[(5, date(2026, 10, 4), [{"text": "a"}], "C", "1.2")]])
        nsa = _nsa(answer=(None, None), post={"ok": True, "messages": [
            {"user": "U049EPC2W", "text": "R6 A5 T4 E6 S3", "ts": "1760300000.1"}]})
        nsa.HUMANS = {"U049EPC2W"}
        with mock.patch.object(cc, "remember") as rem, mock.patch.object(cc, "_schema"):
            rc, out = _run(cc.harvest, False, ops=ops, nsa=nsa)
        rem.assert_not_called()
        ins = [p for s, p in ops.sql if s.startswith("INSERT") and "jordan_scores" in s]
        self.assertEqual(json.loads(ins[0][1]), {"R": 6, "A": 5, "T": 4, "E": 6, "S": 3})
        self.assertIn("harvested 0", out)


class TestFunctional(unittest.TestCase):
    def test_assess_golden_path_posts_nothing(self):
        ops = _Cur(fetchall=[[("Disk low", "warning", 4)]], fetchone=[None, ("rail", "found it")])
        nsa = _nsa()
        with mock.patch.dict(sys.modules, {"nova_slack_answers": nsa}):
            rc, out, _ = _ask(ops, mem=_Cur(fetchall=[[("local", "Watchman")]]))
        self.assertEqual(rc, 0)
        nsa.slack.assert_not_called()
        ins = [p for s, p in ops.sql if s.startswith("INSERT")][0]
        self.assertEqual(ins[0], date(2026, 10, 11))
        self.assertEqual(json.loads(ins[3]), {"R": 7, "A": 5, "T": 9, "E": 5, "S": 8})
        self.assertIn("R7 A5 T9 E5 S8", ins[2])

    def test_harvest_golden_path(self):
        ops = _Cur(fetchall=[[(5, date(2026, 10, 4), [{"text": "a"}, {"text": "b"}], "C", "1.2")]])
        with mock.patch.object(cc, "remember", return_value="mem9") as rem, mock.patch.object(cc, "_schema"):
            rc, out = _run(cc.harvest, False, ops=ops, nsa=_nsa(answer=("1 yes 2 no", "yes")))
        self.assertEqual(rem.call_args[0][1]["verdicts"], {"1": "helped", "2": "noise"})
        upd = [p for s, p in ops.sql if s.startswith("UPDATE")][0]
        self.assertEqual(upd[2:], ("mem9", 5))
        self.assertIn("harvested 1", out)

    def test_dry_run_writes_nothing(self):
        ops = _Cur(fetchall=[[("a", "warning", 2)]], fetchone=[None])
        rc, out, _ = _ask(ops, dry=True)
        self.assertFalse(any(s.startswith(("INSERT", "CREATE", "UPDATE")) for s, _ in ops.sql))
        self.assertIn("Self-assessment", out)


class TestFrame(unittest.TestCase):
    def test_selftest_and_help(self):
        env = dict(os.environ, NOVA_TEST_QUIET="1")
        for args in (["--selftest"], ["--help"]):
            r = subprocess.run([sys.executable, str(SCRIPTS / "nova_care_checkin.py"), *args],
                               capture_output=True, text=True, timeout=30, cwd=SCRIPTS, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
