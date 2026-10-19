#!/opt/homebrew/bin/python3
"""nova_ingest_nontheism.py — the philosophical literature of non-theism (atheism, agnosticism, naturalism,
secular humanism, freethought) into Nova memory, vector `nontheism` (Little Mister 2026-10-18).

Framing: philosophy of religion from the non-theist side, kept alongside the scripture vectors (bible, quran,
tanakh, buddhism, hinduism, ...) as primary sources, never as polemic aimed at the user. Every memory carries
metadata key (per-work tag), work, author, translator, year, license (how US public-domain status was verified),
type (philosophy / commentary / background_note / catalog_entry), section and source url.

Not duplicated (already full text in source=philosophy, see the catalog_entry notes): Lucretius/Leonard (pg785),
Hume Dialogues (pg4583), Hume Enquiry incl. "Of Miracles" (pg9662), Nietzsche Antichrist/Mencken (pg19322),
Twain What Is Man? (pg70). Buddhism/Jainism/Samkhya/Confucian/Taoist material is background notes only; the
texts live in the buddhism, jainism, hinduism, confucianism, taoism vectors (other agents' — not touched).

Sources (cached under /Volumes/Data/nontheism_cache, fixed snapshots; line numbers below refer to them):
  * Project Gutenberg plain text (gutenberg/pgNNNN.txt)
  * Wikisource (wikisource/*.wiki raw wikitext, *.json action=parse HTML)
  * Internet Archive OCR _djvu.txt (ia/<ident>/<ident>.txt), cleaned
  * Nova's background notes (notes/notes.json), her own words with source URLs

Usage:
  nova_ingest_nontheism.py list
  nova_ingest_nontheism.py probe KEY [--show N]
  nova_ingest_nontheism.py run [--work KEY ...] [--dry-run]
Written by Jordan Koch (via Claude).
"""
import argparse
import html as htmllib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_ingest_scripture as nis  # noqa: E402  (split_passages, unwrap, existing_uids, queue_depth, log)

VECTOR = "nontheism"
CACHE = Path("/Volumes/Data/nontheism_cache")
PG, WS, IA = CACHE / "gutenberg", CACHE / "wikisource", CACHE / "ia"
NOTES_FILE = CACHE / "notes" / "notes.json"

PD_OLD = "Public domain in the US: published before 1931 (checked against the edition's title page / imprint)"
PD_HICKS = ("Public domain in the US: Loeb Classical Library vol. 2, published 1925 (US term expired 2021-01-01); "
            "Wikisource tags it PD/US. R. D. Hicks d. 1929, so also PD in life+70 countries.")
PD_BAILEY = ("Public domain in the US: Oxford, Clarendon Press, 1926 (US term expired 2022-01-01). "
             "Cyril Bailey d. 1957, so still in copyright in life+70 countries until 2028.")
PD_RUSSELL_1903 = ("Public domain in the US: first published Independent Review, Dec. 1903; this text from "
                   "Mysticism and Logic (1918). Russell d. 1970, so still in copyright in the UK/EU until 2041.")
PD_RUSSELL_1927 = ("Public domain in the US: lecture of 6 March 1927, first published 1927 (Watts & Co., London; US "
                   "term expired 2023-01-01); this scan is Little Blue Book No. 1372 (Haldeman-Julius, Girard KS, "
                   "copyright 1929; US term expired 2025-01-01). Russell d. 1970: still in copyright in the UK/EU "
                   "until 2041.")
PD_TWAIN_1923 = ("Public domain in the US: Europe and Elsewhere, Harper & Brothers 1923 (US term expired "
                 "2019-01-01); Twain d. 1910.")

PHIL, COMM = "philosophy", "commentary"


def W(key, work, author, translator, year, license, kind, **kw):
    return dict(key=key, work=work, author=author, translator=translator, year=year, license=license, kind=kind, **kw)


# pg segments: (first_line, last_line, label, type, heading_regex_or_None). Headings split a segment into units.
CHAP = r"^\s*(CHAP(TER)?\.? [IVXLC]+\.?)\s*$"
WORKS = [
    W("epicurus_diogenes_laertius_x", "Diogenes Laertius, Lives of Eminent Philosophers, Book X: Epicurus "
      "(Life; Letters to Herodotus, Pythocles, Menoeceus; Principal Doctrines)", "Epicurus (via Diogenes Laertius)",
      "R. D. Hicks", 1925, PD_HICKS, "ws_book_x", url="https://en.wikisource.org/wiki/Lives_of_the_Eminent_Philosophers/Book_X",
      tradition="Epicureanism (ancient Greek naturalism)"),
    W("epicurus_vatican_sayings", "Epicurus, Vatican Sayings (Sententiae Vaticanae), from Epicurus: The Extant Remains",
      "Epicurus (and Metrodorus)", "Cyril Bailey", 1926, PD_BAILEY, "ia",
      ident="EpicurusTheExtantRemainsBaileyOxford1926_201309", tradition="Epicureanism (ancient Greek naturalism)",
      note="English translation pages only; facing Greek text and critical apparatus dropped",
      segments=[(5769, 6522, "Vatican Sayings", PHIL, None)]),
    W("hume_natural_history_religion", "David Hume, The Natural History of Religion (Four Dissertations, 1757)",
      "David Hume", "", 1757, "Public domain (published 1757; Wikisource PD-old)", "ws_parse",
      page="Four_Dissertations_The_Natural_History_of_Religion.json",
      url="https://en.wikisource.org/wiki/Four_Dissertations/The_Natural_History_of_Religion",
      tradition="Enlightenment skepticism", cut_after="Errata"),
    W("holbach_system_of_nature", "Baron d'Holbach, The System of Nature, or the Laws of the Moral and Physical World",
      "Paul-Henri Thiry, Baron d'Holbach (as 'Mirabaud')", "anonymous (English translation 1820-21)", 1770,
      PD_OLD + "; French original 1770, English translation 1820-21. The modern introduction by R. D. Richardson in "
      "the Gutenberg file is excluded.", "pg", tradition="French Enlightenment materialism",
      files=[(8909, [(169, 11369, "Volume I", PHIL, CHAP)]), (8910, [(138, None, "Volume II", PHIL, CHAP)])]),
    W("holbach_good_sense", "Baron d'Holbach, Good Sense without God, or Freethoughts Opposed to Supernatural Ideas "
      "(Le Bon Sens, 1772)", "Paul-Henri Thiry, Baron d'Holbach", "anonymous (W. Stewart & Co., London, c. 1900)",
      1772, PD_OLD + " (undated c. 1900 London 'Freethinker's Library' edition; French original 1772)", "pg",
      tradition="French Enlightenment materialism",
      files=[(7319, [(497, 703, "Publisher's note and Author's preface", PHIL, None),
                     (1187, 7118, "Good Sense", PHIL, r"^(\d{1,3})\.$")])]),
    W("meslier_testament", "Jean Meslier, Testament: Voltaire's Abstract of the Testament, with Voltaire's Life of "
      "Meslier and related documents (from 'Superstition in All Ages', 1878)", "Jean Meslier (abridged by Voltaire)",
      "Anna Knoop", 1878, PD_OLD + " (Peter Eckler, New York, 1878). The book's main part, 'Common Sense', is really "
      "d'Holbach's Le Bon Sens and is stored once under holbach_good_sense instead.", "pg",
      tradition="French Enlightenment atheism",
      files=[(17607, [(67, 245, "Life of Jean Meslier by Voltaire; decree of the National Convention", COMM, None),
                      (6880, 8310, "Abstract of the Testament of Jean Meslier (Voltaire)", PHIL,
                       r"^[IVX]+\.--.*$"),
                      (8315, 8760, "Publisher's preface, translator's note, editor's preface (1830), and letters "
                       "of Voltaire and d'Alembert on Meslier", COMM, None)])]),
    W("feuerbach_essence_of_christianity", "Ludwig Feuerbach, The Essence of Christianity",
      "Ludwig Feuerbach", "George Eliot (Marian Evans)", 1854,
      PD_OLD + " (Eliot's translation, first published 1854; this is the 1890 Kegan Paul second edition)", "pg",
      tradition="Left-Hegelian humanism",
      files=[(47025, [(68, 518, "Publishers' note (1890) and Feuerbach's Preface to the Second Edition", PHIL, None),
                      (610, 14370, "The Essence of Christianity", PHIL, r"^(CHAPTER [IVXLC]+\.|APPENDIX\.)$")])]),
    W("ingersoll_the_gods", "Robert G. Ingersoll, The Gods (lecture, 1872; book 1878)", "Robert G. Ingersoll", "",
      1878, PD_OLD, "pg", tradition="American freethought",
      files=[(38107, [(63, 1620, "The Gods", PHIL, None)])]),
    W("ingersoll_mistakes_of_moses", "Robert G. Ingersoll, Some Mistakes of Moses (1879)", "Robert G. Ingersoll", "",
      1879, PD_OLD + " (1880 edition)", "pg", tradition="American freethought",
      files=[(38099, [(63, 5185, "Some Mistakes of Moses", PHIL, r"^[IVXL]+\. [A-Z][A-Z ,.'’-]+\.?$")])]),
    W("ingersoll_why_agnostic", "Robert G. Ingersoll, Why I Am an Agnostic (1896)", "Robert G. Ingersoll", "",
      1896, PD_OLD + " (Dresden Edition vol. IV, 1900)", "pg", tradition="American freethought",
      files=[(38804, [(275, 1638, "Why I Am an Agnostic", PHIL, r"^[IVX]+\.$")])]),
    W("huxley_agnosticism", "T. H. Huxley, essays on agnosticism (Collected Essays vol. V: Science and Christian "
      "Tradition)", "Thomas Henry Huxley", "", 1894, PD_OLD + " (Macmillan / D. Appleton, 1894; Huxley d. 1895)",
      "pg", tradition="Victorian agnosticism",
      files=[(15905, [(784, 2138, "Prologue (Controverted Questions, 1892)", PHIL, None),
                      (4651, 5498, "The Value of Witness to the Miraculous (1889)", PHIL, None),
                      (5499, 5915, "Possibilities and Impossibilities (1891)", PHIL, None),
                      (5916, 7231, "Agnosticism (1889)", PHIL, None),
                      (7232, 8386, "Agnosticism: A Rejoinder (1889)", PHIL, None),
                      (8387, 9845, "Agnosticism and Christianity (1889)", PHIL, None)])]),
    W("russell_free_mans_worship", "Bertrand Russell, A Free Man's Worship (1903)", "Bertrand Russell", "", 1903,
      PD_RUSSELL_1903, "pg", tradition="20th-century secular humanism",
      files=[(25447, [(1624, 1961, "A Free Man's Worship", PHIL, None)])]),
    W("russell_why_not_christian", "Bertrand Russell, Why I Am Not a Christian (lecture, Battersea Town Hall, "
      "6 March 1927)", "Bertrand Russell", "", 1927, PD_RUSSELL_1927, "ia", ident="whyiamnotchristi1372russ",
      tradition="20th-century secular humanism", note="Little Blue Book No. 1372; the companion essay 'Why I Am a "
      "Rationalist' in the same booklet is not included", plain_ocr=True,
      segments=[(33, 974, "Why I Am Not a Christian", PHIL, r"^[A-Z][A-Z' ]{6,}[A-Z?]$")]),
    W("nietzsche_gay_science", "Friedrich Nietzsche, The Joyful Wisdom (La Gaya Scienza / The Gay Science)",
      "Friedrich Nietzsche", "Thomas Common (prose); Paul V. Cohn and Maude D. Petre (verse)", 1882,
      PD_OLD + " (Complete Works vol. 10, ed. Oscar Levy, T. N. Foulis, Edinburgh & London, 1910)", "pg",
      tradition="19th-century German philosophy", aphorisms=True,
      files=[(52881, [(177, 423, "Preface to the Second Edition", PHIL, None),
                      (424, 1131, "Jest, Ruse and Revenge: A Prelude in Rhyme", PHIL, None),
                      (1132, 2693, "Book First", PHIL, None), (2694, 4039, "Book Second", PHIL, None),
                      (4040, 5875, "Book Third", PHIL, None), (5876, 7463, "Book Fourth", PHIL, None),
                      (7464, 9457, "Book Fifth: We Fearless Ones", PHIL, None),
                      (9458, 9980, "Appendix: Songs of Prince Free-as-a-Bird", PHIL, None)])]),
    W("schopenhauer_religion", "Arthur Schopenhauer, Religion: A Dialogue; A Few Words on Pantheism; The Christian "
      "System (Parerga and Paralipomena)", "Arthur Schopenhauer", "T. Bailey Saunders", 1890,
      PD_OLD + " (Swan Sonnenschein, 1890)", "pg", tradition="19th-century German philosophy",
      files=[(10833, [(63, 196, "Prefatory note (Saunders)", COMM, None),
                      (197, 1451, "Religion: A Dialogue", PHIL, None),
                      (1452, 1543, "A Few Words on Pantheism", PHIL, None),
                      (2493, 2810, "The Christian System", PHIL, None)])]),
    W("shelley_necessity_of_atheism", "Percy Bysshe Shelley, The Necessity of Atheism (1811), as expanded in the "
      "note to 'There is no God' in Queen Mab (1813)", "Percy Bysshe Shelley", "", 1813,
      "Public domain (1813; Wikisource transcription of the 1821 London edition of Queen Mab)", "ws_queen_mab",
      page="Queen_Mab_Notes.json", url="https://en.wikisource.org/wiki/Queen_Mab/Notes",
      tradition="English Romantic freethought",
      note="the 1811 Oxford pamphlet's argument is reproduced and expanded here; quotations from Spinoza, "
           "d'Holbach (French) and Pliny (Latin) are Shelley's own"),
    W("twain_mysterious_stranger", "Mark Twain, The Mysterious Stranger: A Romance (1916)", "Mark Twain", "", 1916,
      PD_OLD + " (Harper & Brothers, 1916, ed. A. B. Paine and F. A. Duneka)", "pg", tradition="American freethought",
      files=[(50109, [(95, 3985, "The Mysterious Stranger", PHIL, r"^\s*CHAPTER [IVXL]+\s*$")])]),
    W("twain_bible_teaching", "Mark Twain, Bible Teaching and Religious Practice (Europe and Elsewhere, 1923)",
      "Mark Twain", "", 1923, PD_TWAIN_1923, "pg", tradition="American freethought",
      files=[(68604, [(11224, 11396, "Bible Teaching and Religious Practice", PHIL, None)])]),
    W("twain_war_prayer", "Mark Twain, The War Prayer (dictated 1905; first published 1923)", "Mark Twain", "", 1923,
      PD_TWAIN_1923, "pg", tradition="American freethought",
      files=[(68604, [(11397, 11529, "The War Prayer", PHIL, None)])]),
    W("notes", "Background notes and catalog entries (Nova)", "Nova", "", 2026, "Original notes by Nova", "note"),
]
BY_KEY = {w["key"]: w for w in WORKS}


# ── text helpers (pure) ────────────────────────────────────────────────────────

def pg_lines(num: int) -> list:
    return (PG / f"pg{num}.txt").read_text(encoding="utf-8-sig", errors="replace").splitlines()


def tidy(text: str) -> str:
    text = text.replace("\r", "")
    text = re.sub(r"(?m)^\s*\[Illustration[^\]]*\]\s*$", "", text)
    text = re.sub(r"(?m)^[ \t]+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_by_heading(lines: list, label: str, head_re: str | None, aphorisms: bool = False) -> list:
    """-> [(section_label, text)]. Heading lines start a new unit; the label takes the heading plus, for bare
    'CHAPTER IV.'-style headings, the next non-blank title line."""
    hx = re.compile(head_re) if head_re else (re.compile(r"^\s*(\d{1,3})\.\s*$") if aphorisms else None)
    units, cur, buf = [], label, []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if hx and hx.match(s):
            if "".join(buf).strip():
                units.append((cur, buf))
            head = s
            if re.match(r"^(CHAP(TER)?\.?|BOOK) [IVXLC]+\.?$", s):
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                nxt = lines[j].strip() if j < len(lines) else ""
                if nxt and not hx.match(nxt) and (nxt.isupper() or nxt.startswith("_")):
                    head = f"{s} {lines[j].strip()}"
            head = head.rstrip(".")
            if aphorisms or re.fullmatch(r"\d{1,3}", head):
                head = f"§{head}"
            if head.isupper() and len(head) > 6:
                head = re.sub(r"\b(?:[IVXLC][ivxlc]*|[ivxlc]+)\b(?=[.\s-]|$)", lambda m: m.group(0).upper(), head.title())
                head = re.sub(r"\b(Of|The|And|In|To|A|An|On|For|By|With|Is|As)\b", lambda m: m.group(0).lower(), head)
                head = re.sub(r"[’']S\b", "'s", head[0].upper() + head[1:])
            cur, buf = f"{label} — {head}"[:200], [lines[i]]
        else:
            buf.append(lines[i])
        i += 1
    if "".join(buf).strip():
        units.append((cur, buf))
    return [(sec, tidy("\n".join(b))) for sec, b in units if len(tidy("\n".join(b))) >= 40]


# OCR cleaning: drop Greek type, apparatus, running heads and page numbers; keep English
_GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")
_WORDS = None


def words() -> set:
    global _WORDS
    if _WORDS is None:
        _WORDS = {w.strip().lower() for w in open("/usr/share/dict/words", encoding="utf-8", errors="ignore")}
        _WORDS |= {"epicurus", "metrodorus", "christ", "christian", "christianity", "thou", "thee", "hath", "doth"}
    return _WORDS


def _known(tok: str) -> bool:
    W_ = words()
    if tok in W_:
        return True
    return any(tok.endswith(s) and tok[: -len(s)] in W_ for s in ("s", "es", "ed", "d", "ing", "ly", "'s", "’s", "er"))


def english_line(t: str) -> bool:
    t = t.strip()
    if not t:
        return True
    if len(_GREEK.findall(t)) > 2:
        return False
    toks = [x.lower() for x in re.findall(r"[A-Za-z’']+", t) if len(x) >= 2]
    if not toks:
        return False
    ratio = sum(_known(x) for x in toks) / len(toks)
    return ratio >= (0.55 if len(toks) >= 4 else 0.7)


def clean_ocr(lines: list, plain: bool = False, runhead=None) -> str:
    out = []
    for ln in lines:
        t = re.sub(r"[ \t]{2,}", " ", ln).rstrip()
        if runhead and runhead.search(t):
            continue
        if re.match(r"^\s*\d{1,4}\s*$", t):
            continue
        if not plain and not english_line(t):
            continue
        out.append(t)
    text = "\n".join(out)
    text = re.sub(r"(\w)[-¬]\s*\n\s*(\w)", r"\1\2", text)       # re-join hyphenation across lines
    text = re.sub(r"(?<![\nA-Z?:])\n(?!\n)", " ", text)                 # OCR paragraphs are blank-line separated
    text = re.sub(r"(?<=[a-z,;])\n\n(?=[‘'a-z(])", " ", text)   # paragraph split by a page break
    for bad, good in ((r"\b1s\b", "is"), (r"\b1n\b", "in"), (r"\b1t\b", "it"), (r"\bttme\b", "time"),
                      (r"\$ny\b", "any"), (r"(?m)^AXV\.", "XXV."), (r"\bT (?=would|have|am|do|think|say)", "I "),
                      (r"\bpre\^v\b", "pretty"), (r"\bop\* ?", "op")):
        text = re.sub(bad, good, text)
    return tidy(text)


def wiki_clean(t: str) -> str:
    t = re.sub(r"(?s)<ref[^>/]*/>", "", t)
    t = re.sub(r"(?s)<ref[^>]*>.*?</ref>", "", t)
    t = re.sub(r"\{\{anchor\+\|(\w+)\}\}", r"[\1]", t)
    t = re.sub(r"\{\{w\|[^|}]*\|([^}]*)\}\}", r"\1", t)
    t = re.sub(r"\{\{w\|([^}]*)\}\}", r"\1", t)
    t = re.sub(r"\{\{(?:Greek|tooltip)\|([^|}]*)(?:\|[^}]*)?\}\}", r"\1", t)
    t = re.sub(r"\{\{ppoem\|", "", t)
    t = re.sub(r"(?s)\{\{[^{}]*\}\}", "", t)
    t = re.sub(r"\[\[[^|\]]*\|([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"'{2,}", "", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"(?m)^=+\s*(.*?)\s*=+\s*$", r"\1", t)
    t = re.sub(r"(?m)^\}\}\s*$", "", t)
    return tidy(htmllib.unescape(t))


def html_text(h: str) -> str:
    h = re.sub(r"(?s)<style.*?</style>", "", h)
    h = re.sub(r"(?s)<span[^>]*class=\"[^\"]*pagenum[^\"]*\"[^>]*>.*?</span></span>", "", h)
    h = re.sub(r"(?s)<sup[^>]*class=\"reference\"[^>]*>.*?</sup>", "", h)
    h = re.sub(r"<br\s*/?>", "\n", h)
    h = re.sub(r"</(p|div|h\d|li|tr)>", "\n\n", h)
    t = htmllib.unescape(re.sub(r"<[^>]+>", "", h)).replace("​", "")
    t = re.sub(r"(?m)^[ \t]+|[ \t]+$", "", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


BOOK_X_SPLITS = [
    ("==Epicurus==", "Life of Epicurus (§§1-34)"),
    ("“Epicurus to Herodotus, greeting.", "Letter to Herodotus (§§35-83)"),
    ("“Epicurus to Pythocles, greeting.", "Letter to Pythocles (§§84-116)"),
    ("{{anchor+|117}}", "On the wise man, and the doctrines that follow (§§117-121)"),
    ("“Epicurus to Menoeceus, greeting.", "Letter to Menoeceus (§§122-135)"),
    ("{{anchor+|136}}", "Epicurus and the Cyrenaics on pleasure (§§136-138)"),
    ("{{section|Kuriai_Doxai}}", "Principal Doctrines (Kuriai Doxai, §§139-154)"),
    ("==Footnotes==", None),
]


# ── units ──────────────────────────────────────────────────────────────────────

def units(work: dict):
    """Yield (uid_base, section, text, extra_meta)."""
    k = work["kind"]
    if k == "pg":
        n = 0
        for num, segs in work["files"]:
            lines = pg_lines(num)
            for si, (a, b, label, typ, hre) in enumerate(segs, 1):
                seg = lines[a - 1:(b if b else next(i for i, x in enumerate(lines) if "END OF THE PROJECT" in x))]
                for sec, text in split_by_heading(seg, label, hre, work.get("aphorisms", False)):
                    n += 1
                    m = re.match(r"^\d{1,3}\.\s*\n+\s*_([^_\n]{2,80})_", text) if work.get("aphorisms") else None
                    if m:
                        sec = f"{sec} ({m.group(1).strip().rstrip('.')})"
                    yield f"pg{num}s{si}u{n:03d}", sec, text, {"type": typ, "order": n, "gutenberg": num,
                                                                "url": f"https://www.gutenberg.org/ebooks/{num}",
                                                                "via": "Project Gutenberg plain text"}
        return
    if k == "ia":
        lines = (IA / work["ident"] / f"{work['ident']}.txt").read_text(encoding="utf-8", errors="replace").splitlines()
        runhead = re.compile(r"^\s*(\w{1,3}\s+)?WHY I AM NOT A CHRISTIAN(\s+\w{1,3})?\s*$") if work.get("plain_ocr") else \
            re.compile(r"^\s*(\d{1,3}\s+)?[IVX]+\.\s+FRAGMENT|^\s*FRAGMENTS\s*$|^\s*A\.\s*$|VATICAN COLLECTION|Sigla|Exhortation")
        n = 0
        for si, (a, b, label, typ, hre) in enumerate(work["segments"], 1):
            text = clean_ocr(lines[a - 1:b], plain=work.get("plain_ocr", False), runhead=runhead)
            if work["key"] == "epicurus_vatican_sayings":
                # one unit per run of ~10 sayings, labelled by the saying numbers it covers
                parts = re.split(r"(?m)^(?=\[?[IVXL]+\.\s)", text)
                parts = [p.strip() for p in parts if re.match(r"\[?[IVXL]+\.", p.strip())]
                for gi in range(0, len(parts), 10):
                    grp = parts[gi:gi + 10]
                    first = re.match(r"\[?([IVXL]+)\.", grp[0]).group(1)
                    last = re.match(r"\[?([IVXL]+)\.", grp[-1]).group(1)
                    n += 1
                    yield f"s{si}u{n:03d}", f"{label} {first}-{last}", "\n\n".join(grp), {
                        "type": typ, "order": n, "url": f"https://archive.org/details/{work['ident']}",
                        "via": "Internet Archive OCR (cleaned)"}
                continue
            raw = text.split("\n")
            for sec, t in split_by_heading(raw, label, hre):
                n += 1
                yield f"s{si}u{n:03d}", sec, t, {"type": typ, "order": n,
                                                  "url": f"https://archive.org/details/{work['ident']}",
                                                  "via": "Internet Archive OCR (cleaned)"}
        return
    if k == "ws_book_x":
        raw = (WS / "Lives_of_the_Eminent_Philosophers_Book_X.wiki").read_text(encoding="utf-8")
        pos = [(raw.index(m), lab) for m, lab in BOOK_X_SPLITS]
        for i, (p, lab) in enumerate(pos[:-1]):
            seg = raw[p:pos[i + 1][0]]
            seg = re.sub(r"^==Epicurus==", "", seg)
            yield f"bx{i + 1}", lab, wiki_clean(seg), {"type": PHIL, "order": i + 1, "url": work["url"],
                                                       "via": "Wikisource (Hicks 1925 transcription)"}
        return
    if k in ("ws_parse", "ws_queen_mab"):
        d = json.loads((WS / work["page"]).read_text(encoding="utf-8"))["parse"]
        t = html_text(d["text"])
        if k == "ws_parse":
            a = t.find("INTRODUCTION.")
            t = t[a:t.find(work["cut_after"], a)] if work.get("cut_after") else t[a:]
            yield "nhr", "The Natural History of Religion", t, {"type": PHIL, "order": 1, "url": work["url"],
                                                                "wikisource_revid": d.get("revid"),
                                                                "via": "Wikisource (1757 edition transcription)"}
        else:
            a = t.find("VII. Page 61.")
            b = t.find("VII. Page 63.", a)
            yield "qm_note_vii61", "Note to Canto VII, 'There is no God!'", t[a + len("VII. Page 61."):b].strip(), {
                "type": PHIL, "order": 1, "url": work["url"], "wikisource_revid": d.get("revid"),
                "via": "Wikisource (1821 edition transcription)"}
        return
    if k == "note":
        for i, n in enumerate(json.loads(NOTES_FILE.read_text(encoding="utf-8")), 1):
            yield n["id"], n["title"], n["text"], {
                "type": n.get("kind", "background_note"), "topic": n["id"], "about_key": n.get("key", "notes"),
                "sources": n.get("sources", []), "url": (n.get("sources") or [""])[0], "order": i,
                "written_by": "Nova (drafted with Claude-supplied facts)",
                "cross_reference_vectors": [v for v in ("philosophy", "buddhism", "jainism", "hinduism",
                                                        "confucianism", "taoism") if v in n["text"]]}


def build_items(work, uid_base, section, text, extra):
    parts = nis.split_passages(text)
    who = (f"{work['translator']} tr., {work['year']}" if work["translator"] else str(work["year"])) \
        if work["kind"] != "note" else "Nova's notes"
    out = []
    for i, p in enumerate(parts, 1):
        if work["kind"] == "note":
            tag = f"{section} ({who}" + (f", part {i}/{len(parts)})" if len(parts) > 1 else ")")
        else:
            head = work["work"] if section.split(" — ")[0] in work["work"] and " — " not in section else \
                f"{work['work']} — {section}"
            tag = f"{head} ({who}" + (f", passage {i}/{len(parts)})" if len(parts) > 1 else ")")
        meta = {"key": work["key"] if work["kind"] != "note" else extra.get("about_key", "notes"),
                "work": work["work"], "author": work["author"], "translator": work["translator"],
                "year": work["year"], "license": work["license"], "tradition": work.get("tradition", "nontheism"),
                "framing": ("background note in Nova's own words (neutral, factual), with sources"
                            if work["kind"] == "note" else
                            "philosophy of religion (non-theist perspective); primary source kept alongside the "
                            "scripture vectors, not polemic"),
                "collection": "nontheism", "section": section, "passage": i, "passages": len(parts),
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
            print(f"{w['key']:36} {w['kind']:12} {w['work'][:60]:60} {w['year']}")
        return 0
    if a.cmd == "probe":
        tot = n = 0
        w = BY_KEY[a.key]
        for uid_base, section, text, extra in units(w):
            tot += len(text)
            n += len(nis.split_passages(text))
            print(f"{uid_base:14} {extra['type'][:10]:10} {len(text):7}  {section[:110]}")
            if a.show:
                print("    " + text[:a.show].replace("\n", "\n    "))
        print("total chars", tot, "passages", n)
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
