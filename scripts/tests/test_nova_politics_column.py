#!/usr/bin/env python3
"""7-category tests for nova_politics_column (Little Mister's set): Security, Performance,
Retry, Unit, Integration, Functional, Frame. DB, LLM, image, and git are all mocked —
the focus is the SAFETY GUARDS: never fabricate on a thin week, never publish an
ungrounded (hallucinated) draft."""
import importlib.util, os, sys, time
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, ".."))
spec = importlib.util.spec_from_file_location("nova_politics_column", os.path.join(SCRIPTS, "nova_politics_column.py"))
pc = importlib.util.module_from_spec(spec); spec.loader.exec_module(pc)

ITEMS = [{"outlet": "Techdirt", "headline": "DOJ blocked charges against an ICE officer"},
         {"outlet": "New Voice of Ukraine", "headline": "Ukrainian drones reach 3,000 km inside Russia"},
         {"outlet": "EFF", "headline": "Drone-as-first-responder programs expanding"}]


# ── Unit ──────────────────────────────────────────────────────────────────────
def test_unit_build_source_block():
    b = pc.build_source_block(ITEMS)
    assert "- [Techdirt] DOJ blocked" in b and b.count("\n") == 2

def test_unit_verify_grounded_true_with_two_outlets():
    art = "As Techdirt reported... and the EFF warned..."
    assert pc.verify_grounded(art, ITEMS) is True

def test_unit_verify_grounded_false_with_one_outlet():
    art = "Techdirt reported a thing, and I have opinions about it."
    assert pc.verify_grounded(art, ITEMS) is False


# ── Security (the load-bearing guarantees) ──────────────────────────────────────
def test_security_thin_week_never_fabricates(monkeypatch):
    monkeypatch.setattr(pc, "gather_brief", lambda *a, **k: ITEMS[:2])  # below MIN_BRIEF_ITEMS
    published = {"n": 0}
    monkeypatch.setattr(pc, "publish", lambda *a, **k: published.__setitem__("n", 1))
    monkeypatch.setattr(pc, "generate_article", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not generate")))
    monkeypatch.setattr(pc, "_log_action", lambda *a, **k: None)
    assert pc.main([]) == 0
    assert published["n"] == 0  # nothing published, nothing generated

def test_security_ungrounded_draft_never_published(monkeypatch):
    monkeypatch.setattr(pc, "gather_brief", lambda *a, **k: ITEMS + ITEMS + ITEMS)  # >= MIN
    monkeypatch.setattr(pc, "generate_article", lambda *a, **k: "A"*2000 + " I made all this up with no sources.")
    published = {"n": 0}
    monkeypatch.setattr(pc, "publish", lambda *a, **k: published.__setitem__("n", 1))
    monkeypatch.setattr(pc, "_log_action", lambda *a, **k: None)
    rc = pc.main([])
    assert rc == 1 and published["n"] == 0  # grounding gate blocked it


# ── Functional ────────────────────────────────────────────────────────────────
def test_functional_happy_path_publishes(monkeypatch):
    monkeypatch.setattr(pc, "gather_brief", lambda *a, **k: ITEMS*4)  # >= MIN
    monkeypatch.setattr(pc, "generate_article", lambda *a, **k:
                        "Per Techdirt, the DOJ blocked charges. The EFF warned about drones. " + "x"*1000)
    monkeypatch.setattr(pc, "generate_title", lambda *a, **k: "A Test Column")
    monkeypatch.setattr(pc, "make_cover", lambda *a, **k: None)
    calls = {}
    monkeypatch.setattr(pc, "publish", lambda t, b, c: calls.setdefault("t", t) or "http://x")
    monkeypatch.setattr(pc, "_log_action", lambda *a, **k: None)
    assert pc.main([]) == 0 and calls["t"] == "A Test Column"

def test_functional_short_article_aborts(monkeypatch):
    monkeypatch.setattr(pc, "gather_brief", lambda *a, **k: ITEMS*4)
    monkeypatch.setattr(pc, "generate_article", lambda *a, **k: "too short")
    published = {"n": 0}
    monkeypatch.setattr(pc, "publish", lambda *a, **k: published.__setitem__("n", 1))
    monkeypatch.setattr(pc, "_log_action", lambda *a, **k: None)
    assert pc.main([]) == 1 and published["n"] == 0


# ── Retry / resilience ────────────────────────────────────────────────────────
def test_retry_log_action_failure_is_swallowed(monkeypatch):
    # _log_action swallows DB errors internally; calling with a broken DSN must not raise
    monkeypatch.setenv("NOVA_OPS_DSN", "host=256.256.256.256 dbname=x user=y")
    pc._log_action("test", "test")  # no exception


# ── Performance ──────────────────────────────────────────────────────────────
def test_performance_verify_grounded_fast():
    art = "Techdirt and the EFF and New Voice of Ukraine " * 200
    start = time.perf_counter()
    for _ in range(2000):
        pc.verify_grounded(art, ITEMS)
    assert time.perf_counter() - start < 2.0


# ── Integration ──────────────────────────────────────────────────────────────
def test_integration_thresholds_and_sources_sane():
    assert pc.MIN_BRIEF_ITEMS >= 5 and pc.MIN_ARTICLE_CHARS >= 500
    # private/personal shelves must never be in the political source set
    for banned in ("imessage", "email_archive", "personal", "scanner"):
        assert banned not in pc.POLITICAL_SOURCES

def test_integration_dry_run_flag_recognized(monkeypatch):
    monkeypatch.setattr(pc, "gather_brief", lambda *a, **k: ITEMS*4)
    monkeypatch.setattr(pc, "generate_article", lambda *a, **k:
                        "Per Techdirt and the EFF, things happened. " + "x"*1000)
    monkeypatch.setattr(pc, "generate_title", lambda *a, **k: "Dry Title")
    published = {"n": 0}
    monkeypatch.setattr(pc, "publish", lambda *a, **k: published.__setitem__("n", 1))
    monkeypatch.setattr(pc, "_log_action", lambda *a, **k: None)
    assert pc.main(["--dry-run"]) == 0 and published["n"] == 0  # dry-run never publishes


# ── Frame (boundary) ──────────────────────────────────────────────────────────
def test_frame_empty_brief_aborts(monkeypatch):
    monkeypatch.setattr(pc, "gather_brief", lambda *a, **k: [])
    monkeypatch.setattr(pc, "_log_action", lambda *a, **k: None)
    assert pc.main([]) == 0  # graceful skip, no crash

def test_frame_verify_grounded_empty_items():
    assert pc.verify_grounded("some article text", []) is False


# ── house categories as unittest classes (added 2026-10-05) ─────────────────────
import io as _io
import json as _json
import re as _re
import subprocess as _sp
import tempfile as _tf
import types as _types
import unittest
from contextlib import redirect_stdout as _redir
from pathlib import Path as _P
from unittest.mock import MagicMock, patch

PC_SRC = _P(SCRIPTS, "nova_politics_column.py").read_text()


class _Resp:
    def __init__(self, obj):
        self.obj = obj

    def read(self):
        return _json.dumps(self.obj).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_mem(rows_by_source):
    """psycopg2-style connection whose cursor answers per-source rows; records (sql, params)."""
    conn = MagicMock()
    seen = []

    def cursor():
        cur = MagicMock()
        state = {}

        def execute(sql, params):
            seen.append((sql, params)); state["src"] = params[0]
        cur.execute.side_effect = execute
        cur.fetchall.side_effect = lambda: [(r,) for r in rows_by_source.get(state["src"], [])]
        ctx = MagicMock()
        ctx.__enter__.return_value = cur
        return ctx
    conn.cursor.side_effect = cursor
    return conn, seen


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = _re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", _re.I)
        self.assertIsNone(pat.search(PC_SRC))
        self.assertNotIn("password", pc.MEM_DSN + pc.OPS_DSN)

    def test_brief_query_parameterized_only_int_interpolated(self):
        conn, seen = _fake_mem({})
        with patch.object(pc, "_mem_conn", return_value=conn):
            pc.gather_brief(days="7")
        sql, params = seen[0]
        self.assertIn("interval '7 days'", sql)               # int(days) is the only f-string value
        self.assertEqual(params[0], "news")
        self.assertIn("%s", sql)
        with patch.object(pc, "_mem_conn", return_value=_fake_mem({})[0]), self.assertRaises(ValueError):
            pc.gather_brief(days="7; select 1")

    def test_private_shelves_never_queried(self):
        conn, seen = _fake_mem({})
        with patch.object(pc, "_mem_conn", return_value=conn):
            pc.gather_brief()
        self.assertEqual([p[0] for _, p in seen], list(pc.POLITICAL_SOURCES))


class TestPerformance(unittest.TestCase):
    def test_political_excerpt_10k(self):
        snip = "mattress sale this weekend only " * 5 + "the senate passed the bill " + "weather " * 10
        t0 = time.perf_counter()
        for _ in range(10_000):
            pc._political_excerpt(snip)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_llm_failure_is_one_shot_and_never_publishes(self):
        # RETRY GAP: generate_article/urlopen — one attempt; the exception stops main() before publish
        with patch.object(pc, "gather_brief", return_value=ITEMS * 4), patch.object(pc, "_log_action"), \
                patch("urllib.request.urlopen", side_effect=OSError("ollama down")) as uo, \
                patch.object(pc, "publish") as pub, _redir(_io.StringIO()):
            with self.assertRaises(OSError):
                pc.main([])
        self.assertEqual(uo.call_count, 1)
        pub.assert_not_called()

    def test_cover_failure_fails_open(self):
        bad = _types.SimpleNamespace(generate_image=MagicMock(side_effect=RuntimeError("no gpu")))
        with patch.dict(sys.modules, {"nova_image_utils": bad}), _redir(_io.StringIO()):
            self.assertIsNone(pc.make_cover("slug"))


class TestUnit(unittest.TestCase):
    def test_political_excerpt_centers_and_falls_back(self):
        ex = pc._political_excerpt("x" * 100 + " the president signed it " + "y" * 300)
        self.assertTrue(ex.startswith("…"))
        self.assertIn("president", ex)
        self.assertEqual(pc._political_excerpt("plain weather report"), "plain weather report")

    def test_generate_title_cleans_and_falls_back(self):
        with patch("urllib.request.urlopen", return_value=_Resp({"response": "<think>hm</think>\n\"Wry Title\""})):
            self.assertEqual(pc.generate_title("body"), "Wry Title")
        with patch("urllib.request.urlopen", return_value=_Resp({"response": "ok"})):
            self.assertEqual(pc.generate_title("intro\n## The Court Folds\nmore"), "Field Notes: The Court Folds")

    def test_generate_article_strips_think(self):
        with patch("urllib.request.urlopen", return_value=_Resp({"response": "<think>plan</think> Column body"})):
            self.assertEqual(pc.generate_article("- [X] y", 1), "Column body")


class TestIntegration(unittest.TestCase):
    def test_gather_brief_round_robins_and_gates_tv(self):
        rows = {"news": ["[PBS NewsHour] The senate voted on the budget bill today in a close count",
                         "[PBS NewsHour] The senate voted on the budget bill today in a close count"],
                "local_news": ["[KTLA] The governor signed the housing legislation this morning downtown"],
                "television": ["[Rick Steves] Rome has a lot of history and the president of the club said hi",
                               "[Pod Save America] Congress cannot agree on the shutdown deadline again"]}
        conn, _ = _fake_mem(rows)
        with patch.object(pc, "_mem_conn", return_value=conn):
            items = pc.gather_brief()
        self.assertEqual([i["outlet"] for i in items], ["PBS NewsHour", "KTLA", "Pod Save America"])  # dedup + TV gate
        conn.close.assert_called_once()

    def test_brief_block_feeds_grounding_gate(self):
        block = pc.build_source_block(ITEMS)
        self.assertTrue(pc.verify_grounded(block, ITEMS))


class TestFunctional(unittest.TestCase):
    def _publish(self, run):
        tmp = _P(_tf.mkdtemp())
        with patch.object(pc, "HUGO_ROOT", tmp), patch.object(pc, "CONTENT_DIR", tmp / "c"), \
                patch.object(pc, "IMAGES_DIR", tmp / "i"), patch.object(pc.subprocess, "run", run), \
                _redir(_io.StringIO()) as out:
            url = pc.publish('A "Quoted" Column', "body text", None)
        return tmp, url, out.getvalue()

    def test_publish_writes_post_and_pushes(self):
        run = MagicMock(return_value=_types.SimpleNamespace(returncode=0, stderr=""))
        tmp, url, out = self._publish(run)
        post = next((tmp / "c").glob("*.md")).read_text()
        self.assertIn('title: "A Quoted Column"', post)
        self.assertEqual([c[0][0][:2] for c in run.call_args_list],
                         [["git", "add"], ["git", "commit"], ["git", "pull"], ["git", "push"]])
        self.assertTrue(url.endswith("-a-quoted-column/"))

    def test_rebase_failure_aborts_without_push(self):
        def run(cmd, **k):
            return _types.SimpleNamespace(returncode=1 if cmd[1] == "pull" else 0, stderr="conflict")
        m = MagicMock(side_effect=run)
        _, _, out = self._publish(m)
        cmds = [c[0][0][1] for c in m.call_args_list]
        self.assertIn("rebase", cmds)
        self.assertNotIn("push", cmds)
        self.assertIn("push ABORTED", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', PC_SRC)
        r = _sp.run([sys.executable, "-c", "import nova_politics_column"], cwd=str(SCRIPTS),
                    capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
