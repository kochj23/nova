#!/usr/bin/env python3
"""Tests for nova_make.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_make.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_make_test_"))


def _load():
    spec = importlib.util.spec_from_file_location("nova_make_ut", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.OUT_DIR = TMP / "out"
    mod.log = lambda m: None
    return mod


nm = _load()
GOOD = {"ok": True, "watertight": True, "dims_mm": [50, 50, 75], "volume_mm3": 12000, "faces": 44,
        "stl": str(TMP / "p.stl"), "png": str(TMP / "p.png")}


def _3mf(config):
    p = TMP / f"s{time.time_ns()}.gcode.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/slice_info.config", config)
    return str(p)


def _watch_status(status_line, print_rc=0):
    calls = []

    def fake(args):
        calls.append(args)
        if args[0] == "status":
            return MagicMock(stdout=status_line, stderr="", returncode=0)
        return MagicMock(stdout="sent", stderr="", returncode=print_rc)
    return fake, calls


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_key_from_keychain(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token|key)\s*=\s*['\"][A-Za-z0-9+/\-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("sk-or-", SRC)
        self.assertIn('"-s", "nova-openrouter-api-key"', SRC)

    def test_refuses_to_print_on_busy_printer(self):
        fake, calls = _watch_status("P1: RUNNING 42%")
        with patch.object(nm, "_watch", fake), self.assertRaises(RuntimeError):
            nm.print_part("x.3mf", "P1")
        self.assertEqual([c[0] for c in calls], ["status"])          # never sent the print

    def test_validation_rejects_oversized_and_leaky_parts(self):
        self.assertFalse(nm.validate({**GOOD, "dims_mm": [251, 10, 10]})[0])
        self.assertFalse(nm.validate({**GOOD, "watertight": False})[0])
        self.assertFalse(nm.validate({**GOOD, "faces": 4})[0])


class TestPerformance(unittest.TestCase):
    def test_validate_and_extract_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            nm.validate({**GOOD, "dims_mm": [i % 300, 10, 10]})
            nm._extract_code(f"```python\nresult = {i}\n```")
        self.assertLess(time.perf_counter() - t0, 1.5)


class TestRetry(unittest.TestCase):
    def test_generate_retries_until_valid(self):
        gen = MagicMock(side_effect=[RuntimeError("ollama down"), "bad code", "good code"])
        runs = MagicMock(side_effect=[{"ok": False, "error": "NameError"}, dict(GOOD)])
        with patch.object(nm, "gen_local", gen), patch.object(nm, "gen_cloud") as cloud, patch.object(nm, "run_part", runs):
            meta = nm.generate_part("a hex tidy")
        self.assertEqual(gen.call_count, 3)
        cloud.assert_not_called()
        self.assertEqual(meta["source"], "good code")
        self.assertIn("NameError", gen.call_args.args[0])           # feedback carried into the repair prompt

    def test_escalates_to_cloud_then_gives_up(self):
        with patch.object(nm, "gen_local", side_effect=OSError("down")) as loc, \
             patch.object(nm, "gen_cloud", side_effect=OSError("down")) as cloud, self.assertRaises(RuntimeError):
            nm.generate_part("x")
        self.assertEqual((loc.call_count, cloud.call_count), (3, 2))


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with redirect_stdout(io.StringIO()) as out:
            nm.selftest()
        self.assertIn("selftest OK", out.getvalue())

    def test_slice_limits_fallbacks(self):
        ok, reason, g, h = nm.slice_limits(_3mf('key="prediction" value="7200" key="weight" value="12.5"'), 40, 4)
        self.assertEqual((ok, g, h), (True, 12.5, 2.0))
        ok, reason, g, _ = nm.slice_limits(_3mf('key="weight" value="" used_m="10"'), 20, 4)
        self.assertFalse(ok)
        self.assertAlmostEqual(g, 29.8)
        ok, reason, _, _ = nm.slice_limits(_3mf('key="prediction" value="36000" used_g="5"'), 40, 4)
        self.assertEqual((ok, reason), (False, "10.0 h > cap 4 h"))
        self.assertEqual(nm.slice_limits(_3mf(""), 1, 1), (True, "ok", None, None))

    def test_run_part_failure_meta(self):
        with patch.object(nm.subprocess, "run", side_effect=subprocess.TimeoutExpired("py", 180)):
            meta = nm.run_part("result = 1", "stem")
        self.assertFalse(meta["ok"])
        self.assertIn("TimeoutExpired", meta["error"])
        self.assertTrue(meta["stl"].endswith("stem.stl"))


class TestIntegration(unittest.TestCase):
    def test_print_uses_bambu_watch_with_no_ams(self):
        fake, calls = _watch_status("P2: idle")
        with patch.object(nm, "_watch", fake):
            self.assertTrue(nm.print_part("x.3mf", "P2"))
        self.assertEqual(calls, [["status", "P2"], ["print", "P2", "x.3mf", "--no-ams"]])
        self.assertTrue(nm.WATCH.endswith("nova_bambu_watch.py"))

    def test_gen_local_posts_to_ollama(self):
        resp = MagicMock(); resp.read.return_value = json.dumps({"response": "```python\nresult=1\n```"}).encode()
        with patch.object(nm.urllib.request, "urlopen", return_value=resp) as u:
            self.assertEqual(nm.gen_local("idea"), "result=1")
        req = u.call_args.args[0]
        self.assertEqual(req.full_url, nm.OLLAMA_URL)
        self.assertEqual(json.loads(req.data)["system"], nm.SYSTEM_PROMPT)


class TestFunctional(unittest.TestCase):
    def _main(self, argv, **patches):
        with patch.object(sys, "argv", ["nova_make.py", *argv]), redirect_stdout(io.StringIO()) as out, \
             patch.multiple(nm, **patches):
            try:
                nm.main(); code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_dry_run_never_slices_or_prints(self):
        sl, pr = MagicMock(), MagicMock()
        code, out = self._main(["a coaster", "--dry-run"], generate_part=MagicMock(return_value=dict(GOOD)),
                               slice_part=sl, print_part=pr)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["volume_cm3"], 12.0)
        sl.assert_not_called(); pr.assert_not_called()

    def test_golden_path_and_cap_abort(self):
        pr = MagicMock(return_value=True)
        code, _ = self._main(["a coaster"], generate_part=MagicMock(return_value=dict(GOOD)),
                             slice_part=MagicMock(return_value="x.3mf"),
                             slice_limits=MagicMock(return_value=(True, "ok", 10.0, 1.0)), print_part=pr)
        self.assertEqual(code, 0)
        pr.assert_called_once_with("x.3mf", "P1")
        pr = MagicMock()
        code, _ = self._main(["big"], generate_part=MagicMock(return_value=dict(GOOD)),
                             slice_part=MagicMock(return_value="x.3mf"),
                             slice_limits=MagicMock(return_value=(False, "too heavy", 99.0, 1.0)), print_part=pr)
        self.assertEqual(code, 3)
        pr.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest OK", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)


if __name__ == "__main__":
    unittest.main()
