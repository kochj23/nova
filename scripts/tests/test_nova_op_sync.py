#!/usr/bin/env python3
"""Tests for nova_op_sync.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_op_sync.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("op_sync_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ops = _load()
FAKE = "tok-" + "x" * 8


def _item(iid, title, fields):
    return {"id": iid, "title": title, "category": "LOGIN", "fields": fields}


def _vault(items):
    """Fake _op: item list -> ids; item get -> the full record."""
    by_id = {i["id"]: i for i in items}

    def op(args, tok):
        if args[:2] == ["item", "list"]:
            return [{"id": i["id"]} for i in items]
        return by_id[args[2]]
    return op


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_never_prints_a_secret_value(self):
        items = [_item("a", "SLACK", [{"id": "f", "type": "CONCEALED", "value": "s3cr3t-VALUE"}])]
        buf = io.StringIO()
        with patch.object(ops, "_token", return_value=FAKE), patch.object(ops, "_op", side_effect=_vault(items)), \
                patch.object(ops, "set_secret"), redirect_stdout(buf):
            ops.main()
        self.assertNotIn("s3cr3t-VALUE", buf.getvalue())

    def test_token_passed_by_env_not_argv(self):
        with patch.object(ops.subprocess, "run") as run:
            run.return_value = types.SimpleNamespace(returncode=0, stdout="[]", stderr="")
            ops._op(["item", "list"], FAKE)
        argv, kw = run.call_args.args[0], run.call_args.kwargs
        self.assertNotIn(FAKE, argv)
        self.assertEqual(kw["env"]["OP_SERVICE_ACCOUNT_TOKEN"], FAKE)


class TestPerformance(unittest.TestCase):
    def test_500_items_sync_fast(self):
        items = [_item(str(i), f"T{i}", [{"id": "p", "type": "CONCEALED", "value": "v"}]) for i in range(500)]
        with patch.object(ops, "_token", return_value=FAKE), patch.object(ops, "_op", side_effect=_vault(items)), \
                patch.object(ops, "set_secret") as ss, redirect_stdout(io.StringIO()):
            t0 = time.perf_counter()
            ops.main()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(ss.call_count, 500)


class TestRetry(unittest.TestCase):
    def test_op_failure_raises_once_no_retry(self):
        # RETRY GAP: _op() — one `op` CLI attempt; a non-zero exit raises (hourly scheduler re-runs it)
        with patch.object(ops.subprocess, "run") as run:
            run.return_value = types.SimpleNamespace(returncode=1, stdout="", stderr="rate limited")
            with self.assertRaises(RuntimeError) as cm:
                ops._op(["item", "list", "--vault", "Nova"], FAKE)
        self.assertEqual(run.call_count, 1)
        self.assertIn("rate limited", str(cm.exception))
        self.assertNotIn(FAKE, str(cm.exception))

    def test_missing_token_exits_cleanly(self):
        with patch.dict(os.environ, {"OP_SERVICE_ACCOUNT_TOKEN": ""}), patch.object(ops, "_load", return_value=None), \
                patch.object(ops.sys, "platform", "linux"):
            with self.assertRaises(SystemExit) as cm:
                ops._token()
        self.assertIn("no 1Password", str(cm.exception.code))


class TestUnit(unittest.TestCase):
    def test_token_prefers_env(self):
        with patch.dict(os.environ, {"OP_SERVICE_ACCOUNT_TOKEN": FAKE}), patch.object(ops.subprocess, "run") as run:
            self.assertEqual(ops._token(), FAKE)
        run.assert_not_called()

    def test_token_keychain_fallback_on_darwin(self):
        with patch.dict(os.environ, {"OP_SERVICE_ACCOUNT_TOKEN": ""}), patch.object(ops, "_load", return_value=None), \
                patch.object(ops.sys, "platform", "darwin"), patch.object(ops.subprocess, "run") as run:
            run.return_value = types.SimpleNamespace(returncode=0, stdout=FAKE + "\n")
            self.assertEqual(ops._token(), FAKE)
        self.assertIn("nova-op-token", run.call_args.args[0])

    def test_op_parses_json(self):
        with patch.object(ops.subprocess, "run") as run:
            run.return_value = types.SimpleNamespace(returncode=0, stdout=json.dumps([{"id": 1}]), stderr="")
            self.assertEqual(ops._op(["item", "list"], FAKE), [{"id": 1}])
        self.assertIn("--format=json", run.call_args.args[0])


class TestIntegration(unittest.TestCase):
    def test_uses_fleet_secret_store_helpers(self):
        import nova_secrets
        self.assertIs(ops.set_secret, nova_secrets.set_secret)
        self.assertIs(ops._load, nova_secrets._load)
        self.assertEqual(ops.VAULT, "Nova")


class TestFunctional(unittest.TestCase):
    def test_naming_single_multi_and_skip(self):
        items = [
            _item("a", " SLACK_TOKEN ", [{"id": "p", "type": "CONCEALED", "value": "v1"},
                                         {"id": "u", "type": "STRING", "value": "user"}]),
            _item("b", "UNIFI", [{"id": "p1", "label": "api", "type": "CONCEALED", "value": "v2"},
                                 {"id": "p2", "label": "", "type": "CONCEALED", "value": "v3"}]),
            _item("c", "NOTE", [{"id": "n", "type": "STRING", "value": "x"},
                                {"id": "e", "type": "CONCEALED", "value": ""}]),
        ]
        buf = io.StringIO()
        with patch.object(ops, "_token", return_value=FAKE), patch.object(ops, "_op", side_effect=_vault(items)), \
                patch.object(ops, "set_secret") as ss, redirect_stdout(buf):
            ops.main()
        names = [c.args[0] for c in ss.call_args_list]
        self.assertEqual(names, ["SLACK_TOKEN", "UNIFI/api", "UNIFI/p2"])
        self.assertIn("1Password:Nova/a", ss.call_args_list[0].kwargs["note"])
        self.assertIn("3 secrets stored, 1 items without", buf.getvalue())

    def test_item_get_failure_aborts(self):
        def op(args, tok):
            if args[:2] == ["item", "list"]:
                return [{"id": "a"}]
            raise RuntimeError("op item get failed")
        with patch.object(ops, "_token", return_value=FAKE), patch.object(ops, "_op", side_effect=op), \
                patch.object(ops, "set_secret") as ss:
            with self.assertRaises(RuntimeError):
                ops.main()
        ss.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_op_sync"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
