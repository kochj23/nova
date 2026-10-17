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
  * WHO is using the LLM (2026-10-17, Jordan: "always add in what job the LLM is running"): from the TCP table
    (macOS `netstat -anv`, Linux `ss -tnpi`) it reports the clients with a request in flight on this machine's LLM
    ports ("clients") and this machine's own processes connected to LLM ports elsewhere ("calls"). A process is
    named only by its script/app name (nova_journal, NovaControl) — never its arguments. The hub joins one node's
    clients with another's calls, so a request is attributed to the job that sent it.
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
VERSION = "1.1"
# LLM ports this machine may call elsewhere: Ollama, llama.cpp, MLX/MTPLX, the inference router
CALL_PORTS = (11434, 11435, 5050, 37475)
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


_LABEL_OK = re.compile(r"[^A-Za-z0-9._ -]")


GENERIC = {"server", "main", "app", "run", "__main__", "worker", "daemon", "service", "index"}


def proc_label(cmd: str, fallback: str = "", cwd: str = "") -> str | None:
    """Pure: a process's job name from its command line — the script, module or app, NEVER its arguments
    (they can carry prompts or secrets). '/opt/.../Python /x/nova_journal.py --run' -> 'nova_journal'."""
    toks = (cmd or "").split()
    label = ""
    if toks:
        exe = os.path.basename(toks[0])
        if re.match(r"(?i)^python[\d.]*$", exe):
            label = "python"
            it = iter(toks[1:])
            for t in it:
                if t == "-m":
                    label = next(it, "python")
                    break
                if t == "-c":
                    break
                if t in ("-W", "-X"):
                    next(it, None)
                    continue
                if t.startswith("-"):
                    continue
                label = os.path.basename(t)
                label = label[:-3] if label.endswith(".py") else label
                if label in GENERIC:      # server.py says nothing: name it by its folder (nova-control-web)
                    label = os.path.basename(os.path.dirname(t)) or os.path.basename(cwd.rstrip("/")) or label
                break
        else:
            m = re.search(r"/([^/]+)\.app/", cmd)
            label = m.group(1) if m else exe
    label = _LABEL_OK.sub("", label or fallback or "").strip()[:40]
    return label or None


def _addr(s: str) -> str:
    """Pure: 'ip:port' from a netstat ('1.2.3.4.5678') or ss ('[::ffff:1.2.3.4]:5678') endpoint."""
    s = s.strip()
    if s.startswith("["):
        host, _, port = s[1:].partition("]:")
    elif LINUX_STYLE.match(s):
        host, _, port = s.rpartition(":")
    else:
        host, _, port = s.rpartition(".")
    host = host.split("%")[0]
    if host.startswith("::ffff:"):
        host = host[7:]
    return f"{host}:{port}"


LINUX_STYLE = re.compile(r"^[0-9.]+:\d+$|^\*:\d+$")
_NETSTAT = re.compile(r"^tcp\S*\s+\d+\s+\d+\s+(\S+)\s+(\S+)\s+ESTABLISHED\s+(\d+)\s+(\d+)\s+\d+\s+\d+\s+(.*?):(\d+)\s+[0-9a-f]{5}\s")


def parse_netstat(out: str) -> list[dict]:
    """Pure: established TCP connections from macOS `netstat -anv -p tcp`: [{l, r, rx, tx, pid, name}]."""
    conns = []
    for line in out.splitlines():
        m = _NETSTAT.match(line)
        if m:
            conns.append({"l": _addr(m.group(1)), "r": _addr(m.group(2)), "rx": int(m.group(3)), "tx": int(m.group(4)),
                          "name": m.group(5).strip(), "pid": int(m.group(6))})
    return conns


def parse_ss(out: str) -> list[dict]:
    """Pure: established TCP connections from Linux `ss -tnpiH state established` (info on the next line)."""
    conns, cur = [], None
    for line in out.splitlines():
        if not line.strip():
            continue
        if line[0].isspace():
            if cur is not None:
                rx = re.search(r"bytes_received:(\d+)", line)
                tx = re.search(r"bytes_sent:(\d+)", line) or re.search(r"bytes_acked:(\d+)", line)
                cur["rx"], cur["tx"] = int(rx.group(1)) if rx else 0, int(tx.group(1)) if tx else 0
            continue
        parts = line.split()
        if len(parts) < 4:
            cur = None
            continue
        u = re.search(r'\(\("([^"]*)",pid=(\d+)', line)
        cur = {"l": _addr(parts[2]), "r": _addr(parts[3]), "rx": 0, "tx": 0,
               "name": u.group(1) if u else "", "pid": int(u.group(2)) if u else None}
        conns.append(cur)
    return conns


def _port(a: str) -> int:
    try:
        return int(a.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 0


def _host(a: str) -> str:
    return a.rsplit(":", 1)[0]


def _loopback(h: str) -> bool:
    return h.startswith("127.") or h in ("::1", "localhost")


REQ_MIN_BYTES = 400
REQ_MIN_S = 3


def track_clients(conns: list, listen: set, state: dict) -> list[dict]:
    """Inbound connections on the LLM ports that carry a model request right now (state is the caller's, kept between
    samples). Read from the byte counters, sampled every second:
      waiting   — the client sent bytes and nothing has come back yet (a request being prefilled / generated);
      streaming — the answer keeps flowing out sample after sample (tokens);
      blip      — request and answer inside one sample (/api/ps, /api/tags polls, sub-second calls): not a job;
      idle      — a keep-alive connection with nothing on it: not a job.
    The first sample only learns what is already open."""
    primed = state.get("primed", False)
    prev, new, active = state.get("conns", {}), {}, []
    for c in conns:
        if _port(c["l"]) not in listen:
            continue
        key = c["l"] + ">" + c["r"]
        p = prev.get(key)
        if p is None:
            st = "idle" if not primed else ("blip" if c["tx"] > 0 else "waiting")
            req, n = (c["rx"], 1)
        else:
            drx, dtx = c["rx"] > p["rx"], c["tx"] > p["tx"]
            if drx and not dtx:
                st = "waiting"
            elif dtx:
                st = "blip" if drx and p["st"] in ("idle", "blip") else "streaming"
            else:
                st = "waiting" if p["st"] == "waiting" else "idle"
            fresh = p["st"] in ("idle", "blip") and st == "waiting"
            req = (c["rx"] - p["rx"]) if fresh else p["req"] + (c["rx"] - p["rx"])
            n = 1 if fresh else p["n"] + 1
        new[key] = {"rx": c["rx"], "tx": c["tx"], "st": st, "req": req, "n": n}
        # a model request carries a body (prompt); a bare GET (/api/ps, /api/tags) is ~100-250 bytes and quick.
        # Small requests count once they have run REQ_MIN_S samples (a model load, a long generation).
        if st in ("waiting", "streaming") and (req >= REQ_MIN_BYTES or n >= REQ_MIN_S):
            active.append(c)
    state["conns"], state["primed"] = new, True
    return active


_PROC: dict = {}           # pid -> (label, epoch)
_TRACK: dict = {}


def label_of(pid, name: str = "") -> str | None:
    if not pid:
        return proc_label("", name)
    hit = _PROC.get(pid)
    if hit and time.time() - hit[1] < 120:
        return hit[0]
    cmd = sh("ps" if LINUX else "/bin/ps", "-o", "args=", "-p", str(pid), timeout=3).strip()
    lab = proc_label(cmd, name)
    if lab in GENERIC:
        lab = proc_label(cmd, name, cwd=_cwd(pid))
    _PROC[pid] = (lab, time.time())
    if len(_PROC) > 500:
        for k in sorted(_PROC, key=lambda k: _PROC[k][1])[:250]:
            _PROC.pop(k, None)
    return lab


def _cwd(pid: int) -> str:
    if LINUX:
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return ""
    out = sh("/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn", timeout=3)
    return next((ln[1:] for ln in out.splitlines() if ln.startswith("n")), "")


def connections() -> list[dict]:
    if LINUX:
        return parse_ss(sh("ss", "-tnpiH", "state", "established", timeout=5))
    return parse_netstat(sh("/usr/sbin/netstat", "-anv", "-p", "tcp", timeout=5))


def who(listen: set, conns: list | None = None, state: dict | None = None) -> dict:
    """{"clients": active inbound requests [{peer, port, proc?}], "calls": this machine's processes connected to LLM
    ports elsewhere [{src, dst, proc}], "ips": this machine's addresses seen in the table}."""
    conns = connections() if conns is None else conns
    by_local = {c["l"]: c for c in conns}
    clients = []
    for c in track_clients(conns, listen, _TRACK if state is None else state)[:32]:
        peer = by_local.get(c["r"])            # the client's own socket, when it runs on this machine
        if peer and peer.get("pid") == os.getpid():
            continue                           # this probe reading a runner's /slots
        clients.append({"peer": c["r"], "port": _port(c["l"]),
                        "proc": label_of(peer.get("pid"), peer.get("name", "")) if peer else None})
    calls, ips = [], set()
    for c in conns:
        h = _host(c["l"])
        if not _loopback(h) and h not in ("*", "0.0.0.0", "::"):
            ips.add(h)
        rh = _host(c["r"])
        if _port(c["r"]) in CALL_PORTS and not _loopback(rh) and rh != h and len(calls) < 64:
            calls.append({"src": c["l"], "dst": c["r"], "proc": label_of(c.get("pid"), c.get("name", ""))})
    return {"clients": clients, "calls": calls, "ips": sorted(ips)[:8]}


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
    loaded, llm = [], bool(rs)
    for port in conf.get("ollama_ports", [11434]):
        try:
            ps_ = (http(f"http://127.0.0.1:{port}/api/ps", timeout=3) or {})
            llm = True
            for m in ps_.get("models", []):
                loaded.append({"name": m.get("name"), "port": port, "size_gb": round((m.get("size") or 0) / 1e9, 1),
                               "vram_gb": round((m.get("size_vram") or 0) / 1e9, 1), "ctx": m.get("context_length")})
        except Exception:  # noqa: BLE001
            pass
    listen = set(conf.get("ollama_ports", [11434])) | set(conf.get("llm_ports", [])) | {
        r["port"] for r in rs if r.get("standalone")}
    try:
        users = who(listen)
    except Exception as e:  # noqa: BLE001 — attribution is best-effort; the LLM view never fails for it
        log(f"connection table unavailable ({e})")
        users = {"clients": [], "calls": [], "ips": []}
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = None
    return {"node": conf["node"], "os": os_label(), "chip": chip(), "cores": os.cpu_count(), "mem_gb": mem_gb(),
            "free_gb": free_gb(), "load": round(load1, 2) if load1 is not None else None, "probe": VERSION,
            "runners": rs, "loaded": loaded, "llm": llm, **users}


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
    # who is using the LLM: script/app names only, never arguments
    assert proc_label("/opt/homebrew/bin/python3 /nova/scripts/nova_journal.py --prompt secret") == "nova_journal"
    assert proc_label("python3 -u -W ignore -m nova_gateway --x y") == "nova_gateway"
    assert proc_label("python3 -c 'print(1)'") == "python"
    assert proc_label("/Applications/Nova Control.app/Contents/MacOS/Nova Control -x") == "Nova Control"
    assert proc_label("/usr/bin/curl http://x", "") == "curl" and proc_label("", "") is None
    assert proc_label("Python server.py", cwd="/x/apps/nova-control-web") == "nova-control-web"
    assert proc_label("python3 /srv/memory-server/server.py") == "memory-server"
    ns = ("tcp4       0      0  127.0.0.1.11434        127.0.0.1.49370        ESTABLISHED          486         5657  408128  146988"
          "           ollama:316    00102 0000000c 00000000015f4604 00000080 01000800      2      0 000000\n"
          "tcp4       0      0  192.168.1.6.65246      192.168.1.86.11434     ESTABLISHED         2860          154  131072  131768"
          "      Nova Cluster:43157  00102 00000000 00000000015f378e 00000080 04000900      2      0 000000\n"
          "tcp4       0      0  127.0.0.1.11434        127.0.0.1.64978        TIME_WAIT            416         2470  408192  146988"
          "           ollama:316    02131 0000000c 00000000015f2f7c 00000080 01000800      0      0 000000\n")
    c = parse_netstat(ns)
    assert c == [{"l": "127.0.0.1:11434", "r": "127.0.0.1:49370", "rx": 486, "tx": 5657, "name": "ollama", "pid": 316},
                 {"l": "192.168.1.6:65246", "r": "192.168.1.86:11434", "rx": 2860, "tx": 154, "name": "Nova Cluster", "pid": 43157}]
    ss = ('0      0      192.168.1.138:41688 192.168.1.86:11434 users:(("python3",pid=543164,fd=13))\n'
          '\t cubic bytes_sent:415 bytes_acked:416 bytes_received:9787 segs_out:11\n'
          '0      0        [::ffff:192.168.1.2]:11434 192.168.1.86:35782\n\t cubic bytes_acked:7 bytes_received:1457\n')
    assert parse_ss(ss) == [{"l": "192.168.1.138:41688", "r": "192.168.1.86:11434", "rx": 9787, "tx": 415, "name": "python3", "pid": 543164},
                            {"l": "192.168.1.2:11434", "r": "192.168.1.86:35782", "rx": 1457, "tx": 7, "name": "", "pid": None}]
    st: dict = {}
    k = {"l": "127.0.0.1:11434", "r": "127.0.0.1:5", "rx": 10, "tx": 10}
    tc = lambda c: track_clients([c], {11434}, st) != []                          # noqa: E731
    assert not tc(k)                                                               # first sample only learns
    assert not tc(dict(k, rx=20, tx=30))                                           # poll answered in one sample
    assert not tc(dict(k, rx=20, tx=30))                                           # idle keep-alive
    assert tc(dict(k, rx=940, tx=30))                                              # request (with a prompt) sent
    assert tc(dict(k, rx=940, tx=30))                                              # waiting for the answer
    assert tc(dict(k, rx=940, tx=90)) and tc(dict(k, rx=940, tx=120))              # answer streaming
    assert not tc(dict(k, rx=940, tx=120))                                         # answered: idle again
    assert not tc(dict(k, rx=1100, tx=120))                                        # a bare GET poll waiting...
    assert not tc(dict(k, rx=1100, tx=900))                                        # ...answered next sample: no job
    assert not tc(dict(k, rx=1100, tx=900))
    assert not tc(dict(k, rx=1200, tx=900)) and not tc(dict(k, rx=1200, tx=900))   # small request, 2 s...
    assert tc(dict(k, rx=1200, tx=900))                                            # ...3 s: a model load / long call
    assert tc(dict(k, r="127.0.0.1:6", rx=2000, tx=0))                             # a new connection asking
    assert not tc(dict(k, r="127.0.0.1:8", tx=500))                                # new + answered at once: a blip
    assert not tc(dict(k, r="127.0.0.1:8", rx=640, tx=3000))                       # ...and the next poll too
    assert track_clients([dict(k, l="127.0.0.1:9999")], {11434}, st) == []         # not an LLM port
    w = who({11434}, [{"l": "127.0.0.1:11434", "r": "127.0.0.1:7", "rx": 900, "tx": 0, "name": "ollama", "pid": 1},
                      {"l": "127.0.0.1:7", "r": "127.0.0.1:11434", "rx": 1, "tx": 1, "name": "x", "pid": None},
                      {"l": "192.168.1.6:9", "r": "192.168.1.77:11434", "rx": 1, "tx": 1, "name": "y", "pid": None}],
            {"primed": True})
    assert w["clients"] == [{"peer": "127.0.0.1:7", "port": 11434, "proc": "x"}]
    assert w["calls"] == [{"src": "192.168.1.6:9", "dst": "192.168.1.77:11434", "proc": "y"}] and w["ips"] == ["192.168.1.6"]
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
