#!/usr/bin/env python3
"""nova_bodach_watch.py — Bodach Watch: independent signals gathering near home.

In Odd Thomas the bodachs gather before violence; no single one means anything, a crowd of
them does. Nova's version: for a rolling 60-minute window around home, score each INDEPENDENT
signal type on its own 0..1 scale and only speak when two or three different types cluster.

Signal types (each capped at 1.0, so no single feed can carry the score on its own):
  scanner  — scanner transcripts geocoded within 1.5 mi of home (memories.metadata.geo)
  chp      — CHP incidents within 1.5 mi (freeway traffic hazards are weak; injury/fire strong)
  air      — a helicopter loitering low and close (>=5 ADS-B samples <2500 ft, <1.5 nm;
             'tight' = >=8 samples <1500 ft, <1.0 nm; full strength only when a tight orbit is
             sustained for >=30 samples in the window)
  network  — the security organ reported a never-seen device on the LAN
  motion   — exterior-camera PERSON episodes at night (22:00-07:00) above this clock window's
             own 14-night 95th percentile (cars in the alley are constant and never count)

Score = sum of the strengths of the types that are present (strength >= 0.5). An alert needs
score >= the calibrated threshold AND >= 2 present types. The threshold is re-tuned with
--calibrate against the last 30 days so the watch would have fired rarely (target <= 2
episodes / 30 days) and stored in service_config('bodach_watch','threshold').

Quiet by default: every window with any signal is recorded to bodach_scores; Jordan is told
(warning, via nova_notify) only above threshold. Text that could reach the journal is built
with journal_safe() — no addresses, no bearings, no home location.

Usage:
  nova_bodach_watch.py                 # score the last 60 min, record, alert if above threshold
  nova_bodach_watch.py --dry-run       # score + print, write nothing, alert nobody
  nova_bodach_watch.py --calibrate     # replay 30 days, pick + store threshold, report fire count
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

TAG = "bodach"
WINDOW = timedelta(minutes=60)
STEP = timedelta(minutes=15)
NEAR_MI = 1.5
PRESENT = 0.5
MIN_TYPES = 2
DEFAULT_THRESHOLD = 2.0
THRESHOLD_GRID = (1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.5, 4.0)
TARGET_MAX_FIRES = 2          # per 30 days
BASELINE_NIGHTS = 14
SERIOUS_CHP = ("fire", "injur", "pursuit", "shots", "fatal", "pedestrian", "overturn", "hazmat",
               "hit and run w", "ambulance", "1144", "1183", "20001")
SERVICE = "bodach_watch"
AIR_SUSTAINED = 30           # ADS-B samples of one airframe inside the 60-min window


# ── pure scoring ────────────────────────────────────────────────────────────

def strength_scanner(hits_mi: list) -> float:
    if not hits_mi:
        return 0.0
    s = 0.5 + 0.25 * (len(hits_mi) - 1) + (0.25 if min(hits_mi) <= 0.5 else 0.0)
    return round(min(1.0, s), 3)


def strength_chp(incidents: list) -> float:
    """incidents: [(type, mi)]. Routine freeway traffic tops out at 0.5; serious ones count 0.8."""
    if not incidents:
        return 0.0
    routine = serious = 0.0
    for typ, _mi in incidents:
        if any(k in (typ or "").lower() for k in SERIOUS_CHP):
            serious += 0.8
        else:
            routine += 0.2
    return round(min(1.0, serious + min(0.5, routine)), 3)


def strength_air(loiters: list) -> float:
    """Helicopters are routine over Burbank (low/close tight passes in ~8% of hours), so only a
    SUSTAINED tight orbit (>= AIR_SUSTAINED samples in the window) earns full strength."""
    if not loiters:
        return 0.0
    if any(l.get("tight") and l.get("hits", 0) >= AIR_SUSTAINED for l in loiters):
        return 1.0
    return 0.75 if any(l.get("tight") for l in loiters) else 0.5


def strength_network(n_new: int) -> float:
    return round(min(1.0, 0.5 * n_new), 3) if n_new > 0 else 0.0


def strength_motion(n: int, baseline_p95: float, night: bool) -> float:
    """Exterior person episodes vs this clock window's own history. Day windows never count."""
    if not night or n < 2 or n <= baseline_p95:
        return 0.0
    return round(min(1.0, 0.5 + 0.25 * (n - baseline_p95) / max(1.0, baseline_p95)), 3)


def combine(strengths: dict) -> tuple[float, list]:
    present = sorted(k for k, v in strengths.items() if v >= PRESENT)
    return round(sum(strengths[k] for k in present), 3), present


def fires(score: float, present: list, threshold: float, min_types: int = MIN_TYPES) -> bool:
    return len(present) >= min_types and score >= threshold


def merge_episodes(firing_ends: list, gap: timedelta = WINDOW) -> list:
    """Consecutive/overlapping firing windows are ONE episode. -> [(first_end, last_end)]"""
    eps = []
    for t in sorted(firing_ends):
        if eps and t - eps[-1][1] <= gap:
            eps[-1][1] = t
        else:
            eps.append([t, t])
    return [tuple(e) for e in eps]


def calibrate(windows: list, grid=THRESHOLD_GRID, target: int = TARGET_MAX_FIRES) -> dict:
    """windows: [(window_end, score, present_types)]. Pick the LOWEST threshold that would have
    fired <= target episodes. Returns {threshold, episodes, by_threshold}."""
    table = {}
    for th in grid:
        ends = [we for we, sc, pr in windows if fires(sc, pr, th)]
        table[th] = len(merge_episodes(ends))
    ok = [th for th in grid if table[th] <= target]
    chosen = min(ok) if ok else max(grid)
    return {"threshold": chosen, "episodes": table[chosen], "by_threshold": table}


# ── evidence over a span (bulk-loaded once, windowed in Python) ─────────────

class Feeds:
    """All five feeds for [start, end) plus BASELINE_NIGHTS of exterior-person history."""

    def __init__(self, cur, start, end):
        lat, lon = W.home()
        self._keys: dict = {}
        self.start, self.end = start, end
        self.scanner = W.load_scanner_near(start, end, NEAR_MI)
        self.chp = W.load_chp_near(cur, start, end, lat, lon, NEAR_MI)
        self.heli = W.load_heli(cur, start, end)
        self.net = W.load_new_devices(cur, start, end)
        det = W.load_ext_detections(cur, start - timedelta(days=BASELINE_NIGHTS), end, {"person"})
        eps = W.episodes(((ts, (cam, lbl)) for ts, cam, _room, lbl in det), gap_s=120)
        self.person_ts = sorted(e[1] for e in eps)
        self.person_eps = eps

    def _slice(self, rows, ws, we):
        import bisect
        keys = self._keys.setdefault(id(rows), [r[0] for r in rows])  # rows are ts-sorted
        return rows[bisect.bisect_left(keys, ws):bisect.bisect_left(keys, we)]

    def window(self, ws, we) -> dict:
        sc = self._slice(self.scanner, ws, we)
        ch = self._slice(self.chp, ws, we)
        lo = W.heli_loiters(self._slice(self.heli, ws, we))
        nt = self._slice(self.net, ws, we)
        night = W.in_night(we, 22, 7) or W.in_night(ws, 22, 7)
        n_person = W.count_between(self.person_ts, ws, we)
        hist = [W.count_between(self.person_ts, ws - timedelta(days=d), we - timedelta(days=d))
                for d in range(1, BASELINE_NIGHTS + 1)]
        p95 = W.percentile(hist, 0.95)
        strengths = {
            "scanner": strength_scanner([r[1] for r in sc]),
            "chp": strength_chp([(r[1], r[3]) for r in ch]),
            "air": strength_air(lo),
            "network": strength_network(len(nt)),
            "motion": strength_motion(n_person, p95, night),
        }
        score, present = combine(strengths)
        evidence = {
            "scanner": [{"ts": r[0].isoformat(), "mi": r[1], "text": r[2][:200]} for r in sc],
            "chp": [{"ts": r[0].isoformat(), "type": r[1], "location": r[2], "mi": r[3]} for r in ch],
            "air": [{"hex": l["hex"], "callsign": l["callsign"], "hits": l["hits"],
                     "min_alt_ft": l["min_alt"], "min_nm": round(l["min_nm"], 2), "tight": l["tight"]}
                    for l in lo],
            "network": [{"ts": r[0].isoformat(), "mac": r[1], "level": r[2]} for r in nt],
            "motion": {"person_episodes": n_person, "baseline_p95": round(p95, 2), "night": night},
        }
        return {"ws": ws, "we": we, "score": score, "present": present,
                "strengths": strengths, "evidence": evidence}


def describe(w: dict, safe: bool) -> str:
    """Evidence-only sentence per present signal. safe=True -> journal-safe (no places)."""
    ev, parts = w["evidence"], []
    for t in w["present"]:
        if t == "scanner":
            n = len(ev["scanner"])
            parts.append(f"{n} scanner transmission(s) geocoded near home" if safe else
                         f"{n} scanner transmission(s) within {NEAR_MI} mi: " +
                         "; ".join(f"\"{s['text'][:90]}\" ({s['mi']} mi)" for s in ev["scanner"][:3]))
        elif t == "chp":
            parts.append(f"{len(ev['chp'])} CHP incident(s) nearby" if safe else
                         "CHP: " + "; ".join(f"{c['type']} at {c['location']} ({c['mi']} mi)"
                                             for c in ev["chp"][:3]))
        elif t == "air":
            parts.append("a helicopter loitering low overhead" if safe else
                         "helicopter loiter: " + "; ".join(
                             f"{a['callsign'] or a['hex']} {a['hits']} samples, as low as "
                             f"{a['min_alt_ft']} ft, {a['min_nm']} nm" for a in ev["air"][:2]))
        elif t == "network":
            parts.append(f"{len(ev['network'])} never-seen device(s) joined the network" if safe else
                         "network: never-seen MAC(s) " + ", ".join(n["mac"] or "?" for n in ev["network"][:4]))
        elif t == "motion":
            m = ev["motion"]
            parts.append(f"{m['person_episodes']} exterior-camera person episodes vs "
                         f"{m['baseline_p95']:.0f} (95th pct for this hour)")
    text = "; ".join(parts)
    return W.journal_safe(text) if safe else text


# ── modes ───────────────────────────────────────────────────────────────────

def run_live(dry_run: bool) -> int:
    conn = W.connect()
    cur = conn.cursor()
    if not dry_run:
        W.ensure_schema(cur)
    th = float(W.get_config(cur, SERVICE, "threshold", DEFAULT_THRESHOLD) or DEFAULT_THRESHOLD)
    we = W.now_utc()
    ws = we - WINDOW
    w = Feeds(cur, ws, we).window(ws, we)
    fired = fires(w["score"], w["present"], th)
    W.log(TAG, f"score {w['score']} types {w['present']} threshold {th} fired={fired}")
    if dry_run:
        print(json.dumps({k: v for k, v in w.items() if k not in ("ws", "we")}, default=str, indent=1))
        return 0
    if not any(w["strengths"].values()):
        return 0  # nothing at all stirring: nothing to record
    safe = describe(w, safe=True)
    alerted = False
    if fired:
        cur.execute("SELECT 1 FROM bodach_scores WHERE alerted AND window_end > %s", (we - 2 * WINDOW,))
        if not cur.fetchone():
            from nova_notify import notify
            body = (describe(w, safe=False) + f"\n\nBodach score {w['score']} from "
                    f"{len(w['present'])} independent signal types ({', '.join(w['present'])}); "
                    f"threshold {th}. Evidence only — no cause is implied.")
            alerted = W.retry(notify, "Bodach Watch: independent signals clustering near home", body=body,
                             level="warning", category="local", source="nova_bodach_watch",
                             dedup_key=f"bodach-{we:%Y%m%d%H}",
                             meta={"score": w["score"], "types": w["present"]}, tag=TAG)
    cur.execute(
        "INSERT INTO bodach_scores (window_start, window_end, score, n_types, strengths, evidence, "
        "safe_summary, threshold, fired, alerted) VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s) "
        "ON CONFLICT (window_end) DO NOTHING",
        (ws, we, w["score"], len(w["present"]), json.dumps(w["strengths"]),
         json.dumps(w["evidence"], default=str), safe, th, fired, bool(alerted)))
    return 0


def replay(cur, days: int = 30) -> list:
    end = W.now_utc().replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % 15)
    start = end - timedelta(days=days)
    feeds = Feeds(cur, start, end)
    out, we = [], start + WINDOW
    while we <= end:
        out.append(feeds.window(we - WINDOW, we))
        we += STEP
    return out


def run_calibrate(days: int, write: bool) -> int:
    conn = W.connect()
    cur = conn.cursor()
    wins = replay(cur, days)
    res = calibrate([(w["we"], w["score"], w["present"]) for w in wins])
    present_hist: dict = {}
    for w in wins:
        for t in w["present"]:
            present_hist[t] = present_hist.get(t, 0) + 1
    multi = sum(1 for w in wins if len(w["present"]) >= 2)
    top = sorted(wins, key=lambda w: w["score"], reverse=True)[:5]
    W.log(TAG, f"{len(wins)} windows over {days}d; windows with each type present: {present_hist}")
    W.log(TAG, f"windows with >=2 types: {multi}; episodes by threshold: {res['by_threshold']}")
    W.log(TAG, f"chosen threshold {res['threshold']} -> would have fired {res['episodes']} time(s)")
    for w in top:
        W.log(TAG, f"  top {w['we'].astimezone(W.TZ):%m-%d %H:%M} score {w['score']} {w['present']} :: "
                   f"{describe(w, safe=True)}")
    if write:
        W.ensure_schema(cur)
        W.set_config(cur, SERVICE, "threshold", res["threshold"], "nova_bodach_watch --calibrate")
        W.set_config(cur, SERVICE, "calibration", {
            "at": W.now_utc().isoformat(), "days": days, "windows": len(wins),
            "type_windows": present_hist, "multi_type_windows": multi,
            "episodes_by_threshold": {str(k): v for k, v in res["by_threshold"].items()},
            "threshold": res["threshold"], "would_have_fired": res["episodes"],
            "fired_at": [f"{a.astimezone(W.TZ):%Y-%m-%d %H:%M}" for a, _b in merge_episodes(
                [w["we"] for w in wins if fires(w["score"], w["present"], res["threshold"])])],
        }, "nova_bodach_watch --calibrate")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--no-write", action="store_true", help="with --calibrate: report only")
    a = ap.parse_args(argv)
    if a.calibrate:
        return run_calibrate(a.days, not a.no_write)
    return run_live(a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
