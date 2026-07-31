#!/usr/bin/env python3
"""Twice-daily SDR analog+milair sweep. Triggers the nova-core2 harvester (30 min), summarizes
the transcribed transmissions, and posts to Slack. Transcripts are already stored to Nova memory
by the harvester itself (source='scanner'). Run by launchd morning + evening."""
import subprocess, datetime, sys
import nova_config
SDR_HOST = "192.168.1.86"
DUR = sys.argv[1] if len(sys.argv) > 1 else "1800"
SLACK = nova_config.SLACK_FEED  # #nova-feed

def sh(cmd, timeout):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", SDR_HOST, cmd],
                          capture_output=True, text=True, timeout=timeout)

def main():
    # run the harvester on the SDR host (blocks ~30 min)
    sh(f"~/scanner-venv/bin/python ~/nova_scanner_harvest.py {DUR}", int(DUR) + 300)
    log = sh("cat /tmp/harvest.log 2>/dev/null", 30).stdout
    hits = [l for l in log.splitlines() if l.strip() and not l.startswith("===")]
    when = datetime.datetime.now().strftime("%a %b %-d, %-I:%M %p")
    if hits:
        body = "\n".join(hits[:30])
        msg = (f":satellite_antenna: *SDR analog sweep — {when}*\n"
               f"{len(hits)} analog transmissions caught (stored to memory):\n```{body[:2800]}```")
    else:
        msg = (f":satellite_antenna: *SDR analog sweep — {when}*\n"
               f"No analog voice this pass (quiet window or antenna-limited on the whip).")
    nova_config.post_both(msg, slack_channel=SLACK)
    print(f"posted {len(hits)} hits to Slack")

if __name__ == "__main__":
    main()
