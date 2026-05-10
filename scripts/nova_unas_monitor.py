#!/usr/bin/env python3
"""
nova_unas_monitor.py — UniFi UNAS Pro 8 monitoring and health reporting.

Polls UNAS Pro every 5 minutes (via scheduler). Writes status JSON for
NovaControl to read. Alerts to Slack #nova-notifications on problems.

Thresholds:
  Storage: warn at 80%, critical at 90%
  Status: alert on anything other than "healthy"

PRIVACY: All UNAS data is local-only. Never routed to cloud LLMs.

Usage:
  python3 nova_unas_monitor.py                  # Full health check (default)
  python3 nova_unas_monitor.py --status         # System status summary
  python3 nova_unas_monitor.py --storage        # Storage details
  python3 nova_unas_monitor.py --shares         # Shared drives listing
  python3 nova_unas_monitor.py --json           # Full status as JSON (for Nova)
  python3 nova_unas_monitor.py --problems       # Only show detected problems
  python3 nova_unas_monitor.py --snapshot       # Save daily snapshot

Written by Jordan Koch.
"""

import argparse
import json
import sys
import time
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_unas_client import UNASClient, UNASError
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

VECTOR_URL = nova_config.VECTOR_URL
NOW = datetime.now()
TODAY = date.today().isoformat()

STATE_DIR = Path.home() / ".openclaw/workspace/state"
STATE_FILE = STATE_DIR / "nova_unas_state.json"
STATUS_FILE = STATE_DIR / "nova_unas_status.json"   # NovaControl reads this
SNAPSHOT_FILE = STATE_DIR / "unas_snapshots.json"
LOG_FILE = Path.home() / ".openclaw/logs/nova_unas_monitor.log"

STORAGE_WARN_PCT = 80
STORAGE_CRIT_PCT = 90

client = UNASClient()


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = NOW.strftime("%H:%M:%S")
    line = f"[nova_unas {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Slack alerting ────────────────────────────────────────────────────────────

def post_slack(message: str, channel: str = nova_config.SLACK_NOTIFY):
    """Post a message to Slack #nova-notifications. Silently skips if token unavailable."""
    try:
        nova_config.post_both(message, slack_channel=channel)
    except Exception as exc:
        log(f"Slack post failed: {exc}")


# ── State helpers ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _save_status(snapshot: dict):
    """Write the status file that NovaControl reads."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(snapshot, indent=2))


# ── Checks ────────────────────────────────────────────────────────────────────

def check_storage(snapshot: dict) -> list[str]:
    """Returns list of problem strings (empty = healthy)."""
    problems = []
    st = snapshot.get("storage", {})
    status = st.get("status", "unknown")
    if status not in ("healthy", ""):
        problems.append(f"Storage status is '{status}' (expected healthy)")
    used_pct = st.get("used_pct", 0)
    if used_pct >= STORAGE_CRIT_PCT:
        free_tb = st.get("free_tb", 0)
        problems.append(
            f"Storage CRITICAL: {used_pct:.1f}% used, only {free_tb:.1f}TB free"
        )
    elif used_pct >= STORAGE_WARN_PCT:
        free_tb = st.get("free_tb", 0)
        problems.append(
            f"Storage warning: {used_pct:.1f}% used, {free_tb:.1f}TB free"
        )
    if st.get("needs_more_disk"):
        problems.append("UNAS reports it needs more disk capacity")
    return problems


def check_shares(snapshot: dict) -> list[str]:
    """Returns list of problem strings for shares."""
    problems = []
    for share in snapshot.get("shares", []):
        status = share.get("status", "")
        if status and status != "active":
            problems.append(f"Share '{share['name']}' status: {status}")
    return problems


def check_device(snapshot: dict) -> list[str]:
    """Returns list of problem strings for device state."""
    problems = []
    state = snapshot.get("device", {}).get("state", "")
    # "setup" is expected for new devices — not an alert condition
    if state not in ("", "setup", "configured", "active"):
        problems.append(f"UNAS device state: {state}")
    return problems


# ── Display helpers ───────────────────────────────────────────────────────────

def _fmt_bytes(b: int) -> str:
    if b >= 1e12:
        return f"{b/1e12:.2f} TB"
    if b >= 1e9:
        return f"{b/1e9:.2f} GB"
    return f"{b/1e6:.1f} MB"


def print_status(snapshot: dict):
    dev = snapshot.get("device", {})
    st = snapshot.get("storage", {})
    print(f"\n{'='*55}")
    print(f"  UniFi UNAS Pro 8 — {dev.get('name', 'UNAS Pro 8')}")
    print(f"  Model: {dev.get('model')}  |  State: {dev.get('state')}")
    print(f"  Internet: {'✓' if dev.get('has_internet') else '✗'}  |  "
          f"Cloud: {'✓' if dev.get('cloud_connected') else '✗ (local only)'}")
    print(f"{'='*55}")
    print(f"  Storage: {st.get('status', 'unknown').upper()}")
    print(f"  Total:   {st.get('total_tb', 0):.2f} TB")
    print(f"  Used:    {_fmt_bytes(st.get('used_bytes', 0))} ({st.get('used_pct', 0):.1f}%)")
    print(f"  Free:    {st.get('free_tb', 0):.2f} TB")
    if snapshot.get("shares"):
        print(f"\n  Shared Drives ({len(snapshot['shares'])}):")
        for s in snapshot["shares"]:
            enc = "🔒" if s.get("encryption") != "unencrypted" else ""
            print(f"    {s['name']}: {s['used_tb']:.2f} TB used  "
                  f"[{s.get('status', '?')}] {enc}")
    print()


def print_problems(problems: list[str]):
    if not problems:
        print("✅  No problems detected on UNAS Pro 8")
    else:
        print(f"⚠️  {len(problems)} problem(s) detected:")
        for p in problems:
            print(f"  • {p}")


# ── Snapshot ──────────────────────────────────────────────────────────────────

def save_snapshot(snapshot: dict):
    try:
        existing = []
        if SNAPSHOT_FILE.exists():
            existing = json.loads(SNAPSHOT_FILE.read_text())
        existing.append({**snapshot, "date": TODAY})
        # Keep 90 days
        existing = existing[-90:]
        SNAPSHOT_FILE.write_text(json.dumps(existing, indent=2))
        log(f"Snapshot saved ({len(existing)} total)")
    except Exception as exc:
        log(f"Snapshot save failed: {exc}")


# ── Memory ingestion ──────────────────────────────────────────────────────────

def _ingest_memory(text: str, source: str = "unas_monitor"):
    """Store a status summary in Nova's vector memory."""
    try:
        import urllib.request as _req
        payload = json.dumps({
            "text": text,
            "source": source,
            "tier": "working",
            "privacy": "local-only",
        }).encode()
        req = _req.Request(
            VECTOR_URL + "/remember",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _req.urlopen(req, timeout=5):
            pass
    except Exception:
        pass


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Nova UNAS Pro 8 Monitor")
    parser.add_argument("--status",   action="store_true", help="System status")
    parser.add_argument("--storage",  action="store_true", help="Storage details")
    parser.add_argument("--shares",   action="store_true", help="Shared drives")
    parser.add_argument("--json",     action="store_true", help="Output JSON")
    parser.add_argument("--problems", action="store_true", help="Only problems")
    parser.add_argument("--snapshot", action="store_true", help="Save daily snapshot")
    args = parser.parse_args()

    # Fetch snapshot
    try:
        snapshot = client.health_snapshot()
    except UNASError as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)

    # Always persist status file (for NovaControl)
    _save_status(snapshot)

    if args.json:
        print(json.dumps(snapshot, indent=2))
        return

    if args.snapshot:
        save_snapshot(snapshot)

    # Detect problems
    problems = (
        check_device(snapshot)
        + check_storage(snapshot)
        + check_shares(snapshot)
    )

    if args.problems:
        print_problems(problems)
        return

    if args.status or args.storage or args.shares or not any(vars(args).values()):
        print_status(snapshot)
        if args.problems or not any(vars(args).values()):
            print_problems(problems)

    # Alert on new problems
    state = _load_state()
    prev_problems = set(state.get("problems", []))
    curr_problems = set(problems)
    new_problems = curr_problems - prev_problems

    if new_problems:
        alert = (
            f"⚠️ *UNAS Pro 8 — New Problems Detected*\n"
            + "\n".join(f"• {p}" for p in sorted(new_problems))
        )
        post_slack(alert)
        log(f"Alerted on {len(new_problems)} new problem(s)")

    if prev_problems and not curr_problems:
        post_slack("✅ *UNAS Pro 8* — All problems resolved")
        log("All problems resolved — notified Slack")

    _save_state({
        "problems": list(curr_problems),
        "last_check": NOW.isoformat(),
        "storage_pct": snapshot.get("storage", {}).get("used_pct", 0),
    })

    # Ingest a brief status memory once per day
    if not state.get("last_check", "").startswith(TODAY):
        st = snapshot.get("storage", {})
        mem_text = (
            f"UNAS Pro 8 daily status {TODAY}: "
            f"storage {st.get('status', 'unknown')}, "
            f"{st.get('used_pct', 0):.1f}% used, "
            f"{st.get('free_tb', 0):.1f}TB free of {st.get('total_tb', 0):.1f}TB total. "
            f"{'Problems: ' + '; '.join(problems) if problems else 'No problems.'}"
        )
        _ingest_memory(mem_text)
        log("Daily memory snapshot ingested")


if __name__ == "__main__":
    main()
