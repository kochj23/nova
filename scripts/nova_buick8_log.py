#!/usr/bin/env python3
"""nova_buick8_log.py — the Buick 8 Logbook: things that happened that nobody can explain.

From King's "From a Buick 8": the troopers keep a logbook of what the car does and refuse to
pretend they know why. This is that book for Nova. Each row in nova_ops.unexplained_events is
an anomaly with NO known cause: a never-seen network device that nobody claimed, a sensor
that went silent, an ADS-B emergency squawk overhead, a power spike on a plug that is normally
dark, a strong static BLE device nobody recognises, an IPS signature the firewall itself calls
"unknown".

THE RULE: Nova never states a cause for an entry without evidence.
  * `cause` is 'unknown' on insert and NOTHING in this module sets it except resolve(), which
    refuses to run without an evidence payload.
  * Guesses go in `hypotheses`, each one labelled {"status": "hypothesis"} with who offered it.
    A hypothesis is never a cause; describe() always says so.
  * Other organs that want to talk about an anomaly call cause_statement(kind, signature) and
    use its words verbatim — it returns "cause unknown" until resolve() has been called.

Recurrence: the same (kind, signature) seen again increments `occurrences` once per distinct
occurrence_key (e.g. a sensor silence episode, a power spike hour), so a detector re-firing
every 30 minutes about one silence is one occurrence, not forty.

Helpers for other organs:
    from nova_buick8_log import log_unexplained, add_hypothesis, resolve, cause_statement
    log_unexplained("scanner_code", "code 10-99X", "Unrecognised code on LAPD NE", {"memory_id": ...})

Usage:
    nova_buick8_log.py              # run every feeder over the last 2 days
    nova_buick8_log.py --backfill   # same over 30 days (idempotent)
    nova_buick8_log.py --list       # open entries, most recent first
    nova_buick8_log.py --dry-run    # print what each feeder would log, write nothing
    nova_buick8_log.py --expiry [--horizon H] [--dry-run]   # the Rama Window (daily 06:15)
    nova_buick8_log.py --case ID [--dry-run]                # Mina's Typescript for one entry
    nova_buick8_log.py --snapshot ID [--dry-run]            # Rama's preservation step (typescript + rama_window row)

Case modes (merged 2026-10-09, organ audit M13): the Rama Window (nova_rama_window.py) and Mina's
Typescript (nova_mina_typescript.py) only ever worked on Buick 8 entries, so they are modes of the
logbook now. Their code, the rama_window table, rama_window/retention_hours and
mina_typescript/out_dir are unchanged; this module calls their functions.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

TAG = "buick8"
EMERGENCY_SQUAWKS = {"7500": "hijack code", "7600": "radio failure code",
                     "7700": "general emergency code", "7400": "lost-link (UAV) code"}
POWER_MIN_W = 300.0
POWER_FACTOR = 1.5
BLE_MIN_HOURS = 12
BLE_MIN_RSSI = -65
NETWORK_GRACE = timedelta(hours=24)   # give Jordan a day to claim a new device first
MAX_EVIDENCE = 25


class CauseWithoutEvidence(ValueError):
    """Raised when someone tries to set a cause without evidence. That is the whole rule."""


# ── core helpers (other organs call these) ──────────────────────────────────

def _cur(cur=None):
    if cur is not None:
        return cur, None
    conn = W.connect()
    return conn.cursor(), conn


def log_unexplained(kind: str, signature: str, description: str, evidence: dict | None = None,
                    occurrence_key: str | None = None, ts=None, source: str | None = None,
                    cur=None) -> int | None:
    """Record (or recount) an anomaly. Never sets a cause. Returns the row id."""
    if not kind or not signature:
        raise ValueError("kind and signature are required")
    ev = dict(evidence or {})
    ev["occurrence_key"] = occurrence_key or ev.get("occurrence_key") or (str(ts) if ts else None)
    ts = ts or W.now_utc()
    c, conn = _cur(cur)
    try:
        c.execute("SELECT id, evidence FROM unexplained_events WHERE kind=%s AND signature=%s",
                  (kind, signature))
        row = c.fetchone()
        if row is None:
            c.execute(
                "INSERT INTO unexplained_events (kind, signature, description, first_seen, last_seen, "
                "occurrences, evidence, source) VALUES (%s,%s,%s,%s,%s,1,%s::jsonb,%s) "
                "ON CONFLICT (kind, signature) DO NOTHING RETURNING id",
                (kind, signature, description, ts, ts, json.dumps([ev], default=str), source))
            r = c.fetchone()
            return r[0] if r else None
        rid, existing = row
        existing = existing or []
        keys = {e.get("occurrence_key") for e in existing if isinstance(e, dict)}
        if ev["occurrence_key"] is not None and ev["occurrence_key"] in keys:
            c.execute("UPDATE unexplained_events SET last_seen=greatest(last_seen, %s) WHERE id=%s",
                      (ts, rid))
            return rid
        new_ev = (existing + [ev])[-MAX_EVIDENCE:]
        c.execute(
            "UPDATE unexplained_events SET occurrences=occurrences+1, "
            "first_seen=least(first_seen, %s), last_seen=greatest(last_seen, %s), "
            "evidence=%s::jsonb WHERE id=%s",
            (ts, ts, json.dumps(new_ev, default=str), rid))
        return rid
    finally:
        if conn:
            conn.close()


def add_hypothesis(event_id: int, label: str, note: str = "", by: str = "nova", cur=None) -> None:
    """Attach a clearly-labelled GUESS. Never touches `cause`."""
    h = {"label": label, "note": note, "by": by, "at": W.now_utc().isoformat(), "status": "hypothesis"}
    c, conn = _cur(cur)
    try:
        c.execute("UPDATE unexplained_events SET hypotheses = hypotheses || %s::jsonb WHERE id=%s",
                  (json.dumps([h]), event_id))
    finally:
        if conn:
            conn.close()


def validate_resolution(cause: str, evidence) -> None:
    if not cause or not str(cause).strip() or str(cause).strip().lower() == "unknown":
        raise CauseWithoutEvidence("a resolution needs a concrete cause")
    if not evidence or (isinstance(evidence, (dict, list, str)) and not evidence):
        raise CauseWithoutEvidence("no cause without evidence: pass what proves it")


def resolve(event_id: int, cause: str, evidence, by: str = "nova", cur=None) -> None:
    """Set the cause. Refuses without evidence — that is the logbook's one rule."""
    validate_resolution(cause, evidence)
    payload = {"evidence": evidence, "by": by, "at": W.now_utc().isoformat()}
    c, conn = _cur(cur)
    try:
        c.execute("UPDATE unexplained_events SET cause=%s, cause_evidence=%s::jsonb, status='resolved' "
                  "WHERE id=%s", (cause.strip(), json.dumps(payload, default=str), event_id))
    finally:
        if conn:
            conn.close()


def describe(row: dict) -> str:
    """The only sanctioned wording. Unknown stays unknown; hypotheses stay hypotheses."""
    base = (f"{row['description']} — seen {row['occurrences']}x "
            f"(first {row['first_seen']:%Y-%m-%d}, last {row['last_seen']:%Y-%m-%d})")
    if row.get("status") == "resolved" and row.get("cause") not in (None, "", "unknown"):
        return base + f". Cause: {row['cause']} (evidence on file)."
    hyps = row.get("hypotheses") or []
    tail = f" {len(hyps)} unconfirmed hypothesis(es) on file, none proven." if hyps else ""
    return base + ". Cause unknown." + tail


def cause_statement(kind: str, signature: str, cur=None) -> str:
    """What any organ may say about an anomaly's cause. Defaults to 'cause unknown'."""
    c, conn = _cur(cur)
    try:
        c.execute("SELECT description, occurrences, first_seen, last_seen, cause, status, hypotheses "
                  "FROM unexplained_events WHERE kind=%s AND signature=%s", (kind, signature))
        r = c.fetchone()
    finally:
        if conn:
            conn.close()
    if not r:
        return "Cause unknown (not in the logbook)."
    keys = ("description", "occurrences", "first_seen", "last_seen", "cause", "status", "hypotheses")
    return describe(dict(zip(keys, r)))


# ── feeders: turn existing detectors' outputs into logbook entries ──────────

def feed_network(cur, since):
    """Security-organ never-seen devices that are STILL unexplained after a day: no owner in
    device_owner and no name in known_devices. Auto-resolves when an owner appears."""
    out = []
    for ts, mac, level, title in W.load_new_devices(cur, since, W.now_utc() - NETWORK_GRACE):
        if not mac:
            continue
        cur.execute("SELECT person, device_label FROM telemetry.device_owner WHERE lower(mac)=lower(%s)",
                    (mac,))
        owner = cur.fetchone()
        cur.execute("SELECT client_name FROM telemetry.known_devices WHERE lower(client_mac)=lower(%s)",
                    (mac,))
        kn = cur.fetchone()
        if owner or (kn and (kn[0] or "").strip()):
            continue
        out.append(dict(kind="network_device", signature=mac.lower(), ts=ts,
                        description=f"Never-seen network device {mac.lower()} that nobody has claimed",
                        evidence={"event_title": title, "level": level}, occurrence_key=f"{ts:%Y%m%d}"))
    return out


def resolve_claimed_network(cur) -> int:
    cur.execute("SELECT u.id, o.person, o.device_label, o.source FROM unexplained_events u "
                "JOIN telemetry.device_owner o ON lower(o.mac)=u.signature "
                "WHERE u.kind='network_device' AND u.status='open'")
    n = 0
    for rid, person, label, src in cur.fetchall():
        resolve(rid, f"device claimed: {label or 'device'} of {person or 'household'}",
                {"telemetry.device_owner": {"person": person, "label": label, "source": src}},
                by="nova_buick8_log", cur=cur)
        n += 1
    return n


_NS = re.compile(r"Presence method '([^']+)'.*?\(last: ([0-9: -]+)\)")


def feed_negative_space(cur, since):
    """Sensors that went silent (nova_negative_space). One occurrence per silence episode."""
    cur.execute("SELECT ts, title FROM telemetry.events WHERE source='nova_negative_space.py' "
                "AND ts >= %s ORDER BY ts", (since,))
    out = []
    for ts, title in cur.fetchall():
        m = _NS.search(title or "")
        if not m:
            continue
        method, last = m.group(1), m.group(2).strip()
        out.append(dict(kind="sensor_silence", signature=f"presence:{method}", ts=ts,
                        description=f"Presence feed '{method}' went silent with no recorded reason",
                        evidence={"last_report": last}, occurrence_key=last))
    return out


def feed_squawks(cur, since):
    cur.execute("SELECT ts, hex, coalesce(callsign,''), squawk, alt_ft, dist_nm FROM telemetry.overhead_flights "
                "WHERE ts >= %s AND squawk = ANY(%s) ORDER BY ts", (since, list(EMERGENCY_SQUAWKS)))
    out = []
    for ts, hx, call, sq, alt, nm in cur.fetchall():
        out.append(dict(kind="adsb_squawk", signature=f"{hx}:{sq}", ts=ts,
                        description=f"Aircraft {call.strip() or hx} squawked {sq} ({EMERGENCY_SQUAWKS[sq]}) "
                                    f"within receiver range",
                        evidence={"alt_ft": alt, "dist_nm": nm}, occurrence_key=f"{hx}:{sq}:{ts:%Y%m%d}"))
    return out


def power_spike(watts: float, prior_max: float) -> bool:
    """Above POWER_MIN_W AND beyond POWER_FACTOR x the most this device has EVER drawn in the
    prior 30 days. Appliance cycles (dryer, dishwasher) reach their usual peaks every week, so
    a percentile flags them constantly; a never-before-seen ceiling is the actual anomaly."""
    return watts >= POWER_MIN_W and watts > POWER_FACTOR * max(prior_max, 1.0)


def feed_power(cur, since):
    """A reading far above anything the device drew in the 30 days before it."""
    cur.execute("SELECT device_id, date_trunc('hour', ts), max(watts) FROM telemetry.energy "
                "WHERE ts >= %s AND watts >= %s GROUP BY 1, 2 ORDER BY 2", (since, POWER_MIN_W))
    out = []
    for dev, hour, w in cur.fetchall():
        cur.execute("SELECT max(watts) FROM telemetry.energy WHERE device_id=%s AND ts >= %s AND ts < %s",
                    (dev, hour - timedelta(days=30), hour))
        prior = cur.fetchone()[0]
        if prior is None:
            continue  # no history: cannot call anything abnormal
        if power_spike(float(w), float(prior)):
            out.append(dict(kind="power_spike", signature=f"energy:{dev}", ts=hour,
                            description=f"Power on '{dev}' far above anything it drew in the prior 30 days",
                            evidence={"max_watts": round(float(w), 1), "prior_30d_max": round(float(prior), 1)},
                            occurrence_key=f"{hour:%Y%m%d%H}"))
    return out


def feed_ble(cur, since):
    """Strong, static BLE devices new in the window, never mapped, whose name is also new."""
    cur.execute(
        "WITH f AS (SELECT device_mac, min(ts) f FROM telemetry.bluetooth WHERE ts >= %s - interval '30 days' "
        "           GROUP BY 1 HAVING min(ts) >= %s) "
        "SELECT b.device_mac, f.f, max(b.device_name), count(DISTINCT date_trunc('hour', b.ts)), max(b.rssi) "
        "FROM telemetry.bluetooth b JOIN f USING (device_mac) WHERE b.ts < f.f + interval '24 hours' "
        "GROUP BY 1, 2", (since, since))
    rows = cur.fetchall()
    cur.execute("SELECT min(ts) FROM telemetry.bluetooth")
    data_start = cur.fetchone()[0]
    out = []
    for mac, first, name, hrs, mx in rows:
        if data_start is None or first < data_start + timedelta(days=7):
            continue  # "new" is meaningless until there is a week of history to be new against
        if hrs < BLE_MIN_HOURS or (mx or -999) < BLE_MIN_RSSI:
            continue
        cur.execute("SELECT 1 FROM telemetry.ble_device_map WHERE device_mac=%s UNION ALL "
                    "SELECT 1 FROM telemetry.device_owner WHERE lower(mac)=lower(%s) LIMIT 1", (mac, mac))
        if cur.fetchone():
            continue
        if name:
            cur.execute("SELECT 1 FROM telemetry.bluetooth WHERE device_name=%s AND ts < %s "
                        "AND device_mac <> %s LIMIT 1", (name, first, mac))
            if cur.fetchone():
                continue  # a known device on a rotated address
        out.append(dict(kind="ble_phantom", signature=f"ble:{mac.lower()}", ts=first,
                        description=f"Unrecognised static BLE device {name or '(no name)'} heard strongly for "
                                    f"{hrs}h on its first day",
                        evidence={"hours": hrs, "max_rssi": mx, "name": name},
                        occurrence_key=f"{first:%Y%m%d}"))
    return out


def feed_unknown_events(cur, since):
    """Bus events whose own detector says it does not know what it saw."""
    # Narrow on purpose: free-text matching on "unidentified" caught video titles and articles.
    cur.execute("SELECT id, ts, source, category, title FROM telemetry.events WHERE ts >= %s "
                "AND source NOT IN ('nova_negative_space.py', 'nova_buick8_log') "
                "AND (meta->>'cause' = 'unknown' OR (category = 'ips' AND title ~* %s)) ORDER BY ts",
                (since, r"\munknown\M"))
    out = []
    for eid, ts, src, cat, title in cur.fetchall():
        sig = re.sub(r"\d{4,}", "#", f"{src}:{cat}:{title}")[:200]
        out.append(dict(kind="event_unknown", signature=sig, ts=ts,
                        description=f"{src} reported something it could not identify: {title[:140]}",
                        evidence={"event_id": eid}, occurrence_key=f"{ts:%Y%m%d}"))
    return out


FEEDERS = (feed_network, feed_negative_space, feed_squawks, feed_power, feed_ble, feed_unknown_events)


def run(days: int, dry_run: bool) -> int:
    conn = W.connect()
    cur = conn.cursor()
    if not dry_run:
        W.ensure_schema(cur)
    since = W.now_utc() - timedelta(days=days)
    total = 0
    for f in FEEDERS:
        try:
            items = f(cur, since)
        except Exception as e:  # one broken feeder never blocks the others
            W.log(TAG, f"{f.__name__} failed: {e}")
            continue
        W.log(TAG, f"{f.__name__}: {len(items)} observation(s)")
        for it in items:
            if dry_run:
                print(f"  would log [{it['kind']}] {it['signature']}: {it['description']}")
                continue
            log_unexplained(it["kind"], it["signature"], it["description"], it["evidence"],
                            occurrence_key=it["occurrence_key"], ts=it["ts"], source=f.__name__, cur=cur)
            total += 1
    if not dry_run:
        n = resolve_claimed_network(cur)
        if n:
            W.log(TAG, f"resolved {n} network device(s) now claimed in device_owner (evidence attached)")
    W.log(TAG, f"processed {total} observation(s)")
    return 0


def list_open(limit: int = 30) -> int:
    conn = W.connect()
    cur = conn.cursor()
    cur.execute("SELECT kind, signature, description, occurrences, first_seen, last_seen, cause, status, "
                "hypotheses FROM unexplained_events ORDER BY last_seen DESC LIMIT %s", (limit,))
    keys = ("kind", "signature", "description", "occurrences", "first_seen", "last_seen", "cause",
            "status", "hypotheses")
    for r in cur.fetchall():
        d = dict(zip(keys, r))
        print(f"[{d['kind']}] {describe(d)}")
    return 0


# ── case modes (merged from the Rama Window and Mina's Typescript, 2026-10-09) ──

def run_expiry(dry_run: bool = False, horizon_h: float | None = None) -> int:
    """The Rama Window: open entries' perishable evidence ranked by time to expiry; records
    rama_window rows and files one claude_queue line (nova_rama_window.run, unchanged)."""
    import nova_rama_window as R
    R.run(dry=dry_run, horizon_h=R.HORIZON_H if horizon_h is None else horizon_h)
    return 0


def run_case(case_id: int, dry_run: bool = False) -> int:
    """Mina's Typescript for one entry, written to mina_typescript/out_dir (nova_mina_typescript.run)."""
    import nova_mina_typescript as M
    return 0 if M.run(case_id, dry=dry_run) else 1


def run_snapshot(case_id: int, dry_run: bool = False) -> int:
    """Rama's preservation step: the typescript plus a rama_window 'snapshot' row, so the daily
    expiry run deletes it once the entry closes (nova_rama_window.snapshot)."""
    import nova_rama_window as R
    return 0 if R.snapshot(case_id, dry=dry_run) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backfill", action="store_true", help="scan 30 days instead of 2")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--expiry", action="store_true",
                    help="Rama Window: open entries' evidence expiring soon; record + queue")
    ap.add_argument("--horizon", type=float, default=None, help="with --expiry: hours ahead (default 48)")
    ap.add_argument("--case", type=int, metavar="ID", help="Mina's Typescript for one entry")
    ap.add_argument("--snapshot", type=int, metavar="ID", help="preserve one entry's typescript (Rama step)")
    a = ap.parse_args(argv)
    if a.expiry:
        return run_expiry(a.dry_run, a.horizon)
    if a.case:
        return run_case(a.case, a.dry_run)
    if a.snapshot:
        return run_snapshot(a.snapshot, a.dry_run)
    if a.list:
        return list_open()
    return run(a.days or (30 if a.backfill else 2), a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
