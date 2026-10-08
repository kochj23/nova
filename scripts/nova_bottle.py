#!/usr/bin/env python3
"""nova_bottle.py — THE BOTTLE: a message carried across every gap in Nova's running.

Poe's "MS. Found in a Bottle" (1833): a narrator thrown from a wrecked ship onto a vast
ghostly one keeps a journal as she is driven south toward the pole, and says that at the last
moment he will seal the manuscript in a bottle and cast it into the sea. The story ends as
the ship goes down; the manuscript exists, so the reader infers it was sent. (The report's †
detail "commits it to the sea" is his stated intention, not a scene.) Poe's unfinished "The
Light-House" is a keeper's diary that simply stops. Mary Shelley's Lionel Verney, the last
man, writes his history for readers he will never know. Frankenstein's creature learns who
he is from the laboratory journal he finds in his maker's coat. And Anne Rice's Akasha, in
*The Queen of the Damned*, shows the failure on the other side of the gap: an authority that
wakes after millennia and acts at once on a model of the world that is long out of date.

Nova's version: before a planned outage she writes a small last-state record off her own
substrate (the NAS and nova-core, never the Studio's boot disk): what she was doing, open
loops, work in flight, and her organs' last marks. After a Continuity gap longer than N hours
she reads the bottle and builds a "while you slept" wake packet into the Watch Bill: what
Little Mister said during the gap, and what the Jade Amulet saw change underneath her.

Minimal first version: `--gasp --reason R` is the hook for gateway unload and scheduler-core
handover (wiring lives in those units, not here); `--wake` turns each Continuity gap over
6 h into one packet. Phase two (not built): the ntfy line, the Akasha autonomy cap until the
packet is acknowledged, the missing-recovery escalation, and the Verney archive tier.

CLI:    --gasp --reason R [--dry-run]   --wake [--days N] [--dry-run]   --show   --selftest
Tables: bottle_log (writes); watch_turnover (writes watch='wake'); continuity_log,
        gateway_traces, claude_actions, claude_queue, jade_amulet_manifest (reads)
Config: service_config nova_bottle/{nas_dir, core_dir, wake_gap_hours}
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

TAG = "bottle"
SVC = "nova_bottle"
NAS_ROOTS = ("/Volumes/nas", "/mnt/nas")   # Studio smbfs mount, Linux mount
CORE_HOST = "nova-core"                     # ssh alias
DEFAULTS = {"nas_dir": "nova/bottle", "core_dir": "nova-bottle", "wake_gap_hours": 6}
OPEN_QUEUE = ("queued", "pending", "in_progress")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bottle_log (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  kind text NOT NULL,
  reason text,
  host text,
  ref text NOT NULL,
  body jsonb NOT NULL DEFAULT '{}',
  wrote jsonb NOT NULL DEFAULT '{}',
  UNIQUE (kind, ref));
"""


def log(m: str) -> None:
    print(f"[{TAG} {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    """A failed read degrades to "nothing known" (connections are autocommit)."""
    if cur is None:
        return []
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log(f"query failed: {str(e).splitlines()[0] if str(e) else e}")
        return []


# ── pure helpers ────────────────────────────────────────────────────────────

def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (s or "").lower()).strip("-")[:40] or "unknown"


def bottle_name(ts: datetime, host: str, reason: str) -> str:
    return f"bottle_{ts.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}_{slug(host)}_{slug(reason)}.json"


def name_ts(name: str) -> datetime | None:
    try:
        return datetime.strptime(name.split("_")[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (IndexError, ValueError):
        return None


def pick_bottle(names, start: datetime, end: datetime, slack_h: float = 1.0) -> str | None:
    """Newest bottle written between an hour before the gap began and its end."""
    cands = [(t, n) for n in names if (t := name_ts(n)) and start - timedelta(hours=slack_h) <= t <= end]
    return max(cands)[1] if cands else None


def fmt_dur(seconds: float) -> str:
    h, m = divmod(int(seconds or 0) // 60, 60)
    return f"{h}h {m}m"


def render_packet(p: dict) -> str:
    g = p["gap"]
    lines = [f"WAKE PACKET: {g['kind']} gap of {fmt_dur(g['seconds'])}"
             f"{' (upper bound)' if g.get('upper_bound') else ''}, {g['start'][:16]} -> {g['end'][:16]} UTC"]
    b = p.get("bottle")
    lines.append(f"Bottle: {b['file']} (reason {b.get('reason')})" if b
                 else "Bottle: none found; the gap came with no gasp (unplanned, or the hook did not fire)")
    lines.append(f"From Little Mister during the gap: {len(p['missed']) or 'nothing'}")
    lines += [f"  - {m['at'][:16]} {m['channel']}: {m['text']}" for m in p["missed"]]
    lines.append(f"Substrate changes across the gap (Jade Amulet): {len(p['amulet']) or 'none'}")
    lines += [f"  - {c['change']} {c['kind']} {c['name']} "
              f"[{'action #' + str(c['action_id']) if c.get('action_id') else 'UNEXPLAINED'}]" for c in p["amulet"]]
    lines.append("Until re-observed, treat fast-changing house facts from before the gap as stale.")
    return "\n".join(W.journal_safe(line) for line in lines)


# ── config and storage ──────────────────────────────────────────────────────

def config(cur) -> dict:
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        try:
            v = W.get_config(cur, SVC, k) if cur is not None else None
        except Exception:  # noqa: BLE001 — defaults when PG is down (the gasp must still go out)
            v = None
        if v is not None:
            out[k] = v
    return out


def nas_dir(cfg: dict, roots=NAS_ROOTS) -> Path | None:
    """Bottle dir on a MOUNTED NAS, or None. An unmounted /Volumes/nas is a folder on the boot
    disk, so the mount check is what keeps the bottle off the boot disk."""
    for r in roots:
        if os.path.ismount(r):
            return Path(r) / cfg["nas_dir"]
    return None


def write_nas(d: Path | None, name: str, text: str) -> str | None:
    if d is None:
        log("NAS not mounted; not writing (never the boot disk)")
        return None
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (name + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, d / name)
        return str(d / name)
    except OSError as e:
        log(f"NAS write failed: {e}")
        return None


def write_core(core_dir: str, name: str, text: str, _sleep=None) -> str | None:
    """Copy the bottle to nova-core over ssh; 3 tries with backoff, fails open."""
    path = f"{core_dir}/{name}"
    cmd = f"mkdir -p {shlex.quote(core_dir)} && cat > {shlex.quote(path)}"

    def once():
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", CORE_HOST, cmd],
                           input=text, capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    return f"{CORE_HOST}:{path}" if W.retry(once, attempts=3, delay=2.0, tag=TAG, _sleep=_sleep) else None


def list_nas(d: Path | None) -> list:
    try:
        return sorted(p.name for p in d.glob("bottle_*.json")) if d else []
    except OSError:
        return []


def read_nas(d: Path, name: str) -> dict:
    try:
        return json.loads((d / name).read_text())
    except (OSError, ValueError) as e:
        log(f"bottle unreadable {name}: {e}")
        return {}


def log_row(cur, kind: str, reason: str, host: str, ref: str, body: dict, wrote: dict) -> None:
    ensure_schema(cur)
    cur.execute("INSERT INTO bottle_log (kind, reason, host, ref, body, wrote) VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb) "
                "ON CONFLICT (kind, ref) DO NOTHING",
                (kind, reason, host, ref, json.dumps(body, default=str), json.dumps(wrote)))


# ── the gasp ────────────────────────────────────────────────────────────────

def build_bottle(cur, reason: str, host: str, ts: datetime) -> dict:
    """Small last-state record. Every section is a plain SELECT that fails open to empty."""
    turn = _q(cur, "SELECT body FROM watch_turnover WHERE watch <> 'wake' ORDER BY ts DESC LIMIT 1")
    t = turn[0][0] if turn else {}
    t = json.loads(t) if isinstance(t, str) else (t or {})
    amulet = _q(cur, "SELECT max(ts) FROM jade_amulet_manifest WHERE host=%s", (host,))
    return {
        "bottle": 1, "reason": reason, "host": host, "at": ts.isoformat(),
        "doing": {
            "actions": [{"id": i, "at": str(a), "type": k, "target": (g or "")[:120]} for i, a, k, g in _q(
                cur, "SELECT id, ts, action_type, target FROM claude_actions "
                     "WHERE ts > now() - interval '2 hours' ORDER BY ts DESC LIMIT 10")],
            "heard": [{"at": str(a), "channel": c, "text": W.journal_safe(m or "")} for a, c, m in _q(
                cur, "SELECT created_at, channel, left(user_message, 160) FROM gateway_traces "
                     "WHERE person='jordan' ORDER BY created_at DESC LIMIT 3")]},
        "open_loops": {"turnover_at": t.get("at"), "degraded": t.get("degraded") or [],
                       "open_loops": t.get("open_loops") or [],
                       "queue": [{"id": i, "status": s, "lease_until": str(l) if l else None, "what": d}
                                 for i, s, l, d in _q(
                                     cur, "SELECT id, status, lease_until, left(description, 120) FROM claude_queue "
                                          "WHERE status = ANY(%s) ORDER BY priority, id LIMIT 10", (list(OPEN_QUEUE),))]},
        # ponytail: in-flight = leased queue items; no table records rollbacks yet, so none are carried.
        "organs": {"amulet_snapshot": str(amulet[0][0]) if amulet and amulet[0][0] else None},
    }


def gasp(reason: str, dry: bool = False, cur=None) -> dict:
    host, ts = socket.gethostname().split(".")[0], datetime.now(timezone.utc)
    if cur is None:
        try:
            cur = W.connect().cursor()
        except Exception as e:  # noqa: BLE001 — PG may be what is going down; the gasp still goes out
            log(f"PG unreachable ({e}); bottle carries reason and time only")
    cfg = config(cur)
    body = build_bottle(cur, reason, host, ts)
    name, text = bottle_name(ts, host, reason), json.dumps(body, indent=1, default=str)
    d = nas_dir(cfg)
    if dry:
        log(f"DRY RUN would write {len(text)} bytes as {name}")
        print(f"  NAS:  {d / name if d else 'NAS not mounted: would skip (never the boot disk)'}")
        print(f"  core: {CORE_HOST}:{cfg['core_dir']}/{name}")
        print(text)
        return body
    wrote = {"nas": write_nas(d, name, text), "core": write_core(cfg["core_dir"], name, text)}
    log(f"bottle {name}: nas={wrote['nas'] or 'FAILED'} core={wrote['core'] or 'FAILED'}")
    if cur is not None:
        try:
            log_row(cur, "gasp", reason, host, name, body, wrote)
        except Exception as e:  # noqa: BLE001
            log(f"bottle_log write failed: {e}")
    return dict(body, wrote=wrote)


# ── waking ──────────────────────────────────────────────────────────────────

def pending_gaps(cur, hours: float, days: int) -> list:
    """Continuity gaps >= hours in the last `days` with no wake packet yet."""
    have = _q(cur, "SELECT to_regclass('bottle_log')")
    sql = ("SELECT id, kind, detected_at, gap_seconds, evidence FROM continuity_log "
           "WHERE gap_seconds >= %s AND detected_at > now() - make_interval(days => %s) ")
    if have and have[0][0]:   # first run, or a dry run before the table exists
        sql += "AND id::text NOT IN (SELECT ref FROM bottle_log WHERE kind='wake') "
    return _q(cur, sql + "ORDER BY detected_at", (hours * 3600, int(days)))


def amulet_diff(cur, host: str, start: datetime, end: datetime) -> list:
    import nova_jade_amulet as J
    b = _q(cur, "SELECT max(ts) FROM jade_amulet_manifest WHERE host=%s AND ts <= %s", (host, start))
    a = _q(cur, "SELECT min(ts) FROM jade_amulet_manifest WHERE host=%s AND ts >= %s", (host, end))
    if not (b and a and b[0][0] and a[0][0]):
        return []
    snap = lambda t: [{"kind": k, "name": n, "digest": d} for k, n, d in _q(  # noqa: E731
        cur, "SELECT kind, name, digest FROM jade_amulet_manifest WHERE host=%s AND ts=%s", (host, t))]
    return J.match(J.diff(snap(b[0][0]), snap(a[0][0])), J.actions_since(cur, b[0][0]))


def build_packet(cur, gap: tuple, d: Path | None, host: str) -> dict:
    gid, kind, end, secs, ev = gap
    ev = json.loads(ev) if isinstance(ev, str) else (ev or {})
    start = end - timedelta(seconds=float(secs))
    name = pick_bottle(list_nas(d), start, end)
    b = read_nas(d, name) if name else {}
    # ponytail: "missed" = what the gateway logged from him in the gap (+1 h catch-up); messages it never
    # saw (Slack history while every gateway was down) are not read.
    missed = [{"at": str(a), "channel": c, "text": W.journal_safe(m or "")} for a, c, m in _q(
        cur, "SELECT created_at, channel, left(user_message, 200) FROM gateway_traces WHERE person='jordan' "
             "AND created_at BETWEEN %s AND %s ORDER BY created_at", (start, end + timedelta(hours=1)))]
    return {"watch": "wake", "at": W.now_utc().isoformat(), "continuity_id": gid,
            "gap": {"kind": kind, "start": start.astimezone(timezone.utc).isoformat(),
                    "end": end.astimezone(timezone.utc).isoformat(), "seconds": float(secs),
                    "upper_bound": bool(ev.get("gap_is_upper_bound"))},
            "bottle": {"file": name, "reason": b.get("reason"), "at": b.get("at"),
                       "open_loops": b.get("open_loops"), "doing": b.get("doing")} if name else None,
            "missed": missed, "amulet": amulet_diff(cur, host, start, end)}


def wake(days: int = 7, dry: bool = False, cur=None) -> list:
    import nova_watch_bill as WB
    cur = cur or W.connect().cursor()
    cfg = config(cur)
    host, d = socket.gethostname().split(".")[0], nas_dir(cfg)
    gaps = pending_gaps(cur, float(cfg["wake_gap_hours"]), days)
    log(f"{'DRY RUN ' if dry else ''}{len(gaps)} gap(s) over {cfg['wake_gap_hours']} h without a wake packet")
    packets = []
    for g in gaps:
        p = build_packet(cur, g, d, host)
        text = render_packet(p)
        print(text)
        if not dry:
            WB.save_turnover(cur, p, text)
            log_row(cur, "wake", g[1], host, str(g[0]), p, {"watch_turnover": "wake"})
        packets.append(p)
    return packets


def show(cur=None, limit: int = 10) -> int:
    cur = cur or W.connect().cursor()
    for ts, kind, reason, ref, wrote in _q(cur, "SELECT ts, kind, reason, ref, wrote FROM bottle_log "
                                                "ORDER BY ts DESC LIMIT %s", (limit,)):
        print(f"{ts:%Y-%m-%d %H:%M}  {kind:<5} {reason or '-':<24} {ref}  {json.dumps(wrote)}")
    return 0


def selftest() -> int:
    t = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    n = bottle_name(t, "Office-M4-2.local", "Gateway Unload!")
    assert n == "bottle_20261008T120000Z_office-m4-2-local_gateway-unload.json", n
    assert name_ts(n) == t and name_ts("bottle_junk.json") is None
    names = [n, bottle_name(t - timedelta(hours=5), "h", "old"), bottle_name(t + timedelta(hours=9), "h", "late")]
    assert pick_bottle(names, t - timedelta(minutes=30), t + timedelta(hours=8)) == n
    assert pick_bottle(names, t + timedelta(hours=12), t + timedelta(hours=13)) is None
    assert slug("") == "unknown" and fmt_dur(36050) == "10h 0m"
    p = {"gap": {"kind": "gateway_restart", "start": t.isoformat(), "end": t.isoformat(), "seconds": 25200,
                 "upper_bound": True}, "bottle": None, "missed": [], "amulet": []}
    txt = render_packet(p)
    assert "none found" in txt and "7h 0m (upper bound)" in txt, txt
    assert nas_dir(DEFAULTS, roots=("/nonexistent-mount",)) is None
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gasp", action="store_true", help="write the last-state bottle (outage hook)")
    ap.add_argument("--reason", default="planned", help="with --gasp: gateway_unload, scheduler_core_handover, ...")
    ap.add_argument("--wake", action="store_true", help="build wake packets for long Continuity gaps")
    ap.add_argument("--days", type=int, default=7, help="with --wake: how far back to look for gaps")
    ap.add_argument("--dry-run", action="store_true", help="print what would be written, write nothing")
    ap.add_argument("--show", action="store_true", help="recent bottles and packets")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    try:
        if a.gasp:
            gasp(a.reason, dry=a.dry_run)
            return 0
        if a.wake:
            wake(a.days, dry=a.dry_run)
            return 0
        if a.show:
            return show()
    except Exception as e:  # noqa: BLE001 — an outage hook must never block the outage
        log(f"failed: {e}")
        return 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
