#!/usr/bin/env python3
"""Tests for nova_journal.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.error    # noqa: F401  (stdlib the script patches through — imported BEFORE the
import urllib.parse    # noqa: F401   sys.modules-scoped load so the restore cannot drop them and split
import urllib.request  # noqa: F401   the test's patch target from the object the module holds)
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2          # noqa: F401  (real module locked in before any stubbing)
import psycopg2.extras   # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_journal.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_journal_test_"))


def _stub_modules():
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    iu = types.ModuleType("nova_image_utils"); iu.generate_image = MagicMock(return_value=None)
    oc = types.ModuleType("nova_ops_context")
    oc.get_full_context = lambda hours=24: {}; oc.format_security_brief = lambda c: ""; oc.format_infra_brief = lambda c: ""
    rs = types.ModuleType("nova_resolve"); rs.resolve_url = lambda svc, path="": f"http://127.0.0.1:0{path}"
    return {"nova_notify": nn, "nova_image_utils": iu, "nova_ops_context": oc, "nova_resolve": rs}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()), patch("psycopg2.connect", side_effect=RuntimeError("offline")), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")), \
         patch("subprocess.run", side_effect=RuntimeError("offline")), patch("subprocess.Popen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


nj = _load("nj", SCRIPT)
nj.LOG_FILE = TMP / "nova_journal.log"
nj.STATE_FILE = TMP / "journal_state.json"
nj.HUGO_ROOT = TMP / "nova-journal"

BODY = ("The scheduler vanished on Tuesday and nobody noticed until the dashboards went quiet. " * 12).strip()
REFUSAL = "I need to stop you right there. This assignment doesn't work: you handed me a grab-bag of excerpts. " * 8


def _lazy_stubs(weather="*Weather: 72F, clear*\n\n"):
    """Modules publish_hugo / git_push import lazily; stubbed so no PG, weather API or memory server is touched."""
    wb = types.ModuleType("nova_weather_blurb"); wb.weather_dateline_line = MagicMock(return_value=weather)
    am = types.ModuleType("nova_articles_to_memory"); am.remember_article = MagicMock(return_value=True)
    cc = types.ModuleType("nova_claude_code"); cc.claude_env = MagicMock(return_value={"HOME": "/tmp"})
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    return {"nova_weather_blurb": wb, "nova_articles_to_memory": am, "nova_claude_code": cc, "nova_notify": nn,
            "nova_config": nj.nova_config}      # the lazy `import nova_config` must resolve to the object the module holds


def _urlopen(payload):
    resp = MagicMock(); resp.read.return_value = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    resp.__enter__ = lambda s: s; resp.__exit__ = lambda s, *a: False
    return MagicMock(return_value=resp)


# Probe strings are assembled at runtime so personal addresses and the home path never appear
# as literals in source (the pre-push scanner rejects them, and that rule is correct).
_PERSONAL = "kochj23" + "@" + "gmail.com"
_WORK = "kochj" + "@" + "digitalnoise.net"
_HOME = str(Path.home())

class _Proc:
    """subprocess.Popen stand-in for `claude -p`."""
    def __init__(self, rc=0, out="generated text", err="", timeout=False):
        self.returncode = rc; self._out = out; self._err = err; self._timeout = timeout; self.pid = 4242; self.inputs = []

    def communicate(self, input=None, timeout=None):
        self.inputs.append(input)
        if self._timeout and input is not None:
            raise subprocess.TimeoutExpired("claude", timeout)
        return self._out, self._err

    def kill(self):
        pass


def _git_runner(staged="", push_rc=(0,), pull_rc=0):
    """A subprocess.run fake for git_push: records argv, answers diff/commit/push/pull."""
    calls = []; pushes = list(push_rc)

    def run(argv, **kw):
        calls.append(list(argv))
        rc, out = 0, ""
        if argv[:2] == ["git", "diff"]:
            out = staged
        elif argv[:2] == ["git", "push"]:
            rc = pushes.pop(0) if pushes else 0
        elif argv[:2] == ["git", "pull"]:
            rc = pull_rc
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="rejected" if rc else "")
    return run, calls


def _quiet():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", nj._PUSH_DSN)

    def test_pg_sql_is_parameterized_and_no_shell(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertNotIn("shell=True", SRC)
        # the one SQL string that is interpolated (fetch_memories_by_source) goes to psql as a single argv
        # element, never through a shell; its `source` values come from the memory server's own stats.
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="")) as r, _quiet():
            nj.fetch_memories_by_source("wikipedia")
        self.assertIsInstance(r.call_args[0][0], list)
        self.assertNotIn("shell", r.call_args[1])

    def test_llm_user_prompt_travels_on_stdin_never_argv(self):
        proc = _Proc(out="ok")
        secret_user = "USERPROMPT-" + "x" * 50
        with patch.dict(sys.modules, _lazy_stubs()), patch("subprocess.Popen", return_value=proc) as p, _quiet():
            self.assertEqual(nj.call_openrouter("sys", secret_user), "ok")
        self.assertNotIn(secret_user, " ".join(p.call_args[0][0]))
        self.assertEqual(proc.inputs, [secret_user])
        self.assertTrue(p.call_args[1]["start_new_session"])

    def test_scrubbers_remove_pii_macs_and_home_paths(self):
        s = nj.scrub_pii("mail " + _PERSONAL + " or nova@digitalnoise.net; dev a1:b2:c3:d4:e5:f6 at " + str(Path.home()) + "/x")
        self.assertNotIn(_PERSONAL, s)
        self.assertIn("nova@digitalnoise.net", s)
        self.assertIn("[redacted-mac]", s)
        self.assertNotIn(str(Path.home()) + "/", s)
        self.assertEqual(nj.scrub_home_paths("see " + _HOME + "/nova-journal/x.md and /images/a.webp"), "see ~/… and /images/a.webp")

    def test_published_body_is_scrubbed(self):
        body = BODY + "\n\nbox 00:11:22:33:44:55 wrote to " + _HOME + "/secret/notes.txt for bob@example.com"
        with patch.dict(sys.modules, _lazy_stubs()), _quiet():
            self.assertTrue(nj.publish_hugo("Scrub Check Title", body, "dreams", ["t"], "d"))
        out = (nj.HUGO_ROOT / "content/dreams").glob("*scrub-check-title.md")
        text = next(out).read_text()
        for leak in ("00:11:22:33:44:55", "/Users/kochj", "bob@example.com"):
            self.assertNotIn(leak, text)


class TestPerformance(unittest.TestCase):
    def test_scrub_and_refusal_scan_fast_on_10k_items(self):
        line = "On 2026-10-05 " + _WORK + " saw aa:bb:cc:dd:ee:ff in " + _HOME + "/logs — fine otherwise."
        t0 = time.perf_counter()
        for i in range(10_000):
            nj.scrub_home_paths(nj.scrub_pii(line)); nj._looks_like_refusal("A Title", BODY if i % 2 else REFUSAL)
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_claude_call_retries_transient_failures_with_backoff(self):
        procs = [_Proc(rc=1, err="ENOENT: Bun could not find a file"), _Proc(rc=0, out=""), _Proc(rc=0, out="third time lucky")]
        with patch.dict(sys.modules, _lazy_stubs()), patch("subprocess.Popen", side_effect=procs) as p, \
             patch("time.sleep") as sl, _quiet():
            self.assertEqual(nj.call_openrouter("sys", "user"), "third time lucky")
        self.assertEqual(p.call_count, 3)
        self.assertEqual([c[0][0] for c in sl.call_args_list], [2, 4])

    def test_claude_call_gives_up_after_three_and_never_retries_a_timeout(self):
        with patch.dict(sys.modules, _lazy_stubs()), patch("subprocess.Popen", side_effect=[_Proc(rc=1)] * 3) as p, \
             patch("time.sleep"), _quiet():
            self.assertIsNone(nj.call_openrouter("sys", "user"))
        self.assertEqual(p.call_count, 3)
        with patch.dict(sys.modules, _lazy_stubs()), patch("subprocess.Popen", return_value=_Proc(timeout=True)) as p, \
             patch("os.getpgid", return_value=4242), patch("os.killpg") as kp, _quiet():
            self.assertIsNone(nj.call_openrouter("sys", "user"))
        self.assertEqual(p.call_count, 1)
        kp.assert_called_once_with(4242, __import__("signal").SIGKILL)
        with patch("subprocess.Popen") as p, _quiet():
            self.assertIsNone(nj.call_openrouter("s" * 130_000, "user"))     # size refusal: no process at all
        p.assert_not_called()

    def test_memory_server_reads_fail_open(self):
        # RETRY GAP: recall_memories()/random_memories()/get_available_sources() — one urlopen each, [] on failure
        with patch("urllib.request.urlopen", side_effect=OSError("down")) as u, _quiet():
            self.assertEqual(nj.recall_memories("q"), [])
            self.assertEqual(nj.random_memories(3), [])
            self.assertEqual(nj.get_available_sources(), [])
        self.assertEqual(u.call_count, 3)
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="x")), _quiet():
            self.assertEqual(nj.fetch_memories_by_source("wikipedia"), [])

    def test_search_fails_open_but_alerts(self):
        # RETRY GAP: _searxng_search() — one attempt; [] on failure, and the dead-search alert fires (deduped in PG)
        with patch("urllib.request.urlopen", side_effect=OSError("down")), patch.object(nj, "_search_dead_alert") as da, _quiet():
            self.assertEqual(nj._searxng_search("ai news"), [])
        da.assert_called_once_with("ai news", [["searxng", "down"]])

    def test_push_lock_degrades_to_unlocked(self):
        # RETRY GAP: _acquire_push_lock() — PG unreachable -> None (best-effort, unlocked push), no exception
        with patch("psycopg2.connect", side_effect=RuntimeError("pg down")), _quiet():
            self.assertIsNone(nj._acquire_push_lock())
        nj._release_push_lock(None)
        cur = MagicMock(); cur.fetchone.return_value = (True,)
        conn = MagicMock(); conn.cursor.return_value.__enter__.return_value = cur
        with patch("psycopg2.connect", return_value=conn):
            self.assertIs(nj._acquire_push_lock(wait_s=5), conn)
        cur.execute.assert_called_once_with("SELECT pg_try_advisory_lock(%s)", (nj._PUSH_LOCK_KEY,))

    def test_self_inventory_builds_from_whatever_answers(self):
        nj._SELF_INVENTORY_CACHE.clear()
        with patch("psycopg2.connect", side_effect=RuntimeError("pg down")), \
             patch("urllib.request.urlopen", side_effect=OSError("down")), _quiet():
            text = nj.self_inventory_block()
        self.assertTrue(text.startswith("SELF-INVENTORY"))
        self.assertIn("RULES:", text)
        self.assertNotIn("LIVE RIGHT NOW", text)
        self.assertIs(nj.self_inventory_block(), nj._SELF_INVENTORY_CACHE["text"])     # cached
        nj._SELF_INVENTORY_CACHE.clear()


class TestUnit(unittest.TestCase):
    def test_state_round_trip_and_recent_pruning(self):
        if nj.STATE_FILE.exists():
            nj.STATE_FILE.unlink()
        self.assertEqual(nj.load_state(), {})
        nj.STATE_FILE.write_text("{not json")
        self.assertEqual(nj.load_state(), {})
        old = (date.today() - timedelta(days=9)).isoformat(); new = (date.today() - timedelta(days=2)).isoformat()
        state = {"essay": {"recent_topics": [{"item": "old", "date": old}, {"item": "new", "date": new}]}}
        self.assertEqual([r["item"] for r in nj.get_recent(state, "essay")], ["new"])
        self.assertEqual(nj.get_recent({}, "essay"), [])
        for i in range(40):
            nj.add_recent(state, "dream", f"t{i}")
        self.assertEqual(len(state["dream"]["recent_topics"]), 30)
        self.assertEqual(state["dream"]["recent_topics"][-1]["item"], "t39")
        nj.save_state(state)
        self.assertEqual(nj.load_state(), state)

    def test_extractors_and_tags(self):
        self.assertEqual(nj._extract_title("**Why Clocks Lie**\n\nbody"), "Why Clocks Lie")
        self.assertEqual(nj._extract_title("FADE IN:\nINT. KITCHEN - NIGHT\nThe Last Pilot\n"), "The Last Pilot")
        self.assertEqual(nj._extract_title(""), "Untitled")
        self.assertEqual(nj._extract_title("ab\n" + "x" * 200), "Untitled")
        self.assertEqual(nj._extract_field("TITLE: Hello\nLOGLINE: a thing\nGENRE: Drama", "LOGLINE"), "a thing")
        self.assertEqual(nj._extract_field("nothing", "TITLE"), "")
        self.assertEqual(nj._topic_to_tags("2024 Quantum Networking Basics | extra"), ["quantum", "networking"])
        self.assertEqual(nj._topic_to_tags("a to be"), [])
        self.assertEqual(nj._canon_section("rando"), "operations")
        self.assertEqual(nj._canon_section("essays"), "essays")

    def test_refusal_and_ai_topic_detectors(self):
        self.assertIsNone(nj._looks_like_refusal("A Real Title", BODY))
        self.assertIn("colon-intro title", nj._looks_like_refusal("What I can do:", BODY))
        self.assertEqual(nj._looks_like_refusal("T", REFUSAL).lower(), "i need to stop you")
        self.assertIsNone(nj._looks_like_refusal("T", BODY * 3 + REFUSAL))             # only the opening is scanned
        self.assertTrue(nj._is_ai_topic("new LLM benchmarks"))
        self.assertFalse(nj._is_ai_topic("gardening in October"))
        self.assertEqual(nj._with_self_inventory("SYS", "gardening"), "SYS")

    def test_conflict_resolution_predicates(self):
        self.assertTrue(nj._auto_resolvable("content/fishbowl/the-fishbowl.md"))
        self.assertTrue(nj._auto_resolvable("static/images/essays/2026-10-05-x.WEBP"))
        self.assertFalse(nj._auto_resolvable("content/essays/2026-10-05-x.md"))
        self.assertFalse(nj._auto_resolvable("static/images/notes.md"))

    def test_parsers_for_news_search_and_psql_records(self):
        xml = b"<rss><title>Google News</title><item><title><![CDATA[Mars Rover Finds Ice]]></title></item><item><title><![CDATA[Rates Hold]]></title></item></rss>"
        with patch("urllib.request.urlopen", _urlopen(xml)):
            self.assertEqual(nj._fetch_google_news(), ["Mars Rover Finds Ice", "Rates Hold"])
        with patch("urllib.request.urlopen", _urlopen({"results": [{"title": f"r{i}"} for i in range(20)]})), \
             patch.object(nj, "_search_dead_alert") as da:
            self.assertEqual(len(nj._searxng_search("q", n=4)), 4)
        da.assert_not_called()
        out = "first memory\nwith newline\x1fwikipedia\x1f{\"show\": \"Cosmos\"}\x1esecond\x1fwikipedia\x1f\x1e"
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=out)):
            mems = nj.fetch_memories_by_source("wikipedia", n=2)
        self.assertEqual([m["text"] for m in mems], ["first memory\nwith newline", "second"])
        self.assertEqual(mems[0]["metadata"], {"show": "Cosmos"})
        with patch("urllib.request.urlopen", _urlopen({"results": [{"text": "m", "source": "wikipedia"}]})):
            self.assertEqual(nj.recall_memories("q")[0]["text"], "m")

    def test_attribution_groups_memories_and_web_sources(self):
        mems = [{"text": "a", "source": "wikipedia", "metadata": {"show": "Cosmos", "title": "Ep 1"}},
                {"text": "b", "source": "wikipedia", "metadata": {"show": "Cosmos"}},
                {"text": "[Web] Headline: some content", "source": "web", "metadata": {"url": "http://x", "title": "Headline"}},
                {"text": "[Web] Bare: body text", "source": "web", "metadata": {}}]
        out = nj._append_attribution("BODY", mems, "topic", "essay")
        self.assertTrue(out.startswith("BODY\n---"))
        self.assertIn("**Cosmos** (2 memories)", out)
        self.assertIn("- *Ep 1*: \"a...\"", out)
        self.assertIn("- [Headline](http://x)", out)
        self.assertIn("- **Bare**: body text", out)
        self.assertIn("drew from **4** memories", out)

    def test_notify_slack_truncates_preview(self):
        nj.notify = MagicMock()
        nj.notify_slack("rando", "T", "word " * 100)
        kw = nj.notify.call_args[1]
        self.assertEqual(nj.notify.call_args[0][0], "Nova Journal — operations: T")
        self.assertTrue(kw["body"].endswith("...") and len(kw["body"]) <= 254)
        self.assertEqual((kw["level"], kw["category"], kw["meta"]), ("info", "journal", {"section": "operations"}))


class TestIntegration(unittest.TestCase):
    def test_publish_hugo_writes_front_matter_cover_and_dateline(self):
        img = TMP / "cover.webp"; img.write_bytes(b"RIFFxxxxWEBP")
        stubs = _lazy_stubs()
        with patch.dict(sys.modules, stubs), patch("subprocess.run") as r, _quiet():
            ok = nj.publish_hugo("Night Shift Report", BODY, "dreams", ["dream", "x"], 'desc "q"', image_path=str(img), emoji="🌙")
        self.assertTrue(ok)
        r.assert_not_called()                                                     # webp source: copied, no cwebp
        md = nj.HUGO_ROOT / f"content/dreams/{nj.today_str()}-night-shift-report.md"
        text = md.read_text()
        self.assertTrue(text.startswith('---\ntitle: "🌙 Night Shift Report"\n'))
        self.assertIn('categories: ["dreams"]\ntags: ["dream", "x"]\ndescription: "desc \'q\'"\n', text)
        self.assertIn(f'cover:\n  image: "/images/dreams/{nj.today_str()}-night-shift-report.webp"', text)
        self.assertIn("*Published ", text)
        self.assertIn("*Weather: 72F, clear*", text)
        self.assertTrue((nj.HUGO_ROOT / f"static/images/dreams/{nj.today_str()}-night-shift-report.webp").exists())
        stubs["nova_articles_to_memory"].remember_article.assert_called_once_with(str(md))
        stubs["nova_weather_blurb"].weather_dateline_line.assert_called_once()

    def test_publish_hugo_stable_slug_degenerate_title_and_guard_block(self):
        stubs = _lazy_stubs()
        with patch.dict(sys.modules, stubs), _quiet():
            self.assertTrue(nj.publish_hugo("Let me", BODY, "dreams", [], "d", stable_slug="evergreen"))
        md = nj.HUGO_ROOT / "content/dreams/evergreen.md"
        self.assertIn(f'title: "Dreams Dispatch — {nj.today_str()}"', md.read_text())
        stubs["nova_articles_to_memory"].remember_article.assert_called_once_with(str(md))
        with patch.dict(sys.modules, stubs), patch.object(nj.nova_config, "post_both") as pb, _quiet():
            self.assertFalse(nj.publish_hugo("Blocked Piece Title", "too short", "dreams", [], "d"))
        self.assertIn("Suppressed a non-publishable *dreams* article", pb.call_args[0][0])

    def test_longform_expansion_only_for_longform_sections(self):
        long = ("Deep elaboration of the same point, carried further with care. " * 600).strip()
        with patch.dict(sys.modules, _lazy_stubs()), patch.object(nj, "call_openrouter", return_value="I can see your draft.\n\n---\n\n" + long) as co, _quiet():
            nj.publish_hugo("Essay On Clocks Lying", BODY, "essays", [], "d")
            nj.publish_hugo("Dream On Clocks Lying", BODY, "dreams", [], "d")
        self.assertEqual(co.call_count, 1)
        self.assertEqual(co.call_args[0][1], BODY)
        text = next((nj.HUGO_ROOT / "content/essays").glob("*essay-on-clocks-lying.md")).read_text()
        self.assertNotIn("I can see your draft", text)
        self.assertIn("Deep elaboration", text)
        self.assertGreater(len(text.split()), 5000)
        with patch.dict(sys.modules, _lazy_stubs()), patch.object(nj, "call_openrouter", return_value="short " * 100), _quiet():
            nj.publish_hugo("Essay Kept Short Here", BODY, "essays", [], "d")
        text = next((nj.HUGO_ROOT / "content/essays").glob("*essay-kept-short-here.md")).read_text()
        self.assertIn(BODY[:60], text)                                             # under-floor expansion is discarded

    def test_longform_short_first_pass_gets_one_more_pass(self):
        mid = ("Elaborated once, still short of the floor here today. " * 300).strip()    # 2700w
        full = ("Elaborated twice, now comfortably past the floor here. " * 700).strip()  # 5600w
        with patch.dict(sys.modules, _lazy_stubs()), \
                patch.object(nj, "call_openrouter", side_effect=[mid, full]) as co, _quiet():
            nj.publish_hugo("Essay On Two Passes", BODY, "essays", [], "d")
        self.assertEqual(co.call_count, 2)
        self.assertEqual(co.call_args_list[1][0][1], mid)                          # 2nd pass expands the 1st
        text = next((nj.HUGO_ROOT / "content/essays").glob("*essay-on-two-passes.md")).read_text()
        self.assertIn("Elaborated twice", text)

    def test_citations_are_recorded_parameterized(self):
        cur = MagicMock(); conn = MagicMock(); conn.cursor.return_value.__enter__.return_value = cur
        with patch.dict(sys.modules, _lazy_stubs()), patch("psycopg2.connect", return_value=conn), _quiet():
            nj.publish_hugo("Cited Article Title", BODY, "dreams", [], "d", cited_memory_ids=[11, 22])
        self.assertEqual(cur.execute.call_count, 2)
        sql, params = cur.execute.call_args_list[0][0]
        self.assertIn("INSERT INTO article_citations", sql)
        self.assertEqual(params, ("cited-article-title", "11"))              # bare slug, no date prefix

    def test_git_push_scrubs_staged_macs_commits_and_pushes(self):
        (nj.HUGO_ROOT / ".git").mkdir(parents=True, exist_ok=True)
        art = nj.HUGO_ROOT / "content/essays/leak.md"; art.parent.mkdir(parents=True, exist_ok=True)
        art.write_text("router de:ad:be:ef:00:01 rebooted")
        run, calls = _git_runner(staged="content/essays/leak.md\0static/images/x.webp\0")
        with patch.dict(sys.modules, _lazy_stubs()), patch.object(nj, "_acquire_push_lock", return_value=None), \
             patch("subprocess.run", side_effect=run), _quiet() as out:
            nj.git_push("rando", "A title that is definitely longer than fifty characters total")
        self.assertEqual(art.read_text(), "router [redacted-mac] rebooted")
        self.assertIn(["git", "add", "content/essays/leak.md"], calls)
        commit = next(c for c in calls if c[:2] == ["git", "commit"])
        self.assertEqual(commit[3], f"operations: {nj.today_str()} — A title that is definitely longer than fifty chara")
        self.assertEqual(calls[-1], ["git", "push"])
        self.assertIn("Pushed to GitHub — deploy triggered", out.getvalue())

    def test_git_push_rebases_once_on_rejection(self):
        (nj.HUGO_ROOT / ".git").mkdir(parents=True, exist_ok=True)
        run, calls = _git_runner(push_rc=(1, 0))
        with patch.dict(sys.modules, _lazy_stubs()), patch.object(nj, "_acquire_push_lock", return_value=None), \
             patch("subprocess.run", side_effect=run), _quiet() as out:
            nj.git_push("essays", "t")
        self.assertEqual([c[1] for c in calls if c[1] in ("push", "pull")], ["push", "pull", "push"])
        self.assertIn("Pushed to GitHub after rebase", out.getvalue())

    def test_profiles_registry_is_complete_and_shared_helpers_are_used(self):
        for name, p in nj.PROFILES.items():
            self.assertEqual(set(p), {"section", "emoji", "topic_fn", "generate_fn", "tags_base", "image_section"}, name)
            self.assertTrue(callable(p["topic_fn"]) and callable(p["generate_fn"]))
        self.assertIn("from nova_notify import notify", SRC)
        self.assertIn("from nova_image_utils import generate_image", SRC)
        self.assertIn("nova_config.filter_private_memories(", SRC)
        self.assertIn("from nova_journal_guard import is_publishable", SRC)
        self.assertNotIn("def is_publishable", SRC)


class TestFunctional(unittest.TestCase):
    def _run(self, topic_fn, generate_fn, name="zz", publish_ok=True):
        prof = {"section": "dreams", "emoji": "🌙", "topic_fn": topic_fn, "generate_fn": generate_fn,
                "tags_base": [name], "image_section": "dreams"}
        if nj.STATE_FILE.exists():
            nj.STATE_FILE.unlink()
        with patch.dict(nj.PROFILES, {name: prof}), patch.object(nj, "get_image_prompt", return_value="prompt"), \
             patch.object(nj, "generate_image", return_value="/tmp/cover.png") as gi, patch.object(nj, "publish_hugo", return_value=publish_ok) as ph, \
             patch.object(nj, "git_push") as gp, patch.object(nj, "notify_slack") as ns, _quiet() as out:
            rc = nj.run_profile(name)
        return rc, ph, gp, ns, gi, out.getvalue()

    def test_golden_path_publishes_pushes_notifies_and_saves_state(self):
        mems = [{"text": "m1", "source": "wikipedia", "metadata": {}}]
        rc, ph, gp, ns, gi, out = self._run(lambda state: ("Ocean Tides Explained", mems),
                                           lambda t, m: ("A Fine Title", BODY + "\n<!--IMGPROMPT: a wave -->\nmore"))
        self.assertEqual(rc, 0)
        kw = ph.call_args[1]
        self.assertEqual((kw["title"], kw["section"], kw["tags"], kw["emoji"], kw["image_path"]),
                         ("A Fine Title", "dreams", ["zz", "ocean", "tides"], "🌙", "/tmp/cover.png"))
        self.assertEqual(kw["description"], "Nova's zz on Ocean Tides Explained")
        self.assertNotIn("IMGPROMPT", kw["body"]); self.assertIn("## Sources & Attribution", kw["body"])
        gi.assert_called_once_with("prompt", section="dreams")
        gp.assert_called_once_with("dreams", "A Fine Title")
        ns.assert_called_once(); self.assertEqual(ns.call_args[0][:2], ("dreams", "A Fine Title"))
        state = json.loads(nj.STATE_FILE.read_text())
        self.assertEqual((state["zz"]["last_title"], state["zz_count"], state["zz"]["recent_topics"][0]["item"]),
                         ("A Fine Title", 1, "Ocean Tides Explained"))
        self.assertIn('=== zz complete: "A Fine Title" ===', out)

    def test_error_paths(self):
        def boom(t, m):
            raise RuntimeError("llm down")
        rc, ph, *_ = self._run(lambda state: ("t", []), boom)
        self.assertEqual(rc, 1); ph.assert_not_called()

        def skip(state):
            raise nj.SkipArticle("no headlines")
        rc, ph, *_ = self._run(skip, lambda t, m: ("T", BODY))
        self.assertEqual(rc, 0); ph.assert_not_called()
        with _quiet() as out:
            self.assertEqual(nj.run_profile("nope"), 1)
        self.assertIn("Unknown profile 'nope'", out.getvalue())
        rc, ph, gp, *_ = self._run(lambda state: ("t", []), lambda t, m: ("Title Here", BODY), publish_ok=False)
        self.assertEqual(rc, 1); ph.assert_called_once(); gp.assert_not_called()

    def test_refusal_retries_same_topic_then_fresh_then_aborts(self):
        topic_fn = MagicMock(return_value=("Ocean Tides", []))
        rc, ph, *_ = self._run(topic_fn, lambda t, m: ("Here's what I can actually do:", REFUSAL))
        self.assertEqual(rc, 1); ph.assert_not_called()
        self.assertEqual(topic_fn.call_count, 3)
        self.assertEqual(topic_fn.call_args_list[1][1], {"retry_topic": "Ocean Tides"})
        calls = []

        def plain(state):                                   # no retry_topic support -> TypeError -> fresh topic
            calls.append(1); return ("t", [])
        rc, *_ = self._run(plain, lambda t, m: ("T", REFUSAL))
        self.assertEqual((rc, len(calls)), (1, 3))             # the TypeError'd call never reaches the body

    def test_main_cli(self):
        with patch.object(sys, "argv", ["nova_journal.py"]), _quiet() as out, self.assertRaises(SystemExit) as cm:
            nj.main()
        self.assertEqual(cm.exception.code, 1); self.assertIn("Usage:", out.getvalue())
        with patch.object(sys, "argv", ["nova_journal.py", "Tech_Today"]), patch.object(nj, "run_profile", return_value=0) as rp, \
             self.assertRaises(SystemExit) as cm:
            nj.main()
        self.assertEqual(cm.exception.code, 0); rp.assert_called_once_with("tech-today")


class TestFrame(unittest.TestCase):
    SHIM = ("import psycopg2, urllib.request, subprocess;"
            "psycopg2.connect=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('offline'));"
            "urllib.request.urlopen=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('offline'));"
            "subprocess.run=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('offline'));")

    def _run(self, code):
        return subprocess.run([sys.executable, "-c", self.SHIM + code], cwd=str(SCRIPTS), capture_output=True,
                              text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})

    def test_no_args_prints_usage_and_exits_1(self):
        r = self._run("import sys, runpy; sys.argv=['nova_journal.py']; runpy.run_path('nova_journal.py', run_name='__main__')")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("Usage:", r.stdout); self.assertIn("Profiles: after-dark, art", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = self._run("import nova_journal")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
