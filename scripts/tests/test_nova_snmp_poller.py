#!/usr/bin/env python3
"""Tests for nova_snmp_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Daemon-safe: no port is bound, no poller loop is left running, no snmpget/Keychain process runs.
Loops are driven for exactly one iteration by making asyncio.sleep raise after the first pass."""
import asyncio
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_snmp_poller.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_snmp_test_"))


def _load():
    spec = importlib.util.spec_from_file_location("nsnmp", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("subprocess.run", side_effect=AssertionError("import must not shell out")):
        spec.loader.exec_module(mod)
    mod.LOG_FILE = TMP / "nova_snmp_poller.log"
    mod.nova_config = types.SimpleNamespace(post_both=MagicMock(), SLACK_BB="C_BB")
    mod._credentials_cache.clear()
    return mod


sp = _load()
DEV = {"ip": "192.168.1.250", "name": "test-box", "version": "v2c", "community_keychain": "k", "port": 161, "enabled": True}


class _Stop(Exception):
    pass


def _one_pass():
    """asyncio.sleep stand-in: first call (the start-up delay) returns, the next stops the loop."""
    calls = {"n": 0}
    async def sleep(_s):
        calls["n"] += 1
        if calls["n"] > 1:
            raise _Stop()
    return sleep


class _Conn:
    def __init__(self, fetch_rows=(), fetchval=None):
        self.fetch_rows = list(fetch_rows); self.fv = fetchval; self.calls = []
    async def fetch(self, q, *a): self.calls.append((q, a)); return self.fetch_rows
    async def fetchval(self, q, *a): self.calls.append((q, a)); return self.fv
    async def executemany(self, q, rows): self.calls.append((q, rows))
    async def execute(self, q, *a): self.calls.append((q, a)); return "DELETE 5"


class _Pool:
    def __init__(self, conn): self.conn = conn
    def acquire(self):
        conn = self.conn
        class Ctx:
            async def __aenter__(s): return conn
            async def __aexit__(s, *a): return False
        return Ctx()


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r'"-c",\s*"[^"]+"')                 # community never literal in an argv
        self.assertNotRegex(SRC, r"postgresql://\w+:[^@]+@")

    def test_community_comes_from_keychain_and_is_cached(self):
        sp._credentials_cache.clear()
        with patch.object(sp.subprocess, "run", return_value=MagicMock(returncode=0, stdout="s3cret\n")) as run:
            self.assertEqual(sp.get_credential("nova-snmp-community"), "s3cret")
            self.assertEqual(sp.get_credential("nova-snmp-community"), "s3cret")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args[0][0][:2], ["security", "find-generic-password"])
        sp._credentials_cache.clear()

    def test_metrics_api_query_is_parameterized(self):
        conn = _Conn([])
        req = types.SimpleNamespace(query={"device": "1.2.3.4'; DROP TABLE snmp_metrics;--", "name": "cpu", "limit": "99999"})
        async def go():
            with patch.object(sp, "get_pool", return_value=_Pool(conn)):
                return await sp.handle_metrics(req)
        asyncio.run(go())
        q, args = conn.calls[0]
        self.assertNotIn("DROP", q)
        self.assertEqual(args, ("1.2.3.4'; DROP TABLE snmp_metrics;--", "cpu%", 1000))   # limit clamped


class TestPerformance(unittest.TestCase):
    def test_parse_10k_values(self):
        vals = ["Timeticks: (12345) 0:02:03.45", "2:18:27:25.00", "3339584 kB", "42", "No Such Object", "up(1)"] * 1700
        t0 = time.perf_counter()
        parsed = [sp._parse_snmp_value(v) for v in vals]
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(parsed), len(vals))


class TestRetry(unittest.TestCase):
    def test_snmp_get_timeout_fails_open(self):
        # RETRY GAP: snmp_get — one snmpget per OID per cycle; a timeout yields None and the next poll retries
        sp._credentials_cache["k"] = "c"
        with patch.object(sp.subprocess, "run", side_effect=subprocess.TimeoutExpired("snmpget", 8)) as run:
            self.assertIsNone(asyncio.run(sp.snmp_get(DEV, "1.3.6.1.2.1.1.3.0")))
            self.assertEqual(asyncio.run(sp.snmp_walk(DEV, "1.3")), [])
            self.assertEqual(asyncio.run(sp.snmp_walk_index(DEV, "1.3")), {})
        self.assertEqual(run.call_count, 3)

    def test_keychain_failure_falls_back(self):
        sp._credentials_cache.clear()
        with patch.object(sp.subprocess, "run", return_value=MagicMock(returncode=44, stdout="")):
            self.assertEqual(sp.get_credential("missing"), "public")

    def test_notify_failure_is_logged(self):
        sp.nova_config.post_both.side_effect = RuntimeError("slack down")
        try:
            with redirect_stdout(io.StringIO()) as out:
                sp.notify("x")
        finally:
            sp.nova_config.post_both.side_effect = None
        self.assertIn("Notification failed: slack down", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_parse_values(self):
        self.assertEqual(sp._parse_snmp_value("Timeticks: (12345) 0:02:03.45"), 12345.0)
        self.assertEqual(sp._parse_snmp_value("1:00:00:00.00"), 86400 * 100)
        self.assertEqual(sp._parse_snmp_value("3339584 kB"), 3339584.0)
        self.assertIsNone(sp._parse_snmp_value(""))
        self.assertIsNone(sp._parse_snmp_value("No Such Instance"))
        self.assertIsNone(sp._parse_snmp_value("eth0"))

    def test_build_cmd_v2c_and_v3(self):
        sp._credentials_cache.update({"k": "comm", "nova-snmpv3-user": "u", "nova-snmpv3-auth": "a", "nova-snmpv3-priv": "p"})
        self.assertEqual(sp.build_snmp_cmd(DEV, "snmpget", "1.3"), ["snmpget", "-v2c", "-c", "comm", "-Oqv", "-t", "3",
                                                                    "192.168.1.250:161", "1.3"])
        v3 = sp.build_snmp_cmd({**DEV, "version": "v3"}, "snmpget", "1.3")
        self.assertEqual(v3[v3.index("-l") + 1], "authPriv")
        self.assertEqual((v3[v3.index("-A") + 1], v3[v3.index("-X") + 1]), ("a", "p"))
        sp._credentials_cache.clear()


class TestIntegration(unittest.TestCase):
    def test_poll_device_derives_cpu_used(self):
        async def go():
            sp._metrics_queue = asyncio.Queue()
            async def fake_get(dev, oid):
                return 30.0 if oid == sp.FAST_OIDS.get("cpu_idle_pct", {}).get("oid") else 7.0
            with patch.object(sp, "snmp_get", side_effect=fake_get):
                n = await sp.poll_device(DEV, sp.FAST_OIDS, "fast")
            return n, _drain(sp._metrics_queue)
        n, metrics = asyncio.run(go())
        self.assertEqual(n, len(metrics))
        names = {m["metric"] for m in metrics}
        if "cpu_idle_pct" in sp.FAST_OIDS:
            self.assertEqual([m["value"] for m in metrics if m["metric"] == "cpu_used_pct"], [70.0])
        self.assertIn("if_in_octets.0", names)                     # legacy per-iface metrics for non-walked hosts

    def test_interface_bps_from_counter_delta(self):
        col_oid, prefix, unit, is_counter = next(c for c in sp.IFACE_COLUMNS if c[3])
        sp._iface_prev.clear()
        tables = {sp.IFNAME_OID: {"1": "eth0", "2": "lo"}, sp.IFOPER_OID: {"1": "1", "2": "1"}}
        async def go(counter):
            sp._metrics_queue = asyncio.Queue()
            async def walk(dev, oid):
                if oid == col_oid:
                    return {"1": str(counter)}
                return tables.get(oid, {})
            with patch.object(sp, "snmp_walk_index", side_effect=walk):
                await sp.collect_interfaces(DEV, None)
            return _drain(sp._metrics_queue)
        with patch.object(sp.time, "time", side_effect=[1000.0]):
            first = asyncio.run(go(1000))
        with patch.object(sp.time, "time", side_effect=[1010.0]):
            second = asyncio.run(go(2250))
        self.assertFalse(any(m["unit"] == "bps" for m in first))
        bps = [m for m in second if m["unit"] == "bps"]
        self.assertEqual(bps[0]["value"], 1000.0)                  # 1250 bytes * 8 / 10 s
        self.assertFalse(any(m["metric"].endswith(".2") for m in second))   # loopback skipped


class TestFunctional(unittest.TestCase):
    def test_threshold_checker_alerts_once_then_resolves(self):
        sp._alert_state.clear(); sp._device_failures.clear()
        sp.nova_config.post_both.reset_mock()
        conn = _Conn([{"device_ip": "192.168.1.2", "device_name": "nova-core", "avg_val": 99.0}])
        sp._device_failures["192.168.1.1"] = 5
        async def run_once():
            with patch.object(sp, "get_pool", return_value=_Pool(conn)), patch.object(sp.asyncio, "sleep", side_effect=_one_pass()):
                try:
                    await sp.threshold_checker()
                except _Stop:
                    pass
        with redirect_stdout(io.StringIO()):
            asyncio.run(run_once())
            texts = [c[0][0] for c in sp.nova_config.post_both.call_args_list]
            self.assertTrue(any("CPU load critical: 99.0" in t for t in texts))
            self.assertTrue(any("udm-pro (192.168.1.1) unreachable" in t for t in texts))
            sp.nova_config.post_both.reset_mock()
            sp._device_failures["192.168.1.1"] = 0
            asyncio.run(run_once())
        texts = [c[0][0] for c in sp.nova_config.post_both.call_args_list]
        self.assertEqual([t for t in texts if "SNMP Resolved" in t and "udm-pro" in t].__len__(), 1)
        self.assertFalse(any("CPU load critical" in t for t in texts))          # no re-alert while still hot
        sp._alert_state.clear(); sp._device_failures.clear()

    def test_batch_writer_inserts_queued_metrics(self):
        conn = _Conn()
        async def go():
            sp._metrics_queue = asyncio.Queue()
            for i in range(3):
                await sp._metrics_queue.put({"ts": i, "ip": "ip", "name": "n", "metric": "m", "value": i,
                                             "oid": "o", "group": "fast", "unit": "u"})
            hits = {"n": 0}
            real_wait_for = asyncio.wait_for
            async def wait_for(coro, timeout):
                hits["n"] += 1
                if hits["n"] > 1:                      # second pass: flag shutdown so the loop exits cleanly
                    coro.close(); sp._shutdown = True
                    raise asyncio.TimeoutError()
                return await real_wait_for(coro, timeout)
            with patch.object(sp, "get_pool", return_value=_Pool(conn)), patch.object(sp.asyncio, "wait_for", side_effect=wait_for):
                await sp.batch_writer()
        try:
            asyncio.run(go())
        finally:
            sp._shutdown = False
        q, rows = conn.calls[0]
        self.assertIn("INSERT INTO snmp_metrics", q)
        self.assertEqual(len(rows), 3)


class TestFrame(unittest.TestCase):
    def test_import_smoke(self):
        # no --help: running it binds 0.0.0.0:37463 and starts pollers, so the frame check is the import
        code = "import sys; sys.path.insert(0, sys.argv[1]); import nova_snmp_poller as m; print(m.VERSION, m._shutdown)"
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), f"{sp.VERSION} False")

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    asyncio.run(main())', SRC)
        with patch.object(sp.asyncio, "run", side_effect=AssertionError("import must not start the daemon")):
            _load()


if __name__ == "__main__":
    unittest.main()
