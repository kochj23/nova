#!/usr/bin/env python3
"""nova_broadcastify_calls.py — Broadcastify Calls API ingest (ad-free trunked dispatch).

Replaces the ad-laden Icecast live feeds for trunked systems. Polls the Broadcastify
Calls `group_archives` endpoint per group (app-level JWT, no user auth needed for
public dispatch tags), downloads each clean per-call MP3/M4A, transcribes with
faster_whisper, and stores to nova_memories with real metadata (groupId, talkgroup
name, timestamp). No ads — calls are individual radio transmissions, not a mixed stream.

Auth: HS256 JWT (kid header, {iss,iat,exp} payload, HMAC-SHA256 signed with the API key
secret). Credentials from the fleet pgcrypto store (nova-broadcastify-calls-{kid,secret,iss}).

Runs on nova-core2 (.86) where faster_whisper + ffmpeg live. Config-driven GROUPS list.
Written by Jordan Koch (via Claude).
"""
import base64, hashlib, hmac, json, os, subprocess, sys, time, urllib.request, urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_secrets

API = "https://api.bcfy.io/calls/v1"
MEM = os.environ.get("MEM_URL", "http://memory-server.digitalnoise.net:18790/remember")
STATE = Path.home() / ".openclaw/state/bcfy_calls_pos.json"
POLL_SECS = int(os.environ.get("BCFY_POLL_SECS", "120"))  # archives lag ~15min; 2min poll is ample
MINLEN = 8

# groupId -> (source, label). Verdugo Fire (Burbank/Glendale) dispatch on ICI (sid 7095).
# source must be one of the blotter's ('scanner','fire','rail','chp'); add more groups here.
GROUPS = {
    "7095-2101": ("fire",    "Verdugo Fire — Red-1 Dispatch"),
    "7095-2102": ("fire",    "Verdugo Fire — Red 2 (Tac)"),
    "7095-2103": ("fire",    "Verdugo Fire — Red 3 (Tac)"),
    "7095-2107": ("fire",    "Verdugo Fire — Red 6 (Tac)"),
    "7095-2115": ("fire",    "Verdugo Fire — Red 8 (Tac)"),
    "7095-2161": ("scanner", "Burbank PD — Dispatch"),
    "7095-2173": ("scanner", "Burbank PD — Dispatch 2"),
}

_creds = None
def _jwt():
    global _creds
    if _creds is None:
        _creds = (nova_secrets.get_secret("nova-broadcastify-calls-kid"),
                  nova_secrets.get_secret("nova-broadcastify-calls-secret"),
                  nova_secrets.get_secret("nova-broadcastify-calls-iss"))
    kid, sec, iss = _creds
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
    now = int(time.time())
    h = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode())
    p = b64(json.dumps({"iss": iss, "iat": now, "exp": now + 300}).encode())
    s = b64(hmac.new(sec.encode(), h + b"." + p, hashlib.sha256).digest())
    return (h + b"." + p + b"." + s).decode()


def _archives(group_id, start, end):
    u = f"{API}/group_archives/{group_id}/{start}/{end}"
    req = urllib.request.Request(u, headers={"Authorization": "Bearer " + _jwt()})
    return json.loads(urllib.request.urlopen(req, timeout=30).read()).get("calls", [])


_model = None
def _transcribe(audio_bytes, suffix):
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel("base.en", device="cpu", compute_type="int8")
    tmp = Path("/tmp/bcfy_call" + suffix)
    tmp.write_bytes(audio_bytes)
    try:
        segs, _ = _model.transcribe(str(tmp), language="en", vad_filter=True,
                                    condition_on_previous_text=False, no_speech_threshold=0.6)
        good = [s.text.strip() for s in segs
                if getattr(s, "no_speech_prob", 0.0) < 0.6 and getattr(s, "avg_logprob", -1.0) > -1.0]
        return " ".join(good).strip()
    finally:
        try: tmp.unlink()
        except OSError: pass


def _remember(text, source, label, gid, ts):
    meta = {"kind": source, "channel": label, "source_feed": f"bcfy-calls/{gid}",
            "receiver": "broadcastify-calls", "call_ts": ts, "location": "Burbank/Glendale (Verdugo dispatch)"}
    data = json.dumps({"text": f"[{label}] {text}", "source": source, "metadata": meta}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(MEM + "?async=1", data=data,
                               headers={"Content-Type": "application/json"}), timeout=15)
    except Exception:
        pass


def _load_pos():
    try: return json.loads(STATE.read_text())
    except Exception: return {}
def _save_pos(pos):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(pos))


def main():
    print(f"[bcfy-calls] up — {len(GROUPS)} groups, poll {POLL_SECS}s", flush=True)
    pos = _load_pos()
    while True:
        now = int(time.time())
        for gid, (source, label) in GROUPS.items():
            # first run: last 20 min; after: strictly AFTER last-seen ts (archives lag
            # ~15min). +1 avoids re-retrieving (re-billing) the boundary call each poll.
            start = (pos[gid] + 1) if gid in pos else (now - 1200)
            try:
                calls = _archives(gid, start, now)
            except Exception as e:
                print(f"[bcfy-calls] {gid} archives error: {e}", flush=True); continue
            newest = start
            for c in sorted(calls, key=lambda x: x["ts"]):
                if c["ts"] <= pos.get(gid, 0):
                    continue
                try:
                    audio = urllib.request.urlopen(c["url"], timeout=30).read()
                    suffix = "." + c["url"].rsplit(".", 1)[-1]
                    if len(audio) < 4000:
                        newest = max(newest, c["ts"]); continue
                    text = _transcribe(audio, suffix)
                    if text and len(text) > MINLEN:
                        _remember(text, source, label, gid, c["ts"])
                        print(f"[{time.strftime('%H:%M:%S')}] {label} :: {text[:90]}", flush=True)
                except Exception as e:
                    print(f"[bcfy-calls] {gid} call {c.get('ts')} err: {e}", flush=True)
                newest = max(newest, c["ts"])
            pos[gid] = newest
        _save_pos(pos)
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    sys.exit(main())
