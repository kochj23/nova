#!/opt/homebrew/bin/python3
"""nova_home_reachability.py — flags HomeKit rooms that go dark and says whether the fault looks like the
controller (every room dark) or a network segment (only some rooms dark).

Reads the latest snapshot in telemetry.homekit_outlets. Optionally pings a few known LAN devices to tell a
powered-but-unreachable device apart from a dead one. Alerts once per room per day via nova_notify.

Usage: nova_home_reachability.py [--dry-run] [--no-ping]
Written by Jordan Koch (via Claude).
"""
import argparse
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn()
DARK_SHARE = 0.5          # a room is dark when fewer than half its outlets report active
PING_HOSTS = {            # LAN devices that were answering when this was written (2026-10-09)
    "outdoor HomePod": "192.168.1.70",
    "outdoor plug": "192.168.1.71",
    "Hue Bridge": "192.168.1.152",
    "Lutron Caseta bridge": "192.168.1.55",
}


def diagnose(rows: list, dark_share: float = DARK_SHARE) -> dict:
    """rows: [(room, accessory, active bool)] from one snapshot. -> {"verdict", "dark_rooms", "text"}. Pure."""
    by_room = defaultdict(list)
    for room, _acc, active in rows:
        by_room[room].append(bool(active))
    if not by_room:
        return {"verdict": "no-data", "dark_rooms": [], "text": "no outlet rows in the latest snapshot"}
    dark = sorted(r for r, v in by_room.items() if sum(v) / len(v) < dark_share)
    if not dark:
        return {"verdict": "ok", "dark_rooms": [], "text": "all rooms reporting"}
    if len(dark) == len(by_room):
        return {"verdict": "controller", "dark_rooms": dark,
                "text": "every room is dark: the HomeKit controller (home hub) or the NovaHomeKit feed is down, "
                        "not one network segment"}
    return {"verdict": "segment", "dark_rooms": dark,
            "text": f"only {', '.join(dark)} dark: check the access point or bridge that serves "
                    f"{'that room' if len(dark) == 1 else 'those rooms'}"}


def ping_ok(ip: str, attempts: int = 2, _sleep=None) -> bool:
    """One ping, retried once after a short wait. Absolute path: the scheduler runs with a minimal PATH."""
    import time
    for i in range(attempts):
        try:
            r = subprocess.run(["/sbin/ping", "-c", "1", "-t", "3", ip], capture_output=True, timeout=10)
            if r.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
        if i < attempts - 1:
            (_sleep or time.sleep)(2 * (i + 1))
    return False


def ping_summary(hosts: dict, _ping=ping_ok) -> str:
    """-> 'name: up/down' for each host. A device that pings but shows unreachable is powered and on the LAN."""
    return "; ".join(f"{name}: {'up' if _ping(ip) else 'down'}" for name, ip in hosts.items())


def latest_rows(conn) -> list:
    cur = conn.cursor()
    cur.execute("""SELECT room, accessory, status_active FROM telemetry.homekit_outlets
                   WHERE ts = (SELECT max(ts) FROM telemetry.homekit_outlets)""")
    return cur.fetchall()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print the verdict, do not notify")
    ap.add_argument("--no-ping", action="store_true")
    a = ap.parse_args(argv)
    import psycopg2
    import nova_notify
    conn = _nova_dsn.pg_connect()
    try:
        rows = latest_rows(conn)
    finally:
        conn.close()
    res = diagnose(rows)
    print(f"verdict: {res['verdict']} — {res['text']}")
    if not a.no_ping and res["verdict"] != "ok":
        print("ping: " + ping_summary(PING_HOSTS))
    if res["verdict"] in ("controller", "segment") and not a.dry_run:
        from datetime import date
        for room in res["dark_rooms"]:
            nova_notify.notify(
                f"HomeKit: {room} unreachable", res["text"], level="warn", category="home",
                source="nova_home_reachability", dedup_key=f"homekit-dark-{room}-{date.today()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
