#!/usr/bin/env python3
"""nova_sdr_actuator.py — apply sdr_assignments to this host's capture units + report tuner health.

Runs as root every minute (nova-sdr-actuator.timer) on each radio host (.86 nova-core2, .5 nova-core3).
For every tuner on this host:
  * health: unit active AND its capture actually opened since the last start (an "active" unit
    stuck in a wedged SDRplay API captured nothing for days on 2026-10-12) -> sdr_health
  * apply: renders /etc/systemd/system/<unit>.service.d/zz-sdr.conf from the assigned target;
    restarts only units whose config changed, master before slave; stops units with no target
  * self-heal: an SDRplay unit unhealthy >5 min -> restart sdrplay.service (max once / 30 min)
  * deep health (2026-10-15, plan #6) -- folded into the same ok/sick, so the heal and the placer act on it:
      stream   nova_fm_capture's per-channel heartbeat (STATS_DIR/<label>.json): IQ samples in the last 30 s,
               stale file = hung capture. Live SNR (channel vs band median) -> sdr_health.snr_db.
      output   transcripts per channel in nova_memories vs that channel's own 7-day rate: silent long enough
               that >= OUTPUT_EXPECT calls were due (and >= 30 min) = sick. Caught the core2 P25 slave
               (LAPD North Hollywood, 819 calls/day) silent from 10:16 to 17:35 on 10-15 with "bridge listening".
      api      SDRplay API liveness, passive: sdrplay.service active, apiService not zombie/uninterruptible,
               no ServiceNotResponding / segfault / repeated submiturb failures in the last 10 min.
               (No active enumerate: opening the API from a probe can itself wedge it.)
    Every tuner (+ 'sdrplay_api' per host) is also written to health_checks (checked_by nova_sdr_actuator).
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
STATS_DIR = "/home/kochj/.cache/nova-sdr"      # nova_fm_capture.py writes <label>.json here (User=kochj units)
STATS_STALE_S = 180
OUTPUT_EXPECT = 20                             # calls that should have arrived during the silence
OUTPUT_SILENCE_MIN_S = 1800
OUTPUT_MIN_HISTORY = 50                        # calls in 7 days before a channel's silence is judged at all
API_BAD = re.compile(r"ServiceNotResponding|segfault at .* in sdrplay_apiService|sdrplay_api_\w+ (failed|Fail)", re.I)
API_URB = re.compile(r"submiturb failed")


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
        # ExecStartPre= clears the legacy usbreset.conf: only the MASTER may reset the Duo -- a slave
        # resetting it yanks the device out from under the running master (2026-10-12).
        return head + ('ExecStartPre=\n'
                       f'Environment="RSP_MODE=SL" "RSP_FREQ={t["freq_hz"]}" "RSP_LABEL={clean(t["label"])}" '
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
    if not up and "Started " not in log:
        # The journal rotated past this start, so the "opened" line is gone too: no evidence either way.
        # 2026-10-15 on core7 (ollama log spam) this read as sick after ~12 h and healed a working capture.
        return True, "capture assumed open (start log rotated)"
    return up, "capture opened" if up else f"no capture since start ({since})"


def lab(label):
    return clean(label).replace(" ", "_")


def stream_health(label, now=None, stats_dir=STATS_DIR):
    """(ok|None, detail, snr_db) from nova_fm_capture's heartbeat. None = no heartbeat file (older capture code)."""
    now = now or time.time()
    try:
        st = json.load(open(os.path.join(stats_dir, f"{lab(label)}.json")))
    except (OSError, ValueError):
        return None, "", None
    age = now - float(st.get("ts", 0))
    snr = st.get("snr_db")
    if age > STATS_STALE_S:
        return False, f"capture heartbeat stale {age / 60:.0f} min (hung)", None
    if not st.get("samples"):
        return False, "no IQ samples in the last 30 s (stream stalled)", None
    return True, f"IQ flowing, SNR {snr} dB" if snr is not None else "IQ flowing", snr


def output_rates(dsn):
    """{channel label: (calls_7d, calls_1h, last_ts, first_ts)} (epoch s) from nova_memories; {} when unreachable."""
    try:
        import psycopg2
        with psycopg2.connect(dsn, connect_timeout=10) as c, c.cursor() as cur:
            cur.execute("""SELECT metadata->>'channel', count(*), count(*) FILTER (WHERE created_at > now() - interval '1 hour'),
                                  extract(epoch FROM max(created_at)), extract(epoch FROM min(created_at))
                           FROM memories WHERE source IN ('scanner', 'rf_discovery') AND created_at > now() - interval '7 days'
                           GROUP BY 1""")
            return {r[0]: (int(r[1]), int(r[2]), float(r[3]), float(r[4])) for r in cur.fetchall() if r[0]}
    except Exception as e:  # noqa: BLE001 -- the memory DB being down must not mark every tuner sick
        print(f"[sdr-actuator] output rates unavailable: {e}")
        return {}


def output_health(label, rates, assigned_since, now=None):
    """(ok|None, detail, calls_1h). None = not judged (no history, or too quiet a channel to tell)."""
    now = now or time.time()
    if label not in rates:
        return None, "", None
    n7, n1h, last, first = rates[label]
    silent_s = now - max(last, assigned_since or 0)
    # rate over the span the channel was actually producing (a channel added 2 days ago is not 7 days quiet)
    expected = n7 / max(last - first, 3600) * silent_s
    if n7 >= OUTPUT_MIN_HISTORY and silent_s >= OUTPUT_SILENCE_MIN_S and expected >= OUTPUT_EXPECT:
        return False, f"no output for {silent_s / 3600:.1f} h (~{expected:.0f} calls were due)", n1h
    return True, f"{n1h} calls/h", n1h


def api_health(journal=None, ps=None, active=None):
    """SDRplay API liveness, passive. Arguments are for tests; live calls read systemd/ps/journal."""
    active = sh("systemctl", "is-active", "sdrplay") if active is None else active
    if active != "active":
        return False, f"sdrplay.service {active}"
    ps = sh("ps", "-o", "stat=", "-C", "sdrplay_apiService") if ps is None else ps
    if not ps.strip():
        return False, "sdrplay_apiService not running"
    if any(st.startswith(("Z", "D")) for st in ps.split()):
        return False, f"sdrplay_apiService state {ps.split()[0]}"
    # no -u/-k filters: journalctl ANDs different match fields, and the segfault line is a kernel message
    journal = sh("journalctl", "--since", "-10 min", "--no-pager", "-o", "cat",
                 "-g", "sdrplay|submiturb|ServiceNotResponding") if journal is None else journal
    # skip our own "[sdr-actuator] sdrplay api: SICK API error: ..." lines: re-reading them kept the API
    # "sick" forever, each run quoting the last one (core3, 10-28..10-30)
    bad = [l for l in journal.splitlines() if API_BAD.search(l) and "[sdr-actuator]" not in l]
    if bad:
        return False, f"API error: {bad[-1].strip()[:120]}"
    urb = len(API_URB.findall(journal))
    if urb >= 3:
        return False, f"{urb} USB submiturb failures in 10 min (stream dying)"
    return True, "API responding"


def combine(base, *checks):
    """base=(ok, detail); checks=(ok|None, detail). Sick if any judged check is sick; details joined."""
    ok, parts = base[0], [base[1]]
    for c_ok, c_detail in checks:
        if c_ok is False:
            ok = False
        if c_detail:
            parts.append(c_detail)
    return ok, "; ".join(p for p in parts if p)


def main(dry):
    import psycopg2
    try:
        import nova_dsn; dsn = nova_dsn.pg_dsn("nova_ops")
    except ImportError:
        dsn = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
    con = psycopg2.connect(dsn); con.autocommit = True; cur = con.cursor()
    host = socket.gethostname()
    cur.execute("""SELECT u.id, u.pipeline, u.unit, u.port, u.meta, u.requires,
                          t.id, t.label, t.freq_hz, t.demod, t.meta, extract(epoch FROM a.since)
                   FROM sdr_tuners u LEFT JOIN sdr_assignments a ON a.tuner_id = u.id
                   LEFT JOIN sdr_targets t ON t.id = a.target_id AND t.enabled
                   WHERE u.host = %s AND u.enabled ORDER BY u.id""", (host,))
    units = {}
    for tid, pipe, unit, port, meta, req, gid, label, hz, demod, gmeta, since in cur.fetchall():
        target = {"id": gid, "label": label, "freq_hz": hz, "demod": demod, "meta": gmeta or {},
                  "since": float(since) if since else None} if gid else None
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

    # 1) health: unit/capture + stream heartbeat + output rate + SDRplay API (all fold into ok/sick)
    sick_sdrplay = False
    has_sdrplay = any(u["pipeline"] in SDRPLAY for u in units.values())
    api = api_health() if has_sdrplay else (True, "")
    rates = output_rates(dsn.replace("dbname=nova_ops", "dbname=nova_memories"))
    deep = {}                                          # tuner id -> (ok, detail, snr, calls_1h)
    for unit, u in units.items():
        assigned = any(t for _, t, _ in u["rows"])
        base = unit_health(unit, u["pipeline"], assigned)
        for (port, t, _), tid in zip(u["rows"], u["tuners"]):
            if not t:
                deep[tid] = (base[0], base[1], None, None)
                continue
            s_ok, s_detail, snr = stream_health(t["label"]) if u["pipeline"] in ("fm_master", "fm_dt") else (None, "", None)
            o_ok, o_detail, n1h = output_health(t["label"], rates, t.get("since"))
            a = api if u["pipeline"] in SDRPLAY else (None, "")
            ok_t, detail_t = combine(base, (s_ok, s_detail), (o_ok, o_detail), (a[0], "" if a[0] else a[1]))
            deep[tid] = (ok_t, detail_t, snr, n1h)
        ok = all(deep[tid][0] for tid in u["tuners"])
        detail = " | ".join(deep[tid][1] for tid in u["tuners"])
        if not ok and u["pipeline"] in SDRPLAY:
            first = state.setdefault("sick_since", {}).setdefault(unit, now)
            # A stuck MASTER/DT capture means a wedged API. A sick slave gets its own restarts first, but
            # if those fail for 3x as long the API is wedged for it too: 2026-10-15 the slave retried
            # sdrplay_api_Open for 3 h after a master restart, and only an sdrplay.service restart fixed it.
            sick_sdrplay |= now - first >= HEAL_AFTER_S * (3 if u["requires"] else 1)
        else:
            state.setdefault("sick_since", {}).pop(unit, None)
        print(f"[sdr-actuator] {unit:22s} {'OK  ' if ok else 'SICK'} {detail}")
        if not dry:
            for tid in u["tuners"]:
                t_ok, t_detail, snr, n1h = deep[tid]
                cur.execute("""INSERT INTO sdr_health(tuner_id, ts, ok, detail, unhealthy_since)
                               VALUES (%s, now(), %s, %s, CASE WHEN %s THEN NULL ELSE now() END)
                               ON CONFLICT (tuner_id) DO UPDATE SET ts=now(), ok=EXCLUDED.ok, detail=EXCLUDED.detail,
                                 unhealthy_since = CASE WHEN EXCLUDED.ok THEN NULL
                                                        ELSE COALESCE(sdr_health.unhealthy_since, now()) END""",
                            (tid, t_ok, t_detail[:500], t_ok))
                try:   # columns added 2026-10-15; an older schema must not break basic health
                    cur.execute("UPDATE sdr_health SET snr_db=%s, calls_1h=%s WHERE tuner_id=%s", (snr, n1h, tid))
                    cur.execute("""INSERT INTO health_checks (service_name, node_name, checked_by, status, error_message)
                                   VALUES (%s, %s, 'nova_sdr_actuator', %s, %s)""",
                                (f"sdr:{tid}", host, "up" if t_ok else "down", None if t_ok else t_detail[:500]))
                except Exception as e:  # noqa: BLE001
                    print(f"[sdr-actuator] deep-health write skipped: {e}")

    if has_sdrplay and not dry:
        try:
            cur.execute("""INSERT INTO health_checks (service_name, node_name, checked_by, status, error_message)
                           VALUES ('sdrplay_api', %s, 'nova_sdr_actuator', %s, %s)""",
                        (host, "up" if api[0] else "down", None if api[0] else api[1]))
        except Exception as e:  # noqa: BLE001
            print(f"[sdr-actuator] api health write skipped: {e}")
    print(f"[sdr-actuator] sdrplay api: {'OK' if api[0] else 'SICK'} {api[1]}")

    # 2) self-heal a wedged SDRplay API (the 2026-10-12 failure on both hosts)
    if sick_sdrplay and now - state.get("last_heal", 0) >= HEAL_EVERY_S:
        print("[sdr-actuator] SDRplay capture stuck >5 min -> restarting sdrplay.service + Duo units")
        if not dry:
            state["last_heal"] = now
            duo_units = [u for u in units if units[u]["pipeline"] in SDRPLAY]
            for unit in duo_units:                    # release the Duo first: killing the API under an open
                sh("systemctl", "stop", unit)         # stream left it "no DT-mode RSPduo found" (10-30)
            sh("systemctl", "kill", "-s", "KILL", "sdrplay.service"); time.sleep(2)   # a wedged service ignores SIGTERM
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
        elif sh("systemctl", "is-active", unit) in ("inactive", "failed"):
            changed.append(unit)                      # assigned but stopped -> start it (not 'activating')
        elif u["requires"] and unit in state.get("sick_since", {}) and now - state["sick_since"][unit] >= HEAL_AFTER_S:
            changed.append(unit)                      # sick slave: restart just the slave
            state["sick_since"][unit] = now
    # ...and a slave restart is a full API reset: when a slave lets go, sdrplay_apiService stops responding
    # (ServiceNotResponding) while the master keeps streaming, and nothing reattaches until
    # sdrplay.service, then the master, then the slave restart (2026-10-15, RSPduo 180603A532).
    reset_api = any(units[x]["requires"] for x in changed)
    for unit in [x for x in changed if units[x]["requires"]]:
        for m in units:
            if set(units[m]["tuners"]) & units[unit]["requires"] and m not in changed and any(t for _, t, _ in units[m]["rows"]):
                changed.append(m)
    master_units = {units[x]["tuners"][0] for x in changed}
    for unit in list(units):                          # a slave restarts when its master did
        if units[unit]["requires"] & master_units and unit not in changed and units[unit]["rows"][0][1]:
            changed.append(unit)
    if not dry:
        if reload:
            sh("systemctl", "daemon-reload")
        if reset_api:
            print("[sdr-actuator] slave restart -> resetting sdrplay.service first")
            for unit in changed:
                sh("systemctl", "stop", unit)
            sh("systemctl", "kill", "-s", "KILL", "sdrplay.service"); time.sleep(2)   # a wedged service ignores SIGTERM
            sh("systemctl", "restart", "sdrplay.service"); time.sleep(5)
        for unit in sorted(changed, key=lambda x: bool(units[x]["requires"])):
            print(f"[sdr-actuator] restarting {unit}")
            sh("systemctl", "restart", unit)
            if not units[unit]["requires"] and units[unit]["pipeline"] in OPENED:
                # a slave can only attach once its master has OPENED (usbreset + open can take >15 s)
                for _ in range(45):
                    if unit_health(unit, units[unit]["pipeline"], True)[0]:
                        break
                    time.sleep(2)
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        json.dump(state, open(STATE, "w"))
    return 0


if __name__ == "__main__":
    sys.exit(main("--dry-run" in sys.argv))
