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
  GET  /games?node=N        X-Minion-Token (viewer); the Games screen (2026-11-10): Nova Games DB health, live and
                            recent play sessions, Casting Director AI calls + tok/s, relay counters, game LLM jobs.
                            1.4 (2026-11-11, Nova Cluster 1.4.0), all additive: live sessions carry elapsed_s, apm,
                            events_5m, events, last_event {kind,text,level,ts} and for GTNW defcon / turn / game_date /
                            polls (last 12 approval values) / difficulty; plus timeline {minutes, series[{game,
                            counts[60]}]} (events per minute, oldest first), feed (last 20 events: id, ts, game, kind,
                            text, count (a same-tick burst), level bad|warn|good|gold|nova|start|info), today (per game: sessions, minutes, wins,
                            losses, other, open, best, events — Burbank day), week {days[7], rows[{day,game,sessions,
                            minutes}], wins, losses, win_rate}, fame (top 5 scores), ai_share {ai, other, pct, sources}
                            (today's events that carry `source`), and partial[] naming any extra that failed.
                            1.5 (2026-11-11, Nova Cluster 1.5.0), additive: chess {games, counted, record{wins,draws,
                            losses}, accuracy{you,them}, rivals[{key, name, kind, games, wins, draws, losses,
                            streak{result,n}, longest{wins,losses}, accuracy{you,them,n}, last}], recent[{id, ended,
                            mode, white/black{name,kind,accuracy}, lm, lm_result, result, termination, opening, moves,
                            plies, level}], review{url,title,at}} — GTNW Chess, Little Mister's head-to-head vs each
                            human and vs Nova. Privacy: a human opponent is "Challenger" unless play.consent says
                            name_in_reviews for that account (then only its display_name); no other names leave the hub.
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
import ipaddress
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


def lan_addr(addr: str | None) -> str | None:
    """The heartbeat's source address if it is a usable LAN address (not loopback), else None. Pure."""
    try:
        ip = ipaddress.ip_address(addr or "")
    except ValueError:
        return None
    return str(ip) if ip.is_private and not ip.is_loopback and not ip.is_link_local else None


def heartbeat(cur, node: str, hb: dict, addr: str | None = None):
    cur.execute("SELECT state, dedicated, coalesce(role,'minion') FROM minion_nodes WHERE node=%s", (node,))
    prev = cur.fetchone()
    if prev and prev[2] == "viewer":
        raise ValueError("a viewer does not heartbeat")
    state = hb["state"]
    if prev and prev[1] and state == "idle":
        state = "dedicated"            # a dedicated node is always available; its agent may still say idle
    cur.execute("""UPDATE minion_nodes SET state=%s, ollama_url=coalesce(%s, ollama_url), models=%s::jsonb,
                   last_seen=now(), since=CASE WHEN state IS DISTINCT FROM %s OR since IS NULL THEN now() ELSE since END,
                   info = info || %s::jsonb, ip = coalesce(%s, ip) WHERE node=%s""",
                (state, hb["ollama_url"], json.dumps(hb["models"]), state, json.dumps(hb["info"]), lan_addr(addr), node))
    # 2026-10-20: ip follows the address the node actually reached us from -- Ethernet when it is up (lower route
    # metric), Wi-Fi while it is down -- so work stops going to a dead NIC (nova-core7, AQC113 failing)
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


def cc_stats(cur) -> dict | None:
    """Claude Code usage from service_config (service='cc_stats', key='latest'), or None.

    Published every 10 min by nova_cc_stats_publish.py (cctime --week/--day) on .6 — the compact
    {"generated_at", "week", "day"} blob, served through to the Nova Cluster app as `claude_code`.
    Fail-open: a missing row, a DB error, or a non-dict value yields None so the snapshot is
    byte-identical to today when there is no data (the key is OMITTED, never null)."""
    try:
        cur.execute("SELECT value FROM service_config WHERE service = 'cc_stats' AND key = 'latest'")
        row = cur.fetchone()
    except Exception:  # noqa: BLE001 — CC stats are a nicety; never let them break the cluster view
        return None
    return row[0] if row and isinstance(row[0], dict) else None


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
        # Only the NEWEST event dates the current leadership. An older event for the same node is a previous
        # stint (10-21: hub said "since Sun 16:36" for a takeover at Wed 06:36 that an old wrapper never recorded).
        since = events[0][1] if events and events[0][0] == leader else None
        out.append({"level": "warn", "kind": "failover", "source": "scheduler-core", "task": None,
                    "text": f"scheduler-core is running on the standby ({leader})"
                            + (f" since {since.astimezone().strftime('%a %H:%M')}" if since else
                               " (takeover time not recorded)"), "reason": None})
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


# ── games (2026-11-10, Nova Cluster Games screen) ───────────────────────────────────────────────────────────
# The Nova Games DB (core3 games-pg :5433, read-only play_reader, TLS verify-full) and the relay's counters.
GAMES_PG = {"host": "192.168.1.5", "port": 5433, "dbname": "games", "user": "play_reader", "sslmode": "verify-full",
            "connect_timeout": 5}
GAMES_CA = ["/Volumes/nas/xcode/NovaGames/deploy/db/games-pg-ca.crt",
            "/Volumes/Data/xcode/NovaGames/deploy/db/games-pg-ca.crt"]
RELAY_STATS = "http://192.168.1.5:8088/healthz/relay/stats"
GAME_CALLER = re.compile(r"(?i)\b(gtnw|casting|nova\.server|blompie|cartest|stunt|mush|ng-relay|nova_games_relay|game_report)")
_GAMES_PW: dict = {}


def _games_password() -> str | None:
    if "pw" not in _GAMES_PW:
        r = subprocess.run(["security", "find-generic-password", "-s", "nova-games-play-reader", "-a", "nova", "-w"],
                           capture_output=True, text=True, timeout=10)
        _GAMES_PW["pw"] = r.stdout.strip() if r.returncode == 0 else None
    return _GAMES_PW["pw"]


def _session_view(r, consent: dict | None = None) -> dict:
    sid, game, client, player, started, seen, ended, outcome, score, actions, summ = r
    summ = summ if isinstance(summ, dict) else {}
    who = summ.get("leader") or summ.get("country_name") or ""
    what = summ.get("book") or summ.get("scenario") or ""
    if game == CHESS:                       # never the summary's names: roles only (see chess_side_name)
        names = {sd: chess_side_name(summ, player, sd, consent or {})["name"] for sd in ("white", "black")}
        who = f"{names['white']} vs {names['black']}"
        op = summ.get("opening") if isinstance(summ.get("opening"), dict) else {}
        what = op.get("name") or CHESS_MODES.get(summ.get("mode"), "")
        player = "default" if player == "default" else "challenger"
    return {"id": str(sid), "game": game, "client": client, "player": player, "started": iso(started),
            "last_seen": iso(seen), "ended": iso(ended) if ended else None, "outcome": outcome, "score": score,
            "actions": actions, "who": str(who)[:80], "what": str(what)[:120]}


def h_game(text: str) -> bool:
    """A game's LLM call (by caller name) — never a minion reading a book that happens to say "casting"."""
    t = text or ""
    return not t.startswith(("reading", "transcribing")) and bool(GAME_CALLER.search(t))


# ── games 1.4 (2026-11-11, Nova Cluster 1.4.0: "pretty-up the games section … more stats about live game play") ──
# Every extra below is its own small query against tiny tables (play.events is ~1 MB), each in its own try: one
# failing part leaves the rest of the payload intact and names itself in out["partial"]. Days are Burbank days.
GAMES_TZ = "America/Los_Angeles"
LIVE_MIN = 10                    # a session is live while open and seen within this many minutes
FEED_N = 20
TIMELINE_MIN = 60
WEEK_DAYS = 7
FAME_N = 5
AI_SOURCES = ("llm", "ai", "ollama", "nova", "model")
_CTRL = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]")
_TODAY = f"date_trunc('day', now() AT TIME ZONE '{GAMES_TZ}') AT TIME ZONE '{GAMES_TZ}'"
# Fixed SQL, built once from the constants above (no per-request interpolation; values go in as parameters).
_COLS = "id, game, client, player, started_at, last_seen_at, ended_at, outcome, score, actions, summary"
_JOIN = "FROM play.events e JOIN play.sessions s ON s.id = e.session_id"
SQL_LIVE = (f"SELECT {_COLS}, now() FROM play.sessions WHERE ended_at IS NULL AND "
            f"last_seen_at > now() - interval '{LIVE_MIN} minutes' ORDER BY last_seen_at DESC LIMIT 12")
SQL_RECENT = f"SELECT {_COLS} FROM play.sessions ORDER BY last_seen_at DESC LIMIT 12"
SQL_TIMELINE = (f"SELECT s.game, floor(extract(epoch FROM now() - e.ts) / 60)::int, count(*) {_JOIN} "
                f"WHERE e.ts > now() - interval '{TIMELINE_MIN} minutes' GROUP BY 1, 2")
SQL_FEED = (f"SELECT e.session_id::text, e.seq, e.ts, s.game, e.kind, e.detail {_JOIN} "
            f"ORDER BY e.ts DESC, e.seq DESC LIMIT {FEED_N * 3}")
SQL_TODAY = (f"SELECT game, count(*), coalesce(sum(extract(epoch FROM last_seen_at - greatest(started_at, {_TODAY}))), 0) "
             "/ 60, count(*) FILTER (WHERE outcome IN ('victory', 'win')), count(*) FILTER (WHERE outcome IN ('defeat', 'loss')), "
             "count(*) FILTER (WHERE ended_at IS NOT NULL AND coalesce(outcome, '') NOT IN ('victory', 'defeat', 'win', 'loss')), "
             "count(*) FILTER (WHERE ended_at IS NULL), max(score) FILTER (WHERE score > 0) "
             f"FROM play.sessions WHERE last_seen_at >= {_TODAY} GROUP BY 1")
SQL_TODAY_EVENTS = f"SELECT s.game, count(*) {_JOIN} WHERE e.ts >= {_TODAY} GROUP BY 1"
SQL_WEEK = (f"SELECT to_char(started_at AT TIME ZONE '{GAMES_TZ}', 'YYYY-MM-DD'), game, count(*), "
            "coalesce(sum(extract(epoch FROM last_seen_at - started_at)), 0) / 60 FROM play.sessions "
            f"WHERE started_at >= {_TODAY} - interval '{WEEK_DAYS - 1} days' GROUP BY 1, 2 ORDER BY 1")
SQL_WEEK_DAYS = (f"SELECT to_char((now() AT TIME ZONE '{GAMES_TZ}')::date - i, 'YYYY-MM-DD') "
                 f"FROM generate_series({WEEK_DAYS - 1}, 0, -1) i")
SQL_WEEK_WL = ("SELECT count(*) FILTER (WHERE outcome IN ('victory', 'win')), count(*) FILTER (WHERE outcome IN ('defeat', 'loss')), count(*) "
               f"FROM play.sessions WHERE coalesce(ended_at, last_seen_at) >= {_TODAY} - interval '{WEEK_DAYS - 1} days'")
SQL_FAME = ("SELECT game, player, summary, score, outcome, coalesce(ended_at, last_seen_at) FROM play.sessions "
            f"WHERE score IS NOT NULL AND score > 0 AND game <> 'gtnwchess' ORDER BY score DESC LIMIT {FAME_N}")
SQL_AI_SHARE = (f"SELECT s.game, lower(e.detail->>'source'), count(*) {_JOIN} "
                f"WHERE e.ts >= {_TODAY} AND e.detail ? 'source' GROUP BY 1, 2")


def clean_text(s, cap: int = 140) -> str:
    """Hub text for a wall display: no control / bidi characters, one line, capped."""
    t = " ".join(_CTRL.sub(" ", str(s if s is not None else "")).split())
    return t if len(t) <= cap else t[:cap - 1] + "…"


def _n(v):
    try:
        f = float(v)
        return int(f) if f == int(f) else round(f, 1)
    except (TypeError, ValueError):
        return v


def _big(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(f) >= div:
            return f"{f / div:.1f}".rstrip("0").rstrip(".") + suf
    return str(_n(f))


def human_kind(kind: str) -> str:
    return (kind or "event").replace("_", " ").replace("-", " ").capitalize()


# ── GTNW Chess (2026-11-11, Nova Cluster 1.5.0: "the rivalry on the dashboard") ──────────────────────────────────
# Sessions: game 'gtnwchess', player 'default' (Little Mister's Macs) or a site account (Amy's Mac); one game = one
# session (LAN: only the host writes). Contract: GTNW Chess PLAY_CONTRACT.md (summary.players/you/winner/analysis).
# Privacy (the reporter's rule): a human opponent shows as "Challenger" unless play.consent says name_in_reviews for
# that account, then only its display_name. Summary names are never passed through (except the fixed "Little Mister").
CHESS = "gtnwchess"
CHESS_MAX = 500                          # sessions read for the head-to-head (newest first)
CHESS_RECENT_N = 6
LM = "Little Mister"
CHALLENGER = "Challenger"
REVIEW_SITE = "https://nova.digitalnoise.net/"
CHESS_MODES = {"vs_nova": "vs Nova", "lan": "LAN", "hotseat": "hot seat"}
CHESS_TERM = {"checkmate": "checkmate", "resignation": "resignation", "stalemate": "stalemate", "fifty_move": "50-move rule",
              "threefold": "threefold repetition", "insufficient_material": "insufficient material",
              "agreement": "draw agreed", "timeout": "time", "abandoned": "abandoned"}
SQL_CHESS = ("SELECT id::text, player, coalesce(ended_at, last_seen_at), outcome, "
             "summary - 'pgn' - 'material_graph' - 'notable' FROM play.sessions "
             f"WHERE game = '{CHESS}' AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT {CHESS_MAX}")
SQL_CONSENT_EXISTS = "SELECT to_regclass('play.consent') IS NOT NULL"
SQL_CONSENT = ("SELECT account, name_in_reviews, display_name FROM play.consent WHERE game = %s "
               "ORDER BY updated_at NULLS FIRST")
SQL_CHESS_REVIEW = ("SELECT e.key, e.value FROM service_config c, jsonb_each(c.value) e "
                    "WHERE c.service = 'nova_game_report' AND c.key = 'reported' AND e.value->>'game' = %s "
                    "AND e.value->>'status' IN ('live', 'published') AND e.value->>'url' LIKE %s "
                    "ORDER BY coalesce(e.value->>'published_at', e.value->>'at') DESC NULLS LAST LIMIT 1")
_SIDES = ("white", "black")
_URL_OK = re.compile(r"^https://nova\.digitalnoise\.net/[A-Za-z0-9._~/%-]*$")


def _other(side: str) -> str:
    return "black" if side == "white" else "white"


def _chess_text(k: str, d: dict) -> str | None:
    """GTNW Chess events (None = not a chess event). Never a player name: sides and roles only."""
    g = d.get
    if k == "move" and g("san"):
        mn = g("move_number")
        t = (f"{mn}{'.' if g('side') != 'black' else '…'} " if mn is not None else "") + str(g("san"))
        cl = g("classification")
        if g("mate"):
            t += " — checkmate"
        elif cl in ("inaccuracy", "mistake", "blunder"):
            t += f" — {cl}" + (f" (best {g('best_san')})" if g("best_san") else "")
        if g("capture") and not g("mate"):
            t += f" · takes {g('capture')}"
        return t
    if k == "draw_offer":
        return f"Draw offered by {str(g('by') or '?').capitalize()} — {'accepted' if g('accepted') else 'declined'}"
    if k == "resign":
        return f"{str(g('by') or '?').capitalize()} resigns"
    if k == "game_over" and ("termination" in d or "white_accuracy" in d):
        bits = [str(g("result") or "game over"), CHESS_TERM.get(g("termination"), str(g("termination") or "").replace("_", " "))]
        op = g("opening") if isinstance(g("opening"), dict) else {}
        if op.get("name"):
            bits.append(str(op["name"]))
        if isinstance(g("white_accuracy"), (int, float)) and isinstance(g("black_accuracy"), (int, float)):
            bits.append(f"accuracy {round(g('white_accuracy'))}/{round(g('black_accuracy'))}")
        return "Chess: " + " · ".join(b for b in bits if b)
    if k == "game_start" and ("white_kind" in d or "black_kind" in d):
        mode = g("mode")
        if mode == "vs_nova":
            return "New chess game vs Nova" + (f" · level {g('level')}" if g("level") is not None else "")
        return "New chess game · " + CHESS_MODES.get(mode, "two players")
    return None


def _chess_level(k: str, d: dict) -> str | None:
    if k == "move" and d.get("san"):
        if d.get("mate"):
            return "gold"
        return {"blunder": "bad", "mistake": "warn"}.get(d.get("classification"), "info")
    if k == "draw_offer":
        return "good" if d.get("accepted") else "info"
    if k == "resign":
        return "warn"
    if k == "game_over" and ("termination" in d or "white_accuracy" in d):
        return "gold" if d.get("winner") in _SIDES else "good"
    return None


def _players(summ: dict) -> dict:
    pl = summ.get("players") if isinstance(summ.get("players"), dict) else {}
    return {sd: (pl.get(sd) if isinstance(pl.get(sd), dict) else {}) for sd in _SIDES}


def chess_lm_side(player, summ: dict) -> str | None:
    """Which side Little Mister played: the side whose account is 'default', else the local side of a 'default'
    session, else (a LAN game Amy hosted) the remote human side; a hot seat on his Mac by the side named
    "Little Mister". None when he wasn't in the game (Amy vs Nova on her Mac) or it can't be told."""
    sides = _players(summ)
    acct = {sd: sides[sd].get("account") for sd in _SIDES}
    lm = [sd for sd in _SIDES if acct[sd] == "default"]
    if len(lm) == 1:
        return lm[0]
    if len(lm) == 2:
        return None                                   # both sides his: not a rivalry game
    you = summ.get("you")
    if player == "default":
        if you in _SIDES:
            return you
        named = [sd for sd in _SIDES if str(sides[sd].get("name") or "").strip().lower() == LM.lower()]
        return named[0] if len(named) == 1 else None
    if player and summ.get("mode") == "lan" and you in _SIDES:
        o = _other(you)
        if sides[o].get("kind", "human") != "nova" and acct[o] in (None, "default"):
            return o
    return None


def chess_side_name(summ: dict, player, side: str, consent: dict, lm: str | None = "?") -> dict:
    """{"name", "kind", "account"} for one side, by role: Little Mister / Nova / a consented display name / Challenger."""
    sides = _players(summ)
    s = sides.get(side, {})
    if lm == "?":
        lm = chess_lm_side(player, summ)
    if s.get("kind") == "nova":
        return {"name": "Nova", "kind": "nova", "account": None}
    if side == lm:
        return {"name": LM, "kind": "human", "account": "default"}
    acct = s.get("account")
    if not acct and player and player != "default" and summ.get("you") == side:
        acct = player                                   # the local side of a session written from that account
    if acct == "default":
        return {"name": LM, "kind": "human", "account": "default"}
    name = consent.get(acct) if acct else None
    return {"name": name or CHALLENGER, "kind": "human", "account": acct or None}


def chess_lm_result(lm: str, summ: dict, outcome, player) -> str | None:
    """win | loss | draw from Little Mister's side; None for abandoned / unfinished games."""
    if summ.get("termination") == "abandoned" or summ.get("result") == "*" or outcome == "abandoned":
        return None
    w, r = summ.get("winner"), summ.get("result")
    if w in _SIDES:
        return "win" if w == lm else "loss"
    if r == "1/2-1/2":
        return "draw"
    if r in ("1-0", "0-1"):
        return "win" if (r == "1-0") == (lm == "white") else "loss"
    if outcome in ("win", "loss", "draw") and player == "default" and summ.get("you") == lm:
        return outcome
    return None


def _accuracy(summ: dict, side: str):
    a = summ.get("analysis") if isinstance(summ.get("analysis"), dict) else {}
    v = (a.get(side) or {}).get("accuracy") if isinstance(a.get(side), dict) else None
    return round(float(v), 1) if isinstance(v, (int, float)) and 0 <= v <= 100 else None


def _rival_key(kind: str, acct) -> str:
    if kind == "nova":
        return "nova"
    return "h-" + hashlib.sha256(str(acct).encode()).hexdigest()[:10] if acct else "challenger"


def _consent(cur) -> dict:
    """{account: display_name} for accounts that opted in to being named (play.consent, game gtnwchess). A missing
    table, row or grant is "no consent": the opponent stays "Challenger"."""
    try:
        cur.execute(SQL_CONSENT_EXISTS)
        row = cur.fetchone()
        if not row or not row[0]:
            return {}
        cur.execute(SQL_CONSENT, (CHESS,))
        rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log(f"games consent unreadable (treated as no consent): {str(e).splitlines()[0][:120] if str(e) else type(e).__name__}")
        return {}
    out: dict = {}
    for acct, ok, name in rows:
        if not acct or acct == "default":
            continue
        nm = clean_text(name, 32) if ok is True and name else ""
        if nm and nm.lower() not in (LM.lower(), "nova"):
            out[str(acct)] = nm
        else:
            out.pop(str(acct), None)                  # a later "no" wins
    return out


def _avg(xs: list):
    return round(sum(xs) / len(xs), 1) if xs else None


def chess_view(rows: list, consent: dict) -> dict:
    """Recent games + Little Mister's head-to-head vs each opponent (newest-first rows in)."""
    recent, games = [], []
    for sid, player, at, outcome, summ in rows:
        summ = summ if isinstance(summ, dict) else {}
        lm = chess_lm_side(player, summ)
        names = {sd: chess_side_name(summ, player, sd, consent, lm) for sd in _SIDES}
        res = chess_lm_result(lm, summ, outcome, player) if lm else None
        sides = _players(summ)
        if len(recent) < CHESS_RECENT_N:
            op = summ.get("opening") if isinstance(summ.get("opening"), dict) else {}
            opening = " ".join(str(x) for x in (op.get("eco"), op.get("name")) if x)
            nova = next((sides[sd] for sd in _SIDES if sides[sd].get("kind") == "nova"), {})
            recent.append({"id": str(sid), "ended": iso(at), "mode": CHESS_MODES.get(summ.get("mode"), None),
                           "white": {"name": names["white"]["name"], "kind": names["white"]["kind"], "accuracy": _accuracy(summ, "white")},
                           "black": {"name": names["black"]["name"], "kind": names["black"]["kind"], "accuracy": _accuracy(summ, "black")},
                           "lm": lm, "lm_result": res, "result": clean_text(summ.get("result") or "", 8) or None,
                           "termination": CHESS_TERM.get(summ.get("termination")) if summ.get("termination") else None,
                           "opening": clean_text(opening, 60) or None,
                           "moves": summ.get("moves") if isinstance(summ.get("moves"), int) else None,
                           "plies": summ.get("plies") if isinstance(summ.get("plies"), int) else None,
                           "level": clean_text(nova.get("level_name"), 20) if nova.get("level_name") else None})
        if lm and res:
            opp = names[_other(lm)]
            games.append((at, _rival_key(opp["kind"], opp["account"]), opp, res,
                          _accuracy(summ, lm), _accuracy(summ, _other(lm))))
    rivals: dict = {}
    for at, key, opp, res, a_you, a_them in reversed(games):         # oldest first, for the streaks
        r = rivals.setdefault(key, {"key": key, "name": "Nova" if opp["kind"] == "nova" else opp["name"], "kind": opp["kind"],
                                    "games": 0, "wins": 0, "draws": 0, "losses": 0, "streak": None,
                                    "longest": {"wins": 0, "losses": 0}, "_you": [], "_them": [], "_run": {"win": 0, "loss": 0},
                                    "last": None})
        r["games"] += 1
        r[{"win": "wins", "draw": "draws", "loss": "losses"}[res]] += 1
        st = r["streak"]
        r["streak"] = {"result": res, "n": st["n"] + 1 if st and st["result"] == res else 1}
        for k, plural in (("win", "wins"), ("loss", "losses")):
            r["_run"][k] = r["_run"][k] + 1 if res == k else 0
            r["longest"][plural] = max(r["longest"][plural], r["_run"][k])
        if a_you is not None and a_them is not None:
            r["_you"].append(a_you); r["_them"].append(a_them)
        r["last"] = iso(at)
    out_rivals, all_you, all_them = [], [], []
    for r in rivals.values():
        all_you += r["_you"]; all_them += r["_them"]
        r["accuracy"] = {"you": _avg(r["_you"]), "them": _avg(r["_them"]), "n": len(r["_you"])}
        for k in ("_you", "_them", "_run"):
            del r[k]
        out_rivals.append(r)
    out_rivals.sort(key=lambda r: (r["kind"] == "nova", -r["games"]))     # humans first, then Nova
    return {"games": len(rows), "counted": len(games),
            "record": {k: sum(r[k] for r in out_rivals) for k in ("wins", "draws", "losses")},
            "accuracy": {"you": _avg(all_you), "them": _avg(all_them)},
            "rivals": out_rivals, "recent": recent, "review": None}


def _chess(cur, consent: dict) -> dict:
    cur.execute(SQL_CHESS)
    return chess_view(cur.fetchall(), consent)


def chess_review(ops_cur) -> dict | None:
    """Nova's newest published GTNW Chess review (nova_game_report's reported map in nova_ops), only on her own site."""
    if ops_cur is None:
        return None
    ops_cur.execute(SQL_CHESS_REVIEW, (CHESS, REVIEW_SITE + "%"))
    row = ops_cur.fetchone()
    if not row:
        return None
    _sid, v = row
    v = v if isinstance(v, dict) else {}
    url = str(v.get("url") or "")
    if not _URL_OK.match(url):
        return None
    return {"url": url, "title": clean_text(v.get("title") or "Nova's review", 120),
            "at": clean_text(v.get("published_at") or v.get("at") or "", 40) or None}


def event_text(kind: str, d) -> str:
    """One human line for a play event (GTNW, Blompie, CarTest, Stunt Driver …)."""
    d = d if isinstance(d, dict) else {}
    g = d.get
    k = kind or ""
    try:
        chess = _chess_text(k, d)
        if chess is not None:
            t = chess
        elif k in ("ticker", "nova_commentary") and g("text"):
            t = g("text")
        elif k == "game_over":
            t = ("Victory" if g("won") else "Game over") + (f" after {g('turns')} turns" if g("turns") is not None else "")
            if g("final_score"):
                t += f" · score {_big(g('final_score'))}"
            if g("reason"):
                t += f" — {g('reason')}"
        elif k == "game_start":
            bits = [g("country_name") or g("country"), g("scenario"), g("difficulty"), g("game_date")]
            t = "New game: " + " · ".join(str(b) for b in bits if b) if any(bits) else "New game"
        elif k == "start":
            t = "New adventure" + (f" · {g('model')}" if g("model") else "") + (f" · {g('tone')}" if g("tone") else "")
        elif k == "turn":
            t = f"Turn {g('turn')}: {g('action') or 'acts'}" + (" (failed)" if g("failed") else "")
        elif k == "nuclear_strike":
            t = f"☢ {g('attacker') or '?'} nukes {g('target') or '?'}: {g('warheads') or '?'} warheads"
            if g("casualties"):
                t += f", {_big(g('casualties'))} dead"
        elif k == "defcon_change":
            t = f"DEFCON {g('from')} → {g('to')}"
        elif k in ("approval_swing", "congress_swing"):
            t = f"{'Approval' if k == 'approval_swing' else 'Congress'} {_n(g('from'))}% → {_n(g('to'))}%"
        elif k == "impeachment_track":
            t = f"Impeachment stage {g('from')} → {g('to')}"
        elif k == "record_set":
            rec = re.sub(r"(?<!^)(?=[A-Z])", " ", str(g("record") or "record")).lower()
            t = f"Record: {rec} {_n(g('value'))}" + (f" (was {_n(g('previous'))})" if g("previous") is not None else "")
        elif k == "crisis_decision":
            t = f"{g('title') or 'Crisis'}: {g('chose') or '?'}" + (" — worked" if g("worked") else " — failed" if g("worked") is False else "")
        elif k == "treaty_answered":
            t = f"{g('type') or 'Treaty'} with {g('from') or '?'} {'accepted' if g('accepted') else 'rejected'}"
        elif k == "world_war":
            t = f"War: {g('aggressor') or '?'} attacks {g('defender') or '?'}"
        elif k in ("president_order", "attack_ordered"):
            t = f"{g('action') or 'Order'} → {g('target_name') or g('target') or '?'}" + (f": {g('summary')}" if g("summary") else "")
        elif k == "peace_brokered":
            t = f"Peace {'brokered' if g('success') else 'failed'}: {g('a')} – {g('b')}"
        elif k == "election_result":
            t = f"Election {g('year')}: {g('outcome')} vs {g('opponent')}"
        elif k in ("ai_tactic", "tactic_used"):
            t = f"{g('actor') or 'US'} {str(g('tactic') or 'tactic').replace('_', ' ')} → {g('target') or '?'}"
        elif k == "world_claim":
            t = f"{g('power') or '?'} {g('how') or 'claims'} {g('land') or '?'}"
        elif k == "renamed_territory":
            t = f"{g('old_name') or g('land') or '?'} renamed {g('name') or '?'}"
        elif k == "drag_race":
            t = f"{g('winner_car') or 'Drag race'} wins" + (f" by {g('margin')}s" if g("margin") is not None else "")
        elif k == "race":
            t = f"{g('car') or 'Race'} · place {g('place')} · {g('course') or ''}".rstrip(" ·")
        else:
            key = next((x for x in ("text", "sentence", "reason", "title", "name", "target_name", "summary") if g(x)), None)
            t = human_kind(k) + (f": {g(key)}" if key else "")
    except Exception:  # noqa: BLE001  (odd detail shapes never break the feed)
        t = human_kind(k)
    return clean_text(t)


def event_level(kind: str, d) -> str:
    """bad | warn | good | gold | nova | start | info — the app maps these to colours."""
    d = d if isinstance(d, dict) else {}
    k = kind or ""
    chess = _chess_level(k, d)
    if chess is not None:
        return chess
    if k in ("nuclear_strike", "world_war", "world_atrocity", "war_powers_violation", "territory_lost", "wmd_program_started"):
        return "bad"
    if k == "defcon_change":
        to, fr = d.get("to"), d.get("from")
        if isinstance(to, (int, float)) and to <= 2:
            return "bad"
        return "warn" if isinstance(to, (int, float)) and isinstance(fr, (int, float)) and to < fr else "good"
    if k in ("treaty_answered", "peace_brokered"):
        return "good" if d.get("accepted", d.get("success")) else "warn"
    if k in ("record_set", "statehood", "nation_founded"):
        return "gold"
    if k == "game_over":
        return "gold" if d.get("won") else "bad"
    if k in ("game_start", "start"):
        return "start"
    if k == "nova_commentary":
        return "nova"
    if k in ("approval_swing", "congress_swing"):
        ch = d.get("change")
        if not isinstance(ch, (int, float)):
            to, fr = d.get("to"), d.get("from")
            ch = to - fr if isinstance(to, (int, float)) and isinstance(fr, (int, float)) else 0
        return "warn" if ch < 0 else "good"
    if k in ("impeachment_track", "crisis_decision", "attack_ordered", "president_order", "ai_tactic", "tactic_used"):
        return "warn"
    if k == "ticker":
        return "warn" if (d.get("severity") or 0) >= 3 else "info"
    if k == "turn":
        return "warn" if d.get("failed") else "info"
    return "info"


def _gtnw_extras(summ: dict, last_detail: dict | None = None) -> dict:
    polls = [p.get("popularity") for p in (summ.get("polls") or []) if isinstance(p, dict)]
    polls = [round(float(p), 1) for p in polls if isinstance(p, (int, float))][-12:]
    gd = summ.get("game_date") or (last_detail or {}).get("game_date") or summ.get("game_date_end")
    return {"defcon": summ.get("defcon") if isinstance(summ.get("defcon"), int) else None,
            "turn": summ.get("turns") if isinstance(summ.get("turns"), int) else None,
            "game_date": clean_text(gd, 24) if gd else None, "polls": polls,
            "difficulty": clean_text(summ.get("difficulty"), 24) if summ.get("difficulty") else None}


def _live_extras(cur, live: list, raw: dict, now: datetime) -> None:
    """Elapsed, APM, events in the last 5 min, last event; GTNW: DEFCON, turn, date, approval polls."""
    ids = [s["id"] for s in live]
    if not ids:
        return
    cur.execute("SELECT session_id::text, count(*) FILTER (WHERE ts > now() - interval '5 minutes'), count(*) "
                "FROM play.events WHERE session_id = ANY(%s::uuid[]) GROUP BY 1", (ids,))
    counts = {sid: (n5, n) for sid, n5, n in cur.fetchall()}
    cur.execute("SELECT DISTINCT ON (session_id) session_id::text, ts, kind, detail FROM play.events "
                "WHERE session_id = ANY(%s::uuid[]) ORDER BY session_id, seq DESC", (ids,))
    last = {sid: (ts, kind, det) for sid, ts, kind, det in cur.fetchall()}
    for s in live:
        st = raw[s["id"]]
        elapsed = max(0.0, (now - st["started"]).total_seconds()) if st["started"] else 0.0
        s["elapsed_s"] = int(elapsed)
        s["apm"] = round((s.get("actions") or 0) / max(1.0, elapsed / 60), 2)
        n5, n = counts.get(s["id"], (0, 0))
        s["events_5m"], s["events"] = n5, n
        if s["id"] in last:
            ts, kind, det = last[s["id"]]
            s["last_event"] = {"kind": kind, "text": event_text(kind, det), "level": event_level(kind, det), "ts": iso(ts)}
        if s.get("game") == "gtnw":
            s.update(_gtnw_extras(st["summary"], last.get(s["id"], (None, None, None))[2]))


def _timeline(cur) -> dict:
    cur.execute(SQL_TIMELINE)
    series: dict = {}
    for game, ago, n in cur.fetchall():
        if 0 <= ago < TIMELINE_MIN:
            series.setdefault(game, [0] * TIMELINE_MIN)[TIMELINE_MIN - 1 - ago] += n
    return {"minutes": TIMELINE_MIN,
            "series": [{"game": g, "counts": c} for g, c in sorted(series.items(), key=lambda x: -sum(x[1]))]}


def _feed(cur) -> list:
    """The newest events across games. A burst of the same kind from one session in the same tick (eight
    world_claims within two seconds) is one line with a count, so the feed reads as a story, not a log."""
    cur.execute(SQL_FEED)
    out: list = []
    for sid, seq, ts, game, kind, det in cur.fetchall():
        prev = out[-1] if out else None
        if prev and prev["_k"][:2] == (sid, kind) and abs((prev["_k"][2] - ts).total_seconds()) <= 2:
            prev["count"] += 1
            prev["_k"] = (sid, kind, ts)
            continue
        if len(out) >= FEED_N:
            break
        out.append({"_k": (sid, kind, ts), "id": f"{sid}:{seq}", "ts": iso(ts), "game": game, "kind": clean_text(kind, 40),
                    "text": event_text(kind, det), "level": event_level(kind, det), "count": 1})
    for e in out:
        del e["_k"]
    return out


def _today(cur) -> list:
    cur.execute(SQL_TODAY)
    rows = {g: {"game": g, "sessions": n, "minutes": round(float(m), 1), "wins": w, "losses": lo, "other": o,
                "open": op, "best": sc, "events": 0} for g, n, m, w, lo, o, op, sc in cur.fetchall()}
    cur.execute(SQL_TODAY_EVENTS)
    for g, n in cur.fetchall():
        rows.setdefault(g, {"game": g, "sessions": 0, "minutes": 0.0, "wins": 0, "losses": 0, "other": 0,
                            "open": 0, "best": None, "events": 0})["events"] = n
    return sorted(rows.values(), key=lambda r: (-r["minutes"], -r["events"]))


def _week(cur) -> dict:
    cur.execute(SQL_WEEK)
    rows = [{"day": d, "game": g, "sessions": n, "minutes": round(float(m), 1)} for d, g, n, m in cur.fetchall()]
    cur.execute(SQL_WEEK_DAYS)
    days = [d for (d,) in cur.fetchall()]
    cur.execute(SQL_WEEK_WL)
    w, lo, n = cur.fetchone()
    return {"days": days, "rows": rows, "wins": w, "losses": lo, "sessions": n,
            "win_rate": round(w / (w + lo), 3) if (w + lo) else None}


def _fame(cur) -> list:
    cur.execute(SQL_FAME)
    out = []
    for game, player, summ, score, outcome, at in cur.fetchall():
        summ = summ or {}
        who = summ.get("leader") or summ.get("country_name") or player or ""
        out.append({"game": game, "who": clean_text(who, 60), "score": score, "outcome": outcome, "at": iso(at),
                    "turns": summ.get("turns") if isinstance(summ.get("turns"), int) else None})
    return out


def _ai_share(cur) -> dict:
    cur.execute(SQL_AI_SHARE)
    rows = [{"game": g, "source": clean_text(src, 20), "n": n} for g, src, n in cur.fetchall()]
    ai = sum(r["n"] for r in rows if r["source"] in AI_SOURCES)
    total = sum(r["n"] for r in rows)
    return {"ai": ai, "other": total - ai, "pct": round(ai / total, 3) if total else None, "sources": rows}


def collect_games(_cur=None) -> dict:
    """Games DB health + sessions + live-play stats + Casting Director's AI calls, and the relay counters.
    Never raises for one part: each extra is its own try, and a failed one is listed in out["partial"]."""
    import psycopg2
    out: dict = {"db": {"up": False}, "relay": None}
    t0 = time.time()

    def part(name, fn):
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            out.setdefault("partial", []).append(name)
            log(f"games {name} failed: {str(e).splitlines()[0][:160] if str(e) else type(e).__name__}")

    try:
        ca = next((c for c in GAMES_CA if os.path.exists(c)), None)
        conn = psycopg2.connect(password=_games_password(), sslrootcert=ca, **GAMES_PG)
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            out["db"] = {"up": True, "ms": round((time.time() - t0) * 1000), "host": "core3 :5433"}
            consent = _consent(cur)                     # never raises: unreadable = nobody consented
            cur.execute(SQL_LIVE)
            rows = cur.fetchall()
            out["live"] = [_session_view(r[:-1], consent) for r in rows]
            raw = {str(r[0]): {"started": r[4], "summary": r[10] or {}} for r in rows}
            db_now = rows[0][-1] if rows else datetime.now(timezone.utc)
            part("live_stats", lambda: _live_extras(cur, out["live"], raw, db_now))
            out.pop("live_stats", None)                 # _live_extras enriches out["live"] in place
            cur.execute(SQL_RECENT)
            out["recent"] = [_session_view(r, consent) for r in cur.fetchall()]
            cur.execute("SELECT game, count(*), count(*) FILTER (WHERE started_at::date = current_date), max(last_seen_at) "
                        "FROM play.sessions GROUP BY game ORDER BY 4 DESC")
            out["games"] = [{"game": g, "sessions": n, "today": d, "last": iso(m)} for g, n, d, m in cur.fetchall()]
            cur.execute("SELECT count(*), count(*) FILTER (WHERE ts > now() - interval '1 hour') FROM play.events "
                        "WHERE ts > now() - interval '1 day'")
            e_day, e_hour = cur.fetchone()
            out["events"] = {"day": e_day, "hour": e_hour}
            part("timeline", lambda: _timeline(cur))
            part("feed", lambda: _feed(cur))
            part("today", lambda: _today(cur))
            part("week", lambda: _week(cur))
            part("fame", lambda: _fame(cur))
            part("ai_share", lambda: _ai_share(cur))
            part("chess", lambda: _chess(cur, consent))
            if "chess" in out:
                try:
                    out["chess"]["review"] = chess_review(_cur)
                except Exception as e:  # noqa: BLE001  (nova_ops down: the rivalry still shows)
                    out.setdefault("partial", []).append("chess_review")
                    log(f"games chess_review failed: {str(e).splitlines()[0][:160] if str(e) else type(e).__name__}")
            try:
                cur.execute("SELECT t, role, backend, model, ok, ms, tokens_out FROM casting.ai_calls ORDER BY t DESC LIMIT 15")
                calls = [{"t": iso(t), "game": "casting", "role": ro, "backend": b, "model": m, "ok": ok, "ms": ms,
                          "tokens_out": to, "tok_s": round(to / (ms / 1000), 1) if to and ms else None}
                         for t, ro, b, m, ok, ms, to in cur.fetchall()]
                cur.execute("SELECT count(*), coalesce(sum(tokens_out),0), coalesce(sum(ms),0) FROM casting.ai_calls "
                            "WHERE t::date = current_date")
                n, toks, ms = cur.fetchone()
                out["ai"] = {"calls": calls, "today": {"calls": n, "tokens_out": int(toks),
                                                      "tok_s": round(toks / (ms / 1000), 1) if ms else None}}
            except Exception as e:  # noqa: BLE001  (an older games DB without the casting grant)
                out["ai"] = {"calls": [], "today": None, "error": str(e)[:120]}
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        out["db"] = {"up": False, "error": str(e).splitlines()[0][:160] if str(e) else type(e).__name__}
    try:
        with urllib.request.urlopen(RELAY_STATS, timeout=4) as r:
            out["relay"] = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        out["relay"] = {"status": "down", "error": str(e)[:120]}
    return out


def games_view(now: float) -> dict:
    """The Games screen: the collector's cache + which LLM work on the cluster right now is for a game."""
    with _EXTRAS_LOCK:
        g = dict(_EXTRAS.get("games") or {"db": {"up": False, "error": "not collected yet"}})
    running, recent, tok_s = [], [], 0.0
    with _LLM_LOCK:
        for node, d in _LLM.items():
            live = now - d["t"] < LLM_FRESH_S
            labels = [k for k, t in _JOBS.get(node, {}).items() if now - t <= JOB_LINGER_S and h_game(k)]
            if labels and live:
                model, phase = busy_phase(d["snap"].get("runners", []))
                running.append({"node": node, "jobs": labels, "model": model, "phase": phase, "tok_s": d["tok_s"]})
                tok_s += d["tok_s"] or 0
            recent += [{"node": node, "text": txt, "at": iso(datetime.fromtimestamp(t, timezone.utc))}
                       for t, txt in _RECENT_LLM.get(node, []) if h_game(txt)]
    g["llm"] = {"running": running, "tok_s": round(tok_s, 1),
                "recent": sorted(recent, key=lambda r: r["at"], reverse=True)[:15]}
    g["updated"] = iso(datetime.now(timezone.utc))
    return g


COLLECTORS = [("ups", collect_ups, 10), ("recent_jobs", collect_recent_jobs, 30), ("alerts", collect_alerts, 30),
              ("published", collect_published, 60), ("games", collect_games, 10)]


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
    snap = {"updated": iso(datetime.now(timezone.utc)), "totals": tot, "nodes": nodes, "events": events, "ups": ups, "alerts": alerts, "published": published,
            "fleet_tonight": fl.get("fleet_tonight"), "queue": fl.get("queue"), "eta_days": fl.get("eta_days"),
            "recent_lessons": fl.get("recent_lessons", [])[:40]}
    cc = cc_stats(cur)                 # additive (2026-10-24): Claude Code usage card, OMITTED when there's no row
    if cc is not None:
        snap["claude_code"] = cc
    return snap


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
        if u.path == "/games":
            node = (parse_qs(u.query).get("node") or [""])[0]
            try:
                cur = db()
                if not authed(cur, node, self.headers.get("X-Minion-Token")):
                    return self._send(403, {"error": "unknown node or bad token"})
                return self._send(200, games_view(time.time()))
            except Exception as e:  # noqa: BLE001
                _LOCAL.conn = None
                log(f"games failed: {e}")
                return self._send(503, {"error": "games unavailable"})
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
            state = heartbeat(cur, node, clean_hb(d), self.client_address[0])
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
    # games (2026-11-10): only game callers count as game traffic
    assert h_game("GTNW @ mac-studio") and h_game("python3.14 nova.server @ nova-core8") and h_game("ng-relay")
    assert not h_game("nova_notifier @ mac-studio") and not h_game("reading: The Gaming of America")
    assert not h_game("reading: Casting Shadows")
    # games 1.4 (2026-11-11): one human line + a level per play event, never raising on odd shapes
    assert event_text("defcon_change", {"from": 3, "to": 2}) == "DEFCON 3 → 2" and event_level("defcon_change", {"from": 3, "to": 2}) == "bad"
    assert event_text("ticker", {"text": "a\u202eb\nc"}) == "a b c" and event_level("nuclear_strike", {}) == "bad"
    assert event_text("weird_kind", None) == "Weird kind" and event_text("game_over", "x") == "Game over"
    assert event_level("record_set", {}) == "gold" and event_level("treaty_answered", {"accepted": True}) == "good"
    # GTNW Chess (2026-11-11): moves / resign / game over read as chess; opponents by role only
    assert event_text("move", {"move_number": 12, "side": "black", "san": "Qxf2+", "classification": "blunder", "best_san": "Qd8"}) \
        == "12… Qxf2+ — blunder (best Qd8)" and event_level("move", {"san": "e4", "classification": "blunder"}) == "bad"
    assert event_text("resign", {"by": "white"}) == "White resigns" and event_level("game_over", {"termination": "agreement"}) == "good"
    assert chess_side_name({"players": {"white": {"name": "Amy", "kind": "human"}}, "you": "black"}, "default", "white", {})["name"] == CHALLENGER
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
    stale = leader_items("nova-core3", ev, datetime.now(timezone.utc))[0]["text"]  # unrecorded re-takeover
    assert "since" not in stale and "not recorded" in stale, stale
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
