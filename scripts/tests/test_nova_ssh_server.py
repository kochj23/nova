#!/usr/bin/env python3
"""Tests for nova_ssh_server.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

No port is ever bound and no secret store is ever read: create_server, nova_secrets.vault_secret (1Password /
PG mirror), Ollama and the memory server are all mocked."""
import asyncio
import hmac
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
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ssh_server.py"
SRC = SCRIPT.read_text()
_TMP = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("nssh_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sh = _load()
import nova_secrets  # noqa: E402  (import-clean; validate_password imports vault_secret from it per call)
sh.HOST_KEY_PATH = Path(_TMP.name) / "ssh" / "host_key"
sh.AUTHORIZED_KEYS = Path(_TMP.name) / "authorized_keys"


def _resp(obj):
    r = MagicMock(); r.read.return_value = json.dumps(obj).encode(); return r


class _Proc:
    """Fake asyncssh process: scripted stdin lines, captured stdout."""
    def __init__(self, lines):
        self.lines = list(lines); self.out = []; self.code = None
        self.stdin = SimpleNamespace(readline=self._readline)
        self.stdout = SimpleNamespace(write=self.out.append)

    async def _readline(self):
        return self.lines.pop(0) if self.lines else ""

    def exit(self, code):
        self.code = code


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{8,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('vault_secret("nova-ssh-password")', SRC)  # password lives in the 1Password vault
        self.assertNotIn("find-generic-password", SRC)

    def test_only_known_usernames(self):
        srv = sh.NovaSSHServer()
        self.assertTrue(srv.begin_auth("nova"))
        self.assertFalse(srv.begin_auth("root"))
        self.assertFalse(srv.begin_auth("admin"))

    def test_password_auth_against_vault(self):
        srv = sh.NovaSSHServer()
        with patch.object(nova_secrets, "vault_secret", return_value="s3cret") as v, \
                patch.object(sh.subprocess, "run", side_effect=AssertionError("no subprocess")):
            self.assertTrue(srv.validate_password("nova", "s3cret"))     # right password
            self.assertFalse(srv.validate_password("nova", "wrong"))     # wrong password
            self.assertFalse(srv.validate_password("nova", ""))
        v.assert_called_with("nova-ssh-password")

    def test_vault_failure_refuses_login(self):
        srv = sh.NovaSSHServer()
        for err in (KeyError("nova-ssh-password"), RuntimeError("op down"), OSError("pg down")):
            with patch.object(nova_secrets, "vault_secret", side_effect=err):
                self.assertFalse(srv.validate_password("nova", "s3cret"))
        with patch.object(nova_secrets, "vault_secret", return_value=""):
            self.assertFalse(srv.validate_password("nova", ""))           # empty item never authenticates

    def test_constant_time_compare_used(self):
        srv = sh.NovaSSHServer()
        with patch.object(nova_secrets, "vault_secret", return_value="s3cret"), \
                patch.object(hmac, "compare_digest", wraps=hmac.compare_digest) as cd:
            self.assertFalse(srv.validate_password("nova", "nope"))
            self.assertTrue(srv.validate_password("nova", "s3cret"))
        self.assertEqual(cd.call_count, 2)
        self.assertEqual(cd.call_args.args, (b"s3cret", b"s3cret"))
        self.assertNotIn("password == stored", SRC)

    def test_public_key_requires_authorized_keys(self):
        srv = sh.NovaSSHServer()
        sh.AUTHORIZED_KEYS.unlink(missing_ok=True)
        self.assertFalse(srv.validate_public_key("nova", object()))
        sh.AUTHORIZED_KEYS.write_text("garbage not a key\n")
        self.assertFalse(srv.validate_public_key("nova", object()))


class TestPerformance(unittest.TestCase):
    def test_history_window_bounded(self):
        prompts = []
        lines = [f"msg {i}\n" for i in range(200)] + ["quit\n"]
        with patch.object(sh, "recall", return_value=[]), \
             patch.object(sh, "generate", side_effect=lambda m, c: prompts.append(c) or "ok"):
            t0 = time.perf_counter()
            asyncio.run(sh.handle_session(_Proc(lines)))
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertLessEqual(prompts[-1].count("Jordan:"), 3)     # last 6 history lines only


class TestRetry(unittest.TestCase):
    def test_llm_and_recall_fail_open(self):
        # RETRY GAP: generate()/recall() — one urlopen attempt each; failures become "[error: ...]" / []
        with patch.object(sh.urllib.request, "urlopen", side_effect=OSError("ollama down")) as u:
            self.assertEqual(sh.recall("x"), [])
            self.assertTrue(sh.generate("hi").startswith("[error:"))
        self.assertEqual(u.call_count, 2)


class TestUnit(unittest.TestCase):
    def test_generate_builds_prompt_with_context(self):
        with patch.object(sh.urllib.request, "urlopen", return_value=_resp({"response": "  hi there  "})) as u:
            self.assertEqual(sh.generate("hello", context="- mem"), "hi there")
        body = json.loads(u.call_args[0][0].data)
        self.assertIn("Relevant memories:\n- mem", body["prompt"])
        self.assertTrue(body["prompt"].startswith("/no_think"))
        self.assertEqual(body["model"], sh.MODEL)

    def test_recall_truncates(self):
        with patch.object(sh.urllib.request, "urlopen", return_value=_resp({"memories": [{"text": "x" * 999}]})):
            self.assertEqual(len(sh.recall("q")[0]), 300)


class TestIntegration(unittest.TestCase):
    def test_session_feeds_recall_into_generate(self):
        seen = []
        with patch.object(sh, "recall", return_value=["Jordan likes jazz"]), \
             patch.object(sh, "generate", side_effect=lambda m, c: seen.append((m, c)) or "Noted."):
            p = _Proc(["what music?\n", "\n", "bye\n"])
            asyncio.run(sh.handle_session(p))
        self.assertEqual(seen[0][0], "what music?")
        self.assertIn("- Jordan likes jazz", seen[0][1])
        self.assertIn("Goodbye.", "".join(p.out))
        self.assertEqual(p.code, 0)


class TestFunctional(unittest.TestCase):
    def test_start_server_generates_key_and_never_binds(self):
        sh.HOST_KEY_PATH.unlink(missing_ok=True)
        key = MagicMock()
        with patch.object(sh.asyncssh, "generate_private_key", return_value=key), \
             patch.object(sh.asyncssh, "create_server", AsyncMock()) as cs, patch("builtins.print"):
            key.write_private_key.side_effect = lambda p: Path(p).write_text("k")
            asyncio.run(sh.start_server())
        self.assertEqual(oct(sh.HOST_KEY_PATH.stat().st_mode & 0o777), "0o600")
        kw = cs.call_args.kwargs
        self.assertEqual((kw["port"], kw["process_factory"]), (sh.PORT, sh.handle_session))

    def test_eof_closes_session(self):
        p = _Proc([])
        asyncio.run(sh.handle_session(p))
        self.assertEqual(p.code, 0)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help: running it binds :2222, so the frame check is an import smoke
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_ssh_server"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
