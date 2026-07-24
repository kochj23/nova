#!/opt/homebrew/bin/python3
"""
nova_cert_monitor.py — TLS certificate expiry collector for Nova's
observability stack (PG -> Grafana).

For a fixed list of HTTPS endpoints (local infra + public Nova services), pulls
the leaf certificate and records its subject + expiry. Writes timestamped rows
into telemetry.cert_expiry (partitioned by month on ts, matching telemetry.*).

Cert retrieval order:
  1. Python `ssl` (preferred — no shell, gets notAfter + subject CN).
  2. `openssl s_client | openssl x509 -enddate -subject` fallback if needed.

Self-signed certs are still readable (we don't verify the chain — we only want
expiry). Unreachable hosts record a row with NULLs + a `note` so the gap is
visible in Grafana rather than crashing the run.

Resilient: each endpoint is collected inside its own try/except.

  python3 nova_cert_monitor.py            # collect all, write PG
  python3 nova_cert_monitor.py --dry-run  # collect + print, no PG write

Written by Jordan Koch.
"""

import argparse
import socket
import ssl
import subprocess
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

DB_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
NOW = datetime.now(timezone.utc)

# (label, host, port)
ENDPOINTS = [
    ("wazuh",          "192.168.1.2",   443),
    ("unas",           "192.168.1.69",  443),
    ("unifi-protect",  "192.168.1.9",   443),
    ("udm",            "192.168.1.1",   443),
    ("synology",       "192.168.1.11",  5001),
    ("nova",           "nova.digitalnoise.net",   443),
    ("chat",           "chat.digitalnoise.net",   443),
    ("gauges",         "gauges.digitalnoise.net", 443),
]

COLUMNS = ["ts", "endpoint", "host", "port", "subject", "not_after",
           "days_until_expiry", "note"]


def log(msg):
    print(f"[cert_monitor {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _row(endpoint, host, port, **kw):
    r = {c: None for c in COLUMNS}
    r["ts"] = NOW
    r["endpoint"] = endpoint
    r["host"] = host
    r["port"] = port
    for k, v in kw.items():
        if k in r:
            r[k] = v
    return r


def _parse_notafter_openssl(s):
    # e.g. "Jun 20 12:00:00 2026 GMT"
    return datetime.strptime(s.strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)


def fetch_via_ssl(host, port):
    """Return (subject_str, not_after_dt). Does NOT verify the chain."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ss:
            cert = ss.getpeercert()
    # With CERT_NONE getpeercert() may be empty; fall back to binary form.
    if cert and cert.get("notAfter"):
        not_after = datetime.strptime(
            cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        subj = "; ".join("=".join(x) for rdn in cert.get("subject", ()) for x in rdn)
        return subj or None, not_after
    return None, None


def fetch_via_openssl(host, port):
    """Fallback: shell out to openssl. Returns (subject, not_after_dt)."""
    proc = subprocess.run(
        ["openssl", "s_client", "-connect", f"{host}:{port}",
         "-servername", host],
        input="", capture_output=True, text=True, timeout=20,
    )
    raw = proc.stdout
    # Extract the PEM cert and feed to x509.
    x = subprocess.run(
        ["openssl", "x509", "-noout", "-enddate", "-subject"],
        input=raw, capture_output=True, text=True, timeout=15,
    )
    subject, not_after = None, None
    for line in x.stdout.splitlines():
        if line.startswith("notAfter="):
            not_after = _parse_notafter_openssl(line.split("=", 1)[1])
        elif line.startswith("subject="):
            subject = line.split("=", 1)[1].strip()
    return subject, not_after


def collect_endpoint(label, host, port):
    try:
        subject, not_after = fetch_via_ssl(host, port)
    except Exception as e1:
        log(f"{label}: ssl path failed ({e1}); trying openssl")
        try:
            subject, not_after = fetch_via_openssl(host, port)
        except Exception as e2:
            log(f"{label}: unreachable ({e2})")
            return _row(label, host, port, note=f"unreachable: {e2}")

    if not not_after:
        try:
            subject, not_after = fetch_via_openssl(host, port)
        except Exception as e:
            return _row(label, host, port, subject=subject,
                        note=f"no notAfter via ssl, openssl failed: {e}")

    if not not_after:
        return _row(label, host, port, subject=subject, note="no expiry parsed")

    days = (not_after - NOW).total_seconds() / 86400.0
    return _row(label, host, port, subject=subject, not_after=not_after,
                days_until_expiry=round(days, 2))


# ── PG ────────────────────────────────────────────────────────────────────────

DDL = """
CREATE SCHEMA IF NOT EXISTS telemetry;
CREATE TABLE IF NOT EXISTS telemetry.cert_expiry (
    ts                 timestamptz NOT NULL,
    endpoint           text        NOT NULL,
    host               text,
    port               integer,
    subject            text,
    not_after          timestamptz,
    days_until_expiry  real,
    note               text
) PARTITION BY RANGE (ts);
CREATE INDEX IF NOT EXISTS idx_cert_expiry_ts ON telemetry.cert_expiry (ts);
CREATE INDEX IF NOT EXISTS idx_cert_expiry_ep_ts ON telemetry.cert_expiry (endpoint, ts);
"""


def ensure_partition(conn, ts):
    try:
        first = ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        nxt = first.replace(year=first.year + 1, month=1) if first.month == 12 \
            else first.replace(month=first.month + 1)
        suffix = first.strftime("%Y%m")
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS telemetry.cert_expiry_{suffix} "
                f"PARTITION OF telemetry.cert_expiry "
                f"FOR VALUES FROM (%s) TO (%s)", (first, nxt))
    except Exception as e:
        log(f"partition ensure: {e}")


def write_rows(rows):
    if not rows:
        return 0
    try:
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = True
    except Exception as e:
        log(f"DB connect failed: {e}")
        return 0
    inserted = 0
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
        ensure_partition(conn, NOW)
        cols = ", ".join(COLUMNS)
        ph = ", ".join(["%s"] * len(COLUMNS))
        sql = f"INSERT INTO telemetry.cert_expiry ({cols}) VALUES ({ph})"
        values = [[r[c] for c in COLUMNS] for r in rows]
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, values)
        inserted = len(values)
    except Exception as e:
        log(f"insert failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return inserted


def main():
    ap = argparse.ArgumentParser(description="Nova TLS cert expiry collector")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = []
    for label, host, port in ENDPOINTS:
        r = collect_endpoint(label, host, port)
        rows.append(r)
        if r["days_until_expiry"] is not None:
            log(f"{label} ({host}:{port}): {r['days_until_expiry']} days "
                f"(expires {r['not_after']})")
        else:
            log(f"{label} ({host}:{port}): no expiry — {r['note']}")

    if args.dry_run:
        log(f"DRY RUN — would insert {len(rows)} row(s)")
        return

    n = write_rows(rows)
    log(f"inserted {n} row(s)")


if __name__ == "__main__":
    main()
