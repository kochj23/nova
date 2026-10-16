#!/usr/bin/env python3
"""nova_minion_hub.py — where Nova's minions check in (Studio, :37590).

Jordan, 2026-10-16: "When I get the new machine, I will let you use it as a minion overnight. Can you create a
Nova screensaver like all the other screensavers out there for years that do work for someone when the machines
are idle? The screensaver could show something cool like the work it is doing for Nova."

A MINION is a Mac that lends its GPU to Nova: it runs Ollama with the reader's model, and nova_minion_agent.py on
it heartbeats here every 30 s with its state — 'idle' (screensaver up / nobody at the keyboard, on AC), 'away'
(someone is using it: no new work), 'dedicated' (a fleet node that is always available). The work reader
(nova_work_lessons.py) sends reads to every minion that is idle/dedicated with a fresh heartbeat, notes what it is
reading in minion_activity, and records the node on each work row. /status turns that into what the Nova Minion
screensaver draws: what this Mac is reading now, what it has learned for Nova tonight, the fleet's lessons.

  POST /heartbeat           X-Minion-Token; {"node","state","ollama_url","models":[...],"info":{...}}
  GET  /status?node=N       X-Minion-Token; the screensaver's JSON (apps/NovaMinionSaver/README.md)
  GET  /fleet?node=N        X-Minion-Token; every minion's state + what it is reading, for displays (the iPad app)
  GET  /health
CLI  --serve [--port 37590] | --enroll NODE [--url http://host:11434] [--dedicated | --viewer] | --list | --selftest
A VIEWER (--viewer) is a display, not a minion: it never heartbeats and is never sent work; its token reads /fleet.

Tokens: --enroll prints a token once; only its sha256 is stored (minion_nodes.token_hash). The minion keeps it in
its login Keychain (service 'nova-minion'). Reads are LAN-only and token-gated: lesson text is Nova's own.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nova_dsn as _nova_dsn  # noqa: E402

OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
PORT = 37590
STATES = ("idle", "away", "dedicated", "offline")
WINDOW_H = 12                 # "tonight" = the last 12 hours
ACTIVITY_MAX_MIN = 20         # a minion_activity row older than this is a crashed reader, not a read
FRESH_S = 90
# only works the reader really has in hand (a --dry-run or a crashed stream leaves rows behind)
LIVE = ("(EXISTS (SELECT 1 FROM work_lessons w WHERE w.work_key = a.work_key AND w.status = 'running') OR "
        "(a.work_key LIKE 'media:%%' AND EXISTS (SELECT 1 FROM media_reingest_queue q "
        "WHERE 'media:' || left(q.file_path, 290) = a.work_key AND q.status = 'running')))")
_CITE = re.compile(r"\s*(\[\d+\])+")
_NODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_LOCAL = threading.local()


def log(m):
    print(f"[minion-hub {datetime.now():%H:%M:%S}] {m}", flush=True)


def db():
    import psycopg2
    c = getattr(_LOCAL, "conn", None)
    if c is None or c.closed:
        c = psycopg2.connect(OPS_DSN)
        c.autocommit = True
        _LOCAL.conn = c
    return c.cursor()


def token_hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def authed(cur, node: str, tok: str | None) -> bool:
    if not tok or not _NODE.match(node or ""):
        return False
    cur.execute("SELECT token_hash FROM minion_nodes WHERE node=%s", (node,))
    r = cur.fetchone()
    return bool(r and r[0] and secrets.compare_digest(r[0], token_hash(tok)))


def clean_hb(d: dict) -> dict:
    """Pure: validate a heartbeat body."""
    state = str(d.get("state") or "")
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}")
    url = str(d.get("ollama_url") or "")
    if url and not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d+)?/?$", url):
        raise ValueError("bad ollama_url")
    models = [str(m)[:80] for m in (d.get("models") or []) if isinstance(m, str)][:40]
    info = d.get("info") if isinstance(d.get("info"), dict) else {}
    info = {k: v for k, v in info.items() if isinstance(v, (int, float, str, bool)) and len(str(k)) < 40}
    return {"state": state, "ollama_url": url.rstrip("/") or None, "models": models, "info": info}


def heartbeat(cur, node: str, hb: dict):
    cur.execute("SELECT state, dedicated, coalesce(role,'minion') FROM minion_nodes WHERE node=%s", (node,))
    prev = cur.fetchone()
    if prev and prev[2] == "viewer":
        raise ValueError("a viewer does not heartbeat")
    state = hb["state"]
    if prev and prev[1] and state == "idle":
        state = "dedicated"            # a dedicated node is always available; its agent may still say idle
    cur.execute("""UPDATE minion_nodes SET state=%s, ollama_url=coalesce(%s, ollama_url), models=%s::jsonb,
                   last_seen=now(), since=CASE WHEN state IS DISTINCT FROM %s OR since IS NULL THEN now() ELSE since END,
                   info = info || %s::jsonb WHERE node=%s""",
                (state, hb["ollama_url"], json.dumps(hb["models"]), state, json.dumps(hb["info"]), node))
    if prev and prev[0] != state:
        log(f"{node}: {prev[0]} -> {state}")
    return state


def transcribed(cur, node: str | None, win: str) -> tuple[int, float]:
    """(episodes, hours of audio) re-transcribed in the window — by one node, or the fleet when node is None."""
    cur.execute("SELECT count(*), coalesce(sum(audio_s),0) / 3600.0 FROM media_reingest_queue "
                "WHERE status = 'done' AND updated_at > now() - %s::interval AND (%s::text IS NULL OR node = %s)",
                (win, node, node))
    n, h = cur.fetchone()
    return int(n), round(float(h), 1)


def iso(t):
    return t.astimezone().isoformat(timespec="seconds") if t else None


def lesson_text(s: str) -> str:
    return _CITE.sub("", s or "").strip()


def status(cur, node: str) -> dict:
    out: dict = {"node": node, "updated": iso(datetime.now(timezone.utc))}
    cur.execute("SELECT state, since, last_seen > now() - %s * interval '1 second', info FROM minion_nodes WHERE node=%s",
                (FRESH_S, node))
    r = cur.fetchone()
    if r:
        out["state"] = r[0] if r[2] else "offline"
        out["since"] = iso(r[1])
        info = r[3] or {}
        if info.get("tok_s") is not None:
            out["tok_s"] = float(info["tok_s"])
        if info.get("model_last"):
            out["model"] = info["model_last"]
        if info.get("os_name") or info.get("os"):
            out["os"] = info.get("os_name") or info.get("os")
    cur.execute("SELECT title, kind, phase, started_at FROM minion_activity a WHERE node=%s AND started_at > now() - "
                "%s * interval '1 minute' AND " + LIVE + " ORDER BY started_at", (node, ACTIVITY_MAX_MIN))
    out["now_reading"] = [{"title": t, "kind": k, "phase": p or "reading", "started": iso(s)} for t, k, p, s in cur.fetchall()]
    win = f"{WINDOW_H} hours"
    cur.execute("""SELECT count(*), coalesce(sum(lessons_kept),0), coalesce(sum(cardinality(belief_ids)),0)
                   FROM work_lessons WHERE status='done' AND host=%s AND processed_at > now() - %s::interval""", (node, win))
    w, l, b = cur.fetchone()
    te, th = transcribed(cur, node, win)
    out["tonight"] = {"works": int(w), "lessons": int(l), "beliefs": int(b), "transcribed": te, "audio_h": th}
    cur.execute("""SELECT count(*), coalesce(sum(lessons_kept),0) FROM work_lessons
                   WHERE status='done' AND processed_at > now() - %s::interval""", (win,))
    fw, fl = cur.fetchone()
    cur.execute("SELECT count(*) FROM minion_nodes WHERE state IN ('idle','dedicated') AND last_seen > now() - "
                "%s * interval '1 second'", (FRESH_S,))
    nmin = int(cur.fetchone()[0])                  # read before the next query reuses the cursor
    fte, fth = transcribed(cur, None, win)
    out["fleet_tonight"] = {"works": int(fw), "lessons": int(fl), "minions": nmin,
                            "transcribed": fte, "audio_h": fth}
    cur.execute("""SELECT w.title, w.kind, l->>'lesson', w.host, w.processed_at FROM work_lessons w,
                   jsonb_array_elements(coalesce(w.lessons_json,'[]')) l
                   WHERE w.status='done' AND coalesce(w.credibility,'') <> 'fringe' AND w.processed_at > now() - interval '48 hours'
                   ORDER BY w.processed_at DESC LIMIT 120""")
    out["recent_lessons"] = [{"title": t, "kind": k, "lesson": lesson_text(x), "node": h, "at": iso(a), "mine": h == node}
                             for t, k, x, h, a in cur.fetchall() if x]
    cur.execute("SELECT tier, count(*) FROM work_lessons WHERE status='queued' GROUP BY 1")
    out["queue"] = {t: int(n) for t, n in cur.fetchall()}
    cur.execute("SELECT count(*) / 3.0 FROM work_lessons WHERE tier='A' AND status='done' AND processed_at > now() - interval '3 hours'")
    wph = float(cur.fetchone()[0] or 0)
    if wph > 0 and out["queue"].get("A"):
        out["eta_days"] = round(out["queue"]["A"] / wph / 24, 1)
    return out


def fleet(cur) -> dict:
    """Every minion (not viewers): state, what it is reading now, its last-12-hours works/lessons, tok/s; plus the
    fleet totals, recent lessons and queue from status()."""
    base = status(cur, "")
    out = {k: base[k] for k in ("updated", "fleet_tonight", "recent_lessons", "queue", "eta_days") if k in base}
    cur.execute("""SELECT m.node, CASE WHEN m.last_seen > now() - %s * interval '1 second' THEN m.state ELSE 'offline' END,
                          m.since, m.info, m.dedicated,
                          (SELECT count(*) FROM work_lessons w WHERE w.status='done' AND w.host=m.node AND w.processed_at > now() - %s::interval),
                          (SELECT coalesce(sum(lessons_kept),0) FROM work_lessons w WHERE w.status='done' AND w.host=m.node AND w.processed_at > now() - %s::interval)
                   FROM minion_nodes m WHERE coalesce(m.role,'minion') = 'minion' ORDER BY m.node""",
                (FRESH_S, f"{WINDOW_H} hours", f"{WINDOW_H} hours"))
    rows = cur.fetchall()
    cur.execute("SELECT node, title, kind, phase, started_at FROM minion_activity a WHERE started_at > now() - %s * interval '1 minute' "
                "AND " + LIVE + " ORDER BY started_at", (ACTIVITY_MAX_MIN,))
    act: dict = {}
    for n, t, k, p, st in cur.fetchall():
        act.setdefault(n, []).append({"title": t, "kind": k, "phase": p or "reading", "started": iso(st)})
    out["minions"] = [{"node": n, "state": stt, "since": iso(si), "dedicated": bool(d),
                       "os": (info or {}).get("os_name") or (info or {}).get("os"),
                       "tok_s": (info or {}).get("tok_s"), "now_reading": act.get(n, []),
                       "tonight": {"works": int(w), "lessons": int(l),
                                   **dict(zip(("transcribed", "audio_h"), transcribed(cur, n, f"{WINDOW_H} hours")))}}
                      for n, stt, si, info, d, w, l in rows]
    for r in out["recent_lessons"]:
        r.pop("mine", None)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "NovaMinionHub/1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self._send(200, {"ok": True})
        if u.path == "/fleet":
            node = (parse_qs(u.query).get("node") or [""])[0]
            try:
                cur = db()
                if not authed(cur, node, self.headers.get("X-Minion-Token")):
                    return self._send(403, {"error": "unknown node or bad token"})
                return self._send(200, fleet(cur))
            except Exception as e:  # noqa: BLE001
                _LOCAL.conn = None
                log(f"fleet failed: {e}")
                return self._send(503, {"error": "fleet unavailable"})
        if u.path == "/status":
            node = (parse_qs(u.query).get("node") or [""])[0]
            try:
                cur = db()
                if not authed(cur, node, self.headers.get("X-Minion-Token")):
                    return self._send(403, {"error": "unknown node or bad token"})
                return self._send(200, status(cur, node))
            except Exception as e:  # noqa: BLE001
                _LOCAL.conn = None
                log(f"status failed: {e}")
                return self._send(503, {"error": "status unavailable"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/heartbeat":
            return self._send(404, {"error": "not found"})
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 65536)
            d = json.loads(self.rfile.read(n) or b"{}")
            node = str(d.get("node") or "")
            cur = db()
            if not authed(cur, node, self.headers.get("X-Minion-Token")):
                return self._send(403, {"error": "unknown node or bad token"})
            state = heartbeat(cur, node, clean_hb(d))
            return self._send(200, {"ok": True, "state": state})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            _LOCAL.conn = None
            log(f"heartbeat failed: {e}")
            return self._send(503, {"error": "heartbeat unavailable"})


def enroll(node: str, url: str | None, dedicated: bool, viewer: bool = False) -> str:
    if not _NODE.match(node):
        raise SystemExit("node: letters, digits, . _ - only")
    tok = secrets.token_urlsafe(32)
    cur = db()
    cur.execute("ALTER TABLE minion_nodes ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'minion'")
    cur.execute("""INSERT INTO minion_nodes (node, ollama_url, dedicated, token_hash, state, role) VALUES (%s,%s,%s,%s,'offline',%s)
                   ON CONFLICT (node) DO UPDATE SET token_hash=EXCLUDED.token_hash, dedicated=EXCLUDED.dedicated,
                   role=EXCLUDED.role, ollama_url=coalesce(EXCLUDED.ollama_url, minion_nodes.ollama_url)""",
                (node, (url or "").rstrip("/") or None, dedicated and not viewer, token_hash(tok),
                 "viewer" if viewer else "minion"))
    return tok


def selftest() -> int:
    assert clean_hb({"state": "idle", "ollama_url": "http://10.0.0.5:11434/", "models": ["qwen3:30b-a3b", 5],
                     "info": {"ac": True, "x": {"no": 1}}}) == {
        "state": "idle", "ollama_url": "http://10.0.0.5:11434", "models": ["qwen3:30b-a3b"], "info": {"ac": True}}
    for bad in ({"state": "busy"}, {"state": "idle", "ollama_url": "file:///etc/passwd"},
                {"state": "idle", "ollama_url": "http://a b"}):
        try:
            clean_hb(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert lesson_text("When X did Y, Z [3][12].") == "When X did Y, Z."
    assert token_hash("a") == hashlib.sha256(b"a").hexdigest()
    assert _NODE.match("nova-core10") and not _NODE.match("../etc") and not _NODE.match("")
    print("selftest OK")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--enroll")
    ap.add_argument("--url")
    ap.add_argument("--dedicated", action="store_true")
    ap.add_argument("--viewer", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.enroll:
        print(enroll(a.enroll, a.url, a.dedicated, a.viewer))
        return 0
    if a.list:
        cur = db()
        cur.execute("SELECT node, state, dedicated, ollama_url, last_seen, info FROM minion_nodes ORDER BY node")
        for r in cur.fetchall():
            print(*r, sep=" | ")
        return 0
    if a.serve:
        log(f"serving on :{a.port}")
        ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
