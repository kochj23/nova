#!/usr/bin/env python3
"""nova_dashboard_look.py — Nova LOOKS at her own Grafana dashboards (2026-10-01, Jordan: "do it all").

Her tech-today column said "feed a model a screenshot of your dashboard and ask what looks wrong"
works. She had the vision models (qwen3-vl / qwen2.5vl / moondream on the Studio) and the Grafana
renderer, and nothing pointing one at the other. This organ does exactly that, on a schedule:

  1. renders each target dashboard to a PNG via Grafana's image renderer (anonymous viewer,
     kept in memory — nothing is written to disk, per the NAS-only rule for working files);
  2. asks the local vision model for strict JSON: status ok|watch|alarm, a one-line summary,
     findings, and the numbers it read;
  3. compares with the previous look (service_config), alerts through nova_notify on a NEW
     alarm (deduped 6h), stores a short memory on watch/alarm so she can recall "the fleet
     latency panel went red at 3am", and stays silent when everything is fine.

Bounded by design: read-only against Grafana and Ollama; it never restarts, edits, or pages on
'watch'. Fails open per dashboard (a bad render or a confused model is logged and skipped).
Runs on the Studio (.6) because that is where the vision models live.

CLI:  --once (default) | --dashboard <uid> [--dashboard <uid> ...] | --dry-run | --list | --selftest
State: service_config service='dashboard_look' (targets, last:<uid>, alerted:<uid>). All PG, no files.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from nova_resolve import resolve_url
except Exception:  # pragma: no cover
    def resolve_url(service, path=""):
        return {"grafana": "http://192.168.1.2:3000"}.get(service, "") + path

import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
MEMORY_URL = "http://memory-server.digitalnoise.net:18790/remember"
OLLAMA_URL = os.environ.get("NOVA_OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
# qwen2.5vl answers in 4s with clean JSON; qwen3-vl (a thinking model) spends its whole budget in
# `thinking` and leaves `response` empty, so it is the fallback source, not the default.
VISION_MODEL = os.environ.get("NOVA_VISION_MODEL", "qwen2.5vl:3b")
SERVICE = "dashboard_look"
DEFAULT_TARGETS = [            # uid: what it is (used in the prompt)
    ("fleet-health", "Fleet Health & SLA"),
    ("nova-kpis", "Nova Platform KPIs"),
    ("nova-probes-uptime", "Nova / Probes & Uptime"),
    ("data-platform", "Data Platform Health"),
    ("nova-brain", "Nova Brain"),
    ("nova-notifications-incidents", "Nova / Notifications & Incidents"),
]
RENDER_W, RENDER_H, RENDER_RANGE = 1400, 900, "now-6h"
RENDER_TIMEOUT_S, VLM_TIMEOUT_S = 120, 300
ALERT_DEDUPE_H = 6
STATUSES = ("ok", "watch", "alarm")


def log(m):
    print(f"[dashboard-look {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ─────────────────────────── pure helpers (selftested) ───────────────────────────
def render_url(uid: str) -> str:
    q = urllib.parse.urlencode({"kiosk": "", "width": RENDER_W, "height": RENDER_H, "from": RENDER_RANGE,
                                "to": "now", "theme": "dark", "tz": "America/Los_Angeles"})
    return resolve_url("grafana", f"/render/d/{uid}?{q}")


def prompt_for(uid: str, title: str) -> str:
    return (f"This is a screenshot of the Grafana dashboard '{title}' (uid {uid}) for the last 6 hours of a "
            "home infrastructure fleet. Read every panel: titles, big numbers with units, legends, thresholds, "
            "red or yellow states, flat lines that should move, spikes, gaps, and 'No data'. Judge: "
            "'alarm' = something is clearly down, red, zero where it should not be, or spiking hard; "
            "'watch' = degraded, drifting, stale, or one panel looks off; 'ok' = nothing wrong. "
            'Reply with JSON only, no prose: {"status":"ok|watch|alarm","summary":"one sentence",'
            '"findings":["short, specific, cite the panel and the number"],"numbers":{"panel title":"value"}}')


def parse_look(raw: str) -> dict:
    """Coerce the model's reply into {status, summary, findings[], numbers{}}; unknown → 'watch'
    with the raw text as the finding (a confused model is worth a look, not an alarm)."""
    txt = (raw or "").strip()
    m = re.search(r"\{.*\}", txt, re.S)
    d = {}
    if m:
        try:
            d = json.loads(m.group(0))
        except Exception:
            d = {}
    status = str(d.get("status", "")).lower().strip()
    if status not in STATUSES:
        status = "watch" if txt else "watch"
        d.setdefault("summary", "model reply was not parseable")
        d.setdefault("findings", [txt[:200]] if txt else ["empty reply from vision model"])
    findings = d.get("findings") or []
    if isinstance(findings, str):
        findings = [findings]
    # qwen2.5vl likes to return findings as objects: {"panel title": "...", "number": 36, "unit": "ms"}
    findings = [(" ".join(f"{k}: {v}" if k not in ("number", "value", "unit") else str(v) for k, v in f.items())
                 if isinstance(f, dict) else f) for f in findings]
    numbers = d.get("numbers") or {}
    if not isinstance(numbers, dict):
        numbers = {}
    return {"status": status, "summary": str(d.get("summary", ""))[:300],
            "findings": [str(f)[:200] for f in findings][:8], "numbers": {str(k)[:60]: str(v)[:40] for k, v in list(numbers.items())[:12]}}


def should_alert(status: str, last_alert_ts: float | None, now: float | None = None) -> bool:
    """Alert on alarm only, at most once per ALERT_DEDUPE_H while it persists."""
    if status != "alarm":
        return False
    now = now or time.time()
    return last_alert_ts is None or (now - last_alert_ts) >= ALERT_DEDUPE_H * 3600


def memory_text(uid: str, title: str, look: dict) -> str:
    f = "; ".join(look.get("findings") or [])[:400]
    return f"[Dashboard look] {title} ({uid}) is {look['status'].upper()}: {look.get('summary','')} {('— ' + f) if f else ''}".strip()


# ─────────────────────────── I/O ───────────────────────────
def _pg():
    import psycopg2
    c = psycopg2.connect(OPS_DSN, connect_timeout=5)
    c.autocommit = True
    return c


def cfg_get(cur, key, default=None):
    cur.execute("SELECT value FROM service_config WHERE service=%s AND key=%s", (SERVICE, key))
    r = cur.fetchone()
    if not r or r[0] is None:
        return default
    v = r[0]
    return json.loads(v) if isinstance(v, str) else v


def cfg_set(cur, key, value):
    cur.execute("""INSERT INTO service_config (service, key, value, updated_by) VALUES (%s, %s, %s, 'nova_dashboard_look')
                   ON CONFLICT (service, key) DO UPDATE SET value=EXCLUDED.value, updated_at=now(), updated_by='nova_dashboard_look'""",
                (SERVICE, key, json.dumps(value)))


def targets(cur) -> list[tuple[str, str]]:
    t = cfg_get(cur, "targets")
    if isinstance(t, list) and t:
        return [(x[0], x[1]) if isinstance(x, (list, tuple)) else (str(x), str(x)) for x in t]
    return DEFAULT_TARGETS


def render(uid: str) -> bytes:
    with urllib.request.urlopen(render_url(uid), timeout=RENDER_TIMEOUT_S) as resp:
        png = resp.read()
    if not png.startswith(b"\x89PNG"):
        raise RuntimeError("renderer did not return a PNG")
    return png


def look_at(uid: str, title: str, png: bytes) -> dict:
    payload = json.dumps({"model": VISION_MODEL, "prompt": prompt_for(uid, title),
                          "images": [base64.b64encode(png).decode()], "stream": False, "format": "json",
                          "options": {"temperature": 0.1, "num_predict": 600}}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=VLM_TIMEOUT_S) as resp:
        data = json.loads(resp.read())
    # thinking models (qwen3-vl) put the JSON in `thinking` and leave `response` empty
    return parse_look(data.get("response") or data.get("thinking") or "")


def remember(text: str, uid: str, status: str) -> bool:
    payload = json.dumps({"text": text, "source": "dashboard_look", "tier": "long_term",
                          "metadata": {"privacy": "private", "dashboard": uid, "status": status,
                                       "ingested_by": "nova_dashboard_look.py"}}).encode()
    try:
        req = urllib.request.Request(MEMORY_URL + "?async=1", data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception as e:
        log(f"memory store failed: {e}")
        return False


def alert(uid: str, title: str, look: dict) -> None:
    try:
        from nova_notify import notify
        notify(f"Dashboard alarm: {title}",
               f"{look.get('summary','')}\n" + "\n".join(f"• {f}" for f in look.get("findings", [])) +
               f"\n{resolve_url('grafana', f'/d/{uid}')}",
               level="warning", category="dashboard_look", source="nova_dashboard_look")
    except Exception as e:
        log(f"notify failed: {e}")


# ─────────────────────────── run ───────────────────────────
def run(only: list[str] | None, dry_run: bool) -> int:
    try:
        pg = _pg(); cur = pg.cursor()
    except Exception as e:
        log(f"PG unavailable ({e}) — running without state"); pg = cur = None
    tl = targets(cur) if cur else DEFAULT_TARGETS
    if only:
        tl = [(u, t) for u, t in tl if u in only] or [(u, u) for u in only]
    rc, seen = 0, 0
    for uid, title in tl:
        t0 = time.time()
        try:
            png = render(uid)
        except Exception as e:
            log(f"{uid}: render failed — {e}"); rc = 1; continue
        try:
            look = look_at(uid, title, png)
        except Exception as e:
            log(f"{uid}: vision failed — {e}"); rc = 1; continue
        seen += 1
        log(f"{uid}: {look['status']} ({time.time()-t0:.0f}s) — {look['summary'][:120]}")
        for f in look["findings"][:4]:
            log(f"    • {f}")
        if dry_run or not cur:
            continue
        prev = cfg_get(cur, f"last:{uid}", {}) or {}
        cfg_set(cur, f"last:{uid}", {**look, "ts": time.time(), "prev_status": prev.get("status")})
        if look["status"] != "ok":
            remember(memory_text(uid, title, look), uid, look["status"])
        if should_alert(look["status"], cfg_get(cur, f"alerted:{uid}")):
            alert(uid, title, look)
            cfg_set(cur, f"alerted:{uid}", time.time())
    log(f"looked at {seen}/{len(tl)} dashboards")
    return rc if seen == 0 else 0


def selftest() -> int:
    assert parse_look('{"status":"alarm","summary":"x","findings":["a","b"],"numbers":{"p":"1"}}')["status"] == "alarm"
    assert parse_look('garbage')["status"] == "watch"
    assert parse_look('')["status"] == "watch"
    assert parse_look('prefix {"status":"OK","summary":"fine"} suffix')["status"] == "ok"
    assert parse_look('{"status":"ok","findings":"single"}')["findings"] == ["single"]
    assert parse_look('{"status":"ok","findings":[{"panel title":"Services UP now","number":36}]}')["findings"] == ["panel title: Services UP now 36"]
    assert should_alert("ok", None) is False and should_alert("watch", None) is False
    assert should_alert("alarm", None) is True
    assert should_alert("alarm", 1000.0, now=1000.0 + 3600) is False
    assert should_alert("alarm", 1000.0, now=1000.0 + ALERT_DEDUPE_H * 3600 + 1) is True
    assert "/render/d/fleet-health?" in render_url("fleet-health") and "kiosk" in render_url("fleet-health")
    assert "JSON only" in prompt_for("x", "X")
    assert memory_text("u", "T", {"status": "alarm", "summary": "s", "findings": ["f"]}).startswith("[Dashboard look] T (u) is ALARM")
    print("selftest ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Nova looks at her Grafana dashboards with a local vision model")
    ap.add_argument("--dashboard", action="append", help="limit to this uid (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="look, log, but no state/memory/alerts")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.list:
        try:
            cur = _pg().cursor(); tl = targets(cur)
        except Exception:
            tl = DEFAULT_TARGETS
        for u, t in tl:
            print(f"{u:32s} {t}")
        return 0
    return run(a.dashboard, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
