#!/usr/bin/env python3
"""nova_rumble_watch.py — follow specific creators on Rumble, ping #nova-feed on new uploads.

⚠ FRAGILE BY NATURE — READ THIS. Rumble killed its public RSS (rumble.com/user/X/rss just
redirects to HTML), so unlike every other creator-feed adapter this one SCRAPES the channel
page's embedded JSON. That is exactly the brittle path we avoid elsewhere; it will break when
Rumble reshuffles their markup. It's isolated (one platform, its own task) so a break can't
touch Nebula/Floatplane/Patreon, and if the scraper comes up empty across ALL channels it fires
a single deduped warning so the breakage is loud, not silent. Accepted because Jordan explicitly
asked, knowing the tradeoff.

CHANNELS: the 8 that survived verification 2026-08-07 (>=5 real on-brand videos, or Jordan's own
known follow) out of a 799-sub name scan. Reuploaders/thin/mainstream-squatters were rejected.

No auth needed (public pages). No dates available from the scrape, so "new" is by stable video id.
"""
from __future__ import annotations
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_creator_feed as feed

PLATFORM = "rumble"
RECENT = 12
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# display name -> channel page URL  (VERIFIED live 2026-08-07: real, on-brand catalog)
CREATORS = {
    "Forgotten Weapons":    "https://rumble.com/c/ForgottenWeapons",
    "Jason Hanson":         "https://rumble.com/c/JasonHanson",
    "POWERtube TV":         "https://rumble.com/user/POWERtubeTV",
    "Banks Power":          "https://rumble.com/c/BanksPower",
    "The Bearded Mechanic": "https://rumble.com/c/TheBeardedMechanic",
    "Dark5":                "https://rumble.com/c/Dark5",
    "Vice Grip Garage":     "https://rumble.com/c/ViceGripGarage",
    "Valley Racing":        "https://rumble.com/user/ValleyRacing",
}

# each embedded video object: "url":"https://rumble.com/vID-slug.html" ... "by":{..."name":"X"...}
_VIDEO_RE = re.compile(
    r'"url":"(https://rumble\.com/(v[0-9a-z]+)-([^"]+?)\.html)"[^}]*?"by":\{[^}]*?"name":"([^"]+)"')


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def fetch_recent(name: str, url: str) -> list:
    """Scrape a channel page for ITS OWN recent videos (filtered by the by-channel name so
    recommended videos on the page are excluded). Returns feed Upload dicts, newest-first."""
    try:
        h = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15
                                   ).read().decode("utf-8", "ignore")
    except Exception as e:
        print(f"rumble: fetch {name} failed: {e}", file=sys.stderr)
        return []
    want = _norm(name)
    seen, out = set(), []
    for m in _VIDEO_RE.finditer(h):
        full_url, vid, slug, by = m.group(1), m.group(2), m.group(3), m.group(4)
        if vid in seen or _norm(by) != want:
            continue
        seen.add(vid)
        out.append({"video_id": vid, "title": slug.replace("-", " ").title(),
                    "url": full_url, "published_at": None})
        if len(out) >= RECENT:
            break
    return out


def main() -> int:
    import psycopg2
    conn = psycopg2.connect(feed.DSN)
    feed.ensure_schema(conn)
    total_new, total_found = 0, 0
    for name, url in CREATORS.items():
        ups = fetch_recent(name, url)
        total_found += len(ups)
        new = feed.process_creator(conn, PLATFORM, name, ups)
        total_new += len(new)
        if new:
            print(f"rumble: {name} -> {len(new)} new")
    conn.close()
    # breakage canary: zero videos across EVERY channel almost certainly means the scrape broke,
    # not that 8 channels all went silent. Fire one deduped warning so it's not a silent failure.
    if total_found == 0:
        try:
            from nova_notify import notify
            notify("Rumble scraper found 0 videos across all channels — markup likely changed",
                   level="warning", category="creator-feed", source="nova_rumble_watch",
                   dedup_key="rumble-scraper-broken")
        except Exception:
            pass
        print("rumble: WARNING — 0 videos across all channels (scraper likely broken)")
    print(f"rumble: {len(CREATORS)} creators checked, {total_new} new upload(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
