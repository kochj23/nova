#!/usr/bin/env python3
"""Tests for nova_say.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The real TTS engine (Coqui XTTS) is never loaded: a fake `TTS.api` module stands in."""
import importlib.util
import io
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
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_say.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with patch.dict(os.environ, {}):            # the module's COQUI_TOS_AGREED setdefault must not leak
    ns = _load("nova_say_t", SCRIPT)
SRC = SCRIPT.read_text()


def _fake_tts(fail=None):
    engine = MagicMock()
    if fail:
        engine.tts_to_file.side_effect = fail
    cls = MagicMock(return_value=engine)
    api = types.ModuleType("TTS.api"); api.TTS = cls
    pkg = types.ModuleType("TTS"); pkg.api = api
    return {"TTS": pkg, "TTS.api": api}, cls, engine


def _say(argv, fail=None):
    mods, cls, engine = _fake_tts(fail)
    with patch.dict(sys.modules, mods), patch.object(sys, "argv", ["nova_say.py", *argv]), \
            redirect_stdout(io.StringIO()) as out:
        ns.main()
    return cls, engine, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_no_shell_no_network(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r"subprocess|os\.system|urllib|requests", SRC))

    def test_builtin_speaker_not_a_clone(self):
        with tempfile.TemporaryDirectory() as d:
            _, engine, _ = _say(["hi", "--out", f"{d}/a.wav"])
        self.assertNotIn("speaker_wav", engine.tts_to_file.call_args.kwargs)   # never a reference-voice clone


class TestPerformance(unittest.TestCase):
    def test_long_text_passed_through_once(self):
        with tempfile.TemporaryDirectory() as d:
            t0 = time.perf_counter()
            _, engine, out = _say(["x" * 10_000, "--out", f"{d}/a.wav"])
            self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(engine.tts_to_file.call_count, 1)
        self.assertIn("x" * 70 + '"', out)                     # confirmation line truncates to 70 chars


class TestRetry(unittest.TestCase):
    def test_tts_failure_propagates_without_claiming_success(self):
        # RETRY GAP: main()/tts_to_file — one synthesis attempt; an engine error escapes (hand-run CLI) and
        # the success line is never printed
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                _say(["hi", "--out", f"{d}/a.wav"], fail=RuntimeError("model load failed"))


class TestUnit(unittest.TestCase):
    def test_voice_default_and_env_override(self):
        with tempfile.TemporaryDirectory() as d:
            _, engine, _ = _say(["hi", "--out", f"{d}/a.wav"])
            self.assertEqual(engine.tts_to_file.call_args.kwargs["speaker"], "Ana Florence")
            _, engine, _ = _say(["hi", "--voice", "Claribel Dervla", "--out", f"{d}/b.wav"])
            self.assertEqual(engine.tts_to_file.call_args.kwargs["speaker"], "Claribel Dervla")

    def test_out_dir_created(self):
        with tempfile.TemporaryDirectory() as d:
            _say(["hi", "--out", f"{d}/deep/nested/a.wav"])
            self.assertTrue(Path(f"{d}/deep/nested").is_dir())


class TestIntegration(unittest.TestCase):
    def test_uses_xtts_v2_quietly(self):
        with tempfile.TemporaryDirectory() as d:
            cls, engine, _ = _say(["hello", "--out", f"{d}/a.wav"])
        self.assertEqual(cls.call_args.args[0], "tts_models/multilingual/multi-dataset/xtts_v2")
        self.assertEqual(cls.call_args.kwargs, {"progress_bar": False})


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_requested_file(self):
        with tempfile.TemporaryDirectory() as d:
            _, engine, out = _say(["Good evening, Little Mister", "--out", f"{d}/n.wav"])
        kw = engine.tts_to_file.call_args.kwargs
        self.assertEqual((kw["text"], kw["language"], kw["file_path"]), ("Good evening, Little Mister", "en", f"{d}/n.wav"))
        self.assertIn("said:", out)

    def test_missing_text_is_usage_error(self):
        with patch.object(sys, "argv", ["nova_say.py"]), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as e:
                ns.main()
        self.assertEqual(e.exception.code, 2)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_without_loading_tts(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--voice", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_say"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
