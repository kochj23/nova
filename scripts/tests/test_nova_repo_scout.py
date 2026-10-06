#!/usr/bin/env python3
"""Tests for nova_repo_scout.py — the 7 house categories (Security, Performance, Retry, Unit,
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
SCRIPT = SCRIPTS / "nova_repo_scout.py"
SRC = SCRIPT.read_text()


def _journal_stub():
    nj = types.ModuleType("nova_journal")
    nj.log = MagicMock()
    nj.call_openrouter = MagicMock(return_value="TITLE: Neat, Not Mine\nVERDICT: PASS\n\nbody text")
    nj.publish_hugo = MagicMock(return_value=True)
    nj.git_push = MagicMock()
    nj.notify_slack = MagicMock()
    return nj


def _voice_stub():
    v = types.ModuleType("nova_voice")
    v.system_prompt = MagicMock(return_value="SYSTEM")
    return v


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_journal": _journal_stub(), "nova_voice": _voice_stub()}):
        spec.loader.exec_module(mod)
    return mod


rs = _load("repo_scout_under_test", SCRIPT)
# Module-level stubs: no gh CLI, no GitHub, no PG.
rs.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=OSError("offline: gh stubbed")))
rs.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=OSError("offline: pg stubbed")))
rs.urllib = types.SimpleNamespace(request=types.SimpleNamespace(
    Request=rs.urllib.request.Request, urlopen=MagicMock(side_effect=OSError("offline: urlopen stubbed"))))


def _repo(full="acme/local-rag", stars=1200, lang="Python", desc="A local RAG engine for Ollama", topics=("rag", "llm"), **kw):
    r = {"full_name": full, "html_url": f"https://github.com/{full}", "stargazers_count": stars, "language": lang,
         "description": desc, "topics": list(topics), "pushed_at": "2026-10-01", "created_at": "2025-01-01", "open_issues_count": 3}
    r.update(kw)
    return r


def _gh(responses):
    """subprocess.run stub for the gh CLI: `responses` maps an argv substring to (returncode, stdout)."""
    def run(argv, **kw):
        joined = " ".join(argv)
        for key, (rc, out) in responses.items():
            if key in joined:
                return types.SimpleNamespace(returncode=rc, stdout=out, stderr="err")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="no stub")
    return MagicMock(side_effect=run)


class _Resp:
    def __init__(self, body): self._b = body.encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Cur:
    def __init__(self, seen=(), raise_on=()):
        self.seen, self.raise_on, self.sql, self.params = list(seen), tuple(raise_on), [], []

    def execute(self, sql, params=None):
        s = " ".join(sql.split()); self.sql.append(s); self.params.append(params)
        for frag in self.raise_on:
            if frag in s:
                raise RuntimeError(f"stub failure on {frag}")

    def fetchall(self): return [(s,) for s in self.seen]

    def ran(self, frag): return [(s, p) for s, p in zip(self.sql, self.params) if frag in s]


def _conn(cur):
    return types.SimpleNamespace(cursor=lambda: cur, close=MagicMock(), autocommit=False)


TRENDING_HTML = """
<article class="Box-row"><h2><a href="/acme/local-rag">acme / local-rag</a></h2><span>1,234 stars today</span></article>
<article class="Box-row"><h2><a href="/ollama/ollama">ollama</a></h2><span>2,000 stars today</span></article>
<article class="Box-row"><h2><a href="/someone/knitting-patterns">knit</a></h2><span>5,000 stars today</span></article>
<article class="Box-row"><h2><a href="/trending/python">nope</a></h2></article>
"""


def _run(argv_dry=True, cur=None, gh=None, urlopen=None, llm=None, force=None):
    cur = cur or _Cur()
    rs.nj.log = MagicMock(); rs.nj.publish_hugo = MagicMock(return_value=True)
    rs.nj.git_push = MagicMock(); rs.nj.notify_slack = MagicMock()
    rs.nj.call_openrouter = MagicMock(return_value=llm if llm is not None else "TITLE: Neat, Not Mine\nVERDICT: PASS\n\nbody text")
    buf = io.StringIO()
    with patch.object(rs.psycopg2, "connect", MagicMock(return_value=_conn(cur))) as pg, \
         patch.object(rs.subprocess, "run", gh or _gh({})), \
         patch.object(rs.urllib.request, "urlopen", urlopen or MagicMock(return_value=_Resp(TRENDING_HTML))), redirect_stdout(buf):
        rc = rs.run(dry_run=argv_dry, force_repo=force)
    return rc, cur, pg, buf.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", rs.DSN)
        self.assertIn("auth lives in the keyring", SRC)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        evil = "x'); DROP TABLE repo_scout_log; --"
        gh = _gh({"repos/" + evil: (0, json.dumps(_repo(full=evil))), "/readme": (0, "readme")})
        rc, cur, _, _ = _run(argv_dry=False, gh=gh, force=evil)
        self.assertEqual(rc, 0)
        ins = cur.ran("INSERT INTO repo_scout_log")[0]
        self.assertNotIn("DROP", ins[0]); self.assertEqual(ins[1][0], evil)
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"repo_scout_log", "telemetry.events"})

    def test_gh_invoked_as_argv_never_shell(self):
        gh = _gh({"search/repositories": (0, json.dumps({"items": []}))})
        with patch.object(rs.subprocess, "run", gh):
            rs.gh_search("llm; rm -rf /", "2026-09-01", "2022-01-01")
        argv, kw = gh.call_args[0][0], gh.call_args[1]
        self.assertIsInstance(argv, list); self.assertFalse(kw.get("shell", False))
        self.assertTrue(argv[6].startswith("q=topic:llm; rm -rf / stars:>600"))   # stays one argv token, data not code

    def test_desk_review_never_clones_or_runs_code(self):
        for banned in ("git clone", "pip install", "os.system", "shell=True"):
            self.assertNotIn(banned, SRC)


class TestPerformance(unittest.TestCase):
    def test_wheelhouse_and_adopted_filters_fast_on_10k_repos(self):
        repos = [_repo(full=f"org{i}/thing{i}", desc=("local llm agent" if i % 2 else "knitting"), topics=()) for i in range(10_000)]
        t0 = time.perf_counter()
        hits = sum(1 for r in repos if rs._in_wheelhouse(r) and not rs._is_adopted(r))
        self.assertLess(time.perf_counter() - t0, 1.5)
        self.assertEqual(hits, 5_000)

    def test_trending_parse_fast_on_10k_articles(self):
        html = "".join(f'<article><a href="/o{i}/r{i}">x</a> {i} stars today</article>' for i in range(10_000))
        with patch.object(rs.urllib.request, "urlopen", MagicMock(return_value=_Resp(html))):
            t0 = time.perf_counter()
            out = rs.fetch_trending()
            self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(out), 10_000)
        self.assertEqual(out[7], ("o7/r7", 7))


class TestRetry(unittest.TestCase):
    def test_gh_calls_fail_open(self):
        # RETRY GAP: gh_search / gh_repo / fetch_readme — one gh invocation each; failures return []/None/""
        boom = MagicMock(side_effect=subprocess.TimeoutExpired("gh", 30))
        with patch.object(rs.subprocess, "run", boom):
            self.assertEqual(rs.gh_search("llm", "a", "b"), [])
            self.assertIsNone(rs.gh_repo("a/b"))
            self.assertEqual(rs.fetch_readme("a/b"), "")
        self.assertEqual(boom.call_count, 3)
        with patch.object(rs.subprocess, "run", _gh({"gh": (1, "")})):
            self.assertEqual(rs.gh_search("llm", "a", "b"), [])
            self.assertIsNone(rs.gh_repo("a/b"))

    def test_trending_fetch_fails_open_to_search_fallback(self):
        # RETRY GAP: fetch_trending — one urlopen; failure returns [] and pick_repo falls back to gh search
        uo = MagicMock(side_effect=OSError("github down"))
        gh = _gh({"search/repositories": (0, json.dumps({"items": [_repo(full="fallback/agent-kit", stars=900)]}))})
        with patch.object(rs.urllib.request, "urlopen", uo), patch.object(rs.subprocess, "run", gh):
            repo = rs.pick_repo(_Cur())
        self.assertEqual(uo.call_count, 1)
        self.assertEqual(repo["full_name"], "fallback/agent-kit")

    def test_llm_failure_aborts_before_publish(self):
        # RETRY GAP: evaluate/call_openrouter — one call; empty answer returns 1 and nothing is published or logged
        gh = _gh({"repos/acme/local-rag": (0, json.dumps(_repo())), "/readme": (0, "r")})
        rc, cur, _, _ = _run(argv_dry=False, gh=gh, llm="", force="acme/local-rag")
        self.assertEqual(rc, 1)
        rs.nj.publish_hugo.assert_not_called(); rs.nj.git_push.assert_not_called()
        self.assertEqual(cur.ran("INSERT INTO repo_scout_log"), [])

    def test_telemetry_insert_failure_is_swallowed(self):
        # RETRY GAP: telemetry.events insert — best effort; the article still ships
        gh = _gh({"repos/acme/local-rag": (0, json.dumps(_repo())), "/readme": (0, "r")})
        rc, cur, _, _ = _run(argv_dry=False, cur=_Cur(raise_on=("telemetry.events",)), gh=gh, force="acme/local-rag")
        self.assertEqual(rc, 0)
        rs.nj.git_push.assert_called_once()
        self.assertTrue(any("telemetry skipped" in c[0][0] for c in rs.nj.log.call_args_list))


class TestUnit(unittest.TestCase):
    def test_in_wheelhouse(self):
        self.assertTrue(rs._in_wheelhouse(_repo()))
        self.assertTrue(rs._in_wheelhouse(_repo(desc=None, topics=("mcp",), full="x/y-mcp")))
        self.assertFalse(rs._in_wheelhouse(_repo(full="a/knit", desc="knitting patterns", topics=())))
        self.assertFalse(rs._in_wheelhouse({}))

    def test_is_adopted(self):
        self.assertTrue(rs._is_adopted(_repo(full="ollama/ollama")))
        self.assertTrue(rs._is_adopted(_repo(full="Open-WebUI/Open-WebUI")))
        self.assertTrue(rs._is_adopted(_repo(full="ggerganov/llama.cpp")))
        self.assertFalse(rs._is_adopted(_repo(full="acme/ollama-tools")))
        self.assertFalse(rs._is_adopted({}))

    def test_fetch_trending_parses_and_filters_paths(self):
        with patch.object(rs.urllib.request, "urlopen", MagicMock(return_value=_Resp(TRENDING_HTML))):
            out = rs.fetch_trending()
        self.assertEqual(out, [("acme/local-rag", 1234), ("ollama/ollama", 2000), ("someone/knitting-patterns", 5000)])
        with patch.object(rs.urllib.request, "urlopen", MagicMock(return_value=_Resp("<html>no articles</html>"))):
            self.assertEqual(rs.fetch_trending(), [])

    def test_evaluate_parses_title_verdict_body_and_defaults(self):
        rs.nj.call_openrouter = MagicMock(return_value='TITLE: "Quoted"\nVERDICT: steal\n\nline1\nline2')
        self.assertEqual(rs.evaluate(_repo(), "readme"), ("Quoted", "STEAL", "line1\nline2"))
        rs.nj.call_openrouter = MagicMock(return_value="VERDICT: MAYBE\nbody only")
        title, verdict, body = rs.evaluate(_repo(), "")
        self.assertEqual(title, "I Looked at local-rag So You Don't Have To")
        self.assertEqual(verdict, "WATCH")                       # unknown verdict coerced
        self.assertEqual(body, "body only")
        rs.nj.call_openrouter = MagicMock(return_value="no markers at all")
        self.assertEqual(rs.evaluate(_repo(), "")[1], "WATCH")
        rs.nj.call_openrouter = MagicMock(return_value=None)
        self.assertIsNone(rs.evaluate(_repo(), ""))

    def test_gh_search_builds_the_query(self):
        gh = _gh({"search/repositories": (0, json.dumps({"items": [{"full_name": "a/b"}]}))})
        with patch.object(rs.subprocess, "run", gh):
            self.assertEqual(rs.gh_search("mlx", "2026-09-14", "2022-10-05", n=5), [{"full_name": "a/b"}])
        argv = gh.call_args[0][0]
        self.assertIn("q=topic:mlx stars:>600 pushed:>2026-09-14 created:>2022-10-05", argv)
        self.assertIn("per_page=5", argv)

    def test_fetch_readme_truncates(self):
        with patch.object(rs.subprocess, "run", _gh({"/readme": (0, "x" * 10_000)})):
            self.assertEqual(len(rs.fetch_readme("a/b", limit=100)), 100)


class TestIntegration(unittest.TestCase):
    def test_journal_helpers_are_imported_not_reimplemented(self):
        self.assertIn("import nova_journal as nj", SRC)
        for fn in ("publish_hugo", "git_push", "notify_slack", "call_openrouter"):
            self.assertNotIn(f"def {fn}", SRC)
        self.assertIn("nj.publish_hugo(", SRC)

    def test_pick_repo_skips_seen_adopted_and_off_wheelhouse(self):
        gh = _gh({"repos/acme/local-rag": (0, json.dumps(_repo())),
                  "repos/ollama/ollama": (0, json.dumps(_repo(full="ollama/ollama"))),
                  "repos/someone/knitting-patterns": (0, json.dumps(_repo(full="someone/knitting-patterns", desc="yarn", topics=())))})
        rs.nj.log = MagicMock()
        with patch.object(rs.urllib.request, "urlopen", MagicMock(return_value=_Resp(TRENDING_HTML))), patch.object(rs.subprocess, "run", gh):
            repo = rs.pick_repo(_Cur())
            self.assertEqual(repo["full_name"], "acme/local-rag")
            self.assertEqual(repo["_stars_today"], 1234)
            self.assertTrue(any("already in Nova's stack" in c[0][0] for c in rs.nj.log.call_args_list))
            seen_cur = _Cur(seen=["acme/local-rag"])
            self.assertIsNone(rs.pick_repo(seen_cur))             # nothing left trending, search returns nothing
        self.assertEqual(seen_cur.sql[0], "SELECT full_name FROM repo_scout_log")

    def test_pick_by_search_takes_highest_stars_excluding_junk(self):
        items = [_repo(full="a/one", stars=700), _repo(full="a/two", stars=5000, archived=True),
                 _repo(full="a/three", stars=4000, fork=True), _repo(full="a/four", stars=3000), _repo(full="a/seen", stars=9000)]
        gh = _gh({"search/repositories": (0, json.dumps({"items": items}))})
        rs.nj.log = MagicMock()
        with patch.object(rs.subprocess, "run", gh):
            best = rs._pick_by_search({"a/seen"})
        self.assertEqual(best["full_name"], "a/four")
        self.assertEqual(gh.call_count, len(rs.THEMES))

    def test_evaluate_feeds_stack_and_readme_to_the_voice(self):
        rs.nj.call_openrouter = MagicMock(return_value="TITLE: t\nVERDICT: ADOPT\n\nb")
        rs.evaluate(_repo(), "README BODY")
        system, user = rs.nj.call_openrouter.call_args[0]
        self.assertEqual(system, "SYSTEM")
        self.assertTrue(user.startswith(rs.NOVA_STACK))
        self.assertIn("Repo: acme/local-rag", user); self.assertIn("README BODY", user)
        self.assertEqual(rs.nj.call_openrouter.call_args[1], {"max_tokens": 3000, "temperature": 0.75})


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_logs_pushes_and_notifies(self):
        gh = _gh({"repos/acme/local-rag": (0, json.dumps(_repo())), "/readme": (0, "readme")})
        rc, cur, pg, out = _run(argv_dry=False, gh=gh, force="acme/local-rag")
        self.assertEqual(rc, 0)
        pg.assert_called_once_with(rs.DSN)
        self.assertIn("CREATE TABLE IF NOT EXISTS repo_scout_log", cur.sql[0])
        title, body, section, tags, desc = rs.nj.publish_hugo.call_args[0]
        self.assertEqual((title, section), ("Neat, Not Mine", "operations"))
        self.assertTrue(body.startswith("body text\n\n---\n\n*Scouted repo: [acme/local-rag]"))
        self.assertIn("Verdict: PASS", body)
        self.assertEqual(tags, ["ai", "github", "repo-scout", "pass", "python"])
        self.assertEqual(rs.nj.publish_hugo.call_args[1], {"emoji": "🪦", "profile": "repo-scout"})
        self.assertEqual(cur.ran("INSERT INTO repo_scout_log")[0][1],
                         ("acme/local-rag", "https://github.com/acme/local-rag", 1200, "Python", "PASS", "Neat, Not Mine"))
        self.assertEqual(cur.ran("INSERT INTO telemetry.events")[0][1], ("Repo scout [PASS]: acme/local-rag", "Neat, Not Mine"))
        rs.nj.git_push.assert_called_once_with("operations", "Neat, Not Mine")
        rs.nj.notify_slack.assert_called_once_with("operations", "🪦 Neat, Not Mine [PASS]", "Scouted acme/local-rag (1200★) — PASS.")

    def test_dry_run_writes_hugo_but_never_logs_pushes_or_posts(self):
        gh = _gh({"repos/acme/local-rag": (0, json.dumps(_repo())), "/readme": (0, "readme")})
        rc, cur, pg, out = _run(argv_dry=True, gh=gh, force="acme/local-rag")
        self.assertEqual(rc, 0)
        rs.nj.publish_hugo.assert_called_once()
        rs.nj.git_push.assert_not_called(); rs.nj.notify_slack.assert_not_called()
        self.assertEqual(cur.ran("INSERT INTO"), [])
        self.assertIn("===== 🪦 Neat, Not Mine  [PASS] =====", out)

    def test_forced_repo_not_found_returns_1(self):
        rc, cur, pg, out = _run(argv_dry=False, gh=_gh({}), force="nope/missing")
        self.assertEqual(rc, 1)
        rs.nj.publish_hugo.assert_not_called()

    def test_nothing_to_review_returns_0_quietly(self):
        rc, cur, pg, out = _run(argv_dry=False, gh=_gh({}), urlopen=MagicMock(return_value=_Resp("<html></html>")))
        self.assertEqual(rc, 0)
        rs.nj.publish_hugo.assert_not_called()
        self.assertTrue(any("nothing to review today" in c[0][0] for c in rs.nj.log.call_args_list))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_the_scout(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertIn('sys.exit(run(dry_run="--dry-run" in sys.argv, force_repo=forced))', SRC)
        # nova_journal resolves service URLs at import, so the smoke run stubs it inside the throwaway process
        code = ("import sys, types\n"
                "for n in ('nova_journal', 'nova_voice'):\n"
                "    sys.modules[n] = types.ModuleType(n)\n"
                "import nova_repo_scout as m\n"
                "print('IMPORT-OK', len(m.THEMES))\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), f"IMPORT-OK {len(rs.THEMES)}")


if __name__ == "__main__":
    unittest.main()
