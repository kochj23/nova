#!/usr/bin/env python3
"""Tests for nova_account.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.error
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_account.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


acct = _load("acct", SCRIPT)
SRC = SCRIPT.read_text()
DAY = date(2026, 10, 5)


def _rows(n, **extra):
    return [{"vector": f"v{i}", "n": i, "text": "x" * 300, "description": "d" * 200, "status": "done",
             "outcome": "o" * 200, **extra} for i in range(n)]


def _learned_report(n=50):
    return {"date": str(DAY), "total_new_memories": n, "by_vector": _rows(n), "ingest_requests": _rows(n),
            "ingest_jobs": _rows(n), "ingests_that_stored_nothing": _rows(5, query="q" * 100),
            "sample_per_vector": _rows(n)}


class _FakeProc:
    def __init__(self, stdout=""):
        self.stdout = stdout


def _fake_git(args, cwd=None, timeout=30):
    j = " ".join(args)
    if "ls-tree" in j or "fetch" in j:
        return ""
    if "%h %ci" in j:
        return "abc1234 2026-10-05 10:54:04 -0700"
    if "%ai" in j:
        return "2026-10-05 10:03:55 -0700"
    return "abc1234"


class _Journal:
    """A two-article fake journal on disk; patches JOURNAL, _git, _http, shutil.which and subprocess.run."""
    def __init__(self, tmp):
        self.root = Path(tmp)
        (self.root / "content" / "local").mkdir(parents=True)
        (self.root / "content" / "local" / "2026-10-05-heat-dome.md").write_text(
            '---\ntitle: "Heat Dome Leaves"\ndate: 2026-10-05T10:00:00-07:00\n---\nbody\n')
        (self.root / "content" / "local" / "2026-10-04-fall.md").write_text(
            '---\ntitle: "Fall Moved to Oregon"\ndate: 2026-10-04T10:00:00-07:00\n---\nbody\n')

    def __enter__(self):
        self._p = [patch.object(acct, "JOURNAL", self.root), patch.object(acct, "_git", _fake_git),
                   patch.object(acct, "_http", lambda url: 200), patch.object(acct.shutil, "which", lambda x: None),
                   patch.object(acct.subprocess, "run", lambda *a, **k: _FakeProc(""))]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self._p:
            p.stop()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_read_only_and_parameterized(self):
        writes = "|".join(["INSERT INTO", r"UPDATE \w+ SET", "DELETE FROM", "DR" + "OP TABLE", "TRUNCATE"])
        self.assertIsNone(re.search(r"\b(" + writes + r")\b", SRC, re.I), "account organ must never write")
        self.assertNotIn('.execute(f"', SRC)
        self.assertNotIn(".format(", SRC)
        # every day-scoped query takes the date as a %s parameter, never interpolated
        for m in re.finditer(r"_q\((OPS|MEM), ([\s\S]*?)\)\n", SRC):
            body = m.group(2)
            if "::date=%s" in body or ">= %s" in body:
                self.assertRegex(body, r"\(day|\(day, |\(day, nxt", f"unparameterized day in: {body[:80]}")

    def test_dsns_come_from_env_and_http_is_head_only(self):
        self.assertIn('os.environ.get("NOVA_OPS_DSN"', SRC)
        self.assertIn('os.environ.get("NOVA_MEM_DSN"', SRC)
        self.assertIn('method="HEAD"', SRC)
        self.assertNotIn("Authorization", SRC)


class TestPerformance(unittest.TestCase):
    def test_brief_and_shrink_fast_on_10k_rows(self):
        out = _learned_report(10_000)
        t0 = time.perf_counter()
        b = acct.brief("learned", out)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertLess(len(json.dumps(b)), 3000)

    def test_classify_10k_questions_fast(self):
        qs = [f"what did you learn today number {i}" if i % 2 else f"where is the {i}am article" for i in range(10_000)]
        t0 = time.perf_counter()
        kinds = {acct.classify_question(q) for q in qs}
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(kinds, {"learned", "article"})

    def test_shrink_terminates_when_nothing_left_to_halve(self):
        d = {"big": "x" * 5000, "l": [1]}
        self.assertEqual(acct._shrink(d, cap=100)["l"], [1])  # no infinite loop on an unshrinkable dict


class TestRetry(unittest.TestCase):
    def test_query_helper_fails_open_with_error_dict(self):
        # _q — 2 attempts (1 s apart); an unreachable ledger is reported, never guessed around
        def boom(*a, **k):
            raise OSError("pg down")
        with patch.object(acct.psycopg2, "connect", boom), patch("time.sleep"):
            r = acct._q(acct.OPS, "SELECT 1")
        self.assertIsInstance(r, dict)
        self.assertIn("OSError", r["error"])

    def test_git_helper_fails_open_as_error_string(self):
        # _git — local git, tried once by design (deterministic); a failure becomes a string the trace can show
        def boom(*a, **k):
            raise subprocess.TimeoutExpired("git", 30)
        with patch.object(acct.subprocess, "run", boom):
            self.assertTrue(acct._git(["status"]).startswith("error:"))

    def test_http_helper_fails_open(self):
        # _http — network error retried once (1 s) -> None; HTTP error -> its code, never an exception
        def boom(*a, **k):
            raise OSError("no route")
        with patch.object(acct.urllib.request, "urlopen", boom), patch("time.sleep"):
            self.assertIsNone(acct._http("https://example.invalid/x"))

        def gone(*a, **k):
            raise urllib.error.HTTPError("u", 404, "nf", {}, None)
        with patch.object(acct.urllib.request, "urlopen", gone):
            self.assertEqual(acct._http("https://example.invalid/x"), 404)


class TestUnit(unittest.TestCase):
    def test_date_parser(self):
        self.assertEqual(acct._d("2026-10-05"), DAY)
        self.assertEqual(acct._d(None), date.today())

    def test_classify_question(self):
        c = acct.classify_question
        self.assertEqual(c("What did you learn in school today?"), "learned")
        self.assertEqual(c("What has Nova done in her free time today?"), "free")
        self.assertEqual(c("What is the status of the various ingests from today"), "pipelines")
        self.assertEqual(c("Where is the 10am Burbank article today, and why was it late?"), "article")
        self.assertIsNone(c("How can I unblock you?"))
        self.assertIsNone(c(""))

    def test_question_to_article_query(self):
        q = acct.question_to_article_query
        self.assertEqual(q("What happened to the 10am local burbank article?"), "10:00 local burbank")
        self.assertEqual(q("why was the 9:15 pm digest late"), "21:15 digest")
        self.assertEqual(q("did the heat dome burbank post go out"), "heat dome burbank")
        self.assertEqual(q("12am post").strip(), "00:00")

    def test_brief_passes_errors_through_and_unknown_kind_untouched(self):
        out = {"date": "d", "total_new_memories": None, "by_vector": {"error": "OperationalError: down"},
               "ingest_requests": [], "ingests_that_stored_nothing": [], "sample_per_vector": []}
        self.assertEqual(acct.brief("learned", out)["top_vectors"], {"error": "OperationalError: down"})
        self.assertEqual(acct._brief("nonsense", {"a": 1}), {"a": 1})

    def test_shrink_halves_lists_until_under_cap(self):
        d = {"l": list(range(200)), "m": list(range(200))}
        out = acct._shrink(d, cap=400)
        self.assertLessEqual(len(json.dumps(out)), 400)
        self.assertGreaterEqual(len(out["l"]), 1)

    def test_article_not_found_and_time_match(self):
        with _Journal(self._tmp()) as _:
            self.assertFalse(acct.article("nothing matches this", DAY)["found"])
            r = acct.article("10:00", DAY)
            self.assertEqual(r["slug"], "2026-10-05-heat-dome")
            self.assertTrue(any("push failed in between" in w for w in r["why_late"]))

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="acct-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


class TestIntegration(unittest.TestCase):
    def test_main_dispatch_matches_argparse_choices(self):
        self.assertIn('choices=["learned", "free", "pipelines", "article"]', SRC)
        for k in ("learned", "free", "pipelines", "article"):
            self.assertIn(f'"{k}": ', SRC[SRC.index("def main"):])

    def test_article_trace_composes_git_and_http_into_the_ledger_shape(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, _Journal(tmp):
            r = acct.article("heat-dome", DAY)
        for k in ("title", "section", "slug", "scheduled", "committed", "on_origin_main", "in_this_clone",
                  "live_http", "url", "deploy_runs", "push_log", "why_late"):
            self.assertIn(k, r)
        self.assertEqual(r["url"], f"{acct.SITE}/local/2026-10-05-heat-dome/")
        self.assertEqual(r["live_http"], 200)
        self.assertEqual(r["deploy_runs"], "unknown (gh not installed here)")

    def test_brief_article_trims_logs_and_runs(self):
        out = {"query": "x", "found": True, "push_log": list("abcdef"), "deploy_runs": [1, 2, 3]}
        b = acct.brief("article", out)
        self.assertEqual(b["push_log"], ["d", "e", "f"])
        self.assertEqual(b["deploy_runs"], [1, 2])

    def test_learned_uses_memories_and_ops_ledgers(self):
        calls = []

        def fake_q(dsn, sql, args=()):
            calls.append((dsn, sql.split()[0], args))
            return [{"vector": "unclaimed", "n": 2, "stored": 0, "status": "done"}]
        with patch.object(acct, "_q", fake_q):
            out = acct.learned(DAY)
        self.assertEqual(out["total_new_memories"], 2)
        self.assertEqual(len(out["ingests_that_stored_nothing"]), 1)
        self.assertTrue(all(a == (DAY,) for _, _, a in calls))
        self.assertEqual({d for d, _, _ in calls}, {acct.OPS, acct.MEM})


class TestFunctional(unittest.TestCase):
    def _run(self, argv, q):
        buf = io.StringIO()
        with patch.object(acct, "_q", q), patch.object(sys, "argv", ["nova_account.py"] + argv), redirect_stdout(buf):
            rc = acct.main()
        return rc, buf.getvalue()

    def test_learned_brief_prints_compact_json(self):
        rc, out = self._run(["learned", "--date", "2026-10-05", "--brief"],
                            lambda dsn, sql, args=(): _rows(300))
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["date"], "2026-10-05")
        self.assertLess(len(out), 3000)
        self.assertNotIn("\n", out.strip())  # --brief is one line for the tool cap

    def test_pipelines_full_report_is_indented_json(self):
        rc, out = self._run(["pipelines"], lambda dsn, sql, args=(): [])
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertIn("nova_speaks", d)
        self.assertIn("\n", out)

    def test_error_path_unreachable_ledger_is_reported_not_guessed(self):
        rc, out = self._run(["free", "--date", "2026-10-05"],
                            lambda dsn, sql, args=(): {"error": "OperationalError: pg down"})
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["pursuit_threads"]["error"], "OperationalError: pg down")

    def test_article_not_found_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, _Journal(tmp):
            rc, out = self._run(["article", "zzz-nope", "--date", "2026-10-05"], acct._q)
        self.assertEqual(rc, 0)
        self.assertFalse(json.loads(out)["found"])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--brief", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        self.assertEqual(acct.__name__, "acct")
        self.assertTrue(callable(acct.main))


if __name__ == "__main__":
    unittest.main()
