#!/opt/homebrew/opt/python@3.14/bin/python3.14
"""nova_channel_recipes.py — the written recipes behind Jordan's cooking channels, in Nova's recipe memories.

Jordan 2026-11-02: "All cooking channels that have recipes should be ingested into Nova's memories and be
candidates for the recipes of the day."

Most cooking channels link the full written recipe in each video's description (their own site, or a recipe
host). For every Food and Friends channel (nova_friends.FOOD_CHANNELS + channels nova_channel_router sent to food):
list its recent uploads, read each NEW video's description (no login), follow every link (short links resolved),
and store each page that carries a schema.org Recipe through nova_recipe_ingest — so it lands as source 'recipes',
type 'recipe', credited to the channel, and nova_friends.fetch_recipes offers it to the three plates.
Seen videos are kept in nova_ops.channel_recipe_videos, so a daily run only reads what is new.

Usage: nova_channel_recipes.py [--per-channel N] [--channel NAME] [--dry-run]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).parent))

YTDLP = "/opt/homebrew/bin/yt-dlp"
URL_RX = re.compile(r"https?://[^\s<>\"')\]]+")
SKIP_HOSTS = re.compile(r"(amazon\.|amzn\.|patreon|instagram|facebook|tiktok|twitter|x\.com|youtube|youtu\.be|spotify|"
                        r"apple\.com|discord|twitch|threads\.net|linktr\.ee|merch|shop|store|bit\.ly/merch|"
                        r"teespring|bonfire|cameo|paypal|venmo|gofundme|google\.com|goo\.gl/maps|snapchat|pinterest)", re.I)
DDL = """CREATE TABLE IF NOT EXISTS channel_recipe_videos (
  video_id text PRIMARY KEY, channel text, seen_at timestamptz NOT NULL DEFAULT now(), recipe_urls text[])"""


def log(m):
    print(f"[channel-recipes {time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


def food_channels() -> list[tuple[str, str]]:
    """[(channel id, name)] for every food channel Jordan is subscribed to."""
    import nova_friends as nf
    import psycopg2
    import nova_yt_subs_audio as ys
    names = set(nf.norm(c) for c in nf.FOOD_CHANNELS)
    c = psycopg2.connect(ys.DSN); cur = c.cursor()
    cur.execute("SELECT value FROM service_config WHERE service='friends' AND key='auto_channels'")
    row = cur.fetchone()
    for r in ((row[0] or {}).get("routes", {}) if row else {}).values():
        if r.get("section") == "food":
            names.add(nf.norm(r["name"]))
    cur.execute("SELECT value FROM service_config WHERE service='yt_subs_audio' AND key='subscriptions'")
    subs = cur.fetchone()[0]
    c.close()
    return [(s[0], s[2]) for s in subs if nf.norm(s[2]) in names]


def uploads(cid: str, n: int) -> list[str]:
    r = subprocess.run([YTDLP, "--no-warnings", "--flat-playlist", "--playlist-end", str(n), "--print", "%(id)s",
                        f"https://www.youtube.com/channel/{cid}/videos"], capture_output=True, text=True, timeout=180)
    return [x for x in r.stdout.split() if len(x) == 11]


def description(vid: str) -> str:
    r = subprocess.run([YTDLP, "--no-warnings", "--skip-download", "--print", "%(description)s",
                        f"https://www.youtube.com/watch?v={vid}"], capture_output=True, text=True, timeout=120)
    return r.stdout


def links(desc: str) -> list[str]:
    """Candidate recipe links from a description (social, shops and YouTube itself left out). Pure."""
    out = []
    for u in URL_RX.findall(desc or ""):
        u = u.rstrip(".,;:!?")
        if SKIP_HOSTS.search(urlsplit(u).netloc + urlsplit(u).path[:12]):
            continue
        out.append(u)
    return list(dict.fromkeys(out))[:8]


def resolve(u: str) -> str:
    """Follow redirects (short links: one HEAD, Location read, no body — bit.ly 429s a GET-follower); drop
    tracking query strings."""
    import http.client
    final = u
    for _ in range(4):
        p = urlsplit(final)
        if p.scheme not in ("http", "https"):
            break
        try:
            conn = (http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection)(p.netloc, timeout=15)
            conn.request("HEAD", p.path or "/" + ("?" + p.query if p.query else ""), headers={"User-Agent": "curl/8"})
            r = conn.getresponse(); loc = r.getheader("Location"); conn.close()
        except Exception:  # noqa: BLE001
            break
        if r.status in (301, 302, 303, 307, 308) and loc:
            from urllib.parse import urljoin
            final = urljoin(final, loc)
            time.sleep(0.3)
        else:
            break
    p = urlsplit(final)
    return urlunsplit(p._replace(query="", fragment=""))


def main(argv) -> int:
    import nova_recipe_ingest as ri
    import nova_dsn
    per = int(argv[argv.index("--per-channel") + 1]) if "--per-channel" in argv else 40
    only = argv[argv.index("--channel") + 1] if "--channel" in argv else None
    dry = "--dry-run" in argv
    c = nova_dsn.pg_connect(); c.autocommit = True; cur = c.cursor()
    cur.execute(DDL)
    cur.execute("SELECT video_id FROM channel_recipe_videos")
    seen = {r[0] for r in cur.fetchall()}
    have = ri.stored_urls()
    chans = [x for x in food_channels() if not only or x[1] == only]
    log(f"{len(chans)} food channels")
    total = 0
    for cid, name in chans:
        new = [v for v in uploads(cid, per) if v not in seen]
        got = 0
        for vid in new:
            found = []
            for u in links(description(vid)):
                final = resolve(u)
                if final in have or SKIP_HOSTS.search(urlsplit(final).netloc):
                    continue
                try:
                    page = ri.get(final)
                    r = ri.parse_recipe(page) or ri.parse_recipe_html(page)
                except Exception:  # noqa: BLE001
                    r = None
                if not r:
                    continue
                host = urlsplit(final).netloc
                if dry or ri.store(final, r, name, host):
                    have.add(final); found.append(final); got += 1
                    log(f"  {name}: {'would store' if dry else 'stored'} {r['name']}")
                time.sleep(0.5)
            if not dry:
                cur.execute("INSERT INTO channel_recipe_videos (video_id, channel, recipe_urls) VALUES (%s,%s,%s) "
                            "ON CONFLICT (video_id) DO NOTHING", (vid, name, found))
        log(f"{name}: {len(new)} new videos, {got} recipes")
        total += got
    log(f"done: {total} recipes from {len(chans)} channels")
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
