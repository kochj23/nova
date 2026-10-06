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
nj._TASK_TIMEOUT_CACHE[:] = [None]   # never read the real scheduler yaml: no task budget in tests
nj._LONGFORM_OVERRIDE_CACHE[:] = [[]]  # never read the real service_config overrides in tests

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
    def test_article_length_table_lookup_per_profile(self):
        G, N = nj.EXPAND_GROUNDED, nj.EXPAND_NEVER
        expect = {"ops-security": (300, 700, N), "emergency-breaking": (300, 700, N), "dream": (400, 900, N),
                  "unclaimed-digest": (600, 1200, N), "digest": (600, 1200, N), "copenhagen": (600, 1200, N),
                  "weekly-summary": (700, 1400, N), "opinion": (1000, 1800, G), "fishbowl-daily": (1000, 1800, G),
                  "local-airwaves": (1200, 2000, G), "tech-today": (1200, 2000, G), "synthesis": (1500, 2500, G),
                  "essay": (2500, 4000, G), "research": (2500, 5000, G),
                  # 2026-10-06 follow-up: monthly pieces >= 3000 + the formerly unmapped live generators
                  "meta": (3000, 6000, G), "monthly-wrap": (3000, 6000, G), "autobiography": (1500, 3000, G),
                  "ledger": (1200, 2500, G), "repo-scout": (800, 1600, N), "iot-scout": (800, 1600, N)}
        for prof, row in expect.items():
            self.assertEqual(nj.article_length(prof), row, prof)
        self.assertGreaterEqual(nj.article_length("meta")[0], 3000)               # "a lot happens in a month"
        for prof in (None, "", "after-dark", "pilot", "art", "meta-analysis"):
            self.assertIsNone(nj.article_length(prof))
        self.assertEqual(nj.RETIRED_PROFILES, {"after-dark", "pilot", "art"})
        self.assertFalse(nj.RETIRED_PROFILES & set(nj.ARTICLE_LENGTH))
        self.assertEqual(nj.LONGFORM_OVERRIDES, [])                               # Jordan names the stories
        for prof, (lo, hi, pol) in nj.ARTICLE_LENGTH.items():
            self.assertLess(lo, hi, prof); self.assertIn(pol, (G, N), prof)
        for p in ("essay", "opinion", "tech-today", "research", "synthesis", "digest", "dream"):
            self.assertIn(p, nj.PROFILES)                                        # keys are real profile names

    def test_sources_from_memories(self):
        self.assertEqual(nj.sources_from_memories([], "t"), "")
        self.assertEqual(nj.sources_from_memories([{"text": "  "}, "junk"], "t"), "")
        out = nj.sources_from_memories([{"text": "fact one", "source": "wiki"},
                                        {"text": "x" * 5000, "source": "web", "metadata": {"url": "https://e.x/a"}}], "Topic")
        self.assertTrue(out.startswith("TOPIC: Topic")); self.assertIn("[1] (wiki) fact one", out)
        self.assertIn("(web, https://e.x/a)", out); self.assertLess(len(out), 1700)

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

    # ── per-article-type length policy + grounded expansion (2026-10-06) ──────────
    def _pub(self, title, body, profile, sources=None, section="essays", replies=None):
        """publish_hugo with call_openrouter mocked; returns (published_text, mock)."""
        with patch.dict(sys.modules, _lazy_stubs()), \
                patch.object(nj, "call_openrouter", side_effect=list(replies or [])) as co, _quiet():
            self.assertTrue(nj.publish_hugo(title, body, section, [], "d", sources=sources, profile=profile))
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        return next((nj.HUGO_ROOT / f"content/{section}").glob(f"*{slug}.md")).read_text(), co

    GROUNDED = ("The scheduler restarted on Tuesday at the memory host, as the source log says. " * 130).strip()  # ~1820w
    SOURCES = "[1] (ops_log) SRC-FACT-42: the scheduler restarted on Tuesday at the memory host."

    def test_grounded_prompt_carries_sources_and_no_invention_rule_and_uses_sonnet(self):
        text, co = self._pub("Essay On Grounded Clocks", BODY, "essay", self.SOURCES,
                             replies=["I can see your draft.\n\n---\n\n" + self.GROUNDED, '{"unsupported": []}'])
        (sys_p, user_p), kw = co.call_args_list[0]
        self.assertIn("SRC-FACT-42", user_p); self.assertIn(BODY, user_p)
        self.assertIn("Expand ONLY using facts present in the DRAFT or the SOURCES", sys_p)
        self.assertIn("STOP where the sources run out", sys_p)
        self.assertIn("2500-4000", sys_p)                                         # the essay row, not 5000
        self.assertEqual((kw["model"], kw["timeout"]), (nj.EXPAND_MODEL, nj.EXPAND_TIMEOUT_S))
        self.assertIn("sonnet", nj.EXPAND_MODEL)
        chk_user = co.call_args_list[1][0][1]
        self.assertIn("SRC-FACT-42", chk_user); self.assertIn("EXPANDED:", chk_user)
        # grounded + meaningfully longer, still under the 2500 min -> ACCEPTED
        self.assertIn("as the source log says", text); self.assertNotIn("I can see your draft", text)

    def test_unsupported_claims_reject_expansion_and_publish_original(self):
        verdict = json.dumps({"unsupported": [{"claim": "Dr. Ada Moss said it was sabotage", "type": "quote"}]})
        text, co = self._pub("Essay With Invented Quote", BODY, "essay", self.SOURCES,
                             replies=[self.GROUNDED, verdict])
        self.assertEqual(co.call_count, 2)
        self.assertNotIn("as the source log says", text); self.assertIn(BODY[:60], text)
        self.assertIn("Dr. Ada Moss", nj.LOG_FILE.read_text())                    # rejected claim is logged

    def test_invented_numbers_are_stripped_then_both_checks_rerun(self):
        # strip-not-scrap: the added sentence carrying the invented numbers is cut, the
        # claim check runs on the full expansion, then BOTH checks re-run on the stripped text
        padded = self.GROUNDED + " It took 29 minutes and 0.08% of the budget."
        text, co = self._pub("Essay With Invented Numbers", BODY, "essay", self.SOURCES,
                             replies=[padded, '{"unsupported": []}', '{"unsupported": []}'])
        self.assertEqual(co.call_count, 3)
        self.assertNotIn("29 minutes", text); self.assertIn("as the source log says", text)
        self.assertIn("EXPANDED:", co.call_args_list[2][0][1])
        self.assertNotIn("29 minutes", co.call_args_list[2][0][1])               # recheck saw the stripped text
        log = nj.LOG_FILE.read_text()
        self.assertIn("number check flagged (not in draft/sources): ['0.08%', '29']", log)
        self.assertIn("strip: removed 'It took 29 minutes", log)
        self.assertIn("strip-and-recheck passed", log)
        self.assertEqual(nj.new_numbers("draft 2014", "src 6.5 and 42", "2014, 6.5, 42 and 1. but 29 and 0.08%"),
                         ["0.08%", "29"])

    # ── strip-not-scrap (2026-10-06) ──
    ADJ = ("amber brisk calm dusty eager faint gentle hollow idle jagged keen lucid mellow nimble "
           "opaque placid quiet rusty sleepy tidy umber vivid weary young zesty bold crisp dim even frank").split()
    ADDED = [f"The {a} scheduler restarted on Tuesday at the memory host, as the source log says." for a in ADJ]
    INVENTED = "Mayor Ada Moss blamed the outage on sabotage."

    def _expansion(self, extra=()):
        paras = [BODY] + [" ".join(self.ADDED[i:i + 5]) for i in range(0, len(self.ADDED), 5)]
        return "\n\n".join(paras + list(extra))

    @staticmethod
    def _flag(sentence, claim=None, typ="event"):
        return json.dumps({"unsupported": [{"claim": claim or sentence[:30], "sentence": sentence, "type": typ}]})

    def test_checker_prompt_asks_for_the_verbatim_sentence(self):
        self.assertIn('"sentence"', nj._CHECK_SYS); self.assertIn("VERBATIM", nj._CHECK_SYS)

    def test_flagged_added_sentence_stripped_and_recheck_passes_publishes_expansion(self):
        exp = self._expansion([f"## Aftermath\n\n{self.INVENTED}", "## Close\n\nAnd so it went."])
        text, co = self._pub("Essay Strip One Invented", BODY, "essay", self.SOURCES,
                             replies=[exp, self._flag(self.INVENTED), '{"unsupported": []}'])
        self.assertEqual(co.call_count, 3)
        self.assertEqual(co.call_args_list[2].kwargs["model"], nj.CHECK_MODEL)
        self.assertNotIn("Ada Moss", text)
        self.assertNotIn("## Aftermath", text)                                   # newly orphaned heading tidied
        self.assertIn("## Close", text); self.assertIn("The amber scheduler", text); self.assertIn(BODY[:60], text)
        log = nj.LOG_FILE.read_text()
        self.assertIn("stripped 1/", log); self.assertIn("strip-and-recheck passed", log)

    def test_flag_in_a_draft_sentence_is_never_removed_and_original_publishes(self):
        draft_sentence = BODY.split(". ")[0] + "."
        text, co = self._pub("Essay Flag Lands In Draft", BODY, "essay", self.SOURCES,
                             replies=[self._expansion(), self._flag(draft_sentence)])
        self.assertEqual(co.call_count, 2)                                         # no recheck: nothing strippable
        self.assertNotIn("The amber scheduler", text); self.assertIn(BODY[:60], text)
        self.assertIn("draft sentences are never removed", nj.LOG_FILE.read_text())
        # the helper itself refuses (draft text untouched)
        st = nj.strip_flagged(BODY, self._expansion(), [], [{"claim": "x", "sentence": draft_sentence, "type": "event"}])
        self.assertFalse(st["ok"]); self.assertEqual(st["removed"], [])

    def test_flag_in_an_edited_draft_sentence_reverts_it_to_the_draft_wording(self):
        draft = "The backup job finished at dawn and nobody had to touch it at all.\n\nThat was the whole week."
        edited = "The backup job finished at dawn after 418 retries and nobody had to touch it at all."
        exp = (edited + "\n\nThat was the whole week.\n\n"
               + "\n\n".join(" ".join(self.ADDED[i:i + 5]) for i in range(0, 30, 5)))
        st = nj.strip_flagged(draft, exp, ["418"], [])
        self.assertTrue(st["ok"], st["why"]); self.assertEqual(st["removed"], [])
        self.assertEqual(st["reverted"], [(edited, "The backup job finished at dawn and nobody had to touch it at all.")])
        self.assertTrue(st["text"].startswith("The backup job finished at dawn and nobody had to touch it at all.\n\n"))
        self.assertNotIn("418", st["text"]); self.assertIn("The amber scheduler", st["text"])

    def test_paraphrased_flag_is_located_fuzzily_else_fails_closed(self):
        exp = self._expansion(["Nine days later, I reached for the Newsom shield-bill metaphor again, unprompted."])
        # the checker paraphrased instead of quoting verbatim (seen live 2026-10-06)
        st = nj.strip_flagged(BODY, exp, [], [{"claim": "the Newsom 'shield bill' metaphor was reused nine days later",
                                               "type": "number"}])
        self.assertTrue(st["ok"], st["why"]); self.assertEqual(len(st["removed"]), 1)
        self.assertIn("shield-bill", st["removed"][0])
        st = nj.strip_flagged(BODY, exp, [], [{"claim": "x", "type": "event",
                                               "sentence": "Nine days on, I reached for that Newsom shield-bill metaphor again."}])
        self.assertTrue(st["ok"], st["why"]); self.assertIn("shield-bill", st["removed"][0])
        # an ambiguous paraphrase (ties across sentences) is not guessed -> fail closed
        st = nj.strip_flagged(BODY, exp, [], [{"claim": "memory host scheduler restart on Tuesday per source log",
                                               "type": "event"}])            # 30 sentences tie at 75%
        self.assertFalse(st["ok"]); self.assertIn("could not locate", st["why"])

    def test_unlocatable_flag_fails_closed(self):
        text, co = self._pub("Essay Flag Not Found", BODY, "essay", self.SOURCES,
                             replies=[self._expansion(), self._flag("Something the text never says at all.")])
        self.assertEqual(co.call_count, 2); self.assertNotIn("The amber scheduler", text)
        self.assertIn("could not locate", nj.LOG_FILE.read_text())

    def test_recheck_still_flags_publishes_original(self):
        exp = self._expansion([self.INVENTED])
        text, co = self._pub("Essay Recheck Still Flags", BODY, "essay", self.SOURCES,
                             replies=[exp, self._flag(self.INVENTED), self._flag(self.ADDED[3], "amber")])
        self.assertEqual(co.call_count, 3)
        self.assertNotIn("The amber scheduler", text); self.assertIn(BODY[:60], text)
        self.assertIn("recheck REJECTED", nj.LOG_FILE.read_text())

    def test_checker_error_on_recheck_publishes_original(self):
        for i, bad in enumerate([None, "garbage verdict", RuntimeError("cli died")]):
            with self.subTest(bad=bad):
                text, co = self._pub(f"Essay Recheck Broke Case {'abc'[i]}", BODY, "essay", self.SOURCES,
                                     replies=[self._expansion([self.INVENTED]), self._flag(self.INVENTED), bad])
                self.assertEqual(co.call_count, 3)
                self.assertNotIn("The amber scheduler", text); self.assertIn(BODY[:60], text)

    def test_more_than_15_percent_cut_publishes_original(self):
        flags = json.dumps({"unsupported": [{"claim": a, "sentence": s, "type": "event"}
                                            for a, s in zip(self.ADJ[:5], self.ADDED[:5])]})   # 5/30 = 17%
        text, co = self._pub("Essay Too Much Invented", BODY, "essay", self.SOURCES,
                             replies=[self._expansion(), flags])
        self.assertEqual(co.call_count, 2)
        self.assertNotIn("The amber scheduler", text); self.assertIn(BODY[:60], text)
        self.assertIn("expansion unreliable", nj.LOG_FILE.read_text())
        # 4/30 = 13% is within the limit
        flags4 = json.dumps({"unsupported": [{"claim": a, "sentence": s, "type": "event"}
                                             for a, s in zip(self.ADJ[:4], self.ADDED[:4])]})
        text, co = self._pub("Essay Some Invented", BODY, "essay", self.SOURCES,
                             replies=[self._expansion(), flags4, '{"unsupported": []}'])
        self.assertNotIn("The amber scheduler", text); self.assertIn("The eager scheduler", text)

    def test_stripped_text_must_still_be_meaningfully_longer(self):
        long_inv = "Mayor Ada Moss blamed the outage on sabotage by a rival crew from across the valley."
        exp = BODY + "\n\n" + " ".join(self.ADDED[:2]) + " " + long_inv     # 202w >= 1.25x; 186w after strip
        with patch.object(nj, "STRIP_MAX_FRACTION", 0.5):
            text, co = self._pub("Essay Strip Leaves Too Little", BODY, "essay", self.SOURCES,
                                 replies=[exp, self._flag(long_inv)])
        self.assertEqual(co.call_count, 2)
        self.assertIn("stripped 186w is not meaningfully longer than the 156w draft", nj.LOG_FILE.read_text())
        self.assertNotIn("The amber scheduler", text)

    def test_time_guard_counts_the_strip_recheck(self):
        # enough for expand + check + publish under the old guard, not with the strip round added
        with patch.object(nj, "_task_time_left",
                          return_value=float(nj.CHECK_TIMEOUT_S + nj.LONGFORM_RESERVE_S + 200)):
            text, co = self._pub("Essay No Time For Strip", BODY, "essay", self.SOURCES)
        co.assert_not_called(); self.assertIn(BODY[:60], text)
        # time for the expansion and first check, none left by the recheck -> original
        with patch.object(nj, "_task_time_left", side_effect=[3000.0, 100.0]):
            text, co = self._pub("Essay Recheck Out Of Time", BODY, "essay", self.SOURCES,
                                 replies=[self._expansion([self.INVENTED]), self._flag(self.INVENTED)])
        self.assertEqual(co.call_count, 2); self.assertNotIn("The amber scheduler", text)
        self.assertIn("no task time left for the recheck", nj.LOG_FILE.read_text())

    def test_strip_cleanup_is_minimal_and_deterministic(self):
        draft = "## Intro\n\nThe cat sat on the mat. Mr. Smith agreed."
        exp = ("## Intro\n\nThe cat sat on the mat. Mr. Smith agreed.\n\nWinds hit the house hard that day. "
               "But the cat did not care at all.\n\n## Storms\n\nThe governor declared an emergency.\n\n---\n\n"
               "## Weather\n\nIt rained a lot, honestly. The sky was gray and wet. Puddles formed everywhere. "
               "Umbrellas came out. Nobody was surprised. Everyone stayed in. Dinner was soup.")
        with patch.object(nj, "STRIP_MAX_FRACTION", 0.5):
            st = nj.strip_flagged(draft, exp, [], [
                {"claim": "winds", "sentence": "Winds hit the house hard that day.", "type": "event"},
                {"claim": "governor", "sentence": "The “governor” declared ... emergency", "type": "event"}])
        self.assertTrue(st["ok"], st["why"])
        self.assertEqual(st["text"], "## Intro\n\nThe cat sat on the mat. Mr. Smith agreed.\n\n"
                         "The cat did not care at all.\n\n---\n\n## Weather\n\nIt rained a lot, honestly. "
                         "The sky was gray and wet. Puddles formed everywhere. Umbrellas came out. "
                         "Nobody was surprised. Everyone stayed in. Dinner was soup.")
        self.assertEqual(len(st["removed"]), 2)

    def test_checker_error_or_garbage_fails_closed(self):
        for i, bad in enumerate([None, "looks fine to me!", RuntimeError("cli died")]):
            with self.subTest(bad=bad):
                text, co = self._pub(f"Essay Checker Broke Case {'abc'[i]}", BODY, "essay", self.SOURCES,
                                     replies=[self.GROUNDED, bad])
                self.assertEqual(co.call_count, 2)
                self.assertNotIn("as the source log says", text); self.assertIn(BODY[:60], text)

    def test_no_sources_means_no_expansion_call(self):
        text, co = self._pub("Essay Without Any Sources", BODY, "essay", None)
        co.assert_not_called()
        self.assertIn(BODY[:60], text)
        self.assertIn("no sources, not expanding", nj.LOG_FILE.read_text())

    def test_never_rows_and_unmapped_profiles_make_no_model_call(self):
        for prof, sec in (("dream", "dreams"), ("weekly-summary", "essays"), ("ops-security", "operations"),
                          ("unclaimed-digest", "operations"), ("repo-scout", "operations"),
                          ("iot-scout", "operations"), (None, "essays")):
            with self.subTest(profile=prof):
                text, co = self._pub(f"Short Piece For {prof or 'nobody'}", BODY, prof, self.SOURCES, section=sec)
                co.assert_not_called()
                self.assertIn(BODY[:60], text)
        self.assertIn("unmapped profile None", nj.LOG_FILE.read_text())

    def test_retired_profiles_log_a_warning_not_unmapped(self):
        for prof, sec in (("after-dark", "after-dark"), ("pilot", "pilot"), ("art", "art")):
            with self.subTest(profile=prof):
                text, co = self._pub(f"Retired Piece For {prof}", BODY, prof, self.SOURCES, section=sec)
                co.assert_not_called(); self.assertIn(BODY[:60], text)
                log = nj.LOG_FILE.read_text()
                self.assertIn(f"WARNING 'Retired Piece For {prof}'", log)
                self.assertIn(f"RETIRED profile {prof!r}", log)
                self.assertNotIn(f"unmapped profile {prof!r}", log)

    def test_new_rows_drive_policy(self):
        # autobiography is grounded now: a short draft with sources gets the 1500-3000 expansion
        text, co = self._pub("Autobiography Chapter Grounded", BODY, "autobiography", self.SOURCES,
                             section="operations", replies=[self.GROUNDED, '{"unsupported": []}'])
        self.assertIn("1500-3000", co.call_args_list[0][0][0]); self.assertIn("as the source log says", text)
        # monthly-wrap / meta ask for 3000-6000
        text, co = self._pub("Monthly Wrap Grounded Test", BODY, "meta", self.SOURCES, section="meta",
                             replies=[self.GROUNDED, '{"unsupported": []}'])
        self.assertIn("3000-6000", co.call_args_list[0][0][0])

    def test_per_story_override_raises_min_but_still_needs_sources_and_grounding(self):
        overrides = [{"profile": "copenhagen", "min_words": 3000},
                     {"pattern": "(?i)two.months.of", "min_words": 3500},
                     {"min_words": 9000}]                                          # no selector -> ignored
        old = list(nj._LONGFORM_OVERRIDE_CACHE)
        try:
            nj._LONGFORM_OVERRIDE_CACHE[:] = [overrides]
            self.assertEqual(nj.override_min_words("copenhagen", "Morning Review"), 3000)
            self.assertEqual(nj.override_min_words("digest", "Two Months Of DNS"), 3500)
            self.assertEqual(nj.override_min_words("digest", "x", "2026-10-06-two-months-of-dns"), 3500)
            self.assertIsNone(nj.override_min_words("digest", "Something Else"))
            # copenhagen's row is 'never' — the override turns on GROUNDED expansion toward 3000-6000
            text, co = self._pub("Copenhagen Override Grounded", BODY, "copenhagen", self.SOURCES,
                                 section="operations", replies=[self.GROUNDED, '{"unsupported": []}'])
            self.assertIn("3000-6000", co.call_args_list[0][0][0])
            self.assertEqual(co.call_args_list[1].kwargs["model"], nj.CHECK_MODEL)  # grounding check ran
            self.assertIn("as the source log says", text)
            self.assertIn("per-story override: min 3000", nj.LOG_FILE.read_text())
            # ...but never without sources
            text, co = self._pub("Copenhagen Override No Sources", BODY, "copenhagen", None, section="operations")
            co.assert_not_called(); self.assertIn(BODY[:60], text)
            # ...and an unsupported claim still rejects the expansion
            bad = json.dumps({"unsupported": [{"claim": "Mayor Lee resigned", "type": "event"}]})
            text, co = self._pub("Copenhagen Override Bad Claim", BODY, "copenhagen", self.SOURCES,
                                 section="operations", replies=[self.GROUNDED, bad])
            self.assertNotIn("as the source log says", text); self.assertIn(BODY[:60], text)
        finally:
            nj._LONGFORM_OVERRIDE_CACHE[:] = old
        # publish_hugo(min_words=) works on an unmapped profile too, still grounded-only
        with patch.dict(sys.modules, _lazy_stubs()), \
                patch.object(nj, "call_openrouter", side_effect=[self.GROUNDED, '{"unsupported": []}']) as co, _quiet():
            self.assertTrue(nj.publish_hugo("One Off Ops Story Long", BODY, "operations", [], "d",
                                            sources=self.SOURCES, profile=None, min_words=3000))
        self.assertIn("3000-6000", co.call_args_list[0][0][0]); self.assertEqual(co.call_count, 2)

    def test_override_lookup_reads_service_config_then_falls_back(self):
        old = list(nj._LONGFORM_OVERRIDE_CACHE)
        try:
            cur = MagicMock(); cur.fetchone.return_value = ([{"profile": "essay", "min_words": 3200}],)
            conn = MagicMock(); conn.cursor.return_value.__enter__.return_value = cur
            nj._LONGFORM_OVERRIDE_CACHE[:] = []
            with patch("psycopg2.connect", return_value=conn), _quiet():
                self.assertEqual(nj.longform_overrides(), [{"profile": "essay", "min_words": 3200}])
            sql, params = cur.execute.call_args[0]
            self.assertIn("FROM service_config", sql); self.assertEqual(params, ("nova_journal", "longform_overrides"))
            nj._LONGFORM_OVERRIDE_CACHE[:] = []
            with patch("psycopg2.connect", side_effect=RuntimeError("pg down")), \
                    patch.object(nj, "LONGFORM_OVERRIDES", [{"profile": "x", "min_words": 3000}]), _quiet():
                self.assertEqual(nj.longform_overrides(), [{"profile": "x", "min_words": 3000}])
        finally:
            nj._LONGFORM_OVERRIDE_CACHE[:] = old

    def test_above_max_gets_one_tighten_pass_else_draft(self):
        long_body = ("Port scan from the usual suspect, blocked at the edge, nothing new to report. " * 70).strip()  # ~1050w > 700
        tight = ("Port scan blocked at the edge; nothing new. " * 60).strip()                                       # ~420w
        text, co = self._pub("Ops Security Too Long Today", long_body, "ops-security", section="operations",
                             replies=[tight])
        self.assertEqual(co.call_count, 1)                                         # tighten only, no check
        sys_p, user_p = co.call_args[0]
        self.assertIn("add NOTHING", sys_p); self.assertIn("at most 700", sys_p); self.assertEqual(user_p, long_body)
        self.assertIn("Port scan blocked at the edge; nothing new.", text)
        self.assertNotIn("from the usual suspect", text)
        # tighten failure (sonnet AND haiku error) -> draft as written
        text, co = self._pub("Ops Security Tighten Failed", long_body, "ops-security", section="operations",
                             replies=[None, None])
        self.assertEqual([c.kwargs["model"] for c in co.call_args_list], [nj.EXPAND_MODEL, nj.EXPAND_FALLBACK_MODEL])
        self.assertIn("from the usual suspect", text)

    def test_haiku_only_as_fallback_and_task_budget_respected(self):
        text, co = self._pub("Essay Sonnet Errored Once", BODY, "essay", self.SOURCES,
                             replies=[None, self.GROUNDED, '{"unsupported": []}'])
        self.assertEqual([c.kwargs["model"] for c in co.call_args_list],
                         [nj.EXPAND_MODEL, nj.EXPAND_FALLBACK_MODEL, nj.CHECK_MODEL])
        self.assertIn("as the source log says", text)
        with patch.object(nj, "_task_time_left", return_value=300.0):            # not enough for expand+check
            text, co = self._pub("Essay Out Of Task Time", BODY, "essay", self.SOURCES)
        co.assert_not_called(); self.assertIn(BODY[:60], text)

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
        self.assertEqual(kw["profile"], "zz"); self.assertIn("m1", kw["sources"])   # grounds expansion
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
