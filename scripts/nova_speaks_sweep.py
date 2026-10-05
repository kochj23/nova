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

# ── 1. new live posts ───────────────────────────────────────────────────────────────────
def scan_new(cur):
    n = 0
    for p in (JOURNAL / "content").glob("*/*.md"):
        if p.name.startswith("_"): continue
        fm = frontmatter(p.read_text(errors="ignore"))
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
        cur.execute("INSERT INTO nova_speaks_renders (slug, section, article_path, url, title) VALUES (%s,%s,%s,%s,%s)", (p.stem, section, str(p), url, title))
        n += 1
    return n

# ── 2. reap finished renders ────────────────────────────────────────────────────────────
def reap(cur, hosts):
    cur.execute("SELECT slug, host, pid, log_path, title FROM nova_speaks_renders WHERE status='rendering'")
    for slug, hname, pid, lp, title in cur.fetchall():
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
            cur.execute("UPDATE nova_speaks_renders SET status='done', mp4_path=%s, finished_at=now(), note=%s WHERE slug=%s", (mp4, f"{hname}: {info}", slug))
            cur.execute("UPDATE nova_speaks_hosts SET fails=0 WHERE host=%s", (hname,))
            yt = upload(slug)
            post_approval(f"🎬 *Nova Speaks — ready for your approval, Little Mister:* {title}\n`{mp4}`\n{info} · rendered on {hname} · voice Gracie Wise\n"
                          + (f"Published to YouTube: https://youtu.be/{yt} (edit: https://studio.youtube.com/video/{yt}/edit)" if yt
                             else "YouTube upload failed (cookies stale?) — sign into YouTube in Safari and I'll retry next sweep."))
            log(f"done: {slug} on {hname}")
        else:
            tail = txt[-400:].replace("\n", " ")
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

# ── 3. dispatch ─────────────────────────────────────────────────────────────────────────
def idle(h):
    rc, _ = sh(h, "pgrep -f '[n]ova_speaks.py' >/dev/null"); return rc != 0

def dispatch(cur, hosts):
    # fastest hosts first, so the Studio takes the next job when it is free
    for h in sorted(hosts.values(), key=lambda x: -(x["speed"] or 0)):
        if not h["enabled"]: continue
        cur.execute("SELECT slug, section, article_path, url FROM nova_speaks_renders WHERE status='queued' ORDER BY queued_at LIMIT 1")
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
            if (sec_dir / f"{slug}.webp").exists() and (sec_dir / f"{slug}.webp") not in covers: covers.append(sec_dir / f"{slug}.webp")
            fm = re.search(r'^\s*image:\s*"?(/images/[^"\s]+)', pathlib.Path(art).read_text(), re.M)   # frontmatter cover when its name != slug
            if fm and (JOURNAL / "static" / fm.group(1).lstrip("/")).exists(): covers.append(JOURNAL / "static" / fm.group(1).lstrip("/"))
            for cv in covers: copy_to(h, str(cv), f"{h['journal_dir']}/static/images/{section}/{cv.name}")
            copy_to(h, str(SCRIPTS / "nova_speaks.py"), f"{h['scripts_dir']}/nova_speaks.py")   # keep the renderer current
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

def main():
    c = psycopg2.connect(DSN); c.autocommit = True; cur = c.cursor()
    cur.execute(DDL)
    cur.execute("""INSERT INTO nova_speaks_hosts (host, ssh, python, scripts_dir, tts_home, journal_dir, out_dir, out_is_nas, env, speed, note)
                   VALUES (%s, NULL, %s, %s, '/Volumes/Data/AI/tts', %s, %s, true, 'PATH=/opt/homebrew/bin:$PATH', 1.5, 'Mac Studio M4 Max, MPS')
                   ON CONFLICT (host) DO NOTHING""", (LOCAL, sys.executable, str(SCRIPTS), str(JOURNAL), REVIEW))
    cur.execute("SELECT host, ssh, python, scripts_dir, tts_home, journal_dir, out_dir, out_is_nas, env, speed, enabled FROM nova_speaks_hosts")
    cols = ["host", "ssh", "python", "scripts_dir", "tts_home", "journal_dir", "out_dir", "out_is_nas", "env", "speed", "enabled"]
    hosts = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
    n = scan_new(cur)
    if n: log(f"queued {n} new article(s)")
    reap(cur, hosts)
    # retry one YouTube upload that failed earlier (stale cookies); a row that keeps failing ages out after 2 days.
    cur.execute("ALTER TABLE nova_speaks_renders ADD COLUMN IF NOT EXISTS youtube_id text, ADD COLUMN IF NOT EXISTS youtube_uploaded_at timestamptz")
    cur.execute("SELECT slug, title FROM nova_speaks_renders WHERE status='done' AND youtube_id IS NULL "
                "AND finished_at > now() - interval '2 days' ORDER BY finished_at LIMIT 1")
    for slug, title in cur.fetchall():
        yt = upload(slug)
        if yt: post_approval(f"🎬 YouTube upload retry succeeded for *{title}*: https://youtu.be/{yt}")
    dispatch(cur, hosts)
    cur.execute("SELECT status, count(*) FROM nova_speaks_renders GROUP BY 1 ORDER BY 1")
    log("state: " + ", ".join(f"{s}={k}" for s, k in cur.fetchall()) + f" · hosts: {', '.join(h for h, v in hosts.items() if v['enabled'])}")

if __name__ == "__main__":
    main()
