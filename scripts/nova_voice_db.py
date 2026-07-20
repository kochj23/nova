#!/usr/bin/env python3
"""nova_voice_db.py — named voiceprint database: enroll known speakers, then auto-attribute.

Enrollment stores a speaker's d-vector voiceprint (running mean over samples) in
nova_memories.voiceprints. Identification diarizes a new video and matches each speaker
against the DB, so Nova can say WHO is talking — by name — across any video/channel.

Usage:
  nova_voice_db.py enroll   "<name>" <video> [--rank 0] [--secs 240]   # enroll Nth-talkative voice
  nova_voice_db.py identify <video> [--secs 240] [--threshold 0.75]
  nova_voice_db.py list
"""
import os
import sys, argparse
import numpy as np
import psycopg2
sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
from nova_voice_fingerprint import windows, diarize, top_speakers

DSN = "host=localhost dbname=nova_memories user=kochj"


def _vec(a):
    return "[" + ",".join(f"{x:.6f}" for x in a) + "]"


def enroll(name, video, rank, secs):
    p, s = windows(video, secs)
    labels = diarize(p, s)
    tops = top_speakers(p, labels, s)
    if rank >= len(tops):
        print(f"  only {len(tops)} speakers found; can't enroll rank {rank}"); return
    _, secs_talk, cent = tops[rank]
    conn = psycopg2.connect(DSN)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT embedding, n_samples FROM voiceprints WHERE name=%s", (name,))
        row = cur.fetchone()
        if row:
            old = np.array(eval(row[0])); n = row[1]
            merged = (old * n + cent) / (n + 1)
            merged = merged / (np.linalg.norm(merged) + 1e-9)
            cur.execute("UPDATE voiceprints SET embedding=%s::vector, n_samples=%s, updated_at=now() WHERE name=%s",
                        (_vec(merged), n + 1, name))
            print(f"  updated '{name}' (now {n+1} samples)")
        else:
            cur.execute("INSERT INTO voiceprints (name, embedding, n_samples) VALUES (%s,%s::vector,1)",
                        (name, _vec(cent)))
            print(f"  enrolled '{name}' from ~{secs_talk}s of speech")


def identify(video, secs, threshold):
    p, s = windows(video, secs)
    labels = diarize(p, s)
    tops = top_speakers(p, labels, s)
    conn = psycopg2.connect(DSN)
    print(f"  {len(tops)} voices in this video; matching against the enrolled DB:")
    with conn, conn.cursor() as cur:
        for spk, secs_talk, cent in tops:
            cur.execute("SELECT name, 1-(embedding <=> %s::vector) AS sim FROM voiceprints "
                        "ORDER BY embedding <=> %s::vector LIMIT 1", (_vec(cent), _vec(cent)))
            row = cur.fetchone()
            if row and row[1] >= threshold:
                print(f"    Speaker {spk} ({secs_talk}s)  ->  {row[0]}   (sim {row[1]:.2f})")
            else:
                near = f"closest '{row[0]}' {row[1]:.2f}" if row else "empty DB"
                print(f"    Speaker {spk} ({secs_talk}s)  ->  UNKNOWN   ({near})")


def attribute_video(video, secs=240, threshold=0.75):
    """Diarize a video, match each speaker to the enrolled DB, and store a searchable
    'who is in this video' summary memory (source=speaker_index). Returns named speakers."""
    import json, urllib.request, os
    p, s = windows(video, secs)
    labels = diarize(p, s)
    tops = top_speakers(p, labels, s)
    conn = psycopg2.connect(DSN)
    named, unknown = [], 0
    with conn, conn.cursor() as cur:
        for spk, secs_talk, cent in tops:
            cur.execute("SELECT name, 1-(embedding <=> %s::vector) AS sim FROM voiceprints "
                        "ORDER BY embedding <=> %s::vector LIMIT 1", (_vec(cent), _vec(cent)))
            row = cur.fetchone()
            if row and row[1] >= threshold:
                named.append(row[0])
            else:
                unknown += 1
    named = sorted(set(named))
    if named or unknown:
        summary = (f"[{os.path.basename(video)}] identified speakers: "
                   f"{', '.join(named) or 'none enrolled'}" + (f" (+{unknown} unknown voice(s))" if unknown else ""))
        body = json.dumps({"text": summary, "source": "speaker_index",
                           "metadata": {"kind": "speaker_index", "video": os.path.basename(video),
                                        "named": named, "unknown": unknown}}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(
                "http://192.168.1.6:18790/remember?async=1", data=body,
                headers={"Content-Type": "application/json"}), timeout=15)
        except Exception:
            pass
    return named


def list_db():
    conn = psycopg2.connect(DSN)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT name, n_samples, updated_at FROM voiceprints ORDER BY name")
        for name, n, ts in cur.fetchall():
            print(f"  {name:28} {n} sample(s)   {ts:%Y-%m-%d %H:%M}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enroll"); e.add_argument("name"); e.add_argument("video")
    e.add_argument("--rank", type=int, default=0); e.add_argument("--secs", type=int, default=240)
    i = sub.add_parser("identify"); i.add_argument("video")
    i.add_argument("--secs", type=int, default=240); i.add_argument("--threshold", type=float, default=0.75)
    sub.add_parser("list")
    a = ap.parse_args()
    if a.cmd == "enroll":   enroll(a.name, a.video, a.rank, a.secs)
    elif a.cmd == "identify": identify(a.video, a.secs, a.threshold)
    else: list_db()
