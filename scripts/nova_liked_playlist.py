#!/opt/homebrew/opt/python@3.14/bin/python3.14
"""nova_liked_playlist.py — every MUSIC video Jordan has liked on YouTube, as a properly tagged MP3 in the Music
app playlist "5 stars - Youtube", rated five stars.

Jordan 2026-11-02: "For every youtube liked video that is music (that hasn't already been downloaded), download
them and put them in a iTunes playlist called "5 stars - Youtube" with accurate ID3 tags and file names and rated
5 stars. Do this every week on Monday."

Per liked video not yet in nova_ops.yt_music_library:
  1. Is it music? The same judge as Liked and Listened (nova_liked_music.classify: hard rules, the local model,
     the plainly-music override; podcasts' themes and vlogs are out). A previous verdict in yt_likes is reused.
  2. Tags: YouTube Music's own artist/track/album/release_year when the upload carries them (Topic / Art Track
     uploads), else the judge's artist + track; then MusicBrainz (1 req/s, score >= 90, artist must match) for
     album, year and track number. YouTube packaging ("(Official Video)", "[4K Remaster]", "(Lyrics)") is
     stripped. DJ sets: artist = the DJ, album = "<channel> — DJ Sets" unless MusicBrainz knows better.
  3. Download best audio -> MP3 V0 with ID3v2.4 (title, artist, album artist, album, year, track, genre if known,
     cover art from the video thumbnail, a comment with the YouTube URL) at
     /Volumes/Data/music/YouTube Liked/<Artist> - <Title>.mp3.
  4. Music app: add the file, put it in "5 stars - Youtube" (created if missing), rating 100 (five stars).
Rows: status music_added / not_music / failed (failed retries next week, at most 3 times).

Usage: nova_liked_playlist.py [--limit N] [--dry-run]       # scheduler: Mondays 09:00
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

OUT = Path("/Volumes/Data/music/YouTube Liked")
PLAYLIST = "5 stars - Youtube"
YTDLP = "/opt/homebrew/bin/yt-dlp"
MB_UA = "NovaLikedPlaylist/1.0 ( kochj23 on GitHub )"
DDL = """CREATE TABLE IF NOT EXISTS yt_music_library (
  video_id text PRIMARY KEY, status text NOT NULL, verdict jsonb, artist text, title text, album text, year int,
  track_no int, path text, attempts int NOT NULL DEFAULT 0, error text, updated_at timestamptz NOT NULL DEFAULT now())"""
JUNK_RX = re.compile(r"\s*[\(\[][^\)\]]*(?:official|video|audio|lyric|visuali[sz]er|remaster|\bhd\b|\bhq\b|\b4k\b|"
                     r"m/?v|clip officiel|full song|explicit)[^\)\]]*[\)\]]", re.I)


def log(m):
    print(f"[liked-playlist {time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


def _conn():
    import nova_dsn
    c = nova_dsn.pg_connect(); c.autocommit = True
    return c


# ── tags ────────────────────────────────────────────────────────────────────────────────
def tidy(s: str) -> str:
    """'More Than This (Lyrics)' -> 'More Than This'. Pure."""
    s = JUNK_RX.sub("", s or "")
    s = re.sub(r"\s*\|\s*.*$", "", s)                  # "Allstars MIC | DnB Allstars" -> "Allstars MIC"
    return re.sub(r"\s{2,}", " ", s).strip(" -–—")


def safe_name(s: str) -> str:
    """A filesystem-safe, still-readable file name part. Pure."""
    s = re.sub(r'[\\/:*?"<>|]+', " ", s)
    return re.sub(r"\s{2,}", " ", s).strip(" .")[:120]


def pick_release(recs: list[dict], artist: str) -> tuple[str, dict] | None:
    """(artist credit, release) for the ORIGINAL album: across every matching recording, releases are grouped by
    release group; compilations, live albums, soundtracks, DJ mixes and Various-Artists releases are out; the group
    with the earliest date wins and its earliest release (date = that group's first date) is returned. Else the
    earliest single/EP group. None when nothing fits. Pure.
    (2026-11-02: a first-hit pick filed "It's My Life" under a 1999 live album and Boards of Canada under a radio
    compilation; MusicBrainz' top 25 for "More Than This" were all compilations.)"""
    norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())  # noqa: E731
    groups = {}
    for rec in recs:
        if rec.get("score", 0) < 90:
            continue
        credit = "".join(a.get("name", "") + a.get("joinphrase", "") for a in rec.get("artist-credit") or [])
        if not norm(artist) or (norm(artist) not in norm(credit) and norm(credit) not in norm(artist)):
            continue
        for r in rec.get("releases") or []:
            rg = r.get("release-group") or {}
            rel_artist = "".join(a.get("name", "") for a in r.get("artist-credit") or [])
            if (r.get("status") or "Official") != "Official" or rg.get("secondary-types") or not r.get("date") \
                    or (rel_artist and norm(rel_artist) != norm(credit)):
                continue
            kind = "album" if rg.get("primary-type") == "Album" else "single" if rg.get("primary-type") in ("Single", "EP") else None
            if not kind:
                continue
            g = groups.setdefault((kind, rg.get("id") or r.get("title")), {"date": r["date"], "rel": r, "credit": credit})
            if r["date"] < g["date"]:
                g.update(date=r["date"], rel=r)
    for kind in ("album", "single"):
        pool = [g for (k, _), g in groups.items() if k == kind]
        if pool:
            g = min(pool, key=lambda g: g["date"])
            return g["credit"], {**g["rel"], "date": g["date"]}
    return None


def musicbrainz(artist: str, title: str) -> dict:
    """{album, year, track_no, artist} from MusicBrainz (see pick_release), or {}. Official albums are asked for
    first (the plain search drowns in compilations), then anything."""
    recs = []
    for extra in (" AND primarytype:album AND status:official", ""):
        q = f'recording:"{title}" AND artist:"{artist}"{extra}'
        url = "https://musicbrainz.org/ws/2/recording?fmt=json&limit=100&query=" + urllib.parse.quote(q)
        for wait in (1.1, 3, 8):                       # MusicBrainz asks for <= 1 req/s; 503 = slow down
            time.sleep(wait)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": MB_UA})
                with urllib.request.urlopen(req, timeout=20) as r:
                    recs = json.load(r).get("recordings") or []
                break
            except Exception as e:  # noqa: BLE001
                log(f"musicbrainz: {type(e).__name__} (retrying)")
        hit = pick_release(recs, artist)
        if hit:
            break
    else:
        return {}
    credit, rel = hit
    out = {"artist": credit, "album": rel.get("title"), "year": int(rel["date"][:4]) if rel["date"][:4].isdigit() else None}
    media = rel.get("media") or []
    try:
        out["track_no"] = int(media[0]["track"][0].get("number"))
    except (TypeError, ValueError, IndexError, KeyError):
        pass
    return out


def tags_for(meta: dict, verdict: dict) -> dict:
    """Artist / title / album / year / track_no for one liked music video."""
    artist = meta.get("artist") or meta.get("creator") or verdict.get("artist") or meta.get("channel") or ""
    artist = artist.split(",")[0].strip() if meta.get("artist") else artist
    title = tidy(meta.get("track") or verdict.get("track") or meta.get("title") or "")
    if not meta.get("track") and artist and title.lower().startswith(artist.lower()):
        title = tidy(title[len(artist):])
    t = {"artist": tidy(artist), "title": title, "album": meta.get("album"),
         "year": meta.get("release_year") or None, "track_no": None, "genre": None}
    if verdict.get("kind") == "dj_set":
        t["genre"] = "DJ Set"
    if not t["album"] or not t["year"]:
        mb = musicbrainz(t["artist"], t["title"]) if t["artist"] and t["title"] else {}
        t["album"] = t["album"] or mb.get("album")
        t["year"] = t["year"] or mb.get("year")
        t["track_no"] = mb.get("track_no")
    if not t["album"]:
        t["album"] = f"{meta.get('channel') or t['artist']} — DJ Sets" if verdict.get("kind") == "dj_set" else \
            f"{t['title']} - Single"
    if not t["year"]:
        d = meta.get("upload_date") or ""
        t["year"] = int(d[:4]) if d[:4].isdigit() else None
    return t


# ── files ───────────────────────────────────────────────────────────────────────────────
def download(vid: str, base: Path) -> tuple[Path, Path | None]:
    OUT.mkdir(parents=True, exist_ok=True)
    tmpl = str(base) + ".%(ext)s"
    r = subprocess.run([YTDLP, "--cookies-from-browser", "safari", "--no-warnings", "--no-playlist", "-x",
                        "--audio-format", "mp3", "--audio-quality", "0", "--write-thumbnail", "--convert-thumbnails", "jpg",
                        "-o", tmpl, f"https://www.youtube.com/watch?v={vid}"], capture_output=True, text=True, timeout=1800)
    mp3 = base.with_suffix(".mp3")
    if r.returncode != 0 or not mp3.exists():
        raise RuntimeError((r.stderr or "download failed").strip()[-300:])
    jpg = base.with_suffix(".jpg")
    return mp3, (jpg if jpg.exists() else None)


def write_tags(mp3: Path, t: dict, vid: str, art: Path | None):
    from mutagen.id3 import APIC, COMM, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, WOAS
    tags = ID3()
    tags.add(TIT2(encoding=3, text=t["title"]))
    tags.add(TPE1(encoding=3, text=t["artist"]))
    tags.add(TPE2(encoding=3, text=t["artist"]))
    tags.add(TALB(encoding=3, text=t["album"]))
    if t.get("year"):
        tags.add(TDRC(encoding=3, text=str(t["year"])))
    if t.get("track_no"):
        tags.add(TRCK(encoding=3, text=str(t["track_no"])))
    if t.get("genre"):
        tags.add(TCON(encoding=3, text=t["genre"]))
    url = f"https://www.youtube.com/watch?v={vid}"
    tags.add(COMM(encoding=3, lang="eng", desc="", text=f"Liked on YouTube: {url}"))
    tags.add(WOAS(url=url))
    if art:
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=art.read_bytes()))
        art.unlink()
    tags.save(str(mp3), v2_version=4)


# ── Music app ───────────────────────────────────────────────────────────────────────────
def osa(script: str, timeout=300) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:])
    return r.stdout.strip()


def esc_as(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def ensure_music():
    subprocess.run(["open", "-g", "-a", "Music"], check=False)
    for _ in range(40):                                # a big NAS library takes a while to come up
        try:
            osa('tell application "Music" to get name', timeout=30); break
        except Exception:  # noqa: BLE001
            time.sleep(15)
    osa(f'''tell application "Music"
  if not (exists user playlist "{esc_as(PLAYLIST)}") then make new user playlist with properties {{name:"{esc_as(PLAYLIST)}"}}
end tell''', timeout=600)


def add_to_music(mp3: Path) -> str:
    """Add, put in the playlist, rate 5 stars. Returns the track's persistent ID."""
    return osa(f'''tell application "Music"
  set t to add (POSIX file "{esc_as(str(mp3))}") to user playlist "{esc_as(PLAYLIST)}"
  set rating of t to 100
  return persistent ID of t
end tell''', timeout=600)


def retag(c) -> int:
    """Second chance for weak tags: rows still filed as '<title> - Single' (MusicBrainz was busy or silent) get
    another lookup each week; a hit rewrites the tags in the Music library's copy and refreshes the track."""
    from mutagen.id3 import ID3, TALB, TDRC, TRCK
    with c.cursor() as cur:
        cur.execute("SELECT video_id, artist, title, error FROM yt_music_library WHERE status='music_added' AND album LIKE '%% - Single'")
        rows = cur.fetchall()
    fixed = 0
    for vid, artist, title, note in rows:
        mb = musicbrainz(artist, title)
        pid = (note or "").removeprefix("music pid ").strip()
        if not mb.get("album") or not pid:
            continue
        try:
            loc = osa(f'tell application "Music" to set f to location of (first track of library playlist 1 whose persistent ID is "{pid}")\n'
                      'return POSIX path of f')
            tags = ID3(loc)
            tags.setall("TALB", [TALB(encoding=3, text=mb["album"])])
            if mb.get("year"):
                tags.setall("TDRC", [TDRC(encoding=3, text=str(mb["year"]))])
            if mb.get("track_no"):
                tags.setall("TRCK", [TRCK(encoding=3, text=str(mb["track_no"]))])
            tags.save(loc, v2_version=4)
            osa(f'tell application "Music" to refresh (first track of library playlist 1 whose persistent ID is "{pid}")')
        except Exception as e:  # noqa: BLE001
            log(f"retag {vid}: {type(e).__name__}: {str(e)[:120]}"); continue
        with c.cursor() as cur:
            cur.execute("UPDATE yt_music_library SET album=%s, year=coalesce(%s, year), track_no=coalesce(%s, track_no), "
                        "updated_at=now() WHERE video_id=%s", (mb["album"], mb.get("year"), mb.get("track_no"), vid))
        log(f"retagged {artist} — {title}: {mb['album']} ({mb.get('year')})")
        fixed += 1
    return fixed


# ── run ─────────────────────────────────────────────────────────────────────────────────
def main(argv) -> int:
    import nova_liked_music as lm
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else 100000
    dry = "--dry-run" in argv
    likes = lm.liked(5000)
    log(f"{len(likes)} liked videos")
    c = _conn()
    with c.cursor() as cur:
        cur.execute(DDL)
        cur.execute("SELECT video_id FROM yt_music_library WHERE status IN ('music_added','not_music') OR attempts >= 3")
        done = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT video_id, kind, verdict FROM yt_likes WHERE kind IS NOT NULL")
        known = {r[0]: (r[1], r[2] or {}) for r in cur.fetchall()}
    todo = [e for e in likes if e["id"] not in done][:limit]
    log(f"{len(todo)} to look at")
    if not dry and todo:
        ensure_music()
    added = notmusic = failed = 0
    for e in todo:
        vid = e["id"]
        try:
            if lm.hard_reject(e):
                verdict = {"music": False, "kind": "other", "reason": lm.hard_reject(e)}
                meta = e
            else:
                meta = json.loads(lm.ytdlp("-j", "--skip-download", f"https://www.youtube.com/watch?v={vid}"))
                meta["duration"] = int(meta.get("duration") or e["duration"] or 0)
                k = known.get(vid)
                verdict = k[1] if k and k[1].get("kind") else lm.classify(meta)
            if not verdict.get("music"):
                notmusic += 1
                if not dry:
                    with c.cursor() as cur:
                        cur.execute("INSERT INTO yt_music_library (video_id, status, verdict, title) VALUES (%s,'not_music',%s,%s) "
                                    "ON CONFLICT (video_id) DO UPDATE SET status='not_music', verdict=EXCLUDED.verdict, updated_at=now()",
                                    (vid, json.dumps(verdict), e["title"]))
                continue
            t = tags_for(meta, verdict)
            base = OUT / safe_name(f"{t['artist']} - {t['title']}")
            log(f"♪ {vid}: {t['artist']} — {t['title']} [{t['album']}, {t['year']}]")
            if dry:
                added += 1; continue
            mp3, art = download(vid, base)
            write_tags(mp3, t, vid, art)
            pid = add_to_music(mp3)
            with c.cursor() as cur:
                cur.execute("""INSERT INTO yt_music_library (video_id, status, verdict, artist, title, album, year, track_no, path, error)
                               VALUES (%s,'music_added',%s,%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (video_id) DO UPDATE SET status='music_added', verdict=EXCLUDED.verdict,
                                 artist=EXCLUDED.artist, title=EXCLUDED.title, album=EXCLUDED.album, year=EXCLUDED.year,
                                 track_no=EXCLUDED.track_no, path=EXCLUDED.path, error=EXCLUDED.error, updated_at=now()""",
                            (vid, json.dumps(verdict), t["artist"], t["title"], t["album"], t["year"], t["track_no"], str(mp3),
                             f"music pid {pid}"))
            added += 1
        except Exception as ex:  # noqa: BLE001
            failed += 1
            log(f"✗ {vid} {e['title'][:60]!r}: {type(ex).__name__}: {str(ex)[:200]}")
            if not dry:
                with c.cursor() as cur:
                    cur.execute("INSERT INTO yt_music_library (video_id, status, title, attempts, error) VALUES (%s,'failed',%s,1,%s) "
                                "ON CONFLICT (video_id) DO UPDATE SET status='failed', attempts=yt_music_library.attempts+1, "
                                "error=EXCLUDED.error, updated_at=now()", (vid, e["title"], str(ex)[:500]))
    if not dry:
        retag(c)
    c.close()
    msg = f"5 stars - Youtube: +{added} songs, {notmusic} not music, {failed} failed (of {len(todo)} looked at)"
    log(msg)
    if not dry and (added or failed):
        try:
            from nova_notify import notify
            notify(f"🎵 {msg}", None, level="info", category="music", source="nova_liked_playlist",
                   dedup_key=f"liked-playlist:{time.strftime('%Y-%m-%d')}")
        except Exception:  # noqa: BLE001
            pass
    return 0 if not failed or added else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
