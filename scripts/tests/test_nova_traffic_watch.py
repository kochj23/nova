#!/usr/bin/env python3
"""Tests for nova_traffic_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import types
import unittest
import urllib.request  # noqa: F401
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_traffic_watch.py"
SRC = SCRIPT.read_text()

import traffic_cams  # noqa: E402  (pure data module)

VA = types.ModuleType("nova_vision_analyzer")
VA.describe_image = MagicMock(name="describe_image", return_value="")
VA.remember = MagicMock(name="remember")
VA.slack_post = MagicMock(name="slack_post")
VA.log = MagicMock(name="log")


def _load():
    spec = importlib.util.spec_from_file_location("ntw", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_vision_analyzer": VA}), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


tw = _load()

CAMS = {"c1": {"name": "SR-134 Pass", "url": "https://cams.example/c1.jpg", "area": "Glendale",
               "lat": 34.1, "lon": -118.2, "role": "fire"},
        "c2": {"name": "I-5 Alameda", "url": "https://cams.example/c2.jpg", "area": "Burbank",
               "lat": 34.2, "lon": -118.3, "role": "commute"},
        "c3": {"name": "I-210 Lake", "url": "https://cams.example/c3.jpg", "area": "Pasadena",
               "lat": 34.3, "lon": -118.1, "role": "commute"}}


def _resp(data):
    r = MagicMock(); r.read.return_value = data
    r.__enter__ = lambda s: s; r.__exit__ = lambda s, *a: False
    return r


def _reset():
    for m in (VA.describe_image, VA.remember, VA.slack_post, VA.log):
        m.reset_mock(); m.side_effect = None


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_captioning_stays_local(self):
        self.assertIn('"http://127.0.0.1:11434/api/generate"', SRC)
        self.assertNotRegex(SRC, r"openrouter\.ai|api\.openai|anthropic\.com")

    def test_model_cannot_inject_unknown_hazard(self):
        self.assertIsNone(tw.parse_hazard("ok\nHAZARD: rm")[1])
        self.assertIsNone(tw.parse_hazard("HAZARD: <script>")[1])
        self.assertEqual(tw.parse_hazard("hazard: FIRE.")[1], "fire")


class TestPerformance(unittest.TestCase):
    def test_parse_10k_captions_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            tw.parse_hazard(f"Traffic flowing on lane {i}, no smoke.\nHAZARD: {'none' if i % 2 else 'crash'}")
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_snapshot_fetch_is_one_shot_and_fails_open(self):
        # RETRY GAP: fetch_snapshot() — one GET per camera per pass, no retry; failure returns None (camera "offline")
        op = MagicMock(side_effect=OSError("timeout"))
        with patch.object(tw.urllib.request, "urlopen", op):
            self.assertIsNone(tw.fetch_snapshot("https://cams.example/x.jpg"))
        self.assertEqual(op.call_count, 1)

    def test_warmup_failure_does_not_stop_the_pass(self):
        _reset()
        with patch.object(tw, "TRAFFIC_CAMERAS", {"c2": CAMS["c2"]}), \
             patch.object(tw.urllib.request, "urlopen", side_effect=OSError("ollama down")), \
             patch.object(tw, "fetch_snapshot", return_value=None):
            self.assertEqual(tw.watch(), (0, 0))
        self.assertTrue(any("warmup skipped" in c[0][0] for c in VA.log.call_args_list))


class TestUnit(unittest.TestCase):
    def test_selftest_offline(self):
        tmp = Path(__file__).with_name("__traffic_selftest.jpg")

        def fake_fetch(url):
            tmp.write_bytes(b"x" * 2000)
            return str(tmp)
        with patch.object(tw, "fetch_snapshot", side_effect=fake_fetch), patch("builtins.print"):
            tw.selftest()
        self.assertFalse(tmp.exists())

    def test_parse_hazard_edges(self):
        self.assertEqual(tw.parse_hazard(""), ("", None))
        self.assertEqual(tw.parse_hazard("Clear and moving. HAZARD: none"), ("Clear and moving.", None))
        self.assertEqual(tw.parse_hazard("No tag at all"), ("No tag at all", None))

    def test_fetch_snapshot_rejects_tiny_and_cache_busts(self):
        with patch.object(tw.urllib.request, "urlopen", return_value=_resp(b"x" * 10)) as op:
            self.assertIsNone(tw.fetch_snapshot("https://cams.example/x.jpg"))
        self.assertRegex(op.call_args[0][0].full_url, r"x\.jpg\?_=\d+$")
        with patch.object(tw.urllib.request, "urlopen", return_value=_resp(b"x" * 5000)):
            p = tw.fetch_snapshot("https://cams.example/x.jpg")
        self.assertEqual(Path(p).stat().st_size, 5000)
        Path(p).unlink()


class TestIntegration(unittest.TestCase):
    def test_reuses_vision_plumbing_and_camera_table(self):
        self.assertIn("from nova_vision_analyzer import describe_image, remember, slack_post, log", SRC)
        self.assertIs(tw.TRAFFIC_CAMERAS, traffic_cams.TRAFFIC_CAMERAS)
        self.assertTrue(all({"name", "url", "area", "role", "lat", "lon"} <= v.keys()
                            for v in traffic_cams.TRAFFIC_CAMERAS.values()))

    def test_role_filter_and_limit(self):
        _reset()
        with patch.object(tw, "TRAFFIC_CAMERAS", CAMS), patch.object(tw.urllib.request, "urlopen"), \
             patch.object(tw, "fetch_snapshot", return_value=None) as fs:
            tw.watch(role="commute", limit=1)
        self.assertEqual([c[0][0] for c in fs.call_args_list], [CAMS["c2"]["url"]])


class TestFunctional(unittest.TestCase):
    def test_pass_stores_digest_and_alerts_hazards(self):
        _reset()
        captions = {"c1": "Smoke rising over the ridge.\nHAZARD: smoke",
                    "c2": "Traffic is free-flowing.\nHAZARD: none",
                    "c3": "Temporarily Unavailable card.\nHAZARD: unavailable"}
        order = iter(["c1", "c2", "c3"])
        tmpdir = Path(__file__).parent

        def fetch(url):
            p = tmpdir / f"__cam_{next(order)}.jpg"; p.write_bytes(b"x"); return str(p)

        def describe(path, prompt, model=None):
            cid = Path(path).stem.split("_")[-1]
            self.assertIn("brush" if cid == "c1" else "Describe this freeway", prompt)
            return captions[cid]
        VA.describe_image.side_effect = describe
        with patch.object(tw, "TRAFFIC_CAMERAS", CAMS), patch.object(tw.urllib.request, "urlopen"), \
             patch.object(tw, "fetch_snapshot", side_effect=fetch):
            self.assertEqual(tw.watch(), (2, 1))
        self.assertEqual(list(tmpdir.glob("__cam_*.jpg")), [])           # snapshots cleaned up
        text = VA.remember.call_args[0][0]
        self.assertIn("[Glendale] SR-134 Pass: Smoke rising over the ridge.", text)
        self.assertNotIn("Unavailable", text)
        self.assertEqual(VA.remember.call_args.kwargs["source"], "traffic_cams")
        msg, kw = VA.slack_post.call_args[0][0], VA.slack_post.call_args.kwargs
        self.assertTrue(msg.startswith("🌫️ SMOKE — SR-134 Pass (Glendale)"))
        self.assertEqual((kw["level"], kw["dedup_key"]), ("critical", "trafficcam-c1-smoke"))

    def test_all_offline_stores_and_alerts_nothing(self):
        _reset()
        with patch.object(tw, "TRAFFIC_CAMERAS", CAMS), patch.object(tw.urllib.request, "urlopen"), \
             patch.object(tw, "fetch_snapshot", return_value=None):
            self.assertEqual(tw.watch(), (0, 0))
        VA.remember.assert_not_called(); VA.slack_post.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        # --selftest does a live snapshot fetch, so the frame smoke is --help
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--role", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_traffic_watch"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Foothill Watch pass", r.stdout)


if __name__ == "__main__":
    unittest.main()
