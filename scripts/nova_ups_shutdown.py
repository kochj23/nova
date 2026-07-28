#!/usr/bin/env python3
"""nova_ups_shutdown.py — graceful fleet shutdown on mains failure.

WHY THIS EXISTS: on 2026-07-27 the power died, every machine hard-crashed, and the Postgres
primary came back with a HOLE IN ITS WAL — the replica refused to reconnect and needed an
84GB rebuild. Then on 2026-07-28 sleep was disabled fleet-wide (correctly, it was causing
phantom SSH failures), which removed the accidental protection sleep had provided. Without
this script the fleet now runs at full draw until the batteries die and crashes exactly the
same way, only faster.

TOPOLOGY (confirmed with Jordan 2026-07-28) — three separate UPSs, and this matters:

  STUDIO UPS   .6 + a 100W mini fridge + a USB-C charger. Light load, hours of runtime.
               Powers the orchestrator, which is why the Studio outlives what it shuts down.
  BEDROOM UPS  .101, .250, .252. Three Macs on one consumer unit — short runtime, and
               nothing monitors it. They shut down IMMEDIATELY on mains loss; we never
               need its battery level because they always go first.
  RACK UPS     .2, .86, .88, .10, .7, both NASs, the NVR, the switch and the UDM.
               Shutdown here is gated on the RACK battery level, and the Studio's USB data
               cable comes from THIS unit — so the Studio can read the battery it is
               deciding about without sharing that battery's fate.

  Printers are on a fourth UPS and are deliberately out of scope.

THE KEY FACT (corrected by Jordan 2026-07-28): the UPS the Studio sees over USB **IS the rack
UPS**. Its DATA cable comes from the rack unit, while its POWER comes from the lighter UPS with
the fridge and the charger. That is close to ideal: the Studio reads the rack battery it is
deciding about, while surviving on a supply that outlasts everything it is shutting down. The
orchestrator does not go down with the thing it orchestrates.
"""
import argparse
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOG = Path.home() / ".openclaw/logs/ups_shutdown.log"

# Shutdown waves, in order. Each is (label, [hosts], trigger).
# trigger 'mains' = the moment utility power is lost.
# trigger <int>   = rack UPS battery percentage at or below which the wave fires.
WAVES = [
    ("bedroom-macs", ["192.168.1.252", "192.168.1.250", "192.168.1.251"], "mains"),
    ("rack-leaf-compute", ["192.168.1.7", "192.168.1.88", "192.168.1.86", "192.168.1.10"], 35),
    # .2 goes AFTER the leaves and BEFORE storage: it holds the Postgres primary, the
    # gateway and Plex, and it must flush and stop while its disks still exist.
    ("rack-primary", ["192.168.1.2"], 35),
    ("storage", ["192.168.1.11", "192.168.1.69"], 35),
    ("nvr", ["192.168.1.9"], 35),
]
# Never touched: the switch (.24) and UDM (.1) ghost-ride — the network must survive long
# enough to reach everything.
#
# The Studio does NOT shut itself down, because its power comes from the lighter UPS. If that
# ever changes — if .6 is moved onto the rack unit — flip this on so the orchestrator stops
# cleanly instead of being the one machine that still hard-crashes.
STUDIO_SELF_SHUTDOWN_AT = None      # set to e.g. 15 (percent) if .6 moves to the rack UPS

# Appliances do not share the fleet's kochj+passwordless-sudo convention, and a wave that
# cannot authenticate is a wave that silently does nothing. Verified 2026-07-28 by --preflight.
HOST_AUTH = {
    "192.168.1.69": ("root", "poweroff"),          # UNAS — root key auth, already root, no sudo
    "192.168.1.9":  ("root", "poweroff"),          # NVR  — key NOT yet installed; see --preflight
}
DEFAULT_AUTH = (None, "sudo -n shutdown -h now")   # every Mac and Linux node, plus the Synology

# DELIBERATELY RAW IPs, unlike the rest of the fleet's tooling.
# Both nameservers (.2 and .86) are on this shutdown list. The moment wave 'rack-primary'
# powers off .2, DNS for the whole house is gone — and the storage and NVR waves still have
# to run after that. A name-addressed orchestrator would resolve fine right up until it
# succeeded at its job, then fail to find the machines it had not finished shutting down.
# Names everywhere else; numbers here.
SAMPLES_TO_CONFIRM = 3      # consecutive on-battery reads before acting
SAMPLE_GAP = 10             # seconds between confirmation samples


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def studio_power():
    """(on_battery, percent) from the rack UPS over USB. 'Drawing from UPS Power' is the
    fleet-wide mains-loss signal; the percentage is the rack's remaining runtime."""
    r = run(["pmset", "-g", "ps"], timeout=15)
    out = r.stdout or ""
    on_batt = "UPS Power" in out or "Battery Power" in out
    m = re.search(r"(\d+)%", out)
    return on_batt, (int(m.group(1)) if m else None)


SIMULATE_PCT = None     # set by --simulate-percent; testing only, never in normal operation


def rack_percent():
    """Rack UPS battery %, straight from the Studio's USB link to the rack unit.

    Returns None when unreadable — and None must NEVER be read as 'fine'. A shutdown trigger
    that cannot see the battery is the same class of lie as a health check reporting success
    without evidence, which is the mistake this fleet spent all week unlearning.
    """
    if SIMULATE_PCT is not None:
        return float(SIMULATE_PCT)
    r = run(["pmset", "-g", "ps"], timeout=15)
    m = re.search(r"(\d+)%", r.stdout or "")
    return float(m.group(1)) if m else None


def ssh_retry(target, remote_cmd, tries=3, timeout=30):
    """SSH with retries. A transient blip is not a decision.

    Observed 2026-07-28: .252 failed one probe on a 6s connect timeout, then answered 6/6 at
    ~200ms. Without retries that blip at outage time means the box never gets the shutdown and
    hard-crashes anyway — the single failure mode this whole script exists to prevent.
    """
    r = None
    for attempt in range(tries):
        r = run(["ssh", "-o", "ConnectTimeout=6", "-o", "BatchMode=yes",
                 "-o", "StrictHostKeyChecking=no", target, remote_cmd], timeout=timeout)
        # Retry on ANYTHING non-zero. 255 is ambiguous — it is both "the host powered off
        # mid-command" (success) and "could not connect at all" (failure). Treating it as
        # success here would give an unreachable host exactly one attempt, which is the case
        # retries exist for. Let the caller interpret 255; wait_down() is the real witness.
        if r.returncode == 0:
            return r, attempt + 1
        if attempt < tries - 1:
            time.sleep(2)
    return r, tries


def ssh_target(host):
    user, cmd = HOST_AUTH.get(host, DEFAULT_AUTH)
    return (f"{user}@{host}" if user else host), cmd


def preflight():
    """Prove every target can actually be shut down, BEFORE the power goes out.

    An unreachable host in a wave fails silently at exactly the moment nobody is watching.
    This is the difference between a plan and a capability.
    """
    log("=== preflight: can each target actually be commanded? ===")
    armed = unarmed = 0
    for label, hosts, trig in WAVES:
        for h in hosts:
            target, cmd = ssh_target(h)
            # Probe the ACTUAL command, not just the login. On 2026-07-28 a `true` probe
            # reported a host unreachable when it was fine, and a bare login probe would
            # equally have passed a host whose shutdown binary does not exist. Prove both.
            parts = cmd.split()
            if parts[0] == "sudo":                      # skip sudo AND its flags (-n)
                parts = [x for x in parts[1:] if not x.startswith("-")]
            binary = parts[0]
            probe = f"command -v {binary} >/dev/null"
            if cmd.startswith("sudo"):
                probe = f"sudo -n true && {probe}"
            r, tries = ssh_retry(target, probe, timeout=20)
            ok = r.returncode == 0
            if ok and tries > 1:
                log(f"           (took {tries} attempts — transient, worth watching)")
            armed, unarmed = (armed + ok), (unarmed + (not ok))
            log(f"  [{'ARMED ' if ok else 'NOT ARMED'}] {target:24} wave={label}")
            if not ok:
                log(f"           fix: ssh-copy-id {target}   (then re-run --preflight)")
    log(f"=== {armed} armed, {unarmed} NOT armed ===")
    return 1 if unarmed else 0


def shutdown_host(host, dry_run):
    target, cmd = ssh_target(host)
    if dry_run:
        log(f"    DRY-RUN would shut down {target} via `{cmd}`")
        return True
    reachable = run(["ping", "-c", "1", "-W", "1", host], timeout=6).returncode == 0
    r, tries = ssh_retry(target, cmd)
    # 255 after a host that WAS reachable = it died mid-command, which is what we wanted.
    # 255 from a host that never answered ping = we never commanded anything. Say so, rather
    # than letting wait_down() later "confirm" an unreachable box as gracefully shut down.
    ok = r.returncode == 0 or (r.returncode == 255 and reachable)
    if not reachable:
        log(f"    {target}: was ALREADY UNREACHABLE before shutdown — not commanded")
        return False
    log(f"    {target}: {'shutdown issued' if ok else 'FAILED rc=' + str(r.returncode)}"
        f"{' after ' + str(tries) + ' attempts' if tries > 1 else ''}")
    return ok


def wait_down(hosts, timeout=150):
    """Block until the hosts stop answering ping, or timeout.

    `shutdown -h now` returns the INSTANT it is accepted, not when the machine is off. Sleeping
    a fixed 20s and moving on would cut the NAS out from under a Postgres primary still flushing
    WAL — which is the exact corruption this whole script exists to prevent. So we watch for the
    host to actually go silent instead of assuming it did.
    """
    deadline = time.time() + timeout
    pending = list(hosts)
    while pending and time.time() < deadline:
        time.sleep(5)
        pending = [h for h in pending
                   if run(["ping", "-c", "1", "-W", "1", h], timeout=6).returncode == 0]
    if pending:
        log(f"    STILL UP after {timeout}s: {pending} — proceeding anyway, battery does not wait")
    else:
        log("    wave confirmed down")


def do_wave(label, hosts, dry_run):
    log(f"  WAVE '{label}': {len(hosts)} host(s)")
    for h in hosts:
        shutdown_host(h, dry_run)
    if not dry_run:
        wait_down(hosts)


def status():
    on_batt, pct = studio_power()
    log(f"rack UPS via Studio USB: {'ON BATTERY' if on_batt else 'on mains'}, {pct}%")
    rp = rack_percent()
    if rp is None:
        log("rack UPS: UNREADABLE — pmset is not reporting a battery percentage.")
        log("          Check the USB link between the rack UPS and the Studio.")
        log("          Until it reads, the rack waves CANNOT fire and this script is half-armed.")
    else:
        log(f"rack UPS: {rp}%")
    for label, hosts, trig in WAVES:
        log(f"  wave {label:20} trigger={trig!s:6} hosts={len(hosts)}")
    return 0


def main(dry_run, force):
    on_batt, pct = studio_power()
    if not on_batt and not force:
        log(f"mains OK (rack UPS {pct}%) — nothing to do")
        return 0

    if force:
        log("FORCED run (no real outage) — treating as mains loss")
    else:
        # Confirm before acting: a single sample can be a blip, and shutting the fleet down
        # over a flicker is its own outage.
        for i in range(SAMPLES_TO_CONFIRM - 1):
            time.sleep(SAMPLE_GAP)
            again, _ = studio_power()
            if not again:
                log("mains returned during confirmation — standing down")
                return 0
            log(f"  confirmation {i + 2}/{SAMPLES_TO_CONFIRM}: still on battery")
        log("MAINS LOSS CONFIRMED")

    mode = "DRY RUN" if dry_run else "LIVE"
    log(f"=== fleet shutdown sequence ({mode}) ===")

    for label, hosts, trig in WAVES:
        if trig == "mains":
            log(f"trigger '{label}': immediate (bedroom UPS is small and unmonitored)")
            do_wave(label, hosts, dry_run)
            continue

        rp = rack_percent()
        if rp is None:
            log(f"trigger '{label}': RACK BATTERY UNREADABLE — cannot decide. SKIPPING.")
            log("      These hosts will hard-crash when the rack UPS dies. Fix the USB link.")
            continue
        if rp > trig:
            log(f"trigger '{label}': rack at {rp}% (> {trig}%) — waiting")
            if dry_run:
                log(f"    DRY-RUN: would wait, then shut down {hosts}")
            continue
        log(f"trigger '{label}': rack at {rp}% (<= {trig}%) — firing")
        do_wave(label, hosts, dry_run)

    if STUDIO_SELF_SHUTDOWN_AT is not None:
        rp = rack_percent()
        if rp is not None and rp <= STUDIO_SELF_SHUTDOWN_AT:
            log(f"studio: rack at {rp}% — shutting self down LAST")
            if dry_run:
                log("    DRY-RUN would shut down the Studio (.6)")
            else:
                run(["sudo", "-n", "shutdown", "-h", "now"])

    log("=== sequence complete; Studio stays up to orchestrate ===")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="log what would happen, touch nothing")
    ap.add_argument("--status", action="store_true", help="show power state and wave plan")
    ap.add_argument("--force", action="store_true",
                    help="pretend mains is lost (for testing with --dry-run)")
    ap.add_argument("--preflight", action="store_true",
                    help="prove every target is reachable and commandable, changing nothing")
    ap.add_argument("--simulate-percent", type=float, default=None,
                    help="pretend the rack battery is at this %% (testing; use with --dry-run)")
    a = ap.parse_args()
    if a.simulate_percent is not None:
        if not a.dry_run:
            sys.exit("refusing to simulate a battery level outside --dry-run")
        SIMULATE_PCT = a.simulate_percent
        log(f"SIMULATING rack battery at {SIMULATE_PCT}%")
    if a.preflight:
        sys.exit(preflight())
    sys.exit(status() if a.status else main(a.dry_run, a.force))
