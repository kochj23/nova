#!/usr/bin/env python3
"""Daily top 10 bandwidth consumers — runs at 23:50 via launchd."""
import json, sys, ssl, subprocess, urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_CHAN = "C0ATAF7NZG9"  # #nova-notifications (not #nova-chat)
SLACK_TOKEN = nova_config.slack_bot_token()
VECTOR_URL = nova_config.VECTOR_URL

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

def get_api_key():
    r = subprocess.run(["security", "find-generic-password", "-a", "nova", "-s", "nova-unifi-api-key", "-w"],
        capture_output=True, text=True)
    return r.stdout.strip()

def slack_post(text):
    data = json.dumps({"channel": SLACK_CHAN, "text": text, "mrkdwn": True}).encode()
    req = urllib.request.Request("https://slack.com/api/chat.postMessage", data=data,
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json; charset=utf-8"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass

def api_get(endpoint, key):
    """GET request to UDM Pro API."""
    url = f"https://192.168.1.1/proxy/network/api/s/default/{endpoint}"
    req = urllib.request.Request(url, headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as r:
        data = json.loads(r.read())
    return data.get("data", [])

def api_post(endpoint, payload, key):
    """POST request to UDM Pro API (for stat reports)."""
    url = f"https://192.168.1.1/proxy/network/api/s/default/{endpoint}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
        headers={"X-API-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as r:
        data = json.loads(r.read())
    return data.get("data", [])

def get_wan_daily(key):
    """Get WAN daily traffic from the hourly site report endpoint."""
    now = datetime.now()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # UniFi report endpoints require epoch MILLISECONDS
    start_ms = int(start_of_day.timestamp()) * 1000
    end_ms = int(now.timestamp()) * 1000

    wan_down = 0
    wan_up = 0

    # Hourly site report — most reliable for daily totals
    try:
        report = api_post("stat/report/hourly.site", {
            "attrs": ["wan-tx_bytes", "wan-rx_bytes"],
            "start": start_ms,
            "end": end_ms,
        }, key)
        for entry in report:
            wan_down += entry.get("wan-rx_bytes", 0)
            wan_up += entry.get("wan-tx_bytes", 0)
    except Exception:
        pass

    return wan_down, wan_up


def get_wan_health(key):
    """Get WAN health details: latency, speed, uptime, ISP, IP."""
    wan_info = {}
    try:
        health_data = api_get("stat/health", key)
        for subsys in health_data:
            if subsys.get("subsystem") == "wan":
                wan_info["status"] = subsys.get("status", "unknown")
                wan_info["latency_ms"] = subsys.get("latency", 0)
                wan_info["uptime_s"] = subsys.get("uptime", 0)
                wan_info["speed_test_ping"] = subsys.get("speedtest_ping", 0)
                wan_info["speed_test_down"] = subsys.get("speedtest_download", 0)
                wan_info["speed_test_up"] = subsys.get("speedtest_upload", 0)
                wan_info["speed_test_time"] = subsys.get("speedtest_lastrun", 0)
                gateways = subsys.get("gateways", [])
                if gateways:
                    gw = gateways[0] if isinstance(gateways[0], dict) else {}
                    wan_info["isp"] = gw.get("isp_name", "")
                    wan_info["ip"] = gw.get("wan_ip", "")
                    wan_info["gw_latency"] = gw.get("latency", 0)
                break
    except Exception:
        pass

    # Also try the device endpoint for UDM Pro details
    try:
        devices = api_get("stat/device", key)
        for dev in devices:
            if dev.get("type") == "udm" or dev.get("model", "").startswith("UDM"):
                wan_table = dev.get("wan1", dev.get("port_table", [{}]))
                if isinstance(wan_table, dict):
                    wan_info.setdefault("ip", wan_table.get("ip", ""))
                    wan_info["wan_speed"] = wan_table.get("speed", 0)
                    wan_info["wan_full_duplex"] = wan_table.get("full_duplex", False)
                uptime = dev.get("uptime", 0)
                if uptime:
                    wan_info.setdefault("uptime_s", uptime)
                break
    except Exception:
        pass

    # Get current throughput rates
    try:
        health_data = api_get("stat/health", key)
        for subsys in health_data:
            if subsys.get("subsystem") == "wan":
                rx_rate = subsys.get("rx_bytes-r", 0)  # bytes/sec current
                tx_rate = subsys.get("tx_bytes-r", 0)
                wan_info["rx_rate_mbps"] = rx_rate * 8 / 1_000_000  # convert to Mbps
                wan_info["tx_rate_mbps"] = tx_rate * 8 / 1_000_000
                wan_info["wan_ip"] = subsys.get("wan_ip", wan_info.get("ip", ""))
                break
    except Exception:
        pass

    return wan_info


def main():
    key = get_api_key()
    if not key:
        return

    try:
        clients = api_get("stat/sta", key)
    except Exception as e:
        print(f"API error: {e}")
        return

    # Get WAN internet totals and health
    wan_down, wan_up = get_wan_daily(key)
    wan_health = get_wan_health(key)

    # Calculate total bytes per client
    ranked = []
    for c in clients:
        name = c.get("hostname", c.get("name", c.get("oui", "Unknown")))
        tx = c.get("tx_bytes", 0)
        rx = c.get("rx_bytes", 0)
        total = tx + rx
        if total > 0:
            ranked.append({"name": name, "tx": tx, "rx": rx, "total": total})

    ranked.sort(key=lambda x: x["total"], reverse=True)
    top10 = ranked[:10]

    now = datetime.now()
    lines = [f"*Daily Bandwidth Report — {now.strftime('%A, %B %d')}*", ""]

    # WAN section
    lines.append("*WAN / Internet:*")
    wan_status = wan_health.get("status", "unknown")
    status_emoji = ":large_green_circle:" if wan_status == "ok" else ":red_circle:"
    lines.append(f"  {status_emoji} Status: {wan_status.upper()}")

    if wan_health.get("ip"):
        isp = wan_health.get("isp", "")
        ip_line = f"  IP: {wan_health['ip']}"
        if isp:
            ip_line += f" ({isp})"
        lines.append(ip_line)

    if wan_health.get("latency_ms"):
        lines.append(f"  Latency: {wan_health['latency_ms']}ms")

    if wan_health.get("uptime_s"):
        uptime_d = wan_health["uptime_s"] // 86400
        uptime_h = (wan_health["uptime_s"] % 86400) // 3600
        lines.append(f"  Uptime: {uptime_d}d {uptime_h}h")

    if wan_health.get("speed_test_down"):
        lines.append(f"  Last speedtest: {wan_health['speed_test_down']:.0f} Mbps down / {wan_health.get('speed_test_up', 0):.0f} Mbps up (ping: {wan_health.get('speed_test_ping', 0):.0f}ms)")

    if wan_health.get("rx_rate_mbps") or wan_health.get("tx_rate_mbps"):
        lines.append(f"  Current: {wan_health.get('rx_rate_mbps', 0):.1f} Mbps down / {wan_health.get('tx_rate_mbps', 0):.1f} Mbps up")

    if wan_down > 0 or wan_up > 0:
        wan_down_gb = wan_down / 1024/1024/1024
        wan_up_gb = wan_up / 1024/1024/1024
        wan_total_gb = (wan_down + wan_up) / 1024/1024/1024
        lines.append(f"  Today: {wan_down_gb:,.1f}G down / {wan_up_gb:,.1f}G up ({wan_total_gb:,.1f}G total)")

    lines.append("")
    lines.append(f"*Top 10 LAN clients:*")
    lines.append(f"```{'Device':<30} {'Total':>10} {'Down':>10} {'Up':>10}")
    lines.append(f"{'-'*30} {'-'*10} {'-'*10} {'-'*10}")

    for c in top10:
        total_gb = c["total"] / 1024/1024/1024
        rx_gb = c["rx"] / 1024/1024/1024
        tx_gb = c["tx"] / 1024/1024/1024
        lines.append(f"{c['name']:<30} {total_gb:>9.1f}G {rx_gb:>9.1f}G {tx_gb:>9.1f}G")

    total_all = sum(c["total"] for c in ranked) / 1024/1024/1024
    lines.append(f"{'-'*30} {'-'*10} {'-'*10} {'-'*10}")
    lines.append(f"{'LAN total':<30} {total_all:>9.1f}G")
    lines.append("```")
    lines.append(f"_{len(clients)} clients connected_")

    msg = "\n".join(lines)
    print(msg)
    slack_post(msg)

    # Store in memory
    wan_str = ""
    if wan_down > 0 or wan_up > 0:
        wan_str = f" WAN: {wan_down/1024/1024/1024:.1f}G down / {wan_up/1024/1024/1024:.1f}G up."
    summary = f"Bandwidth report {now.strftime('%Y-%m-%d')}: top consumer {top10[0]['name']} at {top10[0]['total']/1024/1024/1024:.1f} GB. {len(clients)} clients, {total_all:.0f} GB LAN total.{wan_str}"
    payload = json.dumps({"text": summary, "source": "infrastructure", "metadata": {"type": "bandwidth_report", "date": now.strftime('%Y-%m-%d')}}).encode()
    try:
        req = urllib.request.Request(f"{VECTOR_URL}?async=1", data=payload, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

if __name__ == "__main__":
    main()
