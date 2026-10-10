#!/usr/bin/env python3
"""
nova_attention_zones.py — MERGED into nova_escalation.jordan_state() on 2026-10-09 (organ audit M10).

This was a Frigate-style "zones with inertia" HTTP API (port 37471, Redis keys nova:zone:*) meant to
gate notification delivery. The 2026-10-09 audit found it was never scheduled, its API was not
running (37471 is nova_deploy_agent's port), nothing imported it, and its log was last written
06-10. The question it answered, "is Little Mister available?", now has one answer:
nova_escalation.jordan_state().

Thin wrapper, kept runnable:
  nova_attention_zones.py            # logs that it was merged; exits 0 (no server, no Redis)
  nova_attention_zones.py --status   # prints jordan_state() as a zone
should_notify(severity), get_active_zone() and _in_hours() remain importable and delegate.

Written by Jordan Koch.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_escalation as E  # noqa: E402

MERGED = "2026-10-09"
VERSION = "2.0.0-merged"
CRITICAL = ("critical", "emergency")


def log(msg, level="INFO"):
    print(f"[zones {datetime.now():%H:%M:%S}] [{level}] {msg}", flush=True)


def _in_hours(hour: int, hours_range: tuple) -> bool:
    """Check if hour falls within range (handles midnight wrap)."""
    start, end = hours_range
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _state(st: dict | None = None) -> dict:
    if st is not None:
        return st
    import nova_proactive_peace as P   # same best-effort PG cursor handling
    return P._state()


def zone_for(st: dict) -> str:
    """Map a jordan_state() dict onto the old zone names (pure)."""
    if (st.get("signals") or {}).get("focus") in E.UNAVAILABLE_FOCUS:
        return "focus"
    if st.get("depleted"):
        return "rest"
    return "work" if _in_hours(datetime.now(E.TZ).hour, (8, 18)) else "home"


def get_active_zone(st: dict | None = None) -> str:
    return zone_for(_state(st))


def should_notify(severity: str, st: dict | None = None) -> bool:
    """Deliver now? Yes when Little Mister is available, or the severity is critical/emergency."""
    if str(severity).lower() in CRITICAL:
        return True
    return bool(_state(st).get("available", True))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nova Attention Zones (merged into nova_escalation.jordan_state)")
    ap.add_argument("--status", action="store_true", help="print jordan_state() and the zone it maps to")
    a = ap.parse_args(argv)
    if a.status:
        st = _state()
        print(json.dumps({"active_zone": zone_for(st), "jordan_state": st}, indent=1, default=str))
    else:
        log(f"merged into nova_escalation.jordan_state() on {MERGED}; no zones API is started")
    return 0


if __name__ == "__main__":
    sys.exit(main())
