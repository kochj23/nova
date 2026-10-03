#!/usr/bin/env python3
"""
nova_watchdog.py — independent off-box fleet watchdog (runs on nova-core5 / 192.168.1.10).

The whole point: every other monitor (nova_prober, nova_notifier, big_brother) lives
on mac-studio (.6). So when .6 itself wedges, the thing that *would* alert you dies
with it. This watchdog runs on nova-core5, checks the fleet from the *outside*, and posts
straight to Slack with ZERO dependency on .6 — no PG, no notifier, no .6 services.
Pure stdlib so it can't be taken down by a broken venv either.

Alerts go to #nova-critical on a confirmed DOWN, recovery + heartbeat to #nova-info.
State is a local JSON file so it survives restarts without re-alerting. The Slack bot
token comes from the environment (NOVA_SLACK_BOT_TOKEN, loaded from ~/.openclaw/
secrets.env by the systemd unit) — never hardcoded.
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request

# --- config -----------------------------------------------------------------
SLACK_API = "https://slack.com/api/chat.postMessage"
CH_CRITICAL = "C0B3G7J6N07"   # #nova-critical
CH_INFO = "C0BLNUEM9JS"       # #nova-feed (FYI/muted). Was #nova-info (C0BC4SNUTQR), retired
                              # 2026-07-29 — the watchdog heartbeat/snapshot were the last
                              # stragglers still hitting the dead channel. 2026-08-12.

CHECK_INTERVAL = 45           # seconds between sweeps
FAIL_THRESHOLD = 3            # consecutive SWEEP fails before declaring DOWN (debounce flaps)
HEARTBEAT_EVERY = 24 * 3600   # seconds between "still alive" notes to #nova-info
HTTP_TIMEOUT = 10             # ollama /api/tags can be slow under load; give it room
TCP_TIMEOUT = 4
# Per-probe resilience: a single slow/transient response must not count as a sweep
# failure. Retry each probe a few times with a short backoff before giving up.
# Combined with FAIL_THRESHOLD this requires a sustained outage, not a flap.
PROBE_ATTEMPTS = 3            # attempts per probe within ONE sweep
PROBE_BACKOFF = 1.5           # seconds between attempts
WATCHER = socket.gethostname().split(".")[0]   # which node is doing the watching

STATE_DIR = os.path.expanduser("~/.openclaw/state")
STATE_FILE = os.path.join(STATE_DIR, "watchdog_state.json")

# Each: (label, kind, target). kind 'tcp' -> (host, port); 'http' -> url (expects 200).
CHECKS = [
    ("mac-studio (.6) host",      "tcp",  ("192.168.1.6", 22)),
    # /api/version is a cheap liveness endpoint; /api/tags enumerates every model
    # and can take seconds (and time out) on a loaded box even when ollama is fine.
    ("mac-studio (.6) ollama",    "http", "http://192.168.1.6:11434/api/version"),
    ("pg-primary (.2) postgres",  "tcp",  ("192.168.1.2", 5434)),   # .6:5432 is a localhost-only pgbouncer shim since 2026-10-03
    ("mac-studio (.6) nova-gw",   "tcp",  ("192.168.1.6", 18792)),
    ("mac-studio (.6) mqtt",      "tcp",  ("192.168.1.6", 1883)),
    ("nova-core (.2) host",       "tcp",  ("192.168.1.2", 22)),
    ("nova-core (.2) pg-replica", "tcp",  ("192.168.1.2", 5432)),
    ("nova-core (.2) grafana",    "http", "http://192.168.1.2:3000/api/health"),
    # /identity is Plex's cheap unauthenticated liveness endpoint (200 = up).
    # Added after Plex was silently down 2026-06-22 with nothing alerting (#663).
    ("nova-core (.2) plex",       "http", "http://192.168.1.2:32400/identity"),
    ("nova-core10 (.77) ollama",  "http", "http://192.168.1.77:11434/api/version"),
    # nova-core5: so a SECOND watcher (on .2) catches it going down — the gap the
    # 2026-06-22 power event exposed (its own watchdog died with it).
    ("nova-core5 (.10) host",      "tcp",  ("192.168.1.10", 22)),
    ("nova-core5 (.10) pg-replica","tcp",  ("192.168.1.10", 5432)),
    # storage tier — also went dark in that outage and nothing alerted.
    ("synology NAS (.11) smb",    "tcp",  ("192.168.1.11", 445)),
    ("UNAS backup (.69) smb",     "tcp",  ("192.168.1.69", 445)),
]


# --- checks -----------------------------------------------------------------
def check_tcp(host, port):
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT):
            return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}"


def check_http(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nova-watchdog/1.0"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            code = r.getcode()
            return (200 <= code < 400), (f"HTTP {code}" if code >= 400 else "")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}"


def run_check(kind, target):
    """Probe a target, retrying transient failures within a single sweep.

    A node that's actually healthy can still drop one probe (ollama busy listing
    models, momentary TCP RST, GC pause). Retrying PROBE_ATTEMPTS times with a
    short backoff means only a *sustained* failure across the whole retry window
    is reported up to the sweep — which still needs FAIL_THRESHOLD consecutive
    sweeps before it declares DOWN. Real outages persist; flaps don't.
    """
    last_detail = ""
    for attempt in range(PROBE_ATTEMPTS):
        ok, detail = check_tcp(*target) if kind == "tcp" else check_http(target)
        if ok:
            return True, ""
        last_detail = detail
        if attempt < PROBE_ATTEMPTS - 1:
            time.sleep(PROBE_BACKOFF)
    return False, last_detail


# --- slack (independent path) ----------------------------------------------
def slack_post(channel, text):
    token = os.environ.get("NOVA_SLACK_BOT_TOKEN", "")
    if not token:
        print("[watchdog] NO Slack token in env — cannot alert!", flush=True)
        return False
    data = json.dumps({"channel": channel, "text": text, "mrkdwn": True}).encode()
    req = urllib.request.Request(
        SLACK_API, data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            ok = json.loads(r.read()).get("ok", False)
            if not ok:
                print("[watchdog] slack post not ok", flush=True)
            return ok
    except Exception as e:
        print(f"[watchdog] slack post failed: {type(e).__name__}", flush=True)
        return False


# --- state ------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


# --- main loop --------------------------------------------------------------
def sweep(state):
    """One pass: returns (transitions, up_count, total)."""
    transitions = []
    up = 0
    for label, kind, target in CHECKS:
        ok, detail = run_check(kind, target)
        st = state.setdefault(label, {"state": "up", "fails": 0})
        if ok:
            up += 1
            if st["state"] == "down":
                transitions.append(("recovered", label, ""))
            st["state"], st["fails"] = "up", 0
        else:
            st["fails"] += 1
            if st["state"] == "up" and st["fails"] >= FAIL_THRESHOLD:
                st["state"] = "down"
                transitions.append(("down", label, detail))
    return transitions, up, len(CHECKS)


def main():
    state = load_state()
    # Startup snapshot to #nova-info — establishes baseline without crying wolf.
    trans, up, total = sweep(state)
    save_state(state)
    downs = [l for l, s in state.items() if s["state"] == "down"]
    snap = (f":dog: *nova-watchdog online on {WATCHER}* — watching {total} fleet "
            f"checks every {CHECK_INTERVAL}s.\nBaseline: *{up}/{total} up*"
            + (f" — currently DOWN: {', '.join(downs)}" if downs else " — all green."))
    slack_post(CH_INFO, snap)
    print(f"[watchdog] online — {up}/{total} up", flush=True)
    last_heartbeat = time.time()

    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            trans, up, total = sweep(state)
            save_state(state)
            for kind, label, detail in trans:
                if kind == "down":
                    slack_post(CH_CRITICAL,
                               f":rotating_light: *FLEET DOWN* — `{label}` is unreachable "
                               f"from {WATCHER}{(' (' + detail + ')') if detail else ''}. "
                               f"({up}/{total} checks up)")
                    print(f"[watchdog] DOWN: {label} {detail}", flush=True)
                else:
                    slack_post(CH_INFO,
                               f":white_check_mark: *recovered* — `{label}` is back. "
                               f"({up}/{total} up)")
                    print(f"[watchdog] recovered: {label}", flush=True)
            if time.time() - last_heartbeat >= HEARTBEAT_EVERY:
                slack_post(CH_INFO, f":dog: nova-watchdog ({WATCHER}) heartbeat — {up}/{total} fleet checks up.")
                last_heartbeat = time.time()
        except Exception as e:
            # The watchdog must never die quietly. Log and keep going.
            print(f"[watchdog] sweep error: {type(e).__name__}: {e}", flush=True)


if __name__ == "__main__":
    main()
