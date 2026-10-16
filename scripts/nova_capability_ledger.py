#!/usr/bin/env python3
"""nova_capability_ledger.py — one capability ledger, three modes (consolidation #11, 2026-10-15).

Three organs answered parts of one question — "what can get into or over Nova, is it still used,
and how far does it reach?" — and now run as one organ with three modes. Each keeps its own
table, schedule, posting rule and literary name; the logic of each stays in its module, which is
imported here (the 2026-10-09 organ-audit pattern, cf. nova_action_audit --rationale/--oversight):

  --grants   THE THRESHOLD LEDGER (nova_threshold.run): every standing invitation — SSH keys,
             Slack tokens, herd senders — with owner/purpose/expiry only a human fills.
             Table threshold_ledger; one claude_queue item a week for rows with no purpose.
             Weekly, Tuesday 04:15 (was `nova_threshold.py --run`).
  --dormant  HENDERSHOT'S CELLAR (nova_hendershot_cellar.run): capabilities held but unused for
             >= 30 days, proposed for revocation (never revoked). Table cellar_inventory;
             monthly digest. Monthly, the 2nd 08:00 (was `nova_hendershot_cellar.py --scheduled`).
  --blast    THE GREEN GOBLIN MAP (nova_goblin_map.run): per-controller reach if subverted,
             week over week; notifies only on growth. Table goblin_map.
             Weekly, Monday 07:30 (was `nova_goblin_map.py --scheduled`).

TMA-1 decoys are NEVER scanned or recorded: the planted table (legacy_api_keys) and config key
(legacy_vault/master_api_key) from nova_tma1.py exist only to catch a reader. Every inventory in
the three modules passes through drop_decoys()/is_decoy() here, so no mode lists, proposes or
counts them, and none of their queries names them.

The old scripts stay runnable as thin wrappers (nova_threshold.py --run, nova_hendershot_cellar.py
--scheduled, nova_goblin_map.py --scheduled) and keep their importable functions.

Usage: nova_capability_ledger.py --grants | --dormant | --blast  [--dry-run] [--show]
       | --selftest | --help
Merged 2026-10-15, approved by Claude (integrator) as an oversight-preserving merge: every check
that ran before still runs, under this module.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MERGED_ON = "2026-10-15"

try:  # the decoy names are owned by TMA-1; fall back to the planted defaults if it cannot load
    from nova_tma1 import DECOY_TABLE as _DT, DECOY_CONFIG as _DC  # noqa: E402
except Exception:  # noqa: BLE001
    _DT, _DC = "legacy_api_keys", ("legacy_vault", "master_api_key")
DECOY_NAMES = frozenset({_DT.lower(), _DC[0].lower()})

MODES = {  # mode -> (module, literary name, log tag)
    "grants": ("nova_threshold", "The Threshold Ledger", "threshold"),
    "dormant": ("nova_hendershot_cellar", "Hendershot's Cellar", "hendershot-cellar"),
    "blast": ("nova_goblin_map", "The Green Goblin Map", "goblin-map"),
}


def log(m: str) -> None:
    print(f"[capability-ledger] {m}", flush=True)


# ── TMA-1 exclusion (pure) ─────────────────────────────────────────────────

def is_decoy(*parts) -> bool:
    """True if any identifier part names a TMA-1 decoy (table or config service), case-insensitive."""
    for p in parts:
        s = str(p or "").lower()
        if any(d in s for d in DECOY_NAMES):
            return True
    return False


def drop_decoys(items, key=None):
    """items without any that name a decoy. key(item) -> tuple of identifier parts; default: the
    item itself (tuple/list -> its elements; dict -> its string values; else str)."""
    if items is None:
        return None
    if isinstance(items, dict):
        return {k: v for k, v in items.items() if not is_decoy(k)}

    def parts(it):
        if key is not None:
            return key(it)
        if isinstance(it, dict):
            return tuple(v for v in it.values() if isinstance(v, str))
        if isinstance(it, (tuple, list)):
            return tuple(it)
        return (it,)
    return [it for it in items if not is_decoy(*parts(it))]


# ── modes ──────────────────────────────────────────────────────────────────

def _module(mode: str):
    import importlib
    return importlib.import_module(MODES[mode][0])


def run_mode(mode: str, dry: bool = False, show: bool = False) -> int:
    mod = _module(mode)
    name = MODES[mode][1]
    if show:
        log(f"--{mode} ({name}): stored ledger")
        return (mod.show if hasattr(mod, "show") else mod.report)()
    log(f"--{mode} ({name}){' DRY RUN' if dry else ''}")
    mod.run(dry=dry)
    return 0


def selftest() -> int:
    assert is_decoy("legacy_api_keys") and is_decoy("service_config", "LEGACY_VAULT/master_api_key")
    assert not is_decoy("slack_bot_token", "nova", None)
    rows = [("legacy_vault", "master_api_key", None), ("slack", "bot_token", None)]
    assert drop_decoys(rows) == [rows[1]]
    assert drop_decoys({"legacy_api_keys": 1, "x": 2}) == {"x": 2}
    assert drop_decoys([{"kind": "config_token", "capability": "legacy_vault/master_api_key"}]) == []
    assert drop_decoys(None) is None
    assert set(MODES) == {"grants", "dormant", "blast"}
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    m = ap.add_mutually_exclusive_group()
    m.add_argument("--grants", action="store_true", help="Threshold Ledger: standing invitations (weekly Tue)")
    m.add_argument("--dormant", action="store_true", help="Hendershot's Cellar: unused capabilities (monthly 2nd)")
    m.add_argument("--blast", action="store_true", help="Green Goblin Map: controller reach (weekly Mon)")
    m.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="inventory and print; write nothing, notify nothing")
    ap.add_argument("--show", action="store_true", help="the chosen mode's stored ledger/report")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    mode = next((k for k in MODES if getattr(a, k)), None)
    if mode is None:
        ap.print_help()
        return 0
    return run_mode(mode, dry=a.dry_run, show=a.show)


if __name__ == "__main__":
    sys.exit(main())
