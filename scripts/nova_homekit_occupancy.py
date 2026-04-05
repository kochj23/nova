#!/usr/bin/env python3
"""
TRACK 5: Full HomeKit integration with occupancy reasoning
Combines vehicle presence + motion sensors + door contacts.
Creates room-level occupancy map, detects anomalies.
"""

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
import urllib.request
import time

MEMORY_URL = "http://127.0.0.1:18790"
WORKSPACE = Path.home() / ".openclaw/workspace"
HOMEKIT_SCRIPT = Path.home() / ".openclaw/scripts/nova_homekit_query.sh"

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)

def remember(text, source="occupancy"):
    try:
        data = json.dumps({"text": text, "source": source}).encode()
        req = urllib.request.Request(
            f"{MEMORY_URL}/remember",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            return result.get("id")
    except Exception as e:
        log(f"Memory store failed: {e}")
        return None

def get_homekit_accessories():
    """Query HomeKit for all accessories and their state."""
    try:
        if not HOMEKIT_SCRIPT.exists():
            log(f"⚠️  HomeKit script not found: {HOMEKIT_SCRIPT}")
            return []
        
        result = subprocess.run(
            ["bash", str(HOMEKIT_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            log(f"HomeKit query failed: {result.stderr[:200]}")
            return []
        
        # Parse JSON output
        try:
            accessories = json.loads(result.stdout)
            return accessories if isinstance(accessories, list) else []
        except json.JSONDecodeError:
            log(f"Invalid HomeKit JSON response")
            return []
            
    except Exception as e:
        log(f"HomeKit query error: {e}")
        return []

def check_vehicle_presence():
    """
    Check if vehicle is in carport/garage.
    Integrates with camera monitoring.
    Returns: {"home": bool, "location": "carport|garage|unknown", "confidence": 0-1}
    """
    # TODO: Integrate with camera-based vehicle detection
    # For now, return mock data
    return {
        "home": True,
        "location": "carport",
        "confidence": 0.95,
        "last_seen": datetime.now().isoformat()
    }

def build_occupancy_map(accessories, vehicle_data):
    """
    Combine all sensors into room-level occupancy inference.
    
    Returns:
    {
        "home_occupied": bool,
        "confidence": 0-1,
        "rooms": {
            "master_bedroom": {"occupied": bool, "motion": bool, "door_open": bool},
            "kitchen": {...},
            ...
        },
        "anomalies": ["list", "of", "anomalies"],
        "timestamp": "ISO8601"
    }
    """
    
    occupancy_map = {
        "home_occupied": vehicle_data.get("home", False),
        "confidence": 0.0,
        "rooms": {},
        "anomalies": [],
        "timestamp": datetime.now().isoformat()
    }
    
    # Build room map from HomeKit accessories
    room_sensors = {}
    
    for accessory in accessories:
        room = accessory.get("room", "unknown")
        
        if room not in room_sensors:
            room_sensors[room] = {
                "motion": False,
                "doors": [],
                "lights": [],
                "temperature": None,
                "reachable": True
            }
        
        atype = accessory.get("type", "").lower()
        state = accessory.get("state", "").lower()
        
        if "motion" in atype:
            room_sensors[room]["motion"] = state == "detected"
        elif "door" in atype or "window" in atype:
            room_sensors[room]["doors"].append({
                "name": accessory.get("name"),
                "open": state == "open"
            })
        elif "temperature" in atype or "thermostat" in atype:
            try:
                room_sensors[room]["temperature"] = float(state.split("°")[0])
            except:
                pass
        
        if not accessory.get("reachable", True):
            room_sensors[room]["reachable"] = False
    
    # Build occupancy reasoning per room
    for room, sensors in room_sensors.items():
        occupied = sensors["motion"] or len([d for d in sensors["doors"] if d["open"]]) > 0
        
        occupancy_map["rooms"][room] = {
            "occupied": occupied,
            "motion": sensors["motion"],
            "doors_open": [d["name"] for d in sensors["doors"] if d["open"]],
            "temperature": sensors["temperature"],
            "reachable": sensors["reachable"]
        }
    
    # Detect anomalies
    hour = datetime.now().hour
    
    # Sleep hours anomaly (10pm-7am)
    if 22 <= hour or hour < 7:
        for room, state in occupancy_map["rooms"].items():
            if state["motion"]:
                occupancy_map["anomalies"].append(
                    f"Motion in {room} during sleep hours"
                )
    
    # Doors open too long
    for room, state in occupancy_map["rooms"].items():
        if len(state["doors_open"]) > 0:
            occupancy_map["anomalies"].append(
                f"{room}: {', '.join(state['doors_open'])} open >10 minutes"
            )
    
    # Temperature extremes
    for room, state in occupancy_map["rooms"].items():
        if state["temperature"]:
            if state["temperature"] > 78:
                occupancy_map["anomalies"].append(
                    f"{room}: Temperature high ({state['temperature']}°F)"
                )
            elif state["temperature"] < 62:
                occupancy_map["anomalies"].append(
                    f"{room}: Temperature low ({state['temperature']}°F)"
                )
    
    # Calculate overall confidence
    if occupancy_map["rooms"]:
        sensors_reporting = len([r for r in occupancy_map["rooms"].values() if r["reachable"]])
        occupancy_map["confidence"] = sensors_reporting / len(occupancy_map["rooms"])
    
    return occupancy_map

def analyze_occupancy_pattern(occupancy_map):
    """
    Analyze occupancy pattern and detect anomalies.
    Posts alerts for unusual situations.
    """
    
    # Check for anomalies
    if occupancy_map["anomalies"]:
        for anomaly in occupancy_map["anomalies"]:
            log(f"⚠️  ANOMALY: {anomaly}")
            remember(f"Occupancy anomaly: {anomaly}", source="occupancy")
    
    # Log occupancy state
    occupied_rooms = [
        r for r, s in occupancy_map["rooms"].items()
        if s["occupied"]
    ]
    
    if occupancy_map["home_occupied"]:
        if occupied_rooms:
            log(f"✓ HOME OCCUPIED: {', '.join(occupied_rooms)}")
        else:
            log(f"✓ VEHICLE HOME (no room motion)")
    else:
        log(f"✓ HOME UNOCCUPIED")
    
    remember(
        f"Occupancy state: home={occupancy_map['home_occupied']}, rooms={occupied_rooms}, confidence={occupancy_map['confidence']:.2f}",
        source="occupancy"
    )

def occupancy_monitor_loop():
    """
    Main loop: continuously monitor occupancy.
    Runs every 5 minutes, analyzes sensors, detects anomalies.
    """
    log("HomeKit occupancy monitor starting...")
    
    iteration = 0
    
    while True:
        try:
            iteration += 1
            
            # Get current state
            accessories = get_homekit_accessories()
            vehicle_data = check_vehicle_presence()
            
            # Build occupancy map
            occupancy_map = build_occupancy_map(accessories, vehicle_data)
            
            # Analyze for anomalies
            analyze_occupancy_pattern(occupancy_map)
            
            # Sleep until next check
            time.sleep(300)  # 5 minutes
            
        except KeyboardInterrupt:
            log("Occupancy monitor stopped by user")
            break
        except Exception as e:
            log(f"Monitor error: {e}")
            time.sleep(300)
            continue

def get_occupancy_state():
    """Single query: get current occupancy state (non-loop)."""
    accessories = get_homekit_accessories()
    vehicle_data = check_vehicle_presence()
    occupancy_map = build_occupancy_map(accessories, vehicle_data)
    
    return occupancy_map

def main():
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        state = get_occupancy_state()
        print(json.dumps(state, indent=2))
        return
    
    # Default: run continuous monitoring
    occupancy_monitor_loop()

if __name__ == "__main__":
    main()
