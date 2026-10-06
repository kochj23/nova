#!/usr/bin/env python3
"""Tests for nova_vault_to_keychain.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The script is a module-level one-shot (no main()). Every test runs it via runpy with subprocess.run replaced
by a fake `security` / `op` pair that answers from synthetic data: the real System keychain and the real
1Password vault are NEVER read or written. All secrets below are made-up test fixtures."""
import io
import json
import os
import re
import runpy
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_vault_to_keychain.py"
SRC = SCRIPT.read_text()
FAKE_TOKEN = "fixture-op-token-not-real"


class _Fake:
    """Stands in for `security` and `op`. present = titles already in the keychain."""
    def __init__(self, items, present=(), token=FAKE_TOKEN, list_rc=0, add_rc=0):
        self.items = {i["id"]: i for i in items}; self.present = set(present)
        self.token = token; self.list_rc = list_rc; self.add_rc = add_rc
        self.calls = []; self.envs = []

    def __call__(self, argv, **kw):
        argv = list(argv); self.calls.append(argv); self.envs.append(kw.get("env"))
        ok = lambda out="", rc=0, err="": subprocess.CompletedProcess(argv, rc, out, err)
        if argv[:2] == ["security", "find-generic-password"]:
            name = argv[argv.index("-s") + 1]
            if name == "nova-op-token":
                return ok(self.token + "\n" if self.token else "", 0 if self.token else 44)
            return ok(rc=0 if name in self.present else 44)
        if argv[:3] == ["op", "item", "list"]:
            listing = [{"id": i["id"], "title": i["title"]} for i in self.items.values()]
            return ok(json.dumps(listing), self.list_rc, "" if self.list_rc == 0 else "401 unauthorized")
        if argv[:3] == ["op", "item", "get"]:
            return ok(json.dumps(self.items[argv[3]]))
        if argv[:2] == ["security", "add-generic-password"]:
            return ok(rc=self.add_rc, err="" if self.add_rc == 0 else "write denied")
        raise AssertionError(f"unexpected command: {argv}")


def _item(i, title, pw="s3cret-fixture", user=None):
    fields = [{"id": "password", "type": "CONCEALED", "value": pw}] if pw else []
    if user:
        fields.append({"id": "username", "type": "STRING", "value": user})
    return {"id": str(i), "title": title, "fields": fields}


def _run(fake):
    buf = io.StringIO()
    with patch.object(subprocess, "run", fake), redirect_stdout(buf):
        try:
            runpy.run_path(str(SCRIPT), run_name="vault_to_keychain_under_test")
            code = 0
        except SystemExit as e:
            code = e.code
    return code, buf.getvalue()


def _adds(fake):
    return [c for c in fake.calls if c[:2] == ["security", "add-generic-password"]]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_never_modifies_or_deletes_existing_items(self):
        fake = _Fake([_item(1, "present-svc"), _item(2, "new-svc")], present={"present-svc"})
        _run(fake)
        flat = [" ".join(c) for c in fake.calls]
        self.assertFalse(any("delete-generic-password" in c or " -U " in f" {c} " for c in flat))
        self.assertEqual([a[a.index("-s") + 1] for a in _adds(fake)], ["new-svc"])
        self.assertFalse(any(c[:3] == ["op", "item", "get"] and c[3] == "1" for c in fake.calls))  # present: not even fetched

    def test_secret_never_printed_and_token_only_in_env(self):
        fake = _Fake([_item(1, "svc", pw="TOPSECRET-fixture-value")])
        code, out = _run(fake)
        self.assertNotIn("TOPSECRET-fixture-value", out)
        self.assertNotIn(FAKE_TOKEN, out)
        op_calls = [(c, e) for c, e in zip(fake.calls, fake.envs) if c[0] == "op"]
        self.assertTrue(all(FAKE_TOKEN not in " ".join(c) for c, _ in op_calls))
        self.assertTrue(all(e["OP_SERVICE_ACCOUNT_TOKEN"] == FAKE_TOKEN for _, e in op_calls))


class TestPerformance(unittest.TestCase):
    def test_5k_items_all_present_is_fast(self):
        items = [_item(i, f"svc{i}") for i in range(5000)]
        fake = _Fake(items, present={f"svc{i}" for i in range(5000)})
        t0 = time.perf_counter()
        code, out = _run(fake)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertIn("5000 vault items; 5000 already present, 0 added", out)


class TestRetry(unittest.TestCase):
    def test_missing_token_exits_before_touching_op(self):
        # RETRY GAP: module-level — no retry anywhere; the hourly LaunchDaemon is the retry. Missing token exits.
        fake = _Fake([_item(1, "x")], token="")
        code, _ = _run(fake)
        self.assertEqual(code, "no nova-op-token in System keychain")
        self.assertFalse(any(c[0] == "op" for c in fake.calls))

    def test_op_list_failure_exits_with_reason(self):
        fake = _Fake([_item(1, "x")], list_rc=1)
        code, _ = _run(fake)
        self.assertTrue(str(code).startswith("op item list failed: 401"))
        self.assertEqual(_adds(fake), [])


class TestUnit(unittest.TestCase):
    def test_account_defaults_and_username(self):
        fake = _Fake([_item(1, "a"), _item(2, "b", user="svc-user")])
        _run(fake)
        accts = {a[a.index("-s") + 1]: a[a.index("-a") + 1] for a in _adds(fake)}
        self.assertEqual(accts, {"a": "nova", "b": "svc-user"})

    def test_item_without_concealed_value_skipped(self):
        fake = _Fake([_item(1, "nopw", pw=None)])
        code, out = _run(fake)
        self.assertEqual(_adds(fake), [])
        self.assertIn("0 added, 0 failed", out)


class TestIntegration(unittest.TestCase):
    def test_adds_to_system_keychain_with_trusted_apps(self):
        fake = _Fake([_item(1, "svc")])
        _run(fake)
        add = _adds(fake)[0]
        self.assertEqual(add[-1], "/Library/Keychains/System.keychain")
        trusted = [add[i + 1] for i, a in enumerate(add) if a == "-T"]
        self.assertIn("/usr/bin/security", trusted)
        self.assertTrue(all(os.path.exists(t) for t in trusted))


class TestFunctional(unittest.TestCase):
    def test_golden_path_summary(self):
        fake = _Fake([_item(1, "have"), _item(2, "need1"), _item(3, "need2")], present={"have"})
        code, out = _run(fake)
        self.assertEqual(code, 0)
        self.assertIn("3 vault items; 1 already present, 2 added, 0 failed", out)

    def test_add_failure_reported_once(self):
        fake = _Fake([_item(1, "a"), _item(2, "b")], add_rc=1)
        code, out = _run(fake)
        self.assertIn("0 added, 2 failed | first error: write denied", out)


class TestFrame(unittest.TestCase):
    def test_runs_clean_in_subprocess_with_stubbed_tools(self):
        # No --help and no __main__ guard (the file IS the run), so the frame smoke runs it in a child process
        # whose subprocess.run is a stub: no keychain, no 1Password.
        self.assertNotIn("__main__", SRC)
        code = (
            "import subprocess, json, runpy, sys\n"
            "def fake(argv, **k):\n"
            "    a = list(argv)\n"
            "    if a[:2] == ['security', 'find-generic-password'] and 'nova-op-token' in a:\n"
            "        return subprocess.CompletedProcess(a, 0, 'fixture\\n', '')\n"
            "    if a[:3] == ['op', 'item', 'list']:\n"
            "        return subprocess.CompletedProcess(a, 0, '[]', '')\n"
            "    raise SystemExit('unexpected ' + ' '.join(a))\n"
            "subprocess.run = fake\n"
            "runpy.run_path(sys.argv[1])\n")
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0 vault items; 0 already present, 0 added, 0 failed", r.stdout)


if __name__ == "__main__":
    unittest.main()
