#!/usr/bin/env python3
"""
nova_dns_sync.py — Nova-DNS: pull UniFi clients, assign STICKY intelligent names,
keep the authoritative map in PG, and push A records into the real BIND9 cluster
(primary nova-core .138, secondary nova-core2 .86) for the digitalnoise.net zone
via authenticated nsupdate.

So we can stop talking in IP addresses. UniFi is the *feed*; PG (dns_records) is the
authoritative, sticky, overridable source of truth — a device never silently renames
itself, and you can lock/override any name. Static SERVICE aliases (pg-primary, grafana,
…) are re-pointable for failover. "ollama"/"cluster" are NOT touched here — those are
F5-style health-driven records owned exclusively by nova_lb.py, updated every probe cycle.

Naming priority: user alias (UniFi `name`) -> reported hostname -> vendor+type ->
mac suffix. Run with --dry-run to preview names without touching PG or BIND.
"""
import argparse
import json
import re
import ssl
import subprocess
import sys
import urllib.request

import psycopg2

DOMAIN = "digitalnoise.net"
DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"
UNIFI = "https://192.168.1.1/proxy/network/api/s/default"
BIND_PRIMARY = "192.168.1.2"   # nova-core wired; was .138 (same box's WIFI ip — fragile)
TSIG_KEY_NAME = "nova-dns-key"

# Static service aliases — re-point these on failover (one record, not a sed sweep).
# NOTE: "ollama"/"cluster" deliberately excluded — owned by nova_lb.py (health-driven).
# Corrected 2026-07-24 (twice): first fix pointed pg-primary at .6 per the docs — but the
# docs were stale. Verified live (inet_server_addr + pg_postmaster_start_time): the REAL
# primary since the 2026-07-17 cold start is the pg17 docker container on nova-core .2;
# .6:5432 is only a pgbouncer shim forwarding there (kept old localhost DSNs working),
# and the only live replica is .10. The container named "pg17-replica" is the primary.
SERVICE_ALIASES = {
    "pg-primary":       "192.168.1.10",  # EMERGENCY FAILOVER 2026-09-17 ~02:10: nova-core (.2) died hard (host+BIND down), promoted nova-core5 (.10, port 5432 NOT 5434). .2 is FENCED — rebuild via pg_basebackup from .10 before any failback.
    "memory-server":    "192.168.1.6",   # :18790 — TEMP 2026-09-17: nova-core down; local instance running on .6 (socat forward disabled). Revert to .2 when nova-core rebuilt.
    "grafana":          "192.168.1.2",    # nova-core wired (was its wifi ip)
    "inference-router": "192.168.1.2",    # :37475 fleet LLM proxy
    "nova-gw":          "192.168.1.2",    # Gateway V2 :18792 (migrated off .6 2026-07-13)
    "unas":             "192.168.1.69",
    "nas":              "192.168.1.11",
    # Added 2026-07-28. Every one of these was being referenced by raw IP somewhere, which is
    # why a single Mac mini moving cost edits in five files plus two remote copies today.
    "plex":             "192.168.1.2",    # :32400 — moved off .86 (queue #1348)
    "mlx":              "192.168.1.6",    # :5050 nginx MLX load balancer
    "nova-core6":       "192.168.1.252",  # joined 2026-07-27, never had a record
    # mac-mini resolved to .92 — an address that has never existed. It is a STATIC .251 now,
    # deliberately outside the DHCP pool (.20-.200) so it stops wandering. Listing it here
    # makes this file authoritative over the sticky UniFi-derived client name.
    "mac-mini":         "192.168.1.251",
    "itunes":           "192.168.1.7",    # tv-movies mini; UniFi calls it "Office-M2"
}

# Real PUBLIC subdomains (Cloudflare Tunnel + GitHub Pages) that live under this same
# domain. Since BIND is now authoritative for the whole digitalnoise.net zone, these
# would otherwise NXDOMAIN locally instead of reaching the real internet-facing service
# — mirror their live public answer every sync so they never silently break or drift.
# Source of truth: ~/.cloudflared/config.yml ingress rules + the journal's GitHub Pages CNAME.
PUBLIC_MIRRORS = ["", "www", "chat", "gauges", "analytics"]   # "" = bare apex

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


def _secret(name):
    # ponytail: Keychain on macOS, fleet secret store (systemd-creds) on Linux — same key, two homes
    if sys.platform == "darwin":
        return subprocess.run(["security", "find-generic-password", "-a", "nova", "-s", name, "-w"],
                              capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    import nova_secrets
    return nova_secrets.get_secret(name)


def api_key():
    return _secret("nova-unifi-api-key")


def get_clients(key):
    req = urllib.request.Request(f"{UNIFI}/stat/sta", headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=15, context=_SSL) as r:
        return json.loads(r.read()).get("data", [])


def slug(s):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").strip().lower()).strip("-")
    return re.sub(r"-+", "-", s)


def derive_name(c):
    """Best DNS-safe name for a client, in priority order."""
    for field in ("name", "device_name", "hostname"):
        v = slug(c.get(field))
        if v:
            return v
    vendor = slug(c.get("oui") or c.get("dev_vendor"))
    mac = (c.get("mac") or "").replace(":", "")
    if vendor:
        return f"{vendor}-{mac[-4:]}"
    return f"dev-{mac[-6:]}" if mac else None


def _ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dns_records (
                mac text PRIMARY KEY,
                name text NOT NULL,
                ip text,
                source text DEFAULT 'unifi',
                locked boolean DEFAULT false,
                first_seen timestamptz DEFAULT now(),
                updated_at timestamptz DEFAULT now()
            )""")


def build(conn, clients):
    """Return [(fqdn, ip)] and update the sticky PG map (conn=None -> dry-run)."""
    if conn:
        _ensure_schema(conn)
    used_names = {}        # name -> mac, for collision de-dup
    out = []
    for c in clients:
        mac = c.get("mac")
        ip = c.get("ip") or c.get("fixed_ip") or c.get("last_ip")
        if not mac or not ip:
            continue
        existing = None
        if conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name, locked FROM dns_records WHERE mac=%s", (mac,))
                existing = cur.fetchone()
        if existing:                      # sticky: keep the established name
            name = existing[0]
        else:
            name = derive_name(c)
            if not name:
                continue
            # de-dup collisions by appending the mac tail
            if name in used_names and used_names[name] != mac:
                name = f"{name}-{mac.replace(':','')[-4:]}"
        used_names[name] = mac
        if conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dns_records (mac,name,ip,source) VALUES (%s,%s,%s,'unifi') "
                    "ON CONFLICT (mac) DO UPDATE SET ip=EXCLUDED.ip, updated_at=now()",
                    (mac, name, ip))
        out.append((f"{name}.{DOMAIN}", ip))
    # static service aliases (always emitted, re-pointable)
    for alias, ip in SERVICE_ALIASES.items():
        out.append((f"{alias}.{DOMAIN}", ip))
    return out


def tsig_secret():
    return _secret("nova-bind-tsig-key")


# Failover-critical aliases get a SHORT TTL so clients drop the old primary within a
# minute of a re-point (queue #2656: after the 2026-09-17 failback, .6 kept the stale
# .10 answer for the full 300s and needed a manual dscacheutil flush). Everything else
# keeps the 300s default.
FAILOVER_ALIASES = {"pg-primary", "memory-server"}
FAILOVER_TTL = 60


def push_bind(entries, ttl=300):
    """Push all entries into the BIND primary via authenticated nsupdate. Idempotent —
    each record is deleted then re-added so removed/renamed devices don't leave stale A's."""
    secret = tsig_secret()
    script_lines = [f"server {BIND_PRIMARY}", f"zone {DOMAIN}."]
    for fqdn, ip in entries:
        rec_ttl = FAILOVER_TTL if fqdn.split(".")[0] in FAILOVER_ALIASES else ttl
        script_lines.append(f"update delete {fqdn}. A")
        script_lines.append(f"update add {fqdn}. {rec_ttl} A {ip}")
    script_lines.append("send")
    script = "\n".join(script_lines) + "\n"

    r = subprocess.run(
        ["nsupdate", "-y", f"hmac-sha256:{TSIG_KEY_NAME}:{secret}"],
        input=script, capture_output=True, text=True, timeout=30
    )
    if r.returncode != 0:
        print(f"[nova-dns] nsupdate failed: {r.stderr.strip()[:300]}", flush=True)
        return False
    return True


def resolve_public(hostname: str, resolver: str = "8.8.8.8") -> list[str]:
    """A records for hostname via a REAL external resolver — never our own BIND,
    that would be circular (we're authoritative for this zone, we'd just answer
    from our own possibly-stale mirror instead of checking the real internet)."""
    fqdn = f"{hostname}.{DOMAIN}" if hostname else DOMAIN
    try:
        r = subprocess.run(["dig", f"@{resolver}", fqdn, "A", "+short"],
                           capture_output=True, text=True, timeout=10)
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip() and not ln.endswith(".")]
    except Exception:
        return []


def push_public_mirrors(ttl=300) -> int:
    """Re-resolve the real public-facing subdomains and keep BIND's mirror in sync.
    Cloudflare's edge IPs can rotate — this is what keeps us from silently drifting
    stale after the one-time manual fix. Returns count of names successfully synced."""
    secret = tsig_secret()
    script_lines = [f"server {BIND_PRIMARY}", f"zone {DOMAIN}."]
    synced = 0
    for host in PUBLIC_MIRRORS:
        ips = resolve_public(host)
        if not ips:
            print(f"[nova-dns] public mirror '{host or DOMAIN}' — resolution failed, leaving existing record alone", flush=True)
            continue
        name = f"{host}.{DOMAIN}" if host else DOMAIN
        script_lines.append(f"update delete {name}. A")
        for ip in ips:
            script_lines.append(f"update add {name}. {ttl} A {ip}")
        synced += 1
    if synced == 0:
        return 0
    script_lines.append("send")
    r = subprocess.run(
        ["nsupdate", "-y", f"hmac-sha256:{TSIG_KEY_NAME}:{secret}"],
        input="\n".join(script_lines) + "\n", capture_output=True, text=True, timeout=30
    )
    if r.returncode != 0:
        print(f"[nova-dns] public mirror nsupdate failed: {r.stderr.strip()[:300]}", flush=True)
        return 0
    return synced


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="preview names, no PG/BIND writes")
    args = ap.parse_args()
    clients = get_clients(api_key())
    conn = None if args.dry_run else psycopg2.connect(DSN)
    if conn:
        conn.autocommit = True
    entries = build(conn, clients)
    if args.dry_run:
        for fqdn, ip in sorted(entries):
            print(f"  {ip:<15} {fqdn}")
        print(f"\n  {len(entries)} records ({len(clients)} clients + {len(SERVICE_ALIASES)} service aliases)")
    else:
        ok = push_bind(entries)
        status = "OK (primary, secondary picks it up via AXFR/NOTIFY)" if ok else "FAILED"
        print(f"[nova-dns] {len(entries)} records ({len(clients)} clients) pushed to BIND primary {BIND_PRIMARY}: {status}")
        n_public = push_public_mirrors()
        print(f"[nova-dns] public mirrors re-synced: {n_public}/{len(PUBLIC_MIRRORS)}")
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
