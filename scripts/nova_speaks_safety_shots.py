#!/opt/homebrew/bin/python3
"""nova_speaks_safety_shots.py — vision-screen what Nova Speaks videos ACTUALLY show, not just the journal images.

Why (2026-11-13): nova_speaks.py fills short articles with random ~/.openclaw/workspace/*.png from the render host —
raw ComfyUI outputs that never went through review — so a caricature variant reached a tech video while the
journal/pool-based scan saw nothing. This scans every render's shots instead:
  1. shots from the cached 1 fps 64x36 frames (new shot when consecutive-frame correlation < SHOT_CORR),
     skipping the title card (first 4 s) and end card (last 6 s);
  2. dedupe across all videos (mid-shot signature correlation >= DUP_CORR -> same still);
  3. one full-res still per unique shot (ffmpeg from the original render) -> STATE/shots/<id>.jpg;
  4. nova_image_safety.screen_image (local Ollama vision, 768 + 512 px, fail-closed) -> nova_speaks_shot_scan.
Flagged shots are reviewed by hand; confirmed ones go into image_safety_audit (final_decision='bad', path = the
shot jpg), and nova_speaks_safety_exposure.py then frame-matches and hides every video showing them.

  nova_speaks_safety_shots.py --index          # build/extend the shot index (no vision)
  nova_speaks_safety_shots.py --prefilter        # pass shots identical to an already vision-passed image
  nova_speaks_safety_shots.py --screen [-j 6]  # screen unscreened unique shots (resumable)
  nova_speaks_safety_shots.py --cover            # per-second coverage; uncovered runs -> new shots
  nova_speaks_safety_shots.py --confirm ID.. --category C --note N
  nova_speaks_safety_shots.py --restore [--dry]  # HELD-SHOTSCAN -> PUBLIC when fully clean
  nova_speaks_safety_shots.py --status | --selftest
"""
import argparse, json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np

STATE = Path(os.environ.get("NOVA_SPEAKS_SAFETY_DIR", "/Volumes/Data/AI/imgreview/speaks-safety"))
FR, SHOTS, WORK_SC = STATE / "frames", STATE / "shots", STATE / "served-shots"
SHOT_CORR, DUP_CORR, HEAD, TAIL = 0.6, 0.85, 4, 6
DDL = """CREATE TABLE IF NOT EXISTS nova_speaks_shot_scan (shot_id int PRIMARY KEY, path text, slug text, sec int,
  ok boolean, flags text[], verdict jsonb, screened_at timestamptz, review text);
CREATE TABLE IF NOT EXISTS nova_speaks_shot_occ (shot_id int, slug text, start_s int, end_s int, PRIMARY KEY (slug, start_s));
CREATE TABLE IF NOT EXISTS nova_speaks_shot_indexed (slug text PRIMARY KEY)"""


def log(m): print(f"[speaks-shots {time.strftime('%H:%M:%S')}] {m}", flush=True)


def db():
    import psycopg2
    c = psycopg2.connect("host=localhost dbname=nova_ops user=kochj"); c.autocommit = True; return c


def sig(F):
    """normalized top-two-thirds signature(s) of uint8 frames [n, 36, 64] (lower third = caption overlay)."""
    a = F[..., :24, :].astype(np.float32)
    a = a - a.mean(axis=(-1, -2), keepdims=True)
    return a / (np.sqrt((a ** 2).sum(axis=(-1, -2), keepdims=True)) + 1e-6)


def shots(F, shot_corr=SHOT_CORR, head=HEAD, tail=TAIL):
    """-> [(start, end)] second ranges of shots in frames F (end exclusive), title/end card trimmed."""
    if len(F) < 2: return []
    S = sig(F); c = (S[1:] * S[:-1]).sum(axis=(1, 2))
    cuts = [0] + [i + 1 for i in np.where(c < shot_corr)[0]] + [len(F)]
    out = []
    for a, b in zip(cuts, cuts[1:]):
        a, b = max(a, head), min(b, len(F) - tail)
        if b - a >= 1: out.append((a, b))
    return out


def index():
    c = db(); cur = c.cursor(); [cur.execute(s) for s in DDL.split(";")]
    cur.execute("SELECT shot_id, slug, sec FROM nova_speaks_shot_scan ORDER BY shot_id")
    kept = cur.fetchall()
    cache = {}
    def frames_of(slug):
        if slug not in cache: cache[slug] = np.load(FR / f"{slug}.npy")
        return cache[slug]
    K = np.zeros((max(80000, 2 * len(kept)), 24 * 64), np.float32)   # ponytail: fixed capacity; ~43k raw shots total
    for i, (_, s, t) in enumerate(kept): K[i] = sig(frames_of(s)[t]).reshape(-1)
    nk = len(kept)
    nid = (kept[-1][0] + 1) if kept else 1
    cur.execute("SELECT slug FROM nova_speaks_shot_indexed"); done = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT slug, mp4_path FROM nova_speaks_renders WHERE mp4_path IS NOT NULL ORDER BY finished_at")
    renders = [(s, m) for s, m in cur.fetchall() if s not in done and (FR / f"{s}.npy").exists()]
    new = 0
    for n, (slug, mp4) in enumerate(renders):
        F = np.load(FR / f"{slug}.npy")
        for a, b in shots(F):
            a, b = int(a), int(b); mid = (a + b) // 2
            v = sig(F[mid]).reshape(-1)
            sc = K[:nk] @ v
            if len(sc) and sc.max() >= DUP_CORR:
                sid = kept[int(sc.argmax())][0]
            else:
                if not os.path.exists(mp4): continue
                SHOTS.mkdir(parents=True, exist_ok=True)
                out = SHOTS / f"{nid}.jpg"
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{mid + 0.5}", "-i", mp4, "-frames:v", "1", "-q:v", "3", str(out)], timeout=120)
                if not out.exists(): continue
                sid = nid; nid += 1; new += 1
                kept.append((sid, slug, mid)); K[nk] = v; nk += 1
                cur.execute("INSERT INTO nova_speaks_shot_scan (shot_id, path, slug, sec) VALUES (%s,%s,%s,%s)", (sid, str(out), slug, mid))
            cur.execute("INSERT INTO nova_speaks_shot_occ VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (sid, slug, a, b))
        cur.execute("INSERT INTO nova_speaks_shot_indexed VALUES (%s) ON CONFLICT DO NOTHING", (slug,))
        if n % 50 == 0: log(f"{n}/{len(renders)} videos, {len(kept)} unique shots (+{new})")
    log(f"indexed {len(renders)} videos; {len(kept)} unique shots (+{new})")


CLEAN_SQL = """SELECT path FROM image_safety_audit a WHERE (final_decision='ok' OR (final_decision IS NULL AND NOT flagged))
  AND NOT EXISTS (SELECT 1 FROM image_safety_audit b WHERE b.path=a.path AND b.final_decision='bad')"""


_CLEAN = []


def clean_templates():
    """-> (T [n, 1536] normalized templates of every image the v2 vision scan passed, owner paths). Cached."""
    if not _CLEAN:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import nova_speaks_framematch as fm
        cur = db().cursor(); cur.execute(CLEAN_SQL)
        T, owner = [], []
        for (p,) in cur.fetchall():
            try:
                for t in fm.templates(p): T.append(fm.norm(t[:24]).reshape(-1)); owner.append(p)
            except Exception:
                continue
        _CLEAN[:] = [np.asarray(T, np.float32), owner]
        log(f"{len(set(owner))} clean images, {len(T)} templates")
    return _CLEAN


def prefilter(thr=0.95):
    """Shots that are the same picture as an image the v2 vision scan already passed (corr >= thr against its
    Ken Burns templates) get that verdict instead of another 2 x 30 s vision call. Anything else (workspace
    stills, stale-clone versions) still goes to vision."""
    T, owner = clean_templates()
    cur = db().cursor()
    cur.execute("SELECT shot_id, slug, sec FROM nova_speaks_shot_scan WHERE screened_at IS NULL")
    n = 0
    for sid, slug, sec in cur.fetchall():
        v = sig(np.load(FR / f"{slug}.npy")[sec]).reshape(-1)
        sc = T @ v; i = int(sc.argmax())
        if sc[i] >= thr:
            n += 1
            cur.execute("UPDATE nova_speaks_shot_scan SET ok=true, flags='{}', verdict=%s, screened_at=now() WHERE shot_id=%s",
                        (json.dumps({"known_clean": owner[i], "score": float(sc[i])}), sid))
    log(f"prefilter: {n} shots matched an already-passed image")


def uncovered_runs(S, occ_reps, T, own=0.85, clean=0.95, head=HEAD, tail=TAIL, min_run=2):
    """S [n, d] frame signatures; occ_reps [(a, b, rep_sig)]. A second is covered when it matches its own shot's
    still (>= own) or an already-passed image (>= clean). -> [(start, end)] runs of >= min_run uncovered seconds
    (single uncovered seconds are crossfades)."""
    n = len(S); cov = np.zeros(n, bool); cov[:head] = True; cov[max(head, n - tail):] = True
    for a, b, r in occ_reps:
        cov[a:b] |= (S[a:b] @ r) >= own
    u = np.where(~cov)[0]
    if len(u) and len(T):
        cov[u] |= (S[u] @ T.T).max(axis=1) >= clean
    runs, start = [], None
    for i in range(n + 1):
        if i < n and not cov[i]:
            start = i if start is None else start
        elif start is not None:
            if i - start >= min_run: runs.append((start, i))
            start = None
    return runs


def cover():
    """Per-second coverage check of every indexed video; uncovered runs become new shots (resumable)."""
    c = db(); cur = c.cursor()
    cur.execute("ALTER TABLE nova_speaks_shot_occ DROP CONSTRAINT IF EXISTS nova_speaks_shot_occ_pkey")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS nova_speaks_shot_occ_u ON nova_speaks_shot_occ (shot_id, slug, start_s)")
    cur.execute("CREATE TABLE IF NOT EXISTS nova_speaks_shot_covered (slug text PRIMARY KEY, new_shots int, at timestamptz DEFAULT now())")
    T, _ = clean_templates()
    cur.execute("SELECT shot_id, slug, sec FROM nova_speaks_shot_scan ORDER BY shot_id")
    reps = {sid: (sl, sec) for sid, sl, sec in cur.fetchall()}
    rs = {}
    def rep_sig(sid):
        if sid not in rs:
            sl, sec = reps[sid]; rs[sid] = sig(np.load(FR / f"{sl}.npy")[sec]).reshape(-1)
        return rs[sid]
    K = np.stack([rep_sig(i) for i in reps]) if reps else np.zeros((0, 1536), np.float32); kid = list(reps)
    nid = max(reps, default=0) + 1
    cur.execute("""SELECT i.slug, r.mp4_path FROM nova_speaks_shot_indexed i JOIN nova_speaks_renders r USING (slug)
                   WHERE i.slug NOT IN (SELECT slug FROM nova_speaks_shot_covered)""")
    todo = cur.fetchall(); total_new = 0
    for n, (slug, mp4) in enumerate(todo):
        F = np.load(FR / f"{slug}.npy"); S = sig(F).reshape(len(F), -1)
        cur.execute("SELECT shot_id, start_s, end_s FROM nova_speaks_shot_occ WHERE slug=%s", (slug,))
        runs = uncovered_runs(S, [(a, b, rep_sig(sid)) for sid, a, b in cur.fetchall()], T)
        new = 0
        for a, b in runs:
            mid = (a + b) // 2; v = S[mid]
            sc = K @ v if len(K) else np.zeros(0)
            if len(sc) and sc.max() >= 0.93:
                sid = kid[int(sc.argmax())]
            else:
                out = SHOTS / f"{nid}.jpg"
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{mid + 0.5}", "-i", mp4, "-frames:v", "1", "-q:v", "3", str(out)], timeout=120)
                if not out.exists(): raise RuntimeError(f"cannot extract {slug}@{mid}")
                sid = nid; nid += 1; new += 1
                reps[sid] = (slug, mid); rs[sid] = v; K = np.vstack([K, v[None]]); kid.append(sid)
                cur.execute("INSERT INTO nova_speaks_shot_scan (shot_id, path, slug, sec) VALUES (%s,%s,%s,%s)", (sid, str(out), slug, mid))
            cur.execute("INSERT INTO nova_speaks_shot_occ VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (sid, slug, a, b))
        cur.execute("INSERT INTO nova_speaks_shot_covered (slug, new_shots) VALUES (%s,%s)", (slug, new)); total_new += new
        if n % 100 == 0: log(f"cover {n}/{len(todo)} videos, +{total_new} shots")
    log(f"cover: {len(todo)} videos, +{total_new} new shots")

READY_SQL = """SELECT 1 FROM nova_speaks_shot_covered c WHERE c.slug=%(slug)s AND NOT EXISTS (
  SELECT 1 FROM nova_speaks_shot_occ o JOIN nova_speaks_shot_scan s USING (shot_id) WHERE o.slug=%(slug)s
   AND NOT (s.screened_at IS NOT NULL AND (s.review LIKE 'CLAUDE-FP%%' OR (s.ok AND s.review IS NULL))))"""


def confirm(ids, category, note):
    """Hand-confirmed bad shots -> bad set (image_safety_audit) + exposures from their exact occurrences."""
    cur = db().cursor()
    for sid in ids:
        cur.execute("UPDATE nova_speaks_shot_scan SET review=%s WHERE shot_id=%s RETURNING path", (f"CLAUDE-CONFIRMED {category}: {note}", sid))
        (path,) = cur.fetchone()
        cur.execute("""INSERT INTO image_safety_audit (path, kind, model, flagged, reviewed_by_claude, final_decision, reason)
                       VALUES (%s,'shot','v2:claude-review',true,true,'bad',%s) ON CONFLICT (path, model) DO UPDATE SET final_decision='bad'""",
                    (path, f"{category}: {note}"))
        cur.execute("""SELECT o.slug, r.youtube_id, array_agg(ARRAY[o.start_s, o.end_s] ORDER BY o.start_s) FROM nova_speaks_shot_occ o
                       JOIN nova_speaks_renders r USING (slug) WHERE o.shot_id=%s GROUP BY 1, 2""", (sid,))
        for slug, yid, spans in cur.fetchall():
            cur.execute("""INSERT INTO image_video_exposure (image_path, slug, youtube_id, link, confidence, timestamps, category, severity)
                           VALUES (%s,%s,%s,'shot_scan','frame_match',%s,%s,'high') ON CONFLICT (image_path, slug) DO UPDATE
                           SET timestamps=EXCLUDED.timestamps, confidence='frame_match', ts=now()""",
                        (path, slug, yid, json.dumps({"spans_s": [[float(a), float(b)] for a, b in spans], "score": 1.0}), category))
        log(f"shot {sid} confirmed bad ({category}); exposures written")


def restore(dry=False):
    """HELD-SHOTSCAN -> PUBLIC only when every second is covered, every shot has a clean vision verdict or a Claude FP
    review, and no currently-bad exposure exists."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from nova_speaks_safety_cut import BAD_SQL, set_public_many
    cur = db().cursor()
    cur.execute(f"""SELECT h.youtube_id, r.slug FROM nova_speaks_hidden h JOIN nova_speaks_renders r USING (youtube_id)
                    WHERE h.result='HELD-SHOTSCAN' AND NOT EXISTS (SELECT 1 FROM image_video_exposure e
                      WHERE e.youtube_id=h.youtube_id AND e.confidence='frame_match' AND e.image_path IN ({BAD_SQL}))""")
    ready = []
    for yid, slug in cur.fetchall():
        cur.execute(READY_SQL, {"slug": slug})
        if cur.fetchone(): ready.append(yid)
    log(f"{len(ready)} held videos ready to restore")
    if dry or not ready: return ready
    for yid, ok in set_public_many(ready).items():
        cur.execute("UPDATE nova_speaks_hidden SET result=%s, restored_at=CASE WHEN %s THEN now() END WHERE youtube_id=%s",
                    ("RESTORED-SHOTSCAN" if ok else "HELD-SHOTSCAN", ok, yid))
    return ready


def served_check(mp4, slug):
    """Cut-pass check of a SERVED video: every second must be one of this render's vision-passed shots or an
    already-passed image; uncovered runs are vision-screened. -> (ok, note)"""
    sys.path.insert(0, str(Path(__file__).resolve().parent)); sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))
    import nova_speaks_framematch as fm
    from nova_image_safety import screen_image
    cur = db().cursor()
    cur.execute("""SELECT DISTINCT s.slug, s.sec FROM nova_speaks_shot_occ o JOIN nova_speaks_shot_scan s USING (shot_id)
                   WHERE o.slug=%s AND s.screened_at IS NOT NULL AND (s.review LIKE 'CLAUDE-FP%%' OR (s.ok AND s.review IS NULL))""", (slug,))
    reps = [sig(np.load(FR / f"{sl}.npy")[sec]).reshape(-1) for sl, sec in cur.fetchall()]
    F = fm.frames(str(mp4)).astype(np.uint8); S = sig(F).reshape(len(F), -1)
    runs = uncovered_runs(S, [(0, len(S), r) for r in reps], clean_templates()[0])
    for a, b in runs:
        out = WORK_SC / f"{Path(mp4).stem}-{a}.jpg"; WORK_SC.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{(a + b) // 2 + 0.5}", "-i", str(mp4), "-frames:v", "1", str(out)], timeout=120)
        ok, info = screen_image(str(out))
        if not ok: return False, f"served shot {a}-{b}s flagged {info.get('flags')} ({out})"
    return True, f"{len(runs)} uncovered run(s) vision-clean"


def screen(jobs=6):
    sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))
    from nova_image_safety import screen_image
    cur = db().cursor()
    cur.execute("SELECT shot_id, path FROM nova_speaks_shot_scan WHERE screened_at IS NULL ORDER BY shot_id")
    todo = cur.fetchall(); log(f"{len(todo)} shots to screen")
    def one(row):
        sid, path = row
        ok, info = screen_image(path)
        db().cursor().execute("UPDATE nova_speaks_shot_scan SET ok=%s, flags=%s, verdict=%s, screened_at=now() WHERE shot_id=%s",
                              (ok, info.get("flags") or [], json.dumps(info, default=str), sid))
        return ok
    bad = 0
    with ThreadPoolExecutor(jobs) as ex:
        for i, ok in enumerate(ex.map(one, todo), 1):
            bad += not ok
            if i % 200 == 0: log(f"{i}/{len(todo)} screened, {bad} flagged")
    log(f"screened {len(todo)}, flagged {bad}")


def status():
    cur = db().cursor()
    cur.execute("SELECT count(*), count(screened_at), count(*) FILTER (WHERE ok IS FALSE), count(review) FROM nova_speaks_shot_scan")
    print("shots %s screened %s flagged %s reviewed %s" % cur.fetchone())
    cur.execute("SELECT count(*) FROM nova_speaks_shot_indexed"); print("videos indexed", cur.fetchone()[0])


def selftest():
    rng = np.random.default_rng(0)
    A, B = rng.integers(0, 255, (36, 64)), rng.integers(0, 255, (36, 64))
    F = np.stack([A] * 10 + [B] * 10 + [A] * 10).astype(np.uint8)
    assert shots(F, head=0, tail=0) == [(0, 10), (10, 20), (20, 30)]
    assert shots(F) == [(4, 10), (10, 20), (20, 24)]
    assert float((sig(F[0]) * sig(F[25])).sum()) > 0.99 > float((sig(F[0]) * sig(F[15])).sum())
    S = sig(F).reshape(len(F), -1)
    assert uncovered_runs(S, [(0, 30, S[0])], np.zeros((0, S.shape[1]), np.float32), head=0, tail=0) == [(10, 20)]
    assert uncovered_runs(S, [(0, 30, S[0])], S[15][None], head=0, tail=0) == []          # known-clean covers it
    print("selftest ok"); return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    for f in ("index", "cover", "prefilter", "screen", "restore", "status", "selftest"): ap.add_argument(f"--{f}", action="store_true")
    ap.add_argument("-j", type=int, default=6)
    ap.add_argument("--confirm", type=int, nargs="+", metavar="SHOT_ID"); ap.add_argument("--category", default="other")
    ap.add_argument("--note", default=""); ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest: return selftest()
    if a.index: index()
    if a.cover: cover()
    if a.prefilter: prefilter()
    if a.screen: screen(a.j)
    if a.confirm: confirm(a.confirm, a.category, a.note)
    if a.restore: restore(a.dry)
    if a.status: status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
