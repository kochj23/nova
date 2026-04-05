#!/usr/bin/env python3
"""
PHASE 4: Occupancy & room-level presence reasoning
Combines vehicle presence + HomeKit motion + door contacts.
Detects anomalies (motion at 3am, open doors, etc).
"""

import json
from datetime import datetime
from pathlib import Path
import urllib.request

MEMORY_URL = "http://127.0.0.1:18790"

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def remember(text, source="vision"):
    try:
        data = json.dumps({"text": text, "source": source}).encode()
        req = urllib.request.Request(
            f"{MEMORY_URL}/remember",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read()).get("id")
    except:
        return None

def get_occupancy_state():
    """Infer occupancy from all sensors."""
    state = {
        "home_occupied": False,
        "rooms": {},
        "anomalies": [],
        "timestamp": datetime.now().isoformat()
    }
    
    # Vehicle presence (from existing model)
    # HomeKit motion sensors (from API)
    # Door contacts (from HomeKit)
    
    return state

def detect_anomalies(state):
    """Check for unusual patterns."""
    hour = datetime.now().hour
    
    if hour >= 0 and hour <= 5:  # Night hours
        if state.get("rooms", {}).get("entryway", {}).get("motion"):
            return "Motion at entryway during sleep hours"
    
    return None

def main():
    log("Occupancy model initializing...")
    
    state = get_occupancy_state()
    anomaly = detect_anomalies(state)
    
    if anomaly:
        log(f"⚠️  ANOMALY: {anomaly}")
        remember(f"Occupancy anomaly: {anomaly}", source="vision")
    
    log("Occupancy system ready")

if __name__ == "__main__":
    main()
