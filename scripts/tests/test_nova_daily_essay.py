#!/usr/bin/env python3
"""Tests for nova_daily_essay.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The module reads a Keychain item at import, so subprocess.run is mocked for the load. Every test then
runs with subprocess.run and urllib.request.urlopen mocked to raise (opt-in per test), the module's
nova_config is a local proxy with post_both stubbed, and LOG_FILE / STATE_FILE live in a tempdir."""
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_daily_essay.py"
SRC = SCRIPT.read_text()
sys.path.insert(0, str(SCRIPTS))
TMP = tempfile.TemporaryDirectory()


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)


def _load():
    with patch("subprocess.run", return_value=_cp(out="")) as kc:
        spec = importlib.util.spec_from_file_location("nova_daily_essay_t", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    mod._KEYCHAIN_CALLS = kc.call_args_list
    real_cfg = mod.nova_config
    mod.nova_config = types.SimpleNamespace(
        post_both=MagicMock(), is_private_source=real_cfg.is_private_source,
        filter_private_memories=real_cfg.filter_private_memories)
    mod.LOG_FILE = Path(TMP.name) / "essay.log"
    mod.STATE_FILE = Path(TMP.name) / "state" / "essay_state.json"
    mod.JORDAN_CC = "cc@example.invalid"
    return mod


es = _load()
ESSAY = "The Quiet Mechanics Of Orbits\n\n" + ("Planetary motion follows measurable laws across centuries. " * 40)


class _Base(unittest.TestCase):
    """Every test starts with all outbound calls refused; tests opt in to specific answers."""
    def setUp(self):
        boom = MagicMock(side_effect=AssertionError("unmocked outbound call"))
        for target in ("subprocess.run", "urllib.request.urlopen"):
            p = patch(target, boom)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(es.time, "sleep")
        p.start()
        self.addCleanup(p.stop)
        es.nova_config.post_both.reset_mock()
        if es.STATE_FILE.exists():
            es.STATE_FILE.unlink()


def _resp(obj):
    r = MagicMock()
    r.read.return_value = json.dumps(obj).encode()
    return r


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn("nova-openrouter-api-key", SRC)  # key comes from Keychain

    def test_fetch_memories_escapes_source_quotes(self):
        with patch("subprocess.run", return_value=_cp(out="")) as run:
            es.fetch_memories("x' OR '1'='1", n=3)
        sql = run.call_args_list[0].args[0][-1]
        self.assertIn("source = 'x'' OR ''1''=''1'", sql)

    def test_private_sources_never_picked(self):
        with patch.object(es, "get_sources_with_counts",
                          return_value=[{"source": "work_internal", "count": 99},
                                        {"source": "email_archive", "count": 99},
                                        {"source": "astronomy", "count": 99}]):
            for _ in range(20):
                self.assertEqual(es.pick_subject({"recent_sources": []}), "astronomy")

    def test_scrub_personal(self):
        addr = "kochj23" + "@" + "gmail.com"
        out = es._scrub_personal(f"mail {addr} at {Path.home()}/notes")
        self.assertNotIn(addr, out)
        self.assertNotIn(str(Path.home()), out)


class TestPerformance(_Base):
    def test_format_sources_and_scrub_hot_path(self):
        mems = [{"text": f"memory {i} " * 30, "metadata": json.dumps({"title": f"t{i}"})} for i in range(10_000)]
        t0 = time.perf_counter()
        for m in mems[:2000]:
            es._scrub_personal(m["text"])
        out = es.format_sources(mems, "astronomy")
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(out.count("\n- **"), es.ESSAY_MEMORIES)


class TestRetry(_Base):
    def test_openrouter_failure_falls_through_ollama_chain(self):
        ollama = MagicMock(side_effect=[RuntimeError("busy"), "", ESSAY])
        with patch.object(es, "_generate_via_openrouter", side_effect=RuntimeError("402")), \
                patch.object(es, "_generate_via_ollama", ollama), redirect_stdout(io.StringIO()):
            self.assertEqual(es.generate_essay("astronomy", [{"text": "m"}]), ESSAY)
        self.assertEqual([c.args[2] for c in ollama.call_args_list], [es.OLLAMA_MODEL] + es.FALLBACK_MODELS)

    def test_image_generation_retries_three_times_then_none(self):
        with patch.object(es, "_ensure_swarmui_backend", return_value=True), \
                patch.object(es, "_get_safe_image_prompt", return_value="p"), \
                patch("subprocess.run", return_value=_cp(1, err="oom")) as run, redirect_stdout(io.StringIO()):
            self.assertIsNone(es.generate_essay_image(ESSAY, "astronomy"))
        self.assertEqual(run.call_count, 3)
        self.assertEqual(es.time.sleep.call_count, 2)

    def test_stats_failure_fails_open(self):
        # RETRY GAP: get_sources_with_counts — one urlopen attempt, [] on failure (DB fallback takes over)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(es.get_sources_with_counts(), [])


class TestUnit(_Base):
    def test_extract_title(self):
        self.assertEqual(es.extract_title("\n# The Long Title\nbody"), "The Long Title")
        self.assertEqual(es.extract_title("ab\n\n"), "Nova's Daily Essay")

    def test_short_or_empty_essay_rejected(self):
        with patch.object(es, "_generate_via_openrouter", return_value="too short"), redirect_stdout(io.StringIO()):
            self.assertIsNone(es.generate_essay("s", [{"text": "m"}]))

    def test_pick_subject_resets_when_all_recent(self):
        state = {"recent_sources": ["astronomy"]}
        with patch.object(es, "get_sources_with_counts", return_value=[{"source": "astronomy", "count": 60}]):
            self.assertEqual(es.pick_subject(state), "astronomy")
        self.assertEqual(state["recent_sources"], [])

    def test_format_sources_dedups_and_handles_bad_metadata(self):
        out = es.format_sources([{"text": "same", "metadata": "{bad"}, {"text": "same", "metadata": "{}"}], "deep_sea")
        self.assertEqual(out.count("[Deep Sea] same"), 1)


class TestIntegration(_Base):
    def test_fetch_memories_falls_back_and_applies_privacy_gate(self):
        rows = "\n".join(["ok text\x1f{}\x1f2026\x1fastronomy", "secret\x1f{}\x1f2026\x1fwork_internal"])
        with patch("subprocess.run", side_effect=[_cp(1, err="no vector"), _cp(0, out=rows)]) as run, \
                redirect_stdout(io.StringIO()):
            mems = es.fetch_memories("astronomy")
        self.assertIn("<=>", run.call_args_list[0].args[0][-1])
        self.assertIn("ORDER BY random()", run.call_args_list[1].args[0][-1])
        self.assertEqual([m["text"] for m in mems], ["ok text"])

    def test_db_fallback_parses_counts(self):
        with patch("subprocess.run", return_value=_cp(0, out="astronomy|120\nbirds|55\n")):
            self.assertEqual(es.get_source_counts_from_db(),
                             [{"source": "astronomy", "count": 120}, {"source": "birds", "count": 55}])


class TestFunctional(_Base):
    def test_golden_path_mails_posts_and_saves_state(self):
        herd = types.ModuleType("herd_config")
        herd.HERD = [{"email": "a@example.invalid"}, {"email": "b@example.invalid"}]
        mems = [{"text": f"orbit fact {i}", "metadata": "{}", "source": "astronomy"} for i in range(12)]
        with patch.dict(sys.modules, {"herd_config": herd}), \
                patch.object(es, "get_sources_with_counts", return_value=[{"source": "astronomy", "count": 99}]), \
                patch.object(es, "fetch_memories", return_value=mems), \
                patch.object(es, "generate_essay", return_value=ESSAY), \
                patch.object(es, "generate_essay_image", return_value=None), \
                patch.object(es, "publish_to_journal") as pub, \
                patch("subprocess.run", return_value=_cp(0)) as run, redirect_stdout(io.StringIO()):
            es.main()
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--to") + 1], "a@example.invalid")
        self.assertIn("cc@example.invalid", cmd[cmd.index("--cc") + 1])
        pub.assert_called_once()
        posts = [c.args[0] for c in es.nova_config.post_both.call_args_list]
        self.assertTrue(any("Image generation failed" in p for p in posts))
        self.assertTrue(any("Nova's Daily Essay" in p for p in posts))
        state = json.loads(es.STATE_FILE.read_text())
        self.assertEqual((state["essay_count"], state["recent_sources"]), (1, ["astronomy"]))

    def test_refusal_loops_then_aborts_without_posting(self):
        refusal = "I can't write this responsibly. " * 30
        with patch.object(es, "pick_subject", side_effect=["a", "b", "c", "d", "e"]), \
                patch.object(es, "fetch_memories", return_value=[{"text": "m"}] * 12), \
                patch.object(es, "generate_essay", return_value=refusal), redirect_stdout(io.StringIO()) as out:
            es.main()
        self.assertIn("ABORT", out.getvalue())
        es.nova_config.post_both.assert_not_called()
        self.assertFalse(es.STATE_FILE.exists())


class TestFrame(unittest.TestCase):
    def test_import_reads_only_keychain_and_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertEqual(len(es._KEYCHAIN_CALLS), 1)          # the CC lookup, mocked at load
        self.assertEqual(es._KEYCHAIN_CALLS[0].args[0][0], "security")

    def test_module_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
