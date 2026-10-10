#!/usr/bin/env python3
"""
nova_dead_mans_switch.py — Verify critical deliveries actually happened.

MERGED 2026-10-09 (organ-audit merge M1): the check now lives in nova_output_drift.py
(`run_deliveries`, also `nova_output_drift.py --deliveries`). It reads scheduler_runs, the
table both schedulers write, instead of the Studio scheduler's HTTP port: run from nova-core,
that port was never reachable, so every run since 2026-07-27 skipped and exited 0 (a false
success). It no longer re-runs a missed delivery script; it raises one warning per distinct
set of misses per day (dedup key 'dead-mans-switch-recovery', category 'scheduler').

This script stays runnable as a thin wrapper:
    nova_dead_mans_switch.py            # = nova_output_drift.py --deliveries
    nova_dead_mans_switch.py --dry-run  # = nova_output_drift.py --deliveries --dry-run

Written by Jordan Koch.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def log(msg):
    print(f"[nova_dead_mans_switch {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    log("merged into nova_output_drift.py (--deliveries) on 2026-10-09; running that check")
    import nova_output_drift
    return nova_output_drift.main(["--deliveries"] + (["--dry-run"] if "--dry-run" in argv else []))


if __name__ == "__main__":
    sys.exit(main())
