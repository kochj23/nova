#!/usr/bin/env python3
"""Tests for nova_voice_clone.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
SCRIPT = SCRIPTS / "nova_voice_clone.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="voice-clone-test-"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vc = _load("voice_clone_under_test", SCRIPT)


def _tts_stub(fail=None):
    """A fake `TTS` package: records constructor + tts_to_file calls; `fail` raises from tts_to_file."""
    inst = MagicMock()
    if fail:
        inst.tts_to_file.side_effect = fail
    pkg = types.ModuleType("TTS"); api = types.ModuleType("TTS.api")
    api.TTS = MagicMock(return_value=inst); pkg.api = api
    return {"TTS": pkg, "TTS.api": api}, api.TTS, inst


def _run(argv, stubs=None):
    stubs = stubs or _tts_stub()
    with patch.dict(sys.modules, stubs[0]), patch.object(sys, "argv", ["nova_voice_clone.py", *argv]), \
         redirect_stdout(io.StringIO()) as out:
        vc.main()
    return stubs[1], stubs[2], out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_no_shell_and_license_env_is_setdefault_only(self):
        self.assertNotIn("shell=True", SRC); self.assertNotIn("os.system", SRC); self.assertNotIn("subprocess", SRC)
        self.assertIn('os.environ.setdefault("COQUI_TOS_AGREED", "1")', SRC)   # never clobbers an operator's choice

    def test_text_is_passed_verbatim_never_interpolated_into_a_command(self):
        evil = "'; echo pwned > /tmp/x #"
        out = str(TMP / "sec.wav")
        ctor, inst, _ = _run(["--text", evil, "--out", out])
        self.assertEqual(inst.tts_to_file.call_args[1]["text"], evil)


class TestPerformance(unittest.TestCase):
    def test_main_hot_path_200_runs_under_2s(self):
        out = str(TMP / "perf.wav")
        t0 = time.perf_counter()
        for _ in range(200):
            ctor, inst, _ = _run(["--text", "hi", "--out", out])
        self.assertLess(time.perf_counter() - t0, 2.0)
        ctor.assert_called_once()                                  # one model load per invocation, no hidden retries


class TestRetry(unittest.TestCase):
    def test_synthesis_failure_is_one_shot(self):
        # RETRY GAP: main()/TTS.tts_to_file — a hand-run CLI; one attempt, the error escapes to the operator
        stubs = _tts_stub(fail=RuntimeError("cuda oom"))
        with self.assertRaises(RuntimeError):
            _run(["--text", "x", "--out", str(TMP / "r.wav")], stubs)
        self.assertEqual(stubs[2].tts_to_file.call_count, 1)

    def test_model_load_failure_is_one_shot_and_writes_nothing(self):
        # RETRY GAP: main()/TTS() model download — no retry; the --out file is never created
        stubs, ctor, _ = _tts_stub()
        ctor.side_effect = OSError("download failed")
        out = TMP / "never.wav"
        with self.assertRaises(OSError):
            _run(["--text", "x", "--out", str(out)], (stubs, ctor, None))
        self.assertEqual(ctor.call_count, 1)
        self.assertFalse(out.exists())


class TestUnit(unittest.TestCase):
    def test_defaults_live_under_openclaw(self):
        self.assertTrue(vc.DEFAULT_REF.endswith("/.openclaw/voice_refs/btmrr_spiel.wav"))
        self.assertTrue(vc.DEFAULT_TEXT.startswith("Howdy, partners!"))
        self.assertLess(len(vc.DEFAULT_TEXT), 400)

    def test_text_file_is_read_and_stripped(self):
        tf = TMP / "say.txt"; tf.write_text("  All aboard!  \n\n")
        ctor, inst, out = _run(["--text-file", str(tf), "--out", str(TMP / "u.wav")])
        self.assertEqual(inst.tts_to_file.call_args[1]["text"], "All aboard!")

    def test_missing_text_file_errors_before_loading_the_model(self):
        stubs = _tts_stub()
        with self.assertRaises(FileNotFoundError):
            _run(["--text-file", str(TMP / "absent.txt"), "--out", str(TMP / "m.wav")], stubs)
        stubs[1].assert_not_called()


class TestIntegration(unittest.TestCase):
    def test_out_dir_is_created_and_ref_flows_to_speaker_wav(self):
        out = TMP / "nested" / "deeper" / "clone.wav"
        ctor, inst, _ = _run(["--ref", "/tmp/ref.wav", "--text", "yo", "--out", str(out)])
        self.assertTrue(out.parent.is_dir())
        kw = inst.tts_to_file.call_args[1]
        self.assertEqual(kw, {"text": "yo", "speaker_wav": "/tmp/ref.wav", "language": "en", "file_path": str(out)})
        ctor.assert_called_once_with("tts_models/multilingual/multi-dataset/xtts_v2", progress_bar=False)


class TestFunctional(unittest.TestCase):
    def test_golden_path_reports_what_it_wrote(self):
        out = str(TMP / "golden.wav")
        ctor, inst, text = _run(["--text", "Hang on to them hats", "--out", out])
        self.assertIn("Loading XTTS-v2", text)
        self.assertIn(f"wrote {out}", text)
        self.assertIn('said: "Hang on to them hats..."', text)
        self.assertIn("Cloning voice from btmrr_spiel.wav", text)

    def test_default_text_is_used_when_nothing_given(self):
        ctor, inst, _ = _run(["--out", str(TMP / "d.wav")])
        self.assertEqual(inst.tts_to_file.call_args[1]["text"], vc.DEFAULT_TEXT)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_never_runs_main(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        for flag in ("--ref", "--text", "--text-file", "--out"):
            self.assertIn(flag, r.stdout)
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_voice_clone"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
