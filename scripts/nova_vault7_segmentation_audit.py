#!/usr/bin/env python3
"""nova_vault7_segmentation_audit.py — READ-ONLY IoT/trusted co-residency audit.

Vault-7 Weeping Angel turned a Samsung TV into a listening post; CherryBlossom did the
same to routers/APs. The danger isn't just the compromised device — it's the PIVOT: a
popped IoT device on the same L2/L3 segment as your Macs, laptop, and phone can ARP-
scan, spoof, and reach trusted hosts directly. Network segmentation is the control that
contains that blast radius.

This audits which IoT/camera/smart-TV devices currently share a VLAN/subnet with
trusted hosts, using UniFi client/network data (telemetry.network — the same source
nova_unifi_poller.py fills) + telemetry.known_devices, and produces a PRIORITIZED
recommendation for which device classes to move to an isolated IoT VLAN, and why.

STRICTLY READ-ONLY. It changes NO network / VLAN / firewall / DNS config — segmentation
is Jordan's to action. Output: a report written to agent_docs
(doc_type='vault7-segmentation-audit', agent_id='all') + a one-shot Slack summary.

    nova_vault7_segmentation_audit.py            # audit, write doc, post summary
    nova_vault7_segmentation_audit.py --dry-run  # print only, no DB write / no post
"""
import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
from nova_vault7_ttp import classify_device, IOT_SSID
import nova_config

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
ACTIVE_WINDOW = "14 days"

# Risk priority + rationale per device class (Vault-7 relevance).
CLASS_RISK = {
    "camera":      ("P1", "Cameras carry a mic + video feed and a long history of remotely-"
                          "exploitable firmware (CherryBlossom-style). A popped camera on the "
                          "trusted subnet is both a listening post and a pivot box."),
    "smart-tv-av": ("P1", "Smart TVs / soundbars / AV hubs have always-on microphones — the exact "
                          "Weeping Angel target. On the trusted subnet they can reach Macs/phones "
                          "directly."),
    "iot":         ("P2", "Small IoT (plugs, sensors, Zigbee/Z-Wave bridges, bulbs) run minimal, "
                          "rarely-patched firmware and are the easiest initial foothold; large "
                          "count multiplies exposure."),
    "printer":     ("P2", "Network printers (Bambu) expose services and firmware update paths and "
                          "have no business initiating connections to trusted hosts."),
    "unknown":     ("P3", "Unclassified/again-randomized-MAC clients — audit and label; unknowns "
                          "on the trusted subnet are an ungoverned surface."),
}
UNTRUSTED = ("camera", "smart-tv-av", "iot", "printer", "unknown")


def log(m):
    print(f"[seg-audit {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def _subnet(ip):
    p = (ip or "").split(".")
    return f"{p[0]}.{p[1]}.{p[2]}.0/24" if len(p) == 4 and all(p) else None


def gather(conn):
    """Return per-device rows: {mac, name, ip, subnet, essid, wired, class}."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # Latest observation per MAC in the active window (name/ip/essid/wired).
    cur.execute(f"""
        SELECT DISTINCT ON (client_mac) client_mac, client_name, ip, essid, is_wired
        FROM telemetry.network
        WHERE ts > now() - interval '{ACTIVE_WINDOW}'
        ORDER BY client_mac, ts DESC
    """)
    rows = []
    for r in cur.fetchall():
        # Fall back to known_devices name if the network row is unlabeled.
        name = r["client_name"]
        if not name or name == "unknown":
            cur.execute("SELECT client_name FROM telemetry.known_devices WHERE client_mac=%s "
                        "AND client_name IS NOT NULL AND client_name<>'unknown' LIMIT 1",
                        (r["client_mac"],))
            k = cur.fetchone()
            if k:
                name = k["client_name"]
        rows.append({
            "mac": r["client_mac"], "name": name or "unknown", "ip": r["ip"] or "",
            "subnet": _subnet(r["ip"]), "essid": r["essid"] or ("(wired)" if r["is_wired"] else ""),
            "wired": r["is_wired"], "cls": classify_device(name)})
    cur.close()
    return rows


def build_report(rows):
    active = [r for r in rows if r["subnet"]]
    trusted = [r for r in active if r["cls"] == "trusted"]
    trusted_subnets = sorted({r["subnet"] for r in trusted})
    by_class = defaultdict(list)
    for r in active:
        by_class[r["cls"]].append(r)

    # Co-resident = untrusted device on a subnet that also hosts trusted hosts.
    coresident = [r for r in active
                  if r["cls"] in UNTRUSTED and r["subnet"] in set(trusted_subnets)]
    coresident_by_class = defaultdict(list)
    for r in coresident:
        coresident_by_class[r["cls"]].append(r)

    # SSID-vs-subnet: is there real L3 separation, or just SSID separation on one subnet?
    ssid_subnets = defaultdict(set)
    for r in active:
        if r["essid"] and r["essid"] != "(wired)":
            ssid_subnets[r["essid"]].add(r["subnet"])
    iot_ssid_subnets = sorted({s for e in IOT_SSID for s in ssid_subnets.get(e, set())})
    flat = bool(iot_ssid_subnets) and set(iot_ssid_subnets) <= set(trusted_subnets)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M %Z")
    L = []
    L.append(f"# Vault-7 Segmentation Audit — {ts}")
    L.append("")
    L.append("**Scope:** READ-ONLY. This is a recommendation. No network/VLAN/firewall/DNS "
             "config was changed. Segmentation is Jordan's to action.")
    L.append("")
    L.append("## Verdict")
    if flat:
        L.append(f"**FLAT NETWORK — no L3 segmentation.** The IoT Wi-Fi SSID(s) "
                 f"{', '.join(IOT_SSID)} resolve to the SAME subnet(s) "
                 f"({', '.join(iot_ssid_subnets)}) as the trusted hosts. SSID separation "
                 f"without a distinct VLAN/subnet gives NO L2/L3 isolation — every IoT, camera, "
                 f"and smart-TV device can ARP/scan/reach the Macs, laptop, and phone directly. "
                 f"This is the Weeping-Angel pivot risk, unmitigated.")
    else:
        L.append("Trusted and IoT subnets appear at least partly separated — see per-subnet "
                 "breakdown below and close any remaining co-residency.")
    L.append("")
    L.append(f"- Active devices (last {ACTIVE_WINDOW}): **{len(active)}**")
    L.append(f"- Trusted hosts: **{len(trusted)}** on subnet(s) {', '.join(trusted_subnets) or 'n/a'}")
    L.append(f"- Untrusted devices co-resident with trusted hosts: **{len(coresident)}**")
    L.append("")

    L.append("## Co-resident untrusted devices, by class (prioritized)")
    order = sorted(coresident_by_class, key=lambda c: CLASS_RISK.get(c, ("P9",))[0])
    for cls in order:
        pr, why = CLASS_RISK.get(cls, ("P3", ""))
        devs = coresident_by_class[cls]
        L.append(f"### [{pr}] {cls} — {len(devs)} device(s)")
        L.append(f"_{why}_")
        named = sorted({d["name"] for d in devs if d["name"] != "unknown"})
        sample = ", ".join(named[:18]) + (f", +{len(named)-18} more" if len(named) > 18 else "")
        if named:
            L.append(f"Examples: {sample}")
        if cls == "unknown":
            L.append(f"({len(devs)} unlabeled/randomized-MAC clients — many are transient phones; "
                     f"label the persistent ones and treat the rest as untrusted.)")
        L.append("")

    L.append("## Prioritized recommendation")
    L.append("1. **Create a dedicated IoT VLAN + subnet** (e.g. VLAN 20 / 192.168.20.0/24) in "
             "UniFi and bind the existing IoT SSID(s) to it — SSID alone is not isolation.")
    L.append("2. **Move P1 first — cameras and smart-TV/AV/mic devices.** These are the direct "
             "Weeping Angel / CherryBlossom surveillance-and-pivot targets.")
    L.append("3. **Then P2 — small IoT (plugs, sensors, Zigbee/Z-Wave bridges, bulbs) and the "
             "Bambu printer(s).** Largest count, weakest firmware.")
    L.append("4. **Firewall the IoT VLAN:** allow only internet + the specific hubs it needs "
             "(Home Assistant, mDNS reflector); DENY IoT-VLAN → trusted-VLAN by default. This is "
             "what actually stops the pivot.")
    L.append("5. **Keep trusted hosts (Macs, laptop, phone, nova-core fleet, NAS) on the trusted "
             "VLAN**; put guests on their own.")
    L.append("6. **Re-run this audit after the move** to confirm zero P1/P2 co-residency.")
    L.append("")
    L.append("_Generated by nova_vault7_segmentation_audit.py (read-only). Detection of an actual "
             "pivot/beacon is handled continuously by nova_vault7_ttp.py._")

    summary = (f":shield: *Vault-7 Segmentation Audit* — "
               + ("FLAT network, no L3 isolation. " if flat else "")
               + f"{len(coresident)} untrusted device(s) share the trusted subnet "
                 f"({', '.join(trusted_subnets) or 'n/a'}).\n"
               + "Top risk classes co-resident with your Macs/phone: "
               + ", ".join(f"{c} ({len(coresident_by_class[c])}, {CLASS_RISK.get(c,('P3',))[0]})"
                           for c in order[:4])
               + ".\nRecommendation: dedicated IoT VLAN+subnet, move cameras & smart-TVs (P1) "
                 "first, deny IoT→trusted. Full report in agent_docs "
                 "(doc_type=vault7-segmentation-audit). READ-ONLY — no config changed.")
    stats = {"active": len(active), "coresident": len(coresident), "flat": flat,
             "trusted_subnets": trusted_subnets,
             "by_class": {c: len(v) for c, v in coresident_by_class.items()}}
    return "\n".join(L), summary, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print only; no DB write / no Slack post")
    a = ap.parse_args()

    conn = psycopg2.connect(DSN)
    rows = gather(conn)
    report, summary, stats = build_report(rows)
    log(f"audited {stats['active']} active devices; {stats['coresident']} untrusted co-resident; "
        f"flat={stats['flat']}")

    if a.dry_run:
        print("\n" + report + "\n\n--- SLACK SUMMARY ---\n" + summary)
        conn.close()
        return 0

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO agent_docs (agent_id, doc_type, content, version, updated_at) "
        "VALUES ('all', 'vault7-segmentation-audit', %s, 1, %s) "
        "ON CONFLICT (agent_id, doc_type) DO UPDATE SET content=EXCLUDED.content, "
        "version=agent_docs.version+1, updated_at=EXCLUDED.updated_at",
        (report, int(time.time())))
    conn.commit()
    cur.close()
    log("report written to agent_docs (doc_type=vault7-segmentation-audit, agent_id=all)")

    try:
        nova_config.post_both(summary, slack_channel=nova_config.SLACK_BB)
        log("posted one-shot summary to Slack")
    except Exception as e:
        log(f"Slack post failed (report still saved): {e}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
