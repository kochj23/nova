#!/usr/bin/env python3
"""
nova_action_scripts_ingest.py — Ingest top 100 action movie scripts into Nova's memory.

Same pattern as nova_scifi_scripts_ingest.py — fetches publicly available
screenplays from IMSDB/Simply Scripts, classifies by genre, ingests into
Nova's PostgreSQL vector DB. Notifications to #nova-notifications.

Written by Jordan Koch.
"""

import json, random, re, sys, time, urllib.request, urllib.parse
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

MEMORY_URL    = nova_config.VECTOR_URL
SLACK_CHANNEL = nova_config.SLACK_NOTIFY
STATE_FILE    = Path.home() / ".openclaw/workspace/state/action_scripts_state.json"
LOG_FILE      = Path.home() / ".openclaw/logs/nova_action_scripts_ingest.log"
CHUNK_WORDS   = 400
MIN_CHUNK     = 40
DELAY         = 2.0

MOVIES = [
    ("Die Hard",                           1988, "action",      "Die-Hard"),
    ("Mad Max: Fury Road",                 2015, "action",      None),
    ("The Dark Knight",                    2008, "action",      "Dark-Knight,-The"),
    ("Terminator 2: Judgment Day",         1991, "action",      "Terminator-2-Judgment-Day"),
    ("Raiders of the Lost Ark",            1981, "action",      "Raiders-of-the-Lost-Ark"),
    ("The Matrix",                         1999, "action",      "Matrix,-The"),
    ("Speed",                              1994, "action",      "Speed"),
    ("Point Break",                        1991, "action",      "Point-Break"),
    ("Hard Boiled",                        1992, "action",      None),
    ("Lethal Weapon",                      1987, "action",      "Lethal-Weapon"),
    ("Heat",                               1995, "crime_drama",  "Heat"),
    ("The French Connection",              1971, "crime_drama",  None),
    ("Bullitt",                            1968, "action",      None),
    ("The Good, the Bad and the Ugly",     1966, "action",      None),
    ("Dirty Harry",                        1971, "crime_drama",  "Dirty-Harry"),
    ("Predator",                           1987, "action",      "Predator"),
    ("Aliens",                             1986, "action",      "Aliens"),
    ("First Blood",                        1982, "action",      "First-Blood"),
    ("The Bourne Identity",                2002, "action",      "Bourne-Identity,-The"),
    ("Mission: Impossible",                1996, "action",      "Mission-Impossible"),
    ("Top Gun",                            1986, "action",      "Top-Gun"),
    ("Face/Off",                           1997, "action",      "Face-Off"),
    ("Con Air",                            1997, "action",      "Con-Air"),
    ("The Rock",                           1996, "action",      "Rock,-The"),
    ("Cliffhanger",                        1993, "action",      "Cliffhanger"),
    ("Under Siege",                        1992, "action",      "Under-Siege"),
    ("Total Recall",                       1990, "action",      "Total-Recall"),
    ("RoboCop",                            1987, "action",      "RoboCop"),
    ("Beverly Hills Cop",                  1984, "action",      "Beverly-Hills-Cop"),
    ("48 Hrs.",                            1982, "action",      None),
    ("Commando",                           1985, "action",      "Commando"),
    ("Rambo: First Blood Part II",         1985, "action",      "Rambo"),
    ("True Lies",                          1994, "action",      "True-Lies"),
    ("Bad Boys",                           1995, "action",      "Bad-Boys"),
    ("The Fugitive",                       1993, "action",      "Fugitive,-The"),
    ("Air Force One",                      1997, "action",      "Air-Force-One"),
    ("Executive Decision",                 1996, "action",      None),
    ("Sudden Impact",                      1983, "action",      None),
    ("Tango & Cash",                       1989, "action",      None),
    ("Running Man",                        1987, "action",      "Running-Man,-The"),
    ("Universal Soldier",                  1992, "action",      None),
    ("The Last Boy Scout",                 1991, "action",      "Last-Boy-Scout,-The"),
    ("Long Kiss Goodnight",                1996, "action",      "Long-Kiss-Goodnight,-The"),
    ("Desperado",                          1995, "action",      "Desperado"),
    ("El Mariachi",                        1992, "action",      "El-Mariachi"),
    ("John Wick",                          2014, "action",      None),
    ("Atomic Blonde",                      2017, "action",      None),
    ("The Raid: Redemption",               2011, "action",      None),
    ("Ong-Bak",                            2003, "action",      None),
    ("Crouching Tiger Hidden Dragon",      2000, "action",      None),
    ("The Man from Nowhere",               2010, "action",      None),
    ("Oldboy",                             2003, "crime_drama",  None),
    ("A Better Tomorrow",                  1986, "action",      None),
    ("Police Story",                       1985, "action",      None),
    ("Drunken Master II",                  1994, "action",      None),
    ("Enter the Dragon",                   1973, "action",      None),
    ("Fist of Fury",                       1972, "action",      None),
    ("Bloodsport",                         1988, "action",      None),
    ("Kickboxer",                          1989, "action",      None),
    ("Hard Target",                        1993, "action",      None),
    ("Broken Arrow",                       1996, "action",      "Broken-Arrow"),
    ("The Expendables",                    2010, "action",      None),
    ("Machete",                            2010, "action",      None),
    ("Crank",                              2006, "action",      None),
    ("Shoot 'Em Up",                       2007, "action",      None),
    ("Equilibrium",                        2002, "action",      "Equilibrium"),
    ("V for Vendetta",                     2005, "action",      "V-for-Vendetta"),
    ("The Warriors",                       1979, "action",      None),
    ("Escape from New York",               1981, "action",      "Escape-from-New-York"),
    ("Assault on Precinct 13",             1976, "action",      None),
    ("Dredd",                              2012, "action",      None),
    ("Robocop 2",                          1990, "action",      None),
    ("Black Hawk Down",                    2001, "action",      "Black-Hawk-Down"),
    ("Saving Private Ryan",                1998, "action",      "Saving-Private-Ryan"),
    ("Full Metal Jacket",                  1987, "action",      "Full-Metal-Jacket"),
    ("Apocalypse Now",                     1979, "action",      "Apocalypse-Now"),
    ("Platoon",                            1986, "action",      "Platoon"),
    ("The Hurt Locker",                    2008, "action",      None),
    ("Zero Dark Thirty",                   2012, "action",      None),
    ("Lone Survivor",                      2013, "action",      None),
    ("Act of Valor",                       2012, "action",      None),
    ("Sicario",                            2015, "crime_drama",  "Sicario"),
    ("No Country for Old Men",             2007, "crime_drama",  "No-Country-for-Old-Men"),
    ("Drive",                              2011, "crime_drama",  "Drive"),
    ("Collateral",                         2004, "crime_drama",  "Collateral"),
    ("Michael Clayton",                    2007, "crime_drama",  "Michael-Clayton"),
    ("Training Day",                       2001, "crime_drama",  "Training-Day"),
    ("Man on Fire",                        2004, "action",      "Man-on-Fire"),
    ("Taken",                              2008, "action",      None),
    ("Shooter",                            2007, "action",      None),
    ("Salt",                               2010, "action",      None),
    ("Haywire",                            2011, "action",      None),
    ("Hanna",                              2011, "action",      None),
    ("Edge of Tomorrow",                   2014, "action",      None),
    ("Elysium",                            2013, "action",      None),
    ("Pacific Rim",                        2013, "action",      None),
    ("Avengers: Infinity War",             2018, "action",      None),
    ("Captain America: Winter Soldier",    2014, "action",      None),
    ("Mission Impossible: Fallout",        2018, "action",      None),
    ("Fast Five",                          2011, "action",      None),
]


class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts, self._skip = [], False
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "header", "footer"): self._skip = True
    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "header", "footer"): self._skip = False
    def handle_data(self, data):
        if not self._skip:
            s = data.strip()
            if s: self._parts.append(s)
    @property
    def text(self): return " ".join(self._parts)


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[action_ingest {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f: f.write(line + "\n")
    except: pass


def load_state():
    try:
        if STATE_FILE.exists(): return json.loads(STATE_FILE.read_text())
    except: pass
    return {"done": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fetch_url(url, retries=3):
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                try: return raw.decode("utf-8")
                except: return raw.decode("latin-1", errors="ignore")
        except:
            if attempt < retries - 1: time.sleep(DELAY * (attempt + 1))
    return None


def fetch_imsdb(slug):
    html = fetch_url(f"https://imsdb.com/scripts/{slug}.html")
    if not html: return None
    m = re.search(r'<pre>(.*?)</pre>', html, re.DOTALL | re.IGNORECASE)
    if not m: m = re.search(r'class="scrtext".*?<pre>(.*?)</pre>', html, re.DOTALL | re.IGNORECASE)
    if m:
        text = re.sub(r'<[^>]+>', ' ', m.group(1))
        text = re.sub(r'&\w+;', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text if len(text) > 500 else None
    return None


def fetch_script(title, slug):
    if slug:
        t = fetch_imsdb(slug)
        if t:
            log(f"  Got script from IMSDB: {len(t.split())} words")
            return t
        time.sleep(DELAY)
    auto = title.replace(" ", "-").replace(":", "").replace("'", "").replace(",", "")
    if auto != slug:
        t = fetch_imsdb(auto)
        if t:
            log(f"  Got script from IMSDB (auto): {len(t.split())} words")
            return t
        time.sleep(DELAY)
    return None


def chunk_text(text):
    words = text.split()
    return [" ".join(words[i:i+CHUNK_WORDS]) for i in range(0, len(words), CHUNK_WORDS)
            if len(words[i:i+CHUNK_WORDS]) >= MIN_CHUNK]


def remember(text, source, metadata):
    payload = json.dumps({"text": text[:2000], "source": source,
                          "tier": "long_term", "metadata": metadata}).encode()
    req = urllib.request.Request(MEMORY_URL, data=payload,
                                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15): return True
    except: return False


def random_snippet(title, chunks):
    payload = json.dumps({"query": f"{title} movie action screenplay", "limit": 10}).encode()
    req = urllib.request.Request(
        nova_config.VECTOR_URL.replace("/remember", "/recall"),
        data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            memories = json.loads(r.read()).get("memories", [])
            if memories:
                return re.sub(r"^\[.*?\]\s*", "", random.choice(memories).get("text",""))[:200]
    except: pass
    if chunks:
        return re.sub(r"^\[.*?\]\s*", "", random.choice(chunks))[:200]
    return None


def post_slack(msg):
    try: nova_config.post_both(msg, slack_channel=SLACK_CHANNEL)
    except Exception as e: log(f"Slack error: {e}")


def main():
    log(f"=== Action Scripts Ingest started — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    state = load_state()
    total_ingested = total_chunks = skipped = not_found = 0

    post_slack(
        f":fire: *Top 100 Action Movie Scripts Ingest — Starting*\n"
        f":film_frames: {len(MOVIES)} movies queued · IMSDB source\n"
        f":brain: Classified into action/crime_drama/sci_fi vectors"
    )

    for i, (title, year, vector, slug) in enumerate(MOVIES):
        key = f"{title}_{year}"
        if key in state["done"]:
            skipped += 1
            continue

        log(f"[{i+1}/{len(MOVIES)}] {title} ({year}) → {vector}")
        script = fetch_script(title, slug)

        if not script:
            log(f"  No script found")
            state["done"][key] = {"title": title, "year": year, "status": "not_found"}
            save_state(state)
            not_found += 1
            time.sleep(DELAY)
            continue

        chunks = chunk_text(script)
        ingested = sum(1 for j, chunk in enumerate(chunks) if remember(
            f"[{title} ({year}) screenplay] {chunk}", vector,
            {"type": "movie_screenplay", "title": title, "year": year,
             "chunk": j+1, "total_chunks": len(chunks),
             "ingested_date": datetime.now().strftime("%Y-%m-%d")}
        ))

        state["done"][key] = {"title": title, "year": year, "vector": vector,
                               "status": "ingested", "chunks": ingested}
        save_state(state)
        total_ingested += 1
        total_chunks += ingested
        log(f"  ✓ {ingested} chunks → {vector}")

        snippet = random_snippet(title, chunks)
        notif = [
            f":fire: *{title}* ({year}) → `{vector}`",
            f":brain: {ingested} memories stored · {len(script.split()):,} words",
        ]
        if snippet:
            notif.append(f":thought_balloon: _\"{snippet[:180]}…\"_")
        post_slack("\n".join(notif))
        time.sleep(DELAY)

    post_slack(
        f":fire: *Action Scripts Ingest — Complete*\n"
        f":white_check_mark: {total_ingested} scripts · {total_chunks:,} chunks\n"
        f":x: {not_found} not found · :fast_forward: {skipped} already done"
    )
    log(f"=== Complete: {total_ingested} ingested, {not_found} not found, {skipped} skipped ===")


if __name__ == "__main__":
    main()
