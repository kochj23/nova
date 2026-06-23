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

# ── The fabric registry ──────────────────────────────────────────────────────
# class -> list of backends. Each backend: (host, port, kind, model).
# kind: "ollama" (:11434), "mtplx"/"mlx" (OpenAI on :5050), "tinychat" (:8000).
# Models are PER-NODE and need not match across nodes. A backend only serves a
# class once its health probe confirms the model is present (Ollama) / up (MLX).
N6, N190, N7, N2 = "192.168.1.6", "192.168.1.190", "192.168.1.7", "192.168.1.2"

POOLS = {
    # heavy code — big nodes only
    "code":         [(N6, 11434, "ollama", "qwen3-coder:30b"),
                     (N190, 11434, "ollama", "qwen3-coder:30b")],
    # general chat — big nodes, with .7 as a fast small fallback
    "conversation": [(N6, 11434, "ollama", "qwen3:30b-a3b"),
                     (N190, 11434, "ollama", "qwen3:30b-a3b"),
                     (N7, 11434, "ollama", "qwen3:8b")],
    # Nova's persona voice
    "nova":         [(N6, 11434, "ollama", "nova:latest")],
    # fast / cheap chat — small tier + tinychat
    "fast":         [(N7, 11434, "ollama", "qwen3:8b"),
                     (N2, 8000, "tinychat", "deepseek-r1:8b")],
    # low-latency single-stream — MTPLX speculative decoding
    "mtplx":        [(N6, 5050, "mtplx", "mtplx-qwen36-27b-optimized-speed"),
                     (N190, 5050, "mtplx", "mtplx-qwen36-27b-optimized-speed")],
    # reasoning
    "reasoner":     [(N6, 11434, "ollama", "deepseek-r1:8b"),
                     (N7, 11434, "ollama", "qwen3:8b")],
    # vision
    "vision":       [(N6, 11434, "ollama", "qwen3-vl:4b")],
    # embeddings — offload to .7 first, then peers
    "embed":        [(N7, 11434, "ollama", "nomic-embed-text:latest"),
                     (N190, 11434, "ollama", "nomic-embed-text:latest"),
                     (N6, 11434, "ollama", "nomic-embed-text:latest")],
}

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
                                        "inflight": 0, "lat": 0.0, "kind": kind})
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
            out.append((host, port, kind, model, st["inflight"], st["lat"]))
    return out


def _pick(pool):
    """Least in-flight among eligible (latency tie-break) -> active/active spread."""
    cand = _eligible(pool)
    if not cand:
        return None
    cand.sort(key=lambda b: (b[4], b[5]))   # (inflight, latency)
    return cand[0][:4]                       # (host,port,kind,model)


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
                                     "models": sorted(s["models"])[:12]}
                        for (h, p), s in _state.items()}
            pools = {cls: [f"{h}:{p}->{m}" for h, p, k, m in pool]
                     for cls, pool in POOLS.items()}
            return self._json(200, {"backends": snap, "pools": pools})
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
                                    "lat": 0.0, "kind": ""})["inflight"] += 1
        try:
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"http://{host}:{port}{up_path}", data=body,
                headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=PROXY_TIMEOUT)
            data = resp.read()                     # buffered relay — correct + simple
            self.send_response(resp.status)
            self.send_header("Content-Type",
                             resp.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Nova-Backend", f"{host}:{port}")
            self.send_header("X-Nova-Model", model)
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self._json(e.code, {"error": f"backend {host}:{port}: {e.reason}"})
        except Exception as e:
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
