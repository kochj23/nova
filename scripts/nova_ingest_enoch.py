#!/opt/homebrew/bin/python3
"""nova_ingest_enoch.py — 1 Enoch (the Ethiopic Book of Enoch) into Nova memory, from R. H. Laurence's
1821 English translation, public domain (Little Mister 2026-10-09: "import the book of enoch").

Source: Internet Archive, "The book of Enoch the prophet" (1883 reprint), OCR text. The translation runs
from CHAP. I to CHAP. CVIII. The OCR's chapter headings are unreliable (running headers and misread numerals),
so memories are tagged with the book and a passage number, not a chapter. The verse numbers inside the text
(e.g. "26.") are kept.

Memory source: `apocrypha`. It is kept apart from the canonical `bible` source on purpose.

Usage: nova_ingest_enoch.py [--file local.txt] [--dry-run]
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

URL = "https://archive.org/download/bookofenochproph00laur/bookofenochproph00laur_djvu.txt"
SOURCE = "apocrypha"
BOOK = "1 Enoch"
TRANSLATION = "R. H. Laurence (1821)"
CHUNK_CHARS = 1200
START = re.compile(r"^\s*CHAP\.\s+I\.\s*$")
RUNNING_HEAD = re.compile(r"^\s*(\d{1,3}\s+ENOCH\.?|ENOCH\.?\s+\d{1,3})\s*$")
FOOTNOTE_START = ("*", "^", "†", "‡", "¦")


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


def translation_text(raw: str) -> str:
    """The translation only: from the first CHAP. I heading, with page headers dropped. Pure."""
    lines = raw.splitlines()
    start = next((i for i, l in enumerate(lines) if START.match(l)), None)
    if start is None:
        raise ValueError("no CHAP. I heading found; the source layout has changed")
    body = [l for l in lines[start:] if not RUNNING_HEAD.match(l)]
    return "\n".join(body)


EMBEDDED_HEAD = re.compile(r"\s*\b\d{1,3}\s+ENOCH\.?(?=\s)")
CHAP_HEAD = re.compile(r"^CHAP\.\s+[IVXLC]+\.?(\s+\d+)?$", re.I)


def paragraphs(text: str) -> list:
    """Blank-line paragraphs of the translation: footnotes, chapter headings, running heads, section
    markers and OCR debris removed. Pure."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        p = " ".join(para.split())
        if (not p or p.startswith(FOOTNOTE_START) or "N.B." in p or p.upper().startswith("CHAP")
                or "PRINTED BY" in p.upper() or "CLOWES" in p.upper()):
            continue
        p = re.sub(r"\s*\[SECT\.[^\]]*\]", "", p)
        p = EMBEDDED_HEAD.sub("", p).strip()
        letters = sum(c.isalpha() for c in p)
        if len(p) >= 15 and letters >= 0.6 * len(p):      # drops OCR debris such as the trailing scan junk
            out.append(p)
    return out


def passages(paras: list, size: int = CHUNK_CHARS) -> list:
    """Pack whole paragraphs into passages of about `size` characters. -> [(text, meta)]. Pure."""
    out, cur, n = [], [], 0
    for p in paras:
        if cur and n + len(p) > size:
            out.append(cur); cur, n = [], 0
        cur.append(p); n += len(p) + 1
    if cur:
        out.append(cur)
    result = []
    for k, group in enumerate(out, 1):
        body = " ".join(group)
        result.append((f"[{BOOK} (Laurence translation, passage {k})] {body}",
                       {"book": BOOK, "passage": k, "translation": TRANSLATION, "testament": "Apocrypha",
                        "type": "scripture", "url": URL, "title": BOOK}))
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", help="read a local copy of the OCR text instead of downloading")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    raw = Path(a.file).read_text(errors="replace") if a.file else fetch()
    items = passages(paragraphs(translation_text(raw)))
    if a.dry_run:
        print(f"{BOOK}: {len(items)} passages, {sum(len(t) for t, _ in items):,} characters")
        return 0
    ni.notify(f":scroll: *1 Enoch ingest* — {len(items)} passages ({TRANSLATION}) -> `{SOURCE}`.")
    done, stored = set(), 0
    for text, meta in items:
        if ni.remember(text, SOURCE, meta, done):
            stored += 1
    ni.log(f"{BOOK}: {stored}/{len(items)} passages stored")
    ni.notify(f":white_check_mark: *1 Enoch ingest done* — {stored} passages in `{SOURCE}`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
