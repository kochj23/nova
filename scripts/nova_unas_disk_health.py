#!/usr/bin/env python3
"""nova_unas_disk_health.py — collect UNAS Pro disk/volume/RAID health via SSH
into telemetry.storage_metrics (the UniFi Drive API exposes none of this).

UniFi OS SNMP gives only CPU/mem; the Drive API gives pool used% + share status.
Disk temperatures, SMART health, and mdadm RAID state are only reachable over SSH
(smartctl / df / /proc/mdstat). This fills that gap so the UNAS gets a real
storage-health dashboard like the Synology one.

Writes component_type in {disk, volume, raid} with host=192.168.1.69,
source='unas-ssh'. Runs from .6 (key auth to root@UNAS). Never raises fatally.
Written by Jordan Koch (via Claude).
"""
import json
import re
import subprocess
import sys

import psycopg2

HOST = "192.168.1.69"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", f"root@{HOST}"]
DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
POOL = "/volume/b37f2e84-517c-4a4f-92f0-4d642527ba17"

# Remote one-shot: emit TSV lines for volume, raid arrays, and each disk.
REMOTE = r'''
df -B1 ''' + POOL + r''' 2>/dev/null | awk 'NR==2{print "VOL\tpool\t"$2"\t"$3"\t"$4"\t"$5}'
awk '/^md[0-9]/{md=$1; state=($3=="active")?"active":$3; line=$0}
     /blocks/{ if (md){ deg=($0 ~ /_/)?"degraded":"ok"; print "RAID\t"md"\t"state"\t"deg; md="" } }' /proc/mdstat 2>/dev/null
for d in sda sdb sdc sdd sde sdf sdg sdh nvme0n1; do
  [ -e /dev/$d ] || continue
  o=$(smartctl -A -H -i /dev/$d 2>/dev/null)
  model=$(echo "$o" | awk -F: '/Device Model|Model Number/{gsub(/^[ \t]+/,"",$2);print $2;exit}')
  health=$(echo "$o" | awk -F: '/SMART overall-health|SMART Health Status/{gsub(/^[ \t]+/,"",$2);print $2;exit}')
  temp=$(echo "$o" | awk '/Temperature_Celsius/{print $10;exit} /Current Drive Temperature/{print $4;exit} /^Temperature:/{print $2;exit}')
  poh=$(echo "$o" | awk '/Power_On_Hours/{print $10;exit} /Power On Hours/{gsub(",","",$NF);print $NF;exit}')
  re=$(echo "$o" | awk '/Reallocated_Sector_Ct/{print $10;exit}')
  echo -e "DISK\t$d\t$model\t$health\t$temp\t$poh\t$re"
done
'''


def collect():
    out = subprocess.run(SSH + [REMOTE], capture_output=True, text=True, timeout=120).stdout
    rows = []
    for line in out.splitlines():
        f = line.split("\t")
        if f[0] == "VOL" and len(f) >= 6:
            total, used, free, pct = int(f[2]), int(f[3]), int(f[4]), float(f[5].rstrip("%"))
            rows.append(dict(component_type="volume", component_id="pool", component_name="UNAS pool",
                             total_bytes=total, used_bytes=used, free_bytes=free, used_pct=pct,
                             status="ok", healthy=(pct < 90)))
        elif f[0] == "RAID" and len(f) >= 4:
            healthy = f[3] == "ok" and f[2] == "active"
            rows.append(dict(component_type="raid", component_id=f[1], component_name=f[1],
                             status=f[2], healthy=healthy, extra=json.dumps({"degraded": f[3] == "degraded"})))
        elif f[0] == "DISK" and len(f) >= 7:
            dev, model, health, temp, poh, re_ = f[1], f[2], f[3], f[4], f[5], f[6]
            rows.append(dict(component_type="disk", component_id=dev, component_name=(model or dev).strip(),
                             temp_c=float(temp) if temp and temp.replace(".", "").isdigit() else None,
                             smart_status=health or None, status=health or None,
                             healthy=(health.upper() in ("PASSED", "OK")) if health else None,
                             extra=json.dumps({"model": model, "power_on_hours": poh, "reallocated": re_})))
    return rows


def main():
    rows = collect()
    if not rows:
        print("[unas-disk] no rows collected (UNAS down or ssh failed)", file=sys.stderr)
        return 1
    cols = ("source", "host", "component_type", "component_id", "component_name", "total_bytes",
            "used_bytes", "free_bytes", "used_pct", "temp_c", "status", "smart_status", "healthy", "extra")
    c = psycopg2.connect(DSN); c.autocommit = True
    with c.cursor() as cur:
        for r in rows:
            r.setdefault("source", "unas-ssh"); r["host"] = HOST
            vals = [r.get(k) for k in cols]
            ph = ",".join(["now()"] + ["%s"] * len(cols))
            cur.execute(f"INSERT INTO telemetry.storage_metrics (ts,{','.join(cols)}) VALUES ({ph})", vals)
    c.close()
    ndisk = sum(1 for r in rows if r["component_type"] == "disk")
    print(f"[unas-disk] wrote {len(rows)} rows ({ndisk} disks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
