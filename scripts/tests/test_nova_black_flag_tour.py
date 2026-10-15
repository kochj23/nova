#!/usr/bin/env python3
"""Tests for nova_black_flag_tour.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Hashing and grouping on inline sources; git through a throwaway
local repository in a tempdir; DB through fake cursors.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_black_flag_tour.py"
spec = importlib.util.spec_from_file_location("black_flag_under_test", SCRIPT)
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)
SRC = SCRIPT.read_text()

# Every outbound side effect is stubbed for the whole module (a test that forgets its own patch must
# never reach the real event bus). Individual tests patch again inside this.
_GUARD = mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=mock.Mock(return_value=True))})


def setUpModule():
    _GUARD.start()


def tearDownModule():
    _GUARD.stop()


REMEMBER_OLD = '''
def remember(text, source="x"):
    """Store a memory."""
    body = json.dumps({"text": text, "source": source}).encode()
    req = urllib.request.Request(MEMORY_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status == 200
'''
REMEMBER_FIXED = '''
def remember(text, source="x"):
    """Store a memory, retried."""
    body = json.dumps({"text": text, "source": source}).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(MEMORY_URL, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status == 200
        except OSError:
            time.sleep(2 ** attempt)
    return False
'''
UNRELATED_REMEMBER = '''
def remember(key):
    return CACHE.get(key)
'''


def _fn(src, name="remember"):
    return bf.functions_in(src)[name]


def _funcs(*pairs):
    """(path, source) -> function dicts as scan() returns them."""
    out = []
    for path, src in pairs:
        for name, fp in bf.functions_in(src).items():
            out.append(dict(fp, path=path, name=name))
    return out


class _Cur:
    def __init__(self, routes=None):
        self.routes = routes or []
        self.sql, self.params, self._last = [], [], ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def _route(self, default):
        for needle, val in self.routes:
            if needle in self._last:
                return val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])


def _git(repo, *args, date=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t" + "@" + "example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t" + "@" + "example.invalid")
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env, timeout=30)


class _Repo:
    """A throwaway repo: three copies of remember() committed 30 days ago, one fixed yesterday."""
    def __enter__(self):
        self.root = Path(tempfile.mkdtemp())
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        _git(self.root, "init", "-q")
        for n in ("a", "b", "c"):
            (self.scripts / f"nova_{n}.py").write_text("import json\n" + REMEMBER_OLD)
        (self.scripts / "nova_d.py").write_text(UNRELATED_REMEMBER)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "three towns", date="2026-09-01T10:00:00")
        (self.scripts / "nova_a.py").write_text("import json\n" + REMEMBER_FIXED)
        _git(self.root, "commit", "-q", "-am", "a: retry memory writes", date=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.patches = [mock.patch.object(bf, "REPO", self.root), mock.patch.object(bf, "SCRIPTS_DIR", self.scripts),
                        mock.patch.object(bf, "GATEWAY_PKG", self.root / "gateway" / "nova_gateway")]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.root, ignore_errors=True)


HAVE_GIT = shutil.which("git") is not None


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_ips_emails_or_home_paths(self):
        self.assertNotRegex(SRC, r"(?i)(password|api_key|token)\s*=\s*['\"][^'\"]+['\"]")
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotRegex(SRC, r"/" + r"Users/|/" + r"home/")
        self.assertNotRegex(SRC, r"[\w.+-]+@[\w-]+\.[a-z]{2,}")
        self.assertNotRegex(SRC, r"(?i)\b([0-9a-f]{2}:){5}[0-9a-f]{2}\b")

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(\s*f[\"']")
        self.assertNotRegex(SRC, r"execute\([^)]*%\s*\(")

    def test_never_edits_code_and_writes_only_its_own_tables(self):
        self.assertNotRegex(SRC, r"write_text|open\([^)]*['\"][wa]['\"]|os\.remove|unlink|shutil")
        for verb in (r"\bDROP\s", r"TRUNCATE", r"launchctl", r"systemctl", r"os\.system", r"shell=True"):
            self.assertNotRegex(SRC, verb)
        tables = set(re.findall(r"(?:INSERT INTO|\"UPDATE|DELETE FROM)\s+([\w.]+)", SRC))
        self.assertTrue(tables)
        self.assertTrue(all(t.startswith("black_flag_") for t in tables), tables)
        git_verbs = set(re.findall(r'git\("([\w-]+)"', SRC))
        self.assertTrue(git_verbs <= {"rev-parse", "rev-list", "diff", "log"}, git_verbs)

    def test_ddl_goes_through_safe_ddl(self):
        self.assertIn("nova_pg_schema.safe_ddl(cur, SCHEMA)", SRC)
        self.assertNotIn("cur.execute(SCHEMA)", SRC)


class TestPerformance(unittest.TestCase):
    def test_hash_and_group_10k_functions_fast(self):
        srcs = [REMEMBER_OLD.replace("remember", f"fn{i % 400}").replace("10", str(i % 7)) for i in range(10000)]
        t = time.monotonic()
        funcs = []
        for i, s in enumerate(srcs):
            for name, fp in bf.functions_in(s).items():
                funcs.append(dict(fp, path=f"scripts/f{i}.py", name=name))
        groups = bf.build_groups(funcs)
        self.assertLess(time.monotonic() - t, 20.0)
        self.assertTrue(groups)

    def test_run_budget_under_two_minutes(self):
        self.assertLess(bf.TIME_BUDGET_S, 120)
        self.assertIn('"cat-file", "--batch"', SRC)          # blobs in one process, not one per file
        self.assertNotIn("copy.deepcopy(", SRC)                # 30 s of a 60 s run before it was removed


class TestRetry(unittest.TestCase):
    # RETRY GAP: git / git_blobs — no retry; a failing git fails OPEN (empty history, divergence check
    # skipped and said so), the consolidation ranking still runs. PG connect raises; the weekly run
    # is retried by the scheduler.
    def test_git_failure_fails_open(self):
        with mock.patch.object(bf.subprocess, "run", side_effect=OSError("no git")):
            self.assertEqual(bf.git("log"), "")
            self.assertEqual(bf.git_blobs(["HEAD:x.py"]), {"HEAD:x.py": None})
            self.assertEqual(bf.git_window(8, {"scripts/x.py"}), ("", "", set()))
        with mock.patch.object(bf.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 60)):
            self.assertEqual(bf.git("log"), "")

    def test_failed_digest_leaves_divergences_unnotified(self):
        res = {"groups": [], "measures": bf.measures([]), "divergences": []}
        cur = _Cur()
        with mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=mock.Mock(return_value=False))}):
            div = {"func": "remember", "path": "scripts/a.py", "changed": 1, "copies": 3, "siblings": ["b"],
                   "siblings_with_old_body": ["b"], "commit_sha": "abc", "commit_subject": "fix"}
            self.assertFalse(bf.digest(cur, res, [div], 8))
        self.assertFalse(any("SET notified = true" in q for q in cur.sql))


class TestUnit(unittest.TestCase):
    def test_docstrings_comments_and_formatting_are_not_variants(self):
        a = _fn(REMEMBER_OLD)
        b = _fn(REMEMBER_OLD.replace('"""Store a memory."""', "# just a comment").replace("    body", "    body "))
        self.assertEqual(a["body_hash"], b["body_hash"])

    def test_renames_change_body_not_shape(self):
        a = _fn(REMEMBER_OLD)
        b = _fn(REMEMBER_OLD.replace("text", "msg").replace("timeout=10", "timeout=30"))
        self.assertNotEqual(a["body_hash"], b["body_hash"])
        self.assertEqual(a["shape_hash"], b["shape_hash"])
        self.assertNotEqual(a["shape_hash"], _fn(REMEMBER_FIXED)["shape_hash"])

    def test_functions_in_edge_cases(self):
        self.assertEqual(bf.functions_in("def broken(:\n"), {})
        self.assertEqual(bf.functions_in(""), {})
        self.assertNotIn("main", bf.functions_in("def main():\n    return 1\n"))
        self.assertIn("x", bf.functions_in("async def x():\n    await y()\n"))
        self.assertEqual(bf.jaccard(frozenset(), frozenset()), 1.0)

    def test_groups_need_a_real_copy(self):
        groups = bf.build_groups(_funcs(("scripts/a.py", REMEMBER_OLD), ("scripts/b.py", REMEMBER_OLD),
                                        ("scripts/c.py", REMEMBER_FIXED)))
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual((g["label"], g["copies"], g["variants"], g["score"]), ("remember", 3, 2, 6))
        lonely = bf.build_groups(_funcs(("scripts/a.py", "def helper():\n    return 1\n"),
                                        ("scripts/b.py", "def helper(x):\n    while x:\n        x = f(x, 2)\n"
                                                         "    return g(x) or h(x)\n")))
        self.assertEqual(lonely, [])                          # same name, nothing actually copied

    def test_measures(self):
        groups = [{"copies": 6}, {"copies": 2}, {"copies": 5}]
        self.assertEqual(bf.measures(groups), {"groups": 3, "groups_big": 2, "redundant_copies": 10,
                                               "functions_in_groups": 13})

    def test_selftest(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(bf.selftest(), 0)
        self.assertIn("selftest ok", out.getvalue())


class TestIntegration(unittest.TestCase):
    def _divs(self, a_new, extra=()):
        pairs = [("scripts/a.py", a_new), ("scripts/b.py", REMEMBER_OLD), ("scripts/c.py", REMEMBER_OLD), *extra]
        groups = bf.build_groups(_funcs(*pairs))
        base = {"scripts/a.py": bf.functions_in(REMEMBER_OLD)}
        head = {"scripts/a.py": bf.functions_in(a_new)}
        return bf.find_divergences(groups, base, head, {"scripts/a.py"})

    def test_fix_in_one_of_three_is_divergent(self):
        d = self._divs(REMEMBER_FIXED, extra=[("scripts/d.py", UNRELATED_REMEMBER)])
        self.assertEqual(len(d), 1)
        d = d[0]
        self.assertEqual((d["func"], d["path"], d["changed"], d["copies"]), ("remember", "scripts/a.py", 1, 3))
        self.assertEqual(d["siblings_with_old_body"], ["scripts/b.py:remember", "scripts/c.py:remember"])
        self.assertNotIn("scripts/d.py:remember", d["siblings"])        # unrelated same-name is not a sibling

    def test_catching_up_to_a_sibling_is_not_divergent(self):
        pairs = [("scripts/a.py", REMEMBER_FIXED), ("scripts/b.py", REMEMBER_FIXED), ("scripts/c.py", REMEMBER_OLD)]
        groups = bf.build_groups(_funcs(*pairs))
        base = {"scripts/a.py": bf.functions_in(REMEMBER_OLD)}
        head = {"scripts/a.py": bf.functions_in(REMEMBER_FIXED)}
        self.assertEqual(bf.find_divergences(groups, base, head, {"scripts/a.py"}), [])

    def test_new_copy_is_not_a_fix(self):
        groups = bf.build_groups(_funcs(("scripts/a.py", REMEMBER_FIXED), ("scripts/b.py", REMEMBER_OLD)))
        self.assertEqual(bf.find_divergences(groups, {}, {"scripts/a.py": bf.functions_in(REMEMBER_FIXED)},
                                             {"scripts/a.py"}), [])

    @unittest.skipUnless(HAVE_GIT, "git not installed")
    def test_git_helpers_against_a_local_repo(self):
        with _Repo() as r:
            base, head, changed = bf.git_window(8, {"scripts/nova_a.py", "scripts/nova_b.py"})
            self.assertTrue(base and head and base != head)
            self.assertEqual(changed, {"scripts/nova_a.py"})
            blobs = bf.git_blobs([f"{base}:scripts/nova_a.py", f"{head}:scripts/nova_a.py", f"{head}:nope.py"])
            self.assertIn("urlopen", blobs[f"{base}:scripts/nova_a.py"])
            self.assertIn("for attempt", blobs[f"{head}:scripts/nova_a.py"])
            self.assertIsNone(blobs[f"{head}:nope.py"])
            by = bf.commits_by_path(base)
            self.assertEqual([c[1] for c in by["scripts/nova_a.py"]], ["a: retry memory writes"])

    def test_shared_helpers_are_imported(self):
        self.assertIn("nova_dsn.pg_dsn", SRC)
        self.assertIn("from nova_notify import notify", SRC)
        self.assertIn("import nova_pg_schema", SRC)


class TestFunctional(unittest.TestCase):
    @unittest.skipUnless(HAVE_GIT, "git not installed")
    def test_analyse_finds_the_fix_that_did_not_travel(self):
        with _Repo():
            res = bf.analyse(8)
        self.assertEqual(res["files"], 4)
        self.assertEqual(res["measures"]["groups"], 1)
        self.assertEqual(len(res["divergences"]), 1)
        d = res["divergences"][0]
        self.assertEqual(d["commit_subject"], "a: retry memory writes")
        self.assertEqual(d["siblings_with_old_body"], ["scripts/nova_b.py:remember", "scripts/nova_c.py:remember"])
        title, body = bf.render_digest(res["groups"], res["measures"], res["divergences"], 8)
        self.assertIn("1 divergent fix", title)
        self.assertIn("fix landed in 1 of 3 copies", body)
        self.assertIn("still on the old body: scripts/nova_b.py:remember", body)

    @unittest.skipUnless(HAVE_GIT, "git not installed")
    def test_scheduled_run_writes_tables_and_sends_digest(self):
        cur = _Cur([("RETURNING id", (1,))])
        conn = mock.Mock(cursor=mock.Mock(return_value=cur))
        notify = mock.Mock(return_value=True)
        ev = mock.Mock()
        with _Repo(), mock.patch.object(bf, "connect", return_value=conn), \
                mock.patch.object(bf, "ensure_schema"), \
                mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=notify),
                                              "psycopg2.extras": mock.Mock(execute_values=ev)}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(bf.main(["--scheduled"]), 0)
        self.assertEqual(ev.call_count, 2)                                  # groups + snapshots
        self.assertTrue(any(q.startswith("INSERT INTO black_flag_divergences") for q in cur.sql))
        self.assertTrue(any(q.startswith("INSERT INTO black_flag_runs") for q in cur.sql))
        self.assertEqual(cur.sql[0], "BEGIN")
        self.assertIn("COMMIT", cur.sql)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["category"], "black_flag_tour")

    def test_write_error_rolls_back(self):
        class Boom(_Cur):
            def execute(self, sql, params=None):
                super().execute(sql, params)
                if sql.startswith("INSERT INTO black_flag_runs"):
                    raise RuntimeError("disk full")
        cur = Boom()
        res = {"groups": [], "measures": bf.measures([]), "divergences": [], "files": 0, "functions": 0,
               "seconds": 0.1}
        with mock.patch.dict(sys.modules, {"psycopg2.extras": mock.Mock(execute_values=mock.Mock())}), \
                self.assertRaises(RuntimeError):
            bf.record(cur, res)
        self.assertEqual(cur.sql[-1], "ROLLBACK")


class TestFrame(unittest.TestCase):
    def test_help_and_selftest_subprocess(self):
        env = dict(os.environ, NOVA_TEST_QUIET="1")
        for flag in ("--help", "--selftest"):
            r = subprocess.run([sys.executable, str(SCRIPT), flag], capture_output=True, text=True, timeout=30,
                               env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_bad_days(self):
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            bf.main(["--days", "0"])

    @unittest.skipUnless(HAVE_GIT, "git not installed")
    def test_dry_run_writes_nothing_and_notifies_nothing(self):
        notify = mock.Mock()
        with _Repo(), mock.patch.object(bf, "connect") as connect, \
                mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=notify)}):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(bf.main(["--dry-run"]), 0)
        connect.assert_not_called()
        notify.assert_not_called()
        self.assertIn("nothing written", out.getvalue())
        self.assertIn("remember", out.getvalue())

    def test_house_conventions(self):
        self.assertIn("Little Mister, 2026-10-15", SRC)
        self.assertIn("Written by Jordan Koch (via Claude).", SRC)
        for t in ("black_flag_groups", "black_flag_snapshots", "black_flag_divergences", "black_flag_runs"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {t}", SRC)
        for flag in ("--scheduled", "--dry-run", "--report", "--selftest", "--days"):
            self.assertIn(flag, SRC)
        self.assertNotIn("__main__", SRC.split("def main")[0])


if __name__ == "__main__":
    unittest.main()
