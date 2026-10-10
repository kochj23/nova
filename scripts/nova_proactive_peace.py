#!/usr/bin/env python3
"""
nova_proactive_peace.py — MERGED into nova_escalation.jordan_state() on 2026-10-09 (organ audit M10).

Proactive peace used to poll Little Mister's state every 15 minutes into a flat file
(workspace/state/nova_peace_state.json) and keep a "hold queue" for other scripts. The 2026-10-09
audit found nothing ever imported it and the hold queue never existed, so it never held anything.
Its useful signals (macOS Focus, screen lock) now live in nova_escalation.jordan_state(), the one
answer to "is Little Mister available?".

This file stays runnable as a thin wrapper:
  nova_proactive_peace.py            # logs that it was merged; does nothing else (exit 0)
  nova_proactive_peace.py --status   # prints jordan_state()
  nova_proactive_peace.py --check    # YES/NO from jordan_state()['available']
should_alert(), get_focus_mode() and get_screen_state() remain importable and delegate.
The burnout nudge (Slack line after 23:00 while MLXCode was up) is retired with it.

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


def log(msg):
    print(f"[nova_peace {datetime.now():%H:%M:%S}] {msg}", flush=True)


def _state(oc=None) -> dict:
    """jordan_state() with a best-effort PG cursor; without one it still reads time + Studio signals."""
    conn = None
    try:
        if oc is None:
            try:
                import nova_watch_common as W
                conn = W.connect(attempts=1)
                oc = conn.cursor()
            except Exception:  # noqa: BLE001
                oc = None
        return E.jordan_state(oc)
    except Exception:  # noqa: BLE001
        return {"depleted": False, "reasons": [], "signals": {"focus": "unknown", "screen": "unknown"},
                "available": True}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def get_focus_mode():
    """Delegates to nova_escalation.focus_mode() (Studio-local; 'unknown' elsewhere)."""
    return E.jordan_signals().get("focus", "unknown")


def get_screen_state():
    """Delegates to nova_escalation.screen_state() (Studio-local; 'unknown' elsewhere)."""
    return E.jordan_signals().get("screen", "unknown")


def should_alert(oc=None):
    """(can_send, reason) from nova_escalation.jordan_state()['available']."""
    st = _state(oc)
    if st.get("available", True):
        return True, "available"
    reasons = list(st.get("reasons") or [])
    focus = (st.get("signals") or {}).get("focus")
    if focus in E.UNAVAILABLE_FOCUS and not any("Focus" in r for r in reasons):
        reasons.append(f"macOS Focus: {focus}")
    return False, "; ".join(reasons) or "unavailable"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nova Proactive Peace (merged into nova_escalation.jordan_state)")
    ap.add_argument("--status", action="store_true", help="print jordan_state()")
    ap.add_argument("--check", action="store_true", help="YES/NO: is Little Mister available")
    ap.add_argument("--queue", action="store_true", help="(retired) the hold queue")
    ap.add_argument("--release", action="store_true", help="(retired) the hold queue")
    a = ap.parse_args(argv)
    if a.status:
        print(json.dumps(_state(), indent=1, default=str))
    elif a.check:
        ok, why = should_alert()
        print(f"{'YES' if ok else 'NO'} — {why}")
    else:
        log(f"merged into nova_escalation.jordan_state() on {MERGED}; nothing to do"
            + (" (the hold queue never existed)" if a.queue or a.release else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
