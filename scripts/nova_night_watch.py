#!/usr/bin/env python3
"""nova_night_watch.py — Night Watch: what happened around the house between 22:00 and 07:00.

Orson, the dog in Koontz's "Fear Nothing", keeps watch all night and reports in the morning.
This is the morning report: 3-5 lines, evidence only, posted to Jordan's #nova-chat (Slack
only — never Discord, since it describes the house). On a night with nothing above baseline it
posts a single line instead.

What it reads (22:00 yesterday -> 07:00 today, local):
  * Exterior cameras (Frigate): detections collapsed to episodes and classified ONLY by the
    label the pipeline gave (person / vehicle / cat / dog / ...). Person episodes are compared
    with the 95th percentile of the same window on the previous 14 nights.
  * Scanner transmissions the geo-enricher placed within 1 mi of home (count + closest).
  * Helicopters low and close (ADS-B), with sustained tight orbits called out.
  * Network: never-seen devices from the security organ; new Buick 8 Logbook entries.
  * Bedroom evidence for a sleep window: Jordan's phone placed in master_bedroom by BLE, and
    the bedroom FP2 mmWave reporting occupied. Reported as evidence, never as a diagnosis.
  * Bodach Watch: the highest score recorded overnight.

No street addresses, bearings or home coordinates appear in the post.

Usage: nova_night_watch.py [--dry-run] [--date YYYY-MM-DD]
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402


try:  # P3: camera/face data serve safety or presence only, to Jordan or an internal store
    import nova_privacy_guards as _privacy  # noqa: E402
except Exception:  # noqa: BLE001 — fail closed: no guard, no camera/face data
    _privacy = None


def _camera_ok(purpose: str, recipient: str = "internal") -> bool:
    """nova_privacy_guards.camera_use_ok, failing closed if the guard is missing or errors."""
    if _privacy is None:
        W.log(TAG, "privacy guard unavailable — camera/face data not used")
        return False
    try:
        ok, why = _privacy.camera_use_ok(purpose, recipient)
    except Exception as e:  # noqa: BLE001
        W.log(TAG, f"privacy guard failed ({e}) — camera/face data not used")
        return False
    if not ok:
        W.log(TAG, f"privacy guard refused camera/face use: {why}")
    return ok


def _private(md: dict, kind: str) -> dict:
    """Tag a face/camera output private (tag_private); untagged if the guard is missing."""
    return _privacy.tag_private(md, kind) if _privacy is not None else dict(md or {}, privacy="private")

TAG = "night-watch"
NEAR_MI = 1.0
VEHICLE_LABELS = {"car", "truck", "motorcycle", "bus", "bicycle", "vehicle"}
BASELINE_NIGHTS = 14


def night_bounds(day):
    """22:00 the evening before `day` -> 07:00 on `day`, local, as aware datetimes."""
    end = datetime(day.year, day.month, day.day, 7, 0, tzinfo=W.TZ)
    return end - timedelta(hours=9), end


def classify(label: str) -> str:
    l = (label or "").lower()
    if l in VEHICLE_LABELS:
        return "vehicle"
    return l or "unlabelled"


def hhmm(ts) -> str:
    return ts.astimezone(W.TZ).strftime("%H:%M")


def gather(cur, start, end) -> dict:
    # P3: the report goes to Jordan's own #nova-chat; check before any camera data is read.
    det = (W.load_ext_detections(cur, start - timedelta(days=BASELINE_NIGHTS), end)
           if _camera_ok("safety", "slack:jordan") else [])
    eps = W.episodes(((ts, (cam, classify(lbl), room)) for ts, cam, room, lbl in det), gap_s=120)
    tonight = [e for e in eps if start <= e[1] < end]
    by_class: dict = {}
    person_zones: dict = {}
    for (cam, cls, room), s, _e, _n in tonight:
        by_class[cls] = by_class.get(cls, 0) + 1
        if cls == "person":
            person_zones[room or cam] = person_zones.get(room or cam, 0) + 1
    person_ts = sorted(e[1] for e in eps if e[0][1] == "person")
    hist = [W.count_between(person_ts, start - timedelta(days=d), end - timedelta(days=d))
            for d in range(1, BASELINE_NIGHTS + 1)]
    deep = [e for e in tonight if e[0][1] == "person" and 1 <= e[1].astimezone(W.TZ).hour < 5]

    scanner = W.load_scanner_near(start, end, NEAR_MI)
    loiters = W.heli_loiters(W.load_heli(cur, start, end))
    newdev = W.load_new_devices(cur, start, end)

    cur.execute("SELECT kind, description FROM unexplained_events WHERE first_seen >= %s AND first_seen < %s",
                (start, end))
    buick = cur.fetchall()
    cur.execute("SELECT max(score), bool_or(fired), max(n_types) FROM bodach_scores "
                "WHERE window_end > %s AND window_end <= %s", (start, end))
    bmax, bfired, btypes = cur.fetchone()

    cur.execute("SELECT min(ts), max(ts), count(*) FROM telemetry.presence WHERE person='jordan' "
                "AND room='master_bedroom' AND ts >= %s AND ts < %s", (start, end))
    jb = cur.fetchone()
    cur.execute("SELECT min(ts), max(ts), count(*) FROM telemetry.presence WHERE method='mmwave' "
                "AND room='master_bedroom' AND metadata->>'occupied'='true' AND ts >= %s AND ts < %s",
                (start, end))
    mm = cur.fetchone()
    return {
        "by_class": by_class, "person_zones": person_zones,
        "person_n": by_class.get("person", 0), "person_p95": W.percentile(hist, 0.95),
        "deep_person": len(deep),
        "scanner": scanner, "loiters": loiters, "newdev": newdev, "buick": buick,
        "bodach_max": float(bmax or 0), "bodach_fired": bool(bfired), "bodach_types": int(btypes or 0),
        "bed_phone": jb, "bed_mmwave": mm,
    }


def notable(g: dict) -> list:
    """Reasons this night is worth more than one line. Pure."""
    why = []
    if g["person_n"] > max(2, g["person_p95"]):
        why.append("exterior person activity above baseline")
    if g["deep_person"] and g["person_n"] > g["person_p95"]:
        why.append("person episodes between 01:00 and 05:00")
    if g["scanner"]:
        why.append("scanner traffic near home")
    if any(l.get("tight") and l["hits"] >= 30 for l in g["loiters"]):
        why.append("sustained helicopter orbit")
    if g["newdev"]:
        why.append("never-seen network device")
    if g["buick"]:
        why.append("new unexplained events")
    if g["bodach_fired"] or g["bodach_types"] >= 2:
        why.append("Bodach Watch saw independent signals cluster")
    return why


def sleep_line(g: dict) -> str:
    ph, mm = g["bed_phone"], g["bed_mmwave"]
    parts = []
    if ph and ph[2]:
        parts.append(f"Jordan's phone placed in the bedroom {hhmm(ph[0])}-{hhmm(ph[1])} ({ph[2]} BLE readings)")
    if mm and mm[2]:
        parts.append(f"bedroom mmWave occupied {hhmm(mm[0])}-{hhmm(mm[1])}")
    return "; ".join(parts) if parts else "no bedroom presence evidence recorded"


def compose(g: dict, start, end) -> str:
    why = notable(g)
    head = f"*Night Watch* ({hhmm(start)}-{hhmm(end)})"
    if not why:
        return f"{head}: quiet night — nothing above baseline. Sleep evidence: {sleep_line(g)}."
    lines = [head + " — " + ", ".join(why) + "."]
    cls = ", ".join(f"{n} {c}" for c, n in sorted(g["by_class"].items(), key=lambda x: -x[1])) or "none"
    zones = ", ".join(f"{z} {n}" for z, n in sorted(g["person_zones"].items(), key=lambda x: -x[1])[:4])
    lines.append(f"• Exterior cameras (episodes, Frigate labels): {cls}"
                 + (f"; person by zone: {zones}" if zones else "")
                 + f" — usual person ceiling for this window {g['person_p95']:.0f}.")
    sky = []
    if g["scanner"]:
        med = sum(1 for r in g["scanner"] if W.is_medical(r[2]))
        sky.append(f"{len(g['scanner'])} scanner transmission(s) geocoded within {NEAR_MI:g} mi "
                   f"(closest {min(r[1] for r in g['scanner']):.1f} mi" + (f", {med} medical" if med else "") + ")")
    if g["loiters"]:
        tight = sum(1 for l in g["loiters"] if l.get("tight"))
        sky.append(f"{len(g['loiters'])} helicopter(s) low and close" + (f", {tight} in a tight orbit" if tight else ""))
    if sky:
        lines.append("• " + "; ".join(sky) + ".")
    net = []
    if g["newdev"]:
        net.append(f"{len(g['newdev'])} never-seen device(s) joined the network")
    if g["buick"]:
        net.append(f"{len(g['buick'])} new Buick 8 Logbook entr{'y' if len(g['buick']) == 1 else 'ies'} "
                   f"(cause unknown): " + "; ".join(sorted({k for k, _d in g['buick']})))
    if g["bodach_max"]:
        net.append(f"Bodach peak score {g['bodach_max']:.2f}" + (" (alerted)" if g["bodach_fired"] else ""))
    if net:
        lines.append("• " + "; ".join(net) + ".")
    lines.append(f"• Sleep evidence: {sleep_line(g)}.")
    return W.journal_safe("\n".join(lines[:5]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date", help="report the night ending 07:00 on this local date")
    a = ap.parse_args(argv)
    day = datetime.strptime(a.date, "%Y-%m-%d").date() if a.date else datetime.now(W.TZ).date()
    start, end = night_bounds(day)
    conn = W.connect()
    cur = conn.cursor()
    msg = compose(gather(cur, start, end), start, end)
    print(msg)
    if a.dry_run:
        return 0
    import nova_config
    # Slack only (never Discord — it describes the house). Retried; a final failure exits 1.
    if not W.retry(W.post_slack, msg, nova_config.SLACK_CHAN, tag=TAG):
        W.log(TAG, "post to #nova-chat FAILED after retries")
        return 1
    W.log(TAG, "posted to #nova-chat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
