#!/usr/bin/env python3
"""nova_comedy_scripts_ingest.py — Top 100 comedy movie scripts → Nova's vector DB. Written by Jordan Koch."""

import json, random, re, sys, time, urllib.request
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import nova_config

MEMORY_URL    = nova_config.VECTOR_URL
SLACK_CHANNEL = nova_config.SLACK_NOTIFY
STATE_FILE    = Path.home() / ".openclaw/workspace/state/comedy_scripts_state.json"
LOG_FILE      = Path.home() / ".openclaw/logs/nova_comedy_scripts_ingest.log"
CHUNK_WORDS, MIN_CHUNK, DELAY = 400, 40, 2.0

MOVIES = [
    ("Some Like It Hot",                1959, "comedy", None),
    ("Dr. Strangelove",                 1964, "comedy", "Dr.-Strangelove"),
    ("Annie Hall",                      1977, "comedy", "Annie-Hall"),
    ("Monty Python and the Holy Grail", 1975, "comedy", "Monty-Python-and-the-Holy-Grail"),
    ("Blazing Saddles",                 1974, "comedy", "Blazing-Saddles"),
    ("Young Frankenstein",              1974, "comedy", "Young-Frankenstein"),
    ("The Producers",                   1967, "comedy", None),
    ("Groundhog Day",                   1993, "comedy", "Groundhog-Day"),
    ("Tootsie",                         1982, "comedy", "Tootsie"),
    ("The Philadelphia Story",          1940, "comedy", None),
    ("Bringing Up Baby",                1938, "comedy", None),
    ("His Girl Friday",                 1940, "comedy", None),
    ("It Happened One Night",           1934, "comedy", None),
    ("Singin' in the Rain",             1952, "comedy", None),
    ("The General",                     1926, "comedy", None),
    ("Modern Times",                    1936, "comedy", None),
    ("City Lights",                     1931, "comedy", None),
    ("Sullivan's Travels",              1941, "comedy", None),
    ("The Apartment",                   1960, "comedy", "Apartment,-The"),
    ("Withnail and I",                  1987, "comedy", "Withnail-and-I"),
    ("A Fish Called Wanda",             1988, "comedy", "Fish-Called-Wanda,-A"),
    ("Four Weddings and a Funeral",     1994, "comedy", "Four-Weddings-and-a-Funeral"),
    ("Notting Hill",                    1999, "comedy", None),
    ("About a Boy",                     2002, "comedy", None),
    ("Bridget Jones's Diary",           2001, "comedy", None),
    ("Clueless",                        1995, "comedy", "Clueless"),
    ("Mean Girls",                      2004, "comedy", "Mean-Girls"),
    ("Legally Blonde",                  2001, "comedy", "Legally-Blonde"),
    ("Bridesmaids",                     2011, "comedy", None),
    ("The Hangover",                    2009, "comedy", None),
    ("Superbad",                        2007, "comedy", "Superbad"),
    ("Knocked Up",                      2007, "comedy", None),
    ("The 40-Year-Old Virgin",          2005, "comedy", None),
    ("Anchorman",                       2004, "comedy", None),
    ("Step Brothers",                   2008, "comedy", None),
    ("Talladega Nights",                2006, "comedy", None),
    ("Blades of Glory",                 2007, "comedy", None),
    ("Old School",                      2003, "comedy", None),
    ("Animal House",                    1978, "comedy", "Animal-House"),
    ("Caddyshack",                      1980, "comedy", "Caddyshack"),
    ("Ferris Bueller's Day Off",        1986, "comedy", "Ferris-Bueller's-Day-Off"),
    ("Risky Business",                  1983, "comedy", "Risky-Business"),
    ("Fast Times at Ridgemont High",    1982, "comedy", "Fast-Times-at-Ridgemont-High"),
    ("The Breakfast Club",              1985, "comedy", "Breakfast-Club,-The"),
    ("Sixteen Candles",                 1984, "comedy", "Sixteen-Candles"),
    ("National Lampoon's Vacation",     1983, "comedy", "National-Lampoon's-Vacation"),
    ("Home Alone",                      1990, "comedy", "Home-Alone"),
    ("Mrs. Doubtfire",                  1993, "comedy", "Mrs.-Doubtfire"),
    ("Liar Liar",                       1997, "comedy", "Liar-Liar"),
    ("The Truman Show",                 1998, "comedy", "Truman-Show,-The"),
    ("Ace Ventura: Pet Detective",      1994, "comedy", None),
    ("Dumb and Dumber",                 1994, "comedy", "Dumb-and-Dumber"),
    ("There's Something About Mary",   1998, "comedy", "There's-Something-About-Mary"),
    ("American Pie",                    1999, "comedy", "American-Pie"),
    ("Big Daddy",                       1999, "comedy", None),
    ("Billy Madison",                   1995, "comedy", None),
    ("Happy Gilmore",                   1996, "comedy", None),
    ("Wayne's World",                   1992, "comedy", "Wayne's-World"),
    ("Bill & Ted's Excellent Adventure",1989, "comedy", "Bill-and-Ted's-Excellent-Adventure"),
    ("Ghostbusters",                    1984, "comedy", "Ghostbusters"),
    ("Stripes",                         1981, "comedy", "Stripes"),
    ("Three Amigos",                    1986, "comedy", None),
    ("Planes, Trains and Automobiles",  1987, "comedy", "Planes,-Trains-and-Automobiles"),
    ("Uncle Buck",                      1989, "comedy", None),
    ("Fletch",                          1985, "comedy", "Fletch"),
    ("Beverly Hills Cop",               1984, "comedy", "Beverly-Hills-Cop"),
    ("Trading Places",                  1983, "comedy", None),
    ("48 Hrs.",                         1982, "comedy", None),
    ("Tango & Cash",                    1989, "comedy", None),
    ("Police Academy",                  1984, "comedy", None),
    ("Pink Panther",                    1963, "comedy", None),
    ("The Return of the Pink Panther",  1975, "comedy", None),
    ("Naked Gun",                       1988, "comedy", "Naked-Gun,-The"),
    ("Airplane!",                       1980, "comedy", "Airplane!"),
    ("Hot Shots!",                      1991, "comedy", None),
    ("Austin Powers",                   1997, "comedy", "Austin-Powers:-International-Man-of-Mystery"),
    ("Austin Powers: The Spy Who Shagged Me", 1999, "comedy", None),
    ("Galaxy Quest",                    1999, "comedy", "Galaxy-Quest"),
    ("Men in Black",                    1997, "comedy", "Men-in-Black"),
    ("Ghostbusters II",                 1989, "comedy", None),
    ("Spaceballs",                      1987, "comedy", "Spaceballs"),
    ("Scrooged",                        1988, "comedy", None),
    ("Funny People",                    2009, "comedy", None),
    ("I Love You Man",                  2009, "comedy", None),
    ("Forgetting Sarah Marshall",       2008, "comedy", None),
    ("This Is the End",                 2013, "comedy", None),
    ("Pineapple Express",               2008, "comedy", None),
    ("Harold & Kumar Go to White Castle",2004,"comedy", None),
    ("Eurotrip",                        2004, "comedy", None),
    ("National Lampoon's Animal House", 1978, "comedy", None),
    ("PCU",                             1994, "comedy", None),
    ("Tommy Boy",                       1995, "comedy", None),
    ("Black Sheep",                     1996, "comedy", None),
    ("Private Parts",                   1997, "comedy", None),
    ("Election",                        1999, "comedy", "Election"),
    ("Rushmore",                        1998, "comedy", "Rushmore"),
    ("The Royal Tenenbaums",            2001, "comedy", "Royal-Tenenbaums,-The"),
    ("Bottle Rocket",                   1996, "comedy", None),
    ("Adaptation",                      2002, "comedy", "Adaptation"),
    ("Being John Malkovich",            1999, "comedy", "Being-John-Malkovich"),
    ("Eternal Sunshine of the Spotless Mind", 2004, "comedy", "Eternal-Sunshine-of-the-Spotless-Mind"),
]

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[comedy_ingest {ts}] {msg}"
    print(line, flush=True)
    try: LOG_FILE.parent.mkdir(parents=True, exist_ok=True); open(LOG_FILE,"a").write(line+"\n")
    except: pass

def load_state():
    try:
        if STATE_FILE.exists(): return json.loads(STATE_FILE.read_text())
    except: pass
    return {"done":{}}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

def fetch_url(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
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
        text = re.sub(r'<[^>]+>',' ',m.group(1)); text = re.sub(r'&\w+;',' ',text)
        text = re.sub(r'\s+',' ',text).strip()
        return text if len(text)>500 else None
    return None

def fetch_script(title, slug):
    if slug:
        t = fetch_imsdb(slug)
        if t: log(f"  IMSDB: {len(t.split())}w"); return t
        time.sleep(DELAY)
    auto = title.replace(" ","-").replace(":","").replace("'","").replace(",","").replace("/","").replace("!","")
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
    payload = json.dumps({"text":nova_config.truncate_at_boundary(text),"source":source,"tier":"long_term","metadata":meta}).encode()
    req = urllib.request.Request(MEMORY_URL, data=payload, headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15): return True
    except: return False

def snippet(title, chunks):
    try:
        payload = json.dumps({"query":f"{title} comedy film","limit":10}).encode()
        req = urllib.request.Request(nova_config.VECTOR_URL.replace("/remember","/recall"),
            data=payload,headers={"Content-Type":"application/json"},method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            ms = json.loads(r.read()).get("memories",[])
            if ms: return re.sub(r"^\[.*?\]\s*","",random.choice(ms).get("text",""))[:200]
    except: pass
    return re.sub(r"^\[.*?\]\s*","",random.choice(chunks))[:200] if chunks else None

def post_slack(msg):
    try: nova_config.post_both(msg, slack_channel=SLACK_CHANNEL)
    except: pass

def main():
    log(f"=== Comedy Scripts Ingest started — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    state = load_state()
    total_ingested = total_chunks = skipped = not_found = 0

    post_slack(f":laughing: *Top 100 Comedy Scripts Ingest — Starting*\n"
               f":film_frames: {len(MOVIES)} movies · IMSDB source → comedy vector")

    for i,(title,year,vector,slug) in enumerate(MOVIES):
        key = f"{title}_{year}"
        if key in state["done"]: skipped += 1; continue
        log(f"[{i+1}/{len(MOVIES)}] {title} ({year}) → {vector}")
        script = fetch_script(title, slug)
        if not script:
            log("  Not found")
            state["done"][key] = {"title":title,"year":year,"status":"not_found"}
            save_state(state); not_found+=1; time.sleep(DELAY); continue
        chunks = chunk_text(script)
        ingested = sum(1 for j,c in enumerate(chunks) if remember(
            f"[{title} ({year}) screenplay] {c}", vector,
            {"type":"movie_screenplay","title":title,"year":year,"chunk":j+1,
             "total_chunks":len(chunks),"ingested_date":datetime.now().strftime("%Y-%m-%d")}))
        state["done"][key] = {"title":title,"year":year,"vector":vector,"status":"ingested","chunks":ingested}
        save_state(state); total_ingested+=1; total_chunks+=ingested
        log(f"  ✓ {ingested} chunks → {vector}")
        snip = snippet(title, chunks)
        lines = [f":laughing: *{title}* ({year}) → `{vector}`",
                 f":brain: {ingested} memories · {len(script.split()):,} words"]
        if snip: lines.append(f":thought_balloon: _\"{snip[:180]}…\"_")
        post_slack("\n".join(lines))
        time.sleep(DELAY)

    post_slack(f":laughing: *Comedy Scripts Complete*\n"
               f":white_check_mark: {total_ingested} scripts · {total_chunks:,} chunks\n"
               f":x: {not_found} not found")
    log(f"=== Done: {total_ingested} ingested, {not_found} not found ===")

if __name__ == "__main__":
    main()
