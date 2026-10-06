#!/usr/bin/env python3
"""Tests for nova_vault7_segmentation_audit.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). Written by Jordan Koch (via Claude).

This is a READ-ONLY auditor: it changes NO network/VLAN/firewall config. The tests prove the
read-only contract, the --dry-run path (no DB write / no Slack), and that every outbound call is mocked."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_vault7_segmentation_audit.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("vault7_seg", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


va = _load()


def _rows(*specs):
    """specs: (mac, name, ip, essid, wired) — classify() derives class from name."""
    return [{"mac": m, "name": n, "ip": ip, "subnet": va._subnet(ip),
             "essid": e, "wired": wired, "cls": va.classify_device(n)} for m, n, ip, e, wired in specs]


FLAT = _rows(
    ("aa:1", "Jordan MacBook", "192.168.1.10", "KOCH-MAIN", False),
    ("aa:2", "Front Door Camera", "192.168.1.50", "KOCH-IOT", False),
    ("aa:3", "Living Room TV", "192.168.1.51", "KOCH-IOT", False),
    ("aa:4", "Hue plug", "192.168.1.60", "KOCH-IOT", False),
)


class _Cur:
    def __init__(self, net_rows): self.net_rows = net_rows; self.sql = []
    def execute(self, sql, params=None): self.sql.append((sql, params))
    def fetchall(self): return self.net_rows
    def fetchone(self): return None
    def close(self): pass


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_is_read_only_for_network_config(self):
        # the only write is the agent_docs report row; nothing touches network/VLAN/firewall/DNS
        inserts = re.findall(r"INSERT INTO\s+([\w.]+)", SRC)
        deletes = re.findall(r"DELETE FROM\s+([\w.]+)", SRC)
        self.assertEqual((inserts, deletes), (["agent_docs"], []))   # only write is the report row
        self.assertNotIn("subprocess", SRC)                          # never shells out to touch the network

    def test_report_row_is_parameterized(self):
        self.assertIsNone(re.search(r"execute\(\s*f[\"'].*(INSERT|UPDATE)", SRC))


class TestPerformance(unittest.TestCase):
    def test_build_report_10k_devices_fast(self):
        rows = _rows(*[("m%d" % i, "Jordan Mac" if i % 50 == 0 else "Camera %d" % i,
                        "192.168.1.%d" % (i % 254 + 1), "KOCH-IOT", False) for i in range(10_000)])
        t0 = time.perf_counter()
        report, summary, stats = va.build_report(rows)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertGreater(stats["coresident"], 0)


class TestRetry(unittest.TestCase):
    def test_slack_failure_never_loses_the_saved_report(self):
        # RETRY GAP: main/post_both — one attempt; a post failure is logged, the agent_docs write already committed
        cur = _Cur(_sql_rows(FLAT)); conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(va.psycopg2, "connect", return_value=conn), patch.object(sys, "argv", ["x"]), \
             patch.object(va.nova_config, "post_both", side_effect=RuntimeError("slack down")), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(va.main(), 0)
        conn.commit.assert_called_once()
        self.assertIn("Slack post failed", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_subnet_parsing(self):
        self.assertEqual(va._subnet("192.168.1.55"), "192.168.1.0/24")
        self.assertIsNone(va._subnet(""))
        self.assertIsNone(va._subnet("garbage"))

    def test_class_risk_priorities(self):
        self.assertEqual(va.CLASS_RISK["camera"][0], "P1")
        self.assertEqual(va.CLASS_RISK["smart-tv-av"][0], "P1")
        self.assertEqual(va.CLASS_RISK["iot"][0], "P2")
        self.assertIn("camera", va.UNTRUSTED)
        self.assertNotIn("trusted", va.UNTRUSTED)


class TestIntegration(unittest.TestCase):
    def test_flat_network_detected_from_shared_subnet(self):
        report, summary, stats = va.build_report(FLAT)
        self.assertTrue(stats["flat"])
        self.assertIn("FLAT NETWORK", report)
        self.assertEqual(stats["trusted_subnets"], ["192.168.1.0/24"])
        self.assertGreaterEqual(stats["by_class"]["camera"], 1)

    def test_separated_network_is_not_flat(self):
        rows = _rows(("a", "Jordan MacBook", "192.168.1.10", "KOCH-MAIN", False),
                     ("b", "Front Door Camera", "192.168.20.50", "KOCH-IOT", False))
        report, summary, stats = va.build_report(rows)
        self.assertFalse(stats["flat"])
        self.assertEqual(stats["coresident"], 0)

    def test_gather_queries_network_window(self):
        cur = _Cur(_sql_rows(FLAT)); conn = MagicMock(); conn.cursor.return_value = cur
        rows = va.gather(conn)
        self.assertIn("FROM telemetry.network", cur.sql[0][0])
        self.assertIn(va.ACTIVE_WINDOW, cur.sql[0][0])
        self.assertEqual(len(rows), len(FLAT))


class TestFunctional(unittest.TestCase):
    def test_dry_run_writes_nothing_and_posts_nothing(self):
        cur = _Cur(_sql_rows(FLAT)); conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(va.psycopg2, "connect", return_value=conn), patch.object(sys, "argv", ["x", "--dry-run"]), \
             patch.object(va.nova_config, "post_both") as post, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(va.main(), 0)
        post.assert_not_called()
        conn.commit.assert_not_called()
        self.assertFalse(any("INSERT INTO agent_docs" in s for s, _ in cur.sql))
        self.assertIn("SLACK SUMMARY", out.getvalue())

    def test_real_run_saves_report_and_posts(self):
        cur = _Cur(_sql_rows(FLAT)); conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(va.psycopg2, "connect", return_value=conn), patch.object(sys, "argv", ["x"]), \
             patch.object(va.nova_config, "post_both") as post, redirect_stdout(io.StringIO()):
            self.assertEqual(va.main(), 0)
        self.assertTrue(any("INSERT INTO agent_docs" in s for s, _ in cur.sql))
        conn.commit.assert_called_once()
        post.assert_called_once()
        self.assertIn("Segmentation Audit", post.call_args.args[0])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_vault7_segmentation_audit as m; print(m.ACTIVE_WINDOW)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "14 days")


def _sql_rows(rows):
    """Adapt classified dict rows into the RealDictCursor shape gather() reads."""
    return [{"client_mac": r["mac"], "client_name": r["name"], "ip": r["ip"],
             "essid": r["essid"], "is_wired": r["wired"]} for r in rows]


if __name__ == "__main__":
    unittest.main()
