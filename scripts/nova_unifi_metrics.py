#!/usr/bin/env python3
"""
nova_unifi_metrics.py — UniFi time-series metrics collector for Nova's
observability stack (feeds Grafana via PostgreSQL).

One-shot poller (designed to be run from the scheduler, e.g. every 2m). Polls
the UDM-Pro controller (192.168.1.1 / unifi.digitalnoise.net) and inserts a
long-format metric/value/metadata row set into telemetry.unifi_metrics.

This COMPLEMENTS nova_unifi_poller.py (the always-on daemon that writes
per-client rows into telemetry.network and AP/switch CPU/mem into
telemetry.nova_meta). This collector focuses on graphable network-wide
time-series the daemon does not capture:

  - Per-client RX/TX bytes (cumulative) + signal              (metric=unifi_client_*)
  - Per-AP / per-radio: clients, channel, satisfaction        (metric=unifi_ap_*)
  - Per-switch port stats: throughput rate, errors, drops,
    PoE power, link speed, # ports up, per-switch # clients    (metric=unifi_port_* / unifi_sw_*)
  - WAN up/down throughput (Bps rate) + speedtest + latency    (metric=unifi_wan_*)
  - Overall client count by band (2.4 / 5 / 6 GHz) + wired     (metric=unifi_clients_*)

Reuses the auth pattern from nova_unifi_poller / nova_unifi_monitor:
  macOS Keychain service "nova-unifi-api-key" account "nova", X-API-Key header.

RESILIENCE: every collector is wrapped in try/except and contributes whatever
rows it can; one failing endpoint never aborts the others, and the script
never crashes. Exits 0 always (so the scheduler doesn't flag failures).

Written by Jordan Koch.
"""

import json
import ssl
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

CONTROLLER_IP = "192.168.1.1"
CONTROLLER_BASE = f"https://{CONTROLLER_IP}"
SITE = "default"
API_BASE = f"{CONTROLLER_BASE}/proxy/network/api/s/{SITE}"
DB_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

NOW = datetime.now(timezone.utc)

# Self-signed cert on the UDM Pro — don't verify.
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

_API_KEY = None


def log(msg):
    print(f"[unifi_metrics {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Auth ──────────────────────────────────────────────────────────────────────


def get_api_key():
    """Load UniFi API key: nova_secrets (Linux/nova-core) first, macOS
    Keychain fallback (so this still works if ever run on .6)."""
    global _API_KEY
    if _API_KEY is not None:
        return _API_KEY
    try:
        sys.path.insert(0, "/opt/nova")
        import nova_secrets
        key = nova_secrets.get_secret("nova-unifi-api-key")
        if key:
            _API_KEY = key
            return key
    except Exception as e:
        log(f"nova_secrets lookup failed: {e}")
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "nova",
             "-s", "nova-unifi-api-key", "-w"],
            capture_output=True, text=True,
        )
        key = result.stdout.strip()
        if key:
            _API_KEY = key
            return key
    except Exception as e:
        log(f"Keychain lookup failed: {e}")
    log("ERROR: UniFi API key not found in nova_secrets or Keychain "
        "(service=nova-unifi-api-key, account=nova)")
    return None


def api_get(endpoint):
    """GET a UniFi endpoint, return the 'data' list (or None on failure)."""
    api_key = get_api_key()
    if not api_key:
        return None
    url = f"{API_BASE}/{endpoint}"
    req = urllib.request.Request(url, headers={
        "X-API-Key": api_key,
        "Accept": "application/json",
        "User-Agent": "Nova-UniFi-Metrics/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
            data = json.loads(r.read())
        if isinstance(data, dict):
            return data.get("data", [])
        return data
    except Exception as e:
        log(f"API error ({endpoint}): {e}")
        return None


# ── Helpers ────────────────────────────────────────────────────────────────


def _slug(s):
    return (s or "unknown").lower().replace(" ", "_").replace("-", "_").replace(".", "_")


def _num(v):
    """Best-effort numeric coercion; None if not convertible."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _band_for_radio(radio):
    """Map UniFi radio code to band label."""
    return {"ng": "2.4GHz", "na": "5GHz", "ac": "5GHz", "6e": "6GHz", "ax6": "6GHz"}.get(radio, "other")


# ── Collectors ── each returns a list of (ts, metric, value, metadata) tuples ─
# Each is independently wrapped in try/except by the caller.


def collect_clients(rows):
    """Per-client RX/TX bytes + signal, and overall counts by band/wired."""
    clients = api_get("stat/sta")
    if clients is None:
        log("clients: endpoint unreachable, skipping")
        return

    band_counts = {"2.4GHz": 0, "5GHz": 0, "6GHz": 0, "other": 0}
    wired = 0
    wireless = 0

    for c in clients:
        try:
            mac = (c.get("mac") or "").lower()
            if not mac:
                continue
            name = c.get("hostname") or c.get("name") or c.get("oui") or "unknown"
            is_wired = bool(c.get("is_wired", False))
            meta = {"mac": mac, "name": name, "wired": is_wired}

            rx = _num(c.get("rx_bytes"))
            tx = _num(c.get("tx_bytes"))
            if rx is not None:
                rows.append((NOW, "unifi_client_rx_bytes", rx, json.dumps(meta)))
            if tx is not None:
                rows.append((NOW, "unifi_client_tx_bytes", tx, json.dumps(meta)))

            if is_wired:
                wired += 1
            else:
                wireless += 1
                sig = _num(c.get("signal") or c.get("rssi"))
                if sig is not None:
                    smeta = dict(meta)
                    smeta["radio"] = c.get("radio")
                    smeta["band"] = _band_for_radio(c.get("radio"))
                    rows.append((NOW, "unifi_client_signal_dbm", sig, json.dumps(smeta)))
                band = _band_for_radio(c.get("radio"))
                band_counts[band] = band_counts.get(band, 0) + 1
        except Exception as e:
            log(f"clients: per-client error: {e}")

    # Overall counts by band
    for band, count in band_counts.items():
        rows.append((NOW, "unifi_clients_by_band",
                     float(count), json.dumps({"band": band})))
    rows.append((NOW, "unifi_clients_wired", float(wired), json.dumps({})))
    rows.append((NOW, "unifi_clients_wireless", float(wireless), json.dumps({})))
    rows.append((NOW, "unifi_clients_total", float(len(clients)), json.dumps({})))
    log(f"clients: {len(clients)} total ({wireless} wireless / {wired} wired) "
        f"bands={band_counts}")


def collect_devices(rows):
    """Per-AP/radio stats and per-switch port stats (throughput/errors/PoE)."""
    devices = api_get("stat/device")
    if devices is None:
        log("devices: endpoint unreachable, skipping")
        return

    n_ap = n_sw = n_port = 0
    for dev in devices:
        try:
            dtype = dev.get("type", "")
            dname = dev.get("name") or dev.get("model") or "unknown"
            dslug = _slug(dname)
            base_meta = {"device": dname, "type": dtype}

            # Common: device state, uptime, sys-stats
            state = _num(dev.get("state"))
            if state is not None:
                rows.append((NOW, "unifi_device_state", state, json.dumps(base_meta)))
            sys_stats = dev.get("system-stats") or dev.get("sys_stats") or {}
            cpu = _num(sys_stats.get("cpu"))
            mem = _num(sys_stats.get("mem"))
            if cpu is not None:
                rows.append((NOW, "unifi_device_cpu_pct", cpu, json.dumps(base_meta)))
            if mem is not None:
                rows.append((NOW, "unifi_device_mem_pct", mem, json.dumps(base_meta)))

            # ── Access points: per-radio ───────────────────────────────────
            if dtype == "uap":
                n_ap += 1
                total_sta = _num(dev.get("user-num_sta", dev.get("num_sta")))
                if total_sta is not None:
                    rows.append((NOW, "unifi_ap_clients", total_sta,
                                 json.dumps({"device": dname})))
                for radio in dev.get("radio_table_stats", []):
                    rname = radio.get("name", "")
                    band = _band_for_radio(radio.get("radio"))
                    rmeta = {"device": dname, "radio": rname,
                             "band": band, "channel": radio.get("channel")}
                    r_sta = _num(radio.get("num_sta"))
                    sat = _num(radio.get("satisfaction"))
                    if r_sta is not None:
                        rows.append((NOW, "unifi_ap_radio_clients", r_sta, json.dumps(rmeta)))
                    # satisfaction of -1 means "no clients" — skip as noise
                    if sat is not None and sat >= 0:
                        rows.append((NOW, "unifi_ap_radio_satisfaction", sat, json.dumps(rmeta)))
                    ch = _num(radio.get("channel"))
                    if ch is not None:
                        rows.append((NOW, "unifi_ap_radio_channel", ch, json.dumps(rmeta)))

            # ── Switches / gateway: per-port + per-switch client count ──────
            if dtype in ("usw", "udm", "ugw"):
                if dtype == "usw":
                    n_sw += 1
                sw_sta = _num(dev.get("num_sta"))
                if sw_sta is not None:
                    rows.append((NOW, "unifi_sw_clients", sw_sta,
                                 json.dumps({"device": dname, "type": dtype})))

                ports = dev.get("port_table", []) or []
                ports_up = 0
                poe_total = 0.0
                for p in ports:
                    try:
                        idx = p.get("port_idx")
                        pname = p.get("name") or f"port{idx}"
                        up = bool(p.get("up", False))
                        if up:
                            ports_up += 1
                        pmeta = {"device": dname, "port": idx, "port_name": pname}

                        # Throughput rate (bytes/sec, "-r" suffix = rate)
                        tx_r = _num(p.get("tx_bytes-r"))
                        rx_r = _num(p.get("rx_bytes-r"))
                        if tx_r is not None:
                            rows.append((NOW, "unifi_port_tx_bps", tx_r, json.dumps(pmeta)))
                        if rx_r is not None:
                            rows.append((NOW, "unifi_port_rx_bps", rx_r, json.dumps(pmeta)))

                        # Errors / drops (cumulative)
                        for fld, metric in (
                            ("tx_errors", "unifi_port_tx_errors"),
                            ("rx_errors", "unifi_port_rx_errors"),
                            ("tx_dropped", "unifi_port_tx_dropped"),
                            ("rx_dropped", "unifi_port_rx_dropped"),
                        ):
                            v = _num(p.get(fld))
                            if v is not None:
                                rows.append((NOW, metric, v, json.dumps(pmeta)))

                        # Link speed (Mbps) for up ports
                        spd = _num(p.get("speed"))
                        if up and spd is not None:
                            rows.append((NOW, "unifi_port_speed_mbps", spd, json.dumps(pmeta)))

                        # PoE power (watts)
                        poe = _num(p.get("poe_power"))
                        if poe is not None and poe > 0:
                            rows.append((NOW, "unifi_port_poe_w", poe, json.dumps(pmeta)))
                            poe_total += poe
                        n_port += 1
                    except Exception as e:
                        log(f"devices: port error on {dname}: {e}")

                rows.append((NOW, "unifi_sw_ports_up", float(ports_up),
                             json.dumps({"device": dname, "type": dtype})))
                rows.append((NOW, "unifi_sw_ports_total", float(len(ports)),
                             json.dumps({"device": dname, "type": dtype})))
                rows.append((NOW, "unifi_sw_poe_total_w", round(poe_total, 2),
                             json.dumps({"device": dname, "type": dtype})))
        except Exception as e:
            log(f"devices: per-device error: {e}")

    log(f"devices: {len(devices)} polled ({n_ap} AP, {n_sw} switch, {n_port} ports)")


def collect_wan(rows):
    """WAN up/down throughput, speedtest results, latency, gateway load."""
    health = api_get("stat/health")
    if health is None:
        log("wan: health endpoint unreachable, skipping")
        return

    for subsys in health:
        try:
            name = subsys.get("subsystem")
            status = subsys.get("status")
            meta = {"subsystem": name, "status": status}

            if name == "wan":
                # Live WAN throughput rate (bytes/sec).
                tx_r = _num(subsys.get("tx_bytes-r"))
                rx_r = _num(subsys.get("rx_bytes-r"))
                if tx_r is not None:
                    rows.append((NOW, "unifi_wan_tx_bps", tx_r, json.dumps(meta)))
                if rx_r is not None:
                    rows.append((NOW, "unifi_wan_rx_bps", rx_r, json.dumps(meta)))
                rows.append((NOW, "unifi_wan_up",
                             1.0 if status == "ok" else 0.0, json.dumps(meta)))
                gw = subsys.get("gw_system-stats") or {}
                gcpu = _num(gw.get("cpu"))
                gmem = _num(gw.get("mem"))
                if gcpu is not None:
                    rows.append((NOW, "unifi_wan_gw_cpu_pct", gcpu, json.dumps(meta)))
                if gmem is not None:
                    rows.append((NOW, "unifi_wan_gw_mem_pct", gmem, json.dumps(meta)))

            if name == "www":
                # Speedtest results + measured latency live here.
                up = _num(subsys.get("xput_up"))
                down = _num(subsys.get("xput_down"))
                lat = _num(subsys.get("latency"))
                ping = _num(subsys.get("speedtest_ping"))
                if up is not None:
                    rows.append((NOW, "unifi_wan_speedtest_up_mbps", up, json.dumps(meta)))
                if down is not None:
                    rows.append((NOW, "unifi_wan_speedtest_down_mbps", down, json.dumps(meta)))
                if lat is not None:
                    rows.append((NOW, "unifi_wan_latency_ms", lat, json.dumps(meta)))
                if ping is not None:
                    rows.append((NOW, "unifi_wan_speedtest_ping_ms", ping, json.dumps(meta)))

            # num_user (active users) per subsystem where present
            nu = _num(subsys.get("num_user"))
            if nu is not None:
                rows.append((NOW, "unifi_subsystem_users", nu, json.dumps(meta)))
        except Exception as e:
            log(f"wan: subsystem error: {e}")

    log(f"wan: {len(health)} subsystems processed")


# ── DB ────────────────────────────────────────────────────────────────────


def insert_rows(rows):
    """Bulk-insert collected metric rows into telemetry.unifi_metrics."""
    if not rows:
        log("no rows to insert")
        return 0
    try:
        import psycopg2
        import psycopg2.extras
    except Exception as e:
        log(f"psycopg2 import failed: {e}")
        return 0
    try:
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor()
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO telemetry.unifi_metrics (ts, metric, value, metadata)
               VALUES %s""",
            rows,
            template="(%s, %s, %s, %s::jsonb)",
        )
        conn.commit()
        cur.close()
        conn.close()
        return len(rows)
    except Exception as e:
        log(f"DB insert failed: {e}")
        return 0


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    rows = []
    for collector in (collect_clients, collect_devices, collect_wan):
        try:
            collector(rows)
        except Exception as e:
            log(f"{collector.__name__} crashed (continuing): {e}")
    inserted = insert_rows(rows)
    log(f"DONE: collected {len(rows)} metrics, inserted {inserted} rows")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Absolute backstop — never crash the scheduler.
        log(f"FATAL (suppressed): {e}")
    sys.exit(0)
