#!/opt/homebrew/bin/python3
"""nova_ingest_book_of_mormon.py — The Book of Mormon (the 1830 text, public domain), into Nova memory under
the vector `book_of_mormon` (Little Mister 2026-10-09: "ingest the book of mormon into nova").

Source: Project Gutenberg eBook #17 (2008 release of the 1830 first edition, public domain in the US). The
1830 text has verses but no chapter headings, so memories are tagged by book (for example "1 Nephi") and
passage number. Each book's heading in the body carries a parenthesised name, e.g. "(1 Nephi)".

Usage: nova_ingest_book_of_mormon.py [--file local.txt] [--dry-run]
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

URL = "https://www.gutenberg.org/cache/epub/17/pg17.txt"
SOURCE = "book_of_mormon"
TRANSLATION = "1830 first edition (Project Gutenberg #17)"
CHUNK_CHARS = 1200
PAREN = re.compile(r"\(([^()]+)\)\s*$")


def fetch(url: str = URL, attempts: int = 3, _sleep=time.sleep) -> str:
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "NovaIngest/1.0 (personal knowledge base)"})
            return urllib.request.urlopen(req, timeout=300).read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 — retried, then re-raised
            if i == attempts - 1:
                raise
            ni.log(f"fetch failed ({e}); retry {i + 1}")
            _sleep(5 * 2 ** i)


def books(text: str) -> list:
    """-> [(book name, body text)] in book order. Pure."""
    L = [l.strip() for l in text.split("\n")]
    start = next((i for i, l in enumerate(L) if l.startswith("*** START OF")), 0)
    end = next((i for i, l in enumerate(L) if l.startswith("*** END OF")), len(L))
    contents = next(i for i in range(start, end) if L[i] == "Contents")
    toc = []
    for i in range(contents + 1, end):
        if not L[i]:
            if toc:
                break
            continue
        if L[i].startswith("THE FIRST BOOK OF NEPHI") and "(" in L[i]:
            break
        toc.append(L[i])
    out, pos, heads = [], contents + len(toc) + 1, []
    for t in toc:
        for i in range(pos, end):
            if L[i].startswith(t) and "(" in L[i] and PAREN.search(L[i]):
                heads.append((i, PAREN.search(L[i]).group(1)))
                pos = i + 1
                break
        else:
            for i in range(pos, end):
                if L[i] == t:
                    heads.append((i, t.title()))
                    pos = i + 1
                    break
    for k, (i, name) in enumerate(heads):
        stop = heads[k + 1][0] if k + 1 < len(heads) else end
        body = " ".join(" ".join(l.split()) for l in L[i + 1:stop] if l)
        out.append((name, body))
    return out


def passages(name: str, body: str, size: int = CHUNK_CHARS) -> list:
    """Whole-word chunks of about `size` characters. -> [(text, meta)]. Pure."""
    words, cur, n, k, out = body.split(), [], 0, 0, []
    for w in words:
        if cur and n + len(w) + 1 > size:
            k += 1
            out.append((k, " ".join(cur))); cur, n = [], 0
        cur.append(w); n += len(w) + 1
    if cur:
        k += 1
        out.append((k, " ".join(cur)))
    return [(f"[Book of Mormon, {name} (passage {i})] {b}",
             {"book": name, "passage": i, "translation": TRANSLATION, "testament": "Book of Mormon",
              "type": "scripture", "url": URL, "title": name})
            for i, b in out]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", help="read a local copy of the Gutenberg text instead of downloading")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    text = Path(a.file).read_text(errors="replace") if a.file else fetch()
    bs = books(text)
    items = [it for name, body in bs for it in passages(name, body)]
    if a.dry_run:
        for name, body in bs:
            print(f"{name:28} {len(body):>8,} chars")
        print(f"{len(bs)} books, {len(items)} passages")
        return 0
    ni.notify(f":scroll: *Book of Mormon (1830) ingest* — {len(bs)} books, {len(items)} passages -> `{SOURCE}`.")
    done, stored = set(), 0
    for t, m in items:
        if ni.remember(t, SOURCE, m, done):
            stored += 1
    ni.log(f"Book of Mormon: {stored}/{len(items)} passages stored")
    ni.notify(f":white_check_mark: *Book of Mormon (1830) ingest done* — {stored} passages in `{SOURCE}`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
