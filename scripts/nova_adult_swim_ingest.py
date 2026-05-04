#!/usr/bin/env python3
"""
nova_adult_swim_ingest.py — Ingest Adult Swim / Cartoon Network show episode data
into Nova's vector memory via Wikipedia API.

Shows:
  - Perfect Hair Forever (2004-2014)
  - Aqua Teen Hunger Force (2000-2023, all name variants)
  - The Brak Show (2000-2003)
  - Sealab 2021 (2000-2005)
  - Space Ghost Coast to Coast (1994-2008)

Sources: Wikipedia API (free, no key needed)
Pipeline: Wikipedia parse → structured episodes → vector memory (POST /remember?async=1)
Notifications: Slack #nova-notifications every 5 minutes

Usage:
  python3 nova_adult_swim_ingest.py
  python3 nova_adult_swim_ingest.py --show "aqua_teen"
  python3 nova_adult_swim_ingest.py --dry-run

Written by Jordan Koch.
"""

import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread, Event

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

VECTOR_URL = "http://127.0.0.1:18790/remember?async=1"
STATUS_INTERVAL = 300  # 5 minutes
WIKI_API = "https://en.wikipedia.org/w/api.php"
LOG_FILE = Path("/tmp/nova-adult-swim-ingest.log")

# Show definitions: source tag, Wikipedia episode list pages, show metadata
SHOWS = {
    "perfect_hair_forever": {
        "source": "tv_perfect_hair_forever",
        "title": "Perfect Hair Forever",
        "network": "Adult Swim",
        "years": "2004-2014",
        "creators": ["Matt Harrigan", "Dave Willis", "Jim Fortier"],
        "genre": ["Absurdist humor", "Surreal comedy", "Parody"],
        "wiki_pages": [
            "Perfect Hair Forever",
        ],
        "episode_list_page": "Perfect_Hair_Forever",
    },
    "aqua_teen": {
        "source": "tv_aqua_teen",
        "title": "Aqua Teen Hunger Force",
        "network": "Adult Swim",
        "years": "2000-2023",
        "creators": ["Dave Willis", "Matt Maiellaro"],
        "genre": ["Absurdist humor", "Surreal comedy", "Dark comedy"],
        "aliases": [
            "Aqua Teen Hunger Force",
            "Aqua Unit Patrol Squad 1",
            "Aqua Something You Know Whatever",
            "Aqua TV Show Show",
            "Aqua Teen Hunger Force Forever",
        ],
        "wiki_pages": [
            "List of Aqua Teen Hunger Force episodes",
        ],
        "episode_list_page": "List_of_Aqua_Teen_Hunger_Force_episodes",
    },
    "brak_show": {
        "source": "tv_brak_show",
        "title": "The Brak Show",
        "network": "Adult Swim",
        "years": "2000-2003",
        "creators": ["Jim Fortier", "Pete Smith"],
        "genre": ["Absurdist humor", "Sitcom parody", "Surreal comedy"],
        "wiki_pages": [
            "The Brak Show",
        ],
        "episode_list_page": "The_Brak_Show",
    },
    "sealab_2021": {
        "source": "tv_sealab_2021",
        "title": "Sealab 2021",
        "network": "Adult Swim",
        "years": "2000-2005",
        "creators": ["Adam Reed", "Matt Thompson"],
        "genre": ["Absurdist humor", "Parody", "Satire"],
        "wiki_pages": [
            "List of Sealab 2021 episodes",
            "Sealab 2021",
        ],
        "episode_list_page": "List_of_Sealab_2021_episodes",
    },
    "space_ghost_c2c": {
        "source": "tv_space_ghost_c2c",
        "title": "Space Ghost Coast to Coast",
        "network": "Cartoon Network / Adult Swim",
        "years": "1994-2008",
        "creators": ["Mike Lazzo"],
        "genre": ["Talk show parody", "Absurdist humor", "Surreal comedy"],
        "wiki_pages": [
            "List of Space Ghost Coast to Coast episodes",
        ],
        "episode_list_page": "List_of_Space_Ghost_Coast_to_Coast_episodes",
    },
}

# ── Globals ───────────────────────────────────────────────────────────────────

shutdown = Event()
dry_run = False

stats = {
    "total_shows": 0,
    "current_show": "",
    "episodes_found": 0,
    "memories_stored": 0,
    "errors": 0,
    "start_time": 0,
    "shows_completed": 0,
    "per_show": {},
}


def signal_handler(sig, frame):
    shutdown.set()
    log("Shutdown requested, finishing current operation...")


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[adult_swim {ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def slack_post(text):
    if dry_run:
        log(f"[DRY RUN] Would post to Slack: {text[:100]}...")
        return
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)


# ── Vector Memory ─────────────────────────────────────────────────────────────

def vector_remember(text, source, metadata):
    if dry_run:
        log(f"  [DRY RUN] Would store: {text[:80]}...")
        stats["memories_stored"] += 1
        return True

    payload = json.dumps({
        "text": text[:2000],
        "source": source,
        "metadata": metadata,
    }).encode()
    try:
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=15)
        stats["memories_stored"] += 1
        return True
    except Exception as e:
        log(f"  Memory write failed: {e}")
        stats["errors"] += 1
        return False


# ── Wikipedia API ─────────────────────────────────────────────────────────────

def wiki_get_page_html(page_title):
    """Fetch parsed HTML content of a Wikipedia page."""
    params = {
        "action": "parse",
        "page": page_title,
        "prop": "text",
        "format": "json",
        "formatversion": "2",
    }
    url = f"{WIKI_API}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "NovaBotMemoryIngest/1.0 (jordan@digitalnoise.net)"
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data.get("parse", {}).get("text", "")
    except Exception as e:
        log(f"  Wikipedia fetch failed for '{page_title}': {e}")
        return ""


def wiki_get_page_wikitext(page_title):
    """Fetch raw wikitext of a Wikipedia page (better for table parsing)."""
    params = {
        "action": "parse",
        "page": page_title,
        "prop": "wikitext",
        "format": "json",
        "formatversion": "2",
    }
    url = f"{WIKI_API}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "NovaBotMemoryIngest/1.0 (jordan@digitalnoise.net)"
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data.get("parse", {}).get("wikitext", "")
    except Exception as e:
        log(f"  Wikipedia wikitext fetch failed for '{page_title}': {e}")
        return ""


def wiki_get_sections(page_title):
    """Fetch section list to locate episode tables."""
    params = {
        "action": "parse",
        "page": page_title,
        "prop": "sections",
        "format": "json",
        "formatversion": "2",
    }
    url = f"{WIKI_API}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "NovaBotMemoryIngest/1.0 (jordan@digitalnoise.net)"
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data.get("parse", {}).get("sections", [])
    except Exception as e:
        log(f"  Wikipedia sections fetch failed for '{page_title}': {e}")
        return []


def wiki_get_section_html(page_title, section_index):
    """Fetch HTML of a specific section."""
    params = {
        "action": "parse",
        "page": page_title,
        "prop": "text",
        "section": str(section_index),
        "format": "json",
        "formatversion": "2",
    }
    url = f"{WIKI_API}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "NovaBotMemoryIngest/1.0 (jordan@digitalnoise.net)"
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data.get("parse", {}).get("text", "")
    except Exception as e:
        log(f"  Wikipedia section fetch failed: {e}")
        return ""


# ── HTML Table Parser (stdlib only) ──────────────────────────────────────────

_MONTHS = ["", "January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]


def _month_name(n):
    return _MONTHS[n] if 1 <= n <= 12 else str(n)


def _extract_field_value(text):
    """Extract a wikitext field value, respecting nested {{ }} templates.
    Stops at newline followed by | (next field) or end of template."""
    result = []
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if text[i:i+2] == '{{':
            depth += 1
            result.append('{{')
            i += 2
            continue
        elif text[i:i+2] == '}}':
            if depth > 0:
                depth -= 1
                result.append('}}')
                i += 2
                continue
            else:
                break  # End of outer template
        elif ch == '|' and depth == 0:
            # Check if this is a field separator (preceded by newline or at start)
            # Look back for newline
            preceding = ''.join(result)
            if preceding.endswith('\n') or preceding.rstrip().endswith('\n'):
                break
            # Also break on | preceded by whitespace-only on current line
            last_newline = preceding.rfind('\n')
            if last_newline >= 0:
                after_nl = preceding[last_newline+1:]
                if after_nl.strip() == '':
                    break
            # First | at depth 0 with no content yet = field separator
            if not preceding.strip():
                break
            result.append(ch)
        elif ch == '\n':
            # Check if next non-space is |
            j = i + 1
            while j < len(text) and text[j] in ' \t':
                j += 1
            if j < len(text) and text[j] == '|':
                break
            result.append(ch)
        else:
            result.append(ch)
        i += 1
    return ''.join(result).strip()


def strip_html(html_str):
    """Remove HTML tags, decode entities."""
    text = re.sub(r'<br\s*/?>', ' ', html_str)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&#160;', ' ').replace('&nbsp;', ' ')
    text = text.replace('&#39;', "'").replace('&quot;', '"')
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def parse_episode_tables(html_content):
    """Parse episode rows from Wikipedia HTML tables.

    Returns list of dicts with keys like:
      episode_number, title, season, directed_by, written_by,
      air_date, viewers, description, guests
    """
    episodes = []

    # Find all episode list tables (class="wikiepisodetable" or similar)
    table_pattern = re.compile(
        r'<table[^>]*class="[^"]*(?:wikiepisodetable|wikitable)[^"]*"[^>]*>(.*?)</table>',
        re.DOTALL | re.IGNORECASE
    )
    tables = table_pattern.findall(html_content)

    if not tables:
        # Fallback: find any table with episode-like content
        table_pattern = re.compile(r'<table[^>]*>(.*?)</table>', re.DOTALL | re.IGNORECASE)
        tables = table_pattern.findall(html_content)

    for table_html in tables:
        # Get headers
        header_row = re.search(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL)
        if not header_row:
            continue

        headers = [strip_html(h).lower() for h in re.findall(r'<th[^>]*>(.*?)</th>', header_row.group(1), re.DOTALL)]
        if not headers:
            continue

        # Must look like an episode table
        header_str = ' '.join(headers)
        if not any(kw in header_str for kw in ['episode', 'title', '#', 'no.']):
            continue

        # Parse rows
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL)
        current_season = 0

        for row_html in rows[1:]:  # Skip header row
            cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row_html, re.DOTALL)
            if not cells:
                continue

            # Check for season header row (often spans multiple columns)
            if re.search(r'rowspan|colspan', row_html) and len(cells) < len(headers) - 1:
                season_match = re.search(r'[Ss]eason\s*(\d+)', strip_html(' '.join(cells)))
                if season_match:
                    current_season = int(season_match.group(1))
                continue

            ep = {"season": current_season}

            # Map cells to headers (handle colspan/rowspan misalignment)
            for i, cell in enumerate(cells):
                if i >= len(headers):
                    break
                value = strip_html(cell)
                header = headers[i]

                if 'no' in header or '#' in header or header == 'episode':
                    try:
                        ep["episode_number"] = int(re.search(r'\d+', value).group())
                    except (AttributeError, ValueError):
                        pass
                elif 'title' in header:
                    # Clean up title (remove quotes)
                    value = value.strip('""“”')
                    ep["title"] = value
                elif 'direct' in header:
                    ep["directed_by"] = value
                elif 'writ' in header or 'written' in header:
                    ep["written_by"] = value
                elif 'air' in header or 'date' in header:
                    ep["air_date"] = value
                elif 'view' in header:
                    ep["viewers"] = value
                elif 'guest' in header:
                    ep["guests"] = value
                elif 'description' in header or 'summary' in header or 'plot' in header:
                    ep["description"] = value
                elif 'prod' in header:
                    ep["production_code"] = value
                elif 'season' in header:
                    try:
                        ep["season"] = int(re.search(r'\d+', value).group())
                    except (AttributeError, ValueError):
                        pass

            if ep.get("title") or ep.get("episode_number"):
                episodes.append(ep)

    return episodes


def _find_balanced_templates(wikitext, template_prefix):
    """Find all {{template_prefix...}} blocks respecting nested {{ }}."""
    results = []
    search_str = '{{' + template_prefix
    start = 0
    while True:
        idx = wikitext.lower().find(search_str.lower(), start)
        if idx < 0:
            break
        # Find matching closing }}
        depth = 0
        i = idx
        while i < len(wikitext):
            if wikitext[i:i+2] == '{{':
                depth += 1
                i += 2
            elif wikitext[i:i+2] == '}}':
                depth -= 1
                i += 2
                if depth == 0:
                    results.append((idx, wikitext[idx:i]))
                    break
            else:
                i += 1
        start = idx + 2
    return results


def _clean_wiki_value(value):
    """Clean a wiki template field value into plain text."""
    # Convert [[Link|Display]] or [[Display]] to text
    value = re.sub(r'\[\[([^|\]]*\|)?([^\]]*)\]\]', r'\2', value)
    # Convert {{Start date|YYYY|MM|DD}} to readable date
    value = re.sub(
        r'\{\{Start date\|(\d{4})\|(\d{1,2})\|(\d{1,2})\}\}',
        lambda m: f"{_month_name(int(m.group(2)))} {int(m.group(3))}, {m.group(1)}",
        value, flags=re.IGNORECASE
    )
    # Convert {{nowrap|text}} to just text
    value = re.sub(r'\{\{nowrap\|([^}]*)\}\}', r'\1', value, flags=re.IGNORECASE)
    # Convert {{anchor|text}} to just text
    value = re.sub(r'\{\{anchor\|([^}]*)\}\}', r'\1', value, flags=re.IGNORECASE)
    # Remove {{Cite web ...}} and similar
    value = re.sub(r'\{\{Cite[^}]*?\}\}', '', value, flags=re.IGNORECASE)
    # Remove remaining templates
    value = re.sub(r'\{\{[^}]*?\}\}', '', value)
    # Remove HTML comments
    value = re.sub(r'<!--.*?-->', '', value, flags=re.DOTALL)
    # Remove bold/italic markup
    value = re.sub(r"'''?", '', value)
    # Remove HTML tags
    value = re.sub(r'<[^>]+>', ' ', value)
    # Collapse whitespace
    value = re.sub(r'\s+', ' ', value).strip()
    return value


def parse_episode_rows_from_wikitext(wikitext, show_title):
    """Parse episode info from raw wikitext (handles Episode list/sublist templates)."""
    episodes = []

    # Find all Episode list templates (including /sublist variant)
    templates = _find_balanced_templates(wikitext, 'Episode list')

    fields_map = {
        "EpisodeNumber": "episode_number",
        "EpisodeNumber2": "episode_in_season",
        "Title": "title",
        "DirectedBy": "directed_by",
        "WrittenBy": "written_by",
        "OriginalAirDate": "air_date",
        "Viewers": "viewers",
        "ShortSummary": "description",
        "Aux1": "guests",
        "Aux2": "production_code",
        "Aux4": "guests",
        "ProdCode": "production_code",
    }

    for pos, template in templates:
        ep = {"season": 0}

        for wiki_field, ep_field in fields_map.items():
            pattern = r'\|\s*' + re.escape(wiki_field) + r'\s*=\s*'
            field_match = re.search(pattern, template, re.IGNORECASE)
            if field_match:
                rest = template[field_match.end():]
                value = _extract_field_value(rest)
                value = _clean_wiki_value(value)

                if ep_field == "episode_number":
                    try:
                        ep[ep_field] = int(re.search(r'\d+', value).group())
                    except (AttributeError, ValueError):
                        pass
                elif ep_field == "title":
                    ep[ep_field] = value.strip('"').strip()
                else:
                    if value:
                        ep[ep_field] = value

        if ep.get("title") or ep.get("episode_number"):
            episodes.append(ep)

    # Assign seasons based on section headers in the wikitext
    season_pattern = re.compile(r'==+\s*Season\s*(\d+)', re.IGNORECASE)
    season_positions = [(m.start(), int(m.group(1))) for m in season_pattern.finditer(wikitext)]

    if season_positions:
        for i, (pos, template) in enumerate(templates):
            if i < len(episodes):
                for s_pos, s_num in reversed(season_positions):
                    if pos > s_pos:
                        if episodes[i].get("season", 0) == 0:
                            episodes[i]["season"] = s_num
                        break

    return episodes


def extract_show_overview(html_content, show_title):
    """Extract the show overview/description paragraphs before the episode list."""
    # Get text between start and first heading (or table)
    intro_match = re.search(
        r'<div class="mw-parser-output">(.*?)(?:<h[23]|<table)',
        html_content, re.DOTALL
    )
    if intro_match:
        paragraphs = re.findall(r'<p>(.*?)</p>', intro_match.group(1), re.DOTALL)
        text_parts = [strip_html(p) for p in paragraphs if len(strip_html(p)) > 30]
        return '\n'.join(text_parts[:5])
    return ""


# ── Show-Specific Parsers ────────────────────────────────────────────────────

def parse_perfect_hair_forever(show_config):
    """PHF is only 9 episodes (2 seasons). Simple page structure."""
    log("  Fetching Perfect Hair Forever episode data...")
    html = wiki_get_page_html("Perfect_Hair_Forever")
    wikitext = wiki_get_page_wikitext("Perfect_Hair_Forever")

    episodes = parse_episode_rows_from_wikitext(wikitext, "Perfect Hair Forever")
    if not episodes:
        episodes = parse_episode_tables(html)

    # PHF has limited Wikipedia data — supplement with known episode list
    known_episodes = [
        {"season": 1, "episode_number": 1, "title": "Pilot", "air_date": "November 7, 2004",
         "description": "Uncle Grandfather sends the young hero on a quest for perfect hair, encountering bizarre characters along the way."},
        {"season": 1, "episode_number": 2, "title": "Musclechest", "air_date": "December 5, 2004",
         "description": "The quest continues as the hero encounters Musclechest and Action Hot Dog."},
        {"season": 1, "episode_number": 3, "title": "Toro", "air_date": "February 6, 2005",
         "description": "A bull-themed episode featuring Model Robot and Toro."},
        {"season": 1, "episode_number": 4, "title": "CGI", "air_date": "April 10, 2005",
         "description": "The show shifts to computer-generated animation, breaking the fourth wall extensively."},
        {"season": 1, "episode_number": 5, "title": "Astronaut", "air_date": "June 12, 2005",
         "description": "Space travel and astronaut themes dominate as the quest nears its climax."},
        {"season": 1, "episode_number": 6, "title": "Land", "air_date": "August 14, 2005",
         "description": "The hero returns to land for the season finale."},
        {"season": 1, "episode_number": 7, "title": "Lineage", "air_date": "October 23, 2005",
         "description": "Family history and lineage are explored in surreal fashion."},
        {"season": 1, "episode_number": 8, "title": "Time", "air_date": "December 18, 2005",
         "description": "Time manipulation creates chaos across the show's universe."},
        {"season": 2, "episode_number": 9, "title": "2 – Part 1", "air_date": "November 23, 2014",
         "description": "The long-awaited second season premiere, picking up where season 1 left off after a 9-year hiatus."},
    ]

    # Merge Wikipedia data with known data (prefer Wikipedia where available)
    if len(episodes) < len(known_episodes):
        merged = known_episodes.copy()
        for wiki_ep in episodes:
            for i, known in enumerate(merged):
                if (wiki_ep.get("episode_number") == known.get("episode_number") and
                        wiki_ep.get("season", 1) == known.get("season", 1)):
                    merged[i].update({k: v for k, v in wiki_ep.items() if v})
                    break
        episodes = merged

    return episodes, extract_show_overview(html, "Perfect Hair Forever")


def parse_aqua_teen(show_config):
    """ATHF has 12 seasons across multiple name changes. Episodes are in season sub-pages."""
    log("  Fetching Aqua Teen Hunger Force episode data...")

    # Episodes are transcluded from per-season sub-pages
    season_pages = [
        (1, "Aqua_Teen_Hunger_Force_season_1"),
        (2, "Aqua_Teen_Hunger_Force_season_2"),
        (3, "Aqua_Teen_Hunger_Force_season_3"),
        (4, "Aqua_Teen_Hunger_Force_season_4"),
        (5, "Aqua_Teen_Hunger_Force_season_5"),
        (6, "Aqua_Teen_Hunger_Force_season_6"),
        (7, "Aqua_Teen_Hunger_Force_season_7"),
        (8, "Aqua_Teen_Hunger_Force_season_8"),
        (9, "Aqua_Teen_Hunger_Force_season_9"),
        (10, "Aqua_Teen_Hunger_Force_season_10"),
        (11, "Aqua_Teen_Hunger_Force_season_11"),
        (12, "Aqua_Teen_Hunger_Force_season_12"),
    ]

    all_episodes = []
    for season_num, page_name in season_pages:
        log(f"    Fetching season {season_num}...")
        wikitext = wiki_get_page_wikitext(page_name)
        if not wikitext:
            # Try HTML fallback
            html = wiki_get_page_html(page_name)
            season_eps = parse_episode_tables(html)
        else:
            season_eps = parse_episode_rows_from_wikitext(wikitext, "Aqua Teen Hunger Force")

        for ep in season_eps:
            ep["season"] = season_num
        all_episodes.extend(season_eps)
        time.sleep(0.5)  # Rate limit Wikipedia

    # Get show overview from main page
    main_html = wiki_get_page_html("Aqua_Teen_Hunger_Force")
    overview = extract_show_overview(main_html, "Aqua Teen Hunger Force")

    return all_episodes, overview


def parse_brak_show(show_config):
    """The Brak Show — 2 seasons, 28 episodes."""
    log("  Fetching The Brak Show episode data...")
    wikitext = wiki_get_page_wikitext("The_Brak_Show")
    html = wiki_get_page_html("The_Brak_Show")

    episodes = parse_episode_rows_from_wikitext(wikitext, "The Brak Show")
    if not episodes:
        episodes = parse_episode_tables(html)

    overview = extract_show_overview(html, "The Brak Show")
    return episodes, overview


def parse_sealab_2021(show_config):
    """Sealab 2021 — 4 seasons, 52 episodes."""
    log("  Fetching Sealab 2021 episode data...")
    wikitext = wiki_get_page_wikitext("List_of_Sealab_2021_episodes")
    html = wiki_get_page_html("List_of_Sealab_2021_episodes")

    episodes = parse_episode_rows_from_wikitext(wikitext, "Sealab 2021")
    if not episodes:
        episodes = parse_episode_tables(html)

    # Get show overview
    main_html = wiki_get_page_html("Sealab_2021")
    overview = extract_show_overview(main_html, "Sealab 2021")
    return episodes, overview


def parse_space_ghost(show_config):
    """Space Ghost Coast to Coast — 11 seasons, 110+ episodes + specials."""
    log("  Fetching Space Ghost Coast to Coast episode data...")
    wikitext = wiki_get_page_wikitext("List_of_Space_Ghost_Coast_to_Coast_episodes")
    html = wiki_get_page_html("List_of_Space_Ghost_Coast_to_Coast_episodes")

    episodes = parse_episode_rows_from_wikitext(wikitext, "Space Ghost Coast to Coast")
    if not episodes:
        episodes = parse_episode_tables(html)

    # Get show overview
    main_html = wiki_get_page_html("Space_Ghost_Coast_to_Coast")
    overview = extract_show_overview(main_html, "Space Ghost Coast to Coast")
    return episodes, overview


SHOW_PARSERS = {
    "perfect_hair_forever": parse_perfect_hair_forever,
    "aqua_teen": parse_aqua_teen,
    "brak_show": parse_brak_show,
    "sealab_2021": parse_sealab_2021,
    "space_ghost_c2c": parse_space_ghost,
}


# ── Memory Construction ──────────────────────────────────────────────────────

def build_show_overview_memory(show_key, show_config, overview_text):
    """Create a show overview memory entry."""
    aliases = show_config.get("aliases", [])
    alias_str = f" Also known as: {', '.join(aliases)}." if aliases else ""

    text = (
        f"{show_config['title']} ({show_config['years']}) — "
        f"{show_config['network']}. "
        f"Created by {', '.join(show_config['creators'])}. "
        f"Genre: {', '.join(show_config['genre'])}."
        f"{alias_str}\n\n"
        f"{overview_text}"
    )

    metadata = {
        "type": "show_overview",
        "show": show_config["title"],
        "network": show_config["network"],
        "years": show_config["years"],
        "creators": show_config["creators"],
        "genre": show_config["genre"],
    }
    if aliases:
        metadata["aliases"] = aliases

    return text, metadata


def build_episode_memory(show_config, episode):
    """Create a memory entry for a single episode."""
    show = show_config["title"]
    season = episode.get("season", 0)
    ep_num = episode.get("episode_number", 0)
    title = episode.get("title", "Unknown")

    # Build readable text
    ep_label = f"S{season:02d}E{ep_num:02d}" if season and ep_num else f"#{ep_num}"
    parts = [f"{show} {ep_label}: \"{title}\""]

    if episode.get("air_date"):
        parts.append(f"Aired: {episode['air_date']}")
    if episode.get("directed_by"):
        parts.append(f"Directed by: {episode['directed_by']}")
    if episode.get("written_by"):
        parts.append(f"Written by: {episode['written_by']}")
    if episode.get("guests"):
        parts.append(f"Guests: {episode['guests']}")
    if episode.get("description"):
        parts.append(f"Synopsis: {episode['description']}")
    if episode.get("viewers"):
        parts.append(f"Viewers: {episode['viewers']} million")
    if episode.get("production_code"):
        parts.append(f"Production code: {episode['production_code']}")

    text = ". ".join(parts)

    metadata = {
        "type": "episode",
        "show": show,
        "season": season,
        "episode": ep_num,
        "title": title,
        "network": show_config["network"],
        "category": "adult_swim",
    }
    if episode.get("air_date"):
        metadata["air_date"] = episode["air_date"]
    if episode.get("directed_by"):
        metadata["directed_by"] = episode["directed_by"]
    if episode.get("written_by"):
        metadata["written_by"] = episode["written_by"]
    if episode.get("guests"):
        metadata["guests"] = episode["guests"]

    return text, metadata


def build_season_summary_memory(show_config, season_num, season_episodes):
    """Create a season summary memory."""
    show = show_config["title"]
    ep_titles = [ep.get("title", "?") for ep in season_episodes]

    dates = [ep.get("air_date", "") for ep in season_episodes if ep.get("air_date")]
    date_range = f"{dates[0]} to {dates[-1]}" if len(dates) >= 2 else (dates[0] if dates else "unknown")

    text = (
        f"{show} Season {season_num} — {len(season_episodes)} episodes ({date_range}). "
        f"Episodes: {', '.join(ep_titles[:20])}"
    )
    if len(ep_titles) > 20:
        text += f" ... and {len(ep_titles) - 20} more"

    metadata = {
        "type": "season_summary",
        "show": show,
        "season": season_num,
        "episode_count": len(season_episodes),
        "category": "adult_swim",
    }

    return text, metadata


# ── Status Reporter ──────────────────────────────────────────────────────────

def status_reporter():
    """Post progress to Slack every 5 minutes."""
    while not shutdown.is_set():
        shutdown.wait(STATUS_INTERVAL)
        if shutdown.is_set():
            break
        post_status()


def post_status():
    elapsed = time.time() - stats["start_time"]
    elapsed_str = str(timedelta(seconds=int(elapsed)))

    show_progress = []
    for show_key, show_stats in stats["per_show"].items():
        title = SHOWS[show_key]["title"]
        ep_count = show_stats.get("episodes", 0)
        stored = show_stats.get("stored", 0)
        status = "done" if show_stats.get("done") else "in progress" if show_key == stats["current_show"] else "pending"
        show_progress.append(f"  :tv: {title}: {stored}/{ep_count} episodes ({status})")

    msg = (
        f":brain: *Adult Swim Episode Ingest — Status Update*\n"
        f"  Shows: {stats['shows_completed']}/{stats['total_shows']} complete\n"
        f"  Episodes found: {stats['episodes_found']}\n"
        f"  Memories stored: {stats['memories_stored']}\n"
        f"  Elapsed: {elapsed_str}\n"
        f"  Current: {SHOWS.get(stats['current_show'], {}).get('title', 'N/A')}\n"
    )
    if show_progress:
        msg += "\n" + "\n".join(show_progress)
    if stats["errors"]:
        msg += f"\n  Errors: {stats['errors']}"

    slack_post(msg)
    log(f"Status posted: {stats['memories_stored']} memories, {stats['shows_completed']}/{stats['total_shows']} shows")


# ── Main Ingest Logic ────────────────────────────────────────────────────────

def ingest_show(show_key, show_config):
    """Ingest a single show's episodes into vector memory."""
    stats["current_show"] = show_key
    stats["per_show"][show_key] = {"episodes": 0, "stored": 0, "done": False}

    log(f"\n{'='*60}")
    log(f"Processing: {show_config['title']} ({show_config['years']})")
    log(f"{'='*60}")

    # Parse episodes from Wikipedia
    parser = SHOW_PARSERS.get(show_key)
    if not parser:
        log(f"  No parser for {show_key}")
        stats["errors"] += 1
        return

    episodes, overview = parser(show_config)
    time.sleep(1)  # Polite rate limiting for Wikipedia

    if not episodes:
        log(f"  WARNING: No episodes parsed for {show_config['title']}")
        log(f"  Falling back to HTML table parsing...")
        html = wiki_get_page_html(show_config["episode_list_page"])
        episodes = parse_episode_tables(html)
        time.sleep(1)

    log(f"  Found {len(episodes)} episodes")
    stats["episodes_found"] += len(episodes)
    stats["per_show"][show_key]["episodes"] = len(episodes)

    source = show_config["source"]
    stored = 0

    # Store show overview
    if overview:
        text, meta = build_show_overview_memory(show_key, show_config, overview)
        if vector_remember(text, source, meta):
            stored += 1
        time.sleep(0.05)

    # Store individual episodes
    for ep in episodes:
        if shutdown.is_set():
            break
        text, meta = build_episode_memory(show_config, ep)
        if vector_remember(text, source, meta):
            stored += 1
        time.sleep(0.02)  # Gentle rate limit on memory server

    # Store season summaries
    seasons = {}
    for ep in episodes:
        s = ep.get("season", 0)
        if s:
            seasons.setdefault(s, []).append(ep)

    for season_num, season_eps in sorted(seasons.items()):
        if shutdown.is_set():
            break
        text, meta = build_season_summary_memory(show_config, season_num, season_eps)
        if vector_remember(text, source, meta):
            stored += 1
        time.sleep(0.02)

    stats["per_show"][show_key]["stored"] = stored
    stats["per_show"][show_key]["done"] = True
    stats["shows_completed"] += 1

    log(f"  Complete: {stored} memories stored for {show_config['title']}")


def main():
    global dry_run

    import argparse
    parser = argparse.ArgumentParser(description="Nova Adult Swim Episode Ingest")
    parser.add_argument("--show", choices=list(SHOWS.keys()), help="Ingest only this show")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to memory, just log")
    args = parser.parse_args()

    dry_run = args.dry_run

    # Determine which shows to process
    if args.show:
        shows_to_process = {args.show: SHOWS[args.show]}
    else:
        shows_to_process = SHOWS

    stats["total_shows"] = len(shows_to_process)
    stats["start_time"] = time.time()

    show_list = ", ".join(SHOWS[k]["title"] for k in shows_to_process)
    prefix = "[DRY RUN] " if dry_run else ""

    # Start notification
    slack_post(
        f":brain: *{prefix}Adult Swim Episode Ingest Starting*\n"
        f"  Shows: {show_list}\n"
        f"  Source: Wikipedia API (no API key needed)\n"
        f"  Target: Nova vector memory (pgvector)\n"
        f"  Pipeline: Wikipedia parse → structured episodes → /remember?async=1\n"
        f"  Status updates every 5 minutes"
    )

    # Start status reporter thread
    reporter = Thread(target=status_reporter, daemon=True)
    reporter.start()

    # Process each show
    for show_key, show_config in shows_to_process.items():
        if shutdown.is_set():
            break
        try:
            ingest_show(show_key, show_config)
        except Exception as e:
            log(f"  ERROR processing {show_config['title']}: {e}")
            stats["errors"] += 1
            import traceback
            traceback.print_exc()
        time.sleep(2)  # Pause between shows

    shutdown.set()
    elapsed = time.time() - stats["start_time"]

    # Final report
    show_results = []
    for show_key in shows_to_process:
        s = stats["per_show"].get(show_key, {})
        title = SHOWS[show_key]["title"]
        show_results.append(
            f"  :tv: {title}: {s.get('stored', 0)} memories ({s.get('episodes', 0)} episodes)"
        )

    final_msg = (
        f":white_check_mark: *{prefix}Adult Swim Episode Ingest Complete*\n"
        f"  Total memories stored: {stats['memories_stored']}\n"
        f"  Episodes processed: {stats['episodes_found']}\n"
        f"  Elapsed: {str(timedelta(seconds=int(elapsed)))}\n"
        f"\n" + "\n".join(show_results)
    )
    if stats["errors"]:
        final_msg += f"\n\n  :warning: Errors: {stats['errors']}"

    slack_post(final_msg)
    log(f"\nDone. {stats['memories_stored']} memories stored in {str(timedelta(seconds=int(elapsed)))}")

    # Print summary
    print(f"\n{'='*60}")
    print(f"INGEST COMPLETE")
    print(f"{'='*60}")
    print(f"  Shows: {len(shows_to_process)}")
    print(f"  Episodes: {stats['episodes_found']}")
    print(f"  Memories: {stats['memories_stored']}")
    print(f"  Errors: {stats['errors']}")
    print(f"  Time: {str(timedelta(seconds=int(elapsed)))}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
