#!/opt/homebrew/bin/python3
"""nova_ingest_quran.py — the Qur'an (Al-Qur'an) in J. M. Rodwell's 1861 English translation, into Nova
memory under the vector `quran` (Little Mister 2026-10-09: "import the Qu'ran into Nova's memories").

Source: Project Gutenberg eBook #2800, public domain. Rodwell arranged the suras chronologically, and the
edition keeps his numbering in brackets. Each memory is tagged with the sura's number and title.

Limitation: the text includes Rodwell's own notes and commentary, which sit in the body with no marker that
separates them from the translation. They are kept as written and flagged in each memory's metadata.

Usage: nova_ingest_quran.py [--file local.txt] [--dry-run]
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

URL = "https://www.gutenberg.org/cache/epub/2800/pg2800.txt"
SOURCE = "quran"
TRANSLATION = "J. M. Rodwell (1861), Project Gutenberg #2800"
CHUNK_CHARS = 1200
HEADING = re.compile(r"^SURA\d?\s*[-.]?\s*([IVXLC]+)\.?\s*[-—.]?\s*(.*)$")   # 'SURA1 XCVI.-...' has a footnote digit


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


def roman(s: str) -> int:
    v = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    t = p = 0
    for ch in reversed(s):
        x = v[ch]
        if x < p:
            t -= x
        else:
            t += x; p = x
    return t


def suras(text: str) -> list:
    """-> [(number, title, body)] for the 114 suras, first heading of each, in the book's order. Pure."""
    L = text.split("\n")
    start = next((i for i, l in enumerate(L) if l.startswith("*** START OF")), 0)
    end = next((i for i, l in enumerate(L) if l.startswith("*** END OF")), len(L))
    heads, seen = [], set()
    for i in range(start, end):
        m = HEADING.match(L[i].strip())
        if m:
            n = roman(m.group(1))
            if 1 <= n <= 114 and n not in seen:
                title = re.sub(r"\s*\[.*$", "", m.group(2)).rstrip("0123456789 ").strip()   # drop footnote digits and [numbering]
                seen.add(n); heads.append((i, n, title))
    out = []
    for k, (i, n, title) in enumerate(heads):
        stop = heads[k + 1][0] if k + 1 < len(heads) else end
        body = " ".join(" ".join(l.split()) for l in L[i + 1:stop] if l.strip())
        out.append((n, title, body))
    return sorted(out, key=lambda x: x[0])


def passages(num: int, title: str, body: str, size: int = CHUNK_CHARS) -> list:
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
    label = f"Sura {num}" + (f" — {title}" if title else "")
    return [(f"[Qur'an {label} (Rodwell, passage {i})] {b}",
             {"book": "Qur'an", "sura": num, "title": title, "passage": i, "translation": TRANSLATION,
              "testament": "Qur'an", "type": "scripture", "includes_translator_notes": True,
              "url": URL})
            for i, b in out]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", help="read a local copy of the Gutenberg text instead of downloading")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    text = Path(a.file).read_text(errors="replace") if a.file else fetch()
    ss = suras(text)
    items = [it for n, t, body in ss for it in passages(n, t, body)]
    if a.dry_run:
        print(f"{len(ss)} suras, {len(items)} passages")
        return 0
    ni.notify(f":scroll: *Qur'an (Rodwell 1861) ingest* — {len(ss)} suras, {len(items)} passages -> `{SOURCE}`.")
    done, stored = set(), 0
    for t, m in items:
        if ni.remember(t, SOURCE, m, done):
            stored += 1
    ni.log(f"Qur'an: {stored}/{len(items)} passages stored")
    ni.notify(f":white_check_mark: *Qur'an (Rodwell 1861) ingest done* — {stored} passages in `{SOURCE}`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
