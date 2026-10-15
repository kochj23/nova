#!/usr/bin/env python3
"""nova_wifi_scan.py — track visible WiFi access points day-over-day.

Pulls the UniFi controller's own passive RF neighbor-scan (stat/rogueap --
UniFi APs already continuously scan all channels for interference/planning
purposes; this just reads that data out rather than standing up new scanning
hardware). Stores every sighting in wifi_aps for day-over-day tracking:
signal strength, security type, channel, band.

Flags newly-appeared BSSIDs and any security downgrade (e.g. a previously
WPA2/3 network now showing open/WEP) as notable -- those feed the security
article; the raw table feeds the local Burbank "what's in the air" color.

Written 2026-07-23.
"""
import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_notify import notify

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
CONTROLLER = "https://192.168.1.1"
LOG_FILE = Path.home() / ".openclaw/logs/wifi_scan.log"

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

# BSSIDs belonging to our own UniFi APs -- excluded from "neighbor" analysis,
# tracked separately with is_ours=true so the article can distinguish "our
# network" from "what's actually out there."
import time


def log(msg):
    line = f"[wifi-scan {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def get_unifi_key():
    return nova_config._keychain("nova-unifi-api-key", required=True)


def _get(path, key, attempts=4):
    """GET a UniFi stat endpoint. The Network app answers 503 for ~10-15 s around its 00:00 housekeeping
    (failed the 00:00 run two nights running, 2026-10-14/15) and a stray Tomcat 500 ~1-2% of the time;
    both clear on retry, so 5xx retries with backoff (4+8+12 s fits the 60 s task timeout)."""
    for i in range(attempts):
        req = urllib.request.Request(f"{CONTROLLER}/proxy/network/api/s/default/{path}",
                                     headers={"X-API-Key": key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20, context=_ctx) as resp:
                return json.loads(resp.read()).get("data", [])
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == attempts - 1:
                raise
            time.sleep(4 * (i + 1))


def fetch_neighbors():
    return _get("stat/rogueap", get_unifi_key())


def fetch_our_bssids():
    key = get_unifi_key()
    devices = _get("stat/device", key)
    ours = set()
    for d in devices:
        for vap in d.get("vap_table", []) or []:
            bssid = vap.get("bssid")
            if bssid:
                ours.add(bssid.lower())
    return ours


def main():
    try:
        neighbors = fetch_neighbors()
        ours = fetch_our_bssids()
    except Exception as e:
        log(f"UniFi fetch failed: {e}")
        return 1
    log(f"{len(neighbors)} APs seen ({len(ours)} are ours)")

    import psycopg2
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("SELECT DISTINCT ON (bssid) bssid, security FROM wifi_aps "
               "WHERE ts > now() - interval '14 days' ORDER BY bssid, ts DESC")
    known_security = {row[0]: row[1] for row in cur.fetchall()}
    cur.execute("SELECT DISTINCT bssid FROM wifi_aps WHERE ts > now() - interval '14 days'")
    known_bssids = {row[0] for row in cur.fetchall()}

    new_aps, downgrades = [], []
    for ap in neighbors:
        bssid = (ap.get("bssid") or "").lower()
        if not bssid:
            continue
        essid = ap.get("essid", "")
        security = ap.get("security", "")
        signal = ap.get("signal")
        channel = ap.get("channel")
        radio = ap.get("radio")
        is_ours = bssid in ours

        cur.execute(
            "INSERT INTO wifi_aps (ssid, bssid, signal_dbm, security, channel, radio, is_ours, raw) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (essid, bssid, signal, security, channel, radio, is_ours, json.dumps(ap)))

        if not is_ours:
            if bssid not in known_bssids:
                new_aps.append(f"{essid or '(hidden)'} [{bssid}] {security} ch{channel}")
            elif bssid in known_security and known_security[bssid] and security:
                old_sec, new_sec = known_security[bssid].upper(), security.upper()
                downgraded = (("WPA3" in old_sec and "WPA3" not in new_sec) or
                             ("WPA2" in old_sec and ("OPEN" in new_sec or "WEP" in new_sec)) or
                             ("OPEN" not in old_sec and "OPEN" in new_sec))
                if downgraded:
                    downgrades.append(f"{essid or '(hidden)'} [{bssid}] {old_sec} -> {new_sec}")

    cur.close()
    conn.close()

    if new_aps:
        log(f"{len(new_aps)} new AP(s): {new_aps[:5]}")
    if downgrades:
        log(f"{len(downgrades)} security downgrade(s): {downgrades}")
        try:
            notify("WiFi: security downgrade on a tracked AP", body="\n".join(downgrades[:10]),
                  level="warning", category="security", dedup_key=None)
        except Exception as e:
            log(f"notify failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
