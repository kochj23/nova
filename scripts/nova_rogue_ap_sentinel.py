#!/usr/bin/env python3
"""nova_rogue_ap_sentinel.py — catch YOUR OWN gear broadcasting open/misconfigured WiFi.

Born from the Bose Smart Soundbar 900 incident (2026-07-31): a soundbar wired to the
LAN also broadcast an OPEN WiFi SoftAP for ~13 hours, and the ONLY reason it was caught
is Jordan happened to ask for open-AP names in the daily report. The existing rogue_check
watches for unknown CLIENTS joining the network; nothing watched the BROADCAST
environment for open/misconfigured APs that are actually our own devices. This closes
that gap.

It reads the wifi_aps scan and flags, ALERT-ON-CHANGE:
  * YOUR-GEAR-OPEN — an Open/weak AP, strong signal (physically inside), whose BSSID
    /40-prefix matches a device on our LAN. A SoftAP MAC is adjacent to the device's
    STA/wired MAC, so a /40 match == the SAME physical device broadcasting open.
    Near-zero false positives: neighbor open APs don't share a /40 with our device MACs.
  * UNIFI-ROGUE — UniFi's own is_rogue=true (an AP it detected bridged onto our wire).
Neighbor open APs (weak signal, no device match) are IGNORED — that noise is exactly why
scanning the daily list by hand was useless.

Alerts only on NEW findings (state file) to #nova-alerts, with an allowlist to silence
acknowledged BSSIDs. get_current_flags() feeds the daily alert-patterns report.

    nova_rogue_ap_sentinel.py             # scan + alert on new findings
    nova_rogue_ap_sentinel.py --check     # print current findings, alert nothing
    nova_rogue_ap_sentinel.py --ack BSSID # allowlist a bssid (stop alerting on it)
"""
import json
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
STATE_DIR = Path.home() / ".openclaw/workspace/state"
STATE_FILE = STATE_DIR / "rogue_ap_sentinel.json"
ALLOWLIST_FILE = STATE_DIR / "rogue_ap_allowlist.json"
STRONG_DBM = -68                    # >= this = physically inside/adjacent = likely ours
OPEN_SECS = ("open", "wep", "none")  # security strings (lowercased) treated as open/weak


def _load(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _save(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def _lan_prefixes():
    """{/40-prefix -> device name} for every MAC on our LAN. A device's SoftAP MAC is
    adjacent to its STA/wired MAC, so a /40 (first-5-octet) match == the same device."""
    out = {}
    try:
        c = psycopg2.connect(DSN); cur = c.cursor()
        cur.execute("SELECT client_mac, client_name FROM telemetry.known_devices WHERE client_mac IS NOT NULL")
        for mac, name in cur.fetchall():
            if mac and len(mac) >= 14:
                out[mac[:14].lower()] = name or mac
        c.close()
    except Exception as e:
        print(f"lan device fetch failed: {e}", flush=True)
    return out


def scan_flags(window="25 minutes"):
    """Evaluate the latest wifi_aps scan; return a list of finding dicts."""
    lan = _lan_prefixes()
    findings = []
    try:
        c = psycopg2.connect(DSN); cur = c.cursor()
        cur.execute("""
            SELECT DISTINCT ON (bssid) ssid, bssid, signal_dbm, security, channel, raw
            FROM wifi_aps WHERE ts > now() - interval %s
            ORDER BY bssid, ts DESC
        """, (window,))
        rows = cur.fetchall(); c.close()
    except Exception as e:
        print(f"wifi_aps fetch failed: {e}", flush=True)
        return findings
    for ssid, bssid, dbm, sec, ch, raw in rows:
        sec_l = (sec or "").lower()
        is_open = (not sec_l) or any(s in sec_l for s in OPEN_SECS)
        try:
            rj = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        except Exception:
            rj = {}
        is_rogue = bool(rj.get("is_rogue"))
        mine = lan.get((bssid or "")[:14].lower())
        strong = dbm is not None and dbm >= STRONG_DBM
        if is_open and strong and mine:
            findings.append({
                "kind": "YOUR-GEAR-OPEN", "severity": "high", "bssid": bssid, "ssid": ssid,
                "dbm": dbm, "security": sec or "Open", "device": mine,
                "detail": f"'{ssid}' is an OPEN/unencrypted AP broadcasting from YOUR device "
                          f"({mine}) at {dbm}dBm (inside the house). Misconfigured/setup-mode — close it."})
        elif is_rogue:
            findings.append({
                "kind": "UNIFI-ROGUE", "severity": "high", "bssid": bssid, "ssid": ssid,
                "dbm": dbm, "security": sec, "device": mine or "?",
                "detail": f"UniFi flagged '{ssid}' ({bssid}) as a ROGUE AP bridged onto the wired LAN."})
    return findings


def get_current_flags():
    """Public — current findings minus allowlisted BSSIDs. Consumed by the daily
    alert-patterns report."""
    allow = set(_load(ALLOWLIST_FILE, []))
    return [f for f in scan_flags() if f["bssid"] not in allow]


def main():
    if "--ack" in sys.argv:
        i = sys.argv.index("--ack")
        bssid = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        allow = set(_load(ALLOWLIST_FILE, [])); allow.add(bssid)
        _save(ALLOWLIST_FILE, sorted(allow))
        print(f"acknowledged {bssid} — no longer alerting on it")
        return 0
    flags = get_current_flags()
    if "--check" in sys.argv:
        print(json.dumps(flags, indent=2, default=str) if flags else "no findings (clean)")
        return 0
    # Alert on NEW findings only (change-based; a persistent known one won't re-nag).
    seen = set(_load(STATE_FILE, []))
    curr = {f"{f['kind']}:{f['bssid']}" for f in flags}
    new = [f for f in flags if f"{f['kind']}:{f['bssid']}" not in seen]
    for f in new:
        msg = (f":rotating_light: *Rogue/open AP — {f['kind']}*\n{f['detail']}\n"
               f"BSSID {f['bssid']} | {f['security']} | {f['dbm']}dBm\n"
               f"(silence with: nova_rogue_ap_sentinel.py --ack {f['bssid']})")
        try:
            nova_config.post_both(msg, slack_channel=nova_config.SLACK_ALERTS, discord_channel=None)
        except Exception as e:
            print(f"alert post failed: {e}", flush=True)
        print(f"ALERTED: {f['kind']} {f['bssid']}", flush=True)
    _save(STATE_FILE, sorted(curr))
    if not new:
        print(f"no new findings ({len(flags)} currently flagged)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
