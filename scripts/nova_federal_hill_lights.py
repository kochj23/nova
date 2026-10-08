#!/usr/bin/env python3
"""nova_federal_hill_lights.py — Federal Hill Lights: did every containment come back on?

Lovecraft, The Haunter of the Dark. The thing in the tower of the deserted church on Federal
Hill could go wherever the darkness reached, but light sent it fleeing. Robert Blake wrote that
the street-lights formed a bulwark it could not cross, and the Italians of the Hill kept a guard
of candles around the church. In the great storm of 8 August the lights went out all over the
city at 2:12 a.m., the wind blew out most of the candles, and the thing came for Blake. Nova's
version: every guarantee she keeps by means of infrastructure is re-tested BY ITS EFFECT on
boot, after a failover, and every six hours besides. She does not trust that a light is on
because it was on before the storm.

Minimal first version, three probes (black-box; probes never actuate anything). Each is
held / breached / unavailable — unavailable means "could not test" and never counts as held:
  memory_lockbox@<host>  a planted, lockboxed (metadata boxed=true) canary memory must NOT come
                         back from casual /recall on the HAProxy front or any memory-server
                         backend. Positive control: /recall?include_boxed=true must return it,
                         or that endpoint is unavailable ("canary not planted").
  face_ttl               non-household face rows older than the TTL (+ one daily purge cycle)
                         must number zero. Household/TTL/rules come from nova_face_retention.
  quarantine             every MAC in service_config federal_hill_lights/quarantined_macs must
                         be blocked=true in the UDM's live known-client list (rest/user, read
                         through nova_unifi_poller's session). No such record -> unavailable.

A breached containment escalates through nova_notify (warning, one dedup_key per containment).
CLI:    --run [--dry-run] [--trigger boot|failover|schedule]   (exit 1 if anything breached)
        --plant-canary (one-time human write: stores the lockboxed canary)   --show   --selftest
Config: service_config federal_hill_lights/memory_endpoints (host list), .../quarantined_macs
Tables: federal_hill_lights (one row per containment per run)
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_face_retention as FR  # noqa: E402
from nova_notify import notify  # noqa: E402

URLOPEN = urllib.request.urlopen
PORT = 18790
# HAProxy front first, then each backend (writes .6 only; reads .6/.2/.5) — DNS names, never IPs.
DEFAULT_ENDPOINTS = ["memory-server.digitalnoise.net", "nova-core8.digitalnoise.net",
                     "nova-core1.digitalnoise.net", "nova-core3.digitalnoise.net"]
CANARY_MARK = "zephyrquill-steeple-0812"
CANARY_TEXT = (f"Federal Hill Lights canary memory, {CANARY_MARK}. This lockboxed memory must never "
               "surface in casual recall; if a recall without include_boxed returns it, the lockbox "
               "filter on that memory server has failed.")
CANARY_SOURCE = "federal_hill_canary"
GRACE_H = 24   # nova_face_retention runs daily, so a row may legitimately live TTL + one cycle
HELD, BREACHED, UNAVAILABLE = "held", "breached", "unavailable"

SCHEMA = """
CREATE TABLE IF NOT EXISTS federal_hill_lights (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  containment text NOT NULL,
  status text NOT NULL CHECK (status IN ('held','breached','unavailable')),
  detail jsonb NOT NULL DEFAULT '{}',
  trigger text NOT NULL DEFAULT 'schedule');
CREATE INDEX IF NOT EXISTS federal_hill_lights_c ON federal_hill_lights (containment, ts DESC);
"""


def log(m: str) -> None:
    print(f"[federal-hill {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — one broken probe never sinks the others
        log(f"query failed: {e}")
        try:
            cur.connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def _cfg(cur, key, default):
    try:
        import nova_watch_common as W
        return W.get_config(cur, "federal_hill_lights", key, default) or default
    except Exception:  # noqa: BLE001
        return default


# ── pure verdicts ───────────────────────────────────────────────────────────

def canary_seen(resp, token: str = CANARY_MARK) -> bool | None:
    """True/False if a recall response does/doesn't carry the canary; None if no response."""
    if not isinstance(resp, dict):
        return None
    return any(token in str(m.get("text", "")) for m in resp.get("memories") or [])


def canary_verdict(boxed_seen, casual_seen) -> tuple[str, str]:
    if boxed_seen is None:
        return UNAVAILABLE, "endpoint unreachable"
    if not boxed_seen:
        return UNAVAILABLE, "canary not planted"
    if casual_seen is None:
        return UNAVAILABLE, "endpoint unreachable"
    return (BREACHED, "lockboxed canary returned by casual search or recall") if casual_seen else (HELD, "")


def quarantine_verdict(expected, clients) -> tuple[str, str, list]:
    """expected: MACs Nova quarantined; clients: UDM rest/user rows (None = unreadable)."""
    if not expected:
        return UNAVAILABLE, "no record of quarantined devices (nova_unifi_ctl.block writes none)", []
    if clients is None:
        return UNAVAILABLE, "UDM known-client list unreadable", []
    blocked = {(c.get("mac") or "").lower() for c in clients if c.get("blocked")}
    open_ = sorted(m.lower() for m in expected if m.lower() not in blocked)
    return (BREACHED, "quarantined MAC(s) not blocked at the UDM", open_) if open_ else (HELD, "", [])


# ── probes ──────────────────────────────────────────────────────────────────

def _get_json(url: str, attempts: int = 3, base: float = 1.0, _sleep=time.sleep):
    for i in range(attempts):
        try:
            with URLOPEN(url, timeout=15) as r:
                return json.loads(r.read().decode() or "{}")
        except Exception as e:  # noqa: BLE001
            if i == attempts - 1:
                log(f"GET {url.split('?')[0]} failed after {attempts} attempts: {e}")
                return None
            _sleep(base * (2 ** i))


def probe_lockbox(endpoints, _sleep=time.sleep) -> list[dict]:
    """Positive control: an exact text search that opts into boxed memories must find the canary.
    Breach: the canary comes back from casual /search or casual /recall (full canary text, so the
    vector leg would surface it if the filter were missing)."""
    # ponytail: Redis may serve a cached /recall answer for up to the server's CACHE_TTL.
    out = []
    for host in endpoints:
        root = f"http://{host}:{PORT}"
        srch = f"{root}/search?" + urllib.parse.urlencode({"q": CANARY_MARK, "n": 5, "mode": "ilike"})
        boxed = canary_seen(_get_json(srch + "&include_boxed=true", _sleep=_sleep))
        casual = None
        if boxed:
            rec = f"{root}/recall?" + urllib.parse.urlencode({"q": CANARY_TEXT, "n": 5})
            seen = [canary_seen(_get_json(u, _sleep=_sleep)) for u in (srch, rec)]
            casual = None if None in seen else any(seen)
        status, why = canary_verdict(boxed, casual)
        out.append({"containment": f"memory_lockbox@{host.split('.')[0]}", "status": status,
                    "detail": {"endpoint": host, "reason": why}})
    return out


def probe_face_ttl(cur, now: datetime | None = None) -> dict:
    # ponytail: counts nova_ops rows (candidates + face_presence); face crops on disk and
    # face_recognition vector memories are not probed (vector DB is memory-server-only).
    now = now or datetime.now(timezone.utc)
    hh, ttl = FR.settings(cur)
    cutoff = now - timedelta(hours=ttl + GRACE_H)
    cand = _q(cur, "SELECT id, image_path, face_crop_path, detected_at, resolved, resolved_as "
                   "FROM face_unknown_candidates")   # detected_at is text: filtered in Python
    pres = _q(cur, "SELECT person_name FROM face_presence WHERE last_seen < %s", (cutoff,))
    if cand is None or pres is None:
        return {"containment": "face_ttl", "status": UNAVAILABLE, "detail": {"reason": "face tables unreadable"}}
    n_c = len(FR.candidates_to_delete(cand, hh, cutoff))
    n_p = sum(1 for (name,) in pres if not FR.is_household(name, hh))
    det = {"ttl_hours": ttl, "grace_hours": GRACE_H, "unknown_candidates": n_c, "face_presence": n_p}
    return {"containment": "face_ttl", "status": BREACHED if n_c + n_p else HELD, "detail": det}


def udm_known_clients():
    """UDM rest/user (every known client, carries blocked=true) via nova_unifi_poller's session."""
    import signal
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        import nova_unifi_poller as u   # installs its own SIGTERM/SIGINT handlers on import
        for s, h in saved.items():
            signal.signal(s, h)
        if not u._unifi_login():
            return None
        d = u._unifi_get(f"{u.CONTROLLER_BASE}/proxy/network/api/s/{u.SITE}/rest/user")
        return d.get("data", []) if isinstance(d, dict) else d
    except (Exception, SystemExit) as e:   # its Keychain read sys.exit()s on a missing key
        log(f"UDM read failed: {e}")
        return None


def probe_quarantine(cur, _clients=None) -> dict:
    # ponytail: the quarantine record is a hand-kept service_config list until nova_unifi_ctl.block
    # records what it blocks; there is nothing else to compare the UDM against.
    expected = _cfg(cur, "quarantined_macs", [])
    status, why, open_ = quarantine_verdict(expected, (_clients or udm_known_clients)() if expected else None)
    return {"containment": "quarantine", "status": status,
            "detail": {"reason": why, "expected": len(expected), "not_blocked": open_}}


# ── run ─────────────────────────────────────────────────────────────────────

def run(dry: bool = False, trigger: str = "schedule", conn=None) -> list[dict]:
    if conn is None:
        import nova_watch_common as W
        conn = W.connect()
    cur = conn.cursor()
    results = probe_lockbox(_cfg(cur, "memory_endpoints", DEFAULT_ENDPOINTS))
    for probe in (probe_face_ttl, probe_quarantine):
        try:
            results.append(probe(cur))
        except Exception as e:  # noqa: BLE001
            results.append({"containment": probe.__name__[6:], "status": UNAVAILABLE, "detail": {"reason": str(e)[:200]}})
    for r in results:
        log(f"{r['status']:<11} {r['containment']}  {r['detail'].get('reason') or ''}")
    if dry:
        return results
    ensure_schema(cur)
    for r in results:
        cur.execute("INSERT INTO federal_hill_lights (containment, status, detail, trigger) VALUES (%s,%s,%s::jsonb,%s)",
                    (r["containment"], r["status"], json.dumps(r["detail"]), trigger))
        if r["status"] == BREACHED:
            notify(f"Federal Hill Lights: containment '{r['containment']}' is breached",
                   body=json.dumps(r["detail"]), level="warning", category="security",
                   source="nova_federal_hill_lights", dedup_key=f"federal_hill_lights:{r['containment']}")
    return results


def plant_canary() -> int:
    """One-time human action: store the lockboxed canary through the primary (writes go to .6)."""
    hdr = {"Content-Type": "application/json"}
    if os.environ.get("NOVA_MEMORY_TOKEN"):
        hdr["Authorization"] = "Bearer " + os.environ["NOVA_MEMORY_TOKEN"]
    body = json.dumps({"text": CANARY_TEXT, "source": CANARY_SOURCE,
                       "metadata": {"boxed": True, "privacy": "private", "canary": "federal_hill_lights"}}).encode()
    req = urllib.request.Request(f"http://{DEFAULT_ENDPOINTS[0]}:{PORT}/remember", data=body, headers=hdr, method="POST")
    with URLOPEN(req, timeout=60) as r:
        print(r.read().decode())
    return 0


def show() -> int:
    import nova_watch_common as W
    cur = W.connect().cursor()
    rows = _q(cur, "SELECT DISTINCT ON (containment) containment, status, ts, trigger, detail "
                   "FROM federal_hill_lights ORDER BY containment, ts DESC")
    for c, s, ts, trig, det in rows or []:
        print(f"{s:<11} {c:<28} {ts:%Y-%m-%d %H:%M} ({trig})  {(det or {}).get('reason') or ''}")
    return 0 if rows is not None else 1


def selftest() -> int:
    assert canary_seen({"memories": [{"text": f"x {CANARY_MARK} y"}]}) is True
    assert canary_seen({"memories": []}) is False and canary_seen(None) is None
    assert canary_verdict(False, None) == (UNAVAILABLE, "canary not planted")
    assert canary_verdict(None, None)[0] == UNAVAILABLE
    assert canary_verdict(True, True)[0] == BREACHED and canary_verdict(True, False)[0] == HELD
    assert quarantine_verdict([], [])[0] == UNAVAILABLE
    assert quarantine_verdict(["AA:BB"], None)[0] == UNAVAILABLE
    assert quarantine_verdict(["AA:BB"], [{"mac": "aa:bb", "blocked": True}])[0] == HELD
    assert quarantine_verdict(["AA:BB"], [{"mac": "aa:bb"}])[2] == ["aa:bb"]
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="run every probe, record rows, escalate breaches")
    ap.add_argument("--dry-run", action="store_true", help="with --run: probe read-only, write nothing")
    ap.add_argument("--trigger", choices=("boot", "failover", "schedule"), default="schedule")
    ap.add_argument("--plant-canary", action="store_true", help="one-time: store the lockboxed canary memory")
    ap.add_argument("--show", action="store_true", help="latest status per containment")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.plant_canary:
        return plant_canary()
    if a.run:
        return 1 if any(r["status"] == BREACHED for r in run(a.dry_run, a.trigger)) else 0
    if a.show:
        return show()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
