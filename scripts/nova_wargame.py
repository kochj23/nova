#!/opt/homebrew/bin/python3
"""nova_wargame.py — read-only what-if drills against Nova's home infrastructure.

Three rules, from the design agreed 2026-10-09:
1. Simulate incidents, not decisions. A drill takes a failure, works out what it takes down, and tests one
   candidate response in the model. Nothing is changed on any real system.
2. Label simulated results as simulated. Every result is stored with exercise=true in
   nova_ops.exercise_results. Nothing in this script writes to the action log or to the corroboration gates,
   and nothing reads exercise_results to decide anything.
3. Let simulations recommend restraint. If the candidate response makes the outcome no better or worse, the
   drill recommends HOLD, so doing nothing is always a scored option.

Dependency maps are written by hand from the device inventory and marked as assumptions where they are not
confirmed. A drill is only as good as its map.

Usage: nova_wargame.py [--scenario NAME] [--list] [--dry-run]
Written by Jordan Koch (via Claude).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import nova_config as NC  # noqa: E402
DSN = NC.pg_dsn()

# component -> rooms it serves. "assumed" marks links not yet confirmed against the UniFi client map.
DEPENDS = {
    "garage_access_point": {"rooms": ["Garage"], "status": "assumed"},
    "homekit_hub": {"rooms": ["Bridges", "Dylan's Room", "Garage", "Master Bedroom", "Outdoor"], "status": "assumed"},
    "hue_bridge": {"rooms": ["Outdoor"], "status": "assumed"},
}

SCENARIOS = {
    "garage_ap_down": {
        "description": "The garage access point goes offline.",
        "failed": "garage_access_point",
        "responses": {
            "power_cycle_ap": {"effect": "restores", "rooms_back": ["Garage"],
                               "note": "restarts only the access point; fixes the garage if the fault is the AP itself"},
            "restart_hub": {"effect": "none", "rooms_back": [],
                            "note": "the hub is not the failed part here, so restarting it changes nothing"},
        },
    },
    "homekit_hub_down": {
        "description": "The HomeKit home hub stops responding.",
        "failed": "homekit_hub",
        "responses": {
            "restart_hub": {"effect": "restores", "rooms_back": ["Bridges", "Dylan's Room", "Garage",
                                                                 "Master Bedroom", "Outdoor"],
                            "note": "a restart fixes a hung hub; a hardware fault would not be fixed"},
            "power_cycle_bridge": {"effect": "worse", "rooms_back": [],
                                   "note": "power-cycling the Hue bridge drops the Outdoor lights that were still working"},
        },
    },
}


def simulate(scenario: dict, response: str, depends: dict = None) -> dict:
    """Pure. Returns the impact of the failure, the effect of one candidate response, and a recommendation."""
    depends = DEPENDS if depends is None else depends
    failed = scenario["failed"]
    down = set(depends.get(failed, {}).get("rooms", []))
    status = depends.get(failed, {}).get("status", "unknown")
    resp = scenario["responses"][response]
    back = set(resp["rooms_back"]) & down
    still_down = sorted(down - back)
    # HOLD unless the response brings rooms back and does not take anything else down.
    if resp["effect"] == "restores" and back:
        rec = "APPLY"
        verdict = f"restores {len(back)} of {len(down)} down room(s)"
    elif resp["effect"] == "worse":
        rec, verdict = "HOLD", "makes the outcome worse"
    else:
        rec, verdict = "HOLD", "no effect on the failure: doing nothing scores the same"
    return {"scenario": scenario["description"], "response": response, "assumption": status,
            "rooms_down": sorted(down), "rooms_still_down": still_down, "verdict": verdict,
            "recommendation": rec, "note": resp["note"], "exercise": True}


def ensure_table(conn) -> None:
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS exercise_results (
        id bigserial PRIMARY KEY, run_at timestamptz NOT NULL DEFAULT now(), scenario text NOT NULL,
        response text NOT NULL, recommendation text NOT NULL, result jsonb NOT NULL,
        exercise boolean NOT NULL DEFAULT true)""")
    conn.commit()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="simulate and print, do not store")
    a = ap.parse_args(argv)
    if a.list:
        for name, s in SCENARIOS.items():
            print(f"{name}: {s['description']} responses: {', '.join(s['responses'])}")
        return 0
    names = [a.scenario] if a.scenario else sorted(SCENARIOS)
    results = []
    for name in names:
        s = SCENARIOS[name]
        for resp in s["responses"]:
            r = simulate(s, resp)
            results.append((name, resp, r))
            print(f"[{name}] {resp}: {r['recommendation']} — {r['verdict']} (assumption: {r['assumption']})")
    if not a.dry_run:
        import json
        import psycopg2
        conn = NC.pg_connect()
        ensure_table(conn)
        cur = conn.cursor()
        for name, resp, r in results:
            cur.execute("INSERT INTO exercise_results (scenario, response, recommendation, result) VALUES (%s,%s,%s,%s)",
                        (name, resp, r["recommendation"], json.dumps(r)))
        conn.commit()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
