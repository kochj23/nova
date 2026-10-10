#!/usr/bin/env python3
"""
nova_cert_watch.py — alarm on TLS certs (and cert-backed credentials) about to
expire, before they take a service down.

The ops postmortems that motivate this: "DSM auth failing — expired cert? a
rotated credential nobody updated in Keychain?" A cert quietly reaching its
not_after is a self-inflicted outage that is 100% predictable days in advance.
nova_cert_monitor.py already COLLECTS leaf-cert expiry into telemetry.cert_expiry
every cycle; nothing was reading that table to actually WARN. This does.

For every endpoint, it reads the most recent cert_expiry sample and, if the cert
expires within CERT_WARN_DAYS, emits a notify (category 'cert-expiry', deduped
per endpoint so a given cert pages at most once per notifier window):

  - expiring within 14d  -> warning  "cert for <ep> expires in 9.4 days"
  - already expired, or  -> critical "cert for <ep> EXPIRES in 2 days / EXPIRED"
    expiring within 3d

Read-only + alarm only. Auto-renewal is out of scope (no obvious renew hook
exists here); the job is to make sure a looming expiry is impossible to miss.

CLI:
  nova_cert_watch.py --scan       # read cert_expiry + emit alerts
  nova_cert_watch.py --dry-run    # read + print, emit nothing
  nova_cert_watch.py --report     # print soonest-expiring certs (all)
  nova_cert_watch.py --selftest   # synthetic rows, classification only, no notify

Written by Jordan Koch.
"""
import argparse
import datetime
import math
import sys

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")

CERT_WARN_DAYS = 14      # warn when a cert expires within this many days
CERT_CRIT_DAYS = 3       # critical when within this many days (or already expired)
STALE_SAMPLE_DAYS = 30   # ignore endpoints not sampled within this window


def _connect():
    try:
        import psycopg2
        return psycopg2.connect(DSN, connect_timeout=5)
    except Exception as e:
        print(f"cert_watch: DB connect failed: {e}", file=sys.stderr)
        return None


def _notify(*a, **kw):
    try:
        from nova_notify import notify
        return notify(*a, **kw)
    except Exception:
        return False


# ── Pure classification (no DB, so it is trivially testable) ────────────────

def classify(rows,
             warn_days: int = CERT_WARN_DAYS,
             crit_days: int = CERT_CRIT_DAYS) -> list[dict]:
    """Given latest-per-endpoint cert rows, return alertable ones with a level.

    Each input row is a dict with: endpoint, host, port, subject, not_after,
    days_until_expiry (may be None for an unreachable/unknown endpoint).

    Returns a list of dicts = the input row plus 'level' ('warning'|'critical')
    and 'expired' (bool), for every cert with days_until_expiry <= warn_days.
    Rows with a None days value are skipped (nothing to assert). Sorted soonest
    first. Never raises on a malformed row.
    """
    out = []
    for r in rows:
        try:
            days = r.get("days_until_expiry")
            if days is None:
                continue
            days = float(days)
            if not math.isfinite(days):
                continue
            if days > warn_days:
                continue
            expired = days <= 0
            level = "critical" if (expired or days <= crit_days) else "warning"
            item = dict(r)
            item["days_until_expiry"] = days
            item["level"] = level
            item["expired"] = expired
            out.append(item)
        except Exception:
            continue
    out.sort(key=lambda x: x.get("days_until_expiry", 1e9))
    return out


def _title(row: dict) -> str:
    ep = row.get("endpoint") or row.get("host") or "cert"
    days = row.get("days_until_expiry")
    if row.get("expired"):
        return f"TLS cert for {ep} has EXPIRED"
    if row.get("level") == "critical":
        return f"TLS cert for {ep} expires in {days:.1f} days — CRITICAL"
    return f"TLS cert for {ep} expires in {days:.1f} days"


def _body(row: dict) -> str:
    ep = row.get("endpoint")
    host = row.get("host")
    port = row.get("port")
    subj = row.get("subject")
    na = row.get("not_after")
    where = f"{host}:{port}" if host and port else (host or ep)
    lead = ("already EXPIRED" if row.get("expired")
            else f"{row['days_until_expiry']:.1f} days left")
    return (f"Endpoint '{ep}' ({where}) — {lead}. "
            f"not_after={na}. subject={subj}. "
            f"If a service on this host is failing auth, a stale cert or a "
            f"rotated credential not updated in Keychain is the likely cause.")


# ── DB read (read-only) ─────────────────────────────────────────────────────

def latest_per_endpoint(conn, stale_days: int = STALE_SAMPLE_DAYS) -> list[dict]:
    """Most recent cert_expiry sample per endpoint, within the freshness window."""
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (endpoint)
                       endpoint, host, port, subject, not_after,
                       days_until_expiry, ts
                FROM telemetry.cert_expiry
                WHERE ts > now() - make_interval(days => %s)
                ORDER BY endpoint, ts DESC
                """,
                (stale_days,),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"cert_watch: latest_per_endpoint failed: {e}", file=sys.stderr)
        return []


def scan(conn, emit: bool = True) -> list[dict]:
    """Read latest certs, classify, optionally emit one dedup'd alert per cert.
    Returns the alertable rows. Read-only against the DB either way."""
    rows = latest_per_endpoint(conn)
    alerts = classify(rows)
    if not emit:
        return alerts
    for row in alerts:
        ep = row.get("endpoint") or row.get("host") or "cert"
        _notify(
            _title(row),
            body=_body(row),
            level=row["level"],
            category="cert-expiry",
            source="nova_cert_watch.py",
            dedup_key=f"cert-expiry-{ep}",
            meta={"endpoint": ep, "host": row.get("host"),
                  "days_until_expiry": row.get("days_until_expiry"),
                  "not_after": str(row.get("not_after")),
                  "expired": row.get("expired")},
        )
    return alerts


def report(conn, limit: int = 20) -> list[dict]:
    rows = latest_per_endpoint(conn)
    rows = [r for r in rows if r.get("days_until_expiry") is not None]
    rows.sort(key=lambda x: float(x["days_until_expiry"]))
    return rows[:limit]


# ── Self-test (pure classification; emits NOTHING, touches no DB) ───────────

def _selftest() -> bool:
    now = datetime.datetime.now(datetime.timezone.utc)
    sample = [
        {"endpoint": "far",      "host": "a", "port": 443, "subject": "CN=a",
         "not_after": now + datetime.timedelta(days=90),  "days_until_expiry": 90.0},
        {"endpoint": "warn",     "host": "b", "port": 443, "subject": "CN=b",
         "not_after": now + datetime.timedelta(days=9),   "days_until_expiry": 9.4},
        {"endpoint": "boundary14","host": "c","port": 443, "subject": "CN=c",
         "not_after": now + datetime.timedelta(days=14),  "days_until_expiry": 14.0},
        {"endpoint": "crit",     "host": "d", "port": 443, "subject": "CN=d",
         "not_after": now + datetime.timedelta(days=2),   "days_until_expiry": 2.0},
        {"endpoint": "expired",  "host": "e", "port": 443, "subject": "CN=e",
         "not_after": now - datetime.timedelta(days=1),   "days_until_expiry": -1.0},
        {"endpoint": "unknown",  "host": "f", "port": 443, "subject": None,
         "not_after": None,                               "days_until_expiry": None},
    ]
    alerts = classify(sample)
    by = {a["endpoint"]: a for a in alerts}
    ok = True
    checks = [
        ("far absent (>14d)",            "far" not in by),
        ("warn present + warning",       by.get("warn", {}).get("level") == "warning"),
        ("boundary 14d present+warning", by.get("boundary14", {}).get("level") == "warning"),
        ("crit present + critical",      by.get("crit", {}).get("level") == "critical"),
        ("expired present + critical",   by.get("expired", {}).get("level") == "critical"),
        ("expired flagged expired",      by.get("expired", {}).get("expired") is True),
        ("None-days skipped",            "unknown" not in by),
        ("soonest-first ordering",       [a["endpoint"] for a in alerts][0] == "expired"),
    ]
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    return ok


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Alarm on expiring TLS certs")
    ap.add_argument("--scan", action="store_true", help="read cert_expiry + emit alerts")
    ap.add_argument("--dry-run", action="store_true", help="read + print, emit nothing")
    ap.add_argument("--report", action="store_true", help="print soonest-expiring certs")
    ap.add_argument("--selftest", action="store_true", help="pure classification test (no DB)")
    a = ap.parse_args()

    if a.selftest:
        sys.exit(0 if _selftest() else 1)

    conn = _connect()
    if conn is None:
        sys.exit(1)
    try:
        if a.report:
            for r in report(conn):
                print(f"  {float(r['days_until_expiry']):>8.1f}d  {r['endpoint']:<14} "
                      f"{r.get('host','')}:{r.get('port','')}  not_after={r.get('not_after')}")
            sys.exit(0)
        alerts = scan(conn, emit=a.scan and not a.dry_run)
        if alerts:
            verb = "WOULD alert" if (a.dry_run or not a.scan) else "alerted"
            print(f"{verb} on {len(alerts)} expiring cert(s):")
            for x in alerts:
                print(f"  [{x['level']}] {x['endpoint']}: "
                      f"{x['days_until_expiry']:.1f}d ({x.get('not_after')})")
        else:
            print(f"no certs expiring within {CERT_WARN_DAYS} days")
        sys.exit(0)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
