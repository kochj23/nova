#!/usr/bin/env python3
"""nova_llm_probe.py — what this machine's LLMs are doing right now, pushed to Nova's hub every few seconds.

Jordan, 2026-10-16: "I would like this same view for the cluster as a whole on what the entire cluster is doing as
far as LLMs as a different app." -> the Nova Cluster app reads the hub's /cluster, built from these snapshots.

Read-only and cheap. Every PUSH_S (1 s) it:
  * finds every llama-server process (Ollama's model runners and any standalone llama.cpp server) from `ps`,
    names its model from the Ollama manifests next to the blob, and reads its /slots (local loopback client):
    per slot whether it is generating, prompt size / prompt processed, tokens decoded, the task counter;
  * asks each local Ollama (/api/ps on the configured ports) what is loaded and how much memory it holds;
  * adds load and free memory, and POSTs the snapshot to the hub (/llm, X-Minion-Token). Push, not pull: no inbound
    port on any machine. Never touches prompts or outputs.
Config ~/.nova-probe/config.json {"node", "hub", "ollama_ports": [11434]}; token: Keychain service 'nova-minion'
(account = node) or ~/.nova-probe/token or ~/.nova-minion/token (0600). Python 3.9 stdlib only.
  python3 nova_llm_probe.py [--once] [--selftest]
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import glob
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

DIR = os.path.expanduser("~/.nova-probe")
PUSH_S = 1          # 1 s: short requests and the last tokens before a request ends are not missed
VERSION = "1.0"
LINUX = platform.system() == "Linux"
_NAMES: dict = {}          # models dir -> {blob digest: model name}, refreshed every 5 min
_NAMES_T: dict = {}


def log(m):
    print(f"[llm-probe {datetime.now():%H:%M:%S}] {m}", flush=True)


def sh(*args, timeout=10) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:  # noqa: BLE001
        return ""


def http(url: str, body: dict | None = None, token: str | None = None, timeout=4) -> object:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json", **({"X-Minion-Token": token} if token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def runners(ps_out: str | None = None) -> list[dict]:
    """[{pid, port, blob, models_dir, ctx}] for every llama-server in `ps`. Pure given ps_out."""
    out = ps_out if ps_out is not None else sh("/bin/ps", "-axo", "pid=,command=") if not LINUX else sh("ps", "-eo", "pid=,args=")
    found = []
    for line in out.splitlines():
        if "llama-server" not in line or "--port" not in line:
            continue
        m_port = re.search(r"--port\s+(\d+)", line)
        m_model = re.search(r"--model\s+(\S+)", line)
        m_ctx = re.search(r"(?:-c|--ctx-size)\s+(\d+)", line)
        if not (m_port and m_model):
            continue
        path = m_model.group(1)
        blob = os.path.basename(path).replace("sha256-", "sha256:")
        mdir = path.split("/blobs/")[0] if "/blobs/" in path else ""
        found.append({"pid": int(line.split()[0]), "port": int(m_port.group(1)), "blob": blob, "models_dir": mdir,
                      "ctx": int(m_ctx.group(1)) if m_ctx else None, "standalone": "--offline" not in line})
    return found


def blob_names(mdir: str) -> dict:
    """{blob digest: 'name:tag'} from the Ollama manifests in mdir (cached 5 min)."""
    if not mdir:
        return {}
    if time.time() - _NAMES_T.get(mdir, 0) < 300:
        return _NAMES.get(mdir, {})
    names = {}
    for mf in glob.glob(os.path.join(mdir, "manifests", "**", "*"), recursive=True):
        if not os.path.isfile(mf):
            continue
        try:
            layers = json.load(open(mf)).get("layers", [])
        except Exception:  # noqa: BLE001
            continue
        parts = mf.split(os.sep + "manifests" + os.sep, 1)[-1].split(os.sep)
        # registry.ollama.ai/library/qwen3/8b -> qwen3:8b ; hub.example/user/model/tag -> user/model:tag
        if len(parts) >= 3:
            ns, model, tag = parts[-3], parts[-2], parts[-1]
            name = f"{model}:{tag}" if ns == "library" else f"{ns}/{model}:{tag}"
            for la in layers:
                if la.get("mediaType", "").endswith(".model"):
                    names[la.get("digest", "")] = name
    _NAMES[mdir], _NAMES_T[mdir] = names, time.time()
    return names


_API_NAMES: dict = {"t": 0.0, "map": {}}


def api_names(ports) -> dict:
    """{blob digest: 'name:tag'} from each local Ollama's own API (/api/tags + /api/show's modelfile FROM line).
    Primary source: works under launchd where macOS privacy rules hide the models folder. Cached 10 min."""
    if time.time() - _API_NAMES["t"] < 600 and _API_NAMES["map"]:
        return _API_NAMES["map"]
    names = dict(_API_NAMES["map"])
    for port in ports:
        try:
            tags = [m.get("name") for m in (http(f"http://127.0.0.1:{port}/api/tags", timeout=4) or {}).get("models", [])]
        except Exception:  # noqa: BLE001
            continue
        for name in tags:
            if not name or name in names.values():
                continue
            try:
                mf = (http(f"http://127.0.0.1:{port}/api/show", {"model": name}, timeout=8) or {}).get("modelfile", "")
            except Exception:  # noqa: BLE001
                continue
            m = re.search(r"^FROM\s+\S*sha256[-:]([0-9a-f]{64})", mf, re.M)
            if m:
                names["sha256:" + m.group(1)] = name
    _API_NAMES.update(t=time.time(), map=names)
    return names


def slot_view(slots: list) -> list[dict]:
    """Pure: the few slot fields the cluster view needs."""
    out = []
    for s in slots or []:
        nt = (s.get("next_token") or [{}])
        nt = nt[0] if isinstance(nt, list) and nt else (nt if isinstance(nt, dict) else {})
        out.append({"busy": bool(s.get("is_processing")), "task": s.get("id_task"),
                    "prompt": s.get("n_prompt_tokens") or 0, "prompt_done": s.get("n_prompt_tokens_processed") or 0,
                    "decoded": nt.get("n_decoded") or 0, "ctx": s.get("n_ctx")})
    return out


def free_gb() -> float:
    if LINUX:
        m = re.search(r"MemAvailable:\s+(\d+)", open("/proc/meminfo").read())
        return round(int(m.group(1)) / 2**20, 1) if m else 0.0
    out = sh("/usr/bin/vm_stat")
    ps_ = re.search(r"page size of (\d+)", out)
    n = sum(int(m.group(2)) for m in re.finditer(r"Pages (free|inactive|purgeable|speculative):\s+(\d+)", out))
    return round(n * (int(ps_.group(1)) if ps_ else 16384) / 2**30, 1)


def mem_gb() -> float:
    if LINUX:
        m = re.search(r"MemTotal:\s+(\d+)", open("/proc/meminfo").read())
        return round(int(m.group(1)) / 2**20, 1) if m else 0.0
    return round(int(sh("/usr/sbin/sysctl", "-n", "hw.memsize").strip() or 0) / 2**30, 1)


def os_label() -> str:
    if LINUX:
        try:
            m = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', open("/etc/os-release").read(), re.M)
            return re.sub(r"\s+LTS$", "", m.group(1)) if m else "Linux"
        except OSError:
            return "Linux"
    return f"macOS {platform.mac_ver()[0]}"


def chip() -> str:
    if LINUX:
        m = re.search(r"model name\s*:\s*(.+)", open("/proc/cpuinfo").read())
        return re.sub(r"\(R\)|\(TM\)|CPU|@.*", "", m.group(1)).strip()[:40] if m else ""
    return sh("/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string").strip()[:40]


def snapshot(conf: dict) -> dict:
    rs = []
    by_api = api_names(conf.get("ollama_ports", [11434]))
    for r in runners():
        try:
            slots = http(f"http://127.0.0.1:{r['port']}/slots", timeout=2)
        except Exception:  # noqa: BLE001 — a runner mid-load has no slots yet
            slots = []
        rs.append({"port": r["port"], "model": by_api.get(r["blob"]) or blob_names(r["models_dir"]).get(r["blob"], r["blob"][:19]),
                   "ctx": r["ctx"], "standalone": r["standalone"], "slots": slot_view(slots if isinstance(slots, list) else [])})
    loaded = []
    for port in conf.get("ollama_ports", [11434]):
        try:
            for m in (http(f"http://127.0.0.1:{port}/api/ps", timeout=3) or {}).get("models", []):
                loaded.append({"name": m.get("name"), "port": port, "size_gb": round((m.get("size") or 0) / 1e9, 1),
                               "vram_gb": round((m.get("size_vram") or 0) / 1e9, 1), "ctx": m.get("context_length")})
        except Exception:  # noqa: BLE001
            pass
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = None
    return {"node": conf["node"], "os": os_label(), "chip": chip(), "cores": os.cpu_count(), "mem_gb": mem_gb(),
            "free_gb": free_gb(), "load": round(load1, 2) if load1 is not None else None, "probe": VERSION,
            "runners": rs, "loaded": loaded}


def token(node: str) -> str:
    t = "" if LINUX else sh("/usr/bin/security", "find-generic-password", "-s", "nova-minion", "-a", node, "-w").strip()
    for p in (os.path.join(DIR, "token"), os.path.expanduser("~/.nova-minion/token")):
        if t:
            break
        try:
            t = open(p).read().strip()
        except OSError:
            pass
    return t


def selftest() -> int:
    ps_out = ("123 /Volumes/Data/ollama/bin/llama-server --model /Volumes/Data/ollama/models/blobs/sha256-abc --port 58963 "
              "--host 127.0.0.1 --no-webui --offline -c 32768 -np 1\n"
              "456 /opt/homebrew/bin/llama-server --model /x/.ollama/models/blobs/sha256-def --host 0.0.0.0 --port 11435 "
              "--ctx-size 8192\n789 /usr/bin/python3 something --port 1\n")
    r = runners(ps_out)
    assert [(x["port"], x["blob"], x["ctx"], x["standalone"]) for x in r] == [
        (58963, "sha256:abc", 32768, False), (11435, "sha256:def", 8192, True)]
    assert r[0]["models_dir"] == "/Volumes/Data/ollama/models"
    v = slot_view([{"is_processing": True, "id_task": 7, "n_prompt_tokens": 6915, "n_prompt_tokens_processed": 5573,
                    "n_ctx": 16384, "next_token": [{"n_decoded": 12}]}])
    assert v == [{"busy": True, "task": 7, "prompt": 6915, "prompt_done": 5573, "decoded": 12, "ctx": 16384}]
    assert slot_view(None) == []
    print("selftest OK")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    conf = json.load(open(os.path.join(DIR, "config.json")))
    tok = token(conf["node"])
    if not tok:
        log("no token (Keychain nova-minion / ~/.nova-probe/token) — enroll this node first")
        return 2
    if "--once" in sys.argv:
        print(json.dumps(snapshot(conf), indent=1))
        return 0
    log(f"probe {conf['node']} -> {conf['hub']} every {PUSH_S}s")
    fails = 0
    while True:
        try:
            http(conf["hub"].rstrip("/") + "/llm", snapshot(conf), token=tok, timeout=5)
            fails = 0
        except Exception as e:  # noqa: BLE001
            fails += 1
            if fails in (1, 10, 100) or fails % 1000 == 0:
                log(f"push failed x{fails} ({e})")
        time.sleep(PUSH_S)


if __name__ == "__main__":
    sys.exit(main())
