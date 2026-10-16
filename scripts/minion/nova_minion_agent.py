#!/usr/bin/env python3
"""nova_minion_agent.py — runs ON a minion Mac (a Mac that lends its GPU to Nova while idle).

Every 30 s it decides whether this Mac is free for Nova and tells the hub (nova_minion_hub.py on the Studio):
  idle       the screensaver is up or nobody has touched the keyboard/mouse for idle_after_min, on AC power,
             not thermally throttled, inside the optional overnight window -> the reader may send work here
  away       someone is using it (or on battery / throttled / outside the window) -> no new work; the model is
             unloaded so the person at the keyboard gets the memory back
  dedicated  config "dedicated": true — a fleet node that is always available (never unloads)
It also serves http://127.0.0.1:37591/status for the Nova Minion screensaver (a cached proxy of the hub's
/status for this node), so the screensaver never needs the hub's address or the token.

Python 3.9 stdlib only (runs on the system python3 of a fresh Mac). Config: ~/.nova-minion/config.json
  {"node": "studio-2", "hub": "http://<studio>:37590", "ollama_url": "http://<this mac>:11434",
   "idle_after_min": 10, "hours": "22-07" (optional), "dedicated": false, "models": ["qwen3:30b-a3b"]}
Token: login Keychain, service 'nova-minion', account <node>; else ~/.nova-minion/token (0600, SSH installs)
  python3 nova_minion_agent.py [--once] [--selftest]
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

CONF = os.path.expanduser("~/.nova-minion/config.json")
LOCAL_PORT = 37591
BEAT_S = 30
STATUS_TTL_S = 4
VERSION = "1.0"
_cache = {"t": 0.0, "body": None, "lock": threading.Lock()}
_state = {"state": "offline", "since": None}


def log(m):
    print(f"[minion {datetime.now():%Y-%m-%d %H:%M:%S}] {m}", flush=True)


def load_conf() -> dict:
    with open(CONF) as f:
        c = json.load(f)
    c.setdefault("idle_after_min", 10)
    c.setdefault("models", ["qwen3:30b-a3b"])
    c.setdefault("dedicated", False)
    return c


def sh(*args, timeout=10) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:  # noqa: BLE001
        return ""


def idle_seconds(ioreg_out: str | None = None) -> float:
    out = ioreg_out if ioreg_out is not None else sh("/usr/sbin/ioreg", "-c", "IOHIDSystem", "-d", "4")
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    return int(m.group(1)) / 1e9 if m else 0.0


def screensaver_up() -> bool:
    return bool(sh("/usr/bin/pgrep", "-x", "ScreenSaverEngine").strip())


def on_ac(pmset_out: str | None = None) -> bool:
    out = pmset_out if pmset_out is not None else sh("/usr/bin/pmset", "-g", "batt")
    return "AC Power" in out or not out          # a desktop Mac with no battery reports AC


def throttled(therm_out: str | None = None) -> bool:
    out = therm_out if therm_out is not None else sh("/usr/bin/pmset", "-g", "therm")
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
    return bool(m and int(m.group(1)) < 100)


def in_hours(spec: str | None, hour: int) -> bool:
    """'22-07' -> 22:00..06:59 (wraps midnight); empty -> always."""
    if not spec:
        return True
    a, b = (int(x) for x in spec.split("-"))
    return a <= hour < b if a < b else (hour >= a or hour < b)


def decide(conf: dict, idle_s: float, saver: bool, ac: bool, hot: bool, hour: int) -> str:
    """Pure: the state this Mac should report."""
    if conf.get("dedicated"):
        return "dedicated"
    free = saver or idle_s >= float(conf["idle_after_min"]) * 60
    return "idle" if free and ac and not hot and in_hours(conf.get("hours"), hour) else "away"


def http(url: str, body: dict | None = None, token: str | None = None, timeout=8) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json", **({"X-Minion-Token": token} if token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def token(node: str) -> str:
    """Login Keychain first; else ~/.nova-minion/token (0600) — written when the installer ran over SSH."""
    t = sh("/usr/bin/security", "find-generic-password", "-s", "nova-minion", "-a", node, "-w").strip()
    if t:
        return t
    try:
        with open(os.path.join(os.path.dirname(CONF), "token")) as f:
            return f.read().strip()
    except OSError:
        return ""


def models_here(ollama: str) -> list:
    try:
        return [m["name"] for m in http(ollama.rstrip("/") + "/api/tags", timeout=5).get("models", [])]
    except Exception:  # noqa: BLE001
        return []


def set_loaded(ollama: str, models: list, load: bool):
    """Preload the reader's model when Nova may use this Mac; unload it when a person is back."""
    for m in models:
        try:
            http(ollama.rstrip("/") + "/api/generate", {"model": m, "keep_alive": "60m" if load else 0}, timeout=180)
            log(f"{'loaded' if load else 'unloaded'} {m}")
        except Exception as e:  # noqa: BLE001
            log(f"{m} not {'loaded' if load else 'unloaded'} ({e})")


def tick(conf: dict, tok: str) -> str:
    state = decide(conf, idle_seconds(), screensaver_up(), on_ac(), throttled(), datetime.now().hour)
    ollama = conf.get("ollama_url") or ""
    have = models_here(ollama) if ollama else []
    if not have and state != "away":
        state = "away"                           # Ollama is down: nothing to lend
    prev = _state["state"]
    if state != prev:
        log(f"{prev} -> {state}")
        _state.update(state=state, since=time.time())
        want = [m for m in conf["models"] if m in have]
        if state in ("idle", "dedicated") and want:
            threading.Thread(target=set_loaded, args=(ollama, want, True), daemon=True).start()
        elif state == "away" and prev == "idle" and want:
            threading.Thread(target=set_loaded, args=(ollama, want, False), daemon=True).start()
    try:
        http(conf["hub"].rstrip("/") + "/heartbeat",
             {"node": conf["node"], "state": state, "ollama_url": ollama, "models": have,
              "info": {"agent": VERSION, "ac": on_ac(), "throttled": throttled(), "idle_s": int(idle_seconds())}},
             token=tok)
    except Exception as e:  # noqa: BLE001
        log(f"heartbeat failed ({e})")
    return state


class Status(BaseHTTPRequestHandler):
    conf: dict = {}
    tok: str = ""

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path.split("?")[0] != "/status":
            self.send_response(404)
            self.end_headers()
            return
        with _cache["lock"]:
            if time.time() - _cache["t"] > STATUS_TTL_S:
                try:
                    _cache["body"] = http(f"{self.conf['hub'].rstrip('/')}/status?node={self.conf['node']}",
                                          token=self.tok, timeout=4)
                    _cache["fresh"] = True
                except Exception:  # noqa: BLE001 — the screensaver keeps drawing what it last had
                    _cache["fresh"] = False
                _cache["t"] = time.time()
            body = dict(_cache["body"] or {"node": self.conf.get("node")})
        if not _cache.get("fresh"):
            body["state"] = "offline"
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def selftest() -> int:
    assert idle_seconds('"HIDIdleTime" = 650000000000') == 650.0 and idle_seconds("") == 0.0
    assert on_ac("Now drawing from 'AC Power'") and not on_ac("Now drawing from 'Battery Power'")
    assert throttled("CPU_Speed_Limit \t= 70") and not throttled("CPU_Speed_Limit = 100") and not throttled("")
    assert in_hours("22-07", 23) and in_hours("22-07", 3) and not in_hours("22-07", 12) and in_hours("", 12)
    assert in_hours("09-17", 10) and not in_hours("09-17", 18)
    c = {"idle_after_min": 10, "dedicated": False}
    assert decide(c, 700, False, True, False, 12) == "idle"
    assert decide(c, 30, True, True, False, 12) == "idle"            # screensaver up
    assert decide(c, 30, False, True, False, 12) == "away"
    assert decide(c, 700, False, False, False, 12) == "away"          # on battery
    assert decide(c, 700, False, True, True, 12) == "away"            # throttled
    assert decide({**c, "hours": "22-07"}, 700, False, True, False, 12) == "away"
    assert decide({**c, "dedicated": True}, 0, False, False, True, 12) == "dedicated"
    print("selftest OK")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    conf = load_conf()
    tok = token(conf["node"])
    if not tok:
        log("no token in the Keychain (service nova-minion) — enroll this Mac first")
        return 2
    if "--once" in sys.argv:
        print(tick(conf, tok))
        return 0
    Status.conf, Status.tok = conf, tok
    threading.Thread(target=HTTPServer(("127.0.0.1", LOCAL_PORT), Status).serve_forever, daemon=True).start()
    log(f"minion {conf['node']} up; hub {conf['hub']}; status on 127.0.0.1:{LOCAL_PORT}")
    while True:
        try:
            tick(conf, tok)
        except Exception as e:  # noqa: BLE001
            log(f"tick failed ({e})")
        time.sleep(BEAT_S)


if __name__ == "__main__":
    sys.exit(main())
