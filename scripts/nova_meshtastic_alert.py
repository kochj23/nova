#!/usr/bin/env python3
"""nova_meshtastic_alert.py — the alert path that survives the fleet dying.

On 2026-07-27 the Synology went down, /nova vanished from three nodes, and the gateway spent
the morning in a 536-cycle restart loop. Every alerting channel Nova owns — Slack, Discord,
Signal, email, the journal — rides on the same infrastructure that was failing. When the fleet
is sick, the fleet cannot reliably tell you it is sick. That is a self-report, and this whole
week has been about not trusting those.

LoRa does not care. It is a separate radio, a separate power domain, and it does not need the
NAS, Postgres, the gateway, DNS, or the internet. This sends short critical alerts over
Meshtastic so the one message that matters can get out when nothing else can.

Deliberately minimal: LoRa is slow and the airtime budget is tiny, so messages are capped hard
and only genuinely critical events belong here. Anything chatty stays on Slack.

Usage:
  nova_meshtastic_alert.py "text"        send one alert
  nova_meshtastic_alert.py --self-test   prove the radio works, end to end
  nova_meshtastic_alert.py --watch       poll fleet health, alert when core services are down
"""
import argparse
import glob
import sys
import time

MAX_CHARS = 200          # LoRa payload is small; anything longer is truncated on air anyway
CRITICAL_CHECKS = [
    ("postgres", "pg-primary.digitalnoise.net", 5432),
    ("gateway", "192.168.1.2", 18792),
    ("memory-server", "192.168.1.2", 18790),
]


def log(m):
    print(f"[mesh-alert {time.strftime('%H:%M:%S')}] {m}", flush=True)


def find_device():
    """The serial path is NOT stable — the bridge had /dev/cu.usbmodem31201 hardcoded and the
    node now enumerates as /dev/cu.usbmodemSN234567892. Discover it instead of assuming."""
    for pat in ("/dev/cu.usbmodem*", "/dev/ttyUSB*", "/dev/ttyACM*"):
        for p in sorted(glob.glob(pat)):
            return p
    return None


def send(text, device=None):
    import meshtastic.serial_interface
    dev = device or find_device()
    if not dev:
        log("no Meshtastic device found")
        return False
    text = text.strip()[:MAX_CHARS]
    iface = None
    try:
        iface = meshtastic.serial_interface.SerialInterface(devPath=dev)
        iface.sendText(text)
        time.sleep(3)          # let it actually hit the air before we close the port
        log(f"sent via {dev}: {text[:80]}")
        return True
    except Exception as e:
        log(f"send FAILED on {dev}: {type(e).__name__}: {e}")
        return False
    finally:
        if iface:
            try:
                iface.close()
            except Exception:
                pass


def reachable(host, port, timeout=4):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def watch(alert):
    """Check the services that everything else depends on. If they are down, the normal
    channels are probably down too — which is exactly when this path earns its keep."""
    down = [name for name, host, port in CRITICAL_CHECKS if not reachable(host, port)]
    if not down:
        log("all critical services reachable — nothing to send")
        return 0
    msg = f"NOVA CRITICAL: {', '.join(down)} unreachable at {time.strftime('%H:%M')}. Fleet may be down."
    log(msg)
    if alert:
        send(msg)
    return 1


def self_test():
    """A witness that has never been seen to work is decoration. Prove the radio can send."""
    dev = find_device()
    log(f"device: {dev or 'NOT FOUND'}")
    if not dev:
        return 2
    ok = send(f"Nova self-test {time.strftime('%Y-%m-%d %H:%M')} - out-of-band path OK", dev)
    log("SELF-TEST PASSED" if ok else "SELF-TEST FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("text", nargs="?", help="message to send")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--watch", action="store_true", help="check critical services")
    ap.add_argument("--alert", action="store_true", help="with --watch, actually transmit")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(self_test())
    if a.watch:
        sys.exit(watch(a.alert))
    if a.text:
        sys.exit(0 if send(a.text) else 1)
    ap.print_help()
