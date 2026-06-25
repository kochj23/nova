#!/usr/bin/env python3
"""Geo-gate smoke test for nova_journal_emergency.

Run: python3 tests/test_emergency_geo.py   (no framework; asserts only)
ponytail: guards the block-list heuristic that keeps non-LA events out of /local/.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova_journal_emergency import _is_non_local, find_breaking


def low(s: str) -> str:
    return s.lower()


# Out-of-area → non-local (the actual offenders seen on /local/)
assert _is_non_local(low("Twin earthquakes strike Venezuela, 160+ dead"))
assert _is_non_local(low("InciWeb: UTUTS Iron fire burning in Utah"))
assert _is_non_local(low("Earthquake rattles Northern California"))

# Local → NOT dropped
assert not _is_non_local(low("Brush fire along the 405 in Mission Hills"))
assert not _is_non_local(low("Evacuation order issued in Pasadena foothills"))

# Mixed: names a local place too → keep (heuristic under-blocks on purpose)
assert not _is_non_local(low("Quake felt from Nevada to Los Angeles"))

# Plain local with no place markers at all → not dropped
assert not _is_non_local(low("Structure fire reported downtown overnight"))

# find_breaking applies keyword + geo gate together
items = [
    {"text": "Evacuation ordered as brush fire spreads in Burbank hills", "feed": "LAFD"},
    {"text": "Massive wildfire burning across Utah backcountry", "feed": "InciWeb"},
]
hits = find_breaking(items)
assert any("Burbank" in h["text"] for h in hits), hits
assert not any("Utah" in h["text"] for h in hits), hits

print("OK: emergency geo-gate tests passed")
