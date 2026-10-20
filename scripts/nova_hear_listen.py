#!/opt/homebrew/bin/python3
"""nova_hear_listen.py — Studio-only "listening" service for nova_hear's vocal call (Jordan 2026-10-19).

POST /vocals {"url": "<youtube watch url>"} -> {"lead_vocal": bool, "gender": ..., "style": ..., "votes": [...]}
GET  /health -> {"ok": true, "busy": bool}

Qwen3-Omni-30B-A3B (MLX 4-bit, mlx-vlm venv on /Volumes/Data) hears 3 x 30 s clips at ~20/50/75 % of the
track (mlx-vlm feeds it at most ~30 s per clip) and the answers are majority-voted. Vocal call ONLY — the
measurements stay nova_hear's job; nova_hear labels this as a model's guess.

Guards: MLX capped 48 GB / 4 GB cache (the 2026-10-19 Studio panic was uncapped MLX); single flight (a second
request gets 503 busy, never queues 21 GB); model loaded per request and freed after; callers allowlisted by
DNS name (scheduler nodes + this host). LaunchAgent: net.digitalnoise.nova-hear-listen (launchd/ template).
"""
import glob
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# launchd runs /opt/homebrew/bin/python3 (Nova convention); mlx-vlm lives in its own venv — put it first.
for sp in glob.glob("/Volumes/Data/venvs/mlx-vlm/lib/python3*/site-packages"):
    if sp not in sys.path:
        sys.path.insert(0, sp)
os.environ.setdefault("HF_HOME", "/Volumes/Data/huggingface")

PORT = int(os.environ.get("NOVA_HEAR_LISTEN_PORT", "37481"))
MODEL = "mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit"
ALLOWED_HOSTS = ("nova-core.digitalnoise.net", "nova-core3.digitalnoise.net", "nova-core8.digitalnoise.net",
                 "localhost")
CLIP_AT = (0.2, 0.5, 0.75)
CLIP_S = 30
WORK = "/Volumes/nas/nova-drafts/hear/listen-tmp"   # files Claude/Nova make on .6 go on the NAS
YTDLP = "/opt/homebrew/bin/yt-dlp"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
YT_RE = re.compile(r"^https://(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]{6,}$")
GENDERS = ("female", "male", "mixed", "none", "unsure")
STYLES = ("melodic singing", "rapping", "spoken", "mixed", "none", "unsure")
PROMPT = ("Listen to this audio clip. Answer ONLY with JSON, no prose: "
          '{"lead_vocal": true|false, "gender": "female"|"male"|"mixed"|"none"|"unsure", '
          '"style": "melodic singing"|"rapping"|"spoken"|"mixed"|"none"|"unsure"}. '
          "lead_vocal is true only if a person is singing, rapping or speaking over the music in this clip. "
          "Use unsure when you cannot tell.")
LOCK = threading.Lock()
_allow = {"t": 0.0, "ips": set()}


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [hear-listen] {msg}", flush=True)


def allowed(ip: str) -> bool:
    """Caller IP must resolve from one of ALLOWED_HOSTS (re-resolved every 5 min, so DNS moves are followed)."""
    if time.time() - _allow["t"] > 300:
        ips = {"127.0.0.1", "::1"}
        for h in ALLOWED_HOSTS:
            try:
                ips |= {a[4][0] for a in socket.getaddrinfo(h, None)}
            except OSError:
                pass
        _allow.update(t=time.time(), ips=ips)
    return ip in _allow["ips"]


def clip_starts(duration: float) -> list[float]:
    """Start offsets for the clips; short tracks get fewer, never past the end."""
    if duration <= CLIP_S:
        return [0.0]
    return sorted({round(max(0.0, min(duration * f - CLIP_S / 2, duration - CLIP_S)), 1) for f in CLIP_AT})


def parse_answer(raw: str) -> dict | None:
    m = re.search(r"\{.*?\}", raw or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    lv = d.get("lead_vocal")
    g = str(d.get("gender", "unsure")).lower().strip()
    s = str(d.get("style", "unsure")).lower().strip()
    return {"lead_vocal": lv if isinstance(lv, bool) else None,
            "gender": g if g in GENDERS else "unsure", "style": s if s in STYLES else "unsure"}


def majority(votes: list[dict]) -> dict:
    """Per field, the most common firm answer; a tie or no firm answer -> unsure (lead_vocal -> None)."""
    def pick(field, skip):
        c = Counter(v[field] for v in votes if v and v.get(field) not in skip)
        top = c.most_common(2)
        if not top or (len(top) == 2 and top[0][1] == top[1][1]):
            return None
        return top[0][0]
    lv = pick("lead_vocal", (None,))
    return {"lead_vocal": lv,
            "gender": pick("gender", ("unsure",)) or "unsure",
            "style": pick("style", ("unsure",)) or "unsure", "votes": votes}


def listen(url: str) -> dict:
    import mlx.core as mx
    mx.set_memory_limit(48 << 30)
    mx.set_cache_limit(4 << 30)
    from mlx_vlm import generate, load
    from mlx_vlm.prompt_utils import apply_chat_template
    os.makedirs(WORK, exist_ok=True)
    t0 = time.time()
    with tempfile.TemporaryDirectory(dir=WORK) as d:
        src = os.path.join(d, "a.wav")
        for attempt in range(3):   # intermittent googlevideo 403s
            subprocess.run([YTDLP, "--no-warnings", "--socket-timeout", "30", "--no-playlist", "-f", "bestaudio",
                            "-x", "--audio-format", "wav", "-o", os.path.join(d, "a.%(ext)s"), url],
                           capture_output=True, text=True, timeout=60)
            if os.path.exists(src):
                break
            time.sleep(3)
        if not os.path.exists(src):
            raise RuntimeError("audio download failed")
        dur = float(subprocess.run(["/opt/homebrew/bin/ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "csv=p=0", src], capture_output=True, text=True).stdout.strip() or 0)
        clips = []
        for i, st in enumerate(clip_starts(dur)):
            c = os.path.join(d, f"c{i}.wav")
            subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", str(st), "-t", str(CLIP_S), "-i", src,
                            "-ac", "1", "-ar", "16000", c], check=True)
            clips.append(c)
        model, processor = load(MODEL)
        try:
            prompt = apply_chat_template(processor, model.config, PROMPT, num_audios=1)
            votes = []
            for c in clips:
                out = generate(model, processor, prompt, audio=[c], max_tokens=80, temperature=0.0, verbose=False)
                votes.append(parse_answer(getattr(out, "text", out)))
        finally:
            del model
            mx.clear_cache()
    res = majority(votes)
    res["seconds"] = round(time.time() - t0, 1)
    res["peak_gb"] = round(mx.get_peak_memory() / 2**30, 1)
    return res


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):   # our own log lines only
        pass

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "busy": LOCK.locked(), "model": MODEL})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        ip = self.client_address[0]
        if not allowed(ip):
            log(f"refused {ip}")
            return self._send(403, {"error": "caller not allowlisted"})
        if self.path != "/vocals":
            return self._send(404, {"error": "not found"})
        try:
            url = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}").get("url", "")
        except json.JSONDecodeError:
            url = ""
        if not YT_RE.match(url):
            return self._send(400, {"error": "url must be a youtube watch url"})
        if not LOCK.acquire(blocking=False):
            return self._send(503, {"error": "busy"})
        try:
            res = listen(url)
            log(f"{ip} {url}: {res['gender']} / {res['style']} / lead={res['lead_vocal']} "
                f"({res['seconds']}s, peak {res['peak_gb']} GB)")
            self._send(200, res)
        except Exception as e:  # noqa: BLE001
            log(f"{ip} {url}: failed {type(e).__name__}: {e}")
            self._send(500, {"error": f"{type(e).__name__}: {e}"})
        finally:
            LOCK.release()


def selftest():
    assert clip_starts(20) == [0.0]
    assert clip_starts(255) == [36.0, 112.5, 176.2], clip_starts(255)
    assert all(s + CLIP_S <= 100 for s in clip_starts(100))
    a = parse_answer('Sure: {"lead_vocal": true, "gender": "Female", "style": "melodic singing"}')
    assert a == {"lead_vocal": True, "gender": "female", "style": "melodic singing"}, a
    assert parse_answer("no json") is None and parse_answer('{"gender": "alien"}')["gender"] == "unsure"
    m = majority([a, a, {"lead_vocal": True, "gender": "male", "style": "rapping"}])
    assert (m["lead_vocal"], m["gender"], m["style"]) == (True, "female", "melodic singing"), m
    t = majority([a, {"lead_vocal": True, "gender": "male", "style": "rapping"}])
    assert t["gender"] == "unsure" and t["style"] == "unsure"            # 1-1 tie is not a call
    assert YT_RE.match("https://www.youtube.com/watch?v=vZexE7wAvBE") and not YT_RE.match("file:///etc/passwd")
    assert allowed("127.0.0.1") and not allowed("203.0.113.9")
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    log(f"listening on :{PORT} (model {MODEL}, MLX cap 48 GB / 4 GB cache, single flight)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
