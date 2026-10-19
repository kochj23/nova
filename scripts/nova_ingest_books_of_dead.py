#!/opt/homebrew/bin/python3
"""nova_ingest_books_of_dead.py — the world's "books of the dead" (other than the Egyptian Book of the Dead
proper) into Nova memory, vector `books_of_the_dead` (Little Mister 2026-10-18).

Every memory carries metadata key (per-text tag), work, tradition, framing, translator, year, license, url.
Framing: genuine religious / funerary texts are type=scripture ("historical scripture"); the medieval Christian
Ars Moriendi is type=devotional_literature; editors' introductions are type=commentary; Nova's own notes are
type=background_note.

NOT here (other agents own them): the Egyptian Book of the Dead proper (Papyrus of Ani, Budge 1895,
Renouf/Naville) -> vector egyptian_book_of_the_dead; Eastern scripture vectors (buddhism, hinduism, ...).

Sources (public domain in the US; each verified, see LICENSE notes in WORKS):
  * sacred-texts.com pages read from Wayback `id_` snapshots (sacred-texts itself sits behind Cloudflare),
    via nova_ingest_scripture.fetch_st with this script's own cache under /Volumes/Data.
  * Internet Archive OCR (_djvu.txt) for editions sacred-texts lacks, cleaned: running heads, page numbers and
    OCR noise from hieroglyphic / Greek / Devanagari type are dropped by a dictionary-ratio line filter.
  * Background notes written by Nova (her own words, with source URLs) from JSON in NOTES_FILE.

Usage:
  nova_ingest_books_of_dead.py list
  nova_ingest_books_of_dead.py probe KEY            # print units, no writes
  nova_ingest_books_of_dead.py run [--work KEY ...] [--dry-run]
Written by Jordan Koch (via Claude).
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_ingest_scripture as nis  # noqa: E402  (fetch_st, crawl_st, split_passages, unwrap, existing_uids, queue_depth)

VECTOR = "books_of_the_dead"
CACHE = Path("/Volumes/Data/books_of_the_dead_cache")
NOTES_FILE = CACHE / "notes" / "notes.json"
IA = "https://archive.org/"
nis.CACHE = CACHE / "st"          # keep our Wayback cache apart from the Eastern-scripture agent's

PD_OLD = "Public domain (published before 1931; US)"
PD_1927 = ("Public domain in the US (published 1927; US term expired 2023-01-01). Note: Evans-Wentz d. 1965, "
           "so still in copyright in life+70 countries until 2036.")
PD_MERCER = ("Public domain in the US: published 1952 (Longmans, Green, New York) and the copyright was not "
             "renewed (no entry in the 1978-1982 CCE renewal records, NYPL cce-renewals dataset; sacred-texts "
             "reaches the same conclusion). Volume 1 (translation) only.")

SCRIPTURE, DEVOTIONAL, COMMENTARY = "scripture", "devotional_literature", "commentary"


def W(key, work, tradition, translator, year, license, kind, **kw):
    return dict(key=key, work=work, tradition=tradition, translator=translator, year=year, license=license,
                kind=kind, **kw)


# OCR segments: (first_line, last_line, label, type, heading_regex_or_None). Line numbers refer to the cached
# _djvu.txt files (fixed snapshots), found by reading them.
WORKS = [
    W("bardo_thodol", "Tibetan Book of the Dead (Bardo Thödol)", "Tibetan Buddhism (Nyingma)",
      "Lama Kazi Dawa-Samdup (tr.), W. Y. Evans-Wentz (ed.)", 1927, PD_1927, "ocr",
      ident="tibetanbookofdea0000unse_w2j5", framing="historical scripture (Tibetan Buddhist funerary text)",
      note="1927 Oxford University Press first edition (no Jung commentary, which was added in 1957/1960)",
      segments=[
          (1196, 1874, "Foreword by Sir John Woodroffe", COMMENTARY, None),
          (1875, 5861, "Editor's Introduction (Evans-Wentz)", COMMENTARY, r"^[IVX]+\. [A-Z][A-Z ,'’-]{6,}$"),
          (5862, 9735, "Book I: the Chikhai Bardo and the Chönyid Bardo", SCRIPTURE, r"^\[(?:[IVX]+[:;] ?)?[A-Z\'‘][A-Z ,.;:’\'‘()-]{5,}"),
          (9736, 12105, "Book II: the Sidpa Bardo", SCRIPTURE, r"^\[(?:[IVX]+[:;] ?)?[A-Z\'‘][A-Z ,.;:’\'‘()-]{5,}"),
          (12106, 12843, "Appendix: the Bardo prayers (Paths of Good Wishes, Root Verses)", SCRIPTURE, r"^\[(?:[IVX]+[:;] ?)?[A-Z\'‘][A-Z ,.;:’\'‘()-]{5,}"),
          (12844, 14787, "Addenda (Evans-Wentz)", COMMENTARY, r"^[IVX]+\. [A-Z][A-Z ,'’()-]{6,}$"),
      ]),
    W("amduat_budge", "The Book Am-Tuat (Amduat), from The Egyptian Heaven and Hell vol. I", "Ancient Egyptian religion",
      "E. A. Wallis Budge", 1905, PD_OLD, "ocr", ident="egyptianheavenhe0001budg",
      framing="historical scripture (New Kingdom royal funerary book)",
      note="Internet Archive OCR of the Kegan Paul 1905/1906 printing; hieroglyphic text dropped by OCR filter",
      segments=[(353, 12342, "The Book Am-Tuat", SCRIPTURE, r"^THE [A-Z]+ DIVISION OF THE TUAT")]),
    W("amduat_short_budge", "The Short Form of the Book Am-Tuat, from The Egyptian Heaven and Hell vol. II",
      "Ancient Egyptian religion", "E. A. Wallis Budge", 1905, PD_OLD, "ocr", ident="egyptianheavenhe0002budg",
      framing="historical scripture (New Kingdom royal funerary book)",
      segments=[(323, 2055, "Short Form of the Am-Tuat", SCRIPTURE, r"^THE [A-Z]+ HOUR")]),
    W("book_of_gates_budge", "The Book of Gates, from The Egyptian Heaven and Hell vol. II", "Ancient Egyptian religion",
      "E. A. Wallis Budge", 1905, PD_OLD, "ocr", ident="egyptianheavenhe0002budg",
      framing="historical scripture (New Kingdom royal funerary book)",
      segments=[(2056, 13696, "The Book of Gates", SCRIPTURE, r"^THE [A-Z]+ DIVISION OF THE TUAT")]),
    W("ehh_vol3_budge", "The Egyptian Heaven and Hell vol. III: the Books of the Other World described and compared",
      "Ancient Egyptian religion", "E. A. Wallis Budge", 1905, PD_OLD, "st", index="egy/ehh/index.htm",
      framing="commentary on historical scripture (Amduat and Book of Gates compared)", type=COMMENTARY),
    W("book_of_breathings", "The Book of Respirations (Book of Breathings), Louvre papyrus 3284", "Ancient Egyptian religion",
      "P. J. de Horrack", 1875, PD_OLD, "ocr", ident="recordsofpastbei04lond",
      framing="historical scripture (Ptolemaic-era funerary text)",
      note="Records of the Past vol. IV (Samuel Bagster, London, 1875), pp. 119-128",
      segments=[(14840, 15320, "The Book of Respirations", SCRIPTURE, None)]),
    W("pyramid_texts_mercer", "The Pyramid Texts", "Ancient Egyptian religion", "Samuel A. B. Mercer", 1952,
      PD_MERCER, "st", index="egy/pyt/index.htm",
      framing="historical scripture (Old Kingdom royal funerary spells, c. 2350-2175 BCE)"),
    W("orphic_gold_tablets", "The Orphic gold tablets (Jane Ellen Harrison, Prolegomena ch. XI 'Orphic Eschatology', "
      "with Gilbert Murray's translations and Critical Appendix)", "Greek Orphic religion",
      "Gilbert Murray (tr.), Jane Ellen Harrison", 1903, PD_OLD, "ocr", ident="prolegomenatostu00harruoft",
      framing="historical religious texts (Orphic/Bacchic funerary gold leaves) with scholarly commentary",
      note="Cambridge University Press 1903 first edition; Greek type dropped by OCR filter",
      segments=[(34110, 37436, "Chapter XI: Orphic Eschatology", SCRIPTURE,
                 r"^(?:[IVX]+\.|[a-d]\.) [A-Z][A-Za-z ,'’()-]{4,}"),
                (39607, 40776, "Critical Appendix on the Orphic Tablets (Gilbert Murray)", COMMENTARY,
                 r"^[IVX., and]+ (The|Three|Zhe) [A-Z]")]),
    W("garuda_purana_preta", "Garuda Purana (Saroddhara / Preta Kalpa: the afterlife and funeral rites)", "Hinduism",
      "Ernest Wood & S. V. Subrahmanyam", 1911, PD_OLD, "st", index="hin/gpu/index.htm",
      framing="historical scripture (Hindu Purana on death, the journey to Yama and funeral rites)",
      note="Sacred Books of the Hindus vol. IX (Panini Office, Allahabad, 1911)"),
    W("ars_moriendi", "The Book of the Craft of Dying and other early English tracts concerning death (Ars Moriendi)",
      "Medieval Christianity", "Frances M. M. Comper (ed., modernized spelling)", 1917, PD_OLD, "ocr",
      ident="bookofcraftofdyi00caxtiala",
      framing="devotional literature (15th-century Christian manual on dying well)", type=DEVOTIONAL,
      note="Longmans, Green 1917; texts from British Museum and Bodleian manuscripts and Caxton prints",
      segments=[
          (1519, 3279, "The Book of the Craft of Dying (English Ars Moriendi, long version)", DEVOTIONAL,
           r"^(THE [A-Z]+ CHAPTER .*|CHAPTER [IVX]+)$"),
          (3453, 4724, "The Art and Craft to Know Well to Die (Caxton, 1490)", DEVOTIONAL, r"^CHAPTER +[IVX]+$"),
          (4784, 5121, "The Craft for to Die for the Health of Man's Soul (Ars Moriendi, Caxton)", DEVOTIONAL, None),
          (5167, 5932, "Orologium Sapientiae, chapter 5: how we shall learn to die (Henry Suso)", DEVOTIONAL, None),
          (5933, 6176, "Three fragments which teach a man for to die (Tower of All Towers; Richard Rolle)",
           DEVOTIONAL, r"^HERE (SHOWETH|FOLLOW) .*"),
          (6197, 7386, "The Lamentation of the Dying Creature", DEVOTIONAL, None),
      ]),
    W("notes", "Background notes (Nova)", "Comparative religion", "Nova", 2026, "Original notes by Nova", "note",
      framing="background note, Nova's own words"),
]
BY_KEY = {w["key"]: w for w in WORKS}

# ── OCR cleaning (pure) ────────────────────────────────────────────────────────

_WORDS = None


def words() -> set:
    global _WORDS
    if _WORDS is None:
        _WORDS = {w.strip().lower() for w in open("/usr/share/dict/words", encoding="utf-8", errors="ignore")}
        _WORDS |= {"bardo", "tuat", "osiris", "ra", "yama", "lama", "buddha", "buddhas", "karma", "karmic",
                   "thou", "thee", "thy", "hath", "doth", "shalt", "ye", "unto", "orphic", "dionysos", "persephone"}
    return _WORDS


def _known(tok: str, W_: set) -> bool:
    if tok in W_:
        return True
    for suf in ("s", "es", "ed", "d", "ing", "eth", "th", "est", "ly", "'s", "’s"):
        if tok.endswith(suf) and tok[: -len(suf)] in W_:
            return True
    return False


_RUNHEAD = re.compile(r"(?i)^\s*(\d{1,3}\s+)?([A-Z][A-Za-z.' ’-]{2,60}:?\s?[A-Z ]{0,40})(\s+\d{1,3})?\s*$")


def good_line(line: str) -> bool:
    """Keep a line of real English prose/verse; drop OCR noise from non-Latin type, page numbers, running heads."""
    t = line.strip()
    if not t:
        return True
    letters = re.sub(r"[^A-Za-z]", "", t)
    if len(letters) < 3:
        return False
    toks = [x.lower() for x in re.findall(r"[A-Za-z’']+", t) if len(x) >= 2]
    if not toks:
        return False
    W_ = words()
    ratio = sum(_known(x, W_) for x in toks) / len(toks)
    if len(toks) >= 4:
        return ratio >= 0.5
    return ratio >= 0.6 and len(letters) >= 4


def clean_ocr(lines: list, runhead_re: str | None = None) -> str:
    rh = re.compile(runhead_re) if runhead_re else None
    out = []
    for ln in lines:
        t = re.sub(r"\s{2,}", " ", ln).rstrip()
        if rh and rh.search(t):
            continue
        if re.match(r"^\s*\d{1,3}\s+[A-Z][A-Z .,:’'-]{4,}$|^\s*[A-Z][A-Z .,:’'-]{4,}\s+\d{1,3}\s*$", t):
            continue                                  # running head with page number
        if not good_line(t):
            continue
        out.append(t)
    text = "\n".join(out)
    text = re.sub(r"(\w)[-¬]\n(\w)", r"\1\2", text)         # re-join hyphenation
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def segment_units(raw_lines: list, start: int, end: int, label: str, head_re: str | None) -> list:
    """-> [(section_label, cleaned_text)] for lines start..end (1-based, inclusive), split at headings."""
    hx = re.compile(head_re) if head_re else None
    units_, cur, buf = [], label, []
    for ln in raw_lines[start - 1:end]:
        s = re.sub(r"\s{2,}", " ", ln).strip()
        if hx and hx.match(s) and buf:
            units_.append((cur, buf))
            cur, buf = f"{label} — {s.strip('[]').title()}"[:200], []
        elif hx and hx.match(s):
            cur = f"{label} — {s.strip('[]').title()}"[:200]
        buf.append(ln)
    if buf:
        units_.append((cur, buf))
    out = []
    for sec, b in units_:
        t = clean_ocr(b)
        if len(t) >= 200:
            out.append((sec, t))
    return out


def fetch_ia_txt(ident: str) -> list:
    p = CACHE / ident / f"{ident}.txt"
    if not p.exists() or p.stat().st_size < 1000:
        p.parent.mkdir(parents=True, exist_ok=True)
        meta = json.load(urllib.request.urlopen(f"{IA}metadata/{ident}", timeout=60))
        name = next(f["name"] for f in meta["files"] if f["name"].endswith("_djvu.txt"))
        p.write_bytes(nis.http_get(f"{IA}download/{ident}/{urllib.request.quote(name)}"))
    return p.read_text(encoding="utf-8", errors="replace").splitlines()


# ── units ──────────────────────────────────────────────────────────────────────

def units(work: dict):
    """Yield (uid_base, section, text, extra_meta)."""
    if work["kind"] == "ocr":
        lines = fetch_ia_txt(work["ident"])
        k = 0
        for si, (a, b, label, typ, hre) in enumerate(work["segments"], 1):
            for sec, text in segment_units(lines, a, b, label, hre):
                k += 1
                yield f"s{si}u{k:03d}", sec, text, {"type": typ, "order": k,
                                                     "url": f"{IA}details/{work['ident']}",
                                                     "via": "Internet Archive OCR (cleaned)"}
        return
    if work["kind"] == "st":
        for k, (path, title, text) in enumerate(nis.crawl_st({**work, "split_re": None, "force_index": True}), 1):
            if re.search(r"(?i)^(title page|contents|index|bibliography|abbreviations|list of)", title):
                continue
            yield path.rsplit("/", 1)[-1].replace(".htm", ""), title, text, {
                "type": work.get("type", SCRIPTURE), "order": k, "url": nis.ST + path,
                "via": "web.archive.org snapshot of sacred-texts.com"}
        return
    if work["kind"] == "note":
        for n in json.loads(NOTES_FILE.read_text()):
            yield n["id"], n["title"], n["text"], {"type": "background_note", "topic": n["id"],
                                                   "sources": n.get("sources", []),
                                                   "url": (n.get("sources") or [""])[0],
                                                   "written_by": "Nova (drafted with Claude-supplied facts, reviewed)"}


def build_items(work, uid_base, section, text, extra):
    parts = nis.split_passages(text)
    who = f"{work['translator']} {work['year']}" if work["kind"] != "note" else "Nova's notes"
    out = []
    for i, p in enumerate(parts, 1):
        tag = f"{work['work']} — {section}" if section and work["kind"] != "note" else (section or work["work"])
        tag += f" ({who}" + (f", passage {i}/{len(parts)})" if len(parts) > 1 else ")")
        meta = {"key": work["key"], "work": work["work"], "tradition": work["tradition"], "section": section,
                "framing": work["framing"], "translator": work["translator"], "year": work["year"],
                "license": work["license"], "collection": "books_of_the_dead", "passage": i, "passages": len(parts),
                "uid": f"{work['key']}:{uid_base}:{i}", **({"edition_note": work["note"]} if work.get("note") else {}),
                **extra}
        out.append((meta["uid"], f"[{tag}] " + nis.unwrap(p), meta))
    return out


def run_work(work, dry_run=False):
    import nova_ingest as ni
    have = set() if dry_run else nis.existing_uids(VECTOR)
    done_hashes, n_units, sent, skipped = set(), 0, 0, 0
    for uid_base, section, text, extra in units(work):
        n_units += 1
        for uid, t, meta in build_items(work, uid_base, section, text, extra):
            if uid in have:
                skipped += 1
                continue
            if dry_run:
                sent += 1
                continue
            if sent % 50 == 0:
                while nis.queue_depth() > nis.QUEUE_HIGH:
                    time.sleep(30)
            if ni.remember(t, VECTOR, meta, done_hashes):
                sent += 1
            time.sleep(nis.SEND_DELAY)
    res = {"units": n_units, "sent": sent, "skipped": skipped}
    nis.log(f"[{work['key']}] done: {res}")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("probe")
    p.add_argument("key")
    p.add_argument("--show", type=int, default=0, help="print the first N chars of each unit")
    r = sub.add_parser("run")
    r.add_argument("--work", action="append", default=[])
    r.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        for w in WORKS:
            print(f"{w['key']:22} {w['kind']:5} {w['work'][:70]:70} {w['translator'][:30]} {w['year']}")
        return 0
    if a.cmd == "probe":
        tot = 0
        for uid_base, section, text, extra in units(BY_KEY[a.key]):
            tot += len(text)
            print(f"{uid_base:10} {extra['type'][:10]:10} {len(text):7}  {section[:110]}")
            if a.show:
                print("    " + text[:a.show].replace("\n", "\n    "))
        print("total chars", tot)
        return 0
    todo = [BY_KEY[k] for k in a.work] if a.work else WORKS
    for w in todo:
        nis.log(f"== {w['key']} -> {VECTOR}")
        try:
            run_work(w, a.dry_run)
        except Exception as e:  # noqa: BLE001 — one bad work must not stop the rest
            nis.log(f"[{w['key']}] FAILED: {e!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
