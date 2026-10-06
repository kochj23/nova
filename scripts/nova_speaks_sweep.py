#!/usr/bin/env python3
"""nova_speaks_sweep.py — every article that goes LIVE on Nova's journal gets a "Nova Speaks" video for Jordan's approval.

Jordan, 2026-10-03: "every time an article of any type is written to her blog, create the video version for my approval"
                    "...after it has successfully been written to the blog and it is live"
                    "Let's give the cluster something to talk about (as far as load)"

Runs every 10 min from the Studio scheduler. State in nova_ops (no flat files):
  nova_speaks_renders  one row per article (queued -> rendering -> done|failed; 'handfed' rows are ignored)
  nova_speaks_hosts    render pool: the Studio (MPS) plus any node with the venv + model; one render per host at a time
  1. new journal posts (content/<section>/*.md, not draft, dated after the rule) whose URL answers 200 -> 'queued'
  2. reap: every 'rendering' row whose pid is gone -> log says DONE -> 'done' (+ scp the mp4 to the review dir if the host
     has no NAS) and post to #nova-claude for approval; otherwise 'failed'
  3. dispatch: for each enabled host that is idle, ship the oldest queued article (+ cover) and start nova_speaks.py detached
ponytail: ssh for everything remote; hosts are rows, not config files. A host that fails 3 renders in a row disables itself.
"""
import os, re, sys, json, subprocess, time, pathlib, shlex
import psycopg2

JOURNAL = pathlib.Path.home() / "nova-journal"
# Articles that exist only on origin/main (this clone is dirty/diverged, so it was not fast-forwarded) are
# materialized here, mirroring the repo layout, so dispatch/podcast_index have a real file to read.
ORIGIN_CACHE = pathlib.Path.home() / ".cache" / "nova-speaks" / "origin"
PUSH_LOCK_KEY = 47110815   # == nova_journal._PUSH_LOCK_KEY: the fleet-wide journal-push advisory lock
SCRIPTS = pathlib.Path(__file__).resolve().parent
LOCAL = os.uname().nodename
REVIEW = "/Volumes/nas/nova-fs/videos/review"            # Studio's view of the review dir (UNAS)
RULE_SINCE = "2026-10-03 19:30:00-07"                     # posts older than the rule are not queued
DSN = os.environ.get("NOVA_OPS_DSN", "dbname=nova_ops user=kochj host=pg-primary.digitalnoise.net port=5432")
SITE = "https://nova.digitalnoise.net"
DDL = """
CREATE TABLE IF NOT EXISTS nova_speaks_renders (
  slug text PRIMARY KEY, section text NOT NULL, article_path text NOT NULL, url text NOT NULL, title text,
  status text NOT NULL DEFAULT 'queued', host text, pid int, log_path text, mp4_path text,
  queued_at timestamptz DEFAULT now(), started_at timestamptz, finished_at timestamptz, note text);
ALTER TABLE nova_speaks_renders ADD COLUMN IF NOT EXISTS youtube_id text, ADD COLUMN IF NOT EXISTS youtube_uploaded_at timestamptz,
  ADD COLUMN IF NOT EXISTS quality jsonb, ADD COLUMN IF NOT EXISTS old_youtube_id text, ADD COLUMN IF NOT EXISTS old_youtube_retired boolean;
CREATE TABLE IF NOT EXISTS nova_speaks_hosts (
  host text PRIMARY KEY, ssh text, python text NOT NULL, scripts_dir text NOT NULL, tts_home text NOT NULL,
  journal_dir text NOT NULL, out_dir text NOT NULL, out_is_nas boolean NOT NULL DEFAULT false,
  env text DEFAULT '', speed numeric, enabled boolean NOT NULL DEFAULT true, fails int NOT NULL DEFAULT 0, note text);
"""

def log(m): print(f"[speaks-sweep {time.strftime('%H:%M:%S')}] {m}", flush=True)

def sh(host, cmd, timeout=60):
    """run cmd locally (host row with ssh NULL) or over ssh; returns (rc, stdout)"""
    if not host["ssh"]:
        r = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=timeout)
    else:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", host["ssh"], cmd], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout

def copy_to(host, src, dst):
    if not host["ssh"]:
        if os.path.abspath(src) != os.path.abspath(dst):
            pathlib.Path(dst).parent.mkdir(parents=True, exist_ok=True); subprocess.run(["cp", src, dst], check=True)
    else:
        sh(host, f"mkdir -p {shlex.quote(os.path.dirname(dst))}")
        subprocess.run(["scp", "-q", src, f"{host['ssh']}:{dst}"], check=True, timeout=300)

def post_approval(text):
    try:
        import urllib.request
        tok = subprocess.run(["security", "find-generic-password", "-s", "nova-slack-bot-token", "-w"], capture_output=True, text=True).stdout.strip()
        H = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
        r = json.loads(urllib.request.urlopen(urllib.request.Request("https://slack.com/api/conversations.list?types=public_channel&limit=500", headers=H)).read())
        ch = next(c["id"] for c in r["channels"] if c["name"] == "nova-claude")
        urllib.request.urlopen(urllib.request.Request("https://slack.com/api/chat.postMessage", data=json.dumps({"channel": ch, "text": text}).encode(), headers=H)).read()
    except Exception as e:
        log(f"slack post failed: {e}")

def is_live(url):
    import urllib.request
    try: return urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=15).status == 200
    except Exception: return False

def frontmatter(md):
    m = re.match(r"^---\n(.*?)\n---", md, re.S); return m.group(1) if m else ""

# ── 0. bring the clone up to date ───────────────────────────────────────────────────────
def _jgit(*args, timeout=60):
    return subprocess.run(["git", *args], cwd=JOURNAL, capture_output=True, text=True, timeout=timeout)

def sync_journal(cur):
    """Articles are published from nova-core too; nothing else reliably pulls this clone, so they never got queued.
    git fetch origin main, then fast-forward ONLY if the clone is clean, on main, and strictly behind — under the
    fleet-wide journal-push lock (skip if another writer holds it). Never rebase/reset someone's working copy.
    Returns "origin/main" when origin has commits the working copy lacks (scan its tree too), else None."""
    try:
        f = _jgit("fetch", "-q", "origin", "main", timeout=120)
        if f.returncode != 0:
            log(f"journal fetch failed: {' '.join(f.stderr.split())[:160]}"); return None
        g = JOURNAL / ".git"
        wedged = any((g / x).exists() for x in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"))
        branch = _jgit("symbolic-ref", "-q", "--short", "HEAD").stdout.strip()
        dirty = bool(_jgit("status", "--porcelain").stdout.strip())
        behind = int(_jgit("rev-list", "--count", "HEAD..origin/main").stdout.strip() or 0)
        ahead = int(_jgit("rev-list", "--count", "origin/main..HEAD").stdout.strip() or 0)
        if not behind: return None
        if branch == "main" and not wedged and not dirty and not ahead:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (PUSH_LOCK_KEY,))
            if cur.fetchone()[0]:
                try:
                    m = _jgit("merge", "--ff-only", "-q", "origin/main")
                finally:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (PUSH_LOCK_KEY,))
                if m.returncode == 0:
                    log(f"journal fast-forwarded {behind} commit(s)"); return None
                log(f"journal ff-merge failed: {' '.join(m.stderr.split())[:160]}")
            else:
                log("journal push lock held by a writer — not fast-forwarding this sweep")
        else:
            why = ", ".join(w for w, on in (("dirty", dirty), (f"{ahead} ahead", ahead), ("wedged", wedged),
                                            (f"on {branch or 'detached HEAD'}", branch != "main")) if on)
            log(f"journal clone {why}, {behind} behind — left untouched; scanning origin/main too")
        return "origin/main"
    except Exception as e:
        log(f"journal sync failed: {e}"); return None

def _origin_articles(ref):
    """(relpath, text) for every content/<section>/<post>.md in ref's tree that the working copy doesn't have."""
    ls = _jgit("ls-tree", "-r", "--name-only", ref, "--", "content")
    for rel in ls.stdout.splitlines():
        parts = rel.split("/")
        if len(parts) != 3 or not rel.endswith(".md") or (JOURNAL / rel).exists(): continue
        show = _jgit("show", f"{ref}:{rel}")
        if show.returncode == 0: yield rel, show.stdout

def _materialize(ref, rel, text):
    """Write an origin-only article (and its cover, if any) under ORIGIN_CACHE; return the article path."""
    dst = ORIGIN_CACHE / rel; dst.parent.mkdir(parents=True, exist_ok=True); dst.write_text(text)
    section, stem = rel.split("/")[1], pathlib.Path(rel).stem
    fm = re.search(r'^\s*image:\s*"?(/images/[^"\s]+)', text, re.M)
    for img in {f"static/images/{section}/{stem}.webp", *( [f"static{fm.group(1)}"] if fm else [] )}:
        r = subprocess.run(["git", "show", f"{ref}:{img}"], cwd=JOURNAL, capture_output=True, timeout=60)
        if r.returncode == 0:
            (ORIGIN_CACHE / img).parent.mkdir(parents=True, exist_ok=True); (ORIGIN_CACHE / img).write_bytes(r.stdout)
    return dst

# ── 1. new live posts ───────────────────────────────────────────────────────────────────
def scan_new(cur, origin_ref=None):
    n = 0
    cands = [(p, None) for p in (JOURNAL / "content").glob("*/*.md")]
    if origin_ref:
        cands += [(JOURNAL / rel, (rel, text)) for rel, text in _origin_articles(origin_ref)]
    for p, remote in cands:
        if p.name.startswith("_"): continue
        fm = frontmatter(remote[1] if remote else p.read_text(errors="ignore"))
        if re.search(r"^draft:\s*true", fm, re.M): continue
        d = re.search(r"^date:\s*(\S+)", fm, re.M)
        if not d: continue
        cur.execute("SELECT %s::timestamptz >= %s::timestamptz", (d.group(1), RULE_SINCE))
        if not cur.fetchone()[0]: continue
        cur.execute("SELECT 1 FROM nova_speaks_renders WHERE slug=%s", (p.stem,))
        if cur.fetchone(): continue
        section = p.parent.name; url = f"{SITE}/{section}/{p.stem}/"
        if not is_live(url): log(f"not live yet: {p.stem}"); continue
        t = re.search(r'^title:\s*"?(.+?)"?\s*$', fm, re.M)
        title = re.sub(r"^[^\w]+", "", t.group(1).strip('" ')) if t else p.stem
        if remote: p = _materialize(origin_ref, *remote)
        cur.execute("INSERT INTO nova_speaks_renders (slug, section, article_path, url, title) VALUES (%s,%s,%s,%s,%s)", (p.stem, section, str(p), url, title))
        n += 1
    return n

# ── 2. reap finished renders ────────────────────────────────────────────────────────────
def reap(cur, hosts):
    cur.execute("SELECT slug, host, pid, log_path, title, old_youtube_id, note FROM nova_speaks_renders WHERE status='rendering'")
    for slug, hname, pid, lp, title, old_yt, prev_note in cur.fetchall():
        h = hosts.get(hname)
        if not h: continue
        rc, _ = sh(h, f"kill -0 {pid} 2>/dev/null")
        if rc == 0: continue
        _, txt = sh(h, f"cat {shlex.quote(lp)} 2>/dev/null")
        m = re.search(r"DONE (\S+\.mp4) \(([^)]*)\)", txt)
        if m:
            mp4, info = m.group(1), m.group(2)
            if not h["out_is_nas"]:                                      # pull it home
                dst = f"{REVIEW}/{os.path.basename(mp4)}"
                try:
                    subprocess.run(["scp", "-q", f"{h['ssh']}:{mp4}", dst], check=True, timeout=1800); mp4 = dst
                except Exception as e:
                    log(f"scp back failed for {slug}: {e}")
            elif mp4.startswith(h["out_dir"]):
                mp4 = REVIEW + mp4[len(h["out_dir"]):]
            q = re.search(r"^QUALITY (\{.*\})\s*$", txt, re.M)
            qj = q.group(1) if q else None
            qs = ""
            if qj:
                try:
                    d = json.loads(qj); qs = f" · back-check {d.get('parts')} parts, {d.get('retries')} retries, worst WER {d.get('worst_wer')}"
                except Exception: qj = None
            note = f"{hname}: {info}{qs}" + (f" | {prev_note}" if old_yt and prev_note and "rerender" in prev_note else "")
            cur.execute("UPDATE nova_speaks_renders SET status='done', mp4_path=%s, finished_at=now(), note=%s, quality=%s WHERE slug=%s", (mp4, note, qj, slug))
            cur.execute("UPDATE nova_speaks_hosts SET fails=0 WHERE host=%s", (hname,))
            if old_yt:
                log(f"re-render done: {slug} on {hname} — replacement upload is throttled (one per sweep)")
                continue
            yt = upload(slug)
            post_approval(f"🎬 *Nova Speaks — ready for your approval, Little Mister:* {title}\n`{mp4}`\n{info} · rendered on {hname} · voice Gracie Wise\n"
                          + (f"Published to YouTube: https://youtu.be/{yt} (edit: https://studio.youtube.com/video/{yt}/edit)" if yt
                             else "YouTube upload failed (cookies stale?) — sign into YouTube in Safari and I'll retry next sweep."))
            log(f"done: {slug} on {hname}")
        else:
            tail = txt[-400:].replace("\n", " ")
            if old_yt:     # a failed re-render keeps the published old video; back to done, flagged
                cur.execute("UPDATE nova_speaks_renders SET status='done', old_youtube_id=NULL, note=%s WHERE slug=%s", (f"{prev_note} | re-render FAILED on {hname}: {tail[-200:]}", slug))
            else:
                cur.execute("UPDATE nova_speaks_renders SET status='failed', finished_at=now(), note=%s WHERE slug=%s", (f"{hname}: {tail}", slug))
            cur.execute("UPDATE nova_speaks_hosts SET fails=fails+1, enabled=(fails+1<3) WHERE host=%s", (hname,))
            post_approval(f"⚠️ Nova Speaks render FAILED for *{title}* on {hname} — log `{lp}`")
            log(f"failed: {slug} on {hname}")

def upload(slug):
    """Private YouTube upload in Jordan's title format (nova_speaks_upload.py). Returns the video id or None."""
    try:
        r = subprocess.run([str(SCRIPTS / "nova_speaks_upload.py"), "--slug", slug], capture_output=True, text=True, timeout=1800)
        vid = (r.stdout.strip().splitlines() or [""])[-1]
        if r.returncode == 0 and re.fullmatch(r"[\w-]{11}", vid): return vid
        log(f"upload failed for {slug}: {(r.stderr or r.stdout)[-200:].strip()}")
    except Exception as e:
        log(f"upload failed for {slug}: {e}")
    return None

def replace_one(cur):
    """One quality re-render per sweep goes up (YouTube daily limits + the sweep's 5-minute budget): the uploader posts
    the new version under the same title into the playlist and sets the old one PRIVATE + out of the playlist."""
    cur.execute("SELECT slug, title, old_youtube_id FROM nova_speaks_renders WHERE status='done' AND old_youtube_id IS NOT NULL "
                "AND youtube_id = old_youtube_id ORDER BY finished_at LIMIT 1")
    for slug, title, old in cur.fetchall():
        yt = upload(slug)
        if not yt: continue
        cur.execute("SELECT old_youtube_retired FROM nova_speaks_renders WHERE slug=%s", (slug,))
        retired = (cur.fetchone() or [None])[0]
        post_approval(f"🔁 Nova Speaks re-render (narration quality) replaced *{title}*: https://youtu.be/{yt} — old {old} "
                      + ("set private + removed from the playlist." if retired else "could NOT be hidden automatically; please set it private in Studio."))


# ── 3. dispatch ─────────────────────────────────────────────────────────────────────────
def idle(h):
    rc, _ = sh(h, "pgrep -f '[n]ova_speaks.py' >/dev/null"); return rc != 0

def dispatch(cur, hosts):
    # fastest hosts first, so the Studio takes the next job when it is free
    for h in sorted(hosts.values(), key=lambda x: -(x["speed"] or 0)):
        if not h["enabled"]: continue
            # new articles first; quality re-renders (old_youtube_id set) only take otherwise idle hosts
        cur.execute("SELECT slug, section, article_path, url FROM nova_speaks_renders WHERE status='queued' ORDER BY (old_youtube_id IS NOT NULL), queued_at LIMIT 1")
        row = cur.fetchone()
        if not row: return
        try:
            if not idle(h): continue
        except Exception as e:
            log(f"{h['host']} unreachable: {e}"); continue
        slug, section, art, url = row
        try:
            rart = f"{h['journal_dir']}/content/{section}/{slug}.md"
            copy_to(h, art, rart)
            # the article's cover plus a few recent covers from its section, so the Ken Burns rotation has variety on every host
            sec_dir = JOURNAL / "static/images" / section
            covers = sorted(sec_dir.glob("*.webp"), key=lambda f: f.stat().st_mtime)[-6:]
            fm = re.search(r'^\s*image:\s*"?(/images/[^"\s]+)', pathlib.Path(art).read_text(), re.M)   # frontmatter cover when its name != slug
            for root in (JOURNAL, ORIGIN_CACHE):    # ORIGIN_CACHE: covers of articles taken from origin/main's tree
                own = [root / "static/images" / section / f"{slug}.webp"] + ([root / "static" / fm.group(1).lstrip("/")] if fm else [])
                covers += [c for c in own if c.exists() and c.name not in {x.name for x in covers}]
            for cv in covers: copy_to(h, str(cv), f"{h['journal_dir']}/static/images/{section}/{cv.name}")
            for f in ("nova_speaks.py", "nova_speaks_narration.py"):              # keep the renderer + narration stage current
                copy_to(h, str(SCRIPTS / f), f"{h['scripts_dir']}/{f}")
            lp = f"{h['tts_home']}/logs/{slug}.log"
            inner = (f"cd {shlex.quote(h['scripts_dir'])} && exec env TTS_HOME={shlex.quote(h['tts_home'])} COQUI_TOS_AGREED=1 "
                     f"NOVA_SPEAKS_OUT={shlex.quote(h['out_dir'])} {h['env'] or ''} {shlex.quote(h['python'])} nova_speaks.py "
                     f"--article {shlex.quote(rart)} --url {shlex.quote(url)}")
            # fully detached: nothing in the background job keeps the ssh channel open, so this returns at once with the pid
            cmd = (f"mkdir -p {shlex.quote(os.path.dirname(lp))}; nohup bash -c {shlex.quote(inner)} > {shlex.quote(lp)} 2>&1 < /dev/null & echo $!")
            rc, out = sh(h, cmd, timeout=120)
            pid = int(out.strip().splitlines()[-1])
            cur.execute("UPDATE nova_speaks_renders SET status='rendering', host=%s, pid=%s, log_path=%s, started_at=now() WHERE slug=%s", (h["host"], pid, lp, slug))
            log(f"started: {slug} on {h['host']} pid {pid}")
        except Exception as e:
            log(f"dispatch to {h['host']} failed: {e}")
            cur.execute("UPDATE nova_speaks_hosts SET fails=fails+1, enabled=(fails+1<3) WHERE host=%s", (h["host"],))


# ── 4. podcast index for the Start Here page ───────────────────────────────────────────
def _fm(md, key):
    m = re.search(rf'^{key}:\s*(.+?)\s*$', md, re.M); return m.group(1).strip().strip('"') if m else ""

def podcast_index(cur):
    """data/nova_speaks.json = every published episode, newest first (cover, title, section, date, youtube id).
    Rewritten only when it differs from the DB, then pushed through nova_journal.git_push (the fleet-wide lock)."""
    cur.execute("SELECT slug, section, article_path, url, youtube_id FROM nova_speaks_renders WHERE youtube_id ~ '^[A-Za-z0-9_-]{11}$'")
    eps = []
    for slug, section, art, url, yid in cur.fetchall():
        try: md = pathlib.Path(art).read_text()
        except Exception: md = ""
        title = re.sub(r"[*_`<>]", "", _fm(md, "title")); title = re.sub(r"^[^\w\"']+", "", title).strip() or slug
        cover = re.search(r'^\s*image:\s*"?(/images/[^"\s]+)', md, re.M)
        cover = cover.group(1) if cover and (JOURNAL / "static" / cover.group(1).lstrip("/")).exists() else f"https://i.ytimg.com/vi/{yid}/hqdefault.jpg"
        eps.append({"slug": slug, "title": title, "section": section.replace("-", " ").title(), "date": (_fm(md, "date") or "")[:10],
                    "cover": cover, "url": url, "youtube_id": yid})
    eps.sort(key=lambda e: (e["date"], e["slug"]), reverse=True)
    out = JOURNAL / "data" / "nova_speaks.json"; out.parent.mkdir(exist_ok=True)
    new = json.dumps(eps, indent=1, ensure_ascii=False) + "\n"
    if out.exists() and out.read_text() == new: return 0
    out.write_text(new)
    try:
        import nova_journal; nova_journal.git_push("start-here", f"Nova Speaks index: {len(eps)} episodes")
    except Exception as e:
        log(f"podcast index push failed: {e}")
    log(f"podcast index: {len(eps)} episodes")
    return len(eps)

def main():
    c = psycopg2.connect(DSN); c.autocommit = True; cur = c.cursor()
    cur.execute(DDL)
    cur.execute("""INSERT INTO nova_speaks_hosts (host, ssh, python, scripts_dir, tts_home, journal_dir, out_dir, out_is_nas, env, speed, note)
                   VALUES (%s, NULL, %s, %s, '/Volumes/Data/AI/tts', %s, %s, true, 'PATH=/opt/homebrew/bin:$PATH', 1.5, 'Mac Studio M4 Max, MPS')
                   ON CONFLICT (host) DO NOTHING""", (LOCAL, sys.executable, str(SCRIPTS), str(JOURNAL), REVIEW))
    cur.execute("SELECT host, ssh, python, scripts_dir, tts_home, journal_dir, out_dir, out_is_nas, env, speed, enabled FROM nova_speaks_hosts")
    cols = ["host", "ssh", "python", "scripts_dir", "tts_home", "journal_dir", "out_dir", "out_is_nas", "env", "speed", "enabled"]
    hosts = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
    n = scan_new(cur, sync_journal(cur))
    if n: log(f"queued {n} new article(s)")
    reap(cur, hosts)
    # retry one YouTube upload that failed earlier (stale cookies); a row that keeps failing ages out after 2 days.
    cur.execute("SELECT slug, title FROM nova_speaks_renders WHERE status='done' AND youtube_id IS NULL "
                "AND finished_at > now() - interval '2 days' ORDER BY finished_at LIMIT 1")
    for slug, title in cur.fetchall():
        yt = upload(slug)
        if yt: post_approval(f"🎬 YouTube upload retry succeeded for *{title}*: https://youtu.be/{yt}")
    replace_one(cur)
    dispatch(cur, hosts)
    podcast_index(cur)
    cur.execute("SELECT status, count(*) FROM nova_speaks_renders GROUP BY 1 ORDER BY 1")
    log("state: " + ", ".join(f"{s}={k}" for s, k in cur.fetchall()) + f" · hosts: {', '.join(h for h, v in hosts.items() if v['enabled'])}")

if __name__ == "__main__":
    if "--index" in sys.argv:
        c = psycopg2.connect(DSN); c.autocommit = True; print(podcast_index(c.cursor()))
    else:
        main()
