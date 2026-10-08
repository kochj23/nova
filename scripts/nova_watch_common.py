#!/usr/bin/env python3
"""nova_watch_common.py — shared plumbing for Nova's watch organs.

The watch organs (Bodach Watch, Night Watch, the Buick 8 Logbook, the Derry Clock, The Shine)
all read the same raw feeds: scanner transcripts geocoded relative to home, CHP incidents,
low helicopters from ADS-B, the security organ's never-seen network devices, and exterior
camera detections from Frigate. This module loads those feeds ONCE, the same way, so an organ
can never quietly disagree with another about what "near home" or "exterior motion" means.

Rules every organ inherits from here:
  * Home coordinates come from service_config (service='geo', key='home') via nova_geo_query.
    They are never printed, logged, posted or written to any watch table.
  * journal_safe() strips street addresses and compass bearings, so any text that might reach
    the public journal cannot be used to triangulate the house.
  * Every loader returns raw evidence rows; scoring lives in the organ, not here.

Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import bisect
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

DSN = os.environ.get("NOVA_OPS_DSN", "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")
MEM_DSN = os.environ.get("NOVA_MEM_DSN", "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj")
TZ = ZoneInfo("America/Los_Angeles")

# Frigate cameras whose name marks them as indoors. Everything else Frigate reports is exterior.
INTERIOR_CAMERA_PREFIXES = ("interior_", "3d_printers")
MEDICAL_WORDS = ("ambulance", "medical", "rescue", "unconscious", "not breathing", "cardiac",
                 "fall victim", "fallen", "person down", "man down", "overdose", "paramedic", "902")


def log(tag: str, msg: str) -> None:
    print(f"[{tag} {datetime.now():%H:%M:%S}] {msg}", flush=True)


def connect(dsn: str = DSN, attempts: int = 3, delay: float = 2.0, _sleep=time.sleep):
    """psycopg2 connection with bounded retry + linear backoff (PG failover takes seconds)."""
    import psycopg2
    last = None
    for i in range(attempts):
        try:
            conn = psycopg2.connect(dsn, connect_timeout=10)
            conn.autocommit = True
            return conn
        except Exception as e:  # noqa: BLE001 — retried, then re-raised
            last = e
            if i < attempts - 1:
                _sleep(delay * (i + 1))
    raise last


def retry(fn, *args, attempts: int = 3, delay: float = 2.0, tag: str = "watch", _sleep=None, **kw):
    """Call fn(*args, **kw) up to `attempts` times with linear backoff. An exception OR a falsy
    result (notify/send_imessage/post return False on failure) counts as a failed attempt.
    Every failure is logged — never silent. Returns the last result (False if it kept raising)."""
    res = False
    for i in range(attempts):
        try:
            res = fn(*args, **kw)
            if res:
                return res
            log(tag, f"{getattr(fn, '__name__', 'call')} attempt {i + 1}/{attempts} returned {res!r}")
        except Exception as e:  # noqa: BLE001 — retried, logged
            res = False
            log(tag, f"{getattr(fn, '__name__', 'call')} attempt {i + 1}/{attempts} failed: {e}")
        if i < attempts - 1:
            (_sleep or time.sleep)(delay * (i + 1))
    return res


def post_slack(text: str, channel: str, timeout: int = 10) -> bool:
    """Slack-only post that REPORTS failure (nova_config.post_both swallows it), so callers can
    wrap it in retry(). Returns True only when Slack answered ok."""
    import urllib.request
    import nova_config
    token = nova_config.slack_bot_token()
    if not token:
        return False
    req = urllib.request.Request(
        f"{nova_config.SLACK_API}/chat.postMessage",
        data=json.dumps({"channel": channel, "text": text, "mrkdwn": True}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return bool(json.loads(r.read()).get("ok"))


def home(_sleep=None):
    """(lat, lon) of home from service_config. Never log the return value."""
    from nova_geo_query import home_coords
    h = retry(home_coords, tag="geo", _sleep=_sleep)
    if not h:
        raise RuntimeError("service_config geo/home is not set")
    return h[0], h[1]


def miles(lat1, lon1, lat2, lon2) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))


def is_exterior_camera(name: str | None) -> bool:
    n = (name or "").lower()
    return bool(n) and not n.startswith(INTERIOR_CAMERA_PREFIXES)


def is_medical(text: str | None) -> bool:
    t = (text or "").lower()
    return any(w in t for w in MEDICAL_WORDS)


_ADDR = re.compile(r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Z][\w'.-]*\s+){0,4}"
                   r"(?:Avenue|Ave|Street|St|Boulevard|Blvd|Drive|Dr|Road|Rd|Lane|Ln|Way|Place|Pl|"
                   r"Court|Ct|Circle|Cir|Terrace|Ter|Parkway|Pkwy)\b\.?", re.I)
_BEARING = re.compile(r"\(?~?\s*\d+(?:\.\d+)?\s*(?:mi|miles?|nm|km)\s+(?:N|S|E|W|NE|NW|SE|SW|"
                      r"NNE|ENE|ESE|SSE|SSW|WSW|WNW|NNW)\b\)?", re.I)


def journal_safe(text: str) -> str:
    """Remove street addresses and distance+bearing pairs (either one can locate the house)."""
    t = _ADDR.sub("[a nearby street]", text or "")
    t = _BEARING.sub("(nearby)", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def local_hour(ts: datetime) -> int:
    return ts.astimezone(TZ).hour


def in_night(ts: datetime, start_h: int = 22, end_h: int = 7) -> bool:
    h = local_hour(ts)
    return h >= start_h or h < end_h


def episodes(rows, gap_s: int = 120):
    """Collapse per-frame detections into episodes.
    rows: iterable of (ts, key) — key e.g. (camera, label). Returns [(key, start, end, n)]."""
    last: dict = {}
    out = []
    for ts, key in sorted(rows, key=lambda r: r[0]):
        ep = last.get(key)
        if ep is not None and (ts - ep[2]).total_seconds() <= gap_s:
            ep[2] = ts
            ep[3] += 1
        else:
            ep = [key, ts, ts, 1]
            last[key] = ep
            out.append(ep)
    return [tuple(e) for e in out]


def count_between(sorted_ts: list, start: datetime, end: datetime) -> int:
    return bisect.bisect_left(sorted_ts, end) - bisect.bisect_left(sorted_ts, start)


def percentile(values, q: float) -> float:
    v = sorted(values)
    if not v:
        return 0.0
    k = (len(v) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return float(v[lo]) if lo == hi else v[lo] + (v[hi] - v[lo]) * (k - lo)


# ── config ──────────────────────────────────────────────────────────────────

def get_config(cur, service: str, key: str, default=None):
    cur.execute("SELECT value FROM service_config WHERE service=%s AND key=%s", (service, key))
    r = cur.fetchone()
    if not r:
        return default
    v = r[0]
    return json.loads(v) if isinstance(v, str) else v


def set_config(cur, service: str, key: str, value, by: str = "nova_watch_organs") -> None:
    cur.execute(
        "INSERT INTO service_config (service, key, value, updated_at, updated_by) "
        "VALUES (%s, %s, %s::jsonb, now(), %s) ON CONFLICT (service, key) DO UPDATE "
        "SET value=EXCLUDED.value, updated_at=now(), updated_by=EXCLUDED.updated_by",
        (service, key, json.dumps(value), by))


# ── schema (one place, idempotent) ──────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS bodach_scores (
  id bigserial PRIMARY KEY,
  window_start timestamptz NOT NULL,
  window_end timestamptz NOT NULL UNIQUE,
  score real NOT NULL,
  n_types int NOT NULL,
  strengths jsonb NOT NULL DEFAULT '{}',
  evidence jsonb NOT NULL DEFAULT '{}',
  safe_summary text,
  threshold real,
  fired boolean NOT NULL DEFAULT false,
  alerted boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS bodach_scores_ws ON bodach_scores (window_start);

CREATE TABLE IF NOT EXISTS unexplained_events (
  id bigserial PRIMARY KEY,
  kind text NOT NULL,
  signature text NOT NULL,
  description text NOT NULL,
  first_seen timestamptz NOT NULL DEFAULT now(),
  last_seen timestamptz NOT NULL DEFAULT now(),
  occurrences int NOT NULL DEFAULT 1,
  evidence jsonb NOT NULL DEFAULT '[]',
  cause text NOT NULL DEFAULT 'unknown',
  cause_evidence jsonb,
  hypotheses jsonb NOT NULL DEFAULT '[]',
  status text NOT NULL DEFAULT 'open',
  source text,
  UNIQUE (kind, signature));
CREATE INDEX IF NOT EXISTS unexplained_events_last ON unexplained_events (last_seen DESC);

CREATE TABLE IF NOT EXISTS derry_monthly (
  metric text NOT NULL,
  year int NOT NULL,
  month int NOT NULL,
  value double precision NOT NULL,
  days_covered int NOT NULL,
  evidence text,
  computed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (metric, year, month));

CREATE TABLE IF NOT EXISTS shine_contacts (
  id serial PRIMARY KEY,
  name text NOT NULL,
  relationship text,
  channel text NOT NULL DEFAULT 'imessage' CHECK (channel IN ('imessage')),
  address text NOT NULL,
  priority int NOT NULL DEFAULT 1,
  active boolean NOT NULL DEFAULT true,
  added_by text NOT NULL,
  added_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS shine_log (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  step int NOT NULL,
  action text NOT NULL,
  reason text,
  evidence jsonb,
  dry_run boolean NOT NULL,
  enabled boolean NOT NULL);
"""


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


# ── shared feed loaders (raw evidence; no scoring) ──────────────────────────

def load_scanner_near(start, end, max_mi: float = 2.0, dsn: str = MEM_DSN):
    """Scanner transcripts the geo-enricher placed within max_mi of home.
    -> [(ts, mi, text, channel)] — `mi` is from memories.metadata.geo.nearest_mi."""
    conn = connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT created_at, (metadata->'geo'->>'nearest_mi')::float, left(text, 300), "
            "metadata->>'channel' FROM memories WHERE source='scanner' "
            "AND created_at >= %s AND created_at < %s "
            "AND (metadata->'geo'->>'nearest_mi') IS NOT NULL "
            "AND (metadata->'geo'->>'nearest_mi')::float <= %s ORDER BY 1",
            (start, end, max_mi))
        return cur.fetchall()
    finally:
        conn.close()


def load_chp_near(cur, start, end, lat, lon, max_mi: float = 1.5):
    """CHP incidents within max_mi of home, one row per incident (first sighting).
    -> [(ts, type, location, mi)]"""
    d = max_mi / 55.0 + 0.01  # bbox prefilter in degrees
    cur.execute(
        "SELECT DISTINCT ON (incident_id) ts, type, location, lat, lon FROM telemetry.chp_incidents "
        "WHERE ts >= %s AND ts < %s AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s "
        "ORDER BY incident_id, ts",
        (start, end, lat - d, lat + d, lon - d * 1.3, lon + d * 1.3))
    out = []
    for ts, typ, loc, la, lo in cur.fetchall():
        mi = miles(lat, lon, float(la), float(lo))
        if mi <= max_mi:
            out.append((ts, typ or "", loc or "", round(mi, 2)))
    return sorted(out)


def load_heli(cur, start, end, max_alt: int = 2500, max_nm: float = 1.5):
    """Low, close helicopter samples. -> [(ts, hex, callsign, alt_ft, dist_nm)]"""
    cur.execute(
        "SELECT ts, hex, coalesce(callsign, ''), alt_ft, dist_nm FROM telemetry.overhead_flights "
        "WHERE ts >= %s AND ts < %s AND is_helicopter AND alt_ft < %s AND dist_nm < %s ORDER BY ts",
        (start, end, max_alt, max_nm))
    return cur.fetchall()


def load_new_devices(cur, start, end):
    """Security organ 'never seen before' network devices. -> [(ts, mac, level, title)]"""
    cur.execute(
        "SELECT ts, meta->>'mac', level, title FROM telemetry.events "
        "WHERE source='nova_security_organ' AND title ILIKE %s AND title NOT ILIKE %s "
        "AND ts >= %s AND ts < %s ORDER BY ts",
        ("%NEW DEVICE%", "[TEST]%", start, end))
    return cur.fetchall()


def load_ext_detections(cur, start, end, labels=None):
    """Frigate detections on EXTERIOR cameras. -> [(ts, camera, room, label)]"""
    cur.execute(
        "SELECT ts, metadata->>'camera', room, metadata->>'label' FROM telemetry.presence "
        "WHERE ts >= %s AND ts < %s AND metadata->>'source'='frigate' ORDER BY ts",
        (start, end))
    rows = [r for r in cur.fetchall() if is_exterior_camera(r[1])]
    if labels:
        rows = [r for r in rows if (r[3] or "") in labels]
    return rows


def heli_loiters(samples):
    """Group helicopter samples by airframe. -> [{hex, callsign, hits, min_alt, min_nm, tight}]"""
    by: dict = {}
    for ts, hx, call, alt, nm in samples:
        b = by.setdefault(hx, {"hex": hx, "callsign": call.strip(), "hits": 0,
                               "min_alt": 10 ** 6, "min_nm": 99.0, "first": ts, "last": ts})
        b["hits"] += 1
        b["min_alt"] = min(b["min_alt"], alt if alt is not None else 10 ** 6)
        b["min_nm"] = min(b["min_nm"], float(nm) if nm is not None else 99.0)
        b["last"] = ts
    out = []
    for b in by.values():
        if b["hits"] >= 5:
            b["tight"] = b["hits"] >= 8 and b["min_alt"] < 1500 and b["min_nm"] < 1.0
            out.append(b)
    return out


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [n for n in dir() if not n.startswith("_")] + ["timedelta"]
