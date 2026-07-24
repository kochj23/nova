#!/usr/bin/env python3
"""nova_frame_index.py — index a video's FRAMES into Nova's vector memory.

Extracts evenly-spaced frames, describes each with an Ollama vision model (reads on-screen
text too), and stores the descriptions in Nova memory (source='frame_vision') with the
video + timestamp in metadata. Because they land in the same pgvector store as transcripts,
one /recall query then searches BOTH what was SAID and what was SHOWN — the multi-modal
fusion. Call standalone on any video, or from the ingest pipeline after transcription.

Usage: nova_frame_index.py <video> [--show "Name"] [--frames N]
"""
import os, sys, json, base64, subprocess, tempfile, urllib.request, argparse

OLLAMA = "http://127.0.0.1:11434"
MEMORY = "http://memory-server.digitalnoise.net:18790"
VLM    = os.environ.get("FVS_VLM", "qwen2.5vl:3b")


def _ollama(path, payload, timeout=120):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def duration(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=noprint_wrappers=1:nokey=1", video],
                         capture_output=True, text=True).stdout.strip()
    return float(out) if out else 0.0


def show_from_path(video):
    parts = os.path.abspath(video).split(os.sep)
    for root in ("TVShows", "youtube"):
        if root in parts:
            i = parts.index(root)
            if i + 1 < len(parts):
                return parts[i + 1]
    return os.path.splitext(os.path.basename(video))[0]


def describe(frame_path):
    b64 = base64.b64encode(open(frame_path, "rb").read()).decode()
    r = _ollama("/api/generate", {
        "model": VLM,
        "prompt": "Describe what's visible in this video frame in one concise, specific sentence "
                  "(people, setting, objects, actions, any on-screen text). For search indexing.",
        "images": [b64], "stream": False})
    return " ".join(r.get("response", "").split())


def remember(text, meta):
    body = json.dumps({"text": text, "source": "frame_vision", "metadata": meta}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            MEMORY + "/remember?async=1", data=body,
            headers={"Content-Type": "application/json"}), timeout=15)
        return True
    except Exception as e:
        print(f"  remember failed: {e}", file=sys.stderr); return False


def hms(t):
    return f"{int(t // 3600):02d}:{int((t % 3600) // 60):02d}:{int(t % 60):02d}"


def index_video(video, show, n):
    dur = duration(video) or 60
    step = max(dur / n, 1)
    tmp = tempfile.mkdtemp(prefix="fidx_")
    stored, t, i = 0, step / 2, 0
    while t < dur and i < n:
        fp = os.path.join(tmp, f"f{i:03d}.jpg")
        subprocess.run(["ffmpeg", "-ss", str(t), "-i", video, "-frames:v", "1",
                        "-q:v", "3", "-y", fp], capture_output=True)
        if os.path.exists(fp):
            d = describe(fp)
            if d:
                ts = hms(t)
                text = f"[{show} — frame @ {ts}] {d}"
                meta = {"kind": "frame", "show": show, "video": os.path.basename(video),
                        "t_seconds": round(t, 1), "timestamp": ts}
                if remember(text, meta):
                    stored += 1
                    print(f"  [{ts}] {d[:88]}", flush=True)
            os.remove(fp)
        t += step; i += 1
    return stored


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--show", default=None)
    ap.add_argument("--frames", type=int, default=int(os.environ.get("FVS_FRAMES", "60")))
    a = ap.parse_args()
    show = a.show or show_from_path(a.video)
    print(f"Indexing {a.frames} frames of '{show}' into Nova memory (source=frame_vision)...")
    n = index_video(a.video, show, a.frames)
    print(f"\nStored {n} frame descriptions. Searchable now via /recall, fused with transcripts.")
