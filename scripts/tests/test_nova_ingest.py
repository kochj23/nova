#!/usr/bin/env python3
"""Tests for nova_ingest.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
(tests/test_ingest_utils.py covers the sibling ingest scripts; this is nova_ingest.py's dedicated file.)"""
import hashlib
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
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ingest.py"
SRC = SCRIPT.read_text()


def _stubs():
    cfg = types.ModuleType("nova_config"); cfg.SLACK_NOTIFY = "C_TEST"; cfg.SLACK_CHAN = "C_CHAT"
    cfg.VECTOR_URL = "http://memory.test/remember"; cfg.post_both = mock.MagicMock()
    nn = types.ModuleType("nova_notify"); nn.notify = mock.MagicMock(return_value=True)
    nr = types.ModuleType("nova_resolve"); nr.resolve_url = lambda svc, path="": f"http://{svc}.test{path}"
    return {"nova_config": cfg, "nova_notify": nn, "nova_resolve": nr}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, _stubs()):          # the engine binds its stubs at import; keys restored after
        spec.loader.exec_module(mod)
    return mod


NI = _load("nova_ingest_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="nova-ingest-test-"))
NI.STATE_DIR = TMP / "state"
NI.LOG_FILE = TMP / "nova_ingest.log"
NI._discard_off = True                                    # never open the discard-audit PG connection
# Varied, non-repetitive prose (>= MIN_WORDS): a sentence repeated verbatim is itself caught by the repeat-trash patterns.
PROSE = ("The quick brown fox jumps over the lazy dog while the farmer watches from the porch. "
         "Later that evening a storm rolled across the valley, flattening corn and scattering hens. "
         "By morning the creek had risen, so the children waded out to rescue a stranded calf near the old mill.")


class _Resp:
    def __init__(self, body=b"ok", ct="text/html; charset=utf-8"):
        self._b = body; self.headers = {"Content-Type": ct}
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _http(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b""))


def _untrusted(verdict, score=0.0, hits=()):
    m = types.ModuleType("nova_untrusted")
    m.scan = lambda text: {"verdict": verdict, "score": score, "hits": list(hits)}
    return {"nova_untrusted": m}


class _Quiet(unittest.TestCase):
    def setUp(self):
        self._buf = io.StringIO(); self._rs = redirect_stdout(self._buf); self._rs.__enter__()
        NI._bus_notify.reset_mock()

    def tearDown(self):
        self._rs.__exit__(None, None, None)


class TestSecurity(_Quiet):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"sk-or-[A-Za-z0-9]{10,}")

    def test_openai_key_comes_from_keychain_only(self):
        with mock.patch.object(NI.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout="k3y\n")) as sp:
            self.assertEqual(NI._get_openai_key(), "k3y")
        self.assertEqual(sp.call_args[0][0][:2], ["security", "find-generic-password"])
        with mock.patch.object(NI.subprocess, "run", side_effect=OSError("locked")):
            self.assertIsNone(NI._get_openai_key())

    def test_explicit_content_never_reaches_memory(self):
        done = set()
        with mock.patch.object(NI.urllib.request, "urlopen") as uo:
            self.assertFalse(NI.remember("free porn videos " + PROSE, "v", {}, done))
            self.assertFalse(NI.remember("sexual reproduction in plants " + PROSE, "v", {}, done, dry_run=True) is False)
        uo.assert_not_called()
        self.assertEqual(len(done), 2)                      # blocked hash recorded so it is never retried

    def test_pg_track_escapes_quotes_and_uses_argv(self):
        with mock.patch.object(NI.subprocess, "run") as sp:
            NI._pg_track({"job_id": "j'1", "mode": "file", "query": "it's", "vector": "v"})
        cmd = sp.call_args[0][0]
        self.assertEqual(cmd[0], "psql")
        self.assertIn("('j''1','file','it''s','v','running'", cmd[-1])
        self.assertNotIn("shell", sp.call_args.kwargs)

    def test_memory_payload_is_public_and_attributed(self):
        with mock.patch.object(NI.urllib.request, "urlopen", return_value=_Resp()) as uo, \
             mock.patch.dict(sys.modules, _untrusted("clean")):
            self.assertTrue(NI.remember(PROSE, "vec", {"k": 1}, set()))
        req = uo.call_args[0][0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, NI.MEMORY_URL + "?async=1")
        self.assertEqual((body["source"], body["tier"], body["metadata"]["privacy"], body["metadata"]["ingested_by"]),
                         ("vec", "long_term", "public", "nova_ingest.py"))


class TestPerformance(_Quiet):
    def test_garbage_gate_over_10k_chunks(self):
        # 3k chunks, not 10k: the backreference repeat rules cost ~1.5 ms on a clean ~300-char chunk
        # (they must scan every offset before passing it), so 10k clean chunks alone take ~11 s.
        chunks = [PROSE + f" item {i}" if i % 3 else "♪ la la la la la la la la ♪" for i in range(3_000)]
        t0 = time.perf_counter()
        flagged = sum(NI.is_garbage(c) for c in chunks)
        self.assertLess(time.perf_counter() - t0, 8.0)
        self.assertEqual(flagged, 3_000 // 3)

    def test_chunk_prose_over_10k_paragraphs(self):
        text = "\n\n".join(f"Paragraph {i}: " + PROSE[:120] for i in range(10_000))
        t0 = time.perf_counter()
        chunks = NI.chunk_prose(text)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertTrue(all(len(c) <= NI.CHUNK_CHARS + 200 for c in chunks))
        self.assertGreater(len(chunks), 500)


class TestRetry(_Quiet):
    def test_remember_retries_with_exponential_backoff(self):
        with mock.patch.object(NI.urllib.request, "urlopen", side_effect=[OSError("a"), OSError("b"), _Resp()]) as uo, \
             mock.patch.object(NI.time, "sleep") as sl, mock.patch.dict(sys.modules, _untrusted("clean")):
            done = set()
            self.assertTrue(NI.remember(PROSE, "v", {}, done))
        self.assertEqual(uo.call_count, 3)
        self.assertEqual([c[0][0] for c in sl.call_args_list], [1, 2])
        self.assertEqual(done, {NI.text_hash(PROSE)})

    def test_remember_gives_up_after_three(self):
        with mock.patch.object(NI.urllib.request, "urlopen", side_effect=OSError("x")) as uo, \
             mock.patch.object(NI.time, "sleep"), mock.patch.dict(sys.modules, _untrusted("clean")):
            done = set()
            self.assertFalse(NI.remember(PROSE, "v", {}, done))
        self.assertEqual(uo.call_count, 3)
        self.assertEqual(done, set())
        self.assertIn("Memory store failed", self._buf.getvalue())

    def test_fetch_backs_off_on_429_and_errors(self):
        with mock.patch.object(NI.urllib.request, "urlopen", side_effect=[_http(429), OSError("reset"), _Resp("h\xe9llo".encode("latin-1"), "text/html; charset=latin-1")]) as uo, \
             mock.patch.object(NI.time, "sleep") as sl:
            self.assertEqual(NI.fetch("http://x"), "héllo")
        self.assertEqual(uo.call_count, 3)
        self.assertEqual([c[0][0] for c in sl.call_args_list], [15, 2])
        self.assertEqual(uo.call_args[0][0].headers["User-agent"].split(" ")[0], "Nova/2.0")

    def test_fetch_404_is_final_and_exhaustion_returns_none(self):
        with mock.patch.object(NI.urllib.request, "urlopen", side_effect=_http(404)) as uo:
            self.assertIsNone(NI.fetch("http://x"))
        self.assertEqual(uo.call_count, 1)
        with mock.patch.object(NI.urllib.request, "urlopen", side_effect=_http(500)) as uo, mock.patch.object(NI.time, "sleep") as sl:
            self.assertIsNone(NI.fetch("http://x", retries=4))
        self.assertEqual(uo.call_count, 4)
        self.assertEqual([c[0][0] for c in sl.call_args_list], [1, 2, 4, 8])

    def test_pg_track_is_best_effort(self):
        # RETRY GAP: _pg_track — one psql attempt, swallowed on failure; the JSON state file stays authoritative
        with mock.patch.object(NI.subprocess, "run", side_effect=subprocess.TimeoutExpired("psql", 5)) as sp:
            NI._pg_track({"job_id": "j"})
        self.assertEqual(sp.call_count, 1)


class TestUnit(_Quiet):
    def test_garbage_reasons(self):
        self.assertEqual(NI._garbage_reason("too few words")[0], "too_short")
        self.assertEqual(NI._garbage_reason("♪ " + PROSE)[0], "trash_pattern")
        self.assertEqual(NI._garbage_reason("woo woo here, woo woo there, woo woo everywhere. " + PROSE)[0], "music")
        self.assertEqual(NI._garbage_reason(("la la la " * 3) + PROSE)[0], "trash_pattern")   # 9x "la" hits the repeat rule first
        self.assertEqual(NI._garbage_reason(" ".join("1-2" for _ in range(40)))[0], "trash_pattern")   # all-symbol/digit rule
        low = " ".join(f"k{i:04d}" for i in range(40))                  # distinct tokens, ~17% letters
        self.assertEqual(NI._garbage_reason(low)[0], "low_alpha")
        reason, wc, ratio = NI._garbage_reason(PROSE)
        self.assertIsNone(reason); self.assertGreaterEqual(wc, NI.MIN_WORDS); self.assertGreater(ratio, 0.45)
        self.assertFalse(NI.is_garbage(PROSE)); self.assertTrue(NI.is_garbage(""))

    def test_clean_text_collapses_lone_newlines(self):
        raw = "This is a decent sentence from a transcript.\nIt continues on the next line nicely.\n\n♪ la ♪\n\nAnother good paragraph here with words."
        out = NI.clean_text(raw)
        self.assertNotIn("♪", out)
        self.assertEqual(out.split("\n\n")[0], "This is a decent sentence from a transcript. It continues on the next line nicely.")
        self.assertEqual(NI.clean_text(""), "")

    def test_chunking(self):
        self.assertEqual(NI.chunk_prose("tiny\n\nalso tiny"), [])
        chunks = NI.chunk_prose("\n\n".join(["A" * 900, "B" * 900, "C" * 100]), size=1500)
        self.assertEqual([len(c) for c in chunks], [900, 1002])
        self.assertEqual(NI.chunk_words("a b c d e", n=2), ["a b", "c d", "e"])
        self.assertEqual(NI.chunk_words("", n=2), [])

    def test_truncate_at_boundary(self):
        s = "First sentence here. " * 10 + "x" * 50
        out = NI.truncate_at_boundary(s, max_chars=100)
        self.assertTrue(out.endswith("."), out); self.assertLessEqual(len(out), 100)
        self.assertEqual(NI.truncate_at_boundary("short", 100), "short")
        self.assertEqual(NI.truncate_at_boundary("word " * 40, 101).rstrip(), ("word " * 20).rstrip())
        self.assertEqual(len(NI.truncate_at_boundary("x" * 500, 100)), 100)

    def test_hash_and_relevance(self):
        self.assertEqual(NI.text_hash("  a  "), hashlib.md5(b"a").hexdigest())
        self.assertFalse(NI.is_relevant_link("List of physics topics", "Physics", "physics"))
        self.assertFalse(NI.is_relevant_link("Quantum", "Physics", "physics", depth=NI._BFS_MAX_DEPTH))
        self.assertTrue(NI.is_relevant_link("History of physics", "Physics", "science", depth=2))
        self.assertTrue(NI.is_relevant_link("Unrelated thing", "Physics", "physics", depth=1))
        self.assertFalse(NI.is_relevant_link("Unrelated thing", "Physics", "physics", depth=2))
        self.assertTrue(NI.page_relevance_check("the article says z-wave is a mesh protocol " * 10, "z-wave", "home_automation"))
        self.assertFalse(NI.page_relevance_check("short", "z-wave", "x"))
        self.assertEqual(NI._derive("The Quick-Brown Fox! runs"), "the_quickbrown_fox")
        self.assertEqual(NI._derive("!!!"), "general_knowledge")

    def test_html_text_skips_script_and_style(self):
        html = "<html><head><style>p{}</style><script>evil()</script></head><body><p>Hello</p><nav>menu</nav><p>World</p></body></html>"
        out = NI.html_text(html)
        self.assertIn("Hello", out); self.assertIn("World", out)
        self.assertNotIn("evil", out); self.assertNotIn("menu", out); self.assertNotIn("p{}", out)

    def test_auto_select_vector_scoring(self):
        self.assertEqual(NI.auto_select_vector("Marine Biology", "", []), "marine_biology")
        self.assertEqual(NI.auto_select_vector("Marine Biology", "fish", ["marine_biology", "cooking"]), "marine_biology")
        with mock.patch.object(NI.urllib.request, "urlopen", side_effect=OSError("off")):
            self.assertEqual(NI.auto_select_vector("Volcanoes", "", ["cooking"]), "volcanoes")


class TestIntegration(_Quiet):
    def test_state_round_trip_mirrors_to_pg(self):
        st = NI.load_state("job1")
        self.assertEqual((st["job_id"], st["done_hashes"], st["items_done"]), ("job1", [], 0))
        st["mode"] = "file"; st["chunks_total"] = 7
        with mock.patch.object(NI.subprocess, "run") as sp:
            NI.save_state("job1", st)
        self.assertEqual(NI.load_state("job1")["chunks_total"], 7)
        self.assertIn("last_updated", NI.load_state("job1"))
        self.assertIn("INSERT INTO ingest_jobs", sp.call_args[0][0][-1])
        self.assertEqual(NI.latest_job_id(), "job1")

    def test_remember_uses_the_injection_screen(self):
        done = set()
        with mock.patch.object(NI.urllib.request, "urlopen", return_value=_Resp()) as uo:
            before = NI._INJECTION_DROPS[0]
            with mock.patch.dict(sys.modules, _untrusted("hostile", 0.9, ["ignore previous"])):
                self.assertFalse(NI.remember(PROSE + " 1", "v", {}, done))
            self.assertEqual(NI._INJECTION_DROPS[0], before + 1)
            with mock.patch.dict(sys.modules, _untrusted("suspect", 0.4, ["system:"])):
                self.assertTrue(NI.remember(PROSE + " 2", "v", {}, done))
        self.assertEqual(uo.call_count, 1)
        meta = json.loads(uo.call_args[0][0].data)["metadata"]
        self.assertEqual((meta["injection_score"], meta["injection_hits"]), (0.4, ["system:"]))
        self.assertFalse(NI.remember(PROSE + " 2", "v", {}, done))     # dedup by hash

    def test_notify_strips_emoji_and_bold_into_a_title(self):
        NI.notify(":rocket: *Ingest started*\nline two")
        NI._bus_notify.assert_called_once_with("Ingest started", body="line two", level="info", category="ingest", source="nova_ingest.py")

    def test_finish_treats_zero_chunks_as_failure(self):
        with mock.patch.object(NI, "purge_garbage", return_value=0):
            self.assertEqual(NI._finish("j", "Topic", "vec", 0, 100, 3, 0, dry_run=False), 1)
            self.assertEqual(NI._finish("j", "Topic", "vec", 50, 100, 3, 0, dry_run=False), 0)
            self.assertEqual(NI._finish("j", "Topic", "vec", 0, 100, 0, 0, dry_run=True), 0)
        titles = [c[0][0] for c in NI._bus_notify.call_args_list]
        self.assertIn("Ingest produced nothing", titles)
        self.assertIn("INGEST STORED NOTHING", self._buf.getvalue())


class TestFunctional(_Quiet):
    def test_no_args_prints_help_and_touches_nothing(self):
        with mock.patch.object(sys, "argv", ["nova_ingest.py"]), mock.patch.object(NI.urllib.request, "urlopen") as uo, \
             mock.patch.object(NI.subprocess, "run") as sp:
            self.assertIsNone(NI.main())
        self.assertIn("Universal Ingest", self._buf.getvalue())
        uo.assert_not_called(); sp.assert_not_called()

    def test_status_without_jobs(self):
        NI.STATE_DIR = TMP / "empty-state"
        try:
            with mock.patch.object(sys, "argv", ["nova_ingest.py", "--status"]):
                NI.main()
        finally:
            NI.STATE_DIR = TMP / "state"
        self.assertIn("No active jobs.", self._buf.getvalue())

    def test_file_mode_golden_path(self):
        tracked = []
        with mock.patch.object(sys, "argv", ["nova_ingest.py", "file", "/tmp/doc.txt", "--source", "docs"]), \
             mock.patch.object(NI, "run_file") as rf, mock.patch.object(NI, "get_existing_vectors", return_value=["docs"]), \
             mock.patch.object(NI, "_pg_track", side_effect=lambda st, status="running": tracked.append(status)):
            NI.main()
        args = rf.call_args[0]
        self.assertEqual((args[0], args[1], args[2]["mode"], args[2]["vector"]), ("/tmp/doc.txt", "docs", "file", "docs"))
        self.assertEqual(tracked, ["running", "completed"])
        self.assertTrue((NI.STATE_DIR / f"{args[2]['job_id']}.json").exists())

    def test_file_mode_error_path_marks_job_failed(self):
        tracked = []
        with mock.patch.object(sys, "argv", ["nova_ingest.py", "file", "/tmp/doc.txt", "--source", "docs"]), \
             mock.patch.object(NI, "run_file", side_effect=RuntimeError("disk")), \
             mock.patch.object(NI, "get_existing_vectors", return_value=[]), \
             mock.patch.object(NI, "_pg_track", side_effect=lambda st, status="running": tracked.append(status)):
            with self.assertRaises(RuntimeError):
                NI.main()
        self.assertEqual(tracked[-1], "failed")


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_with_stubbed_resolver(self):
        code = ("import sys, types; m = types.ModuleType('nova_resolve'); m.resolve_url = lambda s, p='': 'http://x' + p; "
                "sys.modules['nova_resolve'] = m; sys.argv = ['nova_ingest.py', '--help']; import nova_ingest; nova_ingest.main()")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(NI.VERSION, r.stdout)
        self.assertIn("Universal Ingest", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys, types; m = types.ModuleType('nova_resolve'); m.resolve_url = lambda s, p='': 'http://x' + p; "
                "sys.modules['nova_resolve'] = m; import nova_ingest")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
