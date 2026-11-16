#!/usr/bin/env python3
"""nova_ram_guard.py — page BEFORE an inference node OOMs, and relieve if critical.

Why this exists: 2026-11-15 the Mac Studio (.6, 512GB) silently OOM'd — Ollama pinned
~157GB of models (KEEP_ALIVE=-1) until jetsam started killing Jordan's browser. Nothing
paged; he had to ask what happened. The root cause (keep-alive) is fixed, but a box that
holds many large models can still spike under concurrent load. This is the safety net:
recycle-on-CONDITION (not a clock), and PAGE so an OOM is a managed event, not a surprise.

Runs every ~10 min from scheduler-core. Read-only probe over SSH; the only write action is
the SAFE, reversible relief (kickstart the model-pinning ollama daemon) at CRITICAL only.
Fail-open: the guard never pages on its own errors. Cooldown via service_config high-water.

  nova_ram_guard.py            # probe nodes, page/relieve as needed
  nova_ram_guard.py --dry-run  # probe + print classification, never post or act
  nova_ram_guard.py --selftest # pure-logic threshold assertions, no SSH/DB/Slack
"""
import os
import subprocess
import sys
from datetime import datetime, timezone

# Inference nodes to watch. .6 = Mac Studio (512GB, the OOM box). Add more as needed.
NODES = os.environ.get("NOVA_RAM_GUARD_NODES", "192.168.1.6").split(",")
# Thresholds (env-overridable). Worst signal wins. pressure_level: 2=warn, 4=critical (macOS).
FREE_WARN_PCT = int(os.environ.get("NOVA_RAM_FREE_WARN", "12"))
FREE_CRIT_PCT = int(os.environ.get("NOVA_RAM_FREE_CRIT", "6"))
SWAP_WARN_MB  = int(os.environ.get("NOVA_RAM_SWAP_WARN", "2048"))
SWAP_CRIT_MB  = int(os.environ.get("NOVA_RAM_SWAP_CRIT", "4096"))
COOLDOWN_MIN  = int(os.environ.get("NOVA_RAM_COOLDOWN_MIN", "30"))
OLLAMA_LABEL  = "system/net.digitalnoise.ollama"   # the interactive (model-pinning) daemon

# Remote probe: one line "UNAME|PRESSURE|FREEPCT|SWAPUSEDMB" on macOS or Linux.
PROBE = r'''
if [ "$(uname)" = "Darwin" ]; then
  PL=$(sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null)
  FP=$(memory_pressure 2>/dev/null | awk -F": " '/free percentage/{gsub(/%/,"",$2);print int($2)}')
  SW=$(sysctl -n vm.swapusage 2>/dev/null | sed -n 's/.*used = \([0-9.]*\)M.*/\1/p')
  echo "Darwin|${PL:-}|${FP:-}|${SW:-0}"
else
  read T A < <(awk '/^MemTotal:/{t=$2}/^MemAvailable:/{a=$2}END{print t,a}' /proc/meminfo)
  ST=$(awk '/^SwapTotal:/{print $2}' /proc/meminfo); SF=$(awk '/^SwapFree:/{print $2}' /proc/meminfo)
  FP=$(( T>0 ? 100*A/T : 100 )); SW=$(( (ST-SF)/1024 ))
  echo "Linux||${FP}|${SW}"
fi
'''


def log(m):
    print(f"[ram-guard {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def classify(pressure_level, free_pct, swap_used_mb):
    """Worst of three signals -> 'ok' | 'warn' | 'critical'. Pure; unit-tested in demo().
    pressure_level is the macOS kernel jetsam level (1/2/4) or None (Linux: ignored)."""
    level = 0  # 0 ok, 1 warn, 2 critical
    if pressure_level is not None:
        if pressure_level >= 4:
            level = max(level, 2)
        elif pressure_level >= 2:
            level = max(level, 1)
    if free_pct is not None:
        if free_pct <= FREE_CRIT_PCT:
            level = max(level, 2)
        elif free_pct <= FREE_WARN_PCT:
            level = max(level, 1)
    # Swap-used is a LAGGING signal on macOS: it does not drain after memory frees, so a
    # calm box (pressure_level==1) can sit on GBs of stale swap from a past spike. Only trust
    # swap when the kernel corroborates pressure (level>=2) or on Linux (pressure_level is None,
    # where swap use is meaningful). ponytail: calibration knob the raw number can't see.
    swap_trusted = (pressure_level is None) or (pressure_level >= 2)
    if swap_trusted and swap_used_mb is not None:
        if swap_used_mb >= SWAP_CRIT_MB:
            level = max(level, 2)
        elif swap_used_mb >= SWAP_WARN_MB:
            level = max(level, 1)
    return {0: "ok", 1: "warn", 2: "critical"}[level]


def ssh(host, cmd, timeout=20):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                           f"kochj@{host}", cmd], capture_output=True, text=True, timeout=timeout)


def probe(host):
    """Return (uname, pressure_level|None, free_pct|None, swap_used_mb) or None on failure."""
    try:
        r = ssh(host, PROBE)
        line = (r.stdout or "").strip().splitlines()[-1]
        uname, pl, fp, sw = line.split("|")
        return (uname,
                int(pl) if pl.strip() else None,
                int(fp) if fp.strip() else None,
                int(float(sw)) if sw.strip() else 0)
    except Exception as e:  # noqa: BLE001 — fail-open: a probe error never pages
        log(f"probe {host} failed ({e})")
        return None


def top_models(host):
    try:
        r = ssh(host, "curl -s -m 5 http://127.0.0.1:11434/api/ps", timeout=12)
        import json
        ms = json.loads(r.stdout or "{}").get("models", [])
        return ", ".join(f"{m['name']}({round(m.get('size',0)/1e9)}GB)" for m in ms[:6]) or "(none)"
    except Exception:
        return "(unknown)"


def _on_cooldown(cur, host):
    cur.execute("SELECT value FROM service_config WHERE service='nova_ram_guard' AND key=%s", (f"last_alert_{host}",))
    row = cur.fetchone()
    if not row or not row[0]:
        return False
    try:
        import json
        prev = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        last = datetime.fromisoformat(prev["ts"])
        return (datetime.now(timezone.utc) - last).total_seconds() < COOLDOWN_MIN * 60
    except Exception:
        return False


def _mark(cur, host, state):
    import json
    cur.execute(
        """INSERT INTO service_config (service,key,value,updated_at,updated_by)
           VALUES ('nova_ram_guard',%s,%s::jsonb,now(),'nova_ram_guard')
           ON CONFLICT (service,key) DO UPDATE SET value=EXCLUDED.value, updated_at=now(), updated_by='nova_ram_guard'""",
        (f"last_alert_{host}", json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "state": state})))


def relieve(host):
    """SAFE, reversible: recycle the model-pinning ollama daemon to drop pinned models."""
    r = ssh(host, f"sudo -n launchctl kickstart -k {OLLAMA_LABEL}", timeout=25)
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()[:200]


def run(dry_run=False):
    import nova_config
    conn = cur = None
    if not dry_run:
        import psycopg2
        import nova_dsn
        conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"), connect_timeout=8)
        conn.autocommit = True
        cur = conn.cursor()
    for host in NODES:
        p = probe(host)
        if not p:
            continue
        uname, pl, fp, sw = p
        state = classify(pl, fp, sw)
        log(f"{host}: {state} (pressure={pl} free={fp}% swap={sw}MB, {uname})")
        if state == "ok":
            continue
        if not dry_run and _on_cooldown(cur, host):
            log(f"{host}: {state} but within {COOLDOWN_MIN}m cooldown — not re-paging")
            continue
        models = top_models(host)
        icon = ":rotating_light:" if state == "critical" else ":warning:"
        msg = (f"{icon} *Memory pressure {state.upper()} on {host}*\n"
               f"  free={fp}% · swap used={sw}MB · jetsam pressure={pl}\n"
               f"  loaded models: {models}")
        if state == "critical":
            if dry_run:
                msg += "\n  _[dry-run] would recycle the ollama daemon to relieve_"
            else:
                ok, detail = relieve(host)
                msg += f"\n  :wrench: recycled ollama daemon to relieve — {'OK' if ok else 'FAILED: '+detail}"
        if dry_run:
            log("DRY-RUN would post:\n" + msg)
        else:
            nova_config.post_both(msg, slack_channel=nova_config.SLACK_ALERTS)
            _mark(cur, host, state)
            log(f"{host}: paged #nova-alerts ({state})")
    if conn:
        conn.close()
    return 0


def demo():
    """Pure-logic checks — no SSH, no DB, no Slack."""
    # all-clear
    assert classify(1, 70, 0) == "ok"
    # macOS kernel pressure drives state even when free% looks fine
    assert classify(2, 70, 0) == "warn"
    assert classify(4, 70, 0) == "critical"
    # free% thresholds (defaults 12/6)
    assert classify(1, 10, 0) == "warn"
    assert classify(1, 5, 0) == "critical"
    # swap is a LAGGING signal on macOS: stale swap on a calm box (pressure==1) must NOT page
    assert classify(1, 70, 4096) == "ok"      # the exact false-positive seen 2026-11-15 post-fix
    assert classify(1, 70, 2048) == "ok"
    # but when the kernel corroborates pressure, swap escalates
    assert classify(2, 70, 4096) == "critical"
    # worst signal wins across the three
    assert classify(2, 5, 0) == "critical"
    # Linux path: pressure_level None -> ignored, free%/swap still work
    assert classify(None, 70, 0) == "ok"
    assert classify(None, 4, 0) == "critical"
    # boundaries: exactly at warn free threshold is warn; one above is ok
    assert classify(1, FREE_WARN_PCT, 0) == "warn"
    assert classify(1, FREE_WARN_PCT + 1, 0) == "ok"
    print("all ram-guard assertions passed")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Page/relieve before an inference node OOMs.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(sys.argv[1:] if argv is None else list(argv))
    if a.selftest:
        demo()
        return 0
    return run(dry_run=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
