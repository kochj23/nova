#!/usr/bin/env python3
"""
Nova Self-Audit — MERGED into nova_reconciler.py (`--self-audit`) on 2026-10-09 (organ audit M14).

Thin wrapper, kept runnable so the existing scheduler-core entry keeps working until it is moved:
  nova_self_audit.py   ==   nova_reconciler.py --self-audit
Same checks and outputs as before (scheduler scripts exist on disk, expected ports listen,
expected processes run; stdout report, ~/.openclaw/logs/self-audit.log, self_audit_state.json,
notify bus with dedup_key "self-audit" only when the issue set changes). Exits 0 either way.

Written by Jordan Koch.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_reconciler as R  # noqa: E402

MERGED = "2026-10-09"

# Importable names kept for anything that used them.
EXPECTED_SERVICES = R.EXPECTED_SERVICES
EXPECTED_PROCESSES = R.EXPECTED_PROCESSES
audit_scripts, audit_services, audit_processes, audit_docs = (
    R.audit_scripts, R.audit_services, R.audit_processes, R.audit_docs)
slack_post = R.self_audit_post


def run_audit():
    R._audit_log().info(f"nova_self_audit merged into nova_reconciler --self-audit on {MERGED}")
    return R.run_audit()


if __name__ == "__main__":
    # Finding audit issues is a SUCCESSFUL run, not a task failure; a crash still exits nonzero.
    run_audit()
    sys.exit(0)
