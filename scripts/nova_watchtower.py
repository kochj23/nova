#!/usr/bin/env python3
"""nova_watchtower.py — watch EVERYTHING on the network, alert on outages not scenes.

The gap this closes: our other monitors watch *layers* (host up? service up? device
pinging?) but nothing watched whether each data FEED still produces, or with scene
context. A wedged PoE Zigbee coordinator pinged fine and drew power for 14 days while
its data was dead — green at every layer we checked. This watches both:

  1. INVENTORY + CLASSIFY every UniFi client into a tier with a monitoring policy.
  2. LIVENESS — LEARNED-BASELINE, WIRED-ONLY: only infra/coordinator/camera devices that
     are wired (seen on a switch port) and were online recently but just dropped count as
     outages. Anything that sleeps (Apple TVs, HomeKit accessories, phones) is inventory +
     feed-only, never liveness-alerted. Confirmed with an active ping probe.
  3. FEED FRESHNESS — a dead-man's-switch per data stream (the thing that catches wedges).
  4. SCENE-AWARE — smart-home devices are never liveness-alerted (they get powered off by
     scenes/by hand); they're judged by hue 'reachable' + feeds. Reachable-first.
  5. ROOT-CAUSE COLLAPSE — a down coordinator is flagged as the likely cause of its stale
     downstream feeds, instead of N separate alarms.
  6. ALERT-ON-CHANGE — fire once when a problem opens, once when it clears (#nova-alerts),
     and record a liveness time-series for the weekly Network-Health report (phase 2).

(Distinct from nova_network_sentinel.py, the NMAP/IDS security-posture scanner.)

    nova_watchtower.py --dryrun   # classify + evaluate, print, no alerts
    nova_watchtower.py            # one pass: alert-on-change + record time-series
"""
import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
import nova_unifi_poller as U

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# Feed dead-man's-switch: (key, SQL -> one max-timestamp, stale-after minutes, owner).
FEEDS = [
    ("climate:zigbee",     "SELECT max(ts) FROM telemetry.climate WHERE source='zigbee'", 30, "zigbee-coordinator"),
    ("climate:hue_bridge", "SELECT max(ts) FROM telemetry.climate WHERE source='hue_bridge'", 30, "hue-bridge"),
    ("climate:weather",    "SELECT max(ts) FROM telemetry.climate WHERE source='weather_station'", 20, "weather-station"),
    ("climate:fp300",      "SELECT max(ts) FROM telemetry.climate WHERE source='fp300'", 30, None),
    ("climate:homekit",    "SELECT max(ts) FROM telemetry.climate WHERE source='homekit'", 30, None),
    ("weather_station",    "SELECT max(ts) FROM telemetry.weather", 20, "weather-station"),
    ("air_quality",        "SELECT max(ts) FROM telemetry.air_quality", 45, None),
    ("hue_light_state",    "SELECT max(polled_at) FROM public.hue_light_state", 20, "hue-bridge"),
    ("ha_sensors",         "SELECT max(ts) FROM telemetry.ha_sensors", 20, None),
    ("lora_mesh",          "SELECT max(last_heard) FROM telemetry.mesh_nodes", 360, None),  # last_heard, not ts (ts only advances on a brand-new node)
    ("nas_backup",         "SELECT max(ts) FROM telemetry.backup_runs WHERE ok", 1560, "nas-backup"),  # a SUCCESSFUL nightly (ok=true) within 26h
]

# Only these tiers are liveness-alerted (and only when wired + recently-online). smart_home
# is deliberately excluded: those get powered off by scenes/by hand — judged by feeds/hue.
LIVENESS_TIERS = ("infra", "coordinator", "camera")
RECENT_ONLINE_MIN = 90     # a drop only counts if the device was online this recently


def log(m):
    print(f"[watchtower {datetime.now():%H:%M:%S}] {m}", flush=True)


def db():
    return psycopg2.connect(DSN)


def ensure_schema():
    with db() as c:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS telemetry.net_inventory (
                    mac text PRIMARY KEY, name text, ip text, tier text, oui text,
                    sw_port int, first_seen timestamptz DEFAULT now(),
                    last_seen timestamptz, last_online timestamptz,
                    last_classified timestamptz DEFAULT now());
                ALTER TABLE telemetry.net_inventory ADD COLUMN IF NOT EXISTS sw_port int;
                ALTER TABLE telemetry.net_inventory ADD COLUMN IF NOT EXISTS last_online timestamptz;
                CREATE TABLE IF NOT EXISTS telemetry.net_liveness (
                    ts timestamptz DEFAULT now(), mac text, name text, tier text,
                    online boolean, reachable boolean, poe_power numeric);
                CREATE TABLE IF NOT EXISTS telemetry.net_problems (
                    key text PRIMARY KEY, kind text, entity text, tier text, detail text,
                    opened_at timestamptz DEFAULT now(), cleared_at timestamptz,
                    status text DEFAULT 'open');
            """)
        c.commit()


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --- classification (order matters: specific network gear before room-name matches) -----
def classify(name: str, oui: str, is_unifi_device: bool) -> str:
    n, o = (name or "").lower(), (oui or "").lower()
    if is_unifi_device:
        return "infra"

    def has(*words):
        return any(w in n for w in words)

    # explicit network gear (APs / switches / gateway / hosts / storage)
    if has("udm", "dream machine", "gateway", "usw", "uap", "wap", "nanohd", "ac-pro",
           "u6", "u7", "lite-8", "poe", " port", "pro-48", "synology", "unas", "unvr",
           "nova-core", "mac-studio", "office-m", "docker", "pg-primary", "rack "):
        return "infra"
    if has("slzb", "smlight", "zigbee", "hue bridge", "conbee", "deconz", "lutron"):
        return "coordinator"
    if has("camera", "-cam", "omna", "circle", "doorbell"):
        return "camera"
    if has("light switch", "koogeek", "meross", "light", "bulb", "plug", "outlet", "lamp",
           "bose", "onkyo", "apple tv", "appletv", "homepod", "sonos", "thermostat",
           "ecobee", "nest", "hdhomerun", "roku", "soundbar", "-tv", "netradio", "receiver",
           "masterbedroom", "living-room"):
        return "smart_home"
    if has("iphone", "ipad", "macbook", "laptop", "watch") or (o.startswith("apple") and has("mac")):
        return "transient"
    if not name or name == "(unnamed)":
        return "transient"
    return "unknown"


def ping_ok(ip: str) -> bool:
    if not ip or ip == "?":
        return False
    try:
        return subprocess.run(["ping", "-c", "1", ip],
                              capture_output=True, timeout=2).returncode == 0
    except Exception:
        return False


def unifi_snapshot():
    U._unifi_login()

    def g(path):
        return (U._unifi_get(f"{U.CONTROLLER_BASE}/proxy/network/api/s/{U.SITE}/{path}") or {}).get("data", [])

    active = {c["mac"]: c for c in g("stat/sta") if c.get("mac")}
    allu = {c["mac"]: c for c in g("stat/alluser") if c.get("mac")}
    dev_macs = {d.get("mac") for d in g("stat/device")}
    return active, allu, dev_macs


def feed_age_minutes(sql: str):
    try:
        with db() as c, c.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            cur.execute("SELECT EXTRACT(EPOCH FROM (now() - %s))/60", (row[0],))
            return cur.fetchone()[0]
    except Exception as e:
        log(f"feed query failed ({sql[:40]}...): {e}")
        return None


# --- evaluation -------------------------------------------------------------
def evaluate():
    ensure_schema()
    active, allu, dev_macs = unifi_snapshot()
    counts = {}

    # 1) Record what's ONLINE now (authoritative ip/sw_port/last_online), + liveness sample.
    with db() as c:
        with c.cursor() as cur:
            for mac, cl in active.items():
                name = cl.get("name") or cl.get("hostname") or "(unnamed)"
                tier = classify(name, cl.get("oui", ""), mac in dev_macs)
                counts[tier] = counts.get(tier, 0) + 1
                cur.execute("""
                    INSERT INTO telemetry.net_inventory (mac,name,ip,tier,oui,sw_port,last_seen,last_online,last_classified)
                    VALUES (%s,%s,%s,%s,%s,%s, now(), now(), now())
                    ON CONFLICT (mac) DO UPDATE SET name=EXCLUDED.name, ip=EXCLUDED.ip,
                        tier=EXCLUDED.tier, sw_port=EXCLUDED.sw_port,
                        last_seen=now(), last_online=now(), last_classified=now()
                """, (mac, name, cl.get("ip", ""), tier, cl.get("oui", ""), cl.get("sw_port")))
                cur.execute("INSERT INTO telemetry.net_liveness (mac,name,tier,online,poe_power) "
                            "VALUES (%s,%s,%s,true,%s)", (mac, name, tier, _num(cl.get("poe_power"))))
            # offline-history: keep inventory name/tier fresh but DON'T touch ip/sw_port/last_online
            for mac, cl in allu.items():
                if mac in active:
                    continue
                name = cl.get("name") or cl.get("hostname") or "(unnamed)"
                tier = classify(name, cl.get("oui", ""), mac in dev_macs)
                counts[tier] = counts.get(tier, 0) + 1
                cur.execute("""
                    INSERT INTO telemetry.net_inventory (mac,name,tier,oui,last_seen)
                    VALUES (%s,%s,%s,%s, now())
                    ON CONFLICT (mac) DO UPDATE SET name=EXCLUDED.name, tier=EXCLUDED.tier, last_seen=now()
                """, (mac, name, tier, cl.get("oui", "")))
        c.commit()

    problems = {}

    # 2) Liveness outages: wired, recently-online infra/coordinator/camera that dropped
    #    and no longer answer a ping. (Learned baseline: unknown-until-seen-online = quiet.)
    with db() as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT mac,name,ip,tier FROM telemetry.net_inventory
                WHERE tier = ANY(%s) AND sw_port IS NOT NULL AND ip <> ''
                  AND last_online > now() - make_interval(mins => %s)
            """, (list(LIVENESS_TIERS), RECENT_ONLINE_MIN))
            for r in cur.fetchall():
                if r["mac"] in active:
                    continue                       # still online
                if ping_ok(r["ip"]):
                    continue                       # answers ping despite UniFi — not down
                problems[f"down:{r['mac']}"] = ("device_down", r["name"], r["tier"],
                    f"{r['tier']} '{r['name']}' ({r['ip']}) dropped off the network and is unreachable")

    # 3) Feed freshness (catches wedges that still ping). Also logged to the
    #    liveness time-series (tier='feed') so the weekly report can score reliability.
    stale_by_owner = {}
    with db() as c:
        with c.cursor() as cur:
            for key, sql, thresh, owner in FEEDS:
                age = feed_age_minutes(sql)
                fresh = age is not None and age <= thresh
                cur.execute("INSERT INTO telemetry.net_liveness (mac,name,tier,online) "
                            "VALUES (%s,%s,'feed',%s)", (f"feed:{key}", key, fresh))
                if not fresh:
                    detail = "no data ever" if age is None else f"{age:.0f} min old (> {thresh} min)"
                    problems[f"stale:{key}"] = ("feed_stale", key, "sensor_feed", f"feed '{key}' {detail}")
                    if owner:
                        stale_by_owner.setdefault(owner, []).append(key)
        c.commit()

    return problems, counts, stale_by_owner


# --- alert-on-change --------------------------------------------------------
def reconcile_and_alert(problems, dryrun):
    new, cleared = [], []
    with db() as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT key, detail FROM telemetry.net_problems WHERE status='open'")
            open_now = {r["key"]: r["detail"] for r in cur.fetchall()}
            for key, (kind, entity, tier, detail) in problems.items():
                if key not in open_now:
                    new.append(f"🔴 {detail}")
                    if not dryrun:
                        cur.execute("""INSERT INTO telemetry.net_problems (key,kind,entity,tier,detail)
                            VALUES (%s,%s,%s,%s,%s) ON CONFLICT (key) DO UPDATE
                            SET status='open', detail=EXCLUDED.detail, cleared_at=NULL, opened_at=now()""",
                            (key, kind, entity, tier, detail))
            for key, detail in open_now.items():
                if key not in problems:
                    cleared.append(f"🟢 recovered: {detail}")
                    if not dryrun:
                        cur.execute("UPDATE telemetry.net_problems SET status='cleared', cleared_at=now() WHERE key=%s", (key,))
        if not dryrun:
            c.commit()
    return new, cleared


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dryrun", action="store_true", help="evaluate + print, no alerts")
    args = ap.parse_args()

    problems, counts, stale_by_owner = evaluate()
    log("inventory by tier: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    log(f"{len(problems)} active problem(s)")
    for key in sorted(problems):
        print(f"    - {problems[key][3]}")
    for owner, feeds in stale_by_owner.items():
        print(f"    * likely root cause '{owner}' -> stale: {', '.join(feeds)}")

    if args.dryrun:
        log("DRYRUN — no alerts sent, no problem-state written")
        return 0

    new, cleared = reconcile_and_alert(problems, dryrun=False)
    if new or cleared:
        body = ["*Watchtower — network change detected*"]
        for owner, feeds in stale_by_owner.items():
            body.append(f"⚠️ likely root cause: *{owner}* → stale feeds: {', '.join(feeds)}")
        body += new + cleared
        try:
            nova_config.post_both("\n".join(body), slack_channel=nova_config.SLACK_ALERTS, discord_channel=None)
            log(f"alerted: {len(new)} new, {len(cleared)} cleared")
        except Exception as e:
            log(f"alert post failed: {e}")
    else:
        log("no change since last pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
