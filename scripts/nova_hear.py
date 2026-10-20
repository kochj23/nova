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

os.environ.setdefault("HF_HOME", "/Volumes/Data/huggingface")   # same model cache as nova_finetune

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
    model, proc = ClapModel.from_pretrained(CLAP_MODEL).eval(), ClapProcessor.from_pretrained(CLAP_MODEL)
    y, sr = librosa.load(path, sr=48000, mono=True)
    win = 10 * sr
    starts = list(range(0, max(len(y) - win, 0) + 1, win))
    with torch.no_grad():
        embs = []
        for s in starts:
            inp = proc(audio=[y[s:s + win]], sampling_rate=sr, return_tensors="pt")
            embs.append(torch.nn.functional.normalize(model.get_audio_features(**inp), dim=-1)[0])
        A = torch.stack(embs)                                            # windows x dim
        out = {}
        texts = {g: v[1] for g, v in LABELS.items()}
        T = {}
        for g, labs in texts.items():
            per = [torch.nn.functional.normalize(model.get_text_features(
                **proc(text=[tp.format(l) for l in labs], return_tensors="pt", padding=True)), dim=-1) for tp in TEMPLATES]
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
    out["windows_analyzed"] = len(starts)
    return out


def hear(path: str, clap: bool = True) -> dict:
    r = {"file": os.path.basename(path), **measure(path), "loudness": loudness(path)}
    if clap:
        r["clap"] = recognize(path, r["sections"])
    return r


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
