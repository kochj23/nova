#!/usr/bin/env python3
"""
nova_homepod.py — Control HomePods and AirPlay devices via pyatv.

Commands:
  list                        — list all devices and current status
  status <name>               — get what's playing on a device
  volume <name> <0-100>       — set volume on a device
  say <name> <text>           — speak text through a HomePod (via macOS say + airplay)
  announce <text>             — speak text through ALL HomePods simultaneously

Written by Jordan Koch.
"""

import asyncio
import subprocess
import sys
import tempfile
import os
from pathlib import Path

try:
    import pyatv
    from pyatv.const import Protocol, DeviceState
except ImportError:
    print('{"error": "pyatv not installed. Run: pip3 install pyatv"}')
    sys.exit(1)


# ── Device Discovery ──────────────────────────────────────────────────────────

async def discover(timeout=6):
    loop = asyncio.get_event_loop()
    return await pyatv.scan(loop, timeout=timeout)


def is_homepod(device):
    """HomePods have AirPlay + Companion or RAOP but are NOT Apple TVs."""
    protocols = {str(s.protocol) for s in device.services}
    has_airplay = "Protocol.AirPlay" in protocols or "Protocol.RAOP" in protocols
    has_dmap    = "Protocol.DMAP" in protocols  # Apple TV marker
    return has_airplay and not has_dmap


# ── Status ────────────────────────────────────────────────────────────────────

async def cmd_list():
    devices = await discover()
    out = []
    for d in sorted(devices, key=lambda x: x.name):
        protocols = [str(s.protocol).replace("Protocol.", "") for s in d.services]
        pod = "🔊" if is_homepod(d) else "📺" if "DMAP" in " ".join(protocols) else "🖥️"
        out.append({
            "name":     d.name,
            "ip":       str(d.address),
            "type":     pod,
            "protocols": protocols,
        })
    import json
    print(json.dumps(out, indent=2))


async def cmd_status(name: str):
    devices = await discover()
    match = next((d for d in devices if d.name.lower() == name.lower()), None)
    if not match:
        names = [d.name for d in devices]
        print(f'{{"error": "Device not found: {name}", "available": {names}}}')
        return

    try:
        atv = await pyatv.connect(match, asyncio.get_event_loop())
        info = atv.metadata
        playing = await info.playing()
        import json
        print(json.dumps({
            "name":        match.name,
            "ip":          str(match.address),
            "state":       str(playing.device_state),
            "title":       playing.title,
            "artist":      playing.artist,
            "album":       playing.album,
            "position":    playing.position,
            "total_time":  playing.total_time,
        }, indent=2))
        await atv.close()
    except Exception as e:
        print(f'{{"name": "{name}", "error": "{e}"}}')


# ── Volume ────────────────────────────────────────────────────────────────────

async def cmd_volume(name: str, level: int):
    devices = await discover()
    match = next((d for d in devices if d.name.lower() == name.lower()), None)
    if not match:
        print(f'{{"error": "Device not found: {name}"}}')
        return

    try:
        atv = await pyatv.connect(match, asyncio.get_event_loop())
        await atv.audio.set_volume(level)
        print(f'{{"ok": true, "device": "{name}", "volume": {level}}}')
        await atv.close()
    except Exception as e:
        print(f'{{"error": "{e}"}}')


# ── TTS via macOS say + RAOP stream ───────────────────────────────────────────

async def cmd_say(name: str, text: str):
    """Generate speech with macOS say and stream to a HomePod via AirPlay."""
    devices = await discover()
    match = next((d for d in devices if d.name.lower() == name.lower()), None)
    if not match:
        print(f'{{"error": "Device not found: {name}"}}')
        return

    # Generate speech to a temp AIFF file
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as f:
        aiff_path = f.name

    try:
        subprocess.run(["say", "-o", aiff_path, "--", text], check=True)

        # Stream to device via RAOP
        atv = await pyatv.connect(match, asyncio.get_event_loop())
        await atv.stream.stream_file(aiff_path)
        await atv.close()

        print(f'{{"ok": true, "device": "{name}", "text": "{text[:50]}"}}')
    except Exception as e:
        print(f'{{"error": "{e}"}}')
    finally:
        os.unlink(aiff_path)


async def cmd_announce(text: str):
    """Speak text through all HomePods simultaneously."""
    devices = await discover()
    homepods = [d for d in devices if is_homepod(d)]

    # Generate speech once
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as f:
        aiff_path = f.name

    try:
        subprocess.run(["say", "-o", aiff_path, "--", text], check=True)

        results = []
        for d in homepods:
            try:
                atv = await pyatv.connect(d, asyncio.get_event_loop())
                await atv.stream.stream_file(aiff_path)
                await atv.close()
                results.append({"device": d.name, "ok": True})
            except Exception as e:
                results.append({"device": d.name, "ok": False, "error": str(e)})

        import json
        print(json.dumps({"announced": True, "devices": results}, indent=2))
    finally:
        os.unlink(aiff_path)


# ── Main ──────────────────────────────────────────────────────────────────────

def usage():
    print(__doc__)
    sys.exit(1)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        usage()

    cmd = args[0].lower()

    if cmd == "list":
        asyncio.run(cmd_list())
    elif cmd == "status" and len(args) >= 2:
        asyncio.run(cmd_status(" ".join(args[1:])))
    elif cmd == "volume" and len(args) == 3:
        asyncio.run(cmd_volume(args[1], int(args[2])))
    elif cmd == "say" and len(args) >= 3:
        asyncio.run(cmd_say(args[1], " ".join(args[2:])))
    elif cmd == "announce" and len(args) >= 2:
        asyncio.run(cmd_announce(" ".join(args[1:])))
    else:
        usage()
