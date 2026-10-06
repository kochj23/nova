#!/usr/bin/env python3
"""Tests for nova_synology_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
import urllib.parse
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_synology_monitor.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_synology_monitor_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sm = _load()
sm.notify = mock.MagicMock()

SYSINFO = {"model": "RS1221+", "firmware_ver": "DSM 7.2.2", "sys_temp": 40}
UTIL = {"cpu": {"user_load": 5, "system_load": 3},
        "memory": {"memory_size": 4_000_000, "avail_real": 2_000_000, "real_usage": 40},
        "network": [{"tx": 100, "rx": 200}], "disk": [{"read_byte": 10, "write_byte": 20}]}
GOOD_STORAGE = {"volumes": [{"id": "volume_1", "status": "normal", "size": {"total": "100", "used": "50"}}],
                "storagePools": [{"id": "pool_1", "status": "normal"}],
                "disks": [{"id": "sata1", "status": "normal", "temp": 35, "smart_status": "normal"}]}
BAD_STORAGE = {"volumes": [{"id": "volume_1", "status": "attention", "size": {"total": "100", "used": "95"}}],
               "storagePools": [{"id": "pool_1", "status": "degraded"}],
               "disks": [{"id": "nvme0", "status": "normal", "temp": 65, "smart_status": "normal"},
                         {"id": "sata2", "status": "crashed", "temp": 56, "smart_status": "failing",
                          "exceed_bad_sector_thr": True}]}


class _Session:
    """Answers query() from a dict keyed by API name."""
    def __init__(self, answers):
        self.answers = answers; self.calls = []

    def query(self, api, version, method, extra_params=None):
        self.calls.append((api, version, method))
        a = self.answers.get(api)
        return a(version) if callable(a) else a


class _Env(unittest.TestCase):
    def setUp(self):
        sm.notify.reset_mock()
        self.td = tempfile.TemporaryDirectory()
        d = Path(self.td.name)
        ps = {"dir": mock.patch.object(sm, "STATE_DIR", d), "file": mock.patch.object(sm, "STATE_FILE", d / "state.json"),
              "snap": mock.patch.object(sm, "SNAPSHOT_FILE", d / "snaps.json"),
              "url": mock.patch.object(sm.urllib.request, "urlopen", side_effect=OSError("offline")),
              "out": mock.patch("sys.stdout", new_callable=io.StringIO)}
        self.m = {k: p.start() for k, p in ps.items()}
        self.dir = d
        self.addCleanup(lambda: ([p.stop() for p in ps.values()], self.td.cleanup()))


def _dsm(*payloads):
    rs = []
    for p in payloads:
        r = mock.MagicMock(); r.__enter__.return_value.read.return_value = json.dumps(p).encode()
        rs.append(r)
    return rs


class TestSecurity(_Env):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|passwd|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{8,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('_get_credential("nova-synology-password")', SRC)
        with mock.patch.object(sm.nova_config, "_keychain", side_effect=lambda s, required=False: f"v-{s}") as kc:
            self.assertEqual(sm.get_credentials(), ("v-nova-synology-username", "v-nova-synology-password"))
        self.assertEqual(kc.call_count, 2)
        with mock.patch.object(sm.nova_config, "_keychain", return_value=None):
            self.assertEqual(sm.get_credentials(), (None, None))

    def test_login_failure_never_logs_password(self):
        self.m["url"].side_effect = _dsm({"success": False, "error": {"code": 400}})
        with mock.patch.object(sm, "get_credentials", return_value=("nova", "Pa55-very-secret")):
            self.assertFalse(sm.SynoSession().login())
        self.assertNotIn("Pa55-very-secret", self.m["out"].getvalue())
        self.assertIn("Login failed (error: 400)", self.m["out"].getvalue())


class TestPerformance(_Env):
    def test_find_problems_on_10k_disks(self):
        st = {"disks": [{"id": f"sata{i}", "status": "normal", "temp": 30 + i % 30, "smart_status": "normal"}
                        for i in range(10_000)]}
        t0 = time.perf_counter()
        probs = sm.find_problems(None, None, st)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertTrue(all(p["category"] == "temperature" for p in probs))


class TestRetry(_Env):
    def test_session_119_relogs_in_once_then_succeeds(self):
        self.m["url"].side_effect = _dsm({"success": False, "error": {"code": 119}},
                                         {"success": True, "data": {"sid": "new"}},
                                         {"success": True, "data": {"ok": 1}})
        s = sm.SynoSession(); s.sid = "old"
        with mock.patch.object(sm, "get_credentials", return_value=("u", "p")):
            self.assertEqual(s.query("SYNO.Core.System", 3, "info"), {"ok": 1})
        self.assertEqual(s.sid, "new")
        self.assertEqual(self.m["url"].call_count, 3)

    def test_second_119_does_not_loop(self):
        self.m["url"].side_effect = _dsm({"success": False, "error": {"code": 119}},
                                         {"success": True, "data": {"sid": "new"}},
                                         {"success": False, "error": {"code": 119}})
        s = sm.SynoSession(); s.sid = "old"
        with mock.patch.object(sm, "get_credentials", return_value=("u", "p")):
            self.assertIsNone(s.query("SYNO.Core.System", 3, "info"))
        self.assertEqual(self.m["url"].call_count, 3)

    def test_http_failure_and_vector_fail_open(self):
        # RETRY GAP: _raw_get / vector_remember — one attempt each; None / silent on failure
        s = sm.SynoSession(); s.sid = "x"
        self.assertIsNone(s.query("SYNO.Core.System", 3, "info"))
        self.assertIsNone(sm.vector_remember("t"))


class TestUnit(_Env):
    def test_find_problems_bad_storage(self):
        probs = sm.find_problems({"sys_tempwarn": True, "sys_temp": 80}, {"cpu": {"user_load": 70, "system_load": 20},
                                 "memory": {"memory_size": 100, "avail_real": 5, "real_usage": 95}}, BAD_STORAGE)
        cats = sorted(p["category"] for p in probs)
        self.assertEqual(cats, ["cpu", "disk", "disk", "disk", "memory", "raid", "storage", "temperature",
                                "temperature", "temperature", "volume"])
        self.assertEqual(sm.find_problems(SYSINFO, UTIL, GOOD_STORAGE), [])
        self.assertEqual(sm.find_problems(None, None, None), [])

    def test_formatters(self):
        self.assertEqual(sm._fmt_bytes(None), "N/A")
        self.assertEqual(sm._fmt_bytes(1536), "1.5 KB")
        self.assertEqual(sm._fmt_uptime("610:53:47"), "25d 10h 53m")
        self.assertEqual(sm._fmt_uptime(90061), "1d 1h 1m")
        self.assertEqual(sm._fmt_uptime(""), "unknown")
        self.assertEqual(sm._pct_bar(50, 4), "[##..] 50.0%")

    def test_slack_post_levels(self):
        sm.slack_post("*Backup Alert*\n  !! failed")
        self.assertEqual(sm.notify.call_args[1]["level"], "critical")
        self.assertEqual(sm.notify.call_args[0][0], "Backup Alert")
        sm.slack_post("*Synology NAS Alert*\n*Warnings:*\n  ! hot")
        self.assertEqual(sm.notify.call_args[1]["level"], "warning")
        self.assertEqual(sm.notify.call_args[1]["dedup_key"], "synology-monitor")


class TestIntegration(_Env):
    def test_backup_api_version_probe_falls_through(self):
        s = _Session({"SYNO.Backup.Task": None, "SYNO.Backup.Repository": lambda v: {"v": v} if v == 2 else None})
        self.assertEqual(sm.get_backup_tasks(s), {"v": 2})
        self.assertEqual([c[:2] for c in s.calls], [("SYNO.Backup.Task", 1), ("SYNO.Backup.Task", 2),
                                                    ("SYNO.Backup.Task", 3), ("SYNO.Backup.Repository", 1),
                                                    ("SYNO.Backup.Repository", 2)])

    def test_state_json_roundtrip_atomic(self):
        p = self.dir / "sub" / "x.json"
        sm._save_json(p, {"a": 1})
        self.assertEqual(sm._load_json(p), {"a": 1})
        self.assertFalse(p.with_suffix(".tmp").exists())
        p.write_text("{corrupt")
        self.assertEqual(sm._load_json(p), {})


class TestFunctional(_Env):
    def _session(self, storage, backups=None):
        return _Session({"SYNO.Core.System": SYSINFO, "SYNO.Core.System.Utilization": UTIL,
                         "SYNO.Storage.CGI.Storage": storage, "SYNO.Backup.Task": backups})

    def test_full_check_alerts_once_then_cooldown(self):
        sm.full_check(self._session(BAD_STORAGE, {"task_list": [{"name": "Offsite", "last_result": "failed"}]}))
        titles = [c[0][0] for c in sm.notify.call_args_list]
        self.assertTrue(titles[0].startswith("Synology NAS Alert — RS1221+"))
        self.assertEqual(titles[1], "Backup Alert")
        state = json.loads((self.dir / "state.json").read_text())
        self.assertEqual(state["problem_count"], 9)          # 8 storage findings + the failed backup
        self.assertEqual((state["net_tx_bps"], state["disk_write_bps"]), (100, 20))
        dedup = json.loads((self.dir / "synology_alert_dedup.json").read_text())
        self.assertIn("volume", dedup)
        sm.notify.reset_mock()
        storage_only = {"volumes": BAD_STORAGE["volumes"], "storagePools": BAD_STORAGE["storagePools"]}
        sm.full_check(self._session(storage_only))
        sm.notify.assert_not_called()                     # volume/storage/raid still in 6h cooldown

    def test_healthy_check_posts_nothing_and_dead_api_alerts(self):
        sm.full_check(self._session(GOOD_STORAGE))
        sm.notify.assert_not_called()
        self.assertEqual(json.loads((self.dir / "state.json").read_text())["problem_count"], 0)
        sm.full_check(_Session({}))
        self.assertEqual(sm.notify.call_args[1]["level"], "critical")

    def test_main_auth_failure_exits_1_and_alerts(self):
        with mock.patch.object(sm, "get_credentials", return_value=(None, None)), \
                mock.patch.object(sys, "argv", ["x"]), self.assertRaises(SystemExit) as cm:
            sm.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("Cannot authenticate", sm.notify.call_args[1]["body"])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(PATH), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--snapshot", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_synology_monitor"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
