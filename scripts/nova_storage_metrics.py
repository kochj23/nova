#!/opt/homebrew/bin/python3
"""
nova_storage_metrics.py — Storage/NAS telemetry collector for Nova's
observability stack (PG -> Grafana).

Polls both NAS devices and writes timestamped, graphable rows into
telemetry.storage_metrics (partitioned by month on ts, matching the
telemetry.* convention):

  Synology RS1221+ (192.168.1.11, DSM 7.x) via the DSM Web API.
    - per-volume used/free/total + %
    - per-disk SMART health + temperature + remaining life
    - storage pool / RAID status
    - system CPU / RAM / load / net throughput / disk I/O / sys temp
    - UPS status if attached

  UniFi UNAS Pro 8 (192.168.1.69) via UniFi Drive API.
    - overall storage used/free/total + %
    - per-share usage + status
    - device health flag

Auth is REUSED from the existing monitors — no credential duplication:
  - Synology: SynoSession from nova_synology_monitor (Keychain DSM creds)
  - UNAS:     UNASClient from nova_unas_client (Keychain API key)

Each device is collected inside its own try/except so one being down never
blocks the other. Each metric extraction is also individually guarded.

Run-once (designed for the Nova scheduler, suggest: every 5m):
  python3 nova_storage_metrics.py            # collect both, insert to PG
  python3 nova_storage_metrics.py --dry-run  # collect + print, no PG write
  python3 nova_storage_metrics.py --synology # only Synology
  python3 nova_storage_metrics.py --unas     # only UNAS

Written by Jordan Koch.
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import psycopg2
import psycopg2.extras

DB_DSN = "host=localhost dbname=nova_ops user=kochj"
NOW = datetime.now(timezone.utc)

SYNO_HOST = "192.168.1.11"
UNAS_HOST = "192.168.1.69"


def log(msg):
    print(f"[storage_metrics {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Row helper ──────────────────────────────────────────────────────────────

# Column order for INSERT — every row dict is normalised against this.
COLUMNS = [
    "ts", "source", "host", "component_type", "component_id", "component_name",
    "total_bytes", "used_bytes", "free_bytes", "used_pct",
    "temp_c", "status", "smart_status", "remain_life_pct",
    "cpu_pct", "ram_pct", "ram_real_pct", "sys_temp_c",
    "load_1m", "load_5m", "load_15m",
    "net_rx_bps", "net_tx_bps", "disk_read_bps", "disk_write_bps",
    "ups_charge_pct", "ups_runtime_s", "ups_load_pct",
    "healthy", "extra",
]


def _row(source, host, component_type, component_id, **kw):
    """Build a fully-keyed row dict (missing fields default to None)."""
    r = {c: None for c in COLUMNS}
    r["ts"] = NOW
    r["source"] = source
    r["host"] = host
    r["component_type"] = component_type
    r["component_id"] = component_id
    for k, v in kw.items():
        if k in r:
            r[k] = v
    return r


def _to_int(v, default=None):
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


# ── Synology collection ───────────────────────────────────────────────────

def collect_synology():
    """Collect Synology metrics. Returns list of row dicts (may be empty)."""
    rows = []
    try:
        from nova_synology_monitor import (
            SynoSession, get_system_info, get_utilization,
            get_storage, get_ups,
        )
    except Exception as e:
        log(f"Synology: cannot import monitor module: {e}")
        return rows

    try:
        with SynoSession() as session:
            sysinfo = _safe(get_system_info, session, label="system_info")
            util = _safe(get_utilization, session, label="utilization")
            storage = _safe(get_storage, session, label="storage")
            ups = _safe(get_ups, session, label="ups")

            rows += _syno_system_row(sysinfo, util)
            rows += _syno_volume_rows(storage)
            rows += _syno_pool_rows(storage)
            rows += _syno_disk_rows(storage)
            rows += _syno_ups_rows(ups)
    except Exception as e:
        log(f"Synology: collection failed ({e}) — host may be down or auth failed")
    return rows


def _safe(fn, *args, label="call"):
    """Run a collector call, swallowing errors into None."""
    try:
        return fn(*args)
    except Exception as e:
        log(f"Synology {label}: {e}")
        return None


def _syno_system_row(sysinfo, util):
    try:
        cpu_pct = ram_pct = ram_real_pct = None
        sys_temp = l1 = l5 = l15 = None
        net_rx = net_tx = disk_r = disk_w = None

        if sysinfo:
            sys_temp = sysinfo.get("sys_temp", sysinfo.get("temperature"))

        if util:
            cpu = util.get("cpu", {}) or {}
            if cpu:
                cpu_pct = (cpu.get("user_load", 0) + cpu.get("system_load", 0)
                           + cpu.get("other_load", 0))
                l1 = cpu.get("1min_load")
                l5 = cpu.get("5min_load")
                l15 = cpu.get("15min_load")

            mem = util.get("memory", {}) or {}
            total_kb = mem.get("memory_size", 0)
            avail_kb = mem.get("avail_real", 0)
            ram_real_pct = mem.get("real_usage")
            if total_kb:
                ram_pct = round((total_kb - avail_kb) / total_kb * 100, 1)

            # network: prefer the 'total' device entry
            net_rx = net_tx = 0
            for iface in util.get("network", []) or []:
                if isinstance(iface, dict) and iface.get("device") == "total":
                    net_rx = iface.get("rx", 0)
                    net_tx = iface.get("tx", 0)
                    break
            else:
                net_rx = net_tx = None

            # disk I/O: prefer the 'total' entry
            disk_obj = util.get("disk", {}) or {}
            tot = disk_obj.get("total") if isinstance(disk_obj, dict) else None
            if isinstance(tot, dict):
                disk_r = tot.get("read_byte")
                disk_w = tot.get("write_byte")

        model = sysinfo.get("model") if sysinfo else None
        fw = sysinfo.get("firmware_ver") if sysinfo else None
        return [_row(
            "synology", SYNO_HOST, "system", "system",
            component_name=model,
            cpu_pct=cpu_pct, ram_pct=ram_pct, ram_real_pct=ram_real_pct,
            sys_temp_c=sys_temp, load_1m=l1, load_5m=l5, load_15m=l15,
            net_rx_bps=net_rx, net_tx_bps=net_tx,
            disk_read_bps=disk_r, disk_write_bps=disk_w,
            healthy=(not (sysinfo or {}).get("sys_tempwarn", False)),
            extra=psycopg2.extras.Json({"firmware": fw, "uptime": (sysinfo or {}).get("up_time")}) if sysinfo else None,
        )]
    except Exception as e:
        log(f"Synology system row: {e}")
        return []


def _syno_volume_rows(storage):
    rows = []
    if not storage:
        return rows
    try:
        for vol in storage.get("volumes", storage.get("vol_info", [])) or []:
            try:
                vid = vol.get("id", vol.get("vol_path", "?"))
                status = vol.get("status", "unknown")
                size = vol.get("size", {})
                if isinstance(size, dict):
                    total = _to_int(size.get("total"), 0)
                    used = _to_int(size.get("used"), 0)
                else:
                    total = _to_int(vol.get("vol_size", vol.get("total_size")), 0)
                    used = _to_int(vol.get("used_size"), 0)
                free = (total - used) if (total and used is not None) else None
                pct = round(used / total * 100, 2) if total else None
                rows.append(_row(
                    "synology", SYNO_HOST, "volume", str(vid),
                    component_name=vol.get("fs_type"),
                    total_bytes=total, used_bytes=used, free_bytes=free,
                    used_pct=pct, status=status,
                    healthy=(str(status).lower() in ("normal", "healthy")),
                ))
            except Exception as e:
                log(f"Synology volume row: {e}")
    except Exception as e:
        log(f"Synology volumes: {e}")
    return rows


def _syno_pool_rows(storage):
    rows = []
    if not storage:
        return rows
    try:
        for pool in storage.get("storagePools", storage.get("raid_info", [])) or []:
            try:
                pid = pool.get("id", "?")
                status = pool.get("status", "unknown")
                rows.append(_row(
                    "synology", SYNO_HOST, "pool", str(pid),
                    component_name=pool.get("desc") or pool.get("device_type"),
                    status=status,
                    healthy=(str(status).lower() in (
                        "normal", "healthy", "background_scrubbing", "scrubbing")),
                    extra=psycopg2.extras.Json({
                        "disks": pool.get("disks"),
                        "device_type": pool.get("device_type"),
                    }),
                ))
            except Exception as e:
                log(f"Synology pool row: {e}")
    except Exception as e:
        log(f"Synology pools: {e}")
    return rows


def _syno_disk_rows(storage):
    rows = []
    if not storage:
        return rows
    try:
        for disk in storage.get("disks", storage.get("disk_info", [])) or []:
            try:
                did = disk.get("id", disk.get("name", "?"))
                status = disk.get("status", "unknown")
                smart = disk.get("smart_status", "unknown")
                temp = disk.get("temp", disk.get("temperature"))
                size = _to_int(disk.get("size_total"), None)
                life = disk.get("remain_life", {}) or {}
                life_val = life.get("value")
                if life_val is not None and life_val < 0:
                    life_val = None
                rows.append(_row(
                    "synology", SYNO_HOST, "disk", str(did),
                    component_name=(disk.get("model") or "").strip() or None,
                    total_bytes=size, temp_c=temp, status=status,
                    smart_status=smart, remain_life_pct=life_val,
                    healthy=(str(status).lower() in ("normal", "healthy", "initialized")
                             and str(smart).lower() in ("normal", "safe", "ok", "unknown")),
                    extra=psycopg2.extras.Json({
                        "disk_type": disk.get("diskType"),
                        "serial": disk.get("serial") or disk.get("ui_serial"),
                        "used_by": disk.get("used_by"),
                        "unc": disk.get("unc"),
                    }),
                ))
            except Exception as e:
                log(f"Synology disk row: {e}")
    except Exception as e:
        log(f"Synology disks: {e}")
    return rows


def _syno_ups_rows(ups):
    if not ups:
        return []
    try:
        model = ups.get("model") or ups.get("ups_model")
        status = ups.get("status") or ups.get("ups_status", "unknown")
        # No real UPS attached -> DSM returns status_unknown / empty model. Skip.
        if not model and "unknown" in str(status).lower():
            return []

        def _num(v):
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        return [_row(
            "synology", SYNO_HOST, "ups", "ups",
            component_name=model, status=status,
            ups_charge_pct=_num(ups.get("battery_charge", ups.get("charge"))),
            ups_runtime_s=_to_int(ups.get("battery_runtime", ups.get("runtime")), None),
            ups_load_pct=_num(ups.get("load", ups.get("ups_load"))),
            healthy=("online" in str(status).lower() or "ol" == str(status).lower()),
        )]
    except Exception as e:
        log(f"Synology UPS row: {e}")
        return []


# ── UNAS collection ─────────────────────────────────────────────────────────

def collect_unas():
    """Collect UNAS Pro metrics. Returns list of row dicts (may be empty)."""
    rows = []
    try:
        from nova_unas_client import UNASClient, UNASError
    except Exception as e:
        log(f"UNAS: cannot import client module: {e}")
        return rows

    try:
        client = UNASClient()
        snap = client.health_snapshot()
    except Exception as e:
        log(f"UNAS: snapshot failed ({e}) — host may be down or auth failed")
        return rows

    try:
        st = snap.get("storage", {}) or {}
        dev = snap.get("device", {}) or {}
        rows.append(_row(
            "unas", UNAS_HOST, "system", "system",
            component_name=dev.get("model") or dev.get("name"),
            total_bytes=st.get("total_bytes"),
            used_bytes=st.get("used_bytes"),
            free_bytes=st.get("free_bytes"),
            used_pct=st.get("used_pct"),
            status=st.get("status"),
            healthy=(str(st.get("status", "")).lower() == "healthy"),
            extra=psycopg2.extras.Json({
                "device_state": dev.get("state"),
                "cloud_connected": dev.get("cloud_connected"),
                "has_internet": dev.get("has_internet"),
                "needs_more_disk": st.get("needs_more_disk"),
            }),
        ))
    except Exception as e:
        log(f"UNAS system row: {e}")

    try:
        for sh in snap.get("shares", []) or []:
            try:
                used = sh.get("used_bytes")
                rows.append(_row(
                    "unas", UNAS_HOST, "share", str(sh.get("name", sh.get("id", "?"))),
                    component_name=sh.get("name"),
                    used_bytes=used, status=sh.get("status"),
                    healthy=(str(sh.get("status", "")).lower() == "active"),
                    extra=psycopg2.extras.Json({
                        "id": sh.get("id"),
                        "encryption": sh.get("encryption"),
                        "quota": sh.get("quota"),
                    }),
                ))
            except Exception as e:
                log(f"UNAS share row: {e}")
    except Exception as e:
        log(f"UNAS shares: {e}")

    return rows


# ── PG write ─────────────────────────────────────────────────────────────────

def ensure_partition(conn, ts):
    """Make sure the monthly partition for ts exists (idempotent)."""
    try:
        first = ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if first.month == 12:
            nxt = first.replace(year=first.year + 1, month=1)
        else:
            nxt = first.replace(month=first.month + 1)
        suffix = first.strftime("%Y%m")
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS telemetry.storage_metrics_{suffix} "
                f"PARTITION OF telemetry.storage_metrics "
                f"FOR VALUES FROM (%s) TO (%s)",
                (first, nxt),
            )
    except Exception as e:
        log(f"Partition ensure: {e}")


def write_rows(rows):
    if not rows:
        log("No rows to write.")
        return 0
    try:
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = True
    except Exception as e:
        log(f"DB connect failed: {e}")
        return 0

    inserted = 0
    try:
        ensure_partition(conn, NOW)
        cols = ", ".join(COLUMNS)
        placeholders = ", ".join(["%s"] * len(COLUMNS))
        sql = f"INSERT INTO telemetry.storage_metrics ({cols}) VALUES ({placeholders})"
        values = [[r[c] for c in COLUMNS] for r in rows]
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, values)
            inserted = len(values)
    except Exception as e:
        log(f"Insert failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return inserted


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Nova storage/NAS metrics collector")
    parser.add_argument("--synology", action="store_true", help="Only Synology")
    parser.add_argument("--unas", action="store_true", help="Only UNAS Pro")
    parser.add_argument("--dry-run", action="store_true", help="Collect + print, no PG write")
    args = parser.parse_args()

    do_syno = args.synology or not args.unas
    do_unas = args.unas or not args.synology

    rows = []
    if do_syno:
        syno = collect_synology()
        log(f"Synology: collected {len(syno)} row(s)")
        rows += syno
    if do_unas:
        unas = collect_unas()
        log(f"UNAS: collected {len(unas)} row(s)")
        rows += unas

    if args.dry_run:
        for r in rows:
            printable = {k: v for k, v in r.items()
                         if v is not None and k != "ts" and not hasattr(v, "adapted")}
            log(f"  {r['source']}/{r['component_type']}/{r['component_id']}: {printable}")
        log(f"DRY RUN — would insert {len(rows)} row(s)")
        return

    n = write_rows(rows)
    log(f"Inserted {n}/{len(rows)} row(s) into telemetry.storage_metrics")
    if n == 0 and rows:
        sys.exit(1)


if __name__ == "__main__":
    main()
