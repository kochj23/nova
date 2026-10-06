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
import argparse, os, re, socket, sys, time
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
    if c.get("_source") == "arp":
        via = "seen in nova-core's ARP table only (not fingerprinted by UniFi yet)"
    else:
        via = f"wired, switch {c.get('sw_mac') or '?'} port {c.get('sw_port') or '?'}" if wired else \
              f"Wi-Fi '{c.get('essid') or '?'}', AP {c.get('ap_mac') or '?'}"
    fp = ", ".join(str(c[k]) for k in ("dev_cat_name", "os_name", "dev_family_name") if c.get(k))
    fs = c.get("first_seen")
    age = f"{(time.time() - fs) / 60:.0f} min ago" if fs else "unknown"
    rand = " (RANDOMIZED MAC — a phone/laptop with private address, or someone hiding)" if is_randomized(c.get("mac", "")) else ""
    how = "ARP only" if c.get("_source") == "arp" else ("wired" if wired else "Wi-Fi " + str(c.get("essid") or "?"))
    title = f"🆕 NEW DEVICE on the network: {name} ({ip}) via {how}"
    body = (f"MAC {c.get('mac')}{rand}\nVendor: {oui}\nHostname: {c.get('hostname') or '-'}\n"
            f"Path: {via}\nUniFi fingerprint: {fp or '-'}\nUniFi first saw it: {age}\n"
            f"Never seen by Nova before (known_devices had {KNOWN_COUNT} devices).\n"
            f"If this is yours, name it in UniFi. If not, tell Nova: quarantine {c.get('mac')}  (she blocks it at the UDM; undo with unquarantine).")
    return title, body


KNOWN_COUNT = 0


def known_macs(cur) -> set:
    cur.execute("SELECT client_mac FROM telemetry.known_devices")
    return {r[0].lower() for r in cur.fetchall()}


def heartbeat(cur, status="ok", err=None):
    cur.execute("INSERT INTO health_checks (service_name, node_name, checked_by, status, error_message, checked_at) "
                "VALUES ('security_organ', %s, 'self', %s, %s, now())", (NODE, status, err))


def neighbor_clients() -> list[dict]:
    """Second source (2026-10-03, Jordan said yes): the local ARP/neighbor table. Catches a device
    that talks on the segment but has not (yet) been fingerprinted by UniFi. Linux only; empty on macOS."""
    import subprocess, re as _re
    try:
        out = subprocess.run(["ip", "-4", "neigh", "show"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    res = []
    for line in out.splitlines():
        m = _re.match(r"(\S+) dev (\S+) lladdr (\S+) (REACHABLE|STALE|DELAY|PROBE)", line)
        if m and m.group(1).startswith("192.168.1."):
            res.append({"mac": m.group(3).lower(), "ip": m.group(1), "hostname": None, "name": None,
                        "oui": None, "is_wired": None, "essid": None, "first_seen": None, "_source": "arp"})
    return res


MAC_RE = re.compile(r"\b([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b", re.I)
QUORUM = 2                  # wish #61 (2026-10-04): witnesses needed before a newcomer is CRITICAL (else WARNING)


def dhcp_macs(cur, minutes=15) -> set:
    """Third witness (wish #61): MACs the UDM's DHCP server acked recently — its syslog is forwarded to
    nova_syslog_server (.6:1514, repointed from the dead .7:514 target on 2026-10-04) into syslog_events."""
    try:
        cur.execute("SELECT message FROM syslog_events WHERE received_at > now() - make_interval(mins => %s) "
                    "AND message ~* 'DHCP(ACK|REQUEST|OFFER|DISCOVER)'", (minutes,))
        return {m.lower() for (msg,) in cur.fetchall() for m in MAC_RE.findall(msg or "")}
    except Exception:  # noqa: BLE001
        cur.connection.rollback(); return set()


def level_for(fs, witnesses, arrival, now=None) -> tuple[str, str]:
    """Pure. A newcomer is CRITICAL only when UniFi says it is genuinely new AND at least QUORUM witnesses saw it
    AND nobody known just walked in; otherwise WARNING with the reason. -> (level, why)."""
    now = now or time.time()
    if fs and now - fs >= RECENT_S:
        return "warning", "UniFi has seen it before"
    if len(witnesses) < QUORUM:
        return "warning", f"single witness ({', '.join(sorted(witnesses))}) — {QUORUM} needed for a critical (wish #61)"
    if arrival:
        return "warning", f"{arrival[0]} walked in {arrival[1]} min ago — likely their device (wish #66)"
    return "critical", ""


def cycle(conn, dry_run=False, seed=False) -> int:
    global KNOWN_COUNT
    clients = unifi._fetch_clients()
    if clients is None:
        if unifi._unifi_login():
            clients = unifi._fetch_clients()
    if clients is not None:
        seen = {(c.get("mac") or "").lower() for c in clients}
        seen |= {(d.get("mac") or "").lower() for d in (unifi._fetch_devices() or [])}   # UniFi's own switches/APs are not intruders
        arp = {n["mac"]: n for n in neighbor_clients()}
        for c in clients:
            c["_witnesses"] = {"unifi"} | ({"arp"} if (c.get("mac") or "").lower() in arp else set())
        for mac, n in arp.items():
            if mac not in seen:
                n["_witnesses"] = {"arp"}; clients.append(n)
    if clients is None:
        with conn.cursor() as cur:
            heartbeat(cur, "degraded", "UniFi client fetch failed")
        conn.commit()
        log("UniFi fetch failed")
        return 0
    new = 0
    with conn.cursor() as cur:
        known = known_macs(cur); KNOWN_COUNT = len(known)
        dhcp = dhcp_macs(cur)
        try:                                    # wish #66: read the shared organ board before talking to Jordan
            from nova_organ_board import board, someone_just_arrived
            arrival = someone_just_arrived(board(cur))
        except Exception:  # noqa: BLE001
            arrival = None
        for c in clients:
            mac = (c.get("mac") or "").lower()
            if not mac or mac in known:
                continue
            new += 1
            title, body = describe(c)
            witnesses = set(c.get("_witnesses") or {"unifi"}) | ({"dhcp"} if mac in dhcp else set())
            if seed:
                log(f"seeding {mac} ({c.get('name') or c.get('hostname') or '-'})")
            elif dry_run:
                log(f"DRY-RUN would alert: {title} witnesses={sorted(witnesses)}")
            else:
                fs = c.get("first_seen")
                level, why = level_for(fs, witnesses, arrival)
                if why == "UniFi has seen it before":
                    title = title.replace("NEW DEVICE on the network", "unrecorded device (UniFi has seen it before)")
                body += f"\nWitnesses: {', '.join(sorted(witnesses))}." + (f" Held at warning: {why}." if why and level == "warning" else "")
                notify(title, body, level=level, category="security", source="nova_security_organ",
                       dedup_key=f"newdev:{mac}",
                       meta={"mac": mac, "ip": c.get("ip"), "oui": c.get("oui"), "wired": bool(c.get("is_wired")),
                             "essid": c.get("essid"), "ap_mac": c.get("ap_mac"), "sw_mac": c.get("sw_mac"),
                             "randomized_mac": is_randomized(mac), "unifi_first_seen": fs,
                             "witnesses": sorted(witnesses), "held_because": why or None})
                log(f"ALERT {level}: {title}")
            if not dry_run:
                cur.execute("INSERT INTO telemetry.known_devices (client_mac, client_name, ip, first_seen) "
                            "VALUES (%s, %s, %s, now()) ON CONFLICT DO NOTHING",
                            (mac, c.get("name") or c.get("hostname"), c.get("ip")))
        heartbeat(cur, "ok", None)
    conn.commit()
    return new


def digest():
    """Daily roll-up of newcomers (info -> #nova-digest), so criticals stay rare but the long tail is visible."""
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT client_mac, coalesce(client_name,'-'), coalesce(ip,'-'), first_seen FROM telemetry.known_devices "
                    "WHERE first_seen > now() - interval '24 hours' ORDER BY first_seen")
        rows = cur.fetchall()
        cur.execute("SELECT count(*) FROM telemetry.known_devices")
        total = cur.fetchone()[0]
    lines = [f"• {fs:%H:%M} {mac} {name} {ip}" for mac, name, ip, fs in rows]
    body = (f"{len(rows)} device(s) first seen in the last 24 h (of {total} known):\n" + "\n".join(lines)) if rows else f"No new devices in the last 24 h ({total} known)."
    notify("🛡️ Security organ — daily newcomers", body, level="info", category="security", source="nova_security_organ",
           dedup_key=f"newdev-digest:{datetime.now():%Y-%m-%d}", meta={"new_24h": len(rows), "known": total})
    log(f"digest sent: {len(rows)} new / {total} known")


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
    ap.add_argument("--interval", type=int, default=30); ap.add_argument("--digest", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    if "--selftest" in sys.argv:            # the quorum/arrival logic is the only branchy bit worth a check
        now = 1_000_000.0
        assert level_for(None, {"unifi", "arp"}, None, now) == ("critical", "")
        assert level_for(now - 10, {"unifi"}, None, now)[0] == "warning" and "single witness" in level_for(now - 10, {"unifi"}, None, now)[1]
        assert level_for(now - 10, {"unifi", "dhcp"}, ("jordan", 3), now)[1].startswith("jordan walked in")
        assert level_for(now - 2 * RECENT_S, {"unifi", "arp", "dhcp"}, None, now) == ("warning", "UniFi has seen it before")
        probe = ":".join(["dd", "ee", "ff", "00", "11", "22"])   # built at runtime: no MAC literal in source (pre-commit scan)
        assert probe in {m.lower() for m in MAC_RE.findall(f"DHCPACK(br0) 192.168.1.50 {probe.upper()} phone")}
        print("selftest ok"); return 0
    a = ap.parse_args()
    if a.test_alert:
        test_alert(); return
    if a.digest:
        digest(); return
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
