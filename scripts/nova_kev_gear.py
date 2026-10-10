#!/usr/bin/env python3
"""nova_kev_gear.py — CISA-KEV cross-referenced against Jordan's ACTUAL gear.

Vault-7 lesson (2017 CIA leak): hoarded 0-days eventually become public. The only
durable defense is being PATCHED on the specific gear you actually run before the
exploit is weaponized at scale. CISA's Known-Exploited-Vulnerabilities (KEV) catalog
is exactly the "now being exploited in the wild" list.

nova_vendor_advisories.py already keyword-matches KEV against a STATIC fleet keyword
list and posts news. This is the INVENTORY-DRIVEN half: it builds Jordan's real
device/software inventory from telemetry.known_devices (device vendors/brands seen on
the LAN) + a curated fleet-software list (UniFi/Synology/Ubuntu/Grafana/Wazuh/...),
then fires a TARGETED alert ONLY when a KEV entry matches gear he actually has — and
never pages on KEVs for gear he doesn't own.

Behaviour:
  * Nightly. Fetch KEV JSON (stdlib only, no key).
  * Build inventory: curated software vendors + dynamic device brand tokens from PG.
  * Match KEV vendorProject/product (word-boundary) against inventory.
  * Persist every match to nova_ops.kev_matches (UNIQUE cve_id+matched_asset).
  * FIRST run baselines silently (records current matches, no page — same discipline
    as nova_vendor_advisories) so we don't flood on the whole historical KEV.
  * Thereafter, NEW matches are enriched through nova_alert_triage.triage()
    (category='security', source='kev_gear') and paged as ONE aggregated alert.

Read-only against the network. Alerting only.
"""
import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
from nova_notify import notify
try:
    from nova_alert_triage import triage
except Exception:
    triage = None

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"

# ── Curated fleet SOFTWARE / infra vendors Jordan actually runs ───────────────
# token -> (canonical asset label, device class, product_require_regex_or_None).
# Tokens match as WHOLE WORDS against KEV vendorProject+product. Keep this scoped to
# gear with real evidence (telemetry.known_devices) or Jordan's stated fleet — the
# mandate is "don't page on KEVs for gear he doesn't have." (Apple is handled
# separately, vendor-scoped, so "Cisco IOS" can't masquerade as Apple iOS.)
FLEET_SOFTWARE = {
    "ubiquiti":       ("Ubiquiti / UniFi", "network", None),
    "unifi":          ("Ubiquiti / UniFi", "network", None),
    "udm":            ("UniFi Dream Machine", "network", None),
    "unas":           ("UniFi NAS (UNAS-Pro-8)", "nas", None),
    "unvr":           ("UniFi NVR", "camera-nvr", None),
    "edgerouter":     ("Ubiquiti EdgeRouter", "network", None),
    "synology":       ("Synology NAS", "nas", None),
    "diskstation":    ("Synology NAS", "nas", None),
    "ubuntu":         ("Ubuntu (fleet Linux)", "host-os", None),
    "canonical":      ("Ubuntu (fleet Linux)", "host-os", None),
    "grafana":        ("Grafana", "software", None),
    "wazuh":          ("Wazuh SIEM", "software", None),
    "docker":         ("Docker", "software", None),
    "postgresql":     ("PostgreSQL", "software", None),
    "postgres":       ("PostgreSQL", "software", None),
    "ollama":         ("Ollama", "software", None),
    "plex":           ("Plex", "software", None),
    "homeassistant":  ("Home Assistant", "iot-hub", None),
    "frigate":        ("Frigate NVR", "camera-nvr", None),
    "redis":          ("Redis", "software", None),
    # consumer / IoT Jordan actually runs (evidenced in known_devices or stated fleet)
    "bose":           ("Bose Smart Soundbar", "smart-audio", None),
    "onkyo":          ("Onkyo AV receiver", "smart-audio", None),
    "nest":           ("Google Nest cam/hub/thermostat", "camera-iot", None),
    "chromecast":     ("Google Chromecast/Hub", "smart-av", None),
    "lutron":         ("Lutron lighting", "iot", None),
    "meross":         ("Meross smart plug", "iot", None),
    "koogeek":        ("Koogeek smart switch", "iot", None),
    "espressif":      ("ESP32 devices", "iot", None),
    "esp32":          ("ESP32 devices", "iot", None),
    "xiaomi":         ("Xiaomi device", "iot", None),
    "nintendo":       ("Nintendo console", "console", None),
    "bambu":          ("Bambu Lab printer", "printer", None),
    "withings":       ("Withings body sensor", "iot", None),
    # Smart TV — he has a Samsung TV, NOT Samsung phones/MagicINFO signage. Require a
    # TV-ish product word so the Samsung-mobile / MagicINFO KEV flood doesn't page.
    "samsung":        ("Samsung Smart TV", "smart-tv", r"\b(tv|tizen|frame|qled|the\s*frame)\b"),
    # Philips Hue — require 'hue' (bare 'philips' is too broad).
    "hue":            ("Philips Hue lighting", "iot", None),
}

# Apple is matched by VENDOR, not product token — otherwise "Cisco IOS" hits "ios".
_APPLE_RE = re.compile(r"(?<![a-z])apple(?![a-z])", re.I)

# Tokens too generic / location-y to trust as a KEV match key when auto-derived
# from device names. (Curated FLEET_SOFTWARE tokens above are always trusted.)
_STOP = {
    "unknown", "mac", "macbookpro", "macmini", "imac", "ipad", "iphone", "watch",
    "office", "kitchen", "garage", "outside", "living", "master", "interior",
    "exterior", "external", "rack", "nova", "core", "ubuntu", "server", "pod",
    "room", "bath", "gbath", "mbath", "front", "back", "presence", "sensor",
    "body", "comp", "smart", "group", "inc", "corporation", "ltd", "electronics",
    "controls", "technology", "software", "digitalnoise", "net", "abit", "arris",
    "micrilor", "rosemount", "sai", "afikim", "incostartec", "gmbh", "beijing",
    "mobile", "co", "outpod", "netradio", "weatherstation", "hdhr", "hpc",
    "beamo", "outside", "left", "right", "middle", "north", "south", "door",
    "blur", "top", "fridge", "couch", "patio", "carport", "alley", "garbage",
    "backyard", "abundio", "laundry", "printers", "dylan", "dylans", "amys",
    "jordans", "jordan", "master-bedroom", "masterbedroom",
}

# Brand tokens worth trusting even though short/derived — real vendors on the LAN.
# Deliberately excludes over-broad tokens ("google" → all Chromium/Chrome CVEs) and
# camera brands with no inventory evidence (hikvision/dahua/reolink/tp-link/netgear).
_TRUST_DERIVED = {
    "bose", "onkyo", "nest", "koogeek", "meross", "lutron", "xiaomi", "nintendo",
    "synology", "ubiquiti", "unifi", "unas", "bambu", "withings", "aqara", "sonos",
}


def log(m):
    print(f"[kev-gear {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def _get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": "nova-kev-gear/1.0 (+fleet defense)"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def build_inventory(conn):
    """Return {token: (label, class)} of gear Jordan actually runs."""
    inv = dict(FLEET_SOFTWARE)
    cur = conn.cursor()
    # Dynamic device brand tokens from what's actually been seen on the LAN.
    cur.execute(
        "SELECT DISTINCT lower(client_name) FROM telemetry.known_devices "
        "WHERE client_name IS NOT NULL AND client_name <> 'unknown'")
    for (name,) in cur.fetchall():
        if not name:
            continue
        for tok in re.split(r"[\s\-_.,]+", name):
            tok = tok.strip().lower()
            if len(tok) < 3 or tok in _STOP or tok.isdigit():
                continue
            if not re.match(r"^[a-z][a-z0-9]{2,}$", tok):
                continue
            if tok in inv:
                continue
            if tok in _TRUST_DERIVED:
                inv[tok] = (name, "iot-device", None)
    cur.close()
    return inv


def match_kev(kev, inv):
    """Yield (cve, vp, product, vuln, matched_token, label, cls, dateAdded, due, ransom)."""
    # Precompile word-boundary matchers per token (+ optional product-require regex).
    matchers = []
    for tok, val in inv.items():
        lbl, cls = val[0], val[1]
        require = val[2] if len(val) > 2 else None
        matchers.append((tok, re.compile(r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])", re.I),
                         lbl, cls, re.compile(require, re.I) if require else None))
    for v in kev.get("vulnerabilities", []):
        vp = v.get("vendorProject", "") or ""
        product = v.get("product", "") or ""
        # Match on vendor+product ONLY (precise) — not the free-text vuln name,
        # which mentions unrelated vendors and inflates false positives.
        blob = f"{vp} {product}"
        row = None
        # Apple is vendor-scoped so "Cisco IOS/IOS XE" can't pose as Apple iOS.
        if _APPLE_RE.search(vp):
            row = ("apple", "Apple (Mac / iPhone / iPad fleet)", "endpoint")
        else:
            for tok, rx, lbl, cls, req in matchers:
                if rx.search(blob) and (req is None or req.search(blob)):
                    row = (tok, lbl, cls)
                    break
        if row:
            yield (v.get("cveID", ""), vp, product, v.get("vulnerabilityName", ""),
                   row[0], row[1], row[2], v.get("dateAdded", ""), v.get("dueDate", ""),
                   v.get("knownRansomwareCampaignUse", ""))


def ensure_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kev_matches (
            id             bigserial PRIMARY KEY,
            cve_id         text NOT NULL,
            vendor_project text,
            product        text,
            vuln_name      text,
            matched_token  text,
            matched_asset  text,
            asset_class    text,
            date_added     text,
            due_date       text,
            ransomware     text,
            first_matched  timestamptz NOT NULL DEFAULT now(),
            alerted        boolean NOT NULL DEFAULT false,
            UNIQUE (cve_id, matched_token)
        )""")
    conn.commit()
    cur.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="match + print, no DB writes / no alert")
    a = ap.parse_args()

    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    ensure_table(conn)

    inv = build_inventory(conn)
    log(f"inventory: {len(inv)} gear tokens ({sum(1 for _ in FLEET_SOFTWARE)} curated + "
        f"{len(inv) - len(FLEET_SOFTWARE)} derived from known_devices)")

    try:
        kev = json.loads(_get(KEV_URL))
    except Exception as e:
        log(f"KEV fetch failed: {e}")
        notify("KEV-gear fetch failed", body=str(e)[:300], level="warning",
               category="security", source="kev_gear", dedup_key="kev-gear-fetch-fail")
        conn.close()
        return 1

    total_kev = len(kev.get("vulnerabilities", []))
    matches = list(match_kev(kev, inv))
    log(f"{len(matches)} KEV entries match Jordan's gear (of {total_kev} in catalog)")

    if a.dry_run:
        for m in matches[:60]:
            print(f"  {m[0]:16} {m[5]:34} <- {m[4]:12} ({m[1]} {m[2]})")
        print(f"... ({len(matches)} total) — dry run, nothing written")
        conn.close()
        return 0

    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM kev_matches")
    baseline = cur.fetchone()[0] == 0  # first-ever run → baseline silently

    new_rows = []
    for (cve, vp, product, vuln, tok, lbl, cls, dadd, due, ransom) in matches:
        cur.execute(
            "INSERT INTO kev_matches (cve_id, vendor_project, product, vuln_name, "
            "matched_token, matched_asset, asset_class, date_added, due_date, ransomware, "
            "alerted) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (cve_id, matched_token) DO NOTHING RETURNING id",
            (cve, vp, product, vuln[:500], tok, lbl, cls, dadd, due, ransom, baseline))
        if cur.fetchone():  # actually inserted → new
            new_rows.append((cve, lbl, cls, product, vuln, ransom, due))
    conn.commit()

    if baseline:
        log(f"BASELINE run — recorded {len(new_rows)} current matches, no alert (as designed)")
        conn.close()
        return 0

    if not new_rows:
        log("no NEW KEV matches on Jordan's gear tonight")
        conn.close()
        return 0

    # Aggregate NEW matches into ONE targeted alert, enriched via triage.
    ranked = sorted(new_rows, key=lambda r: (r[5].lower() != "known", r[6] or "9999"))
    lines = []
    for cve, lbl, cls, product, vuln, ransom, due in ranked[:20]:
        rr = " ☣️ransomware" if (ransom or "").lower() == "known" else ""
        d = f" due:{due}" if due else ""
        lines.append(f"• *{cve}* — {lbl} [{cls}]{rr}{d}\n    {product}: {vuln[:110]}")
    body = (f"{len(new_rows)} newly-KEV-listed (actively-exploited) vuln(s) affect gear you "
            f"actually run. Patch these on YOUR devices:\n" + "\n".join(lines))
    title = f"KEV alert: {len(new_rows)} actively-exploited CVE(s) hit your gear"

    ann, level = "", "warning"
    if triage:
        try:
            d = triage(title, body=body, level="warning", category="security", source="kev_gear",
                       dedup_key="kev-gear-new")
            ann = d.get("annotation", "")
            level = d.get("level", "warning")
        except Exception as e:
            log(f"triage failed (paging anyway): {e}")
    notify(title, body=(body + (f"\n\n{ann}" if ann else "")), level=level,
           category="security", source="kev_gear", dedup_key="kev-gear-new",
           meta={"new": len(new_rows), "kev_total": total_kev})

    cur.execute("UPDATE kev_matches SET alerted=true WHERE alerted=false")
    conn.commit()
    log(f"alerted {len(new_rows)} new KEV match(es) on real gear")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
