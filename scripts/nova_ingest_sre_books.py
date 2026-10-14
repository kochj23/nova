#!/usr/bin/env python3
"""nova_ingest_sre_books.py — Google's free SRE books, full text, into Nova's memory (source `sre_books`).

Jordan's job is SRE (Little Mister, 2026-10-13: "It is what I do for a job so she should ... better understand
me and what is important to me"). Books, all published free by Google under CC BY-NC-ND 4.0:
  - Site Reliability Engineering (2016)        https://sre.google/sre-book/table-of-contents/
  - The Site Reliability Workbook (2018)       https://sre.google/workbook/table-of-contents/
  - Building Secure and Reliable Systems (2020) https://google.github.io/building-secure-and-reliable-systems/raw/toc.html
The 2nd edition of Site Reliability Engineering is paid-only and is not included.

Each chapter is chunked by paragraph (nova_ingest.chunk_prose) and every chunk is prefixed
"[Book — Chapter]" so recall knows where it came from. Re-runs are safe: the memory server dedups by text.

Usage: nova_ingest_sre_books.py [--dry-run]
Written by Jordan Koch (via Claude).
"""
import argparse
import html
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).parent))
import nova_ingest as ni  # noqa: E402

SOURCE = "sre_books"
BOOKS = [  # (title, year, toc url, chapter-link regex, content start marker, content end marker)
    ("Site Reliability Engineering", 2016, "https://sre.google/sre-book/table-of-contents/",
     r'href="(/sre-book/[a-z0-9-]+/)"', '<div id="maia-main"', '<div class="footer">'),
    ("The Site Reliability Workbook", 2018, "https://sre.google/workbook/table-of-contents/",
     r'href="(/workbook/[a-z0-9-]+/)"', '<div id="maia-main"', '<div class="footer">'),
    ("Building Secure and Reliable Systems", 2020,
     "https://google.github.io/building-secure-and-reliable-systems/raw/toc.html",
     r'href="((?:foreword\d+|pr\d+|ch\d+|app[a-z])\.html)"', "<body", "</body>"),
]
SKIP = ("table-of-contents", "bibliography", "/part-")


def chapter_links(toc_html, pattern, base):
    """-> chapter URLs in table-of-contents order, deduped, minus TOC/bibliography/part dividers. Pure."""
    out = []
    for m in re.finditer(pattern, toc_html):
        u = urljoin(base, m.group(1))
        if u not in out and not any(s in u for s in SKIP):
            out.append(u)
    return out


def chapter(page, start, end):
    """-> (chapter title, body text) from one chapter page. Pure."""
    t = (re.search(r'<h2 class="chapter-title">(.*?)</h2>', page, re.S) or re.search(r"<h1>(.*?)</h1>", page, re.S)
         or re.search(r"<title>(.*?)</title>", page, re.S))
    title = " ".join(html.unescape(re.sub(r"<[^>]+>", "", t.group(1))).split()) if t else ""
    i = page.find(start)
    j = page.find(end, i + 1) if i >= 0 else -1
    body = page[i:j] if i >= 0 and j > i else page
    body = ni.html_text(body).replace("\xa0", " ").replace("‎", "")
    return title, re.sub(r"[ \t]+", " ", body)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    done, total = set(), 0
    for book, year, toc, pat, start, end in BOOKS:
        toc_html = ni.fetch(toc)
        if not toc_html:
            ni.log(f"[sre_books] {book}: table of contents unreachable — skipped", "WARN")
            continue
        links = chapter_links(toc_html, pat, toc)
        stored = 0
        for u in links:
            page = ni.fetch(u)
            if not page:
                ni.log(f"[sre_books] fetch failed: {u}", "WARN")
                continue
            title, body = chapter(page, start, end)
            meta = {"book": book, "chapter": title, "year": year, "url": u, "type": "book",
                    "license": "CC BY-NC-ND 4.0", "why": "Jordan's profession (SRE)"}
            for c in ni.chunk_prose(body):
                stored += ni.remember(f"[{book} — {title}] {c}", SOURCE, meta, done, a.dry_run)
        ni.log(f"[sre_books] {book}: {len(links)} chapters, {stored} chunks stored")
        total += stored
    if not a.dry_run:
        ni.notify(f":books: *SRE books ingested* — {total} passages from 3 books -> `{SOURCE}`.")
    print(f"[sre_books] total chunks: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
