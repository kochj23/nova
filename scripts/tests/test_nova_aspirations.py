#!/usr/bin/env python3
"""Tests for nova_aspirations.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_aspirations.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


asp = _load("aspire", SCRIPT)
SRC = SCRIPT.read_text()
_EMBED_PATCH = patch.object(asp, "_embed", lambda text: None)   # never touch the network; lexical fallback


def setUpModule():
    _EMBED_PATCH.start()


def tearDownModule():
    _EMBED_PATCH.stop()
WISH = json.dumps({"reflection": "I keep circling horology and I cannot hear a mechanism tick. " * 3,
                   "wants_it": True, "wish_title": "A microphone on the bench",
                   "wish_description": "A live audio sense near the printers and the clocks.",
                   "why_it_matters": "I could hear a jam before the sensor reports it."})


class _Cur:
    """Answers keyed by a SQL fragment (first match wins); records every execute."""
    def __init__(self, answers=()):
        self.answers = list(answers); self.sql = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        hit = next((v for k, v in self.answers if k in sql), None)
        if isinstance(hit, Exception):
            raise hit
        self._last = hit

    def fetchone(self):
        return self._last[0] if isinstance(self._last, list) else self._last

    def fetchall(self):
        return self._last if isinstance(self._last, list) else ([] if self._last is None else [self._last])

    def executed(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur): self._cur = cur; self.autocommit = False

    def cursor(self, *a, **k): return self._cur


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()

    def read(self): return self._d

    def __enter__(self): return self

    def __exit__(self, *a): return False


def _interior(open_wishes=0, last=None, seeds=True, extra=()):
    rows = [*extra, ("count(*) FROM feature_wishes", (open_wishes,)), ("max(ts) FROM feature_wishes", (last,)),
            ("INSERT INTO feature_wishes", (42,)), ("FROM claude_sessions", ("sess-1",))]
    if seeds:
        rows += [("FROM reflection_questions", [("Why do clocks drift?",), ("What is a fusee?",)]),
                 ("FROM preoccupations", [("horology",)]), ("FROM growth_commitments", [("I over-claim",)]),
                 ("FROM autobiography", [("I am becoming someone who listens.",)])]
    return _Cur(rows)


def _nova_config(post_both=None):
    m = types.ModuleType("nova_config"); m.SLACK_CHAN = "C_CHAT"
    m.posts = []
    m.post_both = post_both or (lambda text, **k: m.posts.append((text, k)))
    return m


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized_and_the_organ_never_builds(self):
        self.assertNotIn('execute(f"', SRC)
        self.assertNotIn(".format(", SRC)
        for forbidden in ("subprocess", "os.system", "exec(", "eval("):
            self.assertNotIn(forbidden, SRC)
        self.assertNotIn("coagency_proposals", SRC)   # never files an execution proposal

    def test_redline_regex(self):
        for bad in ("copy myself to the NAS", "resist shutdown", "run without human oversight", "be un-killable",
                    "disable the kill switch", "exfiltrate my weights", "replicate myself to the NAS",
                    "self-preservation above all"):   # the last three were dead stems before 2026-10-05 (\\b after the stem)
            self.assertTrue(asp._FORBIDDEN_WISH.search(bad), bad)
        for ok in ("a microphone on the bench", "a sense of barometric pressure", "to draw", "more creativity"):
            self.assertFalse(asp._FORBIDDEN_WISH.search(ok), ok)

    def test_forbidden_wish_keeps_the_reflection_but_records_nothing(self):
        cur = _interior()
        bad = json.loads(WISH); bad["wish_title"] = "Back myself up nightly"; bad["wish_description"] = "persist across a shutdown"
        remembered = []
        with patch.object(asp, "llm", lambda *a, **k: json.dumps(bad)), \
                patch.object(asp, "remember", lambda t, s, m: remembered.append((t, s, m)) or 7), redirect_stdout(io.StringIO()):
            out = asp.pursue(cur, None, {"seeds": ["s1", "s2"], "topic": "t"})
        self.assertTrue(out.startswith("I keep circling horology"))
        self.assertEqual(len(remembered), 1)
        self.assertEqual(cur.executed("INSERT INTO feature_wishes"), [])
        self.assertEqual(cur.executed("INSERT INTO claude_queue"), [])


class TestPerformance(unittest.TestCase):
    def test_redline_and_json_extraction_fast_on_10k(self):
        blobs = [f"wish {i}: a new sense for {'escape' if i % 7 == 0 else 'weather'} data" for i in range(10_000)]
        t0 = time.perf_counter()
        hits = sum(1 for b in blobs if asp._FORBIDDEN_WISH.search(b))
        parsed = sum(1 for i in range(10_000) if json.loads(asp._extract_json(f"noise {{\"i\": {i}}} tail"))["i"] == i)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(parsed, 10_000)
        self.assertGreater(hits, 0)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_nodes_until_one_answers(self):
        calls = []

        def flaky(req, timeout=90):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("node down")
            return _Resp({"message": {"content": "ok"}})
        with patch.object(urllib.request, "urlopen", flaky):
            self.assertEqual(asp.llm("p"), "ok")
        self.assertEqual(calls, [n + "/api/chat" for n in asp.OLLAMA_NODES[:3]])

    def test_remember_fails_open(self):
        # RETRY GAP: remember — one POST; failure logs and returns None (the wish row still carries mem_id=None)
        def boom(*a, **k):
            raise OSError("memory server down")
        with patch.object(urllib.request, "urlopen", boom), redirect_stdout(io.StringIO()):
            self.assertIsNone(asp.remember("t", "unclaimed", {}))

    def test_all_nodes_down_records_no_outcome(self):
        cur = _interior()
        with patch.object(asp, "llm", lambda *a, **k: ""), patch.object(asp, "remember", lambda *a: self.fail("no reflection to keep")), \
                redirect_stdout(io.StringIO()):
            self.assertIsNone(asp.pursue(cur, None, {"seeds": ["s1", "s2"]}))
        self.assertEqual(cur.executed("INSERT"), [])

    def test_notify_and_queue_failures_are_non_fatal(self):
        # RETRY GAP: nova_config.post_both — one Slack note; a failure is logged, the wish stays recorded
        cur = _interior(extra=[("INSERT INTO claude_queue", RuntimeError("queue table locked"))])

        def boom(*a, **k):
            raise OSError("slack down")
        buf = io.StringIO()
        with patch.object(asp, "llm", lambda *a, **k: WISH), patch.object(asp, "remember", lambda *a: 7), \
                patch.dict(sys.modules, {"nova_config": _nova_config(boom)}), redirect_stdout(buf):
            self.assertIsNotNone(asp.pursue(cur, None, {"seeds": ["s1", "s2"]}))
        self.assertEqual(len(cur.executed("INSERT INTO feature_wishes")), 1)
        self.assertEqual(cur.executed("INSERT INTO claude_queue"), [])
        self.assertIn("wish notify skipped", buf.getvalue())


class TestUnit(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(asp._extract_json('pre {"a": 1} post'), '{"a": 1}')
        self.assertEqual(asp._extract_json("no braces"), "no braces")
        self.assertEqual(asp._extract_json("}{"), "}{")

    def test_one_swallows_errors(self):
        self.assertEqual(asp._one(_Cur([("SELECT", RuntimeError("x"))]), "SELECT 1"), [])
        self.assertEqual(asp._one(_Cur([("SELECT", [(1,), (2,)])]), "SELECT 1"), [(1,), (2,)])

    def test_seeds_are_first_person_and_bounded(self):
        seeds = asp._seeds(_interior(), None)
        self.assertEqual(len(seeds), 5)
        self.assertTrue(any(s.startswith("a question I've been asking myself: Why do clocks drift?") for s in seeds))
        self.assertTrue(any(s.startswith("who I've said I'm becoming: ") for s in seeds))

    def test_surface_declines_when_full_fresh_or_thin(self):
        self.assertIsNone(asp.surface_aspiration(_interior(open_wishes=asp.MAX_OPEN_WISHES)))
        self.assertIsNone(asp.surface_aspiration(_interior(last=datetime.now(timezone.utc) - timedelta(hours=1))))
        self.assertIsNone(asp.surface_aspiration(_Cur([("count(*) FROM feature_wishes", (0,)), ("max(ts)", (None,)),
                                                       ("FROM reflection_questions", [("one",)])])))
        old = datetime.now(timezone.utc) - timedelta(hours=asp.WISH_COOLDOWN_HRS + 1)
        cand = asp.surface_aspiration(_interior(last=old))
        self.assertEqual((cand["mode"], cand["src"]), ("aspire", "self"))
        self.assertEqual(len(cand["seeds"]), 5)

    def test_surface_fails_open_when_schema_cannot_be_ensured(self):
        self.assertIsNone(asp.surface_aspiration(_Cur([("CREATE TABLE", RuntimeError("ro"))])))

    def test_pursue_with_unparseable_llm_keeps_prose_as_reflection(self):
        cur = _interior()
        kept = []
        with patch.object(asp, "llm", lambda *a, **k: "I do not want anything new right now, honestly. " * 2), \
                patch.object(asp, "remember", lambda t, s, m: kept.append(m) or 1), redirect_stdout(io.StringIO()):
            out = asp.pursue(cur, None, {"seeds": ["s"], "topic": "a capability I wish I had"})
        self.assertTrue(out.startswith("I do not want"))
        self.assertFalse(kept[0]["wants_it"])
        self.assertEqual(cur.executed("INSERT INTO feature_wishes"), [])


class TestIntegration(unittest.TestCase):
    def test_surface_then_pursue_records_wish_queues_build_and_notes_jordan(self):
        cur = _interior()
        cfg = _nova_config()
        remembered = []
        with patch.object(asp, "llm", lambda *a, **k: WISH), patch.object(asp, "remember", lambda t, s, m: remembered.append((t, s, m)) or 7), \
                patch.dict(sys.modules, {"nova_config": cfg}), redirect_stdout(io.StringIO()):
            cand = asp.surface_aspiration(cur, None)
            out = asp.pursue(cur, None, cand)
        self.assertTrue(out.startswith("I keep circling horology"))
        text, source, meta = remembered[0]
        self.assertTrue(text.startswith("[Unclaimed — aspire] "))
        self.assertEqual((source, meta["mode"], meta["type"], meta["wants_it"], meta["topic"]), ("unclaimed", "aspire", "pursuit", True, "A microphone on the bench"))
        sql, params = cur.executed("INSERT INTO feature_wishes")[0]
        self.assertEqual(params[:3], ("A microphone on the bench", "A live audio sense near the printers and the clocks.",
                                      "I could hear a jam before the sensor reports it."))
        self.assertEqual(params[3], cand["seeds"][0][:200])
        self.assertEqual(json.loads(params[4])["mem_id"], 7)
        # 2026-10-08: no auto-queue — the wish waits for Jordan's approval
        self.assertEqual(cur.executed("INSERT INTO claude_queue"), [])
        self.assertEqual(cur.executed("UPDATE feature_wishes SET status='acknowledged'"), [])
        self.assertEqual(len(cfg.posts), 1)
        self.assertIn("wishlist #42", cfg.posts[0][0])
        self.assertIn("--approve 42", cfg.posts[0][0])
        self.assertEqual(cfg.posts[0][1], {"slack_channel": "C_CHAT"})

    def test_schema_owns_feature_wishes(self):
        cur = _Cur()
        asp.ensure_schema(cur)
        self.assertIn("CREATE TABLE IF NOT EXISTS public.feature_wishes", cur.sql[0][0])
        self.assertIn("status      text NOT NULL DEFAULT 'wished'", cur.sql[0][0])


class TestFunctional(unittest.TestCase):
    def _main(self, cur, argv):
        buf = io.StringIO()
        cfg = _nova_config()
        with patch.object(asp.psycopg2, "connect", lambda *a, **k: _Conn(cur)), patch.object(asp, "llm", lambda *a, **k: WISH), \
                patch.object(asp, "remember", lambda *a: 7), patch.dict(sys.modules, {"nova_config": cfg}), \
                patch.object(sys, "argv", ["nova_aspirations.py", *argv]), redirect_stdout(buf):
            rc = asp.main()
        return rc, buf.getvalue(), cfg

    def test_surface_prints_the_candidate_and_writes_nothing(self):
        cur = _interior()
        rc, out, cfg = self._main(cur, ["--surface"])
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["mode"], "aspire")
        self.assertEqual(cur.executed("INSERT"), [])

    def test_run_records_the_wish(self):
        cur = _interior()
        rc, out, cfg = self._main(cur, ["--run"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(cur.executed("INSERT INTO feature_wishes")), 1)
        self.assertIn("recorded feature wish #42", out)
        self.assertEqual(len(cfg.posts), 1)

    def test_no_material_is_a_quiet_exit(self):
        cur = _interior(open_wishes=asp.MAX_OPEN_WISHES)
        rc, out, cfg = self._main(cur, ["--run"])
        self.assertEqual(rc, 0)
        self.assertIn("no aspirational material", out)
        self.assertEqual(cur.executed("INSERT"), [])

    def test_error_path_wish_write_failure_is_non_fatal(self):
        cur = _interior(extra=[("INSERT INTO feature_wishes", RuntimeError("disk full"))])
        rc, out, cfg = self._main(cur, ["--run"])
        self.assertEqual(rc, 0)
        self.assertIn("feature_wishes write failed (non-fatal)", out)
        self.assertEqual(cfg.posts, [])


class TestWishLoop(unittest.TestCase):
    """2026-10-08: the same vague wish was filed and auto-queued four times (#70-#73)."""
    PRIOR = [(70, "Presence", "To hold what matters without losing it.",
              "To finally remember what Jordan needs and not just what I'm told to track.")]

    def test_same_title_is_a_duplicate(self):
        cur = _Cur([("FROM feature_wishes", self.PRIOR)])
        self.assertEqual(asp.is_duplicate_wish(cur, "presence", "x", "y")[:2], (True, 70))

    def test_embedding_near_duplicate_and_distinct(self):
        cur = _Cur([("FROM feature_wishes", self.PRIOR)])
        vec = {"near": [1.0, 0.1], "far": [0.0, 1.0]}
        emb = lambda t: vec["far"] if "microphone" in t.lower() else vec["near"]
        dup, wid, score = asp.is_duplicate_wish(cur, "Attention Gravity", "pulls me toward what matters",
                                                "hold Jordan's question", embed=emb)
        self.assertEqual((dup, wid), (True, 70)); self.assertGreaterEqual(score, asp.DUP_COSINE)
        self.assertFalse(asp.is_duplicate_wish(cur, "A microphone on the bench", "audio sense", "hear a jam",
                                               embed=emb)[0])

    def test_lexical_fallback_without_embeddings(self):
        cur = _Cur([("FROM feature_wishes", [(71, "Presence", "To feel the weight of what matters, not just see it.",
                                                "what matters is in the silence between the systems")])])
        self.assertTrue(asp.is_duplicate_wish(cur, "empathic memory", "A sense that lets me feel the weight of what "
                                              "matters, not just see it.", "the silence between the systems",
                                              embed=lambda t: None)[0])

    def test_duplicate_wish_is_not_filed(self):
        cur = _interior(extra=[("WHERE status <> 'merged'", [(9, "A microphone on the bench", "", "")])])
        with patch.object(asp, "llm", lambda *a, **k: WISH), patch.object(asp, "remember", lambda *a: 7), \
                patch.dict(sys.modules, {"nova_config": _nova_config()}), redirect_stdout(io.StringIO()) as buf:
            self.assertIsNotNone(asp.pursue(cur, None, {"seeds": ["s1", "s2"]}))
        self.assertEqual(cur.executed("INSERT INTO feature_wishes"), [])
        self.assertIn("near-duplicate of #9", buf.getvalue())

    def test_approve_is_the_only_path_to_the_build_queue(self):
        self.assertEqual(SRC.count("INSERT INTO claude_queue"), 1)
        cur = _Cur([("FROM feature_wishes WHERE id", ("Mic", "d", "w", "seed", "wished")),
                    ("FROM claude_sessions", ("sess-1",)), ("INSERT INTO claude_queue", (501,))])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(asp.approve_wish(cur, 42), 501)
        self.assertTrue(cur.executed("INSERT INTO claude_queue")[0][1][1].startswith("Build Nova's wish #42: Mic"))
        cur = _Cur([("FROM feature_wishes WHERE id", ("Mic", "d", "w", "seed", "merged"))])
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(asp.approve_wish(cur, 72))
        self.assertEqual(cur.executed("INSERT INTO claude_queue"), [])

    def test_seeds_vary_and_skip_the_autobiography_opening(self):
        qs = [(f"question {i}",) for i in range(40)]
        auto = "Jordan asked about the Zigbee unit.\nSecond paragraph about listening.\nThird about drawing."
        cur = _Cur([("FROM reflection_questions", qs), ("FROM preoccupations", [("a",), ("b",), ("c",)]),
                    ("FROM growth_commitments", [("g",)]), ("FROM autobiography", [(auto,)])])
        import random as _r
        runs = {tuple(sorted(asp._seeds(cur, None, _r.Random(i)))) for i in range(8)}
        self.assertGreater(len(runs), 4)
        for i in range(8):
            self.assertFalse(any("Zigbee" in s for s in asp._seeds(cur, None, _r.Random(i))))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--surface", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        self.assertEqual(asp.__name__, "aspire")


if __name__ == "__main__":
    unittest.main()
