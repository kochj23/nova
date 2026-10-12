#!/usr/bin/env python3
"""nova_sdr_placement.py — load-balance SDR tuners like the LLM fleet (Jordan 2026-10-12).

Targets (frequencies, each with a priority and a demod) are matched to tuners (a radio port with
a fixed capture pipeline and a measured antenna) by antenna-bench SNR. Higher priority wins:
when a tuner goes unhealthy, its target takes the best free compatible tuner, and if none is
free it displaces the lowest-priority target on a compatible tuner. Damped (MOVE_DB / HOLD_MIN)
because flapping clients are what wedged SDRconnect and the SDRplay API on 2026-10-12.

Tables (nova_ops): sdr_targets, sdr_tuners, sdr_assignments, sdr_health, sdr_placement_log.
Mode: service_config nova/sdr_placement_mode = 'advise' (log + notify only) | 'live' (write
sdr_assignments; nova_sdr_actuator on each radio host applies them to systemd units).

  nova_sdr_placement.py --init     create tables + seed the 2026-10-12 layout (idempotent)
  nova_sdr_placement.py            one placement pass in the configured mode
  nova_sdr_placement.py --selftest
"""
import json, math, sys
from datetime import datetime, timedelta, timezone

MOVE_DB = 6.0          # a healthy target only moves for at least this much better SNR...
HOLD_MIN = 30          # ...and only after holding its tuner this long
DOWN_AFTER_MIN = 5     # a tuner reporting unhealthy this long is excluded
STALE_MIN = 10         # no heartbeat this long: keep what's there, place nothing new on it
DEFAULT_SNR = 15.0     # tuner/band never benchmarked
# band label in antenna_baseline_snapshots -> centre frequency (Hz)
BAND_HZ = {"AM broadcast (KFI 640 kHz)": 640e3, "HF (WWV 10 MHz)": 10e6, "HF (WWV 15 MHz)": 15e6,
           "CB ch19 27.185 MHz": 27.185e6, "VHF/FM broadcast (KROQ 106.7)": 106.7e6,
           "FM broadcast (KROQ 106.7)": 106.7e6, "2m ham (146.520 calling freq)": 146.52e6,
           "VHF NOAA WX 162.55": 162.55e6, "UHF/police-fire (P25 506.9375)": 506.9375e6,
           "UHF P25 LAPD 506.9375": 506.9375e6}


def score(bench: dict, freq_hz: float) -> float:
    """SNR at the nearest benchmarked band, -6 dB per octave of distance. bench = {band_hz: snr}."""
    if not bench:
        return DEFAULT_SNR
    hz = min(bench, key=lambda b: abs(math.log(freq_hz / b)))
    return bench[hz] - 6.0 * abs(math.log2(freq_hz / hz))


def place(targets, tuners, current, health, now):
    """Pure placement. targets: [{id, priority, freq_hz, demod}]; tuners: {id: {demods, min_hz,
    max_hz, requires, bench}}; current: {target_id: (tuner_id, since)}; health: {tuner_id:
    'up'|'down'|'stale'}. Returns ({target_id: tuner_id}, [unplaced target ids])."""
    excluded = set()
    for _ in range(3):                        # re-run if a slave lost its master
        taken, out, unplaced = set(), {}, []
        for t in sorted(targets, key=lambda t: -t["priority"]):
            def ok(tid, new):
                u = tuners[tid]
                return (tid not in taken and tid not in excluded and health.get(tid) != "down"
                        and not (new and health.get(tid) == "stale")
                        and t["demod"] in u["demods"] and u["min_hz"] <= t["freq_hz"] <= u["max_hz"])
            cur, since = current.get(t["id"], (None, None))
            cands = [tid for tid in tuners if ok(tid, tid != cur)]
            if not cands:
                unplaced.append(t["id"]); continue
            best = max(cands, key=lambda tid: score(tuners[tid]["bench"], t["freq_hz"]))
            pick = best
            if cur in cands and cur != best:
                gain = score(tuners[best]["bench"], t["freq_hz"]) - score(tuners[cur]["bench"], t["freq_hz"])
                if gain < MOVE_DB or (since and now - since < timedelta(minutes=HOLD_MIN)):
                    pick = cur
            out[t["id"]] = pick; taken.add(pick)
        orphans = {tid for tid in out.values() if tuners[tid].get("requires") and
                   (tuners[tid]["requires"] not in out.values() or health.get(tuners[tid]["requires"]) == "down")}
        if not orphans:
            return out, unplaced
        excluded |= orphans
    return out, unplaced


# ── database ─────────────────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS sdr_targets (id text PRIMARY KEY, label text NOT NULL, freq_hz bigint NOT NULL,
  demod text NOT NULL CHECK (demod IN ('NFM','AM','P25')), priority int NOT NULL, enabled bool NOT NULL DEFAULT true,
  meta jsonb NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS sdr_tuners (id text PRIMARY KEY, host text NOT NULL, pipeline text NOT NULL, unit text NOT NULL,
  port text, demods text[] NOT NULL, min_hz bigint NOT NULL, max_hz bigint NOT NULL, requires text,
  bench_device text, bench_antenna text, enabled bool NOT NULL DEFAULT true, meta jsonb NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS sdr_assignments (tuner_id text PRIMARY KEY REFERENCES sdr_tuners(id),
  target_id text UNIQUE REFERENCES sdr_targets(id), since timestamptz NOT NULL DEFAULT now(), reason text);
CREATE TABLE IF NOT EXISTS sdr_health (tuner_id text PRIMARY KEY REFERENCES sdr_tuners(id), ts timestamptz NOT NULL,
  ok bool NOT NULL, detail text, unhealthy_since timestamptz);
CREATE TABLE IF NOT EXISTS sdr_placement_log (ts timestamptz NOT NULL DEFAULT now(), mode text, action text, detail jsonb);
"""
L = "Tuner 1 50 ohm"; R = "Tuner 2 50 ohm"
SEED_TARGETS = [  # id, label, freq_hz, demod, priority, meta
    ("lapd-ne", "LAPD Northeast (Glassell Park)", 506937500, "P25", 90, {"nac": "466"}),
    ("lapd-noho", "LAPD North Hollywood", 484962500, "P25", 85, {"nac": "466", "loc": "North Hollywood (LAPD Valley Bureau)"}),
    ("noaa-wx", "NOAA WX", 162550000, "NFM", 80, {}),
    ("bur-tower", "BUR Tower", 118700000, "AM", 60, {}),
    ("cb-19", "CB Channel 19 (truckers)", 27185000, "AM", 40,
     {"source": "cb", "kind": "cb", "tag": "AM voice", "loc": "Burbank / I-5 Golden State Fwy corridor"}),
    ("ham-147435", "World Famous 147.435", 147435000, "NFM", 30, {}),
]
SEED_TUNERS = [  # id, host, pipeline, unit, port, demods, min_hz, max_hz, requires, bench_device, bench_antenna, meta
    ("core2-rtl0", "nova-core2", "p25_rtl", "nova-lapd", None, ["P25"], 24_000_000, 1_766_000_000, None, "RTL2832U-R820T", "RX", {}),
    ("core2-duo1-t2", "nova-core2", "fm_master", "nova-fm-noaa", R, ["NFM", "AM"], 1_000, 2_000_000_000, None,
     "180603A532", R, {"serial": "180603A532", "outdir": "/home/kochj/nrsp_new_calls"}),
    ("core2-duo1-t1", "nova-core2", "p25_slave", "nova-rsp-rotate", L, ["P25"], 1_000, 2_000_000_000, "core2-duo1-t2",
     "180603A532", L, {}),
    ("core3-duo2-t1", "nova-core3", "fm_dt", "nova-fm-ham-aviation", "1", ["NFM", "AM"], 1_000, 2_000_000_000, None,
     "24051A7034", L, {"serial": "24051A7034", "outdir": "/home/kochj/nrsp_calls"}),
    ("core3-duo2-t2", "nova-core3", "fm_dt", "nova-fm-ham-aviation", "2", ["NFM", "AM"], 1_000, 2_000_000_000, None,
     "24051A7034", R, {"serial": "24051A7034", "outdir": "/home/kochj/nrsp_calls"}),
    ("garage-nrsp-b", "nova-core2", "nrsp", "nova-nrsp-rail", "Antenna B", ["NFM", "AM"], 1_000, 2_000_000_000, None,
     "nRSP-ST", "Antenna B", {}),
]
SEED_ASSIGN = {"core2-rtl0": "lapd-ne", "core2-duo1-t2": "noaa-wx", "core2-duo1-t1": "lapd-noho",
               "core3-duo2-t2": "bur-tower", "core3-duo2-t1": "ham-147435", "garage-nrsp-b": "cb-19"}


def _connect():
    import psycopg2
    try:
        import nova_dsn
        return psycopg2.connect(nova_dsn.pg_dsn("nova_ops"))
    except ImportError:
        return psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")


def init(cur):
    cur.execute(SCHEMA)
    for row in SEED_TARGETS:
        cur.execute("INSERT INTO sdr_targets(id,label,freq_hz,demod,priority,meta) VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (id) DO NOTHING", (*row[:5], json.dumps(row[5])))
    for row in SEED_TUNERS:
        cur.execute("INSERT INTO sdr_tuners(id,host,pipeline,unit,port,demods,min_hz,max_hz,requires,bench_device,"
                    "bench_antenna,meta) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                    (*row[:11], json.dumps(row[11])))
    for tuner, target in SEED_ASSIGN.items():
        cur.execute("INSERT INTO sdr_assignments(tuner_id,target_id,reason) VALUES (%s,%s,'seed 2026-10-12 bench layout') "
                    "ON CONFLICT (tuner_id) DO NOTHING", (tuner, target))
    cur.execute("INSERT INTO service_config(service,key,value) VALUES ('nova','sdr_placement_mode','\"advise\"') "
                "ON CONFLICT DO NOTHING")


def load(cur, now):
    cur.execute("SELECT id, priority, freq_hz, demod FROM sdr_targets WHERE enabled")
    targets = [dict(zip(("id", "priority", "freq_hz", "demod"), r)) for r in cur.fetchall()]
    # Latest bench SNR per (device, antenna, band). SDRconnect's snr property is meaningless on a
    # digital P25 carrier, so its UHF rows are ignored (2026-10-12 bench notes).
    cur.execute("""SELECT DISTINCT ON (device, metadata->>'antenna', metadata->>'band')
                          device, metadata->>'antenna', metadata->>'band', value
                   FROM antenna_baseline_snapshots WHERE metric='snr_db'
                     AND NOT (metadata->>'band' LIKE 'UHF%%' AND (metadata->>'method'='sdrconnect' OR label LIKE '%%nrsp-bench'))
                   ORDER BY device, metadata->>'antenna', metadata->>'band', ts DESC""")
    bench = {}
    for dev, ant, band, v in cur.fetchall():
        if band in BAND_HZ:
            bench.setdefault((dev, ant), {})[BAND_HZ[band]] = v
    cur.execute("SELECT id, demods, min_hz, max_hz, requires, bench_device, bench_antenna FROM sdr_tuners WHERE enabled")
    tuners = {r[0]: {"demods": set(r[1]), "min_hz": r[2], "max_hz": r[3], "requires": r[4],
                     "bench": bench.get((r[5], r[6]), {})} for r in cur.fetchall()}
    cur.execute("SELECT target_id, tuner_id, since FROM sdr_assignments WHERE target_id IS NOT NULL")
    current = {t: (u, s) for t, u, s in cur.fetchall()}
    cur.execute("SELECT tuner_id, ts, ok, unhealthy_since FROM sdr_health")
    health = {}
    for tid, ts, ok, since in cur.fetchall():
        if now - ts > timedelta(minutes=STALE_MIN):
            health[tid] = "stale"
        elif not ok and since and now - since >= timedelta(minutes=DOWN_AFTER_MIN):
            health[tid] = "down"
        else:
            health[tid] = "up"
    return targets, tuners, current, health


def run():
    con = _connect(); con.autocommit = True; cur = con.cursor()
    now = datetime.now(timezone.utc)
    cur.execute("SELECT value#>>'{}' FROM service_config WHERE service='nova' AND key='sdr_placement_mode'")
    row = cur.fetchone(); mode = row[0] if row else "advise"
    targets, tuners, current, health = load(cur, now)
    desired, unplaced = place(targets, tuners, current, health, now)
    moves = {t: (current.get(t, (None,))[0], u) for t, u in desired.items() if current.get(t, (None,))[0] != u}
    dropped = [t for t in current if t not in desired]
    detail = {"moves": moves, "unplaced": unplaced, "dropped": dropped,
              "down": sorted(k for k, v in health.items() if v == "down")}
    if not moves and not dropped:
        print(f"[sdr-placement] {mode}: layout optimal, no changes ({len(desired)} placed, unplaced={unplaced})")
        return 0
    cur.execute("INSERT INTO sdr_placement_log(mode,action,detail) VALUES (%s,%s,%s)",
                (mode, "apply" if mode == "live" else "advise", json.dumps(detail)))
    if mode == "live":
        cur.execute("BEGIN")
        cur.execute("UPDATE sdr_assignments SET target_id=NULL, since=now(), reason='released' WHERE target_id = ANY(%s)",
                    (list(moves) + dropped,))
        for t, (src, dst) in moves.items():
            why = f"from {src} ({'down' if health.get(src) == 'down' else 'rebalance'})" if src else "placed"
            cur.execute("INSERT INTO sdr_assignments(tuner_id,target_id,since,reason) VALUES (%s,%s,now(),%s) "
                        "ON CONFLICT (tuner_id) DO UPDATE SET target_id=EXCLUDED.target_id, since=now(), reason=EXCLUDED.reason",
                        (dst, t, why))
        cur.execute("COMMIT")
    lines = [f"• `{t}`: {s or '—'} → {d}" for t, (s, d) in moves.items()] + [f"• `{t}`: dropped (no tuner)" for t in dropped]
    msg = (("Moved" if mode == "live" else "Would move") + " SDR targets:\n" + "\n".join(lines)
           + (f"\nUnplaced: {', '.join(unplaced)}" if unplaced else ""))
    print(f"[sdr-placement] {mode}: {msg}")
    try:
        from nova_notify import notify
        notify(title="SDR placement", body=msg, level="warning" if mode == "live" else "info",
               category="sdr", source="nova_sdr_placement", dedup_key=f"sdr-placement-{json.dumps(detail, sort_keys=True)}")
    except Exception as e:
        print(f"[sdr-placement] notify skipped: {e}")
    return 0


def selftest():
    now = datetime.now(timezone.utc); old = now - timedelta(hours=2)
    T = lambda i, p, f, d: {"id": i, "priority": p, "freq_hz": f, "demod": d}
    U = lambda demods, bench, req=None: {"demods": set(demods), "min_hz": 1, "max_hz": 2e9, "requires": req, "bench": bench}
    targets = [T("hi", 90, 162.55e6, "NFM"), T("lo", 30, 147.4e6, "NFM")]
    tuners = {"a": U(["NFM"], {162.55e6: 40}), "b": U(["NFM"], {162.55e6: 20})}
    cur = {"hi": ("a", old), "lo": ("b", old)}
    assert place(targets, tuners, cur, {}, now)[0] == {"hi": "a", "lo": "b"}            # stable
    out, unplaced = place(targets, tuners, cur, {"a": "down"}, now)                     # a dies:
    assert out == {"hi": "b"} and unplaced == ["lo"], (out, unplaced)                  # hi preempts lo
    tuners["b"]["bench"] = {162.55e6: 43}                                               # +3 dB: under MOVE_DB
    assert place(targets, tuners, cur, {}, now)[0]["hi"] == "a"
    tuners["b"]["bench"] = {162.55e6: 50}                                               # +10 dB but just moved
    assert place(targets, tuners, {"hi": ("a", now), "lo": ("b", now)}, {}, now)[0]["hi"] == "a"
    assert place(targets, tuners, cur, {}, now)[0]["hi"] == "b"                         # +10 dB, held long: move
    tuners = {"m": U(["NFM"], {}), "s": U(["P25"], {}, req="m")}                        # slave needs master
    out, unplaced = place([T("p", 85, 484.9e6, "P25")], tuners, {}, {}, now)
    assert out == {} and unplaced == ["p"], (out, unplaced)
    out, _ = place([T("p", 85, 484.9e6, "P25"), T("n", 80, 162.55e6, "NFM")], tuners, {}, {}, now)
    assert out == {"p": "s", "n": "m"}, out
    assert place([T("u", 50, 506e6, "P25")], {"x": U(["NFM", "AM"], {})}, {}, {}, now)[1] == ["u"]  # demod mismatch
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--init" in sys.argv:
        con = _connect(); con.autocommit = True; init(con.cursor()); print("[sdr-placement] schema + seed ok")
    else:
        sys.exit(run())
