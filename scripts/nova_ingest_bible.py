#!/opt/homebrew/bin/python3
"""nova_ingest_bible.py — the Old Testament (King James Version, public domain) into Nova memory,
one memory per run of whole verses inside a chapter, tagged with book, chapter and verse range
(Little Mister 2026-10-09: "find and import the old testament into Nova's memories").

Source: Project Gutenberg eBook #10, the complete KJV. Its table of contents lists the Old Testament
titles in canonical order; each book's body follows under the same heading, verses as "C:V text"
with wrapped lines. Only Genesis through Malachi are read (the body stops at "The New Testament of
the King James Bible"). Memory source: `bible`; the New Testament could join it later.

Usage: nova_ingest_bible.py [--file pg10.txt] [--dry-run]
Written by Jordan Koch (via Claude).
"""
import argparse
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_ingest as ni  # noqa: E402

URL = "https://www.gutenberg.org/cache/epub/10/pg10.txt"
SOURCE = "bible"
NT_HEADING = "The New Testament of the King James Bible"
CHUNK_CHARS = 1200
OT_BOOKS = ["Genesis", "Exodus", "Leviticus", "Numbers", "Deuteronomy", "Joshua", "Judges", "Ruth",
            "1 Samuel", "2 Samuel", "1 Kings", "2 Kings", "1 Chronicles", "2 Chronicles", "Ezra", "Nehemiah",
            "Esther", "Job", "Psalms", "Proverbs", "Ecclesiastes", "Song of Solomon", "Isaiah", "Jeremiah",
            "Lamentations", "Ezekiel", "Daniel", "Hosea", "Joel", "Amos", "Obadiah", "Jonah", "Micah", "Nahum",
            "Habakkuk", "Zephaniah", "Haggai", "Zechariah", "Malachi"]
VERSE = re.compile(r"(?:(?<=\s)|^)(\d{1,3}):(\d{1,3})\s")


def fetch(url: str = URL, attempts: int = 3, _sleep=time.sleep) -> str:
    """GET with retry and backoff (Gutenberg mirrors drop connections)."""
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "NovaIngest/1.0 (personal knowledge base)"})
            return urllib.request.urlopen(req, timeout=120).read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 — retried, then re-raised
            if i == attempts - 1:
                raise
            ni.log(f"fetch failed ({e}); retry {i + 1}")
            _sleep(5 * 2 ** i)


def split_books(text: str) -> list:
    """-> [(book name, body text)] for the 39 Old Testament books, in canonical order. Pure."""
    lines = text.splitlines()
    nt = [i for i, l in enumerate(lines) if l.strip() == NT_HEADING]
    toc_end = nt[0]                                  # first occurrence closes the table of contents
    titles = [l.strip() for l in lines[:toc_end] if l.strip()]
    titles = titles[-len(OT_BOOKS):]                 # the 39 titles just before the NT heading
    body_start = toc_end + 1
    body_end = nt[1] if len(nt) > 1 else len(lines)
    starts, pos = [], body_start
    for t in titles:
        pos = next(i for i in range(pos, body_end) if lines[i].strip() == t)
        starts.append(pos)
    starts.append(body_end)
    return [(OT_BOOKS[k], "\n".join(lines[starts[k] + 1:starts[k + 1]])) for k in range(len(OT_BOOKS))]


def split_books_nt(text: str) -> list:
    """-> [(book name, body text)] for the 27 New Testament books, in canonical order. The table of contents
    lists the titles right after the New Testament heading; each body starts at the next occurrence of its
    title after the table of contents, and the last book ends at the Project Gutenberg footer. Pure."""
    lines = text.splitlines()
    toc = next(i for i, l in enumerate(lines) if l.strip() == NT_HEADING)
    titles, i = [], toc + 1
    while len(titles) < 27 and i < len(lines):
        if lines[i].strip():
            titles.append(lines[i].strip())
        i += 1
    end = next((k for k in range(i, len(lines)) if "END OF THE PROJECT GUTENBERG" in lines[k].upper()), len(lines))
    starts, pos = [], i
    for t in titles:
        pos = next(k for k in range(pos, end) if lines[k].strip() == t)
        starts.append(pos)
    starts.append(end)
    return [(titles[k], "\n".join(lines[starts[k] + 1:starts[k + 1]])) for k in range(len(titles))]


def verses(body: str) -> list:
    """-> [(chapter, verse, text)] from a book body with wrapped lines. Pure."""
    flat = " ".join(body.split())
    marks = list(VERSE.finditer(flat))
    out = []
    for k, m in enumerate(marks):
        end = marks[k + 1].start() if k + 1 < len(marks) else len(flat)
        out.append((int(m.group(1)), int(m.group(2)), flat[m.end():end].strip().rstrip("* ").strip()))
    return out


def chunks(book: str, vs: list, size: int = CHUNK_CHARS, testament: str = "Old Testament") -> list:
    """Group whole verses into chunks of about `size` chars, never across a chapter.
    -> [(text, meta)]. Pure."""
    out, cur, ch = [], [], None

    def flush():
        if cur:
            c, v1, v2 = cur[0][0], cur[0][1], cur[-1][1]
            ref = f"{book} {c}:{v1}" + (f"-{v2}" if v2 != v1 else "")
            body = " ".join(f"{v} {t}" for _c, v, t in cur)
            out.append((f"[{ref} (KJV)] {body}", {"book": book, "chapter": c, "verses": f"{v1}-{v2}",
                                                   "translation": "King James Version",
                                                   "testament": testament, "type": "scripture",
                                                   "url": URL, "title": f"{book} {c}"}))
            cur.clear()

    for c, v, t in vs:
        if c != ch or (cur and sum(len(x[2]) for x in cur) + len(t) > size):
            flush()
        ch = c
        cur.append((c, v, t))
    flush()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", help="read a local copy of pg10.txt instead of downloading")
    ap.add_argument("--testament", choices=["old", "new"], default="old")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    text = Path(a.file).read_text(errors="replace") if a.file else fetch()
    books = split_books(text) if a.testament == "old" else split_books_nt(text)
    testament = "Old Testament" if a.testament == "old" else "New Testament"
    plan = [(b, chunks(b, verses(body), testament=testament)) for b, body in books]
    total = sum(len(c) for _b, c in plan)
    if a.dry_run:
        for b, c in plan:
            print(f"{b:16} {len(c):4} chunks, chapters {c[0][1]['chapter']}-{c[-1][1]['chapter']}")
        print(f"{len(plan)} books, {total} chunks")
        return 0
    ni.notify(f":scroll: *Old Testament (KJV) ingest* — {len(plan)} books, {total} passages -> `{SOURCE}`.")
    done, stored = set(), 0
    for b, cs in plan:
        n = sum(1 for t, meta in cs if ni.remember(t, SOURCE, meta, done))
        stored += n
        ni.log(f"{b}: {n}/{len(cs)} passages (total {stored:,})")
    ni.notify(f":white_check_mark: *Old Testament (KJV) ingest done* — {stored:,} passages in `{SOURCE}`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
