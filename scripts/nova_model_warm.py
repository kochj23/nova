#!/usr/bin/env python3
"""nova_model_warm.py — keep the fleet's inference models RESIDENT, so a request never pays a model load.

Why (2026-10-05): the gateway's chat TTFT p50 was 31.8 s on ollama. Not switching — reloading: qwen3:8b expired
after Ollama's default 5-minute keep_alive on .6/.77/.252/.125/.7, and nova_llm_ping's ranking sent each turn to
whichever node pinged fastest, usually one where the model had just gone cold. Jordan: "we need to be better at
model switches."

The placement lives in service_config (service='nova_model_warm', key='placement'): {node_url: [models]}. Every run,
for every node, every model in its list is (re)asserted with keep_alive=-1 — an empty-prompt generate (or embed)
call loads it if cold and pins it if warm. Idempotent, no restarts, survives an Ollama restart on the next run.
Reports what it warmed to stdout; cold loads are logged so a chronic re-loader shows up.

  nova_model_warm.py            # assert placement (scheduler: every 10 min)
  nova_model_warm.py --status   # what is loaded where vs. what should be
ponytail: OLLAMA_MAX_LOADED_MODELS still caps how many stay resident per node (set per node, see agent_docs
nova-inference); this script pins within that cap and reports anything that won't stick.
"""
import json, os, sys, time, urllib.request
import psycopg2

DSN = os.environ.get("NOVA_OPS_DSN", "host=localhost dbname=nova_ops user=kochj")
# Order matters: models warm serially, so the tiny embed model goes FIRST — after an Ollama restart it used to wait
# behind ~4 min of big-model loads and the prober's embedding probe timed out (coagency #117, 2026-10-07).
DEFAULT_PLACEMENT = {                                   # seeded into service_config on first run; edit it there
    "http://192.168.1.6:11434":   ["nomic-embed-text:latest", "qwen3:8b", "nova:latest", "qwen3:30b-a3b", "deepseek-r1:8b", "qwen3-vl:4b"],  # Studio 512G
    "http://192.168.1.77:11434":  ["nomic-embed-text:latest", "qwen3:8b", "nova:latest"],        # M4 Pro mini 64G (30b-a3b = cold backup only)
    "http://192.168.1.7:11434":   ["llama3.2:3b", "qwen3:8b"],                                    # M2 Pro mini 32G
    "http://192.168.1.252:11434": ["llama3.2:3b", "qwen3:8b"],                                    # M1 mini 16G
    "http://192.168.1.5:11434":   ["llama3.2:3b", "qwen3:8b"],                                    # nova-core3 27G
    "http://192.168.1.86:11434":  ["llama3.2:3b", "qwen3:8b", "nomic-embed-text:latest"],        # nova-core2 26G
    "http://192.168.1.125:11434": ["llama3.2:3b", "qwen3:8b", "nomic-embed-text:latest"],        # nova-core7 27G
    "http://192.168.1.10:11434":  ["nomic-embed-text:latest"],                                    # nova-core5 15G, embeddings only
}


def log(m): print(f"[model-warm {time.strftime('%H:%M:%S')}] {m}", flush=True)


def _retry(fn, attempts=3, base=0.5):
    """Call fn() up to `attempts` times with exponential backoff (base, 2*base, ...). A timeout is not retried —
    it already spent the whole budget; a refused/reset connection or a 5xx is. The last error is re-raised."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if i == attempts - 1 or isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
                raise
            time.sleep(base * 2 ** i)


def _get(url, timeout=5):
    def once():
        with urllib.request.urlopen(url, timeout=timeout) as r: return json.load(r)
    return _retry(once)


def _post(url, body, timeout=600):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    def once():
        with urllib.request.urlopen(req, timeout=timeout) as r: return json.load(r)
    return _retry(once)


def placement(cur):
    cur.execute("SELECT value FROM service_config WHERE service='nova_model_warm' AND key='placement'")
    row = cur.fetchone()
    if row: return row[0]
    cur.execute("INSERT INTO service_config (service, key, value, updated_by) VALUES ('nova_model_warm','placement',%s,'nova_model_warm.py') ON CONFLICT DO NOTHING",
                (json.dumps(DEFAULT_PLACEMENT),))
    return DEFAULT_PLACEMENT


def warm(url, model):
    """Load+pin one model. Returns 'warm' (already resident), 'loaded' (cold load done) or 'fail: ...'."""
    try:
        loaded = {m["name"] for m in _get(f"{url}/api/ps").get("models", [])}
    except Exception as e:
        return f"fail: ps {e}"
    was_warm = model in loaded
    t0 = time.time()
    try:
        if "embed" in model:
            _post(f"{url}/api/embed", {"model": model, "input": "warm", "keep_alive": -1})
        else:
            _post(f"{url}/api/generate", {"model": model, "prompt": "", "keep_alive": -1})
    except Exception as e:
        return f"fail: {str(e)[:80]}"
    return "warm" if was_warm else f"loaded in {time.time()-t0:.0f}s"


def status(place):
    for url, models in place.items():
        try:
            loaded = {m["name"]: (m.get("expires_at") or "")[:19] for m in _get(f"{url}/api/ps").get("models", [])}
        except Exception as e:
            print(f"{url}: unreachable ({e})"); continue
        missing = [m for m in models if m not in loaded]
        unpinned = [m for m, exp in loaded.items() if m in models and not exp.startswith("2319") and exp]
        extra = [m for m in loaded if m not in models]
        print(f"{url}: loaded={sorted(loaded)} missing={missing} unpinned={unpinned} extra={extra}")


def main():
    conn = _retry(lambda: psycopg2.connect(DSN, connect_timeout=5)); conn.autocommit = True; cur = conn.cursor()
    place = placement(cur)
    if "--status" in sys.argv: status(place); return 0
    cold, fails = [], []
    for url, models in place.items():
        results = {m: warm(url, m) for m in models}
        log(f"{url.split('//')[1].split(':')[0]}: " + ", ".join(f"{m}={r}" for m, r in results.items()))
        cold += [f"{url} {m}" for m, r in results.items() if r.startswith("loaded")]
        fails += [f"{url} {m} ({r})" for m, r in results.items() if r.startswith("fail")]
    if cold: log(f"cold loads this run: {len(cold)} — {'; '.join(cold)}")
    if fails: log(f"FAILED to pin: {len(fails)} — {'; '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
