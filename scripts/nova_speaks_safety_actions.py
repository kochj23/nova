#!/usr/bin/env python3
"""Content-safety actioner (2026-11-12). Consumes nova_ops.image_safety_review (actioned=false), plus
image_safety_audit rows with final_decision='bad'. NEVER changes a URI:
  - pool: active=false + reason, file overwritten in place (same name/format)
  - journal: file overwritten in place with a neutral placeholder (same name/format/size); inline satire
    block removed from the article body (article path unchanged)
  - videos that used the image directly (satire pick or own cover): set PRIVATE only (stay in playlist)
Originals are quarantined under $NOVA_SPEAKS_SAFETY_DIR/quarantine-early (local only) for frame-matching timestamps later.
"""
import hashlib, json, os, re, shutil, subprocess, sys, time
from pathlib import Path
import psycopg2
from PIL import Image

HOME = Path.home()
J = HOME / "nova-journal"
IMG = J / "static/images"
NAS = Path("/Volumes/nas")
POOL = NAS / "nova-satire/pool"
STATE = Path(os.environ.get("NOVA_SPEAKS_SAFETY_DIR", "/Volumes/Data/AI/imgreview/speaks-safety"))
Q = STATE / "quarantine-early"
MARKER = "<!-- satire-image -->"
PY = "/Volumes/Data/AI/youtube-up/venv/bin/python"
UPL = "/Volumes/nas/nova-fs/scripts/nova_speaks_upload.py"
SEV = {"high": 0, "medium": 1, "low": 2}
DRY = "--dry" in sys.argv

DDL = """
ALTER TABLE satire_images ADD COLUMN IF NOT EXISTS inactive_reason text;
CREATE TABLE IF NOT EXISTS image_contained (path text PRIMARY KEY, kind text, category text, severity text, note text,
  source text, original_sha text, quarantine_path text, actions text[], contained_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS image_video_exposure (image_path text, slug text, youtube_id text, link text, confidence text,
  timestamps jsonb, category text, severity text, ts timestamptz DEFAULT now(), PRIMARY KEY (image_path, slug));
CREATE TABLE IF NOT EXISTS nova_speaks_hidden (youtube_id text PRIMARY KEY, slug text, reason text, image_paths text[],
  image_ids int[], categories text[], hidden_at timestamptz, result text, restored_at timestamptz);
"""


def db():
    c = psycopg2.connect("host=localhost dbname=nova_ops user=kochj"); c.autocommit = True; return c


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]


def placeholder(path):
    """Overwrite in place: neutral navy->violet gradient, same size + format."""
    p = Path(path)
    try:
        with Image.open(p) as im:
            w, h = im.size
    except Exception:
        w, h = 1200, 675
    g = Image.linear_gradient("L").resize((w, h)).rotate(90, expand=False).resize((w, h))
    a = Image.new("RGB", (w, h), (11, 15, 26)); b = Image.new("RGB", (w, h), (52, 38, 92))
    out = Image.composite(b, a, g)
    fmt = {".webp": "WEBP", ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}[p.suffix.lower()]
    tmp = p.with_name(p.name + ".tmp")
    out.save(tmp, fmt, **({"quality": 80} if fmt != "PNG" else {}))
    os.replace(tmp, p)


def quarantine(path):
    p = Path(path)
    dst = Q / str(p).lstrip("/")
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
    return dst


def related(path):
    """-> (pool_rel or None, journal_abs or None, satire_slug or None)"""
    p = str(path)
    if p.startswith(str(POOL)) or p.startswith("nova-satire/pool/"):
        name = Path(p).name
        rel = f"nova-satire/pool/{name}"
        m = re.match(r"article-(.+)\.webp$", name)
        jp = IMG / "satire" / f"{m.group(1)}.webp" if m else None
        return rel, (jp if jp and jp.exists() else None), (m.group(1) if m else None)
    jp = Path(p)
    if jp.parent.name == "satire" and jp.parent.parent == IMG:
        rel = f"nova-satire/pool/article-{jp.stem}.webp"
        return rel, jp, jp.stem
    return None, jp, None


def articles_referencing(url):
    r = subprocess.run(["grep", "-rlF", url, str(J / "content")], capture_output=True, text=True)
    return [x for x in r.stdout.splitlines() if x.endswith(".md")]


def strip_satire_block(md_path, url):
    s = Path(md_path).read_text()
    # block = MARKER\n![cap](url)\n*cap*
    pat = re.compile(r"\n*" + re.escape(MARKER) + r"\n!\[[^\]]*\]\(" + re.escape(url) + r"\)\n\*[^\n]*\*\n?")
    new = pat.sub("\n\n", s)
    if new == s:  # fall back: drop just the image line
        new = re.sub(r"!\[[^\]]*\]\(" + re.escape(url) + r"\)\n?", "", s)
    if new != s and not DRY:
        Path(md_path).write_text(new)
    return new != s


def videos_for(cur, pool_rel, jp):
    """Direct exposure: satire picks + the article's own cover. -> [(slug, youtube_id, link, image_ids)]"""
    out = {}
    ids = []
    if pool_rel:
        cur.execute("SELECT id FROM satire_images WHERE path=%s", (pool_rel,))
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute("""SELECT DISTINCT p.slug, r.youtube_id FROM satire_video_picks p LEFT JOIN nova_speaks_renders r
                           ON r.slug=p.slug WHERE p.image_ids && %s""", (ids,))
            for slug, yid in cur.fetchall():
                out[slug] = (slug, yid, "satire_pick", ids)
    if jp is not None and jp.parent.name != "satire":
        section, stem = jp.parent.name, jp.stem
        url = f"/images/{section}/{jp.name}"
        slugs = {stem} | {Path(a).stem for a in articles_referencing(url) if re.search(r'^\s*image:\s*"?' + re.escape(url), Path(a).read_text(), re.M)}
        cur.execute("SELECT slug, youtube_id FROM nova_speaks_renders WHERE slug = ANY(%s)", (list(slugs),))
        for slug, yid in cur.fetchall():
            out.setdefault(slug, (slug, yid, "own_cover", ids))
    return list(out.values())


def hide(cur, vids, reason_by_vid):
    vids = [v for v in vids if v and re.fullmatch(r"[\w-]{11}", v)]
    # cut pass (2026-11-13): videos being cut / cut+verified / deliberately left private are owned by nova_speaks_safety_cut.py,
    # unless an exposure was recorded after the cut was submitted (a newly confirmed image) -> hide again.
    cur.execute("""SELECT youtube_id FROM nova_speaks_hidden h WHERE result='OK'
                   OR (result IN ('CUT-PENDING','CUT-VERIFIED','LEFT-PRIVATE','CUT-FAILED','RESTORED-NOEXPOSURE','CUT-REVERTING','RESTORED-SHOTSCAN')
                       AND NOT EXISTS (SELECT 1 FROM image_video_exposure e WHERE e.youtube_id=h.youtube_id
                                       AND e.ts > greatest(h.cut_submitted_at, h.restored_at, h.hidden_at)  -- verification covers exposures that existed when it ran))""")
    done = {r[0] for r in cur.fetchall()}
    todo = [v for v in dict.fromkeys(vids) if v not in done]
    if not todo or DRY:
        return 0
    n = 0
    for i in range(0, len(todo), 25):
        chunk = todo[i:i + 25]
        r = subprocess.run([PY, UPL, *[f"--private-only={v}" for v in chunk]], capture_output=True, text=True, timeout=900,
                           cwd="/Volumes/nas/nova-fs/scripts")
        res = dict(l.split(" ", 1)[::-1] for l in r.stdout.splitlines() if l.startswith(("OK ", "FAIL ")))
        if not res:
            print("private-only failed:", r.stderr[-400:], flush=True)
        for v in chunk:
            info = reason_by_vid[v]
            st = res.get(v, "ERR")
            cur.execute("""INSERT INTO nova_speaks_hidden (youtube_id, slug, reason, image_paths, image_ids, categories, hidden_at, result)
                           VALUES (%s,%s,%s,%s,%s,%s,CASE WHEN %s='OK' THEN now() END,%s)
                           ON CONFLICT (youtube_id) DO UPDATE SET reason=EXCLUDED.reason, image_paths=EXCLUDED.image_paths,
                             image_ids=EXCLUDED.image_ids, categories=EXCLUDED.categories,
                             hidden_at=COALESCE(nova_speaks_hidden.hidden_at, EXCLUDED.hidden_at), result=EXCLUDED.result""",
                        (v, info["slug"], "; ".join(sorted(info["reasons"]))[:2000], sorted(info["paths"]), sorted(info["ids"]),
                         sorted(info["cats"]), st, st))
            n += st == "OK"
    return n


def contain(cur, path, cat, sev, note, source):
    pool_rel, jp, sat_slug = related(path)
    actions = []
    targets = []
    if pool_rel:
        cur.execute("UPDATE satire_images SET active=false, inactive_reason=%s WHERE path=%s AND (active OR inactive_reason IS NULL)",
                    (f"safety {sev}/{cat}: {note}"[:500], pool_rel))
        if cur.rowcount: actions.append("pool_deactivated")
        targets.append(NAS / pool_rel)
    if jp is not None:
        targets.append(jp)
    qpath = None
    for t in targets:
        if t.exists():
            qp = quarantine(t); qpath = qpath or str(qp)
            if sha(t) == sha(qp) and not DRY:
                placeholder(t); actions.append(f"overwritten:{t}")
    if sat_slug:
        url = f"/images/satire/{sat_slug}.webp"
        for a in articles_referencing(url):
            if strip_satire_block(a, url): actions.append(f"block_removed:{a}")
    cur.execute("""INSERT INTO image_contained (path, kind, category, severity, note, source, quarantine_path, actions)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (path) DO UPDATE SET actions = image_contained.actions || EXCLUDED.actions""",
                (str(path), "pool" if pool_rel else "journal", cat, sev, note, source, qpath, actions))
    vids = videos_for(cur, pool_rel, jp)
    for slug, yid, link, ids in vids:
        cur.execute("""INSERT INTO image_video_exposure (image_path, slug, youtube_id, link, confidence, category, severity)
                       VALUES (%s,%s,%s,%s,'direct',%s,%s) ON CONFLICT (image_path, slug) DO NOTHING""",
                    (str(path), slug, yid, link, cat, sev))
    return actions, vids


def run_once():
    c = db(); cur = c.cursor(); cur.execute(DDL)
    cur.execute("""SELECT path, category, severity, coalesce(note,''), 'review:'||coalesce(reviewer,'') FROM image_safety_review WHERE NOT actioned
                   UNION ALL
                   SELECT path, coalesce(reason,'vision'), 'medium', coalesce(reason,''), 'vision+claude' FROM image_safety_audit
                   WHERE final_decision='bad' AND coalesce(contained,'')=''""")
    rows = sorted(cur.fetchall(), key=lambda r: (SEV.get(r[2], 3), r[1] not in ("antisemitic", "racist", "ethnic-caricature")))
    if not rows:
        return 0, 0
    reason_by_vid, changed = {}, 0
    for path, cat, sev, note, source in rows:
        try:
            actions, vids = contain(cur, path, cat, sev, note, source)
        except Exception as e:
            print(f"ERR {path}: {e}", flush=True); continue
        changed += any(a.startswith(("overwritten", "block_removed")) and str(J) in a for a in actions)
        for slug, yid, link, ids in vids:
            if not yid: continue
            d = reason_by_vid.setdefault(yid, {"slug": slug, "reasons": set(), "paths": set(), "ids": set(), "cats": set(), "sev": 9})
            d["reasons"].add(f"{sev}/{cat} via {link}: {Path(path).name}"); d["paths"].add(str(path)); d["ids"].update(ids or []); d["cats"].add(cat)
            d["sev"] = min(d["sev"], SEV.get(sev, 3))
        print(f"contained {sev}/{cat} {path} -> {len(actions)} actions, {len(vids)} videos", flush=True)
    maxsev = 2 if "--hide-low" in sys.argv else 1      # lows: Claude eyeballs before hiding videos
    held = [v for v in reason_by_vid if reason_by_vid[v]["sev"] > maxsev]
    if held: print(f"{len(held)} videos held (low severity only) pending Claude review", flush=True)
    order = sorted([v for v in reason_by_vid if reason_by_vid[v]["sev"] <= maxsev], key=lambda v: reason_by_vid[v]["sev"])
    hidden = hide(cur, order, reason_by_vid)
    if changed and not DRY:
        sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))
        import nova_journal as nj
        st = nj.git_push("meta", f"content safety: replace {changed} unsafe image(s) in place (URLs unchanged)")
        print("git_push:", st, flush=True)
    if not DRY:
        for path, cat, sev, note, source in rows:
            if source.startswith("review:"):
                cur.execute("UPDATE image_safety_review SET actioned=true WHERE path=%s", (path,))
            else:
                cur.execute("UPDATE image_safety_audit SET contained='done' WHERE path=%s AND final_decision='bad'", (path,))
    print(f"batch: {len(rows)} images, {hidden} videos hidden now", flush=True)
    return len(rows), hidden


if __name__ == "__main__":
    loop = "--loop" in sys.argv
    while True:
        run_once()
        if not loop: break
        time.sleep(120)
