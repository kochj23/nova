#!/usr/bin/env python3
"""
Nova Weekly Network Security Scan
Runs every Friday afternoon via NMAPScanner
Reports to Slack with threat summary
"""

import subprocess
import requests
import json
from datetime import datetime

def run_nmap_scan():
    """Trigger network scan via NovaControl unified API (port 37400)"""
    try:
        # Trigger scan of local subnet
        response = requests.post(
            "http://127.0.0.1:37400/api/nmap/scan",
            json={"ip": "192.168.1.0/24"},
            timeout=300
        )

        if response.status_code != 200:
            return {"error": f"Scan trigger returned {response.status_code}"}

        # Get devices
        devices_resp = requests.get("http://127.0.0.1:37400/api/nmap/devices", timeout=30)
        devices = devices_resp.json() if devices_resp.status_code == 200 else []

        # Get threats
        threats_resp = requests.get("http://127.0.0.1:37400/api/nmap/threats", timeout=30)
        threats = threats_resp.json() if threats_resp.status_code == 200 else []

        return {
            "device_count": len(devices) if isinstance(devices, list) else 0,
            "threats": threats if isinstance(threats, list) else [],
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return {"error": str(e)}

def post_to_slack(scan_results):
    """Post scan results to #nova-chat"""
    
    threats = scan_results.get("threats", [])
    devices = scan_results.get("device_count", 0)
    timestamp = scan_results.get("timestamp", datetime.now().isoformat())
    
    if threats:
        threat_summary = "\n".join([f"  🔴 {t['severity']}: {t['description']}" for t in threats[:10]])
        message = f"""```
🔍 WEEKLY NETWORK SECURITY SCAN

Date: {timestamp}
Devices Scanned: {devices}

THREATS DETECTED: {len(threats)}
{threat_summary}

Action: Review and remediate above threats
—N
```"""
    else:
        message = f"""```
✓ WEEKLY NETWORK SECURITY SCAN

Date: {timestamp}
Devices Scanned: {devices}
Threats: NONE

Network status: CLEAN
—N
```"""
    
    # Post to Slack
    subprocess.run([
        "python3", "-c",
        f"""
import subprocess
subprocess.run(['bash', str(Path.home() / '.openclaw/scripts/nova_herd_broadcast.sh'),
  '--subject', 'Weekly Network Security Scan',
  '--body-file', '/dev/stdin'],
input={repr(message).encode()})
"""
    ], capture_output=True)

if __name__ == "__main__":
    print(f"[{datetime.now().isoformat()}] Running weekly network security scan...")
    
    results = run_nmap_scan()
    
    if "error" not in results:
        print(f"✓ Scan complete: {results.get('device_count', 0)} devices, {len(results.get('threats', []))} threats")
        post_to_slack(results)
    else:
        print(f"✗ Scan error: {results['error']}")
