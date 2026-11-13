#!/Volumes/Data/AI/youtube-up/venv/bin/python
"""nova_speaks_safety_cut.py — cut bad-image shots out of hidden Nova Speaks videos IN PLACE (same video ID/URL), verify
the served result, then flip it PUBLIC. Approved by Jordan 2026-11-13 after the content-safety sweep hid ~500 videos.

Never deletes or re-uploads a video, never changes a URL, never touches the playlist. YouTube's own Studio editor does the
cut: POST studio.youtube.com/youtubei/v1/video_editor/edit_video with
    {"externalVideoId": ID, "videoEdit": {"trimEdit": {"trimmedOutSegments": [{"start": {"millis": a}, "end": {"millis": b}}]}}}
(same cookie session/context as metadata_update; Studio schedules the edit and re-processes the same video).
Revert path: video_editor/get_last_published_edits {"encryptedVideoId": ID} shows remaining segments; Studio's own
"revert" is edit_video {"externalVideoId": ID, "endscreenEdit": {}, "adBreaksEdit": {"enableMidrollAds": false},
"isRevert": true}.

Per hidden video (nova_speaks_hidden.result='OK'):
  - spans = frame_match exposures of currently-bad images, padded 0.5 s, edges moved out to the nearest audio pause
    (<= 2 s), merged. Any direct_unverified exposure -> the ORIGINAL render is frame-matched in full against every bad
    original first: zero matches -> PUBLIC (RESTORED-NOEXPOSURE); matches become spans.
  - cut > 40 % of runtime, or no render to check -> LEFT-PRIVATE (listed, never flipped)
  - submit -> CUT-PENDING; once Studio reports EDITED: download the served video (yt-dlp, same cookies), frame-match
    against every bad original (thr 0.86) and check duration == original - cuts (+-1.5 s); clean -> --public-only,
    CUT-VERIFIED; otherwise CUT-FAILED (stays private). Every 25th verified video gets a narration spot-check
    (faster-whisper transcript around the first seam) stored in cut_note.
State lives in nova_speaks_hidden (result, cut_spans_ms, cut_submitted_at, cut_verified_at, orig_duration_ms, cut_note),
so the run is resumable.

  nova_speaks_safety_cut.py --run [--max N]     # submit + verify until done (loops through rate limits)
  nova_speaks_safety_cut.py --status
  nova_speaks_safety_cut.py --selftest
"""
import argparse, json, os, re, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PY_YT = "/Volumes/Data/AI/youtube-up/venv/bin/python"
UPL = str(HERE / "nova_speaks_upload.py")
YTDLP = "/opt/homebrew/bin/yt-dlp"
COOKIES = Path.home() / ".openclaw/cache/yt_cookies_youtube.txt"
WORK = Path(os.environ.get("NOVA_SPEAKS_SAFETY_DIR", "/Volumes/Data/AI/imgreview/speaks-safety")) / "cutverify"
PAD, SNAP, MAX_FRAC, DUR_TOL, THR = 0.5, 2.0, 0.40, 1.5, 0.86
SUBMIT_GAP = float(os.environ.get("CUT_SUBMIT_GAP", "20"))
CUT_STATES = ("CUT-PENDING", "CUT-VERIFIED", "LEFT-PRIVATE", "CUT-FAILED", "RESTORED-NOEXPOSURE")
BAD_SQL = """SELECT path FROM image_safety_review WHERE coalesce(note,'') NOT LIKE 'CLAUDE-FP%%' AND coalesce(severity,'')<>'fp'
   AND (coalesce(reviewer,'')<>'vision-v2' OR note LIKE 'CLAUDE-CONFIRMED%%'
        OR path IN (SELECT path FROM image_safety_audit WHERE final_decision='bad'))
   UNION SELECT path FROM image_safety_audit WHERE final_decision='bad'"""


def log(m): print(f"[speaks-cut {time.strftime('%H:%M:%S')}] {m}", flush=True)


# ── pure logic ───────────────────────────────────────────────────────────────────────────────────────────────────────
def merge(spans, pad=PAD, dur=None):
    """Pad, clamp and merge [a, b] second spans."""
    out = []
    for a, b in sorted((max(0.0, a - pad), b + pad if dur is None else min(dur, b + pad)) for a, b in spans):
        if out and a <= out[-1][1]: out[-1][1] = max(out[-1][1], b)
        else: out.append([a, b])
    return out


def snap(spans, pauses, dur, limit=SNAP):
    """Move each cut edge OUTWARD to the nearest pause midpoint within `limit` s (start earlier, end later)."""
    out = []
    for a, b in spans:
        before = [p for p in pauses if a - limit <= p <= a]
        after = [p for p in pauses if b <= p <= b + limit]
        out.append([max(before) if before else a, min(after) if after else min(b, dur)])
    return merge(out, pad=0, dur=dur)


def parse_hms(s):
    return sum(int(p) * m for p, m in zip(s.split(":"), (3600, 60, 1)))


# ── I/O helpers ──────────────────────────────────────────────────────────────────────────────────────────────────────
def db():
    import psycopg2
    c = psycopg2.connect("host=localhost dbname=nova_ops user=kochj"); c.autocommit = True; return c


DDL = """ALTER TABLE nova_speaks_hidden ADD COLUMN IF NOT EXISTS cut_spans_ms jsonb,
  ADD COLUMN IF NOT EXISTS cut_submitted_at timestamptz, ADD COLUMN IF NOT EXISTS cut_verified_at timestamptz,
  ADD COLUMN IF NOT EXISTS orig_duration_ms int, ADD COLUMN IF NOT EXISTS cut_note text"""


def pauses(mp4):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", mp4, "-vn", "-af", "silencedetect=noise=-35dB:d=0.25",
                        "-f", "null", "-"], capture_output=True, text=True, timeout=600)
    st = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    en = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    return [(a + b) / 2 for a, b in zip(st, en)]


def duration(mp4):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", mp4],
                       capture_output=True, text=True, timeout=60)
    return float(r.stdout.strip())


_TPL = {}


def bad_templates():
    if not _TPL:
        import nova_speaks_framematch as fm, nova_speaks_safety_exposure as ex
        cur = db().cursor(); cur.execute(BAD_SQL)
        for (p,) in cur.fetchall():
            o = ex.original(p)
            if o: _TPL[p] = [fm.norm(t[: fm.H * 2 // 3]) for t in fm.templates(o)]
    return _TPL


def frame_hits(mp4, thr=THR):
    """-> (n_frames, best score, {bad image path: [second indexes >= thr]})"""
    import numpy as np, nova_speaks_framematch as fm
    F = fm.frames(mp4); Fn = fm.norm(F[:, : fm.H * 2 // 3]); hits, top = {}, 0.0
    for p, ts in bad_templates().items():
        best = np.zeros(len(F))
        for t in ts: best = np.maximum(best, (Fn * t).sum(axis=(1, 2)))
        top = max(top, float(best.max()))
        if best.max() >= thr: hits[p] = [int(i) for i in np.where(best >= thr)[0]]
    return len(F), top, hits


class Studio:
    """Studio internal API through youtube-up's cookie session (cookies stay in ~/.openclaw/cache)."""
    def __init__(self):
        sys.path.insert(0, str(HERE))
        import nova_speaks_upload as u
        self.s = u.session(); self.data = self.s._get_session_data()
        if not self.s._session_token: self.s._get_session_token()

    def call(self, ep, body):
        from youtube_up.schema import APIContext
        d = self.data
        body = dict(body, context=APIContext.from_session_data(d.channel_id, self.s._session_token, d.delegated_session_id).to_dict())
        r = self.s._session.post(f"https://studio.youtube.com/youtubei/v1/{ep}", params={"key": d.innertube_api_key, "alt": "json"},
                                 json=body, timeout=90)
        if r.status_code == 429: raise RuntimeError("rate limited")
        r.raise_for_status(); return r.json()

    def edits(self, vid): return self.call("video_editor/get_last_published_edits", {"encryptedVideoId": vid})
    def status(self, vid): return self.call("video_editor/get_video_processing_status", {"encryptedVideoId": vid})

    def cut(self, vid, spans_ms):
        segs = [{"start": {"millis": int(a)}, "end": {"millis": int(b)}} for a, b in spans_ms]
        return self.call("video_editor/edit_video", {"externalVideoId": vid, "videoEdit": {"trimEdit": {"trimmedOutSegments": segs}}})


def set_public(vid):
    r = subprocess.run([PY_YT, UPL, f"--public-only={vid}"], capture_output=True, text=True, timeout=600)
    return f"OK {vid}" in r.stdout


def download(vid):
    WORK.mkdir(parents=True, exist_ok=True)
    out = WORK / f"{vid}.mp4"
    if out.exists(): out.unlink()
    subprocess.run([YTDLP, "-q", "--no-warnings", "--cookies", str(COOKIES), "-f", "bv*[height<=360][ext=mp4]+ba/b[height<=360]/worst",
                    "--merge-output-format", "mp4", "-o", str(WORK / "%(id)s.%(ext)s"), f"https://www.youtube.com/watch?v={vid}"],
                   capture_output=True, text=True, timeout=1200)
    return out if out.exists() else None


def seam_transcript(mp4, at, win=10):
    """Narration spot check: faster-whisper (homebrew python; the youtube-up venv has no whisper) around a seam."""
    wav = WORK / "seam.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(max(0, at - win)), "-to", str(at + win), "-i", str(mp4),
                    "-vn", "-ac", "1", "-ar", "16000", str(wav)], timeout=120)
    code = ("import sys;from faster_whisper import WhisperModel as M;"
            "m=M('small.en',device='cpu',compute_type='int8',download_root='/Volumes/Data/AI/huggingface_cache');"
            "print(' '.join(s.text.strip() for s in m.transcribe(sys.argv[1])[0]))")
    r = subprocess.run(["/opt/homebrew/bin/python3", "-c", code, str(wav)], capture_output=True, text=True, timeout=600)
    return r.stdout.strip() or f"(transcript failed: {r.stderr.strip()[-200:]})"


# ── planning ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def plan(cur, vid, slug):
    """-> (state, spans_s or None, dur_s, note). state in {'CUT', 'NOEXPOSURE', 'LEFT-PRIVATE'}."""
    cur.execute("SELECT mp4_path FROM nova_speaks_renders WHERE youtube_id=%s", (vid,))
    row = cur.fetchone(); mp4 = row and row[0]
    if not mp4 or not os.path.exists(mp4):
        return "LEFT-PRIVATE", None, None, "original render not on disk; cannot verify"
    dur = duration(mp4)
    cur.execute(f"""SELECT e.image_path, e.confidence, e.timestamps FROM image_video_exposure e
                    WHERE e.youtube_id=%s AND e.image_path IN ({BAD_SQL})""", (vid,))
    rows = cur.fetchall()
    spans, unverified = [], False
    for _, conf, ts in rows:
        if conf == "frame_match" and ts and ts.get("spans_s"): spans += [tuple(x) for x in ts["spans_s"]]
        elif conf in ("direct_unverified",): unverified = True
    if unverified or not spans:
        _, _, hits = frame_hits(mp4)
        if not hits and not spans:
            return "NOEXPOSURE", None, dur, "full frame-match of the original render: 0 bad-image frames"
        for idx in hits.values():
            for i in idx: spans.append((float(i), float(i + 1)))
    cuts = snap(merge(spans, dur=dur), pauses(mp4), dur)
    frac = sum(b - a for a, b in cuts) / dur
    if frac > MAX_FRAC:
        return "LEFT-PRIVATE", cuts, dur, f"cut would remove {frac:.0%} of {dur:.0f}s"
    return "CUT", cuts, dur, f"cut {sum(b - a for a, b in cuts):.1f}s of {dur:.0f}s in {len(cuts)} span(s)"


def run(max_n=None):
    c = db(); cur = c.cursor(); cur.execute(DDL)
    st = Studio(); submitted = verified = 0; last_submit = 0.0
    while True:
        # 1) verify pending edits that Studio finished
        cur.execute("SELECT youtube_id, cut_spans_ms, orig_duration_ms FROM nova_speaks_hidden WHERE result='CUT-PENDING' ORDER BY cut_submitted_at")
        pending = cur.fetchall()
        for vid, spans_ms, odur in pending:
            try: s = st.status(vid)
            except Exception as e: log(f"status {vid}: {e}"); time.sleep(30); continue
            if s.get("videoProcessingStatus") != "VIDEO_PROCESSING_STATUS_EDITED" or \
               s.get("lastPublishedEditVersion") != s.get("lastTranscodesPublishedEditVersion"):
                continue
            verified += verify(cur, vid, spans_ms, odur, spot=(verified % 25 == 0))
        # 2) submit the next cut
        cur.execute(f"""SELECT h.youtube_id, h.slug FROM nova_speaks_hidden h WHERE h.result='OK'
                        AND h.youtube_id ~ '^[A-Za-z0-9_-]{{11}}$' ORDER BY h.hidden_at""")
        todo = cur.fetchall()
        if not todo and not pending: log(f"done: submitted {submitted}, verified {verified}"); return
        if todo and (max_n is None or submitted < max_n) and time.time() - last_submit >= SUBMIT_GAP:
            vid, slug = todo[0]
            try:
                state, cuts, dur, note = plan(cur, vid, slug)
            except Exception as e:
                state, cuts, dur, note = "LEFT-PRIVATE", None, None, f"plan failed: {e}"
            if state == "NOEXPOSURE":
                ok = set_public(vid)
                cur.execute("UPDATE nova_speaks_hidden SET result=%s, restored_at=now(), cut_submitted_at=now(), cut_note=%s WHERE youtube_id=%s",
                            ("RESTORED-NOEXPOSURE" if ok else "CUT-FAILED", note + ("" if ok else "; --public-only failed"), vid))
                log(f"{vid} no exposure -> {'PUBLIC' if ok else 'public failed'}")
            elif state == "LEFT-PRIVATE":
                cur.execute("UPDATE nova_speaks_hidden SET result='LEFT-PRIVATE', cut_note=%s, cut_submitted_at=now(), "
                            "orig_duration_ms=%s, cut_spans_ms=%s WHERE youtube_id=%s",
                            (note, int(dur * 1000) if dur else None,
                             json.dumps([[int(a * 1000), int(b * 1000)] for a, b in cuts]) if cuts else None, vid))
                log(f"{vid} LEFT-PRIVATE: {note}")
            else:
                spans_ms = [[int(a * 1000), int(b * 1000)] for a, b in cuts]
                try:
                    e = st.edits(vid)
                    if not e.get("isOriginal", True):
                        raise RuntimeError("video already has published edits; not stacking a second cut")
                    r = st.cut(vid, spans_ms)
                    if r.get("executionStatus") not in ("EDIT_EXECUTION_STATUS_SCHEDULED", "EDIT_EXECUTION_STATUS_DONE"):
                        raise RuntimeError(f"edit_video: {str(r)[:200]}")
                    odur = int(e.get("originalVideoDurationMs") or dur * 1000)
                    cur.execute("UPDATE nova_speaks_hidden SET result='CUT-PENDING', cut_spans_ms=%s, cut_submitted_at=now(), "
                                "orig_duration_ms=%s, cut_note=%s WHERE youtube_id=%s", (json.dumps(spans_ms), odur, note, vid))
                    submitted += 1; log(f"{vid} CUT-PENDING ({note}) [{submitted}]")
                except RuntimeError as ex:
                    if "rate limited" in str(ex): log("rate limited — backing off 10 min"); time.sleep(600); continue
                    cur.execute("UPDATE nova_speaks_hidden SET result='CUT-FAILED', cut_note=%s, cut_submitted_at=now() WHERE youtube_id=%s",
                                (str(ex)[:500], vid)); log(f"{vid} CUT-FAILED: {ex}")
                except Exception as ex:  # noqa: BLE001 — network/session: retry later
                    log(f"{vid} submit error: {ex}; retrying in 60 s"); time.sleep(60)
                    try: st = Studio()
                    except Exception: pass
            last_submit = time.time()
        time.sleep(5)


def verify(cur, vid, spans_ms, odur, spot=False):
    exp = odur / 1000 - sum(b - a for a, b in spans_ms) / 1000
    mp4 = download(vid)
    if not mp4:
        log(f"{vid} download failed; will retry"); return 0
    try:
        got = duration(str(mp4)); n, top, hits = frame_hits(str(mp4))
        note = f"served {got:.2f}s (expected {exp:.2f}); {len(bad_templates())} bad originals, max {top:.3f}, hits {len(hits)}"
        if spot and spans_ms:
            note += "; seam: " + seam_transcript(mp4, spans_ms[0][0] / 1000)[:400]
        if hits or abs(got - exp) > DUR_TOL:
            cur.execute("UPDATE nova_speaks_hidden SET result='CUT-FAILED', cut_note=%s WHERE youtube_id=%s", (note, vid))
            log(f"{vid} CUT-FAILED: {note}"); return 0
        ok = set_public(vid)
        cur.execute("UPDATE nova_speaks_hidden SET result=%s, cut_verified_at=now(), restored_at=CASE WHEN %s THEN now() END, "
                    "cut_note=coalesce(cut_note,'')||' | '||%s WHERE youtube_id=%s",
                    ("CUT-VERIFIED" if ok else "CUT-FAILED", ok, note + ("" if ok else "; --public-only failed"), vid))
        log(f"{vid} {'CUT-VERIFIED -> PUBLIC' if ok else 'verified but public failed'}: {note}")
        return 1 if ok else 0
    finally:
        mp4.unlink(missing_ok=True)


def status():
    cur = db().cursor()
    cur.execute("SELECT result, count(*) FROM nova_speaks_hidden GROUP BY 1 ORDER BY 1")
    for r, n in cur.fetchall(): print(f"{r:22} {n}")


def selftest():
    assert merge([(10, 12), (12.5, 14)]) == [[9.5, 14.5]]
    assert merge([(0.2, 1)], dur=1.2) == [[0.0, 1.2]]
    assert snap([[10, 20]], [8.5, 9.2, 20.4, 23], 30) == [[9.2, 20.4]]
    assert snap([[10, 20]], [5, 25], 30) == [[10, 20]]                  # nothing within 2 s: unchanged
    assert snap([[10, 20], [21, 25]], [20.8], 30) == [[10, 25]]          # snapping can merge spans
    assert parse_hms("00:04:17") == 257
    assert "isRevert" in __doc__ and "trimmedOutSegments" in __doc__
    print("selftest ok"); return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true"); ap.add_argument("--max", type=int)
    ap.add_argument("--status", action="store_true"); ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest: return selftest()
    if a.status: status(); return 0
    if a.run: run(a.max); return 0
    ap.print_help(); return 0


if __name__ == "__main__":
    sys.exit(main())
