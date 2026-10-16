#!/usr/bin/env python3
"""Tests for nova_service_deps.py (plan #59): depends_on seeding + the multi-service-down roll-up."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nova_service_deps as d  # noqa: E402

REG = {
    "plex@core": ["docker:core"], "searx@core": ["docker:core"], "graf@core": ["docker:core", "pg@core"],
    "pg@core": [], "bouncer@studio": ["pg@core"], "mem@studio": ["bouncer@studio", "redis@studio"],
    "redis@studio": [], "sched@studio": ["pg@core"],
}


class TestRollup(unittest.TestCase):
    def test_docker_engine_outage_is_one_root(self):
        r = d.rollup(["plex@core", "searx@core", "graf@core"], REG)
        self.assertEqual(r["roots"], ["docker:core"])
        self.assertEqual(set(r["explained"]), {"plex@core", "searx@core", "graf@core"})

    def test_whole_host_beats_docker(self):
        r = d.rollup(["plex@core", "searx@core", "graf@core", "pg@core", "bouncer@studio"], REG)
        self.assertIn("host:core", r["roots"])
        self.assertNotIn("docker:core", r["roots"])

    def test_transitive_chain_attributes_to_deepest_down(self):
        r = d.rollup(["pg@core", "bouncer@studio", "mem@studio", "sched@studio"], REG)
        self.assertEqual(r["roots"], ["pg@core"])
        self.assertEqual(r["explained"]["mem@studio"], "pg@core")

    def test_independent_failures_stay_separate(self):
        r = d.rollup(["redis@studio", "plex@core"], REG)
        self.assertEqual(r["roots"], ["plex@core", "redis@studio"])
        self.assertEqual(r["explained"], {})

    def test_single_service_node_is_not_a_host_outage(self):
        r = d.rollup(["x@solo"], {"x@solo": []})
        self.assertEqual(r["roots"], ["x@solo"])

    def test_cycle_safe(self):
        r = d.rollup(["a@n", "b@m"], {"a@n": ["b@m"], "b@m": ["a@n"]})
        self.assertTrue(r["roots"] or r["explained"])


class TestSeed(unittest.TestCase):
    def test_seed_uses_safe_ddl_and_parameterized_updates(self):
        cur = MagicMock(); cur.rowcount = 1
        with patch.object(d.nova_pg_schema, "safe_ddl") as ddl:
            n = d.seed(cur)
        self.assertIn("ADD COLUMN IF NOT EXISTS depends_on", ddl.call_args[0][1])
        self.assertEqual(n, len(d.VERIFIED))
        for c in cur.execute.call_args_list:
            self.assertIn("%s", c[0][0])

    def test_every_dep_is_well_formed(self):
        for deps in d.VERIFIED.values():
            for dep in deps:
                self.assertRegex(dep, r"^([\w-]+@[\w-]+|(host|docker):[\w-]+)$")

    def test_no_ips_in_edges(self):
        self.assertNotRegex(repr(d.VERIFIED), r"\d+\.\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
