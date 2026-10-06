#!/usr/bin/env /Volumes/Data/AI/youtube-up/venv/bin/python
"""nova_speaks_upload.py — upload a finished Nova Speaks render to Jordan's YouTube channel, PUBLIC (his call,
2026-10-05: "Upload them as public"), in his title format, into the "Nova Speaks" playlist.

Uses youtube-up with a YouTube-only cookie jar exported from Safari on each run, so no
Google Cloud Console, no OAuth app, no API quota (Jordan, 2026-10-05: "Oh God, that never works").

  nova_speaks_upload.py --slug 2026-10-05-heat-dome-...          # upload one done render
  nova_speaks_upload.py --slug ... --dry-run                     # print the metadata only
  nova_speaks_upload.py --check                                  # is the cookie session still logged in?
  nova_speaks_upload.py --retire <video_id>                      # set PRIVATE + take it out of the Nova Speaks playlist
Re-renders (2026-10-06): a row whose youtube_id still equals old_youtube_id is a replacement — the new render is uploaded
with the same title into the playlist, youtube_id becomes the new id, and the old video is set PRIVATE and removed from
the playlist (never deleted; reversible in Studio). If that retire call fails, old_youtube_retired stays false and the
note says so — Jordan hides it by hand.
ponytail: cookie sessions go stale; the sweep posts to Slack when --check fails. Jordan signs back into YouTube in Safari.
"""
import argparse, http.cookiejar, os, re, sys
from datetime import date
from pathlib import Path
import psycopg2

DSN = "host=localhost dbname=nova_ops user=kochj"
COOKIES = Path.home() / ".openclaw/cache/yt_cookies_youtube.txt"   # youtube/google cookies only, from Safari
JOURNAL = Path.home() / "nova-journal"
WHITELIST = {"SAPISID", "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID", "__Secure-1PSIDTS", "__Secure-3PSIDTS", "LOGIN_INFO"}
PLAYLIST = "PLY76cVeV8pAY"                      # "Nova Speaks" on youtube.com/@Jord
BOILERPLATE = ("Nova's Podcast - My AI advisor's thoughts about technology, security, Burbank, and random thoughts.\n\n"
               "Journal here - https://nova.digitalnoise.net/start-here/\n"
               "Code and Capabilities here - https://github.com/kochj23/nova")


def log(m): print(f"[speaks-upload] {m}", flush=True)


def fm(md, key):
    m = re.search(rf'^{key}:\s*(.+?)\s*$', md, re.M); return m.group(1).strip().strip('"') if m else ""


def build(slug, article_path, url):
    md = Path(article_path).read_text()
    title = re.sub(r"[*_`]", "", fm(md, "title")); title = re.sub(r"^[^\w\"']+", "", title).strip()
    d = date.fromisoformat(fm(md, "date")[:10])
    section = Path(article_path).parent.name                                  # journal category (operations, local, essays, ...)
    prefix = f"AI: Nova Speaks {d.month}/{d.day}/{d.year % 100} - {section.replace('-', ' ').title()} - "
    room = 100 - len(prefix)
    if len(title) > room: title = title[:room].rsplit(" ", 1)[0].rstrip(" ,;:-")
    tags = re.findall(r'"([^"]+)"', fm(md, "tags")) or []
    desc = fm(md, "description")
    description = (f"{desc}\n\n" if desc else "") + f"Article: {url}\n\n{BOILERPLATE}\n\nNarration is an AI voice (XTTS, 'Gracie Wise'). Written by Nova."
    clean = lambda x: x.replace("<", "").replace(">", "")                    # YouTube rejects angled brackets anywhere
    return dict(title=clean(prefix + title), description=clean(description),
                tags=tuple(clean(t) for t in dict.fromkeys(["Nova", "AI", "Nova Speaks", section] + tags))[:30], recorded=d)


def refresh_cookies():
    """Export a YouTube-only cookie jar from Safari (the browser Jordan is logged into YouTube with).
    Needs a GUI/TCC context; when that is missing (bare launchd) the last good jar is reused."""
    import subprocess, tempfile
    tmp = tempfile.mktemp(suffix=".txt")
    r = subprocess.run(["/opt/homebrew/bin/yt-dlp", "--cookies-from-browser", "safari", "--cookies", tmp, "--skip-download",
                        "--print", "%(id)s", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"], capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not os.path.exists(tmp):
        log(f"safari cookie export failed, reusing {COOKIES.name}: {r.stderr[-120:].strip()}"); return
    rows = [l for l in open(tmp) if not l.startswith("#") and l.count("\t") >= 6]
    yt = [l for l in rows if l.split("\t")[0] in (".youtube.com", "youtube.com")]   # selenium step only accepts youtube.com cookies
    have = {l.split("\t")[5] for l in yt}
    # Safari keeps SAPISID & co. on .google.com only; Google shares those values with youtube.com, so re-domain the copies
    yt += [".youtube.com" + l[len(l.split("\t")[0]):] for l in rows
           if l.split("\t")[0] == ".google.com" and l.split("\t")[5] in WHITELIST and l.split("\t")[5] not in have]
    keep = ["# Netscape HTTP Cookie File\n"] + yt
    COOKIES.parent.mkdir(parents=True, exist_ok=True)
    COOKIES.write_text("".join(keep)); COOKIES.chmod(0o600); os.unlink(tmp)
    log(f"cookies refreshed from Safari ({len(keep)} lines)")


def session():
    from youtube_up import YTUploaderSession
    refresh_cookies()
    jar = http.cookiejar.MozillaCookieJar(str(COOKIES)); jar.load(ignore_discard=True, ignore_expires=True)
    return YTUploaderSession(jar)


def retire(s, vid):
    """Old version of a replaced video: PRIVATE and out of the playlist via Studio's metadata_update (the same call
    youtube-up makes after an upload). Never deletes. Returns True on UPDATE_SUCCESS."""
    from youtube_up.schema import APIContext, APIDelegationContext
    data = s._get_session_data()
    if not s._session_token: s._get_session_token()
    body = {"context": APIContext.from_session_data(data.channel_id, s._session_token, data.delegated_session_id).to_dict(),
            "delegationContext": APIDelegationContext(data.channel_id).to_dict(), "encryptedVideoId": vid,
            "privacyState": {"newPrivacy": "PRIVATE"},
            "addToPlaylist": {"addToPlaylistIds": [], "deleteFromPlaylistIds": [PLAYLIST]}}
    r = s._session.post("https://studio.youtube.com/youtubei/v1/video_manager/metadata_update",
                        params={"key": data.innertube_api_key, "alt": "json"}, json=body, timeout=60)
    r.raise_for_status()
    j = r.json()
    res = (j.get("overallResult") or {}).get("resultCode", "")
    log(f"retire {vid}: {res or str(j)[:200]}")
    return res == "UPDATE_SUCCESS"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug"); ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--check", action="store_true")
    ap.add_argument("--retire", metavar="VIDEO_ID", help="set an old video PRIVATE and remove it from the playlist")
    ap.add_argument("--privacy", default="PUBLIC", choices=["PRIVATE", "UNLISTED", "PUBLIC"])
    a = ap.parse_args()
    if a.check:
        ok = session().has_valid_cookies(); log(f"cookies valid: {ok}"); return 0 if ok else 1
    if a.retire:
        if not re.fullmatch(r"[\w-]{11}", a.retire): log("not a video id"); return 2
        return 0 if retire(session(), a.retire) else 1
    conn = psycopg2.connect(DSN); conn.autocommit = True; cur = conn.cursor()
    cur.execute("ALTER TABLE nova_speaks_renders ADD COLUMN IF NOT EXISTS youtube_id text, ADD COLUMN IF NOT EXISTS youtube_uploaded_at timestamptz, "
                "ADD COLUMN IF NOT EXISTS old_youtube_id text, ADD COLUMN IF NOT EXISTS old_youtube_retired boolean")
    cur.execute("SELECT article_path, url, mp4_path, youtube_id, old_youtube_id FROM nova_speaks_renders WHERE slug=%s AND status='done'", (a.slug,))
    row = cur.fetchone()
    if not row: log(f"no done render for {a.slug}"); return 1
    article, url, mp4, yid, old = row
    replacing = bool(old) and yid == old
    if yid and not replacing: log(f"already uploaded or in progress: {yid}"); return 0
    m = build(a.slug, article, url)
    log(f"title ({len(m['title'])}): {m['title']}"); log(f"tags: {m['tags']}"); log(f"file: {mp4}")
    if a.dry_run: print(m["description"]); return 0
    from youtube_up import Metadata, PrivacyEnum, CategoryEnum
    meta = Metadata(title=m["title"], description=m["description"], privacy=PrivacyEnum[a.privacy], tags=m["tags"],
                    playlist_ids=[PLAYLIST], category=CategoryEnum.SCIENCE_TECH, recorded_date=m["recorded"], made_for_kids=False)
    # claim the row so the sweep's retry and a backfill can't upload the same video twice (after Metadata
    # validation, so a rejected title never leaves the row stuck at 'uploading')
    if replacing:
        cur.execute("UPDATE nova_speaks_renders SET youtube_id='uploading' WHERE slug=%s AND youtube_id=%s", (a.slug, old))
    else:
        cur.execute("UPDATE nova_speaks_renders SET youtube_id='uploading' WHERE slug=%s AND youtube_id IS NULL", (a.slug,))
    if cur.rowcount != 1: log("claimed by another uploader"); return 0
    s = session()
    try:
        vid = s.upload(mp4, meta, progress_callback=lambda step, pct: log(f"{step} {pct:.0f}%") if pct in (0, 100) else None)
    except BaseException:
        cur.execute("UPDATE nova_speaks_renders SET youtube_id=%s WHERE slug=%s AND youtube_id='uploading'", (old if replacing else None, a.slug)); raise
    cur.execute("UPDATE nova_speaks_renders SET youtube_id=%s, youtube_uploaded_at=now() WHERE slug=%s", (vid, a.slug))
    log(f"DONE https://youtu.be/{vid} ({a.privacy})")
    if replacing:
        try:
            ok = retire(s, old)
        except Exception as e:
            log(f"retire {old} failed: {e}"); ok = False
        cur.execute("UPDATE nova_speaks_renders SET old_youtube_retired=%s, note=concat_ws(' | ', note, %s) WHERE slug=%s",
                    (ok, f"replaced old youtube_id {old} -> {vid}; old " + ("set PRIVATE + removed from playlist" if ok else "NEEDS MANUAL HIDE"), a.slug))
    print(vid); return 0


if __name__ == "__main__":
    sys.exit(main())
