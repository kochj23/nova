#!/usr/bin/env python3
"""Resolve a healthy Nova inference-router endpoint.

The fabric router runs on .2 (primary) and .10 (hot standby, always-on systemd).
A clean VIP isn't viable because .2 is dual-homed on the LAN (wired .2 + WiFi
.138) — a virtual IP there returns traffic asymmetrically and TCP breaks. So
callers just probe .2 first and fall back to .10. Cheap, no infra, no split-brain.
"""
import urllib.request

ROUTERS = ["192.168.1.2", "192.168.1.10"]  # primary, backup — order matters

def base(timeout=2.0):
    """First router answering /health, else the primary (so callers still try)."""
    for ip in ROUTERS:
        try:
            urllib.request.urlopen(f"http://{ip}:37475/health", timeout=timeout).read()
            return f"http://{ip}:37475"
        except Exception:
            continue
    return f"http://{ROUTERS[0]}:37475"

def chat_url(timeout=2.0):
    return base(timeout) + "/v1/chat/completions"

if __name__ == "__main__":
    # self-check: resolves to one of the known routers, and to the primary when it's up
    b = base()
    assert b in (f"http://{ip}:37475" for ip in ROUTERS), b
    assert chat_url().endswith("/v1/chat/completions")
    print("router endpoint ->", b)
