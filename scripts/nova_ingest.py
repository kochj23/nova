#!/usr/bin/env python3
"""nova_ingest.py -- Universal ingest engine for Nova vector memory.

Handles Wikipedia BFS crawl, web search (SearXNG), video download
(yt-dlp + Deepgram Nova-2 / MLX Whisper), URL and file ingestion.

Transcription strategy:
  - Non-PII content (YouTube, podcasts, public media): Deepgram Nova-2 cloud API
  - PII content or --local-only flag: local MLX Whisper (Metal GPU)
  - Automatic fallback: if cloud fails, falls back to local whisper
  - Deepgram API key stored in macOS Keychain (service: nova-deepgram-api-key)

Usage:
  nova_ingest.py wikipedia "Physics"
  nova_ingest.py wikipedia "World War II" --target 5000
  nova_ingest.py search "demonology facts history"
  nova_ingest.py video "https://www.youtube.com/@ForgottenWeapons" --channel "Forgotten Weapons"
  nova_ingest.py video "https://youtu.be/xxx" --download-dir ~/Downloads
  nova_ingest.py discover "medieval warfare" --sites 3 --per-site 5
  nova_ingest.py discover "medieval warfare" --sites 3 --per-site 5 --yes
  nova_ingest.py discover "adult content" --sites 2 --per-site 3 --download-dir /Volumes/Data/private --yes
  nova_ingest.py url "https://example.com/article"
  nova_ingest.py file /path/to/doc.txt --source my_source
  nova_ingest.py --resume       # continue last interrupted job
  nova_ingest.py --restart      # restart last job, retry failures
  nova_ingest.py --status       # show current job state
  nova_ingest.py --list-vectors # show all memory source names

Video output: /Volumes/external/videos/TVShows/<Channel>/Season 01/S01E{N} - <Title>.mp4

Written by Jordan Koch.
"""

import argparse, hashlib, json, os, random, re, signal, subprocess, sys
import threading, time, urllib.error, urllib.parse, urllib.request
from collections import deque
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION       = "1.3.0"
MEMORY_URL    = "http://192.168.1.6:18790/remember"
SEARXNG_URL   = "http://192.168.1.10:8080/search"
SLACK_CHANNEL = nova_config.SLACK_NOTIFY
STATE_DIR     = Path.home() / ".openclaw/workspace/state/ingest"
LOG_FILE      = Path.home() / ".openclaw/logs/nova_ingest.log"
WORK_DIR      = Path("/Volumes/Data/nova-ingest-work")
VIDEO_BASE    = Path("/Volumes/external/videos/TVShows")
MUSIC_DIR     = Path("/Volumes/external/music/YouTube")
COOKIES_FILE  = Path.home() / ".openclaw/cache/yt_cookies.txt"
YT_DLP        = "/opt/homebrew/bin/yt-dlp"
FFMPEG        = "/opt/homebrew/bin/ffmpeg"
WHISPER_BIN   = "/opt/homebrew/bin/mlx_whisper"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
WHISPER_COST_PER_MIN = 0.006  # OpenAI Whisper: $0.006/minute ($0.36/hr)
CHUNK_CHARS   = 1500
CHUNK_WORDS   = 400
MIN_WORDS     = 30
RATE_LIMITS   = {"wikipedia": 3.0, "searxng": 1.0, "web": 4.0, "video": 30.0}

DISCOVERY_SITES = [
    ("YouTube",         "https://www.youtube.com/results?search_query={q}"),
    ("Vimeo",           "https://vimeo.com/search?q={q}"),
    ("DailyMotion",     "https://www.dailymotion.com/search/{q}"),
    ("Rumble",          "https://rumble.com/search/video?q={q}"),
    ("Odysee",          "https://odysee.com/$/search?q={q}"),
    ("BitChute",        "https://www.bitchute.com/search/?query={q}"),
    ("Peertube",        "https://sepiasearch.org/search?search={q}"),
    ("TED",             "https://www.ted.com/search?q={q}"),
    ("Internet Archive","https://archive.org/search?query={q}&mediatype=movies"),
]

_SPICY_SITES = [
    # Big four -- most reliable search support
    ("PornHub",     "https://www.pornhub.com/video/search?search={q}"),
    ("XVideos",     "https://www.xvideos.com/?k={q}"),
    ("XHamster",    "https://xhamster.com/search/{q}"),
    ("XNXX",        "https://www.xnxx.com/search/{q}"),
    # Second tier
    ("RedTube",     "https://www.redtube.com/?search={q}"),
    ("YouPorn",     "https://www.youporn.com/search/videos/?query={q}"),
    ("SpankBang",   "https://spankbang.com/s/{q}/"),
    ("Eporner",     "https://www.eporner.com/search/{q}/"),
    ("DrTuber",     "https://www.drtuber.com/search/videos?q={q}"),
    ("SunPorno",    "https://www.sunporno.com/search/{q}/"),
    ("TNAFlix",     "https://www.tnaflix.com/search/?query={q}"),
    ("Txxx",        "https://www.txxx.com/videos/?q={q}"),
    ("Nuvid",       "https://www.nuvid.com/search/videos?q={q}"),
    ("PornTube",    "https://www.porntube.com/videos/search?query={q}"),
    ("Pornotube",   "https://pornotube.com/?search={q}"),
    ("PornFlip",    "https://pornflip.com/search/{q}"),
    ("PornerBros",  "https://www.pornerbros.com/videos/search.html?q={q}"),
    ("AlphaPorno",  "https://www.alphaporno.com/videos/search/?q={q}"),
    ("Slutload",    "https://www.slutload.com/search/?q={q}"),
    ("HellPorno",   "https://hellporno.com/search/?q={q}"),
    ("ZenPorn",     "https://zenporn.com/search/?q={q}"),
    ("Beeg",        "https://beeg.com/search?q={q}"),
    ("ManyVids",    "https://www.manyvids.com/search/?query={q}"),
    ("NubilesPorn", "https://nubilesporn.com/search?q={q}"),
    ("LoveHomePorn","https://www.lovehomeporn.com/search?q={q}"),
    ("Pornbox",     "https://pornbox.com/application/search?query={q}"),
    # Broken in yt-dlp but sometimes work -- kept for completeness
    # ("Tube8",    "https://www.tube8.com/search/videos/?searchValue={q}"),  # BROKEN
]

# ---------------------------------------------------------------------------
# yt-dlp self-upgrade (runs once at startup)
# ---------------------------------------------------------------------------

def _upgrade_ytdlp():
    """Check for and install yt-dlp updates via brew before any downloads."""
    log("Checking yt-dlp for updates...")
    try:
        r = subprocess.run(
            ["/opt/homebrew/bin/brew", "upgrade", "yt-dlp"],
            capture_output=True, text=True, timeout=120)
        if "already installed" in r.stdout or "already installed" in r.stderr:
            # Get current version for the log
            v = subprocess.run([YT_DLP, "--version"],
                               capture_output=True, text=True, timeout=10)
            log(f"yt-dlp up-to-date: {v.stdout.strip()}")
        elif r.returncode == 0:
            v = subprocess.run([YT_DLP, "--version"],
                               capture_output=True, text=True, timeout=10)
            log(f"yt-dlp upgraded to: {v.stdout.strip()}")
            notify(f":arrow_up: *yt-dlp upgraded* to {v.stdout.strip()}")
        else:
            log(f"brew upgrade yt-dlp returned {r.returncode}: {r.stderr[:200]}", "WARN")
    except Exception as e:
        log(f"yt-dlp upgrade check failed: {e}", "WARN")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_shutdown   = False
_log_lock   = threading.Lock()
_last_fetch: dict = {}

def _sig(s, f):
    global _shutdown
    _shutdown = True
    log("Shutdown -- stopping after current item")

signal.signal(signal.SIGINT,  _sig)
signal.signal(signal.SIGTERM, _sig)

# ---------------------------------------------------------------------------
# Logging + Notifications
# ---------------------------------------------------------------------------

def log(msg, level="INFO"):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[nova_ingest {ts}] [{level}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

def notify(text):
    try:
        nova_config.post_both(text, slack_channel=SLACK_CHANNEL)
    except Exception as e:
        log(f"Slack failed: {e}", "WARN")

def notify_item(title, vector, chunks, errors, done, total, nxt, mem):
    pct    = (done / max(total, 1)) * 100
    filled = int(pct / 10)
    bar    = "█" * filled + "░" * (10 - filled)
    lines  = [
        f":white_check_mark: *{title[:80]}*",
        f"  :label: `{vector}` \xb7 :jigsaw: {chunks} chunks \xb7 :x: {errors} errors",
        f"  :bar_chart: `[{bar}]` {pct:.1f}% -- {done}/{total}",
    ]
    if mem:
        lines.append(f"  :thought_balloon: _{mem[:180].replace(chr(10), ' ')}..._")
    if nxt:
        lines.append(f"  :arrow_forward: Next: {nxt[:60]}")
    notify("\n".join(lines))

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _sp(jid):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{jid}.json"

def load_state(jid):
    p = _sp(jid)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {
        "job_id": jid, "mode": "", "query": "", "vector": "",
        "done_urls": [], "done_hashes": [], "failed_urls": [],
        "chunks_total": 0, "items_done": 0, "items_total": 0,
        "started_at": datetime.now().isoformat(),
    }

def save_state(jid, state):
    state["last_updated"] = datetime.now().isoformat()
    _sp(jid).write_text(json.dumps(state, indent=2))

def latest_job_id():
    if not STATE_DIR.exists():
        return None
    files = sorted(STATE_DIR.glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].stem if files else None

# ---------------------------------------------------------------------------
# Vector selection
# ---------------------------------------------------------------------------

def get_existing_vectors():
    try:
        r = subprocess.run(
            ["psql", "-U", "kochj", "-d", "nova_memories", "-tA", "-c",
             "SELECT source FROM memories GROUP BY source ORDER BY COUNT(*) DESC;"],
            capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return [s.strip() for s in r.stdout.strip().split("\n") if s.strip()]
    except Exception as e:
        log(f"Could not fetch vectors: {e}", "WARN")
    return []

def auto_select_vector(topic, sample, existing):
    if not existing:
        return _derive(topic)
    combined = (topic + " " + sample[:500]).lower()
    scores   = {}
    for vec in existing:
        words = re.split(r"[_\s]+", vec.lower())
        score = sum(1.5 for w in words if len(w) > 3 and w in combined)
        if vec.replace("_", " ").lower() in combined:
            score += 10
        if score > 0:
            scores[vec] = score
    if scores:
        best = max(scores, key=scores.get)
        if scores[best] >= 2.0:
            log(f"Auto-selected vector: '{best}' (score={scores[best]:.1f})")
            return best
    try:
        url = "http://192.168.1.6:18790/recall?q=" + urllib.parse.quote(topic) + "&n=5"
        with urllib.request.urlopen(url, timeout=8) as r:
            results = json.loads(r.read())
            sources = [m.get("source", "") for m in results if m.get("source")]
            if sources:
                from collections import Counter
                best = Counter(sources).most_common(1)[0][0]
                log(f"Semantic vector: '{best}'")
                return best
    except Exception:
        pass
    d = _derive(topic)
    log(f"New vector: '{d}'")
    return d

def _derive(topic):
    clean = re.sub(r"[^a-zA-Z0-9\s]", "", topic.lower().strip())
    words = clean.split()[:3]
    return "_".join(words) if words else "general_knowledge"

# ---------------------------------------------------------------------------
# Garbage detection
# ---------------------------------------------------------------------------

_TRASH = [
    re.compile(r"[♪♫♬♭]"),
    re.compile(r"\b(\w+)(?:\s+\1){3,}", re.IGNORECASE),
    re.compile(r"^[A-Z\s\W]{20,}$"),
    re.compile(r"^[^aeiouAEIOU\s]{8,}$"),
    re.compile(r"^[\W\d\s]+$"),
    re.compile(r"subtitles?\s+by|closed\s+caption", re.I),
    re.compile(r"^\[?\s*(silence|music|applause|laughter)\s*\]?$", re.I),
    re.compile(r"(.{3,}?)(\s+\1){2,}"),
    re.compile(r"(.{15,}?)\1{2,}"),
]
_MUSIC = ["♪", "♫", "la la la", "da da da", "na na na",
          "woo woo", "oh oh oh", "yeah yeah yeah"]

def is_garbage(text):
    s = text.strip()
    if len(s.split()) < MIN_WORDS:
        return True
    for pat in _TRASH:
        if pat.search(s):
            return True
    lower = s.lower()
    for ph in _MUSIC:
        if lower.count(ph) >= 3:
            return True
    alpha = sum(c.isalpha() for c in s)
    return len(s) > 0 and alpha / len(s) < 0.45

def clean_text(text):
    paras = re.split(r"\n{2,}", text)
    clean = []
    for para in paras:
        para = para.strip()
        if not para:
            continue
        sents = re.split(r"(?<=[.!?])\s+", para)
        good  = [s for s in sents if not is_garbage(s) and len(s.split()) >= 5]
        if len(good) >= max(1, len(sents) * 0.4):
            clean.append(" ".join(good))
    return "\n\n".join(clean)

def purge_garbage(vector, dry_run=False):
    log(f"Garbage purge on '{vector}'...")
    try:
        r = subprocess.run(
            ["psql", "-U", "kochj", "-d", "nova_memories", "-tA", "-F", "\x1f", "-c",
             "SELECT id, text FROM memories WHERE source='" + vector + "' "
             "ORDER BY created_at DESC LIMIT 5000;"],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return 0
        ids = []
        for line in r.stdout.strip().split("\n"):
            if "\x1f" not in line:
                continue
            mid, txt = line.split("\x1f", 1)
            if is_garbage(txt):
                ids.append(mid.strip())
        if not ids:
            log(f"Purge: nothing to remove from '{vector}'")
            return 0
        log(f"Purge: {len(ids)} fragments from '{vector}'")
        if dry_run:
            return len(ids)
        deleted = 0
        for i in range(0, len(ids), 100):
            batch   = ids[i:i+100]
            id_list = "','".join(batch)
            subprocess.run(
                ["psql", "-U", "kochj", "-d", "nova_memories", "-c",
                 "DELETE FROM memories WHERE id IN ('" + id_list + "');"],
                capture_output=True, timeout=15)
            deleted += len(batch)
        notify(f":broom: *Garbage purge* `{vector}`: removed {deleted} fragments")
        return deleted
    except Exception as e:
        log(f"Purge error: {e}", "WARN")
        return 0

# ---------------------------------------------------------------------------
# Memory helpers
# ---------------------------------------------------------------------------

def truncate_at_boundary(text, max_chars=2000):
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for end_char in ['. ', '! ', '? ', '.\n', '!\n', '?\n']:
        last_sent = cut.rfind(end_char)
        if last_sent > max_chars * 0.6:
            return cut[:last_sent + 1]
    last_space = cut.rfind(' ')
    if last_space > max_chars * 0.8:
        return cut[:last_space]
    return cut


def text_hash(t):
    return hashlib.md5(t.strip().encode()).hexdigest()


# ── Quality Gate ──────────────────────────────────────────────────────────────

_TRIVIAL_TOPICS = {
    "list of", "lists of", "template:", "category:", "portal:", "module:",
    "wikipedia:", "file:", "help:", "talk:", "user:", "draft:",
    "toilet paper", "exploding whale", "wife carrying", "cheese rolling",
    "duck test", "emu war", "list of unusual", "list of fictional",
    "blue waffle", "nutmeg challenge", "tide pod", "cinnamon challenge",
}

_BFS_MAX_DEPTH = 3  # max link hops from seed page


def _title_to_words(title):
    return set(re.sub(r"[^a-z0-9\s]", "", title.lower()).split())


def is_relevant_link(link_title, seed_query, vector_name, depth=0):
    """Decide if a Wikipedia link title is worth following for this ingest."""
    lt = link_title.lower()
    for triv in _TRIVIAL_TOPICS:
        if triv in lt:
            return False
    if depth >= _BFS_MAX_DEPTH:
        return False
    seed_words = _title_to_words(seed_query)
    vector_words = set(vector_name.lower().replace("_", " ").split())
    link_words = _title_to_words(link_title)
    overlap = link_words & (seed_words | vector_words)
    if overlap:
        return True
    if depth <= 1:
        return True
    return False


def page_relevance_check(text, seed_query, vector_name):
    """Quick relevance check — does the page content relate to the topic?"""
    if not text or len(text) < 200:
        return False
    sample = text[:2000].lower()
    seed_words = _title_to_words(seed_query)
    vector_words = set(vector_name.lower().replace("_", " ").split())
    check_words = seed_words | vector_words
    hits = sum(1 for w in check_words if w in sample and len(w) > 3)
    return hits >= 1

def remember(text, source, meta, done_hashes, dry_run=False):
    h = text_hash(text)
    if h in done_hashes:
        return False
    if dry_run:
        done_hashes.add(h)
        return True
    payload = json.dumps({
        "text": truncate_at_boundary(text), "source": source, "tier": "long_term",
        "metadata": {**meta, "ingested_by": "nova_ingest.py", "privacy": "public"},
    }).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                MEMORY_URL + "?async=1", data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15):
                done_hashes.add(h)
                return True
        except Exception as e:
            if attempt == 2:
                log(f"Memory store failed: {e}", "WARN")
                return False
            time.sleep(2 ** attempt)
    return False

def chunk_prose(text, size=CHUNK_CHARS):
    paras   = re.split(r"\n{2,}", text)
    chunks  = []
    current = ""
    for p in paras:
        p = p.strip()
        if not p or len(p) < 30:
            continue
        if len(current) + len(p) > size:
            if current:
                chunks.append(current.strip())
            current = p
        else:
            current += ("\n\n" + p) if current else p
    if current.strip():
        chunks.append(current.strip())
    return chunks

def chunk_words(text, n=CHUNK_WORDS):
    words = text.split()
    return [" ".join(words[i:i+n]) for i in range(0, len(words), n)]

def random_mem(vector):
    try:
        url = ("http://192.168.1.6:18790/random?source="
               + urllib.parse.quote(vector) + "&n=1")
        with urllib.request.urlopen(url, timeout=5) as r:
            data  = json.loads(r.read())
            items = data.get("memories", data.get("results", []))
            if items:
                return items[0].get("text", "")
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

def rate_sleep(stype):
    delay   = RATE_LIMITS.get(stype, 2.0)
    last    = _last_fetch.get(stype, 0)
    elapsed = time.time() - last
    if elapsed < delay:
        time.sleep(delay - elapsed)
    _last_fetch[stype] = time.time()

def fetch(url, timeout=15, retries=4):
    hdr = {"User-Agent": "Nova/2.0 (local research bot; kochj23@github.com)"}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                ct  = r.headers.get("Content-Type", "")
                enc = "utf-8"
                if "charset=" in ct:
                    enc = ct.split("charset=")[-1].split(";")[0].strip()
                return raw.decode(enc, errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 15 * (attempt + 1)
                log(f"  429 rate-limited, waiting {wait}s", "WARN")
                time.sleep(wait)
            elif e.code in (403, 404):
                return None
            else:
                log(f"  HTTP {e.code}: {url[:60]}", "WARN")
                time.sleep(2 ** attempt)
        except Exception as e:
            log(f"  Fetch error: {e} ({url[:60]})", "WARN")
            time.sleep(2 ** attempt)
    return None

class _HX(HTMLParser):
    _SKIP = {"script", "style", "nav", "footer", "header", "aside", "form"}
    _BR   = {"p", "div", "li", "h1", "h2", "h3", "h4", "br", "tr"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._s    = False

    def handle_starttag(self, t, a):
        if t in self._SKIP:
            self._s = True

    def handle_endtag(self, t):
        if t in self._SKIP:
            self._s = False
        if t in self._BR:
            self.parts.append("\n")

    def handle_data(self, d):
        if not self._s:
            self.parts.append(d)

    def get_text(self):
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()

def html_text(html):
    p = _HX()
    try:
        p.feed(html)
        return p.get_text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)

# ---------------------------------------------------------------------------
# Wikipedia mode
# ---------------------------------------------------------------------------

def wiki_random_article(min_bytes=2000, max_attempts=25):
    """Fetch a random Wikipedia article that's substantial enough to crawl."""
    api = ("https://en.wikipedia.org/w/api.php?action=query&list=random"
           "&rnnamespace=0&rnlimit=1&format=json")
    for attempt in range(max_attempts):
        raw = fetch(api, timeout=15)
        if not raw:
            continue
        try:
            data = json.loads(raw)
            title = data["query"]["random"][0]["title"]
        except (json.JSONDecodeError, KeyError, IndexError):
            continue
        url = ("https://en.wikipedia.org/wiki/" +
               urllib.parse.quote(title.replace(" ", "_")))
        _, text, links, err = wiki_fetch(url)
        if err or not text or len(text) < min_bytes:
            log(f"  Random article '{title}' too short ({len(text) if text else 0}B), re-rolling [{attempt+1}/{max_attempts}]")
            continue
        if len(links) < 5:
            log(f"  Random article '{title}' has too few links ({len(links)}), re-rolling [{attempt+1}/{max_attempts}]")
            continue
        log(f"  Random article selected: '{title}' ({len(text)}B, {len(links)} links)")
        return title
    log("Could not find a substantial random article after max attempts", "ERROR")
    return None

def wiki_fetch(url):
    tp  = url.split("/wiki/")[-1]
    api = ("https://en.wikipedia.org/w/api.php?action=query"
           "&titles=" + urllib.parse.quote(tp) +
           "&prop=extracts|links&explaintext=1&pllimit=max&format=json")
    raw = fetch(api, timeout=20)
    if not raw:
        return None, None, [], "fetch failed"
    try:
        data = json.loads(raw)
    except Exception:
        return None, None, [], "json parse"
    pages = data.get("query", {}).get("pages", {})
    if not pages:
        return None, None, [], "no pages"
    page  = list(pages.values())[0]
    if "missing" in page:
        return None, None, [], "missing"
    text  = page.get("extract", "")
    title = page.get("title", tp.replace("_", " "))
    links = []
    for lk in page.get("links", []):
        lt = lk.get("title", "")
        if lk.get("ns", 0) == 0 and ":" not in lt:
            links.append("https://en.wikipedia.org/wiki/" +
                         urllib.parse.quote(lt.replace(" ", "_")))
    return title, text, links, None

def run_wikipedia(query, vector, target, state, dry_run, timeout_hours=0):
    jid         = state["job_id"]
    done_urls   = set(state.get("done_urls", []))
    done_hashes = set(state.get("done_hashes", []))
    failed      = list(state.get("failed_urls", []))
    ct          = state.get("chunks_total", 0)
    items_done  = state.get("items_done", 0)
    _last_notify = [0.0]  # mutable for closure; throttle to every 5 min
    _start_time = time.time()
    _deadline   = _start_time + (timeout_hours * 3600) if timeout_hours > 0 else 0

    q_safe = query.replace(" ", "_")
    start  = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(q_safe)
    queue  = deque([start] + failed)

    log(f"Wikipedia BFS: '{query}' -> '{vector}' target={target:,}" +
        (f" timeout={timeout_hours}h" if timeout_hours else ""))
    notify(f":books: *Wikipedia Ingest Started*\n"
           f"  Topic: *{query}* \xb7 Vector: `{vector}`\n"
           f"  Target: {target:,} chunks" +
           (f" \xb7 Timeout: {timeout_hours}h" if timeout_hours else "") +
           ("  (DRY RUN)" if dry_run else ""))

    _depth_map = {start: 0}
    _skipped = 0

    while queue and ct < target and not _shutdown:
        if _deadline and time.time() >= _deadline:
            log(f"Timeout reached ({timeout_hours}h) — stopping at {ct} chunks")
            notify(f":hourglass: *Wikipedia timeout* ({timeout_hours}h)\n"
                   f"  Topic: *{query}* \xb7 Collected: {ct:,} chunks")
            break
        url = queue.popleft()
        if url in done_urls:
            continue
        rate_sleep("wikipedia")
        title, text, links, err = wiki_fetch(url)
        if err or not text or len(text) < 100:
            failed.append(url)
            state["failed_urls"] = failed[-500:]
            save_state(jid, state)
            continue
        done_urls.add(url)
        current_depth = _depth_map.get(url, 0)
        if not page_relevance_check(text, query, vector):
            _skipped += 1
            continue
        text     = clean_text(text)
        ingested = 0
        for chunk in chunk_prose(text):
            if ct >= target or is_garbage(chunk):
                continue
            if remember(chunk, vector,
                        {"title": title, "url": url, "type": "wikipedia"},
                        done_hashes, dry_run):
                ct      += 1
                ingested += 1
        items_done += 1
        state.update({
            "done_urls":    list(done_urls)[-5000:],
            "done_hashes":  list(done_hashes)[-20000:],
            "chunks_total": ct,
            "items_done":   items_done,
            "items_total":  max(items_done + len(queue), state.get("items_total", 0)),
        })
        save_state(jid, state)
        log(f"  [{ct}/{target}] {title} -> {ingested} chunks (q:{len(queue)} skip:{_skipped})")
        nxt = queue[0].split("/wiki/")[-1].replace("_", " ") if queue else None
        if time.time() - _last_notify[0] >= 300:
            notify_item(title, vector, ingested, 0,
                        ct, target,
                        nxt, random_mem(vector) if not dry_run else None)
            _last_notify[0] = time.time()
        for lnk in links:
            if lnk not in done_urls:
                lnk_title = urllib.parse.unquote(lnk.split("/wiki/")[-1]).replace("_", " ")
                child_depth = current_depth + 1
                if is_relevant_link(lnk_title, query, vector, depth=child_depth):
                    _depth_map[lnk] = child_depth
                    queue.append(lnk)
    _finish(jid, query, vector, ct, target, items_done, len(failed), dry_run)

# ---------------------------------------------------------------------------
# Search mode
# ---------------------------------------------------------------------------

def run_search(query, vector, target, state, dry_run):
    jid         = state["job_id"]
    done_urls   = set(state.get("done_urls", []))
    done_hashes = set(state.get("done_hashes", []))
    ct          = state.get("chunks_total", 0)
    items_done  = state.get("items_done", 0)
    failed      = 0
    _last_notify = [0.0]

    log(f"Search ingest: '{query}' -> '{vector}'")
    notify(f":mag: *Search Ingest Started*\n"
           f"  Query: *{query}* \xb7 Vector: `{vector}`\n"
           f"  Target: {target:,} chunks")

    page = 1
    while ct < target and not _shutdown:
        rate_sleep("searxng")
        params = urllib.parse.urlencode({
            "q": query, "format": "json",
            "categories": "general", "language": "en", "pageno": page,
        })
        raw = fetch(f"{SEARXNG_URL}?{params}", timeout=10)
        if not raw:
            break
        try:
            results = json.loads(raw).get("results", [])
        except Exception:
            break
        if not results:
            break
        for i, r in enumerate(results):
            if ct >= target or _shutdown:
                break
            url = r.get("url", "")
            if not url or url in done_urls:
                continue
            rate_sleep("web")
            html = fetch(url, timeout=20)
            if not html:
                failed += 1
                continue
            done_urls.add(url)
            text = clean_text(html_text(html))
            if len(text.split()) < 100:
                continue
            title    = r.get("title", url[:60])
            ingested = 0
            for chunk in chunk_prose(text):
                if ct >= target or is_garbage(chunk):
                    continue
                if remember(chunk, vector,
                            {"title": title, "url": url,
                             "type": "web_search", "query": query},
                            done_hashes, dry_run):
                    ct      += 1
                    ingested += 1
            items_done += 1
            state.update({
                "done_urls":    list(done_urls)[-2000:],
                "done_hashes":  list(done_hashes)[-20000:],
                "chunks_total": ct,
                "items_done":   items_done,
            })
            save_state(jid, state)
            nxt = results[i+1].get("title", "")[:60] if i+1 < len(results) else None
            if time.time() - _last_notify[0] >= 300:
                notify_item(title[:80], vector, ingested, failed,
                            items_done, max(items_done, target // 5),
                            nxt, random_mem(vector) if not dry_run else None)
                _last_notify[0] = time.time()
        page += 1
    _finish(jid, query, vector, ct, target, items_done, failed, dry_run)

# ---------------------------------------------------------------------------
# Video helpers
# ---------------------------------------------------------------------------

def _refresh_cookies():
    if COOKIES_FILE.exists() and time.time() - COOKIES_FILE.stat().st_mtime < 6 * 3600:
        return
    log("Refreshing Chrome cookies...")
    script = (
        "do shell script \"" + YT_DLP +
        " --cookies-from-browser chrome --cookies " + str(COOKIES_FILE) +
        " --skip-download --print \\\"%(id)s\\\""
        " \\\"https://www.youtube.com/watch?v=dQw4w9WgXcQ\\\"\"")
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script],
                       capture_output=True, timeout=30)
        if COOKIES_FILE.exists():
            os.chmod(COOKIES_FILE, 0o600)
    except Exception as e:
        log(f"Cookie refresh failed: {e}", "WARN")

def _ytbase():
    args = [YT_DLP,
            "--extractor-args", "youtube:player_client=web,default",
            "--windows-filenames", "--no-playlist"]
    if COOKIES_FILE.exists():
        args += ["--cookies", str(COOKIES_FILE)]
    else:
        args += ["--cookies-from-browser", "chrome"]
    return args

def _get_vids(url, dateafter=None):
    _refresh_cookies()
    if dateafter:
        cmd = _ytbase() + [
            "--dateafter", dateafter,
            "--print", "%(id)s\t%(title)s\t%(upload_date)s\t%(uploader)s",
            url,
        ]
    else:
        cmd = _ytbase() + [
            "--flat-playlist",
            "--print", "%(id)s\t%(title)s\t%(upload_date)s\t%(uploader)s",
            url,
        ]
    try:
        timeout = 600 if dateafter else 120
        r    = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        vids = []
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            vids.append({
                "id":       parts[0],
                "title":    parts[1] if len(parts) > 1 else parts[0],
                "date":     parts[2] if len(parts) > 2 else "19700101",
                "uploader": parts[3] if len(parts) > 3 else "",
            })
        return vids
    except Exception as e:
        log(f"yt-dlp list failed: {e}", "ERROR")
        return []

def _get_vids_search(url, limit):
    cmd = _ytbase() + [
        "--flat-playlist",
        "--playlist-end", str(limit),
        "--print", "%(id)s\t%(title)s\t%(webpage_url)s",
        url,
    ]
    try:
        r    = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        vids = []
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            vids.append({
                "id":    parts[0],
                "title": parts[1] if len(parts) > 1 else parts[0],
                "url":   parts[2] if len(parts) > 2 else "",
            })
        return vids[:limit]
    except Exception as e:
        log(f"  Search fetch failed: {e}", "WARN")
        return []

def _ep_path(channel, title, ep):
    sc = re.sub(r"[^\w\s-]", "", channel).strip().replace(" ", "_")
    st = re.sub(r"[^\w\s\-]", "", title).strip()
    st = re.sub(r"\s+", " ", st)[:80]
    d  = VIDEO_BASE / sc / "Season 01"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"S01E{ep:04d} - {st}.mp4"

def _dl_path(download_dir, title, vid_id):
    d    = Path(download_dir).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\s\-]", "", title).strip()
    safe = re.sub(r"\s+", " ", safe)[:100]
    return d / f"{safe} [{vid_id}].mp4"

def _download_url(vid_url, out):
    cmd = _ytbase() + [
        "-f", "bestvideo[height<=540]+bestaudio/best[height<=540]",
        "--merge-output-format", "mp4",
        "-o", str(out),
        "--no-overwrites",
        vid_url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            log(f"  yt-dlp error: {r.stderr[-200:]}", "WARN")
            return False
        return out.exists()
    except Exception as e:
        log(f"  Download exception: {e}", "WARN")
        return False

_MUSIC_UPLOADERS = {"vevo", "- topic", "official", "records", "music"}


def _fetch_video_meta(vid_id):
    """Fetch full metadata JSON for a single video."""
    cmd = _ytbase() + ["--dump-json", "--no-download", "--no-playlist",
                       f"https://www.youtube.com/watch?v={vid_id}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return json.loads(r.stdout)
    except Exception:
        pass
    return {}


def _is_music(meta):
    """Detect music by YouTube category or uploader pattern."""
    categories = [c.lower() for c in (meta.get("categories") or [])]
    if "music" in categories:
        return True
    uploader = (meta.get("uploader") or "").lower()
    channel = (meta.get("channel") or "").lower()
    for pat in _MUSIC_UPLOADERS:
        if pat in uploader or pat in channel:
            return True
    return False


def _download_as_mp3(vid_id, meta):
    """Download audio only as 256kbps MP3 with ID3 tags. Returns output path or None."""
    artist = meta.get("artist") or meta.get("uploader") or "Unknown Artist"
    track = meta.get("track") or meta.get("title") or "Unknown Track"
    album = meta.get("album") or ""
    year = str(meta.get("release_year") or meta.get("upload_date", "")[:4] or "")

    safe_artist = re.sub(r"[^\w\s\-]", "", artist).strip()[:60]
    safe_track = re.sub(r"[^\w\s\-]", "", track).strip()[:80]
    out_path = MUSIC_DIR / f"{safe_artist} - {safe_track}.mp3"

    if out_path.exists():
        return out_path

    MUSIC_DIR.mkdir(parents=True, exist_ok=True)

    cmd = _ytbase() + [
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "256K",
        "--embed-thumbnail",
        "--embed-metadata",
        "--no-overwrites",
        "--no-playlist",
        "-o", str(out_path),
        f"https://www.youtube.com/watch?v={vid_id}",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            log(f"  MP3 download failed: {r.stderr[-200:]}", "WARN")
            return None
    except Exception as e:
        log(f"  MP3 download exception: {e}", "WARN")
        return None

    _apply_id3(out_path, artist, track, album, year)
    log(f"  ♪ {safe_artist} - {safe_track} (MP3 256kbps)")
    return out_path


def _apply_id3(path, artist, track, album, year):
    """Ensure ID3 tags via mutagen (fills gaps --embed-metadata missed)."""
    try:
        from mutagen.id3 import ID3, TPE1, TIT2, TALB, TDRC, ID3NoHeaderError
        try:
            tags = ID3(str(path))
        except ID3NoHeaderError:
            tags = ID3()
        if artist and not tags.get("TPE1"):
            tags.add(TPE1(encoding=3, text=[artist]))
        if track and not tags.get("TIT2"):
            tags.add(TIT2(encoding=3, text=[track]))
        if album and not tags.get("TALB"):
            tags.add(TALB(encoding=3, text=[album]))
        if year and not tags.get("TDRC"):
            tags.add(TDRC(encoding=3, text=[year]))
        tags.save(str(path))
    except Exception:
        pass


def _audio(video, wav):
    cmd = [FFMPEG, "-y", "-i", str(video),
           "-vn", "-ac", "1", "-ar", "16000",
           "-acodec", "pcm_s16le", "-t", "7200",
           str(wav)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=7260)
        return wav.exists() and wav.stat().st_size > 1000
    except Exception as e:
        log(f"  ffmpeg: {e}", "WARN")
        return False

def _transcribe(wav, stem, work):
    cmd = [WHISPER_BIN, str(wav),
           "--model",         WHISPER_MODEL,
           "--output-format", "txt",
           "--output-dir",    str(work),
           "--output-name",   stem,
           "--language",      "en"]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
        txt = work / f"{stem}.txt"
        if txt.exists():
            t = txt.read_text(encoding="utf-8", errors="ignore").strip()
            txt.unlink(missing_ok=True)
            return t if len(t) > 50 else None
    except subprocess.TimeoutExpired:
        log("  Whisper timeout", "WARN")
    except Exception as e:
        log(f"  Whisper: {e}", "WARN")
    return None

def _warm():
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=json.dumps({
                "model": "nomic-embed-text",
                "prompt": " ", "stream": False,
                "options": {"num_predict": 1},
            }).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Cloud transcription (OpenAI Whisper API)
# ---------------------------------------------------------------------------

def _get_openai_key():
    """Retrieve OpenAI-compatible API key from macOS Keychain.
    Uses OpenRouter key (already available) routed to OpenAI Whisper."""
    for svc in ("nova-openrouter-api-key",):
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", svc, "-w"],
                capture_output=True, text=True, timeout=10)
            key = r.stdout.strip()
            if r.returncode == 0 and key:
                return key
        except Exception:
            continue
    return None


def _get_audio_duration(wav_path):
    """Get audio duration in seconds using ffprobe."""
    try:
        r = subprocess.run(
            [FFMPEG.replace("ffmpeg", "ffprobe"),
             "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(wav_path)],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    # Fallback: estimate from PCM file size (16kHz, 16-bit mono = 32000 bytes/sec)
    try:
        return Path(wav_path).stat().st_size / 32000.0
    except Exception:
        return 0.0


def _is_pii_content(vid_url=None, source_type=None):
    """Determine if content might contain PII.

    YouTube, podcasts, and public web content are always non-PII.
    Local files or unknown sources are treated as potentially PII.
    """
    if source_type in ("youtube", "video", "podcast", "web_page", "web_search",
                       "wikipedia", "discover"):
        return False
    if vid_url:
        non_pii_domains = [
            "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com",
            "rumble.com", "odysee.com", "bitchute.com", "ted.com",
            "archive.org", "twitch.tv", "spotify.com", "apple.com/podcast",
        ]
        lower = vid_url.lower()
        if any(d in lower for d in non_pii_domains):
            return False
    # Default: treat as PII (local files, unknown sources)
    return True


def _transcribe_cloud(wav_path, api_key):
    """Transcribe audio via OpenAI Whisper API. Returns transcript text or None."""
    duration = _get_audio_duration(wav_path)
    duration_min = duration / 60.0
    COST_PER_MIN = 0.006  # OpenAI Whisper: $0.006/min

    log(f"  Cloud transcription: {duration_min:.1f} min via OpenAI Whisper")

    file_path = Path(wav_path)
    # OpenAI Whisper has 25MB limit — check size
    file_size = file_path.stat().st_size
    if file_size > 25 * 1024 * 1024:
        log(f"  Audio file too large for Whisper API ({file_size//1024//1024}MB > 25MB), using local", "WARN")
        return None

    try:
        import http.client
        import mimetypes

        boundary = f"----NovaIngest{int(time.time())}"
        body_parts = []
        body_parts.append(f"--{boundary}\r\n".encode())
        body_parts.append(b'Content-Disposition: form-data; name="model"\r\n\r\n')
        body_parts.append(b"whisper-1\r\n")
        body_parts.append(f"--{boundary}\r\n".encode())
        body_parts.append(f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'.encode())
        body_parts.append(b"Content-Type: audio/wav\r\n\r\n")
        body_parts.append(file_path.read_bytes())
        body_parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(body_parts)

        conn = http.client.HTTPSConnection("api.openai.com", timeout=max(120, int(duration / 5 * 60)))
        conn.request("POST", "/v1/audio/transcriptions", body=body, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        })
        resp = conn.getresponse()
        resp_data = resp.read().decode()
        conn.close()

        if resp.status != 200:
            log(f"  OpenAI Whisper API error {resp.status}: {resp_data[:200]}", "ERROR")
            return None

        result = json.loads(resp_data)
        transcript = result.get("text", "")

        if not transcript or len(transcript.strip()) < 50:
            log("  Whisper returned empty/short transcript", "WARN")
            return None

        cost = duration_min * COST_PER_MIN
        log(f"  Whisper transcription complete: {duration_min:.1f} min, "
            f"cost=${cost:.4f}, {len(transcript)} chars")

        return transcript.strip()

    except Exception as e:
        log(f"  OpenAI Whisper transcription failed: {e}", "ERROR")
        return None


def _transcribe_dispatch(wav_path, stem, work, vid_url=None, local_only=False):
    """Smart transcription dispatch: cloud for non-PII, local MLX for PII or fallback.

    Returns transcript text or None.
    """
    # Determine if we can use cloud
    use_cloud = False
    openai_key = None
    if not local_only and not _is_pii_content(vid_url=vid_url, source_type="video"):
        openai_key = _get_openai_key()
        if openai_key:
            use_cloud = True
        else:
            log("  No OpenAI key in Keychain -- falling back to local whisper", "WARN")

    if use_cloud:
        transcript = _transcribe_cloud(str(wav_path), openai_key)
        if transcript:
            return transcript
        log("  Cloud transcription failed -- falling back to local whisper", "WARN")

    # Local MLX whisper path (original behavior)
    return _transcribe(wav_path, stem, work)

# ---------------------------------------------------------------------------
# Video mode
# ---------------------------------------------------------------------------

def run_video(url, channel, vector, target, state, dry_run, download_dir=None, dateafter=None, local_only=False):
    jid         = state["job_id"]
    done_urls   = set(state.get("done_urls", []))
    done_hashes = set(state.get("done_hashes", []))
    ct          = state.get("chunks_total", 0)
    items_done  = state.get("items_done", 0)
    failed      = 0
    dl_only     = bool(download_dir)

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    vids = _get_vids(url, dateafter=dateafter)
    if not vids:
        log("No videos found", "ERROR")
        return
    if dateafter:
        log(f"Date filter (>={dateafter}): {len(vids)} videos matched")

    pending              = [v for v in vids if v["id"] not in done_urls]
    state["items_total"] = len(vids)
    save_state(jid, state)

    log(f"Video: '{channel}' -> " +
        (f"download-only: {download_dir}" if dl_only else f"vector: '{vector}'") +
        f" -- {len(pending)}/{len(vids)} pending")
    notify(f":movie_camera: *Video {'Download' if dl_only else 'Ingest'} Started*\n"
           f"  Channel: *{channel}*\n"
           + (f"  Output: `{download_dir}`\n" if dl_only else f"  Vector: `{vector}`\n") +
           f"  {len(pending)} pending / {len(vids)} total")

    all_sorted = sorted(vids, key=lambda v: v.get("date", "19700101"))
    ep_map     = {v["id"]: i + 1 for i, v in enumerate(all_sorted)}
    pending    = sorted(pending, key=lambda v: v.get("date", "19700101"))
    if not dl_only:
        _warm()

    for vid in pending:
        if (not dl_only and ct >= target) or _shutdown:
            break
        vid_id  = vid["id"]
        title   = vid["title"]
        ep      = ep_map.get(vid_id, items_done + 1)
        wav     = WORK_DIR / f"{vid_id}.wav"

        log(f"  {'DL' if dl_only else 'E%04d' % ep}: {title[:60]}")

        # Check if this is a music video — download as MP3 instead
        meta = _fetch_video_meta(vid_id) if not dry_run else {}
        is_music_vid = _is_music(meta) if meta else False

        if is_music_vid and not dry_run:
            out = None
            mp3_path = _download_as_mp3(vid_id, meta)
            if not mp3_path:
                failed += 1
                state["failed_urls"] = state.get("failed_urls", []) + [vid_id]
                save_state(jid, state)
                continue
            # Still transcribe and ingest lyrics/content into memory
            transcript = None
            if not dl_only and mp3_path.exists():
                mp3_wav = WORK_DIR / f"{vid_id}.wav"
                if _audio(mp3_path, mp3_wav):
                    vid_url = "https://www.youtube.com/watch?v=" + vid_id
                    transcript = _transcribe_dispatch(
                        mp3_wav, vid_id, WORK_DIR,
                        vid_url=vid_url, local_only=local_only)
                    mp3_wav.unlink(missing_ok=True)

            ingested = 0
            if not dl_only and transcript:
                transcript = clean_text(transcript)
                for chunk in chunk_words(transcript):
                    if is_garbage(chunk):
                        continue
                    if remember(chunk, vector,
                                {"title": title, "channel": channel,
                                 "video_id": vid_id, "episode": ep,
                                 "type": "music_transcript"},
                                done_hashes, dry_run):
                        ct      += 1
                        ingested += 1
            import random
            time.sleep(random.uniform(25, 45))
        else:
            # Normal video download path
            out = (_dl_path(download_dir, title, vid_id) if dl_only
                   else _ep_path(channel, title, ep))

            if not out.exists() and not dry_run:
                if not _download_url("https://www.youtube.com/watch?v=" + vid_id, out):
                    failed += 1
                    notify(f":x: *Download failed*: {title[:60]}\n  Will retry on next run.")
                    state["failed_urls"] = state.get("failed_urls", []) + [vid_id]
                    save_state(jid, state)
                    continue
                import random
                time.sleep(random.uniform(25, 45))

            transcript = None
            if not dl_only and not dry_run and out.exists():
                if _audio(out, wav):
                    vid_url = "https://www.youtube.com/watch?v=" + vid_id
                    transcript = _transcribe_dispatch(
                        wav, vid_id, WORK_DIR,
                        vid_url=vid_url, local_only=local_only)
                    wav.unlink(missing_ok=True)

            ingested = 0
            if not dl_only and transcript:
                transcript = clean_text(transcript)
                for chunk in chunk_words(transcript):
                    if is_garbage(chunk):
                        continue
                    if remember(chunk, vector,
                                {"title": title, "channel": channel,
                                 "video_id": vid_id, "episode": ep,
                                 "type": "video_transcript"},
                                done_hashes, dry_run):
                        ct      += 1
                        ingested += 1

        done_urls.add(vid_id)
        items_done += 1
        state.update({
            "done_urls":    list(done_urls)[-5000:],
            "done_hashes":  list(done_hashes)[-20000:],
            "chunks_total": ct,
            "items_done":   items_done,
        })
        save_state(jid, state)

        remaining = [v for v in pending if v["id"] not in done_urls]
        nxt       = remaining[0]["title"][:60] if remaining else None

        if is_music_vid:
            log(f"  ♪ Saved as MP3")
        elif dl_only:
            log(f"  Saved: {out}")
            if not hasattr(run_video, '_last_dl_notify'):
                run_video._last_dl_notify = 0
            if time.time() - run_video._last_dl_notify >= 300:
                notify(f":arrow_down: *Download Progress*: {channel}\n"
                       f"  Latest: {title[:60]}\n"
                       f"  {items_done}/{len(vids)} done ({len(vids)-items_done} remaining)")
                run_video._last_dl_notify = time.time()
        else:
            notify_item(
                f"{channel} -- {title[:60]}", vector, ingested, failed,
                items_done, len(vids), nxt,
                random_mem(vector) if not dry_run and ingested > 0 else None,
            )
            rate_sleep("video")
            _warm()

    if dl_only:
        notify(f":checkered_flag: *Download Complete*: {channel}\n"
               f"  :open_file_folder: `{download_dir}`\n"
               f"  {items_done} files, {failed} errors")
        log(f"Download done: {items_done} files, {failed} errors -> {download_dir}")
    else:
        _finish(jid, url, vector, ct, target, items_done, failed, dry_run)

# ---------------------------------------------------------------------------
# Discover mode
# ---------------------------------------------------------------------------

def run_discover(subject, vector, num_sites, per_site, state, dry_run,
                 yes=False, download_dir=None, _alt_pool=False, local_only=False):
    jid         = state["job_id"]
    done_hashes = set(state.get("done_hashes", []))
    dl_only     = bool(download_dir)

    log(f"Discover: '{subject}' across {num_sites} sites, {per_site} videos each")

    _pool        = _SPICY_SITES if _alt_pool else DISCOVERY_SITES
    chosen_sites = random.sample(_pool, min(num_sites, len(_pool)))

    all_candidates = []
    for site_name, url_tpl in chosen_sites:
        search_url = url_tpl.replace("{q}", urllib.parse.quote(subject))
        log(f"  Searching {site_name}: {search_url[:60]}")
        vids = _get_vids_search(search_url, per_site)
        for v in vids:
            v["site"] = site_name
        all_candidates.extend(vids)
        if not vids:
            log(f"  No results from {site_name}", "WARN")

    if not all_candidates:
        log("No videos found across any site", "ERROR")
        notify(f":x: *Discover failed*: no results for '{subject}' across {num_sites} sites")
        return

    print()
    print(f"Found {len(all_candidates)} candidate videos for '{subject}':")
    print()
    for i, v in enumerate(all_candidates, 1):
        print(f"  {i:2d}. [{v.get('site','?')}] {v.get('title','?')[:70]}")
    print()
    if dl_only:
        print(f"Output directory: '{download_dir}'")
    else:
        print(f"Vector will be: '{vector}'")
    print()

    if dry_run:
        print("DRY RUN -- would download" + ("" if dl_only else " and ingest") + " the above.")
        return

    if not yes:
        try:
            ans = input("Proceed with download" +
                        ("" if dl_only else " and ingest") +
                        "? [y/N] ").strip().lower()
        except EOFError:
            ans = "y"
        if ans not in ("y", "yes"):
            print("Cancelled.")
            return

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    if dl_only and download_dir:
        Path(download_dir).expanduser().resolve().mkdir(parents=True, exist_ok=True)

    ct         = state.get("chunks_total", 0)
    items_done = 0
    failed     = 0

    notify(f":satellite: *Discover {'Download' if dl_only else 'Ingest'} Started*\n"
           f"  Subject: *{subject}*\n"
           f"  Sites: {', '.join(s for s, _ in chosen_sites)}\n"
           f"  Videos: {len(all_candidates)} \xb7 " +
           (f"Output: `{download_dir}`" if dl_only else f"Vector: `{vector}`"))

    for vid in all_candidates:
        if _shutdown:
            break
        vid_id  = vid.get("id", "")
        title   = vid.get("title", vid_id)
        site    = vid.get("site", "unknown")
        ep      = items_done + 1
        vid_url = vid.get("url") or ("https://www.youtube.com/watch?v=" + vid_id)
        wav     = WORK_DIR / f"{vid_id}.wav"
        channel = f"{subject} ({site})"
        out     = (_dl_path(download_dir, title, vid_id) if dl_only
                   else _ep_path(channel, title, ep))

        log(f"  {'DL' if dl_only else 'E%04d' % ep}: {title[:60]} [{site}]")

        if not out.exists():
            if not _download_url(vid_url, out):
                log(f"  Download failed: {title[:60]}", "WARN")
                failed += 1
                continue

        transcript = None
        if not dl_only and out.exists() and _audio(out, wav):
            transcript = _transcribe_dispatch(
                wav, vid_id, WORK_DIR,
                vid_url=vid_url, local_only=local_only)
            wav.unlink(missing_ok=True)

        ingested = 0
        if not dl_only and transcript:
            transcript = clean_text(transcript)
            for chunk in chunk_words(transcript):
                if is_garbage(chunk):
                    continue
                if remember(chunk, vector,
                            {"title": title, "site": site, "subject": subject,
                             "episode": ep, "type": "video_transcript"},
                            done_hashes):
                    ct      += 1
                    ingested += 1

        items_done += 1
        state.update({
            "done_hashes":  list(done_hashes)[-20000:],
            "chunks_total": ct,
            "items_done":   items_done,
        })
        save_state(jid, state)

        remaining = all_candidates[items_done:]
        nxt       = remaining[0].get("title", "")[:60] if remaining else None

        if dl_only:
            notify(f":white_check_mark: *Downloaded*: [{site}] {title[:60]}\n"
                   f"  :open_file_folder: `{out}`\n"
                   f"  {items_done}/{len(all_candidates)} done")
        else:
            notify_item(
                f"[{site}] {title[:60]}", vector, ingested, failed,
                items_done, len(all_candidates), nxt,
                random_mem(vector) if ingested > 0 else None,
            )
            _warm()

    if dl_only:
        notify(f":checkered_flag: *Discover Download Complete*: {subject}\n"
               f"  :open_file_folder: `{download_dir}`\n"
               f"  {items_done} files, {failed} errors")
        log(f"Discover download done: {items_done} files -> {download_dir}")
    else:
        _finish(jid, subject, vector, ct, len(all_candidates) * CHUNK_WORDS // 10,
                items_done, failed, False)

# ---------------------------------------------------------------------------
# URL / File modes
# ---------------------------------------------------------------------------

def run_url(url, vector, state, dry_run, silent=False):
    """Ingest a single URL. silent=True skips Slack notification (for batch callers)."""
    dh   = set(state.get("done_hashes", []))
    html = fetch(url)
    if not html:
        log(f"Could not fetch: {url}", "ERROR")
        return
    text     = clean_text(html_text(html))
    ingested = sum(
        1 for c in chunk_prose(text)
        if not is_garbage(c) and
           remember(c, vector, {"url": url, "type": "web_page"}, dh, dry_run)
    )
    log(f"URL: {ingested} chunks  [{url[:60]}]")
    if not silent:
        mem = random_mem(vector) if ingested > 0 else None
        msg = f":link: *URL Ingested*: {url[:80]}\n  Vector: `{vector}` \xb7 {ingested} chunks"
        if mem:
            msg += f"\n  :thought_balloon: _{mem[:180].replace(chr(10), ' ')}..._"
        notify(msg)

def run_file(path, vector, state, dry_run):
    dh = set(state.get("done_hashes", []))
    p  = Path(path)
    if not p.exists():
        log(f"File not found: {path}", "ERROR")
        return
    text     = clean_text(p.read_text(encoding="utf-8", errors="replace"))
    ingested = sum(
        1 for c in chunk_prose(text)
        if not is_garbage(c) and
           remember(c, vector, {"path": str(p), "type": "local_file"}, dh, dry_run)
    )
    log(f"File: {ingested} chunks")
    mem = random_mem(vector) if ingested > 0 else None
    msg = f":page_facing_up: *File Ingested*: `{p.name}`\n  Vector: `{vector}` \xb7 {ingested} chunks"
    if mem:
        msg += f"\n  :thought_balloon: _{mem[:180].replace(chr(10), ' ')}..._"
    notify(msg)

# ---------------------------------------------------------------------------
# Finish
# ---------------------------------------------------------------------------

def _finish(jid, label, vector, chunks, target, items, errors, dry_run):
    pct    = min(chunks / max(target, 1) * 100, 100)
    status = "DRY RUN complete" if dry_run else ":checkered_flag: *Ingest Complete*"
    notify(
        f"{status}: *{label[:60]}*\n"
        f"  Vector: `{vector}`\n"
        f"  :jigsaw: {chunks:,} / {target:,} chunks ({pct:.1f}%)\n"
        f"  :page_facing_up: {items:,} items \xb7 :x: {errors} errors" +
        ("\n  Running garbage purge..." if not dry_run else "")
    )
    if not dry_run:
        d = purge_garbage(vector)
        if d:
            log(f"Purged {d} garbage fragments from '{vector}'")
    log(f"Done: {chunks} chunks, {items} items, {errors} errors")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Topics-file mode (batch Wikipedia/search across many topics)
# ---------------------------------------------------------------------------

def run_topics_file(path, mode, target, dry_run):
    """
    Read a topics file (one entry per line: topic [TAB source [TAB target]])
    and run wikipedia or search mode for each topic sequentially.

    Line formats:
        Physics
        Physics\tphysics_mechanics
        Physics\tphysics_mechanics\t5000
        # comment lines are skipped
    """
    p = Path(path).expanduser()
    if not p.exists():
        log(f"Topics file not found: {path}", "ERROR")
        return

    lines = [l.strip() for l in p.read_text().splitlines()
             if l.strip() and not l.strip().startswith("#")]

    if not lines:
        log("Topics file is empty", "ERROR")
        return

    log(f"Topics file: {len(lines)} topics, mode={mode}")
    existing = get_existing_vectors()

    for i, line in enumerate(lines):
        if _shutdown:
            break
        parts  = line.split("\t")
        topic  = parts[0].strip()
        src    = parts[1].strip() if len(parts) > 1 else ""
        tgt    = int(parts[2].strip()) if len(parts) > 2 else target
        vector = src or auto_select_vector(topic, topic, existing)

        log(f"[{i+1}/{len(lines)}] {topic} -> {vector} (target={tgt})")
        notify(f":card_index_dividers: *Topics file* [{i+1}/{len(lines)}]: *{topic}* -> `{vector}`")

        jid   = hashlib.md5(f"{mode}:{topic}:{time.time()}".encode()).hexdigest()[:12]
        state = load_state(jid)
        state.update({"mode": mode, "query": topic, "vector": vector, "target": tgt})

        if mode == "wikipedia":
            run_wikipedia(topic, vector, tgt, state, dry_run)
        elif mode == "search":
            run_search(topic, vector, tgt, state, dry_run)
        else:
            log(f"--topics-file only supports wikipedia and search modes (got '{mode}')", "ERROR")
            return

    log(f"Topics file complete: {len(lines)} topics processed")
    notify(f":white_check_mark: *Topics file complete* — {len(lines)} topics ingested")


HELP = """
Nova Universal Ingest Engine v{version}
======================================

MODES
-----
  wikipedia  TOPIC         BFS Wikipedia crawl from TOPIC article, follows
                           all links recursively until TARGET chunks reached.

  search     QUERY         Search SearXNG with QUERY, scrape top results,
                           ingest content. Good for specific fact sets.

  video      URL           Download video(s) from URL (any yt-dlp supported
                           site), extract audio, transcribe with MLX Whisper,
                           ingest transcript. URL can be a channel, playlist,
                           or single video.
                           Add --download-dir to skip ingest and save files only.

  discover   SUBJECT       Pick NUM_SITES random supported sites, search for
                           SUBJECT, find PER_SITE videos each, show a list,
                           then download and ingest.
                           Use --yes/-y to skip the confirmation prompt.
                           Use --download-dir to save files without ingesting.

  url        URL           Fetch and ingest a single web page.

  file       PATH          Ingest a local text/markdown file.

OPTIONS
-------
  --channel NAME       Channel name for video mode (used in file paths)
  --source  VECTOR     Force a specific memory source/vector name
                       (default: auto-selected from existing vectors)
  --target  N          Target number of memory chunks (default: 10000)
  --sites   N          Number of random sites for discover mode (default: 3)
  --per-site N         Videos per site in discover mode (default: 5)
  --download-dir PATH  Download video files to PATH; skip transcription/ingest.
                       Perfect for personal, private, or large batch downloads.
  --topics-file PATH   Batch mode: read a file of topics (one per line) and
                       run wikipedia or search mode for each. Use with mode
                       wikipedia or search. Line format:
                         Topic
                         Topic[TAB]source_vector
                         Topic[TAB]source_vector[TAB]target_chunks
                       Lines starting with # are comments.
  --yes / -y           Skip confirmation prompt (non-interactive / cron mode).
                       Also auto-confirmed when stdin is not a tty.
  --local-only         Force local MLX whisper for transcription (skip cloud).
                       By default, non-PII content uses Deepgram Nova-2 cloud
                       transcription to avoid GPU contention with Ollama.
  --dry-run            Preview without writing anything
  --resume             Continue the last interrupted job
  --restart            Restart the last job, retrying any failures
  --status             Show current job state
  --list-vectors       List all existing memory source names

VECTOR AUTO-SELECTION
---------------------
  Automatically picks the best existing memory source. Never creates generic
  vectors like "wikipedia_general". Uses keyword + semantic similarity.
  Override with --source if needed.

VIDEO OUTPUT PATHS
------------------
  Default (ingest mode):
    /Volumes/external/videos/TVShows/<Channel>/Season 01/S01E0001 - <Title>.mp4
    Episodes numbered by upload date (oldest = E0001).

  With --download-dir /path:
    /path/<Title> [VideoID].mp4
    No transcription or memory ingest -- just the file.

SUPPORTED DISCOVER SITES
------------------------
  YouTube, Vimeo, DailyMotion, Rumble, Odysee, BitChute, PeerTube,
  TED Talks, Internet Archive

EXAMPLES
--------
  # Ingest 10,000 Wikipedia chunks about physics
  nova_ingest.py wikipedia "Physics"

  # Search and ingest facts about demonology
  nova_ingest.py search "demonology facts history occult"

  # Download and ingest a YouTube channel
  nova_ingest.py video "https://www.youtube.com/@ForgottenWeapons" \\
                 --channel "Forgotten Weapons"

  # Just download a single video to ~/Downloads (no ingest)
  nova_ingest.py video "https://youtu.be/dQw4w9WgXcQ" --download-dir ~/Downloads

  # Discover videos about medieval warfare (interactive confirm)
  nova_ingest.py discover "medieval warfare siege weapons" --sites 3 --per-site 5

  # Same, non-interactive (cron / command line)
  nova_ingest.py discover "medieval warfare" --sites 3 --per-site 5 --yes

  # Download-only discover to a private directory
  nova_ingest.py discover "vintage photography" --sites 2 --per-site 3 \\
                 --download-dir /Volumes/Data/private --yes

  # Ingest a single web page
  nova_ingest.py url "https://en.wikipedia.org/wiki/Siege_of_Constantinople"

  # Ingest a local file
  nova_ingest.py file ~/Documents/research.txt --source research_notes

  # Batch-ingest many Wikipedia topics from a file
  nova_ingest.py wikipedia --topics-file ~/.openclaw/data/wiki_topics.tsv

  # Example topics file content:
  #   Physics\tphysics_mechanics\t10000
  #   World War II\tww2_nations\t10000
  #   Chess\tchess\t5000
  #   # comment line

  # Dry run
  nova_ingest.py wikipedia "Quantum mechanics" --dry-run

  # Resume / restart
  nova_ingest.py --resume
  nova_ingest.py --restart

  # List all vectors
  nova_ingest.py --list-vectors
"""

def main():
    p = argparse.ArgumentParser(
        description="Nova Universal Ingest Engine v" + VERSION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    p.add_argument("mode", nargs="?",
                   choices=["wikipedia", "search", "video", "discover", "url", "file"])
    p.add_argument("query",           nargs="?")
    p.add_argument("--channel",       help="Channel name for video mode")
    p.add_argument("--source",        help="Force vector/source name")
    p.add_argument("--target",        type=int, default=10000)
    p.add_argument("--sites",         type=int, default=3)
    p.add_argument("--per-site",      type=int, default=5)
    p.add_argument("--download-dir",  metavar="PATH",
                   help="Save files here only -- skip transcription and ingest")
    p.add_argument("--dateafter",     metavar="YYYYMMDD",
                   help="Video mode: only include videos uploaded on or after this date")
    p.add_argument("--topics-file",   metavar="PATH",
                   help="Batch: run mode for each topic in file (wikipedia/search)")
    p.add_argument("--yes", "-y",     action="store_true",
                   help="Skip confirmation prompt")
    p.add_argument("--local-only",    action="store_true",
                   help="Force local MLX whisper transcription (skip cloud)")
    p.add_argument("--spicy",         action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--random",        action="store_true",
                   help="Wikipedia mode: pick a random article as the seed topic")
    p.add_argument("--timeout-hours", type=float, default=0,
                   help="Stop after N hours regardless of target")
    p.add_argument("--dry-run",       action="store_true")
    p.add_argument("--resume",        action="store_true")
    p.add_argument("--restart",       action="store_true")
    p.add_argument("--status",        action="store_true")
    p.add_argument("--list-vectors",  action="store_true")
    p.add_argument("-h", "--help",    action="store_true")
    args = p.parse_args()

    if args.help or (not args.mode and not args.resume and not args.restart
                     and not args.status and not args.list_vectors
                     and not args.topics_file):
        print(HELP.format(version=VERSION))
        return

    if args.list_vectors:
        for v in get_existing_vectors():
            print(f"  {v}")
        return

    if args.topics_file:
        if args.mode not in ("wikipedia", "search", None):
            print("Error: --topics-file only works with wikipedia or search mode")
            return
        run_topics_file(
            args.topics_file,
            args.mode or "wikipedia",
            args.target,
            args.dry_run,
        )
        return

    if args.status:
        jid = latest_job_id()
        if jid:
            print(json.dumps(load_state(jid), indent=2))
        else:
            print("No active jobs.")
        return

    # Upgrade yt-dlp before doing any video work
    if args.mode in ("video", "discover") or args.resume or args.restart:
        _upgrade_ytdlp()

    if args.resume:
        jid = latest_job_id()
        if not jid:
            print("No job to resume.")
            return
        state        = load_state(jid)
        args.mode    = state.get("mode")
        args.query   = state.get("query")
        args.channel = state.get("channel", "")
        args.source  = state.get("vector")
        args.target  = state.get("target", 10000)

    elif args.restart:
        jid = latest_job_id()
        if not jid:
            print("No job to restart.")
            return
        state        = load_state(jid)
        failed       = state.get("failed_urls", [])
        args.mode    = state.get("mode")
        args.query   = state.get("query")
        args.channel = state.get("channel", "")
        args.source  = state.get("vector")
        args.target  = state.get("target", 10000)
        state.update({
            "done_urls": [], "done_hashes": [],
            "chunks_total": 0, "items_done": 0, "failed_urls": failed,
        })

    else:
        if not args.mode:
            print(HELP.format(version=VERSION))
            return
        if args.random and args.mode == "wikipedia":
            topic = wiki_random_article()
            if not topic:
                print("Error: could not find a suitable random article")
                return
            args.query = topic
            args.source = args.source or _derive(topic)
            log(f"Random Wikipedia topic: '{topic}' -> vector '{args.source}'")
        elif not args.query and args.mode not in ("url", "file"):
            print(f"Error: query required for mode '{args.mode}'")
            return
        jid   = hashlib.md5(f"{args.mode}:{args.query}:{time.time()}".encode()).hexdigest()[:12]
        state = load_state(jid)
        state.update({
            "mode":    args.mode,
            "query":   args.query or "",
            "channel": args.channel or "",
            "target":  args.target,
        })

    existing = get_existing_vectors()
    vector   = args.source or state.get("vector", "")
    if not vector and not args.download_dir:
        # For url mode, extract a readable topic from the URL path rather than
        # passing the full URL (which produces vectors like "httpsenwikipediaorg...")
        topic = args.query or args.channel or ""
        if args.mode == "url" and topic.startswith("http"):
            # Use the last path segment, decoded and humanised
            import urllib.parse as _up
            path_slug = _up.unquote(topic.rstrip("/").split("/")[-1])
            topic = path_slug.replace("_", " ").replace("-", " ")
        vector = auto_select_vector(topic, topic, existing)
    elif not vector:
        vector = "download_only"
    state["vector"] = vector
    save_state(jid, state)

    log(f"Job {jid}: mode={args.mode} query='{args.query}' "
        f"vector='{vector}' target={args.target}" +
        (f" [download-dir={args.download_dir}]" if args.download_dir else "") +
        (" [DRY RUN]" if args.dry_run else ""))

    if args.mode == "wikipedia":
        run_wikipedia(args.query, vector, args.target, state, args.dry_run,
                      timeout_hours=args.timeout_hours)
    elif args.mode == "search":
        run_search(args.query, vector, args.target, state, args.dry_run)
    elif args.mode == "video":
        ch = (args.channel or
              (args.query.split("/")[-1].lstrip("@") if args.query else "Unknown"))
        run_video(args.query, ch, vector, args.target, state, args.dry_run,
                  download_dir=args.download_dir, dateafter=args.dateafter,
                  local_only=args.local_only)
    elif args.mode == "discover":
        run_discover(
            args.query, vector,
            args.sites, getattr(args, "per_site", 5),
            state, args.dry_run,
            yes=args.yes,
            download_dir=args.download_dir,
            _alt_pool=getattr(args, "spicy", False),
            local_only=args.local_only,
        )
    elif args.mode == "url":
        run_url(args.query, vector, state, args.dry_run)
    elif args.mode == "file":
        run_file(args.query, vector, state, args.dry_run)

if __name__ == "__main__":
    main()
