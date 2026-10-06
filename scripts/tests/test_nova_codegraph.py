#!/usr/bin/env python3
"""Tests for nova_codegraph.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_codegraph.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("ncg_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cg = _load()

A = '''import os
from helpers import shout

class Greeter(Base):
    def hello(self, name):
        return shout(name)

def main():
    g = Greeter()
    g.hello("x")
    shout("y")
'''
B = '''def shout(s):
    return s.upper()
'''


class _Repo(unittest.TestCase):
    """Each test gets a temp repo and a temp DB (never touches ~/.openclaw/cache)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"; self.root.mkdir()
        (self.root / "a.py").write_text(A)
        (self.root / "helpers.py").write_text(B)
        p = patch.object(cg, "DB", str(Path(self.tmp.name) / "cache" / "cg.db"))
        p.start(); self.addCleanup(p.stop)


class TestSecurity(_Repo):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_queries_are_parameterized(self):
        # the only f-string SQL interpolates table/column names from a fixed tuple, values go via ?
        for m in re.finditer(r'execute\(f"([^"]*)"', SRC):
            self.assertIn("=?", m.group(1))
        cg.index(str(self.root))
        hostile = "x' OR '1'='1"
        self.assertIn("no definition", cg.where(hostile))
        self.assertTrue(cg.callers(hostile).startswith("0 call sites"))

    def test_db_lives_under_cache_by_default(self):
        self.assertIn(".openclaw/cache/codegraph.db", SRC)


class TestPerformance(_Repo):
    def test_index_10k_functions_fast(self):
        big = "\n".join(f"def f{i}():\n    return f{i-1}()\n" for i in range(1, 10_001))
        (self.root / "big.py").write_text(big)
        t0 = time.perf_counter()
        out = cg.index(str(self.root))
        self.assertLess(time.perf_counter() - t0, 10.0)
        self.assertIn("parse errors", out)
        self.assertIn("call sites", cg.callers("f5"))


class TestRetry(_Repo):
    def test_parse_error_fails_open(self):
        # RETRY GAP: _index_one()/ast.parse — no retry (local, deterministic); a bad file is counted, not raised
        (self.root / "broken.py").write_text("def (:\n")
        out = cg.index(str(self.root))
        self.assertIn("(1 parse errors)", out)
        self.assertIn("function", cg.where("shout"))

    def test_unreadable_file_returns_false(self):
        c = sqlite3.connect(":memory:")
        c.executescript("CREATE TABLE symbols(name,kind,file,line,detail);CREATE TABLE edges(caller,caller_file,callee,line);"
                        "CREATE TABLE imports(file,module,name);")
        self.assertFalse(cg._index_one(c.cursor(), Path(self.tmp.name) / "missing.py"))


class TestUnit(_Repo):
    def test_where_callers_callees(self):
        cg.index(str(self.root))
        self.assertIn("class", cg.where("Greeter"))
        self.assertIn("(Base)", cg.where("Greeter"))
        self.assertIn("method", cg.where("hello"))
        self.assertIn("2 call sites of 'shout'", cg.callers("shout"))
        self.assertIn("hello", cg.callees("main"))
        self.assertIn("no definition for 'nope'", cg.where("nope"))

    def test_importers(self):
        cg.index(str(self.root))
        self.assertIn("a.py  (imports shout)", cg.importers("helpers"))
        self.assertIn("1 files import 'os'", cg.importers("os"))

    def test_scan_skips_archive_and_git(self):
        (self.root / "archive").mkdir(); (self.root / "archive" / "old.py").write_text("x=1")
        names = {p.name for p in cg._scan(self.root)}
        self.assertEqual(names, {"a.py", "helpers.py"})

    def test_unparse_failure(self):
        self.assertEqual(cg._unparse(object()), "?")


class TestIntegration(_Repo):
    def test_reindex_one_file_replaces_rows(self):
        cg.index(str(self.root))
        (self.root / "helpers.py").write_text("def whisper(s):\n    return s\n")
        c = cg._conn(); cur = c.cursor()
        self.assertTrue(cg._index_one(cur, self.root / "helpers.py"))
        c.commit(); c.close()
        self.assertIn("no definition", cg.where("shout"))
        self.assertIn("helpers.py", cg.where("whisper"))

    def test_stats_and_demo_compose(self):
        cg.index(str(self.root))
        s = cg.stats()
        self.assertIn("files: 2", s)
        self.assertIn("Query: 'who calls shout()?'", cg.demo())


class TestFunctional(_Repo):
    def test_cli_index_then_where(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1", "HOME": self.tmp.name}
        r = subprocess.run([sys.executable, str(SCRIPT), "index", str(self.root)], capture_output=True,
                           text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("indexed 2 files", r.stdout)
        self.assertTrue((Path(self.tmp.name) / ".openclaw/cache/codegraph.db").exists())
        r = subprocess.run([sys.executable, str(SCRIPT), "where", "shout"], capture_output=True,
                           text=True, timeout=30, env=env)
        self.assertIn("helpers.py:1", r.stdout)

    def test_unknown_command(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "bogus"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": self.tmp.name})
        self.assertIn("unknown cmd", r.stdout)


class TestFrame(unittest.TestCase):
    def test_no_args_prints_usage(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Usage:", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_codegraph"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
