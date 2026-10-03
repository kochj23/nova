#!/usr/bin/env python3
"""nova_mac_media_folder_guard.py — keep Music.app / TV.app's media folder on the UNAS share.

WHY THIS EXISTS (TV-Movies-3, 2026-10-03)
  At login AMPLibraryAgent / Music / TV resolve their `media-folder-bookmark` BEFORE
  nova_mac_share_mount.py has /Volumes/nas mounted (that takes 2-3 min after login).
  The resolve fails and the apps silently rewrite `media-folder-url` to the default
  ~/Music/Music/Media.localized (resp. ~/Movies/TV/Media.localized) — observed after
  the 2026-10-03 reboot. Existing tracks keep their per-track locations on the `nas`
  volume, so Home Sharing playback survives, but new imports / "Automatically Add"
  would land on the local SSD. Jordan's rule: UNAS (192.168.1.69) is primary, so
  this guard re-points both apps at /Volumes/nas/iTunes once the mount is readable.

HOW
  * Runs as a kochj LaunchAgent every 120 s (same cadence as the share mount).
  * TCC: an unsigned LaunchAgent cannot read ~/Library/Preferences/com.apple.Music.plist
    nor network volumes, so every read/write goes through `sudo -n` (passwordless)
    and the plists are chown'ed back to kochj:staff afterwards.
  * Only acts on DRIFT (url != UNAS url). Then: quit Music/TV if running, regenerate
    the bookmark with a tiny swift snippet (bookmark data embeds the volume's
    smb:// URL, so it MUST be made while the UNAS mount is up), write url + bookmark,
    flush cfprefsd, bounce AMPLibraryAgent (launchd respawns it), relaunch any app
    we quit. Backups: <plist>.bak-media-guard.
"""
import os
import subprocess
import sys
import time

MEDIA_DIR = "/Volumes/nas/iTunes"
MEDIA_URL = "file:///Volumes/nas/iTunes/"
APPS = {  # app name -> prefs plist
    "Music": os.path.expanduser("~/Library/Preferences/com.apple.Music.plist"),
    "TV": os.path.expanduser("~/Library/Preferences/com.apple.TV.plist"),
}
SWIFT_SRC = os.path.expanduser("~/.openclaw/state/mk_bookmark.swift")
SWIFT_BODY = '''import Foundation
let url = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let data = try! url.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil)
print(data.base64EncodedString())
'''


def log(m):
    print(f"[media-folder-guard] {time.strftime('%Y-%m-%d %H:%M:%S')} {m}", flush=True)


def run(cmd, timeout=60):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def media_readable():
    # root is not TCC-blocked on the share; the agent itself is.
    return run(["sudo", "-n", "/bin/ls", MEDIA_DIR], timeout=15).returncode == 0


def current_url(plist):
    r = run(["sudo", "-n", "plutil", "-extract", "media-folder-url", "raw", "-o", "-", plist], timeout=15)
    return r.stdout.strip() if r.returncode == 0 else None


def running(app):
    return run(["pgrep", "-x", app], timeout=5).returncode == 0


def make_bookmark():
    os.makedirs(os.path.dirname(SWIFT_SRC), exist_ok=True)
    with open(SWIFT_SRC, "w") as f:
        f.write(SWIFT_BODY)
    r = run(["/usr/bin/swift", SWIFT_SRC, MEDIA_DIR], timeout=120)
    b64 = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if r.returncode != 0 or len(b64) < 100:
        log(f"bookmark generation failed: {(r.stderr or '').strip()[:200]}")
        return None
    return b64


def notify(msg, level="info"):
    try:
        sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
        from nova_notify import notify as _n
        _n(f"Media folder guard: {msg}", level=level, category="storage",
           source="nova_mac_media_folder_guard.py", dedup_key="mac-media-folder-guard",
           meta={"dedup_window_s": 3600})
    except Exception:
        pass


def main():
    check_only = "--check" in sys.argv
    if not media_readable():
        return 0  # mount not up yet; the share-mount agent will bring it
    drifted = {a: p for a, p in APPS.items() if current_url(p) != MEDIA_URL}
    if not drifted:
        return 0
    for a, p in drifted.items():
        log(f"{a}: media-folder-url is {current_url(p)!r}, expected {MEDIA_URL!r}")
    if check_only:
        return 1
    b64 = make_bookmark()
    if not b64:
        return 1
    relaunch = [a for a in drifted if running(a)]
    for a in relaunch:
        run(["pkill", "-TERM", "-x", a]); log(f"{a}: quit for repoint")
    if relaunch:
        time.sleep(5)
    for a, p in drifted.items():
        run(["sudo", "-n", "cp", "-p", p, p + ".bak-media-guard"])
        ok = (run(["sudo", "-n", "plutil", "-replace", "media-folder-url", "-string", MEDIA_URL, p]).returncode == 0
              and run(["sudo", "-n", "plutil", "-replace", "media-folder-bookmark", "-data", b64, p]).returncode == 0)
        run(["sudo", "-n", "chown", "kochj:staff", p, p + ".bak-media-guard"])
        log(f"{a}: {'repointed to ' + MEDIA_URL if ok else 'WRITE FAILED'}")
    run(["sudo", "-n", "killall", "-u", "kochj", "cfprefsd"])
    run(["pkill", "-x", "AMPLibraryAgent"])  # launchd respawns it; it re-reads the prefs
    for a in relaunch:
        run(["open", "-a", a]); log(f"{a}: relaunched")
    notify(f"re-pointed {', '.join(drifted)} media folder to {MEDIA_URL} on {os.uname().nodename}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
