#!/usr/bin/env python3
"""Tests for nova_healthkit_export.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_healthkit_export.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="hk_test_"))


def _load():
    # import creates ~/.openclaw/private/health (0700): point HOME at a tempdir for the load
    with patch.dict(os.environ, {"HOME": str(TMP)}):
        spec = importlib.util.spec_from_file_location("hk_export", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


hk = _load()
GOOD = 'HEALTHKIT_JSON:{"hrv_sdnn_ms":42.5,"resting_heart_rate_bpm":58,"sleep_hours":7.2,"step_count":4000}'


def _main(stdout=GOOD, rc=0, side_effect=None):
    """Run main() with xcrun mocked; returns (exit code, printed text, run mock)."""
    run = MagicMock(return_value=MagicMock(returncode=rc, stdout=stdout, stderr="swift: error"), side_effect=side_effect)
    buf = io.StringIO()
    with patch.object(hk.subprocess, "run", run), redirect_stdout(buf), \
         patch.object(hk, "Path", side_effect=lambda *a: Path(TMP) if a == ("/tmp",) else Path(*a)):
        try:
            hk.main(); code = None
        except SystemExit as e:
            code = e.code
    return code, buf.getvalue(), run


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_health_dir_and_output_are_private(self):
        self.assertEqual(stat.S_IMODE(hk.HEALTH_DIR.stat().st_mode), 0o700)
        self.assertTrue(str(hk.OUTPUT_PATH).startswith(str(TMP)))
        if hk.OUTPUT_PATH.exists():
            hk.OUTPUT_PATH.unlink()
        _main()
        self.assertEqual(stat.S_IMODE(hk.OUTPUT_PATH.stat().st_mode), 0o600)

    def test_healthkit_is_read_only(self):
        self.assertIn("toShare: []", SRC)
        self.assertNotIn("shell=True", SRC)


class TestPerformance(unittest.TestCase):
    def test_parse_large_payload_fast(self):
        big = {f"metric_{i}": float(i) for i in range(10_000)}
        t0 = time.perf_counter()
        code, _, _ = _main(stdout="HEALTHKIT_JSON:" + json.dumps(big))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(hk.OUTPUT_PATH.read_text())), 10_001)


class TestRetry(unittest.TestCase):
    def test_swift_timeout_is_one_shot_and_exits_1(self):
        # RETRY GAP: main/xcrun swift — one attempt; a timeout exits 1 without writing
        if hk.OUTPUT_PATH.exists():
            hk.OUTPUT_PATH.unlink()
        code, out, run = _main(side_effect=subprocess.TimeoutExpired("xcrun", 60))
        self.assertEqual(code, 1)
        self.assertEqual(run.call_count, 1)
        self.assertIn("Error:", out)
        self.assertFalse(hk.OUTPUT_PATH.exists())


class TestUnit(unittest.TestCase):
    def test_swift_failure_exits_1(self):
        code, out, _ = _main(rc=1)
        self.assertEqual(code, 1)
        self.assertIn("Swift failed: swift: error", out)

    def test_unexpected_output_exits_1(self):
        code, out, _ = _main(stdout="garbage")
        self.assertEqual((code, out.strip()), (1, "Unexpected output"))

    def test_bad_json_exits_1(self):
        code, out, _ = _main(stdout="HEALTHKIT_JSON:{not json")
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("Error:"))


class TestIntegration(unittest.TestCase):
    def test_swift_script_queries_the_four_metrics_and_emits_prefix(self):
        for needle in ("sleepAnalysis", "heartRateVariabilitySDNN", "restingHeartRate", "stepCount", "HEALTHKIT_JSON:"):
            self.assertIn(needle, hk.SWIFT_SCRIPT)

    def test_runs_temp_script_with_xcrun_and_cleans_up(self):
        _, _, run = _main()
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["xcrun", "swift"])
        self.assertFalse(Path(argv[2]).exists())


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_json_with_timestamp(self):
        code, out, _ = _main()
        self.assertEqual(code, 0)
        data = json.loads(hk.OUTPUT_PATH.read_text())
        self.assertEqual((data["sleep_hours"], data["step_count"]), (7.2, 4000))
        self.assertIn("collected_at", data)
        self.assertIn("Health data written to", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        home = tempfile.mkdtemp(prefix="hk_frame_")
        code = (f"import importlib.util as u; s=u.spec_from_file_location('h', {str(SCRIPT)!r}); "
                "m=u.module_from_spec(s); s.loader.exec_module(m); print(m.OUTPUT_PATH.name)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "HOME": home, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "latest.json")
        self.assertFalse((Path(home) / ".openclaw/private/health/latest.json").exists())


if __name__ == "__main__":
    unittest.main()
