#!/usr/bin/env python3
"""nova_cluster_render.py — wish #60 (queue #3113 tier 1): the Nova Cluster dashboard as a picture.

Every run renders the Grafana kiosk dashboard (Grafana's image renderer) to a 1920x1080 PNG on the UNAS share
(never a Studio disk): /Volumes/nas/nova-fs/dash/nova-cluster.png (always the latest, atomic rename) and
/Volumes/nas/nova-fs/dash/hourly/nova-cluster-YYYYMMDD-HH.png (one per hour, 48 kept). TV-Movies-3 (.7)
mirrors the newest hourly frames into ~/Pictures/NovaTV/Nova (nova_tv_dash_import.sh, LaunchAgent
net.digitalnoise.nova-tv-dash); Home Sharing (folder mode) exposes that folder to the six Apple TVs as a
screensaver album. --selftest --out DIR
"""
import os, sys, json, subprocess, time, pathlib, shutil, urllib.request

# 2026-10-13: Grafana's own image renderer (one HTTP call) replaces a local headless Chromium, which timed out on
# about half the runs (93 failures/24 h) and left multi-GB code-sign clones behind. nova-cluster is the top
# section of the consolidated nova-fleet page, so a 1920x1080 kiosk frame is the same picture as before.
# 2026-10-21: a "Reliability" row now sits above "Nova Cluster" on nova-fleet, so a plain 1080-tall kiosk frame showed
# SLO tables instead of the cluster. We render tall and crop the Nova Cluster row out, located from the dashboard's
# own gridPos (Grafana grid unit = 30px cell + 8px margin; kiosk header + variable bar ~80px).
GRAFANA = "http://192.168.1.2:3000"
ROW_TITLE = "Nova Cluster"
GRID_PX, TOP_PX = 38, 80
URL = os.environ.get("NOVA_DASH_URL", GRAFANA + "/render/d/nova-fleet?kiosk=1&width=1920&height={h}"
                     "&theme=dark&tz=America%2FLos_Angeles&from=now-24h&to=now")
OUT = pathlib.Path(os.environ.get("NOVA_DASH_DIR", "/Volumes/nas/nova-fs/dash"))
KEEP_HOURLY = 48
TV_HOST = "kochj@192.168.1.7"
TV_CACHE = "Library/Caches/nova-dash"       # local on .7; its importer reads from here (see nova_tv_dash_import.sh)
STALE_S = 20 * 60          # last good frame older than this => a failed render is a real failure
MIN_BYTES = 30_000           # a blank or error page renders under this; Grafana's dashboard is a few hundred KB

def log(m): print(f"[cluster-render] {m}", flush=True)

def hourly_name(ts): return time.strftime("nova-cluster-%Y%m%d-%H.png", time.localtime(ts))

def prune(paths, keep=KEEP_HOURLY):
    """Pure: newest `keep` names survive (names sort chronologically)."""
    s = sorted(paths)
    return s[:-keep] if len(s) > keep else []

def section_px(panels, title=ROW_TITLE):
    """Pure: (top, height) in px of the row `title` up to the next row, or None. Rows must be expanded."""
    rows = sorted(p["gridPos"]["y"] for p in panels if p.get("type") == "row")
    y = next((p["gridPos"]["y"] for p in panels if p.get("type") == "row" and p.get("title") == title), None)
    if y is None:
        return None
    end = next((r for r in rows if r > y), None)
    if end is None:
        end = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in panels)
    return TOP_PX + (y + 1) * GRID_PX, (end - y - 1) * GRID_PX - 8

def render(tmp):
    sec = None
    try:
        with urllib.request.urlopen(GRAFANA + "/api/dashboards/uid/nova-fleet", timeout=20) as r:
            sec = section_px(json.load(r)["dashboard"]["panels"])
    except (OSError, KeyError, ValueError) as e:
        log(f"dashboard layout lookup failed ({e}) — rendering the top of the page")
    try:
        with urllib.request.urlopen(URL.format(h=(sec[0] + sec[1] + 40) if sec else 1080), timeout=90) as r:
            png = r.read()
    except OSError as e:
        log(f"Grafana renderer unreachable: {e}"); return False
    if not png.startswith(b"\x89PNG"):
        return False
    tmp.write_bytes(png)
    if sec:
        r = subprocess.run(["sips", "-c", str(sec[1]), "1920", "--cropOffset", str(sec[0]), "0", str(tmp), "--out", str(tmp)],
                           capture_output=True, timeout=30)
        if r.returncode != 0:
            log(f"crop failed: {r.stderr.decode()[:120]}"); return False
    return tmp.stat().st_size >= MIN_BYTES

def main():
    out = pathlib.Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else OUT
    if not out.parent.exists():
        log(f"{out.parent} not mounted — skipping (never render to a Studio disk)"); return 1
    (out / "hourly").mkdir(parents=True, exist_ok=True)
    tmp = out / ".nova-cluster.tmp.png"
    if not render(tmp):
        log(f"render failed or too small ({tmp.stat().st_size if tmp.exists() else 0} B) — keeping last good frame")
        # One missed refresh is harmless while the last frame is still fresh. The 02:00 pg_backup slows the
        # dashboard's PG panels past the renderer's timeout (Grafana 500 rendering.serverTimeout) for one run
        # a night; only a STALE frame is a failure worth paging on.
        last = out / "nova-cluster.png"
        age = time.time() - last.stat().st_mtime if last.exists() else None
        if age is not None and age < STALE_S:
            log(f"last good frame is {int(age // 60)} min old — not a failure"); return 0
        return 1
    os.replace(tmp, out / "nova-cluster.png")
    h = out / "hourly" / hourly_name(time.time())
    if not h.exists():
        shutil.copyfile(out / "nova-cluster.png", h)
        # push the hourly frame to TV-Movies-3's LOCAL cache: its Photos-import LaunchAgent cannot read the SMB share
        # (TCC blocks network volumes for launchd jobs; only ssh/FDA can). Best effort; the NAS copy is the record.
        r = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", str(h), f"{TV_HOST}:{TV_CACHE}/"],
                           capture_output=True, timeout=60)
        log(f"pushed {h.name} to {TV_HOST}" if r.returncode == 0 else f"push to {TV_HOST} failed: {r.stderr.decode()[:120]}")
    for old in prune([p.name for p in (out / "hourly").glob("nova-cluster-*.png")]):
        (out / "hourly" / old).unlink(missing_ok=True)
    log(f"ok {out / 'nova-cluster.png'} ({(out / 'nova-cluster.png').stat().st_size // 1024} KB), hourly={h.name}")
    return 0

def selftest():
    names = [f"nova-cluster-202610{d:02d}-{h:02d}.png" for d in (1, 2, 3) for h in range(24)]
    gone = prune(names, keep=48)
    assert len(gone) == 24 and gone[0] == "nova-cluster-20261001-00.png" and "nova-cluster-20261003-23.png" not in gone
    assert prune(names[:10], keep=48) == []
    assert hourly_name(0).startswith("nova-cluster-19")
    row = lambda t, y: {"type": "row", "title": t, "gridPos": {"y": y, "h": 1}}
    assert section_px([row("Reliability", 0), row("Nova Cluster", 28), row("Hosts", 56)]) == (80 + 29 * 38, 27 * 38 - 8)
    assert section_px([row("Reliability", 0)]) is None
    print("selftest ok")

if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else main())
