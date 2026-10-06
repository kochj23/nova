#!/usr/bin/env python3
"""Tests for nova_software_inventory.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Every brew/npm/pip/which call is mocked; output goes to a tempdir.
Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_software_inventory.py").read_text()
_TMP = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("swinv", SCRIPTS / "nova_software_inventory.py")
    mod = importlib.util.module_from_spec(spec)
    with patch("pathlib.Path.mkdir"):            # import-time mkdir never touches the real workspace
        spec.loader.exec_module(mod)
    return mod


si = _load()
si.INVENTORY_DIR = Path(_TMP.name)
si.INVENTORY_FILE = Path(_TMP.name) / f"inventory-{si.TODAY}.json"
si.LATEST_FILE = Path(_TMP.name) / "inventory-latest.json"
si.log = lambda *a, **k: None

OUT = {
    ("brew", "list", "--versions"): "git 2.44.0\nwget 1.24\n",
    ("brew", "list", "--cask", "--versions"): "iterm2 3.5\n",
    ("brew", "tap"): "homebrew/core\n",
    ("npm", "list", "-g", "--depth=0", "--json"): json.dumps({"dependencies": {"pnpm": {"version": "9.0"}}}),
    ("python3", "-m", "pip", "list", "--format=json"): json.dumps([{"name": "requests", "version": "2.32"}]),
    ("sw_vers",): "ProductName: macOS\nProductVersion: 27.0\nBuildVersion: 27A1\n",
    ("uname", "-m"): "arm64", ("uname", "-r"): "27.0.0",
    ("which", "git"): "/usr/bin/git", ("git", "--version"): "git version 2.44.0",
}


def _fake_run(cmd, **k):
    out = OUT.get(tuple(cmd))
    return SimpleNamespace(returncode=0 if out is not None else 1, stdout=out or "", stderr="")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_commands_are_argv_lists(self):
        self.assertNotIn("shell=True", SRC)
        with patch.object(si.subprocess, "run", side_effect=_fake_run) as r:
            si.get_homebrew_packages()
        self.assertTrue(all(isinstance(c[0][0], list) for c in r.call_args_list))

    def test_tool_version_truncated(self):
        OUT[("which", "git")] = "/usr/bin/git"
        with patch.dict(OUT, {("git", "--version"): "v" * 500}), patch.object(si.subprocess, "run", side_effect=_fake_run):
            tools = si.get_cli_tools()
        self.assertEqual(len(tools[0]["version"]), 100)


class TestPerformance(unittest.TestCase):
    def test_parse_10k_formulae_fast(self):
        big = "\n".join(f"pkg{i} 1.{i}" for i in range(10_000))
        with patch.dict(OUT, {("brew", "list", "--versions"): big}), patch.object(si.subprocess, "run", side_effect=_fake_run):
            t0 = time.perf_counter()
            res = si.get_homebrew_packages()
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(res["formulae"]), 10_000)


class TestRetry(unittest.TestCase):
    def test_run_command_fails_open(self):
        # RETRY GAP: run_command — one attempt per tool; timeout/missing binary returns "" and the scan goes on
        calls = []

        def boom(cmd, **k):
            calls.append(cmd); raise subprocess.TimeoutExpired(cmd, 30)

        with patch.object(si.subprocess, "run", side_effect=boom):
            self.assertEqual(si.run_command(["brew", "list"]), "")
            self.assertEqual(si.get_npm_packages(), [])
        self.assertEqual(len(calls), 2)


class TestUnit(unittest.TestCase):
    def test_parsers_on_bad_output(self):
        bad = {("npm", "list", "-g", "--depth=0", "--json"): "{not json",
               ("python3", "-m", "pip", "list", "--format=json"): "nope",
               ("brew", "list", "--versions"): "lonely\n\n"}
        with patch.dict(OUT, bad), patch.object(si.subprocess, "run", side_effect=_fake_run):
            self.assertEqual(si.get_npm_packages(), [])
            self.assertEqual(si.get_python_packages(), [])
            self.assertEqual(si.get_homebrew_packages()["formulae"], [])

    def test_system_info(self):
        with patch.object(si.subprocess, "run", side_effect=_fake_run):
            self.assertEqual(si.get_system_info(),
                             {"os_version": "27.0", "build_version": "27A1", "architecture": "arm64", "kernel": "27.0.0"})


class TestIntegration(unittest.TestCase):
    def test_collectors_compose_into_inventory_shape(self):
        with patch.object(si.subprocess, "run", side_effect=_fake_run):
            hb, npm, py = si.get_homebrew_packages(), si.get_npm_packages(), si.get_python_packages()
        self.assertEqual(hb["formulae"][0], {"name": "git", "version": "2.44.0"})
        self.assertEqual(hb["casks"], [{"name": "iterm2", "version": "3.5"}])
        self.assertEqual(npm, [{"name": "pnpm", "version": "9.0"}])
        self.assertEqual(py, [{"name": "requests", "version": "2.32"}])


class TestFunctional(unittest.TestCase):
    def test_main_writes_inventory_latest_and_report(self):
        with patch.object(si.subprocess, "run", side_effect=_fake_run), \
             patch.object(si, "get_applications", return_value=[{"name": "Xcode", "path": "/A/Xcode.app", "version": "17"}]):
            self.assertEqual(si.main(), 0)
        inv = json.loads(si.INVENTORY_FILE.read_text())
        self.assertEqual(inv, json.loads(si.LATEST_FILE.read_text()))
        self.assertEqual(inv["cli_tools"][0]["name"], "git")
        report = (si.INVENTORY_DIR / f"report-{si.TODAY}.txt").read_text()
        self.assertIn("System: macOS 27.0 (arm64)", report)
        self.assertIn("• Xcode (17)", report)

    def test_main_with_nothing_installed(self):
        with patch.object(si.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="")), \
             patch.object(si, "get_applications", return_value=[]):
            self.assertEqual(si.main(), 0)
        self.assertIn("Homebrew Packages: 0", (si.INVENTORY_DIR / f"report-{si.TODAY}.txt").read_text())


class TestFrame(unittest.TestCase):
    def test_full_run_in_isolated_home_exits_zero(self):
        # empty PATH -> every tool lookup fails open; HOME is a tempdir so nothing real is written
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / ".openclaw/workspace").mkdir(parents=True)
            r = subprocess.run([sys.executable, str(SCRIPTS / "nova_software_inventory.py")],
                               capture_output=True, text=True, timeout=60,
                               env={"HOME": home, "PATH": "", "NOVA_TEST_QUIET": "1"})
            written = list((Path(home) / ".openclaw/workspace/software-inventory").glob("inventory-*.json"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(written), 2)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / ".openclaw/workspace").mkdir(parents=True)
            r = subprocess.run([sys.executable, "-c", "import nova_software_inventory"], cwd=str(SCRIPTS),
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "HOME": home, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
