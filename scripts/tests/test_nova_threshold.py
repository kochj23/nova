#!/usr/bin/env python3
"""Tests for nova_threshold.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import base64
import inspect
import os
import subprocess
import sys
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_threshold as T  # noqa: E402

SRC = (SCRIPTS / "nova_threshold.py").read_text()
REAL_SSH_CAT, REAL_HERD = T._ssh_cat, T.herd_senders      # before setUpModule stubs them
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
BLOB = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + b"k" * 36).decode()
KEYS = f'command="~/bin/gate.sh" ssh-ed25519 {BLOB} gate\nssh-ed25519 {BLOB}x= broken\n'
_PATCHERS = []


def setUpModule():
    # no SSH, no ifconfig, no herd file from any test in this module
    for p in (mock.patch.object(T, "_ssh_cat", return_value=("ok", KEYS)),
              mock.patch("nova_fleet_exec.is_local", return_value=False),
              mock.patch.object(T, "herd_senders", return_value=[])):
        p.start()
        _PATCHERS.append(p)


def tearDownModule():
    for p in reversed(_PATCHERS):
        p.stop()
    _PATCHERS.clear()


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = next((list(v) for k, v in self.routes.items() if k in sql), [])

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def fake_conn(cur):
    c = mock.MagicMock()
    c.cursor.return_value = cur
    return c


FP = T.fingerprint(BLOB)
ROUTES = {
    "FROM node_status": [("nova-core", "node.example")],
    "FROM nova.secrets": [("nova-slack-bot-token", T0)],
    "to_regclass": [("threshold_ledger",)],
    "FROM threshold_ledger": [("ssh_key", "nova-core", FP, None, None, None, None),
                              ("slack_token", "", "nova-slack-bot-token", "jordan", "gateway", None, None)],
    "FROM claude_queue": [],
    "RETURNING id": [(99,)],
}


class TestSecurity(unittest.TestCase):
    def test_no_secrets_and_sql_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(\w+(\.cursor\(\))?,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_secret_store_metadata_only(self):
        src = inspect.getsource(T.slack_tokens)
        self.assertIn("SELECT name, updated_at FROM nova.secrets", src)
        self.assertNotIn("cipher", SRC)
        self.assertNotIn("get_secret", SRC)
        self.assertNotIn("find-generic-password", SRC)

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotIn("kochj", SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_human_columns_never_overwritten(self):
        upd = T.upsert.__code__.co_consts
        sql = " ".join(c for c in upd if isinstance(c, str))
        set_part = sql.split("DO UPDATE SET", 1)[1]
        for col in ("owner=", "purpose=", "invited_by="):
            self.assertNotIn(col, set_part)

    def test_hostile_label_stays_a_parameter(self):
        evil = "x'); DROP TABLE claude_queue; --"
        cur = FakeCur()
        T.upsert(cur, [{"kind": "herd_sender", "host": "", "ident": evil, "label": evil, "fingerprint": None,
                        "sender_bound": False, "detail": {}, "expires_at": None, "last_rotated": None}])
        sql, params = [x for x in cur.sql if "INSERT" in x[0]][0]
        self.assertNotIn(evil, sql)
        self.assertIn(evil, params)


class TestPerformance(unittest.TestCase):
    def test_parse_and_flag_10k_keys(self):
        text = "\n".join(f"ssh-ed25519 {BLOB} user{i}@host" for i in range(10000))
        t = time.monotonic()
        rows = T.parse_authorized_keys(text, "h")
        for r in rows:
            T.flags(r, now=T0)
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertEqual(len(rows), 10000)


class TestRetry(unittest.TestCase):
    def test_ssh_retries_with_backoff(self):
        bad, good = mock.Mock(returncode=255, stdout=""), mock.Mock(returncode=0, stdout=KEYS)
        sleeps = []
        with mock.patch.object(T, "_ssh_cat", REAL_SSH_CAT), mock.patch("subprocess.run", side_effect=[bad, bad, good]) as run, mock.patch("builtins.print"):
            rows = T.host_keys("nova-core", "node.example", _sleep=sleeps.append)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleeps, [3.0, 6.0])
        self.assertEqual(len(rows), 1)

    def test_unreachable_host_fails_open(self):
        with mock.patch.object(T, "_ssh_cat", return_value=None), mock.patch("builtins.print"):
            self.assertIsNone(T.host_keys("x", "node.example", _sleep=lambda s: None))
            self.assertEqual(T.fleet_keys(FakeCur({"FROM node_status": [("x", "node.example")]}),
                                          _sleep=lambda s: None), [])

    def test_query_failure_contained(self):
        with mock.patch("builtins.print"):
            self.assertEqual(T.stored(FakeCur(boom=True)), {})
            self.assertEqual(T.slack_tokens(FakeCur(boom=True)), [])


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(T.selftest(), 0)

    def test_parse_edges(self):
        self.assertEqual(T.parse_authorized_keys("", "h"), [])
        self.assertEqual(T.parse_authorized_keys("# only\n\n", "h"), [])
        rows = T.parse_authorized_keys(KEYS, "h")
        self.assertEqual(len(rows), 1)                      # second line's base64 is invalid
        self.assertEqual(rows[0]["detail"]["forced_command"], "~/bin/gate.sh")
        self.assertFalse(rows[0]["sender_bound"])

    def test_expiry_and_flags(self):
        self.assertEqual(T.parse_expiry("202701011230"), datetime(2027, 1, 1, 12, 30, tzinfo=timezone.utc))
        self.assertIsNone(T.parse_expiry("soon"))
        r = {"owner": "j", "purpose": "p", "expires_at": T0 - timedelta(days=1), "last_rotated": None}
        self.assertEqual(T.flags(r, now=T0), ["expired"])


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_imported(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("from nova_fleet_exec import is_local", SRC)
        self.assertIn("W.retry(_ssh_cat", SRC)
        self.assertIn("FROM node_status", SRC)

    def test_schema_contract(self):
        for col in ("owner text", "purpose text", "expires_at timestamptz", "last_rotated timestamptz",
                    "sender_bound boolean", "PRIMARY KEY (kind, host, ident)"):
            self.assertIn(col, T.SCHEMA)

    def test_queue_dedup_returns_existing(self):
        cur = FakeCur({"FROM claude_queue": [(41,)]})
        self.assertEqual(T.file_queue(cur, [{"kind": "x", "host": "", "label": "a", "fingerprint": None}], []), 41)
        self.assertFalse(any("INSERT" in s for s, _ in cur.sql))

    def test_herd_rows_from_herd_config(self):
        fake = types.SimpleNamespace(HERD=[{"name": "Sam", "email": "Sam@Example.org", "profile": "p"}])
        with mock.patch.dict(sys.modules, {"herd_config": fake}):
            rows = REAL_HERD()
        self.assertEqual(rows[0]["ident"], "sam@example.org")
        self.assertEqual(rows[0]["kind"], "herd_sender")


class TestFunctional(unittest.TestCase):
    def _run(self, dry, routes=ROUTES):
        cur = FakeCur(routes)
        with mock.patch.object(T.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print"):
            rows = T.run(dry=dry, _sleep=lambda s: None)
        return cur, rows

    def test_run_upserts_and_files_no_purpose(self):
        cur, rows = self._run(False)
        self.assertEqual({r["kind"] for r in rows}, {"ssh_key", "slack_token"})
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS threshold_ledger" in s for s, _ in cur.sql))
        self.assertEqual(sum("INSERT INTO threshold_ledger" in s for s, _ in cur.sql), 2)
        q = [p for s, p in cur.sql if "INSERT INTO claude_queue" in s]
        self.assertEqual(len(q), 1)
        self.assertIn("nova-core:gate", q[0][2])             # the ssh key has no purpose
        self.assertNotIn("nova-slack-bot-token", q[0][2])     # the token has one

    def test_dry_run_writes_nothing(self):
        cur, rows = self._run(True)
        self.assertEqual(len(rows), 2)
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE")))

    def test_empty_world_lists_nothing(self):
        cur, rows = self._run(True, {})
        self.assertEqual(rows, [])


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_threshold.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_threshold.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
