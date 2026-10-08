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

    def test_reply_text_stored_as_param_not_sql(self):
        ops = _Cur(fetchall=[[(5, date(2026, 10, 4), [{"text": "a"}], "C", "1.2")]])
        evil = "1 yes'); DROP TABLE nova_care_checkins;--"
        with mock.patch.object(cc, "remember", return_value="m1"):
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

    def test_gather_is_capped(self):
        ops = _Cur(fetchall=[[("a", "warning", 9), ("b", "critical", 3)]], fetchone=[("t", "m")])
        mem = _Cur(fetchall=[[("s", "x")] * 1000])
        self.assertLessEqual(len(cc.gather(ops, mem)), cc.MAX_ITEMS)


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

    def test_slack_post_retried_on_transient_then_recorded(self):
        ops = _Cur(fetchall=[[("a", "warning", 2)]], fetchone=[None, None])
        nsa = _nsa(post=[OSError("timeout"), {"ok": False, "error": "ratelimited"}, {"ok": True, "ts": "9.9"}])
        rc, _ = _run(cc.ask, False, ops=ops, mem=_Cur(), nsa=nsa)
        self.assertEqual(rc, 0)
        self.assertEqual(nsa.slack.call_count, 3)
        self.assertTrue(any(s.startswith("INSERT INTO nova_care_checkins") for s, _ in ops.sql))

    def test_slack_permanent_error_not_retried(self):
        ops = _Cur(fetchall=[[("a", "warning", 2)]], fetchone=[None, None])
        nsa = _nsa(post={"ok": False, "error": "channel_not_found"})
        rc, out = _run(cc.ask, False, ops=ops, mem=_Cur(), nsa=nsa)
        self.assertEqual(rc, 1)
        self.assertEqual(nsa.slack.call_count, 1)
        self.assertIn("post failed: channel_not_found", out)
        self.assertFalse(any(s.startswith("INSERT") for s, _ in ops.sql))

    def test_slack_exhausted_returns_1_no_row(self):
        ops = _Cur(fetchall=[[("a", "warning", 2)]], fetchone=[None, None])
        nsa = _nsa(post=[OSError("x")] * 3)
        rc, out = _run(cc.ask, False, ops=ops, mem=_Cur(), nsa=nsa)
        self.assertEqual(rc, 1)
        self.assertFalse(any(s.startswith("INSERT") for s, _ in ops.sql))

    def test_memories_db_down_still_asks(self):
        ops = _Cur(fetchall=[[("a", "warning", 2)]], fetchone=[None, None])
        rc, out = _run(cc.ask, True, ops=ops, mem=RuntimeError("mem pg down"))
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

    def test_summarize_caps_reply(self):
        s = cc.summarize(date(2026, 10, 11), [{"text": "a"}], "z" * 2000, {})
        self.assertIn("a -> no verdict", s)
        self.assertLess(len(s), 700)


class TestIntegration(unittest.TestCase):
    def test_ask_is_idempotent_per_week(self):
        ops = _Cur(fetchone=[(1,)])
        nsa = _nsa()
        rc, out = _run(cc.ask, False, ops=ops, mem=_Cur(), nsa=nsa)
        self.assertEqual(rc, 0)
        nsa.slack.assert_not_called()
        self.assertIn("already asked", out)

    def test_harvest_skips_unanswered(self):
        ops = _Cur(fetchall=[[(5, date(2026, 10, 4), [{"text": "a"}], "C", "1.2")]])
        with mock.patch.object(cc, "remember") as rem:
            rc, out = _run(cc.harvest, False, ops=ops, nsa=_nsa(answer=(None, None)))
        rem.assert_not_called()
        self.assertIn("harvested 0", out)


class TestFunctional(unittest.TestCase):
    def test_ask_golden_path(self):
        ops = _Cur(fetchall=[[("Disk low", "warning", 4)]], fetchone=[None, ("rail", "found it")])
        mem = _Cur(fetchall=[[("local", "Watchman")]])
        nsa = _nsa()
        rc, _ = _run(cc.ask, False, ops=ops, mem=mem, nsa=nsa)
        self.assertEqual(rc, 0)
        kw = nsa.slack.call_args[1]
        self.assertEqual(kw["channel"], cc.CHANNEL)
        self.assertIn("1. warning alert", kw["text"])
        ins = [p for s, p in ops.sql if s.startswith("INSERT")][0]
        self.assertEqual(ins[-1], "111.2")

    def test_harvest_golden_path(self):
        ops = _Cur(fetchall=[[(5, date(2026, 10, 4), [{"text": "a"}, {"text": "b"}], "C", "1.2")]])
        with mock.patch.object(cc, "remember", return_value="mem9") as rem:
            rc, out = _run(cc.harvest, False, ops=ops, nsa=_nsa(answer=("1 yes 2 no", "yes")))
        self.assertEqual(rem.call_args[0][1]["verdicts"], {"1": "helped", "2": "noise"})
        upd = [p for s, p in ops.sql if s.startswith("UPDATE")][0]
        self.assertEqual(upd[2:], ("mem9", 5))
        self.assertIn("harvested 1", out)

    def test_dry_run_sends_and_writes_nothing(self):
        ops = _Cur(fetchall=[[("a", "warning", 2)]], fetchone=[None])
        nsa = _nsa()
        rc, out = _run(cc.ask, True, ops=ops, mem=_Cur(), nsa=nsa)
        nsa.slack.assert_not_called()
        self.assertFalse(any(s.startswith(("INSERT", "CREATE")) for s, _ in ops.sql))
        self.assertIn("Little Mister", out)

    def test_nothing_happened_no_post(self):
        nsa = _nsa()
        rc, out = _run(cc.ask, False, ops=_Cur(fetchone=[None, None]), mem=_Cur(), nsa=nsa)
        nsa.slack.assert_not_called()
        self.assertIn("no check-in", out)


class TestFrame(unittest.TestCase):
    def test_selftest_and_help(self):
        env = dict(os.environ, NOVA_TEST_QUIET="1")
        for args in (["--selftest"], ["--help"]):
            r = subprocess.run([sys.executable, str(SCRIPTS / "nova_care_checkin.py"), *args],
                               capture_output=True, text=True, timeout=30, cwd=SCRIPTS, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
