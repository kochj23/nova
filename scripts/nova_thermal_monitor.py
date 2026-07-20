#!/usr/bin/env python3
"""nova_thermal_monitor.py — watch closet hardware temps, ping Slack on trouble.

Reads temps/fans over keyless SSH from the closet gear and posts to
#nova-notifications only when something crosses a threshold (with per-condition
rate-limiting so it never spams), plus a one-shot "recovered" when it clears.

Targets (all reachable by SSH key from this Mac):
  core2   192.168.1.86  AMD k10temp
  UNAS    192.168.1.69  cpu-thermal + chassis/PSU fans + drive temps
  Synology 192.168.1.11 system hwmon
  UDM/Protect 192.168.1.1 ubnt-systool cputemp

Run manually to see a snapshot; scheduled every 5 min via launchd.
"""
import json, os, subprocess, time

STATE = os.path.expanduser("~/.openclaw/state/thermal_monitor.json")
SLACK_CHANNEL = "#nova-notifications"
REALERT_SEC = 30 * 60          # re-warn at most every 30 min while a condition holds

# name -> (host, remote cmd producing the reading(s)); parsed below
THRESH = {
    "core2_cpu":   93.0,       # Ryzen under Ollama load runs warm; throttles ~95
    "unas_cpu":    90.0,
    "synology":    72.0,
    "udm_cpu":     90.0,
    "drive_max":   58.0,       # any UNAS drive above this
}

def ssh(host, cmd, timeout=10):
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
             "-o", "StrictHostKeyChecking=accept-new", host, cmd],
            capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""

def read_all():
    r = {}
    # core2 — hottest hwmon reading (k10temp Tctl)
    out = ssh("192.168.1.86",
              "cat /sys/class/hwmon/hwmon*/temp*_input 2>/dev/null | sort -rn | head -1")
    r["core2_cpu"] = int(out)/1000 if out.isdigit() else None
    # synology
    out = ssh("kochj@192.168.1.11",
              "cat /sys/class/hwmon/hwmon*/temp1_input 2>/dev/null | head -1")
    r["synology"] = int(out)/1000 if out.isdigit() else None
    # udm/protect
    out = ssh("root@192.168.1.1", "ubnt-systool cputemp 2>/dev/null | grep -oE '[0-9.]+' | head -1")
    try: r["udm_cpu"] = float(out) if out else None
    except ValueError: r["udm_cpu"] = None
    # unas — cpu, fans, drive temps
    out = ssh("root@192.168.1.69",
              "echo CPU $(($(cat /sys/class/thermal/thermal_zone0/temp)/1000)); "
              "echo FANS $(cat /sys/class/hwmon/hwmon*/fan*_input 2>/dev/null | tr '\\n' ' '); "
              "echo DRV $(for d in /dev/sd?; do smartctl -A -d auto $d 2>/dev/null | "
              "awk '/Airflow_Temperature_Cel|Temperature_Celsius/{print $10; exit}'; done | tr '\\n' ' ')")
    r["unas_cpu"] = None; r["unas_fans"] = []; r["unas_drives"] = []
    for line in out.splitlines():
        p = line.split()
        if p and p[0] == "CPU" and len(p) > 1 and p[1].isdigit(): r["unas_cpu"] = int(p[1])
        elif p and p[0] == "FANS": r["unas_fans"] = [int(x) for x in p[1:] if x.isdigit()]
        elif p and p[0] == "DRV":  r["unas_drives"] = [int(x) for x in p[1:] if x.isdigit()]
    r["drive_max"] = max(r["unas_drives"]) if r["unas_drives"] else None
    r["unas_fans_spinning"] = sum(1 for f in r["unas_fans"] if f > 500)
    # PG streaming replicas of .2 — replay lag (None = unreachable/not streaming)
    for key, host in (("repl_tv_7", "192.168.1.7"), ("repl_mini_190", "192.168.1.190")):
        out = ssh(host, "bash -lc 'psql -p 5432 -d postgres -tAc \"SELECT coalesce(round(extract(epoch from (now()-pg_last_xact_replay_timestamp())))::int,-1)\"' 2>/dev/null")
        r[key] = int(out) if out.strip().lstrip("-").isdigit() and out.strip() != "-1" else None
    return r

def evaluate(r):
    """Return list of (key, message) for active alert conditions."""
    alerts = []
    for k, limit in THRESH.items():
        v = r.get(k)
        if v is not None and v > limit:
            label = {"core2_cpu":"core2 CPU","unas_cpu":"UNAS CPU","synology":"Synology",
                     "udm_cpu":"UDM/Protect","drive_max":"UNAS hottest drive"}[k]
            alerts.append((k, f"{label} at {v:.0f}°C (limit {limit:.0f}°C)"))
    # UNAS fan loss: baseline is 3 spinning (2 chassis + PSU); <2 => multiple stopped
    if r.get("unas_fans") and r["unas_fans_spinning"] < 2:
        alerts.append(("unas_fans",
                       f"UNAS has only {r['unas_fans_spinning']} spinning fan(s) "
                       f"(rpm: {r['unas_fans']}) — possible fan failure"))
    # PG replica health — alert if a standby is unreachable/not streaming, or falling behind
    for key, label in (("repl_tv_7", "PG replica .7 (TV-Movies)"), ("repl_mini_190", "PG replica .190 (Mac-mini)")):
        v = r.get(key)
        if v is None:
            alerts.append((key, f"{label} is DOWN or not streaming from .2 (was re-synced 2026-07-11)"))
        elif v > 600:
            alerts.append((key, f"{label} replay lag {v}s (>600s) — replication falling behind"))
    return alerts

def load_state():
    try:
        with open(STATE) as f: return json.load(f)
    except Exception:
        return {}

def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f: json.dump(s, f)

def slack(text):
    tok = subprocess.run(["security","find-generic-password","-s","nova-slack-bot-token","-w"],
                         capture_output=True, text=True).stdout.strip()
    if not tok:
        print("slack: no token"); return
    try:
        subprocess.run(["curl","-s","-X","POST","https://slack.com/api/chat.postMessage",
                        "-H",f"Authorization: Bearer {tok}","-H","Content-type: application/json; charset=utf-8",
                        "-d",json.dumps({"channel":SLACK_CHANNEL,"text":text})],
                       capture_output=True, text=True, timeout=15)
    except Exception as e:
        print("slack err:", e)

def main():
    now = int(time.time())
    r = read_all()
    snap = (f"core2 {r['core2_cpu']}°C | UNAS {r['unas_cpu']}°C fans{r['unas_fans']} "
            f"drives{r['unas_drives']} | Synology {r['synology']}°C | UDM {r['udm_cpu']}°C")
    print(snap)
    active = dict(evaluate(r))
    st = load_state(); last = st.get("last", {})
    # fire new / re-fire stale alerts
    for k, msg in active.items():
        if k not in last or (now - last[k]) > REALERT_SEC:
            slack(f":rotating_light: *Thermal alert* — {msg}\n_{snap}_")
            last[k] = now
    # recovery notices
    for k in list(last):
        if k not in active:
            slack(f":white_check_mark: *Recovered* — {k} back to normal\n_{snap}_")
            del last[k]
    st["last"] = last; st["snap"] = snap; st["ts"] = now
    save_state(st)

if __name__ == "__main__":
    main()
