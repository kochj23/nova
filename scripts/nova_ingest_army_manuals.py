#!/opt/homebrew/bin/python3
"""nova_ingest_army_manuals.py — public-domain U.S. Army training publications (FM / TM / TC / ATP ...)
from the Internet Archive's OCR text into Nova memory (Jordan 2026-10-08: "find and /ingest all of the
public-domain U.S. Army training publications").

HathiTrust holds the same scans but sits behind a Cloudflare bot check, so the Internet Archive is the
source. Most-downloaded first; resumable (nova_ops.ia_ingest_seen); stops at --target stored chunks.
Usage: nova_ingest_army_manuals.py [--target 50000] [--vector military_doctrine] [--dry-run]
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_ingest as ni  # noqa: E402

import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
UA = {"User-Agent": "NovaIngest/1.0 (personal knowledge base)"}
QUERY = ('mediatype:texts AND (title:("field manual") OR title:("training circular") OR title:("technical manual") '
         'OR subject:("field manual") OR subject:("military training")) '
         'AND (creator:(army) OR subject:(army) OR title:(army) OR publisher:(army))')
# A real Army publication says so in its title; drops conspiracy uploads and random scans that only match on subject.
PUB = re.compile(r"\b(FM|TM|TC|ATP|ADP|ADRP|ATTP|AR|ST|GTA|DA\s*PAM)[\s_-]*\d|field\s*manual|technical\s*manual|"
                 r"training\s*circular|army\s*training", re.I)
SKIP = re.compile(r"leaked|conspiracy|re-?education|fouo|for official use|classified", re.I)


MAX_PAGES = 100   # 500 rows/page; the real result set is a few thousand, so this only stops a runaway loop


def get(url, timeout=60, attempts=3):
    """GET with retry: archive.org 5xx/timeouts are common; backoff 5 s, 10 s, then re-raise."""
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()
        except Exception as e:
            if attempt == attempts - 1:
                raise
            ni.log(f"GET {url[:80]} failed ({e}); retry {attempt + 1}")
            time.sleep(5 * 2 ** attempt)


def _connect(attempts=3):
    """nova_ops connection with retry (PG failover blips), backoff 5 s / 10 s, re-raise on the last try."""
    for attempt in range(attempts):
        try:
            return psycopg2.connect(OPS_DSN, connect_timeout=10)
        except psycopg2.OperationalError as e:
            if attempt == attempts - 1:
                raise
            ni.log(f"PG connect failed ({e}); retry {attempt + 1}")
            time.sleep(5 * 2 ** attempt)


def search():
    """-> [(identifier, title, date)] most-downloaded first, filtered to real Army pubs."""
    out, page = [], 1
    while page <= MAX_PAGES:
        qs = urllib.parse.urlencode([("q", QUERY), ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "date"),
                                     ("rows", "500"), ("page", str(page)), ("sort[]", "downloads desc"),
                                     ("output", "json")])
        docs = json.loads(get("https://archive.org/advancedsearch.php?" + qs))["response"]["docs"]
        if not docs:
            return out
        for d in docs:
            t = str(d.get("title") or "")
            if PUB.search(t) and not SKIP.search(t):
                out.append((d["identifier"], t[:300], str(d.get("date") or "")[:10]))
        page += 1
    ni.log(f"search stopped at the {MAX_PAGES}-page cap")
    return out


def ocr_text(ident):
    ident = urllib.parse.quote(ident, safe="")   # identifiers come from the search API; keep them one path segment
    files = json.loads(get(f"https://archive.org/metadata/{ident}/files")).get("result", [])
    txt = next((f["name"] for f in files if f["name"].endswith("_djvu.txt")), None)
    if not txt:
        return ""
    return get(f"https://archive.org/download/{ident}/{urllib.parse.quote(txt)}", timeout=180).decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=50000)
    ap.add_argument("--vector", default="military_doctrine")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    oc = _connect(); oc.autocommit = True; cur = oc.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS ia_ingest_seen (identifier text PRIMARY KEY, title text,
                   chunks int, at timestamptz DEFAULT now())""")
    cur.execute("SELECT identifier FROM ia_ingest_seen")
    seen = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT coalesce(sum(chunks), 0) FROM ia_ingest_seen")
    stored = int(cur.fetchone()[0])

    pubs = [p for p in search() if p[0] not in seen]
    ni.notify(f":military_helmet: *Army training publications ingest* — {len(pubs)} manuals to go from the Internet "
              f"Archive -> `{a.vector}`, target {a.target:,} memories ({stored:,} already stored).")
    done_hashes, last = set(), time.time()
    for i, (ident, title, date) in enumerate(pubs, 1):
        if stored >= a.target or ni._shutdown:
            break
        try:
            text = ocr_text(ident)
        except Exception as e:  # noqa: BLE001 — one bad item must not stop the run
            ni.log(f"{ident}: fetch failed: {e}")
            continue
        n = 0
        meta = {"url": f"https://archive.org/details/{ident}", "type": "document", "site": "archive.org",
                "topic": "U.S. Army training publication", "title": title, "date": date}
        for c in ni.chunk_prose(ni.clean_text(text)):
            if not ni.is_garbage(c) and ni.remember(f"[{title}] {c}", a.vector, meta, done_hashes, a.dry_run):
                n += 1
        stored += n
        if not a.dry_run:
            cur.execute("INSERT INTO ia_ingest_seen (identifier, title, chunks) VALUES (%s, %s, %s) "
                        "ON CONFLICT (identifier) DO UPDATE SET chunks = excluded.chunks, at = now()", (ident, title, n))
        ni.log(f"[{i}/{len(pubs)}] {title[:80]}: {n} chunks (total {stored:,})")
        if time.time() - last >= 300:
            ni.notify(f":military_helmet: Army manuals: {i}/{len(pubs)} — latest *{title[:90]}* — {stored:,} memories")
            last = time.time()
        time.sleep(1)   # politeness to archive.org
    ni.notify(f":white_check_mark: *Army training publications ingest stopped* — {stored:,} memories in `{a.vector}`.")


if __name__ == "__main__":
    main()
