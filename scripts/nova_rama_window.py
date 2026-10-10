#!/usr/bin/env python3
"""nova_rama_window.py — THE RAMA WINDOW: save perishable evidence before it rotates out.

From Clarke's "Rendezvous with Rama": a vast alien cylinder crosses the solar system, and
Endeavour is sent because it is the only ship close enough to reach it. Inside, the crew has
only weeks: as Rama nears the Sun its frozen sea thaws, storms rise, and they must evacuate
before it swings too close. Rama then leaves, its purpose unknown and most of its interior
never understood. The lesson for Nova is the window, not the mystery: evidence has a clock,
and someone must choose what to look at before it is gone.

Minimal first version: a static retention table (hours per raw source) in service_config
rama_window/retention_hours, overriding the defaults below. Daily, for every open Buick 8
entry it computes when each source's evidence for the case window (Mina's +/-2 h around the
latest sighting) rotates out, keeps the items expiring within 48 h, ranks them most
perishable first (RFC 3227 order of volatility), records them in rama_window and files one
claude_queue line with the export command per item. No automatic snapshots.

Snapshot step (manual, the export command): --snapshot ID writes Mina's Typescript for the
case (raw DB rows only, no media, no face data) to the data volumes, and the daily run deletes
that file once the case is no longer open. Camera media is never copied by this organ; the
queue line says when person footage is already past the face TTL (service_config
face_retention/ttl_hours, 72 h) and must not be exported.

CLI:    --run [--dry-run] [--horizon H]   --snapshot ID [--dry-run]   --selftest
Tables: rama_window (listed items and snapshots)
Config: rama_window/retention_hours (seeded from defaults on the first real run);
        snapshots go to mina_typescript/out_dir
Schedule: daily 06:15 `nova_buick8_log.py --expiry` (merged into the Buick 8 Logbook on
2026-10-09, organ audit M13; `nova_rama_window.py --run/--snapshot` still work as thin wrappers).
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

SERVICE = "rama_window"
QUEUE_SESSION = "rama-window"
HORIZON_H = 48
# Retention in hours per raw source. ponytail: static guesses until each is measured (the spec's "first job").
DEFAULT_RETENTION_H = {
    "scanner_audio": 24,     # local audio is deleted after transcription; Broadcastify archive window assumed
    "frigate": 288,          # NAS frigate/recordings held 12 day-dirs on 2026-10-08
    "syslog": 2160,          # nova_retention.py: syslog_events 90 d
    "presence": 2160,        # nova_retention.py: telemetry.presence 90 d (monthly partitions)
    "adsb": 4320,            # telemetry.overhead_flights: no pruning job found; 180 d assumed
}
TYPESCRIPT_SOURCES = ("syslog", "presence", "adsb")   # rows Mina's Typescript preserves

SCHEMA = """
CREATE TABLE IF NOT EXISTS rama_window (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  case_id bigint NOT NULL, source text NOT NULL, expires_at timestamptz,
  score real, status text NOT NULL DEFAULT 'listed', path text);
CREATE UNIQUE INDEX IF NOT EXISTS rama_window_item ON rama_window (case_id, source, expires_at, status);
"""


def log(m: str) -> None:
    print(f"[rama-window {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a failed read degrades to "nothing known"
        log(f"query failed: {e}")
        return []


# ── pure ────────────────────────────────────────────────────────────────────

def expiries(window_start: datetime, ret: dict) -> dict:
    """When each source's evidence for a window starting at window_start rotates out."""
    return {s: window_start + timedelta(hours=float(h)) for s, h in ret.items()}


def due(cases: list, ret: dict, now: datetime, horizon_h: float = HORIZON_H) -> list:
    """Items still alive but gone within the horizon, most perishable first.
    cases = [{id, kind, last_seen}]."""
    from nova_mina_typescript import window
    out = []
    for c in cases:
        start, _end = window(c["last_seen"])
        for src, exp in expiries(start, ret).items():
            left = (exp - now).total_seconds() / 3600
            if 0 < left <= horizon_h:
                # ponytail: value of information = 1 for every item, so rank is pure volatility.
                # Severity x CARDINAL uncertainty is the next step once CARDINAL grades Buick 8 kinds.
                out.append({"case_id": c["id"], "kind": c["kind"], "source": src, "expires_at": exp,
                            "window_start": start, "hours_left": round(left, 1), "score": round(1.0 / left, 4)})
    return sorted(out, key=lambda i: (-i["score"], i["case_id"], i["source"]))


def export_cmd(item: dict, now: datetime, face_ttl_h: float) -> str:
    if item["source"] in TYPESCRIPT_SOURCES:
        return f"python3 nova_buick8_log.py --snapshot {item['case_id']}"
    if item["source"] == "frigate":
        deadline = item["window_start"] + timedelta(hours=face_ttl_h)
        faces = ("person footage is past the face TTL: export objects and vehicles only"
                 if now >= deadline else f"person footage of non-household people must be deleted by "
                                         f"{deadline:%Y-%m-%d %H:%M} UTC (face TTL)")
        return (f"Frigate UI -> Export, exterior cameras, {item['window_start']:%Y-%m-%d %H:%M} UTC + 4 h; {faces}")
    return ("transcripts are already permanent in nova_memories; audio only from the Broadcastify Calls "
            f"archive for {item['window_start']:%Y-%m-%d %H:%M} UTC + 4 h")


def queue_text(items: list, now: datetime, face_ttl_h: float) -> tuple:
    desc = f"Rama Window {now:%Y-%m-%d}: evidence for {len({i['case_id'] for i in items})} open Buick 8 case(s) " \
           f"rotates out within {HORIZON_H} h"
    ctx = "\n".join(f"#{i['case_id']} {i['kind']} · {i['source']} expires {i['expires_at']:%Y-%m-%d %H:%M} UTC "
                    f"({i['hours_left']} h): {export_cmd(i, now, face_ttl_h)}" for i in items)
    return desc, ctx


# ── PG ──────────────────────────────────────────────────────────────────────

def retention(cur) -> dict:
    try:
        cfg = W.get_config(cur, SERVICE, "retention_hours", None)
    except Exception as e:  # noqa: BLE001 — fall back to the static table
        log(f"retention config unreadable: {e}")
        cfg = None
    return {**DEFAULT_RETENTION_H, **(cfg or {})}


def face_ttl(cur) -> float:
    from nova_face_retention import settings
    return settings(cur)[1]


def open_cases(cur) -> list:
    rows = _q(cur, "SELECT id, kind, last_seen FROM unexplained_events WHERE status='open' ORDER BY id")
    return [{"id": r[0], "kind": r[1], "last_seen": r[2]} for r in rows]


def record(cur, items: list) -> None:
    for i in items:
        cur.execute("INSERT INTO rama_window (case_id, source, expires_at, score) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING", (i["case_id"], i["source"], i["expires_at"], i["score"]))


def file_queue(cur, desc: str, ctx: str) -> None:
    cur.execute("INSERT INTO claude_sessions (session_id, status) VALUES (%s,'active') "
                "ON CONFLICT (session_id) DO NOTHING", (QUEUE_SESSION,))
    cur.execute("INSERT INTO claude_queue (session_id, status, priority, description, context) "
                "SELECT %s,'queued',3,%s,%s WHERE NOT EXISTS (SELECT 1 FROM claude_queue WHERE description=%s)",
                (QUEUE_SESSION, desc, ctx, desc))


def expire_snapshots(cur, dry: bool = False) -> int:
    """Snapshots expire when their case closes: delete the file (data volumes only) and mark it."""
    from nova_mina_typescript import safe_dir
    exists = _q(cur, "SELECT to_regclass('rama_window')")
    if not exists or exists[0][0] is None:
        return 0
    rows = _q(cur, "SELECT r.id, r.path FROM rama_window r LEFT JOIN unexplained_events u ON u.id=r.case_id "
                   "WHERE r.status='snapshot' AND coalesce(u.status, 'gone') <> 'open'")
    for rid, path in rows:
        if dry:
            print(f"  would expire snapshot {path}")
            continue
        try:
            p = Path(path)
            safe_dir(str(p.parent))
            p.unlink(missing_ok=True)
        except (ValueError, OSError, TypeError) as e:
            log(f"snapshot {rid} not removed: {e}")
            continue
        cur.execute("UPDATE rama_window SET status='expired' WHERE id=%s", (rid,))
    return len(rows)


def run(dry: bool = False, horizon_h: float = HORIZON_H, now: datetime | None = None) -> list:
    now = now or datetime.now(timezone.utc)
    conn = W.connect()
    try:
        cur = conn.cursor()
        ret, ttl = retention(cur), face_ttl(cur)
        cases = open_cases(cur)
        items = due(cases, ret, now, horizon_h)
        log(f"{'DRY RUN ' if dry else ''}{len(cases)} open case(s); {len(items)} item(s) expire within {horizon_h} h")
        for i in items:
            print(f"  #{i['case_id']:<5} {i['source']:<14} {i['hours_left']:>6} h  {export_cmd(i, now, ttl)}")
        expired = expire_snapshots(cur, dry=dry)
        if dry:
            return items
        ensure_schema(cur)
        if W.get_config(cur, SERVICE, "retention_hours", None) is None:
            W.set_config(cur, SERVICE, "retention_hours", DEFAULT_RETENTION_H, by="nova_rama_window")
        record(cur, items)
        if items:
            file_queue(cur, *queue_text(items, now, ttl))
        log(f"recorded {len(items)} item(s); {expired} snapshot(s) expired")
        return items
    finally:
        conn.close()


def snapshot(case_id: int, dry: bool = False) -> str | None:
    """The preservation step: Mina's Typescript for the case, on the data volumes."""
    import nova_mina_typescript as M
    conn = W.connect()
    try:
        cur = conn.cursor()
        case = M.load_case(cur, case_id)
        if not case:
            log(f"no Buick 8 entry #{case_id}")
            return None
        text = M.build(cur, case)
        n = text.count("\n| 20")
        if dry:
            out = W.get_config(cur, M.SERVICE, "out_dir", M.DEFAULT_OUT)
            log(f"DRY RUN: would preserve typescript of #{case_id} ({n} timeline lines) under {out}")
            return text
        p = M.write(cur, case, text)
        ensure_schema(cur)
        cur.execute("INSERT INTO rama_window (case_id, source, status, path) VALUES (%s,'typescript','snapshot',%s) "
                    "ON CONFLICT DO NOTHING", (case_id, str(p)))
        log(f"preserved #{case_id} -> {p}")
        return str(p)
    finally:
        conn.close()


def selftest() -> int:
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    ret = {"scanner_audio": 24, "frigate": 288, "syslog": 2160}
    cases = [{"id": 1, "kind": "k", "last_seen": now - timedelta(hours=10)},     # audio expires in 12 h
             {"id": 2, "kind": "k", "last_seen": now - timedelta(hours=250)}]    # frigate in 40 h
    items = due(cases, ret, now)
    assert [(i["case_id"], i["source"]) for i in items] == [(1, "scanner_audio"), (2, "frigate")], items
    assert due([], ret, now) == [] and due(cases, ret, now, horizon_h=1) == []
    assert "objects and vehicles only" in export_cmd(items[1], now, 72)
    assert "--snapshot 7" in export_cmd({"source": "syslog", "case_id": 7}, now, 72)
    desc, ctx = queue_text(items, now, 72)
    assert "2026-10-08" in desc and "#2" in ctx
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="list open Buick 8 evidence expiring soon; record and queue")
    ap.add_argument("--snapshot", type=int, metavar="ID", help="preserve Mina's Typescript for one Buick 8 id")
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    ap.add_argument("--horizon", type=float, default=HORIZON_H, help="hours ahead to look (default 48)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.snapshot or a.run:   # merged into nova_buick8_log.py on 2026-10-09 (organ audit M13)
        import nova_buick8_log as B8
        log("merged into nova_buick8_log.py on 2026-10-09 — running its "
            + ("--snapshot" if a.snapshot else "--expiry") + " mode")
        if a.snapshot:
            return B8.run_snapshot(a.snapshot, a.dry_run)
        return B8.run_expiry(a.dry_run, a.horizon)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
