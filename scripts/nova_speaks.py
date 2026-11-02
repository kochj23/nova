#!/usr/bin/env python3
"""nova_speaks.py — "Nova Speaks: <subject>": a journal article read aloud in a cloned voice over a
slow rotation of images, as a 1920x1080 mp4 for Jordan to review and upload to YouTube.

Jordan's idea (2026-10-03): not just blog posts — videos. Voice = XTTS v2 clone from a reference clip
(default: XTTS studio speaker "Gracie Wise" — Nova's voice, chosen by Jordan 2026-10-03; --voice may also be a .wav to clone). Images = the
article's cover plus FLUX renders (or any PNG/WEBP you point at), rotated per paragraph group with a
Ken Burns move and the current chapter title as a lower-third. Title card + end card with the URL.
Nothing is uploaded: the mp4 lands in the review folder and a Slack note says where.

  nova_speaks.py --article content/nova/2026-10-03-foo.md                # full article
  nova_speaks.py --article ... --sections "Abstract,Chapter 2,Conclusion" --suffix preview
  nova_speaks.py --article ... --images img1.png img2.webp ...        # your own stills
  nova_speaks.py --article ... --generate-images 5                    # FLUX renders from chapter titles
  nova_speaks.py --article ... --script-only                          # narration script (.txt) only, no TTS
Narration stage (nova_speaks_narration.py, 2026-10-06): foreign phrases introduced from the PG phrasebook, each paragraph
rewritten for the ear by the local qwen3:8b (grounded, fail-open), spoken-form normalization (°F, times, units, acronyms…),
and a Whisper back-check of every voiced part with seed retries. The exact spoken script lands next to the mp4 as .txt and
a QUALITY {json} line goes to the log (the sweep stores it in nova_speaks_renders.quality).
Env: NOVA_SPEAKS_VOICE (ref wav), NOVA_SPEAKS_OUT (review dir), NOVA_SPEAKS_REWRITE=0 (skip the LLM pass).
ponytail: XTTS runs ~0.67x real time on the Studio GPU; a 5,000-word article is ~50 min of synthesis.
Captions are a chapter lower-third, not word-level; add mlx-whisper .srt generation when wanted.
SONG CLIPS (2026-11-02, Jordan's daily liked-music article): a paragraph that is exactly a Hugo
  {{< youtube id="<11>" start="S" end="E" title="..." >}}
becomes a clip: the video plays that stretch of the source video (picture + sound) at that point. The clip file is
pre-cut by nova_liked_music.py to <OUT_DIR>/../clips/<id>-<S>-<E>.mp4 (NAS, so every render host has it); a
missing file is skipped (narration only). Articles without such embeds render exactly as before. In clip mode the
narration audio is padded 0.3 s per paragraph to match the picture, so a clip's sound and picture stay in sync.
"""
import argparse, os, re, subprocess, sys, json, glob, random, math, time, hashlib
from pathlib import Path

HOME = Path.home()
OUT_DIR = Path(os.environ.get("NOVA_SPEAKS_OUT", "/Volumes/nas/nova-fs/videos/review"))
VOICE = os.environ.get("NOVA_SPEAKS_VOICE", "Gracie Wise")   # XTTS speaker name, or a path to a reference .wav
TTS_HOME = "/Volumes/Data/AI/tts"
W, H = 1920, 1080
CLIP_RX = re.compile(r'^\{\{<\s*youtube\s+id="([\w-]{11})"\s+start="(\d+)"\s+end="(\d+)"(?:\s+title="([^"]*)")?\s*>\}\}$')
CLIP = "\x00CLIP"          # marker paragraph / pseudo-chapter heading: "\x00CLIP <id> <start> <end> <title>"
FONT = next((f for f in ["/System/Library/Fonts/Supplemental/Avenir Next.ttc", "/System/Library/Fonts/HelveticaNeue.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"] if os.path.exists(f)), None)


def log(m): print(f"[nova-speaks {time.strftime('%H:%M:%S')}] {m}", flush=True)


# ── script from markdown ─────────────────────────────────────────────────────────────────
def scrub_chapters(chapters):
    """The narration goes to PUBLIC YouTube, so it gets the journal's publish scrub (household names, e-mail, MACs,
    home paths) after the rewrite. 2026-10-13 Card-Rack: three uploaded narrations named household members because
    they were rendered from articles before the journal scrubbed them. No fallback: a host that cannot import the
    scrubber fails the render rather than speaking unscrubbed text."""
    import nova_journal as nj
    clean = lambda t: t if t.startswith(CLIP) else nj.scrub_home_paths(nj.scrub_pii(t))  # noqa: E731
    return [(clean(h), [clean(p) for p in ps]) for h, ps in chapters]


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
        m = CLIP_RX.match(b)
        if m:                                   # a cued song clip: played in the video, never read aloud
            cur_p.append(f"{CLIP} {m.group(1)} {m.group(2)} {m.group(3)} {m.group(4) or ''}".rstrip()); continue
        if b.startswith("#"):
            if cur_p: chapters.append((cur_h, cur_p))
            cur_h, cur_p = re.sub(r"^#+\s*", "", b).strip(" *"), []
            continue
        if b.startswith(("|", "```", "<", "![")): continue
        # tables inside a block are never read; list items become sentences instead of one run-on
        lines = [ln for ln in b.splitlines() if not ln.lstrip().startswith("|")]
        lines = [(lambda x: x if re.search(r"[.!?:;]$", x) else x + ".")(re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", ln).strip())
                 if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", ln) else ln for ln in lines]
        b = "\n".join(lines)
        t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", b)                      # links -> text
        t = re.sub(r"\[\d+\]", "", t)                                       # citation markers
        t = re.sub(r"https?://\S+", "", t)
        t = re.sub(r"[*_`>#]+", "", t)
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) > 20 or re.search(r"[A-Za-z]{2,}", t): cur_p.append(t)     # short ones ("Qapla'.") merge into a neighbour later
    if cur_p: chapters.append((cur_h, cur_p))
    drop = ("references", "sources & attribution", "sources and attribution")
    chapters = [(h, ps) for h, ps in chapters if not h.lower().startswith(drop)]
    if sections:
        want = [s.lower() for s in sections]
        chapters = [(h, ps) for h, ps in chapters if any(h.lower().startswith(w) for w in want)]
    return chapters


def clip_runs(chapters):
    """[(h, [p, CLIP, p])] -> [(h, [p]), (CLIP..., []), (h, [p])], order kept. Pure."""
    out = []
    for h, ps in chapters:
        run = []
        for p in ps:
            if p.startswith(CLIP):
                if run: out.append((h, run)); run = []
                out.append((p, []))
            else:
                run.append(p)
        if run: out.append((h, run))
    return out


def narrate_with_clips(chapters, nn):
    """The narration stage merges and drops paragraphs, so clips are pulled out first and each text run is
    narrated on its own; clip pseudo-chapters pass through untouched, in place."""
    out, stats, cache, book = [], {}, nn.RewriteCache(), nn.load_phrasebook()
    for h, ps in clip_runs(chapters):
        if h.startswith(CLIP):
            out.append((h, [])); continue
        r, st = nn.narrate([(h, ps)], book=book, cache=cache)
        for k, v in st.items(): stats[k] = stats.get(k, 0) + v
        out += r
    return out, stats


def parse_clip(marker):
    """'\x00CLIP id 51 66 Talk Talk - It's My Life' -> (id, 51, 66, title). Pure."""
    _, vid, a, b, *t = marker.split(" ")
    return vid, int(a), int(b), " ".join(t)


def clip_file(vid, a, b):
    return OUT_DIR.parent / "clips" / f"{vid}-{a}-{b}.mp4"


def song_clip(src, dur, label, work, i):
    """(video mp4 at the house format, 48 kHz stereo wav), both exactly `dur` long."""
    lt = str(work / f"ltc{i}.png"); lower_third(label, lt)
    v, a = str(work / f"song{i:03d}.mp4"), str(work / f"song{i:03d}.wav")
    vf = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x0b0f1a,"
          f"fps=25,format=yuv420p[v];[v][1:v]overlay=0:0:format=auto,format=yuv420p[o]")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-i", lt, "-filter_complex", vf,
                    "-map", "[o]", "-an", "-t", f"{dur:.3f}", "-r", "25", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "20", "-pix_fmt", "yuv420p", v], check=True)
    fo = max(0.1, dur - 0.8)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-vn", "-t", f"{dur:.3f}",
                    "-af", f"afade=t=in:d=0.3,afade=t=out:st={fo:.3f}:d=0.8,loudnorm=I=-18:TP=-2,apad,atrim=0:{dur:.3f}",
                    "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", a], check=True)
    return v, a


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


def split_parts(text):
    """≤400-char parts on sentence boundaries; fragments under 4 words ride with a neighbour (XTTS growls on them)."""
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
    for s0 in re.split(r"(?<=[.!?])\s+", text):
        ps = pieces(s0)
        if len(ps) > 1:                 # a split run-on: each piece is its own part, or XTTS sees one >250-char sentence
            if cur: parts.append(cur); cur = ""
            parts += ps[:-1]; cur = ps[-1]; continue
        sent = ps[0]
        if len(cur) + len(sent) > 400 and cur and len(sent.split()) >= 4: parts.append(cur); cur = sent
        else: cur = (cur + " " + sent).strip()
    if cur: parts.append(cur)
    if len(parts) > 1 and len(parts[-1].split()) < 4: last = parts.pop(); parts[-1] += " " + last
    return parts


def _dur(path):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip() or 0)


def speak(tts, text, path, checker=None, stats=None):
    """Voice one paragraph. Each part is back-checked (Whisper WER + duration sanity) and re-synthesized with another
    seed when it reads as garbage — XTTS v2 occasionally growls."""
    import nova_speaks_narration as nn
    stats = stats if stats is not None else nn.new_stats(getattr(checker, "backend", "none"))
    kw = {"speaker_wav": str(VOICE)} if str(VOICE).endswith(".wav") else {"speaker": str(VOICE)}

    def synth(part, wav, seed):
        try:
            import torch; torch.manual_seed(seed)
        except Exception:
            pass
        random.seed(seed)
        tts.tts_to_file(text=part, language="en", file_path=wav, **kw, **nn.XTTS_KW)
    wavs = []
    for i, p in enumerate(split_parts(text)):
        w = f"{path}.{i}.wav"
        nn.synth_checked(synth, p, w, checker, stats, _dur, seed_base=int(hashlib.md5(p.encode()).hexdigest()[:6], 16))
        wavs.append(w)
    if len(wavs) == 1: os.replace(wavs[0], path)
    else:
        lst = f"{path}.txt"; open(lst, "w").write("".join(f"file '{w}'\n" for w in wavs))
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", path], check=True)
        for w in wavs: os.remove(w)
        os.remove(lst)
    return _dur(path)


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


MOTIONS = ("in-center", "out-center", "pan-right", "pan-left", "in-topleft", "in-bottomright", "pan-up", "pan-down")


def motion_expr(motion, frames):
    """Ken Burns variants (Jordan 2026-10-19: the same move every shot is boring). Returns (z, x, y) zoompan exprs."""
    zin, zout = "min(zoom+0.0006,1.18)", "if(eq(on,1),1.18,max(zoom-0.0006,1.0))"
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    f = max(1, frames)
    return {
        "in-center": (zin, cx, cy),
        "out-center": (zout, cx, cy),
        "pan-right": ("1.15", f"(iw-iw/zoom)*on/{f}", cy),
        "pan-left": ("1.15", f"(iw-iw/zoom)*(1-on/{f})", cy),
        "in-topleft": (zin, "0", "0"),
        "in-bottomright": (zin, "iw-iw/zoom", "ih-ih/zoom"),
        "pan-up": ("1.15", cx, f"(ih-ih/zoom)*(1-on/{f})"),
        "pan-down": ("1.15", cx, f"(ih-ih/zoom)*on/{f}"),
    }.get(motion, (zin, cx, cy))


def plan_shots(n_segments, n_stills, rng=random):
    """Which still each paragraph shows: holds of 1-3 paragraphs (varied timing), walking the picked stills in
    order (the picker already ranked them), never the same still twice in a row. -> [(still_index, motion)]"""
    plan, s, i, last_motion = [], 0, 0, None
    while i < n_segments:
        hold = rng.choice((1, 1, 2, 2, 3))
        motion = rng.choice([m for m in MOTIONS if m != last_motion])
        for _ in range(min(hold, n_segments - i)):
            plan.append((s % max(1, n_stills), motion))
            i += 1
        last_motion = motion
        s += 1
        if n_stills > 1 and s % n_stills == 0 and plan and plan[-1][0] == 0:
            s += 1                                   # wrap-around never repeats the still just shown
    return plan


def clip(img, dur, out, overlay=None, zoom_in=True, motion=None):
    frames = max(2, int(dur * 25))
    z, x, y = motion_expr(motion or ("in-center" if zoom_in else "out-center"), frames)
    vf = f"scale=2400:1350,zoompan=z='{z}':d={frames}:x='{x}':y='{y}':s={W}x{H}:fps=25,format=yuv420p"
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
    ap.add_argument("--script-only", action="store_true", help="write the narration script (.txt) and stop before TTS")
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
    # narration stage: foreign phrases introduced, rewritten for the ear (local model, grounded), spoken form
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import nova_speaks_narration as nn
    t_n = time.time()
    has_clips = any(p.startswith(CLIP) for _, ps in chapters for p in ps)
    if has_clips:
        chapters, rstats = narrate_with_clips(chapters, nn)
    else:
        chapters, rstats = nn.narrate(chapters, cache=nn.RewriteCache())
    log(f"narration: {rstats} ({time.time()-t_n:.0f}s)")
    chapters = scrub_chapters(chapters)
    words = sum(len(p.split()) for _, ps in chapters for p in ps)
    log(f"{len(chapters)} chapters, {words} words, ~{words/150:.0f} min of speech")
    work = Path(f"{HOME}/.openclaw/workspace/speaks/{slug}{('-'+a.suffix) if a.suffix else ''}"); work.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    script_txt = OUT_DIR / f"NovaSpeaks-{slug}{('-'+a.suffix) if a.suffix else ''}.txt"     # exactly what the voice was given
    script_txt.write_text(f"{title}\n{url}\n\n" + "\n\n".join(
        (f"## [song clip {' '.join(h.split(' ')[1:4])}]" if h.startswith(CLIP) else f"## {h}\n\n" + "\n\n".join(ps))
        for h, ps in chapters) + "\n")
    log(f"script: {script_txt}")
    if a.script_only:
        print(str(script_txt)); return

    # images: cover + given/generated/random recent renders
    imgs = []
    cover = glob.glob(f"{HOME}/nova-journal/static/images/{section}/{slug}.webp")
    fm_cover = re.search(r'^\s*image:\s*"?(/images/[^"\s]+)', md, re.M)       # frontmatter cover when its name != slug
    if fm_cover: cover += glob.glob(f"{HOME}/nova-journal/static{fm_cover.group(1)}")
    if cover: imgs.append(cover[0])
    if a.images: imgs += a.images
    if a.generate_images:
        sys.path.insert(0, str(HOME / ".openclaw/scripts"))
        from nova_image_utils import generate_image
        for h, _ in chapters[:a.generate_images]:
            p = generate_image(f"cinematic illustration for a chapter titled '{h}' of an essay by an AI about {subject}; painterly, dark navy and violet and cyan, no text, no logos", 1024, 768, section=section)
            if p: imgs.append(p)
    if section == "art" and imgs:
        imgs = imgs[:1]                      # Jordan 2026-10-05: art videos show the artwork itself only — no rotation through other covers
    if len(imgs) < 3 and section != "art":
        # other recent covers from the same journal section (works on every render host, not just the Studio)
        others = [f for f in sorted(glob.glob(f"{HOME}/nova-journal/static/images/{section}/*.webp"), key=os.path.getmtime)[-16:] if f not in imgs]
        random.shuffle(others); imgs += others[: max(0, 5 - len(imgs))]
    if len(imgs) < 3 and section != "art":
        recent = sorted(glob.glob(f"{HOME}/.openclaw/workspace/*.png"), key=os.path.getmtime)[-12:]
        random.shuffle(recent); imgs += recent[: max(0, 5 - len(imgs))]
    stills = []
    for i, im in enumerate(imgs):
        p = str(work / f"still{i}.png"); to_png(im, p); stills.append(p)
    log(f"{len(stills)} stills")

    # voice per paragraph
    tts = load_tts()
    checker = nn.BackCheck()
    qstats = nn.new_stats(checker.backend)
    log(f"back-check: {checker.backend}")
    segs = []  # (chapter, wav, dur)
    t0 = time.time()
    songs = []      # (index in segs, clip mp4, dur, label) — clip mode only
    for ci, (h, ps) in enumerate(chapters):
        if h.startswith(CLIP):
            vid, ca, cb, ctitle = parse_clip(h)
            f = clip_file(vid, ca, cb)
            if f.exists():
                songs.append((len(segs), str(f), _dur(str(f)), f"Now playing: {ctitle[:60]}  {ca//60}:{ca%60:02d}"))
            else:
                log(f"clip missing, narration only: {f}")
            continue
        for pi, p in enumerate(ps):
            # the text hash is in the name: a re-render with a new script never reuses a stale wav
            wav = str(work / f"c{ci:02d}p{pi:03d}-{hashlib.sha1((p + json.dumps(nn.XTTS_KW)).encode()).hexdigest()[:10]}.wav")
            if not os.path.exists(wav):
                speak(tts, p, wav, checker, qstats)
            segs.append((h, wav, _dur(wav)))
        log(f"voiced chapter {ci+1}/{len(chapters)} '{h[:40]}' ({time.time()-t0:.0f}s elapsed)")
    if has_clips:
        out = assemble_with_clips(work, segs, songs, stills, title, url, subject, slug, a.suffix)
        total = sum(d for _, _, d in segs) + sum(s[2] for s in songs)
        return finish(out, total, qstats, rstats, segs, stills, subject, url, slug, a.suffix)
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
    shots = plan_shots(len(segs), len(stills))
    groups = []                                   # one continuous clip per held still (no jump cut mid-hold)
    for i, (h, wav, dur) in enumerate(segs):
        if h != last_h:
            lower_third(h, str(work / f"lt{i}.png")); last_h = h
        lt = str(work / f"lt{[j for j in range(i+1) if os.path.exists(work / f'lt{j}.png')][-1]}.png")
        key = (shots[i], lt)
        if groups and groups[-1][0] == key:
            groups[-1][1] += dur + 0.3
        else:
            groups.append([key, dur + 0.3])
    for gi, (((si, motion), lt), gdur) in enumerate(groups):
        c = str(work / f"clip{gi:04d}.mp4")
        clip(stills[si], gdur, c, overlay=lt, motion=motion); clips.append(c)
    e_card = str(work / "clip_end.mp4"); clip(str(work / "end.png"), 6.0, e_card, zoom_in=False); clips.append(e_card)
    open(work / "clips.txt", "w").write("".join(f"file '{c}'\n" for c in clips))
    silent = str(work / "video_silent.mp4")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(work / "clips.txt"), "-c", "copy", silent], check=True)
    out = OUT_DIR / f"NovaSpeaks-{slug}{('-'+a.suffix) if a.suffix else ''}.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", silent, "-itsoffset", "4", "-i", narration, "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", str(out)], check=True)
    return finish(out, total, qstats, rstats, segs, stills, subject, url, slug, a.suffix)


def finish(out, total, qstats, rstats, segs, stills, subject, url, slug, suffix):
    import nova_speaks_narration as nn
    q = nn.finish_stats(qstats); q["rewrite"] = rstats
    print("QUALITY " + json.dumps(q), flush=True)
    log(f"back-check: {q['parts']} parts, {q['retries']} retries, worst WER {q['worst_wer']}, mean WER {q['mean_wer']}, flagged {q['flagged']}")
    log(f"DONE {out} ({os.path.getsize(out)/1e6:.0f} MB, {total/60+0.2:.1f} min)")
    try:
        sys.path.insert(0, str(HOME / ".openclaw/scripts"))
        from nova_notify import notify
        notify(f"🎬 Nova Speaks ready for review: {subject}", f"{out}\n{total/60:.1f} min · {len(segs)} paragraphs · {len(stills)} stills\nArticle: {url}", level="info", category="video", source="nova_speaks", dedup_key=f"nova-speaks:{slug}:{suffix}")
    except Exception as e:
        log(f"notify skipped: {e}")
    print(str(out))



def assemble_with_clips(work, segs, songs, stills, title, url, subject, slug, suffix):
    """Clip mode: narration paragraphs (padded 0.3 s so picture and sound line up) interleaved with song clips."""
    card("NOVA SPEAKS", subject, str(work / "title.png"))
    card("nova.digitalnoise.net", f"{title}\n\n{url}\n\nNarration is an AI voice. Written by Nova.", str(work / "end.png"))
    vids, auds = [], []
    t_card = str(work / "clip_title.mp4"); clip(str(work / "title.png"), 4.0, t_card, zoom_in=False); vids.append(t_card)
    shots = plan_shots(len(segs), len(stills))
    at = {i: s for i, *s in songs}                 # segs index -> (mp4, dur, label): the clip plays BEFORE that paragraph
    last_h, lt = None, None
    tail = [s for i, *s in songs if i >= len(segs)]
    for i, (h, wav, dur) in enumerate(segs):
        if i in at:
            v, aw = song_clip(at[i][0], at[i][1], at[i][2], work, i); vids.append(v); auds.append(aw)
        if h != last_h:
            lt = str(work / f"lt{i}.png"); lower_third(h, lt); last_h = h
        si, motion = shots[i]
        c = str(work / f"clip{i:04d}.mp4"); clip(stills[si], dur + 0.3, c, overlay=lt, motion=motion); vids.append(c)
        pw = str(work / f"pad{i:04d}.wav")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", wav, "-af", f"apad,atrim=0:{dur + 0.3:.3f}",
                        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", pw], check=True)
        auds.append(pw)
    for j, (mp4, d, label) in enumerate(tail):
        v, aw = song_clip(mp4, d, label, work, 900 + j); vids.append(v); auds.append(aw)
    e_card = str(work / "clip_end.mp4"); clip(str(work / "end.png"), 6.0, e_card, zoom_in=False); vids.append(e_card)
    open(work / "clips.txt", "w").write("".join(f"file '{c}'\n" for c in vids))
    open(work / "auds.txt", "w").write("".join(f"file '{c}'\n" for c in auds))
    silent, sound = str(work / "video_silent.mp4"), str(work / "soundtrack.wav")
    # RE-ENCODE the join: a song clip's H.264 parameters differ from the stills', and a stream-copy concat left
    # every piece before the first clip undecodable (2026-11-02 test: title + first paragraph vanished).
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(work / "clips.txt"),
                    "-vf", "setsar=1", "-r", "25", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", silent], check=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(work / "auds.txt"), "-af", "loudnorm=I=-16:TP=-1.5", "-ar", "48000", sound], check=True)
    out = OUT_DIR / f"NovaSpeaks-{slug}{('-'+suffix) if suffix else ''}.mp4"
    # audio padded with silence to the picture's length, so the end card is not cut off
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", silent, "-itsoffset", "4", "-i", sound,
                    "-filter_complex", "[1:a]apad[a]", "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)], check=True)
    log(f"clip mode: {len(songs)} song clips, {len(segs)} paragraphs")
    return out


if __name__ == "__main__":
    main()
