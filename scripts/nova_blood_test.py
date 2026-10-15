#!/usr/bin/env python3
"""nova_blood_test.py — MACREADY'S BLOOD TEST: which of Nova's health checks could an impostor pass?

Little Mister, 2026-10-15 (after Nova watched The Thing Expanded): "Add all 9."

In Carpenter's The Thing the creature imitates. It looks like Norris, talks like Norris, and goes
after "the people that are going to uncover him". Asking it whether it is human proves nothing;
MacReady's blood test does, because a hot wire in a petri dish is a real test the imitation cannot
fake. Nova's health checks have the same problem. A port that accepts a connection, an HTTP 200,
a heartbeat row, a fresh file mtime: a hung, hollow or wedged process passes every one of them.

Two jobs:

  1. CLASSIFY every check Nova runs on its critical services (memory, chat, scheduler, pg,
     journal) as IMITABLE (process exists, port open, HTTP status, a heartbeat/timestamp the
     checked thing writes about itself, a reply whose content is never compared to anything) or
     BLOOD (a fresh nonce makes a round trip through the real work path and comes back intact).
     The catalogue is static for the scripted checks and read live from service_registry for the
     mesh agent's TCP/HTTP checks. Written once a day as blood_test rows with src='inventory'.

  2. RUN BLOOD CANARIES, each next to the imitable check it would replace:
       memory     /remember a nonce memory (source blood_test), find it again through the memory
                  server's own /search, then delete it. The prober's memory_roundtrip already proves
                  remember -> DB; this proves remember -> recall, the path Nova actually uses.
       chat       POST the gateway's /api/chat (full agent -> router -> Ollama) asking it to echo a
                  nonce; pass only if the nonce comes back. The trace/session rows it creates are
                  deleted by id afterwards.
       scheduler  every scheduled run writes a signed beat (nonce, time) for its host into
                  blood_canary; the next run on ANY host checks every host's beat is fresh and its
                  signature verifies. A scheduler that reports success without running code cannot.
       pg         write a signed nonce into blood_canary on the primary, read it back on a second
                  connection, check the signature and that the server is not in recovery.
       journal    the Hugo feed's lastBuildDate is recent and the newest post actually serves with
                  its own title (the build pipeline ran and the CDN serves its output).
     A canary that fails while its imitable sibling passes is an IMPOSTOR: critical notification.

Canary values are signed with HMAC-SHA256 under the key 'nova-blood-test-key' from the fleet secret
store (nova_secrets; created there on first scheduled run or --init-key). Without the key every row
is hash-chained instead (mac = sha256(previous mac | row)), so edits to history are still visible.

Measured for the monthly continuity review (--report): imitable share of checks per critical
service (target: falling), blood pass rate per service and month, impostor count.

CLI:   --scheduled      classify (once a day) + all canaries + beat, write, notify on state change
       --beat           only write this host's scheduler beat (the second scheduler's entry)
       --dry-run        run canaries and classification, print, write and notify nothing
                        (read-only canaries only: memory and chat are skipped, pg is read-only)
       --report         latest results, imitable shares, monthly pass rate, row integrity
       --selftest       pure-logic assertions, no I/O
       --init-key       create the HMAC key in the secret store if it is missing
Tables: blood_test (one row per check per run), blood_canary (one signed row per canary key).
Probe rows: telemetry.probe_results probe='blood_<service>' (SLO-ready, same shape as nova_prober).
Schedule: scheduler-core every 30m (--scheduled); scheduler (mac-studio) every 30m (--beat).
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import re
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_dsn  # noqa: E402

SERVICE = "nova_blood_test"
KEY_NAME = "nova-blood-test-key"
CRITICAL = ("memory", "chat", "scheduler", "pg", "journal")

MEMORY_URL = "http://memory-server.digitalnoise.net:18790"
GATEWAY_URL = "http://nova-gw.digitalnoise.net:18792"
JOURNAL_URL = "https://nova.digitalnoise.net"
CANARY_SOURCE = "blood_test"            # memory source of the nonce memories (deleted after each run)

HTTP_TIMEOUT = 12
CHAT_TIMEOUT = 150                      # a full agent turn takes 8-30 s, more under load
BEAT_STALE_MIN = 75                     # beats are every 30 min: 2.5 intervals
BEAT_RETIRED_DAYS = 7                   # a beat this old is a retired host, not a dead scheduler
JOURNAL_MAX_AGE_H = 36                  # the journal builds several times a day
INVENTORY_EVERY_H = 20                  # classification rows: once a day
STATEMENT_TIMEOUT_MS = 20000

SCHEMA = """
CREATE TABLE IF NOT EXISTS blood_test (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  service text NOT NULL,
  "check" text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('imitable', 'blood')),
  ok boolean,
  latency_ms integer,
  detail text,
  src text NOT NULL DEFAULT 'canary',
  impostor boolean NOT NULL DEFAULT false,
  mac text,
  mac_kind text);
CREATE INDEX IF NOT EXISTS blood_test_ts ON blood_test (ts);
CREATE INDEX IF NOT EXISTS blood_test_service_ts ON blood_test (service, ts);
CREATE TABLE IF NOT EXISTS blood_canary (
  key text PRIMARY KEY,
  nonce text NOT NULL,
  ts timestamptz NOT NULL,
  host text,
  mac text NOT NULL,
  mac_kind text NOT NULL);
"""

# --------------------------------------------------------------- the classification catalogue
# (check, service, kind, why). Scripted checks Nova already runs, read from their code 2026-10-15.
# The mesh agent's per-service TCP/HTTP checks are added at run time from service_registry.
STATIC_CHECKS = (
    ("probe:memory_roundtrip", "memory", "blood", "uuid token written through /remember and read back from the DB by id (nova_prober)"),
    ("probe:embedding", "memory", "imitable", "any 768-float vector passes; a constant or cached vector would too"),
    ("selfcheck:svc-memory-server", "memory", "imitable", "GET /health says status ok"),
    ("selfcheck:memory-ingest", "memory", "imitable", "newest memory row is younger than 6h (a timestamp)"),
    ("deep:memory", "memory", "imitable", "/recall for a fixed phrase returns >= 1 memory (any old memory satisfies it)"),
    ("core_liveness:Memory server", "memory", "imitable", "TCP connect"),
    ("ntfy_canary:memory", "memory", "imitable", "TCP connect (nova_canary)"),
    ("probe:inference_vantage", "chat", "imitable", "the router's own healthy flags plus /api/version reachability"),
    ("selfcheck:svc-gateway-v2", "chat", "imitable", "/health ok:true and a local backend flagged healthy"),
    ("deep:gateway", "chat", "imitable", "/api/chat returns any non-empty reply to 'reply with one word' (a canned reply passes)"),
    ("core_liveness:Gateway", "chat", "imitable", "TCP connect"),
    ("ntfy_canary:gateway", "chat", "imitable", "TCP connect (nova_canary)"),
    ("ntfy_canary:ollama_inference", "chat", "imitable", "one-token generation, done=true; the token is never checked"),
    ("health:llm:*", "chat", "imitable", "one-token generation per LLM node; reply content never compared to a nonce (nova_llm_ping)"),
    ("health:task_sentinel", "scheduler", "imitable", "heartbeat row with status hard-coded 'up' whenever the sentinel runs"),
    ("selfcheck:heartbeats", "scheduler", "imitable", "mesh-agent heartbeat timestamps are fresh"),
    ("deep:scheduler", "scheduler", "imitable", "scheduler_runs shows more successes than failures (the scheduler's own report)"),
    ("slo:scheduled_jobs", "scheduler", "imitable", "ratio of scheduler_runs statuses the scheduler writes about itself"),
    ("core_liveness:Scheduler", "scheduler", "imitable", "TCP connect"),
    ("ntfy_canary:scheduler", "scheduler", "imitable", "TCP connect (nova_canary)"),
    ("probe:postgres", "pg", "imitable", "SELECT 1 (a read-only standby or a pooler answers it)"),
    ("selfcheck:pg-primary", "pg", "imitable", "SELECT 1 through pgbouncer"),
    ("selfcheck:replication", "pg", "imitable", "pg_stat_replication row count and replay lag"),
    ("deep:postgres", "pg", "imitable", "writable flag + an INSERT that is never read back"),
    ("core_liveness:PostgreSQL primary", "pg", "imitable", "TCP connect"),
    ("probe:http_endpoints", "journal", "imitable", "HTTP 200 and the page contains '<html' (a stale or error page passes)"),
    ("deep:journal", "journal", "imitable", "the operations feed has an <item> (any old item passes)"),
    ("health:core_liveness", "watchers", "imitable", "heartbeat row with status hard-coded 'up' whenever core_liveness runs"),
    ("gateway:/health", "chat", "imitable", "returns ok:true whenever the HTTP server thread answers"),
)
# the canaries this organ runs (also listed in the inventory)
CANARIES = (
    ("canary:nonce_recall", "memory", "nonce memory /remember -> /search -> delete"),
    ("canary:nonce_echo", "chat", "nonce echoed through gateway /api/chat -> agent -> router -> LLM"),
    ("canary:signed_beat", "scheduler", "every scheduler host's signed beat is fresh and verifies"),
    ("canary:nonce_write_read", "pg", "signed nonce written on the primary, read back on a second connection"),
    ("canary:build_marker", "journal", "feed lastBuildDate is recent and the newest post serves with its title"),
)
# service_registry name -> critical service (the mesh agent's checks on these are imitable)
REGISTRY_MAP = {"memory_server": "memory", "gateway": "chat", "inference_router": "chat", "ollama": "chat",
                "mlx_server": "chat", "llama_server": "chat", "scheduler": "scheduler", "scheduler-core": "scheduler",
                "postgresql": "pg", "postgresql_replica": "pg"}
# the imitable sibling run next to each canary: (service_registry name, expected substring) or special
SIBLINGS = {"memory": ("memory_server", '"status":"ok"'), "chat": ("gateway", '"ok": true'),
            "scheduler": ("scheduler-core", '"status": "running"')}


def log(m: str) -> None:
    print(f"[blood-test {datetime.now():%H:%M:%S}] {m}", flush=True)


# --------------------------------------------------------------- pure helpers

def make_nonce() -> str:
    return "bt" + secrets.token_hex(6)


def classify_registry(health_url: str | None) -> tuple[str, str]:
    """How the mesh agent checks a registered service -> (kind, why). Pure."""
    if not (health_url or "").strip():
        return "imitable", "TCP connect (mesh agent)"
    return "imitable", "HTTP status < 500 counts as up, 4xx included; body never read (mesh agent)"


def sign(key: bytes, *parts) -> str:
    """HMAC-SHA256 over the parts joined with '|'. Pure."""
    return hmac.new(key, "|".join(str(p) for p in parts).encode(), hashlib.sha256).hexdigest()


def chain(prev: str | None, *parts) -> str:
    """Hash-chain link: sha256(prev | parts). Pure."""
    return hashlib.sha256("|".join([prev or "genesis", *[str(p) for p in parts]]).encode()).hexdigest()


def mac_for(key: bytes | None, prev: str | None, *parts) -> tuple[str, str]:
    """-> (mac, mac_kind): HMAC when there is a key, else a chain link. Pure."""
    return (sign(key, *parts), "hmac") if key else (chain(prev, *parts), "chain")


def mac_ok(key: bytes | None, mac: str, kind: str, prev: str | None, *parts) -> bool | None:
    """Verify a stored mac. None when it cannot be checked (hmac row, no key here). Pure."""
    if kind == "hmac":
        return None if key is None else hmac.compare_digest(mac or "", sign(key, *parts))
    return hmac.compare_digest(mac or "", chain(prev, *parts))


def row_parts(ts: datetime, service, check, kind, ok, latency_ms, detail) -> tuple:
    """Canonical fields a blood_test row's mac covers. Pure."""
    return (ts.astimezone(timezone.utc).isoformat(), service, check, kind, ok, latency_ms, detail or "")


def echo_ok(nonce: str, response: str | None) -> bool:
    """Did the nonce come back? Case-insensitive, tolerant of markdown around it. Pure."""
    return bool(nonce) and nonce.lower() in (response or "").lower()


def parse_feed(xml: str) -> tuple[datetime | None, str | None, str | None]:
    """Hugo RSS -> (lastBuildDate, newest item link, newest item title). Pure."""
    built = None
    m = re.search(r"<lastBuildDate>([^<]+)</lastBuildDate>", xml or "")
    if m:
        try:
            built = parsedate_to_datetime(m.group(1).strip())
        except (TypeError, ValueError):
            built = None
    item = re.search(r"<item>(.*?)</item>", xml or "", re.S)
    link = title = None
    if item:
        lm = re.search(r"<link>([^<]+)</link>", item.group(1))
        tm = re.search(r"<title>(.*?)</title>", item.group(1), re.S)
        link = lm.group(1).strip() if lm else None
        title = html.unescape(tm.group(1).strip()) if tm else None
    return built, link, title


def beat_verdict(beats: list[dict], now: datetime, key: bytes | None,
                 stale_min: int = BEAT_STALE_MIN, retired_days: int = BEAT_RETIRED_DAYS) -> tuple[bool, str]:
    """beats: [{host, nonce, ts, mac, mac_kind}] (latest per host) -> (ok, detail). Pure."""
    if not beats:
        return True, "first beat: nothing to verify yet"
    bad, good, retired = [], [], []
    for b in beats:
        age = (now - b["ts"]).total_seconds() / 60
        if age > retired_days * 1440:
            retired.append(b["host"])
            continue
        v = mac_ok(key, b["mac"], b["mac_kind"], None, "beat", b["host"], b["nonce"], b["ts"].astimezone(timezone.utc).isoformat())
        if v is False:
            bad.append(f"{b['host']}: signature does not verify (beat forged or edited)")
        elif age > stale_min:
            bad.append(f"{b['host']}: last beat {age:.0f} min ago (> {stale_min})")
        else:
            good.append(f"{b['host']} {age:.0f}m" + ("" if v else " (unsigned check)"))
    tail = f"; retired: {', '.join(retired)}" if retired else ""
    if bad:
        return False, "; ".join(bad) + (f"; fresh: {', '.join(good)}" if good else "") + tail
    if not good:
        return True, "no live scheduler beats (all retired)" + tail
    return True, f"{len(good)} scheduler beat(s) fresh and verified: {', '.join(good)}" + tail


def inventory(registry_rows: list[tuple]) -> list[dict]:
    """Static catalogue + the mesh agent's registry checks + this organ's canaries. Pure."""
    out = [{"check": c, "service": s, "kind": k, "why": w} for c, s, k, w in STATIC_CHECKS]
    for name, node, health_url in registry_rows:
        svc = REGISTRY_MAP.get(name)
        if svc:
            kind, why = classify_registry(health_url)
            out.append({"check": f"health:{name}@{node}", "service": svc, "kind": kind, "why": why})
    out += [{"check": c, "service": s, "kind": "blood", "why": w} for c, s, w in CANARIES]
    return out


def imitable_share(rows: list[dict]) -> dict:
    """{service: {'checks', 'imitable', 'blood', 'share'}} over the critical services. Pure."""
    out = {}
    for s in CRITICAL:
        mine = [r for r in rows if r["service"] == s]
        imi = sum(1 for r in mine if r["kind"] == "imitable")
        out[s] = {"checks": len(mine), "imitable": imi, "blood": len(mine) - imi,
                  "share": round(imi / len(mine), 3) if mine else None}
    return out


def is_impostor(imitable_ok: bool | None, blood_ok: bool) -> bool:
    """The imitation passes its liveness check while the blood test fails. Pure."""
    return bool(imitable_ok) and not blood_ok


def monthly_stats(rows: list[tuple]) -> list[dict]:
    """rows: (month, service, total, passed, impostors) -> dicts with pass_pct. Pure."""
    return [{"month": m, "service": s, "runs": int(t), "passed": int(p), "impostors": int(i),
             "pass_pct": round(100 * p / t, 1) if t else None} for m, s, t, p, i in rows]


# --------------------------------------------------------------- I/O helpers

def _http(url: str, data: bytes | None = None, timeout: float = HTTP_TIMEOUT, attempts: int = 3,
          base: float = 0.5, method: str | None = None, _sleep=time.sleep) -> tuple[int, str]:
    """GET/POST with retry + backoff on transport errors (not on HTTP errors). -> (status, body)."""
    hdrs = {"User-Agent": "nova-blood-test/1.0"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.getcode(), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, (e.read() or b"").decode("utf-8", "replace")[:500]
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last = e
            if i < attempts - 1:
                _sleep(base * (2 ** i))
    raise last  # type: ignore[misc]


def connect(dbname: str = "nova_ops"):
    import psycopg2
    conn = psycopg2.connect(nova_dsn.pg_dsn(dbname, "connect_timeout=10"),
                            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
    conn.autocommit = True
    return conn


def ensure_schema(cur) -> None:
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, SCHEMA)


def load_key(create: bool = False) -> bytes | None:
    """The HMAC key from the fleet secret store; created there when asked and missing. None -> chain mode."""
    try:
        import nova_secrets
    except Exception:
        return None
    try:
        return nova_secrets.get_secret(KEY_NAME).encode()
    except KeyError:
        if not create:
            return None
        try:
            val = secrets.token_hex(32)
            nova_secrets.set_secret(KEY_NAME, val, note="nova_blood_test: HMAC key for blood canary values")
            return val.encode()
        except (Exception, SystemExit) as e:
            log(f"could not create {KEY_NAME}: {type(e).__name__}; using a hash chain")
            return None
    except (Exception, SystemExit):
        return None


def registry(cur) -> list[tuple]:
    cur.execute("SELECT service_name, node_name, health_url FROM service_registry ORDER BY 1, 2")
    return cur.fetchall()


def registry_url(cur, name: str) -> str | None:
    cur.execute("SELECT host(host)::text, port, health_url FROM service_registry WHERE service_name = %s "
                "ORDER BY node_name LIMIT 1", (name,))
    r = cur.fetchone()
    if not r:
        return None
    host, port, hu = r
    hu = (hu or "").strip()
    if hu.startswith("http"):
        return hu
    return f"http://{host}:{port}{hu or '/'}"


def sibling(cur, service: str) -> tuple[bool | None, str]:
    """Run the imitable check that stands next to a canary. -> (ok, detail)."""
    try:
        if service == "pg":
            cur.execute("SELECT 1")
            return cur.fetchone()[0] == 1, "SELECT 1"
        if service == "journal":
            st, body = _http(JOURNAL_URL + "/")
            return st == 200 and "<html" in body, f"GET / -> {st}"
        name, needle = SIBLINGS[service]
        url = registry_url(cur, name)
        if not url:
            return None, f"{name} not in service_registry"
        st, body = _http(url, attempts=1)
        return st == 200 and needle.replace(" ", "") in body.replace(" ", ""), f"GET {name} health -> {st}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# --------------------------------------------------------------- the canaries

def canary_memory(nonce: str, _http_fn=None, _mem_conn=None, _sleep=time.sleep) -> tuple[bool, str]:
    """remember a nonce memory, find it with /search, delete it (always)."""
    hf = _http_fn or _http
    text = (f"nova blood test canary memory, do not keep: nonce {nonce} written at "
            f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} to prove remember and recall both work")
    mem_id = None
    try:
        st, body = hf(MEMORY_URL + "/remember", data=json.dumps({"text": text, "source": CANARY_SOURCE}).encode(),
                      attempts=1)
        resp = json.loads(body) if st == 200 else {}
        mem_id = resp.get("id")
        if not mem_id or resp.get("status") not in (None, "stored"):
            return False, f"/remember -> {st} {str(resp or body)[:160]}"
        q = urllib.parse.urlencode({"q": nonce, "source": CANARY_SOURCE, "n": 3, "mode": "auto"})
        for i in range(4):                       # reads may land on a replica a moment behind
            st, body = hf(f"{MEMORY_URL}/search?{q}")
            found = st == 200 and any(nonce in (m.get("text") or "") for m in json.loads(body).get("memories", []))
            if found:
                return True, f"nonce memory recalled through /search (attempt {i + 1})"
            _sleep(1.5 * (i + 1))
        return False, f"wrote {mem_id} but /search never returned the nonce (last HTTP {st})"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        if mem_id:
            try:
                mc = _mem_conn() if _mem_conn else connect("nova_memories")
                with mc.cursor() as c:
                    c.execute("DELETE FROM memories WHERE id = %s AND source = %s", (mem_id, CANARY_SOURCE))
                mc.close()
            except Exception as e:
                log(f"cleanup of canary memory {mem_id} failed: {e}")


def canary_chat(cur, nonce: str, _http_fn=None) -> tuple[bool, str]:
    """Ask the full agent pipeline to echo the nonce, then delete the trace and session rows it made."""
    hf = _http_fn or _http
    session = f"gw2:hc:blood-{nonce}"
    msg = f"healthcheck: blood test. Reply with exactly this code and nothing else: {nonce}"
    trace = None
    try:
        st, body = hf(GATEWAY_URL + "/api/chat", data=json.dumps(
            {"message": msg, "session_id": session, "agent_id": "chat", "person": "service"}).encode(),
            timeout=CHAT_TIMEOUT, attempts=1)
        resp = json.loads(body) if body.strip().startswith("{") else {}
        trace = resp.get("trace_id")
        reply = (resp.get("response") or "").strip()
        if st != 200 or not resp.get("ok"):
            return False, f"/api/chat -> {st} {str(resp.get('error') or body)[:160]}"
        if echo_ok(nonce, reply):
            return True, f"nonce echoed (trace {trace})"
        return False, f"reply did not contain the nonce: {reply[:120]!r}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            if trace:
                cur.execute("DELETE FROM gateway_traces WHERE trace_id = %s AND channel = 'hc'", (trace,))
            cur.execute("DELETE FROM gateway_query_log WHERE session_id = %s", (session,))
            cur.execute("DELETE FROM gateway_sessions WHERE session_id = %s", (session,))
        except Exception as e:
            log(f"cleanup of chat canary rows failed: {e}")


def read_beats(cur) -> list[dict]:
    cur.execute("SELECT substr(key, 6), nonce, ts, mac, mac_kind FROM blood_canary WHERE key LIKE 'beat:%%'")
    return [{"host": h, "nonce": n, "ts": t, "mac": m, "mac_kind": k} for h, n, t, m, k in cur.fetchall()]


def write_beat(cur, key: bytes | None, host: str, now: datetime) -> None:
    nonce = make_nonce()
    mac, kind = mac_for(key, None, "beat", host, nonce, now.astimezone(timezone.utc).isoformat())
    cur.execute("INSERT INTO blood_canary (key, nonce, ts, host, mac, mac_kind) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (key) DO UPDATE SET nonce = EXCLUDED.nonce, ts = EXCLUDED.ts, host = EXCLUDED.host, "
                "mac = EXCLUDED.mac, mac_kind = EXCLUDED.mac_kind", (f"beat:{host}", nonce, now, host, mac, kind))


def canary_pg(cur, key: bytes | None, nonce: str, now: datetime, write: bool = True, _connect=None) -> tuple[bool, str]:
    """Signed nonce written on the primary, read back on a second connection. Read-only when write=False."""
    try:
        cur.execute("SELECT pg_is_in_recovery()")
        if cur.fetchone()[0]:
            return False, "the 'primary' is in recovery (a standby is answering as the primary)"
        mac, kind = mac_for(key, None, "pg", nonce, now.astimezone(timezone.utc).isoformat())
        if write:
            cur.execute("INSERT INTO blood_canary (key, nonce, ts, host, mac, mac_kind) VALUES ('pg', %s, %s, %s, %s, %s) "
                        "ON CONFLICT (key) DO UPDATE SET nonce = EXCLUDED.nonce, ts = EXCLUDED.ts, host = EXCLUDED.host, "
                        "mac = EXCLUDED.mac, mac_kind = EXCLUDED.mac_kind", (nonce, now, short_host(), mac, kind))
        c2 = (_connect or connect)()
        try:
            with c2.cursor() as k:
                k.execute("SELECT nonce, ts, mac, mac_kind FROM blood_canary WHERE key = 'pg'")
                row = k.fetchone()
        finally:
            c2.close()
        if not write:
            return (row is not None), ("read-only: canary row readable" if row else "read-only: no canary row yet")
        if not row or row[0] != nonce:
            return False, f"wrote nonce {nonce} but a second connection read {row[0] if row else None}"
        if mac_ok(key, row[2], row[3], None, "pg", row[0], row[1].astimezone(timezone.utc).isoformat()) is False:
            return False, "nonce read back but its signature does not verify"
        return True, "signed nonce written and read back on a second connection"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def canary_journal(now: datetime, _http_fn=None) -> tuple[bool, str]:
    hf = _http_fn or _http
    try:
        st, xml = hf(f"{JOURNAL_URL}/index.xml?blood={make_nonce()}")
        if st != 200:
            return False, f"feed -> HTTP {st}"
        built, link, title = parse_feed(xml)
        if not built:
            return False, "feed has no lastBuildDate"
        age_h = (now - built).total_seconds() / 3600
        if age_h > JOURNAL_MAX_AGE_H:
            return False, f"last build {age_h:.0f}h ago (> {JOURNAL_MAX_AGE_H}h): the site serves, the pipeline stopped"
        if not link or not title:
            return False, "feed has no newest item"
        st, page = hf(link)
        if st != 200 or title[:60] not in html.unescape(page):
            return False, f"newest post {link} -> HTTP {st}" + (", its title is not on the page" if st == 200 else "")
        return True, f"built {age_h:.1f}h ago; newest post serves with its title"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def short_host() -> str:
    return socket.gethostname().split(".")[0].lower()


# --------------------------------------------------------------- recording

def last_mac(cur) -> str | None:
    cur.execute("SELECT mac FROM blood_test WHERE mac_kind = 'chain' ORDER BY id DESC LIMIT 1")
    r = cur.fetchone()
    return r[0] if r else None


def record(cur, key, row: dict, prev: str | None) -> str:
    """Insert one blood_test row (signed or chained). -> its mac (the next chain link's prev)."""
    ts = row.get("ts") or datetime.now(timezone.utc)
    parts = row_parts(ts, row["service"], row["check"], row["kind"], row.get("ok"), row.get("latency_ms"), row.get("detail"))
    mac, kind = mac_for(key, prev, *parts)
    cur.execute('INSERT INTO blood_test (ts, service, "check", kind, ok, latency_ms, detail, src, impostor, mac, mac_kind) '
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (ts, row["service"], row["check"], row["kind"], row.get("ok"), row.get("latency_ms"),
                 (row.get("detail") or "")[:2000], row.get("src", "canary"), bool(row.get("impostor")), mac, kind))
    return mac if kind == "chain" else prev


def record_probe(cur, service: str, ok: bool, latency_ms: int, detail: str) -> None:
    cur.execute("INSERT INTO telemetry.probe_results (probe, ok, latency_ms, detail) VALUES (%s, %s, %s, %s)",
                (f"blood_{service}", ok, latency_ms, (detail or "")[:2000]))


def last_blood_ok(cur, service: str) -> bool | None:
    cur.execute("SELECT ok FROM blood_test WHERE service = %s AND src = 'canary' AND kind = 'blood' "
                "ORDER BY id DESC LIMIT 1", (service,))
    r = cur.fetchone()
    return None if r is None else bool(r[0])


def alert(row: dict, prev_ok: bool | None) -> bool:
    """State-change alerting, like nova_prober: quiet on steady state."""
    from nova_notify import notify
    s = row["service"]
    if row["ok"]:
        if prev_ok is False:
            return notify(f"Blood test passes again: {s}", body=row["detail"], level="info", category="blood_test",
                          dedup_key=f"blood:{s}:recovered", meta={"service": s})
        return False
    if prev_ok is False and not row.get("impostor"):
        return False
    if row.get("impostor"):
        return notify(f"IMPOSTOR: {s} passes its liveness check but failed the blood test",
                      body=(f"{row['check']}: {row['detail']}. Its imitable check ({row.get('sibling')}) says it is "
                            f"fine, so nothing else will page for this. The process is up; the work path is not."),
                      level="critical", category="blood_test", dedup_key=f"blood:{s}:impostor",
                      meta={"service": s, "check": row["check"], "sibling": row.get("sibling")})
    return notify(f"Blood test failed: {s}", body=f"{row['check']}: {row['detail']} (its liveness check fails too)",
                  level="warning", category="blood_test", dedup_key=f"blood:{s}:fail", meta={"service": s})


# --------------------------------------------------------------- run

def run_canaries(cur, key, write: bool, now: datetime | None = None, have_table: bool = True) -> list[dict]:
    """Every canary next to its imitable sibling. write=False: read-only (memory/chat skipped)."""
    now = now or datetime.now(timezone.utc)
    out = []
    plan = [("memory", "canary:nonce_recall"), ("chat", "canary:nonce_echo"), ("scheduler", "canary:signed_beat"),
            ("pg", "canary:nonce_write_read"), ("journal", "canary:build_marker")]
    beats = read_beats(cur) if have_table else None
    for service, check in plan:
        sib_ok, sib_detail = sibling(cur, service)
        nonce = make_nonce()
        t0 = time.monotonic()
        if service == "memory":
            ok, detail = canary_memory(nonce) if write else (None, "skipped in --dry-run (writes a memory)")
        elif service == "chat":
            ok, detail = canary_chat(cur, nonce) if write else (None, "skipped in --dry-run (runs an agent turn)")
        elif service == "scheduler":
            ok, detail = beat_verdict(beats, now, key) if beats is not None else (None, "no beats table yet")
        elif service == "pg":
            ok, detail = canary_pg(cur, key, nonce, now, write=write) if have_table else (None, "no canary table yet")
        else:
            ok, detail = canary_journal(now)
        lat = int((time.monotonic() - t0) * 1000)
        out.append({"service": service, "check": check, "kind": "blood", "ok": ok, "latency_ms": lat,
                    "detail": detail, "sibling_ok": sib_ok, "sibling": sib_detail,
                    "impostor": ok is not None and is_impostor(sib_ok, ok)})
    if write:
        write_beat(cur, key, short_host(), now)
    return out


def latest_inventory_age_h(cur) -> float | None:
    cur.execute("SELECT extract(epoch FROM now() - max(ts)) / 3600 FROM blood_test WHERE src = 'inventory'")
    r = cur.fetchone()
    return None if not r or r[0] is None else float(r[0])


def scheduled(cur, key, force_inventory: bool = False) -> dict:
    ensure_schema(cur)
    prev = last_mac(cur) if key is None else None
    inv = []
    age = latest_inventory_age_h(cur)
    if force_inventory or age is None or age >= INVENTORY_EVERY_H:
        inv = inventory(registry(cur))
        ts = datetime.now(timezone.utc)
        for r in inv:
            prev = record(cur, key, {**r, "ts": ts, "ok": None, "detail": r["why"], "src": "inventory"}, prev)
    results = run_canaries(cur, key, write=True)
    sent = 0
    for r in results:
        p = last_blood_ok(cur, r["service"])
        prev = record(cur, key, r, prev)
        record_probe(cur, r["service"], bool(r["ok"]), r["latency_ms"], r["detail"])
        try:
            sent += bool(alert(r, p))
        except Exception as e:
            log(f"notify failed for {r['service']} (result still recorded): {e}")
    return {"inventory": len(inv), "results": results, "notified": sent}


def print_results(results: list[dict], inv: list[dict]) -> None:
    for r in results:
        flag = "SKIP" if r["ok"] is None else ("PASS" if r["ok"] else "FAIL")
        imp = "  << IMPOSTOR" if r["impostor"] else ""
        sib = "-" if r["sibling_ok"] is None else ("ok" if r["sibling_ok"] else "fail")
        print(f"  [{flag}] {r['service']:<9} {r['check']:<24} {r['latency_ms']:>6}ms  imitable={sib:<4} {r['detail']}{imp}")
    if inv:
        print("\nImitable share of checks per critical service:")
        for s, v in imitable_share(inv).items():
            print(f"  {s:<9} {v['imitable']:>2}/{v['checks']:<2} imitable ({v['share']:.0%})  blood: {v['blood']}")


def verify_integrity(cur, key) -> tuple[int, int, int]:
    """-> (verified, failed, unverifiable) over every blood_test row."""
    cur.execute('SELECT ts, service, "check", kind, ok, latency_ms, detail, mac, mac_kind FROM blood_test ORDER BY id')
    good = bad = unk = 0
    prev = None
    for ts, s, c, k, ok, lat, d, mac, mk in cur.fetchall():
        v = mac_ok(key, mac, mk, prev, *row_parts(ts, s, c, k, ok, lat, d))
        if mk == "chain":
            prev = mac
        good, bad, unk = good + (v is True), bad + (v is False), unk + (v is None)
    return good, bad, unk


def report(cur, key) -> None:
    cur.execute("SELECT to_regclass('blood_test') IS NOT NULL")
    if not (cur.fetchone() or (False,))[0]:
        print("No blood tests yet (table not created; the first scheduled run creates it).")
        return
    cur.execute('SELECT DISTINCT ON (service) service, "check", ok, impostor, ts, detail FROM blood_test '
                "WHERE src = 'canary' ORDER BY service, id DESC")
    print("Latest blood canaries:")
    for s, c, ok, imp, ts, d in cur.fetchall():
        print(f"  {s:<9} {'PASS' if ok else 'FAIL'}{' IMPOSTOR' if imp else ''}  {ts:%Y-%m-%d %H:%M}  {c}: {d[:90]}")
    cur.execute("SELECT service, \"check\", kind FROM blood_test WHERE src = 'inventory' "
                "AND ts = (SELECT max(ts) FROM blood_test WHERE src = 'inventory')")
    inv = [{"service": s, "check": c, "kind": k} for s, c, k in cur.fetchall()]
    if inv:
        print("\nImitable share per critical service (latest classification):")
        for s, v in imitable_share(inv).items():
            if v["checks"]:
                print(f"  {s:<9} {v['share']:.0%}  ({v['imitable']} imitable / {v['blood']} blood)")
    cur.execute("SELECT to_char(date_trunc('month', ts), 'YYYY-MM'), service, count(*), count(*) FILTER (WHERE ok), "
                "count(*) FILTER (WHERE impostor) FROM blood_test WHERE src = 'canary' AND ok IS NOT NULL "
                "GROUP BY 1, 2 ORDER BY 1 DESC, 2 LIMIT 60")
    print("\nMonthly blood pass rate (continuity review):")
    for m in monthly_stats(cur.fetchall()):
        print(f"  {m['month']}  {m['service']:<9} {m['passed']:>5}/{m['runs']:<5} {m['pass_pct']}%  impostors {m['impostors']}")
    g, b, u = verify_integrity(cur, key)
    print(f"\nIntegrity: {g} rows verify, {b} FAIL, {u} unverifiable here (signed, no key on this host)")


def selftest() -> int:
    k = b"k" * 32
    m, kind = mac_for(k, None, "pg", "n1", "t")
    assert kind == "hmac" and mac_ok(k, m, kind, None, "pg", "n1", "t") is True
    assert mac_ok(k, m, kind, None, "pg", "n2", "t") is False
    c1, _ = mac_for(None, None, "a")
    assert mac_ok(None, c1, "chain", None, "a") and not mac_ok(None, c1, "chain", "x", "a")
    assert echo_ok("bt12ab", "**BT12AB**") and not echo_ok("bt12ab", "OK")
    now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
    ts = now - timedelta(minutes=10)
    bm, bk = mac_for(k, None, "beat", "h", "n", ts.isoformat())
    assert beat_verdict([{"host": "h", "nonce": "n", "ts": ts, "mac": bm, "mac_kind": bk}], now, k)[0]
    assert not beat_verdict([{"host": "h", "nonce": "x", "ts": ts, "mac": bm, "mac_kind": bk}], now, k)[0]
    assert is_impostor(True, False) and not is_impostor(False, False)
    sh = imitable_share(inventory([("gateway", "nova-core", "/health")]))
    assert 0 < sh["chat"]["share"] < 1 and sh["pg"]["blood"] == 1
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scheduled", action="store_true", help="classify (daily) + canaries + beat; write + notify")
    ap.add_argument("--beat", action="store_true", help="only write this host's signed scheduler beat")
    ap.add_argument("--dry-run", action="store_true", help="read-only canaries + classification; write nothing")
    ap.add_argument("--classify", action="store_true", help="with --scheduled: write the classification now")
    ap.add_argument("--report", action="store_true", help="latest results, imitable shares, monthly pass rate")
    ap.add_argument("--selftest", action="store_true", help="pure-logic assertions, no I/O")
    ap.add_argument("--init-key", action="store_true", help=f"create {KEY_NAME} in the secret store if missing")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.init_key:
        print("key: " + ("present (HMAC mode)" if load_key(create=True) else "unavailable (hash-chain mode)"))
        return 0
    if not (a.scheduled or a.beat or a.dry_run or a.report):
        ap.print_help()
        return 0
    conn = connect()
    cur = conn.cursor()
    try:
        if a.report:
            report(cur, load_key())
            return 0
        if a.beat:
            ensure_schema(cur)
            write_beat(cur, load_key(create=True), short_host(), datetime.now(timezone.utc))
            log(f"beat written for {short_host()}")
            return 0
        if a.dry_run:
            key = load_key()
            print(f"key mode: {'hmac' if key else 'hash chain (no key)'}")
            cur.execute("SELECT to_regclass('blood_canary') IS NOT NULL")
            have = (cur.fetchone() or (False,))[0]
            inv = inventory(registry(cur))
            results = run_canaries(cur, key, write=False, have_table=have)
            print_results(results, inv)
            print(f"\n[dry-run] {len(inv)} checks classified, {len(results)} canaries — nothing written")
            return 0
        out = scheduled(cur, load_key(create=True), force_inventory=a.classify)
        bad = [r["service"] for r in out["results"] if not r["ok"]]
        log(f"{len(out['results'])} canaries, failing: {', '.join(bad) or 'none'}; "
            f"{out['inventory']} checks classified; {out['notified']} notified")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
