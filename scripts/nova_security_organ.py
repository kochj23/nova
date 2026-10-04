#!/usr/bin/env python3
"""nova_security_organ.py — Nova's security organ: who just joined my network?

Every 30 s it asks the UDM Pro for the live client table and compares it with everything Nova
has ever seen (telemetry.known_devices). A MAC that has never been seen is a CRITICAL alert
(#nova-critical + LoRa relay) with everything we know about it: name/hostname, IP, vendor OUI,
wired or Wi-Fi + SSID, the switch/AP it came in through, UniFi's own device fingerprint, and
whether the MAC is randomized (locally administered). The device is then recorded so it alerts
exactly once; the notifier's state-change dedup covers any re-fire.

Built 2026-10-03 after another Claude session dumped the Wi-Fi passwords (Jordan: "the wifi
password is annoying"). Runs on nova-core (.2) as systemd nova-security-organ.service so it
survives the Mac Studio's login session dying; reuses nova_unifi_poller's UniFi session and
nova_notify's bus. Heartbeats into health_checks as service 'security_organ'.

Usage: nova_security_organ.py [--once] [--seed] [--dry-run] [--test-alert] [--interval 30]
  --seed       record every current client WITHOUT alerting (first run on a fresh table)
  --test-alert fire one clearly-labelled TEST critical for a synthetic device, then exit
ponytail: UniFi stat/sta is the only source (covers DHCP + static + Wi-Fi); add UDM DHCP-lease
and syslog DHCPACK feeds only if a device ever gets in without showing up in stat/sta.
"""
import argparse, os, socket, sys, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import psycopg2  # noqa: E402
import nova_unifi_poller as unifi  # noqa: E402
from nova_notify import notify  # noqa: E402

DSN = os.environ.get("NOVA_OPS_DSN", "dbname=nova_ops user=kochj host=pg-primary.digitalnoise.net port=5432")
NODE = socket.gethostname().split(".")[0]
RECENT_S = 15 * 60          # UniFi first_seen within this window = genuinely new, not just new to us


def log(msg):
    print(f"[security-organ {datetime.now():%H:%M:%S}] {msg}", flush=True)


def is_randomized(mac: str) -> bool:
    try:
        return int(mac.split(":")[0], 16) & 0x02 == 0x02
    except Exception:
        return False


def describe(c: dict) -> tuple[str, str]:
    name = c.get("name") or c.get("hostname") or "(no name)"
    ip = c.get("ip") or "no IP yet"
    oui = c.get("oui") or "unknown vendor"
    wired = c.get("is_wired")
    via = f"wired, switch {c.get('sw_mac') or '?'} port {c.get('sw_port') or '?'}" if wired else \
          f"Wi-Fi '{c.get('essid') or '?'}', AP {c.get('ap_mac') or '?'}"
    fp = ", ".join(str(c[k]) for k in ("dev_cat_name", "os_name", "dev_family_name") if c.get(k))
    fs = c.get("first_seen")
    age = f"{(time.time() - fs) / 60:.0f} min ago" if fs else "unknown"
    rand = " (RANDOMIZED MAC — a phone/laptop with private address, or someone hiding)" if is_randomized(c.get("mac", "")) else ""
    title = f"🆕 NEW DEVICE on the network: {name} ({ip}) via {'wired' if wired else 'Wi-Fi ' + str(c.get('essid') or '?')}"
    body = (f"MAC {c.get('mac')}{rand}\nVendor: {oui}\nHostname: {c.get('hostname') or '-'}\n"
            f"Path: {via}\nUniFi fingerprint: {fp or '-'}\nUniFi first saw it: {age}\n"
            f"Never seen by Nova before (known_devices had {KNOWN_COUNT} devices).\n"
            f"If this is yours, name it in UniFi. If not: UniFi > Clients > block, or ask Nova to quarantine (gated).")
    return title, body


KNOWN_COUNT = 0


def known_macs(cur) -> set:
    cur.execute("SELECT client_mac FROM telemetry.known_devices")
    return {r[0].lower() for r in cur.fetchall()}


def heartbeat(cur, status="ok", err=None):
    cur.execute("INSERT INTO health_checks (service_name, node_name, checked_by, status, error_message, checked_at) "
                "VALUES ('security_organ', %s, 'self', %s, %s, now())", (NODE, status, err))


def cycle(conn, dry_run=False, seed=False) -> int:
    global KNOWN_COUNT
    clients = unifi._fetch_clients()
    if clients is None:
        if unifi._unifi_login():
            clients = unifi._fetch_clients()
    if clients is None:
        with conn.cursor() as cur:
            heartbeat(cur, "degraded", "UniFi client fetch failed")
        conn.commit()
        log("UniFi fetch failed")
        return 0
    new = 0
    with conn.cursor() as cur:
        known = known_macs(cur); KNOWN_COUNT = len(known)
        for c in clients:
            mac = (c.get("mac") or "").lower()
            if not mac or mac in known:
                continue
            new += 1
            title, body = describe(c)
            if seed:
                log(f"seeding {mac} ({c.get('name') or c.get('hostname') or '-'})")
            elif dry_run:
                log(f"DRY-RUN would alert: {title}")
            else:
                fs = c.get("first_seen")
                level = "critical" if (not fs or time.time() - fs < RECENT_S) else "warning"
                if level == "warning":
                    title = title.replace("NEW DEVICE on the network", "unrecorded device (UniFi has seen it before)")
                notify(title, body, level=level, category="security", source="nova_security_organ",
                       dedup_key=f"newdev:{mac}",
                       meta={"mac": mac, "ip": c.get("ip"), "oui": c.get("oui"), "wired": bool(c.get("is_wired")),
                             "essid": c.get("essid"), "ap_mac": c.get("ap_mac"), "sw_mac": c.get("sw_mac"),
                             "randomized_mac": is_randomized(mac), "unifi_first_seen": fs})
                log(f"ALERT {level}: {title}")
            if not dry_run:
                cur.execute("INSERT INTO telemetry.known_devices (client_mac, client_name, ip, first_seen) "
                            "VALUES (%s, %s, %s, now()) ON CONFLICT DO NOTHING",
                            (mac, c.get("name") or c.get("hostname"), c.get("ip")))
        heartbeat(cur, "ok", None)
    conn.commit()
    return new


def test_alert():
    fake = {"mac": "02:de:ad:be:ef:01", "name": "TEST-DEVICE (security organ self-test)", "ip": "192.168.1.254",
            "oui": "Nova Test", "is_wired": False, "essid": "TEST-SSID", "ap_mac": "00:00:00:00:00:00",
            "hostname": "test-device", "first_seen": time.time() - 60}
    title, body = describe(fake)
    notify("[TEST] " + title, "THIS IS A TEST of nova_security_organ — no real device joined.\n\n" + body,
           level="critical", category="security", source="nova_security_organ",
           dedup_key=f"newdev:test:{int(time.time())}", meta={"test": True})
    log("test alert sent")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true"); ap.add_argument("--seed", action="store_true")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--test-alert", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    a = ap.parse_args()
    if a.test_alert:
        test_alert(); return
    if not unifi._unifi_login():
        sys.exit("[security-organ] UniFi login failed")
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    log(f"started on {NODE}, interval {a.interval}s, seed={a.seed}, dry_run={a.dry_run}")
    while True:
        try:
            n = cycle(conn, dry_run=a.dry_run, seed=a.seed)
            if n: log(f"{n} new device(s) this cycle")
        except psycopg2.Error as e:
            log(f"db error: {e}"); conn.rollback()
            try: conn.close()
            except Exception: pass
            time.sleep(5); conn = psycopg2.connect(DSN)
        except Exception as e:
            log(f"cycle error: {e}")
            try: conn.rollback()
            except Exception: pass
        if a.once or a.seed:
            log(f"cycle done: {KNOWN_COUNT} known devices, {n} new")
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
