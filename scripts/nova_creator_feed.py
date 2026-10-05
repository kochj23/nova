#!/usr/bin/env python3
"""nova_creator_feed.py — shared plumbing for cross-platform creator following.

The reliable half of the Grayjay idea: for the creators you already follow on YouTube,
get pinged in #nova-feed when they post on a *different* platform you'd otherwise miss.

This module holds everything the per-platform adapters share so there is ONE copy of the
plumbing, not one per platform (see operations/2026-08-06-the-hoarder-s-reckoning):
  - credential loading from the macOS Keychain (graceful when not yet configured)
  - PG dedup state (creator_feed_seen) so each upload is announced exactly once
  - announce() -> notify() at level=info, which nova_notifier routes to #nova-feed
  - process_creator(): the fetch -> diff-vs-seen -> announce -> record loop

An adapter (nova_nebula_watch.py, nova_floatplane_watch.py) only has to implement two
things: how to authenticate, and how to list a creator's recent uploads as a list of
Upload dicts. Everything else lives here.

Upload dict shape (what an adapter's fetch function must return per item):
    {"video_id": <stable unique id, str>, "title": str, "url": str,
     "published_at": <iso str or None>}
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

DSN = "host=localhost dbname=nova_ops user=kochj"


# ── credentials ───────────────────────────────────────────────────────────────
def keychain(service: str, account: str = "nova") -> str | None:
    """Return a Keychain secret, or None if it isn't set yet (so an unconfigured
    adapter can skip cleanly rather than crash). Never raises, never logs the secret."""
    import subprocess
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", service,
                            "-a", account, "-w"], capture_output=True, text=True, timeout=8)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        # some entries are stored without the account; try service-only
        r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                           capture_output=True, text=True, timeout=8)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


# ── browser-cookie auth (for platforms whose password login is captcha-walled) ──
YT_DLP = "/opt/homebrew/bin/yt-dlp"


def refresh_browser_cookies(browser: str, probe_url: str, cache_file, max_age_h: float = 12.0):
    """Ensure `cache_file` holds fresh cookies for `browser`, extracted via osascript so it
    works even from a launchd daemon (the GUI session is TCC-allowed to read Safari/Chrome
    cookies; a bare daemon is not — same trick nova_yt_new_episodes uses). Returns the path
    if usable, else None. Never raises."""
    import os
    import subprocess
    import time
    cache_file = str(cache_file)
    try:
        if os.path.exists(cache_file) and (time.time() - os.path.getmtime(cache_file)) / 3600 < max_age_h:
            return cache_file
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        inner_argv = [YT_DLP, "--cookies-from-browser", browser, "--cookies", cache_file,
                      "--skip-download", "--simulate", probe_url]
        inner = (f'{YT_DLP} --cookies-from-browser {browser} --cookies {cache_file} '
                 f'--skip-download --simulate "{probe_url}"')
        # osascript first (TCC-safe from launchd); fall back to direct (works from a terminal)
        subprocess.run(["/usr/bin/osascript", "-e", f'do shell script "{inner}"'],
                       capture_output=True, text=True, timeout=60)
        if not os.path.exists(cache_file):
            subprocess.run(inner_argv, capture_output=True, text=True, timeout=60)
        if os.path.exists(cache_file):
            _filter_cookies_to_domain(cache_file, probe_url)  # security: keep only what we need
            os.chmod(cache_file, 0o600)
            return cache_file
    except Exception:
        pass
    return None


def _filter_cookies_to_domain(cache_file: str, probe_url: str):
    """yt-dlp dumps EVERY browser cookie; we only need the target site's session. Strip the
    file to just that registered domain so we don't persist every logged-in session on disk."""
    from urllib.parse import urlparse
    host = urlparse(probe_url).netloc
    reg = ".".join(host.split(".")[-2:]) if "." in host else host  # e.g. floatplane.com
    try:
        kept = []
        for ln in open(cache_file, errors="ignore"):
            if ln.startswith("#") or not ln.strip() or reg in ln.split("\t", 1)[0]:
                kept.append(ln)
        open(cache_file, "w").writelines(kept)
    except Exception:
        pass  # a filter failure must never break auth


def cookie_opener(cache_file):
    """Build a urllib opener from a Netscape cookie file. None on failure."""
    import http.cookiejar
    import urllib.request
    try:
        jar = http.cookiejar.MozillaCookieJar(cache_file)
        jar.load(ignore_discard=True, ignore_expires=True)
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    except Exception:
        return None


# ── dedup state ───────────────────────────────────────────────────────────────
def ensure_schema(conn):
    """Idempotent: the one small table this whole feature needs."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS creator_feed_seen (
                platform    text NOT NULL,
                video_id    text NOT NULL,
                creator     text,
                title       text,
                url         text,
                published_at timestamptz,
                seen_at     timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (platform, video_id)
            )""")
    conn.commit()


def seen_ids(conn, platform: str) -> set:
    with conn.cursor() as cur:
        cur.execute("SELECT video_id FROM creator_feed_seen WHERE platform=%s", (platform,))
        return {r[0] for r in cur.fetchall()}


def select_new(uploads: list, already: set) -> list:
    """Pure: the uploads whose video_id isn't in `already`, de-duped within the batch.
    Order-preserving. This is the whole diff — kept pure so it's unit-testable."""
    out, batch = [], set()
    for u in uploads:
        vid = u.get("video_id")
        if not vid or vid in already or vid in batch:
            continue
        batch.add(vid)
        out.append(u)
    return out


def record_seen(conn, platform: str, creator: str, uploads: list):
    if not uploads:
        return
    with conn.cursor() as cur:
        for u in uploads:
            cur.execute(
                """INSERT INTO creator_feed_seen (platform, video_id, creator, title, url, published_at)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (platform, video_id) DO NOTHING""",
                (platform, u["video_id"], creator, u.get("title"), u.get("url"),
                 u.get("published_at")))
    conn.commit()


# ── announce ──────────────────────────────────────────────────────────────────
_PLATFORM_EMOJI = {"nebula": "🟣", "floatplane": "🔵", "rumble": "🟢", "odysee": "⚫",
                   "patreon": "🧡"}


def announce(creator: str, platform: str, title: str, url: str):
    """Ping #nova-feed. level=info -> nova_notifier routes to the ambient feed channel.
    dedup_key guards against a double-send if a run overlaps."""
    try:
        from nova_notify import notify
    except Exception:
        return
    emoji = _PLATFORM_EMOJI.get(platform, "🎥")
    notify(f"{emoji} {creator} posted on {platform.title()}: {title}",
           body=url, level="info", category="creator-feed",
           source=f"nova_{platform}_watch",
           dedup_key=f"creator-feed:{platform}:{url}")


# ── the shared loop ───────────────────────────────────────────────────────────
def process_creator(conn, platform: str, creator: str, uploads: list,
                    announce_new: bool = True, seed_only: bool = False) -> list:
    """Diff a creator's fetched uploads against what we've seen, announce the new ones
    (unless seed_only — used on first run so we don't dump a creator's whole backlog to
    Slack), and record them. Returns the new uploads."""
    already = seen_ids(conn, platform)
    first_time = not already  # nothing recorded for this platform yet -> seed silently
    new = select_new(uploads, already)
    if new and announce_new and not (seed_only or first_time):
        for u in new:
            announce(creator, platform, u.get("title", "(untitled)"), u.get("url", ""))
    record_seen(conn, platform, creator, new)
    return new
