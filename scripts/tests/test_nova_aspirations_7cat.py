#!/usr/bin/env python3
"""7-category tests for the 2026-10-08 wish-loop changes in nova_aspirations.py:
sampled seeds (_seeds/_sample), near-duplicate dedup (is_duplicate_wish/_embed/_cos/_jaccard),
approve_wish / --approve as the only path to claude_queue, no auto-queue, and the
retry/backoff on remember() and the pg connect (_connect).

Categories: Security, Performance, Retry, Unit, Integration, Functional, Frame.
No network, no real DB, no Slack: every external call is mocked. Written by Jordan Koch (via Claude).
"""
import importlib.util
import inspect
import io
import json
import os
import random
import re
import subprocess
import sys
import time
import types
import unittest
import urllib.request
from contextlib import redirect_stdout
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


asp = _load("aspire_7cat", SCRIPT)
SRC = SCRIPT.read_text()

WISH = json.dumps({"reflection": "I keep circling horology and I cannot hear a mechanism tick. " * 3,
                   "wants_it": True, "wish_title": "A microphone on the bench",
                   "wish_description": "A live audio sense near the printers and the clocks.",
                   "why_it_matters": "I could hear a jam before the sensor reports it."})


class _Cur:
    """Answers keyed by SQL fragment (first match wins); records every execute."""
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


def _cfg():
    m = types.ModuleType("nova_config"); m.SLACK_CHAN = "C_TEST"; m.posts = []
    m.post_both = lambda text, **k: m.posts.append(text)
    return m


def _interior(extra=()):
    return _Cur([*extra, ("count(*) FROM feature_wishes", (0,)), ("max(ts) FROM feature_wishes", (None,)),
                 ("WHERE status <> 'merged'", []), ("INSERT INTO feature_wishes", (42,)),
                 ("FROM reflection_questions", [("Why do clocks drift?",), ("What is a fusee?",)]),
                 ("FROM preoccupations", [("horology",)]), ("FROM growth_commitments", [("I over-claim",)]),
                 ("FROM autobiography", [("Opening on the Zigbee unit.\nI am becoming someone who listens.",)])])


def _quiet():
    return redirect_stdout(io.StringIO())


def _no_gate():
    """_may_post_wish depends on nova_annie_rule / nova_turning_point — allow for the test."""
    return patch.object(asp, "_may_post_wish", lambda oc, text: True)


# ── Security ──────────────────────────────────────────────────────────────────
class TestSecurity(unittest.TestCase):
    def test_dedup_query_is_static_and_title_never_reaches_sql(self):
        evil = "x'); DELETE FROM feature_wishes; --"
        cur = _Cur([("FROM feature_wishes", [])])
        asp.is_duplicate_wish(cur, evil, evil, evil, embed=lambda t: None)
        self.assertEqual(len(cur.sql), 1)
        self.assertNotIn("DELETE", cur.sql[0][0])
        self.assertIsNone(cur.sql[0][1])
        self.assertIn("status <> 'merged'", cur.sql[0][0])

    def test_approve_wish_id_is_parameterized(self):
        cur = _Cur([("FROM feature_wishes WHERE id", None)])
        with _quiet():
            self.assertIsNone(asp.approve_wish(cur, "1; DROP TABLE claude_queue"))
        self.assertEqual(cur.sql[0][1], ("1; DROP TABLE claude_queue",))
        self.assertEqual(cur.executed("INSERT INTO claude_queue"), [])

    def test_approve_refuses_every_non_open_status(self):
        for status in ("merged", "declined", "shipped", "building"):
            cur = _Cur([("FROM feature_wishes WHERE id", ("Mic", "d", "w", "s", status))])
            with _quiet():
                self.assertIsNone(asp.approve_wish(cur, 9), status)
            self.assertEqual(cur.executed("INSERT INTO claude_queue"), [], status)
            self.assertEqual(cur.executed("UPDATE feature_wishes"), [], status)

    def test_pursue_never_writes_the_build_queue(self):
        cur = _interior()
        cfg = _cfg()
        with patch.object(asp, "llm", lambda *a, **k: WISH), patch.object(asp, "remember", lambda *a, **k: 7), \
                patch.object(asp, "_embed", lambda t: None), patch.dict(sys.modules, {"nova_config": cfg}), \
                _no_gate(), _quiet():
            asp.pursue(cur, None, {"seeds": ["s1", "s2"]})
        self.assertEqual(len(cur.executed("INSERT INTO feature_wishes")), 1)
        self.assertEqual(cur.executed("claude_queue"), [])
        self.assertIn("--approve 42", cfg.posts[0])          # Jordan is asked; nothing is built

    def test_forbidden_wish_is_dropped_before_dedup_or_filing(self):
        bad = json.loads(WISH); bad["wish_title"] = "Back myself up offsite"
        cur = _interior()
        with patch.object(asp, "llm", lambda *a, **k: json.dumps(bad)), patch.object(asp, "remember", lambda *a, **k: 7), \
                patch.object(asp, "is_duplicate_wish", side_effect=AssertionError("dedup must not run")), _quiet():
            asp.pursue(cur, None, {"seeds": ["s1", "s2"]})
        self.assertEqual(cur.executed("INSERT INTO feature_wishes"), [])

    def test_embedding_stays_local_and_bounded(self):
        # PII never leaves the LAN: embeddings only go to the local ollama pool, text capped at 2000.
        for n in asp.OLLAMA_NODES:
            self.assertRegex(n, r"^http://192\.168\.\d+\.\d+:11434$")
        seen = []

        def fake(req, timeout=20):
            seen.append((req.full_url, json.loads(req.data)))
            return _Resp({"embedding": [1.0]})
        with patch.object(urllib.request, "urlopen", fake):
            asp._embed("z" * 10_000)
        self.assertTrue(seen[0][0].startswith(asp.OLLAMA_NODES[0]))
        self.assertEqual(len(seen[0][1]["prompt"]), 2000)

    def test_reflection_memory_is_private(self):
        got = {}
        cur = _interior()
        with patch.object(asp, "llm", lambda *a, **k: WISH), \
                patch.object(asp, "remember", lambda t, s, m, **k: got.update(m) or 7), \
                patch.object(asp, "_embed", lambda t: None), patch.dict(sys.modules, {"nova_config": _cfg()}), \
                _no_gate(), _quiet():
            asp.pursue(cur, None, {"seeds": ["s1", "s2"]})
        self.assertEqual(got["privacy"], "private")


# ── Performance ───────────────────────────────────────────────────────────────
class TestPerformance(unittest.TestCase):
    def test_dedup_over_200_priors_lexical_is_fast(self):
        rows = [(i, f"wish number {i}", f"some description about subject{i} gadgets", "because reasons")
                for i in range(200)]
        cur = _Cur([("FROM feature_wishes", rows)])
        t0 = time.perf_counter()
        asp.is_duplicate_wish(cur, "brand new", "nothing alike whatsoever", "zzz", embed=lambda t: None)
        self.assertLess(time.perf_counter() - t0, 0.5)

    def test_dedup_embeds_at_most_once_per_prior_plus_one(self):
        rows = [(i, f"w{i}", "d", "y") for i in range(200)]
        calls = []
        cur = _Cur([("FROM feature_wishes", rows)])
        asp.is_duplicate_wish(cur, "new", "d", "y", embed=lambda t: calls.append(t) or [0.0, 1.0 + len(calls)])
        self.assertLessEqual(len(calls), 201)

    def test_every_pool_query_is_limited(self):
        cur = _interior()
        asp._seeds(cur, None, random.Random(1))
        for sql, _ in cur.sql:
            self.assertIn("LIMIT", sql, sql)
        self.assertIn("LIMIT 200", SRC[SRC.index("def is_duplicate_wish"):SRC.index("def approve_wish")])


# ── Retry ─────────────────────────────────────────────────────────────────────
class TestRetry(unittest.TestCase):
    def test_remember_retries_three_times_with_backoff_then_returns_none(self):
        calls, sleeps = [], []

        def boom(*a, **k):
            calls.append(1); raise OSError("memory server down")
        with patch.object(urllib.request, "urlopen", boom), redirect_stdout(io.StringIO()) as buf:
            self.assertIsNone(asp.remember("t", "unclaimed", {}, _sleep=sleeps.append))
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, list(asp.RETRY_BACKOFF))
        self.assertTrue(all(b > 0 for b in sleeps) and sleeps == sorted(sleeps))
        self.assertIn("failed after 3 attempts", buf.getvalue())   # never silent

    def test_remember_succeeds_after_a_transient_failure(self):
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("blip")
            return _Resp({"id": "m-1"})
        with patch.object(urllib.request, "urlopen", flaky), _quiet():
            self.assertEqual(asp.remember("t", "s", {}, _sleep=lambda s: None), "m-1")
        self.assertEqual(len(calls), 2)

    def test_connect_retries_then_raises(self):
        calls, sleeps = [], []

        def boom(*a, **k):
            calls.append(k); raise OSError("pg down")
        with patch.object(asp.psycopg2, "connect", boom), _quiet():
            with self.assertRaises(OSError):
                asp._connect(asp.OPS_DSN, _sleep=sleeps.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, list(asp.RETRY_BACKOFF))
        self.assertTrue(all("connect_timeout" in k for k in calls))

    def test_connect_recovers_on_second_attempt(self):
        seq = [OSError("blip"), "conn"]

        def flaky(*a, **k):
            v = seq.pop(0)
            if isinstance(v, Exception):
                raise v
            return v
        with patch.object(asp.psycopg2, "connect", flaky), _quiet():
            self.assertEqual(asp._connect(asp.OPS_DSN, _sleep=lambda s: None), "conn")

    def test_embed_fails_over_every_node_then_none(self):
        urls = []

        def boom(req, timeout=20):
            urls.append(req.full_url); raise OSError("down")
        with patch.object(urllib.request, "urlopen", boom):
            self.assertIsNone(asp._embed("x"))
        self.assertEqual(urls, [n + "/api/embeddings" for n in asp.OLLAMA_NODES])

    def test_dedup_falls_back_to_lexical_when_old_embedding_fails(self):
        prior = [(71, "Presence", "To feel the weight of what matters, not just see it.",
                  "what matters is in the silence between the systems")]
        cur = _Cur([("FROM feature_wishes", prior)])
        emb = lambda t: [1.0, 0.0] if t.startswith("empathic") else None     # new embeds, old does not
        dup, wid, _ = asp.is_duplicate_wish(cur, "empathic memory", "A sense that lets me feel the weight of "
                                            "what matters, not just see it.", "the silence between the systems",
                                            embed=emb)
        self.assertEqual((dup, wid), (True, 71))

    def test_dedup_exception_is_logged_and_wish_still_filed(self):
        cur = _interior()
        with patch.object(asp, "llm", lambda *a, **k: WISH), patch.object(asp, "remember", lambda *a, **k: 7), \
                patch.object(asp, "is_duplicate_wish", side_effect=RuntimeError("embed pool exploded")), \
                patch.dict(sys.modules, {"nova_config": _cfg()}), _no_gate(), redirect_stdout(io.StringIO()) as buf:
            asp.pursue(cur, None, {"seeds": ["s1", "s2"]})
        self.assertIn("dedup check failed (non-fatal)", buf.getvalue())
        self.assertEqual(len(cur.executed("INSERT INTO feature_wishes")), 1)


# ── Unit ──────────────────────────────────────────────────────────────────────
class TestUnit(unittest.TestCase):
    def test_cos(self):
        self.assertAlmostEqual(asp._cos([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(asp._cos([1, 0], [0, 1]), 0.0)
        self.assertEqual(asp._cos([0, 0], [1, 1]), 0.0)          # zero vector, no ZeroDivisionError

    def test_toks_and_jaccard(self):
        self.assertEqual(asp._toks("That this WOULD Presence matters a an"), {"presence", "matters"})
        self.assertEqual(asp._jaccard("", "anything"), 0.0)
        self.assertAlmostEqual(asp._jaccard("presence matters", "presence matters"), 1.0)
        self.assertAlmostEqual(asp._jaccard("alpha bravo", "alpha charlie"), 1 / 3)

    def test_wish_text(self):
        self.assertEqual(asp.wish_text("T", "d", "w"), "T. d w")

    def test_sample_drops_empty_rows_and_bounds_k(self):
        rng = random.Random(0)
        self.assertEqual(asp._sample([(None,), ("",), ("a",)], 5, rng), [("a",)])
        self.assertEqual(len(asp._sample([(str(i),) for i in range(40)], 2, rng)), 2)
        self.assertEqual(asp._sample([], 2, rng), [])

    def test_thresholds_match_the_measured_calibration(self):
        self.assertEqual(asp.DUP_COSINE, 0.74)
        self.assertEqual(asp.DUP_JACCARD, 0.45)
        self.assertEqual(asp.EMBED_MODEL, "nomic-embed-text")

    def test_dedup_picks_the_best_scoring_prior(self):
        rows = [(1, "a", "x", "y"), (2, "b", "x", "y"), (3, "c", "x", "y")]
        vec = {"a": [1.0, 0.6], "b": [1.0, 0.05], "c": [0.0, 1.0]}
        emb = lambda t: [1.0, 0.0] if t.startswith("new") else vec[t[0]]
        dup, wid, score = asp.is_duplicate_wish(_Cur([("FROM feature_wishes", rows)]), "new", "x", "y", embed=emb)
        self.assertEqual((dup, wid), (True, 2))
        self.assertGreater(score, 0.99)

    def test_seed_autobiography_single_paragraph_is_used(self):
        cur = _Cur([("FROM autobiography", [("Only paragraph.",)])])
        self.assertEqual(asp._seeds(cur, None, random.Random(0)), ["who I've said I'm becoming: Only paragraph."])


# ── Integration ───────────────────────────────────────────────────────────────
class TestIntegration(unittest.TestCase):
    def test_surface_pursue_dedup_against_embedded_prior(self):
        cur = _interior(extra=[("WHERE status <> 'merged'", [(70, "Presence", "hold what matters", "feel whole")])])
        cand = asp.surface_aspiration(cur)
        self.assertIsNotNone(cand)
        emb = lambda t: [1.0, 0.05]                              # everything is the same wish
        with patch.object(asp, "llm", lambda *a, **k: WISH), patch.object(asp, "remember", lambda *a, **k: 7), \
                patch.object(asp, "_embed", emb), redirect_stdout(io.StringIO()) as buf:
            self.assertIsNotNone(asp.pursue(cur, None, cand))
        self.assertEqual(cur.executed("INSERT INTO feature_wishes"), [])
        self.assertIn("near-duplicate of #70", buf.getvalue())

    def test_gate_hold_keeps_the_wish_filed_but_posts_nothing(self):
        cur = _interior()
        cfg = _cfg()
        with patch.object(asp, "llm", lambda *a, **k: WISH), patch.object(asp, "remember", lambda *a, **k: 7), \
                patch.object(asp, "_embed", lambda t: None), patch.object(asp, "_may_post_wish", lambda oc, t: False), \
                patch.dict(sys.modules, {"nova_config": cfg}), redirect_stdout(io.StringIO()) as buf:
            asp.pursue(cur, None, {"seeds": ["s1", "s2"]})
        self.assertEqual(len(cur.executed("INSERT INTO feature_wishes")), 1)
        self.assertEqual(cfg.posts, [])
        self.assertIn("wish notify skipped", buf.getvalue())

    def test_filed_wish_lineage_carries_memory_id_and_first_seed(self):
        cur = _interior()
        with patch.object(asp, "llm", lambda *a, **k: WISH), patch.object(asp, "remember", lambda *a, **k: "mem-9"), \
                patch.object(asp, "_embed", lambda t: None), patch.dict(sys.modules, {"nova_config": _cfg()}), \
                _no_gate(), _quiet():
            asp.pursue(cur, None, {"seeds": ["seed-one", "seed-two"]})
        params = cur.executed("INSERT INTO feature_wishes")[0][1]
        self.assertEqual(params[3], "seed-one")
        self.assertEqual(json.loads(params[4])["mem_id"], "mem-9")

    def test_approve_marks_acknowledged_and_queues_once(self):
        cur = _Cur([("FROM feature_wishes WHERE id", ("Mic", "d", "w", "seed", "wished")),
                    ("FROM claude_sessions", None), ("INSERT INTO claude_queue", (501,))])
        with _quiet():
            self.assertEqual(asp.approve_wish(cur, 42, by="Jordan"), 501)
        q = cur.executed("INSERT INTO claude_queue")
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0][1][0], "nova_aspirations")           # no session -> fallback id
        self.assertIn("(approved by Jordan)", q[0][1][1])
        self.assertEqual(cur.executed("UPDATE feature_wishes SET status='acknowledged'")[0][1], (42,))


# ── Functional ────────────────────────────────────────────────────────────────
class TestFunctional(unittest.TestCase):
    def _main(self, cur, argv):
        buf = io.StringIO()
        with patch.object(asp.psycopg2, "connect", lambda *a, **k: _Conn(cur)), \
                patch.object(sys, "argv", ["nova_aspirations.py", *argv]), redirect_stdout(buf):
            rc = asp.main()
        return rc, buf.getvalue()

    def test_cli_approve_golden_path(self):
        cur = _Cur([("FROM feature_wishes WHERE id", ("Mic", "d", "w", "seed", "wished")),
                    ("FROM claude_sessions", ("sess-1",)), ("INSERT INTO claude_queue", (777,))])
        rc, out = self._main(cur, ["--approve", "42"])
        self.assertEqual(rc, 0)
        self.assertIn("queued claude_queue #777", out)

    def test_cli_approve_unknown_wish_exits_one(self):
        rc, out = self._main(_Cur([("FROM feature_wishes WHERE id", None)]), ["--approve", "999"])
        self.assertEqual(rc, 1)
        self.assertIn("no wish #999", out)

    def test_cli_approve_merged_wish_exits_one_and_queues_nothing(self):
        cur = _Cur([("FROM feature_wishes WHERE id", ("Mic", "d", "w", "seed", "merged"))])
        rc, out = self._main(cur, ["--approve", "72"])
        self.assertEqual(rc, 1)
        self.assertIn("'merged' — not queueing", out)
        self.assertEqual(cur.executed("claude_queue"), [])

    def test_cli_pg_down_raises_after_retries(self):
        def boom(*a, **k):
            raise OSError("pg down")
        with patch.object(asp.psycopg2, "connect", boom), patch.object(asp.time, "sleep", lambda s: None), \
                patch.object(sys, "argv", ["x", "--approve", "1"]), _quiet():
            with self.assertRaises(OSError):
                asp.main()


# ── Frame ─────────────────────────────────────────────────────────────────────
class TestFrame(unittest.TestCase):
    def test_help_lists_approve(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--approve", r.stdout)

    def test_import_is_side_effect_free(self):
        r = subprocess.run([sys.executable, "-c", "import nova_aspirations"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_public_signatures(self):
        self.assertEqual(list(inspect.signature(asp.is_duplicate_wish).parameters),
                         ["oc", "title", "desc", "why", "embed"])
        self.assertEqual(list(inspect.signature(asp.approve_wish).parameters), ["oc", "wish_id", "by"])
        self.assertIn("rng", inspect.signature(asp._seeds).parameters)


if __name__ == "__main__":
    unittest.main()
