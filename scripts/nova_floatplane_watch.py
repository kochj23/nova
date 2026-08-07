#!/usr/bin/env python3
"""nova_floatplane_watch.py — follow specific creators on Floatplane, ping #nova-feed.

Seeded from Jordan's YouTube subs: these 2 creators he follows on YouTube also post on
Floatplane (LMG's platform), found 2026-08-07 by mining their YouTube video descriptions.
Floatplane is paid/subscription-gated, so this needs Jordan's Floatplane login — Keychain,
never in code.

STATUS: STUBBED, pending Jordan's Floatplane signup. Until the Keychain creds exist it exits
0 with "not configured". The moment the creds are added it goes live next run — no code or
scheduler change needed.

Keychain (account 'nova'):  nova-floatplane-user   nova-floatplane-password

⚠ NEEDS-VERIFICATION once creds exist: Floatplane's API is well-documented and stable, but
login can require a 2FA step (handled below as a hook) and creator urlnames must be confirmed.
Verify against a live login, then remove this note.
"""
from __future__ import annotations
import json
import sys
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_creator_feed as feed

PLATFORM = "floatplane"

# creator display name -> Floatplane urlname  (⚠ VERIFY against a live account)
CREATORS = {
    "3D Printing Nerd":  "3dprintingnerd",
    "Forgotten Weapons": "ForgottenWeapons",
}

BASE = "https://www.floatplane.com/api"
LOGIN_URL   = f"{BASE}/v2/auth/login"
NAMED_URL   = f"{BASE}/v3/creator/named?creatorURL={{urlname}}"
CONTENT_URL = f"{BASE}/v3/content/creator?id={{gid}}&limit=6&fetchAfter=0"
UA = "Mozilla/5.0 (NovaCreatorFeed)"


def _make_opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def _post(opener, url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    return json.loads(opener.open(req, timeout=20).read())


def _get(opener, url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return json.loads(opener.open(req, timeout=20).read())


def login():
    """Return an authenticated opener (cookie jar holds sails.sid), or None. Never logs the
    secret. ⚠ VERIFY: if the account has 2FA, wire nova-floatplane-2fa here."""
    user = feed.keychain("nova-floatplane-user")
    password = feed.keychain("nova-floatplane-password")
    if not user or not password:
        return None
    opener = _make_opener()
    try:
        resp = _post(opener, LOGIN_URL, {"username": user, "password": password})
        if resp.get("needs2FA"):
            print("floatplane: account needs 2FA — add nova-floatplane-2fa handling", file=sys.stderr)
            return None
        return opener
    except Exception as e:
        print(f"floatplane: login failed: {e}", file=sys.stderr)
        return None


def _creator_gid(opener, urlname: str) -> str | None:
    try:
        data = _get(opener, NAMED_URL.format(urlname=urlname))
        rows = data if isinstance(data, list) else [data]
        return (rows[0] or {}).get("id") if rows else None
    except Exception as e:
        print(f"floatplane: resolve {urlname} failed: {e}", file=sys.stderr)
        return None


def fetch_recent(opener, urlname: str) -> list:
    gid = _creator_gid(opener, urlname)
    if not gid:
        return []
    try:
        posts = _get(opener, CONTENT_URL.format(gid=gid))
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
    opener = login()
    if not opener:
        print("floatplane: not configured (no Keychain creds) — skipping")
        return 0
    conn = psycopg2.connect(feed.DSN)
    feed.ensure_schema(conn)
    total_new = 0
    for name, urlname in CREATORS.items():
        ups = fetch_recent(opener, urlname)
        new = feed.process_creator(conn, PLATFORM, name, ups)
        total_new += len(new)
        if new:
            print(f"floatplane: {name} -> {len(new)} new")
    conn.close()
    print(f"floatplane: {len(CREATORS)} creators checked, {total_new} new upload(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
