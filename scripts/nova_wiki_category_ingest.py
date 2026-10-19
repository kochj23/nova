#!/usr/bin/env python3
"""nova_wiki_category_ingest.py -- ingest the member articles of a Wikipedia category
(recursing into subcategories) into one Nova vector.

nova_ingest.py's `wikipedia` mode is a link-following BFS that deliberately drops
Category: pages, so it cannot enumerate a category. This walks the category tree with
the MediaWiki `categorymembers` API and reuses nova_ingest's fetch/clean/chunk/quality/
injection-screen/remember pipeline for each article.

Pages already present in the target vector (matched by metadata title or URL) are skipped.
Wikipedia text is CC BY-SA 4.0; license + URL are recorded in each memory's metadata.

Usage:
  python3 -I nova_wiki_category_ingest.py "Category:Occult books" --source occult \
      --depth 2 --skip-cat "Category:Visionary fiction" --skip-cat "Category:I Ching" [--dry-run]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse, json, urllib.parse  # noqa: E402
import psycopg2  # noqa: E402
import nova_ingest as ni  # noqa: E402

API = "https://en.wikipedia.org/w/api.php?"
LICENSE = "CC BY-SA 4.0"


def members(cat):
    out, cont = [], {}
    while True:
        q = {"action": "query", "list": "categorymembers", "cmtitle": cat,
             "cmlimit": "500", "cmtype": "page|subcat", "format": "json", **cont}
        raw = ni.fetch(API + urllib.parse.urlencode(q), timeout=30)
        if not raw:
            ni.log(f"category fetch failed: {cat}", "WARN")
            return out
        data = json.loads(raw)
        out += data.get("query", {}).get("categorymembers", [])
        if "continue" not in data:
            return out
        cont = data["continue"]


def walk(root, max_depth, skip_cats):
    """Return ordered {page_title: category} for all ns-0 members down to max_depth."""
    pages, seen, stack = {}, {root}, [(root, 0)]
    while stack:
        cat, d = stack.pop(0)
        for m in members(cat):
            t = m["title"]
            if m["ns"] == 0:
                pages.setdefault(t, cat)
            elif m["ns"] == 14 and t not in seen and t not in skip_cats and d < max_depth:
                seen.add(t)
                stack.append((t, d + 1))
    return pages, seen


def existing(vector):
    conn = psycopg2.connect(host="localhost", dbname="nova_memories", user="kochj")
    with conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT lower(metadata->>'title'), metadata->>'url' FROM memories "
                    "WHERE source=%s", (vector,))
        rows = cur.fetchall()
    conn.close()
    titles = {r[0] for r in rows if r[0]}
    urls = {urllib.parse.unquote(r[1]).replace("_", " ").lower() for r in rows if r[1]}
    return titles, urls


def main():
    p = argparse.ArgumentParser()
    p.add_argument("category")
    p.add_argument("--source", required=True)
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--skip-cat", action="append", default=[])
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    cat = a.category
    if "/wiki/" in cat:
        cat = urllib.parse.unquote(cat.split("/wiki/")[-1])
    cat = cat.replace("_", " ")
    pages, cats = walk(cat, a.depth, set(a.skip_cat))
    ni.log(f"{cat}: {len(pages)} pages across {len(cats)} categories -> '{a.source}'")

    titles, urls = existing(a.source)
    done_hashes, stored, skipped, failed, ingested = set(), 0, 0, 0, 0
    for title, from_cat in pages.items():
        url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
        key = title.lower()
        if key in titles or ("https://en.wikipedia.org/wiki/" + key) in urls:
            skipped += 1
            ni.log(f"  skip (present): {title}")
            continue
        ni.rate_sleep("wikipedia")
        rtitle, text, _links, err = ni.wiki_fetch(url)
        if err or not text or len(text) < 100:
            failed += 1
            ni.log(f"  fail: {title} ({err})", "WARN")
            continue
        if rtitle and rtitle.lower() in titles:
            skipped += 1
            ni.log(f"  skip (present via redirect): {title} -> {rtitle}")
            continue
        n = 0
        chunks = ni.chunk_prose(ni.clean_text(text))
        for i, chunk in enumerate(chunks):
            if ni._garbage_reason(chunk)[0]:
                continue
            meta = {"title": rtitle, "url": url, "type": "wikipedia", "category": from_cat,
                    "seed_category": cat, "license": LICENSE, "chunk": i,
                    "total_chunks": len(chunks)}
            if ni.remember(chunk, a.source, meta, done_hashes, a.dry_run):
                n += 1
        stored += n
        ingested += 1
        titles.add((rtitle or title).lower())
        ni.log(f"  [{ingested}] {rtitle} ({from_cat}) -> {n} chunks")
    summary = (f"{cat}: found={len(pages)} ingested={ingested} skipped={skipped} "
               f"failed={failed} chunks={stored} vector={a.source}")
    ni.log("DONE " + summary)
    print(json.dumps({"found": len(pages), "ingested": ingested, "skipped": skipped,
                      "failed": failed, "chunks": stored, "categories": sorted(cats)}))


if __name__ == "__main__":
    main()
