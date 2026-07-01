#!/usr/bin/env python3
"""nova_bambu_status.py — poll the Bambu Lab X1C printers over LAN MQTT and write a
state file the daily ops column reads. Comments on printers ONLY when they're doing
something (printing/paused/finished/failed) — idle is skipped upstream by the column.

Connects TLS:8883 (self-signed -> insecure), user 'bblp', password = LAN access code
from Keychain (svc nova-bambu-<serial>). Forces a full status push and parses print.*.
"""
import json
import subprocess
import time
from pathlib import Path

import paho.mqtt.client as mqtt

import bambu_printers

STATE = Path.home() / ".openclaw/workspace/state/nova_bambu_state.json"
# gcode_state values worth writing about right now. FINISH counts only while still
# hot (just finished) — a cold FINISH is a stale calibration, i.e. effectively idle.
ACTIVE = {"RUNNING", "PAUSE", "PREPARE", "SLICING", "FAILED"}


def access_code(serial):
    r = subprocess.run(["security", "find-generic-password", "-a", "nova",
                        "-s", f"nova-bambu-{serial}", "-w"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    # fall back to no -a (some entries store the code without the nova account)
    r = subprocess.run(["security", "find-generic-password",
                        "-s", f"nova-bambu-{serial}", "-w"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def poll_one(pid, meta, timeout=8):
    """Return a status dict for one printer, or None if unreachable."""
    serial, ip = meta["serial"], meta["ip"]
    code = access_code(serial)
    if not code:
        return {"id": pid, "name": meta["name"], "error": "no access code in Keychain"}
    got = {"report": None}

    def on_connect(c, *_):
        c.subscribe(f"device/{serial}/report")
        c.publish(f"device/{serial}/request",
                  json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))

    def on_message(c, u, msg):
        try:
            p = json.loads(msg.payload).get("print")
            if p and ("gcode_state" in p or "mc_percent" in p):
                got["report"] = p
        except Exception:
            pass

    c = mqtt.Client()
    c.username_pw_set("bblp", code)
    c.tls_set(cert_reqs=__import__("ssl").CERT_NONE)
    c.tls_insecure_set(True)
    c.on_connect = on_connect
    c.on_message = on_message
    try:
        c.connect(ip, 8883, 15)
    except Exception as e:
        return {"id": pid, "name": meta["name"], "error": f"connect: {e}"}
    c.loop_start()
    deadline = time.time() + timeout
    while time.time() < deadline and got["report"] is None:
        time.sleep(0.3)
    c.loop_stop(); c.disconnect()

    p = got["report"]
    if not p:
        return {"id": pid, "name": meta["name"], "error": "no report (offline/asleep?)"}
    state = (p.get("gcode_state") or "UNKNOWN").upper()
    nozzle = p.get("nozzle_temper")
    active = state in ACTIVE or (state == "FINISH" and (nozzle or 0) > 60)
    return {
        "id": pid, "name": meta["name"], "state": state,
        "active": active,
        "percent": p.get("mc_percent"),
        "remaining_min": p.get("mc_remaining_time"),
        "job": p.get("subtask_name") or p.get("gcode_file"),
        "layer": p.get("layer_num"), "total_layers": p.get("total_layer_num"),
        "nozzle_c": p.get("nozzle_temper"), "bed_c": p.get("bed_temper"),
    }


def main():
    out = {}
    for pid, meta in bambu_printers.PRINTERS.items():
        st = poll_one(pid, meta)
        out[pid] = st
        print(f"[bambu] {pid} {meta['name']}: "
              + (st.get("error") or f"{st['state']} "
                 + (f"{st.get('percent')}% {st.get('job')}" if st.get("active") else "(idle)")), flush=True)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(out, indent=2, default=str))
    active = [v for v in out.values() if v.get("active")]
    print(f"[bambu] wrote {STATE} — {len(active)} printer(s) active", flush=True)


if __name__ == "__main__":
    main()
