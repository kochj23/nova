#!/usr/bin/env python3
"""nova_grafana_consolidate.py — build Nova's 9 big Grafana pages from the 56 source dashboards (Jordan 2026-10-13:
"I would rather look at larger pages with more graphs than more pages with fewer graphs").

Sources live in grafana/sources/<uid>.json (snapshot of every dashboard as of 2026-10-13, plus whatever
nova_grafana_coverage.py regenerates there). Each PAGE stacks its sources top to bottom, every source under an
expanded section row titled with the source's name; a source's own collapsed rows stay collapsed inside it.
Panels whose queries are identical to one already on the page are dropped, as are the dead ones in DROP.
Template variables are shared when identical and renamed per source when they clash.

Output: grafana/dashboards/<page uid>.json (the only files deployed). Deploy to nova-core with mode 644:
  nova_grafana_consolidate.py && scp grafana/dashboards/*.json nova-core:/tmp/g/ &&
  ssh nova-core 'install -m 644 /tmp/g/*.json ~/grafana/dashboards/'
Edit a page by editing its source (or PAGES below) and re-running — never the output.
"""
import copy
import json
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "grafana")
PAGES = [   # (uid, title, default range, [source uids in order])
    ("nova-fleet", "Fleet & Platform", "now-24h",
     ["nova-cluster", "nova-hosts-fleet", "fleet-health", "nova-kpis", "nova-probes-uptime", "nova-components",
      "nova-signal-health", "nova-notifications-incidents"]),
    ("nova-home", "Home", "now-24h",
     ["nova-home-overview", "nova-home-sensors", "nova-home-environment", "nova-home-sensors-ha", "homekit-sensors",
      "zigbee-mesh", "garden-air", "nova-fridge", "home-devices", "homekit-outlets", "hue-lights"]),
    ("nova-energy", "Energy & Power", "now-7d",
     ["nova-kwh", "smart-plugs-power", "laundry-room", "poe-power"]),
    ("nova-net", "Network", "now-24h",
     ["nova-network", "network-clients", "snmp-device-health", "snmp-switch-ports", "nova-dns", "nova-tools-unifi"]),
    ("nova-rf", "Radio, Airspace & Bluetooth", "now-24h",
     ["nova-tools-radio-wifi", "nova-scanner", "overhead-flights", "chp-traffic", "ble-traffic", "ble-churn"]),
    ("nova-sec", "Security", "now-7d",
     ["nova-security", "security-posture", "home-vehicle-security"]),
    ("nova-data", "Data & Storage", "now-7d",
     ["data-platform", "nova-postgres", "storage-unas", "snmp-storage", "nova-nas-backup", "nova-backup-service",
      "nas-dedup", "media-gardener"]),
    ("nova-mind", "Nova — Mind", "now-7d",
     ["nova-brain", "nova-interior", "nova-governance", "nova-tools-self", "nova-tools-watch"]),
    ("nova-work", "Nova — Work", "now-7d",
     ["nova-activity", "nova-tools-ops-safety", "nova-tools-media-ingest", "web-analytics"]),
]
RETIRED = {"home-scenes": "every panel empty for 30 days (scene activations no longer recorded)"}
DROP = {("home-vehicle-security", "Home scene activations — last 30 days")}   # same dead source as home-scenes
HOME_UID = "nova-fleet"


def _targets_sig(p):
    sqls = tuple(sorted((t.get("rawSql") or t.get("expr") or "").strip() for t in p.get("targets") or []))
    return (p.get("type"), sqls) if any(sqls) else None


def _rename_vars(obj, renames):
    s = json.dumps(obj)
    for old, new in renames.items():
        s = re.sub(r"\$\{%s([}:])" % re.escape(old), r"${%s\1" % new, s)
        s = re.sub(r"\$%s\b" % re.escape(old), "$" + new, s)
        s = s.replace("[[%s]]" % old, "[[%s]]" % new)
    return json.loads(s)


def _var_sig(v):
    v = {k: v.get(k) for k in ("type", "query", "datasource", "definition", "options", "multi", "includeAll")
         if k != "options" or v.get("type") == "custom"}
    return json.dumps(v, sort_keys=True)


def build_page(uid, title, time_from, src_uids, sources, warn=print):
    panels, variables, var_sig, seen = [], [], {}, set()
    y = 0
    for su in src_uids:
        src = sources.get(su)
        if src is None:
            warn(f"[consolidate] {uid}: source {su} missing — skipped")
            continue
        renames = {}
        for v in (src.get("templating") or {}).get("list", []):
            name, sig = v["name"], _var_sig(v)
            if name in var_sig and var_sig[name] != sig:
                renames[name] = f"{name}_{re.sub(r'[^a-z0-9]', '_', su)}"
            if renames.get(name, name) not in var_sig:
                nv = copy.deepcopy(v); nv["name"] = renames.get(name, name)
                if name in renames:
                    nv["label"] = f"{v.get('label') or name} ({src.get('title')})"
                variables.append(nv); var_sig[nv["name"]] = sig
        body = _rename_vars(copy.deepcopy(src.get("panels") or []), renames)

        keep = []
        for p in body:
            if (su, p.get("title")) in DROP:
                continue
            if p.get("type") == "row":
                inner = []
                for q in p.get("panels") or []:
                    sig = _targets_sig(q)
                    if (su, q.get("title")) in DROP or (sig and sig in seen):
                        continue
                    if sig:
                        seen.add(sig)
                    inner.append(q)
                if p.get("collapsed") and not inner:
                    continue
                p["panels"] = inner
                keep.append(p)
                continue
            sig = _targets_sig(p)
            if sig and sig in seen:
                continue
            if sig:
                seen.add(sig)
            keep.append(p)
        if not keep:
            continue
        # section header for this source, then its panels shifted below it
        panels.append({"type": "row", "title": src.get("title") or su, "collapsed": False, "panels": [],
                       "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}})
        y += 1
        top = min((p.get("gridPos") or {}).get("y", 0) for p in keep)
        bottom = y
        for p in keep:
            gp = p.setdefault("gridPos", {"x": 0, "y": 0, "w": 12, "h": 8})
            gp["y"] = gp.get("y", 0) - top + y
            for q in p.get("panels") or []:
                qg = q.setdefault("gridPos", {"x": 0, "y": 0, "w": 12, "h": 8})
                qg["y"] = qg.get("y", 0) - top + y
            bottom = max(bottom, gp["y"] + (1 if p.get("type") == "row" else gp.get("h", 8)))
            panels.append(p)
        y = bottom
    pid = 1
    for p in panels:
        p["id"] = pid; pid += 1
        for q in p.get("panels") or []:
            q["id"] = pid; pid += 1
    return {
        "uid": uid, "title": title, "tags": ["nova", "nova-page"], "timezone": "browser", "schemaVersion": 39,
        "editable": True, "graphTooltip": 1, "time": {"from": time_from, "to": "now"}, "refresh": "5m",
        "templating": {"list": variables}, "panels": panels,
        "links": [{"type": "dashboards", "tags": ["nova-page"], "asDropdown": False, "title": "Pages",
                   "includeVars": False, "keepTime": True}],
        "description": "Generated by scripts/nova_grafana_consolidate.py from grafana/sources: "
                       + ", ".join(src_uids) + ". Edit the sources, not this file.",
    }


def load_sources(src_dir):
    out = {}
    for f in sorted(os.listdir(src_dir)):
        if f.endswith(".json"):
            d = json.load(open(os.path.join(src_dir, f)))
            out[d["uid"]] = d
    return out


def main():
    src_dir, out_dir = os.path.join(ROOT, "sources"), os.path.join(ROOT, "dashboards")
    sources = load_sources(src_dir)
    used = {u for _, _, _, us in PAGES for u in us}
    orphans = sorted(set(sources) - used - set(RETIRED))
    if orphans:
        print(f"[consolidate] WARNING: sources on no page: {', '.join(orphans)}")
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        if f.endswith(".json"):
            os.remove(os.path.join(out_dir, f))
    for uid, title, tf, us in PAGES:
        d = build_page(uid, title, tf, us, sources)
        n = sum(1 for p in d["panels"] if p["type"] != "row") + sum(len(p.get("panels") or []) for p in d["panels"])
        json.dump(d, open(os.path.join(out_dir, f"{uid}.json"), "w"), indent=1)
        print(f"[consolidate] {uid}: {title} — {len(us)} sources, {n} panels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
