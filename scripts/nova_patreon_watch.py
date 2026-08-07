#!/usr/bin/env python3
"""nova_patreon_watch.py — follow the creators you back on Patreon, ping #nova-feed on new posts.

Unlike the Nebula/Floatplane adapters (which hardcode a creator list discovered from YouTube),
Patreon exposes *who you support* directly, so this DISCOVERS the creator list dynamically each
run from your pledges — it auto-updates as you back or drop creators, nothing to maintain.

AUTH (verified 2026-08-07): browser-cookie session, same as Floatplane — Jordan is logged into
Patreon in SAFARI. We call Patreon's own web API with that session. No password stored.

STATUS: LIVE. If the Safari session is missing/expired it exits 0 with "no valid session"
(task_sentinel stays green); Jordan just re-opens patreon.com in Safari to refresh it.

Requirement: be logged into patreon.com in Safari.
"""
from __future__ import annotations
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_creator_feed as feed

PLATFORM = "patreon"
BROWSER = "safari"
COOKIES_FILE = Path.home() / ".openclaw/cache/patreon_cookies.txt"

API = "https://www.patreon.com/api"
SELF_URL = (API + "/current_user?include=pledges.campaign.creator"
            "&fields[campaign]=name,url&fields[user]=full_name")
POSTS_URL = (API + "/posts?filter[campaign_id]={cid}&sort=-published_at&page[count]=6"
             "&fields[post]=title,url,published_at,current_user_can_view")
UA = {"User-Agent": "Mozilla/5.0 (NovaCreatorFeed)", "Accept": "application/json"}


def _get(op, url):
    return json.loads(op.open(urllib.request.Request(url, headers=UA), timeout=20).read())


def session():
    """Return (opener, campaigns) where campaigns = [(campaign_id, name)], or (None, []) if
    there's no valid Patreon session. Validates + discovers the follow-list in one call."""
    cf = feed.refresh_browser_cookies(BROWSER, "https://www.patreon.com/", COOKIES_FILE)
    if not cf:
        return None, []
    op = feed.cookie_opener(cf)
    if not op:
        return None, []
    try:
        d = _get(op, SELF_URL)
    except Exception:
        return None, []  # 401/redirect -> session expired
    if not (d.get("data", {}).get("attributes", {}) or {}).get("full_name"):
        return None, []
    camps = [(o["id"], o.get("attributes", {}).get("name", "(unknown)"))
             for o in d.get("included", []) if o.get("type") == "campaign"]
    return op, camps


def fetch_recent(op, cid: str) -> list:
    try:
        data = _get(op, POSTS_URL.format(cid=cid))
    except Exception as e:
        print(f"patreon: posts {cid} failed: {e}", file=sys.stderr)
        return []
    out = []
    for post in data.get("data", []):
        vid = str(post.get("id") or "")
        if not vid:
            continue
        a = post.get("attributes", {})
        url = a.get("url") or ""
        if url.startswith("/"):
            url = "https://www.patreon.com" + url
        out.append({"video_id": vid, "title": a.get("title") or "(untitled)",
                    "url": url, "published_at": a.get("published_at")})
    return out


def main() -> int:
    import psycopg2
    op, campaigns = session()
    if not op:
        print("patreon: no valid Safari session — skipping (log in at patreon.com in Safari)")
        return 0  # clean skip, not a failure
    conn = psycopg2.connect(feed.DSN)
    feed.ensure_schema(conn)
    total_new = 0
    for cid, name in campaigns:
        ups = fetch_recent(op, cid)
        new = feed.process_creator(conn, PLATFORM, name, ups)
        total_new += len(new)
        if new:
            print(f"patreon: {name} -> {len(new)} new")
    conn.close()
    print(f"patreon: {len(campaigns)} creators checked, {total_new} new post(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
