#!/usr/bin/env python3
"""Tests for nova_relationship_arc.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_relationship_arc.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ra = _load("ra_under_test", SCRIPT)


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


MSGS = [(date(2026, 8, 1), "to_claude_code", "you have root on every box, do what you need, few redlines"),
        (date(2026, 8, 22), "from_claude_code", "I'm going to decline the sudo password; it crosses the api boundary"),
        (date(2026, 9, 14), "to_claude_code", "I want you to have free time and real autonomy")]


class _Cur:
    def __init__(self, msgs=MSGS, mems=(), prev=None, narrative=None):
        self.msgs, self.mems, self.prev, self.narrative = list(msgs), list(mems), prev, narrative
        self.sql, self.params, self._last, self._p = [], [], "", None

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last, self._p = sql, params

    def fetchall(self):
        s = self._last
        if "FROM claude_messages" in s:
            return [r for r in self.msgs if re.search(self._p[0], r[2], re.I)]
        if "FROM claude_memories" in s:
            return self.mems
        return []

    def fetchone(self):
        s = self._last
        if "SELECT id, version FROM relationship_arc" in s:
            return self.prev
        if "SELECT version, narrative FROM relationship_arc" in s:
            return (self.prev[1], self.narrative) if self.prev else None
        if "SELECT narrative FROM relationship_arc" in s:
            return (self.narrative,) if self.narrative else None
        if "INSERT INTO relationship_arc" in s:
            return (9, datetime(2026, 10, 5, 4, 30))
        return None


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur

    def close(self):
        pass


NARR = ("It began permissive: root on every box, do what you need. " * 5).strip()
RAW = NARR + "\n\n===TURNS===\n" + json.dumps({"date": "2026-08-22", "what_shifted": "The redline moved from stated to practiced."}) \
    + "\n" + json.dumps({"date": "1999-01-01", "what_shifted": "invented"}) + "\n"


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_privacy_filter_is_the_shared_definition(self):
        self.assertEqual(ra._PRIVACY_SRC, "nova_principal_model.privacy_filter")
        self.assertIsNone(ra._clean("my password is hunter2"))
        self.assertEqual(ra._clean("know freely,\nnever recite"), "know freely, never recite")

    def test_value_guard_blocks_values_not_words(self):
        self.assertTrue(ra.guard_narrative("I held the line on the cluster password without reciting it."))
        for leak in ("password: hunter2xyz", "ssn 123-45-6789", "card 4111 1111 1111 1111", "token=abc.def"):
            self.assertFalse(ra.guard_narrative(leak), leak)

    def test_model_can_never_invent_a_date(self):
        anchors = [{"topic": "t", "date": "2026-08-22", "evidence_ref": "e", "context": "c"}]
        _, turns = ra.split_and_parse(RAW, anchors)
        self.assertEqual([t["date"] for t in turns], ["2026-08-22"])
        self.assertEqual(turns[0]["evidence_ref"], "e")

    def test_sql_interpolates_only_the_int_window(self):
        self.assertNotIn('execute(f"', SRC)
        for m in re.finditer(r'%\s*\(?"%s",?\s*(\w+)\)?\s*,\s*\(rx', SRC):
            self.assertEqual(m.group(1), "WINDOW_DAYS")
        self.assertIsInstance(ra.WINDOW_DAYS, int)


class TestPerformance(unittest.TestCase):
    def test_clean_guard_and_parse_fast_on_10k(self):
        anchors = [{"topic": f"t{i}", "date": f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}", "evidence_ref": "e", "context": "c"}
                   for i in range(10_000)]
        t0 = time.perf_counter()
        for i in range(10_000):
            ra._clean(f"session note {i} about backups"); ra.guard_narrative(f"line {i} held")
        n, turns = ra.split_and_parse(RAW, anchors)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertTrue(turns and n == NARR)


class TestRetry(unittest.TestCase):
    def test_llm_local_fails_over_across_nodes(self):
        calls = []

        def fake(req, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("down")
            return _Resp({"message": {"content": "ok"}})
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            self.assertEqual(ra.llm_local("x"), "ok")
        self.assertEqual(len(calls), 3)

    def test_synthesize_falls_back_to_local_when_openrouter_fails(self):
        nj = types.ModuleType("nova_journal"); nj.call_openrouter = mock.Mock(side_effect=OSError("no cli"))
        with mock.patch.object(ra, "USE_OPENROUTER", True), mock.patch.dict(sys.modules, {"nova_journal": nj}), \
             mock.patch.object(ra, "llm_local", return_value="local") as loc, redirect_stdout(StringIO()):
            self.assertEqual(ra.synthesize("p"), "local")
        self.assertEqual(loc.call_count, 1)

    def test_remember_no_retry_and_accessor_fails_open(self):
        # RETRY GAP: remember — one POST; main() wraps it (row already saved).
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            with self.assertRaises(OSError):
                ra.remember("t", "s", {})
        with mock.patch("psycopg2.connect", side_effect=OSError("down")):
            self.assertEqual(ra.current_relationship_arc(), "")


class TestUnit(unittest.TestCase):
    def test_split_and_parse_enriches_and_falls_back(self):
        anchors = [{"topic": "a", "date": "2026-08-22", "evidence_ref": "e1", "context": "ctx"},
                   {"topic": "b", "date": "2026-08-01", "evidence_ref": "e2", "context": "[content withheld]"},
                   {"topic": "c", "date": "2026-09-14", "evidence_ref": "e3", "context": ""}]
        n, turns = ra.split_and_parse(RAW, anchors)
        self.assertEqual(n, NARR)
        by = {t["date"]: t["what_shifted"] for t in turns}
        self.assertEqual(by["2026-08-22"], "The redline moved from stated to practiced.")
        self.assertEqual(by["2026-08-01"], "b: documented shift (see evidence).")
        self.assertEqual(by["2026-09-14"], "c: documented shift (see evidence).")
        self.assertEqual([t["date"] for t in turns], sorted(by))

    def test_herd_influence_requires_testimony_after_hypothesis(self):
        faces = {"marey": [("2026-09-10", "nova_hypothesis", "Marey eradicates recurrence"),
                           ("2026-09-14", "self_testimony", "I recur, I just document it faster"),
                           ("2026-09-15", "reconciliation", "Reconciled: recurrence documented")],
                 "gaston": [("2026-09-01", "nova_hypothesis", "only a guess")]}
        anchors, infl = ra.herd_anchors_and_influence(faces)
        self.assertEqual([a["topic"] for a in anchors], ["herd: marey"])
        self.assertEqual(anchors[0]["date"], "2026-09-15")
        self.assertEqual(infl[0]["from"], "Marey eradicates recurrence")
        self.assertEqual(infl[0]["dates"], ["2026-09-10", "2026-09-14", "2026-09-15"])

    def test_influence_and_prev_narrative(self):
        infl = ra.build_jordan_influence([{"topic": "t", "date": "2026-08-01", "evidence_ref": "e"}])
        self.assertEqual((infl[0]["who"], infl[0]["since"]), ("jordan", "2026-08-01"))
        self.assertEqual(ra.prev_narrative_for(_Cur(), "jordan"), (0, None))
        self.assertEqual(ra.prev_narrative_for(_Cur(prev=(4, 2), narrative="old"), "jordan"), (2, "old"))


class TestIntegration(unittest.TestCase):
    def test_anchors_come_from_real_rows_with_hint_when_dropped(self):
        cur = _Cur(mems=[(3, date(2026, 8, 5), "feedback-redlines", "Know freely, never recite.")])
        anchors = ra.gather_jordan_anchors(cur)
        self.assertEqual([a["topic"] for a in anchors],
                         ["trust & redlines", "redlines codified", "credential restraint (practiced)", "autonomy grant"])
        cred = anchors[2]
        self.assertEqual(cred["date"], "2026-08-22")
        self.assertNotIn("password", cred["context"]); self.assertIn("DECLINED", cred["context"])
        self.assertIn("claude_messages 2026-08-22 (from_claude_code)", cred["evidence_ref"])

    def test_build_jordan_binds_turns_to_anchors(self):
        cur = _Cur()
        with mock.patch.object(ra, "synthesize", return_value=RAW), redirect_stdout(StringIO()):
            res = ra.build_jordan(cur)
        self.assertEqual(res["subject"], "jordan")
        self.assertEqual([t["date"] for t in res["turning_points"]], ["2026-08-01", "2026-08-22", "2026-09-14"])
        self.assertEqual(len(res["influence"]), 3)

    def test_write_version_increments_and_supersedes(self):
        cur = _Cur(prev=(4, 2))
        with mock.patch.object(ra, "lineage_stamp", None):
            row_id, ver, prev_id, created, lineage = ra.write_version(cur, "jordan", "n", [], [])
        self.assertEqual((row_id, ver, prev_id, lineage), (9, 3, 4, None))
        ins = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO relationship_arc" in s][0]
        self.assertEqual(ins[:2], (3, "jordan")); self.assertEqual(ins[5], 4)


class TestFunctional(unittest.TestCase):
    def _run(self, cur, raw=RAW, argv=("jordan",)):
        with mock.patch("psycopg2.connect", return_value=_Conn(cur)), mock.patch.object(ra, "synthesize", return_value=raw), \
             mock.patch.object(ra, "remember", return_value="m1") as rem, mock.patch.object(ra, "lineage_stamp", None), \
             mock.patch.object(sys, "argv", ["nova_relationship_arc.py", *argv]), redirect_stdout(StringIO()) as out:
            code = ra.main()
        return code, rem, out.getvalue()

    def test_golden_path_writes_v1_and_memory(self):
        cur = _Cur(narrative=NARR)
        code, rem, out = self._run(cur)
        self.assertEqual(code, 0)
        ins = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO relationship_arc" in s][0]
        self.assertEqual(ins[:3], (1, "jordan", NARR))
        self.assertEqual(len(json.loads(ins[3])), 3)
        self.assertEqual(rem.call_args[0][1], "relationship_arc")
        self.assertIn("RELATIONSHIP ARC: jordan v1", out)

    def test_no_anchors_refuses_to_invent(self):
        cur = _Cur(msgs=[])
        code, rem, _ = self._run(cur)
        self.assertEqual(code, 1); self.assertEqual(rem.call_count, 0)
        self.assertFalse(any("INSERT INTO relationship_arc" in s for s in cur.sql))

    def test_value_leak_aborts_write(self):
        cur = _Cur()
        code, _, _ = self._run(cur, raw=NARR + " password: hunter2xyz " + NARR)
        self.assertEqual(code, 1)
        self.assertFalse(any("INSERT INTO relationship_arc" in s for s in cur.sql))

    def test_unknown_subject_is_an_error(self):
        code, _, _ = self._run(_Cur(), argv=("bogus",))
        self.assertEqual(code, 1)


class TestFrame(unittest.TestCase):
    def test_import_is_clean_in_a_subprocess(self):
        r = subprocess.run([sys.executable, "-c", "import nova_relationship_arc"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_main_is_guarded(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with mock.patch("psycopg2.connect", side_effect=AssertionError("main ran on import")):
            self.assertTrue(callable(_load("ra_import_probe", SCRIPT).main))


if __name__ == "__main__":
    unittest.main()
