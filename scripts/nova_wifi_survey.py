#!/usr/bin/env python3
"""nova_wifi_survey.py — passive Wi-Fi survey from a dedicated monitor-mode radio (aircrack-ng).

Runs as root on the sensor host (nova-core7 .125, spare Intel AX200) via nova-wifi-survey.timer.
Each run: make sure NetworkManager leaves the card alone and a monitor interface exists, hop all
bands for CAPTURE_S with airodump-ng, then store every access point heard (wifi_survey) and the
deauth/disassoc frame count per BSSID from the same capture (wifi_survey_runs.deauth). No alerting
here -- nova_wifi_guard.py on .6 judges the data against our real BSSIDs from the UniFi controller.
Receive-only: nothing is transmitted.

  nova_wifi_survey.py [--iface wlp194s0]
"""
import csv, glob, json, os, socket, subprocess, sys, tempfile
from collections import Counter

IFACE = sys.argv[sys.argv.index("--iface") + 1] if "--iface" in sys.argv else "wlp194s0"
CAPTURE_S = 60
NM_CONF = "/etc/NetworkManager/conf.d/99-nova-wifi-sensor.conf"


def sh(*cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout


def monitor_iface():
    """NetworkManager kept the AX200 deaf in monitor mode (0 frames) until told to unmanage it."""
    if not os.path.exists(NM_CONF):
        open(NM_CONF, "w").write(f"[keyfile]\nunmanaged-devices=interface-name:{IFACE};interface-name:{IFACE}mon\n")
        sh("nmcli", "general", "reload")
    for name in (IFACE + "mon", IFACE):
        if "type monitor" in sh("iw", "dev", name, "info"):
            return name
    sh("nmcli", "dev", "set", IFACE, "managed", "no")
    sh("airmon-ng", "start", IFACE)
    return IFACE + "mon"


def parse_csv(path):
    rows = list(csv.reader(open(path, errors="ignore")))
    cut = next((i for i, r in enumerate(rows) if r and r[0].strip() == "Station MAC"), len(rows))
    aps = []
    for r in rows[:cut]:
        if len(r) > 13 and r[0].count(":") == 5:
            power = int(r[8].strip()) if r[8].strip().lstrip("-").isdigit() else None
            aps.append({"bssid": r[0].strip().lower(), "channel": int(r[3]) if r[3].strip().isdigit() else None,
                        "privacy": r[5].strip(), "power": power if power not in (-1, 0) else None,
                        "beacons": int(r[9]) if r[9].strip().isdigit() else 0, "essid": r[13].strip()})
    stations = sum(1 for r in rows[cut + 1:] if r and r[0].count(":") == 5)
    return aps, stations


def deauth_counts(pcap):
    """deauth (subtype 12) + disassoc (10) frames per BSSID, from the same capture."""
    out = sh("tshark", "-r", pcap, "-Y", "wlan.fc.type_subtype == 0x000c || wlan.fc.type_subtype == 0x000a",
             "-T", "fields", "-e", "wlan.bssid", timeout=300)
    return Counter(b.strip().lower() for b in out.splitlines() if b.strip())


def main():
    import psycopg2
    mon = monitor_iface()
    with tempfile.TemporaryDirectory() as d:
        # stdout MUST go to /dev/null: with no TTY airodump-ng redraws its screen non-stop, and
        # capturing that grew this process to 4 GB and swapped the host to a halt (2026-10-12).
        subprocess.run(["timeout", str(CAPTURE_S), "airodump-ng", "--band", "abg", "-w", f"{d}/s",
                        "--output-format", "csv,pcap", mon],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=CAPTURE_S + 30)
        csvs, pcaps = sorted(glob.glob(f"{d}/s-*.csv")), sorted(glob.glob(f"{d}/s-*.cap"))
        if not csvs:
            sys.exit(f"[wifi-survey] no capture from {mon}")
        aps, stations = parse_csv(csvs[-1])
        deauth = deauth_counts(pcaps[-1]) if pcaps else Counter()
    con = psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"); con.autocommit = True
    cur = con.cursor(); sensor = socket.gethostname()
    cur.execute("""CREATE TABLE IF NOT EXISTS wifi_survey_runs (id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
                     sensor text, capture_s int, aps int, stations int, deauth jsonb)
                   ;CREATE TABLE IF NOT EXISTS wifi_survey (run_id bigint REFERENCES wifi_survey_runs(id), bssid text,
                     essid text, channel int, privacy text, power int, beacons int)""")
    cur.execute("INSERT INTO wifi_survey_runs(sensor,capture_s,aps,stations,deauth) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (sensor, CAPTURE_S, len(aps), stations, json.dumps(dict(deauth))))
    run = cur.fetchone()[0]
    for a in aps:
        cur.execute("INSERT INTO wifi_survey(run_id,bssid,essid,channel,privacy,power,beacons) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (run, a["bssid"], a["essid"], a["channel"], a["privacy"], a["power"], a["beacons"]))
    print(f"[wifi-survey] {sensor} run {run}: {len(aps)} APs, {stations} stations, "
          f"{sum(deauth.values())} deauth/disassoc frames across {len(deauth)} BSSIDs")


if __name__ == "__main__":
    main()
