#!/usr/bin/env python3
"""nova_vault7_ttp.py — behavioral detection signatures derived from Vault-7 TTPs.

DEFENSIVE. These are NOT the leaked implants — they are BEHAVIORAL detection rules
that fire on the *fingerprint* a Vault-7-style implant leaves behind, computed over
telemetry Nova already collects (Wazuh security_events, telemetry.network,
telemetry.known_devices). They are consumed by nova_wazuh_bridge.py (imported and run
every 2 min as an extra detection step) so they ride the existing detection flow, and
are runnable standalone for validation:

    nova_vault7_ttp.py --list          # show the rule catalog + rationale
    nova_vault7_ttp.py --scan          # run all rules once, print findings (no alert)
    nova_vault7_ttp.py --scan --alert  # run + emit via triage/notify

TUNING PHILOSOPHY (low false-positive, per Jordan's steer that validating detections
matters more than adding scanners): every rule is corroboration-gated, maintenance-
window-suppressed, and each finding is emitted AT MOST once per 24h per (rule,host)
via a stable dedup_key — so a standing condition never re-pages or re-triages the LLM
every 2-minute cycle. Weak single-signal findings emit at 'info'; only corroborated
fingerprints (rootkit, ransomware-adjacent, outbound-dominant exfil) escalate.

RULE CATALOG (Vault-7 project -> fingerprint):
  v7_firmware_tamper   CherryBlossom / DarkMatter / Sonic Screwdriver — implant flashed
                       to router/AP firmware over-the-air, or Mac EFI/boot infected.
                       Fingerprint: Wazuh FIM change or rootcheck hit on boot/firmware/
                       kernel-module paths, OUTSIDE maintenance and NOT explained by a
                       package (dpkg) operation on the same host in the same window.
  v7_antiforensic_gap  Marble (obfuscation) / Dumbo (suspends webcams, corrupts
                       recordings) — attacker blinds the sensors. Fingerprint: explicit
                       log/audit tamper (auditd stopped, journal vacuumed, history
                       cleared, log truncated) in the logs.
  v7_implant_beacon    HIVE / CherryBlossom Flytrap — implanted device beacons to a C2
                       (CherryTree) on an interval. Fingerprint: an IoT/camera-class
                       device is the SOURCE of a suspicious-DNS / C2 / beacon detection.
  v7_smart_tv_fakeoff  Weeping Angel — Samsung F-series TV "Fake-Off": appears powered
                       down while recording mic audio and exfiltrating. Fingerprint: a
                       TV/AV/mic-capable device pushing OUTBOUND-DOMINANT traffic
                       (tx > rx) during the household quiet window (02:00-05:00) — the
                       inverse of normal streaming/updates, which are download-heavy.
  v7_rogue_persistence Grasshopper / Achilles / Aeris — persistence + trojaned
                       installers, and identity crossing the trust boundary to enable a
                       Weeping-Angel pivot. Fingerprint: (a) new persistence unit
                       (systemd/cron/launchd/rc.local) outside maintenance, or (b) a
                       trusted-host identity (Mac/nova-core/laptop) appearing on the IoT
                       Wi-Fi (KOCH-IOT).

Written for Jordan's fleet defense (Vault-7 threat model), read-only.
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
from nova_notify import notify
try:
    from nova_alert_triage import triage
except Exception:
    triage = None
try:
    import nova_maintenance
except Exception:
    nova_maintenance = None

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
IOT_SSID = ("KOCH-IOT", "KOCHJ-GARAGE-2.4")   # the Wi-Fi SSIDs used for IoT gear

# ── Device classification (shared with the segmentation audit) ────────────────
# Ordered: first match wins. Trusted = fleet hosts + Jordan's personal endpoints.
_TRUSTED_RE = re.compile(
    r"\b(mac|macbook|macbookpro|macmini|mac-mini|mac-studio|macstudio|imac|iphone|ipad|"
    r"watch|nova-core\d?|ubuntu-server|lts0\d|unas|synology\s*nas|rack.*synology)\b", re.I)
_CLASS_RULES = [
    ("camera",      re.compile(r"(nest-?cam|nest-?doorbell|unifi-?nvr|unvr|camera|(^|-)cam(-|$)|"
                               r"protect|frigate|hikvision|dahua|reolink|wyze|"
                               r"^(interior|exterior|external|outside|inside)---)", re.I)),
    ("smart-tv-av", re.compile(r"(samsung|bose|onkyo|sonos|soundbar|receiver|tx-nr|"
                               r"nest-?hub|google-?nest-?hub|chromecast|roku|appletv|apple-?tv|"
                               r"\btv\b|netradio|vizio|lg-?tv)", re.I)),
    ("printer",     re.compile(r"(bambu|prusa|octoprint|printer|\bp1[ps]\b|x1c)", re.I)),
    ("iot",         re.compile(r"(koogeek|meross|lutron|esp32|esp32s3|slzb|xiaomi|aqara|"
                               r"presence-?sensor|fp2|adt|matter|thermostat|plug|switch|"
                               r"sensor|hue|body-?comp|body-?smart|beamo|weatherstation|"
                               r"hdhr|nintendo|meross|kasa|tapo|sonoff|ring|ecobee)", re.I)),
]


def classify_device(name: str) -> str:
    """Return device class: trusted | camera | smart-tv-av | printer | iot | unknown."""
    if not name or name.strip().lower() in ("unknown", ""):
        return "unknown"
    if _TRUSTED_RE.search(name):
        return "trusted"
    for cls, rx in _CLASS_RULES:
        if rx.search(name):
            return cls
    return "unknown"


# Mic / camera / AV classes — the Weeping-Angel / Dumbo "sensor" surface.
_SENSOR_CLASSES = ("camera", "smart-tv-av")


def log(m):
    print(f"[vault7-ttp {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def _recently_alerted(conn, dedup_key, hours=24) -> bool:
    """True if we already emitted this exact finding in the last `hours` — so a standing
    condition never re-triages the LLM or re-pages every 2-minute bridge cycle."""
    with conn.cursor() as c:
        c.execute("SELECT 1 FROM telemetry.events WHERE dedup_key=%s "
                  "AND ts > now() - make_interval(hours => %s) LIMIT 1", (dedup_key, hours))
        return c.fetchone() is not None


# ── Rule 1: firmware / boot / EFI tampering ───────────────────────────────────
_FW_RE = re.compile(r"(/boot|/efi|efi|firmware|grub|initramfs|initrd|vmlinuz|"
                    r"kernel\s*module|\.ko\b|rootkit|bootloader|uboot|u-boot)", re.I)

def rule_firmware_tamper(conn):
    findings = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(
            "SELECT id, agent_name, rule_description, rule_groups, full_log, ts "
            "FROM security_events WHERE ts > now() - interval '15 minutes' "
            "AND (rule_groups && ARRAY['syscheck','syscheck_entry_modified',"
            "'syscheck_entry_added','rootcheck']) ORDER BY ts DESC LIMIT 200")
        for e in c.fetchall():
            blob = f"{e['rule_description']} {e['full_log'] or ''}"
            if not _FW_RE.search(blob):
                continue
            is_rootkit = "rootcheck" in (e["rule_groups"] or [])
            # A dpkg/apt op on the same host in the same window explains a legit
            # FIM change (kernel/firmware package update) — not an implant.
            c2 = conn.cursor()
            c2.execute("SELECT 1 FROM security_events WHERE agent_name=%s "
                       "AND rule_groups && ARRAY['dpkg','config_changed'] "
                       "AND ts BETWEEN %s - interval '12 min' AND %s + interval '12 min' LIMIT 1",
                       (e["agent_name"], e["ts"], e["ts"]))
            explained = c2.fetchone() is not None
            c2.close()
            if explained and not is_rootkit:
                continue
            findings.append({
                "rule": "v7_firmware_tamper", "host": e["agent_name"],
                "level": "warning" if is_rootkit else "info",
                "title": f"Firmware/boot integrity change on {e['agent_name']}"
                         + (" (rootcheck)" if is_rootkit else ""),
                "detail": (e["rule_description"] or "")[:200],
                "why": "Vault-7 CherryBlossom flashed router/AP firmware OTA; DarkMatter/Sonic "
                       "Screwdriver infected Mac EFI. A boot/firmware/kernel-module change not "
                       "explained by a package op is the implant fingerprint."})
    return findings


# ── Rule 2: anti-forensic log/audit tampering ─────────────────────────────────
_AF_RE = re.compile(r"(audit(d)?\b.{0,30}(stop|disabl|kill)|"
                    r"(stop|disabl|kill).{0,20}audit|"
                    r"rsyslog.{0,20}(stop|kill)|journal.{0,20}(vacuum|rotate --vacuum|--flush)|"
                    r"\bhistory -c\b|truncate.{0,15}\.log|"
                    r"(rm|shred|>)\s*/var/log|log.{0,10}clear|cleared the audit|"
                    r"wtmp|lastlog.{0,10}(delet|clear)|auditctl -e0)", re.I)

def rule_antiforensic_gap(conn):
    findings = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(
            "SELECT id, agent_name, rule_description, full_log, ts FROM security_events "
            "WHERE ts > now() - interval '15 minutes' ORDER BY ts DESC LIMIT 400")
        for e in c.fetchall():
            blob = f"{e['rule_description']} {e['full_log'] or ''}"
            if _AF_RE.search(blob):
                findings.append({
                    "rule": "v7_antiforensic_gap", "host": e["agent_name"], "level": "warning",
                    "title": f"Anti-forensic log/audit tampering on {e['agent_name']}",
                    "detail": (e["rule_description"] or blob)[:200],
                    "why": "Vault-7 Marble obfuscated and Dumbo suspended sensors/corrupted "
                           "recordings — implants blind the logs before acting. Audit/log "
                           "disable, clear, or truncate is that fingerprint."})
    return findings


# ── Rule 3: IoT/camera device beaconing to C2 ─────────────────────────────────
_C2_RE = re.compile(r"(suspicious.?dns|c2\b|command.?and.?control|beacon|dga|"
                    r"\.(xyz|top|tk|gq|ml|cf|ru|su)\b|newly.?registered|dns.?tunnel)", re.I)

def rule_implant_beacon(conn):
    findings = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        # Map recent device IPs -> class, then look for C2/DNS detections sourced there.
        c.execute("SELECT DISTINCT ON (ip) ip, client_name FROM telemetry.network "
                  "WHERE ts > now() - interval '2 days' AND ip <> '' ORDER BY ip, ts DESC")
        ipclass = {r["ip"]: (classify_device(r["client_name"]), r["client_name"])
                   for r in c.fetchall()}
        c.execute(
            "SELECT id, agent_name, rule_description, full_log, src_ip, ts FROM security_events "
            "WHERE ts > now() - interval '15 minutes' AND src_ip IS NOT NULL "
            "ORDER BY ts DESC LIMIT 400")
        for e in c.fetchall():
            cls, name = ipclass.get(e["src_ip"], (None, None))
            if cls not in ("iot", "camera", "smart-tv-av", "printer"):
                continue
            blob = f"{e['rule_description']} {e['full_log'] or ''}"
            if _C2_RE.search(blob):
                findings.append({
                    "rule": "v7_implant_beacon", "host": f"{name or e['src_ip']} ({e['src_ip']})",
                    "level": "warning",
                    "title": f"IoT/{cls} device beaconing to suspected C2: {name or e['src_ip']}",
                    "detail": (e["rule_description"] or "")[:200],
                    "why": "Vault-7 CherryBlossom Flytraps and HIVE implants beaconed to a C2 on "
                           "intervals. A low-trust IoT/camera device as the SOURCE of a C2/"
                           "suspicious-DNS detection is that fingerprint."})
    return findings


# ── Rule 4: smart-TV / AV device "fake-off" exfil ─────────────────────────────
def rule_smart_tv_fakeoff(conn):
    findings = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        # Per-device quiet-window (02:00-05:00) upload, split into RECENT (last 3
        # nights) vs the device's OWN trailing-30-day BASELINE. We flag a SPIKE
        # relative to the device's own history — not raw volume — so cloud cameras
        # and always-on hubs (steady nightly upload = high, flat baseline) don't trip.
        # Cameras are excluded entirely below: cloud upload IS their function. The
        # target is a TV/soundbar/AV device that is normally quiet at 3am and
        # suddenly uploads (outbound-dominant, the inverse of streaming/updates).
        c.execute("""
            WITH q AS (
                SELECT client_mac,
                       date_trunc('day', ts AT TIME ZONE 'America/Los_Angeles') AS d,
                       max(client_name) AS name,
                       sum(tx_bytes) AS tx, sum(rx_bytes) AS rx
                FROM telemetry.network
                WHERE ts > now() - interval '33 days'
                  AND extract(hour FROM ts AT TIME ZONE 'America/Los_Angeles') BETWEEN 2 AND 4
                GROUP BY client_mac, d
            ),
            recent AS (
                SELECT client_mac, max(name) AS name, avg(tx) AS rtx,
                       sum(tx) AS tx_sum, sum(rx) AS rx_sum, count(*) AS nights
                FROM q WHERE d > (now() - interval '3 days') GROUP BY client_mac
            ),
            base AS (
                SELECT client_mac, avg(tx) AS btx
                FROM q WHERE d <= (now() - interval '3 days') GROUP BY client_mac
            )
            SELECT r.client_mac, r.name, r.rtx, r.tx_sum, r.rx_sum, r.nights, b.btx
            FROM recent r JOIN base b USING (client_mac)
            WHERE r.rtx > 20*1024*1024                      -- >20 MB/night uploaded (volume floor)
              AND r.tx_sum > 2 * GREATEST(r.rx_sum, 1)      -- outbound-dominant (exfil-shaped)
              AND r.rtx >= 5 * GREATEST(b.btx, 2*1024*1024) -- >=5x the device's OWN 30-day norm
        """)
        for r in c.fetchall():
            cls = classify_device(r["name"])
            if cls != "smart-tv-av":     # cameras excluded — cloud upload is their job
                continue
            findings.append({
                "rule": "v7_smart_tv_fakeoff", "host": r["name"] or r["client_mac"],
                "level": "warning",
                "title": f"'Fake-off' exfil spike on AV/TV device: {r['name'] or r['client_mac']}",
                "detail": f"{int(r['rtx'])//1048576}MB/night up (02:00-05:00) vs "
                          f"{int(r['btx'])//1048576}MB/night baseline — {r['rtx']/max(r['btx'],1):.0f}x "
                          f"spike, outbound-dominant, {r['nights']} night(s).",
                "why": "Vault-7 Weeping Angel put Samsung TVs in 'Fake-Off': appearing powered down "
                       "while recording mic audio and exfiltrating. A TV/AV device that is normally "
                       "quiet at 3am suddenly uploading far more than it downloads is that "
                       "fingerprint (baseline-relative, so steady cloud uploaders don't false-fire)."})
    return findings


# ── Rule 5: rogue persistence / identity crossing the trust boundary ──────────
_PERSIST_RE = re.compile(r"(new .{0,15}(systemd|service unit|cron|crontab|launchd|launchagent|"
                         r"launchdaemon)|rc\.local|/etc/init\.d|added to startup|"
                         r"persistence|autostart|/Library/LaunchAgents|/Library/LaunchDaemons|"
                         r"@reboot)", re.I)

def rule_rogue_persistence(conn):
    findings = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        # (a) new persistence unit outside maintenance
        c.execute(
            "SELECT id, agent_name, rule_description, full_log, ts FROM security_events "
            "WHERE ts > now() - interval '15 minutes' "
            "AND (rule_groups && ARRAY['systemd','config_changed','ossec']) "
            "ORDER BY ts DESC LIMIT 300")
        for e in c.fetchall():
            blob = f"{e['rule_description']} {e['full_log'] or ''}"
            if _PERSIST_RE.search(blob):
                findings.append({
                    "rule": "v7_rogue_persistence", "host": e["agent_name"], "level": "info",
                    "title": f"New persistence mechanism on {e['agent_name']}",
                    "detail": (e["rule_description"] or "")[:200],
                    "why": "Vault-7 Grasshopper built custom persistence; Achilles trojaned macOS "
                           ".dmg installers. A new systemd/cron/launchd/rc.local persistence unit "
                           "outside a maintenance window is that fingerprint."})
        # (b) trusted-host identity appearing on the IoT Wi-Fi (pivot enabler)
        c.execute(
            "SELECT DISTINCT client_name, essid, ip FROM telemetry.network "
            "WHERE ts > now() - interval '30 minutes' AND essid = ANY(%s) "
            "AND client_name IS NOT NULL AND client_name <> 'unknown'", (list(IOT_SSID),))
        for r in c.fetchall():
            if classify_device(r["client_name"]) == "trusted":
                findings.append({
                    "rule": "v7_rogue_persistence",
                    "host": f"{r['client_name']} on {r['essid']}", "level": "info",
                    "title": f"Trusted-host identity on IoT Wi-Fi: {r['client_name']} @ {r['essid']}",
                    "detail": f"{r['client_name']} ({r['ip']}) associated to IoT SSID {r['essid']}.",
                    "why": "A trusted-host name on the IoT segment is either mis-segmentation or an "
                           "identity-spoof enabling the Weeping-Angel pivot from a compromised IoT "
                           "device into trusted hosts."})
    return findings


RULES = [rule_firmware_tamper, rule_antiforensic_gap, rule_implant_beacon,
         rule_smart_tv_fakeoff, rule_rogue_persistence]

_CATALOG = [
    ("v7_firmware_tamper",   "CherryBlossom / DarkMatter / Sonic Screwdriver — OTA firmware / Mac EFI implant"),
    ("v7_antiforensic_gap",  "Marble / Dumbo — audit/log disable, clear, or truncate (blinding the sensors)"),
    ("v7_implant_beacon",    "HIVE / CherryBlossom Flytrap — IoT/camera device beaconing to a C2"),
    ("v7_smart_tv_fakeoff",  "Weeping Angel — TV/AV/mic device outbound-dominant exfil during quiet hours"),
    ("v7_rogue_persistence", "Grasshopper / Achilles / Aeris — new persistence unit or trusted ID on IoT Wi-Fi"),
]


def scan(conn, do_alert=False, logger=log):
    """Run every rule. Emits (triage + notify) each NEW finding at most once/24h when
    do_alert=True. Returns the list of findings. Never raises out to the caller."""
    if nova_maintenance and nova_maintenance.is_active():
        logger("maintenance window active — skipping Vault-7 TTP scan")
        return []
    all_findings = []
    for rule in RULES:
        try:
            all_findings.extend(rule(conn) or [])
        except Exception as e:
            logger(f"{rule.__name__} failed (skipped): {e}")
    for f in all_findings:
        host_key = re.sub(r"\s+", "_", str(f["host"]).lower())[:40]
        f["dedup_key"] = f"vault7:{f['rule']}:{host_key}"
    if not do_alert:
        return all_findings
    emitted = 0
    for f in all_findings:
        try:
            if _recently_alerted(conn, f["dedup_key"]):
                continue
            body = f"{f['detail']}\n\nWhy this fired (Vault-7 rationale): {f['why']}"
            level = f["level"]
            ann = ""
            if triage:
                try:
                    d = triage(f["title"], body=body, level=level, category="security",
                               source="vault7_ttp", dedup_key=f["dedup_key"])
                    ann, level = d.get("annotation", ""), d.get("level", level)
                except Exception as e:
                    logger(f"triage failed for {f['rule']} (emitting anyway): {e}")
            notify(f["title"], body=(body + (f"\n\n{ann}" if ann else "")), level=level,
                   category="security", source="vault7_ttp", dedup_key=f["dedup_key"],
                   meta={"rule": f["rule"], "host": str(f["host"])})
            emitted += 1
        except Exception as e:
            logger(f"emit failed for {f.get('rule')}: {e}")
    logger(f"scan: {len(all_findings)} finding(s), {emitted} newly alerted")
    return all_findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="show the rule catalog + rationale")
    ap.add_argument("--scan", action="store_true", help="run all rules once")
    ap.add_argument("--alert", action="store_true", help="with --scan, emit via triage/notify")
    a = ap.parse_args()
    if a.list or not a.scan:
        print("Vault-7 TTP behavioral detection rules:")
        for rid, desc in _CATALOG:
            print(f"  {rid:22} {desc}")
        return 0
    conn = psycopg2.connect(DSN)
    findings = scan(conn, do_alert=a.alert)
    for f in findings:
        print(f"  [{f['level']:7}] {f['rule']:22} {f['title']}")
        print(f"            {f['detail']}")
    print(f"\n{len(findings)} finding(s).")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
