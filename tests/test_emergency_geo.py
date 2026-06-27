#!/usr/bin/env python3
"""Geofence tests for nova_journal_emergency.

Runnable offline (monkeypatches the geocoder + LLM):
    env -u PYTHONPATH PYTHONPATH=$HOME/.openclaw/scripts python3 test_emergency_geo.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path.home() / ".openclaw"))
import nova_journal_emergency as e

home = (e.HOME_LAT, e.HOME_LON)
LITTLEROCK = (34.521, -117.981)
BURBANK = (34.181, -118.308)
SAN_DIEGO = (32.716, -117.161)

# 1. haversine sanity against known coordinates
assert e._haversine_mi(*home, *BURBANK) < 3, "Burbank ~0 mi from home"
d_lr = e._haversine_mi(*home, *LITTLEROCK)
assert 25 < d_lr < 40, f"Littlerock should be ~30 mi (outside 25), got {d_lr:.1f}"
assert e._haversine_mi(*home, *SAN_DIEGO) > 100, "San Diego far"

# 2. block-list fallback still works
assert e._is_non_local("magnitude 6 earthquake in nevada")
assert not e._is_non_local("brush fire in burbank near the 134")

# 3. within_radius decision — monkeypatch geocode + LLM so it's deterministic/offline
COORDS = {"littlerock, ca": LITTLEROCK, "burbank, ca": BURBANK, "san diego, ca": SAN_DIEGO}
e._geocode = lambda place: COORDS.get(place.strip().lower())
def _fake_primary(text):
    t = text.lower()
    if "littlerock" in t: return "Littlerock, CA"   # primary, even if Burbank is name-dropped
    if "burbank" in t:    return "Burbank, CA"
    if "san diego" in t:  return "San Diego, CA"
    return None
e._primary_location = _fake_primary

# the real-world miss: a Littlerock fire that tangentially mentions Burbank -> must DROP
keep, mi, place, reason = e.within_radius(
    "Brush fire near Littlerock in the Angeles forest; smoke may drift to the 210 in Burbank")
assert not keep, f"Littlerock event must be DROPPED (primary=Littlerock ~{mi}); got keep"

assert e.within_radius("Structure fire in Burbank near the 134")[0], "Burbank event KEPT"
assert not e.within_radius("Wildfire threatens San Diego county")[0], "San Diego DROPPED"

# clearly out-of-area short-circuits (no geocode needed)
keep, _, _, reason = e.within_radius("Wildfire near Sacramento, northern california")
assert not keep and "block-list" in reason

# ungeocodable -> fail-safe KEEP (never drop a real local alert on a geocoder hiccup)
e._primary_location = lambda t: "Nowheresville, ZZ"
e._geocode = lambda place: None
keep, _, _, reason = e.within_radius("brush fire somewhere unparseable")
assert keep and "fail-safe" in reason

print("test_emergency_geo OK")
