#!/usr/bin/env python3
"""
nova_tvshow_detector.py — distinguish REAL TV shows from YouTube channels in the
media-gardener prune list, by matching each show name against TVMaze (free, no key).

A strong match to a real *networked/cable* series (close name + premiered + network)
is almost certainly OTA/DVD content that got dumped into TVShows — irreplaceable, so
flag it to KEEP. Writes results to /tmp/tv_matches.tsv for review. Never deletes.
"""
import difflib
import json
import re
import time
import urllib.parse
import urllib.request

import psycopg2

conn = psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")
cur = conn.cursor()
cur.execute("SELECT DISTINCT show FROM media_prune_proposals WHERE status='proposed' ORDER BY show")
shows = [r[0] for r in cur.fetchall()]


def clean(s):
    s = re.sub(r'[^\x00-\x7F]+', '', s)          # strip emoji/non-ascii
    s = re.sub(r'#.*$', '', s)                    # strip trailing #tags
    s = re.sub(r'\s*\(\d{4}\)\s*', '', s)         # strip (YYYY)
    return s.strip()


def tvmaze(q):
    url = "https://api.tvmaze.com/singlesearch/shows?q=" + urllib.parse.quote(q)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2)
                continue
            return None
        except Exception:
            return None
    return None


matches, checked = [], 0
out = open("/tmp/tv_matches.tsv", "w")
for s in shows:
    q = clean(s)
    checked += 1
    if len(q) < 3:
        continue
    d = tvmaze(q)
    time.sleep(0.4)                               # respect TVMaze rate limit (~2/s)
    if not d:
        continue
    name = d.get("name", "") or ""
    premiered = d.get("premiered")
    net = (d.get("network") or {}).get("name") if d.get("network") else None
    web = (d.get("webChannel") or {}).get("name") if d.get("webChannel") else None
    ratio = difflib.SequenceMatcher(None, q.lower(), name.lower()).ratio()
    # strong = close name match to a real series with a premiere + a broadcast/cable network
    if ratio >= 0.82 and premiered and net:
        line = f"{s}\t{name}\t{premiered[:4]}\t{net}\t{ratio:.2f}"
        matches.append(line)
        out.write(line + "\n"); out.flush()
out.close()
print(f"[tv-detector] checked {checked} shows, found {len(matches)} strong real-TV matches. -> /tmp/tv_matches.tsv")
for m in matches:
    print("  " + m)
