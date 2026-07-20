#!/usr/bin/env python3
"""nova_voice_fingerprint.py — PROTOTYPE: speaker diarization + voice attribution.

Extracts audio, computes resemblyzer d-vector voice embeddings over sliding windows,
clusters them into distinct speakers (diarization), and — the crown jewel — matches a
voice ACROSS videos: fingerprint a speaker in one clip, then detect whether that same
person appears in another. This is what turns "searchable transcripts" into "searchable
PEOPLE" (attribute who said what across a whole channel/community).

Usage:
  nova_voice_fingerprint.py <video> [--secs 300]                 # diarize one video
  nova_voice_fingerprint.py <video> --match <other_video> [--secs 300]   # cross-video match
"""
import os, sys, tempfile, subprocess, argparse
import numpy as np
from resemblyzer import VoiceEncoder, preprocess_wav
from sklearn.cluster import AgglomerativeClustering

_ENC = None
def encoder():
    global _ENC
    if _ENC is None:
        _ENC = VoiceEncoder(verbose=False)
    return _ENC


def audio_wav(video, secs):
    """Extract the first `secs` of audio as a 16 kHz mono wav resemblyzer expects."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    subprocess.run(["ffmpeg", "-t", str(secs), "-i", video, "-ac", "1", "-ar", "16000",
                    "-vn", "-y", tmp], capture_output=True)
    return tmp


def windows(video, secs):
    """Return (partial_embeds [N,256], wav_splits) — one d-vector per ~1.6s window."""
    wav = preprocess_wav(audio_wav(video, secs))
    _, partials, splits = encoder().embed_utterance(wav, return_partials=True, rate=1.3)
    return np.asarray(partials), splits


def _norm(v):
    return v / (np.linalg.norm(v) + 1e-9)


def diarize(partials, splits, init_dist=0.45, merge_sim=0.82):
    """Cluster window embeddings into speakers, then MERGE clusters whose voiceprints are
    near-identical (fixes over-segmentation from noisy 1.6s windows). Returns per-window
    speaker labels (relabeled 0..K by talk-time, most-talkative = Speaker 0)."""
    if len(partials) < 2:
        return np.zeros(len(partials), dtype=int)
    raw = AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average",
                                  distance_threshold=init_dist).fit_predict(partials)
    cents = {l: _norm(partials[raw == l].mean(0)) for l in set(raw)}
    ids = list(cents)
    parent = {i: i for i in ids}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if float(np.dot(cents[ids[i]], cents[ids[j]])) > merge_sim:
                parent[find(ids[j])] = find(ids[i])
    merged = np.array([find(l) for l in raw])
    # relabel by talk-time (window count), Speaker 0 = most-talkative
    order = sorted(set(merged), key=lambda l: -(merged == l).sum())
    remap = {old: new for new, old in enumerate(order)}
    return np.array([remap[l] for l in merged])


def turns_from_labels(labels, splits):
    turns, cur = [], None
    for lab, sl in zip(labels, splits):
        s, e = sl.start / 16000, sl.stop / 16000
        if cur and cur[2] == lab and s - cur[1] < 1.5:
            cur[1] = e
        else:
            if cur:
                turns.append(tuple(cur))
            cur = [s, e, int(lab)]
    if cur:
        turns.append(tuple(cur))
    return turns


def top_speakers(partials, labels, splits, k=6):
    """Return [(speaker_id, talk_seconds, centroid)] for the k most-talkative voices."""
    win = (splits[0].stop - splits[0].start) / 16000 if len(splits) else 1.6
    out = []
    for lab in sorted(set(labels), key=lambda l: -(labels == l).sum())[:k]:
        secs = int((labels == lab).sum() * win)
        out.append((int(lab), secs, _norm(partials[labels == lab].mean(0))))
    return out


def hms(t):
    return f"{int(t // 60):02d}:{int(t % 60):02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--match", default=None, help="second video to attribute against the first")
    ap.add_argument("--secs", type=int, default=300)
    a = ap.parse_args()

    print(f"Fingerprinting voices in {os.path.basename(a.video)} (first {a.secs}s)...")
    p1, s1 = windows(a.video, a.secs)
    labels1 = diarize(p1, s1)
    top1 = top_speakers(p1, labels1, s1)
    print(f"  {len(p1)} windows -> {len(set(labels1))} speakers; top voices by talk-time:")
    for spk, secs, _ in top1:
        print(f"    Speaker {spk}: ~{secs}s of speech")
    print("  turn timeline (top voices):")
    keep = {spk for spk, _, _ in top1}
    for st, en, spk in turns_from_labels(labels1, s1):
        if en - st >= 2 and spk in keep:
            print(f"    {hms(st)}-{hms(en)}  Speaker {spk}")

    if a.match:
        print(f"\nCross-video attribution vs {os.path.basename(a.match)} (first {a.secs}s)...")
        p2, s2 = windows(a.match, a.secs)
        labels2 = diarize(p2, s2)
        top2 = top_speakers(p2, labels2, s2)
        print("  matching each video-2 voice to the closest video-1 voice:")
        for spk2, secs2, c2 in top2:
            best = max(top1, key=lambda t: float(np.dot(t[2], c2)))
            sim = float(np.dot(best[2], c2))
            verdict = "SAME PERSON" if sim > 0.80 else ("likely same" if sim > 0.72 else "different")
            print(f"    vid2 Spk{spk2} ({secs2}s)  ->  vid1 Spk{best[0]}   sim={sim:.2f}  [{verdict}]")
        print("  (shared voices = the recurring cast; unique = that episode's guest)")


if __name__ == "__main__":
    main()
