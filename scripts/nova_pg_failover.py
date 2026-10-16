#!/usr/bin/env python3
"""nova_pg_failover.py — one-command, human-triggered Postgres failover.

DELIBERATELY NOT a fully-automatic daemon. A hand-rolled auto-promote-on-death
watchdog risks split-brain (both the old and new primary accepting writes) if it
misjudges a network blip as a dead primary — a worse failure mode than the
current safe-but-manual status quo. This tool does every mechanical step
correctly and fast once a human confirms the primary is actually down; it does
not decide that on its own.

REAL CURRENT TOPOLOGY (verified 2026-07-19, NOT what the stale service_registry
row says — that still claims ".6 is primary", which hasn't been true for weeks):
  - nova-core (192.168.1.2) is the actual Postgres primary (pg_is_in_recovery=false,
    has a real streaming replica connected).
  - nova-core5 (192.168.1.10) is its one streaming standby (pg_is_in_recovery=true,
    ~2ms replication lag as of last check).
  - mac-studio (.6) runs NO local Postgres at all — port 5432 there is 100%
    pgbouncer, forwarding every database to nova-core. This is the one real,
    load-bearing indirection point: repointing it is most of what "failover"
    actually means for the fleet, since scripts hitting .6:5432 (local or
    remote) never talk to Postgres directly.
  - Other hosts' pgbouncer installs (tv-movies-mini, nova-core4) were checked
    and found unconfigured/inactive — not load-bearing, not touched here.

Usage:
  nova_pg_failover.py check      — read-only status report, safe to run anytime.
  nova_pg_failover.py promote    — the real thing. Requires --confirm and refuses
                                    to run if nova-core still answers.
  nova_pg_failover.py promote --dry-run
                                 — evaluate every gate read-only and print the plan
                                    (plus wal_log_hints / max_slot_wal_keep_size /
                                    data_checksums per node). Changes nothing.
  nova_pg_failover.py check --settings
                                 — the status report plus the per-node settings.

HARD GATES before any promote / pgbouncer / DNS repoint (#43, 2026-10-15). Config lives in
service_config (service='pg_failover'), read from the primary or, when it is down, from the
candidate standby's replicated copy:
  1. WITNESS  key 'witness' {host, name, power_independent, pg_ports}. A third voter on
     independent power must itself be reachable AND unable to reach the primary's PG ports.
     Unreachable witness, or one that still reaches the primary (a partition, not a dead
     primary), or power_independent not set true => refuse.
  2. FENCE    key 'fence' {ssh_stop_cmd, verify_stopped_cmd, plug_entity, power_cut}. Stop PG on
     the old primary over SSH and VERIFY it is stopped. Optional smart-plug power cut
     (power_cut: if_ssh_fails | always | never) goes ONLY through nova_safety_guards
     (kill switch + physical_guard, Proteus); no plug configured => the SSH stop must verify.
  3. LSN      key 'lsn' {controldata_cmd, max_behind_bytes}. Candidate pg_last_wal_receive_lsn
     must be STRICTLY greater than the stopped primary's 'Latest checkpoint location'
     (pg_controldata — the 2026-09-28 switchover lesson). If that is unreadable the loss window
     is unknown; a peer standby that received more WAL also refuses. --accept-loss overrides.
  key 'nodes' [{name, host, psql}] lists the nodes for the settings report and peer check.
  nova_pg_failover.py unfence    — clear the fence marker once nova-core has been
                                    manually verified safe to rejoin (as a NEW
                                    standby, rebuilt via pg_basebackup — never
                                    just restarted in place, its WAL history has
                                    diverged from the moment nova-core5 was
                                    promoted).
"""
import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

PRIMARY_IP = "192.168.1.2"
PRIMARY_NAME = "nova-core"
STANDBY_IP = "192.168.1.10"
STANDBY_NAME = "nova-core5"
PGBOUNCER_INI = "/opt/homebrew/etc/pgbouncer.ini"
FENCE_MARKER = Path.home() / ".openclaw/workspace/state/pg_primary_fenced.json"
TSIG_KEY_NAME = "nova-dns-key"
DOMAIN = "digitalnoise.net"


def log(m):
    print(f"[pg-failover] {m}", flush=True)


def ssh(host, cmd, timeout=15):
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", f"kochj@{host}", cmd],
                        capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def check_primary_alive():
    """Two independent signals, not one: does nova-core's Postgres actually
    answer, AND is the host itself reachable at all. A dead Postgres process
    on a live host and a dead host entirely are different failure modes but
    both mean "can't serve writes" for this tool's purposes."""
    pg_rc, pg_out, _ = ssh(PRIMARY_IP, "psql -h 127.0.0.1 -U kochj -d nova_ops -tAc 'SELECT pg_is_in_recovery();'", timeout=8)
    pg_alive = (pg_rc == 0 and pg_out.strip() == "f")
    host_rc, _, _ = ssh(PRIMARY_IP, "true", timeout=8)
    host_alive = (host_rc == 0)
    return pg_alive, host_alive


def check_standby_health():
    rc, out, err = ssh(STANDBY_IP, "psql -h 127.0.0.1 -U kochj -d nova_ops -tAc 'SELECT pg_is_in_recovery();'", timeout=8)
    in_recovery = (rc == 0 and out.strip() == "t")
    lag_rc, lag_out, _ = ssh(PRIMARY_IP,
        "psql -h 127.0.0.1 -U kochj -d nova_ops -tAc \"SELECT COALESCE(replay_lag::text,'0') FROM pg_stat_replication WHERE client_addr='192.168.1.10'\"",
        timeout=8)
    return in_recovery, (lag_out.strip() if lag_rc == 0 else "unknown"), err


# ── #43 hard gates: witness, fence, LSN ─────────────────────────────────────

CFG_SERVICE = "pg_failover"
DEFAULT_FENCE = {
    "ssh_stop_cmd": "sudo -n systemctl stop postgresql-17.service",
    "verify_stopped_cmd": "! systemctl is-active --quiet postgresql-17.service && ! pgrep -x postgres >/dev/null",
    "plug_entity": None,
    "power_cut": "if_ssh_fails",
}
DEFAULT_LSN = {"controldata_cmd": None, "max_behind_bytes": 16 * 1024 * 1024}
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_LSN_RE = re.compile(r"^([0-9A-Fa-f]{1,8})/([0-9A-Fa-f]{1,8})$")
PSQL = "psql -h 127.0.0.1 -U kochj -d nova_ops -tAc"
SETTINGS_SQL = ("SELECT current_setting('wal_log_hints') || '|' || current_setting('max_slot_wal_keep_size') "
                "|| '|' || current_setting('data_checksums') || '|' || pg_is_in_recovery()::text")


def lsn_int(lsn):
    """'16/B374D848' -> int bytes (None if unparseable)."""
    m = _LSN_RE.match((lsn or "").strip())
    return (int(m.group(1), 16) << 32) + int(m.group(2), 16) if m else None


def _safe_host(h):
    if not h or not _HOST_RE.match(str(h)):
        raise ValueError(f"refusing unsafe host value {h!r}")
    return str(h)


def node_sql(host, sql, psql=PSQL, timeout=10):
    """Run one read-only query on a node over SSH. -> (ok, output)."""
    rc, out, err = ssh(_safe_host(host), f"{psql} {shlex.quote(sql)}", timeout=timeout)
    return rc == 0, (out if rc == 0 else (err or out or f"rc={rc}"))


def load_failover_cfg(_connect=None, _ssh_sql=None):
    """service_config pg_failover.* from the primary; when it is down, from the candidate
    standby (service_config replicates). Missing everywhere -> {} (gates then refuse)."""
    rows = None
    try:
        if _connect is None:
            import psycopg2
            import nova_dsn
            _connect = lambda: psycopg2.connect(nova_dsn.pg_dsn("nova_ops"), connect_timeout=5)  # noqa: E731
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM service_config WHERE service=%s", (CFG_SERVICE,))
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — primary down is the normal case here
        log(f"config from primary unavailable ({str(e)[:80]}); trying the standby's copy")
        ok, out = (_ssh_sql or node_sql)(
            STANDBY_IP, f"SELECT json_object_agg(key, value) FROM service_config WHERE service='{CFG_SERVICE}'")
        if ok and out.strip():
            try:
                return json.loads(out)
            except ValueError:
                pass
        return {}
    return {k: (json.loads(v) if isinstance(v, str) else v) for k, v in rows}


def witness_gate(cfg, primary_host=PRIMARY_IP, dry_run=False):
    """(ok, detail). Read-only. The witness must be up and must NOT reach the primary."""
    w = cfg.get("witness") or {}
    if not w.get("host"):
        return False, "no witness configured (service_config pg_failover.witness)"
    try:
        host, phost = _safe_host(w["host"]), _safe_host(primary_host)
        ports = [int(p) for p in (w.get("pg_ports") or [5432])]
    except (ValueError, TypeError) as e:
        return False, f"witness config invalid: {e}"
    name = w.get("name") or host
    rc, _, _ = ssh(host, "true", timeout=10)
    if rc != 0:
        return False, f"witness {name} unreachable — no third vote (we may be the partitioned side)"
    reach = []
    for port in ports:
        rc, out, _ = ssh(host, f"timeout 5 bash -c 'exec 3<>/dev/tcp/{phost}/{port}' 2>/dev/null "
                               f"&& echo open || echo closed", timeout=15)
        if rc == 0 and out.strip().endswith("open"):
            reach.append(port)
    if reach:
        return False, (f"witness {name} STILL reaches the primary on {reach} — a partition, not a dead "
                       f"primary. Refusing (split-brain).")
    if w.get("power_independent") is not True:
        return False, (f"witness {name} cannot reach the primary, but its power independence is unverified "
                       f"(set pg_failover.witness.power_independent=true once checked)")
    return True, f"witness {name} (independent power) is up and cannot reach the primary on {ports}"


def _ha_plug_off(entity, _urlopen=None):
    """switch.turn_off through Home Assistant, then read the state back. -> (ok, detail)."""
    import urllib.request
    import nova_drawbridge as D
    tok = D._ha_token()
    hdr = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    op = _urlopen or urllib.request.urlopen
    op(urllib.request.Request(f"{D.HA_URL}/api/services/switch/turn_off",
                              data=json.dumps({"entity_id": entity}).encode(), headers=hdr), timeout=10).read()
    time.sleep(3)
    st = json.loads(op(urllib.request.Request(f"{D.HA_URL}/api/states/{entity}", headers=hdr), timeout=10).read())
    return st.get("state") == "off", f"plug {entity} state={st.get('state')}"


def _power_cut(entity, confirmation_id=None, _guards=None, _plug_off=None):
    """Smart-plug power cut ONLY via the Proteus physical path. -> (ok, detail)."""
    try:
        G = _guards
        if G is None:
            import nova_safety_guards as G
    except Exception as e:  # noqa: BLE001 — no guards, no actuation
        return False, f"safety guards unavailable ({e}) — power cut refused"
    if G.kill_engaged():
        return False, "autonomy kill switch engaged — power cut refused"
    action = f"power-cut the old Postgres primary {PRIMARY_NAME} via smart plug {entity} (pg failover fence)"
    ok, why = G.physical_guard(action, entity_ids=[entity], source="nova_pg_failover",
                               confirmation_id=confirmation_id)
    if not ok:
        try:
            G.report_block(None, source="nova_pg_failover", action=action, reason=why, guard="physical")
        except Exception:  # noqa: BLE001
            pass
        return False, f"physical_guard refused: {why}"
    try:
        return (_plug_off or _ha_plug_off)(entity)
    except Exception as e:  # noqa: BLE001
        return False, f"plug actuation failed: {e}"


def fence_gate(cfg, primary_host=PRIMARY_IP, dry_run=False, confirmation_id=None, _power=None):
    """(ok, detail, method). Stop PG on the old primary and VERIFY it; optional plug power cut."""
    f = {**DEFAULT_FENCE, **(cfg.get("fence") or {})}
    plug, mode = f.get("plug_entity"), f.get("power_cut") or "if_ssh_fails"
    host = _safe_host(primary_host)
    if dry_run:
        rc, _, _ = ssh(host, f["verify_stopped_cmd"], timeout=10)
        state = "already stopped" if rc == 0 else "running or unreachable"
        return True, (f"WOULD run `{f['ssh_stop_cmd']}` on {PRIMARY_NAME} (PG now {state}), verify with "
                      f"`{f['verify_stopped_cmd']}`; plug={plug or 'none'} power_cut={mode}"), "plan"
    rc, _, err = ssh(host, f["ssh_stop_cmd"], timeout=60)
    vrc, _, _ = ssh(host, f["verify_stopped_cmd"], timeout=15)
    ssh_ok = vrc == 0
    detail = f"ssh stop rc={rc}{' (' + err[:80] + ')' if rc and err else ''}, verified stopped={ssh_ok}"
    if plug and mode != "never" and (mode == "always" or not ssh_ok):
        pok, pdetail = (_power or _power_cut)(plug, confirmation_id)
        if pok:
            return True, f"{detail}; {pdetail}", "ssh+plug" if ssh_ok else "plug"
        if not ssh_ok or mode == "always":
            return False, f"{detail}; {pdetail} — old primary NOT fenced, refusing", "none"
    if ssh_ok:
        return True, detail, "ssh"
    return False, (f"{detail}; no smart plug configured — the SSH stop must verify. Old primary NOT "
                   f"fenced, refusing (split-brain risk)"), "none"


def evaluate_lsn(candidate_receive, ref, ref_kind, max_behind_bytes, accept_loss=False, peer_best=None):
    """Pure decision. ref_kind 'checkpoint' = stopped primary's pg_controldata Latest checkpoint
    (candidate must be STRICTLY past it); None = unknown loss window. -> (ok, detail)."""
    c = lsn_int(candidate_receive)
    if c is None:
        return (accept_loss, f"candidate receive LSN unreadable ({candidate_receive!r})"
                + (" — --accept-loss" if accept_loss else " — refusing"))
    if peer_best is not None and peer_best[1] is not None and peer_best[1] - c > max_behind_bytes:
        gap = peer_best[1] - c
        return (accept_loss, f"peer {peer_best[0]} received {gap} bytes more WAL than the candidate"
                + (" — --accept-loss" if accept_loss else f" — refusing (promote {peer_best[0]} instead)"))
    r = lsn_int(ref) if ref_kind == "checkpoint" else None
    if r is None:
        return (accept_loss, "old primary's final checkpoint unreadable — loss window unknown"
                + (" — --accept-loss" if accept_loss else " — refusing (pass --accept-loss to accept it)"))
    if c > r:
        return True, f"candidate receive {candidate_receive} is past the primary's final checkpoint {ref}"
    return (accept_loss, f"candidate receive {candidate_receive} is NOT past the final checkpoint {ref} "
            f"({r - c} bytes behind; promoting would fork the timeline early)"
            + (" — --accept-loss" if accept_loss else " — refusing"))


def _candidate_lsns():
    ok, out = node_sql(STANDBY_IP, "SELECT pg_last_wal_receive_lsn()::text || '|' || "
                                   "pg_last_wal_replay_lsn()::text")
    return tuple((out.split("|") + [None])[:2]) if ok else (None, None)


def lsn_gate(cfg, fenced_by_ssh, accept_loss=False, dry_run=False, wait_s=30, _sleep=None):
    """Gather LSNs (read-only) and decide. The standby gets up to wait_s to receive the END of the
    stopped primary's shutdown checkpoint record. -> (ok, detail)."""
    lc = {**DEFAULT_LSN, **(cfg.get("lsn") or {})}
    recv, replay = _candidate_lsns()
    ref = None
    if fenced_by_ssh and lc.get("controldata_cmd"):
        rc, cd, _ = ssh(_safe_host(PRIMARY_IP), lc["controldata_cmd"], timeout=15)
        m = re.search(r"Latest checkpoint location:\s*([0-9A-Fa-f]+/[0-9A-Fa-f]+)", cd or "") if rc == 0 else None
        ref = m.group(1) if m else None
    peers = []
    for n in cfg.get("nodes") or []:
        h = n.get("host")
        if not h or h in (PRIMARY_IP, STANDBY_IP) or n.get("name") in (PRIMARY_NAME, STANDBY_NAME):
            continue
        pok, pout = node_sql(h, "SELECT pg_last_wal_receive_lsn()::text", psql=n.get("psql") or PSQL)
        peers.append((n.get("name") or h, lsn_int(pout) if pok else None))
    best = max((p for p in peers if p[1] is not None), key=lambda p: p[1], default=None)
    decide = lambda r: evaluate_lsn(r, ref, "checkpoint" if ref else None,  # noqa: E731
                                    int(lc.get("max_behind_bytes") or 0), accept_loss, best)
    okd, detail = decide(recv)
    waited = 0
    while not okd and ref and not dry_run and waited < wait_s:
        (_sleep or time.sleep)(5)
        waited += 5
        recv, replay = _candidate_lsns()
        okd, detail = decide(recv)
    note = " [dry run: the binding check runs after the fence]" if dry_run else ""
    return okd, f"{detail} (replay {replay}){note}"


def node_settings(cfg):
    """Read-only: wal_log_hints / max_slot_wal_keep_size / data_checksums / role per node."""
    nodes = cfg.get("nodes") or [{"name": PRIMARY_NAME, "host": PRIMARY_IP},
                                 {"name": STANDBY_NAME, "host": STANDBY_IP}]
    out = []
    for n in nodes:
        try:
            ok, res = node_sql(n["host"], SETTINGS_SQL, psql=n.get("psql") or PSQL)
        except ValueError as e:
            ok, res = False, str(e)
        if ok and res.count("|") == 3:
            hints, keep, sums, rec = res.split("|")
            role = "standby" if rec == "true" else "primary"
            warn = [w for w, bad in (("wal_log_hints off and no checksums: pg_rewind impossible",
                                      hints != "on" and sums != "on"),
                                     ("max_slot_wal_keep_size unlimited: a dead slot can fill the disk",
                                      keep in ("-1", "-1MB"))) if bad]
            out.append(f"{n.get('name', n['host'])}: {role} wal_log_hints={hints} max_slot_wal_keep_size={keep} "
                       f"data_checksums={sums}" + (f"  !! {'; '.join(warn)}" if warn else ""))
        else:
            out.append(f"{n.get('name', n.get('host'))}: unreadable ({str(res)[:80]})")
    return out


def run_gates(args, cfg, dry_run=False):
    """Witness -> fence -> LSN, in that order. Live: stops at the first failure (the fence is
    destructive, so it only runs once the witness agrees). Dry run: evaluates all read-only.
    -> (ok, [(gate, ok, detail)])."""
    results = []
    ok, d = witness_gate(cfg, dry_run=dry_run)
    results.append(("witness", ok, d))
    if not ok and not dry_run:
        return False, results
    fok, fd, method = fence_gate(cfg, dry_run=dry_run, confirmation_id=getattr(args, "plug_confirmation", None))
    results.append(("fence", fok, fd))
    if not fok and not dry_run:
        return False, results
    lok, ld = lsn_gate(cfg, fenced_by_ssh=method in ("ssh", "ssh+plug", "plan"),
                       accept_loss=getattr(args, "accept_loss", False), dry_run=dry_run)
    results.append(("lsn", lok, ld))
    return all(r[1] for r in results), results


def cmd_check(args):
    pg_alive, host_alive = check_primary_alive()
    print(f"nova-core ({PRIMARY_IP}) Postgres responding: {pg_alive}")
    print(f"nova-core ({PRIMARY_IP}) host reachable:       {host_alive}")
    if pg_alive:
        in_recovery, lag, err = check_standby_health()
        print(f"nova-core5 ({STANDBY_IP}) is standby:        {in_recovery}")
        print(f"nova-core5 replication lag:                 {lag}")
        if err:
            print(f"  (standby check stderr: {err})")
    fenced = FENCE_MARKER.exists()
    print(f"Old-primary fence active:                   {fenced}")
    if fenced:
        print(f"  {FENCE_MARKER.read_text()}")
    if not pg_alive and not host_alive:
        print("\n>>> nova-core appears genuinely down (both signals). "
              "Run with `promote --confirm` to fail over. <<<")
    elif not pg_alive and host_alive:
        print("\n>>> nova-core's HOST is reachable but Postgres isn't answering — "
              "this could be Postgres crashed (fixable in place, check it first) "
              "or a partial outage. Investigate before promoting. <<<")
    else:
        print("\nAll healthy. Nothing to do.")
    if getattr(args, "settings", False):
        print("\nPer-node settings (read-only):")
        for line in node_settings(load_failover_cfg()):
            print(f"  {line}")
    return 0


def cmd_dry_run(args):
    """Print the failover plan with every gate evaluated read-only. Never mutates anything."""
    print("PG FAILOVER DRY RUN — nothing will be changed")
    pg_alive, host_alive = check_primary_alive()
    print(f"  step 0  primary {PRIMARY_NAME}: postgres answering={pg_alive} host reachable={host_alive}"
          + ("  -> live run would ABORT here (primary is up)" if pg_alive else ""))
    cfg = load_failover_cfg()
    ok, results = run_gates(args, cfg, dry_run=True)
    for i, (gate, gok, detail) in enumerate(results, 1):
        print(f"  gate {i}  {gate:<8} {'PASS' if gok else 'FAIL'}  {detail}")
    print(f"  then    promote {STANDBY_NAME} -> repoint pgbouncer ({PGBOUNCER_INI}) -> DNS pg-primary "
          f"-> fence marker -> alert")
    print(f"  verdict: {'all gates pass' if ok and not pg_alive else 'a live run would REFUSE'}")
    print("Per-node settings (read-only):")
    for line in node_settings(cfg):
        print(f"  {line}")
    return 0


def cmd_promote(args):
    if getattr(args, "dry_run", False):
        return cmd_dry_run(args)
    if not args.confirm:
        log("Refusing to promote without --confirm. Run `check` first.")
        return 1

    log("Re-verifying nova-core is actually down before touching anything...")
    pg_alive, host_alive = check_primary_alive()
    if pg_alive:
        log("ABORT: nova-core's Postgres just answered successfully. "
            "It is NOT down. Refusing to promote — this would create split-brain.")
        return 1
    log(f"nova-core Postgres alive={pg_alive}, host alive={host_alive} — proceeding.")

    log("Hard gates: witness -> fence -> LSN (#43)...")
    gates_ok, gate_results = run_gates(args, load_failover_cfg())
    for gate, gok, detail in gate_results:
        log(f"  gate {gate}: {'PASS' if gok else 'FAIL'} — {detail}")
    if not gates_ok:
        log("ABORT: a failover gate refused. Nothing was promoted or repointed.")
        return 1
    gate_note = "; ".join(f"{g}: {d}" for g, _, d in gate_results)

    log(f"Promoting {STANDBY_NAME} ({STANDBY_IP})...")
    rc, out, err = ssh(STANDBY_IP, "psql -h 127.0.0.1 -U kochj -d nova_ops -tAc 'SELECT pg_promote();'", timeout=15)
    if rc != 0 or "t" not in out.lower():
        log(f"ABORT: promotion command failed or returned unexpected result: rc={rc} out={out!r} err={err!r}")
        return 1
    time.sleep(3)
    rc2, out2, _ = ssh(STANDBY_IP, "psql -h 127.0.0.1 -U kochj -d nova_ops -tAc 'SELECT pg_is_in_recovery();'", timeout=8)
    if rc2 != 0 or out2.strip() != "f":
        log(f"ABORT: {STANDBY_NAME} does not report as a writable primary after promote (got {out2!r}). "
            "Manual investigation needed — do NOT repoint traffic yet.")
        return 1
    log(f"{STANDBY_NAME} confirmed promoted (pg_is_in_recovery=false).")

    log(f"Repointing .6's pgbouncer ({PGBOUNCER_INI}) from {PRIMARY_IP} to {STANDBY_IP}...")
    ini = Path(PGBOUNCER_INI)
    text = ini.read_text()
    # Replace host AND port together: the two nodes serve on different ports
    # (nova-core's docker primary is host-port 5434, nova-core5's linuxbrew PG is 5432).
    # Host-only replacement left pgbouncer pointing at .10:5434 during the 2026-09-17 failover.
    new_text = text.replace(f"host={PRIMARY_IP} port=5434", f"host={STANDBY_IP} port=5432")
    new_text = new_text.replace(f"host={PRIMARY_IP}", f"host={STANDBY_IP}")
    if new_text == text:
        log(f"WARNING: no occurrences of host={PRIMARY_IP} found in pgbouncer.ini — "
            "check the file manually, it may have already been repointed or the format changed.")
    else:
        ini.write_text(new_text)
        # Full path: launchd/escalation contexts don't have /opt/homebrew/bin on PATH —
        # bare "brew" crashed the 2026-09-17 run right after promotion.
        subprocess.run(["/opt/homebrew/bin/brew", "services", "restart", "pgbouncer"], capture_output=True, timeout=30)
        log("pgbouncer.ini updated and service restarted.")

    log("Updating pg-primary.digitalnoise.net DNS record...")
    try:
        secret = subprocess.run(
            ["security", "find-generic-password", "-a", "nova", "-s", "nova-bind-tsig-key", "-w"],
            capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        script = (f"server 192.168.1.138\nzone {DOMAIN}.\n"
                  f"update delete pg-primary.{DOMAIN}. A\n"
                  f"update add pg-primary.{DOMAIN}. 60 A {STANDBY_IP}\nsend\n")  # 60s TTL (queue #2656)
        r = subprocess.run(["nsupdate", "-y", f"hmac-sha256:{TSIG_KEY_NAME}:{secret}"],
                            input=script, capture_output=True, text=True, timeout=15)
        log("DNS updated." if r.returncode == 0 else f"DNS update failed (non-fatal, fix manually): {r.stderr[:200]}")
    except Exception as e:
        log(f"DNS update failed (non-fatal, fix manually): {e}")

    FENCE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    FENCE_MARKER.write_text(
        f"Old primary ({PRIMARY_NAME}/{PRIMARY_IP}) fenced at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}.\n"
        f"New primary: {STANDBY_NAME} ({STANDBY_IP}).\n"
        f"Gates: {gate_note}\n"
        f"If {PRIMARY_NAME} comes back online, its Postgres MUST NOT be restarted as-is — "
        f"its WAL history has diverged from this point. It needs to be rebuilt as a fresh "
        f"standby via pg_basebackup FROM {STANDBY_IP}, or wiped and re-synced, before it can "
        f"safely rejoin. Run `nova_pg_failover.py unfence` only after that's done.\n"
    )
    log(f"Fence marker written: {FENCE_MARKER}")

    nova_config.post_both(
        f":rotating_light: *Postgres failover executed.* {PRIMARY_NAME} ({PRIMARY_IP}) is down — "
        f"promoted {STANDBY_NAME} ({STANDBY_IP}) to primary, repointed .6's pgbouncer, updated DNS.\n"
        f"*{PRIMARY_NAME} is fenced* — if/when it comes back, its Postgres must be rebuilt as a fresh "
        f"standby before rejoining, never just restarted. Run `nova_pg_failover.py unfence` after that.\n"
        f"nova-core5 now has NO standby of its own — that's the next real gap to close.\n"
        f"Gates: {gate_note[:700]}",
        slack_channel=nova_config.JORDAN_DM,
    )
    # Federal Hill Lights: re-test every containment after the failover. Detached and best-effort —
    # the probe must never delay or fail the failover itself.
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).parent / "nova_federal_hill_lights.py"),
                          "--run", "--trigger", "failover"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:  # noqa: BLE001
        log(f"Federal Hill Lights probe not started (non-fatal): {e}")
    log("DONE. Slack alert sent.")
    return 0


def cmd_unfence(args):
    if not FENCE_MARKER.exists():
        log("No fence marker present — nothing to clear.")
        return 0
    if not args.confirm:
        log("Refusing to unfence without --confirm. Make SURE the old primary has been "
            "rebuilt as a fresh standby (pg_basebackup from the new primary) before doing this.")
        print(FENCE_MARKER.read_text())
        return 1
    FENCE_MARKER.unlink()
    log("Fence cleared.")
    nova_config.post_both(":white_check_mark: Postgres old-primary fence cleared manually.",
                          slack_channel=nova_config.JORDAN_DM)
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check")
    p_check.add_argument("--settings", action="store_true",
                         help="also report wal_log_hints / max_slot_wal_keep_size per node")
    p_promote = sub.add_parser("promote")
    p_promote.add_argument("--confirm", action="store_true")
    p_promote.add_argument("--dry-run", action="store_true", help="evaluate every gate read-only, print the plan")
    p_promote.add_argument("--accept-loss", action="store_true",
                           help="promote even if the LSN gate cannot prove zero loss")
    p_promote.add_argument("--plug-confirmation", type=int, default=None,
                           help="safety_guards confirmation id, if physical_guard demands one for the plug")
    p_unfence = sub.add_parser("unfence")
    p_unfence.add_argument("--confirm", action="store_true")
    args = ap.parse_args()
    return {"check": cmd_check, "promote": cmd_promote, "unfence": cmd_unfence}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
