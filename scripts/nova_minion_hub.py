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
  POST /llm                 X-Minion-Token; a node's LLM snapshot (scripts/cluster/nova_llm_probe.py, every 3 s)
  GET  /cluster?node=N      X-Minion-Token (viewer); the whole cluster's LLM activity, for the Nova Cluster app.
                            Every node carries llm_job {state running|idle|none|silent, text, jobs[], model?, last?}
                            and purpose / alias / display_name / ip (minion_nodes columns, seeded from agent_docs).
  GET  /health
1.1 (2026-10-19, Nova Cluster 1.1.0): /cluster also carries, from read-only caches a background thread refreshes
(so a request never waits on the network): per node `net` (wired link speed + Wi-Fi backdoor), `temps`, and
`recent_jobs` (last 10: LLM jobs from memory, works read and episodes transcribed from PG); top-level `ups` (every
UPS in service_config service='ups', read from NUT's upsd over TCP 3493) and `alerts` (failing / dead-letter /
quarantined tasks on both schedulers — the Studio's and the scheduler-core leader's /tasks — and leader changes from
leader_events in the last 7 days), and `published` (the newest posts on nova.digitalnoise.net, from the journal repo's
git history: title, section, emoji, link, time).
CLI  --serve [--port 37590] | --enroll NODE [--url http://host:11434] [--dedicated | --viewer | --probe] | --list | --selftest
A VIEWER (--viewer) is a display, not a minion: it never heartbeats and is never sent work; its token reads /fleet.

Tokens: --enroll prints a token once; only its sha256 is stored (minion_nodes.token_hash). The minion keeps it in
its login Keychain (service 'nova-minion'). Reads are LAN-only and token-gated: lesson text is Nova's own.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import socket
import subprocess
import urllib.request
import json
import os
import re
import secrets
import sys
import threading
import time
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
# ... except a transcription: a CPU minion takes 20-40 min on one episode, and its queue row being 'running' already
# proves it is live (a dead worker's row reverts after 2 h). 2026-10-18: Linux minions showed "waiting for work"
# while transcribing because their activity aged past 20 min.
RECENT = "(a.started_at > now() - %s * interval '1 minute' OR a.work_key LIKE 'media:%%')"
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
    cur.execute("SELECT title, kind, phase, started_at FROM minion_activity a WHERE node=%s AND " + RECENT + " AND "
                + LIVE + " ORDER BY started_at", (node, ACTIVITY_MAX_MIN))
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
    cur.execute("SELECT node, title, kind, phase, started_at FROM minion_activity a WHERE " + RECENT + " "
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


# ── cluster LLM view (2026-10-16): probes push snapshots; the hub keeps the latest per node in memory, turns slot
# changes into tok/s and "request finished" events. Nothing here touches prompts or outputs.
LLM_FRESH_S = 15
_LLM: dict = {}
_EVENTS: list = []
_LLM_LOCK = threading.Lock()
# what job each node's LLM is running (2026-10-17, Jordan: "always add in what job the LLM is running and then, on a
# different line, the node's core purpose"). Probes (1.1+) report the clients with a request in flight on their LLM
# ports and their own processes' connections to LLM ports elsewhere; joining the two names the job that sent each
# request. A job stays "running" JOB_LINGER_S after it was last seen, so 1 s gaps between samples don't flicker.
JOB_LINGER_S = 6.0
LAST_JOB_MAX_S = 24 * 3600
READER_PROCS = ("nova_work_lessons", "nova_media_reingest", "nova_minion_whisper")   # already named by minion_activity
_JOBS: dict = {}               # node -> {job label: last seen epoch}
_LAST_JOB: dict = {}           # node -> (text, epoch) — the last job seen, for "Idle · last: …"
_RECENT_LLM: dict = {}         # node -> deque[(epoch, text)] — the last 10 distinct LLM jobs (machine drill-down)
_ADDR = re.compile(r"^[0-9A-Fa-f:.]{2,45}:\d{1,5}$")
_LABEL = re.compile(r"[^A-Za-z0-9._ -]")


def clean_users(d: dict) -> dict:
    """Pure: the probe's clients / calls / ips, validated (a probe is a LAN peer, not trusted to send markup)."""
    def lab(x):
        x = _LABEL.sub("", str(x or ""))[:40].strip()
        return x or None

    def addr(x):
        x = str(x or "")
        return x if _ADDR.match(x) else None
    clients = []
    for c in (d.get("clients") or [])[:32]:
        if isinstance(c, dict) and addr(c.get("peer")):
            clients.append({"peer": addr(c.get("peer")), "port": int(c.get("port") or 0) if str(c.get("port") or 0).isdigit() else 0,
                            "proc": lab(c.get("proc"))})
    calls = [{"src": addr(c.get("src")), "proc": lab(c.get("proc"))} for c in (d.get("calls") or [])[:64]
             if isinstance(c, dict) and addr(c.get("src")) and lab(c.get("proc"))]
    ips = [i for i in (d.get("ips") or [])[:8] if isinstance(i, str) and re.match(r"^[0-9A-Fa-f:.]{2,45}$", i)]
    return {"clients": clients, "calls": calls, "ips": ips}


def attribute(node: str, clients: list, snaps: dict, ip_nodes: dict) -> list[str]:
    """Pure: job labels for a node's in-flight clients. A local client already carries its process name; a remote one
    is matched to the caller node's own report of that connection (same source ip:port); otherwise the caller machine
    is named — never a guessed job."""
    calls = {c["src"]: (c["proc"], n) for n, sn in snaps.items() for c in (sn.get("calls") or [])}
    out = []
    for c in clients:
        host = c["peer"].rsplit(":", 1)[0]
        if c.get("proc"):
            lab = c["proc"]
        elif c["peer"] in calls:
            proc, cn = calls[c["peer"]]
            lab = proc if cn == node else f"{proc} @ {cn}"
        else:
            cn = ip_nodes.get(host)
            lab = f"client on {cn or host}" if cn != node else "local client"
        if lab not in out:
            out.append(lab)
    return out


def _ago(s: float) -> str:
    s = max(0.0, s)
    return f"{int(s)}s" if s < 90 else f"{int(s / 60)}m" if s < 5400 else f"{int(s / 3600)}h" if s < 172800 else f"{int(s / 86400)}d"


def busy_phase(runners: list) -> tuple[str | None, str | None]:
    """Pure: (model, 'generating 240 tok' | 'prefill 46%') of the busiest slot, '×N' when several are busy."""
    busy = [(r.get("model"), s) for r in runners or [] for s in r.get("slots") or [] if s.get("busy")]
    if not busy:
        return None, None
    model, s = max(busy, key=lambda b: (b[1].get("decoded") or 0, b[1].get("prompt_done") or 0))
    if s.get("decoded"):
        ph = f"generating {int(s['decoded']):,} tok"
    else:
        p = s.get("prompt") or 0
        ph = f"prefill {int(100 * (s.get('prompt_done') or 0) / p)}%" if p else "starting"
    return model, ph + (f" ×{len(busy)}" if len(busy) > 1 else "")


def job_view(live: bool, age_s: float, llm: bool | None, n_loaded: int, minion_jobs: list, jobs: list,
             runners: list, last: tuple | None, now: float) -> dict:
    """Pure: the node's job line — ALWAYS one: running (named job, or 'caller not identified'), idle (with the last
    job seen), no LLM, or silent. Never invents a job: names come from minion_activity or the connection join."""
    if not live:
        return {"state": "silent", "text": f"Silent · probe last heard {_ago(age_s)} ago", "jobs": []}
    others = [j for j in jobs if not j.split(" @ ")[0].startswith(READER_PROCS)] if minion_jobs else jobs
    jobs = minion_jobs + others
    model, phase = busy_phase(runners)
    if jobs and model:
        return {"state": "running", "text": f"{' + '.join(jobs)} → {model} · {phase}", "jobs": jobs, "model": model}
    if jobs:      # minion work (reading, whisper) is named as is; a bare client is a request not yet on a slot
        tail = " · request in flight" if others else ""
        return {"state": "running", "text": f"{' + '.join(jobs)}{tail}", "jobs": jobs}
    if model:
        return {"state": "running", "text": f"{model} · {phase} · caller not identified", "jobs": [], "model": model}
    if llm is False and not n_loaded:
        return {"state": "none", "text": "No LLM on this node", "jobs": []}
    out = {"state": "idle", "jobs": []}
    if last and now - last[1] < LAST_JOB_MAX_S:
        out["text"] = f"Idle · last: {last[0]} · {_ago(now - last[1])} ago"
        out["last"] = {"text": last[0], "at": datetime.fromtimestamp(last[1], timezone.utc).astimezone().isoformat(timespec="seconds")}
    else:
        out["text"] = "Idle · " + (f"{n_loaded} model{'s' if n_loaded != 1 else ''} loaded" if n_loaded else "no model loaded")
    return out


def note_jobs(node: str, labels: list, model: str | None, now: float):
    """Remember what ran (under _LLM_LOCK): per-label last-seen for the linger, and the node's last job text."""
    seen = _JOBS.setdefault(node, {})
    for lab in labels:
        seen[lab] = now
    for lab in [k for k, t in seen.items() if now - t > JOB_LINGER_S]:
        seen.pop(lab, None)
    if labels:
        text = " + ".join(labels) + (f" → {model}" if model else "")
        _LAST_JOB[node] = (text, now)
        dq = _RECENT_LLM.setdefault(node, collections.deque(maxlen=10))
        if not dq or dq[-1][1] != text or now - dq[-1][0] > 120:   # a new job, or the same one again later
            dq.append((now, text))


def slot_events(node: str, prev: dict | None, cur: dict, dt: float) -> tuple[list, float, float]:
    """Pure: (finished-request events, decode tok/s, prefill tok/s) between two snapshots of one node."""
    events, dec, pre = [], 0.0, 0.0
    pmap = {r["port"]: r for r in (prev or {}).get("runners", [])}
    for r in cur.get("runners", []):
        pr = pmap.get(r["port"])
        if not pr or pr.get("model") != r.get("model"):
            continue
        for i, s in enumerate(r.get("slots", [])):
            if i >= len(pr.get("slots", [])):
                continue
            p = pr["slots"][i]
            if p.get("task") is not None and s.get("task") != p.get("task") and p.get("busy") is not None:
                if p.get("busy") and (p.get("decoded") or p.get("prompt")):
                    events.append({"node": node, "model": r.get("model"), "prompt": p.get("prompt") or 0,
                                   "decoded": p.get("decoded") or 0})
                # NOT counted: task-id jumps. llama-server's task counter also ticks for every /slots query (ours,
                # once a second) and other slots' work, so a jump is not a request count. A request that starts and
                # ends between two 1 s samples is missed — mostly sub-second embeddings.
            elif p.get("busy") and not s.get("busy") and s.get("task") == p.get("task"):
                # the runner zeroes its decoded counter when a request ends: keep the highest value seen
                events.append({"node": node, "model": r.get("model"),
                               "prompt": max(s.get("prompt") or 0, p.get("prompt") or 0),
                               "decoded": max(s.get("decoded") or 0, p.get("decoded") or 0)})
            elif s.get("busy") and s.get("task") == p.get("task") and dt > 0:
                dec += max(0, (s.get("decoded") or 0) - (p.get("decoded") or 0)) / dt
                pre += max(0, (s.get("prompt_done") or 0) - (p.get("prompt_done") or 0)) / dt
    return events, round(dec, 1), round(pre, 1)


def count_tokens(cur, node: str, events: list):
    """Cumulative counters (llm_tokens_daily): finished requests + their prompt/decoded tokens, per day and node.
    A request's decoded count is its last value seen by the probe (<= 3 s before it finished)."""
    if not events:
        return
    cur.execute("INSERT INTO llm_tokens_daily (day, node, requests, prompt_tokens, decoded_tokens) "
                "VALUES (current_date, %s, %s, %s, %s) ON CONFLICT (day, node) DO UPDATE SET "
                "requests = llm_tokens_daily.requests + EXCLUDED.requests, "
                "prompt_tokens = llm_tokens_daily.prompt_tokens + EXCLUDED.prompt_tokens, "
                "decoded_tokens = llm_tokens_daily.decoded_tokens + EXCLUDED.decoded_tokens",
                (node, sum(int(e.get("unseen") or 1) for e in events), sum(int(e.get("prompt") or 0) for e in events),
                 sum(int(e.get("decoded") or 0) for e in events)))


_IPS: dict = {"t": 0.0, "map": {}}


def db_ips(cur) -> dict:
    """{ip: node} from minion_nodes.ip, refreshed every minute (the caller-machine fallback)."""
    if cur is not None and time.time() - _IPS["t"] > 60:
        try:
            cur.execute("SELECT ip, node FROM minion_nodes WHERE ip IS NOT NULL")
            _IPS["map"] = dict(cur.fetchall())
        except Exception as e:  # noqa: BLE001
            log(f"node ips not read ({e})")
        _IPS["t"] = time.time()
    return _IPS["map"]


def llm_push(node: str, snap: dict, cur=None, addr: str | None = None):
    now = time.time()
    snap = {**snap, **clean_users(snap)}
    known = db_ips(cur)
    with _LLM_LOCK:
        old = _LLM.get(node)
        dt = now - old["t"] if old else 0.0
        ev, dec, pre = slot_events(node, old["snap"] if old and dt < 30 else None, snap, dt)
        stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        for e in ev:
            e["at"] = stamp
            _EVENTS.append({**e, "t": now})
        del _EVENTS[:-400]
        _LLM[node] = {"snap": snap, "t": now, "tok_s": dec, "prefill_tok_s": pre, "addr": addr}
        snaps = {n: d["snap"] for n, d in _LLM.items() if now - d["t"] < LLM_FRESH_S}
        ip_nodes = {**known, **{ip: n for n, d in _LLM.items() for ip in [d.get("addr")] + list(d["snap"].get("ips") or []) if ip}}
        labels = attribute(node, snap.get("clients") or [], snaps, ip_nodes)
        model, _ = busy_phase(snap.get("runners", []))
        note_jobs(node, labels, model, now)
    if cur is not None:
        try:
            count_tokens(cur, node, ev)
        except Exception as e:  # noqa: BLE001 — the live view never fails for the counter
            log(f"token counter not updated ({e})")


# ── Nova Cluster 1.1 extras: read-only collectors, refreshed in the background ───────────────────
_EXTRAS: dict = {"ups": [], "recent_jobs": {}, "alerts": {"items": [], "counts": {}}, "published": []}
_EXTRAS_LOCK = threading.Lock()
_VAR = re.compile(r'^VAR (\S+) (\S+) "(.*)"$')
UPS_VARS = ("ups.status", "ups.load", "ups.realpower.nominal", "battery.charge", "battery.runtime", "input.voltage",
            "ups.model", "battery.charge.low", "battery.runtime.low")


def parse_nut(text: str, ups: str) -> dict:
    """NUT 'LIST VAR <ups>' reply -> {var: value} for the vars Nova shows (values are data, length-capped)."""
    out = {}
    for line in text.splitlines():
        m = _VAR.match(line.strip())
        if m and m.group(1) == ups and m.group(2) in UPS_VARS:
            out[m.group(2)] = m.group(3)[:64]
    return out


def nut_vars(host: str, port: int, ups: str, timeout: float = 4.0) -> dict:
    """Read one UPS from upsd (unauthenticated, read-only LIST VAR)."""
    if not _NODE.match(ups):
        raise ValueError("bad ups name")
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(f"LIST VAR {ups}\n".encode())
        buf = b""
        while b"END LIST VAR" not in buf and b"ERR" not in buf[:16] and len(buf) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        try:
            s.sendall(b"LOGOUT\n")
        except OSError:
            pass
    text = buf.decode("utf-8", "replace")
    if text.startswith("ERR"):
        raise ValueError(text.split("\n")[0][:80])
    return parse_nut(text, ups)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ups_view(key: str, conf: dict, raw: dict | None, err: str | None) -> dict:
    """service_config row + NUT vars -> what the app draws. Status flags: OL online, OB on battery, LB low battery."""
    st = (raw or {}).get("ups.status", "")
    flags = set(st.split())
    load = _num((raw or {}).get("ups.load"))
    nominal = _num((raw or {}).get("ups.realpower.nominal"))
    return {"key": key, "label": conf.get("label") or key, "model": ((raw or {}).get("ups.model") or conf.get("model") or "").strip() or None,
            "monitor_node": conf.get("monitor_node"), "powers": [p for p in conf.get("powers", []) if isinstance(p, str)][:16],
            "ok": raw is not None and err is None, "error": err, "status": st or None,
            "on_battery": "OB" in flags, "low_battery": "LB" in flags, "online": "OL" in flags,
            "load_pct": load, "watts": round(load * nominal / 100) if load is not None and nominal else None,
            "charge_pct": _num((raw or {}).get("battery.charge")), "runtime_s": _num((raw or {}).get("battery.runtime")),
            "input_v": _num((raw or {}).get("input.voltage")), "at": iso(datetime.now(timezone.utc))}


def collect_ups(cur) -> list:
    cur.execute("SELECT key, value FROM service_config WHERE service = 'ups' ORDER BY key")
    out = []
    for key, conf in cur.fetchall():
        conf = conf if isinstance(conf, dict) else {}
        try:
            raw, err = nut_vars(str(conf.get("host") or ""), int(conf.get("port") or 3493), str(conf.get("ups") or "")), None
        except Exception as e:  # noqa: BLE001 — a dark UPS is shown as unreachable, never breaks the view
            raw, err = None, f"{e.__class__.__name__}: {str(e)[:60]}"
        out.append(ups_view(key, conf, raw, err))
    return out


def collect_recent_jobs(cur) -> dict:
    """Last 10 minion jobs per node from PG: works read for lessons and TV episodes transcribed."""
    cur.execute("""
        SELECT node, at, kind, text FROM (
          SELECT node, at, kind, text, row_number() OVER (PARTITION BY node ORDER BY at DESC) rn FROM (
            SELECT host AS node, processed_at AS at, 'reading' AS kind, title AS text
              FROM work_lessons WHERE status = 'done' AND host IS NOT NULL AND processed_at > now() - interval '7 days'
            UNION ALL
            SELECT node, updated_at, 'transcribing', coalesce(title, show)
              FROM media_reingest_queue WHERE status = 'done' AND node IS NOT NULL AND updated_at > now() - interval '7 days'
          ) u
        ) r WHERE rn <= 10""")
    jobs: dict = {}
    for node, at, kind, text in cur.fetchall():
        jobs.setdefault(node, []).append({"at": iso(at), "t": at.timestamp(), "kind": kind, "text": (text or "")[:160]})
    return jobs


def recent_jobs_for(node: str, db_jobs: list, llm: list, limit: int = 10) -> list:
    """Merge PG minion jobs with the hub's LLM job log, newest first."""
    rows = [dict(j) for j in db_jobs] + [{"t": t, "at": iso(datetime.fromtimestamp(t, timezone.utc)), "kind": "llm", "text": x}
                                         for t, x in llm]
    rows.sort(key=lambda j: -j["t"])
    return [{k: v for k, v in j.items() if k != "t"} for j in rows[:limit]]


# alerts: failing / dead-letter tasks on both schedulers + scheduler-core leader changes
STUDIO_SCHED = "http://localhost:37460/tasks"           # the hub runs on the Studio
CORE_SCHED = "http://{node}.digitalnoise.net:37464/tasks"  # scheduler-core leader, by DNS name
PREFERRED_CORE_LEADER = "nova-core"
FAILING_AT, DEAD_AT = 3, 10


def classify_tasks(source: str, tasks: dict, reasons: dict | None = None) -> list:
    """/tasks JSON -> alert items. Disabled tasks never alert. Pure."""
    out = []
    for tid, t in (tasks or {}).items():
        if not isinstance(t, dict) or t.get("enabled") is False:
            continue
        n = int(t.get("consecutive_failures") or 0)
        quarantined = bool(t.get("quarantined"))
        if quarantined or n >= DEAD_AT:
            level, kind = "bad", "dead-letter" if not quarantined else "quarantined"
            text = f"{tid}: {kind} after {n} failures in a row"
        elif n >= FAILING_AT:
            level, kind, text = "warn", "failing", f"{tid}: failed {n}× in a row"
        else:
            continue
        reason = (reasons or {}).get(tid)
        out.append({"level": level, "kind": kind, "source": source, "task": tid[:80], "text": text[:160],
                    "reason": re.sub(r"\s+", " ", reason)[:140] if reason else None, "failures": n})
    return out


def _get_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _last_errors(cur, task_ids: list) -> dict:
    if not task_ids:
        return {}
    cur.execute("SELECT DISTINCT ON (task_id) task_id, coalesce(nullif(error_tail, ''), stdout_tail) FROM scheduler_runs "
                "WHERE task_id = ANY(%s) AND status <> 'success' ORDER BY task_id, started_at DESC", (task_ids,))
    return {t: (e or "")[-300:] for t, e in cur.fetchall()}


def _failing_from_runs(cur) -> dict:
    """Fallback when a scheduler's /tasks can't be reached: trailing failure streaks from scheduler_runs (2 days)."""
    cur.execute("""SELECT task_id, count(*) FROM (
                     SELECT task_id, status, row_number() OVER (PARTITION BY task_id ORDER BY started_at DESC) rn,
                            count(*) FILTER (WHERE status = 'success') OVER (PARTITION BY task_id ORDER BY started_at DESC
                              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) ok_seen
                     FROM scheduler_runs WHERE started_at > (extract(epoch FROM now()) - 2 * 86400) * 1000) r
                   WHERE ok_seen = 0 AND status NOT IN ('running', 'success') GROUP BY task_id""")
    return {t: {"enabled": True, "consecutive_failures": int(n)} for t, n in cur.fetchall()}


def leader_items(leader: str | None, events: list, now: datetime) -> list:
    """Leader-change alerts. Pure: events = [(node, ts, note)] newest first."""
    out = []
    if leader and leader != PREFERRED_CORE_LEADER:
        since = next((ts for n, ts, _ in events if n == leader), None)
        out.append({"level": "warn", "kind": "failover", "source": "scheduler-core", "task": None,
                    "text": f"scheduler-core is running on the standby ({leader})"
                            + (f" since {since.astimezone().strftime('%a %H:%M')}" if since else ""), "reason": None})
    for n, ts, note in events[:4]:
        out.append({"level": "info", "kind": "leader", "source": "scheduler-core", "task": None,
                    "text": f"leader → {n} · {ts.astimezone().strftime('%a %H:%M')}",
                    "reason": (note or "")[:140] or None, "at": iso(ts)})
    return out


def collect_alerts(cur) -> dict:
    items: list = []
    cur.execute("SELECT node FROM leader_standby WHERE name = 'nova-scheduler-core' AND role = 'leader' "
                "AND ts > now() - interval '5 minutes' ORDER BY ts DESC LIMIT 1")
    row = cur.fetchone()
    leader = row[0] if row else None
    sources = [("studio", STUDIO_SCHED)]
    if leader and _NODE.match(leader):
        sources.append(("scheduler-core", CORE_SCHED.format(node=leader)))
    else:
        items.append({"level": "bad", "kind": "no-leader", "source": "scheduler-core", "task": None,
                      "text": "scheduler-core has no live leader", "reason": None})
    for name, url in sources:
        try:
            tasks = _get_json(url)
        except Exception as e:  # noqa: BLE001
            items.append({"level": "bad", "kind": "unreachable", "source": name, "task": None,
                          "text": f"{name} scheduler not answering", "reason": f"{e.__class__.__name__}"})
            tasks = _failing_from_runs(cur) if name == "scheduler-core" else {}
        found = classify_tasks(name, tasks)
        reasons = _last_errors(cur, [a["task"] for a in found])
        items += classify_tasks(name, tasks, reasons)
    cur.execute("SELECT node, ts, note FROM leader_events WHERE name = 'nova-scheduler-core' "
                "AND ts > now() - interval '7 days' ORDER BY ts DESC LIMIT 8")
    items += leader_items(leader, cur.fetchall(), datetime.now(timezone.utc))
    rank = {"bad": 0, "warn": 1, "info": 2}
    items.sort(key=lambda a: (rank.get(a["level"], 3), -(a.get("failures") or 0)))
    counts = {k: sum(1 for a in items if a["level"] == k) for k in ("bad", "warn", "info")}
    return {"items": items[:24], "counts": counts, "leader": leader, "at": iso(datetime.now(timezone.utc))}


# just published: newest posts on nova.digitalnoise.net (from the journal repo the Studio publishes from)
JOURNAL = os.path.expanduser("~/nova-journal")
SITE = "https://nova.digitalnoise.net"
SECTION_EMOJI = {"burbank-ops": "🛫", "nova": "✨", "watches": "⌚", "cars": "🚗", "tech": "💻", "food": "🍳",
                 "music": "🎵", "military": "🎖", "screen": "🎬", "science": "🔬", "mystery": "🕯", "crime": "🕵",
                 "workshop": "🔨", "travel": "🧭", "history": "🏛", "belief": "🔍"}
_POST = re.compile(r"^content/([a-z0-9-]+)/([A-Za-z0-9._-]+)\.md$")
_LEAD_EMOJI = re.compile(r"^([^\w\s\"'(\[]{1,3})\s+(.+)$")


def parse_added(log_text: str) -> list:
    """`git log --diff-filter=A --name-only --format=@@%ct` -> [(epoch, section, slug)], newest first, posts only."""
    out, t = [], None
    for line in log_text.splitlines():
        if line.startswith("@@"):
            t = int(line[2:]) if line[2:].isdigit() else None
        elif t and (m := _POST.match(line.strip())) and m.group(2) != "_index":
            out.append((t, m.group(1), m.group(2)))
    return out


def front_title(text: str) -> str | None:
    m = re.search(r'^title:\s*"((?:[^"\\]|\\.)*)"\s*$', text[:3000], re.M) or re.search(r"^title:\s*'?([^'\n]+)'?\s*$", text[:3000], re.M)
    return m.group(1).replace('\\"', '"').strip() if m else None


def post_view(t: int, section: str, slug: str, title: str | None, names: dict) -> dict:
    title = (title or slug.replace("-", " ")).strip()
    emoji = SECTION_EMOJI.get(section, "•")
    m = _LEAD_EMOJI.match(title)
    if m:
        emoji, title = m.group(1), m.group(2)
    return {"title": title[:160], "section": section, "section_name": names.get(section, section), "emoji": emoji,
            "url": f"{SITE}/{section}/{slug}/", "at": iso(datetime.fromtimestamp(t, timezone.utc))}


def menu_names(yaml_text: str) -> dict:
    """identifier -> name from hugo.yaml's main menu (regex; the hub has no YAML dependency)."""
    return {m.group(1): m.group(2).strip() for m in
            re.finditer(r"- identifier:\s*([a-z0-9-]+)\s*\n\s*name:\s*([^\n]+)", yaml_text)}


def collect_published(_cur=None, limit: int = 12) -> list:
    log_text = subprocess.run(["git", "-C", JOURNAL, "log", "--since=4.days", "--diff-filter=A", "--name-only",
                               "--format=@@%ct", "--", "content"], capture_output=True, text=True, timeout=20).stdout
    try:
        names = menu_names(open(os.path.join(JOURNAL, "hugo.yaml")).read())
    except OSError:
        names = {}
    out, seen = [], set()
    for t, section, slug in parse_added(log_text):
        if (section, slug) in seen:
            continue
        seen.add((section, slug))
        path = os.path.join(JOURNAL, "content", section, slug + ".md")
        try:
            title = front_title(open(path, encoding="utf-8", errors="replace").read(3000))
        except OSError:
            continue                    # moved or retired since: not on the site any more
        out.append(post_view(t, section, slug, title, names))
        if len(out) >= limit:
            break
    return out


COLLECTORS = [("ups", collect_ups, 10), ("recent_jobs", collect_recent_jobs, 30), ("alerts", collect_alerts, 30),
              ("published", collect_published, 60)]


def refresh_extras(force: bool = False):
    """One pass: run every collector that is due (each failure is logged and keeps the last value)."""
    now = time.time()
    for name, fn, every in COLLECTORS:
        last = _EXTRAS.get(f"_t_{name}", 0)
        if not force and now - last < every:
            continue
        try:
            v = fn(db())
            with _EXTRAS_LOCK:
                _EXTRAS[name] = v
        except Exception as e:  # noqa: BLE001
            _LOCAL.conn = None
            log(f"extras {name} failed: {e}")
        _EXTRAS[f"_t_{name}"] = now


def extras_loop():
    while True:
        refresh_extras()
        time.sleep(2)


def cluster(cur) -> dict:
    now = time.time()
    cur.execute("SELECT node, role, state, last_seen > now() - %s * interval '1 second', purpose, alias, ip "
                "FROM minion_nodes", (FRESH_S,))
    roles = {n: {"role": r, "minion_state": s if f else "offline", "purpose": pu, "alias": al, "ip": ip,
                 "display_name": f"{n} ({al})" if al else n}
             for n, r, s, f, pu, al, ip in cur.fetchall()}
    jobs_now, last_job = {}, {}
    nodes, tot = [], {"nodes": 0, "models_loaded": 0, "vram_gb": 0.0, "busy_slots": 0, "tok_s": 0.0,
                      "prefill_tok_s": 0.0, "req_min": 0}
    with _LLM_LOCK:
        recent = [e for e in _EVENTS if now - e["t"] < 60]
        recent_n = sum(int(e.get("unseen") or 1) for e in recent)
        for node, d in sorted(_LLM.items()):
            sn, live = d["snap"], now - d["t"] < LLM_FRESH_S
            runners = sn.get("runners", [])
            busy = sum(1 for r in runners for s in r.get("slots", []) if s.get("busy"))
            vram = round(sum(m.get("vram_gb") or 0 for m in sn.get("loaded", [])), 1)
            req = sum(int(e.get("unseen") or 1) for e in recent if e["node"] == node)
            nodes.append({"node": node, "live": live, "age_s": round(now - d["t"], 1), "os": sn.get("os"),
                          "chip": sn.get("chip"), "cores": sn.get("cores"), "mem_gb": sn.get("mem_gb"),
                          "free_gb": sn.get("free_gb"), "load": sn.get("load"), "loaded": sn.get("loaded", []),
                          "runners": runners, "busy": busy if live else 0, "tok_s": d["tok_s"] if live else 0,
                          "prefill_tok_s": d["prefill_tok_s"] if live else 0, "req_min": req, "vram_gb": vram,
                          "llm": sn.get("llm"), "net": sn.get("net"), "temps": sn.get("temps"),
                          **roles.get(node, {"role": None, "minion_state": None, "display_name": node})})
            jobs_now[node] = [k for k, t in sorted(_JOBS.get(node, {}).items(), key=lambda kv: -kv[1])
                              if now - t <= JOB_LINGER_S]
            last_job[node] = _LAST_JOB.get(node)
            if live:
                tot["nodes"] += 1
                tot["models_loaded"] += len(sn.get("loaded", []))
                tot["vram_gb"] = round(tot["vram_gb"] + vram, 1)
                tot["busy_slots"] += busy
                tot["tok_s"] = round(tot["tok_s"] + d["tok_s"], 1)
                tot["prefill_tok_s"] = round(tot["prefill_tok_s"] + d["prefill_tok_s"], 1)
        tot["req_min"] = recent_n
        tot["tok_s_total"] = round(tot["tok_s"] + tot["prefill_tok_s"], 1)   # every token the cluster handles, live
        events = [{k: v for k, v in e.items() if k != "t"} for e in reversed(_EVENTS[-100:]) if not e.get("unseen")]
    # the whole cluster (Jordan 2026-10-16): fold in what each machine is doing FOR Nova as a minion — reading
    # for lessons, transcribing — plus the fleet's lessons, so one view shows traffic and the work it serves
    # cumulative: today and since the probes started (llm_tokens_daily)
    cur.execute("SELECT node, sum(requests) FILTER (WHERE day = current_date), sum(prompt_tokens) FILTER (WHERE day = current_date), "
                "sum(decoded_tokens) FILTER (WHERE day = current_date), sum(requests), sum(prompt_tokens), sum(decoded_tokens) "
                "FROM llm_tokens_daily GROUP BY node")
    cum = {r[0]: [int(x or 0) for x in r[1:]] for r in cur.fetchall()}
    t = [sum(v[i] for v in cum.values()) for i in range(6)]
    tot["today"] = {"requests": t[0], "prompt_tokens": t[1], "decoded_tokens": t[2], "tokens": t[1] + t[2]}
    tot["all_time"] = {"requests": t[3], "prompt_tokens": t[4], "decoded_tokens": t[5], "tokens": t[4] + t[5]}
    for n in nodes:
        v = cum.get(n["node"], [0] * 6)
        n["today"] = {"requests": v[0], "prompt_tokens": v[1], "decoded_tokens": v[2], "tokens": v[1] + v[2]}
        n["tok_s_total"] = round((n.get("tok_s") or 0) + (n.get("prefill_tok_s") or 0), 1)
    fl = fleet(cur)
    by_node = {m["node"]: m for m in fl.get("minions", [])}
    for n in nodes:
        m = by_node.pop(n["node"], None)
        n["minion"] = ({"state": m["state"], "dedicated": m["dedicated"], "now_reading": m["now_reading"],
                        "tonight": m["tonight"], "tok_s": m.get("tok_s")} if m else None)
    for m in by_node.values():          # a minion with no probe still belongs on the map
        nodes.append({"node": m["node"], "live": False, "os": m.get("os"), "loaded": [], "runners": [], "busy": 0,
                      "tok_s": 0, "prefill_tok_s": 0, "req_min": 0, "vram_gb": 0,
                      **roles.get(m["node"], {"display_name": m["node"]}), "role": "minion",
                      "minion_state": m["state"], "minion": {"state": m["state"], "dedicated": m["dedicated"],
                      "now_reading": m["now_reading"], "tonight": m["tonight"], "tok_s": m.get("tok_s")}})
    for n in nodes:
        reading = [f"{'transcribing (whisper)' if r.get('phase') == 'transcribing' else (r.get('phase') or 'reading')}: "
                   f"{r.get('title') or '…'}"
                   for r in ((n.get("minion") or {}).get("now_reading") or [])]
        n["llm_job"] = job_view(bool(n.get("live")), n.get("age_s") or 0, n.get("llm"), len(n.get("loaded") or []),
                                reading, jobs_now.get(n["node"], []), n.get("runners") or [],
                                last_job.get(n["node"]), now)
    with _EXTRAS_LOCK:
        ups, db_jobs = list(_EXTRAS.get("ups") or []), dict(_EXTRAS.get("recent_jobs") or {})
        alerts = dict(_EXTRAS.get("alerts") or {})
        published = list(_EXTRAS.get("published") or [])
    with _LLM_LOCK:
        llm_jobs = {n: list(d) for n, d in _RECENT_LLM.items()}
    for n in nodes:
        n["recent_jobs"] = recent_jobs_for(n["node"], db_jobs.get(n["node"], []), llm_jobs.get(n["node"], []))
    return {"updated": iso(datetime.now(timezone.utc)), "totals": tot, "nodes": nodes, "events": events, "ups": ups, "alerts": alerts, "published": published,
            "fleet_tonight": fl.get("fleet_tonight"), "queue": fl.get("queue"), "eta_days": fl.get("eta_days"),
            "recent_lessons": fl.get("recent_lessons", [])[:40]}


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
        if u.path == "/cluster":
            node = (parse_qs(u.query).get("node") or [""])[0]
            try:
                cur = db()
                if not authed(cur, node, self.headers.get("X-Minion-Token")):
                    return self._send(403, {"error": "unknown node or bad token"})
                return self._send(200, cluster(cur))
            except Exception as e:  # noqa: BLE001
                _LOCAL.conn = None
                log(f"cluster failed: {e}")
                return self._send(503, {"error": "cluster unavailable"})
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
        if urlparse(self.path).path == "/llm":
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 262144)
                d = json.loads(self.rfile.read(n) or b"{}")
                node = str(d.get("node") or "")
                if not authed(db(), node, self.headers.get("X-Minion-Token")):
                    return self._send(403, {"error": "unknown node or bad token"})
                llm_push(node, d, db(), self.client_address[0])
                return self._send(200, {"ok": True})
            except Exception as e:  # noqa: BLE001
                _LOCAL.conn = None
                log(f"llm push failed: {e}")
                return self._send(400, {"error": "bad snapshot"})
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


def ensure_schema(cur):
    """Columns the cluster view reads (2026-10-17): what each machine is FOR, and the name it also goes by."""
    cur.execute("ALTER TABLE minion_nodes ADD COLUMN IF NOT EXISTS purpose text, "
                "ADD COLUMN IF NOT EXISTS alias text, ADD COLUMN IF NOT EXISTS ip text")


def enroll(node: str, url: str | None, dedicated: bool, viewer: bool = False, probe: bool = False) -> str:
    if not _NODE.match(node):
        raise SystemExit("node: letters, digits, . _ - only")
    tok = secrets.token_urlsafe(32)
    cur = db()
    cur.execute("ALTER TABLE minion_nodes ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'minion'")
    cur.execute("""INSERT INTO minion_nodes (node, ollama_url, dedicated, token_hash, state, role) VALUES (%s,%s,%s,%s,'offline',%s)
                   ON CONFLICT (node) DO UPDATE SET token_hash=EXCLUDED.token_hash, dedicated=EXCLUDED.dedicated,
                   role=EXCLUDED.role, ollama_url=coalesce(EXCLUDED.ollama_url, minion_nodes.ollama_url)""",
                (node, (url or "").rstrip("/") or None, dedicated and not viewer, token_hash(tok),
                 "viewer" if viewer else "probe" if probe else "minion"))
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
    a = {"runners": [{"port": 1, "model": "m", "slots": [{"busy": True, "task": 5, "prompt": 100, "prompt_done": 40, "decoded": 0},
                                                         {"busy": True, "task": 9, "prompt": 10, "prompt_done": 10, "decoded": 30}]}]}
    b = {"runners": [{"port": 1, "model": "m", "slots": [{"busy": True, "task": 5, "prompt": 100, "prompt_done": 100, "decoded": 20},
                                                         {"busy": True, "task": 10, "prompt": 7, "prompt_done": 0, "decoded": 0}]}]}
    ev, dec, pre = slot_events("n", a, b, 2.0)
    assert ev == [{"node": "n", "model": "m", "prompt": 10, "decoded": 30}] and dec == 10.0 and pre == 30.0
    c = {"runners": [{"port": 1, "model": "m", "slots": [{"busy": False, "task": 5, "prompt": 100, "prompt_done": 0, "decoded": 0},
                                                         {"busy": True, "task": 13, "prompt": 7, "prompt_done": 7, "decoded": 2}]}]}
    ev2 = slot_events("n", b, c, 1.0)[0]
    assert {"node": "n", "model": "m", "prompt": 100, "decoded": 20} in ev2          # max(seen) survives the reset
    assert not any(e.get("unseen") for e in ev2)                                      # task jumps are not requests
    assert slot_events("n", None, b, 0)[0] == []
    assert token_hash("a") == hashlib.sha256(b"a").hexdigest()
    # job attribution + job line
    u = clean_users({"clients": [{"peer": "192.168.1.6:5000", "port": 11434, "proc": None},
                                 {"peer": "<script>", "proc": "x"}, "junk"],
                     "calls": [{"src": "192.168.1.6:5000", "proc": "nova_journal<b>"}], "ips": ["192.168.1.6", "bad ip"]})
    assert u == {"clients": [{"peer": "192.168.1.6:5000", "port": 11434, "proc": None}],
                 "calls": [{"src": "192.168.1.6:5000", "proc": "nova_journalb"}], "ips": ["192.168.1.6"]}
    snaps = {"mac-studio": {"calls": [{"src": "192.168.1.6:5000", "proc": "nova_journal"}]}}
    assert attribute("nova-core10", [{"peer": "192.168.1.6:5000"}], snaps, {}) == ["nova_journal @ mac-studio"]
    assert attribute("nova-core10", [{"peer": "192.168.1.2:9", "proc": None}], snaps, {"192.168.1.2": "nova-core"}) == ["client on nova-core"]
    assert attribute("n", [{"peer": "127.0.0.1:9", "proc": "curl"}, {"peer": "::1:7", "proc": "curl"}], {}, {}) == ["curl"]
    run = [{"model": "qwen3:8b", "slots": [{"busy": True, "decoded": 240}, {"busy": True, "prompt": 100, "prompt_done": 46}]}]
    assert busy_phase(run) == ("qwen3:8b", "generating 240 tok ×2") and busy_phase([]) == (None, None)
    jv = job_view(True, 1, True, 2, [], ["nova_journal @ mac-studio"], run, None, 100.0)
    assert jv["state"] == "running" and jv["text"] == "nova_journal @ mac-studio → qwen3:8b · generating 240 tok ×2"
    assert job_view(True, 1, True, 2, [], [], run, None, 1)["text"].endswith("caller not identified")
    assert job_view(True, 1, True, 1, ["reading: Dune"], ["nova_work_lessons @ mac-studio"], [], None, 1)["text"] == \
        "reading: Dune"
    assert job_view(True, 1, True, 1, ["reading: Dune"], ["curl"], [], None, 1)["text"] == "reading: Dune + curl · request in flight"
    assert job_view(True, 1, False, 0, [], [], [], None, 1) == {"state": "none", "text": "No LLM on this node", "jobs": []}
    assert job_view(True, 1, True, 0, [], [], [], None, 1)["text"] == "Idle · no model loaded"
    assert job_view(True, 1, True, 3, [], [], [], ("nova_journal → qwen3:8b", 0.0), 240.0)["text"] == \
        "Idle · last: nova_journal → qwen3:8b · 4m ago"
    assert job_view(False, 125, True, 3, [], [], [], None, 1)["text"] == "Silent · probe last heard 2m ago"
    assert _NODE.match("nova-core10") and not _NODE.match("../etc") and not _NODE.match("")
    # 1.1 extras: NUT parsing + UPS view + merged recent jobs
    nut = 'BEGIN LIST VAR bedroom\nVAR bedroom ups.load "17"\nVAR bedroom ups.status "OB LB"\nVAR bedroom battery.charge "42"\n' \
          'VAR bedroom battery.runtime "300"\nVAR bedroom ups.realpower.nominal "900"\nVAR other ups.load "99"\nVAR bedroom ups.serial "x"\nEND LIST VAR bedroom\n'
    raw = parse_nut(nut, "bedroom")
    assert raw == {"ups.load": "17", "ups.status": "OB LB", "battery.charge": "42", "battery.runtime": "300",
                   "ups.realpower.nominal": "900"}
    u = ups_view("master_bedroom", {"label": "Master Bedroom", "powers": ["nova-core4", 5]}, raw, None)
    assert u["on_battery"] and u["low_battery"] and not u["online"] and u["watts"] == 153 and u["powers"] == ["nova-core4"]
    assert u["charge_pct"] == 42.0 and u["runtime_s"] == 300.0 and u["ok"]
    d = ups_view("x", {}, None, "ConnectionRefusedError: x")
    assert not d["ok"] and d["load_pct"] is None and d["label"] == "x"
    j = recent_jobs_for("n", [{"t": 100.0, "at": "a", "kind": "reading", "text": "Dune"}], [(200.0, "nova_journal → qwen3:8b"), (50.0, "x")])
    assert [x["kind"] for x in j] == ["llm", "reading", "llm"] and "t" not in j[0]
    _RECENT_LLM.clear(); _LAST_JOB.clear(); _JOBS.clear()
    note_jobs("n", ["a"], "m", 1000.0); note_jobs("n", ["a"], "m", 1001.0); note_jobs("n", ["b"], None, 1002.0)
    assert [t for _, t in _RECENT_LLM["n"]] == ["a → m", "b"]
    note_jobs("n", ["b"], None, 1200.0)
    assert len(_RECENT_LLM["n"]) == 3
    tasks = {"a": {"enabled": True, "consecutive_failures": 12}, "b": {"enabled": True, "consecutive_failures": 4},
             "c": {"enabled": False, "consecutive_failures": 50}, "d": {"enabled": True, "consecutive_failures": 1},
             "e": {"enabled": True, "consecutive_failures": 0, "quarantined": True}}
    al = classify_tasks("studio", tasks, {"b": "Errno 2  no such\n file"})
    assert [(x["task"], x["kind"]) for x in al] == [("a", "dead-letter"), ("b", "failing"), ("e", "quarantined")]
    assert al[1]["reason"] == "Errno 2 no such file"
    ev = [("nova-core", datetime(2026, 10, 20, 0, 59, tzinfo=timezone.utc), "back"),
          ("nova-core3", datetime(2026, 10, 18, 23, 36, tzinfo=timezone.utc), "failover")]
    li = leader_items("nova-core3", ev[1:], datetime.now(timezone.utc))
    assert li[0]["kind"] == "failover" and "nova-core3" in li[0]["text"] and li[1]["kind"] == "leader"
    assert [x["kind"] for x in leader_items("nova-core", ev, datetime.now(timezone.utc))] == ["leader", "leader"]
    lg = "@@200\n\ncontent/belief/2026-10-19-a.md\ncontent/belief/_index.md\nstatic/x.webp\n@@100\n\ncontent/nova/b.md\n"
    assert parse_added(lg) == [(200, "belief", "2026-10-19-a"), (100, "nova", "b")]
    assert front_title('---\ntitle: "🔍 Belief and Friends: \\"Q\\""\ndate: x\n---') == '🔍 Belief and Friends: "Q"'
    pv = post_view(200, "belief", "2026-10-19-a", "🔍 Belief and Friends: Garments", {"belief": "Belief and Friends"})
    assert pv["emoji"] == "🔍" and pv["title"] == "Belief and Friends: Garments" and pv["url"].endswith("/belief/2026-10-19-a/")
    assert post_view(1, "cars", "x", "Plain title", {})["emoji"] == "🚗"
    assert menu_names("    - identifier: belief\n      name: Belief and Friends\n      url: /belief/\n") == {"belief": "Belief and Friends"}
    try:
        nut_vars("localhost", 1, "../x")
        raise AssertionError("bad ups name accepted")
    except ValueError:
        pass
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
    ap.add_argument("--probe", action="store_true", help="an LLM probe only: pushes /llm, never sent work")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.enroll:
        print(enroll(a.enroll, a.url, a.dedicated, a.viewer, a.probe))
        return 0
    if a.list:
        cur = db()
        cur.execute("SELECT node, state, dedicated, ollama_url, last_seen, info FROM minion_nodes ORDER BY node")
        for r in cur.fetchall():
            print(*r, sep=" | ")
        return 0
    if a.serve:
        try:
            ensure_schema(db())
        except Exception as e:  # noqa: BLE001 — a read-only replica moment must not stop the hub
            log(f"schema check skipped ({e})")
        threading.Thread(target=extras_loop, name="extras", daemon=True).start()
        log(f"serving on :{a.port}")
        ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
