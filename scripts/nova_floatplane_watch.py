#!/usr/bin/env python3
"""nova_floatplane_watch.py — follow specific creators on Floatplane, ping #nova-feed.

Seeded from Jordan's YouTube subs: these 2 creators he follows on YouTube also post on
Floatplane (LMG's platform), found 2026-08-07 by mining their YouTube video descriptions.

AUTH (verified 2026-08-07): Floatplane's password login (/api/v3/auth/login) now REQUIRES a
captcha token, so headless user/password auth is blocked. Floatplane sessions are cookie-based
though, so we authenticate exactly like nova_yt_new_episodes does for YouTube: read the logged-in
`sails.sid` session cookie from the browser (Jordan is logged into Floatplane in SAFARI). No
password is stored or used — the only requirement is a live Safari web login.

STATUS: LIVE. If the Safari session is missing/expired it exits 0 with "no valid session"
(task_sentinel stays green); Jordan just re-opens floatplane.com in Safari to refresh it.

Requirement: be logged into floatplane.com in Safari. (nova-floatplane-user/password Keychain
entries are unused — password login is captcha-walled.)
"""
from __future__ import annotations
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_creator_feed as feed

PLATFORM = "floatplane"
BROWSER = "safari"
COOKIES_FILE = Path.home() / ".openclaw/cache/fp_cookies.txt"

# creator display name -> Floatplane urlname  (VERIFIED live 2026-08-07)
CREATORS = {
    "3D Printing Nerd":  "3dprintingnerd",
    "Forgotten Weapons": "ForgottenWeapons",
}

BASE = "https://www.floatplane.com/api"
SELF_URL    = f"{BASE}/v3/user/self"
NAMED_URL   = f"{BASE}/v3/creator/named?creatorURL={{urlname}}"
CONTENT_URL = f"{BASE}/v3/content/creator?id={{gid}}&limit=6&fetchAfter=0"
UA = {"User-Agent": "Mozilla/5.0 (NovaCreatorFeed)"}


def session():
    """Return an authenticated opener from the Safari session cookie, or None if there's no
    valid login. Validates against /user/self so an expired session skips cleanly."""
    cf = feed.refresh_browser_cookies(BROWSER, SELF_URL, COOKIES_FILE)
    if not cf:
        return None
    op = feed.cookie_opener(cf)
    if not op:
        return None
    try:
        r = op.open(urllib.request.Request(SELF_URL, headers=UA), timeout=15)
        who = json.loads(r.read()).get("username")
        return op if who else None
    except Exception:
        return None  # 403 notLoggedInError -> session expired


def _get(op, url):
    return json.loads(op.open(urllib.request.Request(url, headers=UA), timeout=20).read())


def _creator_gid(op, urlname: str) -> str | None:
    try:
        data = _get(op, NAMED_URL.format(urlname=urlname))
        rows = data if isinstance(data, list) else [data]
        return (rows[0] or {}).get("id") if rows else None
    except Exception as e:
        print(f"floatplane: resolve {urlname} failed: {e}", file=sys.stderr)
        return None


def fetch_recent(op, urlname: str) -> list:
    gid = _creator_gid(op, urlname)
    if not gid:
        return []
    try:
        posts = _get(op, CONTENT_URL.format(gid=gid))
    except Exception as e:
        print(f"floatplane: content {urlname} failed: {e}", file=sys.stderr)
        return []
    out = []
    for p in (posts if isinstance(posts, list) else []):
        vid = str(p.get("id") or "")
        if not vid:
            continue
        out.append({"video_id": vid, "title": p.get("title", "(untitled)"),
                    "url": f"https://www.floatplane.com/post/{vid}",
                    "published_at": p.get("releaseDate")})
    return out


def main() -> int:
    import psycopg2
    op = session()
    if not op:
        print("floatplane: no valid Safari session — skipping (log in at floatplane.com in Safari)")
        return 0  # clean skip, not a failure
    conn = psycopg2.connect(feed.DSN)
    feed.ensure_schema(conn)
    total_new = 0
    for name, urlname in CREATORS.items():
        ups = fetch_recent(op, urlname)
        new = feed.process_creator(conn, PLATFORM, name, ups)
        total_new += len(new)
        if new:
            print(f"floatplane: {name} -> {len(new)} new")
    conn.close()
    print(f"floatplane: {len(CREATORS)} creators checked, {total_new} new upload(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
