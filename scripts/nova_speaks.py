#!/usr/bin/env python3
"""nova_speaks.py — "Nova Speaks: <subject>": a journal article read aloud in a cloned voice over a
slow rotation of images, as a 1920x1080 mp4 for Jordan to review and upload to YouTube.

Jordan's idea (2026-10-03): not just blog posts — videos. Voice = XTTS v2 clone from a reference clip
(default: XTTS studio speaker "Gracie Wise" — Nova's voice, chosen by Jordan 2026-10-03; --voice may also be a .wav to clone). Images = the
article's cover plus FLUX renders (or any PNG/WEBP you point at), rotated per paragraph group with a
Ken Burns move and the current chapter title as a lower-third. Title card + end card with the URL.
Nothing is uploaded: the mp4 lands in the review folder and a Slack note says where.

  nova_speaks.py --article content/essays/2026-10-03-foo.md            # full article
  nova_speaks.py --article ... --sections "Abstract,Chapter 2,Conclusion" --suffix preview
  nova_speaks.py --article ... --images img1.png img2.webp ...        # your own stills
  nova_speaks.py --article ... --generate-images 5                    # FLUX renders from chapter titles
Env: NOVA_SPEAKS_VOICE (ref wav), NOVA_SPEAKS_OUT (review dir).
ponytail: XTTS runs ~0.67x real time on the Studio GPU; a 5,000-word article is ~50 min of synthesis.
Captions are a chapter lower-third, not word-level; add mlx-whisper .srt generation when wanted.
"""
import argparse, os, re, subprocess, sys, json, glob, random, math, time
from pathlib import Path

HOME = Path.home()
OUT_DIR = Path(os.environ.get("NOVA_SPEAKS_OUT", "/Volumes/nas/nova-fs/videos/review"))
VOICE = os.environ.get("NOVA_SPEAKS_VOICE", "Gracie Wise")   # XTTS speaker name, or a path to a reference .wav
TTS_HOME = "/Volumes/Data/AI/tts"
W, H = 1920, 1080
FONT = next((f for f in ["/System/Library/Fonts/Supplemental/Avenir Next.ttc", "/System/Library/Fonts/HelveticaNeue.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"] if os.path.exists(f)), None)


def log(m): print(f"[nova-speaks {time.strftime('%H:%M:%S')}] {m}", flush=True)


# ── script from markdown ─────────────────────────────────────────────────────────────────
def article_to_script(md: str, sections: list[str] | None):
    body = re.sub(r"^---.*?---\s*", "", md, count=1, flags=re.S)         # frontmatter
    body = re.sub(r"^\*Published .*?\*\s*$", "", body, flags=re.M)         # dateline lines
    body = re.sub(r"^\*Burbank .*?\*\s*$", "", body, flags=re.M)
    body = re.sub(r"```.*?```", "", body, flags=re.S)                  # fenced code / mermaid: never read aloud
    title = None
    chapters = []   # (heading, [paragraphs])
    cur_h, cur_p = "Opening", []
    for block in re.split(r"\n\s*\n", body):
        b = block.strip()
        if not b: continue
        if b.startswith("#"):
            if cur_p: chapters.append((cur_h, cur_p))
            cur_h, cur_p = re.sub(r"^#+\s*", "", b).strip(" *"), []
            continue
        if b.startswith(("|", "```", "<", "![")): continue
        t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", b)                      # links -> text
        t = re.sub(r"\[\d+\]", "", t)                                       # citation markers
        t = re.sub(r"https?://\S+", "", t)
        t = re.sub(r"[*_`>#]+", "", t)
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) > 20: cur_p.append(t)
    if cur_p: chapters.append((cur_h, cur_p))
    drop = ("references", "sources & attribution", "sources and attribution")
    chapters = [(h, ps) for h, ps in chapters if not h.lower().startswith(drop)]
    if sections:
        want = [s.lower() for s in sections]
        chapters = [(h, ps) for h, ps in chapters if any(h.lower().startswith(w) for w in want)]
    return chapters


# ── voice ────────────────────────────────────────────────────────────────────────────────
def load_tts():
    os.environ.setdefault("TTS_HOME", TTS_HOME); os.environ.setdefault("COQUI_TOS_AGREED", "1")
    import numpy as np, torch, torchaudio
    def _load(path, *a, **k):
        import soundfile as sf
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        return torch.from_numpy(np.ascontiguousarray(data.T)), sr
    torchaudio.load = _load
    import TTS.tts.models.xtts as xm
    if hasattr(xm, "torchaudio"): xm.torchaudio.load = _load
    from TTS.api import TTS
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
    dev = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
    try: tts.to(dev)
    except Exception: tts.to("cpu")
    log(f"tts device: {dev}")
    return tts


def speak(tts, text, path):
    # XTTS handles sentence splitting; keep chunks < ~600 chars to stay well inside its window
    def pieces(sent, limit=230):
        # XTTS asserts < 400 tokens per call; a single run-on sentence (lists, ledgers) can exceed it. Split long
        # sentences at clause boundaries, then hard-wrap at a word boundary as a last resort.
        if len(sent) <= limit: return [sent]
        out, cur = [], ""
        for cl in re.split(r"(?<=[;:,])\s+|\s+[—–]\s+", sent):
            if len(cur) + len(cl) > limit and cur: out.append(cur); cur = cl
            else: cur = (cur + " " + cl).strip()
        if cur: out.append(cur)
        final = []
        for o in out:
            while len(o) > limit:
                cut = o.rfind(" ", 0, limit); cut = cut if cut > 40 else limit
                final.append(o[:cut].strip()); o = o[cut:].strip()
            if o: final.append(o)
        return final
    parts, cur = [], ""
    for sent in (pc for s0 in re.split(r"(?<=[.!?])\s+", text) for pc in pieces(s0)):
        if len(cur) + len(sent) > 400 and cur: parts.append(cur); cur = sent
        else: cur = (cur + " " + sent).strip()
    if cur: parts.append(cur)
    wavs = []
    for i, p in enumerate(parts):
        w = f"{path}.{i}.wav"
        kw = {"speaker_wav": str(VOICE)} if str(VOICE).endswith(".wav") else {"speaker": str(VOICE)}
        tts.tts_to_file(text=p, language="en", file_path=w, **kw); wavs.append(w)
    if len(wavs) == 1: os.replace(wavs[0], path)
    else:
        lst = f"{path}.txt"; open(lst, "w").write("".join(f"file '{w}'\n" for w in wavs))
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", path], check=True)
        for w in wavs: os.remove(w)
        os.remove(lst)
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip() or 0)


# ── stills + cards ───────────────────────────────────────────────────────────────────────
def to_png(src, dst):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}", dst], check=True)


def card(text, sub, path, bg=(11, 15, 26)):
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (W, H), bg); d = ImageDraw.Draw(im)
    f1 = ImageFont.truetype(FONT, 110) if FONT else ImageFont.load_default()
    f2 = ImageFont.truetype(FONT, 46) if FONT else ImageFont.load_default()
    d.text((W // 2, H // 2 - 70), text, fill=(139, 92, 246), font=f1, anchor="mm")
    y = H // 2 + 50
    for line in wrap(sub, 60):
        d.text((W // 2, y), line, fill=(202, 211, 245), font=f2, anchor="mm"); y += 62
    d.rectangle([W // 2 - 220, H // 2 - 150, W // 2 + 220, H // 2 - 146], fill=(34, 211, 238))
    im.save(path)


def lower_third(text, path):
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    f = ImageFont.truetype(FONT, 40) if FONT else ImageFont.load_default()
    tw = d.textlength(text, font=f)
    d.rectangle([60, H - 150, 60 + tw + 60, H - 70], fill=(11, 15, 26, 190))
    d.rectangle([60, H - 150, 68, H - 70], fill=(139, 92, 246, 255))
    d.text((90, H - 110), text, fill=(202, 211, 245), font=f, anchor="lm")
    im.save(path)


def wrap(s, n):
    out, cur = [], ""
    for w in s.split():
        if len(cur) + len(w) + 1 > n and cur: out.append(cur); cur = w
        else: cur = (cur + " " + w).strip()
    if cur: out.append(cur)
    return out


def clip(img, dur, out, overlay=None, zoom_in=True):
    frames = max(2, int(dur * 25))
    z = f"min(zoom+0.0006,1.18)" if zoom_in else f"if(eq(on,1),1.18,max(zoom-0.0006,1.0))"
    vf = f"scale=2400:1350,zoompan=z='{z}':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps=25,format=yuv420p"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-i", img]
    if overlay:
        cmd += ["-i", overlay, "-filter_complex", f"[0:v]{vf}[v];[v][1:v]overlay=0:0:format=auto,format=yuv420p[o]", "-map", "[o]"]
    else:
        cmd += ["-vf", vf]
    cmd += ["-t", f"{dur:.3f}", "-r", "25", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", out]
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--article", required=True); ap.add_argument("--sections", default=None)
    ap.add_argument("--images", nargs="*", default=None); ap.add_argument("--generate-images", type=int, default=0)
    ap.add_argument("--suffix", default=""); ap.add_argument("--subject", default=None)
    ap.add_argument("--url", default=None); ap.add_argument("--voice", default=None)
    a = ap.parse_args()
    global VOICE
    if a.voice: VOICE = a.voice
    md = Path(a.article).read_text()
    title = re.search(r'^title:\s*"?(.+?)"?\s*$', md, re.M).group(1).strip('" ')
    title = re.sub(r"^[^\w]+", "", title)                                   # drop leading emoji
    slug = Path(a.article).stem
    section = Path(a.article).parent.name
    url = a.url or f"https://nova.digitalnoise.net/{section}/{slug}/"
    subject = a.subject or title.split(":")[0]
    chapters = article_to_script(md, [s.strip() for s in a.sections.split(",")] if a.sections else None)
    words = sum(len(p.split()) for _, ps in chapters for p in ps)
    log(f"{len(chapters)} chapters, {words} words, ~{words/150:.0f} min of speech")
    work = Path(f"{HOME}/.openclaw/workspace/speaks/{slug}{('-'+a.suffix) if a.suffix else ''}"); work.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # images: cover + given/generated/random recent renders
    imgs = []
    cover = glob.glob(f"{HOME}/nova-journal/static/images/{section}/{slug}.webp")
    if cover: imgs.append(cover[0])
    if a.images: imgs += a.images
    if a.generate_images:
        sys.path.insert(0, str(HOME / ".openclaw/scripts"))
        from nova_image_utils import generate_image
        for h, _ in chapters[:a.generate_images]:
            p = generate_image(f"cinematic illustration for a chapter titled '{h}' of an essay by an AI about {subject}; painterly, dark navy and violet and cyan, no text, no logos", 1024, 768, section=section)
            if p: imgs.append(p)
    if len(imgs) < 3:
        # other recent covers from the same journal section (works on every render host, not just the Studio)
        others = [f for f in sorted(glob.glob(f"{HOME}/nova-journal/static/images/{section}/*.webp"), key=os.path.getmtime)[-16:] if f not in imgs]
        random.shuffle(others); imgs += others[: max(0, 5 - len(imgs))]
    if len(imgs) < 3:
        recent = sorted(glob.glob(f"{HOME}/.openclaw/workspace/*.png"), key=os.path.getmtime)[-12:]
        random.shuffle(recent); imgs += recent[: max(0, 5 - len(imgs))]
    stills = []
    for i, im in enumerate(imgs):
        p = str(work / f"still{i}.png"); to_png(im, p); stills.append(p)
    log(f"{len(stills)} stills")

    # voice per paragraph
    tts = load_tts()
    segs = []  # (chapter, wav, dur)
    t0 = time.time()
    for ci, (h, ps) in enumerate(chapters):
        for pi, p in enumerate(ps):
            wav = str(work / f"c{ci:02d}p{pi:03d}.wav")
            if not os.path.exists(wav):
                speak(tts, p, wav)
            dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", wav], capture_output=True, text=True).stdout.strip() or 0)
            segs.append((h, wav, dur))
        log(f"voiced chapter {ci+1}/{len(chapters)} '{h[:40]}' ({time.time()-t0:.0f}s elapsed)")
    narration = str(work / "narration.wav")
    open(work / "narr.txt", "w").write("".join(f"file '{w}'\n" for _, w, _ in segs))
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(work / "narr.txt"), "-af", "loudnorm=I=-16:TP=-1.5", narration], check=True)
    total = sum(d for _, _, d in segs)
    log(f"narration {total/60:.1f} min")

    # video: title card, one clip per paragraph (image rotates per paragraph, chapter lower-third), end card
    card("NOVA SPEAKS", subject, str(work / "title.png"))
    card("nova.digitalnoise.net", f"{title}\n\n{url}\n\nNarration is an AI voice. Written by Nova.", str(work / "end.png"))
    clips = []
    t_card = str(work / "clip_title.mp4"); clip(str(work / "title.png"), 4.0, t_card, zoom_in=False); clips.append(t_card)
    last_h = None
    for i, (h, wav, dur) in enumerate(segs):
        if h != last_h:
            lower_third(h, str(work / f"lt{i}.png")); last_h = h
        lt = str(work / f"lt{[j for j in range(i+1) if os.path.exists(work / f'lt{j}.png')][-1]}.png")
        c = str(work / f"clip{i:04d}.mp4")
        clip(stills[i % len(stills)], dur + 0.3, c, overlay=lt, zoom_in=(i % 2 == 0)); clips.append(c)
    e_card = str(work / "clip_end.mp4"); clip(str(work / "end.png"), 6.0, e_card, zoom_in=False); clips.append(e_card)
    open(work / "clips.txt", "w").write("".join(f"file '{c}'\n" for c in clips))
    silent = str(work / "video_silent.mp4")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(work / "clips.txt"), "-c", "copy", silent], check=True)
    out = OUT_DIR / f"NovaSpeaks-{slug}{('-'+a.suffix) if a.suffix else ''}.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", silent, "-itsoffset", "4", "-i", narration, "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", str(out)], check=True)
    log(f"DONE {out} ({os.path.getsize(out)/1e6:.0f} MB, {total/60+0.2:.1f} min)")
    try:
        sys.path.insert(0, str(HOME / ".openclaw/scripts"))
        from nova_notify import notify
        notify(f"🎬 Nova Speaks ready for review: {subject}", f"{out}\n{total/60:.1f} min · {len(segs)} paragraphs · {len(stills)} stills\nArticle: {url}", level="info", category="video", source="nova_speaks", dedup_key=f"nova-speaks:{slug}:{a.suffix}")
    except Exception as e:
        log(f"notify skipped: {e}")
    print(str(out))


if __name__ == "__main__":
    main()
