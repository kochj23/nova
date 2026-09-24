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
  nova_pg_failover.py unfence    — clear the fence marker once nova-core has been
                                    manually verified safe to rejoin (as a NEW
                                    standby, rebuilt via pg_basebackup — never
                                    just restarted in place, its WAL history has
                                    diverged from the moment nova-core5 was
                                    promoted).
"""
import argparse
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
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=5", f"kochj@{host}", cmd],
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
    return 0


def cmd_promote(args):
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
        f"nova-core5 now has NO standby of its own — that's the next real gap to close.",
        slack_channel=nova_config.JORDAN_DM,
    )
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
    sub.add_parser("check")
    p_promote = sub.add_parser("promote")
    p_promote.add_argument("--confirm", action="store_true")
    p_unfence = sub.add_parser("unfence")
    p_unfence.add_argument("--confirm", action="store_true")
    args = ap.parse_args()
    return {"check": cmd_check, "promote": cmd_promote, "unfence": cmd_unfence}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
