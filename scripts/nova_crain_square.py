#!/usr/bin/env python3
"""nova_crain_square.py — CRAIN'S SQUARE: do the house's angles still add up?

From Shirley Jackson's "The Haunting of Hill House": Dr. Montague explains that Hugh Crain
made his house to suit his mind. Every angle is a fraction of a degree off, one way or another,
and the small errors add up to a house where doors swing shut unless they are held. No single
measurement is wrong enough to notice; only the whole fails to close. Poe adds the nail in "The
Murders in the Rue Morgue": the nail that "secured" the window looked sound, and its head came
away in Dupin's fingers. A settled check is only settled until someone touches it.
(Jackson details via secondary sources: LitCharts and GradeSaver ch. 4.)

Nova's version checks the house model as a whole, not each sensor alone (CARDINAL does that).
It computes one residual per cross-sensor invariant per day and watches the residuals, not the
raw sensors, with a Shewhart control chart. Invariants are about rooms and devices, never people.

Minimal first version, adapted to what the data supports (checked 2026-10-08):
  * mmwave_identity   no two rooms' mmWave series should be minute-for-minute identical
                      (residual = fraction of shared minutes that agree; verdict from CARDINAL's
                      own mmwave_health, so the two organs cannot disagree about "duplicate")
  * motion_vs_camera  a non-camera motion sensor and the cameras that watch the same ground
                      should agree (residual = hit rate minus the camera's base rate). The spec
                      asked for a door contact; Home Assistant has no door contacts, so the Hue
                      outdoor motion sensor stands in. The daily hit rate goes to CARDINAL as an
                      organ-fed outcome: the first partial ground truth for those cameras.
  * temp_pair         two independent temperature sensors in the same place should hold a
                      stable offset (residual = median hourly difference). The spec asked for
                      indoor temperature vs HVAC state; there is no HVAC entity (the Nest
                      thermostat is outside Nova's control), so same-place sensor pairs stand in.
                      Identical series are flagged too: excess agreement is also a finding.
  Not built: plugs vs whole-house power (sensor.nova_energy_house_power is a constant 5 W, not
  a meter), rain vs soil moisture, the Hill House same-sign bias, the quarterly nail test.

A residual outside 3 sigma of its own 28-day history on two consecutive days goes to the Buick 8
Logbook as 'house_invariant' (cause unknown).

CLI:      --run [--day YYYY-MM-DD] [--dry-run]   --show   --selftest
Table:    crain_square_residuals; feeds source_outcomes (CARDINAL) via record_outcome
Config:   service_config crain_square/motion_pairs  {"<binary_sensor>": ["<camera>", ...]}
          service_config crain_square/temp_pairs    [["room","source","room","source"], ...]
Schedule: daily 05:30 (before CARDINAL at 05:50): `nova_crain_square.py --run`
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import bisect
import json
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

TAG = "crain-square"
MOTION_PAIRS = {"binary_sensor.hue_outdoor_motion_sensor_1_motion": ["front_yard_alt", "front_door"]}
TEMP_PAIRS = [["patio", "fp300", "patio", "homekit"],
              ["outdoor", "weather_station", "outdoor_front", "ha_hue_sensor"]]
WINDOW_MIN = 2          # camera must fire within +/- 2 min of the motion sensor
HISTORY_DAYS = 28
MIN_HISTORY = 7
SIGMA = 3.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS crain_square_residuals (
  day date NOT NULL,
  invariant text NOT NULL,
  subject text NOT NULL,
  residual real,
  n int NOT NULL DEFAULT 0,
  flag text,
  out_of_control boolean NOT NULL DEFAULT false,
  detail jsonb NOT NULL DEFAULT '{}',
  computed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (day, invariant, subject));
"""


def log(m: str) -> None:
    W.log(TAG, m)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a failed read degrades to "nothing known"
        log(f"query failed: {str(e).splitlines()[0]}")
        return []


# ── invariants (pure) ───────────────────────────────────────────────────────

def mmwave_identity(per_room: dict, min_common: int = 200) -> list:
    """per_room = {room: {minute: occupied}}. One residual per room pair with enough overlap."""
    from nova_cardinal import mmwave_health
    bad = mmwave_health(per_room)
    rooms, out = sorted(per_room), []
    for i, a in enumerate(rooms):
        for b in rooms[i + 1:]:
            common = set(per_room[a]) & set(per_room[b])
            if len(common) < min_common:
                continue
            same = sum(per_room[a][m] == per_room[b][m] for m in common)
            flag = bad[b] if bad.get(b, "").startswith(f"duplicate of {a}:") else None
            out.append({"invariant": "mmwave_identity", "subject": f"{a}|{b}", "residual": same / len(common),
                        "n": len(common), "flag": flag, "detail": {"same": same}})
    return out


def motion_vs_camera(sensor: str, events: list, cam_ts: dict, start: datetime, end: datetime) -> list:
    """events = motion-on timestamps; cam_ts = {camera: sorted detection ts}. Hit = camera fired
    within WINDOW_MIN of an episode start; base = share of the day's minutes with such a firing."""
    starts = [e[1] for e in W.episodes([(t, sensor) for t in events], gap_s=120)]
    w = timedelta(minutes=WINDOW_MIN)
    minutes = int((end - start).total_seconds() // 60)
    out = []
    for cam, ts in sorted(cam_ts.items()):
        hits = sum(W.count_between(ts, s - w, s + w) > 0 for s in starts)
        base = sum(W.count_between(ts, start + timedelta(minutes=i) - w, start + timedelta(minutes=i) + w) > 0
                   for i in range(minutes)) / max(minutes, 1)
        rate = hits / len(starts) if starts else None
        out.append({"invariant": "motion_vs_camera", "subject": f"{sensor}|{cam}",
                    "residual": None if rate is None else rate - base, "n": len(starts), "flag": None,
                    "detail": {"camera": cam, "hits": hits, "hit_rate": rate, "base_rate": round(base, 4)}})
    return out


def temp_pair(a: str, b: str, sa: dict, sb: dict, min_hours: int = 6) -> dict:
    """sa/sb = {hour: mean temp_f}. Residual = median(a - b) over shared hours."""
    hours = sorted(set(sa) & set(sb))
    d = [sa[h] - sb[h] for h in hours]
    if len(d) < min_hours:
        return {"invariant": "temp_pair", "subject": f"{a}|{b}", "residual": None, "n": len(d), "flag": None,
                "detail": {}}
    flag = "identical: one sensor counted twice" if max(abs(x) for x in d) < 0.05 else None
    q = statistics.quantiles(d, n=4) if len(d) > 1 else [d[0]] * 3
    return {"invariant": "temp_pair", "subject": f"{a}|{b}", "residual": round(statistics.median(d), 2),
            "n": len(d), "flag": flag, "detail": {"iqr": round(q[2] - q[0], 2)}}


def control(r: dict, history: list) -> dict:
    """Shewhart chart on the residual's own history -> out_of_control / persistent."""
    hist = [h for h in history if h is not None]
    r["out_of_control"] = False
    if r["residual"] is not None and len(hist) >= MIN_HISTORY:
        mu, sd = statistics.mean(hist), max(statistics.pstdev(hist), 0.01)
        r["out_of_control"] = abs(r["residual"] - mu) > SIGMA * sd
        r["detail"] = dict(r["detail"], mean=round(mu, 4), sd=round(sd, 4))
    return r


# ── PG ──────────────────────────────────────────────────────────────────────

def load_all(cur, start, end, motion_pairs: dict, temp_pairs: list) -> list:
    per: dict = {}
    for t, room, occ in _q(cur, "SELECT date_trunc('minute', ts), room, bool_or(metadata->>'occupied'='true') "
                                "FROM telemetry.presence WHERE method='mmwave' AND ts >= %s AND ts < %s "
                                "GROUP BY 1,2", (start, end)):
        per.setdefault(room, {})[t] = bool(occ)
    rows = mmwave_identity(per)
    for sensor, cams in motion_pairs.items():
        ev = [t for (t,) in _q(cur, "SELECT ts FROM telemetry.ha_sensors WHERE entity_id=%s AND state_text='on' "
                                    "AND ts >= %s AND ts < %s ORDER BY ts", (sensor, start, end))]
        cam_ts = {c: [] for c in cams}
        for t, c in _q(cur, "SELECT ts, metadata->>'camera' FROM telemetry.presence WHERE metadata->>'source'='frigate' "
                            "AND metadata->>'camera' = ANY(%s) AND ts >= %s AND ts < %s ORDER BY ts",
                       (list(cams), start, end)):
            cam_ts[c].append(t)
        rows += motion_vs_camera(sensor, ev, cam_ts, start, end)
    for ra, sa, rb, sb in temp_pairs:
        series = {}
        for room, src, h, v in _q(cur, "SELECT room, source, date_trunc('hour', ts), avg(temp_f) FROM telemetry.climate "
                                       "WHERE ((room=%s AND source=%s) OR (room=%s AND source=%s)) AND temp_f IS NOT NULL "
                                       "AND ts >= %s AND ts < %s GROUP BY 1,2,3", (ra, sa, rb, sb, start, end)):
            series.setdefault((room, src), {})[h] = float(v)
        rows.append(temp_pair(f"{ra}/{sa}", f"{rb}/{sb}", series.get((ra, sa), {}), series.get((rb, sb), {})))
    return rows


def history(cur, day: date) -> dict:
    """{(invariant, subject): ([residuals oldest first], yesterday_out_of_control)}"""
    exists = _q(cur, "SELECT to_regclass('crain_square_residuals')")
    if not exists or exists[0][0] is None:
        return {}
    out: dict = {}
    for inv, sub, d, res, ooc in _q(cur, "SELECT invariant, subject, day, residual, out_of_control FROM "
                                         "crain_square_residuals WHERE day >= %s AND day < %s ORDER BY day",
                                    (day - timedelta(days=HISTORY_DAYS), day)):
        h = out.setdefault((inv, sub), [[], False])
        h[0].append(res)
        h[1] = bool(ooc) and d == day - timedelta(days=1)
    return out


def write(cur, day: date, rows: list) -> tuple:
    """Insert residual rows (idempotent per day); feed CARDINAL only for newly inserted rows."""
    from nova_cardinal import record_outcome
    ensure_schema(cur)
    new = fed = 0
    for r in rows:
        cur.execute("INSERT INTO crain_square_residuals (day, invariant, subject, residual, n, flag, out_of_control, "
                    "detail) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING day",
                    (day, r["invariant"], r["subject"], r["residual"], r["n"], r["flag"], r["out_of_control"],
                     json.dumps(r["detail"], default=str)))
        if not cur.fetchone():
            continue
        new += 1
        if r["invariant"] == "motion_vs_camera" and r["detail"].get("hit_rate") is not None:
            record_outcome(cur, f"camera:{r['detail']['camera']}", "camera", r["detail"]["hit_rate"],
                           ref=f"crain_square:{day}:{r['subject']}", recorded_by="crain_square")
            fed += 1
    return new, fed


def report(cur, day: date, rows: list) -> int:
    from nova_buick8_log import log_unexplained
    n = 0
    for r in rows:
        if not r.get("persistent"):
            continue
        log_unexplained("house_invariant", f"{r['invariant']}:{r['subject']}",
                        f"house invariant {r['invariant']} for {r['subject']} out of control two days running "
                        f"(residual {r['residual']})", evidence={"day": str(day), **r["detail"]},
                        occurrence_key=str(day), source="crain_square", cur=cur)
        n += 1
    return n


def config(cur):
    try:
        return (W.get_config(cur, "crain_square", "motion_pairs", MOTION_PAIRS),
                W.get_config(cur, "crain_square", "temp_pairs", TEMP_PAIRS))
    except Exception as e:  # noqa: BLE001
        log(f"config read failed, using defaults: {e}")
        return MOTION_PAIRS, TEMP_PAIRS


def run(day: date | None = None, dry: bool = False) -> list:
    day = day or (datetime.now(W.TZ).date() - timedelta(days=1))
    start = datetime(day.year, day.month, day.day, tzinfo=W.TZ)
    end = start + timedelta(days=1)
    conn = W.connect()
    try:
        cur = conn.cursor()
        motion, temps = config(cur)
        hist = history(cur, day)
        rows = load_all(cur, start, end, motion, temps)
        for r in rows:
            past, prev_ooc = hist.get((r["invariant"], r["subject"]), ([], False))
            control(r, past)
            r["persistent"] = r["out_of_control"] and prev_ooc
        log(f"{'DRY RUN ' if dry else ''}{day}: {len(rows)} residuals, "
            f"{sum(r['out_of_control'] for r in rows)} out of control")
        for r in rows:
            res = "-" if r["residual"] is None else f"{r['residual']:.3f}"
            mark = "PERSISTENT" if r["persistent"] else "OUT" if r["out_of_control"] else "ok"
            print(f"  {r['invariant']:<17} {res:>8} n={r['n']:<5} {mark:<10} {r['subject']}"
                  + (f"  [{r['flag']}]" if r["flag"] else ""))
        if not dry:
            new, fed = write(cur, day, rows)
            log(f"wrote {new} rows; {fed} camera outcomes to CARDINAL; {report(cur, day, rows)} to Buick 8")
        return rows
    finally:
        conn.close()


def show() -> int:
    conn = W.connect()
    try:
        cur = conn.cursor()
        for d, inv, sub, res, n, flag, ooc in _q(cur, "SELECT day, invariant, subject, residual, n, flag, out_of_control "
                                                      "FROM crain_square_residuals WHERE day = (SELECT max(day) FROM "
                                                      "crain_square_residuals) ORDER BY invariant, subject"):
            print(f"{d} {inv:<17} {'-' if res is None else f'{res:.3f}':>8} n={n:<5} {'OUT' if ooc else 'ok':<4} {sub}"
                  + (f"  [{flag}]" if flag else ""))
        return 0
    finally:
        conn.close()


def selftest() -> int:
    t0 = datetime(2026, 10, 1, tzinfo=W.TZ)
    mins = [t0 + timedelta(minutes=i) for i in range(300)]
    per = {"a": {m: i % 7 == 0 for i, m in enumerate(mins)}, "b": {m: i % 7 == 0 for i, m in enumerate(mins)},
           "c": {m: i % 3 == 0 for i, m in enumerate(mins)}}
    res = {r["subject"]: r for r in mmwave_identity(per)}
    assert res["a|b"]["residual"] == 1.0 and res["a|b"]["flag"], res["a|b"]
    assert res["a|c"]["residual"] < 0.9 and res["a|c"]["flag"] is None
    end = t0 + timedelta(hours=5)
    cams = {"cam": [t0 + timedelta(minutes=10, seconds=30)], "blind": []}
    mv = {r["detail"]["camera"]: r for r in motion_vs_camera("s", [t0 + timedelta(minutes=10)], cams, t0, end)}
    assert mv["cam"]["detail"]["hit_rate"] == 1.0 and mv["cam"]["residual"] > 0.9
    assert mv["blind"]["detail"]["hit_rate"] == 0.0
    sa = {h: 70.0 + h for h in range(12)}
    tp = temp_pair("x", "y", sa, {h: v - 3 for h, v in sa.items()})
    assert tp["residual"] == 3.0 and tp["flag"] is None
    assert temp_pair("x", "y", sa, sa)["flag"]
    assert temp_pair("x", "y", sa, {})["residual"] is None
    assert control({"residual": 3.0, "detail": {}}, [0.0, 0.1, -0.1, 0.05, 0.0, -0.05, 0.1])["out_of_control"]
    assert not control({"residual": 3.0, "detail": {}}, [0.0])["out_of_control"]
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="compute yesterday's residuals, write, feed CARDINAL")
    ap.add_argument("--day", type=date.fromisoformat, help="with --run: local day YYYY-MM-DD (default yesterday)")
    ap.add_argument("--dry-run", action="store_true", help="with --run: print residuals, write nothing")
    ap.add_argument("--show", action="store_true", help="latest stored day")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.run:
        run(a.day, dry=a.dry_run)
        return 0
    if a.show:
        return show()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
