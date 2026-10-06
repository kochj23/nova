#!/usr/bin/env python3
"""Tests for nova_app_suggestions.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


aps = _load("nova_app_suggestions_t", SCRIPTS / "nova_app_suggestions.py")
aps.notify = MagicMock()                      # stub the notification bus at load
_TMP = tempfile.mkdtemp()
aps.DATA_FILE = Path(_TMP) / "app_usage_log.json"
SRC = (SCRIPTS / "nova_app_suggestions.py").read_text()


def _ago(days):
    return (date.today() - timedelta(days=days)).isoformat()


class _Resp:
    def __init__(self, obj):
        self.obj = obj

    def read(self):
        return json.dumps(self.obj).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_app_probes_are_loopback_only(self):
        urls = re.findall(r'f"(http://[^"]+)"', SRC)
        self.assertTrue(urls)
        self.assertTrue(all(u.startswith("http://127.0.0.1:") for u in urls))


class TestPerformance(unittest.TestCase):
    def test_analyze_patterns_10k_snapshots(self):
        snaps = [{"date": _ago(i % 60), "hour": i % 24, "day": "Monday", "running": ["MLXCode", "TopGUI"]}
                 for i in range(10_000)]
        t0 = time.perf_counter()
        p = aps.analyze_patterns({"snapshots": snaps})
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(p["last_seen"]["MLXCode"], _ago(0))


class TestRetry(unittest.TestCase):
    def test_probes_are_one_shot_and_fail_open(self):
        # RETRY GAP: check_app/get_app_data/vector_remember — one urlopen each, safe defaults
        m = MagicMock(side_effect=OSError("refused"))
        with patch.object(aps.urllib.request, "urlopen", m):
            self.assertEqual(aps.check_app(1), (False, {}))
            self.assertIsNone(aps.get_app_data(1, "/x"))
            self.assertIsNone(aps.vector_remember("t"))
        self.assertEqual(m.call_count, 3)


class TestUnit(unittest.TestCase):
    def test_analyze_patterns_needs_a_week(self):
        self.assertEqual(aps.analyze_patterns({"snapshots": [{}] * 6}), {})
        self.assertEqual(aps.analyze_patterns({}), {})

    def test_get_app_data_without_endpoint(self):
        self.assertIsNone(aps.get_app_data(1, None))

    def test_load_data_corrupt_file_defaults(self):
        aps.DATA_FILE.write_text("{not json")
        self.assertEqual(aps.load_data(), {"snapshots": [], "suggestions_sent": {}})

    def test_save_data_prunes_old_snapshots(self):
        with patch.object(aps, "NOW", datetime.now()):
            aps.save_data({"snapshots": [{"date": _ago(90)}, {"date": _ago(1)}]})
        self.assertEqual(len(json.loads(aps.DATA_FILE.read_text())["snapshots"]), 1)

    def test_stale_and_pattern_suggestions(self):
        patterns = {"last_seen": {"MLXCode": _ago(10)},
                    "day_hour_usage": {"TopGUI": {"Monday": {"morning": 4}}}}
        with patch.object(aps, "DAY_NAME", "Monday"), patch.object(aps, "HOUR", 9):
            s = aps.generate_suggestions({}, [], patterns)
        kinds = {(x["app"], x["type"]) for x in s}
        self.assertIn(("MLXCode", "stale"), kinds)
        self.assertIn(("TopGUI", "pattern"), kinds)


class TestIntegration(unittest.TestCase):
    def test_vector_url_from_shared_config(self):
        import nova_config
        self.assertEqual(aps.VECTOR_URL, nova_config.VECTOR_URL)
        self.assertIn("from nova_notify import notify", SRC)

    def test_nmap_warnings_surface_as_high_priority(self):
        data = {"results": [{"severity": "critical"}, {"severity": "low"}]}
        with patch.object(aps, "get_app_data", return_value=data):
            s = aps.generate_suggestions({}, ["NMAPScanner"], {})
        hi = [x for x in s if x["app"] == "NMAPScanner"]
        self.assertEqual(hi[0]["priority"], "high")
        self.assertIn("1 security warning", hi[0]["message"])

    def test_slack_post_routes_through_notify(self):
        aps.notify.reset_mock()
        aps.slack_post("Head\nline1")
        args, kw = aps.notify.call_args
        self.assertEqual((args[0], kw["body"], kw["category"]), ("Head", "line1", "app_intel"))


class TestFunctional(unittest.TestCase):
    def setUp(self):
        aps.notify.reset_mock()
        if aps.DATA_FILE.exists():
            aps.DATA_FILE.unlink()

    def _run(self, urlopen):
        with patch.object(aps.urllib.request, "urlopen", urlopen), \
                patch.object(aps, "NOW", datetime.now()), patch.object(aps, "TODAY", date.today().isoformat()), \
                redirect_stdout(io.StringIO()):
            aps.main()

    def test_main_posts_suggestions_and_dedups(self):
        snaps = [{"date": _ago(20), "hour": 9, "day": "Monday", "running": ["NMAPScanner"]}] * 8
        aps.DATA_FILE.write_text(json.dumps({"snapshots": snaps, "suggestions_sent": {}}))
        self._run(MagicMock(side_effect=OSError("down")))
        self.assertEqual(aps.notify.call_count, 1)
        self.assertIn("NMAPScanner", aps.notify.call_args[1]["body"])
        saved = json.loads(aps.DATA_FILE.read_text())
        self.assertEqual(saved["snapshots"][-1]["running"], [])
        aps.notify.reset_mock()
        self._run(MagicMock(side_effect=OSError("down")))     # same day -> already sent
        self.assertEqual(aps.notify.call_count, 0)

    def test_main_with_no_history_posts_nothing(self):
        self._run(MagicMock(return_value=_Resp({"ok": True})))
        self.assertEqual(aps.notify.call_count, 0)
        self.assertEqual(len(json.loads(aps.DATA_FILE.read_text())["snapshots"]), 1)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_app_suggestions.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--snapshot", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_app_suggestions"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
