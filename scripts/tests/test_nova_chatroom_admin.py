#!/usr/bin/env python3
"""Tests for nova_chatroom_admin.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_chatroom_admin.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


adm = _load("chatroom_admin_under_test", SCRIPT)
JORDAN = adm.ADMIN_EMAILS[0]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("xoxb-", SRC)

    def test_allowlists_are_closed_and_jordan_only(self):
        self.assertEqual(adm.ADMIN_EMAILS, [JORDAN])
        self.assertEqual(adm.CODE_EXEC_ALLOWED, [JORDAN])
        self.assertTrue(JORDAN.endswith("@gmail.com"))
        for probe in ("", "anyone@example.com", JORDAN + ".evil.net", "evil." + JORDAN, " " + JORDAN):
            self.assertFalse(adm.is_admin(probe), probe)
            self.assertFalse(adm.can_execute_code(probe), probe)

    def test_admin_page_is_served_from_a_constant_not_user_input(self):
        self.assertTrue(adm.ADMIN_HTML.startswith("<!DOCTYPE html>"))
        self.assertNotIn("{sender}", adm.ADMIN_HTML)
        self.assertNotIn("innerHTML = req", adm.ADMIN_HTML)
        self.assertNotIn("execute(", SRC)          # the console holds no SQL of its own

    def test_admin_api_endpoints_are_scoped_under_admin(self):
        for ep in re.findall(r"fetch\('(/[^']+)'", adm.ADMIN_HTML):
            self.assertTrue(ep.startswith("/admin/api/"), ep)


class TestPerformance(unittest.TestCase):
    def test_10k_auth_checks_are_fast(self):
        emails = [f"user{i}@example.com" for i in range(10_000)] + [JORDAN.upper()]
        t0 = time.perf_counter()
        admins = sum(adm.is_admin(e) for e in emails)
        execs = sum(adm.can_execute_code(e) for e in emails)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual((admins, execs), (1, 1))


class TestRetry(unittest.TestCase):
    def test_pure_gatekeepers_have_no_external_calls_to_retry(self):
        # RETRY GAP: none applicable — is_admin()/can_execute_code() are list lookups with no I/O; the module
        # does no HTTP/PG/subprocess work (the chatroom server owns persistence). Proven here: no I/O modules used.
        for mod in ("urllib", "requests", "psycopg2", "asyncpg", "subprocess", "aiohttp"):
            self.assertNotIn(f"import {mod}", SRC)
        with patch.object(adm, "ADMIN_EMAILS", []):
            self.assertFalse(adm.is_admin(JORDAN))        # an empty allowlist fails closed, never open
        self.assertTrue(adm.is_admin(JORDAN))

    def test_bad_input_types_fail_closed(self):
        for bad in (None, 42, ["a"]):
            with self.assertRaises(AttributeError):
                adm.is_admin(bad)


class TestUnit(unittest.TestCase):
    def test_is_admin_is_case_insensitive(self):
        self.assertTrue(adm.is_admin(JORDAN))
        self.assertTrue(adm.is_admin(JORDAN.upper()))
        self.assertFalse(adm.is_admin("nobody@example.com"))
        self.assertFalse(adm.is_admin(""))

    def test_can_execute_code(self):
        self.assertTrue(adm.can_execute_code(JORDAN.title()))
        self.assertFalse(adm.can_execute_code("nova@digitalnoise.net"))

    def test_allowlist_edits_take_effect_without_reload(self):
        with patch.object(adm, "CODE_EXEC_ALLOWED", adm.CODE_EXEC_ALLOWED + ["Dev@Example.com"]):
            self.assertTrue(adm.can_execute_code("dev@example.com"))
        self.assertFalse(adm.can_execute_code("dev@example.com"))


class TestIntegration(unittest.TestCase):
    def test_admin_implies_exec_but_not_vice_versa(self):
        for e in adm.ADMIN_EMAILS:
            self.assertTrue(adm.can_execute_code(e))
        self.assertTrue(set(adm.ADMIN_EMAILS) <= set(adm.CODE_EXEC_ALLOWED))

    def test_html_wires_every_handler_the_page_calls(self):
        html = adm.ADMIN_HTML
        for fn in ("renderStats", "renderUsers", "renderAllowed", "renderExec", "renderAccessLog", "renderMessages",
                   "kickUser", "banEmail", "addAllowed", "removeAllowed", "banUser"):
            self.assertIn(f"function {fn}(", html, fn)
        for key in ("stats", "users", "allowed_users", "exec_allowed", "access_log", "recent_messages"):
            self.assertIn(f"data.{key}", html)
        self.assertIn("/admin/api/status", html)

    def test_chatroom_is_the_only_consumer_and_imports_not_reimplements(self):
        src = (SCRIPTS / "nova_chatroom.py").read_text()
        if "nova_chatroom_admin" not in src:
            self.skipTest("chatroom does not wire the admin console on this build")
        self.assertNotIn("def is_admin", src)


class TestFunctional(unittest.TestCase):
    def test_golden_path_admin_request_is_let_through(self):
        email = "Cf-Access: " + JORDAN
        email = email.split(": ")[1].strip().lower()           # what the chatroom does with the CF header
        self.assertTrue(adm.is_admin(email))
        self.assertTrue(adm.can_execute_code(email))
        self.assertIn("Admin Console", adm.ADMIN_HTML)

    def test_error_path_herd_member_is_refused_everything(self):
        herd = "jules@example.com"
        self.assertFalse(adm.is_admin(herd))
        self.assertFalse(adm.can_execute_code(herd))


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        self.assertNotIn("__main__", SRC)      # library module: nothing to run, nothing to guard
        r = subprocess.run([sys.executable, "-c", "import nova_chatroom_admin as a; print(len(a.ADMIN_HTML) > 1000)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")

    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
