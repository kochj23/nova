#!/opt/homebrew/bin/python3
"""
nova_strix_rotation.py — daily rotating Strix purple-team schedule.

Scheduled at 02:00 daily. Picks today's target group by weekday and hands it to
nova_strix_run.py (which opens the maintenance window, enforces the hard auto-kill,
and posts findings + Wazuh scorecard to #nova-info).

TIERS:
  robust  -> mode 'standard', full autonomous testing, 45m cap
  fragile -> recon-only (no exploitation/fuzzing/writes), 20m cap  (cameras/printers/bridges)

EDIT `ROTATION` to tune targets. The live Zigbee coordinator (.23) is deliberately
excluded — reconning the radio running the home automation is not worth the risk.
"""
import subprocess, sys, os
from datetime import datetime

RUN = os.path.expanduser("~/.openclaw/scripts/nova_strix_run.py")

# weekday() : Mon=0 .. Sun=6
ROTATION = {
    0: dict(label="grafana-2stack", mode="standard", max_min=45, recon=False,
            targets=["http://192.168.1.2:3000"]),                                   # Grafana + .2 web
    1: dict(label="home-assistant", mode="standard", max_min=45, recon=False,
            targets=["http://192.168.1.6:8123"]),                                   # HA
    2: dict(label="unifi", mode="standard", max_min=45, recon=False,
            targets=["https://192.168.1.1", "https://192.168.1.9"]),                # router + Protect/NVR
    3: dict(label="nas-admin", mode="standard", max_min=45, recon=False,
            targets=["http://192.168.1.11:5000", "http://192.168.1.69"]),           # Synology + UNAS
    4: dict(label="misc-web", mode="standard", max_min=45, recon=False,
            targets=["http://192.168.1.11:5000"]),                                  # TODO: add Plex/OpenWebUI/SearXNG/TinyChat/Homebridge addrs
    5: dict(label="cameras", mode="quick", max_min=20, recon=True,
            targets=["https://192.168.1.9", "http://192.168.1.176", "http://192.168.1.41"]),  # UniFi NVR + sample Nest cams (recon-only)
    6: dict(label="printers-bridges", mode="quick", max_min=20, recon=True,
            targets=["http://192.168.1.141", "http://192.168.1.179", "http://192.168.1.91"]),  # printer + SPARE SLZB bridges (NOT .23)
}

def main():
    day = datetime.now().weekday()
    g = ROTATION.get(day)
    if not g or not g["targets"]:
        print(f"rotation: nothing scheduled for weekday {day}"); return
    cmd = ["/opt/homebrew/bin/python3", RUN,
           "--targets", ",".join(g["targets"]),
           "--label", g["label"],
           "--mode", g["mode"],
           "--max-min", str(g["max_min"])]
    if g["recon"]:
        cmd.append("--recon-only")
    print(f"rotation [weekday {day}] -> {g['label']}: {g['targets']} (recon={g['recon']})")
    subprocess.run(cmd)

if __name__ == "__main__":
    main()
