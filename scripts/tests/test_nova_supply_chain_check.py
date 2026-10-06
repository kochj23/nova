#!/usr/bin/env python3
"""Tests for nova_supply_chain_check.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_supply_chain_check.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SC = _load("supply_chain_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="supply-chain-test-"))
SC.LOG_FILE = TMP / "supply_chain_check.log"          # never touch ~/.openclaw/logs


def _cp(rc=0, stdout=""):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr="")


def _project(name, pkg=None, req=None):
    d = TMP / name
    d.mkdir(parents=True, exist_ok=True)
    if pkg is not None:
        (d / "package.json").write_text(pkg if isinstance(pkg, str) else json.dumps(pkg))
    if req is not None:
        (d / "requirements.txt").write_text(req)
    return d


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_known_attack_markers_are_covered(self):
        flat = [p for pats in SC.MALICIOUS_PATTERNS.values() for p in pats]
        for marker in ("plain-crypto-js", "ComfyUI_LLMVISION", "discord_webhook", "chrome_cookie", "pastebin"):
            self.assertIn(marker, flat)

    def test_no_shell_invocation_and_notify_lives_in_scripts_dir(self):
        self.assertNotIn("shell=True", SRC)
        self.assertNotIn("os.system", SRC)
        self.assertIn('SCRIPTS / "nova_slack_notify.py"', SRC)

    def test_log_never_leaks_the_scanned_file_contents(self):
        d = _project("sec", req="AKIAFAKEFAKEFAKEFAKE==secret-ish-line-SillyTavern\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            SC.scan_directory(d)
        self.assertEqual(buf.getvalue(), "")          # scan_directory reports, it does not print


class TestPerformance(unittest.TestCase):
    def test_10k_line_requirements_scanned_fast(self):
        lines = [f"package{i}=={i}.0" for i in range(10_000)]
        lines[5000] = "SillyTavern==1.0"
        d = _project("perf", req="\n".join(lines))
        t0 = time.perf_counter()
        r = SC.scan_directory(d)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(r["issues"]), 1)


class TestRetry(unittest.TestCase):
    def test_installed_scan_fails_open_on_tool_errors(self):
        # RETRY GAP: scan_installed_packages — npm/pip are tried once; any error becomes a warning, no exception
        with mock.patch.object(SC.subprocess, "run", side_effect=FileNotFoundError("npm")) as sp:
            r = SC.scan_installed_packages()
        self.assertEqual(sp.call_count, 2)
        self.assertEqual(r["npm"]["issues"], [])
        self.assertTrue(r["npm"]["warnings"][0].startswith("npm scan error"))
        self.assertTrue(r["pip"]["warnings"][0].startswith("pip scan error"))

    def test_slack_post_failure_never_hides_the_finding(self):
        # RETRY GAP: main()/nova_slack_notify — single attempt; a failed post still returns 1 (issues found)
        d = _project("home_a"); _project("home_a/code", pkg={"dependencies": {"plain-crypto-js": "1"}})
        calls = []
        def _run(cmd, **kw):
            calls.append(cmd)
            if "nova_slack_notify.py" in str(cmd[1] if len(cmd) > 1 else ""):
                raise subprocess.TimeoutExpired(cmd, 10)
            return _cp(1)
        with mock.patch.object(SC.Path, "home", return_value=d), mock.patch.object(SC.subprocess, "run", side_effect=_run), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(SC.main(), 1)
        self.assertTrue(any("nova_slack_notify.py" in str(c) for c in calls))


class TestUnit(unittest.TestCase):
    def test_package_json_dependency_and_postinstall(self):
        d = _project("u1", pkg={"dependencies": {"plain-crypto-js": "^1.0", "lodash": "4"},
                                "devDependencies": {"fine": "1"},
                                "scripts": {"postinstall": "node -e \"eval(Buffer.from('x'))\"", "test": "eval(x)"}})
        r = SC.scan_directory(d)
        self.assertEqual(len(r["issues"]), 2)
        self.assertTrue(any("plain-crypto-js" in i and "nullbulge" in i for i in r["issues"]))
        self.assertTrue(any("postinstall" in i for i in r["issues"]))
        self.assertEqual(r["warnings"], [])

    def test_invalid_package_json_is_a_warning(self):
        d = _project("u2", pkg="{not json")
        r = SC.scan_directory(d)
        self.assertEqual(r["issues"], [])
        self.assertTrue(r["warnings"][0].startswith("Error reading package.json"))

    def test_requirements_rules(self):
        d = _project("u3", req="# comment\n\nrequests==2.0\nSillyTavern\ngit+https://evil.example/x\n"
                               "git+https://github.com/org/repo\n")
        r = SC.scan_directory(d)
        self.assertEqual(len(r["issues"]), 1)
        self.assertIn("SillyTavern", r["issues"][0])
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("evil.example", r["warnings"][0])

    def test_empty_dir_is_clean(self):
        r = SC.scan_directory(_project("u4"))
        self.assertEqual((r["issues"], r["warnings"]), ([], []))
        self.assertEqual(r["path"], str(TMP / "u4"))

    def test_log_writes_to_redirected_file(self):
        with redirect_stdout(io.StringIO()):
            SC.log("hello")
        self.assertIn("hello", SC.LOG_FILE.read_text())


class TestIntegration(unittest.TestCase):
    def test_installed_scan_parses_npm_and_pip_json(self):
        def _run(cmd, **kw):
            if cmd[0] == "npm":
                return _cp(0, json.dumps({"dependencies": {"AppleBotzz": {}, "typescript": {}}}))
            return _cp(0, json.dumps([{"name": "requests"}, {"name": "Fadmino"}]))
        with mock.patch.object(SC.subprocess, "run", side_effect=_run) as sp:
            r = SC.scan_installed_packages()
        self.assertEqual(r["npm"]["issues"], ["Suspicious global npm: AppleBotzz"])
        self.assertEqual(r["pip"]["issues"], ["Suspicious pip package: Fadmino"])
        self.assertEqual([c[0][0][0] for c in sp.call_args_list], ["npm", "python3"])
        self.assertTrue(all(c.kwargs.get("timeout") for c in sp.call_args_list))

    def test_same_pattern_table_drives_files_and_installed_scans(self):
        self.assertEqual(SRC.count("MALICIOUS_PATTERNS.items()"), 4)   # one table, four consumers


class TestFunctional(unittest.TestCase):
    def test_main_reports_issue_and_posts_to_slack(self):
        home = _project("home_b")
        _project("home_b/projects", pkg={"dependencies": {"ComfyUI_LLMVISION": "1"}})
        calls = []
        with mock.patch.object(SC.Path, "home", return_value=home), \
             mock.patch.object(SC.subprocess, "run", side_effect=lambda cmd, **kw: (calls.append(cmd), _cp(1))[1]), \
             redirect_stdout(io.StringIO()):
            rc = SC.main()
        self.assertEqual(rc, 1)
        slack = [c for c in calls if "nova_slack_notify.py" in str(c[1])]
        self.assertEqual(len(slack), 1)
        self.assertEqual(slack[0][0], sys.executable)
        self.assertIn("Supply Chain Alert", slack[0][2])
        self.assertIn("ComfyUI_LLMVISION", slack[0][2])
        self.assertIn("FOUND 1 ISSUES", SC.LOG_FILE.read_text())

    def test_main_clean_returns_zero_without_posting(self):
        home = _project("home_c"); _project("home_c/code", req="requests==2.0\n")
        calls = []
        with mock.patch.object(SC.Path, "home", return_value=home), \
             mock.patch.object(SC.subprocess, "run", side_effect=lambda cmd, **kw: (calls.append(cmd), _cp(1))[1]), \
             redirect_stdout(io.StringIO()):
            rc = SC.main()
        self.assertEqual(rc, 0)
        self.assertFalse(any("nova_slack_notify.py" in str(c) for c in calls))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_supply_chain_check"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
