#!/usr/bin/env python3
"""
nova_plex.py — Comprehensive Plex Media Server integration for Nova.

Single script with subcommands for watch history, active sessions, stats,
library sync, recommendations, mood tracking, and more.

Plex server: Synology NAS at 192.168.1.10:32400
Auth: macOS Keychain (service: nova-plex-token, account: nova)
Skips library key 23 ("Other") in ALL queries.

Usage:
  python3 nova_plex.py history      # Ingest yesterday's watch history
  python3 nova_plex.py playing      # What's currently playing
  python3 nova_plex.py stats        # Weekly viewing stats
  python3 nova_plex.py sync         # Library vs disk comparison
  python3 nova_plex.py ondeck       # Stale on-deck items
  python3 nova_plex.py recommend    # Unwatched recommendations
  python3 nova_plex.py mood         # Genre/time mood ring
  python3 nova_plex.py filmschool   # Cross-reference watch with memories
  python3 nova_plex.py shame        # Abandoned pile roast
  python3 nova_plex.py velocity     # Binge / late-night detection
  python3 nova_plex.py guest        # Unknown device detection
  python3 nova_plex.py rewatch      # Rewatch index / canon tracker
  python3 nova_plex.py seasonal     # Genre distribution by month

Written by Jordan Koch.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Constants ─────────────────────────────────────────────────────────────────

PLEX_URL = "http://192.168.1.10:32400"
SKIP_LIBRARIES = {23}
WORKSPACE = Path.home() / ".openclaw/workspace"
LOG_FILE = "/tmp/nova-plex.log"
VECTOR_URL = nova_config.VECTOR_URL

TV_DIR = Path("/Volumes/external/videos/TVShows")
MOVIE_DIR = Path("/Volumes/external/videos/Ripped Movies")

PLAYING_FILE = WORKSPACE / "plex_playing.json"
MOOD_FILE = WORKSPACE / "plex_mood.json"
GUEST_FILE = WORKSPACE / "plex_guests.json"
CANON_FILE = WORKSPACE / "plex_canon.json"
SEASONAL_FILE = WORKSPACE / "plex_seasonal.json"

LIBRARY_NAMES = {
    21: "Documentary", 7: "Movies", 26: "Stand-Up Comedy", 6: "TV Shows",
    9: "Home Videos - Koch", 10: "Music Videos", 25: "My Youtube",
    24: "Random Dumping Ground",
}

logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nova_plex")

QUIET = False

# ── Plex API ──────────────────────────────────────────────────────────────────

def _plex_token() -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-plex-token", "-w"],
        capture_output=True, text=True,
    )
    token = result.stdout.strip()
    if not token:
        log.error("Plex token not found in Keychain")
        sys.exit(1)
    return token


_TOKEN_CACHE = None

def token() -> str:
    global _TOKEN_CACHE
    if _TOKEN_CACHE is None:
        _TOKEN_CACHE = _plex_token()
    return _TOKEN_CACHE


def plex_get(path: str, params: dict = None, timeout: int = 15) -> ET.Element:
    params = params or {}
    params["X-Plex-Token"] = token()
    url = f"{PLEX_URL}{path}?{urllib.parse.urlencode(params)}" if params else f"{PLEX_URL}{path}?X-Plex-Token={token()}"
    req = urllib.request.Request(url, headers={"Accept": "application/xml"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return ET.fromstring(resp.read())


def plex_get_json(path: str, params: dict = None, timeout: int = 15) -> dict:
    params = params or {}
    params["X-Plex-Token"] = token()
    url = f"{PLEX_URL}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _skip_library(key) -> bool:
    try:
        return int(key) in SKIP_LIBRARIES
    except (ValueError, TypeError):
        return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def post(msg: str, channel: str = nova_config.SLACK_NOTIFY):
    if QUIET:
        print(msg)
        return
    try:
        nova_config.post_both(msg, slack_channel=channel)
    except Exception as e:
        log.error(f"Post failed: {e}")
        print(msg)


def post_chat(msg: str):
    post(msg, channel=nova_config.SLACK_CHAN)


def post_dm(msg: str):
    post(msg, channel=nova_config.JORDAN_DM)


def store_vector(text: str, source: str, metadata: dict = None):
    payload = json.dumps({
        "text": text,
        "source": source,
        "metadata": metadata or {},
    }).encode()
    req = urllib.request.Request(
        VECTOR_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        log.error(f"Vector store failed: {e}")


def load_json(path: Path, default=None):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return default if default is not None else {}


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def ts_to_dt(ts) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def get_all_libraries() -> list:
    root = plex_get("/library/sections")
    libs = []
    for d in root.findall(".//Directory"):
        key = int(d.get("key", 0))
        if not _skip_library(key):
            libs.append({"key": key, "title": d.get("title"), "type": d.get("type")})
    return libs


def format_duration(seconds: int) -> str:
    h, m = divmod(seconds // 60, 60)
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


# ── Subcommands ───────────────────────────────────────────────────────────────

def cmd_history(args):
    """Ingest yesterday's watch history into vector memory."""
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    start_ts = int(yesterday.replace(hour=0, minute=0, second=0).timestamp())
    end_ts = start_ts + 86400
    date_str = yesterday.strftime("%Y-%m-%d")

    try:
        root = plex_get("/status/sessions/history/all", {
            "sort": "viewedAt:desc",
            "viewedAt>": start_ts,
            "viewedAt<": end_ts,
        })
    except Exception as e:
        log.error(f"History fetch failed: {e}")
        print(f"Error fetching history: {e}")
        return

    items = []
    for video in root.findall(".//Video"):
        lib_id = video.get("librarySectionID", "0")
        if _skip_library(lib_id):
            continue
        title = video.get("grandparentTitle", "") or video.get("title", "Unknown")
        ep_title = video.get("title", "") if video.get("grandparentTitle") else ""
        genre_elems = video.findall(".//Genre")
        genres = [g.get("tag") for g in genre_elems] if genre_elems else []
        viewed_at = video.get("viewedAt", "")
        duration_ms = int(video.get("duration", 0))
        view_offset = int(video.get("viewOffset", 0))
        items.append({
            "title": title,
            "episode": ep_title,
            "genres": genres,
            "viewed_at": viewed_at,
            "duration_min": duration_ms // 60000,
            "library": LIBRARY_NAMES.get(int(lib_id), lib_id),
            "type": video.get("type", "unknown"),
            "rating_key": video.get("ratingKey", ""),
        })

    if not items:
        log.info(f"No watch history for {date_str}")
        print(f"No watch history for {date_str}")
        return

    for item in items:
        ep_str = f" - {item['episode']}" if item['episode'] else ""
        genre_str = ", ".join(item["genres"][:3]) if item["genres"] else "unknown genre"
        text = (
            f"Jordan watched {item['title']}{ep_str} ({genre_str}) on {date_str}. "
            f"Library: {item['library']}. Duration: {item['duration_min']}min."
        )
        store_vector(text, "plex_watch_history", {
            "date": date_str,
            "title": item["title"],
            "genres": item["genres"],
            "library": item["library"],
            "type": item["type"],
        })

    log.info(f"Ingested {len(items)} watch history items for {date_str}")
    print(f"Stored {len(items)} watch history entries for {date_str}")


def cmd_playing(args):
    """Check active Plex sessions, write state file."""
    try:
        root = plex_get("/status/sessions")
    except Exception as e:
        log.error(f"Sessions fetch failed: {e}")
        if PLAYING_FILE.exists():
            PLAYING_FILE.unlink()
        return

    sessions = []
    for video in root.findall(".//Video"):
        lib_id = video.get("librarySectionID", "0")
        if _skip_library(lib_id):
            continue
        player = video.find(".//Player")
        user = video.find(".//User")
        title = video.get("grandparentTitle", "") or video.get("title", "Unknown")
        ep = video.get("title", "") if video.get("grandparentTitle") else ""
        sessions.append({
            "title": title,
            "episode": ep,
            "type": video.get("type", "unknown"),
            "state": player.get("state", "unknown") if player is not None else "unknown",
            "player": player.get("title", "unknown") if player is not None else "unknown",
            "device": player.get("device", "unknown") if player is not None else "unknown",
            "address": player.get("address", "unknown") if player is not None else "unknown",
            "user": user.get("title", "unknown") if user is not None else "unknown",
            "progress_pct": round(int(video.get("viewOffset", 0)) / max(int(video.get("duration", 1)), 1) * 100, 1),
            "duration_min": int(video.get("duration", 0)) // 60000,
            "library": LIBRARY_NAMES.get(int(lib_id), lib_id),
            "checked_at": datetime.now().isoformat(),
        })

    if sessions:
        save_json(PLAYING_FILE, {"sessions": sessions, "updated": datetime.now().isoformat()})
        for s in sessions:
            ep_str = f" - {s['episode']}" if s['episode'] else ""
            print(f"  [{s['state']}] {s['title']}{ep_str} on {s['player']} ({s['progress_pct']}%)")
    else:
        if PLAYING_FILE.exists():
            PLAYING_FILE.unlink()
        print("Nothing playing.")


def cmd_stats(args):
    """Weekly viewing pattern analysis."""
    now = datetime.now(timezone.utc)
    week_ago_ts = int((now - timedelta(days=7)).timestamp())

    try:
        root = plex_get("/status/sessions/history/all", {
            "sort": "viewedAt:desc",
            "viewedAt>": week_ago_ts,
        })
    except Exception as e:
        log.error(f"Stats fetch failed: {e}")
        return

    genre_counter = Counter()
    hour_counter = Counter()
    total_min = 0
    titles = []
    consecutive = defaultdict(list)

    for video in root.findall(".//Video"):
        if _skip_library(video.get("librarySectionID", "0")):
            continue
        duration_min = int(video.get("duration", 0)) // 60000
        total_min += duration_min
        viewed_ts = video.get("viewedAt", "0")
        viewed_dt = ts_to_dt(viewed_ts) if viewed_ts.isdigit() else now
        hour_counter[viewed_dt.hour] += 1
        title = video.get("grandparentTitle", "") or video.get("title", "Unknown")
        titles.append(title)
        for g in video.findall(".//Genre"):
            genre_counter[g.get("tag")] += 1
        day_key = viewed_dt.strftime("%Y-%m-%d")
        consecutive[day_key].append({"title": title, "ts": int(viewed_ts) if viewed_ts.isdigit() else 0})

    if not titles:
        post("No Plex viewing this week.")
        return

    hours = total_min / 60
    top_genres = genre_counter.most_common(5)
    peak_hours = hour_counter.most_common(3)
    title_counts = Counter(titles)

    binges = []
    for day, views in consecutive.items():
        views.sort(key=lambda x: x["ts"])
        streak = 1
        for i in range(1, len(views)):
            if views[i]["ts"] - views[i-1]["ts"] < 7200:
                streak += 1
            else:
                if streak >= 3:
                    binges.append((day, streak, views[i-1]["title"]))
                streak = 1
        if streak >= 3:
            binges.append((day, streak, views[-1]["title"]))

    genre_str = ", ".join(f"{g} ({c})" for g, c in top_genres)
    peak_str = ", ".join(f"{h}:00 ({c}x)" for h, c in peak_hours)
    most_watched = title_counts.most_common(3)
    mw_str = ", ".join(f"{t} ({c}x)" for t, c in most_watched)

    msg = (
        f"*Plex Weekly Digest*\n"
        f"Total: {hours:.1f} hours across {len(titles)} items\n"
        f"Top genres: {genre_str}\n"
        f"Peak hours: {peak_str}\n"
        f"Most watched: {mw_str}"
    )
    if binges:
        binge_str = "; ".join(f"{d}: {s} in a row ({t})" for d, s, t in binges)
        msg += f"\nBinge detected: {binge_str}"

    post(msg)
    log.info(f"Weekly stats: {hours:.1f}h, {len(titles)} items")


def cmd_sync(args):
    """Compare Plex library contents against disk directories."""
    findings = []

    plex_titles = {"movies": set(), "tv": set()}
    for lib in get_all_libraries():
        try:
            root = plex_get(f"/library/sections/{lib['key']}/all")
        except Exception as e:
            log.error(f"Sync: failed to read library {lib['key']}: {e}")
            continue
        for item in root.findall(".//*[@title]"):
            title = item.get("title", "")
            if lib["type"] == "show":
                plex_titles["tv"].add(title.lower().strip())
            elif lib["type"] == "movie":
                plex_titles["movies"].add(title.lower().strip())

    disk_tv = set()
    if TV_DIR.exists():
        disk_tv = {d.name.lower().strip() for d in TV_DIR.iterdir() if d.is_dir()}
    disk_movies = set()
    if MOVIE_DIR.exists():
        disk_movies = {d.name.lower().strip() for d in MOVIE_DIR.iterdir() if d.is_dir() or d.suffix in (".mkv", ".mp4", ".avi", ".m4v")}

    tv_not_in_plex = disk_tv - plex_titles["tv"]
    movies_not_in_plex = disk_movies - plex_titles["movies"]

    if tv_not_in_plex:
        findings.append(f"*TV on disk, not in Plex ({len(tv_not_in_plex)}):*\n" + "\n".join(f"  - {t}" for t in sorted(tv_not_in_plex)[:15]))
    if movies_not_in_plex:
        findings.append(f"*Movies on disk, not in Plex ({len(movies_not_in_plex)}):*\n" + "\n".join(f"  - {m}" for m in sorted(movies_not_in_plex)[:15]))

    plex_files = set()
    for lib in get_all_libraries():
        if lib["type"] not in ("show", "movie"):
            continue
        try:
            root = plex_get(f"/library/sections/{lib['key']}/all", {"includeMedia": "1"})
        except Exception:
            continue
        for media in root.findall(".//*[@file]"):
            f = media.get("file", "")
            if f:
                plex_files.add(f)
        for part in root.findall(".//Part"):
            f = part.get("file", "")
            if f:
                plex_files.add(f)

    missing_files = [f for f in plex_files if not Path(f).exists()]
    if missing_files:
        findings.append(f"*Plex items with missing files ({len(missing_files)}):*\n" + "\n".join(f"  - {Path(f).name}" for f in missing_files[:10]))

    if findings:
        msg = "*Plex Library Sync Report*\n\n" + "\n\n".join(findings)
    else:
        msg = "Plex Library Sync: everything looks good. No mismatches found."

    post(msg)
    log.info(f"Sync: {len(findings)} findings")


def cmd_ondeck(args):
    """Check partially-watched items stale for 30+ days."""
    if PLAYING_FILE.exists():
        playing = load_json(PLAYING_FILE)
        if playing.get("sessions"):
            print("Something is currently playing, skipping on-deck check.")
            return

    try:
        root = plex_get("/library/onDeck")
    except Exception as e:
        log.error(f"On-deck fetch failed: {e}")
        return

    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
    stale = []
    for video in root.findall(".//Video"):
        if _skip_library(video.get("librarySectionID", "0")):
            continue
        last_viewed = int(video.get("lastViewedAt", "0"))
        if 0 < last_viewed < cutoff_ts:
            title = video.get("grandparentTitle", "") or video.get("title", "Unknown")
            ep = video.get("title", "") if video.get("grandparentTitle") else ""
            days_ago = (datetime.now(timezone.utc) - ts_to_dt(str(last_viewed))).days
            progress = round(int(video.get("viewOffset", 0)) / max(int(video.get("duration", 1)), 1) * 100, 1)
            stale.append({"title": title, "episode": ep, "days": days_ago, "progress": progress})

    if not stale:
        print("On-deck is clean. Nothing stale.")
        return

    lines = []
    for s in stale[:5]:
        ep_str = f" - {s['episode']}" if s['episode'] else ""
        lines.append(f"  {s['title']}{ep_str} ({s['progress']}% done, {s['days']} days ago)")

    msg = f"Hey Little Mister, these have been collecting dust on your deck:\n" + "\n".join(lines)
    post_chat(msg)


def cmd_recommend(args):
    """Recommend unwatched items based on recent genre preferences."""
    now = datetime.now(timezone.utc)
    month_ago_ts = int((now - timedelta(days=30)).timestamp())

    try:
        root = plex_get("/status/sessions/history/all", {
            "sort": "viewedAt:desc",
            "viewedAt>": month_ago_ts,
        })
    except Exception as e:
        log.error(f"Recommend: history fetch failed: {e}")
        return

    genre_scores = Counter()
    for video in root.findall(".//Video"):
        if _skip_library(video.get("librarySectionID", "0")):
            continue
        for g in video.findall(".//Genre"):
            genre_scores[g.get("tag")] += 1

    if not genre_scores:
        print("Not enough watch history for recommendations.")
        return

    top_genres = {g for g, _ in genre_scores.most_common(5)}
    candidates = []

    for lib in get_all_libraries():
        if lib["type"] not in ("movie", "show"):
            continue
        try:
            root = plex_get(f"/library/sections/{lib['key']}/unwatched", {"sort": "addedAt:desc"})
        except Exception:
            continue
        for item in root.findall(".//*[@title]"):
            item_genres = {g.get("tag") for g in item.findall(".//Genre")}
            overlap = item_genres & top_genres
            if overlap:
                title = item.get("title", "Unknown")
                year = item.get("year", "")
                rating = item.get("audienceRating", item.get("rating", ""))
                candidates.append({
                    "title": title,
                    "year": year,
                    "rating": rating,
                    "genres": list(item_genres),
                    "score": len(overlap),
                    "library": lib["title"],
                })

    candidates.sort(key=lambda x: (-x["score"], -float(x["rating"] or 0)))
    picks = candidates[:3]

    if not picks:
        print("No unwatched recommendations found matching your taste.")
        return

    lines = ["Based on what you've been watching, you might like:"]
    for p in picks:
        yr = f" ({p['year']})" if p['year'] else ""
        rt = f" [{p['rating']}]" if p['rating'] else ""
        lines.append(f"  - *{p['title']}*{yr}{rt} — {', '.join(p['genres'][:3])}")

    post_chat("\n".join(lines))


def cmd_mood(args):
    """Track genre+time patterns to build emotional rhythm model."""
    now = datetime.now(timezone.utc)
    day_ago_ts = int((now - timedelta(days=1)).timestamp())
    today_str = date.today().isoformat()
    weekday = date.today().strftime("%A")

    try:
        root = plex_get("/status/sessions/history/all", {
            "sort": "viewedAt:desc",
            "viewedAt>": day_ago_ts,
        })
    except Exception as e:
        log.error(f"Mood fetch failed: {e}")
        return

    entries = []
    for video in root.findall(".//Video"):
        if _skip_library(video.get("librarySectionID", "0")):
            continue
        viewed_ts = video.get("viewedAt", "0")
        viewed_dt = ts_to_dt(viewed_ts) if viewed_ts.isdigit() else now
        genres = [g.get("tag") for g in video.findall(".//Genre")]
        hour = viewed_dt.hour
        time_block = "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 17 else "evening" if 17 <= hour < 22 else "late_night"
        entries.append({"genres": genres, "time_block": time_block, "hour": hour})

    mood_data = load_json(MOOD_FILE, {"days": {}})
    mood_data["days"][today_str] = {
        "weekday": weekday,
        "entries": entries,
        "genre_counts": dict(Counter(g for e in entries for g in e["genres"])),
        "time_blocks": dict(Counter(e["time_block"] for e in entries)),
    }

    # Trim to 90 days
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    mood_data["days"] = {k: v for k, v in mood_data["days"].items() if k >= cutoff}

    save_json(MOOD_FILE, mood_data)
    print(f"Mood updated for {today_str}: {len(entries)} viewings tracked.")


def cmd_filmschool(args):
    """Cross-reference new watches with Nova's memory for 'did you know' facts."""
    now = datetime.now(timezone.utc)
    day_ago_ts = int((now - timedelta(days=1)).timestamp())

    try:
        root = plex_get("/status/sessions/history/all", {
            "sort": "viewedAt:desc",
            "viewedAt>": day_ago_ts,
        })
    except Exception as e:
        log.error(f"Filmschool: history fetch failed: {e}")
        return

    titles = []
    for video in root.findall(".//Video"):
        if _skip_library(video.get("librarySectionID", "0")):
            continue
        show = video.get("grandparentTitle", "")
        title = video.get("title", "Unknown")
        director = ""
        for d in video.findall(".//Director"):
            director = d.get("tag", "")
            break
        actors = [r.get("tag") for r in video.findall(".//Role")[:3]]
        titles.append({"show": show, "title": title, "director": director, "actors": actors})

    if not titles:
        return

    try:
        import psycopg2
        conn = psycopg2.connect("dbname=nova_memories")
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
    except Exception as e:
        log.error(f"Filmschool: DB connect failed: {e}")
        return

    posted = 0
    for item in titles[:5]:
        search_terms = [item["show"], item["title"]] + item["actors"]
        search_terms = [t for t in search_terms if t and len(t) > 2]
        if not search_terms:
            continue

        conditions = " OR ".join(["text ILIKE %s"] * len(search_terms))
        params = [f"%{t}%" for t in search_terms]
        try:
            cur.execute(
                f"SELECT text FROM memories WHERE source != 'plex_watch_history' AND ({conditions}) ORDER BY created_at DESC LIMIT 3",
                params,
            )
            rows = cur.fetchall()
        except Exception:
            continue

        if rows:
            snippet = rows[0][0][:200].strip()
            show_str = f"{item['show']} - " if item['show'] else ""
            msg = f"Film school: {show_str}{item['title']}\n{snippet}"
            if len(msg.split("\n")) <= 3:
                post_chat(msg)
                posted += 1
                if posted >= 2:
                    break

    try:
        cur.close()
        conn.close()
    except Exception:
        pass

    if posted == 0:
        print("No cross-references found for today's watches.")


def cmd_shame(args):
    """Weekly roast of abandoned on-deck items (30+ days)."""
    try:
        root = plex_get("/library/onDeck")
    except Exception as e:
        log.error(f"Shame: on-deck fetch failed: {e}")
        return

    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
    abandoned = []
    for video in root.findall(".//Video"):
        if _skip_library(video.get("librarySectionID", "0")):
            continue
        last_viewed = int(video.get("lastViewedAt", "0"))
        if 0 < last_viewed < cutoff_ts:
            title = video.get("grandparentTitle", "") or video.get("title", "Unknown")
            ep = video.get("title", "") if video.get("grandparentTitle") else ""
            days = (datetime.now(timezone.utc) - ts_to_dt(str(last_viewed))).days
            pct = round(int(video.get("viewOffset", 0)) / max(int(video.get("duration", 1)), 1) * 100, 1)
            abandoned.append((title, ep, days, pct))

    if not abandoned:
        print("On-deck is clean. No shame today.")
        return

    roasts = [
        "still waiting for you like a loyal dog at the window",
        "gathering so much dust it qualifies as a historical artifact",
        "abandoned harder than a New Year's resolution",
        "sitting there like a half-eaten sandwich you swore you'd finish",
        "collecting cobwebs and emotional damage",
        "starting to think you two need couples therapy",
    ]

    lines = ["*The Abandoned Pile Shame Board*\n"]
    for i, (title, ep, days, pct) in enumerate(abandoned[:6]):
        ep_str = f" - {ep}" if ep else ""
        roast = roasts[i % len(roasts)]
        lines.append(f"  {title}{ep_str} — {pct}% done, {days} days ago. {roast}.")

    lines.append("\nEither finish them or let them go, Little Mister.")
    post_dm("\n".join(lines))


def cmd_velocity(args):
    """Detect binge watching and late-night sessions."""
    now = datetime.now(timezone.utc)
    today_ts = int((now - timedelta(hours=12)).timestamp())

    try:
        root = plex_get("/status/sessions/history/all", {
            "sort": "viewedAt:desc",
            "viewedAt>": today_ts,
        })
    except Exception as e:
        log.error(f"Velocity: history fetch failed: {e}")
        return

    items = []
    for video in root.findall(".//Video"):
        if _skip_library(video.get("librarySectionID", "0")):
            continue
        viewed_ts = video.get("viewedAt", "0")
        title = video.get("grandparentTitle", "") or video.get("title", "Unknown")
        if viewed_ts.isdigit():
            items.append({"title": title, "ts": int(viewed_ts), "dt": ts_to_dt(viewed_ts)})

    items.sort(key=lambda x: x["ts"])

    # Binge detection: 3+ items within 2-hour windows
    if len(items) >= 3:
        streak = 1
        streak_title = items[0]["title"]
        for i in range(1, len(items)):
            if items[i]["ts"] - items[i-1]["ts"] < 7200:
                streak += 1
            else:
                streak = 1
            streak_title = items[i]["title"]

        if streak >= 3:
            msg = f"Binge alert: {streak} items in a row. Last: {streak_title}. Impressive stamina, Little Mister."
            post_dm(msg)

    # Late-night check
    local_now = datetime.now()
    if local_now.hour >= 0 and local_now.hour < 5:
        playing = load_json(PLAYING_FILE)
        if playing.get("sessions"):
            post_dm("It's past midnight and you're still watching Plex. Go to bed, Little Mister.")
    elif items:
        late_items = [i for i in items if i["dt"].astimezone().hour >= 0 and i["dt"].astimezone().hour < 5]
        if late_items:
            print(f"Late-night viewing detected: {len(late_items)} items after midnight.")

    if not items:
        print("No recent viewing for velocity check.")


def cmd_guest(args):
    """Detect unknown devices/IPs on active sessions."""
    try:
        root = plex_get("/status/sessions")
    except Exception as e:
        log.error(f"Guest: sessions fetch failed: {e}")
        return

    guest_data = load_json(GUEST_FILE, {"known_devices": {}, "sessions": []})

    for video in root.findall(".//Video"):
        player = video.find(".//Player")
        user = video.find(".//User")
        if player is None:
            continue
        device_id = player.get("machineIdentifier", "unknown")
        session = {
            "device": player.get("device", "unknown"),
            "title": player.get("title", "unknown"),
            "address": player.get("address", "unknown"),
            "user": user.get("title", "unknown") if user is not None else "unknown",
            "machine_id": device_id,
            "seen_at": datetime.now().isoformat(),
            "watching": video.get("grandparentTitle", "") or video.get("title", "Unknown"),
        }

        if device_id not in guest_data["known_devices"]:
            guest_data["known_devices"][device_id] = {
                "first_seen": session["seen_at"],
                "device": session["device"],
                "title": session["title"],
                "user": session["user"],
                "count": 0,
                "flagged": True,
            }
            log.info(f"New device detected: {session['device']} ({session['address']})")
            print(f"NEW DEVICE: {session['device']} / {session['title']} from {session['address']} (user: {session['user']})")

        guest_data["known_devices"][device_id]["count"] = guest_data["known_devices"][device_id].get("count", 0) + 1
        guest_data["known_devices"][device_id]["last_seen"] = session["seen_at"]

        guest_data["sessions"].append(session)

    # Trim session log to last 500
    guest_data["sessions"] = guest_data["sessions"][-500:]
    save_json(GUEST_FILE, guest_data)

    known_count = len(guest_data["known_devices"])
    flagged = sum(1 for d in guest_data["known_devices"].values() if d.get("flagged"))
    print(f"Guest tracker: {known_count} known devices ({flagged} flagged as new)")


def cmd_rewatch(args):
    """Track items watched multiple times, build canon list."""
    canon = load_json(CANON_FILE, {"items": {}, "updated": ""})

    # Pull all-time history (last 365 days max)
    year_ago_ts = int((datetime.now(timezone.utc) - timedelta(days=365)).timestamp())
    try:
        root = plex_get("/status/sessions/history/all", {
            "sort": "viewedAt:desc",
            "viewedAt>": year_ago_ts,
        })
    except Exception as e:
        log.error(f"Rewatch: history fetch failed: {e}")
        return

    watch_counts = Counter()
    for video in root.findall(".//Video"):
        if _skip_library(video.get("librarySectionID", "0")):
            continue
        title = video.get("grandparentTitle", "") or video.get("title", "Unknown")
        watch_counts[title] += 1

    new_canon_entries = []
    for title, count in watch_counts.items():
        prev = canon["items"].get(title, {}).get("count", 0)
        canon["items"][title] = {"count": count, "updated": date.today().isoformat()}
        if count >= 3 and prev < 3:
            new_canon_entries.append((title, count))

    canon["updated"] = datetime.now().isoformat()
    save_json(CANON_FILE, canon)

    rewatched = [(t, d["count"]) for t, d in canon["items"].items() if d["count"] >= 2]
    rewatched.sort(key=lambda x: -x[1])

    print(f"Rewatch index: {len(rewatched)} items with 2+ views")
    if rewatched[:5]:
        for t, c in rewatched[:5]:
            print(f"  {t}: {c}x")

    if new_canon_entries:
        lines = ["*New additions to Jordan's Canon* (3+ rewatches):"]
        for title, count in new_canon_entries:
            lines.append(f"  - {title} ({count}x)")
        post_chat("\n".join(lines))


def cmd_seasonal(args):
    """Track genre distribution by month for seasonal pattern detection."""
    now = datetime.now(timezone.utc)
    month_str = now.strftime("%Y-%m")
    month_start_ts = int(now.replace(day=1, hour=0, minute=0, second=0).timestamp())

    try:
        root = plex_get("/status/sessions/history/all", {
            "sort": "viewedAt:desc",
            "viewedAt>": month_start_ts,
        })
    except Exception as e:
        log.error(f"Seasonal: history fetch failed: {e}")
        return

    genre_counter = Counter()
    total = 0
    for video in root.findall(".//Video"):
        if _skip_library(video.get("librarySectionID", "0")):
            continue
        total += 1
        for g in video.findall(".//Genre"):
            genre_counter[g.get("tag")] += 1

    seasonal_data = load_json(SEASONAL_FILE, {"months": {}})
    seasonal_data["months"][month_str] = {
        "total_items": total,
        "genres": dict(genre_counter.most_common(20)),
        "updated": datetime.now().isoformat(),
    }

    save_json(SEASONAL_FILE, seasonal_data)

    # Detect patterns if 3+ months of data
    months = sorted(seasonal_data["months"].keys())
    if len(months) >= 3:
        all_genres = set()
        for m in months:
            all_genres.update(seasonal_data["months"][m].get("genres", {}).keys())

        drift = {}
        for genre in all_genres:
            counts = [seasonal_data["months"][m].get("genres", {}).get(genre, 0) for m in months]
            if max(counts) > 0:
                recent_avg = sum(counts[-3:]) / 3
                older_avg = sum(counts[:-3]) / max(len(counts) - 3, 1) if len(counts) > 3 else recent_avg
                if older_avg > 0:
                    drift[genre] = round((recent_avg - older_avg) / older_avg * 100, 1)

        trending_up = [(g, d) for g, d in drift.items() if d > 30]
        trending_down = [(g, d) for g, d in drift.items() if d < -30]

        if trending_up or trending_down:
            lines = [f"*Seasonal Drift Report* ({len(months)} months tracked)"]
            if trending_up:
                trending_up.sort(key=lambda x: -x[1])
                lines.append("Trending up: " + ", ".join(f"{g} (+{d}%)" for g, d in trending_up[:5]))
            if trending_down:
                trending_down.sort(key=lambda x: x[1])
                lines.append("Trending down: " + ", ".join(f"{g} ({d}%)" for g, d in trending_down[:5]))
            post("\n".join(lines))

    print(f"Seasonal data updated for {month_str}: {total} items, {len(genre_counter)} genres")


# ── Main ──────────────────────────────────────────────────────────────────────

COMMANDS = {
    "history": (cmd_history, "Ingest yesterday's watch history into vector memory"),
    "playing": (cmd_playing, "Check what's currently playing on Plex"),
    "stats": (cmd_stats, "Weekly viewing pattern analysis"),
    "sync": (cmd_sync, "Compare Plex library contents against disk"),
    "ondeck": (cmd_ondeck, "Check stale on-deck items (30+ days)"),
    "recommend": (cmd_recommend, "Recommend unwatched items based on taste"),
    "mood": (cmd_mood, "Track genre/time mood patterns"),
    "filmschool": (cmd_filmschool, "Cross-reference watches with Nova's memory"),
    "shame": (cmd_shame, "Weekly roast of abandoned on-deck items"),
    "velocity": (cmd_velocity, "Binge detection and late-night nudges"),
    "guest": (cmd_guest, "Detect unknown devices on Plex"),
    "rewatch": (cmd_rewatch, "Track rewatch counts and build canon list"),
    "seasonal": (cmd_seasonal, "Genre distribution by month / seasonal drift"),
}


def main():
    global QUIET

    parser = argparse.ArgumentParser(description="Nova Plex integration")
    parser.add_argument("command", choices=COMMANDS.keys(), help="Subcommand to run")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress Slack posting (print only)")
    args = parser.parse_args()

    QUIET = args.quiet
    WORKSPACE.mkdir(parents=True, exist_ok=True)

    log.info(f"Running: {args.command}")
    try:
        func, _ = COMMANDS[args.command]
        func(args)
    except urllib.error.URLError as e:
        log.error(f"Plex connection failed: {e}")
        print(f"Error: Cannot reach Plex at {PLEX_URL} — {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.error(f"Unhandled error in {args.command}: {e}", exc_info=True)
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
