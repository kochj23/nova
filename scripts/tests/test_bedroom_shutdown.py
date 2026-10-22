#!/usr/bin/env python3
"""Runnable check for the Master Bedroom UPS shutdown chain (.77 jordans-mini).

Asserts the ORDER and the SAFETY properties of /var/root/nut/bedroom_shutdown.sh
without shutting anything down: the script is invoked with NUT_SHUTDOWN_DRYRUN=1,
which makes it print its plan and exit.

Run on .77:   sudo python3 test_bedroom_shutdown.py
Run from .6:  ssh kochj@192.168.1.77 'sudo python3 /var/root/nut/test_bedroom_shutdown.py'

No test framework, no network, no shutdown. Exit 0 = chain is wired correctly.
"""
import os
import subprocess
import sys

SCRIPT = "/var/root/nut/bedroom_shutdown.sh"
UPSMON_CONF = "/opt/homebrew/etc/nut/upsmon.conf"
SELF = "jordans-mini.digitalnoise.net"
# Everything on the bedroom UPS except .77 itself. .77 is the NUT host AND a load,
# so it must be absent from the peer list and halt last.
EXPECTED_PEERS = [
    "nova-core6.digitalnoise.net",
    "nova-core7.digitalnoise.net",
    "nova-core4.digitalnoise.net",
]

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"ok   {label}")
    else:
        print(f"FAIL {label}{(' -- ' + detail) if detail else ''}")
        failures.append(label)


def main():
    check("shutdown script exists", os.path.isfile(SCRIPT))
    if not os.path.isfile(SCRIPT):
        return finish()

    st = os.stat(SCRIPT)
    check("script is root-owned", st.st_uid == 0, f"uid={st.st_uid}")
    check("script is not group/world writable", not (st.st_mode & 0o022),
          f"mode={oct(st.st_mode & 0o777)}")
    check("script is not on /Volumes (noowners mounts are world-writable)",
          not os.path.realpath(SCRIPT).startswith("/Volumes/"))

    env = dict(os.environ, NUT_SHUTDOWN_DRYRUN="1")
    out = subprocess.run(["/bin/sh", SCRIPT], env=env, capture_output=True,
                         text=True, timeout=20)
    check("dry run exits 0", out.returncode == 0, out.stderr.strip())

    lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
    peers = [l.split(" ", 1)[1] for l in lines if l.startswith("PEER ")]
    settle = [l.split(" ", 1)[1] for l in lines if l.startswith("SETTLE ")]
    selfs = [l.split(" ", 1)[1] for l in lines if l.startswith("SELF ")]

    check("peer list matches the bedroom UPS loads", peers == EXPECTED_PEERS,
          f"got {peers}")
    check("this host is NOT in the peer list (it halts last, locally)",
          SELF not in peers, f"got {peers}")
    check("self is declared", selfs == [SELF], f"got {selfs}")
    check("settle delay is at least 30 s so peers finish first",
          bool(settle) and int(settle[0]) >= 30, f"got {settle}")

    # Ordering is structural, not textual: the peers are fired, waited on, then
    # the settle sleep, then the local halt. Assert that order in the source.
    src = open(SCRIPT).read()
    i_loop = src.index("for h in $PEERS")
    i_wait = src.index("\nwait\n", i_loop)
    i_sleep = src.index('sleep "$SETTLE_SECONDS"', i_wait)
    i_halt = src.index("/sbin/shutdown -h now", i_sleep)
    check("order: peers -> wait -> settle -> local halt",
          i_loop < i_wait < i_sleep < i_halt)

    check("no unconditional local shutdown before the peer loop",
          src.index("/sbin/shutdown -h now") > i_loop)

    if os.path.isfile(UPSMON_CONF):
        conf = open(UPSMON_CONF).read()
        check("upsmon SHUTDOWNCMD points at this script", SCRIPT in conf)
    else:
        check("upsmon.conf readable (run with sudo)", False, UPSMON_CONF)

    return finish()


def finish():
    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed; nothing was shut down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
