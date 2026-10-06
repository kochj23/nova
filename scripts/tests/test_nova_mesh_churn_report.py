#!/usr/bin/env python3
"""Tests for nova_mesh_churn_report.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2         # noqa: F401
import psycopg2.extras  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_mesh_churn_report.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="mesh_churn_test_"))

import nova_notify  # noqa: E402,F401


def _load():
    spec = importlib.util.spec_from_file_location("mcr", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("psycopg2.connect", side_effect=RuntimeError("offline")), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


mc = _load()
mc.LOG_FILE = TMP / "mesh_churn_report.log"
mc.notify = MagicMock(return_value=True)


def _resp(payload):
    r = MagicMock(); r.read.return_value = json.dumps(payload).encode()
    r.__enter__ = lambda s: s; r.__exit__ = lambda s, *a: False
    return r


def _node(i, heard=1_700_000_000):
    return {"id": f"!{i:08x}", "longName": f"Node {i}", "shortName": f"N{i}", "hwModel": "T114",
            "snr": 5.5, "hopsAway": 1, "lastHeard": heard, "batteryLevel": 90}


def _collect_conn(seen=False):
    cur = MagicMock(); cur.fetchone.return_value = (1,) if seen else None
    conn = MagicMock(); conn.cursor.return_value = cur
    return conn, cur


def _report_conn(new=(), gone=(), active=()):
    cur = MagicMock(); cur.fetchall.side_effect = [list(new), list(gone), list(active)]
    conn = MagicMock(); conn.cursor.return_value = cur
    return conn, cur


def _quiet():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", mc.DSN)

    def test_node_fields_are_bound_parameters(self):
        evil = "x'); SELECT pg_sleep(9);--"
        n = _node(1); n["longName"] = evil
        conn, cur = _collect_conn()
        with patch.object(mc.urllib.request, "urlopen", return_value=_resp({"nodes": [n]})), \
             patch.object(mc.psycopg2, "connect", return_value=conn), _quiet():
            mc.collect()
        ins = [c for c in cur.execute.call_args_list if "INSERT INTO telemetry.mesh_nodes" in c[0][0]][0]
        self.assertNotIn(evil, ins[0][0])
        self.assertIn(evil, ins[0][1])
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')


class TestPerformance(unittest.TestCase):
    def test_collect_10k_nodes_is_bounded(self):
        conn, cur = _collect_conn()
        t0 = time.perf_counter()
        with patch.object(mc.urllib.request, "urlopen", return_value=_resp({"nodes": [_node(i) for i in range(10_000)]})), \
             patch.object(mc.psycopg2, "connect", return_value=conn), _quiet():
            mc.collect()
        self.assertLess(time.perf_counter() - t0, 8.0)
        self.assertIn("10000 new sightings", mc.LOG_FILE.read_text())


class TestRetry(unittest.TestCase):
    def test_bridge_fails_twice_then_succeeds(self):
        conn, _ = _collect_conn()
        op = MagicMock(side_effect=[OSError("mdns"), OSError("mdns"), _resp({"nodes": [_node(1)]})])
        with patch.object(mc.urllib.request, "urlopen", op), patch.object(mc.time, "sleep") as sl, \
             patch.object(mc.psycopg2, "connect", return_value=conn), _quiet():
            mc.collect()
        self.assertEqual(op.call_count, 3)
        self.assertEqual(sl.call_count, 2)
        conn.commit.assert_called_once()

    def test_bridge_down_three_times_exits_clean_without_pg(self):
        op = MagicMock(side_effect=OSError("unplugged"))
        with patch.object(mc.urllib.request, "urlopen", op), patch.object(mc.time, "sleep"), \
             patch.object(mc.psycopg2, "connect") as pc, _quiet():
            mc.collect()
        self.assertEqual(op.call_count, 3)
        pc.assert_not_called()
        self.assertIn("unreachable after 3 attempts", mc.LOG_FILE.read_text())


class TestUnit(unittest.TestCase):
    def test_already_seen_sighting_is_skipped(self):
        conn, cur = _collect_conn(seen=True)
        with patch.object(mc.urllib.request, "urlopen", return_value=_resp({"nodes": [_node(1)]})), \
             patch.object(mc.psycopg2, "connect", return_value=conn), _quiet():
            mc.collect()
        self.assertFalse(any("INSERT" in c[0][0] for c in cur.execute.call_args_list))

    def test_missing_last_heard_is_none(self):
        n = _node(1); n.pop("lastHeard")
        conn, cur = _collect_conn()
        with patch.object(mc.urllib.request, "urlopen", return_value=_resp({"nodes": [n]})), \
             patch.object(mc.psycopg2, "connect", return_value=conn), _quiet():
            mc.collect()
        ins = [c for c in cur.execute.call_args_list if "INSERT" in c[0][0]][0]
        self.assertIsNone(ins[0][1][6])

    def test_empty_nodedb(self):
        conn, cur = _collect_conn()
        with patch.object(mc.urllib.request, "urlopen", return_value=_resp({})), \
             patch.object(mc.psycopg2, "connect", return_value=conn), _quiet():
            mc.collect()
        self.assertIn("0 nodes in NodeDB, 0 new", mc.LOG_FILE.read_text())


class TestIntegration(unittest.TestCase):
    def test_tables_and_shared_notify(self):
        self.assertIn("telemetry.mesh_nodes", mc.DDL)
        self.assertIn("INSERT INTO shared_observations", SRC)
        self.assertIn("from nova_notify import notify", SRC)
        self.assertTrue(mc.BRIDGE_NODES_URL.endswith("/nodes"))


class TestFunctional(unittest.TestCase):
    def test_report_writes_observations_and_posts(self):
        new = [{"node_id": "!a", "long_name": "Alpha"}]
        gone = [{"node_id": "!b", "long_name": None, "days_seen": 9, "last_heard": datetime(2026, 1, 1, 8, 0)}]
        active = [{"node_id": "!a", "long_name": "Alpha", "short_name": "A", "last_heard": datetime(2026, 1, 5),
                   "best_snr": 6.25, "min_hops": 0}]
        conn, cur = _report_conn(new, gone, active)
        mc.notify.reset_mock()
        with patch.object(mc.psycopg2, "connect", return_value=conn), _quiet():
            mc.report()
        obs = [c[0][1][0] for c in cur.execute.call_args_list if "shared_observations" in c[0][0]]
        self.assertEqual(len(obs), 2)
        self.assertIn("Alpha", obs[0]); self.assertIn("!b, last heard 2026-01-01 08:00", obs[1])
        body = mc.notify.call_args.kwargs["body"]
        self.assertIn("1 new, 1 gone; 1 node(s)", body)
        self.assertIn("+ Alpha (!a)", body); self.assertIn("- unnamed (!b)", body)
        self.assertIn("Alpha (!a), direct, SNR 6.2", body)
        self.assertEqual(mc.notify.call_args.kwargs["dedup_key"], "mesh-churn-daily")

    def test_report_notify_failure_is_logged_not_raised(self):
        conn, _ = _report_conn()
        with patch.object(mc, "notify", side_effect=RuntimeError("slack down")), \
             patch.object(mc.psycopg2, "connect", return_value=conn), _quiet():
            mc.report()
        self.assertIn("Notify failed: slack down", mc.LOG_FILE.read_text())
        conn.close.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--collect", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_mesh_churn_report"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
