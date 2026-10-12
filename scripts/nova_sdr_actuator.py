#!/usr/bin/env python3
"""nova_sdr_actuator.py — apply sdr_assignments to this host's capture units + report tuner health.

Runs as root every minute (nova-sdr-actuator.timer) on each radio host (.86 nova-core2, .5 nova-core3).
For every tuner on this host:
  * health: unit active AND its capture actually opened since the last start (an "active" unit
    stuck in a wedged SDRplay API captured nothing for days on 2026-10-12) -> sdr_health
  * apply: renders /etc/systemd/system/<unit>.service.d/zz-sdr.conf from the assigned target;
    restarts only units whose config changed, master before slave; stops units with no target
  * self-heal: an SDRplay unit unhealthy >5 min -> restart sdrplay.service (max once / 30 min)
nova_sdr_placement.py decides the assignments; this only applies them.

  nova_sdr_actuator.py [--dry-run]
"""
import json, os, re, socket, subprocess, sys, time
from datetime import datetime, timezone

DROPIN = "/etc/systemd/system/{unit}.service.d/zz-sdr.conf"
STATE = "/var/lib/nova-sdr/actuator.json"
HEAL_AFTER_S, HEAL_EVERY_S = 300, 1800
FM_CAPTURE = "/usr/bin/python3 /home/kochj/nova_fm_capture.py"
OPENED = {  # pipeline -> how to tell its capture really opened since the unit started
    "fm_master": r"\[fm-capture\] \S+ (DT|MA) mode:", "fm_dt": r"\[fm-capture\] \S+ (DT|MA) mode:",
    "nrsp": r"pynrsp capture up",
}
SDRPLAY = {"fm_master", "fm_dt", "p25_slave"}


def sh(*cmd, check=False):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=check).stdout.strip()


def clean(s):  # labels end up inside systemd quoting and fm_capture's ':'-separated args
    return re.sub(r'[:"\\]', "", str(s)).strip()


def render(pipeline, rows):
    """rows: [(tuner_port, target_dict_or_None, tuner_meta)] for one unit. Returns drop-in text or None (stop)."""
    live = [(port, t, meta) for port, t, meta in rows if t]
    if not live:
        return None
    head = "# Managed by nova_sdr_actuator from sdr_assignments -- edit the tables, not this file.\n[Service]\n"
    port, t, meta = live[0]
    m = t["meta"]
    if pipeline == "p25_rtl":
        return head + (f'Environment="SDR_FREQ={t["freq_hz"]/1e6:.4f}M" "SDR_LABEL={clean(t["label"])}" '
                       f'"SDR_NAC={clean(m.get("nac", ""))}"\n')
    if pipeline == "p25_slave":
        return head + (f'Environment="RSP_MODE=SL" "RSP_FREQ={t["freq_hz"]}" "RSP_LABEL={clean(t["label"])}" '
                       f'"RSP_NAC={clean(m.get("nac", ""))}" "RSP_LOC={clean(m.get("loc", ""))}"\n')
    if pipeline == "nrsp":
        return head + (f'Environment="NRSP_FREQ={t["freq_hz"]/1e6:.4f}" "NRSP_DEMOD={t["demod"]}" "NRSP_ANTENNA={port}" '
                       f'"NRSP_SOURCE={clean(m.get("source", t["id"]))}" "NRSP_LABEL={clean(t["label"])}" '
                       f'"NRSP_KIND={clean(m.get("kind", "scanner"))}" "NRSP_TAG={clean(m.get("tag", t["demod"] + " voice"))}" '
                       f'"NRSP_LOC={clean(m.get("loc", ""))}"\n')
    if pipeline in ("fm_master", "fm_dt"):
        lab = lambda t: clean(t["label"]).replace(" ", "_")
        if pipeline == "fm_dt" and len(live) == 2:
            # dual-tuner mode maps argument ORDER to tuner (1st arg = Tuner 1); the number is only a marker
            live.sort(key=lambda r: r[0])
            args = " ".join(f'"{lab(t)}:{t["freq_hz"]}:{p}:{t["demod"]}"' for p, t, _ in live)
        else:
            ant = port if pipeline == "fm_master" else f"Tuner {port} 50 ohm"
            args = f'"{lab(t)}:{t["freq_hz"]}:{ant}:{t["demod"]}"'
        return head + f'ExecStart=\nExecStart={FM_CAPTURE} {meta["serial"]} {meta["outdir"]} {args}\n'
    raise ValueError(pipeline)


def unit_health(unit, pipeline, assigned):
    if not assigned:
        return True, "idle (no target)"
    props = dict(l.split("=", 1) for l in sh("systemctl", "show", unit, "-p", "ActiveState",
                                               "-p", "ActiveEnterTimestamp").splitlines() if "=" in l)
    if props.get("ActiveState") != "active":
        return False, f"unit {props.get('ActiveState')}"
    if pipeline == "p25_slave":
        up = ":7355 " in sh("ss", "-tln")
        return up, "bridge listening on :7355" if up else "bridge not listening on :7355"
    if pipeline == "p25_rtl":
        up = bool(sh("pgrep", "-f", "dsd-fme.*-i rtl"))
        return up, "dsd-fme on rtl running" if up else "dsd-fme not running"
    since = props.get("ActiveEnterTimestamp", "").split(" ", 1)[-1].rsplit(" ", 1)[0]
    log = sh("journalctl", "-u", unit, "--since", since, "--no-pager", "-o", "cat")
    up = bool(re.search(OPENED[pipeline], log))
    return up, "capture opened" if up else f"no capture since start ({since})"


def main(dry):
    import psycopg2
    try:
        import nova_dsn; dsn = nova_dsn.pg_dsn("nova_ops")
    except ImportError:
        dsn = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
    con = psycopg2.connect(dsn); con.autocommit = True; cur = con.cursor()
    host = socket.gethostname()
    cur.execute("""SELECT u.id, u.pipeline, u.unit, u.port, u.meta, u.requires,
                          t.id, t.label, t.freq_hz, t.demod, t.meta
                   FROM sdr_tuners u LEFT JOIN sdr_assignments a ON a.tuner_id = u.id
                   LEFT JOIN sdr_targets t ON t.id = a.target_id AND t.enabled
                   WHERE u.host = %s AND u.enabled ORDER BY u.id""", (host,))
    units = {}
    for tid, pipe, unit, port, meta, req, gid, label, hz, demod, gmeta in cur.fetchall():
        target = {"id": gid, "label": label, "freq_hz": hz, "demod": demod, "meta": gmeta or {}} if gid else None
        units.setdefault(unit, {"pipeline": pipe, "rows": [], "tuners": [], "requires": set()})
        units[unit]["rows"].append((port, target, meta or {}))
        units[unit]["tuners"].append(tid)
        if req:
            units[unit]["requires"].add(req)
    if not units:
        print(f"[sdr-actuator] {host}: no tuners"); return 0
    try:
        state = json.load(open(STATE))
    except Exception:
        state = {}
    now = time.time()

    # 1) health
    sick_sdrplay = False
    for unit, u in units.items():
        assigned = any(t for _, t, _ in u["rows"])
        ok, detail = unit_health(unit, u["pipeline"], assigned)
        if not ok and u["pipeline"] in SDRPLAY:
            first = state.setdefault("sick_since", {}).setdefault(unit, now)
            sick_sdrplay |= now - first >= HEAL_AFTER_S
        else:
            state.setdefault("sick_since", {}).pop(unit, None)
        print(f"[sdr-actuator] {unit:22s} {'OK  ' if ok else 'SICK'} {detail}")
        if not dry:
            for tid in u["tuners"]:
                cur.execute("""INSERT INTO sdr_health(tuner_id, ts, ok, detail, unhealthy_since)
                               VALUES (%s, now(), %s, %s, CASE WHEN %s THEN NULL ELSE now() END)
                               ON CONFLICT (tuner_id) DO UPDATE SET ts=now(), ok=EXCLUDED.ok, detail=EXCLUDED.detail,
                                 unhealthy_since = CASE WHEN EXCLUDED.ok THEN NULL
                                                        ELSE COALESCE(sdr_health.unhealthy_since, now()) END""",
                            (tid, ok, detail, ok))

    # 2) self-heal a wedged SDRplay API (the 2026-10-12 failure on both hosts)
    if sick_sdrplay and now - state.get("last_heal", 0) >= HEAL_EVERY_S:
        print("[sdr-actuator] SDRplay capture stuck >5 min -> restarting sdrplay.service + Duo units")
        if not dry:
            state["last_heal"] = now
            sh("systemctl", "restart", "sdrplay.service"); time.sleep(5)
            if os.path.exists("/home/kochj/rsp_usbreset.sh"):
                sh("/home/kochj/rsp_usbreset.sh"); time.sleep(5)
            for unit in sorted(units, key=lambda x: bool(units[x]["requires"])):   # masters first
                if units[unit]["pipeline"] in SDRPLAY and any(t for _, t, _ in units[unit]["rows"]):
                    sh("systemctl", "restart", unit); time.sleep(10)

    # 3) apply assignments, masters before slaves
    changed, reload = [], False
    for unit in sorted(units, key=lambda x: bool(units[x]["requires"])):
        u = units[unit]
        want = render(u["pipeline"], u["rows"])
        path = DROPIN.format(unit=unit)
        have = open(path).read() if os.path.exists(path) else None
        if want is None:
            if sh("systemctl", "is-active", unit) == "active":
                print(f"[sdr-actuator] {unit}: no target assigned -> stop")
                if not dry:
                    sh("systemctl", "stop", unit)
            continue
        if want != have:
            print(f"[sdr-actuator] {unit}: config change ->\n{want}")
            if not dry:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "w").write(want); reload = True
            changed.append(unit)
        elif sh("systemctl", "is-active", unit) != "active":
            changed.append(unit)                      # assigned but stopped -> start it
    master_units = {units[x]["tuners"][0] for x in changed}
    for unit in list(units):                          # a slave restarts when its master did
        if units[unit]["requires"] & master_units and unit not in changed and units[unit]["rows"][0][1]:
            changed.append(unit)
    if not dry:
        if reload:
            sh("systemctl", "daemon-reload")
        for unit in sorted(changed, key=lambda x: bool(units[x]["requires"])):
            print(f"[sdr-actuator] restarting {unit}")
            sh("systemctl", "restart", unit)
            if not units[unit]["requires"]:
                time.sleep(15)                        # let a master open before its slave starts
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        json.dump(state, open(STATE, "w"))
    return 0


if __name__ == "__main__":
    sys.exit(main("--dry-run" in sys.argv))
