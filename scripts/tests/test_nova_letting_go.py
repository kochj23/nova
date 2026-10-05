#!/usr/bin/env python3
"""Tests for nova_letting_go.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_letting_go.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lg = _load("lg", SCRIPT)


class _Cur:
    """Routes each query to a canned answer by SQL substring (first match wins); records every statement."""
    def __init__(self, routes=()):
        self.routes = list(routes); self.sql = []; self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split()); self.sql.append((self._last, params))

    def _hit(self):
        for key, val in self.routes:
            if key in self._last:
                return val
        return None

    def fetchone(self):
        v = self._hit()
        if isinstance(v, list):
            return v[0] if v else None
        return v

    def fetchall(self):
        v = self._hit()
        return list(v) if isinstance(v, list) else ([] if v is None else [v])

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Boom(_Cur):
    def execute(self, sql, params=None):
        raise RuntimeError("relation does not exist")


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.closed = False; self.autocommit = False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


# (id, topic, kind, summary, returns, since_dev, age)
STALE = (7, "old radios", "fascination", "valve sets", 2, 90, 400)
FRESH = (8, "the failing disk", "worry", "smart errors", 12, 2, 60)
RELEASE_RAW = ("LIVE-THREAD: none\nVERDICT: RELEASE\n\nI picked this up three times and nothing new came of it. "
               "It was a good question once; it is answered now, or it was never a question. Putting it down.")


def _mc():
    return _Cur([("FROM memories", (0, 0))])


def _oc(preoccs=(), anchors=(), projects=(), tastes=(), goals=(), proposals=()):
    return _Cur([("FROM memory_anchors", [(k,) for k in anchors]),
                 ("FROM preoccupations WHERE status='active'", list(preoccs)),
                 ("FROM projects", list(projects)), ("FROM taste", list(tastes)),
                 ("FROM goals WHERE status='active'", list(goals)),
                 ("SELECT id, lineage->>'context', proposed_action FROM coagency_proposals", list(proposals)),
                 ("SELECT 1 FROM coagency_proposals", None),
                 ("INSERT INTO letting_go_log", (5,))])


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        oc = _oc()
        cand = {"kind": "preoccupation", "id": 1, "subject": "x'); DROP TABLE preoccupations; --", "status_field": "status",
                "prior_status": "active", "reason": "r", "evidence": {}}
        with patch.object(lg, "remember", lambda *a, **k: None):
            lg.release(oc, cand, "a reflection long enough to count")
        sql, params = oc.ran("INSERT INTO letting_go_log")[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(params[2], cand["subject"])

    def test_release_is_reversible_never_destructive(self):
        # the only DELETE is the selftest removing its own log row; the schema is additive only
        deletes = re.findall(r"DELETE FROM\s+(\w+)", SRC)
        self.assertEqual(deletes, ["letting_go_log"])
        self.assertLess(SRC.index("def run_selftest"), SRC.index("DELETE FROM letting_go_log"))
        self.assertNotIn("DROP ", SRC)
        self.assertIn("ADD COLUMN IF NOT EXISTS retired_at", SRC)
        self.assertIn('"privacy": "private"', SRC)


class TestPerformance(unittest.TestCase):
    def test_nomination_fast_on_10k_preoccupations(self):
        rows = [(i, f"topic {i}", "k", "s", i % 10, (i * 7) % 60, 100) for i in range(10_000)]
        oc = _oc(preoccs=rows, anchors=[f"preocc:{i}" for i in range(0, 10_000, 2)])
        t0 = time.perf_counter()
        out = lg.nominate_preoccupations(oc, _mc())
        self.assertLess(time.perf_counter() - t0, 1.5)
        self.assertTrue(out)
        self.assertTrue(all(c["id"] % 2 == 1 for c in out))          # every even id was anchored


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_across_nodes_then_succeeds(self):
        calls = []

        def fake(req, timeout=0):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("node down")
            return _Resp({"message": {"content": "VERDICT: KEEP"}})
        with patch("urllib.request.urlopen", side_effect=fake):
            self.assertEqual(lg.llm("q"), "VERDICT: KEEP")
        self.assertEqual(calls, [n + "/api/chat" for n in lg.OLLAMA_NODES[:3]])
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(lg.llm("q"), "")                        # every node down -> empty -> gate KEEPs

    def test_remember_failure_leaves_the_log_row_standing(self):
        # RETRY GAP: remember — one POST, no retry; release() catches it, the audit row is the source of truth
        oc = _oc()
        cand = {"kind": "project", "id": 3, "subject": "p", "status_field": "status", "prior_status": "active", "reason": "r"}
        with patch.object(lg, "remember", MagicMock(side_effect=OSError("memsrv down"))), redirect_stdout(io.StringIO()):
            log_id, mid = lg.release(oc, cand, "reflection text that is long enough")
        self.assertEqual((log_id, mid), (5, None))
        self.assertTrue(oc.ran("UPDATE projects SET status='shelved'"))

    def test_fail_open_helpers(self):
        self.assertEqual(lg._fizzle_stats(_Boom(), "t"), (0, 0))
        self.assertEqual(lg._anchored_preoccupations(_Boom()), set())
        with patch.object(lg.psycopg2, "connect", side_effect=OSError("no pg")):
            self.assertEqual(lg.recent_lettings(), [])


class TestUnit(unittest.TestCase):
    def _gate(self, raw):
        with patch.object(lg, "llm", lambda *a, **k: raw):
            return lg.gate_and_reflect({"kind": "preoccupation", "subject": "s", "context": "c", "reason": "r"})

    def test_gate_is_conservative(self):
        self.assertEqual(self._gate(""), (False, ""))
        self.assertEqual(self._gate("LIVE-THREAD: measure the drift\nVERDICT: KEEP"), (False, ""))
        self.assertEqual(self._gate("VERDICT: RELEASE\n\nok."), (False, ""))     # a release with no real reflection is performance
        ok, refl = self._gate(RELEASE_RAW)
        self.assertTrue(ok); self.assertTrue(refl.startswith("I picked this up"))
        ok, _ = self._gate("**VERDICT: RELEASE**\n\n" + "a" * 40)                # markdown decoration tolerated
        self.assertTrue(ok)

    def test_nominate_projects_and_taste_shapes(self):
        oc = _oc(projects=[(4, "radio bench", "why", 35, 30)], tastes=[(9, "lo-fi", "music", "meh", 0.2, 0.4, 50)])
        p = lg.nominate_projects(oc)[0]
        self.assertEqual((p["kind"], p["prior_status"], p["reason"]), ("project", "active", "active but untouched 30d, stuck at 35%"))
        t = lg.nominate_taste(oc)[0]
        self.assertEqual((t["kind"], t["status_field"], t["prior_status"]), ("taste", "retired_at", None))
        self.assertIn("|valence|=0.20", t["reason"])
        self.assertEqual(oc.ran("FROM taste")[0][1], (lg.TASTE_WEAK_VALENCE, lg.TASTE_LOW_CONF, lg.TASTE_STALE_DAYS))

    def test_nominate_preoccupations_heuristics(self):
        rows = [STALE, FRESH, (9, "played out", "k", "", 1, 3, 30)]
        oc = _oc(preoccs=rows)
        mc = _Cur([("FROM memories", (4, 1))])                      # 4 fizzles, 1 pursuit for every topic
        out = {c["id"]: c for c in lg.nominate_preoccupations(oc, mc)}
        self.assertEqual(set(out), {7, 9})                           # 8 is fresh and rewarding (returns 12)
        self.assertEqual(out[7]["reason"], "not developed in 90d; fizzled 4x vs 1 real developments, returns=2")
        self.assertEqual(out[9]["evidence"]["fizzles"], 4)

    def test_recent_lettings_formats_first_person(self):
        cur = _Cur([("FROM letting_go_log", [("preoccupation", "old radios", "It ran its course. More after."), ("taste", "lo-fi", None)])])
        with patch.object(lg.psycopg2, "connect", return_value=_Conn(cur)):
            self.assertEqual(lg.recent_lettings(2), ["I let go of the preoccupation with old radios — It ran its course.",
                                                     "I outgrew my taste for lo-fi."])
        self.assertEqual(cur.sql[0][1], (2,))

    def test_conservatism_knobs(self):
        self.assertLessEqual(lg.MAX_RELEASE, 3)
        self.assertGreaterEqual(lg.PREOCC_STALE_DAYS, 30)


class TestIntegration(unittest.TestCase):
    def test_anchored_preoccupations_are_never_nominated(self):
        anchored = (7, "old radios", "fascination", "valve sets", 2, 200, 400)        # staler than anything, but HELD
        other = (8, "the failing disk", "worry", "smart errors", 2, 200, 400)
        oc = _oc(preoccs=[anchored, other], anchors=["preocc:7"])
        out = lg.nominate_preoccupations(oc, _mc())
        self.assertEqual([c["id"] for c in out], [8])
        self.assertEqual(oc.sql[0][0], "SELECT key FROM memory_anchors WHERE released_at IS NULL AND key LIKE 'preocc:%'")
        # the guard reads the key format the anchor organ writes (via nova_weight_of_memory.gather)
        self.assertIn('f"preocc:{pid}"', (SCRIPTS / "nova_weight_of_memory.py").read_text())
        self.assertIn("released_at IS NULL", (SCRIPTS / "nova_memory_anchor.py").read_text())

    def test_lineage_is_the_shared_helper(self):
        import nova_lineage
        self.assertIs(lg.lineage_stamp, nova_lineage.lineage_stamp)

    def test_release_chains_log_flip_and_memory(self):
        oc = _oc()
        remember = MagicMock(return_value="mem-1")
        cand = {"kind": "taste", "id": 9, "subject": "lo-fi", "status_field": "retired_at", "prior_status": None,
                "reason": "weak", "evidence": {"since": 50}}
        with patch.object(lg, "remember", remember):
            self.assertEqual(lg.release(oc, cand, "a reflection with some real length"), (5, "mem-1"))
        lineage = json.loads(oc.ran("INSERT INTO letting_go_log")[0][1][5])
        self.assertEqual((lineage["prior_status"], lineage["status_field"], lineage["evidence"]), (None, "retired_at", {"since": 50}))
        self.assertTrue(oc.ran("UPDATE taste SET retired_at=now()"))
        text, source, meta = remember.call_args[0]
        self.assertEqual(source, "letting_go"); self.assertEqual(meta["log_id"], 5); self.assertTrue(text.startswith("[Letting go — taste: lo-fi]"))

    def test_goal_retirement_goes_through_coagency(self):
        coag = types.ModuleType("nova_coagency"); coag.file_proposal = MagicMock()
        oc = _oc(goals=[("abcdef12-goal", "learn Morse", 7, 100), ("deadbeef-goal", "fresh goal", 7, 10)])
        with patch.dict(sys.modules, {"nova_coagency": coag}), redirect_stdout(io.StringIO()):
            self.assertEqual(lg.propose_goal_retirements(oc), 1)
        action = coag.file_proposal.call_args[0][2]
        self.assertEqual(action, "retire goal 'learn Morse' (abcdef12-goal): untouched 100d, check-in every 7d")
        self.assertEqual(coag.file_proposal.call_args[1]["context"], "goal_id=abcdef12-goal")


class TestFunctional(unittest.TestCase):
    def test_review_golden_path_releases_one_and_keeps_one(self):
        oc = _oc(preoccs=[STALE, FRESH])
        remember = MagicMock(return_value="mem-9")
        with patch.object(lg, "llm", lambda *a, **k: RELEASE_RAW), patch.object(lg, "remember", remember), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(lg.run_review(oc, _mc()), 0)
        self.assertEqual(oc.ran("UPDATE preoccupations SET status='retired'"), [("UPDATE preoccupations SET status='retired' WHERE id=%s", (7,))])
        self.assertEqual(len(oc.ran("INSERT INTO letting_go_log")), 1)
        self.assertEqual(remember.call_args[0][2]["ref_id"], 7)

    def test_review_respects_max_release(self):
        rows = [(i, f"t{i}", "k", "", 1, 99, 200) for i in range(5)]
        with patch.object(lg, "llm", lambda *a, **k: RELEASE_RAW), patch.object(lg, "remember", lambda *a, **k: None), \
             redirect_stdout(io.StringIO()):
            oc = _oc(preoccs=rows); lg.run_review(oc, _mc())
        self.assertEqual(len(oc.ran("UPDATE preoccupations")), lg.MAX_RELEASE)

    def test_review_error_path_silent_llm_releases_nothing(self):
        oc = _oc(preoccs=[STALE])
        with patch.object(lg, "llm", lambda *a, **k: ""), redirect_stdout(io.StringIO()):
            self.assertEqual(lg.run_review(oc, _mc()), 0)
        self.assertEqual(oc.ran("UPDATE preoccupations"), [])
        self.assertEqual(oc.ran("INSERT INTO letting_go_log"), [])

    def test_apply_goal_retirements_drops_an_approved_goal(self):
        oc = _oc(proposals=[(12, "goal_id=abcdef12", "retire goal 'learn Morse' (abcdef12)")])
        oc.routes.append(("FROM goals WHERE id LIKE", ("abcdef12-goal", "learn Morse")))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(lg.apply_goal_retirements(oc), 1)
        self.assertEqual(oc.ran("UPDATE goals SET status='dropped'")[0][1], ("abcdef12-goal",))
        self.assertEqual(oc.ran("INSERT INTO goal_log")[0][1][0], "abcdef12-drop-12")

    def test_main_report_and_unknown_mode(self):
        oc = _oc()
        out = io.StringIO()
        with patch.object(lg.psycopg2, "connect", return_value=_Conn(oc)), patch.object(lg, "recent_lettings", lambda n=3: ["I shelved x."]), \
             patch.object(sys, "argv", ["nova_letting_go.py", "--mode", "report"]), redirect_stdout(out):
            self.assertEqual(lg.main(), 0)
        self.assertIn("I shelved x.", out.getvalue())
        self.assertTrue(oc.ran("CREATE TABLE IF NOT EXISTS letting_go_log"))
        with patch.object(lg.psycopg2, "connect", return_value=_Conn(_oc())), patch.object(sys, "argv", ["x", "--mode=bogus"]), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(lg.main(), 2)


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free_and_main_is_guarded(self):
        # --mode selftest needs PG + a live LLM, so the frame check is a clean import with main() guarded
        r = subprocess.run([sys.executable, "-c", "import nova_letting_go"], cwd=SCRIPTS, capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)


if __name__ == "__main__":
    unittest.main()
