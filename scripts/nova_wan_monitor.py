#!/opt/homebrew/bin/python3
"""
nova_wan_monitor.py — WAN uptime / latency / speedtest collector for Nova's
observability stack (PG -> Grafana).

Each run:
  * Pings a few anchor hosts (1.1.1.1, 8.8.8.8) for latency + packet loss and
    writes one "ping" row per anchor.
  * Periodically (gated to ~hourly via a sentinel file) runs a bandwidth test
    and writes one "speedtest" row.
      - Prefers `speedtest` (Ookla CLI) or `speedtest-cli` if installed.
      - Falls back to a timed HTTP download for a rough down_mbps estimate and
        flags the limitation in `note` (up_mbps stays NULL for the fallback).

Writes timestamped, graphable rows into telemetry.wan_quality (partitioned by
month on ts, matching the telemetry.* convention).

Resilient: every probe is individually guarded; one anchor or the speedtest
failing never blocks the rest. Designed for the Nova scheduler (every 5m;
speedtest is self-gated to hourly inside).

  python3 nova_wan_monitor.py              # ping + (gated) speedtest, write PG
  python3 nova_wan_monitor.py --dry-run    # collect + print, no PG write
  python3 nova_wan_monitor.py --speedtest  # force a speedtest this run
  python3 nova_wan_monitor.py --no-speedtest

Written by Jordan Koch.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras

DB_DSN = "host=localhost dbname=nova_ops user=kochj"
NOW = datetime.now(timezone.utc)

PING_ANCHORS = ["1.1.1.1", "8.8.8.8"]
PING_COUNT = 5
SPEEDTEST_GATE = timedelta(minutes=55)          # min spacing between speedtests
GATE_FILE = Path.home() / ".openclaw" / ".wan_speedtest_last"
# A ~10 MB file for the timed-download fallback (Cloudflare speed endpoint).
FALLBACK_URL = "https://speed.cloudflare.com/__down?bytes=10000000"
FALLBACK_BYTES = 10_000_000

COLUMNS = [
    "ts", "kind", "target", "latency_ms", "packet_loss_pct",
    "down_mbps", "up_mbps", "note",
]


def log(msg):
    print(f"[wan_monitor {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _row(kind, target, **kw):
    r = {c: None for c in COLUMNS}
    r["ts"] = NOW
    r["kind"] = kind
    r["target"] = target
    for k, v in kw.items():
        if k in r:
            r[k] = v
    return r


# ── Ping ────────────────────────────────────────────────────────────────────

def ping(target):
    """Return a ping row dict for `target` (latency_ms avg, packet_loss_pct)."""
    try:
        out = subprocess.run(
            ["ping", "-c", str(PING_COUNT), "-t", "10", target],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception as e:
        log(f"ping {target}: {e}")
        return _row("ping", target, packet_loss_pct=100.0, note=f"ping error: {e}")

    loss = None
    m = re.search(r"([\d.]+)%\s+packet loss", out)
    if m:
        loss = float(m.group(1))

    latency = None
    # macOS/BSD: round-trip min/avg/max/stddev = a/b/c/d ms
    m = re.search(r"=\s*[\d.]+/([\d.]+)/", out)
    if m:
        latency = float(m.group(1))

    return _row("ping", target, latency_ms=latency, packet_loss_pct=loss)


# ── Speedtest ─────────────────────────────────────────────────────────────────

def _should_speedtest(force, disable):
    if disable:
        return False
    if force:
        return True
    try:
        if GATE_FILE.exists():
            last = datetime.fromtimestamp(GATE_FILE.stat().st_mtime, tz=timezone.utc)
            if NOW - last < SPEEDTEST_GATE:
                return False
    except Exception:
        pass
    return True


def _touch_gate():
    try:
        GATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        GATE_FILE.touch()
    except Exception as e:
        log(f"gate touch: {e}")


def speedtest_ookla(binpath):
    """Ookla `speedtest` CLI with JSON output."""
    try:
        out = subprocess.run(
            [binpath, "--format=json", "--accept-license", "--accept-gdpr"],
            capture_output=True, text=True, timeout=120,
        ).stdout
        d = json.loads(out)
        down = d["download"]["bandwidth"] * 8 / 1e6   # bytes/s -> Mbps
        up = d["upload"]["bandwidth"] * 8 / 1e6
        ping_ms = d.get("ping", {}).get("latency")
        srv = d.get("server", {}).get("name", "ookla")
        return _row("speedtest", srv, latency_ms=ping_ms,
                    down_mbps=round(down, 2), up_mbps=round(up, 2),
                    note="ookla speedtest CLI")
    except Exception as e:
        log(f"ookla speedtest: {e}")
        return None


def speedtest_cli(binpath):
    """Sivel speedtest-cli (`speedtest-cli`) with JSON output."""
    try:
        out = subprocess.run(
            [binpath, "--json"], capture_output=True, text=True, timeout=120,
        ).stdout
        d = json.loads(out)
        down = d["download"] / 1e6   # bits/s -> Mbps
        up = d["upload"] / 1e6
        ping_ms = d.get("ping")
        srv = d.get("server", {}).get("sponsor", "speedtest-cli")
        return _row("speedtest", srv, latency_ms=ping_ms,
                    down_mbps=round(down, 2), up_mbps=round(up, 2),
                    note="speedtest-cli")
    except Exception as e:
        log(f"speedtest-cli: {e}")
        return None


def speedtest_fallback():
    """Rough down_mbps via a timed HTTP download. up_mbps stays NULL."""
    try:
        import urllib.request
        req = urllib.request.Request(
            FALLBACK_URL,
            headers={"User-Agent": "nova-wan-monitor/1.0 (+https://digitalnoise.net)"},
        )
        start = time.monotonic()
        got = 0
        with urllib.request.urlopen(req, timeout=60) as resp:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                got += len(chunk)
        elapsed = time.monotonic() - start
        if elapsed <= 0 or got <= 0:
            return None
        down = (got * 8) / elapsed / 1e6
        return _row("speedtest", "cloudflare-fallback",
                    down_mbps=round(down, 2),
                    note=("FALLBACK timed download (no speedtest CLI); "
                          "down_mbps approximate, up_mbps unmeasured"))
    except Exception as e:
        log(f"fallback download: {e}")
        return None


def run_speedtest():
    ookla = shutil.which("speedtest")
    cli = shutil.which("speedtest-cli")
    row = None
    if ookla:
        row = speedtest_ookla(ookla)
    if row is None and cli:
        row = speedtest_cli(cli)
    if row is None:
        log("no speedtest CLI found; using timed-download fallback")
        row = speedtest_fallback()
    return row


# ── PG ────────────────────────────────────────────────────────────────────────

DDL = """
CREATE SCHEMA IF NOT EXISTS telemetry;
CREATE TABLE IF NOT EXISTS telemetry.wan_quality (
    ts               timestamptz NOT NULL,
    kind             text        NOT NULL,   -- 'ping' | 'speedtest'
    target           text,
    latency_ms       real,
    packet_loss_pct  real,
    down_mbps        real,
    up_mbps          real,
    note             text
) PARTITION BY RANGE (ts);
CREATE INDEX IF NOT EXISTS idx_wan_quality_ts ON telemetry.wan_quality (ts);
CREATE INDEX IF NOT EXISTS idx_wan_quality_kind_ts ON telemetry.wan_quality (kind, ts);
"""


def ensure_partition(conn, ts):
    try:
        first = ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        nxt = first.replace(year=first.year + 1, month=1) if first.month == 12 \
            else first.replace(month=first.month + 1)
        suffix = first.strftime("%Y%m")
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS telemetry.wan_quality_{suffix} "
                f"PARTITION OF telemetry.wan_quality "
                f"FOR VALUES FROM (%s) TO (%s)", (first, nxt))
    except Exception as e:
        log(f"partition ensure: {e}")


def write_rows(rows):
    if not rows:
        log("no rows to write")
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
        sql = f"INSERT INTO telemetry.wan_quality ({cols}) VALUES ({ph})"
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


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Nova WAN quality collector")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--speedtest", action="store_true", help="force speedtest")
    ap.add_argument("--no-speedtest", action="store_true", help="skip speedtest")
    args = ap.parse_args()

    rows = []
    for anchor in PING_ANCHORS:
        r = ping(anchor)
        rows.append(r)
        log(f"ping {anchor}: latency={r['latency_ms']}ms loss={r['packet_loss_pct']}%")

    if _should_speedtest(args.speedtest, args.no_speedtest):
        st = run_speedtest()
        if st:
            rows.append(st)
            log(f"speedtest {st['target']}: down={st['down_mbps']} up={st['up_mbps']} ({st['note']})")
            if not args.dry_run:
                _touch_gate()
        else:
            log("speedtest produced no row")
    else:
        log("speedtest gated (not due) / disabled")

    if args.dry_run:
        for r in rows:
            log(f"  {r['kind']}/{r['target']}: " +
                ", ".join(f"{k}={r[k]}" for k in COLUMNS if r[k] is not None and k not in ("ts", "kind", "target")))
        log(f"DRY RUN — would insert {len(rows)} row(s)")
        return

    n = write_rows(rows)
    log(f"inserted {n} row(s)")


if __name__ == "__main__":
    main()
