#!/usr/bin/env python3
"""Tests for nova_capacity.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
asyncpg (a fake pool), ssh/vm_stat/df (subprocess.run), Slack (nova_config stub on the loaded module),
aiohttp's AppRunner/TCPSite and signal handlers are all mocked: no port is bound, no host is touched."""
import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_capacity.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="capacity_"))


def _load():
    spec = importlib.util.spec_from_file_location("nova_capacity_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.nova_config = types.SimpleNamespace(post_both=MagicMock(), SLACK_BB="C_TEST")
    mod.LOG_FILE = TMP / "cap.log"
    mod.print = lambda *a, **k: None
    return mod


cap = _load()
VMSTAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               1000.
Pages active:                             2000.
Pages inactive:                           1000.
Pages speculative:                           0.
Pages wired down:                         1000.
Pages occupied by compressor:                0.
Bogus line without number:              abc.
"""
DF_MAC = """Filesystem 1G-blocks Used Available Capacity iused ifree %iused Mounted on
/dev/disk3s1 1000 900 100 90% 1 2 0% /
/dev/disk5 2000 200 1800 10% 1 2 0% /Volumes/My Disk
/dev/zero 0 0 0 0% 0 0 0% /dev
"""
DF_LINUX = """Filesystem 1G-blocks Used Available Use% Mounted on
/dev/sda1 100G 50G 50G 50% /
tmpfs 1G 0G 1G 0% /run
"""


def _proc(out="", rc=0):
    return SimpleNamespace(stdout=out, returncode=rc, stderr="")


class _Conn:
    def __init__(self, cpu=None, mem=None, rules=()):
        self.fetchrow = AsyncMock(side_effect=lambda sql, *a: mem if "mem_total_real" in sql else cpu)
        self.fetch = AsyncMock(return_value=list(rules))
        self.execute = AsyncMock(return_value="DELETE 0")


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


def _run(coro, conn):
    cap._pool = _Pool(conn)
    try:
        return asyncio.run(coro)
    finally:
        cap._pool = None


class _Base(unittest.TestCase):
    def setUp(self):
        cap._active_alerts.clear()
        cap._latest_snapshot.clear()
        cap.nova_config.post_both.reset_mock()
        cap._shutdown = False


class TestSecurity(_Base):
    def test_no_credentials_and_positional_sql(self):
        self.assertIsNone(re.search(r"(password|token|secret)\s*=\s*['\"]", SRC, re.I))
        self.assertIsNone(re.search(r"(execute|fetch|fetchrow)\(\s*f[\"']", SRC))
        self.assertIn("WHERE device_name=$1", SRC)

    def test_ssh_is_batch_mode_argv(self):
        with patch.object(cap.subprocess, "run", return_value=_proc(VMSTAT)) as run:
            cap.get_remote_memory("10.0.0.5")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:5], ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes"])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_history_hours_clamped_and_device_bound(self):
        conn = _Conn()
        req = SimpleNamespace(query={"device": "x' OR 1=1--", "hours": "99999"})
        _run(cap.handle_history(req), conn)
        sql, *params = conn.fetch.call_args[0]
        self.assertNotIn("OR 1=1", sql)
        self.assertEqual(params[0].total_seconds(), 720 * 3600)
        self.assertEqual(params[1], "x' OR 1=1--")


class TestPerformance(_Base):
    def test_parse_large_df_fast(self):
        lines = "\n".join(f"/dev/d{i} 100 {i % 100} 1 {i % 100}% 1 2 0% /m{i}" for i in range(10_000))
        with patch.object(cap.subprocess, "run", return_value=_proc("hdr\n" + lines)):
            t0 = time.perf_counter()
            disks = cap.get_local_disk()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(disks), 10_000)


class TestRetry(_Base):
    def test_collectors_fail_open(self):
        # RETRY GAP: get_local_memory/get_remote_memory/get_*_disk — one subprocess call; failure -> None/[]
        with patch.object(cap.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 10)) as run:
            self.assertEqual(cap.get_local_memory(), (None, None, None))
            self.assertEqual(cap.get_remote_memory("h"), (None, None, None))
            self.assertEqual(cap.get_remote_disk("h"), [])
            self.assertEqual(cap.get_linux_disk("h"), [])
        self.assertEqual(run.call_count, 4)

    def test_one_host_failure_does_not_stop_the_loop(self):
        calls = []

        async def build(name, ip):
            calls.append(name)
            if name == "mac-studio":
                raise RuntimeError("pg")
            return {"device_name": name}

        async def fake_sleep(s):
            if s == cap.SNAPSHOT_INTERVAL:
                cap._shutdown = True
        with patch.object(cap, "build_snapshot", side_effect=build), patch.object(cap, "evaluate_alerts", AsyncMock()), \
             patch.object(cap.asyncio, "sleep", side_effect=fake_sleep):
            asyncio.run(cap.snapshot_loop())
        self.assertEqual(len(calls), len(cap.MONITORED_HOSTS))
        self.assertIn("Snapshot failed for mac-studio", cap.LOG_FILE.read_text())

    def test_notify_failure_logged(self):
        with patch.object(cap.nova_config, "post_both", side_effect=RuntimeError("slack")):
            cap.notify("x")
        self.assertIn("Notification failed", cap.LOG_FILE.read_text())


class TestUnit(_Base):
    def test_parse_vm_stat(self):
        total, used, free = cap._parse_vm_stat(VMSTAT, 16384)
        self.assertAlmostEqual(total, 5000 * 16384 / 1048576)
        self.assertAlmostEqual(used, 3000 * 16384 / 1048576)
        self.assertEqual(cap._parse_vm_stat("", 4096), (0.0, 0.0, 0.0))

    def test_disk_parsers(self):
        with patch.object(cap.subprocess, "run", return_value=_proc(DF_MAC)):
            mac = cap.get_local_disk()
        self.assertEqual([d["mount"] for d in mac], ["/", "/Volumes/My Disk"])
        with patch.object(cap.subprocess, "run", return_value=_proc(DF_LINUX)):
            lin = cap.get_linux_disk("h")
        self.assertEqual(lin[0], {"mount": "/", "size_gb": 100, "used_gb": 50, "avail_gb": 50, "percent": 50})

    def test_status_uses_load_per_core_and_swap_gate(self):
        cpu = {"load1": 20, "load5": 20, "load15": 20}         # nova-core 16 cores -> 1.25x: ok
        mem = {"total": 1024 * 1000, "avail": 1024 * 10, "buffer": 0, "cached": 0, "swap_total": 100, "swap_avail": 90}
        with patch.object(cap, "get_linux_disk", return_value=[{"mount": "/", "percent": 40}]):
            snap = _run(cap.build_snapshot("nova-core", "192.168.1.2"), _Conn(cpu=cpu, mem=mem))
        self.assertEqual(snap["overall_status"], "ok")         # 1% free but swap untouched -> cache, not pressure
        mem["swap_avail"] = 10
        with patch.object(cap, "get_linux_disk", return_value=[{"mount": "/", "percent": 40}]):
            snap = _run(cap.build_snapshot("nova-core", "192.168.1.2"), _Conn(cpu=cpu, mem=mem))
        self.assertEqual(snap["overall_status"], "crit")


class TestIntegration(_Base):
    def test_mac_snapshot_filters_mounts_and_inserts(self):
        conn = _Conn(cpu={"load1": 1, "load5": 70, "load15": 1})   # 70/32 = 2.19x -> crit
        with patch.object(cap, "get_local_memory", return_value=(100.0, 50.0, 50.0)), \
             patch.object(cap, "get_local_disk", return_value=[{"mount": "/", "percent": 50},
                                                               {"mount": "/private/var/vm", "percent": 99}]):
            snap = _run(cap.build_snapshot("mac-studio", "127.0.0.1"), conn)
        self.assertEqual(snap["disk_worst_pct"], 50)
        self.assertEqual(snap["overall_status"], "crit")
        sql, *vals = conn.execute.call_args[0]
        self.assertIn("INSERT INTO capacity_snapshots", sql)
        self.assertEqual(len(vals), 14)

    def test_alert_fires_once_then_resolves(self):
        rule = {"id": 1, "name": "disk", "metric": "disk_percent", "condition": "gt", "threshold": 80, "severity": "critical"}
        conn = _Conn(rules=[rule])
        snap = {"device_name": "h", "disk_worst_pct": 90, "cpu_load_5m": 0, "mem_headroom_pct": 50}
        _run(cap.evaluate_alerts(snap), conn)
        _run(cap.evaluate_alerts(snap), conn)
        self.assertEqual(cap.nova_config.post_both.call_count, 1)
        self.assertIn("Capacity Alert", cap.nova_config.post_both.call_args[0][0])
        _run(cap.evaluate_alerts(dict(snap, disk_worst_pct=10)), conn)
        self.assertIn("Resolved", cap.nova_config.post_both.call_args[0][0])

    def test_mem_alert_skipped_for_cache_hosts(self):
        rule = {"id": 2, "name": "mem", "metric": "mem_headroom_pct", "condition": "lt", "threshold": 10, "severity": "warn"}
        _run(cap.evaluate_alerts({"device_name": "synology-nas", "mem_headroom_pct": 1}), _Conn(rules=[rule]))
        cap.nova_config.post_both.assert_not_called()


class TestFunctional(_Base):
    def test_capacity_endpoint_rolls_up_worst_status(self):
        cap._latest_snapshot.update({
            "mac-studio": {"device_name": "mac-studio", "device_ip": "127.0.0.1", "overall_status": "warn",
                           "cpu_load_5m": 1, "cpu_cores": 32, "cpu_headroom_pct": 90, "mem_total_mb": 1,
                           "mem_used_mb": 1, "mem_free_mb": 0, "mem_headroom_pct": 0, "disks": [], "disk_worst_pct": 0}})
        body = json.loads(asyncio.run(cap.handle_capacity(None)).text)
        self.assertEqual(body["overall_status"], "warn")
        self.assertEqual(len(body["hosts"]), 1)
        health = json.loads(asyncio.run(cap.handle_health(None)).text)
        self.assertEqual(health["hosts_monitored"], len(cap.MONITORED_HOSTS))

    def test_main_never_binds_a_real_port(self):
        runner, site = MagicMock(), MagicMock()
        runner.setup, runner.cleanup, site.start = AsyncMock(), AsyncMock(), AsyncMock()

        async def stop(_):
            cap._shutdown = True
        with patch.object(cap, "signal"), patch.object(cap.web, "AppRunner", return_value=runner), \
             patch.object(cap.web, "TCPSite", return_value=site) as ts, \
             patch.object(cap, "snapshot_loop", AsyncMock()), patch.object(cap, "retention_purge", AsyncMock()), \
             patch.object(cap.asyncio, "sleep", side_effect=stop):
            asyncio.run(cap.main())
        ts.assert_called_once_with(runner, cap.BIND_ADDR, cap.HTTP_PORT)
        site.start.assert_awaited_once()
        runner.cleanup.assert_awaited_once()
        self.assertIn("Capacity Monitor", cap.nova_config.post_both.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_capacity"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
