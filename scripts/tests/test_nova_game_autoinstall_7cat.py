#!/usr/bin/env python3
"""7-category tests for nova_game_autoinstall.py (Jordan 2026-10-20: "Always install the latest versions of the game
on my desktop"). Builds fake .app bundles in a temp dir; pgrep and the copy are injected.
Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_game_autoinstall_7cat.py
"""
import os
import plistlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_game_autoinstall as ga  # noqa: E402


def mkapp(path: Path, build, version="1.0"):
    (path / "Contents").mkdir(parents=True, exist_ok=True)
    (path / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleVersion": str(build), "CFBundleShortVersionString": version}))
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root, self.apps = Path(self.td.name) / "bin", Path(self.td.name) / "Applications"
        self.apps.mkdir()


class TestSecurity(Base):
    def test_half_copied_or_garbage_export_is_ignored(self):
        bad = self.root / "x-GTNW-v9" / "GTNW.app" / "Contents"
        bad.mkdir(parents=True)
        (bad / "Info.plist").write_bytes(b"not a plist")
        self.assertEqual(ga.newest_export("GTNW", self.root), (None, ()))

    def test_build_from_another_team_or_unsigned_is_refused(self):
        mkapp(self.apps / "GTNW.app", 26)
        mkapp(self.root / "evil" / "GTNW.app", 99)
        inst = MagicMock()
        for other in ("OTHER", None):       # foreign team, then unsigned/tampered
            r = ga.check("GTNW", self.apps, self.root, _running=lambda g: False, _install=inst,
                         _team=lambda a, o=other: "MINE" if "Applications" in str(a) else o)
            self.assertTrue(r.startswith("REFUSED"))
        inst.assert_not_called()

    def test_real_codesign_rejects_an_unsigned_bundle(self):
        self.assertIsNone(ga.team_of(mkapp(self.root / "u" / "GTNW.app", 1)))


class TestPerformance(Base):
    def test_scanning_200_exports_is_fast(self):
        for i in range(200):
            mkapp(self.root / f"d{i}-GTNW-v{i}" / "GTNW.app", i)
        t = time.time()
        app, key = ga.newest_export("GTNW", self.root)
        self.assertLess(time.time() - t, 2.0)
        self.assertEqual(key[0], (199,))


class TestRetry(Base):
    def test_running_game_is_never_touched_then_installs_after_quit(self):
        mkapp(self.apps / "GTNW.app", 26, "3.5.0")
        mkapp(self.root / "20261020-GTNW-v3.8.0" / "GTNW.app", 29, "3.8.0")
        inst = MagicMock()
        self.assertIn("running", ga.check("GTNW", self.apps, self.root, _running=lambda g: True, _install=inst, _team=lambda a: "T"))
        inst.assert_not_called()
        self.assertIn("installed", ga.check("GTNW", self.apps, self.root, _running=lambda g: False, _install=inst, _team=lambda a: "T"))
        inst.assert_called_once()


class TestUnit(Base):
    def test_build_ordering_is_numeric(self):
        self.assertGreater(ga.build_of(mkapp(self.root / "a" / "G.app", 10, "3.10.0")),
                           ga.build_of(mkapp(self.root / "b" / "G.app", 9, "3.9.0")))


class TestIntegration(Base):
    def test_install_swaps_through_the_symlink_target(self):
        real = mkapp(Path(self.td.name) / "Data" / "CarTest.app", 5, "1.2.1")
        os.symlink(real, self.apps / "CarTest.app")
        mkapp(self.root / "20261020-CarTest-v1.3.0" / "CarTest.app", 6, "1.3.0")
        ga.check("CarTest", self.apps, self.root, _running=lambda g: False, _team=lambda a: "T")
        self.assertTrue((self.apps / "CarTest.app").is_symlink())               # link kept
        self.assertEqual(ga.build_of(real)[0], (6,))                              # real bundle replaced


class TestFunctional(Base):
    def test_current_and_not_installed(self):
        mkapp(self.apps / "GTNW.app", 29)
        mkapp(self.root / "x" / "GTNW.app", 29)
        self.assertEqual(ga.check("GTNW", self.apps, self.root, _running=lambda g: False, _team=lambda a: "T"), "current")
        self.assertEqual(ga.check("Blompie", self.apps, self.root, _running=lambda g: False, _team=lambda a: "T"), "not installed")


class TestFrame(Base):
    def test_never_installs_an_older_build(self):
        mkapp(self.apps / "GTNW.app", 30)
        mkapp(self.root / "x" / "GTNW.app", 29)
        inst = MagicMock()
        self.assertEqual(ga.check("GTNW", self.apps, self.root, _running=lambda g: False, _install=inst, _team=lambda a: "T"), "current")
        inst.assert_not_called()


if __name__ == "__main__":
    unittest.main()
