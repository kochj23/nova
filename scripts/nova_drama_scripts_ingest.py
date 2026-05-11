#!/usr/bin/env python3
"""nova_drama_scripts_ingest.py — Top 100 drama movie scripts → Nova's vector DB. Written by Jordan Koch."""

import json, random, re, sys, time, urllib.request
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import nova_config

MEMORY_URL    = nova_config.VECTOR_URL
SLACK_CHANNEL = nova_config.SLACK_NOTIFY
STATE_FILE    = Path.home() / ".openclaw/workspace/state/drama_scripts_state.json"
LOG_FILE      = Path.home() / ".openclaw/logs/nova_drama_scripts_ingest.log"
CHUNK_WORDS, MIN_CHUNK, DELAY = 400, 40, 2.0

MOVIES = [
    ("The Godfather",                   1972, "drama",       "Godfather,-The"),
    ("The Shawshank Redemption",        1994, "drama",       "Shawshank-Redemption,-The"),
    ("Schindler's List",                1993, "drama",       "Schindler's-List"),
    ("Casablanca",                      1942, "drama",       "Casablanca"),
    ("Citizen Kane",                    1941, "drama",       None),
    ("12 Angry Men",                    1957, "drama",       "12-Angry-Men"),
    ("To Kill a Mockingbird",           1962, "drama",       "To-Kill-a-Mockingbird"),
    ("One Flew Over the Cuckoo's Nest", 1975, "drama",       "One-Flew-Over-the-Cuckoo's-Nest"),
    ("Network",                         1976, "drama",       "Network"),
    ("All About Eve",                   1950, "drama",       None),
    ("Sunset Boulevard",                1950, "drama",       "Sunset-Blvd."),
    ("American Beauty",                 1999, "drama",       "American-Beauty"),
    ("Forrest Gump",                    1994, "drama",       "Forrest-Gump"),
    ("The Godfather Part II",           1974, "drama",       "Godfather-Part-II,-The"),
    ("Goodfellas",                      1990, "crime_drama",  "Goodfellas"),
    ("Chinatown",                       1974, "crime_drama",  "Chinatown"),
    ("Taxi Driver",                     1976, "drama",       "Taxi-Driver"),
    ("Raging Bull",                     1980, "drama",       "Raging-Bull"),
    ("Apocalypse Now",                  1979, "drama",       "Apocalypse-Now"),
    ("The Deer Hunter",                 1978, "drama",       None),
    ("Kramer vs. Kramer",               1979, "drama",       None),
    ("Ordinary People",                 1980, "drama",       None),
    ("On the Waterfront",               1954, "drama",       None),
    ("A Streetcar Named Desire",        1951, "drama",       None),
    ("Who's Afraid of Virginia Woolf?", 1966, "drama",       None),
    ("Requiem for a Dream",             2000, "drama",       "Requiem-for-a-Dream"),
    ("Boogie Nights",                   1997, "drama",       "Boogie-Nights"),
    ("Magnolia",                        1999, "drama",       "Magnolia"),
    ("There Will Be Blood",             2007, "drama",       "There-Will-Be-Blood"),
    ("No Country for Old Men",          2007, "crime_drama",  "No-Country-for-Old-Men"),
    ("Fargo",                           1996, "crime_drama",  "Fargo"),
    ("The Big Lebowski",                1998, "drama",       "Big-Lebowski,-The"),
    ("Barton Fink",                     1991, "drama",       "Barton-Fink"),
    ("Blood Simple",                    1984, "crime_drama",  "Blood-Simple"),
    ("Adaptation",                      2002, "drama",       "Adaptation"),
    ("Mulholland Drive",                2001, "drama",       "Mulholland-Drive"),
    ("Blue Velvet",                     1986, "drama",       "Blue-Velvet"),
    ("Lost Highway",                    1997, "drama",       None),
    ("The Elephant Man",                1980, "drama",       None),
    ("Persona",                         1966, "drama",       None),
    ("Wild Strawberries",               1957, "drama",       None),
    ("The Seventh Seal",                1957, "drama",       None),
    ("8 1/2",                           1963, "drama",       None),
    ("La Dolce Vita",                   1960, "drama",       None),
    ("Amarcord",                        1973, "drama",       None),
    ("Jules and Jim",                   1962, "drama",       None),
    ("Au Revoir les Enfants",           1987, "drama",       None),
    ("The 400 Blows",                   1959, "drama",       None),
    ("Breathless",                      1960, "drama",       None),
    ("M",                               1931, "crime_drama",  None),
    ("The Third Man",                   1949, "crime_drama",  "Third-Man,-The"),
    ("Brief Encounter",                 1945, "drama",       None),
    ("Lawrence of Arabia",              1962, "drama",       None),
    ("Gandhi",                          1982, "drama",       None),
    ("Amadeus",                         1984, "drama",       "Amadeus"),
    ("Rain Man",                        1988, "drama",       "Rain-Man"),
    ("Good Will Hunting",               1997, "drama",       "Good-Will-Hunting"),
    ("A Beautiful Mind",                2001, "drama",       "Beautiful-Mind,-A"),
    ("The Social Network",              2010, "drama",       "Social-Network,-The"),
    ("Spotlight",                       2015, "drama",       None),
    ("12 Years a Slave",                2013, "drama",       None),
    ("Moonlight",                       2016, "drama",       None),
    ("Parasite",                        2019, "drama",       None),
    ("Marriage Story",                  2019, "drama",       None),
    ("Marriage",                        1979, "drama",       None),
    ("Kramer vs. Kramer",               1979, "drama",       None),
    ("Philadelphia",                    1993, "drama",       "Philadelphia"),
    ("The Accused",                     1988, "drama",       None),
    ("Mystic River",                    2003, "crime_drama",  "Mystic-River"),
    ("Prisoners",                       2013, "crime_drama",  None),
    ("Manchester by the Sea",           2016, "drama",       None),
    ("Three Billboards Outside Ebbing", 2017, "drama",       None),
    ("Joker",                           2019, "drama",       None),
    ("The Master",                      2012, "drama",       None),
    ("Inherent Vice",                   2014, "drama",       None),
    ("The Revenant",                    2015, "drama",       None),
    ("Dunkirk",                         2017, "action",      None),
    ("1917",                            2019, "drama",       None),
    ("Platoon",                         1986, "drama",       "Platoon"),
    ("Full Metal Jacket",               1987, "drama",       "Full-Metal-Jacket"),
    ("The Hurt Locker",                 2008, "drama",       None),
    ("Zero Dark Thirty",                2012, "drama",       None),
    ("Hacksaw Ridge",                   2016, "drama",       None),
    ("Whiplash",                        2014, "drama",       None),
    ("Black Swan",                      2010, "drama",       None),
    ("The Wrestler",                    2008, "drama",       None),
    ("Atonement",                       2007, "drama",       None),
    ("The English Patient",             1996, "drama",       "English-Patient,-The"),
    ("The Remains of the Day",          1993, "drama",       None),
    ("The Age of Innocence",            1993, "drama",       None),
    ("Eyes Wide Shut",                  1999, "drama",       "Eyes-Wide-Shut"),
    ("Barry Lyndon",                    1975, "drama",       None),
    ("Doctor Zhivago",                  1965, "drama",       None),
    ("The African Queen",               1951, "drama",       None),
    ("All Quiet on the Western Front",  1930, "drama",       None),
    ("The Best Years of Our Lives",     1946, "drama",       None),
    ("It's a Wonderful Life",           1946, "drama",       None),
    ("Sunset Boulevard",                1950, "drama",       "Sunset-Blvd."),
    ("Vertigo",                         1958, "drama",       "Vertigo"),
    ("Rear Window",                     1954, "drama",       "Rear-Window"),
    ("Psycho",                          1960, "horror",      "Psycho"),
    ("Notorious",                       1946, "drama",       None),
]

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[drama_ingest {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        open(LOG_FILE, "a").write(line + "\n")
    except: pass

def load_state():
    try:
        if STATE_FILE.exists(): return json.loads(STATE_FILE.read_text())
    except: pass
    return {"done": {}}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

def fetch_url(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            try: return raw.decode("utf-8")
            except: return raw.decode("latin-1", errors="ignore")
    except: return None

def fetch_imsdb(slug):
    html = fetch_url(f"https://imsdb.com/scripts/{slug}.html")
    if not html: return None
    m = re.search(r'<pre>(.*?)</pre>', html, re.DOTALL|re.IGNORECASE)
    if not m: m = re.search(r'class="scrtext".*?<pre>(.*?)</pre>', html, re.DOTALL|re.IGNORECASE)
    if m:
        text = re.sub(r'<[^>]+>', ' ', m.group(1))
        text = re.sub(r'&\w+;', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text if len(text) > 500 else None
    return None

def fetch_script(title, slug):
    if slug:
        t = fetch_imsdb(slug)
        if t: log(f"  IMSDB: {len(t.split())}w"); return t
        time.sleep(DELAY)
    auto = title.replace(" ","-").replace(":","").replace("'","").replace(",","").replace("/","")
    if auto != slug:
        t = fetch_imsdb(auto)
        if t: log(f"  IMSDB(auto): {len(t.split())}w"); return t
        time.sleep(DELAY)
    return None

def chunk_text(text):
    words = text.split()
    return [" ".join(words[i:i+CHUNK_WORDS]) for i in range(0,len(words),CHUNK_WORDS)
            if len(words[i:i+CHUNK_WORDS]) >= MIN_CHUNK]

def remember(text, source, meta):
    payload = json.dumps({"text":text[:2000],"source":source,"tier":"long_term","metadata":meta}).encode()
    req = urllib.request.Request(MEMORY_URL, data=payload, headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15): return True
    except: return False

def snippet(title, chunks):
    try:
        payload = json.dumps({"query":f"{title} drama film", "limit":10}).encode()
        req = urllib.request.Request(nova_config.VECTOR_URL.replace("/remember","/recall"),
            data=payload, headers={"Content-Type":"application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            ms = json.loads(r.read()).get("memories",[])
            if ms: return re.sub(r"^\[.*?\]\s*","",random.choice(ms).get("text",""))[:200]
    except: pass
    return re.sub(r"^\[.*?\]\s*","",random.choice(chunks))[:200] if chunks else None

def post_slack(msg):
    try: nova_config.post_both(msg, slack_channel=SLACK_CHANNEL)
    except: pass

def main():
    log(f"=== Drama Scripts Ingest started — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    state = load_state()
    total_ingested = total_chunks = skipped = not_found = 0

    post_slack(f":performing_arts: *Top 100 Drama Scripts Ingest — Starting*\n"
               f":film_frames: {len(MOVIES)} movies · IMSDB source → drama/crime_drama vectors")

    for i, (title, year, vector, slug) in enumerate(MOVIES):
        key = f"{title}_{year}"
        if key in state["done"]: skipped += 1; continue
        log(f"[{i+1}/{len(MOVIES)}] {title} ({year}) → {vector}")
        script = fetch_script(title, slug)
        if not script:
            log("  Not found")
            state["done"][key] = {"title":title,"year":year,"status":"not_found"}
            save_state(state); not_found += 1; time.sleep(DELAY); continue
        chunks = chunk_text(script)
        ingested = sum(1 for j,c in enumerate(chunks) if remember(
            f"[{title} ({year}) screenplay] {c}", vector,
            {"type":"movie_screenplay","title":title,"year":year,"chunk":j+1,
             "total_chunks":len(chunks),"ingested_date":datetime.now().strftime("%Y-%m-%d")}))
        state["done"][key] = {"title":title,"year":year,"vector":vector,"status":"ingested","chunks":ingested}
        save_state(state); total_ingested += 1; total_chunks += ingested
        log(f"  ✓ {ingested} chunks → {vector}")
        snip = snippet(title, chunks)
        lines = [f":performing_arts: *{title}* ({year}) → `{vector}`",
                 f":brain: {ingested} memories · {len(script.split()):,} words"]
        if snip: lines.append(f":thought_balloon: _\"{snip[:180]}…\"_")
        post_slack("\n".join(lines))
        time.sleep(DELAY)

    post_slack(f":performing_arts: *Drama Scripts Complete*\n"
               f":white_check_mark: {total_ingested} scripts · {total_chunks:,} chunks\n"
               f":x: {not_found} not found · :fast_forward: {skipped} already done")
    log(f"=== Done: {total_ingested} ingested, {not_found} not found ===")

if __name__ == "__main__":
    main()
