#!/usr/bin/env python3
"""Tests for nova_secrets.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Never touches the real Keychain, 1Password, sudo/systemd-creds or PG: subprocess.run,
shutil.which and psycopg2.connect are mocked in every test, and the credential env is replaced."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_secrets.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_secrets_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ns = _load()
CLEAN_ENV = {k: v for k, v in os.environ.items()
             if not k.startswith(("NOVA_SECRET", "OP_", "CREDENTIALS_DIRECTORY"))}


class _PG:
    def __init__(self, row=("plain",)):
        self.cur = mock.MagicMock()
        self.cur.__enter__.return_value = self.cur
        self.cur.fetchone.return_value = row
        self.conn = mock.MagicMock()
        self.conn.__enter__.return_value = self.conn
        self.conn.cursor.return_value = self.cur


class _Env(unittest.TestCase):
    """Isolated credential env (only a fake master key), all subprocess/PG/op mocked."""
    def setUp(self):
        self.pg = _PG()
        ps = {"env": mock.patch.dict(os.environ, {**CLEAN_ENV, "NOVA_SECRET_KEY": "test-master-key"}, clear=True),
              "run": mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stdout="", stderr="")),
              "which": mock.patch("shutil.which", return_value=None),
              "pg": mock.patch("psycopg2.connect", return_value=self.pg.conn)}
        self.m = {k: p.start() for k, p in ps.items()}
        self.addCleanup(lambda: [p.stop() for p in ps.values()])


OP_TOK = "ro-" + "fixture-token"


def _op_calls(run_mock):
    """Only the `op read` calls (the PG mirror may also probe the Keychain for its DB password)."""
    return [c for c in run_mock.call_args_list if c.args and c.args[0][:2] == ["op", "read"]]


class TestSecurity(_Env):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token|passphrase)\s*=\s*['\"][A-Za-z0-9+/\-]{12,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"ops_[A-Za-z0-9]{20,}")       # no 1Password service-account token

    def test_master_key_and_value_are_bound_params_never_sql_text(self):
        ns.set_secret("api_x", "s3cr3t-value", note="1Password: mirror")
        sql, params = self.pg.cur.execute.call_args[0]
        self.assertNotIn("test-master-key", sql)
        self.assertNotIn("s3cr3t-value", sql)
        self.assertEqual((params["k"], params["v"]), ("test-master-key", "s3cr3t-value"))
        ns.get_secret("api_x")
        sql, params = self.pg.cur.execute.call_args[0]
        self.assertIn("%(k)s", sql)
        self.assertNotIn("test-master-key", sql)

    def test_cli_set_reads_stdin_and_never_prints_value(self):
        with mock.patch.object(sys, "argv", ["x", "set", "svc_pw"]), \
                mock.patch("sys.stdin", io.StringIO("hunter2-long-value\n")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            ns.main()
        self.assertEqual(out.getvalue(), "stored: svc_pw\n")
        self.assertEqual(self.pg.cur.execute.call_args[0][1]["v"], "hunter2-long-value")

    def test_vault_secret_rejects_bad_names_without_calling_op(self):
        self.m["which"].return_value = "/usr/bin/op"
        with mock.patch.dict(os.environ, {"OP_SERVICE_ACCOUNT_TOKEN": OP_TOK}):
            for name, field in (("../x", "password"), ("a/b", "password"), ("", "password"),
                                ("ok-name", "../pw"), ("ok-name", "a/b"), ("x\nop://Other/y", "password")):
                with self.assertRaises(ValueError, msg=(name, field)):
                    ns.vault_secret(name, field)
        self.m["run"].assert_not_called()
        self.m["pg"].assert_not_called()

    def test_vault_token_passed_only_via_env_never_argv(self):
        self.m["which"].return_value = "/usr/bin/op"
        self.m["run"].return_value = mock.Mock(returncode=0, stdout="vault-val\n", stderr="")
        with mock.patch.dict(os.environ, {"OP_SERVICE_ACCOUNT_TOKEN": OP_TOK}):
            self.assertEqual(ns.vault_secret("nova-x"), "vault-val")
        argv, kw = self.m["run"].call_args.args[0], self.m["run"].call_args.kwargs
        self.assertEqual(argv, ["op", "read", "op://Nova/nova-x/password"])
        self.assertFalse(any(OP_TOK in a for a in argv))
        self.assertEqual(kw["env"]["OP_SERVICE_ACCOUNT_TOKEN"], OP_TOK)
        self.assertIs(kw["stdin"], subprocess.DEVNULL)

    def test_reader_uses_least_privilege_role(self):
        ns.get_secret("x")
        self.assertIn("user=nova_secrets", self.m["pg"].call_args[0][0])
        ns.delete_secret("x")
        self.assertIn("user=kochj", self.m["pg"].call_args[0][0])


class TestPerformance(_Env):
    def test_env_resolution_10k(self):
        t0 = time.perf_counter()
        for _ in range(10_000):
            ns._load("NOVA_SECRET_KEY")
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.m["run"].assert_not_called()                       # env hit short-circuits Keychain


class TestRetry(_Env):
    def test_linux_sealed_tries_both_names_then_env_files(self):
        # Credential lookup is a fallback chain, not a timed retry: two systemd-creds names, then two env files.
        self.m["run"].side_effect = [mock.Mock(returncode=1, stdout=""), mock.Mock(returncode=1, stdout=""),
                                     mock.Mock(returncode=1, stdout=""),
                                     mock.Mock(returncode=0, stdout='NOVA_X=1\nNOVA_SECRETS_DB_PASS="pw"\n')]
        self.assertEqual(ns._linux_sealed("NOVA_SECRETS_DB_PASS"), "pw")
        argvs = [c[0][0] for c in self.m["run"].call_args_list]
        self.assertEqual([a[:3] for a in argvs[:2]], [["sudo", "-n", "systemd-creds"]] * 2)
        self.assertEqual(argvs[2][-1], "/etc/nova/nova-secrets-db-pass.env")
        self.assertEqual(argvs[3][-1], "/etc/nova/nova-secret.env")

    def test_vault_read_retries_once_then_uses_mirror(self):
        self.m["which"].return_value = "/usr/bin/op"
        self.m["run"].side_effect = [subprocess.TimeoutExpired("op", 30), mock.Mock(returncode=0, stdout="v2\n", stderr="")]
        with mock.patch.dict(os.environ, {"OP_SERVICE_ACCOUNT_TOKEN": OP_TOK}):
            self.assertEqual(ns.vault_secret("nova-x"), "v2")              # second attempt wins
        self.assertEqual(self.m["run"].call_count, 2)
        self.m["pg"].assert_not_called()
        self.m["run"].reset_mock(); self.m["run"].side_effect = None
        self.m["run"].return_value = mock.Mock(returncode=1, stdout="", stderr="network unreachable")
        with mock.patch.dict(os.environ, {"OP_SERVICE_ACCOUNT_TOKEN": OP_TOK}):
            self.assertEqual(ns.vault_secret("nova-x"), "plain")           # both attempts fail -> PG mirror
        self.assertEqual(len(_op_calls(self.m["run"])), 2)                      # exactly one retry, no more
        self.m["pg"].assert_called_once()

    def test_vault_write_failure_is_non_fatal(self):
        # RETRY GAP: _vault_put — one `op` attempt; failure is reported on stderr and returns False
        self.m["which"].return_value = "/usr/bin/op"
        self.m["run"].side_effect = [mock.Mock(returncode=0, stdout='{"id": "abc"}'),
                                     mock.Mock(returncode=1, stdout="", stderr="rate limited")]
        with mock.patch.dict(os.environ, {"OP_SERVICE_ACCOUNT_TOKEN_RW": "tok", "NOVA_SECRETS_ADMIN_PASS": "pw"}), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            ns.set_secret("api_y", "v")
        self.assertIn("vault write failed for api_y", err.getvalue())
        self.assertNotIn("v\n", err.getvalue())
        self.assertEqual(self.m["run"].call_count, 2)


class TestUnit(_Env):
    def test_load_prefers_credentials_directory(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "NOVA_SECRET_KEY").write_text("from-cred-dir\n")
            with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": d}):
                self.assertEqual(ns._load("NOVA_SECRET_KEY"), "from-cred-dir")

    def test_missing_credential_exits_with_name_only(self):
        with mock.patch.dict(os.environ, {"NOVA_SECRET_KEY": ""}), mock.patch.object(ns.sys, "platform", "darwin"):
            with self.assertRaises(SystemExit) as cm:
                ns._env("NOVA_SECRET_KEY")
        self.assertEqual(str(cm.exception), "[nova_secrets] missing credential: NOVA_SECRET_KEY")

    def test_keychain_service_name_kebab_and_non_darwin(self):
        self.m["run"].return_value = mock.Mock(returncode=0, stdout="val\n")
        with mock.patch.object(ns.sys, "platform", "darwin"):
            self.assertEqual(ns._keychain("NOVA_SECRET_KEY"), "val")
        self.assertIn("nova-secret-key", self.m["run"].call_args[0][0])
        with mock.patch.object(ns.sys, "platform", "linux"):
            self.assertIsNone(ns._keychain("NOVA_SECRET_KEY"))

    def test_vault_secret_hit_returns_stripped_value_and_skips_mirror(self):
        self.m["which"].return_value = "/usr/bin/op"
        self.m["run"].return_value = mock.Mock(returncode=0, stdout="  from-vault \n", stderr="")
        with mock.patch.dict(os.environ, {"OP_SERVICE_ACCOUNT_TOKEN": OP_TOK}):
            self.assertEqual(ns.vault_secret("nova-x", field="credential"), "from-vault")
        self.assertEqual(self.m["run"].call_args.args[0][-1], "op://Nova/nova-x/credential")
        self.assertEqual(self.m["run"].call_count, 1)
        self.m["pg"].assert_not_called()

    def test_get_unknown_raises_keyerror(self):
        self.pg.cur.fetchone.return_value = None
        with self.assertRaises(KeyError):
            ns.get_secret("nope")


class TestIntegration(_Env):
    def test_set_mirrors_to_vault_unless_note_marks_vault_origin(self):
        with mock.patch.object(ns, "_vault_put") as vp:
            ns.set_secret("a", "1")
            ns.set_secret("b", "2", note="1Password: synced")
        self.assertEqual([c[0][0] for c in vp.call_args_list], ["a"])
        self.assertEqual(self.pg.conn.commit.call_count, 2)

    def test_vault_not_found_falls_back_to_mirror_without_retry(self):
        self.m["which"].return_value = "/usr/bin/op"
        self.m["run"].return_value = mock.Mock(returncode=1, stdout="",
                                              stderr='[ERROR] "nova-x" isn\'t an item in the "Nova" vault')
        with mock.patch.dict(os.environ, {"OP_SERVICE_ACCOUNT_TOKEN": OP_TOK}):
            self.assertEqual(ns.vault_secret("nova-x"), "plain")           # mirror = get_secret(name)
        self.assertEqual(len(_op_calls(self.m["run"])), 1)                 # definitive miss: no retry
        sql, params = self.pg.cur.execute.call_args.args
        self.assertIn("FROM nova.secrets", sql)
        self.assertEqual(params["n"], "nova-x")

    def test_no_op_or_token_goes_straight_to_mirror(self):
        self.assertEqual(ns.vault_secret("nova-x"), "plain")               # which(op) is None in _Env
        self.assertFalse([c for c in self.m["run"].call_args_list if c.args[0][:1] == ["op"]])

    def test_vault_create_uses_0600_template_and_cleans_up(self):
        self.m["which"].return_value = "/usr/bin/op"
        seen = {}
        def run(argv, **k):
            if argv[:3] == ["op", "item", "get"]:
                return mock.Mock(returncode=1, stdout="", stderr="not found")
            path = argv[argv.index("--template") + 1]
            seen["mode"] = os.stat(path).st_mode & 0o777
            seen["path"] = path
            return mock.Mock(returncode=0, stdout="", stderr="")
        self.m["run"].side_effect = run
        with mock.patch.dict(os.environ, {"OP_SERVICE_ACCOUNT_TOKEN_RW": "tok"}):
            self.assertTrue(ns._vault_put("new_item", "v"))
        self.assertEqual(seen["mode"], 0o600)
        self.assertFalse(os.path.exists(seen["path"]))


class TestFunctional(_Env):
    def test_cli_get_list_delete(self):
        with mock.patch.object(sys, "argv", ["x", "get", "svc"]), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            ns.main()
        self.assertEqual(out.getvalue(), "plain")
        self.pg.cur.fetchall.return_value = [("svc", "n", "2026-01-01 00:00:00", "kochj")]
        with mock.patch.object(sys, "argv", ["x", "list"]), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            ns.main()
        self.assertIn("svc", out.getvalue())
        with mock.patch.object(sys, "argv", ["x", "delete", "svc"]), mock.patch("sys.stdout", new_callable=io.StringIO):
            ns.main()
        self.assertEqual(self.pg.cur.execute.call_args[0], ("DELETE FROM nova.secrets WHERE name=%s", ("svc",)))

    def test_vault_and_mirror_both_miss_raise_keyerror_never_default(self):
        self.m["which"].return_value = "/usr/bin/op"
        self.m["run"].return_value = mock.Mock(returncode=1, stdout="", stderr="item not found")
        self.pg.cur.fetchone.return_value = None
        with mock.patch.dict(os.environ, {"OP_SERVICE_ACCOUNT_TOKEN": OP_TOK}):
            with self.assertRaises(KeyError) as cm:
                ns.vault_secret("nova-x")
        self.assertIn("nova-x", str(cm.exception))
        self.m["pg"].side_effect = OSError("pg down")                      # mirror unreachable is also KeyError
        with mock.patch.dict(os.environ, {"OP_SERVICE_ACCOUNT_TOKEN": OP_TOK}):
            with self.assertRaises(KeyError):
                ns.vault_secret("nova-x")

    def test_cli_usage_and_unknown(self):
        for argv in (["x"], ["x", "frobnicate"]):
            with mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
                ns.main()
        self.m["pg"].assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main_and_usage_touches_nothing(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        env = {**CLEAN_ENV, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, "-c", "import nova_secrets"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        r = subprocess.run([sys.executable, str(PATH)], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("usage:", r.stderr)


if __name__ == "__main__":
    unittest.main()
