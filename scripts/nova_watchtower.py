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
  3. SCENE-AWARE — smart-home devices are never liveness-alerted (they get powered off by
     scenes/by hand); they're judged by hue 'reachable' + feeds. Reachable-first.
  4. ALERT-ON-CHANGE — fire once when a device problem opens, once when it clears
     (#nova-alerts), and record a liveness time-series for the weekly Network-Health report.

FEED FRESHNESS (the per-feed dead-man's-switch that caught the Zigbee wedge) and its
root-cause collapse moved to nova_freshness_monitor.py on 2026-10-09 (organ-audit merge M1:
one silence detector). It keeps writing the net_liveness tier='feed' samples and the
net_problems 'stale:<feed>' episodes; this script owns only the 'down:<mac>' episodes.

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

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")

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

    return problems, counts


# --- alert-on-change --------------------------------------------------------
def reconcile_and_alert(problems, dryrun):
    new, cleared = [], []
    with db() as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 'stale:<feed>' episodes belong to nova_freshness_monitor (since 2026-10-09)
            cur.execute("SELECT key, detail FROM telemetry.net_problems "
                        "WHERE status='open' AND left(key, 6) <> 'stale:'")
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

    problems, counts = evaluate()
    log("inventory by tier: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    log(f"{len(problems)} active problem(s)")
    for key in sorted(problems):
        print(f"    - {problems[key][3]}")

    if args.dryrun:
        log("DRYRUN — no alerts sent, no problem-state written")
        return 0

    new, cleared = reconcile_and_alert(problems, dryrun=False)
    if new or cleared:
        body = ["*Watchtower — network change detected*"]
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
