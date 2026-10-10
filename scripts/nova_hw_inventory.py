#!/usr/bin/env python3
"""nova_hw_inventory.py — daily HARDWARE/peripheral inventory across the fleet.

The sibling to nova_pkg_audit: that catalogues what SOFTWARE runs; this catalogues what HARDWARE
exists. Built 2026-08-13 after a six-message scavenger hunt to answer "which box has the spare LoRa
board and which boxes have Bluetooth" — questions Nova should answer from a table, not by SSH-ing
around. Per node it records:
  - USB devices        (lsusb / system_profiler)                 category='usb'
  - Serial ports       (/dev/ttyUSB*,/dev/cu.usbmodem*) + holder category='serial'
  - Bluetooth adapters (hciconfig) + up/down state               category='bluetooth'
  - LoRa/Meshtastic    (serial-bridge chip + meshtastic process) category='lora'

Clean rows land in hardware_inventory. The security report reads it for Ring 1 (peripherals per
node) and can flag a NEW/rogue USB device appearing — a real security signal.

Robust: an unreachable host is recorded reachable=false, never fabricated as empty.
Scheduled daily (scheduler task hw_inventory). Manual: python3 nova_hw_inventory.py
"""
import re
import subprocess
import sys
import time

import psycopg2

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
LOCAL_IPS = {"192.168.1.6"}   # .6 (mac-studio) runs this — collect locally

# BLE/serial-bridge chips that identify a plugged-in dev board (ESP32/LoRa/Arduino etc.)
BRIDGE_CHIPS = {"1a86": "CH340/CH341", "10c4": "CP210x", "0403": "FTDI", "303a": "Espressif-native",
                "239a": "Adafruit", "2e8a": "RP2040", "0483": "STM32"}


def log(m):
    print(f"[hw_inventory {time.strftime('%H:%M:%S')}] {m}", flush=True)


def _run(cmd, ip=None, timeout=30):
    """Run one of this file's static collection scripts locally (LOCAL_IPS) or over ssh.
    No shell=True: local runs use an explicit sh -c argv, remote runs pass the script as a
    single ssh argument (what the old single-quote wrapping achieved via the local shell)."""
    if ip in LOCAL_IPS:
        argv = ["/bin/sh", "-c", cmd]
    else:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "StrictHostKeyChecking=accept-new", f"kochj@{ip}", cmd]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "")
    except Exception:
        return -1, ""


def get_hosts():
    conn = psycopg2.connect(DSN); conn.autocommit = True; cur = conn.cursor()
    cur.execute("SELECT node_name, node_ip, os_family FROM cinc_node_configs ORDER BY node_name")
    rows = cur.fetchall(); conn.close()
    return rows


def _section(blob, tag):
    """Pull one ===TAG=== ... block out of the single-shot collection output. Line-anchored
    lookahead (re.M) so an EMPTY section (its marker immediately followed by the next ===MARKER===)
    correctly captures nothing instead of swallowing the next section."""
    m = re.search(rf"^===\s*{tag}\s*===[ \t]*\n(.*?)(?=^===|\Z)", blob, re.S | re.M)
    return (m.group(1).strip() if m else "")


def collect(name, ip, osf):
    """Return (items, reachable). ONE ssh call per host (avoids sshd rate-limiting and is fast)."""
    items = []
    if osf == "macos":
        script = ("echo '===HOST==='; hostname; "
                  "echo '===SERIAL==='; ls /dev/ | grep -iE 'cu.usbmodem|cu.usbserial|tty.usbserial'; "
                  "echo '===USB==='; system_profiler SPUSBDataType 2>/dev/null | grep -E '.+:$' | "
                  "grep -viE 'hub|bus|host controller|SPUSBDataType' | head -40")
        rc, out = _run(script, ip, timeout=40)
        if rc != 0 or "===HOST===" not in out:
            return [], False
        for s in [x.strip() for x in _section(out, "SERIAL").splitlines() if x.strip()]:
            items.append(("serial", s, "", "present", ""))
        for u in [x.strip().rstrip(":") for x in _section(out, "USB").splitlines() if x.strip()][:40]:
            items.append(("usb", u, "", "present", ""))
        items.append(("bluetooth", "built-in (Apple)", "", "present", "macOS CoreBluetooth"))
        return items, True

    # linux — one combined command. by-id names the actual device (Z-Wave/Zigbee/LoRa/ESP), which
    # is far better than guessing from the bridge chip: nova-core's CP210x is a SONOFF Z-Wave dongle,
    # NOT a LoRa board. That distinction is exactly what a hardware inventory should get right.
    script = ("echo '===HOST==='; hostname; "
              "echo '===USB==='; lsusb 2>/dev/null; "
              "echo '===SERIAL==='; ls /dev/ 2>/dev/null | grep -iE 'ttyUSB|ttyACM'; "
              "echo '===BYID==='; ls -l /dev/serial/by-id/ 2>/dev/null; "
              "echo '===HCI==='; hciconfig 2>/dev/null")
    rc, out = _run(script, ip, timeout=40)
    if rc != 0 or "===HOST===" not in out:
        return [], False
    usb = _section(out, "USB")
    for line in usb.splitlines():
        m = re.search(r"ID ([0-9a-f]{4}):([0-9a-f]{4})\s*(.*)$", line, re.I)
        if not m:
            continue
        vid, pid, desc = m.group(1).lower(), m.group(2).lower(), (m.group(3) or "").strip()
        if re.search(r"linux foundation|root hub", desc, re.I):
            continue
        items.append(("usb", desc or f"{vid}:{pid}", f"{vid}:{pid}", "present", BRIDGE_CHIPS.get(vid, "")))
    # map each tty to its by-id identity (e.g. "SONOFF_ZWave_Dongle", "1a86_USB_Serial")
    byid = {}
    for line in _section(out, "BYID").splitlines():
        m = re.search(r"(\S+)\s*->\s*\.\.\/\.\.\/(tty\w+)", line)
        if m:
            byid[m.group(2)] = m.group(1)

    def _classify(idname):
        low = (idname or "").lower()
        if "zwave" in low or "z-wave" in low or "zooz" in low or "aeotec" in low:
            return "zwave", "Z-Wave controller"
        if "zigbee" in low or "conbee" in low or "sonoff_zigbee" in low or "cc253" in low or "slzb" in low:
            return "zigbee", "Zigbee controller"
        if any(k in low for k in ("heltec", "lilygo", "ttgo", "tbeam", "t-beam", "meshtastic", "lora", "esp32", "elecrow")):
            return "lora", "LoRa/ESP dev board"
        return "serial", (idname or "serial device")

    for s in [x.strip() for x in _section(out, "SERIAL").splitlines() if x.strip()]:
        idn = byid.get(s, "")
        cat, label = _classify(idn)
        items.append((cat, f"{label} on {s}" if cat != "serial" else s, "", "present", idn[:120]))
    cur_name = None
    for line in _section(out, "HCI").splitlines():
        hm = re.match(r"^(hci\d+):", line)
        if hm:
            cur_name = hm.group(1)
        elif cur_name and re.search(r"\b(UP|DOWN)\b", line):
            items.append(("bluetooth", cur_name, "", "up" if "UP" in line else "down", "hci adapter"))
            cur_name = None
    return items, True


def ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS hardware_inventory (
            id serial PRIMARY KEY, ts timestamptz DEFAULT now(), host_name text,
            category text, name text, identifier text, status text, detail text);
        CREATE TABLE IF NOT EXISTS hardware_inventory_hosts (
            host_name text PRIMARY KEY, ts timestamptz DEFAULT now(), reachable boolean,
            usb int, serial int, bluetooth int, lora int);""")


def main():
    conn = psycopg2.connect(DSN); conn.autocommit = True; cur = conn.cursor()
    ensure_table(cur)
    hosts = get_hosts()
    log(f"inventorying {len(hosts)} hosts")
    for name, ip, osf in hosts:
        try:
            items, reachable = collect(name, ip, osf)
        except Exception as e:
            log(f"  {name}: error {e}"); items, reachable = [], False
        cats = {"usb": 0, "serial": 0, "bluetooth": 0, "lora": 0}
        if reachable:
            cur.execute("DELETE FROM hardware_inventory WHERE host_name=%s", (name,))
            for cat, nm, ident, status, detail in items:
                cats[cat] = cats.get(cat, 0) + 1
                cur.execute("INSERT INTO hardware_inventory (host_name,category,name,identifier,status,detail) "
                            "VALUES (%s,%s,%s,%s,%s,%s)",
                            (name, cat, (nm or "")[:160], (ident or "")[:32], (status or "")[:20], (detail or "")[:160]))
        cur.execute("""INSERT INTO hardware_inventory_hosts (host_name,reachable,usb,serial,bluetooth,lora,ts)
                       VALUES (%s,%s,%s,%s,%s,%s,now())
                       ON CONFLICT (host_name) DO UPDATE SET reachable=EXCLUDED.reachable, usb=EXCLUDED.usb,
                       serial=EXCLUDED.serial, bluetooth=EXCLUDED.bluetooth, lora=EXCLUDED.lora, ts=now()""",
                    (name, reachable, cats["usb"], cats["serial"], cats["bluetooth"], cats["lora"]))
        log(f"  {name}: {'ok' if reachable else 'UNREACHABLE'} — "
            f"usb={cats['usb']} serial={cats['serial']} bt={cats['bluetooth']} lora={cats['lora']}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
