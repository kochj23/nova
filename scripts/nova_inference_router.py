#!/usr/bin/env python3
"""
nova_inference_router.py — Nova Inference Fabric router (true active/active).

One smart endpoint in front of every inference engine on every Mac. A request
names a model CLASS (or a concrete model); the router finds every healthy node
that actually hosts a model for that class and proxies to the LEAST-LOADED one,
so concurrent requests fan out and all boxes work at once — not active/standby.

Design goals (Jordan's original vision: "right prompt → right model"):
  * Capability-aware — models are CUSTOMIZED per node by its resources; nodes do
    NOT have to run the same models. A node joins a class pool only once its
    health probe confirms it actually serves that class's model.
  * True active/active — pick = fewest in-flight requests among healthy pool
    members (latency tie-break), so load spreads across .6 / .190 / .7.
  * Self-assembling — register the full intended pool now; each node lights up
    automatically as its model pulls finish.
  * Zero third-party deps — stdlib http.server + urllib, runs anywhere (.2).

Runs on .2 (no GPU) as the neutral front door. Callers (gateway, intent-router)
point at http://192.168.1.2:<PORT> instead of a specific box.

Endpoints:
  POST /v1/chat/completions   OpenAI-compatible; "model" may be a CLASS or a model
  POST /api/chat | /api/generate | /api/embeddings   Ollama-native, routed
  GET  /pool/status           per-node health + in-flight (for the dashboard)
  GET  /health                router liveness

Model field resolution:
  - a CLASS alias ("code","conversation","fast","reasoner","vision","embed",
    "mtplx","nova")  -> that pool
  - a concrete model name ("qwen3-coder:30b") -> the pools that serve it
  The upstream "model" is rewritten to the chosen node's actual model name.
"""
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 37475
PROBE_INTERVAL = 10          # seconds between backend health probes
PROBE_TIMEOUT = 4
PROXY_TIMEOUT = 600          # big models can be slow on cold load

# Keep Ollama models resident between requests so a 30B doesn't cold-load after
# Ollama's 5-min idle default — matches the convention in nova_vision_analyzer
# (30m). Injected into every routed Ollama payload that doesn't set its own.
KEEP_ALIVE = os.environ.get("NOVA_OLLAMA_KEEP_ALIVE", "30m")

# Opt-in Thompson-sampling routing: weight backend choice by learned reliability
# (Beta(ok+1,fail+1)) on top of least-loaded, so a flaky node gets explored down
# instead of receiving traffic on a tie. OFF by default — least-loaded stays the
# live path; ok/fail counts accrue regardless and are visible at /pool/status, so
# the bandit can be validated from real data before being switched on.
BANDIT = os.environ.get("NOVA_ROUTER_BANDIT", "") not in ("", "0", "false", "no")

# ── The fabric registry ──────────────────────────────────────────────────────
# class -> list of backends. Each backend: (host, port, kind, model).
# kind: "ollama" (:11434), "mtplx"/"mlx" (OpenAI on :5050), "tinychat" (:8000).
# Models are PER-NODE and need not match across nodes. A backend only serves a
# class once its health probe confirms the model is present (Ollama) / up (MLX).
N6, N190, N7, N2 = "192.168.1.6", "192.168.1.101", "192.168.1.7", "192.168.1.2"
N10 = "192.168.1.10"   # nova-core5 — no GPU, idle; serves CPU embeddings to offload the GPU nodes
N5, N86 = "192.168.1.5", "192.168.1.86"   # nova-core3 (NPU), nova-core2 (ROCm) — fast-tier backups

POOLS = {
    # code: qwen3:30b-a3b (fast MoE) on both big nodes. qwen3-coder:30b is broken
    # (loads but hangs generation, 2026-06-23) — routed around until re-pulled.
    "code":         [(N6, 11434, "ollama", "qwen3:30b-a3b"),
                     (N190, 11434, "ollama", "qwen3:30b-a3b")],
    # quality general chat — the two fast 30B nodes only (kept .7 OUT so a 3B
    # never answers a quality-chat request; .7 serves the 'fast' tier instead)
    "conversation": [(N6, 11434, "ollama", "qwen3:30b-a3b"),
                     (N190, 11434, "ollama", "qwen3:30b-a3b")],
    # Nova's persona voice
    "nova":         [(N6, 11434, "ollama", "nova:latest")],
    # fast / cheap chat — .7's light tier. llama3.2:3b (~25 tok/s on the M2 Pro);
    # qwen3:8b was a chronic 4 tok/s straggler there, dropped. (tinychat is a UI,
    # not an API — removed.)
    # fast / cheap chat. .7 is primary; .5 (NPU) + .86 (ROCm) are backups so a sustained
    # burst spreads instead of collapsing .7 (load test 2026-07-14). All run llama3.2:3b.
    "fast":         [(N7, 11434, "ollama", "llama3.2:3b"),
                     (N5, 11434, "ollama", "llama3.2:3b"),
                     (N86, 11434, "ollama", "llama3.2:3b")],
    # low-latency single-stream — MTPLX speculative decoding
    "mtplx":        [(N6, 5050, "mtplx", "mtplx-qwen36-27b-optimized-speed"),
                     (N190, 5050, "mtplx", "mtplx-qwen36-27b-optimized-speed")],
    # reasoning — deepseek-r1 on .6
    "reasoner":     [(N6, 11434, "ollama", "deepseek-r1:8b")],
    # vision
    "vision":       [(N6, 11434, "ollama", "qwen3-vl:4b")],
    # embeddings — DEDICATED to .10 (idle CPU node) so every embed offloads the
    # GPUs entirely, keeping .6/.190/.7 free for generation. nomic is tiny and
    # fast on CPU. (.10 is reliable always-on infra; if it's ever down the
    # watchdog alerts.)
    "embed":        [(N10, 11434, "ollama", "nomic-embed-text:latest")],
}

# Per-backend inflight ceiling. .7 (M2 Pro) collapses past ~2 concurrent (load test
# 2026-07-14: fell to 6 tok/s, 114s p95, timeouts at conc 6) — cap it so the router
# spreads the fast tier to .5/.86 instead of piling on. Others get a high default.
MAX_INFLIGHT = {(N7, 11434): 2}
_DEFAULT_INFLIGHT_CAP = 32

# ── Backend health/load state ────────────────────────────────────────────────
_lock = threading.Lock()
_state = {}   # (host,port) -> {"healthy":bool,"models":set,"inflight":int,"lat":float}


def _all_backends():
    seen = {}
    for pool in POOLS.values():
        for host, port, kind, model in pool:
            seen[(host, port)] = kind
    return seen  # (host,port)->kind


def _probe(host, port, kind):
    """Return (healthy, models_set). Confirms a node actually serves its models."""
    try:
        if kind == "ollama":
            url = f"http://{host}:{port}/api/tags"
            with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as r:
                data = json.loads(r.read())
            return True, {m["name"] for m in data.get("models", [])}
        else:  # mtplx / mlx / tinychat — OpenAI-style /v1/models or root
            for path in ("/v1/models", "/health", "/"):
                try:
                    with urllib.request.urlopen(f"http://{host}:{port}{path}",
                                                timeout=PROBE_TIMEOUT) as r:
                        r.read(64)
                        return True, set()   # model identity trusted from registry
                except urllib.error.HTTPError:
                    return True, set()       # 404 still means the port is alive
                except Exception:
                    continue
        return False, set()
    except Exception:
        return False, set()


def _health_loop():
    backends = _all_backends()
    while True:
        for (host, port), kind in backends.items():
            healthy, models = _probe(host, port, kind)
            t0 = time.time()
            with _lock:
                st = _state.setdefault((host, port),
                                       {"healthy": False, "models": set(),
                                        "inflight": 0, "lat": 0.0, "gen_lat": 2.0,
                                        "ok": 0, "fail": 0, "kind": kind})
                st["healthy"], st["models"] = healthy, models
                st["lat"] = (time.time() - t0) * 1000
        time.sleep(PROBE_INTERVAL)


def _eligible(pool):
    """Healthy backends in pool that actually host their model (Ollama) / are up."""
    out = []
    with _lock:
        for host, port, kind, model in pool:
            st = _state.get((host, port))
            if not st or not st["healthy"]:
                continue
            if kind == "ollama" and model not in st["models"]:
                continue   # model not pulled here yet — skip until it lands
            if st["inflight"] >= MAX_INFLIGHT.get((host, port), _DEFAULT_INFLIGHT_CAP):
                continue   # at its concurrency ceiling — let another backend take it
            out.append((host, port, kind, model, st["inflight"],
                        st.get("gen_lat", 2.0),    # observed request latency (EWMA)
                        st.get("ok", 0), st.get("fail", 0)))
    return out


def _score(b):
    """Lower is better. Base = (inflight+1)*gen_lat (least-loaded, latency-aware).

    With BANDIT on, divide by a Thompson sample of reliability ~ Beta(ok+1,fail+1)
    so a backend that has been failing gets a probabilistically worse score and is
    explored down; a clean backend samples near 1.0 and keeps its least-loaded score.
    """
    inflight, gen_lat, ok, fail = b[4], b[5], b[6], b[7]
    base = (inflight + 1) * gen_lat
    if BANDIT:
        rel = random.betavariate(ok + 1, fail + 1)   # sampled per decision
        return base / max(rel, 0.05)
    return base


def _pick(pool):
    """Latency-aware least-loaded (Thompson-weighted when BANDIT is on).

    Least-loaded spreads load active/active across fast peers and stops a slow
    node (high gen-latency) from being handed concurrent traffic it would
    straggle on — it only receives a request once the fast nodes are loaded
    enough that (inflight+1)*fast_lat exceeds the slow node's latency.
    """
    cand = _eligible(pool)
    if not cand:
        return None
    cand.sort(key=_score)
    return cand[0][:4]                            # (host,port,kind,model)


def _resolve(model_field):
    """Map the request's 'model' to a pool. Class alias, or concrete model name."""
    if model_field in POOLS:
        return model_field, POOLS[model_field]
    # concrete model name -> any pool that lists it
    for cls, pool in POOLS.items():
        if any(m == model_field for _, _, _, m in pool):
            return cls, pool
    return None, None


def route(model_field):
    cls, pool = _resolve(model_field or "conversation")
    if not pool:
        return None, f"unknown model/class: {model_field!r}"
    chosen = _pick(pool)
    if not chosen:
        return None, f"no healthy backend for class {cls!r}"
    return chosen, cls


# ── HTTP server ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):   # quiet default logging
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True, "service": "nova-inference-router"})
        if self.path.startswith("/pool/status"):
            with _lock:
                snap = {f"{h}:{p}": {"healthy": s["healthy"], "inflight": s["inflight"],
                                     "lat_ms": round(s["lat"], 1), "kind": s["kind"],
                                     "ok": s.get("ok", 0), "fail": s.get("fail", 0),
                                     "models": sorted(s["models"])[:12]}
                        for (h, p), s in _state.items()}
            pools = {cls: [f"{h}:{p}->{m}" for h, p, k, m in pool]
                     for cls, pool in POOLS.items()}
            return self._json(200, {"backends": snap, "bandit": BANDIT, "pools": pools})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            return self._json(400, {"error": "bad json"})

        chosen, info = route(payload.get("model"))
        if not chosen:
            return self._json(503, {"error": info})
        host, port, kind, model = chosen
        payload["model"] = model                 # rewrite to node's actual model
        if kind == "ollama":                     # keep the model resident between calls
            payload.setdefault("keep_alive", KEEP_ALIVE)
        # OpenAI path for mtplx/mlx; Ollama-native path otherwise
        if kind in ("mtplx", "mlx"):
            up_path = "/v1/chat/completions"
        elif kind == "tinychat":
            up_path = "/v1/chat/completions"
        else:
            up_path = self.path                   # /api/chat, /api/generate, /v1/...
        self._proxy(host, port, up_path, payload, model)

    def _proxy(self, host, port, up_path, payload, model):
        key = (host, port)
        with _lock:
            _state.setdefault(key, {"healthy": True, "models": set(), "inflight": 0,
                                    "lat": 0.0, "gen_lat": 2.0, "ok": 0, "fail": 0,
                                    "kind": ""})["inflight"] += 1
        t0 = time.time()
        try:
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"http://{host}:{port}{up_path}", data=body,
                headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=PROXY_TIMEOUT)
            data = resp.read()                     # buffered relay — correct + simple
            with _lock:                            # learn this backend's real speed
                st = _state[key]
                st["gen_lat"] = 0.6 * st.get("gen_lat", 2.0) + 0.4 * (time.time() - t0)
                st["ok"] = st.get("ok", 0) + 1     # reliability signal for the bandit
            self.send_response(resp.status)
            self.send_header("Content-Type",
                             resp.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Nova-Backend", f"{host}:{port}")
            self.send_header("X-Nova-Model", model)
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            with _lock:
                _state[key]["fail"] = _state[key].get("fail", 0) + 1
            self._json(e.code, {"error": f"backend {host}:{port}: {e.reason}"})
        except Exception as e:
            with _lock:
                _state[key]["fail"] = _state[key].get("fail", 0) + 1
            self._json(502, {"error": f"backend {host}:{port} unreachable: {e}"})
        finally:
            with _lock:
                _state[key]["inflight"] = max(0, _state[key]["inflight"] - 1)


def main():
    threading.Thread(target=_health_loop, daemon=True).start()
    time.sleep(0.5)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[nova-router] active/active inference fabric on :{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
