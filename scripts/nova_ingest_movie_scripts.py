#!/usr/bin/env python3
"""nova_ingest_movie_scripts.py — ingest screenplays for the top ~100 films from IMSDb.

For each top film that (a) has a script on IMSDb and (b) isn't already in the
`movie_scripts` vector, fetch the raw screenplay page and run it through
nova_ingest's `url` mode (fetch + chunk + dedup + embed -> nova_memories).

IMSDb structure: index links are "/Movie Scripts/<T> Script.html"; that page links
to the raw screenplay at "/scripts/<X>.html". We resolve the real raw link per film
(formats vary: hyphens, dropped "The", etc.) rather than guessing it.

Run:  nova_ingest_movie_scripts.py            # ingest missing top-100 scripts
      nova_ingest_movie_scripts.py --dry-run  # just report matches, no ingest
Written by Jordan Koch.
"""
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import psycopg2

VECTOR = "movie_scripts"
ALL_SCRIPTS = "https://imsdb.com/all-scripts.html"
UA = "Mozilla/5.0 (nova-ingest movie-scripts)"
INGEST = str(Path(__file__).parent / "nova_ingest.py")
import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_memories")

# IMDb Top ~100 (canonical greats). Matching to IMSDb naturally filters to those
# with an available script — not every title will be present.
TOP_FILMS = [
    "The Shawshank Redemption", "The Godfather", "The Dark Knight", "The Godfather Part II",
    "12 Angry Men", "Schindler's List", "The Lord of the Rings: The Return of the King",
    "Pulp Fiction", "The Lord of the Rings: The Fellowship of the Ring", "The Good, the Bad and the Ugly",
    "Forrest Gump", "Fight Club", "The Lord of the Rings: The Two Towers", "Inception",
    "Star Wars: Episode V - The Empire Strikes Back", "The Matrix", "Goodfellas", "One Flew Over the Cuckoo's Nest",
    "Se7en", "Seven Samurai", "It's a Wonderful Life", "The Silence of the Lambs", "Saving Private Ryan",
    "City of God", "Life Is Beautiful", "The Green Mile", "Interstellar", "Star Wars",
    "Terminator 2: Judgment Day", "Back to the Future", "Spirited Away", "The Pianist",
    "Psycho", "Parasite", "Gladiator", "The Lion King", "The Departed", "Whiplash",
    "American History X", "The Prestige", "Casablanca", "Grave of the Fireflies", "Once Upon a Time in the West",
    "Alien", "Rear Window", "Cinema Paradiso", "Apocalypse Now", "Memento", "Raiders of the Lost Ark",
    "The Great Dictator", "Django Unchained", "The Shining", "Paths of Glory", "WALL-E",
    "Aliens", "Dr. Strangelove", "The Usual Suspects", "Witness for the Prosecution", "Oldboy",
    "Toy Story", "Coco", "Princess Mononoke", "Avengers: Infinity War", "Once Upon a Time in America",
    "Reservoir Dogs", "Braveheart", "Requiem for a Dream", "Your Name", "Eternal Sunshine of the Spotless Mind",
    "2001: A Space Odyssey", "Singin' in the Rain", "Lawrence of Arabia", "The Hunt", "Amadeus",
    "A Clockwork Orange", "Taxi Driver", "Double Indemnity", "Toy Story 3", "Vertigo",
    "Full Metal Jacket", "Scarface", "Heat", "Inglourious Basterds", "1917", "Snatch",
    "L.A. Confidential", "Up", "Metropolis", "Blade Runner", "The Sixth Sense", "No Country for Old Men",
    "The Truman Show", "Gone with the Wind", "Jurassic Park", "No Country for Old Men", "The Thing",
    "Ford v Ferrari", "Kill Bill: Vol. 1", "Some Like It Hot", "The Big Lebowski", "Donnie Darko",
]


def norm(t: str) -> str:
    t = t.lower().strip()
    t = re.sub(r"^(the|a|an)\s+", "", t)
    t = re.sub(r",\s*(the|a|an)$", "", t)   # IMSDb indexes "Matrix, The"; TOP_FILMS says "The Matrix"
    t = re.sub(r"[^a-z0-9 ]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")


def imsdb_index():
    """{normalized title -> index href} for every script IMSDb lists."""
    html = fetch(ALL_SCRIPTS)
    out = {}
    for href in re.findall(r'href="(/Movie Scripts/[^"]+Script\.html)"', html):
        title = href.replace("/Movie Scripts/", "").replace(" Script.html", "")
        out[norm(title)] = href
    return out


def raw_script_url(index_href):
    """From a '/Movie Scripts/X Script.html' index page, find the raw /scripts/Y.html link."""
    page = fetch("https://imsdb.com" + urllib.parse.quote(index_href))
    m = re.search(r'href="(/scripts/[^"]+\.html)"', page)
    return "https://imsdb.com" + m.group(1) if m else None


def already_have():
    """Raw script URLs already ingested into movie_scripts (to skip)."""
    try:
        with psycopg2.connect(DSN) as c, c.cursor() as cur:
            cur.execute("SELECT DISTINCT metadata->>'url' FROM memories WHERE source=%s", (VECTOR,))
            return {r[0] for r in cur.fetchall() if r[0]}
    except Exception:
        return set()


def main():
    dry = "--dry-run" in sys.argv
    print(f"[movie_scripts] building IMSDb index…", flush=True)
    idx = imsdb_index()
    print(f"[movie_scripts] {len(idx)} scripts on IMSDb; matching {len(TOP_FILMS)} top films", flush=True)
    have = already_have()
    matched, ingested, missing = [], 0, []
    for film in TOP_FILMS:
        href = idx.get(norm(film))
        if not href:
            missing.append(film); continue
        matched.append(film)
        try:
            raw = raw_script_url(href)
        except Exception as e:
            print(f"  ! {film}: resolve failed ({e})", flush=True); continue
        if not raw:
            print(f"  ! {film}: no raw link", flush=True); continue
        if raw in have:
            print(f"  = {film}: already in memory, skip", flush=True); continue
        print(f"  + {film} -> {raw}", flush=True)
        if not dry:
            subprocess.run(["/opt/homebrew/bin/python3", INGEST, "url", raw,
                            "--source", VECTOR, "--target", "2000"],
                           timeout=600)
            ingested += 1
        time.sleep(2)  # be polite to IMSDb
    print(f"\n[movie_scripts] matched {len(matched)}/{len(TOP_FILMS)} on IMSDb; "
          f"ingested {ingested}; not on IMSDb: {len(missing)}", flush=True)
    if missing:
        print("  not available: " + ", ".join(missing[:30]), flush=True)


if __name__ == "__main__":
    main()
