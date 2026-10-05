#!/usr/bin/env python3
"""Geo-gate tests for nova_journal_emergency.

Run: python3 tests/test_emergency_geo.py   or   python3 -m pytest tests/test_emergency_geo.py
ponytail: guards the block-list heuristic + radius gate that keep non-LA events out of /local/.

The LLM location extractor (_primary_location) and the Nominatim geocoder (_geocode)
are mocked — these tests never touch the network or a model.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nova_journal_emergency as em  # noqa: E402
from nova_journal_emergency import _is_non_local, find_breaking, within_radius  # noqa: E402


def low(s: str) -> str:
    return s.lower()


class TestBlockList(unittest.TestCase):
    """_is_non_local: cheap marker heuristic, under-blocks on purpose."""

    def test_out_of_area_is_non_local(self):
        # the actual offenders seen on /local/
        self.assertTrue(_is_non_local(low("Twin earthquakes strike Venezuela, 160+ dead")))
        self.assertTrue(_is_non_local(low("InciWeb: UTUTS Iron fire burning in Utah")))
        self.assertTrue(_is_non_local(low("Earthquake rattles Northern California")))

    def test_local_is_not_dropped(self):
        self.assertFalse(_is_non_local(low("Brush fire along the 405 in Mission Hills")))
        self.assertFalse(_is_non_local(low("Evacuation order issued in Pasadena foothills")))

    def test_mixed_names_local_place_is_kept(self):
        # names a local place too -> keep (heuristic under-blocks on purpose)
        self.assertFalse(_is_non_local(low("Quake felt from Nevada to Los Angeles")))

    def test_plain_local_without_markers_is_kept(self):
        self.assertFalse(_is_non_local(low("Structure fire reported downtown overnight")))


class TestRadiusGate(unittest.TestCase):
    """within_radius: block-list short-circuit, then LLM place -> geocode -> distance."""

    def test_block_listed_item_skips_geocoding(self):
        with patch.object(em, "_primary_location") as loc, patch.object(em, "_geocode") as geo:
            keep, miles, place, reason = within_radius("Massive wildfire burning across Utah backcountry")
        self.assertFalse(keep)
        self.assertIn("block-list", reason)
        loc.assert_not_called()
        geo.assert_not_called()

    def test_inside_radius_is_kept(self):
        with patch.object(em, "_primary_location", return_value="Burbank, CA"), \
             patch.object(em, "_geocode", return_value=(em.HOME_LAT, em.HOME_LON)):
            keep, miles, place, _ = within_radius("Evacuation ordered as brush fire spreads in Burbank hills")
        self.assertTrue(keep)
        self.assertEqual(place, "Burbank, CA")
        self.assertLessEqual(miles, em.RADIUS_MI)

    def test_outside_radius_is_dropped(self):
        # Littlerock, CA (~30 mi NE) — "SoCal" but beyond RADIUS_MI
        with patch.object(em, "_primary_location", return_value="Littlerock, CA"), \
             patch.object(em, "_geocode", return_value=(34.521, -117.984)):
            keep, miles, place, _ = within_radius("Brush fire near Littlerock prompts evacuation warning")
        self.assertFalse(keep)
        self.assertGreater(miles, em.RADIUS_MI)

    def test_no_location_is_kept_failsafe(self):
        with patch.object(em, "_primary_location", return_value=None):
            keep, miles, place, reason = within_radius("Structure fire reported downtown overnight")
        self.assertTrue(keep)
        self.assertIn("fail-safe", reason)

    def test_ungeocodable_is_kept_failsafe(self):
        with patch.object(em, "_primary_location", return_value="Somewhere, CA"), \
             patch.object(em, "_geocode", return_value=None):
            keep, miles, place, reason = within_radius("Evacuation warning issued overnight")
        self.assertTrue(keep)
        self.assertEqual(place, "Somewhere, CA")
        self.assertIn("fail-safe", reason)


class TestFindBreaking(unittest.TestCase):
    """find_breaking applies the breaking-keyword filter and the geo gate together."""

    def test_keyword_plus_geo_gate(self):
        items = [
            {"text": "Evacuation ordered as brush fire spreads in Burbank hills", "feed": "LAFD"},
            {"text": "Massive wildfire burning across Utah backcountry", "feed": "InciWeb"},
        ]
        with patch.object(em, "_primary_location", return_value="Burbank, CA"), \
             patch.object(em, "_geocode", return_value=(em.HOME_LAT, em.HOME_LON)), \
             patch.object(em, "log"):
            hits = find_breaking(items)
        self.assertTrue(any("Burbank" in h["text"] for h in hits), hits)
        self.assertFalse(any("Utah" in h["text"] for h in hits), hits)

    def test_non_breaking_item_ignored_before_geo(self):
        items = [{"text": "City council approves new bike lanes in Burbank", "feed": "LAist"}]
        with patch.object(em, "_primary_location") as loc:
            hits = find_breaking(items)
        self.assertEqual(hits, [])
        loc.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
