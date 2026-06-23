#!/usr/bin/env python3
"""Tests for nova_flights_poller.py — the pure bearing→compass logic + config sanity."""
import importlib

f = importlib.import_module("nova_flights_poller")


def test_compass_8_point():
    assert f.compass(0) == "N"
    assert f.compass(45) == "NE"
    assert f.compass(90) == "E"
    assert f.compass(135) == "SE"
    assert f.compass(180) == "S"
    assert f.compass(225) == "SW"
    assert f.compass(270) == "W"
    assert f.compass(315) == "NW"
    assert f.compass(359) == "N"        # wraps back to N
    assert f.compass(None) == "?"       # missing bearing


def test_type_name_lookup_falls_back_to_code():
    assert f.TYPE_NAMES["AS50"] == "Airbus AS350"
    assert f.TYPE_NAMES.get("ZZZZ", "ZZZZ") == "ZZZZ"   # unknown -> raw code


def test_scoped_to_91506_low_overhead():
    assert (f.LAT, f.LON) == (34.169, -118.325)         # Burbank 91506 centroid
    assert f.ALT_CEILING_FT == 10000                     # low/overhead only
