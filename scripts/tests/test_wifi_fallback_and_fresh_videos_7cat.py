#!/usr/bin/env python3
"""7-category tests for the 2026-10-20 cluster Wi-Fi fallback + fresh-video fixes.

Wi-Fi fallback: nova-core7's NIC died but the box stayed up on its Wi-Fi backdoor; work kept going to the dead
Ethernet IP. Now minion_nodes.ip follows each heartbeat's source address (nova_minion_hub.lan_addr), DNS
cluster names follow minion_nodes (nova_dns_sync.live_nodes, 60 s TTL, every 5 min), and service_registry rows
follow too (sync_registry, since host is inet).

Fresh videos: a June Watch Nicholas stream re-transcribed on 10-19 ran as "today's stream" and its loop junk
("I don't know. [loop x18]") was reported as speech. Roundups now skip chunks marked supersedes/skipping_needle.
Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_wifi_fallback_and_fresh_videos_7cat.py
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_dns_sync as dns  # noqa: E402
import nova_minion_hub as hub  # noqa: E402

SKIP = "NOT metadata ?| array['supersedes', 'skipping_needle']"


class Cur:
    def __init__(self, rows=(), prev=("idle", False, "minion")):
        self.rows, self.prev, self.sql, self.rowcount = list(rows), prev, [], 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.prev

    def fetchall(self):
        return self.rows


class Conn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur


HB = {"state": "idle", "ollama_url": None, "models": [], "info": {}}


class TestSecurity(unittest.TestCase):
    def test_only_lan_addresses_are_recorded(self):
        for bad in ("127.0.0.1", "8.8.8.8", "fe80::1", "", None, "192.168.1.1; DROP", "::1"):
            self.assertIsNone(hub.lan_addr(bad), bad)
        self.assertEqual(hub.lan_addr("192.168.1.197"), "192.168.1.197")

    def test_registry_update_is_parameterized(self):
        cur = Cur()
        dns.sync_registry(Conn(cur), {"nova-core7": "192.168.1.197"})
        sql, params = cur.sql[0]
        self.assertNotIn("192.168", sql)
        self.assertEqual(params, ("192.168.1.197", "nova-core7", "192.168.1.197"))


class TestPerformance(unittest.TestCase):
    def test_lan_addr_10k_fast(self):
        t = time.time()
        for _ in range(10000):
            hub.lan_addr("192.168.1.197")
        self.assertLess(time.time() - t, 1.0)


class TestRetry(unittest.TestCase):
    def test_loopback_heartbeat_keeps_the_known_ip(self):
        cur = Cur()
        hub.heartbeat(cur, "mac-studio", dict(HB), "127.0.0.1")
        upd = [p for s, p in cur.sql if s.startswith("UPDATE minion_nodes")][0]
        self.assertIsNone(upd[-2])          # coalesce(NULL, ip) keeps the stored address

    def test_registry_skips_loopback_rows(self):
        cur = Cur()
        dns.sync_registry(Conn(cur), {"nova-core": "192.168.1.2"})
        self.assertIn("NOT host <<= '127.0.0.0/8'::inet", cur.sql[0][0])


class TestUnit(unittest.TestCase):
    def test_heartbeat_records_source_address(self):
        cur = Cur()
        hub.heartbeat(cur, "nova-core7", dict(HB), "192.168.1.197")
        sql, params = [x for x in cur.sql if x[0].startswith("UPDATE minion_nodes")][0]
        self.assertIn("ip = coalesce(%s, ip)", sql)
        self.assertEqual(params[-2:], ("192.168.1.197", "nova-core7"))

    def test_node_ttl_is_short(self):
        self.assertLessEqual(dns.NODE_TTL, 60)


class TestIntegration(unittest.TestCase):
    def test_live_node_overrides_stale_unifi_record(self):
        cur = Cur(rows=[("nova-core7", "192.168.1.197")])
        stale = {"mac": "78:55:36:09:96:87", "ip": "192.168.1.125", "hostname": "nova-core7"}
        cur.fetchone = lambda: ("nova-core7", True)
        out = dns.build(Conn(cur), [stale])
        recs = [ip for f, ip in out if f == f"nova-core7.{dns.DOMAIN}"]
        self.assertEqual(recs, ["192.168.1.197"])


class TestFunctional(unittest.TestCase):
    def test_roundups_skip_retranscribed_and_loop_chunks(self):
        wat = (SCRIPTS / "nova_watches_and_friends.py").read_text()
        self.assertEqual(wat.count(SKIP), 2)                  # watch news + fishbowl
        self.assertIn("text NOT LIKE '%%[loop x%%'", wat)
        self.assertIn(SKIP, (SCRIPTS / "nova_friends.py").read_text())   # every "and Friends" daily


class TestFrame(unittest.TestCase):
    def test_dns_sync_runs_every_5_minutes(self):
        y = (SCRIPTS.parent / "config" / "scheduler.yaml").read_text()
        i = y.index("script: nova_dns_sync.py")
        self.assertIn("schedule: every 5m", y[i:i + 120])


if __name__ == "__main__":
    unittest.main()
