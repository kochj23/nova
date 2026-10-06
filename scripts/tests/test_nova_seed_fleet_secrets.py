#!/usr/bin/env python3
"""Tests for nova_seed_fleet_secrets.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_seed_fleet_secrets.py"
SRC = SCRIPT.read_text()
_ENV_KEYS = ("NOVA_SECRETS_ADMIN_DSN", "NOVA_SECRETS_DSN")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # The script setdefault()s the fleet DSNs at import; keep that out of the process env for other files.
    with patch.dict(os.environ, {}, clear=False):
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        spec.loader.exec_module(mod)
        captured = {k: os.environ.get(k) for k in _ENV_KEYS}
    mod._captured_env = captured
    return mod


sf = _load("seed_fleet_under_test", SCRIPT)
# nova_secrets is bound at import; its PG-backed calls are stubbed for every test below (never a real connect).
sf.nova_secrets = MagicMock(name="nova_secrets")
SECRET = "xoxb-TEST-not-a-real-token-0000000000"


def _cp(rc=0, out=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr="")


def _keychain(table):
    """subprocess.run stand-in: `table` maps service -> value (None == absent)."""
    def run(args, **kw):
        svc = args[args.index("-s") + 1]
        val = table.get(svc)
        return _cp(0, val + "\n") if val else _cp(44, "")
    return run


def _seed(names, table, store=None, platform="darwin"):
    """Run seed() fully offline; returns (rc, stdout, store_dict, nova_secrets mock)."""
    store = {} if store is None else store
    ns = MagicMock(name="nova_secrets")
    ns.set_secret.side_effect = lambda n, v, note=None: store.__setitem__(n, v)
    ns.get_secret.side_effect = lambda n: store.get(n)
    with patch.object(sf, "nova_secrets", ns), patch.object(sf.subprocess, "run", _keychain(table)), \
         patch.object(sf.sys, "platform", platform), redirect_stdout(io.StringIO()) as out:
        rc = sf.seed(list(names))
    return rc, out.getvalue(), store, ns


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password=", SRC.lower())          # DSNs carry no password; nova_secrets resolves it

    def test_secret_values_never_reach_stdout(self):
        rc, out, store, _ = _seed(["nova-slack-bot-token"], {"nova-slack-bot-token": SECRET})
        self.assertEqual(rc, 0)
        self.assertNotIn(SECRET, out)
        self.assertNotIn(SECRET[:12], out)
        self.assertEqual(store["nova-slack-bot-token"], SECRET)   # it went to the store, not the transcript

    def test_mismatch_path_prints_status_only(self):
        ns = MagicMock(); ns.get_secret.return_value = "something-else"
        with patch.object(sf, "nova_secrets", ns), patch.object(sf.subprocess, "run", _keychain({"nova-x": SECRET})), \
             patch.object(sf.sys, "platform", "darwin"), redirect_stdout(io.StringIO()) as o:
            self.assertEqual(sf.seed(["nova-x"]), 1)
        self.assertIn("MISMATCH", o.getvalue())
        self.assertNotIn(SECRET, o.getvalue()); self.assertNotIn("something-else", o.getvalue())

    def test_keychain_read_is_argv_only_and_secrets_come_from_the_keychain(self):
        self.assertNotIn("shell=True", SRC)
        with patch.object(sf.subprocess, "run", MagicMock(return_value=_cp(0, "v\n"))) as sp:
            self.assertEqual(sf.keychain_read("svc"), "v")
        self.assertEqual(sp.call_args[0][0], ["security", "find-generic-password", "-a", "nova", "-s", "svc", "-w"])

    def test_targets_the_pg_primary_not_loopback(self):
        self.assertIn("host=192.168.1.2", sf._captured_env["NOVA_SECRETS_ADMIN_DSN"])
        self.assertIn("user=nova_secrets", sf._captured_env["NOVA_SECRETS_DSN"])
        self.assertNotIn("127.0.0.1", sf._captured_env["NOVA_SECRETS_ADMIN_DSN"])
        for k in _ENV_KEYS:
            self.assertNotIn(k, os.environ)                 # the import-time default did not leak into this process


class TestPerformance(unittest.TestCase):
    def test_seeding_10k_names_is_linear_and_fast(self):
        names = [f"svc-{i}" for i in range(10_000)]
        table = {n: f"value-{i}" for i, n in enumerate(names)}
        t0 = time.perf_counter()
        rc, out, store, ns = _seed(names, table)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(rc, 0)
        self.assertEqual(len(store), 10_000)
        self.assertEqual(ns.set_secret.call_count, 10_000)


class TestRetry(unittest.TestCase):
    def test_keychain_read_tries_account_then_no_account(self):
        # The only "retry" is the two-form Keychain lookup: `-a nova` first, then service-only.
        calls = []

        def run(args, **kw):
            calls.append(args); return _cp(44, "") if "-a" in args else _cp(0, "v2\n")
        with patch.object(sf.subprocess, "run", run):
            self.assertEqual(sf.keychain_read("svc"), "v2")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("-a", calls[1])

    def test_missing_keychain_entry_is_skipped_not_fatal(self):
        # RETRY GAP: keychain_read — two lookup forms then give up; seed() counts it as a skip and keeps going
        rc, out, store, ns = _seed(["absent", "present"], {"present": "p"})
        self.assertEqual(rc, 1)
        self.assertIn("SKIP  absent", out)
        self.assertEqual(store, {"present": "p"})
        self.assertIn("1 stored+verified, 1 skipped/failed, of 2", out)

    def test_store_failure_propagates(self):
        # RETRY GAP: nova_secrets.set_secret — one attempt; a PG error escapes so a half-seeded run is loud, not silent
        ns = MagicMock(); ns.set_secret.side_effect = RuntimeError("pg primary unreachable")
        with patch.object(sf, "nova_secrets", ns), patch.object(sf.subprocess, "run", _keychain({"a": "1"})), \
             patch.object(sf.sys, "platform", "darwin"), redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                sf.seed(["a"])


class TestUnit(unittest.TestCase):
    def test_keychain_read_edges(self):
        with patch.object(sf.subprocess, "run", MagicMock(return_value=_cp(0, "   \n"))):
            self.assertIsNone(sf.keychain_read("blank"))           # whitespace-only == absent
        with patch.object(sf.subprocess, "run", MagicMock(return_value=_cp(1, "junk"))):
            self.assertIsNone(sf.keychain_read("rc-nonzero"))
        with patch.object(sf.subprocess, "run", MagicMock(return_value=_cp(0, "  v  \n"))):
            self.assertEqual(sf.keychain_read("ok"), "v")

    def test_seed_empty_list_is_a_clean_noop(self):
        rc, out, store, ns = _seed([], {})
        self.assertEqual(rc, 0)
        self.assertEqual(store, {})
        self.assertIn("0 stored+verified, 0 skipped/failed, of 0", out)
        ns.set_secret.assert_not_called()

    def test_refuses_to_run_off_macos(self):
        with patch.object(sf.sys, "platform", "linux"), patch.object(sf.subprocess, "run", MagicMock()) as sp:
            with self.assertRaises(SystemExit) as cm:
                sf.seed(["x"])
        self.assertIn("run this on .6", str(cm.exception))
        sp.assert_not_called()

    def test_gateway_wave_one_set(self):
        self.assertEqual(sf.GATEWAY_SECRETS, ["nova-slack-bot-token", "nova-slack-app-token",
                                             "nova-discord-token", "nova-openrouter-api-key"])
        for n in sf.GATEWAY_SECRETS:
            self.assertRegex(n, r"^nova-[a-z-]+$")           # Keychain service name == fleet store name


class TestIntegration(unittest.TestCase):
    def test_uses_the_shared_fleet_store_helpers_not_its_own_sql(self):
        self.assertNotIn("psycopg2", SRC)
        self.assertNotIn("pgp_sym_encrypt", SRC.split('"""', 2)[2])     # only mentioned in the docstring
        self.assertIn("nova_secrets.set_secret(", SRC); self.assertIn("nova_secrets.get_secret(", SRC)
        import nova_secrets as real
        self.assertTrue(callable(real.set_secret) and callable(real.get_secret))

    def test_set_then_get_roundtrip_with_note(self):
        rc, out, store, ns = _seed(["nova-discord-token"], {"nova-discord-token": "d"})
        ns.set_secret.assert_called_once_with("nova-discord-token", "d", note="seeded from .6 Keychain (#502)")
        ns.get_secret.assert_called_once_with("nova-discord-token")
        self.assertIn("OK   nova-discord-token", out)
        self.assertIn("stored + verified", out)

    def test_dsn_defaults_are_overridable(self):
        with patch.dict(os.environ, {"NOVA_SECRETS_ADMIN_DSN": "host=10.9.9.9 dbname=x user=y"}):
            spec = importlib.util.spec_from_file_location("seed_fleet_override", SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertEqual(os.environ["NOVA_SECRETS_ADMIN_DSN"], "host=10.9.9.9 dbname=x user=y")
        self.assertNotIn("NOVA_SECRETS_ADMIN_DSN", os.environ)


class TestFunctional(unittest.TestCase):
    def test_golden_path_seeds_the_default_set(self):
        table = {n: f"val-{i}" for i, n in enumerate(sf.GATEWAY_SECRETS)}
        rc, out, store, ns = _seed(sf.GATEWAY_SECRETS, table)
        self.assertEqual(rc, 0)
        self.assertEqual(store, table)
        self.assertIn("[seed] 4 stored+verified, 0 skipped/failed, of 4", out)
        self.assertEqual(out.count("OK   "), 4)
        for v in table.values():
            self.assertNotIn(v, out)

    def test_error_path_partial_keychain_returns_one(self):
        table = {"nova-slack-bot-token": "a", "nova-discord-token": "b"}
        rc, out, store, _ = _seed(sf.GATEWAY_SECRETS, table)
        self.assertEqual(rc, 1)
        self.assertEqual(set(store), set(table))
        self.assertIn("SKIP  nova-slack-app-token", out); self.assertIn("SKIP  nova-openrouter-api-key", out)
        self.assertIn("2 stored+verified, 2 skipped/failed, of 4", out)


class TestFrame(unittest.TestCase):
    def test_main_guard_and_import_never_seeds(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(seed(sys.argv[1:] or GATEWAY_SECRETS))', SRC)
        # No --help/--selftest (a one-shot seeder would hit the Keychain + PG primary); import smoke instead.
        r = subprocess.run([sys.executable, "-c", "import nova_seed_fleet_secrets as m; print(len(m.GATEWAY_SECRETS))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "4")


if __name__ == "__main__":
    unittest.main()
