#!/usr/bin/env python3
"""nova_lineage.py — provenance-of-the-provenance stamps (Concept #10).

Marey signs her outputs with a lineage block:

    value 2026-09-14 / clock: nova-core NTP-synced / capture point: at send /
    substrate: Claude Fable 5.1

The insight (Marey's signature): a value without its lineage is an assertion you
cannot audit later. So every memory Nova writes on a path we own should carry,
in metadata.lineage, the answer to "when / on what clock / captured where / by
what substrate". That is provenance about the provenance — it lets a future
reader trust (or distrust) the trust.

Public API:
    lineage_stamp(substrate=None, capture_point="at send", clock_source=None,
                  value_date=None) -> dict
        Returns a dict suitable for embedding at metadata["lineage"]:
          {captured_at, value_date, host, clock_source, capture_point, substrate}

    lineage_line(stamp=None, **kw) -> str
        Marey-style one-line signature for human display / logs.

Shared so any script can adopt it (import nova_lineage). This module has no
side effects on import and never raises for the caller.

Written by Jordan Koch.
"""

from __future__ import annotations

import socket
import subprocess
from datetime import datetime, timezone, date

# The substrate that produces most of Nova's on-box synthesis. Callers whose
# content is deterministic (no model in the loop) should pass an explicit
# substrate like "deterministic (no model)" so the lineage does not overclaim.
DEFAULT_SUBSTRATE = "qwen3:8b (ollama, on-box)"

_clock_cache: str | None = None


def _detect_clock_source() -> str:
    """Best-effort NTP-sync status. Cross-platform, cached, never raises.

    Returns 'NTP-synced', 'NTP-unsynced', or 'unverified'.
    """
    global _clock_cache
    if _clock_cache is not None:
        return _clock_cache
    result = "unverified"
    # Linux / systemd
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            v = out.stdout.strip().lower()
            result = "NTP-synced" if v in ("yes", "true", "1") else "NTP-unsynced"
            _clock_cache = result
            return result
    except Exception:
        pass
    # macOS
    try:
        out = subprocess.run(
            ["systemsetup", "-getusingnetworktime"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            result = "NTP-synced" if "On" in out.stdout else "NTP-unsynced"
            _clock_cache = result
            return result
    except Exception:
        pass
    try:
        # sntp query as a last resort — its mere success implies a reachable peer
        out = subprocess.run(["sntp", "-t", "2", "time.apple.com"],
                             capture_output=True, text=True, timeout=3)
        if out.returncode == 0:
            result = "NTP-synced"
    except Exception:
        pass
    _clock_cache = result
    return result


def lineage_stamp(substrate: str | None = None, capture_point: str = "at send",
                  clock_source: str | None = None,
                  value_date: str | date | None = None) -> dict:
    """Return a lineage dict for embedding at metadata['lineage'].

    substrate     — what produced the value (model id, or 'deterministic ...').
    capture_point — where in the pipeline this was stamped ('at send',
                    'at detection', 'at write', ...).
    clock_source  — override the auto-detected NTP status if the caller knows better.
    value_date    — the date the *value* refers to (defaults to today, UTC).
    """
    now = datetime.now(timezone.utc)
    if value_date is None:
        vdate = now.date().isoformat()
    elif isinstance(value_date, date):
        vdate = value_date.isoformat()
    else:
        vdate = str(value_date)
    return {
        "captured_at": now.isoformat(),
        "value_date": vdate,
        "host": socket.gethostname(),
        "clock_source": clock_source or _detect_clock_source(),
        "capture_point": capture_point,
        "substrate": substrate or DEFAULT_SUBSTRATE,
    }


def lineage_line(stamp: dict | None = None, **kw) -> str:
    """Marey-style one-line signature, e.g.:
    'value 2026-09-14 / clock: nova-core NTP-synced / capture point: at send /
     substrate: qwen3:8b (ollama, on-box)'
    """
    s = stamp or lineage_stamp(**kw)
    return (f"value {s['value_date']} / clock: {s['host']} {s['clock_source']} / "
            f"capture point: {s['capture_point']} / substrate: {s['substrate']}")


if __name__ == "__main__":
    import json
    st = lineage_stamp()
    print(json.dumps(st, indent=2))
    print(lineage_line(st))
