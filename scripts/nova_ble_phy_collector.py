#!/usr/bin/env python3
"""nova_ble_phy_collector.py — PHY-layer BLE observer via Ubertooth One.

The host Bluetooth stack (bleak/BlueZ) only reports what it chooses to surface. An
Ubertooth listens to the raw radio, which is the only way past the population
nova_ble_monitor documents as unsolvable in software: bare randomising devices with
no name, no service UUIDs and a generic company ID all collapse into one weak
fingerprint. At the PHY layer we additionally see channel index and true per-packet
RSSI, which is what makes those devices separable later.

Feeds the SAME table as the host-stack observers, tagged observer='<host>-phy', and
uses compute_ble_fingerprint() from nova_ble_monitor VERBATIM — an identity computed
differently on a different observer correlates with nothing, which would quietly
defeat the entire point of the observer column.

Runs on nova-core5 (.10), where the Ubertooth lives. udev grants unprivileged access.
"""
import os
import re
import signal
import socket
import subprocess
from collections import Counter, defaultdict
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import psycopg2
import psycopg2.extras

from nova_ble_monitor import (compute_ble_fingerprint,          # identity MUST match exactly
                              compute_cross_observer_fingerprint)

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
OBSERVER = os.environ.get("NOVA_BLE_OBSERVER") or f"{socket.gethostname().split('.')[0].lower()}-phy"
UBERTOOTH = os.environ.get("UBERTOOTH_BIN", "/usr/bin/ubertooth-btle")
FLUSH_SECONDS = 30          # batch inserts; the radio is far chattier than the DB should be
DEDUPE_SECONDS = 60         # one row per (address, observer) per minute — see ble_sightings view

_shutdown = False


def _sig(_s, _f):
    global _shutdown
    _shutdown = True


signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)


def log(m):
    print(f"[ble-phy {time.strftime('%H:%M:%S')}] {m}", flush=True)


# ubertooth-btle -n emits a header line then an indented decode block per packet.
RE_HDR = re.compile(r"systime=(\d+)\s+freq=(\d+)\s+addr=\w+\s+delta_t=[\d.]+\s+ms\s+rssi=(-?\d+)")
RE_ADVA = re.compile(r"AdvA:\s+([0-9a-fA-F:]{17})\s*(\(random\)|\(public\))?")
RE_CHAN = re.compile(r"Channel Index:\s*(\d+)")



# ── AdvData TLV decoding ──────────────────────────────────────────────────────
# BLE advertising data is a packed chain of [Length][Type][Value...] elements. The
# host-stack observer gets these already parsed by bleak; this observer only has raw
# bytes, so it must decode them into the IDENTICAL shapes or the fingerprints cannot
# match (measured 2026-07-27: 1 shared fingerprint fleet-wide before this existed).
#
# bleak reports service UUIDs as lowercase full 128-bit strings, so 16- and 32-bit
# UUIDs must be expanded against the Bluetooth Base UUID. Multi-byte values in the
# advertisement are LITTLE-ENDIAN, including the 128-bit UUIDs (reversed byte order).
_BASE_UUID = "-0000-1000-8000-00805f9b34fb"

AD_UUID16 = (0x02, 0x03)      # incomplete / complete list of 16-bit service UUIDs
AD_UUID32 = (0x04, 0x05)
AD_UUID128 = (0x06, 0x07)
AD_NAME = (0x08, 0x09)        # shortened / complete local name
AD_TXPOWER = 0x0A
AD_MANUFACTURER = 0xFF


def _uuid16(b):
    return f"0000{int.from_bytes(b, 'little'):04x}{_BASE_UUID}"


def _uuid32(b):
    return f"{int.from_bytes(b, 'little'):08x}{_BASE_UUID}"


def _uuid128(b):
    h = b[::-1].hex()          # little-endian on the wire
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def decode_advdata(raw: bytes):
    """Walk the TLV chain. Returns (name, service_uuids, company_ids, tx_power)
    shaped exactly like bleak's AdvertisementData so both observers hash the same
    inputs. Malformed/truncated tails stop the walk instead of raising — radio
    captures are lossy and a bad packet must not kill the collector."""
    name, uuids, companies, tx = None, [], [], None
    i, n = 0, len(raw)
    while i < n:
        ln = raw[i]
        if ln == 0 or i + ln >= n + 1 or i + 1 >= n:
            break
        typ = raw[i + 1]
        val = raw[i + 2:i + 1 + ln]
        if typ in AD_UUID16:
            uuids += [_uuid16(val[j:j + 2]) for j in range(0, len(val) - 1, 2)]
        elif typ in AD_UUID32:
            uuids += [_uuid32(val[j:j + 4]) for j in range(0, len(val) - 3, 4)]
        elif typ in AD_UUID128:
            uuids += [_uuid128(val[j:j + 16]) for j in range(0, len(val) - 15, 16)]
        elif typ in AD_NAME and val:
            try:
                # A lossy radio capture yields garbage bytes inside "names". Strip every
                # control character, not just leading/trailing NULs: Postgres rejects NUL
                # in string literals outright and killed the collector mid-insert.
                raw_name = val.decode("utf-8", "ignore")
                cleaned = "".join(ch for ch in raw_name if ch.isprintable()).strip()
                name = cleaned or name
            except Exception:
                pass
        elif typ == AD_TXPOWER and val:
            tx = int.from_bytes(val[:1], "little", signed=True)
        elif typ == AD_MANUFACTURER and len(val) >= 2:
            companies.append(int.from_bytes(val[:2], "little"))
        i += ln + 1
    return name, sorted(set(uuids)), sorted(set(companies)), tx



# ── Apple Find My / tracker classification ───────────────────────────────────
# Apple manufacturer data (company 0x004C) carries a subtype byte. 0x12 is the Find My
# network — AirTags, AirPods, and third-party Find My accessories. Its LENGTH is the
# signal that matters for tracker safety:
#   len 0x02  -> SHORT form. The tag is paired and its owner's device is nearby.
#   len 0x19  -> LONG form, carrying a rotating EC public key. The tag is SEPARATED from
#                its owner and is broadcasting for passing iPhones to relay its location.
# A separated tag that keeps appearing near one person, across days, is the unwanted-tracker
# case Apple's own Item Safety Alerts look for. We cannot tell WHICH tag is whose — the keys
# rotate and are derived from a secret only the owner's iCloud account holds — but "separated
# and persistent near us" is exactly the question worth asking, and it needs no Find My API.
APPLE_CID = 0x004C
APPLE_SUBTYPES = {0x02: "ibeacon", 0x05: "airdrop", 0x07: "proximity_pairing",
                  0x09: "airplay", 0x0C: "handoff", 0x10: "nearby", 0x12: "findmy"}


def classify_apple(adv: bytes):
    """Return (subtype_name, findmy_state) for Apple manufacturer data, else (None, None).
    findmy_state is 'owner_nearby' | 'separated' | None."""
    i = 0
    while i < len(adv):
        ln = adv[i]
        if ln == 0 or i + ln >= len(adv) + 1 or i + 1 >= len(adv):
            break
        if adv[i + 1] == 0xFF:
            v = adv[i + 2:i + 1 + ln]
            if len(v) >= 3 and int.from_bytes(v[:2], "little") == APPLE_CID:
                sub = v[2]
                name = APPLE_SUBTYPES.get(sub)
                if sub == 0x12:
                    plen = v[3] if len(v) > 3 else 0
                    return name, ("separated" if plen >= 0x18 else "owner_nearby")
                return name, None
        i += ln + 1
    return None, None


def parse_packets(stream):
    """Yield one dict per decoded advertisement. Tolerates partial/garbled blocks —
    a radio capture is lossy by nature and a malformed packet must not kill the run."""
    pkt = None
    for raw in stream:
        line = raw.rstrip("\n")
        m = RE_HDR.search(line)
        if m:
            if pkt and pkt.get("mac"):
                yield pkt
            pkt = {"ts": int(m.group(1)), "freq": int(m.group(2)),
                   "rssi": int(m.group(3)), "raw": []}
            continue
        if pkt is None:
            continue
        pkt["raw"].append(line)
        m = RE_ADVA.search(line)
        if m:
            pkt["mac"] = m.group(1).upper()
            pkt["addr_type"] = (m.group(2) or "").strip("()") or "unknown"
        m = RE_CHAN.search(line)
        if m:
            pkt["channel"] = int(m.group(1))
    if pkt and pkt.get("mac"):
        yield pkt


def enrich(pkt):
    """Decode the advertisement into bleak-shaped fields, then fingerprint it."""
    blob = "\n".join(pkt.get("raw", []))
    m = re.search(r"AdvData:\s*((?:[0-9a-fA-F]{2}\s+)+)", blob)
    adv = b""
    if m:
        try:
            adv = bytes.fromhex(m.group(1).replace(" ", "").replace("\n", ""))
        except ValueError:
            adv = b""
    name, uuids, company_ids, tx_power = decode_advdata(adv)
    apple_sub, findmy_state = classify_apple(adv)
    pkt["name"] = name
    pkt["uuids"] = uuids
    pkt["company_ids"] = company_ids
    pkt["tx_power"] = tx_power
    pkt["apple_subtype"] = apple_sub
    pkt["findmy_state"] = findmy_state
    pkt["fingerprint"] = compute_ble_fingerprint(name, uuids, company_ids, tx_power)
    return pkt


def main():
    if not Path(UBERTOOTH).exists():
        log(f"FATAL: {UBERTOOTH} not found")
        return 2
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    log(f"starting PHY capture as observer={OBSERVER}")

    # stdbuf -oL: ubertooth-btle is a C program whose stdout goes FULLY buffered when it is
    # a pipe rather than a tty, so packets arrive in stalled 4KB chunks (the first live run
    # captured nothing for 80s while the file capture worked perfectly).
    cmd = ["stdbuf", "-oL", UBERTOOTH, "-n"] if Path("/usr/bin/stdbuf").exists() else [UBERTOOTH, "-n"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1)
    # CONSENSUS BUFFER: a radio capture contains bit-errors that decode into plausible but
    # WRONG values — the same HomePod decoded as Apple in 18 packets and as garbage in 2,
    # and each garbage decode minted a phantom identity. There is no CRC to filter on
    # (promiscuous mode marks everything "valid"), so instead every MAC's packets are
    # gathered over a window and the MODAL field values win. Noise cannot outvote signal.
    window = defaultdict(lambda: {"names": Counter(), "companies": Counter(),
                                  "uuids": Counter(), "rssi": [], "channel": None, "n": 0})
    last_flush, total, anon = time.time(), 0, {"count": 0, "rssi": []}
    try:
        for pkt in parse_packets(iter(proc.stdout.readline, '')):
            if _shutdown:
                break
            pkt = enrich(pkt)
            now = time.time()
            if not (pkt["company_ids"] or pkt["uuids"] or pkt.get("name")):
                anon["count"] += 1
                if pkt.get("rssi") is not None:
                    anon["rssi"].append(pkt["rssi"])
            else:
                w = window[pkt["mac"]]
                w["n"] += 1
                if pkt.get("name"):
                    w["names"][pkt["name"]] += 1
                w["companies"][tuple(pkt["company_ids"])] += 1
                if pkt.get("findmy_state"):
                    w.setdefault("findmy", Counter())[pkt["findmy_state"]] += 1
                if pkt.get("apple_subtype"):
                    w.setdefault("apple", Counter())[pkt["apple_subtype"]] += 1
                w["uuids"][tuple(pkt["uuids"])] += 1
                if pkt.get("rssi") is not None:
                    w["rssi"].append(pkt["rssi"])
                if pkt.get("channel") is not None:
                    w["channel"] = pkt["channel"]

            if now - last_flush < FLUSH_SECONDS:
                continue

            rows = []
            for mac, w in window.items():
                # Modal value wins. A single-packet MAC is kept but flagged, since one
                # observation cannot outvote anything and may still be a bit-error.
                name = w["names"].most_common(1)[0][0] if w["names"] else None
                companies = list(w["companies"].most_common(1)[0][0])
                uuids = list(w["uuids"].most_common(1)[0][0])
                agree = w["companies"].most_common(1)[0][1]
                rssi = max(w["rssi"]) if w["rssi"] else None
                rows.append((
                    mac, name, rssi, None, "ble_phy", False,
                    psycopg2.extras.Json({
                        "channel": w["channel"], "source": "ubertooth",
                        "company_ids": [f"{c:#06x}" for c in companies],
                        "service_uuids": uuids,
                        "packets": w["n"], "consensus": agree,
                        "apple_subtype": (w.get("apple") or Counter()).most_common(1)[0][0]
                                          if w.get("apple") else None,
                        "findmy_state": (w.get("findmy") or Counter()).most_common(1)[0][0]
                                         if w.get("findmy") else None,
                        "single_packet": w["n"] == 1,
                        "xfp": compute_cross_observer_fingerprint(uuids, companies)}),
                    compute_ble_fingerprint(name, uuids, companies, None), OBSERVER))

            if rows:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur,
                        "INSERT INTO telemetry.bluetooth (ts, device_mac, device_name, rssi, "
                        "battery_pct, device_type, is_connected, metadata, fingerprint, observer) "
                        "VALUES %s",
                        rows,
                        template="(NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)")
                total += len(rows)
            if anon["count"]:
                med = sorted(anon["rssi"])[len(anon["rssi"]) // 2] if anon["rssi"] else None
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO telemetry.bluetooth (ts, device_mac, device_name, rssi, "
                        "device_type, metadata, observer) VALUES (NOW(), %s, %s, %s, %s, %s, %s)",
                        ("--:--:--:--:--:--", f"anonymous x{anon['count']}", med, "ble_phy_anon",
                         psycopg2.extras.Json({"source": "ubertooth", "rollup": True,
                                               "count": anon["count"], "window_s": FLUSH_SECONDS}),
                         OBSERVER))
            log(f"flushed {len(rows)} consensus devices (total {total}) "
                f"+ {anon['count']} anonymous rolled up")
            window.clear()
            anon = {"count": 0, "rssi": []}
            last_flush = now
    finally:
        proc.terminate()
        conn.close()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
