#!/usr/bin/env python3
"""Daily top 10 bandwidth consumers — runs at 23:50 via launchd."""
import json, sys, ssl, subprocess, urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_CHAN = nova_config.SLACK_CHAN
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

def main():
    key = get_api_key()
    if not key:
        return

    url = f"https://192.168.1.1/proxy/network/api/s/default/stat/sta"
    req = urllib.request.Request(url, headers={"X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"API error: {e}")
        return

    clients = data.get("data", [])
    
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
    lines.append(f"```{'Device':<30} {'Total':>10} {'Down':>10} {'Up':>10}")
    lines.append(f"{'-'*30} {'-'*10} {'-'*10} {'-'*10}")

    for c in top10:
        total_gb = c["total"] / 1024/1024/1024
        rx_gb = c["rx"] / 1024/1024/1024
        tx_gb = c["tx"] / 1024/1024/1024
        lines.append(f"{c['name']:<30} {total_gb:>9.1f}G {rx_gb:>9.1f}G {tx_gb:>9.1f}G")

    total_all = sum(c["total"] for c in ranked) / 1024/1024/1024
    lines.append(f"{'-'*30} {'-'*10} {'-'*10} {'-'*10}")
    lines.append(f"{'Network total':<30} {total_all:>9.1f}G")
    lines.append("```")
    lines.append(f"_{len(clients)} clients connected_")

    msg = "\n".join(lines)
    print(msg)
    slack_post(msg)

    # Store in memory
    summary = f"Bandwidth report {now.strftime('%Y-%m-%d')}: top consumer {top10[0]['name']} at {top10[0]['total']/1024/1024/1024:.1f} GB. {len(clients)} clients, {total_all:.0f} GB total."
    payload = json.dumps({"text": summary, "source": "infrastructure", "metadata": {"type": "bandwidth_report", "date": now.strftime('%Y-%m-%d')}}).encode()
    try:
        req = urllib.request.Request(f"{VECTOR_URL}?async=1", data=payload, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

if __name__ == "__main__":
    main()
