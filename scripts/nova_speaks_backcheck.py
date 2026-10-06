#!/usr/bin/env python3
"""nova_speaks_backcheck.py — score already-published Nova Speaks videos for narration quality, and queue re-renders.

Jordan, 2026-10-06: the back catalogue was voiced before the narration stage existed (nova_speaks_narration.py), so some
episodes growl, garble, or say "ninety-one-F" and skip past Qapla'. For every done render with a YouTube id this
transcribes the mp4's audio (mlx-whisper base.en, Studio) and aligns it with the article text:
  garbage   a run of ≥6 transcript words that match nothing in the article (growl / hallucinated babble)
  dropout   a run of ≥12 article words the audio doesn't contain recognisably (garbled or swallowed)
  segments  Whisper segments with compression_ratio > 2.4 (looping) or avg_logprob < -1.2 over ≥2 s
  source    the article has °F/°C, a phrasebook phrase, or non-Latin script — the old pipeline mis-spoke those
A video fails on ≥2 garbage runs, ≥2 dropouts, any looping segment, or any source signal. Scores land in nova_speaks_renders.quality.legacy_backcheck.

  nova_speaks_backcheck.py                     # score everything not yet scored (no changes to the queue)
  nova_speaks_backcheck.py --slug <slug>       # one
  nova_speaks_backcheck.py --requeue           # queue the failures through the normal sweep (re-render + YouTube replace)
ponytail: the alignment is difflib on normalised words, not forced alignment — good enough to find runs, not to time them.
"""
import argparse, difflib, json, os, re, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_speaks as ns
import nova_speaks_narration as nn

DSN = os.environ.get("NOVA_OPS_DSN", "dbname=nova_ops user=kochj host=localhost")
TAG = "rerender-quality-20261006"
REVIEW_LOCAL = "/Volumes/nas/nova-fs/videos/review"
GARBAGE_RUN, DROPOUT_RUN = 6, 12


def log(m): print(f"[speaks-backcheck {time.strftime('%H:%M:%S')}] {m}", flush=True)


def source_signals(md, book):
    body = re.sub(r"^---.*?---\s*", "", md, count=1, flags=re.S)
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    body = re.sub(r"^\*(Published|Burbank) .*?\*\s*$", "", body, flags=re.M)      # datelines are never read
    sig = []
    if re.search(r"\d\s*°\s*[FC]?|℉|℃", body): sig.append("temperature")
    folded = nn.fold(body)
    hits = sorted({p["phrase"] for p in book if nn._phrase_re(p).search(folded)})
    if hits: sig.append("foreign:" + ",".join(hits[:6]))
    for rx, lang, _ in nn._SCRIPTS:
        if lang != "Greek" and re.search(rx, body): sig.append(f"script:{lang}")
    return sig


def align(ref_words, hyp_words):
    sm = difflib.SequenceMatcher(None, ref_words, hyp_words, autojunk=False)
    garbage, dropouts, err = [], [], 0
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal": continue
        err += max(i2 - i1, j2 - j1)
        rl, hl = i2 - i1, j2 - j1
        if hl >= GARBAGE_RUN and hl >= rl + 4: garbage.append(" ".join(hyp_words[j1:j2])[:120])
        if rl >= DROPOUT_RUN and hl <= rl / 3: dropouts.append(" ".join(ref_words[i1:i2])[:120])
    return garbage, dropouts, err / max(1, len(ref_words))


def transcribe(mp4):
    import mlx_whisper
    os.environ.setdefault("HF_HOME", "/Volumes/Data/huggingface")
    with tempfile.TemporaryDirectory(dir="/Volumes/Data/AI/tts") as d:
        wav = f"{d}/a.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", "4", "-i", mp4, "-ac", "1", "-ar", "16000", wav], check=True)
        return mlx_whisper.transcribe(wav, path_or_hf_repo="mlx-community/whisper-base.en-mlx", language="en",
                                      condition_on_previous_text=False)


def score(row, book):
    slug, art, mp4 = row
    md = Path(art).read_text()
    chapters = ns.article_to_script(md, None)
    ref = " ".join(p for _, ps in chapters for p in ps)
    r = transcribe(mp4)
    garbage, dropouts, err = align(nn._norm_words(ref), nn._norm_words(r["text"]))
    bad_segs = [round(s["start"], 1) for s in r.get("segments", [])
                if s.get("compression_ratio", 0) > 2.4 or (s.get("avg_logprob", 0) < -1.2 and s["end"] - s["start"] >= 2)]
    src = source_signals(md, book)
    growl = len(garbage) >= 2 or len(dropouts) >= 2 or bool(bad_segs)      # one stray run is Whisper noise; two is a pattern
    return {"err_rate": round(err, 3), "garbage_runs": len(garbage), "dropouts": len(dropouts), "bad_segments": bad_segs[:20],
            "garbage_examples": garbage[:3], "dropout_examples": dropouts[:3], "source": src,
            "fail": growl or bool(src), "reasons": (["growl/garble"] if growl else []) + src,
            "scored_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug"); ap.add_argument("--requeue", action="store_true"); ap.add_argument("--rescore", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    import psycopg2
    c = psycopg2.connect(DSN); c.autocommit = True; cur = c.cursor()
    cur.execute("ALTER TABLE nova_speaks_renders ADD COLUMN IF NOT EXISTS quality jsonb, ADD COLUMN IF NOT EXISTS old_youtube_id text, "
                "ADD COLUMN IF NOT EXISTS old_youtube_retired boolean")
    book = nn.load_phrasebook()
    q = ("SELECT slug, article_path, mp4_path FROM nova_speaks_renders WHERE status='done' AND youtube_id ~ '^[A-Za-z0-9_-]{11}$' "
         "AND old_youtube_id IS NULL")
    args = []
    if a.slug: q += " AND slug=%s"; args.append(a.slug)
    elif not a.rescore: q += " AND (quality IS NULL OR NOT quality ? 'legacy_backcheck')"
    q += " ORDER BY finished_at"
    if a.limit: q += f" LIMIT {int(a.limit)}"
    cur.execute(q, args)
    rows = cur.fetchall()
    log(f"{len(rows)} video(s) to score")
    for slug, art, mp4 in rows:
        if not (mp4 and os.path.exists(mp4)) or not os.path.exists(art):
            log(f"skip {slug}: missing {'mp4' if not (mp4 and os.path.exists(mp4)) else 'article'}"); continue
        t0 = time.time()
        try:
            s = score((slug, art, mp4), book)
        except Exception as e:
            log(f"score failed {slug}: {e}"); continue
        cur.execute("UPDATE nova_speaks_renders SET quality = coalesce(quality, '{}'::jsonb) || jsonb_build_object('legacy_backcheck', %s::jsonb) "
                    "WHERE slug=%s", (json.dumps(s), slug))
        log(f"{'FAIL' if s['fail'] else 'ok  '} {slug[:70]} err={s['err_rate']} garbage={s['garbage_runs']} dropouts={s['dropouts']} "
            f"segs={len(s['bad_segments'])} src={s['source']} ({time.time()-t0:.0f}s)")
    if a.requeue:
        cur.execute("SELECT slug, youtube_id, quality->'legacy_backcheck'->'reasons' FROM nova_speaks_renders WHERE status='done' "
                    "AND old_youtube_id IS NULL AND (quality->'legacy_backcheck'->>'fail')::boolean" + (" AND slug=%s" if a.slug else ""),
                    [a.slug] if a.slug else [])
        for slug, yid, reasons in cur.fetchall():
            cur.execute("UPDATE nova_speaks_renders SET status='queued', old_youtube_id=youtube_id, old_youtube_retired=NULL, host=NULL, pid=NULL, "
                        "queued_at=now(), note=%s WHERE slug=%s AND status='done' AND old_youtube_id IS NULL",
                        (f"{TAG} old_youtube_id={yid}: {', '.join(reasons or [])}", slug))
            log(f"queued re-render: {slug} (old {yid})")


if __name__ == "__main__":
    main()
