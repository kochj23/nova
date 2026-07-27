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
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import psycopg2
import psycopg2.extras

from nova_ble_monitor import compute_ble_fingerprint   # identity MUST match exactly

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
    pkt["name"] = name
    pkt["uuids"] = uuids
    pkt["company_ids"] = company_ids
    pkt["tx_power"] = tx_power
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
    pending, last_seen, last_flush, total = [], {}, time.time(), 0
    anon = {"count": 0, "rssi": []}   # unfingerprintable one-shots, rolled up per flush
    try:
        for pkt in parse_packets(iter(proc.stdout.readline, '')):
            if _shutdown:
                break
            pkt = enrich(pkt)
            now = time.time()
            # Dedupe on IDENTITY, not address. A rotating random address is seen once and
            # never again, so keying on the MAC dedupes nothing: the first live run produced
            # 2,031 rows with 2,031 distinct addresses in 80s (~2.2M rows/day from one radio,
            # which would double the entire BLE dataset twice a day).
            fp = pkt.get("fingerprint")
            if not fp:
                # No stable advertising fields = an anonymous one-shot we cannot identify
                # even in principle. Counted in an aggregate below rather than stored
                # individually; storing millions of never-repeated addresses buys nothing.
                anon["count"] += 1
                if pkt.get("rssi") is not None:
                    anon["rssi"].append(pkt["rssi"])
                continue
            if now - last_seen.get(fp, 0) < DEDUPE_SECONDS:
                continue
            last_seen[fp] = now
            pending.append((
                pkt["mac"], pkt.get("name"), pkt.get("rssi"), None, "ble_phy", False,
                psycopg2.extras.Json({"channel": pkt.get("channel"), "freq": pkt.get("freq"),
                                      "addr_type": pkt.get("addr_type"),
                                      # Store company IDs as hex STRINGS and include
                                      # service_uuids — the host-stack observer writes
                                      # {"company_ids": ["0x004c"], "service_uuids": [...]}, and
                                      # any recompute-from-metadata hashes the stored text. Writing
                                      # ints here made the same vendor hash as "6216" on one
                                      # observer and "0x1848" on the other.
                                      "company_ids": [f"{c:#06x}" for c in pkt["company_ids"]],
                                      "service_uuids": pkt.get("uuids", []),
                                      "tx_power": pkt.get("tx_power"),
                                      "source": "ubertooth"}),
                pkt.get("fingerprint"), OBSERVER))

            if now - last_flush >= FLUSH_SECONDS and pending:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur,
                        "INSERT INTO telemetry.bluetooth (ts, device_mac, device_name, rssi, "
                        "battery_pct, device_type, is_connected, metadata, fingerprint, observer) "
                        "VALUES %s",
                        pending,
                        template="(NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)")
                total += len(pending)
                if anon["count"]:
                    med = sorted(anon["rssi"])[len(anon["rssi"]) // 2] if anon["rssi"] else None
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO telemetry.bluetooth (ts, device_mac, device_name, rssi, "
                            "device_type, metadata, observer) VALUES (NOW(), %s, %s, %s, %s, %s, %s)",
                            ("--:--:--:--:--:--", f"anonymous x{anon['count']}", med,
                             "ble_phy_anon",
                             psycopg2.extras.Json({"source": "ubertooth", "rollup": True,
                                                   "count": anon["count"],
                                                   "window_s": FLUSH_SECONDS}),
                             OBSERVER))
                log(f"flushed {len(pending)} identified (total {total}, {len(last_seen)} fingerprints) "
                    f"+ {anon['count']} anonymous rolled up")
                pending, last_flush = [], now
                anon = {"count": 0, "rssi": []}
                # keep the dedupe map from growing without bound on a busy street
                if len(last_seen) > 20000:
                    cutoff = now - DEDUPE_SECONDS
                    last_seen = {k: v for k, v in last_seen.items() if v > cutoff}
    finally:
        proc.terminate()
        conn.close()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
