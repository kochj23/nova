#!/usr/bin/env python3
"""Tests for nova_operations_security.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
PG, the LLM, image generation, Hugo publish, git push and Slack (all via nova_journal) are mocked at module
load: nothing is published."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_operations_security.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("operations_security_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


osec = _load()
# module-level stubs: nova_journal (LLM/publish/git/Slack), voice and PG never touch the world
osec.nj = MagicMock()
osec.nj.call_openrouter.return_value = "TITLE: Quiet Night On The Rack\n\nAll clean."
osec.nj.grafana_panel_image.return_value = None
osec.nj.today_str.return_value = "2026-01-01"
osec.nova_voice = MagicMock()
osec.nova_voice.system_prompt.return_value = "SYSTEM"
osec.psycopg2 = MagicMock()
osec.psycopg2.connect.side_effect = OSError("offline: pg stubbed")
_HIST = types.ModuleType("nova_article_history"); _HIST.recent_articles_context = lambda s: ""


class _Cur:
    """Answers each query by a substring key; records every SQL."""
    def __init__(self, answers):
        self.answers = answers; self.sql = []; self._rows = []
        self.connection = MagicMock()

    def execute(self, sql, args=None):
        self.sql.append(sql)
        self._rows = next((v for k, v in self.answers.items() if k in sql), [])
        if isinstance(self._rows, Exception):
            raise self._rows

    def fetchall(self):
        return self._rows


NET = [("Amys-iPhone", "10.0.0.5", False, "AP-Living"), ("exterior---front-door", "10.0.0.9", True, "SW-1"),
       ("mac-studio", "10.0.0.6", True, "SW-1"), ("\x03", None, True, "—")]


def _answers():
    return {"telemetry.network": NET,
            "package_audit_hosts": [("nova-core", 900, 3, True, "01-01 05:00"), ("pi", 0, 0, False, "01-01 04:00")],
            "FROM package_audit": [("nova-core", "openssl", "3.0.1", "3.0.2", "apt"), ("nova-core", "vim", "1", "2", "apt")],
            "hardware_inventory_hosts": [("nova-core", 4, 1, True)],
            "security_scan_results": [("nova-core", "rkhunter", "clean", "[]")],
            "FROM security_events WHERE": [(12, "sshd: auth failure")]}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", osec.OPS_DSN)

    def test_household_names_redacted(self):
        self.assertEqual(osec._scrub_name("Amys-iPhone"), "iPhone")
        self.assertEqual(osec._scrub_name("Dylan's_Room-2"), "Room-2")
        self.assertEqual(osec._scrub_name("amy"), "(unnamed)")
        self.assertEqual(osec._scrub_name("\x03"), "(unnamed)")

    def test_cameras_collapsed_not_listed(self):
        devices, cams, infra = osec.get_inventory(_Cur(_answers()))
        names = [d["name"] for d in devices]
        self.assertEqual(cams, 1)
        self.assertFalse(any("exterior" in n or "Amy" in n for n in names))
        self.assertEqual(infra, ["AP-Living", "SW-1"])

    def test_sql_never_interpolates(self):
        self.assertNotRegex(SRC, r"execute\(\s*f[\"']")
        self.assertNotRegex(SRC, r"q\(\w+,\s*f[\"']")


class TestPerformance(unittest.TestCase):
    def test_inventory_10k_devices(self):
        rows = [(f"device-{i}", f"10.0.{i // 250}.{i % 250}", i % 2 == 0, f"AP-{i % 7}") for i in range(10_000)]
        t0 = time.perf_counter()
        devices, cams, infra = osec.get_inventory(_Cur({"telemetry.network": rows}))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual((len(devices), len(infra)), (10_000, 7))


class TestRetry(unittest.TestCase):
    def test_query_failure_rolls_back_and_returns_empty(self):
        # RETRY GAP: q() — one attempt; a failing query rolls back and yields [] (report degrades, never crashes)
        cur = _Cur({"package_audit_hosts": RuntimeError("relation does not exist")})
        self.assertEqual(osec.q(cur, "SELECT 1 FROM package_audit_hosts"), [])
        cur.connection.rollback.assert_called_once()

    def test_advisories_fail_open_when_memories_db_down(self):
        # RETRY GAP: get_security_advisories — single connect; failure returns ([], [])
        self.assertEqual(osec.get_security_advisories(), ([], []))


class TestUnit(unittest.TestCase):
    def test_is_camera(self):
        for n in ("interior---kitchen", "Nest-Cam", "G4 Doorbell", "UVC-G3"):
            self.assertTrue(osec._is_camera(n), n)
        self.assertFalse(osec._is_camera("mac-studio"))
        self.assertFalse(osec._is_camera(None))

    def test_package_audit_notable_first(self):
        summary, exposure = osec.get_package_audit(_Cur(_answers()))
        self.assertIn("900 packages installed across 1 reachable hosts; 3 updates pending", summary)
        self.assertIn("Unreachable: pi", summary)
        self.assertTrue(exposure.startswith("- openssl 3.0.1 -> 3.0.2"))
        self.assertIn("no software audit yet", osec.get_package_audit(_Cur({}))[0])

    def test_advisories_split_by_gear(self):
        rows = [("Synology DSM CVE-2026-1 RCE",), ("Cisco ASA CVE-2026-2",), ("[arXiv] apple exploit study",),
                ("Synology DSM CVE-2026-1 RCE",)]
        conn = MagicMock(); conn.cursor.return_value = _Cur({"FROM memories": rows})
        with patch.object(osec.psycopg2, "connect", return_value=conn, side_effect=None):
            mine, broad = osec.get_security_advisories()
        self.assertEqual(mine, ["Synology DSM CVE-2026-1 RCE"])
        self.assertEqual(broad, ["Cisco ASA CVE-2026-2", "[arXiv] apple exploit study"])


class TestIntegration(unittest.TestCase):
    def test_uses_journal_and_voice_helpers(self):
        self.assertIn("import nova_journal as nj", SRC)
        self.assertIn('nova_voice.system_prompt(ctx, section="security")', SRC)
        self.assertEqual(osec.get_hardware_inventory(_Cur({})), "(no hardware inventory yet — nova_hw_inventory hasn't populated it)")


class TestFunctional(unittest.TestCase):
    def _main(self, answers):
        conn = MagicMock(); conn.cursor.return_value = _Cur(answers)
        for m in (osec.nj.publish_hugo, osec.nj.git_push, osec.nj.notify_slack, osec.nj.call_openrouter):
            m.reset_mock()
        with patch.object(osec.psycopg2, "connect", return_value=conn, side_effect=None), \
             patch.dict(sys.modules, {"nova_article_history": _HIST}):
            return osec.main()

    def test_golden_path_publishes_scrubbed_report(self):
        self.assertEqual(self._main(_answers()), 0)
        user = osec.nj.call_openrouter.call_args[0][1]
        self.assertIn("UniFi Protect Cameras ×1", user)
        self.assertNotIn("Amy", user)
        self.assertNotIn("front-door", user)
        self.assertEqual(osec.nj.publish_hugo.call_args[0][:3], ("Quiet Night On The Rack", "All clean.", "operations"))
        osec.nj.git_push.assert_called_once()
        osec.nj.notify_slack.assert_called_once()

    def test_no_data_aborts_without_publishing(self):
        self.assertEqual(self._main({}), 1)
        osec.nj.call_openrouter.assert_not_called()
        osec.nj.publish_hugo.assert_not_called()

    def test_empty_llm_aborts(self):
        osec.nj.call_openrouter.return_value = ""
        try:
            conn = MagicMock(); conn.cursor.return_value = _Cur(_answers())
            osec.nj.publish_hugo.reset_mock()
            with patch.object(osec.psycopg2, "connect", return_value=conn, side_effect=None), \
                 patch.dict(sys.modules, {"nova_article_history": _HIST}):
                self.assertEqual(osec.main(), 1)
            osec.nj.publish_hugo.assert_not_called()
        finally:
            osec.nj.call_openrouter.return_value = "TITLE: Quiet Night On The Rack\n\nAll clean."


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help/--selftest: running the script writes + publishes the article, so import is the smoke test
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_operations_security as m; print(callable(m.main))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
