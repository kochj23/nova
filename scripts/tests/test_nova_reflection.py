#!/usr/bin/env python3
"""Tests for nova_reflection.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_reflection.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with patch("psycopg2.connect", side_effect=OSError("offline test")):
    rf = _load("reflection_t", SCRIPT)
SRC = SCRIPT.read_text()
rf.post_both = MagicMock()
rf.notify = MagicMock()
rf.log = lambda m: None
URLOPEN = MagicMock(side_effect=RuntimeError("urlopen not mocked in test"))
rf.urllib = types.SimpleNamespace(request=types.SimpleNamespace(Request=urllib.request.Request, urlopen=URLOPEN))
rf._connect = MagicMock(side_effect=RuntimeError("PG not mocked in test"))

CAND = [(1, "imessage", "We picked up the blue tile from the place on Olive, remember to return the extra box " * 2),
        (2, "imessage", "your verification code is 123456 do not share it with anyone at all ok thanks " * 2),
        (3, "automotive", "Oil change at 87k miles, they said the rear brakes have maybe six months left on them " * 2)]


class _DB:
    """Routes fetch()/execute() by SQL substring; records every call."""
    def __init__(self, asked=0, candidates=CAND, qrow=("Q?", "imessage", "excerpt")):
        self.asked = asked; self.candidates = candidates; self.qrow = qrow; self.calls = []; self.next_id = 100

    def fetch(self, dsn, sql, args=()):
        self.calls.append((dsn, sql, args))
        if "GROUP BY 1" in sql:
            return [("reddit", 900), ("scanner", 100)]
        if "SELECT count(*) FROM memories" in sql:
            return [(1000,)]
        if "ORDER BY random() LIMIT 20" in sql:
            return [("reddit", "a thread")]
        if "reflection_questions\n        WHERE asked_at" in sql:
            return [(self.asked,)]
        if "access_count" in sql:
            return list(self.candidates)
        if sql.lstrip().startswith("INSERT INTO reflection_questions"):
            self.next_id += 1
            return [(self.next_id,)]
        if "WHERE answer IS NULL" in sql:
            return [(101, "open q")]
        if "WHERE id=%s" in sql:
            return [self.qrow] if self.qrow else []
        return []

    def execute(self, dsn, sql, args=()):
        self.calls.append((dsn, sql, args))

    def patches(self):
        return patch.object(rf, "fetch", side_effect=self.fetch), patch.object(rf, "execute", side_effect=self.execute)


def _llm_reply(text):
    r = MagicMock(); r.read.return_value = json.dumps({"response": text}).encode(); r.status = 200
    r.__enter__ = lambda s: s; r.__exit__ = lambda *a: False
    return r


def _main(db, argv, llm=None):
    rf.post_both.reset_mock()
    f, e = db.patches()
    with f, e, patch.object(rf, "local_llm", side_effect=llm or (lambda *a, **k: "")), \
            patch.object(rf, "ingest_memory", return_value=True) as ing, patch.object(sys, "argv", ["x", *argv]):
        rf.main()
    return ing


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_param_sql(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r'(fetch|execute)\(PG_\w+,\s*f["\']', SRC))

    def test_never_say_guard_drops_credential_shapes(self):
        db = _DB()
        f, e = db.patches()
        seen = []
        with f, e, patch.object(rf, "local_llm", side_effect=lambda s, u, **k: seen.append(u) or ""):
            qs = rf.pick_questions(3, dry=True)
        self.assertNotIn("verification code", seen[0])
        self.assertEqual(len(qs), 2)
        for s in ("your PIN is 4421", "SSN on file", "acct 12345678", "one-time passcode"):
            self.assertTrue(rf.NEVER_SAY.search(s), s)

    def test_llm_is_local_only(self):
        self.assertTrue(rf.OLLAMA_URL.startswith("http://127.0.0.1:"))
        self.assertIsNone(re.search(r"openrouter|api\.openai|anthropic\.com", SRC, re.I))


class TestPerformance(unittest.TestCase):
    def test_never_say_10k(self):
        t0 = time.perf_counter()
        hits = sum(1 for i in range(10_000) if rf.NEVER_SAY.search(f"note {i} about the garden and tile " * 5))
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(hits, 0)


class TestRetry(unittest.TestCase):
    def test_local_llm_down_falls_back_never_cloud(self):
        # RETRY GAP: local_llm — one Ollama attempt; failure returns "" and callers use the template fallback
        URLOPEN.reset_mock(); URLOPEN.side_effect = OSError("ollama down")
        try:
            self.assertEqual(rf.local_llm("s", "u"), "")
            self.assertFalse(rf.ingest_memory("t", "title"))     # RETRY GAP: ingest_memory — one POST, returns False
        finally:
            URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")
        self.assertEqual(URLOPEN.call_count, 2)
        self.assertTrue(all(c.args[0].full_url in (rf.OLLAMA_URL, rf.INGEST_API) for c in URLOPEN.call_args_list))

    def test_episode_template_fallback(self):
        db = _DB()
        f, e = db.patches()
        with f, e, patch.object(rf, "local_llm", return_value=""):
            ep = rf.build_episode(dry=True)
        self.assertIn("ingested 1000 memories in 24 hours", ep)
        self.assertIn("reddit (900)", ep)


class TestUnit(unittest.TestCase):
    def test_local_llm_strips_think(self):
        URLOPEN.reset_mock(); URLOPEN.side_effect = None; URLOPEN.return_value = _llm_reply("<think>x</think> answer")
        try:
            self.assertEqual(rf.local_llm("s", "u"), "answer")
        finally:
            URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")
        body = json.loads(URLOPEN.call_args.args[0].data)
        self.assertEqual((body["model"], body["stream"]), (rf.OLLAMA_MODEL, False))

    def test_pick_questions_parses_llm_json_and_caps(self):
        db = _DB()
        f, e = db.patches()
        raw = 'sure: [{"idx": 1, "question": " What tile? "}, {"idx": 9, "question": "bad idx"}, {"idx": 0, "question": "Brakes?"}]'
        with f, e, patch.object(rf, "local_llm", return_value=raw):
            qs = rf.pick_questions(1, dry=True)
        self.assertEqual(qs, [(None, "What tile?")])
        self.assertEqual(rf.pick_questions(0, dry=True), [])


class TestIntegration(unittest.TestCase):
    def test_questions_persist_to_reflection_questions_and_sources_are_personal(self):
        db = _DB()
        f, e = db.patches()
        with f, e, patch.object(rf, "local_llm", return_value=""):
            qs = rf.pick_questions(2, dry=False)
        self.assertEqual([q[0] for q in qs], [101, 102])
        cand_q = [c for c in db.calls if "access_count" in c[1]][0]
        self.assertEqual((cand_q[0], cand_q[2]), (rf.PG_MEM, (rf.PERSONAL_SOURCES,)))
        ins = [c for c in db.calls if "INSERT INTO reflection_questions" in c[1]][0]
        self.assertEqual(ins[0], rf.PG_OPS)
        self.assertEqual(ins[2][:2], ("1", "imessage"))


class TestFunctional(unittest.TestCase):
    def test_nightly_golden_path_posts_to_nova_claude(self):
        ing = _main(_DB(asked=1), [], llm=lambda s, u, **k: "A wry day." if "autobiography" in s else "")
        self.assertTrue(ing.call_args.args[0].startswith("[Nova reflection — episode of"))
        msg, kw = rf.post_both.call_args.args[0], rf.post_both.call_args.kwargs
        self.assertIn("A wry day.", msg)
        self.assertIn("*Q101.* open q", msg)
        self.assertEqual(kw["slack_channel"], rf.SLACK_CLAUDE)

    def test_dry_run_posts_and_ingests_nothing(self):
        db = _DB()
        ing = _main(db, ["--dry-run"])
        rf.post_both.assert_not_called(); ing.assert_not_called()
        self.assertFalse(any("INSERT" in c[1] for c in db.calls))

    def test_answer_path_and_unknown_id(self):
        db = _DB()
        ing = _main(db, ["--answer", "7", "It was the kitchen remodel"])
        upd = [c for c in db.calls if c[1].startswith("UPDATE reflection_questions")][0]
        self.assertEqual(upd[2], ("It was the kitchen remodel", 7))
        self.assertIn("Jordan's answer: It was the kitchen remodel", ing.call_args.args[0])
        with self.assertRaises(SystemExit):
            _main(_DB(qrow=None), ["--answer", "8", "x"])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys;sys.path.insert(0,'.');import psycopg2;"
                "psycopg2.connect=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
                "import importlib.util as u;s=u.spec_from_file_location('m','nova_reflection.py');"
                "m=u.module_from_spec(s);s.loader.exec_module(m);print('ok')")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
