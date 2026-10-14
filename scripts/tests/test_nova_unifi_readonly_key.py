#!/usr/bin/env python3
"""Tests for the optional read-only UniFi key (BSRS ch.5 least privilege, 2026-10-13):
nova_unifi_poller prefers nova-unifi-api-key-readonly for read-only callers, falls back to the admin
key when it is absent or the UDM refuses it; nova_unifi_toolkit runs only its read commands on it.
`security` (subprocess.run) and the UDM opener are mocked — nothing touches the network.
Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_unifi_readonly_key.py
"""
import io
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_unifi_poller as poller   # noqa: E402
import nova_unifi_toolkit as m       # noqa: E402

RO, ADMIN = "ro-key-value", "admin-key-value"


def fake_security(store):
    def run(args, **kw):
        name = args[args.index("-s") + 1]
        if name in store:
            return SimpleNamespace(returncode=0, stdout=store[name] + "\n", stderr="")
        return SimpleNamespace(returncode=44, stdout="", stderr="not found")
    return run


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = (poller._READONLY, poller._ro_rejected, poller._key_role, poller._api_key)
        poller._READONLY, poller._ro_rejected, poller._key_role = False, False, None

    def tearDown(self):
        poller._READONLY, poller._ro_rejected, poller._key_role, poller._api_key = self._saved


class TestKeySelection(Base):
    def test_admin_callers_never_get_ro_key(self):
        with patch.object(poller.subprocess, "run", side_effect=fake_security(
                {poller.UNIFI_RO_KEY_NAME: RO, poller.UNIFI_KEY_NAME: ADMIN})):
            self.assertEqual(poller._get_unifi_api_key(), ADMIN)
            self.assertEqual(poller._key_role, "admin")

    def test_readonly_prefers_ro_key(self):
        poller._READONLY = True
        with patch.object(poller.subprocess, "run", side_effect=fake_security(
                {poller.UNIFI_RO_KEY_NAME: RO, poller.UNIFI_KEY_NAME: ADMIN})):
            self.assertEqual(poller._get_unifi_api_key(), RO)
            self.assertEqual(poller._key_role, "readonly")

    def test_readonly_falls_back_when_ro_key_absent(self):
        poller._READONLY = True
        with patch.object(poller.subprocess, "run", side_effect=fake_security({poller.UNIFI_KEY_NAME: ADMIN})):
            self.assertEqual(poller._get_unifi_api_key(), ADMIN)
            self.assertEqual(poller._key_role, "admin")

    def test_explicit_readonly_arg_overrides_flag(self):
        with patch.object(poller.subprocess, "run", side_effect=fake_security(
                {poller.UNIFI_RO_KEY_NAME: RO, poller.UNIFI_KEY_NAME: ADMIN})):
            self.assertEqual(poller._get_unifi_api_key(readonly=True), RO)

    def test_no_admin_key_exits(self):
        with patch.object(poller.subprocess, "run", side_effect=fake_security({})), self.assertRaises(SystemExit):
            poller._get_unifi_api_key()

    def test_security_oserror_is_absent_not_crash(self):
        with patch.object(poller.subprocess, "run", side_effect=OSError("no security")):
            self.assertIsNone(poller._keychain_get("x"))


class TestRefusalFallback(Base):
    def test_refused_ro_key_switches_to_admin_once(self):
        poller._READONLY = True
        with patch.object(poller.subprocess, "run", side_effect=fake_security(
                {poller.UNIFI_RO_KEY_NAME: RO, poller.UNIFI_KEY_NAME: ADMIN})):
            poller._get_unifi_api_key()
            self.assertTrue(poller._readonly_refused())
            self.assertFalse(poller._readonly_refused())        # only once
            self.assertEqual(poller._get_unifi_api_key(), ADMIN)

    def test_admin_refusal_is_not_masked(self):
        poller._key_role = "admin"
        self.assertFalse(poller._readonly_refused())

    def test_poller_get_403_on_ro_returns_none_for_relogin(self):
        poller._key_role, poller._api_key = "readonly", RO
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b""))
        with patch.object(poller._opener, "open", side_effect=err):
            self.assertIsNone(poller._unifi_get("https://udm/x"))
        self.assertTrue(poller._ro_rejected)


class TestToolkit(Base):
    def test_read_commands_set(self):
        self.assertEqual(m.READ_ONLY_CMDS, {"threats", "stalk", "audit", "track", "report"})
        for w in ("lockdown", "release", "isolate", "block", "unblock", "dns-lockdown", "mdns", "dhcp-dns"):
            self.assertNotIn(w, m.READ_ONLY_CMDS)

    def test_main_sets_readonly_flag_per_command(self):
        with patch.object(m, "cmd_report", return_value=0):
            m.main(["report", "02:00:5e:be:ef:01"])
        self.assertTrue(poller._READONLY)
        with patch.object(m, "cmd_block", return_value=0):
            m.main(["block", "02:00:5e:be:ef:01"])
        self.assertFalse(poller._READONLY)

    def test_call_retries_on_admin_key_after_ro_403(self):
        poller._key_role, poller._api_key = "readonly", RO
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b""))
        ok = MagicMock()
        ok.__enter__ = lambda s: SimpleNamespace(status=200, read=lambda: b'{"data": []}')
        ok.__exit__ = lambda s, *a: False
        seen = []

        def opener(req, timeout=None):
            seen.append(req.headers.get("X-api-key"))
            if len(seen) == 1:
                raise err
            return ok

        def relogin():
            poller._api_key, poller._key_role = ADMIN, "admin"
            return True

        with patch.object(poller._opener, "open", side_effect=opener), \
                patch.object(poller, "_unifi_login", side_effect=relogin):
            status, body = m._call("GET", "/x")
        self.assertEqual(status, 200)
        self.assertEqual(seen, [RO, ADMIN])

    def test_write_403_on_admin_is_returned_not_retried(self):
        poller._key_role, poller._api_key = "admin", ADMIN
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b"nope"))
        with patch.object(poller._opener, "open", side_effect=err) as op:
            status, _ = m._call("POST", "/x", {"a": 1})
        self.assertEqual(status, 403)
        self.assertEqual(op.call_count, 1)

    def test_no_key_literal_in_source(self):
        src = (SCRIPTS / "nova_unifi_poller.py").read_text()
        self.assertNotRegex(src, r"X-API-Key\"\]\s*=\s*\"[A-Za-z0-9]{16,}")


if __name__ == "__main__":
    unittest.main()
