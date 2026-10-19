#!/opt/homebrew/bin/python3
"""nova_ingest_occult.py — classic Western occult / esoteric books into Nova memory (vector `occult_classics`).
(Little Mister 2026-10-18, claude_queue #3830.)

Two kinds of memory, both under source/vector `occult_classics`:
  * type=occult_text      — passages of public-domain editions (US PD: published before 1931), read from
                            Internet Archive OCR / e-texts cached under /Volumes/Data/occult_cache.
  * type=background_note  — short factual notes in Nova's own words: history, edition, copyright status;
                            the only treatment for copyrighted books (no quoted text).

Framing: these are historical grimoires and esoteric literature. Every passage is tagged as a historical
document whose claims (spirits, magical effects, revelations) are the author's, not established fact.

Modern editorial additions are stripped: Celephaïs Press "T.S." notes, O.T.O./Heidrick "WEH" notes and license
boilerplate, Benjamin Rowe typesetting notices, Mario Lampic's web-edition credits, and the 1938 introduction /
Tunis comment around Liber AL. Each memory carries a stable metadata uid, so re-runs skip what is present.

Usage:
  nova_ingest_occult.py list
  nova_ingest_occult.py run [--key K ...] [--dry-run] [--notes-only]
  nova_ingest_occult.py probe KEY        # print cleaned sections/passages (no writes)
Shared helpers (split_passages, existing_uids, queue_depth, unwrap) come from nova_ingest_scripture.py.
Written by Jordan Koch (via Claude).
"""
import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_ingest_scripture import split_passages, existing_uids, queue_depth  # noqa: E402

VECTOR = "occult_classics"
CACHE = Path("/Volumes/Data/occult_cache")
QUEUE_HIGH = 400
SEND_DELAY = 0.15
FRAME = ("historical esoteric literature; its claims about spirits, magic and revelation are the author's "
         "beliefs, not established fact")
US_PD = "Public domain in the US (published before 1931)"


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# ── per-work text preparation ────────────────────────────────────────────────────────────────────

def read(rel: str) -> str:
    return (CACHE / rel).read_bytes().decode("utf-8", "replace").replace("\r\n", "\n")


def lines(text: str, *ranges) -> str:
    """Keep 1-based inclusive line ranges."""
    ls = text.split("\n")
    return "\n".join("\n".join(ls[a - 1:b]) for a, b in ranges)


def between(text: str, start: str, end: str | None = None) -> str:
    m = re.search(start, text, re.M)
    text = text[m.start():] if m else text
    if end:
        e = re.search(end, text, re.M)
        text = text[:e.start()] if e else text
    return text


def prep_goetia():
    t = between(read("ac_goetia/ac_goetia.txt"), r"^PREFATORY NOTE", r"^Notes by the creator of this electronic edition")
    t = re.sub(r"\[[^\[\]]*?T\.\s?S\.\s*\]", "", t, flags=re.S)          # Celephaïs editor notes [... — T.S.]
    t = re.sub(r"\[\[.*?\]\]", "", t, flags=re.S)                        # Crowley marginalia (1993 First Impressions)
    return t


def prep_levi():
    return lines(read("transcendentalma00leviuoft/transcendentalma00leviuoft.txt"), (48, 505), (905, 18074))


def prep_abramelin():
    out = []
    for i in (1, 2, 3):
        t = read(f"sacred-magic-abramelin/Abramelin_{i}.txt")
        t = re.sub(r"(?s)This Adobe Acrobat edition.*?(Text set in Adobe Garamond\.|\d{4}\.)\s*\n", "\n", t, count=1)
        out.append(t)
    return "\n\n".join(out)


def prep_mtp():
    t = read("MagickInTheoryAndPracticeAleisterCrowley/mtp.txt")
    t = t[:re.search(r'(?m)^Aleister Crowley : "', t).start()]          # uploader's appended blurbs (not 1929 text)
    # O.T.O. diskette headers / license blocks inserted between the 1929 text's sections
    t = re.sub(r"(?s)Magick in Theory and Practice by Aleister Crowley\s*\n\s*\n+\s*\d{4} e\.v\. key entry.*?WEH note = Bill Heidrick note, etc\.", "\n", t)
    t = re.sub(r"(?s)«\s*WEH NOTE:.*?»", "", t)                           # Heidrick editorial notes
    t = re.sub(r"(?s)«(.*?)»", lambda m: f" [Note: {m.group(1).strip()}] ", t)  # Crowley's own footnotes, inline
    t = re.sub(r"\{[^{}\n]{1,12}\}", "", t)                              # {page} markers
    t = re.sub(r"(?im)^.*copyright \(c\) o\.?t\.?o\..*$", "", t)
    return t


def prep_777():
    t = read("aleister-crowley-liber-777/aleister-crowley-liber-777.txt")
    t = lines(t, (1, 311), (4091, 4162), (4380, 4659), (4817, 4904))   # 1909 Preface + Crowley's notes to the tables
    t = re.sub(r"\[[^\[\]]*?T\.\s?S\.\s*\]?", "", t, flags=re.S)
    t = re.sub(r"(?m)^.*— T\.S\.\s*$", "", t)
    t = re.sub(r"\[\[.*?\]\]", "", t, flags=re.S)                        # 1955 "777 Revised" additions
    return t


def prep_hall():
    t = read("TheSecretTeachingsOfAllAgesManlyHall/TheSecretTeachingsOfAllAgesManlyHall.txt")
    ls = t.split("\n")
    end = max(i for i, x in enumerate(ls) if x.strip() == "ABOUT THE AUTHOR")
    return lines(t, (38, 200), (621, end))


def prep_agrippa():
    t = read("cu31924028928236/cu31924028928236.txt")
    return lines(t, (620, len(t.split("\n"))))


def prep_lies():
    t = read("CrowleyBookOfLies/CrowleyBookOfLies.txt")
    return t[:re.search(r"(?m)^PRO AND CON ?TENTS", t).start()]            # drop the closing table of contents


def prep_liber_al():
    t = between(read("CrowleyTheBookOfTheLaw/CrowleyTheBookOfTheLaw.txt"), r"^Chapter I\s*$", r"^THE COMMENT\.")
    return t


IA = "https://archive.org/details/"
WORKS = [
    dict(key="goetia_1904", work="The Book of the Goetia of Solomon the King (Lemegeton, Book I)",
         author="Anonymous (17th-c. Lemegeton); edited and introduced by Aleister Crowley",
         translator="S. L. MacGregor Mathers", year=1904,
         license="Public domain worldwide (pub. 1904; Mathers d. 1918, Crowley d. 1947)",
         source_url=IA + "ac_goetia", prep=prep_goetia,
         edition="1904 Society for the Propagation of Religious Truth, Boleskine; Celephaïs Press re-set text (2003) with its T.S. editorial notes and Crowley's posthumously published marginalia removed",
         ignore_heads=r"GOETIA|CLAVICULA|^\d+$"),
    dict(key="levi_transcendental_magic", work="Transcendental Magic, its Doctrine and Ritual (Dogme et Rituel de la Haute Magie)",
         author="Éliphas Lévi (Alphonse Louis Constant)", translator="Arthur Edward Waite", year=1896,
         license="Public domain worldwide (French orig. 1854-56; Waite tr. London: George Redway 1896; Waite d. 1942)",
         source_url=IA + "transcendentalma00leviuoft", prep=prep_levi,
         edition="London: George Redway, 1896 (University of Toronto scan); Waite's biographical preface kept, contents and index dropped",
         ignore_heads=r"TRANS\w*NDENT|^[A-Z]{0,3}\s?(THE )?\w?\s?(D\w{2,4}INE|[RKE]\w?ITUAL) O"),
    dict(key="abramelin_mathers", work="The Book of the Sacred Magic of Abramelin the Mage",
         author="Attributed to Abraham of Worms (Abraham the Jew)", translator="S. L. MacGregor Mathers", year=1900,
         license="Public domain worldwide (1898; 2nd ed. 1900; Mathers d. 1918)",
         source_url=IA + "sacred-magic-abramelin", prep=prep_abramelin,
         edition="John M. Watkins, London, 2nd ed. 1900 (first 1898), complete text as re-set by Benjamin Rowe 1998-99 (typesetting notice removed); magic-square tables in Book III do not survive OCR",
         ignore_heads=r"^THE SACRED MAGIC$|^\d+$"),
    dict(key="crowley_magick_theory_practice", work="Magick in Theory and Practice (Book 4, Part III)",
         author="Aleister Crowley (The Master Therion)", translator=None, year=1929,
         license="US public domain since 2025-01-01 (pub. Paris: Lecram Press, 1929; 95-year term); UK/EU public domain since 2018 (Crowley d. 1947)",
         source_url=IA + "MagickInTheoryAndPracticeAleisterCrowley", prep=prep_mtp,
         edition="1929 text via the O.T.O. Heidrick key entry (1988-90) of the Castle Books reprint; WEH editorial notes, O.T.O. license headers and page markers removed",
         ignore_heads=r"^MAGICK$"),
    dict(key="crowley_777_1909", work="777 (Liber 777 vel Prolegomena Symbolica ad Systemam Sceptico-Mysticae Viae Explicandae)",
         author="Aleister Crowley (published anonymously)", translator=None, year=1909,
         license="Public domain worldwide (London: Walter Scott Publishing, 1909; Crowley d. 1947)",
         source_url=IA + "aleister-crowley-liber-777", prep=prep_777,
         edition="1909 Preface and Crowley's notes to the Tables of Correspondence only; the 1955 '777 Revised' additions ([[...]]), Celephaïs T.S. notes and the OCR-garbled tables themselves excluded",
         ignore_heads=r"^TABLE OF CORRESPONDENCES$"),
    dict(key="hall_secret_teachings", work="The Secret Teachings of All Ages",
         author="Manly P. Hall", translator=None, year=1928,
         license="US public domain (pub. San Francisco 1928; 95-year term expired 2024-01-01, and copyright was never renewed); not PD in life+70 countries until 2061 (Hall d. 1990)",
         source_url=IA + "TheSecretTeachingsOfAllAgesManlyHall", prep=prep_hall,
         edition="San Francisco: H. S. Crocker for Manly P. Hall, 1928 (text as edited for web by Mario Lampic 2009; his credits, the contents list and the modern 'About the Author' dropped)",
         ignore_heads=r"SECRET TEACHINGS|^TEACHINGS OF"),
    dict(key="agrippa_occult_philosophy_book1", work="Three Books of Occult Philosophy, Book I: Natural Magic",
         author="Heinrich Cornelius Agrippa von Nettesheim", translator="'J. F.' (1651), edited by Willis F. Whitehead", year=1898,
         license="Public domain worldwide (Latin 1533; English 1651; Whitehead ed. Chicago: Hahn & Whitehead 1898)",
         source_url=IA + "cu31924028928236", prep=prep_agrippa,
         edition="Hahn & Whitehead, Chicago 1898 (Cornell scan): Book I only, with Whitehead's preface, Henry Morley's life of Agrippa (1856) and Whitehead's 'Magic Mirror'. Books II-III not ingested: only 1651 scans with unusable long-s OCR were available",
         ignore_heads=r"AGRIPPA|OCCULT PHILOSOPHY|NATURAL MAGIC|^\d+$"),
    dict(key="crowley_book_of_lies", work="The Book of Lies, which is also falsely called Breaks (Liber CCCXXXIII)",
         author="Aleister Crowley (Frater Perdurabo)", translator=None, year=1913,
         license="Public domain worldwide (London: Wieland & Co., 1913; Crowley d. 1947)",
         source_url=IA + "CrowleyBookOfLies", prep=prep_lies,
         edition="1913 Wieland first edition text (no commentary: Crowley's commentary appeared only in the 1952 edition)",
         ignore_heads=r"^$"),
    dict(key="crowley_liber_al", work="Liber AL vel Legis (The Book of the Law), chapters I-III",
         author="Aleister Crowley (as received text attributed to 'Aiwass')", translator=None, year=1909,
         license="Public domain worldwide (written 1904; published 1909 in ΘΕΛΗΜΑ and 1913 in The Equinox I(10); Crowley d. 1947)",
         source_url=IA + "CrowleyTheBookOfTheLaw", prep=prep_liber_al,
         edition="Text of the three chapters only; the 1938 introduction and the 'Comment' (later material, still in US copyright) excluded",
         ignore_heads=r"^$", head_re=r"^Chapter [IVX]+$"),
]
BY_KEY = {w["key"]: w for w in WORKS}


# ── generic OCR cleanup + sectioning ─────────────────────────────────────────────────────────────

_HEAD = re.compile(r"^(?:[\dIVXLivxl*]{1,6}\s+)?([A-Z][A-Z0-9 ,.'’:;&()\-—]{3,80}?)\.?(?:\s+[\d\s*]{1,8})?$")


_PAGED = re.compile(r"^(?:[\dIVXLivxl*]{1,5}\s+\S.*|.*\S\s+[\d*]{1,4})$")   # heading text with a page number


def is_heading_line(s: str) -> bool:
    if len(s) > 90 or not _HEAD.match(s):
        return False
    letters = re.sub(r"[^A-Za-z]", "", s)
    return len(letters) >= 4 and not re.search(r"[a-z]", s)


def sections(text: str, ignore: str, head_re: str = ""):
    """-> [(section, body_text)]: all-caps heading paragraphs (and running heads) set the section and are
    removed; page-number lines dropped; hyphenation repaired."""
    text = text.replace("­", "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"(\w)[-¬]\s*\n\s*(?:\d{1,4}\s*\n\s*)?([a-z])", r"\1\2", text)   # hyphenated line breaks
    text = re.sub(r"(\w)- ([a-z]{2,})", r"\1\2", text)                               # 'Chris- tian'
    ign = re.compile(ignore) if ignore else None
    out, cur, buf = [], "", []
    just_head = False

    def flush():
        if buf:
            out.append((cur, "\n\n".join(buf)))
            buf.clear()

    for para in re.split(r"\n\s*\n", text):
        ls = [x.strip() for x in para.split("\n") if x.strip()]
        ls = [x for x in ls if not re.fullmatch(r"[\divxlcIVXLC*.\s]{1,8}", x)]   # page numbers
        if not ls:
            continue
        if len(ls) <= 3 and all(is_heading_line(x) or (head_re and re.match(head_re, x)) for x in ls):
            h = re.sub(r"^[\dIVXLivxl*]+\s+|\s+[\dIVXLivxl\s*]+$", "", " ".join(ls)).strip(" .")
            if ign and ign.search(h):
                continue
            if h and h.title() != cur:
                if not buf and cur and just_head:
                    cur = f"{cur} — {h.title()}"[:120]
                else:
                    flush()
                    cur = h.title()
                just_head = True
            continue
        ls = [x for x in ls if not (is_heading_line(x) and ((ign and ign.search(x)) or _PAGED.match(x)))]  # running heads
        if ls:
            just_head = False
            buf.append(join_lines(ls))
            if sum(len(b) for b in buf) > 12000:
                flush()
    flush()
    return out


def join_lines(ls):
    """Re-flow hard-wrapped prose; keep breaks after short (verse / list) lines."""
    out = ls[0]
    for prev, x in zip(ls, ls[1:]):
        out += (" " if len(prev) >= 48 else "\n") + x
    return out


def quality(t: str) -> float:
    toks = re.findall(r"\S+", t)
    if not toks:
        return 0.0
    good = sum(1 for x in toks if re.fullmatch(r"[\"'(\[]*[A-Za-z][a-z’'\-]*[.,;:!?\"')\]]*", x))
    return good / len(toks)


def build_items(work: dict):
    """-> [(uid, memory_text, meta)]."""
    who = work["author"] + (f", tr. {work['translator']}" if work["translator"] else "") + f", {work['year']}"
    items, seen = [], {}
    for sec, body in sections(work["prep"](), work.get("ignore_heads", ""), work.get("head_re", "")):
        if re.search(r"\bIndex\b|^Contents$", sec or ""):
            continue
        for i, p in enumerate(split_passages(body), 1):
            p = p.strip()
            if len(p) < 120 or quality(p) < 0.62:
                continue
            base = re.sub(r"[^a-z0-9]+", "-", (sec or "front").lower()).strip("-")[:60] or "x"
            seen[base] = seen.get(base, 0) + 1
            n = seen[base]
            label = f"{work['work']}" + (f" — {sec}" if sec else "")
            text = f"[Historical occult text ({FRAME}) — {label} ({who})] {p}"
            meta = {"type": "occult_text", "key": work["key"], "work": work["work"], "author": work["author"],
                    "translator": work["translator"], "year": work["year"], "license": work["license"],
                    "source_url": work["source_url"], "edition": work["edition"], "section": sec,
                    "passage": n, "framing": FRAME, "uid": f"{work['key']}:{base}:{n}"}
            items.append((meta["uid"], text, meta))
    return items


# ── background notes (Nova's own words; the only treatment for copyrighted books) ─────────────────

def N(key, work, author, year, license, url, body, translator=None, related=None):
    return dict(key=key, work=work, author=author, year=year, license=license, source_url=url,
                translator=translator, body=body, related=related)


CR = "In copyright — background note only, no text ingested"
NOTES = [
    N("note_collection", "Occult classics collection (overview)", "Nova", 2026, "Nova's own summary",
      "https://archive.org/",
      "The occult_classics memory collection holds public-domain editions of classic Western occult books "
      "(the 1904 Goetia, Lévi's Transcendental Magic in Waite's 1896 translation, Mathers' Abramelin, "
      "Crowley's 777, Book of Lies, Liber AL and Magick in Theory and Practice, Manly P. Hall's Secret Teachings "
      "of All Ages, Book I of Agrippa's Occult Philosophy) plus background notes on influential books that are "
      "still in copyright. These are historical documents of Western esotericism: grimoires, ceremonial-magic "
      "manuals and occult philosophy. Their statements about spirits, demons, magical powers, correspondences "
      "and revelations record what their authors and traditions believed; they are not established fact, and "
      "Nova should present them as history of ideas, literature and religious practice."),
    N("note_goetia", "The Lesser Key of Solomon / Goetia (background)", "Anonymous; Mathers & Crowley", 1904, "Nova's own summary",
      "https://en.wikipedia.org/wiki/Lesser_Key_of_Solomon",
      "The Lesser Key of Solomon (Lemegeton Clavicula Salomonis) is an anonymous 17th-century grimoire compiled "
      "from earlier material such as Johann Weyer's Pseudomonarchia Daemonum (1577) and the Heptameron; the main "
      "manuscripts are British Library Sloane MSS 2731 and 3825. Its first book, the Ars Goetia, catalogues 72 "
      "spirits with their ranks, seals and powers, followed by conjurations. S. L. MacGregor Mathers translated "
      "it for the Hermetic Order of the Golden Dawn; after their 1900 split Aleister Crowley published the "
      "translation in 1904 with his own preface, the 'Preliminary Invocation' (adapted from a Greco-Egyptian "
      "magical papyrus) and Enochian versions of the conjurations. L. W. de Laurence pirated it in 1916. Joseph "
      "Peterson's critical edition (2001) is in copyright and is not ingested here. Historians treat the work as "
      "a document of early-modern learned magic, not as a description of real beings."),
    N("note_levi", "Éliphas Lévi, Dogme et Rituel de la Haute Magie (background)", "Éliphas Lévi", 1855, "Nova's own summary",
      "https://en.wikipedia.org/wiki/Dogme_et_Rituel_de_la_Haute_Magie",
      "Éliphas Lévi (Alphonse Louis Constant, 1810-1875), a former seminarian, published Dogme et Rituel de la "
      "Haute Magie in two volumes (1854-56; dated 1855-56). It is usually counted as the start of the 19th-century "
      "French occult revival. Each volume has 22 chapters keyed to the Hebrew letters and Tarot trumps; it popularised "
      "the idea of the 'astral light', linked the Tarot to the Kabbalah, and includes the Baphomet image that later "
      "became iconic. A. E. Waite's English translation, Transcendental Magic, its Doctrine and Ritual (London: "
      "George Redway, 1896), with a critical biographical preface, is the edition ingested."),
    N("note_abramelin", "The Book of Abramelin (background)", "Abraham of Worms (attributed)", 1898, "Nova's own summary",
      "https://en.wikipedia.org/wiki/The_Book_of_Abramelin",
      "The Book of Abramelin presents itself as the account of Abraham of Worms, a 14th/15th-century Jew who learned "
      "a system of sacred magic from the Egyptian mage Abramelin and passed it to his son Lamech. Its core is a long "
      "ascetic retreat (six months in Mathers' version, eighteen in German manuscripts) aimed at the 'knowledge and "
      "conversation of the Holy Guardian Angel', after which the operator is said to command spirits through "
      "letter squares. Mathers translated an 18th-century French manuscript in the Bibliothèque de l'Arsenal "
      "(Watkins, 1898; 2nd ed. 1900); the Golden Dawn and Crowley drew heavily on it. Georg Dehn and Steven Guth's "
      "2006 translation from German manuscripts is in copyright and not ingested."),
    N("note_mtp", "Crowley, Magick in Theory and Practice (background)", "Aleister Crowley", 1929, "Nova's own summary",
      "https://en.wikipedia.org/wiki/Magick_(Book_4)",
      "Magick in Theory and Practice is Part III of Crowley's Book 4, published in Paris by Lecram Press in 1929. "
      "It defines Magick as 'the Science and Art of causing Change to occur in conformity with Will' and sets out "
      "Crowley's Thelemic system: chapters on the magical formulae, the Banishing rituals, invocation, evocation, "
      "divination, the Holy Guardian Angel, plus appendices including Liber Samekh. US status: works first published "
      "in 1929 entered the US public domain on 1 January 2025; Crowley died in 1947, so the text is also public "
      "domain in life+70 countries since 2018. Later annotated editions (e.g. the 1994/1997 Hymenaeus Beta 'Magick: "
      "Liber ABA') carry new copyrighted editorial material and are not used."),
    N("note_777", "Crowley, 777 (background)", "Aleister Crowley", 1909, "Nova's own summary",
      "https://en.wikipedia.org/wiki/777_and_Other_Qabalistic_Writings_of_Aleister_Crowley",
      "Liber 777 (London, 1909) is a book of tables of correspondences that align the 10 Sephiroth and 22 paths of "
      "the Qabalistic Tree of Life with gods, colours, planets, Tarot cards, animals, plants, perfumes, magical "
      "weapons and more, drawing largely on Golden Dawn teaching. Only the 1909 preface and Crowley's notes are "
      "ingested here (the tables do not survive OCR). '777 Revised' (1955, with Crowley's later additions) and the "
      "compilation '777 and Other Qabalistic Writings' (1977, Israel Regardie introduction) are in copyright."),
    N("note_hall", "Manly P. Hall, The Secret Teachings of All Ages (background)", "Manly P. Hall", 1928, "Nova's own summary",
      "https://en.wikipedia.org/wiki/The_Secret_Teachings_of_All_Ages",
      "Manly Palmer Hall (1901-1990) self-published The Secret Teachings of All Ages in San Francisco in 1928, a "
      "large-format illustrated encyclopedia of ancient mysteries, Hermeticism, Qabbalah, alchemy, Rosicrucianism, "
      "Freemasonry, Tarot and related symbolism, with colour plates by J. Augustus Knapp. It is a synthesis written "
      "from a believer's perspective and mixes scholarship with speculation (e.g. the Bacon-Shakespeare theory). "
      "US status: the 1928 copyright was not renewed, and in any case all 1928 US publications entered the public "
      "domain on 1 January 2024. Hall founded the Philosophical Research Society in Los Angeles in 1934."),
    N("note_agrippa", "Agrippa, Three Books of Occult Philosophy (background)", "Heinrich Cornelius Agrippa", 1533, "Nova's own summary",
      "https://en.wikipedia.org/wiki/Three_Books_of_Occult_Philosophy",
      "Heinrich Cornelius Agrippa von Nettesheim (1486-1535) wrote De occulta philosophia libri tres, circulated in "
      "manuscript from 1510 and printed in full in 1533. Its three books cover natural (elemental), celestial "
      "(mathematical/astrological) and ceremonial (religious) magic, a Renaissance synthesis of Neoplatonism, "
      "Hermeticism and Christian Kabbalah. The first English translation, by 'J. F.' (often identified as John "
      "French), appeared in London in 1651. The spurious 'Fourth Book' was attributed to him after his death. Only "
      "Book I is ingested (Willis F. Whitehead's 1898 Chicago edition); Donald Tyson's 1993 annotated edition is in "
      "copyright."),
    N("note_book_of_lies", "Crowley, The Book of Lies (background)", "Aleister Crowley", 1913, "Nova's own summary",
      "https://en.wikipedia.org/wiki/The_Book_of_Lies_(Crowley)",
      "The Book of Lies (Liber 333, London: Wieland & Co., 1913) is a set of 93 short, often paradoxical chapters "
      "(numbered 0 to 92 plus front matter) on mysticism, Thelema and magical formulae, written in a deliberately "
      "riddling style. Crowley's explanatory commentary for each chapter was first published in the 1952 edition "
      "and is not included; the 1913 text is public domain."),
    N("note_liber_al", "The Book of the Law (background)", "Aleister Crowley", 1904, "Nova's own summary",
      "https://en.wikipedia.org/wiki/The_Book_of_the_Law",
      "Liber AL vel Legis, the central text of Thelema, was written by Crowley in Cairo on 8-10 April 1904; Crowley "
      "said it was dictated by a being named Aiwass. Its three chapters speak in the voices of Nuit, Hadit and "
      "Ra-Hoor-Khuit and contain the maxims 'Do what thou wilt shall be the whole of the Law' and 'Love is the law, "
      "love under will.' It was first printed in 1909 (in the privately issued ΘΕΛΗΜΑ) and in The Equinox I(10) in "
      "1913. Believers regard it as revealed scripture; outside Thelema it is studied as a 20th-century religious "
      "text. The 1938 introduction and Crowley's 'Comment' are not ingested."),
    # ── copyrighted: notes only ──
    N("note_mystical_qabalah", "Dion Fortune, The Mystical Qabalah", "Dion Fortune (Violet Mary Firth)", 1935, CR,
      "https://openlibrary.org/works/OL114617W",
      "Dion Fortune (Violet Mary Firth, 1890-1946), a British occultist trained in the Golden Dawn offshoot Alpha et "
      "Omega and founder of the Fraternity (later Society) of the Inner Light, published The Mystical Qabalah in "
      "London (Williams & Norgate) in 1935. It is a practical introduction to the Hermetic Qabalah of the Golden "
      "Dawn tradition: the Tree of Life and each of the ten Sephiroth in turn, with their divine names, archangels, "
      "magical images, virtues, vices and symbols, and how the Tree is used as a map for meditation and ritual. "
      "It remains one of the most-recommended starting books on Western Qabalah. Status: still under US copyright "
      "(1935 publication), so only this note is stored."),
    N("note_regardie_golden_dawn", "Israel Regardie, The Golden Dawn", "Israel Regardie", 1937, CR,
      "https://en.wikipedia.org/wiki/The_Golden_Dawn_(book)",
      "Israel Regardie (1907-1985), once Crowley's secretary and later a member of the Stella Matutina (a Golden Dawn "
      "successor order), published the order's instructional papers and rituals in four volumes (Chicago: Aries "
      "Press, 1937-1940), breaking the order's secrecy on the grounds that the system was dying out. The set covers "
      "the grade rituals, knowledge lectures, Qabalah, Tarot, geomancy, Enochian magic, the Lesser Banishing Ritual "
      "of the Pentagram and talisman consecration, and became the main source for modern ceremonial magic. Later "
      "editions (Llewellyn, 1969 onward) reorganised and expanded it. Status: in copyright; note only."),
    N("note_cunningham_wicca", "Scott Cunningham, Wicca: A Guide for the Solitary Practitioner", "Scott Cunningham", 1988, CR,
      "https://openlibrary.org/works/OL80473W",
      "Scott Cunningham (1956-1993) published Wicca: A Guide for the Solitary Practitioner with Llewellyn in 1988. It "
      "was aimed at people practising alone rather than in an initiatory coven, which made it one of the most "
      "influential books in spreading eclectic, self-initiated Wicca. It covers Wiccan theology (Goddess and God), "
      "tools, the magic circle, the seasonal sabbats and lunar esbats, and ends with a sample 'Standing Stones' Book "
      "of Shadows. Cunningham also wrote widely read books on herb, crystal and elemental magic. Status: in "
      "copyright; note only."),
    N("note_buckland_complete", "Buckland's Complete Book of Witchcraft", "Raymond Buckland", 1986, CR,
      "https://openlibrary.org/works/OL1849933W",
      "Raymond Buckland (1934-2017) brought Gardnerian Wicca from Britain to the United States in the 1960s and in "
      "1973 founded his own tradition, Seax-Wica. His Complete Book of Witchcraft (Llewellyn, 1986; revised 2002), "
      "nicknamed 'Big Blue', is arranged as a self-study workbook of lessons with review questions, covering "
      "history, beliefs, rituals, the sabbats, tools, divination, herbalism, healing and coven organisation. It "
      "has been a standard primer for decades. Status: in copyright; note only."),
    N("note_auryn_psychic_witch", "Mat Auryn, Psychic Witch", "Mat Auryn", 2020, CR,
      "https://openlibrary.org/works/OL20756365W",
      "Psychic Witch: A Metaphysical Guide to Meditation, Magick & Manifestation by Mat Auryn (Llewellyn, 2020) is "
      "a contemporary witchcraft manual that treats psychic perception and spellwork as one skill set. It moves "
      "through progressive exercises (meditation, energy work, the 'witch's eye', cleansing and protection, working "
      "with spirits and the self) and became one of the best-selling modern witchcraft books of its decade. Status: "
      "in copyright; note only."),
    N("note_cavendish_black_arts", "Richard Cavendish, The Black Arts", "Richard Cavendish", 1967, CR,
      "https://openlibrary.org/works/OL3478458W",
      "Richard Cavendish (1930-2016), a British historian of the occult who later edited the part-work encyclopedia "
      "Man, Myth & Magic (1970-72), published The Black Arts in 1967 (Routledge & Kegan Paul; Putnam in the US). It "
      "is a readable outsider's survey of European magic: Kabbalah and the magic of names, numerology, astrology, "
      "alchemy, ritual magic and the grimoires, witchcraft trials, Satanism and the black mass. It treats its "
      "subjects historically rather than as practice and is still widely cited as an introduction. Status: in "
      "copyright; note only."),
    N("note_greer_occult_book", "John Michael Greer, The Occult Book", "John Michael Greer", 2017, CR,
      "https://openlibrary.org/works/OL20809616W",
      "The Occult Book: A Chronological Journey from Alchemy to Wicca by John Michael Greer (Sterling, 2017, in the "
      "Sterling Milestones series) is a illustrated timeline of 100 short entries, each tied to a date, running from "
      "ancient divination and the Hermetica through the Renaissance magi, the Golden Dawn, Crowley and Thelema, to "
      "Wicca, chaos magic and modern Paganism. Greer (born 1962) is a prolific writer on occultism and was Grand "
      "Archdruid of the Ancient Order of Druids in America (2003-2015). Status: in copyright; note only."),
    N("note_book_of_thoth", "Crowley, The Book of Thoth", "Aleister Crowley", 1944, CR,
      "https://en.wikipedia.org/wiki/The_Book_of_Thoth_(Crowley)",
      "The Book of Thoth (1944, issued as The Equinox vol. III no. 5) is Crowley's study of the Tarot, written to "
      "accompany the Thoth deck painted by Lady Frieda Harris between 1938 and 1943. It explains the 78 cards in "
      "terms of Qabalah, astrology, alchemy and Thelema and renames several trumps (e.g. 'Lust', 'Art', 'Adjustment', "
      "'The Aeon'). Because it was first published in 1944 it remains under US copyright (to 2039), so only this "
      "note is stored."),
    N("note_reading_lists", "Popular 'classic occult books' reading lists", "Nova", 2026, "Nova's own summary",
      "https://www.goodreads.com/shelf/show/occult",
      "Community reading lists of classic occult books (the r/occult subreddit's recommendations, Goodreads 'occult' "
      "shelves and lists, and used-book sites such as BookScouter) tend to repeat the same core: Crowley's Book of "
      "the Law, Book of Lies, Book of Thoth and Magick, Agrippa's Three Books of Occult Philosophy, the Lesser Key of "
      "Solomon, Lévi's Transcendental Magic, Hall's Secret Teachings of All Ages, The Kybalion, Fortune's Mystical "
      "Qabalah, Regardie's Golden Dawn, and, for witchcraft, Cunningham's Wicca, Buckland's 'Big Blue' and newer "
      "titles like Auryn's Psychic Witch. Pre-1931 titles on those lists are public domain in the US and are held in "
      "full in occult_classics where a clean edition was found; later titles have background notes only."),
]


def note_items():
    out = []
    for n in NOTES:
        text = f"[Background note — {n['work']}] {n['body']} (Source: {n['source_url']})"
        meta = {"type": "background_note", "key": n["key"], "work": n["work"], "author": n["author"],
                "translator": n["translator"], "year": n["year"], "license": n["license"],
                "source_url": n["source_url"], "framing": FRAME, "uid": f"{n['key']}:1"}
        out.append((meta["uid"], text, meta))
    return out


# ── runner ───────────────────────────────────────────────────────────────────────────────────────

def send(items, have, dry_run):
    import nova_ingest as ni
    done_hashes, sent, skipped = set(), 0, 0
    for uid, text, meta in items:
        if uid in have:
            skipped += 1
            continue
        if dry_run:
            sent += 1
            continue
        if sent % 50 == 0:
            while queue_depth() > QUEUE_HIGH:
                time.sleep(30)
        if ni.remember(text, VECTOR, meta, done_hashes):
            sent += 1
        time.sleep(SEND_DELAY)
    return sent, skipped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("--key", action="append")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--notes-only", action="store_true")
    p = sub.add_parser("probe")
    p.add_argument("key")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        for w in WORKS:
            print(f"{w['key']:36} {w['year']}  {w['work']}")
        for n in NOTES:
            print(f"{n['key']:36} {n['year']}  [note] {n['work']}")
        return 0
    if a.cmd == "probe":
        items = build_items(BY_KEY[a.key])
        secs = {}
        for _, t, m in items:
            secs[m["section"]] = secs.get(m["section"], 0) + 1
        for s, c in secs.items():
            print(f"{c:5}  {s}")
        print(f"{len(items)} passages")
        for _, t, _m in items[:2] + items[len(items) // 2:len(items) // 2 + 1] + items[-2:]:
            print("----\n" + t[:900])
        return 0
    have = set() if a.dry_run else existing_uids(VECTOR)
    total = 0
    s, k = send(note_items(), have, a.dry_run)
    log(f"[notes] sent={s} skipped={k}")
    total += s
    if not a.notes_only:
        for w in WORKS:
            if a.key and w["key"] not in a.key:
                continue
            items = build_items(w)
            s, k = send(items, have, a.dry_run)
            log(f"[{w['key']}] passages={len(items)} sent={s} skipped={k}")
            total += s
    log(f"done: {total} sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
