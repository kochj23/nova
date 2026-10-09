#!/opt/homebrew/bin/python3
"""nova_ingest_tanakh.py — the Tanakh (Torah, Nevi'im, Ketuvim) in the 1917 Jewish Publication Society
English translation, into Nova memory under the vector `tanakh` (Little Mister 2026-10-09: "import the Torah,
the Nevi'im and the Ketuvim").

The JPS 1917 translation is in the US public domain (copyright 1917, not renewed). The source here is the
Internet Archive scan `tanakh-1917_202402` (Tanakh1917_djvu.txt). The 2026 Archive copy with the Apocrypha is
licensed CC BY-NC-ND and is deliberately NOT used.

Books are found in the Tanakh's own order: each book's first heading comes after the previous book's last
page header. Running page headers and footnotes are removed. Passages are chunks of about 1,200 characters;
the verse numbers stay in the text but are not stored as metadata.

Usage: nova_ingest_tanakh.py [--file local.txt] [--dry-run]
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

URL = "https://archive.org/download/tanakh-1917_202402/Tanakh1917_djvu.txt"
SOURCE = "tanakh"
TRANSLATION = "Jewish Publication Society (1917)"
CHUNK_CHARS = 1200
# (division, book, accepted heading lines as they appear in the scan)
ORDER = [
    ("Torah", "Genesis", ["GENESIS"]), ("Torah", "Exodus", ["EXODUS"]), ("Torah", "Leviticus", ["LEVITICUS"]),
    ("Torah", "Numbers", ["NUMBERS"]), ("Torah", "Deuteronomy", ["DEUTERONOMY"]),
    ("Nevi'im", "Joshua", ["JOSHUA"]), ("Nevi'im", "Judges", ["JUDGES"]),
    ("Nevi'im", "I Samuel", ["FIRST SAMUEL", "I SAMUEL"]), ("Nevi'im", "II Samuel", ["SECOND SAMUEL", "II SAMUEL"]),
    ("Nevi'im", "I Kings", ["FIRST KINGS", "I KINGS"]), ("Nevi'im", "II Kings", ["SECOND KINGS", "II KINGS"]),
    ("Nevi'im", "Isaiah", ["ISAIAH"]), ("Nevi'im", "Jeremiah", ["JEREMIAH"]), ("Nevi'im", "Ezekiel", ["EZEKIEL"]),
    ("Nevi'im", "Hosea", ["HOSEA"]), ("Nevi'im", "Joel", ["JOEL"]), ("Nevi'im", "Amos", ["AMOS"]),
    ("Nevi'im", "Obadiah", ["OBADIAH"]), ("Nevi'im", "Jonah", ["JONAH"]), ("Nevi'im", "Micah", ["MICAH"]),
    ("Nevi'im", "Nahum", ["NAHUM"]), ("Nevi'im", "Habakkuk", ["HABAKKUK"]),
    ("Nevi'im", "Zephaniah", ["ZEPHANIAH", "ZEPHAIAH"]), ("Nevi'im", "Haggai", ["HAGGAI"]),
    ("Nevi'im", "Zechariah", ["ZECHARIAH"]), ("Nevi'im", "Malachi", ["MALACHI", "MALACHAI"]),
    ("Ketuvim", "Psalms", ["PSALMS"]), ("Ketuvim", "Proverbs", ["PROVERBS"]), ("Ketuvim", "Job", ["JOB"]),
    ("Ketuvim", "Song of Songs", ["SONG OF SONGS"]), ("Ketuvim", "Ruth", ["RUTH"]),
    ("Ketuvim", "Lamentations", ["LAMENTATIONS"]), ("Ketuvim", "Ecclesiastes", ["ECCLESIASTES"]),
    ("Ketuvim", "Esther", ["ESTHER"]), ("Ketuvim", "Daniel", ["DANIEL"]), ("Ketuvim", "Ezra", ["EZRA"]),
    ("Ketuvim", "Nehemiah", ["NEHEMIAH"]), ("Ketuvim", "I Chronicles", ["FIRST CHRONICLES", "I CHRONICLES"]),
    ("Ketuvim", "II Chronicles", ["SECOND CHRONICLES", "II CHRONICLES"]),
]
RUNNING_HEADS = {h for _d, _b, al in ORDER for h in al}
PAGE_NUMBER = re.compile(r"^\s*(\d{1,4}|[ivxlcdm]{1,6})\s*$", re.I)


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


def split_books(text: str, order: list = None) -> list:
    """-> [(division, book, body text)] in Tanakh order. Pure."""
    order = ORDER if order is None else order
    lines = [l.strip() for l in text.split("\n")]
    gen = next((i for i, l in enumerate(lines) if l.startswith("In the beginning God created")), None)
    if gen is None:
        raise ValueError("Genesis opening verse not found; the scan layout has changed")
    starts, prev_last = [], gen - 1
    for div, name, al in order:
        s = gen - 1 if name == "Genesis" else next(
            (i for i in range(prev_last + 1, len(lines)) if lines[i] in al), None)
        if s is None:
            raise ValueError(f"heading for {name} not found after line {prev_last}")
        starts.append((div, name, s))
        prev_last = max([i for i in range(len(lines)) if lines[i] in al] + [s])
    out = []
    for k, (div, name, s) in enumerate(starts):
        end = starts[k + 1][2] if k + 1 < len(starts) else len(lines)
        body = " ".join(
            l for l in lines[s + 1:end]
            if l and l not in RUNNING_HEADS and not PAGE_NUMBER.match(l) and not l.startswith("©"))
        body = body.replace("©", " ")   # footnote markers left by the scan (2026-10-09)
        out.append((div, name, " ".join(body.split())))
    return out


def passages(div: str, book: str, body: str, size: int = CHUNK_CHARS) -> list:
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
    return [(f"[{book} (JPS 1917, passage {i})] {b}",
             {"book": book, "division": div, "passage": i, "translation": TRANSLATION,
              "testament": "Tanakh", "type": "scripture", "url": URL, "title": book})
            for i, b in out]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", help="read a local copy of the scan instead of downloading")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    text = Path(a.file).read_text(errors="replace") if a.file else fetch()
    books = split_books(text)
    items = [it for div, book, body in books for it in passages(div, book, body)]
    if a.dry_run:
        for div, book, body in books:
            print(f"{div:8} {book:16} {len(body):>9,} chars")
        print(f"{len(books)} books, {len(items)} passages")
        return 0
    ni.notify(f":scroll: *Tanakh (JPS 1917) ingest* — {len(books)} books, {len(items)} passages -> `{SOURCE}`.")
    done, stored = set(), 0
    for t, m in items:
        if ni.remember(t, SOURCE, m, done):
            stored += 1
    ni.log(f"Tanakh: {stored}/{len(items)} passages stored")
    ni.notify(f":white_check_mark: *Tanakh (JPS 1917) ingest done* — {stored} passages in `{SOURCE}`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
