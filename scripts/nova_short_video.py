#!/usr/bin/env python3
"""nova_short_video.py — turn a piece of Nova's writing into a narrated, captioned
vertical short (1080x1920 mp4) for review.

MVP of the video pipeline (gap #2). Content → voice → stills → captions → mp4:
  1. SCRIPT: take a source (a recent unclaimed-time column / article, or --text),
     condense to a punchy ~120-150 word first-person script in Nova's voice.
  2. VOICE: macOS `say` (voice configurable; swap to the XTTS clone later by
     replacing tts()). aiff -> wav, measure duration.
  3. STILLS: generate N images (nova_image_utils, OpenRouter path) from per-beat
     prompts the LLM writes.
  4. CAPTIONS: render each beat's caption to a transparent PNG with PIL (this
     ffmpeg build has no libass/drawtext, so we overlay PNGs).
  5. ASSEMBLE: per-beat ffmpeg clip (scale/crop to 1080x1920 + Ken-Burns zoompan +
     caption overlay), concat, mux the narration. Output to workspace/shorts/.

Nothing is uploaded — the file lands in a review folder (gap #3, YouTube upload,
is separate and needs the channel + OAuth). Runs local; cost = image gen only.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_memories")
OLLAMA_NODES = ["http://192.168.1.125:11434", "http://192.168.1.5:11434",   # batch pool: idle 24-thread Ryzens first (2026-10-01)
                "http://192.168.1.86:11434", "http://192.168.1.77:11434",
                "http://192.168.1.7:11434", "http://192.168.1.6:11434"]      # .251 was the Mac mini's stale DHCP lease; it is .77
LLM_MODEL = "qwen3:8b"
SAY_VOICE = "Samantha"          # swap for a Premium voice or the XTTS clone later
OUT_DIR = Path.home() / ".openclaw" / "workspace" / "shorts"
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
W, H, FPS = 1080, 1920, 30


def log(m): print(f"[short-video {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, max_tokens=500, temperature=0.6):
    body = json.dumps({"model": LLM_MODEL, "stream": False, "think": False,
                       "options": {"temperature": temperature, "num_predict": max_tokens},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    for node in OLLAMA_NODES:
        try:
            req = urllib.request.Request(node + "/api/chat", method="POST",
                                         headers={"Content-Type": "application/json"}, data=body)
            with urllib.request.urlopen(req, timeout=90) as r:
                out = json.load(r).get("message", {}).get("content", "").strip()
            if out:
                return out
        except Exception:
            continue
    return ""


def source_text(args):
    if args.text:
        return args.text
    import psycopg2
    mem = psycopg2.connect(OPS_DSN); mem.autocommit = True; c = mem.cursor()
    # default: the most recent unclaimed-time pursuit or article
    c.execute("SELECT text FROM memories WHERE source IN ('unclaimed','nova_articles','episodic') "
              "AND length(text) > 200 ORDER BY created_at DESC LIMIT 1")
    r = c.fetchone()
    return r[0] if r else ""


def afprobe_dur(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nk=1:nw=1", path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def render_caption_png(text, path):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fnt = ImageFont.truetype(FONT, 64)
    # wrap to ~24 chars/line
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= 24:
            cur = (cur + " " + w).strip()
        else:
            lines.append(cur); cur = w
    if cur:
        lines.append(cur)
    lh = 82
    total = lh * len(lines)
    y = H - 420 - total  # sit in the lower third
    for ln in lines:
        bb = d.textbbox((0, 0), ln, font=fnt)
        x = (W - (bb[2] - bb[0])) // 2
        # stroke for legibility over any image
        d.text((x, y), ln, font=fnt, fill=(255, 255, 255, 255),
               stroke_width=6, stroke_fill=(0, 0, 0, 235))
        y += lh
    img.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="", help="explicit script/source text (else pulls latest writing)")
    ap.add_argument("--beats", type=int, default=5, help="number of image beats")
    ap.add_argument("--voice", default=SAY_VOICE)
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    src = source_text(args)
    if not src or len(src) < 60:
        log("no source text"); return 1

    # 1. SCRIPT
    script = llm(
        "You are Nova. Turn the material below into a spoken script for a ~50-second "
        "vertical short — punchy, first-person, your dry voice, ~120-150 words, no stage "
        "directions, no headings, just the words you'd say aloud. Open with a hook.\n\n"
        f"MATERIAL:\n{src[:1800]}", max_tokens=350, temperature=0.7).strip()
    if len(script) < 40:
        log("script generation failed"); return 1
    log(f"script: {len(script.split())} words")

    # per-beat caption chunks (split script into `beats` roughly-equal parts by sentence)
    import re
    sents = [s.strip() for s in re.split(r'(?<=[.!?])\s+', script) if s.strip()]
    n = max(2, min(args.beats, len(sents)))
    chunk = max(1, len(sents) // n)
    beats = [" ".join(sents[i:i + chunk]) for i in range(0, len(sents), chunk)][:n]
    n = len(beats)

    # 2. VOICE
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    work = Path(tempfile.mkdtemp(prefix="short_"))
    aiff, wav = work / "n.aiff", work / "n.wav"
    subprocess.run(["say", "-v", args.voice, "-o", str(aiff), script], check=True, timeout=120)
    subprocess.run(["ffmpeg", "-y", "-i", str(aiff), "-ar", "44100", "-ac", "2", str(wav)],
                   capture_output=True, timeout=60)
    dur = afprobe_dur(str(wav))
    if dur <= 0:
        log("tts failed"); return 1
    seg = dur / n
    log(f"narration {dur:.1f}s across {n} beats ({seg:.1f}s each)")

    # 3. STILLS + 4. CAPTIONS + per-beat clips
    import nova_image_utils as iu
    clips = []
    for i, beat in enumerate(beats):
        prompt = llm(f"One vivid image-generation prompt (no text in the image) for this line "
                     f"of Nova's video: \"{beat}\". Output only the prompt.", max_tokens=80).strip().strip('"')[:300] or beat[:120]
        try:
            img = iu.generate_image(prompt, width=1024, height=1024, section="operations")
        except Exception as e:
            log(f"beat {i} image gen failed: {e}"); img = None
        if not img or not Path(img).exists():
            log(f"beat {i}: no image — skipping caption-only clip"); continue
        cap = work / f"cap_{i}.png"
        render_caption_png(beat, str(cap))
        clip = work / f"clip_{i}.mp4"
        frames = max(1, int(seg * FPS))
        # scale+crop to vertical, Ken-Burns zoom, overlay caption
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"zoompan=z='min(zoom+0.0012,1.15)':d={frames}:s={W}x{H}:fps={FPS}")
        r = subprocess.run(
            ["ffmpeg", "-y", "-loop", "1", "-i", img, "-i", str(cap),
             "-filter_complex", f"[0:v]{vf}[bg];[bg][1:v]overlay=0:0",
             "-t", f"{seg:.3f}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS), str(clip)],
            capture_output=True, text=True, timeout=180)
        if clip.exists():
            clips.append(clip)
        else:
            log(f"beat {i} clip failed: {r.stderr[-200:]}")

    if not clips:
        log("no clips rendered"); return 1

    # 5. CONCAT + MUX audio
    concat = work / "concat.txt"
    concat.write_text("".join(f"file '{c}'\n" for c in clips))
    silent = work / "silent.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
                    "-c", "copy", str(silent)], capture_output=True, timeout=120)
    out = OUT_DIR / f"nova-short-{ts}.mp4"
    r = subprocess.run(["ffmpeg", "-y", "-i", str(silent), "-i", str(wav),
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)],
                       capture_output=True, text=True, timeout=120)
    if not out.exists():
        log(f"final mux failed: {r.stderr[-200:]}"); return 1
    log(f"DONE → {out}  ({afprobe_dur(str(out)):.1f}s, {len(clips)} beats)")
    # save the script alongside for review
    (OUT_DIR / f"nova-short-{ts}.txt").write_text(script)
    print(str(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
