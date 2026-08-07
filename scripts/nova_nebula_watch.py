#!/usr/bin/env python3
"""nova_nebula_watch.py — follow specific creators on Nebula, ping #nova-feed on new uploads.

Seeded from Jordan's YouTube subscriptions: these 7 creators he already follows on YouTube
cross-post to Nebula (found 2026-08-07 by mining their YouTube video descriptions). Nebula
is subscription-gated, so this needs Jordan's Nebula login — stored in Keychain, never in code.

STATUS: LIVE (verified against Jordan's account 2026-08-07). All 7 channel slugs resolve and
the endpoints are confirmed. Still exits 0 with "not configured" if the Keychain creds ever go
missing, so task_sentinel stays green either way.

Keychain (account 'nova'):  nova-nebula-email   nova-nebula-password
"""
from __future__ import annotations
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_creator_feed as feed

PLATFORM = "nebula"

# creator display name -> Nebula channel slug  (VERIFIED live 2026-08-07)
CREATORS = {
    "RealLifeLore":        "reallifelore",
    "Real Engineering":    "realengineering",
    "Joe Scott":           "joescott",
    "The Great War":       "the-great-war",
    "Adam Neely":          "adam-neely",
    "12tone":              "12tone",
    "The Overview Effekt": "overvieweffekt",
}

# VERIFIED 2026-08-07: nebula.tv/auth/login is behind a Cloudflare bot-challenge, but the
# users API takes email+password directly and returns a Bearer token. Content lives on the
# content API. Item fields: id (unique), title, share_url, published_at.
AUTH_URL     = "https://users.api.nebula.app/api/v1/authorization/"
EPISODES_URL = "https://content.api.nebula.app/video_channels/{slug}/video_episodes/"
UA = "Mozilla/5.0 (NovaCreatorFeed)"
RECENT = 6  # only look at the newest few per creator


def _post_json(url, payload, headers=None):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": UA, **(headers or {})})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def login() -> str | None:
    """Return a Bearer token, or None if unconfigured/failed. Never logs the secret.
    The users API accepts email+password directly and returns the bearer in one call."""
    email = feed.keychain("nova-nebula-email")
    password = feed.keychain("nova-nebula-password")
    if not email or not password:
        return None
    try:
        return _post_json(AUTH_URL, {"email": email, "password": password}).get("token")
    except Exception as e:
        print(f"nebula: login failed: {e}", file=sys.stderr)
        return None


def fetch_recent(bearer: str, slug: str) -> list:
    """Return recent uploads for a channel as feed Upload dicts. ⚠ VERIFY field names."""
    try:
        data = _get_json(EPISODES_URL.format(slug=slug),
                         {"Authorization": f"Bearer {bearer}"})
    except Exception as e:
        print(f"nebula: fetch {slug} failed: {e}", file=sys.stderr)
        return []
    out = []
    for ep in (data.get("results") or [])[:RECENT]:
        vid = str(ep.get("id") or ep.get("slug") or ep.get("share_url") or "")
        if not vid:
            continue
        url = ep.get("share_url") or f"https://nebula.tv/videos/{ep.get('slug','')}"
        out.append({"video_id": f"{slug}:{vid}", "title": ep.get("title", "(untitled)"),
                    "url": url, "published_at": ep.get("published_at")})
    return out


def main() -> int:
    import psycopg2
    bearer = login()
    if not bearer:
        print("nebula: not configured (no Keychain creds) — skipping")
        return 0  # clean skip, not a failure
    conn = psycopg2.connect(feed.DSN)
    feed.ensure_schema(conn)
    total_new = 0
    for name, slug in CREATORS.items():
        ups = fetch_recent(bearer, slug)
        new = feed.process_creator(conn, PLATFORM, name, ups)
        total_new += len(new)
        if new:
            print(f"nebula: {name} -> {len(new)} new")
    conn.close()
    print(f"nebula: {len(CREATORS)} creators checked, {total_new} new upload(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
