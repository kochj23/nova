#!/opt/homebrew/bin/python3
"""nova_ingest_necronomicon.py — the Necronomicon (and Al Azif) as LITERARY LORE into Nova memory.

The Necronomicon is a FICTIONAL book invented by H. P. Lovecraft. It is not a real grimoire or religious
text. Every memory here is prefixed and tagged as fiction (metadata fiction=true, genre=literary_lore) so
recall never presents it as genuine occult material.

Only public-domain (US) or freely licensed material is ingested:
  * Lovecraft's "History of the Necronomicon" (1927/1938): Wikisource, PD-US-no-renewal.
  * Lovecraft stories that cite the Necronomicon or Abdul Alhazred: Project Gutenberg (US clearance,
    transcriber notes find no evidence of renewal) and Wikisource (PD-US / PD-US-no-renewal).
  * Nova's own summarized research notes (data/necronomicon_notes.jsonl), written from Wikipedia
    (CC BY-SA, source URL kept in metadata) plus Henley's public-domain notes to Vathek (the 'azif' source).
No text from modern copyrighted "Necronomicon" editions (Owlswick 1973, Simon 1977, Hay 1978, Tyson 2004)
is ingested. Only factual notes that they exist.

Memories mentioning Al Azif carry the extra tag `al_azif`. Chunking follows nova_ingest_enoch.py:
whole paragraphs packed into passages of about 1,200 characters.

Usage: nova_ingest_necronomicon.py DOWNLOAD_DIR [--dry-run] [--force]
  DOWNLOAD_DIR holds raw/pg<ID>.txt (Gutenberg) and ws/<key>.txt (Wikisource), as fetched on
  2026-10-18 into /Volumes/Data/downloads/necronomicon.
Written by Jordan Koch (via Claude).
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_ingest as ni  # noqa: E402

SOURCE = "necronomicon"
CHUNK_CHARS = 1200
NOTES = Path(__file__).parent / "data" / "necronomicon_notes.jsonl"
DISCLAIMER = ("Literary horror fiction. The Necronomicon is an invented book created by H. P. Lovecraft, "
              "not a real grimoire")
BASE_TAGS = ["necronomicon", "fiction", "lovecraft", "cthulhu_mythos", "literary_lore"]
GB = "https://www.gutenberg.org/ebooks/"
WS = "https://en.wikisource.org/wiki/"
PG_PD = "Project Gutenberg (US): no evidence the US copyright was renewed"

# key -> (title, author, year, file, url, pd_basis)
TEXTS = [
    ("history", "History of the Necronomicon", "H. P. Lovecraft", 1938, "ws/history.txt",
     WS + "History_of_the_Necronomicon", "Wikisource PD-US-no-renewal (written 1927, published 1938)"),
    ("nameless_city", "The Nameless City", "H. P. Lovecraft", 1921, "ws/nameless_city.txt",
     WS + "Weird_Tales/Volume_32/Issue_5/The_Nameless_City", "Wikisource PD-US (first published 1921)"),
    ("hound", "The Hound", "H. P. Lovecraft", 1924, "ws/hound.txt",
     WS + "Weird_Tales/Volume_3/Issue_2/The_Hound", "Wikisource PD-US (Weird Tales, Feb 1924)"),
    ("festival", "The Festival", "H. P. Lovecraft", 1925, "raw/pg68553.txt", GB + "68553", PG_PD),
    ("call_of_cthulhu", "The Call of Cthulhu", "H. P. Lovecraft", 1928, "raw/pg68283.txt", GB + "68283", PG_PD),
    ("last_test", "The Last Test", "Adolphe de Castro and H. P. Lovecraft", 1928, "raw/pg70606.txt",
     GB + "70606", PG_PD),
    ("dunwich_horror", "The Dunwich Horror", "H. P. Lovecraft", 1929, "raw/pg50133.txt", GB + "50133", PG_PD),
    ("whisperer", "The Whisperer in Darkness", "H. P. Lovecraft", 1931, "ws/whisperer.txt",
     WS + "The_Whisperer_in_Darkness", "Wikisource PD-US-no-renewal (Weird Tales, Aug 1931)"),
    ("witch_house", "The Dreams in the Witch House", "H. P. Lovecraft", 1933, "ws/witch_house.txt",
     WS + "The_Dreams_in_the_Witch-House", "Wikisource PD-US-no-renewal (Weird Tales, Jul 1933)"),
    ("silver_key_gates", "Through the Gates of the Silver Key", "H. P. Lovecraft and E. Hoffmann Price", 1934,
     "raw/pg71167.txt", GB + "71167", PG_PD),
    ("mountains_of_madness", "At the Mountains of Madness", "H. P. Lovecraft", 1936, "raw/pg70652.txt",
     GB + "70652", PG_PD),
    ("haunter_of_the_dark", "The Haunter of the Dark", "H. P. Lovecraft", 1936, "raw/pg73233.txt",
     GB + "73233", PG_PD),
    ("thing_on_the_doorstep", "The Thing on the Doorstep", "H. P. Lovecraft", 1937, "raw/pg73230.txt",
     GB + "73230", PG_PD),
    ("medusas_coil", "Medusa's Coil", "Zealia Bishop and H. P. Lovecraft", 1939, "raw/pg70899.txt",
     GB + "70899", PG_PD),
    ("charles_dexter_ward", "The Case of Charles Dexter Ward", "H. P. Lovecraft", 1941, "raw/pg73547.txt",
     GB + "73547", PG_PD),
]


def body_text(raw: str) -> str:
    """The work itself: Gutenberg header/footer, bracketed transcriber notes and Wikisource license
    residue removed. Pure."""
    m = re.search(r"\*\*\* ?START OF TH(E|IS) PROJECT GUTENBERG[^\n]*\n", raw)
    if m:
        raw = raw[m.end():]
        e = re.search(r"\*\*\* ?END OF TH(E|IS) PROJECT GUTENBERG", raw)
        raw = raw[:e.start()] if e else raw
    raw = re.sub(r"(?s)\[(Transcriber|Illustration|Sidenote)[^\]]*\]", "", raw)
    raw = raw.replace("Public domainPublic domainfalsefalse", "").replace("​", "")
    return raw.replace("\r\n", "\n")


def paragraphs(text: str) -> list:
    """Blank-line paragraphs, whitespace-normalized, debris (rules, page marks) dropped. Pure."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        p = " ".join(para.split())
        if len(p) >= 3 and sum(c.isalpha() for c in p) >= 0.5 * len(p):
            out.append(p)
    return out


def pack(paras: list, size: int = CHUNK_CHARS) -> list:
    """Whole paragraphs packed into ~size-char passages (an over-long paragraph is split at sentence
    boundaries). Pure."""
    pieces = []
    for p in paras:
        while len(p) > size * 1.5:
            cut = p.rfind(". ", 0, size)
            cut = cut + 1 if cut > size // 3 else size
            pieces.append(p[:cut].strip()); p = p[cut:].strip()
        pieces.append(p)
    out, cur, n = [], [], 0
    for p in pieces:
        if cur and n + len(p) > size:
            out.append(" ".join(cur)); cur, n = [], 0
        cur.append(p); n += len(p) + 1
    if cur:
        out.append(" ".join(cur))
    return out


def tags_for(text: str, extra=()) -> list:
    t = list(BASE_TAGS) + [x for x in extra if x not in BASE_TAGS]
    if re.search(r"\bazif\b", text, re.I) and "al_azif" not in t:
        t.append("al_azif")
    if re.search(r"alhazred", text, re.I) and "alhazred" not in t:
        t.append("alhazred")
    return t


def text_items(root: Path) -> list:
    items = []
    for key, title, author, year, rel, url, pd in TEXTS:
        chunks = pack(paragraphs(body_text((root / rel).read_text(errors="replace"))))
        for k, c in enumerate(chunks, 1):
            text = (f"[FICTION: {author}, \"{title}\" ({year}), passage {k}/{len(chunks)}. {DISCLAIMER}.] {c}")
            items.append((text, {
                "title": title, "author": author, "year": year, "work_key": key, "passage": k,
                "passages": len(chunks), "type": "fiction_text", "fiction": True, "genre": "literary_lore",
                "tags": tags_for(c), "url": url, "license": "public domain (US)", "pd_basis": pd,
                "is_real_grimoire": False}))
    return items


def note_items(path: Path = NOTES) -> list:
    items = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        n = json.loads(line)
        text = f"[Nova research note on fictional lore: {n['title']}] {n['text']}"
        items.append((text, {
            "title": n["title"], "type": "research_note", "fiction": True, "genre": "literary_lore",
            "tags": tags_for(n["text"], n.get("tags", [])), "url": n["url"], "license": n["license"],
            "is_real_grimoire": False, "author": "Nova (summary)"}))
    return items


def existing_count() -> int:
    q = f"SELECT count(*) FROM memories WHERE source='{SOURCE}'"
    r = subprocess.run(["psql", "-h", "pg-primary.digitalnoise.net", "-U", "kochj", "-d", "nova_memories",
                        "-tAc", q], capture_output=True, text=True, timeout=120)
    return int(r.stdout.strip() or 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("download_dir")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="ingest even if the source already has memories")
    a = ap.parse_args(argv)
    items = note_items() + text_items(Path(a.download_dir))
    azif = sum("al_azif" in m["tags"] for _, m in items)
    summary = (f"{len(items)} memories ({sum(m['type'] == 'research_note' for _, m in items)} notes, "
               f"{len(TEXTS)} PD texts), {azif} tagged al_azif")
    if a.dry_run:
        print(summary)
        for key, *_ in TEXTS:
            print(f"  {key}: {sum(m.get('work_key') == key for _, m in items)} passages")
        return 0
    have = existing_count()
    if have and not a.force:
        ni.log(f"necronomicon: {have} memories already in source `{SOURCE}`; refusing to duplicate (--force)")
        return 1
    ni.notify(f":closed_book: *Necronomicon ingest* (Lovecraft fiction/lore): {summary} -> `{SOURCE}`.")
    done, stored, stored_azif = set(), 0, 0
    for text, meta in items:
        if ni.remember(text, SOURCE, meta, done):
            stored += 1
            stored_azif += "al_azif" in meta["tags"]
    ni.log(f"necronomicon: {stored}/{len(items)} stored ({stored_azif} al_azif)")
    ni.notify(f":white_check_mark: *Necronomicon ingest done*: {stored} memories in `{SOURCE}` "
              f"({stored_azif} tagged al_azif), all marked fiction.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
