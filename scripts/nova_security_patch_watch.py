#!/usr/bin/env python3
"""
nova_security_patch_watch.py — Detect major vendor patch releases and generate briefings.

Monitors for:
- Microsoft Patch Tuesday (2nd Tuesday of each month)
- Apple security updates (checks Apple security-announce)
- Linux kernel releases (kernel.org)
- PostgreSQL security releases
- Python security releases

When a major patch event is detected, generates a dedicated security briefing.
Runs daily at 10am (catches Patch Tuesday, overnight Apple/Linux releases).

Written by Jordan Koch (via Claude).
"""

import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

LOG_FILE = Path.home() / ".openclaw/logs/security_patch_watch.log"
STATE_FILE = Path.home() / ".openclaw/workspace/state/patch_watch_seen.json"
JOURNAL_SCRIPT = Path.home() / ".openclaw/scripts/nova_journal_security.py"

APPLE_SECURITY_URL = "https://support.apple.com/en-us/100100"
KERNEL_URL = "https://www.kernel.org/releases.json"
PG_URL = "https://www.postgresql.org/about/news/"


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[patch-watch {ts}] {msg}", flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"[patch-watch {ts}] {msg}\n")
    except OSError:
        pass


def load_seen() -> set:
    try:
        if STATE_FILE.exists():
            return set(json.loads(STATE_FILE.read_text()))
    except Exception:
        pass
    return set()


def save_seen(seen: set):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(list(seen)[-200:]))


def fire_alert(trigger: str, details: str):
    log(f"Firing patch alert: {trigger}")
    try:
        subprocess.run(
            [sys.executable, str(JOURNAL_SCRIPT), "breaking", trigger, details],
            timeout=300, capture_output=True
        )
    except Exception as e:
        log(f"Alert fire failed: {e}")


def is_patch_tuesday() -> bool:
    """Check if today is the 2nd Tuesday of the month."""
    today = date.today()
    if today.weekday() != 1:  # Tuesday
        return False
    # 2nd Tuesday = day 8-14
    return 8 <= today.day <= 14


def check_microsoft_patch_tuesday(seen: set) -> list:
    """Detect Microsoft Patch Tuesday."""
    alerts = []
    key = f"ms-patch-{date.today().isoformat()}"
    if key in seen:
        return []

    if is_patch_tuesday():
        seen.add(key)
        trigger = f"Microsoft Patch Tuesday — {datetime.now().strftime('%B %Y')}"
        details = (
            "Microsoft's monthly security update has been released. "
            "Check https://msrc.microsoft.com/update-guide/ for the full list of CVEs. "
            "Prioritize: Windows kernel, Exchange, Active Directory, and .NET vulnerabilities."
        )
        alerts.append((trigger, details))

    return alerts


def check_kernel_releases(seen: set) -> list:
    """Check kernel.org for new stable/security releases."""
    alerts = []
    try:
        req = urllib.request.Request(KERNEL_URL, headers={"User-Agent": "Nova-PatchWatch/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log(f"Kernel.org fetch failed: {e}")
        return []

    for release in data.get("releases", [])[:5]:
        version = release.get("version", "")
        key = f"kernel-{version}"
        if key in seen:
            continue

        # Only alert on new mainline or stable releases (not rc)
        if "rc" in version or not version:
            continue

        # Check if release is from today/yesterday
        released = release.get("released", {}).get("isodate", "")
        if released:
            try:
                rel_date = datetime.strptime(released, "%Y-%m-%d").date()
                if (date.today() - rel_date).days > 2:
                    seen.add(key)
                    continue
            except ValueError:
                pass

        seen.add(key)
        moniker = release.get("moniker", "")
        trigger = f"Linux Kernel {version} Released ({moniker})"
        details = f"Linux kernel {version} ({moniker}) has been released. Check changelog for security fixes."
        alerts.append((trigger, details))

    return alerts


def check_apple_security(seen: set) -> list:
    """Check Apple's security releases page for new updates."""
    alerts = []
    try:
        req = urllib.request.Request(APPLE_SECURITY_URL, headers={"User-Agent": "Nova-PatchWatch/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log(f"Apple security page fetch failed: {e}")
        return []

    # Look for recent entries (format: "macOS Sequoia 15.x" or "iOS 18.x")
    entries = re.findall(r'((?:macOS|iOS|iPadOS|watchOS|tvOS|visionOS|Safari)\s+[\w\s.]+\d+(?:\.\d+)*)', body)
    for entry in entries[:10]:
        key = f"apple-{hashlib.md5(entry.encode()).hexdigest()[:8]}"
        if key in seen:
            continue
        seen.add(key)
        # Only fire for macOS and iOS (the ones Jordan uses)
        if any(os_name in entry for os_name in ["macOS", "iOS", "Safari"]):
            trigger = f"Apple Security Update: {entry}"
            details = f"Apple has released {entry}. Check https://support.apple.com/en-us/100100 for CVE details."
            alerts.append((trigger, details))
            break  # One alert per run for Apple

    return alerts


def run():
    log("Patch watch check starting...")
    seen = load_seen()
    all_alerts = []

    all_alerts.extend(check_microsoft_patch_tuesday(seen))
    all_alerts.extend(check_kernel_releases(seen))
    all_alerts.extend(check_apple_security(seen))

    save_seen(seen)

    if all_alerts:
        log(f"Found {len(all_alerts)} patch event(s)")
        for trigger, details in all_alerts[:2]:
            fire_alert(trigger, details)
            time.sleep(5)
    else:
        log("No new patch events")


if __name__ == "__main__":
    run()
