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
RE_NAME = re.compile(r"Type (?:08|09) \(.*?Name\)\s*\n\s*(.+)")
RE_COMPANY = re.compile(r"Type ff \(Manufacturer.*?\)\s*\n\s*Company:\s*(.+)", re.S)


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
    """Pull the stable advertising fields the fingerprint needs out of the decode block."""
    blob = "\n".join(pkt.get("raw", []))
    name = None
    m = re.search(r"Type 0[89] \([^)]*\)\s*\n\s*([^\n]+)", blob)
    if m:
        cand = m.group(1).strip()
        if cand and not cand.startswith("Type "):
            name = cand
    company_ids = []
    for cm in re.finditer(r"Company:\s*([^\n(]+)\(?(0x[0-9a-fA-F]{4})?", blob):
        hexid = cm.group(2)
        if hexid:
            company_ids.append(int(hexid, 16))
    if not company_ids:
        # Fall back to the raw manufacturer-data header: "ff <lo> <hi>" is little-endian.
        fm = re.search(r"\bff\s+([0-9a-f]{2})\s+([0-9a-f]{2})\b", blob, re.I)
        if fm:
            company_ids.append(int(fm.group(2) + fm.group(1), 16))
    # KNOWN LIMITATION (measured 2026-07-27): do NOT scrape UUIDs with a loose regex. The
    # first attempt matched any 4-hex token whenever "UUID" appeared later in the block,
    # producing noise — and because the host stack feeds compute_ble_fingerprint() full
    # 128-bit UUID strings while this fed it 4-char fragments, the two observers computed
    # DIFFERENT identities for the same device: cross-observer correlation measured 1 shared
    # fingerprint. Passing [] is honest; correlating properly needs real AdvData TLV decoding
    # (parse the length/type/value chain and expand 16-bit UUIDs to full form), which is
    # queued rather than faked here.
    uuids = []
    pkt["name"] = name
    pkt["company_ids"] = sorted(set(company_ids))
    pkt["uuids"] = sorted(set(u.lower() for u in uuids))
    pkt["fingerprint"] = compute_ble_fingerprint(name, pkt["uuids"], pkt["company_ids"], None)
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
                                      "company_ids": pkt["company_ids"], "source": "ubertooth"}),
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
