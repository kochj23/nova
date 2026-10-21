#!/usr/bin/env python3
"""nova_game_autoinstall.py — keep Jordan's desktop on the newest build of every Nova game.

Jordan, 2026-10-20: "Always install the latest versions of the game on my desktop."

Runs on the Studio from LaunchAgent net.digitalnoise.game-autoinstall every minute. For each game it finds the
newest exported build under the binaries dir (highest CFBundleVersion, then version string) and, when it is newer than
the installed copy AND the game is not running, swaps it in place: wherever /Applications/<Game>.app really points
(CarTest's /Applications entry is a symlink to /Volumes/Data/Applications). A running game is never touched; it
gets the update on the first run after he quits. No state: the installed bundle is the record.

ponytail: a fixed game list; add a line when a new game ships.
Written by Jordan Koch (via Claude).
"""
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

BINARIES = Path("/Volumes/Data/xcode/binaries")
APPS = Path("/Applications")
GAMES = ("GTNW", "CarTest", "Blompie", "StuntDriver")


def log(m):
    print(f"[game-autoinstall] {m}", flush=True)


def build_of(app: Path) -> tuple:
    """(build, version) as int tuples for ordering; () if unreadable. Pure apart from the plist read."""
    try:
        info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    except Exception:  # noqa: BLE001 — a half-copied export is just skipped
        return ()

    def ints(s):
        return tuple(int(p) for p in str(s or "0").split(".") if p.isdigit())
    return ints(info.get("CFBundleVersion")), ints(info.get("CFBundleShortVersionString"))


def newest_export(game: str, root: Path = BINARIES):
    best, best_key = None, ()
    for app in root.glob(f"*/{game}.app"):
        key = build_of(app)
        if key and key > best_key:
            best, best_key = app, key
    return best, best_key


def team_of(app: Path) -> str | None:
    """Team ID of a bundle whose signature verifies (deep, strict); None if unsigned, tampered or unreadable."""
    if subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True).returncode:
        return None
    out = subprocess.run(["codesign", "-dv", str(app)], capture_output=True, text=True).stderr
    m = next((ln.split("=", 1)[1].strip() for ln in out.splitlines() if ln.startswith("TeamIdentifier=")), None)
    return m if m and m != "not set" else None


def running(game: str) -> bool:
    return subprocess.run(["pgrep", "-x", game], capture_output=True).returncode == 0


def install(src: Path, target: Path) -> None:
    """Copy next to the target, then swap, so a failed copy never leaves him without the game."""
    tmp = target.with_name(target.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    subprocess.run(["ditto", str(src), str(tmp)], check=True)
    old = target.with_name(target.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        target.rename(old)
    tmp.rename(target)
    shutil.rmtree(old, ignore_errors=True)


def check(game: str, apps: Path = APPS, root: Path = BINARIES, _running=running, _install=install,
          _team=team_of) -> str:
    link = apps / f"{game}.app"
    if not link.exists():
        return "not installed"
    target = Path(os.path.realpath(link))
    src, key = newest_export(game, root)
    have = build_of(target)
    if not src or key <= have:
        return "current"
    # the binaries dir is writable by anything running as Jordan: only a validly signed build from the same
    # developer team as the copy he already runs is ever installed (Jordan 2026-10-20: "No worms, viruses, etc")
    want, got = _team(target), _team(src)
    if not want or got != want:
        return f"REFUSED {src.parent.name}: signature invalid or team {got} != installed {want}"
    if _running(game):
        return f"newer build waiting ({src.parent.name}); {game} is running"
    _install(src, target)
    return f"installed {src.parent.name}"


def main() -> int:
    for game in GAMES:
        try:
            log(f"{game}: {check(game)}")
        except Exception as e:  # noqa: BLE001 — one game's failure never blocks the others
            log(f"{game}: FAILED {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
