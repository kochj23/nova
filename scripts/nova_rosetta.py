#!/usr/bin/env python3
"""
nova_rosetta.py — the universal translation layer between Nova's inference runtimes.

Nova's fleet runs the same open models on three different stacks:
  ollama    — native /api/chat; Metal on the Macs, ROCm on the AMD Radeon iGPU boxes (.86/.150/.5)
  mlx       — mlx_lm.server, OpenAI-compatible /v1/chat/completions, Apple Silicon only
  llamacpp  — llama-server, OpenAI-compatible, GGUF (Metal on the Studio; would be CUDA/ROCm/Vulkan elsewhere)

Each one names the same weights differently (qwen3:8b vs mlx-community/Qwen3-8B-4bit vs a GGUF blob), asks for
structured output differently (Ollama `format`, llama.cpp `response_format`, MLX not at all), and reports usage
differently. Rosetta hides that: callers ask for a CANONICAL model ("qwen3-8b") with plain OpenAI-style messages,
an optional JSON schema and a temperature, and get one normalized answer back from whichever node nova_lb ranks
best — with <think> blocks split out, JSON extracted/validated (and retried on MLX, which can't constrain decoding),
tokens and tok/s in the same units, and failover to the next candidate on error.

It deliberately never sends num_ctx: a request whose context differs from the server default spawns a new runner
(a reload), see agent_docs 'nova-inference'. It only ever targets models already RESIDENT on a server (nova_lb
discovers those), so routing across the fleet can't trigger cold loads either.

  python3 nova_rosetta.py --list                       # catalog + where each model is resident right now
  python3 nova_rosetta.py --model qwen3-8b --prompt "hi" [--schema '{"type":"object",...}'] [--no-think]
  python3 nova_rosetta.py --resolve qwen3:30b-a3b      # any runtime name -> canonical id
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

PG_DSN = "dbname=nova_ops user=kochj host=localhost"
DEFAULT_TIMEOUT = 300

# Which wire API each runtime speaks. "openai" = any other OpenAI-compatible server (vLLM on CUDA/ROCm, LM Studio,
# a llama-server on Vulkan…) — whatever an idle laptop happens to run.
API = {"ollama": "ollama", "mlx": "openai", "llamacpp": "openai", "openai": "openai"}
# Runtimes that can constrain decoding to a JSON schema (MLX can't: prompt + validate + retry instead).
CONSTRAINED = {"ollama", "llamacpp", "openai"}
# Prior tok/s by accelerator for a ~8B Q4 model, used to rank a node before Rosetta has measured it.
# "gpu" = offloaded to some GPU whose vendor we couldn't see (joined without --accel or ssh).
ACCEL_PRIOR_TPS = {"cuda": 90.0, "mlx": 45.0, "metal": 40.0, "gpu": 20.0, "rocm": 18.0, "vulkan": 14.0,
                   "sycl": 12.0, "cpu": 5.0}
# Where runtimes listen by default — what --join looks for on a new machine.
RUNTIME_PORTS = [(11434, "ollama"), (5050, "mlx"), (11435, "llamacpp"), (8080, "llamacpp"), (8000, "openai"),
                 (1234, "openai")]


# ── Canonical catalog ─────────────────────────────────────────────────────────

@dataclass
class Model:
    family: str
    think: bool = False                 # emits <think>…</think> reasoning by default
    vision: bool = False
    artifacts: dict = field(default_factory=dict)   # runtime -> [names]; ollama exact tag, others substring match


CATALOG = {
    "qwen3-8b":        Model("qwen3", think=True, artifacts={"ollama": ["qwen3:8b"], "mlx": ["qwen3-8b"]}),
    "qwen3-14b":       Model("qwen3", think=True, artifacts={"ollama": ["qwen3:14b"], "mlx": ["qwen3-14b"]}),
    "qwen3-30b-a3b":   Model("qwen3", think=True, artifacts={"ollama": ["qwen3:30b-a3b"], "mlx": ["qwen3-30b-a3b"]}),
    "qwen3-coder-30b": Model("qwen3", artifacts={"ollama": ["qwen3-coder:30b"], "mlx": ["qwen3-coder-30b"],
                                                  # the Studio's llama-server runs Ollama's own GGUF blob of it
                                                  "llamacpp": ["sha256-1194192cf2a1", "qwen3-coder-30b"]}),
    "qwen2.5-32b":     Model("qwen2.5", artifacts={"mlx": ["qwen2.5-32b"], "ollama": ["qwen2.5:32b"]}),
    "deepseek-r1-8b":  Model("deepseek-r1", think=True, artifacts={"ollama": ["deepseek-r1:8b"]}),
    "qwen3-vl-4b":     Model("qwen3-vl", vision=True, artifacts={"ollama": ["qwen3-vl:4b"]}),
    "qwen3-vl-30b":    Model("qwen3-vl", vision=True, artifacts={"ollama": ["qwen3-vl:30b"]}),
    "qwen2.5-vl-3b":   Model("qwen2.5-vl", vision=True, artifacts={"ollama": ["qwen2.5vl:3b"]}),
    "gemma3-27b":      Model("gemma3", vision=True, artifacts={"ollama": ["gemma3:27b"], "mlx": ["gemma-3-27b"]}),
    "llama3.2-3b":     Model("llama3.2", artifacts={"ollama": ["llama3.2:3b"], "mlx": ["llama-3.2-3b"]}),
    "nova":            Model("qwen3", artifacts={"ollama": ["nova:latest"]}),
}


def _norm_tag(name: str) -> str:
    name = (name or "").strip().lower()
    return name if ":" in name else name + ":latest"


def matches(canonical: str, runtime: str, served_name: str) -> bool:
    """Is `served_name` (as a server lists it) this canonical model on that runtime?"""
    m = CATALOG.get(canonical)
    if not m:
        return False
    names = m.artifacts.get(runtime, [])
    if runtime == "ollama":
        return _norm_tag(served_name) in {_norm_tag(n) for n in names}
    s = (served_name or "").lower()
    return any(n.lower() in s for n in names)


def resolve(canonical: str, runtime: str, served: list) -> str | None:
    """The name to send to this runtime for the canonical model, from what it actually serves (None if absent)."""
    return next((s for s in served or [] if matches(canonical, runtime, s)), None)


def canonical(name: str) -> str | None:
    """Any runtime's model name (or a canonical id) -> canonical id."""
    if name in CATALOG:
        return name
    for cid in CATALOG:
        for rt in ("ollama", "mlx", "llamacpp"):
            if matches(cid, rt, name):
                return cid
    return None


# ── Schema translation ────────────────────────────────────────────────────────

def normalize_schema(schema):
    """JSON Schema with lowercase types (Gemini-style "OBJECT"/"STRING" schemas are common in Nova's games)."""
    if isinstance(schema, dict):
        out = {k: normalize_schema(v) for k, v in schema.items()}
        if isinstance(out.get("type"), str):
            out["type"] = out["type"].lower()
        return out
    if isinstance(schema, list):
        return [normalize_schema(v) for v in schema]
    return schema


_PY_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int, "number": (int, float)}


def validate(value, schema) -> str | None:
    """Small structural validator (type, required, properties, items, enum). Returns an error or None."""
    t = schema.get("type")
    if t in _PY_TYPES and not isinstance(value, _PY_TYPES[t]) or (t in ("integer", "number") and isinstance(value, bool)):
        return f"expected {t}, got {type(value).__name__}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{value!r} not in {schema['enum']}"
    if t == "object":
        for k in schema.get("required", []):
            if k not in value:
                return f"missing required key '{k}'"
        for k, sub in (schema.get("properties") or {}).items():
            if k in value and isinstance(sub, dict):
                err = validate(value[k], sub)
                if err:
                    return f"{k}: {err}"
    if t == "array" and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            err = validate(v, schema["items"])
            if err:
                return f"[{i}]: {err}"
    return None


def extract_json(text: str):
    """First JSON value in a reply (tolerates ```json fences and chatter around it). Raises ValueError."""
    t = re.sub(r"```(?:json)?", "", text or "")
    dec = json.JSONDecoder()
    for i, ch in enumerate(t):
        if ch in "{[":
            try:
                return dec.raw_decode(t[i:])[0]
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON in reply")


# ── Response normalization ────────────────────────────────────────────────────

_THINK = re.compile(r"<think>(.*?)</think>", re.S)


def split_think(text: str) -> tuple[str, str]:
    """(answer, reasoning) — strips <think> blocks; an unterminated one (cut off by max_tokens) is all reasoning."""
    text = text or ""
    thinking = "\n".join(m.strip() for m in _THINK.findall(text))
    answer = _THINK.sub("", text)
    if "<think>" in answer:
        head, _, tail = answer.partition("<think>")
        answer, thinking = head, (thinking + "\n" + tail).strip()
    return answer.strip(), thinking.strip()


# ── Request translation ───────────────────────────────────────────────────────

def _openai_messages(messages: list) -> list:
    """Ollama-style {"images": [b64]} -> OpenAI content parts."""
    out = []
    for m in messages:
        imgs = m.get("images")
        if not imgs:
            out.append({k: v for k, v in m.items() if k != "images"})
            continue
        parts = [{"type": "text", "text": m.get("content", "")}]
        parts += [{"type": "image_url", "image_url": {"url": i if str(i).startswith("data:") else f"data:image/png;base64,{i}"}}
                  for i in imgs]
        out.append({"role": m["role"], "content": parts})
    return out


def _no_think(messages: list) -> list:
    """Qwen3's soft switch for runtimes without a think flag: '/no_think' on the last user turn."""
    msgs = [dict(m) for m in messages]
    for m in reversed(msgs):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            m["content"] += " /no_think"
            break
    return msgs


def build_request(runtime: str, base: str, model: str, messages: list, *, temperature=None, max_tokens=None,
                  schema=None, think=None, family="") -> tuple[str, dict]:
    """(url, payload) for one runtime."""
    if API[runtime] == "ollama":
        body = {"model": model, "messages": messages, "stream": False, "options": {}}
        if temperature is not None:
            body["options"]["temperature"] = temperature
        if max_tokens:
            body["options"]["num_predict"] = max_tokens
        if schema:
            body["format"] = schema
        if think:
            body["think"] = True
        # think=False is deliberately NOT sent: on Ollama 0.40 qwen3 then reasons untagged INTO the answer. Left at
        # the default, Ollama separates reasoning into message.thinking, and we return only the clean answer.
        return f"{base}/api/chat", body
    msgs = _no_think(messages) if think is False and family == "qwen3" else messages
    body = {"model": model, "messages": _openai_messages(msgs), "stream": False}
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens:
        body["max_tokens"] = max_tokens
    if schema and runtime in CONSTRAINED:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "reply", "schema": schema}}
    return f"{base}/v1/chat/completions", body


def parse_response(runtime: str, raw: dict, elapsed_s: float) -> dict:
    """Normalized {text, thinking, tokens_in, tokens_out, tok_s, finish} from any runtime's reply."""
    if API[runtime] == "ollama":
        msg = raw.get("message") or {}
        text, thinking = split_think(msg.get("content", ""))
        thinking = "\n".join(x for x in (msg.get("thinking") or "", thinking) if x).strip()
        out_n, dur = raw.get("eval_count") or 0, raw.get("eval_duration") or 0
        tps = out_n / (dur / 1e9) if dur else (out_n / elapsed_s if elapsed_s else 0)
        return {"text": text, "thinking": thinking, "tokens_in": raw.get("prompt_eval_count") or 0,
                "tokens_out": out_n, "tok_s": round(tps, 1), "finish": raw.get("done_reason") or "stop"}
    choice = (raw.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text, thinking = split_think(msg.get("content") or "")
    thinking = "\n".join(x for x in (msg.get("reasoning_content") or "", thinking) if x).strip()
    usage = raw.get("usage") or {}
    out_n = usage.get("completion_tokens") or 0
    tps = (raw.get("timings") or {}).get("predicted_per_second") or (out_n / elapsed_s if elapsed_s else 0)
    return {"text": text, "thinking": thinking, "tokens_in": usage.get("prompt_tokens") or 0,
            "tokens_out": out_n, "tok_s": round(tps, 1), "finish": choice.get("finish_reason") or "stop"}


def _post(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ── Measured speed (feeds nova_lb's ranking) ──────────────────────────────────

def record_perf(node: str, runtime: str, model: str, tok_s: float, dsn: str = PG_DSN, failed: bool = False) -> None:
    """EWMA tok/s per (node, runtime, canonical model) in PG; best-effort, never raises. A failure cuts the node's
    score to 30% (a server that crashes on every request sinks to the bottom after two tries instead of costing
    each caller a timeout); one success pulls it back up."""
    if failed:
        tok_s = 1.0
    elif not tok_s or tok_s <= 0:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(dsn, connect_timeout=2)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS rosetta_perf (
                         node_name TEXT, runtime TEXT, model TEXT, tok_s REAL, samples INT DEFAULT 1,
                         updated_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY (node_name, runtime, model))""")
        cur.execute("""INSERT INTO rosetta_perf (node_name, runtime, model, tok_s) VALUES (%s,%s,%s,%s)
                       ON CONFLICT (node_name, runtime, model) DO UPDATE SET
                         tok_s = CASE WHEN %s THEN rosetta_perf.tok_s * 0.3
                                      ELSE rosetta_perf.tok_s * 0.7 + EXCLUDED.tok_s * 0.3 END,
                         samples = rosetta_perf.samples + 1, updated_at = now()""",
                    (node, runtime, model, float(tok_s), failed))
        conn.close()
    except Exception:
        pass


# ── The call ──────────────────────────────────────────────────────────────────

def _call_one(cand: dict, model: str, messages: list, *, temperature, max_tokens, schema, think, timeout,
              json_retries: int) -> dict:
    runtime, m = cand["protocol"], CATALOG[model]
    base = f"http://{cand['ip']}:{cand['port']}"
    msgs = list(messages)
    constrained = bool(schema) and runtime in CONSTRAINED
    if schema and not constrained:
        msgs = msgs + [{"role": "system", "content": "Reply with ONLY a JSON value matching this JSON Schema, no "
                        "prose, no code fences:\n" + json.dumps(schema)}]
    for attempt in range(json_retries + 1):
        url, body = build_request(runtime, base, cand["runtime_model"], msgs, temperature=temperature,
                                  max_tokens=max_tokens, schema=schema if constrained else None, think=think,
                                  family=m.family)
        t0 = time.perf_counter()
        out = parse_response(runtime, _post(url, body, timeout), time.perf_counter() - t0)
        if not schema:
            return out
        try:
            value = extract_json(out["text"])
            err = validate(value, schema)
        except ValueError as e:
            value, err = None, str(e)
        if not err:
            return {**out, "json": value}
        if attempt < json_retries:
            msgs = msgs + [{"role": "assistant", "content": out["text"]},
                           {"role": "user", "content": f"That did not match the schema ({err}). Reply with only the "
                                                       "corrected JSON."}]
    raise ValueError(f"reply never matched the schema: {err}")


def chat(model: str, messages: list, *, temperature: float | None = None, max_tokens: int | None = None,
         schema: dict | None = None, think: bool | None = None, candidates: list | None = None,
         exclude: list | None = None, runtimes: list | None = None, max_attempts: int = 3,
         timeout: float = DEFAULT_TIMEOUT, json_retries: int = 1) -> dict:
    """One chat turn against canonical `model`, wherever in the fleet it's resident.

    candidates: ranked nodes (from nova_lb.candidates_shared) — fetched when omitted. Each failure moves on to the
    next candidate, up to max_attempts. Returns the normalized result (plus node/runtime/accelerator) or
    {"success": False, "error": ..., "attempts": [...]}.
    """
    cid = canonical(model)
    if not cid:
        return {"success": False, "error": f"unknown model '{model}'", "attempts": []}
    schema = normalize_schema(schema) if schema else None
    if candidates is None:
        import nova_lb
        candidates = nova_lb.candidates_shared(cid, exclude=exclude, runtimes=runtimes, structured=bool(schema))
    attempts = []
    for cand in (candidates or [])[:max_attempts]:
        try:
            out = _call_one(cand, cid, messages, temperature=temperature, max_tokens=max_tokens, schema=schema,
                            think=think, timeout=timeout, json_retries=json_retries)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            attempts.append({"node": cand["name"], "runtime": cand["protocol"], "error": str(e)[:200]})
            if not isinstance(e, ValueError):        # a schema miss is the model's fault, not the node's
                record_perf(cand["name"], cand["protocol"], cid, 0, failed=True)
            continue
        record_perf(cand["name"], cand["protocol"], cid, out["tok_s"])
        attempts.append({"node": cand["name"], "runtime": cand["protocol"], "ok": True})
        return {"success": True, **out, "model": cid, "runtime_model": cand["runtime_model"],
                "node": cand["name"], "runtime": cand["protocol"], "accelerator": cand.get("accelerator", ""),
                "attempts": attempts}
    if not candidates:
        return {"success": False, "error": f"no healthy node has '{cid}' resident", "attempts": attempts}
    return {"success": False, "error": "; ".join(f"{a['node']}/{a['runtime']}: {a['error']}" for a in attempts),
            "attempts": attempts}


# ── Joining the fleet ("we have all these idle laptops") ─────────────────────
# A machine joins by running any supported runtime with a model loaded — Ollama is the universal one: same binary
# and API on NVIDIA (CUDA), AMD (ROCm), Apple (Metal), Intel/other (Vulkan) and plain CPU. Then, from any Nova
# host, `nova_rosetta.py --join <ip>`: Rosetta finds its runtimes, works out its accelerator, and records it in
# PG rosetta_nodes; nova_lb picks it up within a minute and it starts taking canonical-model work it has resident.

def detect_local() -> dict:
    """This machine's accelerator + RAM (run ON the node: `python3 nova_rosetta.py --detect`)."""
    import os
    import platform
    import shutil
    import subprocess

    def run(*cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return ""
    sysname, machine = platform.system(), platform.machine().lower()
    info = {"os": sysname.lower(), "arch": machine, "hostname": platform.node().split(".")[0], "gpu_name": ""}
    if sysname == "Darwin" and machine in ("arm64", "aarch64"):
        info["accel"] = "metal"
        info["gpu_name"] = (run("sysctl", "-n", "machdep.cpu.brand_string") or "").strip()
    elif shutil.which("nvidia-smi") and run("nvidia-smi", "-L").strip():
        info["accel"] = "cuda"
        info["gpu_name"] = run("nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader").strip()
    elif os.path.exists("/dev/kfd") or shutil.which("rocminfo"):
        info["accel"] = "rocm"
        info["gpu_name"] = next((l.split(":", 1)[1].strip() for l in run("rocminfo").splitlines()
                                 if "Marketing Name" in l and "CPU" not in l and "Processor" not in l), "AMD GPU")
    elif shutil.which("vulkaninfo") and "deviceName" in run("vulkaninfo", "--summary"):
        info["accel"] = "vulkan"
        info["gpu_name"] = next((l.split("=", 1)[1].strip() for l in run("vulkaninfo", "--summary").splitlines()
                                 if "deviceName" in l), "")
    else:
        info["accel"] = "cpu"
    try:
        info["ram_gb"] = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except (ValueError, OSError, AttributeError):
        info["ram_gb"] = None
    return info


def _get_json(url: str, timeout: float = 3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def probe_runtimes(ip: str, ports=None) -> dict:
    """{runtime: port} for every inference server answering on `ip` (one per runtime, first port wins)."""
    found = {}
    for port, guess in ports or RUNTIME_PORTS:
        base = f"http://{ip}:{port}"
        try:
            if guess == "ollama":
                _get_json(f"{base}/api/version")
                found.setdefault("ollama", port)
                continue
            _get_json(f"{base}/v1/models")
        except Exception:
            continue
        try:
            _get_json(f"{base}/props")           # only llama-server has /props
            rt = "llamacpp"
        except Exception:
            rt = "mlx" if guess == "mlx" else "openai"
        found.setdefault(rt, port)
    return found


def _remote_detect(ip: str) -> dict | None:
    """detect_local() on the node over ssh (keys only, no prompts) — None if ssh isn't available."""
    import subprocess
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", ip, "python3", "-", "--detect"],
                           input=open(__file__).read(), capture_output=True, text=True, timeout=40)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except Exception:
        return None


def parse_duration(text: str) -> int:
    """'90m', '8h', '2d', '1h30m' -> seconds. Raises ValueError."""
    parts = re.findall(r"(\d+)\s*([smhd])", (text or "").lower())
    if not parts or re.sub(r"[\d\s smhd]", "", text.lower()):
        raise ValueError(f"bad duration '{text}' (try 30m, 8h, 2d)")
    return sum(int(n) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u] for n, u in parts)


def _minion_ip(name: str, dsn: str) -> tuple[str, str] | None:
    """(ip, node) for an enrolled minion name, so `--join office-2 --for 8h` works without knowing its IP."""
    try:
        import psycopg2
        conn = psycopg2.connect(dsn, connect_timeout=3)
        cur = conn.cursor()
        cur.execute("SELECT ip, node FROM minion_nodes WHERE node = %s OR alias = %s", (name, name))
        r = cur.fetchone()
        conn.close()
        return (r[0], r[1]) if r and r[0] else None
    except Exception:
        return None


def join(ip: str, name: str = "", accel: str = "", dsn: str = PG_DSN, max_connections: int = 2,
         seconds: int = 0) -> dict:
    """Probe `ip` (or an enrolled minion's name), work out its accelerator, record it in rosetta_nodes. `seconds`
    > 0 makes it a loan: nova_lb lets it go when it ends, whatever the machine is doing. Returns the row written."""
    import ipaddress
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        found = _minion_ip(ip, dsn)
        if not found:
            raise ValueError(f"'{ip}' is neither an IP nor an enrolled minion")
        ip, name = found[0], name or found[1]
    if not ipaddress.ip_address(ip).is_private:
        raise ValueError(f"{ip} is not a LAN address — Nova's inference never leaves the house")
    runtimes = probe_runtimes(ip)
    if not runtimes:
        raise ValueError(f"no inference runtime answering on {ip} (start Ollama with OLLAMA_HOST=0.0.0.0)")
    info = _remote_detect(ip) or {}
    base = accel or info.get("accel")
    if not base and "ollama" in runtimes:          # no ssh: Ollama still tells us whether weights sit in VRAM
        try:
            ps = _get_json(f"http://{ip}:{runtimes['ollama']}/api/ps").get("models", [])
            base = "gpu" if any(m.get("size_vram") for m in ps) else "cpu"
        except Exception:
            pass
    base = base or "cpu"
    if base not in ACCEL_PRIOR_TPS:
        raise ValueError(f"unknown accelerator '{base}' (one of {', '.join(ACCEL_PRIOR_TPS)})")
    accels = {rt: ("mlx" if rt == "mlx" else base) for rt in runtimes}
    node = {"name": name or info.get("hostname") or f"node-{ip.replace('.', '-')}", "ip": ip, "protocols": runtimes,
            "accel": accels, "gpu": base not in ("cpu",), "max_connections": max_connections, "info": info,
            "expires_at": time.time() + seconds if seconds else None}
    import psycopg2
    conn = psycopg2.connect(dsn, connect_timeout=3)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS rosetta_nodes (
                     name TEXT PRIMARY KEY, ip TEXT NOT NULL, protocols JSONB NOT NULL, accel JSONB NOT NULL,
                     gpu BOOLEAN DEFAULT false, max_connections INT DEFAULT 2, info JSONB, enabled BOOLEAN DEFAULT true,
                     joined_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now())""")
    cur.execute("ALTER TABLE rosetta_nodes ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
    cur.execute("""INSERT INTO rosetta_nodes (name, ip, protocols, accel, gpu, max_connections, info, expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s > 0 THEN now() + %s * interval '1 second' END)
                   ON CONFLICT (name) DO UPDATE SET ip=EXCLUDED.ip, protocols=EXCLUDED.protocols, accel=EXCLUDED.accel,
                     gpu=EXCLUDED.gpu, max_connections=EXCLUDED.max_connections, info=EXCLUDED.info, enabled=true,
                     expires_at=EXCLUDED.expires_at, updated_at=now()""",
                (node["name"], ip, json.dumps(runtimes), json.dumps(accels), node["gpu"], max_connections,
                 json.dumps(info), seconds, seconds))
    conn.close()
    return node


def leave(name: str, dsn: str = PG_DSN) -> bool:
    import psycopg2
    conn = psycopg2.connect(dsn, connect_timeout=3)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("UPDATE rosetta_nodes SET enabled = false, updated_at = now() WHERE name = %s", (name,))
    n = cur.rowcount
    conn.close()
    return n > 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--resolve")
    ap.add_argument("--model")
    ap.add_argument("--prompt")
    ap.add_argument("--schema")
    ap.add_argument("--no-think", action="store_true")
    ap.add_argument("--detect", action="store_true", help="print THIS machine's accelerator (run on the node)")
    ap.add_argument("--join", metavar="IP", help="add a machine running Ollama/MLX/llama.cpp/vLLM to the fleet")
    ap.add_argument("--name", default="")
    ap.add_argument("--accel", default="", help="override: cuda, rocm, metal, vulkan, sycl, cpu")
    ap.add_argument("--for", dest="duration", default="", help="a loan: 30m, 8h, 2d (default: until --leave)")
    ap.add_argument("--leave", metavar="NAME")
    a = ap.parse_args()
    if a.detect:
        print(json.dumps(detect_local()))
        return 0
    if a.join:
        try:
            node = join(a.join, a.name, a.accel, seconds=parse_duration(a.duration) if a.duration else 0)
        except Exception as e:
            print(f"join failed: {e}", file=sys.stderr)
            return 1
        loan = f" for {a.duration}" if a.duration else ""
        print(f"joined {node['name']} ({node['ip']}){loan}: " + ", ".join(f"{rt}:{p} on {node['accel'][rt]}"
                                                                for rt, p in node["protocols"].items()))
        print("nova_lb picks it up within a minute; check with: nova_rosetta.py --list")
        return 0
    if a.leave:
        print("left" if leave(a.leave) else f"no joined node named {a.leave}")
        return 0
    if a.resolve:
        print(canonical(a.resolve) or "unknown")
        return 0
    if a.list:
        import nova_lb
        for cid in CATALOG:
            where = [f"{c['name']}/{c['protocol']}({c.get('accelerator')})" for c in nova_lb.candidates_shared(cid)]
            print(f"{cid:16} {', '.join(where) or '— not resident anywhere'}")
        return 0
    if not (a.model and a.prompt):
        ap.error("--model and --prompt (or --list / --resolve)")
    r = chat(a.model, [{"role": "user", "content": a.prompt}], schema=json.loads(a.schema) if a.schema else None,
             think=False if a.no_think else None)
    print(json.dumps(r, indent=2))
    return 0 if r["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
