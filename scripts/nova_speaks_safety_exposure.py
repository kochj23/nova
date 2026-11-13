#!/usr/bin/env python3
"""YouTube side only (never touches image files). For every flagged image (image_safety_review, all severities, +
image_safety_audit final_decision='bad'):
  1. recover the ORIGINAL pixels (quarantine copy, journal git history, or pool<->journal satire twin)
  2. frame-match it against every cached video -> image_video_exposure (confidence frame_match, timestamps)
     plus direct links (satire pick / own cover) for videos with no cached frames -> 'direct_unverified'
  3. set every exposed video PRIVATE (--private-only; playlist + URL untouched) -> nova_speaks_hidden
Loop with --loop. Worst categories first. Frames of new uploads are cached each pass.
State (frames, originals, scanned.json, early quarantine) lives in $NOVA_SPEAKS_SAFETY_DIR
(default /Volumes/Data/AI/imgreview/speaks-safety). Videos owned by nova_speaks_safety_cut.py (CUT-*/LEFT-PRIVATE)
are not re-hidden unless a newer exposure appears."""
import hashlib, io, json, os, re, subprocess, sys, time
from pathlib import Path
import numpy as np
import psycopg2
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import nova_speaks_framematch as fm
import nova_speaks_safety_actions as act

J = Path.home() / "nova-journal"
FR = act.STATE / "frames"
ORIG = act.STATE / "originals"; ORIG.mkdir(exist_ok=True)
THR = 0.86
PRI = {"antisemitic": 0, "racist": 0, "ethnic-caricature": 0, "hate-symbol": 1, "nudity": 1, "sexual": 1}
SEV = {"high": 0, "medium": 1, "low": 2}


def is_placeholder(p):
    try:
        im = Image.open(p).convert("L").resize((64, 36))
        a = np.asarray(im, np.float32)
        # flat gradient (+ maybe a word): tiny horizontal detail
        return float(np.abs(np.diff(a, axis=1)).mean()) < 2.0
    except Exception:
        return True


def git_original(jp):
    rel = str(Path(jp).relative_to(J))
    r = subprocess.run(["git", "log", "--format=%H", "--", rel], cwd=J, capture_output=True, text=True)
    for h in r.stdout.split():
        b = subprocess.run(["git", "show", f"{h}:{rel}"], cwd=J, capture_output=True).stdout
        if b:
            try:
                im = Image.open(io.BytesIO(b)); im.load()
                tmp = ORIG / (hashlib.md5(rel.encode()).hexdigest() + Path(rel).suffix)
                tmp.write_bytes(b)
                if not is_placeholder(tmp):
                    return tmp
            except Exception:
                continue
    return None


def original(path):
    p = Path(path)
    cands = [act.Q / str(p).lstrip("/"), p]
    pool_rel, jp, slug = act.related(path)
    if pool_rel: cands.append(act.Q / ("Volumes/nas/" + pool_rel))
    if jp is not None: cands.append(act.Q / str(jp).lstrip("/"))
    MQ = Path("/Volumes/Data/AI/imgreview/quarantine")
    names = {p.name} | ({Path(pool_rel).name} if pool_rel else set()) | ({jp.name} if jp is not None else set())
    for n in names:
        cands += [MQ / str(p).lstrip("/")] + sorted(MQ.glob(f"*-{n}"))
        if pool_rel: cands.append(MQ / ("Volumes/nas/" + pool_rel))
        if jp is not None: cands.append(MQ / str(jp).lstrip("/"))
    for c in cands:
        if c.exists() and c.is_file() and not is_placeholder(c):
            return c
    for j in ([jp] if jp is not None else []):
        if str(j).startswith(str(J)):
            g = git_original(j)
            if g: return g
    return None


def cache_new_frames(limit=20):
    """Cache 1 s / 64x36 gray frames of newly uploaded renders (the original render mp4, pre-cut)."""
    FR.mkdir(parents=True, exist_ok=True)
    cur = act.db().cursor()
    cur.execute("""SELECT slug, mp4_path FROM nova_speaks_renders WHERE youtube_id ~ '^[A-Za-z0-9_-]{11}$'
                   AND mp4_path IS NOT NULL ORDER BY finished_at DESC""")
    n = 0
    for slug, mp4 in cur.fetchall():
        if n >= limit: break
        if (FR / f"{slug}.npy").exists() or not os.path.exists(mp4): continue
        F = fm.frames(mp4).astype(np.uint8)
        tmp = FR / f"{slug}.tmp.npy"; np.save(tmp, F); os.replace(tmp, FR / f"{slug}.npy"); n += 1
    return n


def load_frames():
    out = {}
    for f in FR.glob("*.npy"):
        if ".tmp" in f.name: continue
        try:
            F = np.load(f).astype(np.float32)
            out[f.stem] = fm.norm(F[:, : fm.H * 2 // 3])
        except Exception:
            pass
    return out


def match_all(img, frames):
    T = [fm.norm(t[: fm.H * 2 // 3]) for t in fm.templates(img)]
    hits = {}
    for slug, Fn in frames.items():
        best = np.zeros(len(Fn), np.float32)
        for tn in T:
            best = np.maximum(best, np.einsum("nij,ij->n", Fn, tn))
        idx = np.where(best >= THR)[0]
        if len(idx):
            spans = []
            for i in idx:
                s = float(i * fm.STEP)
                if spans and s - spans[-1][1] <= fm.STEP * 1.5: spans[-1][1] = s
                else: spans.append([s, s])
            hits[slug] = (float(best.max()), [[a, b + fm.STEP] for a, b in spans])
    return hits


SCANNED = act.STATE / "scanned.json"


def run_once(frames):
    c = act.db(); cur = c.cursor(); cur.execute(act.DDL)
    cur.execute("""SELECT path, category, severity, coalesce(note,'') FROM image_safety_review
                   WHERE coalesce(note,'') NOT LIKE 'CLAUDE-FP%%' AND coalesce(severity,'') <> 'fp' AND (coalesce(reviewer,'') <> 'vision-v2'
                      OR path IN (SELECT path FROM image_safety_audit WHERE final_decision='bad')
                      OR note LIKE 'CLAUDE-CONFIRMED%%')
                   UNION ALL SELECT path, coalesce(reason,'vision'), 'medium', coalesce(reason,'') FROM image_safety_audit WHERE final_decision='bad'""")
    rows = {r[0]: r for r in cur.fetchall()}
    scanned = json.loads(SCANNED.read_text()) if SCANNED.exists() else {}
    cur.execute("SELECT slug, youtube_id FROM nova_speaks_renders WHERE youtube_id ~ '^[A-Za-z0-9_-]{11}$'")
    yid = dict(cur.fetchall())
    # originals + templates for every image that still has unscanned videos (or no original yet)
    imgs, T, owner = [], [], []
    for path, (p_, cat, sev, note) in rows.items():
        sc = set(scanned.get(path, []))
        if frames.keys() <= sc and path in scanned: continue
        orig = original(path)
        if not orig:
            scanned.setdefault(path, [])
        imgs.append((path, cat, sev, orig, sc))
        if orig:
            for t in fm.templates(orig):
                T.append(fm.norm(t[: fm.H * 2 // 3]).ravel()); owner.append(len(imgs) - 1)
    if not imgs: return 0
    T = np.array(T, np.float32).T if T else np.zeros((1, 0), np.float32)
    owner = np.array(owner)
    hits = {i: {} for i in range(len(imgs))}
    for slug, Fn in frames.items():
        need = [i for i, im in enumerate(imgs) if im[3] and slug not in im[4]]
        if not need: continue
        S = Fn.reshape(len(Fn), -1) @ T              # frames x templates
        for i in need:
            best = S[:, owner == i].max(axis=1)
            idx = np.where(best >= THR)[0]
            if len(idx):
                spans = []
                for k in idx:
                    t = float(k * fm.STEP)
                    if spans and t - spans[-1][1] <= fm.STEP * 1.5: spans[-1][1] = t
                    else: spans.append([t, t])
                hits[i][slug] = (float(best.max()), [[a, b + fm.STEP] for a, b in spans])
    reason_by_vid = {}
    for i, (path, cat, sev, orig, sc) in enumerate(imgs):
        pool_rel, jp, _ = act.related(path)
        direct = act.videos_for(cur, pool_rel, jp)
        exp = {slug: ("frame_match", sp, score) for slug, (score, sp) in hits[i].items()}
        for slug, y, link, ids in direct:
            if slug in exp: continue
            if orig and slug in frames:
                cur.execute("UPDATE image_video_exposure SET confidence='frame_negative' WHERE image_path=%s AND slug=%s AND confidence='direct'", (path, slug))
            else:
                exp[slug] = ("direct_unverified:" + link, None, None)
        for slug, (conf, spans, score) in exp.items():
            v = yid.get(slug)
            cur.execute("""INSERT INTO image_video_exposure (image_path, slug, youtube_id, link, confidence, timestamps, category, severity)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (image_path, slug) DO UPDATE SET confidence=EXCLUDED.confidence,
                           timestamps=EXCLUDED.timestamps, youtube_id=EXCLUDED.youtube_id,
                           ts=CASE WHEN (image_video_exposure.confidence, image_video_exposure.timestamps, image_video_exposure.youtube_id)
                                   IS DISTINCT FROM (EXCLUDED.confidence, EXCLUDED.timestamps, EXCLUDED.youtube_id)
                                   THEN now() ELSE image_video_exposure.ts END
                           WHERE image_video_exposure.link IS DISTINCT FROM 'shot_scan'""",  # ts = 'new exposure' clock for the cut pass; shot-scan rows carry exact shot occurrences and are never narrowed
                        (path, slug, v, conf.split(":")[-1], conf.split(":")[0], json.dumps({"spans_s": spans, "score": score}) if spans else None, cat, sev))
            if v:
                d = reason_by_vid.setdefault(v, {"slug": slug, "reasons": set(), "paths": set(), "ids": set(), "cats": set(), "sev": 9})
                d["reasons"].add(f"{sev}/{cat} {conf}: {Path(path).name}"); d["paths"].add(path); d["cats"].add(cat)
                d["sev"] = min(d["sev"], PRI.get(cat, 2))
        if orig:
            scanned[path] = sorted(sc | set(frames))
        if exp or not orig:
            print(f"{sev}/{cat} {Path(path).name[:60]} orig={'Y' if orig else 'N'} exposed={len(exp)} (frame {len(hits[i])})", flush=True)
    SCANNED.write_text(json.dumps(scanned))
    # hide set comes from the DB every pass, so earlier failures are retried
    cur.execute("""SELECT e.youtube_id, e.slug, e.image_path, e.category, e.severity, e.confidence FROM image_video_exposure e
                   WHERE (e.confidence IN ('frame_match','direct_unverified')
                          OR (e.confidence IN ('frame_negative','direct') AND e.severity IN ('high','medium')))
                     AND e.youtube_id ~ '^[A-Za-z0-9_-]{11}$'""")
    reason_by_vid = {}
    for v, slug, path, cat, sev, conf in cur.fetchall():
        if path not in rows: continue          # image was cleared as a false positive since it was recorded
        d = reason_by_vid.setdefault(v, {"slug": slug, "reasons": set(), "paths": set(), "ids": set(), "cats": set(), "sev": 9})
        d["reasons"].add(f"{sev}/{cat} {conf}: {Path(path).name}"); d["paths"].add(path); d["cats"].add(cat)
        d["sev"] = min(d["sev"], PRI.get(cat, 2))
    order = sorted(reason_by_vid, key=lambda v: reason_by_vid[v]["sev"])
    n = act.hide(cur, order, reason_by_vid)
    print(f"pass: {len(imgs)} images, {len(reason_by_vid)} exposed videos, {n} newly hidden", flush=True)
    return n


if __name__ == "__main__":
    act.DRY = False
    while True:
        try: cache_new_frames()
        except Exception as e: print("frame cache:", e, flush=True)
        frames = load_frames()
        print(f"{len(frames)} videos cached", flush=True)
        try: run_once(frames)
        except Exception as e:                  # ponytail: transient PG errors must not kill the loop
            import traceback; traceback.print_exc()
            if "--loop" not in sys.argv: raise
        if "--loop" not in sys.argv: break
        time.sleep(180)
