"""Tests for nova_power_schedule.py and its nova_notify hook — no PG needed."""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nova_power_schedule as ps  # noqa: E402

NODES = {"nova-core7": {"aliases": ["192.168.1.197", "core7"], "off": "22:00", "on": "10:00"}}


def at(h, m):
    return datetime(2026, 11, 16, h, m)


class Window(unittest.TestCase):
    def test_crosses_midnight(self):
        self.assertTrue(ps.in_window("22:00", "10:00", at(23, 0)))
        self.assertTrue(ps.in_window("22:00", "10:00", at(3, 0)))
        self.assertTrue(ps.in_window("22:00", "10:00", at(10, 15)))   # boot grace
        self.assertFalse(ps.in_window("22:00", "10:00", at(10, 30)))  # late wake pages
        self.assertFalse(ps.in_window("22:00", "10:00", at(21, 59)))

    def test_same_day_window(self):
        self.assertTrue(ps.in_window("01:00", "05:00", at(2, 0)))
        self.assertFalse(ps.in_window("01:00", "05:00", at(6, 0)))


class Mentions(unittest.TestCase):
    def test_name_and_alias_match_in_window(self):
        self.assertEqual(ps.mentioned_off_node("LLM DOWN: ollama on nova-core7/.125", at(23, 0), NODES), "nova-core7")
        self.assertEqual(ps.mentioned_off_node("host 192.168.1.197 unreachable", at(2, 0), NODES), "nova-core7")

    def test_no_match_outside_window(self):
        self.assertIsNone(ps.mentioned_off_node("nova-core7 down", at(12, 0), NODES))

    def test_no_partial_matches(self):
        self.assertIsNone(ps.mentioned_off_node("nova-core70 down", at(23, 0), NODES))
        self.assertIsNone(ps.mentioned_off_node("192.168.1.1970 down", at(23, 0), NODES))
        self.assertIsNone(ps.mentioned_off_node("score7 rising", at(23, 0), NODES))

    def test_bad_config_fails_open(self):
        self.assertEqual(ps.off_nodes(at(23, 0), {"x": {"off": "garbage"}}), [])


if __name__ == "__main__":
    unittest.main()
