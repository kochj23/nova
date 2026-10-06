#!/usr/bin/env python3
"""Tests for nova_rf_discovery_whisper.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The script is a bare worker: it loads a Whisper model and enters its watch loop at import time
(no __main__ guard), so it is NEVER imported here. Its functions and constants are lifted out of
the AST and exec'd in isolation; the watch loop body is exercised one pass at a time from the AST
with every filesystem/model/network call mocked."""
import ast
import glob as _glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
PATH = SCRIPTS / "nova_rf_discovery_whisper.py"
if not PATH.exists():          # the worker is .gitignored (host-local); skip on clones that lack it
    import pytest
    pytest.skip("nova_rf_discovery_whisper.py not present on this host", allow_module_level=True)
SRC = PATH.read_text()
TREE = ast.parse(SRC)


def _isolated():
    """Exec only the defs + constant assigns (MEM, D) — no model load, no loop."""
    keep = [n for n in TREE.body if isinstance(n, ast.FunctionDef)
            or (isinstance(n, ast.Assign) and n.targets[0].id in ("MEM", "D"))]
    ns = {"os": os, "time": time, "glob": _glob, "json": json, "urllib": urllib}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(PATH), "exec"), ns)
    return types.SimpleNamespace(**{k: v for k, v in ns.items() if not k.startswith("__")}), ns


rf, NS = _isolated()


def _loop_pass_code():
    """The body of the `while True:` loop minus the trailing sleep, as one compiled pass."""
    loop = next(n for n in TREE.body if isinstance(n, ast.While))
    body = [n for n in loop.body if not (isinstance(n, ast.Expr) and "sleep" in ast.dump(n))]
    return compile(ast.Module(body=body, type_ignores=[]), str(PATH), "exec")


PASS = _loop_pass_code()


def _run_pass(d, segs_text):
    model = mock.Mock()
    model.transcribe.return_value = ([types.SimpleNamespace(text=t) for t in segs_text], None)
    ns = dict(NS, D=d, m=model)
    with mock.patch.object(urllib.request, "urlopen") as uo, mock.patch("builtins.print"):
        exec(PASS, ns)
    return model, uo


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_filename_label_cannot_escape_metadata(self):
        freq, demod, label = rf.parse_name('/x/nrsp2_1__462.5__nfm__a"}_injected.wav')
        with mock.patch.object(urllib.request, "urlopen") as uo:
            rf.rem("hi", freq, demod, label)
        body = json.loads(uo.call_args[0][0].data)          # still valid JSON — label is data, not syntax
        self.assertEqual(body["metadata"]["channel"], 'a"} injected')
        self.assertEqual(body["source"], "rf_discovery")


class TestPerformance(unittest.TestCase):
    def test_parse_name_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            rf.parse_name(f"/c/nrsp2_{i}__{i % 900}.5__nfm__fire_dispatch.wav")
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_remember_fails_open(self):
        # RETRY GAP: rem() — one urlopen attempt; a dead memory server is swallowed
        with mock.patch.object(urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertIsNone(rf.rem("t", 1.0, "nfm", "x"))
        self.assertEqual(uo.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_parse_name_shapes(self):
        self.assertEqual(rf.parse_name("/c/nrsp2_9__462.55__nfm__frs_ch_1.wav"), (462.55, "nfm", "frs ch 1"))
        self.assertEqual(rf.parse_name("/c/nrsp2_9__bad__am__air.wav"), (None, "am", "air"))
        self.assertEqual(rf.parse_name("/c/random.wav"), (None, None, "unknown"))

    def test_tuner_for_bands(self):
        self.assertIsNone(rf.tuner_for(None))
        self.assertEqual(rf.tuner_for(400), "Tuner 1 50 ohm")
        self.assertEqual(rf.tuner_for(162.4), "Tuner 2 50 ohm")


class TestIntegration(unittest.TestCase):
    def test_posts_async_to_memory_remember(self):
        self.assertTrue(rf.MEM.endswith("/remember"))
        with mock.patch.object(urllib.request, "urlopen") as uo:
            rf.rem("copy that", 462.5, "nfm", "frs")
        req = uo.call_args[0][0]
        self.assertTrue(req.full_url.endswith("/remember?async=1"))
        meta = json.loads(req.data)["metadata"]
        self.assertEqual((meta["kind"], meta["freq_mhz"], meta["tuner"]), ("rf_discovery", 462.5, "Tuner 1 50 ohm"))

    def test_parse_feeds_rem_text_prefix(self):
        with mock.patch.object(urllib.request, "urlopen") as uo:
            rf.rem("hello", *rf.parse_name("/c/nrsp2_1__121.5__am__guard.wav"))
        self.assertTrue(json.loads(uo.call_args[0][0].data)["text"].startswith("[guard 121.5MHz am] hello"))


class TestFunctional(unittest.TestCase):
    def _wav(self, d, name, size, age=10):
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(b"\0" * size)
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    def test_one_pass_transcribes_posts_and_deletes(self):
        with tempfile.TemporaryDirectory() as d:
            good = self._wav(d, "nrsp2_1__462.5__nfm__frs.wav", 50_000)
            tiny = self._wav(d, "nrsp2_2__462.5__nfm__frs.wav", 100)
            fresh = self._wav(d, "nrsp2_3__462.5__nfm__frs.wav", 50_000, age=0)
            model, uo = _run_pass(d, ["unit four", "on scene now"])
            self.assertEqual(model.transcribe.call_count, 1)
            self.assertEqual(uo.call_count, 1)
            self.assertIn("unit four on scene now", json.loads(uo.call_args[0][0].data)["text"])
            self.assertFalse(os.path.exists(good))
            self.assertFalse(os.path.exists(tiny))
            self.assertTrue(os.path.exists(fresh))       # still being written — left for next pass

    def test_short_transcript_not_posted_and_model_error_survives(self):
        with tempfile.TemporaryDirectory() as d:
            self._wav(d, "nrsp2_1__1__nfm__x.wav", 50_000)
            _, uo = _run_pass(d, ["ok"])
            uo.assert_not_called()
            w = self._wav(d, "nrsp2_2__1__nfm__x.wav", 50_000)
            model = mock.Mock()
            model.transcribe.side_effect = RuntimeError("decode")
            with mock.patch("builtins.print"):
                exec(PASS, dict(NS, D=d, m=model))
            self.assertFalse(os.path.exists(w))          # finally: removed even on error


class TestFrame(unittest.TestCase):
    def test_compiles_and_has_one_bounded_loop(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(PATH)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        loops = [n for n in TREE.body if isinstance(n, ast.While)]
        self.assertEqual(len(loops), 1)
        self.assertIn("sleep", ast.dump(loops[0].body[-1]))   # every pass ends in a sleep — no hot spin
        # NOTE: no __main__ guard — importing starts the model + loop, so tests never import it.
        self.assertNotIn("__main__", SRC)


if __name__ == "__main__":
    unittest.main()
