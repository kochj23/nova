#!/usr/bin/env python3
"""
nova_llm_ping.py — "ping" every LLM server in the fleet with a REAL one-token generation
(Jordan 2026-09-26: "regularly send ping to all of the nova servers that have LLMs running…
prioritize the LLMs that are actually working with models running. We are supposed to be
fully redundant").

A port answering /api/tags is not a working LLM. This probe asks each node to actually
generate, measures the latency, and publishes a RANKING that the gateway router reads to
choose its Ollama / MLX target (nova_gateway/router.py::_best_url). Side effect: the ping
keeps models warm (keep_alive) so the first real chat isn't a cold load.

Writes: health_checks (service_name='llm:<node>', checked_by='nova_llm_ping'),
        service_config (service='nova_llm_ping', key='ranking'),
        telemetry.events via nova_notify on down/slow (dedup 'llm-ping:<node>') + recovery.

  nova_llm_ping.py            # probe all, write ranking + health rows
  nova_llm_ping.py --dry-run  # probe, print, write nothing
  nova_llm_ping.py --selftest # pure-logic assertions
"""
import argparse
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
CHECKED_BY = "nova_llm_ping"
CHAT_MODEL = "qwen3:8b"          # what the gateway asks Ollama for (router.py _DEFAULT_MODELS)
SLOW_MS = 8000
DEAD_MS = 30000
TAGS_TIMEOUT = 5
GEN_TIMEOUT = 35

# (node, kind, url) — every LLM endpoint on the fleet (system map 2026-09-24)
ENDPOINTS = [
    ("nova-core8/.6",   "ollama",   "http://192.168.1.6:11434"),
    ("nova-core10/.77", "ollama",   "http://192.168.1.77:11434"),
    ("nova-core2/.86",  "ollama",   "http://192.168.1.86:11434"),
    ("nova-core3/.5",   "ollama",   "http://192.168.1.5:11434"),
    ("nova-core5/.10",  "ollama",   "http://192.168.1.10:11434"),
    ("nova-core7/.125", "ollama",   "http://192.168.1.125:11434"),
    ("nova-core9/.7",   "ollama",   "http://192.168.1.7:11434"),
    ("nova-core6/.252", "ollama",   "http://192.168.1.252:11434"),
    ("nova-core8/.6",   "mlx",      "http://192.168.1.6:5050"),
    ("nova-core10/.77", "mlx",      "http://192.168.1.77:5050"),
    ("nova-core8/.6",   "llamacpp", "http://192.168.1.6:11435"),
]


def log(m):
    print(f"[llm-ping {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def _get(url, timeout):
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
        return json.load(r)


def _post(url, payload, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ── pure logic (covered by --selftest) ─────────────────────────────────────────
def classify(ok: bool, latency_ms: int | None) -> str:
    if not ok or latency_ms is None:
        return "down"
    if latency_ms >= DEAD_MS:
        return "down"
    if latency_ms >= SLOW_MS:
        return "slow"
    return "up"


def rank(results: list) -> dict:
    """results: dicts with node, kind, url, status, latency_ms, has_chat_model, loaded.
    Ranking per kind: up before slow before down; then chat-model-capable first; then fastest."""
    order = {"up": 0, "slow": 1, "down": 2}
    out = {}
    for kind in sorted({r["kind"] for r in results}):
        rows = [r for r in results if r["kind"] == kind]
        rows.sort(key=lambda r: (order.get(r["status"], 3), not r.get("has_chat_model", False),
                                 r.get("latency_ms") if r.get("latency_ms") is not None else 10**9))
        out[kind] = [{k: r.get(k) for k in ("node", "url", "status", "latency_ms", "has_chat_model", "model", "loaded")} for r in rows]
    return out


def pick_model(available: list, loaded: list) -> str | None:
    """Prefer the gateway's chat model if the node has it; else whatever is already loaded;
    else the smallest available (so a CPU box isn't forced to load 45 GB just to say 'ping')."""
    names = [m.get("name") or m.get("model") for m in available]
    if CHAT_MODEL in names:
        return CHAT_MODEL
    if loaded:
        return loaded[0].get("name") or loaded[0].get("model")
    if available:
        return sorted(available, key=lambda m: m.get("size", 10**15))[0].get("name")
    return None


# ── probes ─────────────────────────────────────────────────────────────────────
def probe(ep):
    node, kind, url = ep
    r = {"node": node, "kind": kind, "url": url, "ok": False, "latency_ms": None,
         "has_chat_model": False, "model": None, "loaded": [], "error": ""}
    try:
        if kind == "ollama":
            tags = _get(f"{url}/api/tags", TAGS_TIMEOUT).get("models", [])
            try:
                ps = _get(f"{url}/api/ps", TAGS_TIMEOUT).get("models", [])
            except Exception:
                ps = []
            r["loaded"] = [m.get("name") for m in ps]
            r["has_chat_model"] = any((m.get("name") or "") == CHAT_MODEL for m in tags)
            model = pick_model(tags, ps)
            if not model:
                r["error"] = "no models"; return r
            r["model"] = model
            t0 = time.time()
            _post(f"{url}/api/generate", {"model": model, "prompt": "ping", "stream": False,
                                           "think": False, "keep_alive": "15m",
                                           "options": {"num_predict": 1}}, GEN_TIMEOUT)
            r["latency_ms"] = int((time.time() - t0) * 1000); r["ok"] = True
        else:
            models = _get(f"{url}/v1/models", TAGS_TIMEOUT).get("data", [])
            model = (models[0].get("id") if models else None)
            if not model:
                r["error"] = "no models"; return r
            r["model"] = model; r["has_chat_model"] = True; r["loaded"] = [model]
            t0 = time.time()
            _post(f"{url}/v1/chat/completions", {"model": model, "max_tokens": 1,
                                                  "messages": [{"role": "user", "content": "ping"}]}, GEN_TIMEOUT)
            r["latency_ms"] = int((time.time() - t0) * 1000); r["ok"] = True
    except Exception as e:  # noqa: BLE001
        r["error"] = str(e)[:160]
    r["status"] = classify(r["ok"], r["latency_ms"])
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as ex:
        results = list(ex.map(probe, ENDPOINTS))
    for r in results:
        log(f"{r['kind']:8} {r['node']:16} {r['status']:5} {str(r['latency_ms'])+'ms' if r['latency_ms'] is not None else '-':>8}  model={r['model']}  loaded={len(r['loaded'])}  {r['error']}")
    ranking = rank(results); ranking["ts"] = datetime.now().isoformat(timespec="seconds"); ranking["chat_model"] = CHAT_MODEL
    if args.dry_run:
        print(json.dumps({k: v for k, v in ranking.items() if k != "ts"}, indent=1)[:1500]); return 0
    import psycopg2
    conn = psycopg2.connect(OPS_DSN, connect_timeout=5); conn.autocommit = True; cur = conn.cursor()
    # previous statuses for recovery detection
    cur.execute("SELECT value FROM service_config WHERE service=%s AND key='ranking'", (CHECKED_BY,))
    row = cur.fetchone(); prev = {}
    if row and row[0]:
        v = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        for kind, rows in v.items():
            if isinstance(rows, list):
                for x in rows: prev[(kind, x.get("node"))] = x.get("status")
    for r in results:
        cur.execute("""INSERT INTO health_checks (service_name, node_name, checked_by, status, latency_ms, error_message, checked_at)
                       VALUES (%s,%s,%s,%s,%s,%s,now())""",
                    (f"llm:{r['kind']}", r["node"], CHECKED_BY, r["status"], r["latency_ms"], r["error"] or None))
    cur.execute("""INSERT INTO service_config (service, key, value, updated_at, updated_by) VALUES (%s,'ranking',%s::jsonb,now(),%s)
                   ON CONFLICT (service, key) DO UPDATE SET value=EXCLUDED.value, updated_at=now(), updated_by=EXCLUDED.updated_by""",
                (CHECKED_BY, json.dumps(ranking), CHECKED_BY))
    try:
        import nova_notify
        for r in results:
            key = f"llm-ping:{r['kind']}:{r['node']}"
            was = prev.get((r["kind"], r["node"]))
            if r["status"] in ("down", "slow"):
                nova_notify.notify(title=f"LLM {r['status'].upper()}: {r['kind']} on {r['node']}",
                                   body=f"{r['url']} — {'no answer: '+r['error'] if r['status']=='down' else str(r['latency_ms'])+' ms for one token'} (model {r['model']}). The gateway will route around it.",
                                   level="warning", category="fleet", source=CHECKED_BY, dedup_key=key)
            elif was in ("down", "slow"):
                nova_notify.notify(title=f"LLM recovered: {r['kind']} on {r['node']}", body=f"{r['latency_ms']} ms for one token (model {r['model']})",
                                   level="info", category="fleet", source=CHECKED_BY, dedup_key=key + ":recovered")
    except Exception as e:  # noqa: BLE001
        log(f"notify skipped: {e}")
    best = {k: (v[0]["node"] if v else None) for k, v in ranking.items() if isinstance(v, list)}
    log(f"ranking written; best per kind: {best}")
    return 0


def demo():
    assert classify(True, 500) == "up" and classify(True, 9000) == "slow" and classify(True, 31000) == "down" and classify(False, None) == "down"
    res = [{"node": "cpu", "kind": "ollama", "url": "u1", "status": "up", "latency_ms": 5000, "has_chat_model": True},
           {"node": "gpu", "kind": "ollama", "url": "u2", "status": "up", "latency_ms": 300, "has_chat_model": True},
           {"node": "tiny", "kind": "ollama", "url": "u3", "status": "up", "latency_ms": 100, "has_chat_model": False},
           {"node": "dead", "kind": "ollama", "url": "u4", "status": "down", "latency_ms": None, "has_chat_model": True},
           {"node": "m", "kind": "mlx", "url": "u5", "status": "slow", "latency_ms": 9000, "has_chat_model": True}]
    rk = rank(res)
    assert [x["node"] for x in rk["ollama"]] == ["gpu", "cpu", "tiny", "dead"], rk
    assert rk["mlx"][0]["node"] == "m"
    assert pick_model([{"name": "qwen3:8b", "size": 5}, {"name": "x", "size": 1}], []) == "qwen3:8b"
    assert pick_model([{"name": "big", "size": 50}, {"name": "small", "size": 1}], [{"name": "big"}]) == "big"
    assert pick_model([{"name": "big", "size": 50}, {"name": "small", "size": 1}], []) == "small"
    assert pick_model([], []) is None
    print("all llm-ping assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        demo()
    else:
        sys.exit(main())
