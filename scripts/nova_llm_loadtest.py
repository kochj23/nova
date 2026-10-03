#!/usr/bin/env python3
"""
nova_llm_loadtest.py — load-test the local LLM fleet.

For each (node, model) endpoint: warm the model, measure single-stream latency +
tokens/sec, then fire a concurrent burst to measure aggregate throughput and how
it scales. Also exercises the .2 active/active fabric router to show load spread.

All endpoints are OpenAI-compatible (/v1/chat/completions). Read-only on the
models; generates short fixed-length completions.
"""
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PROMPT = "Explain in one detailed paragraph how a four-stroke engine works."
MAX_TOKENS = 100
CONCURRENCY = 4

# (label, url, model). MTPLX binds loopback-only on .6, so use 127.0.0.1.
# tinychat (.2:8000) is a web UI, not an API endpoint — dropped.
TARGETS = [
    (".6 ollama qwen3:30b",  "http://192.168.1.6:11434/v1/chat/completions",  "qwen3:30b-a3b"),
    (".6 MTPLX 27B",         "http://127.0.0.1:5050/v1/chat/completions",     "mtplx-qwen36-27b-optimized-speed"),
    (".190 ollama qwen3:30b","http://192.168.1.77:11434/v1/chat/completions","qwen3:30b-a3b"),
    (".7 llama3.2:3b (fast)","http://192.168.1.7:11434/v1/chat/completions",  "llama3.2:3b"),
    ("FABRIC conversation",  "http://192.168.1.2:37475/v1/chat/completions",  "conversation"),
    ("FABRIC fast→.7",       "http://192.168.1.2:37475/v1/chat/completions",  "fast"),
]


def call(url, model, timeout=300):
    """One completion. Returns (latency_s, completion_tokens, backend) or (None,err,None)."""
    body = json.dumps({"model": model, "stream": False, "max_tokens": MAX_TOKENS,
                       "temperature": 0.3,
                       "messages": [{"role": "user", "content": PROMPT}]}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            backend = r.headers.get("X-Nova-Backend", "")
            data = json.loads(r.read())
        dt = time.time() - t0
        toks = (data.get("usage") or {}).get("completion_tokens")
        if toks is None:  # some servers omit usage; estimate from text
            txt = data["choices"][0]["message"]["content"]
            toks = max(1, len(txt) // 4)
        return dt, toks, backend
    except Exception as e:
        return None, str(e)[:60], None


def bench(label, url, model):
    # warm-up (loads the model; not measured)
    w_dt, w_info, _ = call(url, model, timeout=420)
    if w_dt is None:
        return {"label": label, "model": model, "error": w_info}

    # single-stream: 3 sequential
    singles = []
    for _ in range(3):
        dt, toks, _ = call(url, model)
        if dt:
            singles.append(toks / dt)
    single_tps = sum(singles) / len(singles) if singles else 0
    single_lat = None
    dt, toks, _ = call(url, model)
    single_lat = dt

    # concurrent burst
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        results = list(ex.map(lambda _: call(url, model),
                              range(CONCURRENCY)))
    wall = time.time() - t0
    ok = [(dt, tk, be) for dt, tk, be in results if dt]
    total_toks = sum(tk for _, tk, _ in ok)
    agg_tps = total_toks / wall if wall else 0
    backends = sorted({be for _, _, be in ok if be})
    return {
        "label": label, "model": model,
        "single_tps": round(single_tps, 1),
        "single_lat": round(single_lat, 2) if single_lat else None,
        "concurrent_tps": round(agg_tps, 1),
        "concurrent_ok": f"{len(ok)}/{CONCURRENCY}",
        "speedup": round(agg_tps / single_tps, 2) if single_tps else 0,
        "backends": ",".join(b.replace("192.168.1.", ".") for b in backends),
    }


def main():
    print(f"=== LLM load test (prompt~{MAX_TOKENS} tok, concurrency {CONCURRENCY}) ===\n")
    rows = []
    for label, url, model in TARGETS:
        print(f"  testing {label} ...", flush=True)
        rows.append(bench(label, url, model))

    print(f"\n{'ENDPOINT':<24}{'single tok/s':>13}{'latency s':>11}"
          f"{'conc tok/s':>12}{'scale':>7}{'ok':>6}  spread")
    print("-" * 92)
    for r in rows:
        if r.get("error"):
            print(f"{r['label']:<24}  ERROR: {r['error']}")
            continue
        spread = f"  {r['backends']}" if r['backends'] else ""
        print(f"{r['label']:<24}{r['single_tps']:>13}{str(r['single_lat']):>11}"
              f"{r['concurrent_tps']:>12}{str(r['speedup'])+'x':>7}{r['concurrent_ok']:>6}{spread}")
    print("\nsingle tok/s = one-stream speed · conc tok/s = aggregate under "
          f"{CONCURRENCY} parallel · scale = concurrency speedup")


if __name__ == "__main__":
    main()
