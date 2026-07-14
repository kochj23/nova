#!/usr/bin/env python3
"""nova_fleet_loadtest.py — whole-fleet inference load test (ramp + soak, local-only).

Ramp: for each target, fire bursts at increasing concurrency and record aggregate
tokens/sec, p50/p95 latency, and error rate — revealing each node's ceiling and the
cluster's aggregate max. Soak: sustained moderate load over N minutes to expose thermal
throttling and error drift. Chat uses qwen3:8b (common to .6/.190/.7/.86); .5 uses its
own model. Embed (nomic-embed) runs on all six nodes. The .2 fabric router has no cloud
backend, so everything here is on-prem — no OpenRouter spillover.
Written by Jordan Koch (via Claude). Run from .6 (MTPLX is loopback there).
"""
import argparse, json, statistics, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

PROMPT = "Write a detailed paragraph explaining how a radio trunking system assigns talkgroups."
MAX_TOK = 128

# (label, url, model, kind)  kind in {chat, embed}
CHAT = [
    (".6 ollama",   "http://192.168.1.6:11434/v1/chat/completions",   "qwen3:8b",     "chat"),
    (".190 ollama",  "http://192.168.1.190:11434/v1/chat/completions", "qwen3:8b",     "chat"),
    (".7 ollama",    "http://192.168.1.7:11434/v1/chat/completions",   "qwen3:8b",     "chat"),
    (".86 ROCm",     "http://192.168.1.86:11434/v1/chat/completions",  "qwen3:8b",     "chat"),
    (".5 NPU",       "http://192.168.1.5:11434/v1/chat/completions",   "llama3.2:3b",  "chat"),
    ("FABRIC conv",  "http://192.168.1.2:37475/v1/chat/completions",   "conversation", "chat"),
    ("FABRIC fast",  "http://192.168.1.2:37475/v1/chat/completions",   "fast",         "chat"),
]
EMBED = [(f".{h.split('.')[-1]} embed", f"http://{h}:11434/api/embeddings", "nomic-embed-text", "embed")
         for h in ["192.168.1.6", "192.168.1.190", "192.168.1.7", "192.168.1.86", "192.168.1.5", "192.168.1.10"]]


def _one(url, model, kind, timeout=120):
    t0 = time.time()
    try:
        if kind == "chat":
            body = {"model": model, "messages": [{"role": "user", "content": PROMPT}],
                    "max_tokens": MAX_TOK, "temperature": 0.7, "stream": False}
        else:
            body = {"model": model, "prompt": PROMPT}
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        dt = time.time() - t0
        if kind == "chat":
            tok = d.get("usage", {}).get("completion_tokens") or MAX_TOK
            return True, dt, tok
        return True, dt, 1  # embed = 1 op
    except Exception:
        return False, time.time() - t0, 0


def burst(url, model, kind, concurrency, reps):
    """Run concurrency*reps requests; return aggregate stats."""
    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(_one, url, model, kind) for _ in range(concurrency * reps)]
        for f in as_completed(futs):
            results.append(f.result())
    wall = time.time() - t0
    ok = [r for r in results if r[0]]
    lat = sorted(r[1] for r in ok) or [0]
    toks = sum(r[2] for r in ok)
    return {
        "conc": concurrency, "n": len(results), "ok": len(ok), "err": len(results) - len(ok),
        "wall": wall,
        "tok_s": round(toks / wall, 1) if wall else 0,
        "p50": round(lat[len(lat)//2], 2), "p95": round(lat[min(len(lat)-1, int(len(lat)*0.95))], 2),
    }


def ramp(targets, levels, reps):
    print(f"\n=== RAMP (reps={reps}/level, MAX_TOK={MAX_TOK}) ===")
    print(f"{'target':16} {'conc':>4} {'ok/n':>7} {'tok/s':>8} {'p50':>7} {'p95':>7}")
    for label, url, model, kind in targets:
        best = 0
        for c in levels:
            s = burst(url, model, kind, c, reps)
            unit = "tok/s" if kind == "chat" else "emb/s"
            val = s["tok_s"] if kind == "chat" else round(s["ok"]/s["wall"], 1)
            best = max(best, val)
            flag = " ⚠ERR" if s["err"] else ""
            print(f"{label:16} {c:>4} {str(s['ok'])+'/'+str(s['n']):>7} {val:>8} {s['p50']:>7} {s['p95']:>7}{flag}")
            if s["err"] > s["n"] // 2:  # >50% errors — node saturated/down, stop ramping it
                print(f"{label:16}  (saturated — stopping ramp)"); break
        print(f"{label:16}  → ceiling ≈ {best} {unit}")


def soak(targets, concurrency, minutes):
    print(f"\n=== SOAK (concurrency={concurrency}, {minutes}min) ===")
    end = time.time() + minutes * 60
    samples = {t[0]: [] for t in targets}
    tick = 0
    while time.time() < end:
        for label, url, model, kind in targets:
            s = burst(url, model, kind, concurrency, 1)
            val = s["tok_s"] if kind == "chat" else round(s["ok"]/max(s["wall"], 0.01), 1)
            samples[label].append((val, s["p95"], s["err"]))
        tick += 1
        if tick % 3 == 0:
            elapsed = int((time.time() - (end - minutes*60)))
            line = "  ".join(f"{l.split()[0]}:{samples[l][-1][0]}" for l in samples)
            print(f"  t+{elapsed}s  {line}", flush=True)
    print("  --- soak summary (median tok|emb-per-s, worst p95, total errors) ---")
    for l, s in samples.items():
        vals = [x[0] for x in s]; errs = sum(x[2] for x in s); wp95 = max((x[1] for x in s), default=0)
        first, last = statistics.median(vals[:max(1,len(vals)//3)]), statistics.median(vals[-max(1,len(vals)//3):])
        drift = round((last-first)/first*100, 1) if first else 0
        print(f"  {l:16} median={round(statistics.median(vals),1):>7}  worst_p95={wp95:>6}s  errors={errs}  drift={drift:+}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["ramp", "soak", "embed"], default="ramp")
    ap.add_argument("--levels", default="1,2,4,8", help="concurrency levels for ramp")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--conc", type=int, default=6)
    ap.add_argument("--minutes", type=int, default=15)
    a = ap.parse_args()
    levels = [int(x) for x in a.levels.split(",")]
    if a.mode == "ramp":
        ramp(CHAT, levels, a.reps); ramp(EMBED, levels, a.reps)
    elif a.mode == "embed":
        ramp(EMBED, levels, a.reps)
    else:
        soak(CHAT[:5] + [EMBED[0]], a.conc, a.minutes)
