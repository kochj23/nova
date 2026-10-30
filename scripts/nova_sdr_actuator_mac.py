#!/usr/bin/env python3
"""nova_sdr_actuator_mac.py — nova_sdr_actuator for macOS radio hosts (launchd instead of systemd).

Written 2026-10-30 when RSPduo 24051E8734 moved to nova-core4 (.250, M1 mini). Runs as the login user every
60 s (LaunchAgent net.digitalnoise.nova-sdr-actuator). For every enabled tuner whose sdr_tuners.host is this
machine:
  * apply: renders ~/Library/LaunchAgents/net.digitalnoise.<unit>.plist running nova_fm_capture.py with the
    assigned target(s); reloads only on change; boots the agent out (and removes the plist) with no target
  * health: agent running + nova_fm_capture's heartbeat + output rate, the same checks and tables as the Linux
    actuator (sdr_health, health_checks), so nova_sdr_placement treats this host like any other
  * self-heal: sick >5 min -> boot the capture OUT first, then bootout/bootstrap com.sdrplay.service, then
    start the capture (max once / 30 min). macOS has no usbreset and a SIGKILLed or kickstart -k'd API service
    has dropped a Duo off the bus (2026-10-21), so the API is never restarted under an open stream.
Only fm_master / fm_dt pipelines are supported here. Runtime state lives in service_config
(service 'nova_sdr_actuator', key = host).

  nova_sdr_actuator_mac.py [--dry-run]
"""
import json, os, plistlib, socket, subprocess, sys, time

import nova_sdr_actuator as lin

HOME = os.path.expanduser("~")
SCRIPTS = os.path.join(HOME, ".openclaw", "scripts")
PYTHON = os.path.join(HOME, ".openclaw", "sdr-venv", "bin", "python")
AGENTS = os.path.join(HOME, "Library", "LaunchAgents")
LOGS = os.path.join(HOME, ".openclaw", "logs")
STATS_DIR = os.path.join(HOME, ".cache", "nova-sdr")
SDRPLAY_PLIST = "/Library/LaunchDaemons/com.sdrplay.service.plist"
SUPPORTED = {"fm_master", "fm_dt"}


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout.strip()


def label_for(unit):
    return f"net.digitalnoise.{unit}"


def capture_args(pipeline, rows):
    """nova_fm_capture.py argv for one unit (same argument rules as nova_sdr_actuator.render), or None (stop)."""
    live = [(port, t, meta) for port, t, meta in rows if t]
    if not live:
        return None
    port, t, meta = live[0]
    if pipeline == "fm_dt" and len(live) == 2:
        live.sort(key=lambda r: r[0])      # dual-tuner: argument ORDER maps to tuner (1st = Tuner 1)
        chans = [f"{lin.lab(t['label'])}:{t['freq_hz']}:{p}:{t['demod']}" for p, t, _ in live]
    else:
        ant = port if pipeline == "fm_master" else f"Tuner {port} 50 ohm"
        chans = [f"{lin.lab(t['label'])}:{t['freq_hz']}:{ant}:{t['demod']}"]
    return [PYTHON, os.path.join(SCRIPTS, "nova_fm_capture.py"), meta["serial"], meta["outdir"], *chans]


def plist_for(unit, args):
    log = os.path.join(LOGS, f"{unit}.log")
    return plistlib.dumps({
        "Label": label_for(unit), "ProgramArguments": args, "RunAtLoad": True, "KeepAlive": True,
        "ThrottleInterval": 15, "StandardOutPath": log, "StandardErrorPath": log,
        "EnvironmentVariables": {"NOVA_SDR_STATS_DIR": STATS_DIR, "PYTHONUNBUFFERED": "1",
                                 "PATH": "/opt/homebrew/bin:/usr/bin:/bin"},
        "Comment": "Managed by nova_sdr_actuator_mac from sdr_assignments -- edit the tables, not this file.",
    })


def agent_running(unit, uid):
    out = sh("launchctl", "print", f"gui/{uid}/{label_for(unit)}")
    return "state = running" in out


RELEASE_S = 5   # after the capture exits, let the API let go of the Duo before reopening it


def boot(unit, uid, path=None, out=False):
    if out and "state = " in sh("launchctl", "print", f"gui/{uid}/{label_for(unit)}"):
        sh("launchctl", "bootout", f"gui/{uid}/{label_for(unit)}")
        # bootstrapping straight after bootout reopened the Duo before the API released it:
        # "no DT-mode RSPduo found" (first reassignment on nova-core4, 2026-10-30)
        for _ in range(20):
            if not agent_running(unit, uid):
                break
            time.sleep(0.5)
        time.sleep(RELEASE_S)
    if path:
        sh("launchctl", "bootstrap", f"gui/{uid}", path)


def api_health():
    """Passive, like the Linux check: the API daemon is loaded and its process exists."""
    if "state = running" not in sh("launchctl", "print", "system/com.sdrplay.service"):
        return False, "com.sdrplay.service not running"
    return (True, "API responding") if sh("pgrep", "-x", "sdrplay_apiService") else (False, "sdrplay_apiService not running")


def heal(units, uid):
    """Capture out first, then the API, then the capture back. Never kickstart -k / pkill -9 the API."""
    for unit in units:
        boot(unit, uid, out=True)
    time.sleep(3)
    sh("sudo", "-n", "launchctl", "bootout", "system/com.sdrplay.service"); time.sleep(3)
    sh("sudo", "-n", "launchctl", "bootstrap", "system", SDRPLAY_PLIST); time.sleep(5)
    for unit in units:
        path = os.path.join(AGENTS, f"{label_for(unit)}.plist")
        if os.path.exists(path):
            boot(unit, uid, path)


def main(dry):
    import psycopg2
    try:
        import nova_dsn; dsn = nova_dsn.pg_dsn("nova_ops")
    except ImportError:
        dsn = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
    con = psycopg2.connect(dsn, connect_timeout=10); con.autocommit = True; cur = con.cursor()
    host, uid = socket.gethostname().split(".")[0], os.getuid()
    cur.execute("""SELECT u.id, u.pipeline, u.unit, u.port, u.meta, t.id, t.label, t.freq_hz, t.demod, t.meta,
                          extract(epoch FROM a.since)
                   FROM sdr_tuners u LEFT JOIN sdr_assignments a ON a.tuner_id = u.id
                   LEFT JOIN sdr_targets t ON t.id = a.target_id AND t.enabled
                   WHERE u.host = %s AND u.enabled ORDER BY u.id""", (host,))
    units = {}
    for tid, pipe, unit, port, meta, gid, label, hz, demod, gmeta, since in cur.fetchall():
        if pipe not in SUPPORTED:
            print(f"[sdr-actuator-mac] {tid}: pipeline {pipe} not supported on macOS, skipped"); continue
        target = {"id": gid, "label": label, "freq_hz": hz, "demod": demod, "meta": gmeta or {},
                  "since": float(since) if since else None} if gid else None
        u = units.setdefault(unit, {"pipeline": pipe, "rows": [], "tuners": []})
        u["rows"].append((port, target, meta or {})); u["tuners"].append(tid)
    if not units:
        print(f"[sdr-actuator-mac] {host}: no tuners"); return 0
    cur.execute("SELECT value FROM service_config WHERE service='nova_sdr_actuator' AND key=%s", (host,))
    row = cur.fetchone(); state = row[0] if row else {}
    now = time.time()

    # 1) apply assignments
    os.makedirs(AGENTS, exist_ok=True); os.makedirs(LOGS, exist_ok=True)
    for unit, u in units.items():
        path = os.path.join(AGENTS, f"{label_for(unit)}.plist")
        args = capture_args(u["pipeline"], u["rows"])
        have = open(path, "rb").read() if os.path.exists(path) else None
        if args is None:
            if have is not None:
                print(f"[sdr-actuator-mac] {unit}: no target assigned -> stop")
                if not dry:
                    boot(unit, uid, out=True); os.remove(path)
            continue
        want = plist_for(unit, args)
        if want != have:
            print(f"[sdr-actuator-mac] {unit}: config change -> {' '.join(args[2:])}")
            if not dry:
                open(path, "wb").write(want); boot(unit, uid, path, out=True)
        elif not agent_running(unit, uid) and not dry:
            boot(unit, uid, path, out=True)

    # 2) health -> sdr_health / health_checks (same rows as the Linux actuator)
    api = api_health()
    rates = lin.output_rates(dsn.replace("dbname=nova_ops", "dbname=nova_memories"))
    sick_units = []
    for unit, u in units.items():
        assigned = any(t for _, t, _ in u["rows"])
        running = agent_running(unit, uid)
        base = (True, "idle (no target)") if not assigned else (running, "agent running" if running else "agent not running")
        details = []
        for (port, t, _), tid in zip(u["rows"], u["tuners"]):
            snr = n1h = None
            if t:
                s_ok, s_detail, snr = lin.stream_health(t["label"], stats_dir=STATS_DIR)
                o_ok, o_detail, n1h = lin.output_health(t["label"], rates, t.get("since"))
                ok_t, detail_t = lin.combine(base, (s_ok, s_detail), (o_ok, o_detail), (api[0], "" if api[0] else api[1]))
            else:
                ok_t, detail_t = base
            details.append((tid, ok_t, detail_t, snr, n1h))
        ok = all(d[1] for d in details)
        if not ok and assigned:
            first = state.setdefault("sick_since", {}).setdefault(unit, now)
            if now - first >= lin.HEAL_AFTER_S:
                sick_units.append(unit)
        else:
            state.setdefault("sick_since", {}).pop(unit, None)
        print(f"[sdr-actuator-mac] {unit:22s} {'OK  ' if ok else 'SICK'} {' | '.join(d[2] for d in details)}")
        if not dry:
            for tid, ok_t, detail_t, snr, n1h in details:
                cur.execute("""INSERT INTO sdr_health(tuner_id, ts, ok, detail, unhealthy_since, snr_db, calls_1h)
                               VALUES (%s, now(), %s, %s, CASE WHEN %s THEN NULL ELSE now() END, %s, %s)
                               ON CONFLICT (tuner_id) DO UPDATE SET ts=now(), ok=EXCLUDED.ok, detail=EXCLUDED.detail,
                                 snr_db=EXCLUDED.snr_db, calls_1h=EXCLUDED.calls_1h,
                                 unhealthy_since = CASE WHEN EXCLUDED.ok THEN NULL
                                                        ELSE COALESCE(sdr_health.unhealthy_since, now()) END""",
                            (tid, ok_t, detail_t[:500], ok_t, snr, n1h))
                cur.execute("""INSERT INTO health_checks (service_name, node_name, checked_by, status, error_message)
                               VALUES (%s, %s, 'nova_sdr_actuator', %s, %s)""",
                            (f"sdr:{tid}", host, "up" if ok_t else "down", None if ok_t else detail_t[:500]))
    if not dry:
        cur.execute("""INSERT INTO health_checks (service_name, node_name, checked_by, status, error_message)
                       VALUES ('sdrplay_api', %s, 'nova_sdr_actuator', %s, %s)""",
                    (host, "up" if api[0] else "down", None if api[0] else api[1]))
    print(f"[sdr-actuator-mac] sdrplay api: {'OK' if api[0] else 'SICK'} {api[1]}")

    # 3) self-heal
    if sick_units and now - state.get("last_heal", 0) >= lin.HEAL_EVERY_S:
        print(f"[sdr-actuator-mac] {', '.join(sick_units)} sick >5 min -> capture out, API bootout/bootstrap, capture in")
        if not dry:
            state["last_heal"] = now
            heal(sick_units, uid)
    if not dry:
        cur.execute("""INSERT INTO service_config(service, key, value, updated_by) VALUES ('nova_sdr_actuator', %s, %s, %s)
                       ON CONFLICT (service, key) DO UPDATE SET value=EXCLUDED.value, updated_at=now(), updated_by=EXCLUDED.updated_by""",
                    (host, json.dumps(state), "nova_sdr_actuator_mac"))
    return 0


if __name__ == "__main__":
    sys.exit(main("--dry-run" in sys.argv))
