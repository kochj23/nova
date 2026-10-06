#!/usr/bin/env python3
"""Tests for nova_unclaimed_digest.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_unclaimed_digest.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nud_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ud = _load()
# stub every outbound side effect at module load (LLM, Hugo publish, git push, Slack, image gen)
ud.nj = MagicMock()
ud.nj.today_str.return_value = "2026-01-01"
ud.nova_voice = MagicMock()
ud.nova_voice.system_prompt.side_effect = lambda ctx: "SYS:" + ctx


class _Cur:
    """SQL-substring routed cursor. `tables` = which to_regclass lookups exist; `answers` maps a
    substring of the query to its fetchall() rows."""
    def __init__(self, tables=(), answers=None, fail=()):
        self.tables = set(tables); self.answers = answers or {}; self.fail = fail
        self.sql = []; self._rows = []; self._one = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if any(f in sql for f in self.fail):
            raise RuntimeError("relation does not exist")
        if "to_regclass" in sql:
            self._one = (params[0] if params[0] in self.tables else None,)
            return
        self._rows = next((v for k, v in self.answers.items() if k in sql), [])

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


def _mem_rows(n_pursuits=3, n_quiet=0):
    rows = [("pursuit", "passion", f"topic{i}", f"I chased idea {i} " * 10) for i in range(n_pursuits)]
    rows += [("quiet", None, None, "nothing") for _ in range(n_quiet)]
    return rows


def _run(mem_rows, oc=None, raw="TITLE: A Day Off\n\n" + "word " * 3100, publish=True):
    mc = _Cur(answers={"source='unclaimed'": mem_rows})
    oc = oc or _Cur(answers={"preoccupations": [("jazz", "passion", 3)]})
    ud.nj.call_openrouter.return_value = raw
    ud.nj.publish_hugo.return_value = publish
    mem, ops = MagicMock(), MagicMock()
    mem.cursor.return_value = mc; ops.cursor.return_value = oc
    with patch.object(ud.psycopg2, "connect", side_effect=[mem, ops]):
        return ud.main()


class _Base(unittest.TestCase):
    def setUp(self):
        ud.nj.reset_mock(return_value=False, side_effect=False)
        ud.nj.today_str.return_value = "2026-01-01"


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_read_only_and_parameterized_table_probe(self):
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE \w+ SET|DELETE FROM)\b", SRC))
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        oc = _Cur(tables={"public.x"})
        self.assertTrue(ud._has(oc, "public.x"))
        self.assertEqual(oc.sql[0], ("SELECT to_regclass(%s)", ("public.x",)))


class TestPerformance(_Base):
    def test_extend_is_bounded_to_4_continuations(self):
        ud.nj.call_openrouter.return_value = "more " * 100
        t0 = time.perf_counter()
        body = ud._extend_to_length("sys", "start", 10_000)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(ud.nj.call_openrouter.call_count, 4)
        self.assertEqual(len(body.split()), 1 + 400)

    def test_organ_gather_10k_rows(self):
        oc = _Cur(tables={"public.feature_wishes"}, answers={"feature_wishes": [(f"w{i}", "why") for i in range(10_000)]})
        t0 = time.perf_counter()
        self.assertEqual(len(ud.gather_organ_activity(oc, _Cur())), 10_000)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(_Base):
    def test_continuation_stops_on_thin_reply(self):
        # RETRY GAP: _extend_to_length()/call_openrouter — a too-short continuation ends the loop (no retry)
        ud.nj.call_openrouter.return_value = "Sure, here you go"
        self.assertEqual(ud._extend_to_length("s", "body", 3000), "body")
        self.assertEqual(ud.nj.call_openrouter.call_count, 1)

    def test_llm_empty_aborts(self):
        self.assertEqual(_run(_mem_rows(), raw=""), 1)
        ud.nj.publish_hugo.assert_not_called()

    def test_missing_organ_tables_fail_soft(self):
        oc = _Cur(tables={"public.letting_go_log"}, fail=("letting_go_log", "turing_scoreboard"))
        self.assertEqual(ud.gather_organ_activity(oc, _Cur()), [])


class TestUnit(_Base):
    def test_strip_preamble(self):
        self.assertEqual(ud._strip_preamble("Sure, continuing.\n---\n\nReal text"), "Real text")
        self.assertEqual(ud._strip_preamble(""), "")
        self.assertEqual(ud._strip_preamble("Body stays"), "Body stays")

    def test_calibration_leash_and_trust(self):
        oc = _Cur(tables={"public.autonomy_trust"},
                  answers={"turing_scoreboard": [("0.31",)],
                           "autonomy_trust": [("restart", 5, 0, True), ("prune", 2, 1, False)]})
        items = ud.gather_organ_activity(oc, _Cur())
        self.assertTrue(any(i.startswith("[EARNED STANDING AUTONOMY] I can now do 'restart'") for i in items))
        self.assertTrue(any("a veto reset my streak" in i for i in items))
        self.assertTrue(any(i.startswith("[THE LEASH I HAVEN'T SLIPPED]") for i in items))

    def test_autonomy_ledger_verbs(self):
        oc = _Cur(tables={"public.autonomy_ledger"},
                  answers={"autonomy_ledger": [("actor", "L1", "restart", "svc", True, True, "up")]})
        self.assertIn("[SELF-HEALED · L1] restart svc — verified up: up", ud.gather_organ_activity(oc, _Cur()))


class TestIntegration(_Base):
    def test_organ_actions_count_toward_quiet_gate(self):
        oc = _Cur(tables={"public.feature_wishes"},
                  answers={"feature_wishes": [("a", "x"), ("b", "y")], "preoccupations": []})
        self.assertEqual(_run(_mem_rows(1), oc=oc), 0)
        ud.nj.publish_hugo.assert_called_once()
        ctx = ud.nova_voice.system_prompt.call_args[0][0]
        self.assertIn("[WISHED FOR] a — x", ctx)


class TestFunctional(_Base):
    def test_golden_path_publishes(self):
        self.assertEqual(_run(_mem_rows(3, n_quiet=2)), 0)
        args, kw = ud.nj.publish_hugo.call_args
        self.assertEqual((args[0], args[2], kw["emoji"]), ("A Day Off", "operations", "🌱"))
        ud.nj.git_push.assert_called_once_with("operations", "A Day Off")
        self.assertIn("2 wake(s) went nowhere", ud.nova_voice.system_prompt.call_args[0][0])

    def test_quiet_day_publishes_nothing(self):
        self.assertEqual(_run(_mem_rows(1)), 0)
        ud.nj.call_openrouter.assert_not_called()
        self.assertEqual(_run(_mem_rows(3), raw="QUIET_DAY: nothing much"), 0)
        ud.nj.publish_hugo.assert_not_called()

    def test_guard_rejection_fails_run(self):
        self.assertEqual(_run(_mem_rows(3), publish=False), 1)
        ud.nj.git_push.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help: any invocation queries PG and may publish, so the frame check is an import smoke
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_unclaimed_digest"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
