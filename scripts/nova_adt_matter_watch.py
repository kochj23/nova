#!/usr/bin/env python3
"""nova_adt_matter_watch.py — watch for the ADT+ hub gaining Matter support.

The ADT Self Setup hub (192.168.1.100, advertises _adt._tcp) does NOT expose Matter
today, so its door/window/motion sensors are unreachable by
Nova. ADT is rolling Matter out via firmware. This watch browses the LAN for Matter
devices and alerts (Slack) the moment a NEW one appears — especially anything that
maps to the ADT hub — so we can pair it into Apple Home / HA and finally ingest the
sensors. Zero-cost: a daily mDNS browse + diff against a baseline.

launchd: net.digitalnoise.nova-adt-matter-watch (daily).
Run:  nova_adt_matter_watch.py            # one check
      nova_adt_matter_watch.py --selftest # parse/diff self-test (offline)
Written by Jordan Koch.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

STATE = Path.home() / ".openclaw/config/adt_matter_watch.json"
# The only Matter device on the LAN at baseline (a Google Nest hub) — not ADT. Anything
# NEW beyond this, or any commissionable device, is the signal worth a ping.
BASELINE_KNOWN = {"B370A66631AAB94D-00000000FC238A36"}


def _parse_instances(out: str) -> set:
    """Extract mDNS instance names from `dns-sd -B` output (Add rows only)."""
    names = set()
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 7 and p[1] == "Add":   # time Add flags ifindex domain type instance...
            names.add(" ".join(p[6:]))
    return names


def browse(svc: str, secs: int = 8) -> set:
    """Set of mDNS instance names for a service type (dns-sd never exits → we time it out)."""
    try:
        p = subprocess.run(["dns-sd", "-B", svc, "local."],
                           capture_output=True, text=True, timeout=secs)
        out = p.stdout
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "ignore")
    except Exception:
        out = ""
    return _parse_instances(out)


def main():
    op = browse("_matter._tcp")       # operational (commissioned into a fabric)
    com = browse("_matterc._udp")     # commissionable (a device in Matter pairing mode)
    try:
        state = json.loads(STATE.read_text())
    except Exception:
        state = {"seen": [], "fired": []}
    seen = set(state.get("seen", [])) | BASELINE_KNOWN
    fired = set(state.get("fired", []))

    # Signal: any commissionable device, or a new operational Matter device.
    fresh = [s for s in (list(com) + list(op - seen)) if s not in fired]
    if fresh:
        body = ("A Matter device just appeared on the LAN — ADT may have turned on Matter "
                f"for the hub.\nNew: {', '.join(fresh)[:300]}\n"
                "If it's the ADT+ hub: open the ADT+ app → link it to Apple Home (Matter) → "
                "tell Claude to ingest the ADT sensors into Nova.")
        try:
            from nova_notify import notify
            notify("\U0001f513 Possible ADT Matter support detected", body=body,
                   level="warning", category="home", source="nova_adt_matter_watch.py",
                   dedup_key="adt-matter-" + "|".join(sorted(fresh))[:80])
        except Exception as e:
            print(f"notify failed: {e}")
        fired |= set(fresh)

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(
        {"seen": sorted(seen | op), "fired": sorted(fired),
         "last_op": sorted(op), "last_com": sorted(com)}, indent=2))
    print(f"matter watch: {len(op)} operational, {len(com)} commissionable, {len(fresh)} new signal(s)")


def selftest():
    sample = (
        "DATE: ---Fri 26 Jun 2026---\n"
        "15:41:24.000  ...STARTING...\n"
        "Timestamp     A/R    Flags  if Domain   Service Type   Instance Name\n"
        "15:41:24.656  Add        3  26 local.   _matter._tcp.  ABC123-DEF456\n"
        "15:41:24.700  Rmv        3  26 local.   _matter._tcp.  GHOST-SHOULD-IGNORE\n")
    got = _parse_instances(sample)
    assert got == {"ABC123-DEF456"}, got          # Add kept, Rmv + headers ignored
    assert "B370A66631AAB94D-00000000FC238A36" in BASELINE_KNOWN
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
