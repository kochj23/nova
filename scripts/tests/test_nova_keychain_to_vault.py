#!/usr/bin/env python3
"""Tests for nova_keychain_to_vault.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Never reads the real Keychain, 1Password or the fleet store: subprocess.run (security / op) is mocked in
every test, list_secrets / get_secret are mocked, and SCAN points at synthetic files in a tempdir."""
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
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_keychain_to_vault.py"
SRC = SCRIPT.read_text()
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kv = _load("nova_keychain_to_vault_t", SCRIPT)
DUMP = ('keychain: "/k/login.keychain-db"\nclass: "genp"\nattributes:\n    "acct"<blob>="nova"\n'
        '    "svce"<blob>="nova-from-dump"\n'
        'keychain: "/k/login.keychain-db"\nclass: "genp"\nattributes:\n    "acct"<blob>="someone"\n'
        '    "svce"<blob>="other-svc"\n')   # real attribute order: acct before svce


def _cp(rc=0, out=""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr="")


class FakeShell:
    """Answers security/op calls; records every argv."""
    def __init__(self, vault=("already",), values=None, list_rc=0, create_rc=0):
        self.vault, self.values, self.list_rc, self.create_rc = list(vault), values or {}, list_rc, create_rc
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[:2] == ["security", "dump-keychain"]:
            return _cp(0, DUMP)
        if argv[:2] == ["security", "find-generic-password"]:
            v = self.values.get(argv[argv.index("-s") + 1])
            return _cp(0, v + "\n") if v else _cp(44, "")
        if argv[:3] == ["op", "item", "list"]:
            return _cp(self.list_rc, json.dumps([{"title": t} for t in self.vault]) if not self.list_rc else "")
        if argv[:3] == ["op", "item", "create"]:
            return _cp(self.create_rc, "{}")
        raise AssertionError(f"unexpected argv {argv}")

    def creates(self):
        return [a for a in self.calls if a[:3] == ["op", "item", "create"]]


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        f = Path(self.td.name) / "x.py"
        f.write_text('subprocess.run(["security", "find-generic-password", "-a", "nova", "-s", "nova-from-code", "-w"])\n'
                     'sh = "security find-generic-password -s $DYNAMIC -w"\n')
        p = patch.object(kv, "SCAN", [str(f), str(Path(self.td.name) / "missing.py")])
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(kv.subprocess, "run", MagicMock(side_effect=AssertionError("unmocked subprocess")))
        p.start()
        self.addCleanup(p.stop)

    def run_main(self, shell, fleet=(), fleet_values=None):
        fleet_values = fleet_values or {}

        def get_secret(n):
            if n not in fleet_values:
                raise KeyError(n)
            return fleet_values[n]
        with patch.object(kv.subprocess, "run", shell), \
                patch.object(kv, "list_secrets", return_value=[(n,) for n in fleet]), \
                patch.object(kv, "get_secret", side_effect=get_secret), redirect_stdout(io.StringIO()) as out:
            kv.main()
        return out.getvalue()


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_never_prints_a_secret_value(self):
        sh = FakeShell(values={"nova-from-code": "S3CRET-VALUE-1"})
        out = self.run_main(sh, fleet=["fleet-only"], fleet_values={"fleet-only": "S3CRET-VALUE-2"})
        self.assertNotIn("S3CRET-VALUE", out)
        self.assertEqual(len(sh.creates()), 2)

    def test_subprocess_is_argv_never_shell(self):
        self.assertNotIn("shell=True", SRC)


class TestPerformance(_Base):
    def test_name_regex_over_large_source_fast(self):
        blob = "\n".join(f'["security", "find-generic-password", "-s", "svc-{i}", "-w"]' for i in range(10_000))
        t0 = time.perf_counter()
        names = set(kv.RX.findall(blob))
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(names), 10_000)


class TestRetry(_Base):
    def test_vault_list_failure_aborts_without_creating(self):
        # RETRY GAP: vault_titles/op item list — one attempt; on failure main() aborts (no duplicate creates)
        sh = FakeShell(list_rc=1, values={"nova-from-code": "v"})
        out = self.run_main(sh)
        self.assertIn("aborting", out)
        self.assertEqual(sh.creates(), [])

    def test_create_failure_is_reported_not_raised(self):
        # RETRY GAP: create/op item create — one attempt per item; failures are listed
        sh = FakeShell(values={"nova-from-code": "v"}, create_rc=1)
        out = self.run_main(sh)
        self.assertIn("FAILED to create: nova-from-code", out)
        self.assertEqual(len(sh.creates()), 1)


class TestUnit(_Base):
    def test_keychain_names_from_code_and_dump(self):
        with patch.object(kv.subprocess, "run", FakeShell()):
            names = kv.keychain_names()
        self.assertEqual(names, {"nova-from-code", "nova-from-dump"})   # $DYNAMIC dropped, non-nova acct dropped

    def test_keychain_value_missing_and_present(self):
        with patch.object(kv.subprocess, "run", FakeShell(values={"a": "va"})):
            self.assertEqual(kv.keychain_value("a"), "va")
            self.assertIsNone(kv.keychain_value("b"))

    def test_vault_titles(self):
        with patch.object(kv.subprocess, "run", FakeShell(vault=["x", "y"])):
            self.assertEqual(kv.vault_titles(), {"x", "y"})
        with patch.object(kv.subprocess, "run", FakeShell(list_rc=1)):
            self.assertIsNone(kv.vault_titles())


class TestIntegration(_Base):
    def test_uses_shared_nova_secrets_helpers(self):
        self.assertIn("from nova_secrets import get_secret, list_secrets", SRC)
        self.assertEqual(kv.VAULT, "Nova")

    def test_create_argv_shape(self):
        sh = FakeShell()
        with patch.object(kv.subprocess, "run", sh):
            self.assertTrue(kv.create("n1", "val", "keychain"))
        argv = sh.calls[0]
        self.assertEqual(argv[argv.index("--vault") + 1], "Nova")
        self.assertEqual(argv[argv.index("--tags") + 1], "nova,keychain")


class TestFunctional(_Base):
    def test_golden_path_skips_existing_and_sources_values(self):
        sh = FakeShell(vault=["nova-from-dump"], values={"nova-from-code": "v1"})
        out = self.run_main(sh, fleet=["fleet-a", "ghost"], fleet_values={"fleet-a": "v2"})
        titles = sorted(a[a.index("--title") + 1] for a in sh.creates())
        self.assertEqual(titles, ["fleet-a", "nova-from-code"])
        tags = {a[a.index("--title") + 1]: a[a.index("--tags") + 1] for a in sh.creates()}
        self.assertEqual(tags["fleet-a"], "nova,fleet-store")
        self.assertIn("created 2, no value anywhere 1, failed 0", out)
        self.assertIn("ghost", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help (running it writes to 1Password), so the smoke is an import in a child process
        code = ("import importlib.util as u, subprocess\n"
                "subprocess.run = lambda *a, **k: (_ for _ in ()).throw(SystemExit('subprocess at import'))\n"
                f"s=u.spec_from_file_location('k', {str(SCRIPT)!r}); m=u.module_from_spec(s)\n"
                "s.loader.exec_module(m); print(m.VAULT)\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "Nova")
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
