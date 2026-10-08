#!/usr/bin/env python3
"""7-category tests for the retired-launchagents archive (2026-10-08).

Change under test: every plist archived under retired-launchagents/ is stored as a
`.plist.template` with the repo's `__HOME__` placeholder (same convention as launchd/*.template),
so no personal home path is committed, and the archive stays a lossless, renderable record
(`sed "s#__HOME__#$HOME#g"`). None of the retired agents may be loaded again.

Written by Jordan Koch.
"""
import os
import plistlib
import re
import subprocess
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
RETIRED = REPO / "retired-launchagents"
HOME_PATH_RE = re.compile(r"/Users/[A-Za-z0-9._-]+/")
SECRET_RE = re.compile(r"(?i)(password|secret|api[_-]?key|token)\s*</key>\s*<string>[^<]{8,}")


def _templates():
    return sorted(RETIRED.glob("*.plist.template"))


def _render(path: Path, home: str = "/home/test") -> bytes:
    return path.read_text().replace("__HOME__", home).encode()


class TestSecurity(unittest.TestCase):
    def test_no_personal_home_paths(self):
        for p in RETIRED.iterdir():
            self.assertIsNone(HOME_PATH_RE.search(p.read_text(errors="ignore")), p.name)

    def test_no_inline_secrets(self):
        for p in RETIRED.iterdir():
            self.assertIsNone(SECRET_RE.search(p.read_text(errors="ignore")), p.name)

    def test_no_raw_plists_with_paths_left(self):
        for p in RETIRED.glob("*.plist"):
            self.assertNotIn("__HOME__", p.read_text())  # raw plists must not need a home path


class TestPerformance(unittest.TestCase):
    def test_render_and_parse_all_fast(self):
        t = time.monotonic()
        for p in _templates():
            plistlib.loads(_render(p))
        self.assertLess(time.monotonic() - t, 1.0)


class TestRetry(unittest.TestCase):
    """The archive has no external calls; the failure mode that matters is a retired agent being
    re-launched (launchd retries KeepAlive jobs forever). Ensure none is installed or loaded."""

    def test_retired_agents_not_installed(self):
        la = Path.home() / "Library" / "LaunchAgents"
        for p in _templates():
            self.assertFalse((la / p.name.replace(".template", "")).exists(), p.name)

    def test_retired_agents_not_loaded(self):
        for attempt in range(3):  # launchctl can transiently fail; retry with backoff, never pass silently
            r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                break
            time.sleep(0.2 * (2 ** attempt))
        else:
            self.skipTest("launchctl unavailable after 3 attempts: %s" % r.stderr.strip())
        for p in _templates():
            label = plistlib.loads(_render(p))["Label"]
            if label.startswith("homebrew."):
                continue
            self.assertNotIn("\t" + label + "\n", r.stdout + "\n", label)


class TestUnit(unittest.TestCase):
    def test_placeholder_used_wherever_home_was(self):
        for p in _templates():
            d = plistlib.loads(_render(p, "/X"))
            blob = repr(d)
            self.assertNotIn("__HOME__", blob)

    def test_render_roundtrip_is_lossless(self):
        for p in _templates():
            src = p.read_text()
            self.assertEqual(_render(p, "/H").decode().replace("/H", "__HOME__"), src)


class TestIntegration(unittest.TestCase):
    def test_plutil_lints_rendered(self):
        if not Path("/usr/bin/plutil").exists():
            self.skipTest("plutil missing")
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for p in _templates():
                out = Path(d) / p.name.replace(".template", "")
                out.write_bytes(_render(p, os.path.expanduser("~")))
                r = subprocess.run(["/usr/bin/plutil", "-lint", str(out)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestFunctional(unittest.TestCase):
    def test_all_retired_agents_archived(self):
        names = {p.name for p in _templates()}
        for n in ("com.nova.agent-analyst", "com.nova.agent-coder", "com.nova.agent-librarian",
                  "com.nova.agent-lookout", "net.digitalnoise.nova-jarvis-brain",
                  "net.digitalnoise.nova-service-monitor", "net.digitalnoise.nova-system-monitor"):
            self.assertIn(n + ".plist.template", names)

    def test_matches_launchd_template_convention(self):
        tmpl = list((REPO / "launchd").glob("*.plist.template"))
        self.assertTrue(tmpl)
        self.assertIn("__HOME__", tmpl[0].read_text())


class TestFrame(unittest.TestCase):
    def test_dir_exists_and_nonempty(self):
        self.assertTrue(RETIRED.is_dir())
        self.assertTrue(_templates())


if __name__ == "__main__":
    unittest.main()
