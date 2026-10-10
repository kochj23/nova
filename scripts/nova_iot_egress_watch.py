#!/usr/bin/env python3
"""nova_iot_egress_watch.py — Vault7-defense IoT egress anomaly watch.

Defensive lesson from Vault 7 (Weeping Angel turned a Samsung TV into a bug;
a compromised appliance phones home): detect when an IoT device — camera,
smart TV, printer, smart bulb/plug, Zigbee bridge, sensor — starts resolving a
destination it NEVER normally contacts. That first odd DNS lookup is the
earliest accessible sign of a compromised appliance.

DNS SOURCE
    BIND (named) query log on the fleet recursive resolver on nova-core (.2),
    read locally from journald. Every client on the LAN uses .2/.86 as its
    resolver (DHCP), so this is the authoritative per-device -> domain feed
    with timestamps. (Pi-hole on .2 was decommissioned ~2026-06-17; its
    poller/stats are dead. dnsmasq on nuk .10 has query logging off. UniFi DPI
    is disabled. BIND querylog is the only live per-client DNS source.)

    named logs queries to journald as:
      <iso> nova-core named[pid]: client @0x.. <ip>#<port> (<qname>): query: ...
    We enable `rndc querylog on` (idempotent, purely observational — it does
    NOT change resolution, blocking, routing, or any answer) and parse the
    lines out of journald.

WHAT IT DOES
    * Per-device BASELINE of normally-resolved registrable domains, learned
      continuously and stored in nova_ops.iot_egress_baseline.
    * Each run: pull the recent window, upsert observations into the baseline,
      and for TRAINED IoT devices flag registrable domains that were never in
      the baseline before this window — the "new destination" signal.
    * Anti-cry-wolf: only anomalies carrying a real suspicion signal
      (suspicious TLD, dynamic-DNS/tunnel host, raw-IP contact) are routed to
      the triage brain (which runs an LLM per call). Plain new CDN churn is
      recorded but not paged. Devices that behave like general hosts (too many
      distinct domains) are demoted from strict alerting automatically.
    * Findings go through nova_alert_triage.triage(...) — it decides
      page/downgrade/suppress. We never page raw.

SAFETY
    Read-only on all DNS/network data. Writes only its own PG tables. Never
    blocks anything, never changes DNS/firewall/network config, never touches
    the appliances.

USAGE
    nova_iot_egress_watch.py            # scheduled: learn + detect + triage
    nova_iot_egress_watch.py learn      # ingest window into baseline only
    nova_iot_egress_watch.py report     # print baseline + would-flag, no triage
    nova_iot_egress_watch.py selftest   # exercise scoring on synthetic domains
"""

from __future__ import annotations
import nova_dsn as _nova_dsn  # noqa: E402

import ipaddress
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

# ── Config (env-overridable) ────────────────────────────────────────────────
PG_DSN = os.environ.get(
    "NOVA_PG_DSN", _nova_dsn.pg_dsn("nova_ops")
)
NAMED_UNIT = os.environ.get("NOVA_NAMED_UNIT", "named")
WINDOW_MIN = int(os.environ.get("NOVA_IOT_WINDOW_MIN", "65"))   # lookback per run
LEARN_DAYS = float(os.environ.get("NOVA_IOT_LEARN_DAYS", "3"))  # per-device warmup
CHURN_MAX = int(os.environ.get("NOVA_IOT_CHURN_MAX", "40"))     # >this distinct = treat as general host
MAX_TRIAGE = int(os.environ.get("NOVA_IOT_MAX_TRIAGE", "12"))   # cap LLM triage calls per run

# ── Domain normalisation ─────────────────────────────────────────────────────
# Minimal public-suffix handling: enough multi-label TLDs for this network.
_MULTI_SUFFIX = {
    "co.uk", "org.uk", "gov.uk", "ac.uk", "com.au", "net.au", "org.au",
    "co.nz", "co.jp", "com.br", "com.cn", "co.in", "com.mx", "co.kr",
    "com.tr", "co.za", "com.sg", "com.hk",
}
# Internal / non-egress zones we ignore entirely.
_INTERNAL_SUFFIX = (
    ".digitalnoise.net", ".nova", ".local", ".lan", ".internal", ".home",
    ".arpa", ".localdomain",
)
# Signals that make a brand-new domain genuinely worth a human's attention.
_SUSPICIOUS_TLDS = {
    "top", "xyz", "tk", "ml", "ga", "cf", "gq", "pw", "su", "cc", "click",
    "country", "work", "monster", "buzz", "cam", "quest", "sbs", "rest",
    "kim", "mom", "lol", "zip", "mov", "cfd", "icu", "wtf",
}
_DDNS_MARKERS = (
    "duckdns.org", "no-ip.", "noip.", "ddns", "dyndns", "hopto.org",
    "zapto.org", "sytes.net", "serveo", "ngrok", "trycloudflare", "loclx",
    "pagekite", "portmap.io", "myftp.", "servebeer", "servegame", "gotdns",
    "freedns", "afraid.org", "chickenkiller", "mooo.com",
)

_QLINE = re.compile(
    r"^(?P<ts>\S+)\s+\S+\s+named\[\d+\]:\s+client\s+@\S+\s+"
    r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3})#\d+(?:/key\s+\S+)?\s+"
    r"\((?P<q>[^)]+)\):\s+query:\s+"
)


def reg_domain(qname: str) -> str | None:
    """Reduce a qname to its registrable domain (eTLD+1) for CDN-churn-tolerant
    comparison. Returns None for internal/non-egress/invalid names."""
    q = qname.rstrip(".").lower()
    if not q or "." not in q:
        return None
    if q.endswith(_INTERNAL_SUFFIX):
        return None
    # A literal IP as the queried name (rare, but a raw-IP contact signal).
    try:
        ipaddress.ip_address(q)
        return None  # handled separately as raw-IP
    except ValueError:
        pass
    labels = q.split(".")
    last2 = ".".join(labels[-2:])
    if last2 in _MULTI_SUFFIX and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last2


def is_raw_ip(qname: str) -> bool:
    q = qname.rstrip(".").lower()
    try:
        ipaddress.ip_address(q)
        return True
    except ValueError:
        return False


def suspicion_signals(reg: str, sample_qname: str) -> list[str]:
    """Return the list of reasons a new domain is worth attention (empty = benign)."""
    sig = []
    if is_raw_ip(sample_qname):
        sig.append("raw-ip-destination")
    tld = reg.rsplit(".", 1)[-1] if reg else ""
    if tld in _SUSPICIOUS_TLDS:
        sig.append(f"suspicious-tld:.{tld}")
    low = (reg or "") + " " + sample_qname.lower()
    for m in _DDNS_MARKERS:
        if m in low:
            sig.append(f"dynamic-dns/tunnel:{m.strip('.')}")
            break
    # Long random-looking single label (DGA-ish): 12+ chars, high digit ratio.
    host_label = reg.split(".")[0] if reg else ""
    if len(host_label) >= 16 and sum(c.isdigit() for c in host_label) >= 5:
        sig.append("dga-like-label")
    return sig


# ── Device inventory / classification ────────────────────────────────────────
# General-purpose hosts: laptops, phones, servers, the fleet Macs/Linux and
# infra. These legitimately talk to everything, so they are NEVER strictly
# baselined/alerted. Everything else is treated as an IoT appliance.
_EXCLUDE_IPS = {
    "192.168.1.1",    # UDM gateway
    "192.168.1.2", "192.168.1.5", "192.168.1.6", "192.168.1.7",
    "192.168.1.10", "192.168.1.11", "192.168.1.69", "192.168.1.9",
    "192.168.1.86", "192.168.1.88", "192.168.1.138", "192.168.1.250",
    "192.168.1.251", "192.168.1.252",
}
_EXCLUDE_NAME = re.compile(
    r"iphone|ipad|android|pixel|macbook|mac-studio|mac-mini|office-m|"
    r"docker-m|nova-core|ubuntu-server|unas|synology|unvr|-laptop|-pc\b",
    re.I,
)


def is_iot(ip: str, name: str) -> bool:
    if ip in _EXCLUDE_IPS:
        return False
    if name and _EXCLUDE_NAME.search(name):
        return False
    return True


def load_names(cur) -> dict[str, str]:
    """ip -> current device name, drift-proof.

    IP->name mappings drift as DHCP reassigns addresses (telemetry.known_devices
    accumulates stale rows — e.g. an old 'mac-mini' can shadow the Bose that now
    holds that IP). So we anchor on the STABLE identity, the MAC: resolve the
    live IP->MAC from the resolver's own neighbor table, then MAC->name from the
    hourly-synced dns_records (authoritative, current). Fall back to the most
    recently *updated* dns_records row per IP, then known_devices.
    """
    # MAC -> current name (dns_records is synced hourly from UniFi).
    cur.execute("SELECT DISTINCT ON (lower(mac)) lower(mac), name FROM dns_records "
                "WHERE mac IS NOT NULL AND mac <> '' ORDER BY lower(mac), updated_at DESC")
    name_by_mac = {m: (n or "") for m, n in cur.fetchall()}
    cur.execute("SELECT DISTINCT ON (lower(client_mac)) lower(client_mac), client_name "
                "FROM telemetry.known_devices WHERE client_mac IS NOT NULL "
                "ORDER BY lower(client_mac), first_seen DESC")
    for m, n in cur.fetchall():
        name_by_mac.setdefault(m, n or "")

    # Live IP -> MAC from the resolver's neighbor table (current occupant).
    ip_mac: dict[str, str] = {}
    try:
        out = subprocess.run(["ip", "neigh"], capture_output=True, text=True, timeout=10).stdout
        for ln in out.splitlines():
            mm = re.match(r"(\d{1,3}(?:\.\d{1,3}){3})\b.*\blladdr\s+([0-9a-fA-F:]{17})", ln)
            if mm:
                ip_mac[mm.group(1)] = mm.group(2).lower()
    except Exception:  # noqa: BLE001
        pass

    # IP -> (mac, name) from most-recently-updated dns_records, as fallback.
    cur.execute("SELECT DISTINCT ON (ip) ip, lower(mac), name FROM dns_records "
                "WHERE ip IS NOT NULL AND ip <> '' ORDER BY ip, updated_at DESC")
    ip_name_fallback: dict[str, str] = {}
    for ip, mac, name in cur.fetchall():
        ip_mac.setdefault(ip, (mac or ""))
        ip_name_fallback[ip] = name or ""

    names: dict[str, str] = {}
    for ip, mac in ip_mac.items():
        nm = name_by_mac.get(mac, "")
        names[ip] = nm or ip_name_fallback.get(ip, "")
    for ip, nm in ip_name_fallback.items():
        names.setdefault(ip, nm)
    return names


# ── DNS source (BIND querylog via journald) ──────────────────────────────────
def ensure_querylog() -> str:
    """Make sure BIND query logging is on. Observational only. Returns state note."""
    try:
        st = subprocess.run(
            ["sudo", "-n", "rndc", "status"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        if "query logging is ON" in st:
            return "querylog already ON"
        subprocess.run(["sudo", "-n", "rndc", "querylog", "on"],
                       capture_output=True, text=True, timeout=15)
        return "querylog enabled"
    except Exception as e:  # noqa: BLE001
        return f"querylog check failed: {e}"


def fetch_queries(since: datetime):
    """Yield (ts, client_ip, qname) tuples from named's journal since `since`."""
    since_s = since.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    proc = subprocess.run(
        ["sudo", "-n", "journalctl", "-u", NAMED_UNIT, "--since", since_s,
         "-o", "short-iso", "--no-pager"],
        capture_output=True, text=True, timeout=120,
    )
    for line in proc.stdout.splitlines():
        if " query: " not in line:
            continue
        m = _QLINE.match(line)
        if not m:
            continue
        try:
            ts = datetime.fromisoformat(m.group("ts"))
        except ValueError:
            ts = None
        yield ts, m.group("ip"), m.group("q")


# ── Schema ───────────────────────────────────────────────────────────────────
DDL = """
CREATE TABLE IF NOT EXISTS iot_egress_baseline (
    device_ip   text NOT NULL,
    device_name text,
    domain      text NOT NULL,          -- registrable domain (eTLD+1)
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    count       bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (device_ip, domain)
);
CREATE INDEX IF NOT EXISTS idx_iot_baseline_dev ON iot_egress_baseline(device_ip);

CREATE TABLE IF NOT EXISTS iot_egress_anomaly (
    device_ip   text NOT NULL,
    device_name text,
    domain      text NOT NULL,          -- registrable domain (eTLD+1)
    sample_qname text,
    signals     text[] NOT NULL DEFAULT '{}',
    severity    text NOT NULL,
    first_flagged timestamptz NOT NULL DEFAULT now(),
    last_flagged  timestamptz NOT NULL DEFAULT now(),
    hits        bigint NOT NULL DEFAULT 1,
    triaged     boolean NOT NULL DEFAULT false,
    decision    text,
    verdict     text,
    PRIMARY KEY (device_ip, domain)
);
"""


# ── Core run ─────────────────────────────────────────────────────────────────
def run(mode: str = "run"):
    note = ensure_querylog() if mode != "selftest" else "skipped"
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(DDL)

    names = load_names(cur)
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=WINDOW_MIN)

    # Aggregate the window: per (device_ip, reg_domain) -> [count, sample_qname]
    window: dict[tuple[str, str], list] = {}
    raw_lines = 0
    for ts, ip, qname in fetch_queries(since):
        raw_lines += 1
        if not is_iot(ip, names.get(ip, "")):
            continue
        reg = reg_domain(qname)
        if reg is None:
            if is_raw_ip(qname):        # raw-IP contact keyed under the literal
                reg = qname.rstrip(".").lower()
            else:
                continue
        k = (ip, reg)
        slot = window.setdefault(k, [0, qname])
        slot[0] += 1

    # Which (device,domain) pairs are already known (pre-upsert) -> the rest are new.
    known: set[tuple[str, str]] = set()
    if window:
        cur.execute(
            "SELECT device_ip, domain FROM iot_egress_baseline WHERE device_ip = ANY(%s)",
            (list({ip for ip, _ in window}),),
        )
        known = {(ip, d) for ip, d in cur.fetchall()}

    # Per-device training age + churn (distinct domains already learned).
    dev_stats: dict[str, dict] = {}
    if window:
        cur.execute(
            "SELECT device_ip, count(*) AS ndom, min(first_seen) AS oldest "
            "FROM iot_egress_baseline WHERE device_ip = ANY(%s) GROUP BY device_ip",
            (list({ip for ip, _ in window}),),
        )
        for ip, ndom, oldest in cur.fetchall():
            trained = oldest is not None and (now - oldest) >= timedelta(days=LEARN_DAYS)
            dev_stats[ip] = {"ndom": ndom, "trained": trained,
                             "churny": ndom > CHURN_MAX}

    # Upsert observations into the baseline (continuous learning).
    if window and mode != "report":
        rows = [(ip, names.get(ip, ""), d, cnt) for (ip, d), (cnt, _) in window.items()]
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO iot_egress_baseline (device_ip, device_name, domain, count) "
            "VALUES %s "
            "ON CONFLICT (device_ip, domain) DO UPDATE SET "
            "last_seen = now(), count = iot_egress_baseline.count + EXCLUDED.count, "
            "device_name = COALESCE(NULLIF(EXCLUDED.device_name,''), iot_egress_baseline.device_name)",
            rows,
        )

    # Detect: new domains for TRAINED, non-churny devices.
    findings = []          # every new-domain observation (recorded)
    to_triage = []         # subset with suspicion signals (routed to brain)
    for (ip, reg), (cnt, sample) in window.items():
        if (ip, reg) in known:
            continue                       # already in baseline -> not new
        st = dev_stats.get(ip, {})
        learning = not st.get("trained", False)   # brand-new device still warming up
        churny = st.get("churny", False)
        sig = suspicion_signals(reg, sample)
        findings.append({
            "ip": ip, "name": names.get(ip, ""), "reg": reg, "sample": sample,
            "count": cnt, "signals": sig, "learning": learning, "churny": churny,
        })
        # Route only if trained, not churny, and carries a real signal.
        if sig and not learning and not churny:
            sev = "critical" if any(
                s.startswith(("raw-ip", "suspicious-tld", "dynamic-dns")) for s in sig
            ) else "warning"
            to_triage.append({**findings[-1], "severity": sev})

    # Record anomalies (all new domains for trained/non-churny devices).
    recordable = [f for f in findings if not f["learning"] and not f["churny"]]
    if recordable and mode != "report":
        rows = [
            (f["ip"], f["name"], f["reg"], f["sample"], f["signals"],
             ("critical" if any(s.startswith(("raw-ip", "suspicious-tld", "dynamic-dns"))
                                for s in f["signals"]) else
              ("warning" if f["signals"] else "info")))
            for f in recordable
        ]
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO iot_egress_anomaly "
            "(device_ip, device_name, domain, sample_qname, signals, severity) VALUES %s "
            "ON CONFLICT (device_ip, domain) DO UPDATE SET "
            "last_flagged = now(), hits = iot_egress_anomaly.hits + 1",
            rows, template="(%s,%s,%s,%s,%s::text[],%s)",
        )

    # Triage the suspicious subset (LLM per call — capped).
    triaged_out = []
    if mode == "run" and to_triage:
        to_triage.sort(key=lambda f: (f["severity"] != "critical", f["reg"]))
        try:
            from nova_alert_triage import triage
        except Exception as e:  # noqa: BLE001
            triage = None
            triaged_out.append(f"triage import failed: {e}")
        if triage:
            for f in to_triage[:MAX_TRIAGE]:
                title = f"IoT egress anomaly: {f['name'] or f['ip']} -> {f['reg']}"
                body = (f"Device {f['name'] or ''} ({f['ip']}), classed IoT, resolved a "
                        f"destination not in its learned baseline: {f['sample']} "
                        f"(reg-domain {f['reg']}, {f['count']}x this window). "
                        f"Signals: {', '.join(f['signals'])}. "
                        f"Baseline size {dev_stats.get(f['ip'],{}).get('ndom','?')} domains. "
                        f"Earliest sign of a compromised appliance phoning home (Vault7/Weeping Angel pattern).")
                try:
                    d = triage(title, body, level=f["severity"], category="security",
                               source="iot_egress",
                               dedup_key=f"iot_egress:{f['ip']}:{f['reg']}")
                    dec = (d or {}).get("decision"); ver = (d or {}).get("verdict")
                    cur.execute(
                        "UPDATE iot_egress_anomaly SET triaged=true, decision=%s, verdict=%s "
                        "WHERE device_ip=%s AND domain=%s",
                        (dec, ver, f["ip"], f["reg"]))
                    triaged_out.append(f"{f['ip']} {f['reg']} -> {dec}/{ver}")
                except Exception as e:  # noqa: BLE001
                    triaged_out.append(f"{f['ip']} {f['reg']} triage error: {e}")

    # ── Summary ──────────────────────────────────────────────────────────────
    n_dev = len({ip for ip, _ in window})
    n_new = len(findings)
    n_new_trained = len(recordable)
    print(f"[iot_egress] {note}; window={WINDOW_MIN}m raw_query_lines={raw_lines} "
          f"iot_devices_active={n_dev} new_domains={n_new} "
          f"(trained/recordable={n_new_trained}) routed_to_triage={len(to_triage)}")
    if mode == "report":
        cur.execute("SELECT count(DISTINCT device_ip), count(*) FROM iot_egress_baseline")
        d, r = cur.fetchone()
        print(f"[iot_egress] baseline: {d} devices, {r} device-domain pairs")
        for f in sorted(findings, key=lambda x: (not x["signals"], x["ip"]))[:40]:
            tag = ("LEARNING" if f["learning"] else ("CHURNY" if f["churny"] else "NEW"))
            sg = f" !! {','.join(f['signals'])}" if f["signals"] else ""
            print(f"  {tag:8} {f['ip']:15} {f['name'][:20]:20} {f['reg']:32} x{f['count']}{sg}")
    for t in triaged_out:
        print(f"[iot_egress] triage: {t}")
    conn.close()


def selftest():
    """Prove the classification/scoring pipeline without touching PG/DNS."""
    cases = [
        ("a1b2.cloudfront.net", "cloudfront.net"),
        ("device-api.arlo.com", "arlo.com"),
        ("evil-c2.top", "evil-c2.top"),
        ("myhome.duckdns.org", "duckdns.org"),
        ("203.0.113.66", "203.0.113.66"),
        ("x8f3kd92jf01ab77z.cn", "x8f3kd92jf01ab77z.cn"),
        ("nova-core.digitalnoise.net", None),
    ]
    print("selftest — reg_domain + suspicion scoring:")
    for q, _ in cases:
        reg = reg_domain(q)
        if reg is None and is_raw_ip(q):
            reg = q
        if reg is None:
            print(f"  {q:34} -> (internal/ignored)")
            continue
        sig = suspicion_signals(reg, q)
        verdict = "PAGE-path" if sig else "record-only (benign/CDN)"
        print(f"  {q:34} -> reg={reg:24} {verdict} {sig}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "run"
    if arg == "selftest":
        selftest()
    else:
        run(mode=arg)
