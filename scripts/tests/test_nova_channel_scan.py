#!/usr/bin/env python3
"""Tests for nova_channel_scan.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import contextlib
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
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_channel_scan.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="channel-scan-test-"))
_MISSING = object()


@contextlib.contextmanager
def _stub_modules(stubs):
    saved = {k: sys.modules.get(k, _MISSING) for k in stubs}
    sys.modules.update(stubs)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _load():
    spec = importlib.util.spec_from_file_location("channel_scan_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    cfg = types.ModuleType("nova_config"); cfg.post_both = MagicMock()
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    with _stub_modules({"nova_config": cfg, "nova_notify": nn}), patch.dict(os.environ, {"HOME": str(TMP)}):
        spec.loader.exec_module(mod)
    mod.LOG_FILE = str(TMP / "scan.log")               # never append to /tmp/nova-channel-scan.log
    mod.WORK_DIR = TMP / "work"
    mod.PREFS_FILE = TMP / "livetv_novas_prefs.json"
    return mod


cs = _load()
LINEUP = [{"GuideNumber": "4.1", "GuideName": "KNBC"}, {"GuideNumber": "11.2", "GuideName": "Cozi"}]


def _ffmpeg(good):
    """subprocess.run stub: writes a real wav-sized file for channels in `good`."""
    def run(argv, **kw):
        out = Path(argv[-1]); ch = argv[argv.index("-i") + 1].rsplit("/v", 1)[1]
        if ch in good:
            out.parent.mkdir(parents=True, exist_ok=True); out.write_bytes(b"\0" * (cs.MIN_BYTES + 1))
        return types.SimpleNamespace(returncode=0 if ch in good else 1)
    return MagicMock(side_effect=run)


def _run(lineup, good=("4.1",), ffmpeg=None):
    cs.notify = MagicMock(return_value=True)
    cs.PREFS_FILE.unlink(missing_ok=True)
    with patch.object(cs, "get_lineup", return_value=lineup), patch.object(cs.time, "sleep") as slp, \
         patch.object(subprocess, "run", ffmpeg or _ffmpeg(good)) as sp, redirect_stdout(io.StringIO()) as out:
        try:
            cs.main(); code = 0
        except SystemExit as e:
            code = e.code
    return code, sp, slp, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_shell(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)
        self.assertTrue(cs.HDHR_LINEUP.startswith("http://192.168.1."))       # LAN tuner only

    def test_ffmpeg_runs_as_argv_and_writes_only_under_work_dir(self):
        with patch.object(subprocess, "run", _ffmpeg(("7.1",))) as sp:
            ok, size = cs.test_channel("7.1")
        argv = sp.call_args[0][0]
        self.assertEqual(argv[0], cs.FFMPEG)
        self.assertEqual(argv[argv.index("-i") + 1], f"{cs.HDHR_STREAM}7.1")
        self.assertEqual(Path(argv[-1]).parent, cs.WORK_DIR)
        self.assertTrue(ok); self.assertGreater(size, cs.MIN_BYTES)
        self.assertFalse((cs.WORK_DIR / "scan_7_1.wav").exists())            # scratch file removed


class TestPerformance(unittest.TestCase):
    def test_sort_10k_channel_numbers(self):
        chans = [f"{i % 70}.{i % 9}" for i in range(10_000)] + ["bad", "x.y"]
        t0 = time.perf_counter()
        out = sorted(chans, key=cs.sort_key)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(out[0], "0.0"); self.assertEqual(out[-1], "x.y")


class TestRetry(unittest.TestCase):
    def test_lineup_fetch_fails_open_to_empty(self):
        # RETRY GAP: get_lineup — one urlopen; any error logs and returns [] so main() exits 1 cleanly
        with patch("urllib.request.urlopen", side_effect=OSError("tuner offline")) as u, redirect_stdout(io.StringIO()):
            self.assertEqual(cs.get_lineup(), [])
        self.assertEqual(u.call_count, 1)

    def test_ffmpeg_timeout_is_one_shot_and_cleans_up(self):
        # RETRY GAP: test_channel — a single ffmpeg attempt; TimeoutExpired -> (False, 0), no partial file left
        sp = MagicMock(side_effect=subprocess.TimeoutExpired("ffmpeg", 35))
        with patch.object(subprocess, "run", sp):
            self.assertEqual(cs.test_channel("4.1"), (False, 0))
        self.assertEqual(sp.call_count, 1)
        self.assertFalse((cs.WORK_DIR / "scan_4_1.wav").exists())


class TestUnit(unittest.TestCase):
    def test_sort_key(self):
        self.assertEqual(cs.sort_key("4.1"), (4, 1)); self.assertEqual(cs.sort_key("12"), (12, 0))
        self.assertEqual(cs.sort_key("abc"), (999, 0))

    def test_prefs_default_and_corrupt(self):
        cs.PREFS_FILE.unlink(missing_ok=True)
        self.assertEqual(cs.load_prefs(), {"viewed": [], "favorites": [], "history_count": 0, "bad_channels": {}})
        cs.PREFS_FILE.write_text("{not json")
        self.assertEqual(cs.load_prefs()["bad_channels"], {})
        cs.save_prefs({"whitelist": [1]})
        self.assertEqual(json.loads(cs.PREFS_FILE.read_text()), {"whitelist": [1]})

    def test_slack_splits_title_and_body(self):
        cs.notify = MagicMock()
        cs.slack(":satellite_antenna: *Scan done*\nline2\nline3")
        args, kw = cs.notify.call_args
        self.assertEqual(args[0], "*Scan done*"); self.assertEqual(kw["body"], "line2\nline3")
        cs.slack("only title")
        self.assertIsNone(cs.notify.call_args[1]["body"])

    def test_log_appends_to_the_redirected_file(self):
        with redirect_stdout(io.StringIO()):
            cs.log("hello")
        self.assertIn("] hello", Path(cs.LOG_FILE).read_text())


class TestIntegration(unittest.TestCase):
    def test_slack_goes_through_the_central_bus_as_tv_info(self):
        self.assertIn("from nova_notify import notify", SRC)
        cs.notify = MagicMock(); cs.slack("t")
        kw = cs.notify.call_args[1]
        self.assertEqual((kw["level"], kw["category"], kw["dedup_key"]), ("info", "tv", "channel-scan"))

    def test_bad_channels_are_forced_past_the_skip_threshold(self):
        code, sp, slp, out = _run(LINEUP, good=("4.1",))
        prefs = json.loads(cs.PREFS_FILE.read_text())
        self.assertEqual(prefs["whitelist"], [{"ch": "4.1", "name": "KNBC"}])
        bad = prefs["bad_channels"]["11.2"]
        self.assertGreaterEqual(bad["failures"], 2)
        self.assertEqual(bad["reason"], "no signal — channel scan")
        self.assertIn("whitelist_updated", prefs)


class TestFunctional(unittest.TestCase):
    def test_golden_path_scans_posts_and_saves(self):
        cs.PREFS_FILE.write_text(json.dumps({"viewed": [], "favorites": [], "history_count": 0,
                                             "bad_channels": {"4.1": {"ch": "4.1", "failures": 5}}}))
        with patch.object(cs, "get_lineup", return_value=LINEUP), patch.object(cs.time, "sleep") as slp, \
             patch.object(subprocess, "run", _ffmpeg(("4.1",))) as sp, redirect_stdout(io.StringIO()) as out:
            cs.notify = MagicMock(); cs.main()
        self.assertEqual(sp.call_count, 2); self.assertEqual(slp.call_count, 2)
        titles = [c[0][0] for c in cs.notify.call_args_list]
        self.assertTrue(titles[0].startswith("*Nova Channel Scan Starting*"))
        self.assertEqual(titles[1], "*Nova Channel Scan Complete*")
        body = cs.notify.call_args_list[1][1]["body"]
        self.assertIn("*1 working channels*", body); self.assertIn("ch 11.2 — Cozi", body)
        prefs = json.loads(cs.PREFS_FILE.read_text())
        self.assertNotIn("4.1", prefs["bad_channels"])                       # recovered channel un-flagged
        self.assertIn("Scan complete: 1 good, 1 bad", out.getvalue())

    def test_no_lineup_exits_one_without_posting(self):
        code, sp, slp, out = _run([])
        self.assertEqual(code, 1); sp.assert_not_called(); cs.notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        boot = ("import sys, unittest.mock as um, subprocess, urllib.request, runpy; "
                "sys.modules['nova_config'] = um.MagicMock(); sys.modules['nova_notify'] = um.MagicMock(); "
                "subprocess.run = um.MagicMock(side_effect=AssertionError('ffmpeg at import')); "
                "urllib.request.urlopen = um.MagicMock(side_effect=AssertionError('net at import')); "
                "runpy.run_path(sys.argv[1], run_name='imported'); print('IMPORT_OK')")
        r = subprocess.run([sys.executable, "-c", boot, str(SCRIPT)], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "IMPORT_OK")


if __name__ == "__main__":
    unittest.main()
