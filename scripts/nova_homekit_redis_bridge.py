#!/opt/homebrew/bin/python3
"""Bridge: reads HomeKit accessories from NovaHomeKit, caches in Redis every 60s."""
import json, signal, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import redis

import nova_homekit_client as hk  # Bearer token (NovaHomeKit 51e7a91); loop re-polls every 60s

HOMEKIT_URL = "http://127.0.0.1:37433/api/accessories"
REDIS_KEY = "nova:homekit:accessories"
INTERVAL = 60
_shutdown = False

def _sig(s, f): global _shutdown; _shutdown = True
signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)

r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True)

while not _shutdown:
    try:
        resp = urllib.request.urlopen(urllib.request.Request(HOMEKIT_URL, headers=hk.auth_headers()), timeout=120)
        data = resp.read().decode()
        r.setex(REDIS_KEY, 300, data)  # Cache for 5 min
        accessories = json.loads(data)
        print(f"[homekit_bridge] Cached {len(accessories)} accessories in Redis", flush=True)
    except Exception as e:
        print(f"[homekit_bridge] Error: {e}", flush=True)
    for _ in range(INTERVAL):
        if _shutdown: break
        time.sleep(1)
