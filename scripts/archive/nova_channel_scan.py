#!/usr/bin/env python3
"""
nova_channel_scan.py — Scan all HDHomeRun channels for signal quality.
Records 20s from each channel, builds a good/bad list in livetv_novas_prefs.json.
Posts progress + final whitelist to #nova-notifications.

Written by Jordan Koch.
"""

import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

HDHR_LINEUP  = "http://192.168.1.89/lineup.json"
HDHR_STREAM  = "http://192.168.1.89:5004/auto/v"
FFMPEG       = "/opt/homebrew/bin/ffmpeg"
WORK_DIR     = Path("/Volumes/Data/nova-livetv/scan")
PREFS_FILE   = Path.home() / ".openclaw/workspace/livetv_novas_prefs.json"
LOG_FILE     = "/tmp/nova-channel-scan.log"
RECORD_SECS  = 20
MIN_BYTES    = 50_000   # ~50KB = real audio signal
PAUSE_SECS   = 3        # between channels


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def slack(msg: str):
    nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)


def load_prefs() -> dict:
    if PREFS_FILE.exists():
        try:
            return json.loads(PREFS_FILE.read_text())
        except Exception:
            pass
    return {"viewed": [], "favorites": [], "history_count": 0, "bad_channels": {}}


def save_prefs(prefs: dict):
    PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PREFS_FILE.write_text(json.dumps(prefs, indent=2))


def get_lineup() -> list[dict]:
    try:
        with urllib.request.urlopen(HDHR_LINEUP, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"ERROR: Could not fetch lineup: {e}")
        return []


def test_channel(ch_num: str) -> tuple[bool, int]:
    """Record 20s from a channel. Returns (success, file_size_bytes)."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    safe = ch_num.replace(".", "_")
    outfile = WORK_DIR / f"scan_{safe}.wav"
    url = f"{HDHR_STREAM}{ch_num}"

    try:
        result = subprocess.run(
            [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
             "-i", url, "-t", str(RECORD_SECS),
             "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
             str(outfile)],
            capture_output=True, text=True, timeout=RECORD_SECS + 15,
        )
        size = outfile.stat().st_size if outfile.exists() else 0
        outfile.unlink(missing_ok=True)
        return result.returncode == 0 and size > MIN_BYTES, size
    except subprocess.TimeoutExpired:
        outfile.unlink(missing_ok=True)
        return False, 0
    except Exception:
        outfile.unlink(missing_ok=True)
        return False, 0


def sort_key(ch: str) -> tuple:
    parts = ch.split(".")
    try:
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except ValueError:
        return (999, 0)


def main():
    log("=== Nova Channel Scan Starting ===")
    lineup = get_lineup()
    if not lineup:
        log("No lineup — is HDHomeRun reachable?")
        sys.exit(1)

    total = len(lineup)
    log(f"Scanning {total} channels...")
    slack(f":satellite_antenna: *Nova Channel Scan Starting* — testing {total} OTA channels for signal. Results posted when complete.")

    prefs = load_prefs()
    prefs.setdefault("bad_channels", {})

    good = []
    bad = []

    for i, ch_info in enumerate(lineup, 1):
        ch_num = ch_info["GuideNumber"]
        ch_name = ch_info.get("GuideName", ch_num)

        ok, size = test_channel(ch_num)

        if ok:
            log(f"✓ [{i}/{total}] ch {ch_num} ({ch_name}) — {size:,} bytes")
            good.append({"ch": ch_num, "name": ch_name})
            # Remove from bad list if previously flagged
            prefs["bad_channels"].pop(ch_num, None)
        else:
            log(f"✗ [{i}/{total}] ch {ch_num} ({ch_name}) — no signal")
            bad.append({"ch": ch_num, "name": ch_name})
            entry = prefs["bad_channels"].get(ch_num, {"ch": ch_num, "name": ch_name, "failures": 0})
            entry["failures"] = max(entry.get("failures", 0), 2)  # force to skip threshold
            entry["last_failed"] = datetime.now().isoformat()
            entry["reason"] = "no signal — channel scan"
            prefs["bad_channels"][ch_num] = entry

        # Save incrementally so progress survives interruption
        if i % 10 == 0:
            prefs["whitelist"] = good[:]
            save_prefs(prefs)

        time.sleep(PAUSE_SECS)

    # Final save
    prefs["whitelist"] = good
    prefs["whitelist_updated"] = datetime.now().isoformat()
    save_prefs(prefs)

    # Build summary
    good_sorted = sorted(good, key=lambda x: sort_key(x["ch"]))
    bad_sorted  = sorted(bad,  key=lambda x: sort_key(x["ch"]))

    good_list = "\n".join(f"  ch {c['ch']} — {c['name']}" for c in good_sorted)
    bad_list  = "\n".join(f"  ch {c['ch']} — {c['name']}" for c in bad_sorted)

    msg = (
        f":satellite_antenna: *Nova Channel Scan Complete*\n"
        f":white_check_mark: *{len(good)} working channels* (whitelisted for TV time)\n"
        f":x: *{len(bad)} dead channels* (blocked — no signal)\n\n"
        f"*Working:*\n{good_list or '(none)'}\n\n"
        f"*Blocked:*\n{bad_list or '(none)'}"
    )

    # Slack has a 3000-char limit per message — split if needed
    if len(msg) <= 3000:
        slack(msg)
    else:
        slack(
            f":satellite_antenna: *Nova Channel Scan Complete*\n"
            f":white_check_mark: *{len(good)} working* | :x: *{len(bad)} blocked*\n"
            f"Full list saved to `livetv_novas_prefs.json`."
        )

    log(f"=== Scan complete: {len(good)} good, {len(bad)} bad ===")


if __name__ == "__main__":
    main()
