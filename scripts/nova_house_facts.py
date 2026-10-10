#!/usr/bin/env python3
"""
nova_house_facts.py — the house facts ledger (six-month build #1, 2026-09-28).

Jordan asked three times in four days what firmware the master-bedroom Zigbee device runs.
Nova had two million memories and no answer — while the version sat in a retained MQTT
topic the whole time. This is the structured inventory she never had: one row per
(device, attribute), refreshed from the sources that actually know, consulted by the
gateway BEFORE vector recall whenever a question is about the house.

Sources (all read-only):
  zigbee2mqtt  bridge/devices  -> model, manufacturer, firmware (software_build_id), date_code, ieee, type
  zigbee2mqtt  <device> state  -> installed_version, latest_version, link quality
  home assistant /api/states   -> update.* entities: installed/latest version, title
  telemetry.ha_sensors         -> room (area) per entity, last 2h
  dns_records + net_inventory  -> ip, mac, switch port, last online (UniFi)
  service_registry             -> host:port and status per fleet service

Runs on the Mac Studio (needs local Mosquitto + the Keychain HA token), every 15 minutes.
  nova_house_facts.py            # collect + upsert
  nova_house_facts.py --dry-run  # print what it would write
  nova_house_facts.py --selftest
  nova_house_facts.py --ask "zigbee master bedroom firmware"   # what the gateway will see
"""
import json
import re
import subprocess
import sys
from datetime import datetime

import psycopg2

import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
MQTT_HOST = "127.0.0.1"
STOP = {"the", "and", "for", "what", "which", "does", "run", "running", "version", "firmware", "does",
        "is", "are", "on", "in", "my", "our", "a", "an", "of", "to", "device", "unit", "thing"}


def log(m):
    print(f"[house-facts {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def ensure_schema(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS house_facts (
        entity text NOT NULL, attr text NOT NULL, value text NOT NULL, source text NOT NULL,
        observed_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (entity, attr))""")


# ── collectors: each returns [(entity, attr, value, source)] ─────────────────

def _mqtt(topic, count, wait):
    try:
        out = subprocess.run(["mosquitto_sub", "-h", MQTT_HOST, "-t", topic, "-C", str(count), "-W", str(wait), "-v"],
                             capture_output=True, text=True, timeout=wait + 10).stdout
    except Exception as e:  # noqa: BLE001
        log(f"mqtt {topic} failed: {e}"); return []
    rows = []
    for line in out.splitlines():
        t, _, payload = line.partition(" ")
        try:
            rows.append((t, json.loads(payload)))
        except Exception:  # noqa: BLE001
            continue
    return rows


def collect_zigbee():
    facts = []
    for _, devices in _mqtt("zigbee2mqtt/bridge/devices", 1, 5):
        for d in devices if isinstance(devices, list) else []:
            name = d.get("friendly_name")
            if not name or d.get("type") == "Coordinator" and name == "Coordinator":
                continue
            for attr, val in (("model", (d.get("definition") or {}).get("model") or d.get("model_id")),
                              ("manufacturer", d.get("manufacturer")), ("firmware", d.get("software_build_id")),
                              ("date_code", d.get("date_code")), ("ieee", d.get("ieee_address")),
                              ("zigbee_type", d.get("type"))):
                if val:
                    facts.append((name, attr, str(val), "zigbee2mqtt"))
    for topic, st in _mqtt("zigbee2mqtt/+", 200, 4):
        name = topic.split("/", 1)[1]
        if name.startswith("bridge") or not isinstance(st, dict):
            continue
        upd = st.get("update") or {}
        for attr, val in (("installed_version", upd.get("installed_version")),
                          ("latest_version", upd.get("latest_version")), ("link_quality", st.get("linkquality"))):
            if val is not None:
                facts.append((name, attr, str(val), "zigbee2mqtt"))
    return facts


def collect_ha():
    facts = []
    try:
        import nova_ha_metrics as h
        for s in h.ha_get_states():
            eid = s.get("entity_id", "")
            if not eid.startswith("update."):
                continue
            a = s.get("attributes") or {}
            ent = eid.split(".", 1)[1]
            for attr, val in (("installed_version", a.get("installed_version")), ("latest_version", a.get("latest_version")),
                              ("update_available", "yes" if s.get("state") == "on" else "no"), ("title", a.get("title"))):
                if val:
                    facts.append((ent, attr, str(val), "home_assistant"))
    except Exception as e:  # noqa: BLE001
        log(f"HA states failed: {e}")
    return facts


def collect_pg(cur):
    facts = []
    try:
        cur.execute("SELECT DISTINCT ON (entity_id) entity_id, area FROM telemetry.ha_sensors "
                    "WHERE ts > now() - interval '2 hours' AND area IS NOT NULL AND area <> '' ORDER BY entity_id, ts DESC")
        facts += [(e.split(".", 1)[-1], "room", a, "home_assistant") for e, a in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        log(f"ha_sensors areas failed: {e}")
    try:
        cur.execute("SELECT d.name, d.ip, d.mac, i.sw_port, i.last_online::text FROM dns_records d "
                    "LEFT JOIN telemetry.net_inventory i ON i.mac = d.mac WHERE d.name IS NOT NULL")
        for name, ip, mac, port, last in cur.fetchall():
            for attr, val in (("ip", ip), ("mac", mac), ("switch_port", port), ("last_online", last)):
                if val:
                    facts.append((name, attr, str(val), "unifi"))
    except Exception as e:  # noqa: BLE001
        log(f"dns/net_inventory failed: {e}")
    try:
        cur.execute("SELECT service_name, node_name, host(host), port, status, last_heartbeat::text FROM service_registry")
        for svc, node, host, port, status, hb in cur.fetchall():
            ent = f"{svc}@{node}"
            facts += [(ent, "endpoint", f"{host}:{port}", "service_registry"), (ent, "status", status or "?", "service_registry")]
            if hb:
                facts.append((ent, "last_heartbeat", hb, "service_registry"))
    except Exception as e:  # noqa: BLE001
        log(f"service_registry failed: {e}")
    return facts


def upsert(cur, facts):
    cur.executemany("INSERT INTO house_facts (entity, attr, value, source, observed_at) VALUES (%s,%s,%s,%s,now()) "
                    "ON CONFLICT (entity, attr) DO UPDATE SET value=EXCLUDED.value, source=EXCLUDED.source, observed_at=now()",
                    facts)


# ── lookup (what the gateway calls) ──────────────────────────────────────────

def tokens(question):
    return [w for w in re.findall(r"[a-z0-9]+", (question or "").lower()) if len(w) >= 3 and w not in STOP]


def score(entity, toks):
    parts = set(re.findall(r"[a-z0-9]+", entity.lower()))
    return sum(1 for t in toks if t in parts or any(t in p for p in parts if len(t) >= 4))


def lookup_sql(cur, question, limit=4):
    """Pure-SQL-free scorer over the ledger: returns [(entity, {attr: value}, observed_at)] best first."""
    toks = tokens(question)
    if not toks:
        return []
    cur.execute("SELECT entity, attr, value, max(observed_at) OVER (PARTITION BY entity) FROM house_facts")
    by = {}
    for ent, attr, val, seen in cur.fetchall():
        by.setdefault(ent, ({}, seen))[0][attr] = val
    ranked = sorted(((score(e, toks), e) for e in by), reverse=True)
    return [(e, by[e][0], by[e][1]) for sc, e in ranked[:limit] if sc > 0]


def format_block(hits):
    if not hits:
        return ""
    lines = [f"{e}: " + ", ".join(f"{a}={v}" for a, v in sorted(attrs.items())) + f"  (as of {seen:%Y-%m-%d %H:%M})"
             for e, attrs, seen in hits]
    return "[House facts — live inventory, trust these over memory]\n" + "\n".join(lines) + "\n[End house facts]\n\n"


def main():
    if "--selftest" in sys.argv:
        return demo()
    conn = psycopg2.connect(OPS_DSN, connect_timeout=5); conn.autocommit = True; cur = conn.cursor()
    ensure_schema(cur)
    if "--ask" in sys.argv:
        q = sys.argv[sys.argv.index("--ask") + 1]
        print(format_block(lookup_sql(cur, q)) or "(no house facts match)"); return 0
    facts = collect_zigbee() + collect_ha() + collect_pg(cur)
    log(f"collected {len(facts)} fact(s) over {len({f[0] for f in facts})} entities")
    if "--dry-run" in sys.argv:
        for f in facts[:40]:
            print(f)
        return 0
    upsert(cur, facts)
    log("upserted")
    return 0


def demo():
    assert tokens("What firmware is the master bedroom Zigbee unit running?") == ["master", "bedroom", "zigbee"]
    assert score("master_bedroom_presence", ["master", "bedroom", "zigbee"]) == 2
    assert score("garage_plug_6", ["master", "bedroom"]) == 0
    assert score("slzb_06u_5_core_firmware", ["slzb"]) == 1
    from datetime import datetime as dt
    blk = format_block([("master_bedroom_plug", {"firmware": "1.01.01", "room": "Master Bedroom"}, dt(2026, 9, 28, 12, 0))])
    assert "firmware=1.01.01" in blk and blk.startswith("[House facts")
    assert format_block([]) == ""
    print("all house-facts assertions passed"); return 0


if __name__ == "__main__":
    sys.exit(main())
