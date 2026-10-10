#!/usr/bin/env python3
"""nova_mina_typescript.py — MINA'S TYPESCRIPT: scattered records typed into one case file.

From Stoker's "Dracula": the hunters' evidence is spread over diaries, letters, newspaper
cuttings and Seward's phonograph cylinders, and nobody can find a date in the cylinders.
Mina Harker types all of it in order of time, several copies at once, so the whole case can
be read as one timeline. When the Count breaks in and burns the manuscripts, the work survives
because a copy was locked in a safe. Later she reasons from that record by ruling out the
routes he could not have taken. Harker's closing note admits the irony: almost none of the
record is an original document, only typewriting. So every line here points back to its original.

Minimal first version: given a Buick 8 Logbook id, pull a +/-2 h window around the entry's
latest sighting from five tables (telemetry.events, telemetry.incidents, syslog_events,
telemetry.presence, telemetry.overhead_flights), normalise every timestamp to UTC, sort, and
write a markdown typescript to the NAS (the copy in the safe). Each line carries its source
table, row pointer and a sha256 of the raw row. Raw lines only: no LLM, no paraphrase.
The header carries the Rama Window's evidence-expiry table for the same window
(nova_rama_window.expiries); the Rama Window in turn calls build()/write() as its snapshot step.

Privacy: no face table is read and presence rows from face methods are excluded, so a
typescript holds no face data and never outlives the 72 h face TTL of its sources.

CLI:    --case ID [--dry-run]   (dry run prints the typescript; writes nothing)   --selftest
Config: service_config mina_typescript/out_dir (default /Volumes/nas/nova/typescripts;
        must be on /Volumes/Data or the NAS, never the boot disk)
Tables: none (reads only; the file is the output)
Schedule: on demand (`nova_buick8_log.py --case ID`; merged into the Buick 8 Logbook on
2026-10-09, organ audit M13 — `nova_mina_typescript.py --case ID` is a thin wrapper), and via
the Rama Window's snapshot step.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

SERVICE = "mina_typescript"
DEFAULT_OUT = "/Volumes/nas/nova/typescripts"
ALLOWED_ROOTS = ("/Volumes/Data", "/Volumes/nas", "/Volumes/MoreData")
HALF_WINDOW = timedelta(hours=2)
# ponytail: a hard cap per source keeps syslog (~100k rows/h) bounded; the header names capped sources,
# and the cap keeps the EARLIEST rows of the window. Paging or a priority filter would lift the ceiling.
CAP = 500

# source -> SQL returning (row_id, ts, *raw fields). Params: (start, end, cap).
SOURCES = {
    "telemetry.events": "SELECT id::text, ts, source, level, category, title FROM telemetry.events "
                        "WHERE ts BETWEEN %s AND %s AND source !~* 'face' ORDER BY ts LIMIT %s",
    "telemetry.incidents": "SELECT id::text, opened_at, severity, status, host, title FROM telemetry.incidents "
                           "WHERE opened_at BETWEEN %s AND %s ORDER BY opened_at LIMIT %s",
    "syslog_events": "SELECT id::text, received_at, hostname, app_name, severity, message FROM syslog_events "
                     "WHERE received_at BETWEEN %s AND %s AND (severity <= 4 OR threat_type IS NOT NULL "
                     "OR alert_fired) ORDER BY received_at LIMIT %s",
    "telemetry.presence": "SELECT concat_ws(';', ts, method, room, person), ts, method, room, person, "
                          "metadata::text FROM telemetry.presence WHERE ts BETWEEN %s AND %s "
                          "AND coalesce(method, '') !~* 'face' ORDER BY ts LIMIT %s",
    "telemetry.overhead_flights": "SELECT id::text, ts, hex, callsign, squawk, alt_ft, dist_nm "
                                  "FROM telemetry.overhead_flights WHERE ts BETWEEN %s AND %s ORDER BY ts LIMIT %s",
}


def log(m: str) -> None:
    print(f"[mina-typescript {datetime.now():%H:%M:%S}] {m}", flush=True)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a failed read degrades to "nothing known"
        log(f"query failed: {e}")
        return []


# ── pure ────────────────────────────────────────────────────────────────────

def utc(ts) -> datetime:
    return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def window(anchor: datetime) -> tuple:
    """The case window both organs share: +/-2 h around the latest sighting."""
    a = utc(anchor)
    return a - HALF_WINDOW, a + HALF_WINDOW


def row_hash(source: str, row) -> str:
    return hashlib.sha256(json.dumps([source, *row], default=str).encode()).hexdigest()


def line(source: str, row) -> dict:
    rid, ts, *raw = row
    text = " · ".join(str(x) for x in raw if x not in (None, ""))
    return {"ts": utc(ts), "source": source, "row": str(rid).replace("|", "/"), "hash": row_hash(source, row),
            "text": " ".join(text.replace("|", "/").split())[:400]}


def collate(rows_by_source: dict) -> list:
    """Every source's rows as one chronological list (ties broken by source, then row)."""
    out = [line(s, r) for s, rows in rows_by_source.items() for r in rows]
    return sorted(out, key=lambda l: (l["ts"], l["source"], str(l["row"])))


def render(case: dict, lines: list, expiry: dict | None = None, capped=()) -> str:
    start, end = window(case["last_seen"])
    out = [f"# Typescript: Buick 8 #{case['id']} ({case['kind']})", "",
           f"- Opener: {case['description']}",
           f"- Window (UTC): {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}",
           f"- Sources: {', '.join(SOURCES)}; clock: all times UTC (skew not yet estimated)",
           "- Raw lines only. Each line points to its original row; nothing here is paraphrased.",
           "- Face data: none (no face tables read; face-method presence rows excluded).",
           f"- Lines: {len(lines)}" + (f"; capped at {CAP} for: {', '.join(capped)}" if capped else "")]
    if expiry:
        out += ["", "## Perishable evidence (Rama Window)", "", "| source | evidence expires (UTC) |", "|---|---|"]
        out += [f"| {s} | {e:%Y-%m-%d %H:%M} |" for s, e in sorted(expiry.items(), key=lambda kv: kv[1])]
    out += ["", "## Timeline", "", "| time (UTC) | source | row | sha256 | raw |", "|---|---|---|---|---|"]
    out += [f"| {l['ts']:%Y-%m-%d %H:%M:%S} | {l['source']} | {l['row']} | {l['hash'][:12]} | {l['text']} |"
            for l in lines]
    body = "\n".join(out) + "\n"
    return body + f"\nDocument sha256: {hashlib.sha256(body.encode()).hexdigest()}\n"


def safe_dir(path: str) -> Path:
    """Refuse anything that is not on /Volumes/Data, MoreData or the NAS, or that sits on the boot disk."""
    p = Path(path).resolve()
    if not str(p).startswith(ALLOWED_ROOTS):
        raise ValueError(f"refusing output dir off the data volumes: {p}")
    probe = next((q for q in (p, *p.parents) if q.exists()), Path("/"))
    if os.stat(probe).st_dev == os.stat("/").st_dev:
        raise ValueError(f"refusing output dir on the boot disk: {p}")
    return p


# ── PG ──────────────────────────────────────────────────────────────────────

def load_case(cur, case_id: int) -> dict | None:
    r = _q(cur, "SELECT id, kind, signature, description, first_seen, last_seen, status "
                "FROM unexplained_events WHERE id=%s", (case_id,))
    keys = ("id", "kind", "signature", "description", "first_seen", "last_seen", "status")
    return dict(zip(keys, r[0])) if r else None


def build(cur, case: dict) -> str:
    """Read the five sources over the case window and return the typescript text. Reads only."""
    start, end = window(case["last_seen"])
    rows = {s: _q(cur, sql, (start, end, CAP)) for s, sql in SOURCES.items()}
    capped = [s for s, r in rows.items() if len(r) >= CAP]
    from nova_rama_window import expiries, retention
    exp = expiries(start, retention(cur))
    return render(case, collate(rows), exp, capped)


def write(cur, case: dict, text: str, now: datetime | None = None) -> Path:
    """Append-only: a new file per generation; never overwrites."""
    out = safe_dir(W.get_config(cur, SERVICE, "out_dir", DEFAULT_OUT))
    out.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    p = out / f"buick8-{case['id']}-{utc(case['last_seen']):%Y%m%dT%H%M}-gen{now:%Y%m%dT%H%M%S}.md"
    with open(p, "x") as f:
        f.write(text)
    return p


def run(case_id: int, dry: bool = False) -> Path | str | None:
    conn = W.connect()
    try:
        cur = conn.cursor()
        case = load_case(cur, case_id)
        if not case:
            log(f"no Buick 8 entry #{case_id}")
            return None
        text = build(cur, case)
        if dry:
            print(text)
            log(f"DRY RUN: typescript for #{case_id} not written")
            return text
        p = write(cur, case, text)
        log(f"wrote {p}")
        return p
    finally:
        conn.close()


def selftest() -> int:
    t = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    s, e = window(t)
    assert (e - s) == timedelta(hours=4)
    ls = collate({"syslog_events": [("9", t, "host", "a|b")],
                  "telemetry.events": [("3", t - timedelta(minutes=5), "src", None, "cat", "title")]})
    assert [l["source"] for l in ls] == ["telemetry.events", "syslog_events"], ls
    assert "|" not in ls[1]["text"] and ls[0]["hash"] == row_hash("telemetry.events", ("3", t - timedelta(minutes=5),
                                                                                      "src", None, "cat", "title"))
    doc = render({"id": 1, "kind": "k", "description": "d", "last_seen": t}, ls, {"syslog": t})
    assert "Document sha256" in doc and "Perishable evidence" in doc
    for bad in ("/tmp/x", "/etc"):
        try:
            safe_dir(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert not any("face_" in sql for sql in SOURCES.values())
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--case", type=int, help="Buick 8 Logbook id (unexplained_events.id)")
    ap.add_argument("--dry-run", action="store_true", help="print the typescript, write nothing")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.case:   # merged into nova_buick8_log.py on 2026-10-09 (organ audit M13)
        import nova_buick8_log as B8
        log("merged into nova_buick8_log.py on 2026-10-09 — running its --case mode")
        return B8.run_case(a.case, a.dry_run)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
