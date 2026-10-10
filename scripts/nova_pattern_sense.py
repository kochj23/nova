#!/usr/bin/env python3
"""nova_pattern_sense.py — Nova's granted wish (feature_wishes #34, "Pattern Sense").

MERGED 2026-10-09 (organ audit M7, one calibration pass). This is now a thin wrapper:

  1. SYSTEMATIC MISCALIBRATION — moved into nova_soft_certainty.py --refresh, the single
     nightly calibration pass (surface_miscalibration()). Same memory source
     ('pattern_sense'), same wording, same 7-day de-dupe row (service_config
     nova_pattern_sense/high_water).
  2. RECURRING INCIDENTS — moved into nova_alert_learn.py recurrence (cmd_recurrence()).

Running this script still does both (delegating to those homes), so an old schedule entry
keeps working until it is removed; the shared de-dupe row means nothing is remembered twice.
The pure helpers stay importable from here under their old names.

Owned file: scripts/nova_pattern_sense.py. Written by Jordan Koch (via Claude).
"""
from __future__ import annotations
import argparse
import sys
import types
from datetime import datetime

import psycopg2

import nova_alert_learn as _al
import nova_soft_certainty as _sc

OPS_DSN = _sc.OPS_DSN
MEMSRV = _sc.MEMSRV
SOURCE = _sc.PATTERN_SOURCE
STATE_SERVICE = _sc.PATTERN_SERVICE
STATE_KEY = _sc.PATTERN_KEY
MIN_RESOLVED_PER_DOMAIN = _sc.MIN_RESOLVED_PER_DOMAIN
CALIB_GAP = _sc.CALIB_GAP
RESURFACE_DAYS = _sc.RESURFACE_DAYS
RECUR_MIN = _al.RECUR_MIN
RECUR_WINDOW_DAYS = _al.RECUR_WINDOW_DAYS

# old names, new homes
calibration_patterns = _sc.calibration_patterns
calib_insight = _sc.calib_insight
recurrence_patterns = _al.recurrence_patterns
recur_insight = _al.recur_insight
remember = _sc.remember
load_seen = _sc.load_seen
save_seen = _sc.save_seen
_sig = _sc._sig
_fresh = _sc._fresh

MERGED_NOTE = ("merged on 2026-10-09 (M7): miscalibration -> nova_soft_certainty.py --refresh, "
               "recurring incidents -> nova_alert_learn.py recurrence")


def log(m):
    print(f"[pattern-sense {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Nova's Pattern Sense (merged 2026-10-09; thin wrapper)")
    ap.add_argument("--dry-run", action="store_true", help="print insights, write nothing")
    args = ap.parse_args()
    log(MERGED_NOTE)
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=5)
    except Exception as e:
        log(f"no PG ({e}) — fail-open, nothing to do"); return 0
    conn.autocommit = True
    try:
        rows = _sc.scored_rows(conn.cursor())
        _sc.surface_miscalibration(conn.cursor(), rows, dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001
        log(f"miscalibration half failed ({e})")
    finally:
        conn.close()
    try:
        aconn = _al._connect()
        try:
            _al.cmd_recurrence(aconn, types.SimpleNamespace(dry_run=args.dry_run))
        finally:
            aconn.close()
    except Exception as e:  # noqa: BLE001
        log(f"recurrence half failed ({e})")
    return 0


def demo():
    """Runnable check on the pure pattern math (now living in its new homes)."""
    rows = [("self", 0.8, i < 2) for i in range(5)]
    rows += [("ops", 0.6, True)]  # n=1 < MIN_RESOLVED -> ignored
    out = calibration_patterns(rows)
    assert len(out) == 1 and out[0]["domain"] == "self" and out[0]["direction"] == "overconfident", out
    assert abs(out[0]["gap"] - 0.4) < 1e-9, out
    u = calibration_patterns([("world", 0.6, True)] * 4)
    assert u and u[0]["direction"] == "underconfident", u
    wc = calibration_patterns([("cal", 0.5, i < 2) for i in range(4)])
    assert wc == [], wc
    assert recurrence_patterns([("A", 3), ("B", 2)]) == [{"title": "A", "count": 3}]
    print("all pattern-sense assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        demo()
    else:
        sys.exit(main())
