#!/usr/bin/env python3
"""nova_hear.py — let Nova "hear" a track: measurements + zero-shot recognition from the audio itself.

Prototype (Jordan 2026-10-19). Two levels:
  1. measurement: tempo, key/mode (Krumhansl), loudness (EBU R128 via ffmpeg), dynamics, sections
  2. recognition: CLAP (laion/larger_clap_music_and_speech) zero-shot scores for genre, instruments, vocals, mood,
     production traits — whole track and per section (vocal presence per section comes from here)

Usage:  nova_hear.py <audio file> [--no-clap] [--out file.json]
        nova_hear.py --selftest
Output: JSON on stdout (or --out). Never sends audio anywhere; the LLM only ever sees the JSON.
ponytail: no stem separation (demucs not installed); add --stems when per-instrument timelines matter.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

import numpy as np

if os.path.isdir("/Volumes/Data/huggingface"):                  # .6: same model cache as nova_finetune
    os.environ.setdefault("HF_HOME", "/Volumes/Data/huggingface")  # Linux nodes keep the default ~/.cache

FFMPEG = "/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else "ffmpeg"
CLAP_MODEL = "laion/larger_clap_music_and_speech"   # larger_clap_music loads but scores flat (logit scale ~1) under transformers 4.57
TEMPLATES = ("This is {}.", "{}")                         # averaged: genre answers swing with the wording
SR = 22050
KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Krumhansl-Kessler key profiles
MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

# Mutually exclusive groups get a softmax; multi-label groups report relative similarity (z-score).
LABELS = {
    "genre": (True, ["drum and bass", "breakbeat", "trip hop", "house music", "trance", "techno", "dubstep",
                     "pop music", "rock music", "punk rock", "hip hop", "r&b", "ambient", "classical music",
                     "jazz", "folk music", "metal", "synth-pop", "downtempo electronica"]),
    "vocals": (True, ["female singing voice", "male singing voice", "rap vocals", "choir", "spoken word",
                      "instrumental music with no vocals"]),
    "mood": (True, ["melancholic", "euphoric", "aggressive", "calm", "dark and brooding", "uplifting",
                    "romantic", "tense"]),
    "instruments": (False, ["breakbeat drums", "four-on-the-floor kick drum", "deep sub bass", "synthesizer pads",
                            "acoustic piano", "electric guitar", "acoustic guitar", "strings", "brass",
                            "drum machine", "vocal chops", "orchestral percussion"]),
    "production": (False, ["live recording", "heavily compressed loud master", "lo-fi recording",
                           "polished studio production", "reverb-heavy atmosphere", "sparse minimal arrangement",
                           "dense layered arrangement"]),
}


def loudness(path: str) -> dict:
    """EBU R128 integrated loudness, loudness range and true peak via ffmpeg's ebur128 filter."""
    p = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", path, "-filter_complex",
                        "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True, timeout=600)
    summary = p.stderr[p.stderr.rfind("Summary:"):]
    num = lambda k: float(re.search(rf"{k}:\s+(-?[\d.]+|-inf)", summary).group(1).replace("-inf", "-inf"))
    lufs, lra, peak = num("I"), num("LRA"), num("Peak")
    return {"integrated_lufs": lufs, "loudness_range_lu": lra, "true_peak_dbtp": peak,
            "peak_to_loudness_db": round(peak - lufs, 1)}   # PLR: < ~8 dB reads as heavily limited


def key_of(chroma_mean: np.ndarray) -> dict:
    scores = []
    for i in range(12):
        for mode, prof in (("major", MAJOR), ("minor", MINOR)):
            scores.append((float(np.corrcoef(chroma_mean, np.roll(prof, i))[0, 1]), KEYS[i], mode))
    scores.sort(reverse=True)
    (best, k, m), (second, k2, m2) = scores[0], scores[1]
    return {"key": f"{k} {m}", "correlation": round(best, 3), "runner_up": f"{k2} {m2}",
            "margin": round(best - second, 3)}   # small margin = ambiguous (relative keys, modal music)


def measure(path: str) -> dict:
    import librosa
    y, sr = librosa.load(path, sr=SR, mono=True)
    dur = len(y) / sr
    onset = librosa.onset.onset_strength(y=y, sr=sr)
    # Tempo from the strongest tempogram peaks, not beat_track's 120-BPM prior (it read Kosheen's 172 DnB as 112).
    tg = librosa.feature.tempogram(onset_envelope=onset, sr=sr).mean(axis=1)
    bpms = librosa.tempo_frequencies(len(tg), sr=sr)
    cands = []
    for strength, b in sorted(zip(tg, bpms), reverse=True):
        if 70 <= b <= 180 and all(abs(b - c) / c > 0.04 for c in cands):   # musical range; octave stays ambiguous (see half/double)
            cands.append(float(b))
        if len(cands) == 3:
            break
    tempo = cands[0]
    _, beats = librosa.beat.beat_track(onset_envelope=onset, sr=sr, start_bpm=tempo)
    bt = librosa.frames_to_time(beats, sr=sr)
    ibi = np.diff(bt)
    regularity = float(1 - np.std(ibi) / np.mean(ibi)) if len(ibi) > 4 else 0.0
    chroma = librosa.feature.chroma_cqt(y=librosa.effects.harmonic(y), sr=sr)
    rms = librosa.feature.rms(y=y)[0]
    cent = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    # sections: agglomerative clustering over chroma + timbre, roughly one boundary per ~25 s
    feats = np.vstack([librosa.util.normalize(chroma, axis=1),
                       librosa.util.normalize(librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13), axis=1)])
    k = int(np.clip(round(dur / 25), 3, 10))
    bounds = librosa.frames_to_time(librosa.segment.agglomerative(feats, k), sr=sr)
    edges = list(bounds) + [dur]
    hop = 512
    db = lambda a: round(max(float(20 * np.log10(np.mean(a) + 1e-9)), -90.0), 1)   # silence floors at -90
    sections = []
    for s, e in zip(edges[:-1], edges[1:]):
        a, b = int(s * sr / hop), max(int(e * sr / hop), int(s * sr / hop) + 1)
        o = librosa.onset.onset_detect(onset_envelope=onset[a:b], sr=sr)
        sections.append({"start": round(float(s), 1), "end": round(float(e), 1), "rms_db": db(rms[a:b]),
                         "brightness_hz": int(np.mean(cent[a:b])), "onsets_per_s": round(len(o) / max(e - s, 1e-3), 2)})
    return {"duration_s": round(dur, 1),
            "tempo": {"bpm": round(tempo, 1), "half": round(tempo / 2, 1), "double": round(tempo * 2, 1),
                      "candidates": [round(c, 1) for c in cands],   # strongest periodicities, best first
                      "beat_regularity": round(regularity, 3)},
            "key": key_of(chroma.mean(axis=1)),
            "dynamics": {"rms_db_mean": db(rms), "rms_db_p10": round(float(20 * np.log10(np.percentile(rms, 10) + 1e-9)), 1),
                         "rms_db_p90": round(float(20 * np.log10(np.percentile(rms, 90) + 1e-9)), 1)},
            "sections": sections}


def recognize(path: str, sections: list) -> dict:
    """CLAP zero-shot over 10 s windows (CPU, batch 1 — the Studio OOM'd on MLX today; keep it small)."""
    import librosa
    import torch
    from transformers import ClapModel, ClapProcessor
    torch.set_num_threads(8)
    emb = lambda o: o if torch.is_tensor(o) else o.pooler_output   # transformers 5 returns an output object
    model, proc = ClapModel.from_pretrained(CLAP_MODEL).eval(), ClapProcessor.from_pretrained(CLAP_MODEL)
    y, sr = librosa.load(path, sr=48000, mono=True)
    win = 10 * sr
    starts = list(range(0, max(len(y) - win, 0) + 1, win))
    with torch.no_grad():
        embs = []
        for s in starts:
            inp = proc(audio=[y[s:s + win]], sampling_rate=sr, return_tensors="pt")
            embs.append(torch.nn.functional.normalize(emb(model.get_audio_features(**inp)), dim=-1)[0])
        A = torch.stack(embs)                                            # windows x dim
        out = {}
        texts = {g: v[1] for g, v in LABELS.items()}
        T = {}
        for g, labs in texts.items():
            per = [torch.nn.functional.normalize(emb(model.get_text_features(
                **proc(text=[tp.format(l) for l in labs], return_tensors="pt", padding=True))), dim=-1) for tp in TEMPLATES]
            T[g] = torch.nn.functional.normalize(torch.stack(per).mean(0), dim=-1)
        track = torch.nn.functional.normalize(A.mean(0), dim=-1)
        scale = float(model.logit_scale_a.detach().exp())
        if scale < 5:   # trained CLAP sits at ~18-40; ~1 means the weights didn't really load
            raise RuntimeError(f"{CLAP_MODEL}: logit scale {scale:.2f} — checkpoint not usable, scores would be flat")
        for g, (exclusive, labs) in LABELS.items():
            sims = track @ T[g].T
            vals = torch.softmax(sims * scale, -1) if exclusive else (sims - sims.mean()) / (sims.std() + 1e-9)
            ranked = sorted(zip(labs, vals.tolist()), key=lambda x: -x[1])
            out[g] = [{"label": l, ("prob" if exclusive else "z"): round(v, 3)} for l, v in ranked[:5]]
        # vocal presence per section: P(any singing/rap) vs P(instrumental) on that section's windows
        v_labs = LABELS["vocals"][1]
        inst = v_labs.index("instrumental music with no vocals")
        for sec in sections:
            idx = [i for i, s in enumerate(starts) if sec["start"] - 5 <= s / sr < sec["end"]] or \
                  [min(range(len(starts)), key=lambda i: abs(starts[i] / sr - sec["start"]))]
            e = torch.nn.functional.normalize(A[idx].mean(0), dim=-1)
            p = torch.softmax((e @ T["vocals"].T) * scale, -1)
            sec["vocal_presence"] = round(1 - float(p[inst]), 2)
            g = torch.softmax((e @ T["genre"].T) * scale, -1)
            sec["top_genre"] = LABELS["genre"][1][int(g.argmax())]
        # talk detector: share of 10 s windows CLAP calls "speech" over "singing"/"instrumental music" (bare labels).
        # Calibrated 2026-10-19: Kosheen song 0.00, a composer reaction 0.56, a making-of documentary 0.83.
        # ("spoken word" in the vocals group barely fired on either talker: 0.05 / 0.00.)
        talk = torch.nn.functional.normalize(emb(model.get_text_features(
            **proc(text=["speech", "singing", "instrumental music"], return_tensors="pt", padding=True))), dim=-1)
        out["speech_fraction"] = round(float(((A @ talk.T).argmax(-1) == 0).float().mean()), 2)
    out["windows_analyzed"] = len(starts)
    return out


def hear(path: str, clap: bool = True) -> dict:
    r = {"file": os.path.basename(path), **measure(path), "loudness": loudness(path)}
    if clap:
        r["clap"] = recognize(path, r["sections"])
    return r


MAX_TRACK_S = 900        # longer = a DJ set or a documentary, not a track
KEY_MIN_MARGIN = 0.05    # below this the major/minor (or relative-key) call is a coin flip
SPEECH_MAX = 0.3         # share of windows that are mostly talk; above it = a reaction/making-of, not a track


def _log(msg):
    print(f"[hear] {msg}", file=sys.stderr, flush=True)


def _for_writer(r: dict) -> dict:
    """The JSON the writer sees: vocal-TYPE labels dropped (CLAP called Sian Evans 'rap vocals' at 0.95),
    vocal presence kept; a weak key becomes 'ambiguous'."""
    r = json.loads(json.dumps(r))
    clap = r.get("clap", {})
    clap.pop("vocals", None)
    if r["key"]["margin"] < KEY_MIN_MARGIN:
        r["key"] = {"key": "ambiguous", "best_guess": r["key"]["key"], "runner_up": r["key"]["runner_up"]}
    return r


LISTEN_URL = os.environ.get("NOVA_HEAR_LISTEN_URL", "http://nova-core8.digitalnoise.net:37481/vocals")
NOT_THE_RECORD = re.compile(r"\b(reaction|reacts?|reacting|making of|interview|live|cover|karaoke|remix|extended|"
                            r"edit|sped up|slowed|nightcore|8d|instrumental|tutorial|lesson|review|explained|"
                            r"documentary|behind the|breakdown|first time hearing|lyrics?)\b")


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s)).strip()


def _yt_search(query: str, n: int = 10) -> list[dict]:
    import shutil
    ytdlp = shutil.which("yt-dlp") or os.path.join(os.path.dirname(sys.executable), "yt-dlp")
    p = subprocess.run([ytdlp, "--no-warnings", "--socket-timeout", "30", "--flat-playlist", "--print",
                        "%(id)s\t%(title)s\t%(channel)s\t%(duration)s", f"ytsearch{n}:{query}"],
                       capture_output=True, text=True, timeout=120)
    out = []
    for line in p.stdout.splitlines():
        vid, title, chan, dur = (line.split("\t") + ["", "", "", ""])[:4]
        out.append({"id": vid, "title": title, "channel": chan,
                    "duration": float(dur) if dur.replace(".", "", 1).isdigit() else 0.0})
    return out


def find_official(artist: str, title: str, log=_log, search=None) -> str | None:
    """The official upload of one song: artist's own channel, '<Artist> - Topic' or VEVO; title must contain the
    song title, nothing marking it as a reaction/live/cover/remix/making-of, 1.5-10 min. None if nothing verifies."""
    a, t = _norm(artist), _norm(title)
    if not a or not t:
        return None
    search = search or _yt_search
    best = None
    for i, v in enumerate(search(f"{artist} {title}")):
        vt, ch = _norm(v["title"]), _norm(v["channel"])
        extra = vt.replace(t, " ")                  # words in the video title beyond the song title itself
        if t not in vt or NOT_THE_RECORD.search(extra) or not 90 <= v["duration"] <= 600:
            continue
        if ch == f"{a} topic":
            rank = 0
        elif "vevo" in ch and a.replace(" ", "") in (ch + vt).replace(" ", ""):
            rank = 1
        elif a.replace(" ", "") in ch.replace(" ", ""):
            rank = 2
        else:
            continue                                # not the artist's own upload: unverified
        if best is None or (rank, i) < best[0]:
            best = ((rank, i), f"https://www.youtube.com/watch?v={v['id']}", v)
    if best:
        log(f"official upload for {artist} - {title}: {best[2]['title']} [{best[2]['channel']}] {best[1]}")
        return best[1]
    log(f"no verified official upload for {artist} - {title}")
    return None


def song_from_block(block: str) -> tuple[str, str] | None:
    """(artist, title) of the one song a Track of the Day features, read from the edition's own text."""
    import nova_journal as nj
    raw = (nj.call_openrouter(
        'From this text, return ONLY JSON {"artist": "...", "title": "..."} for the ONE song it features as the '
        "track of the day, exactly as named in the text. If it features a DJ set, an album, a playlist or no single "
        "song, return NONE.", block, model="anthropic/claude-haiku-4.5", max_tokens=200, temperature=0) or "").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    d = json.loads(m.group(0))
    return (d["artist"].strip(), d["title"].strip()) if d.get("artist") and d.get("title") else None


def vocal_guess(url: str, log=_log, timeout: int = 120) -> dict | None:
    """Qwen3-Omni's vocal call from the Studio's listen service (nova_hear_listen.py). None if unreachable/busy."""
    import urllib.request
    try:
        req = urllib.request.Request(LISTEN_URL, data=json.dumps({"url": url}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001 — no vocal line, the note still publishes
        log(f"vocal guess unavailable ({type(e).__name__}: {e})")
        return None


def vocal_line(v: dict | None) -> str:
    """One labeled sentence from the listen service's majority vote, or "" when it has nothing firm."""
    if not v:
        return ""
    if v.get("lead_vocal") is False:
        return " *Vocal guess (a listening model, not measured): no lead vocal.*"
    g, s = v.get("gender", "unsure"), v.get("style", "unsure")
    bits = [x for x in (g if g not in ("unsure", "none") else "", s if s not in ("unsure", "none") else "") if x]
    return f" *Vocal guess (a listening model, not measured): {', '.join(bits)} lead vocal.*" if bits else ""


def describe_track(url: str, log=_log, song=None) -> str | None:
    """'🎧 What it sounds like' note for one YouTube track, written ONLY from nova_hear's measurements.
    `song` (callable -> (artist, title) | None): when `url` is talk-heavy or too long (a reaction, making-of or DJ
    set), find that song's official upload and analyze it instead. Fail soft: any problem -> None + a log line."""
    import shutil
    try:
        ytdlp = shutil.which("yt-dlp") or os.path.join(os.path.dirname(sys.executable), "yt-dlp")
        why = None
        with tempfile.TemporaryDirectory(prefix="nova-hear-") as d:
            wav = os.path.join(d, "a.wav")
            for attempt in range(3):   # googlevideo hands out intermittent 403s; a retry usually gets through
                p = subprocess.run([ytdlp, "--no-warnings", "--socket-timeout", "30", "--no-playlist",
                                    "--match-filter", f"duration < {MAX_TRACK_S}", "-f", "bestaudio", "-x",
                                    "--audio-format", "wav", "-o", os.path.join(d, "a.%(ext)s"), url],
                                   capture_output=True, text=True, timeout=600)
                if os.path.exists(wav):
                    break
                if "does not pass filter" in (p.stdout + p.stderr):
                    why = "longer than a track"
                    break
                import time
                time.sleep(10 * (attempt + 1))
            if not os.path.exists(wav) and not why:
                log(f"sound note skipped: no audio for {url} ({(p.stderr or p.stdout).strip()[-200:]})")
                return None
            r = hear(wav) if not why else None
        if r is not None:
            speech = r["clap"].get("speech_fraction", 0)
            if speech > SPEECH_MAX:
                why = f"{speech:.0%} talk (reaction / making-of)"
        if why:
            s = song() if song else None
            official = find_official(*s, log) if s else None
            if official and official != url:
                log(f"{url} is {why}; analyzing the official upload instead")
                return describe_track(official, log)
            log(f"sound note skipped: {url} is {why}, not a track" + ("" if s else " (no single song named)"))
            return None
        import nova_journal as nj
        import nova_voice
        ctx = ("You are writing a 3-5 sentence '🎧 What it sounds like' note under today's Track of the Day. You get "
               "ONLY machine measurements of the audio (tempo, key, loudness, sections, CLAP zero-shot tags) — no "
               "title, artist or lyrics. Describe what the track sounds like using only those numbers, in under 120 words. CLAP tags are "
               "probabilistic guesses: hedge them. Tempo octave is ambiguous (half/double/candidates). If key is "
               "'ambiguous', say so. Loudness scale: -6 to -9 LUFS = very loud, limited club/pop master; around -14 "
               "= streaming normal; -16 or lower = quiet, dynamic, older or live. Vocals: say only whether and where they are present — never guess singing vs rapping, the singer's gender or voice type (the analysis can't tell). Never name the artist or title, never invent instruments, lyrics or facts the "
               "numbers don't support. No headers, no bullet points, no addressing Little Mister.")
        text = (nj.call_openrouter(nova_voice.system_prompt(ctx), json.dumps(_for_writer(r)),
                                   model="anthropic/claude-sonnet-4.5", max_tokens=600, temperature=0.5) or "").strip()
        text = re.sub(r"^[*_\s]*🎧?\s*what it sounds like\s*:?[*_\s]*", "", text, flags=re.I).strip()  # we add the label
        n = len(text.split())
        if not 30 <= n <= 200:
            log(f"sound note skipped: writer returned {n} words")
            return None
        log(f"sound note: {n} words for {url} (tempo {r['tempo']['bpm']}, {r['loudness']['integrated_lufs']} LUFS)")
        # vocal TYPE comes only from the listening model, labeled as its guess (CLAP's vocal labels are hidden)
        # Jordan 2026-10-19: link the exact upload that was analyzed (never a guessed one)
        return f"**🎧 What it sounds like:** {text}{vocal_line(vocal_guess(url, log))} [Listen on YouTube]({url})"
    except Exception as e:  # noqa: BLE001 — the note never sinks the edition
        log(f"sound note skipped: {type(e).__name__}: {e}")
        return None


def add_sound_note(block: str, videos, log=_log) -> str:
    """Music and Friends hook: append the sound note for the video the Track of the Day links. Never raises."""
    try:
        url = next((v["url"] for v in videos if v.get("url") and v["url"] in block), None)
        if not url:
            log("sound note skipped: Track of the Day links no video")
            return block
        note = describe_track(url, log, song=lambda: song_from_block(block))
        return f"{block}\n\n{note}" if note else block
    except Exception as e:  # noqa: BLE001
        log(f"sound note skipped: {type(e).__name__}: {e}")
        return block


def selftest():
    """120 BPM kick clicks over a sustained A-major triad -> tempo ~120, key A major, finite loudness."""
    import soundfile as sf
    sr, dur = SR, 20.0
    t = np.arange(int(sr * dur)) / sr
    y = sum(0.2 * np.sin(2 * np.pi * f * t) for f in (220.0, 277.18, 329.63))      # A3 C#4 E4
    click = np.exp(-np.arange(int(0.03 * sr)) / (0.004 * sr)) * np.sin(2 * np.pi * 60 * np.arange(int(0.03 * sr)) / sr)
    for b in np.arange(0, dur, 0.5):                                               # 120 BPM
        i = int(b * sr)
        y[i:i + len(click)] += 0.9 * click[:len(y) - i]
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        sf.write(f.name, y / np.abs(y).max() * 0.8, sr)
        r = hear(f.name, clap=False)
    os.unlink(f.name)
    assert abs(r["tempo"]["bpm"] - 120) < 3, r["tempo"]
    assert r["key"]["key"] == "A major", r["key"]
    assert -40 < r["loudness"]["integrated_lufs"] < 0, r["loudness"]
    assert r["sections"] and r["sections"][-1]["end"] == r["duration_s"]
    w = _for_writer({**r, "key": {"key": "D# major", "runner_up": "D# minor", "margin": 0.016},
                     "clap": {"vocals": [{"label": "rap vocals", "prob": 0.95}], "genre": []}})
    assert "vocals" not in w["clap"] and w["key"]["key"] == "ambiguous", w
    assert _for_writer(r)["key"]["key"] == "A major"                    # a clear key survives
    assert re.sub(r"^[*_\s]*🎧?\s*what it sounds like\s*:?[*_\s]*", "", "🎧 What it sounds like: Loud.", flags=re.I) == "Loud."
    assert add_sound_note("block, no links", [{"url": "https://youtu.be/x"}], log=lambda m: None) == "block, no links"
    print("selftest OK", r["tempo"]["bpm"], r["key"]["key"], r["loudness"]["integrated_lufs"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", nargs="?")
    ap.add_argument("--no-clap", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        sys.exit(0)
    if not a.audio:
        ap.error("give an audio file or --selftest")
    res = json.dumps(hear(a.audio, clap=not a.no_clap), indent=2)
    if a.out:
        open(a.out, "w").write(res)
    print(res)
