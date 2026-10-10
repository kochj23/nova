"""Tests for nova_wargame: simulation is pure, so no database or real system is touched."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nova_wargame as W  # noqa: E402


def test_restoring_response_is_applied_and_labelled_exercise():
    r = W.simulate(W.SCENARIOS["garage_ap_down"], "power_cycle_ap")
    assert r["recommendation"] == "APPLY" and r["exercise"] is True
    assert r["rooms_down"] == ["Garage"] and r["rooms_still_down"] == []


def test_no_effect_response_is_held():
    r = W.simulate(W.SCENARIOS["garage_ap_down"], "restart_hub")
    assert r["recommendation"] == "HOLD", r


def test_worsening_response_is_held():
    r = W.simulate(W.SCENARIOS["homekit_hub_down"], "power_cycle_bridge")
    assert r["recommendation"] == "HOLD" and "worse" in r["verdict"]


def test_simulation_reports_rooms_still_down():
    depends = {"hub": {"rooms": ["A", "B"], "status": "assumed"}}
    scen = {"description": "d", "failed": "hub",
            "responses": {"fix_a_only": {"effect": "restores", "rooms_back": ["A"], "note": ""}}}
    r = W.simulate(scen, "fix_a_only", depends)
    assert r["rooms_still_down"] == ["B"] and r["recommendation"] == "APPLY"


def test_assumption_status_is_carried_through():
    r = W.simulate(W.SCENARIOS["homekit_hub_down"], "restart_hub")
    assert r["assumption"] == "assumed"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
