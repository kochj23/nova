#!/usr/bin/env python3
"""Tests for nova_spinnaker.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_spinnaker as SP  # noqa: E402

SRC = (SCRIPTS / "nova_spinnaker.py").read_text()


class TestSecurity(unittest.TestCase):
    def test_no_secrets_no_network(self):
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")
        self.assertNotIn("urlopen", SRC)
        self.assertNotIn("psycopg2", SRC)

    def test_motive_sources_never_count_as_sensors(self):
        # Nova cannot corroborate herself: reasoning + prediction + llm is still zero sensors
        a = SP.assess({"sources": [{"id": "nova:reasoning"}, {"id": "nova:prediction:self"}, {"id": "llm:x"}]})
        self.assertEqual(a["independent"], 0)
        self.assertEqual(a["verdict"], "UNCORROBORATED")
        self.assertFalse(SP.may_trigger({"sources": [{"id": "nova:reasoning"}]}, "ask")[0])

    def test_unknown_rung_treated_as_most_invasive(self):
        ok, _ = SP.may_trigger({"sources": [{"id": "camera:a"}]}, "launch-the-missiles")
        self.assertFalse(ok)


class TestPerformance(unittest.TestCase):
    def test_grouping_200_sources_fast(self):
        srcs = [{"id": f"camera:c{i}"} for i in range(100)] + [{"id": f"scanner:tg{i}"} for i in range(100)]
        t = time.monotonic()
        a = SP.assess({"sources": srcs})
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertEqual(a["independent"], 101)   # 100 cameras share one NVR; 100 talkgroups are distinct

    def test_wire_detection_10k(self):
        t = time.monotonic()
        for i in range(10000):
            SP.wire_of(f"story {i} (AP) — something happened")
        self.assertLess(time.monotonic() - t, 2.0)


class TestRetry(unittest.TestCase):
    # RETRY GAP: none — pure library with no external calls. Prove it fails safe on garbage input.
    def test_garbage_input_fails_safe(self):
        for item in ({}, {"sources": None}, {"sources": [None, {}, {"id": ""}]}, {"sources": [{"id": 5}]}):
            a = SP.assess(item)
            self.assertIn(a["verdict"], ("UNCORROBORATED", "SINGLE_SOURCE"))


class TestUnit(unittest.TestCase):
    def test_two_cameras_one_nvr(self):
        a = SP.assess({"sources": [{"id": "camera:front_yard"}, {"id": "camera:front_door"}]})
        self.assertEqual(a["independent"], 1)
        self.assertTrue(a["shared_upstream"])
        self.assertEqual(a["verdict"], "SINGLE_SOURCE")

    def test_two_articles_one_wire_story(self):
        a = SP.assess({"sources": [{"id": "news:LA Times", "text": "LOS ANGELES (AP) — x"},
                                   {"id": "news:KTLA", "text": "the Associated Press said"}]})
        self.assertEqual(a["independent"], 1)
        self.assertIn("wire:ap", a["shared_upstream"][0]["shared"])

    def test_udm_witnesses_collapse_arp_does_not(self):
        self.assertEqual(SP.assess({"sources": [{"id": "network:unifi"}, {"id": "network:dhcp"}]})["independent"], 1)
        self.assertEqual(SP.assess({"sources": [{"id": "network:unifi"}, {"id": "network:arp"}]})["independent"], 2)

    def test_single_source_with_motive(self):
        a = SP.assess({"sources": [{"id": "news:Vendor Blog", "motive": "sells the product"}]})
        self.assertEqual(a["verdict"], "UNCORROBORATED")
        self.assertEqual(a["max_rung"], "journal")

    def test_expectation_raises_bar(self):
        item = {"sources": [{"id": "camera:alley_north"}, {"id": "scanner:Burbank PD"}]}
        self.assertEqual(SP.assess(item)["verdict"], "CORROBORATED")
        self.assertEqual(SP.assess(dict(item, expected=True))["verdict"], "SINGLE_SOURCE")
        item3 = {"sources": item["sources"] + [{"id": "adsb:N123"}], "expected": True}
        self.assertEqual(SP.assess(item3)["verdict"], "CORROBORATED")

    def test_contested(self):
        a = SP.assess({"sources": [{"id": "camera:a"}, {"id": "scanner:x"}],
                       "contradicted_by": [{"id": "presence:mmwave"}]})
        self.assertEqual(a["verdict"], "CONTESTED")
        self.assertEqual(a["max_rung"], "ask")

    def test_same_talkgroup_is_one_dispatcher(self):
        a = SP.assess({"sources": [{"id": "scanner:Burbank PD", "upstream": []}, {"id": "scanner:burbank pd"}]})
        self.assertEqual(a["independent"], 1)

    def test_selftest(self):
        self.assertEqual(SP.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_overrides_merge_upstreams(self):
        ov = {"camera:": ["poe-switch-1"], "presence:mmwave": ["poe-switch-1"]}
        a = SP.assess({"sources": [{"id": "camera:x"}, {"id": "presence:mmwave"}]}, ov)
        self.assertEqual(a["independent"], 1)

    def test_independent_types_and_line(self):
        item = {"sources": [{"id": "camera:a"}, {"id": "scanner:b"}, {"id": "chp:1"}]}
        self.assertEqual(SP.independent_types(item), 3)
        self.assertIn("CORROBORATED", SP.line(SP.assess(item)))


class TestFunctional(unittest.TestCase):
    def test_gate_ladder(self):
        single = {"sources": [{"id": "camera:a"}]}
        self.assertTrue(SP.may_trigger(single, "ask")[0])
        self.assertFalse(SP.may_trigger(single, "mention")[0])   # only corroborated goes above ask
        self.assertFalse(SP.may_trigger(single, "escalate")[0])
        corr = {"sources": [{"id": "camera:a"}, {"id": "chp:1"}]}
        self.assertTrue(SP.may_trigger(corr, "act")[0])

    def test_cli_assess_json(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_spinnaker.py"),
                            '{"sources":[{"id":"camera:a"},{"id":"camera:b"}]}'],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0)
        self.assertIn("SINGLE_SOURCE", r.stdout)


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_spinnaker.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_has_no_side_effects(self):
        r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import nova_spinnaker" % str(SCRIPTS)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
