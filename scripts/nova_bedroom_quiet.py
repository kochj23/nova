#!/usr/bin/env python3
"""nova_bedroom_quiet.py — nightly quiet window for the master-bedroom machines.

Jordan 2026-10-22: "I don't want to hear the fans cycling on and off all night."
Window 21:30 -> 04:00 daily.

MEASURED CAUSE (2026-10-22, .77 / nova-core10, M4 Pro mini Mac16,11): macOS relaunched
`com.apple.mediaanalysisd.photos.full` ELEVEN times between 22:35 and 09:45 on a ~67 min
cycle (22:35 23:42 00:50 01:56 03:05 04:18 05:23 06:30 07:36 08:40 09:45), re-analysing a
251 GB / 54,646-item Photos library at 74-95 % CPU plus hardware video decode
(VTDecoderXPCService ~34 %). Trigger is a stale analysis-model version in the library --
the log throws `Invalid embedding version (hasVersion: 1, version: 5)` -- so every asset
needs regenerating; it is real progress, not a stuck loop, and will run for several more
nights. Each burst ramps the fan, the batch ends, the fan spins down, an hour later it
repeats. That periodicity IS the cycling Jordan hears. The other three bedroom nodes were
ruled out: .252 steady 26 % on one whisper reingest (constant, no cycle), .197 load ~1.0,
.250 idle.

TWO LEVERS, both already native or already present -- no new daemon, no new state table:

  1. Low Power Mode on .77 (`pmset -a powermode 1`). This is the only lever that touches
     Apple's own background analysis, which the Nova scheduler cannot gate. VERIFIED
     settable on Mac16,11. .252 (Macmini9,1) answers "LowPowerMode not supported on AC
     Power", so it gets lever 2 only -- do not add it to POWERMODE_HOSTS.

  2. `service_config media_reingest/exclude_nodes` -- the pool filter that
     nova_media_reingest.whisper_minions() ALREADY honours. Keeps Nova's own whisper
     transcription off the bedroom nodes for the window without idling the rest of the
     fleet (gating the scheduler task itself would have stopped every minion everywhere).

The window THROTTLES, it does not stop: ollama on .77 still answers, just slower, and
every non-whisper scheduled task still runs. That was Jordan's explicit ask -- "Let them
do things but jeesh".

exclude_nodes is shared with humans (nova-core4/.250 is excluded permanently, by hand), so
`on` records exactly which nodes IT added under key `quiet_excluded` and `off` removes only
those. A manual exclusion made while the window is open therefore survives `off`.

Usage: nova_bedroom_quiet.py on|off|status [--dry-run]
Check:  nova_bedroom_quiet.py --selftest   (asserts the add/remove algebra, touches nothing)
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys

sys.path.insert(0, "/Volumes/nas/nova-fs/scripts")

SERVICE = "media_reingest"          # must match nova_media_reingest.SERVICE
EXCLUDE_KEY = "exclude_nodes"       # read by whisper_minions()
MARKER_KEY = "quiet_excluded"       # ours: what this script added, so off removes only that

# minion_nodes.node names for the four master-bedroom machines (the "Jordan" PoE switches).
# nova-core4 is normally already in exclude_nodes by hand; the union below makes that a no-op.
BEDROOM = ("nova-core10", "nova-core6", "nova-core7", "nova-core4")

# Only hosts whose hardware actually supports Low Power Mode. ssh target by IP, because
# nova-core10's wired name and its .251 wifi address both resolve and we want the wired one.
POWERMODE_HOSTS = {"nova-core10": "192.168.1.77"}


def plan(existing: list[str], marker: list[str], turning_on: bool) -> tuple[list[str], list[str]]:
    """(new exclude_nodes, new marker). Pure, so --selftest can check it without a DB.

    on : add the bedroom nodes that are not already excluded, and remember exactly those.
    off: drop only the remembered ones, leaving hand-made exclusions (e.g. nova-core4) alone.
    """
    if turning_on:
        added = [n for n in BEDROOM if n not in existing]
        return existing + added, sorted(set(marker) | set(added))
    return [n for n in existing if n not in marker], []


def _get(cur, key: str) -> list[str]:
    cur.execute("SELECT value FROM service_config WHERE service = %s AND key = %s", (SERVICE, key))
    row = cur.fetchone()
    if not row or row[0] is None:
        return []
    val = row[0] if isinstance(row[0], list) else json.loads(row[0])
    return list(val)


def _put(cur, key: str, value: list[str]) -> None:
    cur.execute(
        "INSERT INTO service_config (service, key, value) VALUES (%s, %s, %s::jsonb) "
        "ON CONFLICT (service, key) DO UPDATE SET value = EXCLUDED.value",
        (SERVICE, key, json.dumps(value)),
    )


def set_powermode(on: bool, dry_run: bool = False) -> list[str]:
    """Low Power Mode on every POWERMODE_HOSTS box. Returns human-readable result lines.

    Never fatal: a bedroom node that is down must not stop the whisper exclusion from
    being applied, or a reboot at 21:29 would leave the fans loud all night.
    """
    out = []
    want = "1" if on else "0"
    for node, ip in POWERMODE_HOSTS.items():
        cmd = ["sudo", "-n", "pmset", "-a", "powermode", want]
        local = socket.gethostname().split(".")[0] in (node, "jordans-mini", "Jordans-Mac-mini")
        if not local:
            cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", ip] + cmd
        if dry_run:
            out.append(f"{node}: would run {' '.join(cmd)}")
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                out.append(f"{node}: powermode -> {want}")
            else:
                out.append(f"{node}: FAILED rc={r.returncode} {(r.stderr or '').strip()[:120]}")
        except Exception as e:
            out.append(f"{node}: FAILED {type(e).__name__}: {e}")
    return out


def read_powermode() -> list[str]:
    out = []
    for node, ip in POWERMODE_HOSTS.items():
        try:
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", ip,
                 "pmset -g custom | awk '/AC Power/,0' | awk '/powermode/{print $2}'"],
                capture_output=True, text=True, timeout=30)
            v = (r.stdout or "").strip() or "?"
            out.append(f"{node}: powermode={v} ({'LOW POWER' if v == '1' else 'normal'})")
        except Exception as e:
            out.append(f"{node}: unreadable ({type(e).__name__})")
    return out


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}

    if "--selftest" in flags:
        # on from a clean slate adds all four and remembers all four
        new, mark = plan([], [], True)
        assert new == list(BEDROOM), new
        assert mark == sorted(BEDROOM), mark
        # off then restores empty
        assert plan(new, mark, False) == ([], []), plan(new, mark, False)
        # a hand-made exclusion (nova-core4) is NOT claimed by us, so off must keep it
        new, mark = plan(["nova-core4"], [], True)
        assert "nova-core4" in new and "nova-core4" not in mark, (new, mark)
        back, _ = plan(new, mark, False)
        assert back == ["nova-core4"], back
        # a manual exclusion added WHILE the window is open survives off
        new2, mark2 = plan(["nova-core4"], [], True)
        back2, _ = plan(new2 + ["nova-core3"], mark2, False)
        assert back2 == ["nova-core4", "nova-core3"], back2
        # on is idempotent: running it twice must not duplicate or re-claim
        a, ma = plan([], [], True)
        b, mb = plan(a, ma, True)
        assert (b, mb) == (a, ma), (b, mb)
        print("selftest OK")
        return 0

    cmd = args[0] if args else "status"
    if cmd not in ("on", "off", "status"):
        sys.stderr.write(__doc__.split("Usage:")[1])
        return 2
    dry = "--dry-run" in flags

    import nova_dsn
    conn = nova_dsn.pg_connect()
    try:
        with conn, conn.cursor() as cur:
            existing, marker = _get(cur, EXCLUDE_KEY), _get(cur, MARKER_KEY)

            if cmd == "status":
                print(f"exclude_nodes : {existing}")
                print(f"quiet_excluded: {marker}  ({'WINDOW OPEN' if marker else 'window closed'})")
                for line in read_powermode():
                    print(line)
                return 0

            new, new_marker = plan(existing, marker, cmd == "on")
            print(f"exclude_nodes : {existing} -> {new}")
            print(f"quiet_excluded: {marker} -> {new_marker}")
            if not dry:
                _put(cur, EXCLUDE_KEY, new)
                _put(cur, MARKER_KEY, new_marker)
    finally:
        conn.close()

    for line in set_powermode(cmd == "on", dry):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
