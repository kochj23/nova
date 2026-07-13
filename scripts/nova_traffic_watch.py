#!/usr/bin/env python3
"""
nova_traffic_watch.py — "Foothill Watch": Nova's commute & wildfire/incident sentinel.

Polls public Caltrans D7 traffic-camera snapshots around Burbank, Glendale,
Glassell Park and Pasadena, captions each frame with the local vision model,
stores one ambient digest to Nova's memory (so Nova can answer "how's the 134?"),
and Slacks Jordan when a hazard appears — smoke/fire on the foothill cams
(Verdugo / San Gabriel brush country) or a crash / stoppage / emergency response
on a commute corridor.

All processing is local: snapshots are public, captioning is qwen3-vl on Ollama,
memory + Slack reuse nova_vision_analyzer.

Usage:
  python3 nova_traffic_watch.py                 # one pass over all cameras
  python3 nova_traffic_watch.py --role fire     # only brush-adjacent cams (cheap fire scan)
  python3 nova_traffic_watch.py --role commute  # only commute corridors
  python3 nova_traffic_watch.py --limit 5       # cap cameras this pass (testing)
  python3 nova_traffic_watch.py --selftest      # offline checks + one live snapshot fetch

Written by Jordan Koch.
"""

import argparse
import re
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from traffic_cams import TRAFFIC_CAMERAS
# reuse Nova's existing vision + memory + alert plumbing
from nova_vision_analyzer import describe_image, remember, slack_post, log

SOURCE = "traffic_cams"
# qwen2.5-vl:3b — fast (sub-second/img warm), accurate, NON-thinking. Free/local.
# (moondream 1.8B hallucinated badly; qwen3-vl is a reasoning model that burns the budget thinking
#  and stays the home-security default. Traffic data is public, so OpenRouter is also fine as a
#  drop-in if local GPU is ever busy — set VISION_MODEL + route describe_image accordingly.)
VISION_MODEL = "qwen2.5vl:3b"

# Hazard tokens. "none" = nothing actionable.
HAZARDS = {"smoke", "fire", "crash", "stopped", "emergency", "flood", "closure"}

# qwen2.5-vl is capable enough to classify the scene itself — far more reliable than regex on prose
# (list-negations like "no smoke, flames, or flooding" defeat keyword matching). Ask for one sentence
# then a structured HAZARD token, and trust the model's verdict.
_TAG = (" Then on a new line write exactly 'HAZARD: ' followed by ONE word: none, smoke, fire, crash, "
        "stopped, emergency, flood, or closure (use none if nothing is wrong). If the image is a "
        "'Temporarily Unavailable' placeholder, write 'HAZARD: unavailable'.")
FIRE_PROMPT = (
    "This is a freeway camera in the Los Angeles foothills (brush/wildfire country). In one sentence "
    "describe the traffic flow and whether there is any smoke, haze, flames, or glow on the hillsides."
    + _TAG
)
COMMUTE_PROMPT = (
    "Describe this freeway traffic camera in one sentence: the traffic flow and any crash, stalled "
    "vehicle, emergency vehicles, debris, or flooding." + _TAG
)


def fetch_snapshot(url):
    """Download a camera snapshot to a temp jpg; cache-bust per call. Returns path or None."""
    # ponytail: per-second cache-bust is plenty; snapshots only refresh ~1/min.
    bust = f"{url}?_={int(datetime.now().timestamp())}"
    try:
        req = urllib.request.Request(bust, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        if len(data) < 1000:  # truncated / placeholder = treat as offline
            return None
        f = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        f.write(data)
        f.close()
        return f.name
    except Exception as e:
        log(f"snapshot fetch failed {url}: {e}")
        return None


_HAZARD_RE = re.compile(r"HAZARD:\s*([a-zA-Z]+)\.?", re.IGNORECASE)


def parse_hazard(caption):
    """Read the model's 'HAZARD: <token>' verdict (inline or own line). Returns (clean_text, hazard).
    hazard is a real hazard token, None for none, or 'unavailable' for placeholder frames."""
    if not caption:
        return "", None
    hazard = None
    m = _HAZARD_RE.search(caption)
    if m:
        tok = m.group(1).lower()
        if tok == "unavailable":
            hazard = "unavailable"
        elif tok in HAZARDS:
            hazard = tok  # "none"/junk -> stays None
    text = " ".join(_HAZARD_RE.sub("", caption).split()).strip()
    return text, hazard


def watch(role=None, limit=None):
    cams = list(TRAFFIC_CAMERAS.items())
    if role:
        cams = [(k, v) for k, v in cams if v["role"] == role]
    if limit:
        cams = cams[:limit]

    log(f"Foothill Watch pass: {len(cams)} cameras (role={role or 'all'})")
    # warm the vision model once so the first camera doesn't eat a cold-load timeout
    try:
        urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=f'{{"model":"{VISION_MODEL}","prompt":"ok","stream":false,"keep_alive":"30m"}}'.encode(),
            headers={"Content-Type": "application/json"}), timeout=120).read()
    except Exception as e:
        log(f"warmup skipped: {e}")
    digest, hazards = [], []

    # ponytail: sequential. ~2-4s/cam on qwen3-vl:4b; fine at a 15-min interval.
    # If a pass starts overrunning the interval, thread the fetch+describe loop.
    for cid, cam in cams:
        path = fetch_snapshot(cam["url"])
        if not path:
            log(f"  {cam['name']}: offline")
            continue
        prompt = FIRE_PROMPT if cam["role"] == "fire" else COMMUTE_PROMPT
        text, hazard = parse_hazard(describe_image(path, prompt, model=VISION_MODEL))
        Path(path).unlink(missing_ok=True)
        # Caltrans serves "Temporarily Unavailable" as a real ~16KB JPEG; the model tags it for us.
        if hazard == "unavailable" or any(k in text.lower() for k in ("temporarily unavailable", "no signal")):
            log(f"  {cam['name']}: placeholder (camera offline)")
            continue
        if not text:
            log(f"  {cam['name']}: empty caption (skipped)")  # visibility, no silent drops
            continue
        digest.append(f"[{cam['area']}] {cam['name']}: {text}")
        log(f"  {cam['name']}: {text}" + (f"  ⚠ {hazard}" if hazard else ""))
        if hazard:
            hazards.append((cid, cam, hazard, text))

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if digest:
        remember(f"Traffic snapshot {stamp} (Burbank/Glendale/Glassell Park/Pasadena):\n"
                 + "\n".join(digest), source=SOURCE)

    for cid, cam, hazard, text in hazards:
        emoji = {"smoke": "🌫️", "fire": "🔥", "flood": "🌊"}.get(hazard, "🚨")
        level = "critical" if hazard in ("fire", "smoke", "flood") else "warning"
        slack_post(
            f"{emoji} {hazard.upper()} — {cam['name']} ({cam['area']})\n{text}\n"
            f"https://cwwp2.dot.ca.gov/vm/iframemap.htm?long={cam['lon']}&lat={cam['lat']}&zoom=18",
            level=level, category="traffic_watch",
            dedup_key=f"trafficcam-{cid}-{hazard}",  # suppress repeats while a hazard persists
        )

    log(f"pass complete: {len(digest)} captioned, {len(hazards)} hazard(s)")
    return len(digest), len(hazards)


def selftest():
    # the model's HAZARD token is authoritative; the description line is kept, the tag line stripped
    t, h = parse_hazard("Heavy stop-and-go traffic with a stalled car.\nHAZARD: stopped")
    assert h == "stopped" and "HAZARD" not in t and "stalled" in t, (t, h)
    # "none" -> no alert, EVEN when scary words appear in the (negated) description
    assert parse_hazard("Free-flowing, no smoke, flames, or flooding visible.\nHAZARD: none")[1] is None
    # placeholder verdict surfaces as 'unavailable' (loop treats as offline, never alerts)
    assert parse_hazard("The image is unavailable.\nHAZARD: unavailable")[1] == "unavailable"
    # real positives pass through
    assert parse_hazard("Thick smoke over the ridge.\nHAZARD: smoke")[1] == "smoke"
    # junk token ignored, empty safe
    assert parse_hazard("Clear.\nHAZARD: banana")[1] is None
    assert parse_hazard(None) == ("", None)
    # config sanity
    assert len(TRAFFIC_CAMERAS) >= 1
    assert all({"name", "url", "area", "role"} <= v.keys() for v in TRAFFIC_CAMERAS.values())
    # one live snapshot actually fetches
    any_url = next(iter(TRAFFIC_CAMERAS.values()))["url"]
    p = fetch_snapshot(any_url)
    assert p and Path(p).stat().st_size > 1000, "live snapshot fetch failed"
    Path(p).unlink(missing_ok=True)
    print("selftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=["commute", "fire"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
    else:
        watch(role=args.role, limit=args.limit)
