#!/usr/bin/env python3
"""Tests for nova_mac_media_folder_guard.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The guard runs sudo plutil / pkill / killall: every command goes through a recording fake `run`,
and the refusal paths (mount down, --check, bookmark failure) are proven to write nothing."""
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


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mg = _load("nova_mac_media_folder_guard_t", SCRIPTS / "nova_mac_media_folder_guard.py")
_REAL_NOTIFY = mg.notify
_REAL_RUN = mg.run
_TMP = Path(tempfile.mkdtemp())
mg.SWIFT_SRC = str(_TMP / "mk_bookmark.swift")
mg.APPS = {"Music": str(_TMP / "com.apple.Music.plist"), "TV": str(_TMP / "com.apple.TV.plist")}
mg.notify = MagicMock()
mg.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=RuntimeError("unmocked subprocess")),
                                      TimeoutExpired=subprocess.TimeoutExpired,
                                      CompletedProcess=subprocess.CompletedProcess)
SRC = (SCRIPTS / "nova_mac_media_folder_guard.py").read_text()
B64 = "A" * 200


class _Box:
    """Fake command runner: answers by argv shape, records every call."""
    def __init__(self, readable=True, urls=None, running=(), bookmark=B64):
        self.readable = readable; self.urls = urls or {}; self.running = set(running)
        self.bookmark = bookmark; self.calls = []

    def __call__(self, cmd, timeout=60):
        self.calls.append(cmd)
        ok = lambda out="": subprocess.CompletedProcess(cmd, 0, out, "")
        if cmd[:3] == ["sudo", "-n", "/bin/ls"]:
            return ok() if self.readable else subprocess.CompletedProcess(cmd, 1, "", "")
        if "-extract" in cmd:
            return ok(self.urls.get(cmd[-1], "file:///Users/x/Music/Media.localized/"))
        if cmd[0] == "pgrep":
            return ok() if cmd[-1] in self.running else subprocess.CompletedProcess(cmd, 1, "", "")
        if cmd[0] == "/usr/bin/swift":
            return ok(self.bookmark + "\n") if self.bookmark else subprocess.CompletedProcess(cmd, 1, "", "boom")
        return ok()

    def writes(self):
        return [c for c in self.calls if ("-replace" in c or c[0] in ("pkill", "open") or "killall" in c
                                          or "chown" in c or c[2:3] == ["cp"])]


def _main(box, *argv):
    with patch.object(mg, "run", box), patch.object(sys, "argv", ["g.py", *argv]), \
            patch.object(mg.time, "sleep"), redirect_stdout(io.StringIO()):
        return mg.main()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sudo_is_non_interactive_only(self):
        for m in re.finditer(r'"sudo",\s*"([^"]+)"', SRC):
            self.assertEqual(m.group(1), "-n")
        self.assertNotIn("shell=True", SRC)

    def test_mount_down_refuses_to_touch_anything(self):
        box = _Box(readable=False)
        self.assertEqual(_main(box), 0)
        self.assertEqual(len(box.calls), 1)
        self.assertEqual(box.writes(), [])

    def test_check_mode_reports_drift_without_writing(self):
        mg.notify.reset_mock()
        box = _Box()
        self.assertEqual(_main(box, "--check"), 1)
        self.assertEqual(box.writes(), [])
        mg.notify.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_drift_scan_10k_fast(self):
        box = _Box(urls={p: mg.MEDIA_URL for p in mg.APPS.values()})
        t0 = time.perf_counter()
        with patch.object(mg, "run", box):
            for _ in range(5_000):
                self.assertEqual(mg.current_url(mg.APPS["TV"]), mg.MEDIA_URL)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_timeout_becomes_rc_124(self):
        # RETRY GAP: run() — one attempt per command; the 120 s LaunchAgent cadence is the retry
        with patch.object(mg.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)) as r:
            cp = _REAL_RUN(["true"])
        self.assertEqual((cp.returncode, cp.stderr), (124, "timeout"))
        self.assertEqual(r.call_count, 1)

    def test_bookmark_failure_aborts_before_writes(self):
        box = _Box(bookmark="")
        self.assertEqual(_main(box), 1)
        self.assertEqual(box.writes(), [])

    def test_notify_failure_swallowed(self):
        fake = types.SimpleNamespace(notify=MagicMock(side_effect=RuntimeError("bus down")))
        with patch.dict(sys.modules, {"nova_notify": fake}):
            self.assertIsNone(_REAL_NOTIFY("x"))
        fake.notify.assert_called_once()


class TestUnit(unittest.TestCase):
    def test_current_url_none_on_failure(self):
        with patch.object(mg, "run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertIsNone(mg.current_url("p"))

    def test_short_bookmark_rejected(self):
        with patch.object(mg, "run", _Box(bookmark="short")), redirect_stdout(io.StringIO()):
            self.assertIsNone(mg.make_bookmark())
        self.assertTrue(Path(mg.SWIFT_SRC).read_text().startswith("import Foundation"))

    def test_no_drift_is_a_noop(self):
        box = _Box(urls={p: mg.MEDIA_URL for p in mg.APPS.values()})
        self.assertEqual(_main(box), 0)
        self.assertEqual(box.writes(), [])


class TestIntegration(unittest.TestCase):
    def test_notify_goes_through_bus_with_dedup(self):
        fake = types.SimpleNamespace(notify=MagicMock())
        with patch.dict(sys.modules, {"nova_notify": fake}):
            _REAL_NOTIFY("hello")
        kw = fake.notify.call_args[1]
        self.assertEqual((kw["category"], kw["dedup_key"]), ("storage", "mac-media-folder-guard"))


class TestFunctional(unittest.TestCase):
    def test_repoint_golden_path(self):
        mg.notify.reset_mock()
        music = mg.APPS["Music"]
        box = _Box(urls={mg.APPS["TV"]: mg.MEDIA_URL}, running={"Music"})
        self.assertEqual(_main(box), 0)
        flat = [" ".join(c) for c in box.calls]
        self.assertIn(f"sudo -n cp -p {music} {music}.bak-media-guard", flat)        # backup first
        self.assertIn(f"sudo -n plutil -replace media-folder-url -string {mg.MEDIA_URL} {music}", flat)
        self.assertIn(f"sudo -n plutil -replace media-folder-bookmark -data {B64} {music}", flat)
        self.assertLess(flat.index("pkill -TERM -x Music"), flat.index("open -a Music"))
        self.assertFalse(any(mg.APPS["TV"] in f and "-replace" in f for f in flat))   # TV not drifted
        self.assertIn("re-pointed Music", mg.notify.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_mac_media_folder_guard"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
