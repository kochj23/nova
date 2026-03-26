#!/usr/bin/env python3
"""Find the whisper-stream capture device number for a given device name.
Runs whisper-stream briefly, parses the device list, exits.
Usage: python3 _voice_find_device.py "MX Brio"
Prints the device number (e.g. "1") or "0" as fallback."""
import sys, subprocess, re, time

preferred = sys.argv[1] if len(sys.argv) > 1 else "MX Brio"
fallback_excludes = ["teams", "iphone", "virtual", "aggregate"]

try:
    # whisper-stream lists devices then tries to open one — kill it right after listing
    proc = subprocess.Popen(
        ["whisper-stream",
         "-m", "/opt/homebrew/share/whisper-cpp/ggml-base.en.bin",
         "--length", "100"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    time.sleep(2)
    proc.terminate()
    out = proc.stderr.read() + proc.stdout.read()

    # Parse "Capture device #N: 'Name'"
    devices = {}
    for m in re.finditer(r"Capture device #(\d+): '([^']+)'", out):
        devices[int(m.group(1))] = m.group(2)

    # Find preferred device
    for num, name in sorted(devices.items()):
        if preferred.lower() in name.lower():
            print(num)
            sys.exit(0)

    # Find first non-excluded device
    for num, name in sorted(devices.items()):
        if not any(x in name.lower() for x in fallback_excludes):
            print(num)
            sys.exit(0)

    # Last resort
    print(0)
except Exception as e:
    print(0)
