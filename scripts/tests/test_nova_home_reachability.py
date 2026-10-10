"""Tests for nova_home_reachability: the diagnosis is pure, so no network or database is touched."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nova_home_reachability as H  # noqa: E402


def rows(spec):
    """spec: {room: [bool, ...]} -> [(room, accessory, active)]"""
    return [(room, f"acc{i}", a) for room, vals in spec.items() for i, a in enumerate(vals)]


def test_all_rooms_reporting_is_ok():
    assert H.diagnose(rows({"Garage": [True, True], "Patio": [True]}))["verdict"] == "ok"


def test_every_room_dark_points_at_controller():
    r = H.diagnose(rows({"Garage": [False, False], "Patio": [False], "Bridges": [False]}))
    assert r["verdict"] == "controller"
    assert r["dark_rooms"] == ["Bridges", "Garage", "Patio"]


def test_some_rooms_dark_points_at_segment():
    r = H.diagnose(rows({"Garage": [False, False], "Patio": [False], "Office": [True, True]}))
    assert r["verdict"] == "segment"
    assert r["dark_rooms"] == ["Garage", "Patio"]


def test_half_active_is_not_dark():
    # exactly half active is at the threshold, not below it
    assert H.diagnose(rows({"Garage": [True, False]}))["verdict"] == "ok"


def test_empty_snapshot_is_no_data():
    assert H.diagnose([])["verdict"] == "no-data"


def test_ping_summary_marks_up_and_down():
    fake = {"a": "10.0.0.1", "b": "10.0.0.2"}
    out = H.ping_summary(fake, _ping=lambda ip: ip == "10.0.0.1")
    assert out == "a: up; b: down", out


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
