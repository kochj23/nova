#!/opt/homebrew/bin/python3
"""nova_ingest_owned_book.py — a book the owner has purchased, imported into Nova's private memory.

Used for the complete Ethiopian canon as published in English by Asher Wilson (2024), supplied as a PDF
the owner says was bought (Little Mister, 2026-10-09). Nothing here checks the purchase; the copy is
recorded as "owner-declared" in each memory's metadata.

Memories go under source `private_document`, which the memory server already excludes from public-journal
recall, and carry privacy=private. This text must never be quoted in the public journal, the website, or any
public output.

Books are found by the table-of-contents titles, matched in order against their headings in the body.
Passages are chunks of about 1,200 characters; the PDF's chapter:verse numbering is not preserved, so
memories are tagged by book and passage, not by verse.

Usage: nova_ingest_owned_book.py PDF_TEXT_FILE --title "..." --translator "..." [--dry-run]
Written by Jordan Koch (via Claude).
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_ingest as ni  # noqa: E402

SOURCE = "private_document"
CHUNK_CHARS = 1200
TOC_START = "Table of Contents"
BODY_FIRST = "The First Book of Moses, Genesis"


def book_starts(lines: list, body_first: str = BODY_FIRST) -> list:
    """-> [(title, line index)] for each book heading in body order, found from the table of contents. Pure."""
    toc = next(i for i, l in enumerate(lines) if l.strip() == TOC_START)
    body0 = next(i for i in range(toc + 1, len(lines)) if lines[i].strip() == body_first and i > toc + 5)
    titles = [l.strip() for l in lines[toc + 1:body0] if l.strip()]
    out, pos = [], body0
    for t in titles:
        for k in range(pos, len(lines)):
            if lines[k].strip() == t:
                out.append((t, k))
                pos = k + 1
                break
    return out


def books(text: str) -> list:
    """-> [(title, body text)] for every book, in order. Pure."""
    lines = text.split("\n")
    starts = book_starts(lines)
    out = []
    for n, (title, k) in enumerate(starts):
        end = starts[n + 1][1] if n + 1 < len(starts) else len(lines)
        body = "\n".join(lines[k + 1:end])
        body = " ".join(body.split())
        if body:
            out.append((title, body))
    return out


def passages(title: str, body: str, translator: str, size: int = CHUNK_CHARS) -> list:
    """Whole-word chunks of about `size` characters. -> [(text, meta)]. Pure."""
    words, cur, n, k = body.split(), [], 0, 0
    out = []
    for w in words:
        if cur and n + len(w) + 1 > size:
            k += 1
            out.append((k, " ".join(cur))); cur, n = [], 0
        cur.append(w); n += len(w) + 1
    if cur:
        k += 1
        out.append((k, " ".join(cur)))
    return [(f"[{title} (passage {i})] {b}",
             {"book": title, "passage": i, "translation": translator, "copy": "owner-declared",
              "privacy": "private", "type": "book", "title": title, "testament": "Ethiopian canon"})
            for i, b in out]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("text_file")
    ap.add_argument("--title", required=True)
    ap.add_argument("--translator", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    text = Path(a.text_file).read_text(errors="replace")
    bs = books(text)
    items = [it for title, body in bs for it in passages(title, body, a.translator)]
    if a.dry_run:
        print(f"{len(bs)} books, {len(items)} passages, {sum(len(t) for t, _ in items):,} characters")
        return 0
    ni.notify(f":closed_book: *{a.title} ingest* — {len(bs)} books, {len(items)} passages -> `{SOURCE}` (private).")
    done, stored = set(), 0
    for t, m in items:
        if ni.remember(t, SOURCE, m, done):
            stored += 1
    ni.notify(f":white_check_mark: *{a.title} ingest done* — {stored} passages stored (private).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
