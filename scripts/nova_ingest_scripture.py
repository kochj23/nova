#!/opt/homebrew/bin/python3
"""nova_ingest_scripture.py — the core scriptures of the Eastern religions, into Nova memory
(Little Mister 2026-10-18: Taoism, Shinto, Hinduism, Buddhism, Confucianism, Jainism, Sikhism, Zoroastrianism).

One vector per tradition (taoism, shinto, hinduism, buddhism, confucianism, jainism, sikhism, zoroastrianism),
the same per-scripture-vector convention as `bible` / `quran` / `book_of_mormon`. Every memory carries
metadata type=scripture plus tradition, work, section/sutta/hymn, translator, year, license and source url.

Sources (public domain or CC0 only; the manifest in nova_ops.scripture_manifest records each one):
  * sacred-texts.com pages (19th/early-20th-century translations, mostly Sacred Books of the East). The live
    site sits behind a Cloudflare browser challenge, so pages are read from the Internet Archive's Wayback
    copies (raw `id_` snapshots), politely, one request at a time, cached under /Volumes/Data.
  * SuttaCentral bilara-data (Bhikkhu Sujato's English translations, CC0) for the Pali suttas: a sparse
    git checkout of translation/en/sujato/sutta.

Chunking follows the text's own structure: one sacred-texts page = one hymn / chapter / sutra / section,
one bilara file = one sutta. Long units are split into ~1,500-character passages on paragraph boundaries.
Each memory has a stable metadata uid; units already in memory are skipped, so re-runs resume safely.

Usage:
  nova_ingest_scripture.py list
  nova_ingest_scripture.py run [--tradition T ...] [--work KEY ...] [--dry-run] [--max-pages N]
  nova_ingest_scripture.py probe KEY          # crawl only, print units found (no memory writes)
Written by Jordan Koch (via Claude).
"""
import argparse
import hashlib
import html as htmllib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

CACHE = Path("/Volumes/Data/scripture_cache")
UA = "NovaIngest/1.0 (personal knowledge base; polite single-threaded crawl)"
ST = "https://sacred-texts.com/"
WAYBACK_STAMPS = ("2022", "2019", "2024")
CHUNK = 1500
PD = "Public domain (pre-1929 publication)"
CC0 = "CC0 1.0 (SuttaCentral)"
PG_OPS = "host=localhost dbname=nova_ops user=kochj"
PG_MEM = "host=localhost dbname=nova_memories user=kochj"
STATS_URL = "http://127.0.0.1:18790/stats"
QUEUE_HIGH = 400          # memory-server write queue depth above which we pause (keeps embedding load moderate)
SEND_DELAY = 0.15         # seconds between memory writes
_last_fetch = {}

# Pages whose titles mark translator front/back matter rather than scripture.
SKIP_TITLE = re.compile(
    r"(?i)\b(introduction|introductory|preface|contents|index|abbreviations|transliteration|errata|"
    r"title page|bibliography|translator's note|editor's note|list of|glossary|appendix [ivx]+: notes|"
    r"general index|addenda|corrigenda|sacred texts|the sacred books of the east|sanskrit text)\b")


def W(key, tradition, work, translator, year, index, expected, unit, **kw):
    vec = {"Taoism": "taoism", "Shinto": "shinto", "Hinduism": "hinduism", "Buddhism": "buddhism",
           "Confucianism": "confucianism", "Jainism": "jainism", "Sikhism": "sikhism",
           "Zoroastrianism": "zoroastrianism"}[tradition]
    d = dict(key=key, tradition=tradition, vector=vec, work=work, translator=translator, year=year,
             license=kw.pop("license", PD), index=index, expected=expected, unit=unit, kind=kw.pop("kind", "st"))
    d.update(kw)
    return d


# The canon. `index` is a sacred-texts path (kind st) or a bilara collection (kind bilara).
WORKS = [
    # ── Taoism ──
    W("tao_te_ching", "Taoism", "Tao Te Ching", "James Legge", 1891, "tao/taote.htm", 81, "chapter",
      split_re=r"^(\d{1,2})$", label="Chapter {}"),
    W("zhuangzi_1", "Taoism", "Zhuangzi (Chuang Tzu), part 1 (SBE 39, Books I-XVII)", "James Legge", 1891,
      "tao/sbe39/index.htm", 17, "book", skip_title=r"(?i)tao te king|t[aâ]o teh? king"),
    W("zhuangzi_2", "Taoism", "Zhuangzi (Chuang Tzu), part 2 + Thai-Shang Tractate", "James Legge", 1891,
      "tao/sbe40/index.htm", 16, "book"),
    W("liezi", "Taoism", "Liezi (Taoist Teachings from the Book of Lieh Tzu)", "Lionel Giles", 1912,
      "tao/tt/index.htm", 8, "chapter"),
    W("i_ching", "Taoism", "I Ching (Yi King)", "James Legge", 1899, "ich/index.htm", 64, "hexagram",
      skip_path=r"ictp|ictoc|icpref|icintr|/pl\d"),
    W("kan_ying_pien", "Taoism", "T'ai-Shang Kan-Ying P'ien (Treatise on Response and Retribution)",
      "D. T. Suzuki & Paul Carus", 1906, "tao/ts/index.htm", 1, "text"),
    # ── Shinto ──
    W("kojiki", "Shinto", "Kojiki", "Basil Hall Chamberlain", 1882, "shi/kj/index.htm", 180, "section"),
    W("nihongi", "Shinto", "Nihongi (Chronicles of Japan), abridged", "W. G. Aston", 1896, "shi/nihon0.htm", 6, "book",
      extra_index=["shi/nihon1.htm", "shi/nihon2.htm", "shi/nihon3.htm"], split_re=r"^((?:Book|BOOK) [IVXL]+)\.?$", label="{}",
      note="sacred-texts edition is Aston's abridgement (Books I-III, XXV-XXX selections), not the complete 30 books"),
    W("norito", "Shinto", "Yengishiki norito (Shinto rituals)", "Ernest Satow", 1879, "shi/yengi.htm", 27, "ritual",
      split_re=r"^((THE )?[A-Z][A-Z ,'.\-]{6,})$", label="{}"),
    # ── Hinduism ──
    W("rig_veda", "Hinduism", "Rig Veda", "Ralph T. H. Griffith", 1896, "hin/rigveda/index.htm", 1028, "hymn",
      id_re=r"rv(?P<mandala>\d\d)(?P<hymn>\d{3})\.htm"),
    W("sama_veda", "Hinduism", "Sama Veda", "Ralph T. H. Griffith", 1895, "hin/sv.htm", 50, "chapter",
      split_re=r"^((BOOK|CHAPTER|PART) [IVXL]+\.?)$", parent_re=r"^(BOOK|PART) ", label="{}"),
    W("yajur_veda", "Hinduism", "White Yajur Veda", "Ralph T. H. Griffith", 1899, "hin/wyv/index.htm", 40, "book",
      force_index=True, path_re=r"wyvbk\d\d\.htm"),  # index page carries an epigraph + prose, so is_index_page misses it
    W("atharva_veda", "Hinduism", "Atharva Veda", "Ralph T. H. Griffith", 1895, "hin/av/index.htm", 731, "hymn"),
    W("upanishads_1", "Hinduism", "Upanishads, part 1 (SBE 1)", "F. Max Müller", 1879, "hin/sbe01/index.htm", 50, "section"),
    W("upanishads_2", "Hinduism", "Upanishads, part 2 (SBE 15)", "F. Max Müller", 1884, "hin/sbe15/index.htm", 120, "section"),
    W("gita_arnold", "Hinduism", "Bhagavad Gita (The Song Celestial)", "Sir Edwin Arnold", 1885, "hin/gita/index.htm", 18, "chapter"),
    W("gita_telang", "Hinduism", "Bhagavad Gita (SBE 8)", "Kashinath Trimbak Telang", 1882, "hin/sbe08/index.htm", 18, "chapter"),
    W("mahabharata", "Hinduism", "Mahabharata", "Kisari Mohan Ganguli", 1896, "hin/maha/mahatxt.zip", 2100, "section",
      kind="maha_zip"),  # archived index is the JS redesign (no book links); sacred-texts' own all-books text zip instead
    W("ramayana", "Hinduism", "Ramayana", "Ralph T. H. Griffith", 1895, "hin/rama/index.htm", 650, "canto"),
    W("yoga_sutras", "Hinduism", "Yoga Sutras of Patanjali", "Charles Johnston", 1912, "hin/ysp/index.htm", 4, "book"),
    W("manu", "Hinduism", "Laws of Manu (SBE 25)", "Georg Bühler", 1886, "hin/manu.htm", 12, "chapter", scope="hin/manu/"),
    # ── Buddhism ──
    W("pali_dn", "Buddhism", "Digha Nikaya (Long Discourses)", "Bhikkhu Sujato", 2018, "dn", 34, "sutta",
      kind="bilara", license=CC0),
    W("pali_mn", "Buddhism", "Majjhima Nikaya (Middle-Length Discourses)", "Bhikkhu Sujato", 2018, "mn", 152, "sutta",
      kind="bilara", license=CC0),
    W("pali_sn", "Buddhism", "Samyutta Nikaya (Linked Discourses)", "Bhikkhu Sujato", 2018, "sn", 1819, "sutta",
      kind="bilara", license=CC0),
    W("pali_an", "Buddhism", "Anguttara Nikaya (Numbered Discourses)", "Bhikkhu Sujato", 2018, "an", 1408, "sutta",
      kind="bilara", license=CC0),
    W("pali_kn", "Buddhism", "Khuddaka Nikaya (Dhammapada, Sutta Nipata, Udana, Itivuttaka, Theragatha, Therigatha, ...)",
      "Bhikkhu Sujato", 2018, "kn", 755, "sutta", kind="bilara", license=CC0),
    W("lotus_sutra", "Buddhism", "Lotus Sutra (Saddharma Pundarika, SBE 21)", "Hendrik Kern", 1884, "bud/lotus/index.htm", 27, "chapter"),
    W("mahayana_sbe49", "Buddhism", "Buddhist Mahayana Texts (SBE 49): Larger & Smaller Sukhavati-vyuha, Diamond Sutra, Heart Sutras, Amitayur-dhyana",
      "F. Max Müller, J. Takakusu, E. B. Cowell", 1894, "bud/sbe49/index.htm", 30, "section"),
    # ── Confucianism ──
    W("analects", "Confucianism", "Analects (Lun Yu)", "James Legge", 1893, "cfu/conf1.htm", 20, "book", split_re=r"^(\d{1,2})$", label="Book {}"),
    W("great_learning", "Confucianism", "Great Learning (Ta Hsio)", "James Legge", 1893, "cfu/conf2.htm", 1, "text"),
    W("doctrine_of_mean", "Confucianism", "Doctrine of the Mean (Chung Yung)", "James Legge", 1893, "cfu/conf3.htm", 1, "text"),
    W("mencius", "Confucianism", "Mencius (Meng Tzu)", "James Legge", 1895, "cfu/menc/index.htm", 14, "book"),
    W("xiao_jing", "Confucianism", "Classic of Filial Piety (Hsiao King, SBE 3)", "James Legge", 1879, "cfu/sbe03/index.htm", 18, "chapter",
      path_re=r"sbe031(8[1-9]|9\d)\.htm"),
    W("shu_shih_king", "Confucianism", "Shu King (Book of Documents) and Shih King religious odes (SBE 3)", "James Legge", 1879,
      "cfu/sbe03/index.htm", 150, "book/ode", path_re=r"sbe03(0[0-9]\d|1[0-7]\d)\.htm|shu\d+\.htm"),
    # ── Jainism ──
    W("jaina_sutras_1", "Jainism", "Jaina Sutras, part 1 (SBE 22): Akaranga Sutra, Kalpa Sutra", "Hermann Jacobi", 1884,
      "jai/sbe22/index.htm", 60, "lecture"),
    W("jaina_sutras_2", "Jainism", "Jaina Sutras, part 2 (SBE 45): Uttaradhyayana Sutra, Sutrakritanga", "Hermann Jacobi", 1895,
      "jai/sbe45/index.htm", 60, "lecture"),
    # ── Sikhism ──
    W("macauliffe", "Sikhism", "The Sikh Religion: Gurus, Sacred Writings and Authors (translations from the Guru Granth Sahib)",
      "Max Arthur Macauliffe", 1909, "skh/tsr1/index.htm", 25, "section",
      note="Only volume 1 of 6 survives in the archived sacred-texts copy; Khalsa's full Granth translation skipped (no free license)"),
    W("macauliffe_v2_6", "Sikhism", "The Sikh Religion, vols. II-VI (Lives and hymns of Gurus Angad through Gobind Singh, Bhagats)",
      "Max Arthur Macauliffe", 1909, "sikhreligionitsg02unse sikhreligionitsg03unse sikhreligionitsg04unse "
      "sikhreligionitsg05unse sikhreligionitsg06unse", 1100, "passage-group", kind="ia",
      note="Internet Archive OCR (Princeton Theological Seminary scan, Oxford 1909); vol. I comes from sacred-texts (key macauliffe)"),
    # ── Zoroastrianism ──
    W("avesta_vendidad", "Zoroastrianism", "Zend-Avesta part 1: Vendidad (SBE 4)", "James Darmesteter", 1880,
      "zor/sbe04/index.htm", 22, "fargard"),
    W("avesta_yashts", "Zoroastrianism", "Zend-Avesta part 2: Sirozahs, Yashts, Nyayish (SBE 23)", "James Darmesteter", 1883,
      "zor/sbe23/index.htm", 40, "yasht"),
    W("avesta_yasna", "Zoroastrianism", "Zend-Avesta part 3: Yasna (incl. Gathas), Visparad (SBE 31)", "L. H. Mills", 1887,
      "zor/sbe31/index.htm", 72, "chapter"),
]
BY_KEY = {w["key"]: w for w in WORKS}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# ── fetching ───────────────────────────────────────────────────────────────────

def _cache_path(url: str) -> Path:
    p = urllib.parse.urlparse(url)
    rel = (p.netloc + p.path).strip("/") or "root"
    return CACHE / "wayback" / rel


def is_challenge(body: str) -> bool:
    """Cloudflare interstitial, or the 2024 WordPress redesign (absolute links, no classic body) — both unusable."""
    return ("Just a moment..." in body[:3000] or "challenge-platform" in body[:6000]
            or "woocommerce" in body[:20000])


def http_get(url: str, attempts: int = 5, min_gap: float = 4.0, _sleep=time.sleep, _open=urllib.request.urlopen) -> bytes:
    host = urllib.parse.urlparse(url).netloc
    for i in range(attempts):
        gap = time.time() - _last_fetch.get(host, 0)
        if gap < min_gap:
            _sleep(min_gap - gap)
        _last_fetch[host] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            return _open(req, timeout=90).read()
        except urllib.error.HTTPError as e:
            if e.code == 404 or i == attempts - 1:
                raise
            log(f"  HTTP {e.code} on {url}; retry {i + 1}")
        except Exception as e:  # noqa: BLE001 — network flake: retried, then re-raised
            if i == attempts - 1:
                raise
            log(f"  fetch error {e!r} on {url}; retry {i + 1}")
        _sleep(15 * 2 ** i)
    raise RuntimeError("unreachable")


def decode(raw: bytes) -> str:
    if raw[:2] == b"\x1f\x8b":          # Wayback id_ snapshots replay the original gzip Content-Encoding
        import gzip
        raw = gzip.decompress(raw)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def fetch_st(path: str) -> str | None:
    """A sacred-texts page by its site path, via the cache or Wayback. None when no clean snapshot exists."""
    cp = _cache_path(ST + path)
    if cp.exists():
        body = decode(cp.read_bytes())
        return None if body == "" else body
    for stamp in WAYBACK_STAMPS:
        url = f"https://web.archive.org/web/{stamp}id_/{ST}{path}"
        try:
            raw = http_get(url)
            body = decode(raw)
        except urllib.error.HTTPError:
            continue
        except Exception as e:  # noqa: BLE001
            log(f"  giving up on {path}: {e!r}")
            return None
        if body and not is_challenge(body):
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_bytes(raw)
            return body
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_bytes(b"")        # remember the miss so re-runs do not hammer Wayback
    return None


# ── sacred-texts parsing (pure) ────────────────────────────────────────────────

def page_title(html: str) -> str:
    m = re.search(r"(?is)<title>(.*?)</title>", html)
    t = htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1)))).strip() if m else ""
    return t.split(": ", 1)[1].strip() if ": " in t else t


def local_links(html: str, page_path: str) -> list:
    """Ordered, de-duplicated .htm links that stay inside the page's own directory tree."""
    base_dir = page_path.rsplit("/", 1)[0] + "/" if "/" in page_path else ""
    out, seen = [], set()
    for href in re.findall(r'(?i)<a\s[^>]*href="([^"#]+)"', html):
        if "://" in href or href.startswith(("/", "..", "mailto:")) or not href.lower().endswith(".htm"):
            continue
        p = urllib.parse.urljoin(ST + page_path, href)[len(ST):]
        if not p.startswith(base_dir) or p == page_path or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


_NAV_HEAD = re.compile(r"(?is)^.*?at sacred-texts\.com.*?</p>")
_FOOTNOTES = re.compile(r"(?is)<h\d[^>]*>\s*footnotes\s*</h\d>.*$")
_TAIL_NAV = re.compile(r"(?is)<hr>\s*<center>\s*<a href=[^>]*>\s*(next|previous|index)\b.*$")
_PAGE_NO = re.compile(r'(?is)<a name="(page_|p)[^"]*">\s*(<font[^>]*>)?\s*p\.\s*[\divxlc]+\s*(</font>)?\s*</a>')
_PAGE_NO2 = re.compile(r'(?is)<font size="1" color="green">\s*p\.\s*[\divxlc]+\s*</font>')
_FN_REF = re.compile(r'(?is)<a name="fr_\d+"></a>\s*<a href="#fn_\d+">.*?</a>')
_FN_SUP = re.compile(r'(?is)<a href="#fn_\d+">.*?</a>')


def page_text(html: str) -> str:
    """Scripture text of one sacred-texts page: nav, page numbers, footnote marks and footnotes removed."""
    body = re.sub(r"(?is)^.*?<body[^>]*>", "", html)
    body = _NAV_HEAD.sub("", body, count=1) if "at sacred-texts.com" in body[:6000] else body
    body = _FOOTNOTES.sub("", body)
    body = _TAIL_NAV.sub("", body)
    body = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", "", body)
    for rx in (_PAGE_NO, _PAGE_NO2, _FN_REF, _FN_SUP):
        body = rx.sub("", body)
    body = re.sub(r"(?i)<br\s*/?>", "\n", body)
    body = re.sub(r"(?i)</?(p|h\d|div|blockquote|tr|li|center|hr)[^>]*>", "\n\n", body)
    body = htmllib.unescape(re.sub(r"(?s)<[^>]+>", "", body)).replace("\xa0", " ")
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r" *\n *", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    body = re.sub(r"(?im)^(next|previous|index|sacred texts|buy this book at amazon\.com)\b.*$", "", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def is_index_page(html: str, text: str, n_local: int = 0) -> bool:
    """True for table-of-contents pages: mostly link text, little prose."""
    links = re.findall(r"(?is)<a\s[^>]*href=\"[^\"#]+\.htm\"[^>]*>(.*?)</a>", html)
    if len(links) < 3:
        return False
    link_chars = sum(len(re.sub(r"<[^>]+>", "", l)) for l in links)
    if link_chars >= 0.45 * max(len(text), 1):
        return True
    # a short hymn page has prev/next/index links only; a real TOC with a little prose has many more
    return n_local >= 5 and len(text) - link_chars < 700


def split_units(text: str, split_re: str, label: str = "{}", parent_re: str | None = None,
                initial: str | None = None) -> list:
    """Split one long page into its own headed units. Text before the first heading (front matter) is dropped.
    A heading matching parent_re (e.g. BOOK) prefixes the following child headings (e.g. CHAPTER).
    `initial` continues a unit begun on the previous page (multi-page works). Pure."""
    rx, prx = re.compile(split_re), re.compile(parent_re) if parent_re else None
    out, parent, cur, buf = [], "", initial, []
    for line in text.split("\n"):
        m = rx.match(line.strip())
        if m:
            if cur is not None:
                out.append((cur, "\n".join(buf).strip()))
            head = label.format(m.group(1).strip().rstrip("."))
            if prx and prx.match(line.strip()):
                parent, cur = head, head
            else:
                cur = f"{parent}, {head}" if parent else head
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out.append((cur, "\n".join(buf).strip()))
    return [(h, t) for h, t in out if len(t) >= 80]


def split_passages(text: str, size: int = CHUNK) -> list:
    """Paragraph-respecting chunks of at most ~`size` chars; oversize paragraphs split on sentences/words. Pure."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, cur = [], ""
    for p in paras:
        pieces = [p]
        if len(p) > size:
            pieces, buf = [], ""
            for s in re.split(r"(?<=[.;:!?])\s+|\n", p):
                while len(s) > size:                     # a run-on with no punctuation: cut at a space
                    cut = s.rfind(" ", 0, size)
                    cut = cut if cut > size // 2 else size
                    if buf:
                        pieces.append(buf); buf = ""
                    pieces.append(s[:cut].strip()); s = s[cut:].strip()
                if buf and len(buf) + len(s) + 1 > size:
                    pieces.append(buf); buf = ""
                buf = f"{buf} {s}".strip() if buf else s
            if buf:
                pieces.append(buf)
        for q in pieces:
            if cur and len(cur) + len(q) + 2 > size:
                out.append(cur); cur = ""
            cur = f"{cur}\n\n{q}" if cur else q
    if cur:
        out.append(cur)
    return out


def crawl_st(work: dict, max_pages: int = 0, fetch=None):
    """Yield (path, title, text) for every content unit under the work's index(es), in reading order."""
    fetch = fetch or fetch_st
    seen, n, last = set(), [0], [None]
    skip = re.compile(work["skip_title"]) if work.get("skip_title") else None
    skip_path = re.compile(work["skip_path"]) if work.get("skip_path") else None
    path_re = re.compile(work["path_re"]) if work.get("path_re") else None
    scope = work.get("scope") or (work["index"].rsplit("/", 1)[0] + "/" if "/" in work["index"] else "")

    def walk(path, depth):
        if path in seen or (max_pages and n[0] >= max_pages):
            return
        seen.add(path)
        if depth and (not path.startswith(scope) or path.endswith("pageidx.htm")
                      or (skip_path and skip_path.search(path))):
            return
        html = fetch(path)
        if html is None:
            log(f"  [{work['key']}] no snapshot: {path}")
            return
        text = page_text(html)
        title = page_title(html)
        if depth < 3 and not work.get("split_re") and ((depth == 0 and work.get("force_index"))
                                                       or is_index_page(html, text, len(local_links(html, path)))):
            for p in local_links(html, path):
                if path_re and not path_re.search(p) and depth == 0:
                    continue
                yield from walk(p, depth + 1)
            return
        if SKIP_TITLE.search(title) and not re.search(r"(?i)\b(hymn|chapter|book|section|canto|lecture|sutra)\b", title):
            return
        if skip and skip.search(title):
            return
        if work.get("split_re"):
            prev = last[0]
            units_ = split_units(text, work["split_re"], work.get("label", "{}"), work.get("parent_re"), last[0])
            for k, (head, body) in enumerate(units_, 1):
                n[0] += 1
                last[0] = head
                yield f"{path}#{k}", head + (" (cont.)" if k == 1 and head == prev else ""), body
            return
        if len(text) < 80:
            return
        n[0] += 1
        yield path, title, text

    for idx in [work["index"], *work.get("extra_index", [])]:
        yield from walk(idx, 0)


# ── bilara (SuttaCentral) ──────────────────────────────────────────────────────

BILARA = CACHE / "bilara-data"
BILARA_SUB = "translation/en/sujato/sutta"


def bilara_sync() -> Path:
    """Sparse, blob-filtered checkout of only Sujato's English sutta translations (published branch)."""
    if not (BILARA / ".git").exists():
        BILARA.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", "--branch", "published",
                        "https://github.com/suttacentral/bilara-data.git", str(BILARA)], check=True, timeout=1800)
        subprocess.run(["git", "-C", str(BILARA), "sparse-checkout", "set", BILARA_SUB, "_publication"],
                       check=True, timeout=3600)
    return BILARA / BILARA_SUB


def _natkey(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def sutta_units(path: Path) -> tuple:
    """-> (uid, title, text) from one bilara translation JSON. Heading segments (':0.') become the title. Pure-ish."""
    d = json.loads(path.read_text())
    uid = path.name.split("_")[0]
    heads, body = [], []
    for seg, s in d.items():
        s = (s or "").strip()
        if not s:
            continue
        (heads if re.search(r":0\.\d+$", seg) else body).append(s)
    title = " — ".join(h for h in heads if not re.match(r"(?i)^(linked|numbered|middle|long) discourses", h))[:200]
    text = "\n".join(body)
    return uid, title, text


def bilara_files(coll: str) -> list:
    root = bilara_sync() / coll
    return sorted(root.rglob("*.json"), key=lambda p: _natkey(str(p.relative_to(root))))


# ── memory writes ──────────────────────────────────────────────────────────────

def existing_uids(vector: str) -> set:
    import psycopg2
    with psycopg2.connect(PG_MEM) as c, c.cursor() as cur:
        cur.execute("SELECT metadata->>'uid' FROM memories WHERE source=%s AND metadata ? 'uid'", (vector,))
        return {r[0] for r in cur.fetchall()}


def queue_depth() -> int:
    try:
        return int(json.load(urllib.request.urlopen(STATS_URL, timeout=10)).get("queue_length", 0))
    except Exception:  # noqa: BLE001 — stats outage: do not block, just go slowly
        return 0


def unwrap(text: str) -> str:
    """Collapse blank lines and re-join hard-wrapped prose lines (a long line followed by a lowercase
    continuation); short verse lines keep their breaks. Pure."""
    text = re.sub(r"\n{2,}", "\n", text)
    return re.sub(r"(?<=[^\n]{55})\n(?=[a-z(])", " ", text)


def build_items(work: dict, uid_base: str, section: str, text: str, extra: dict) -> list:
    """-> [(uid, memory_text, meta)] for one unit. Pure."""
    parts = split_passages(text)
    who = f"{work['translator']} {work['year']}"
    out = []
    for i, p in enumerate(parts, 1):
        tag = f"{work['work']} — {section}" if section else work["work"]
        tag += f" ({who}" + (f", passage {i}/{len(parts)})" if len(parts) > 1 else ")")
        meta = {"type": "scripture", "tradition": work["tradition"], "work": work["work"], "section": section,
                "translator": work["translator"], "year": work["year"], "license": work["license"],
                "passage": i, "passages": len(parts), "uid": f"{work['key']}:{uid_base}:{i}", **extra}
        out.append((meta["uid"], f"[{tag}] " + unwrap(p), meta))
    return out


IA = "https://archive.org/"
_IA_HEAD = re.compile(r"^(?:\d{1,3} )?(THE SIKH RELIGION|(LIFE|HYMNS|SLOKS|COMPOSITIONS|WRITINGS) OF [A-Z ,.’']+|SIKH[.,]? [IVXHL1]+( [A-Z]| P\. [IVX1]+)?|[A-Z]-?|[\divxlc ]{1,6})(?: \d{1,3})?$")


def fetch_ia(ident: str) -> str:
    """OCR text (_djvu.txt) of an Internet Archive item, cached under /Volumes/Data."""
    cp = CACHE / "ia" / f"{ident}.txt"
    if not cp.exists() or cp.stat().st_size == 0:
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_bytes(http_get(f"{IA}download/{ident}/{ident}_djvu.txt"))
    return cp.read_bytes().decode("utf-8", "replace")


def ia_units(raw: str, size: int = 6000) -> list:
    """-> [(section, text)] for one OCR'd volume: library front matter and the closing index dropped, running
    heads / page numbers / printer signatures removed, hyphenations re-joined, grouped into ~`size`-char units
    on paragraph boundaries. section = the most recent running head (e.g. 'Life of Guru Angad'). Pure."""
    m = re.search(r"(?m)^https://archive\.org/details/\S+\s*$", raw)
    raw = raw[m.end():] if m else raw
    ends = [x.start() for x in re.finditer(r"(?m)^\s*INDEX\s*$", raw)]
    if ends and ends[-1] > len(raw) * 0.8:
        raw = raw[:ends[-1]]
    raw = re.sub(r"¬\s*\n\s*", "", raw)
    out, buf, head, cur_head = [], [], "", ""
    for para in re.split(r"\n\s*\n", raw):
        lines = []
        for ln in para.splitlines():
            t = re.sub(r"\s{2,}", " ", ln).strip()
            if not t:
                continue
            if _IA_HEAD.match(t):
                h = re.sub(r"^\d+ | \d+$", "", t)
                if re.match(r"^(LIFE|HYMNS|SLOKS|COMPOSITIONS|WRITINGS) OF", h):
                    cur_head = h.rstrip(" .,").title().replace("’S", "’s")
                continue
            if len(re.sub(r"[^A-Za-z]", "", t)) < 3 and len(t) < 8:
                continue
            lines.append(t)
        if not lines:
            continue
        p = "\n".join(lines)
        if not buf:
            head = cur_head
        buf.append(p)
        if sum(len(x) for x in buf) >= size:
            out.append((head, "\n\n".join(buf))); buf = []
    if buf:
        out.append((head, "\n\n".join(buf)))
    return out


def maha_sections(raw: str) -> tuple:
    """-> (parva_name, [(section_label, text)]) for one maha<NN>.txt of sacred-texts' Mahabharata zip.
    Section heads are 'SECTION XII' (most books) or a bare number line (books 8-11, 16-18). Pure."""
    raw = raw.replace("\r\n", "\n")
    m = re.search(r"(?m)^BOOK \d+\s*\n\s*\n?\s*(.+?)\s*$", raw)
    parva = m.group(1).strip().title().replace("-Parva", " Parva") if m else ""
    raw = re.split(r"(?m)^\s*FOOTNOTES\s*$", raw)[0]
    raw = re.sub(r"(?m)^\s*END OF .*PARVA.*$", "", raw)
    parts = re.split(r"(?m)^[ \t]*(SECTION [IVXLCDM]+|\d{1,3})[ \t]*$", raw)
    out = []
    for i in range(1, len(parts) - 1, 2):
        txt = parts[i + 1].strip()
        if len(txt) >= 80:
            out.append((parts[i].title() if parts[i].startswith("SECTION") else f"Section {parts[i]}", txt))
    return parva, out


def units(work: dict, max_pages: int = 0):
    """Yield (uid_base, section, text, extra_meta) for a work."""
    if work["kind"] == "maha_zip":
        d = CACHE / "maha_zip" / "txt"
        if not d.exists():
            import zipfile
            z = CACHE / "maha_zip" / "mahatxt.zip"
            if not z.exists():
                z.parent.mkdir(parents=True, exist_ok=True)
                z.write_bytes(http_get(f"https://web.archive.org/web/2022id_/{ST}{work['index']}"))
            zipfile.ZipFile(z).extractall(d)
        for f in sorted(d.glob("maha*.txt")):
            book = int(re.search(r"(\d+)", f.name).group(1))
            parva, secs = maha_sections(f.read_bytes().decode("utf-8", "replace"))
            for k, (label, text) in enumerate(secs, 1):
                if max_pages and k > max_pages:
                    break
                yield f"b{book:02d}s{k:03d}", f"Book {book}, {parva}, {label}", text, {
                    "book": book, "parva": parva, "section_no": k,
                    "url": f"{ST}hin/m{book:02d}/index.htm", "via": "sacred-texts mahatxt.zip (Wayback)"}
        return
    if work["kind"] == "ia":
        for ident in work["index"].split():
            vol = int(re.search(r"(\d+)\D*$", ident).group(1))
            for k, (head, text) in enumerate(ia_units(fetch_ia(ident)), 1):
                if max_pages and k > max_pages:
                    break
                yield f"v{vol}u{k:04d}", f"Vol. {vol}" + (f", {head}" if head else ""), text, {
                    "volume": vol, "order": k, "url": f"{IA}details/{ident}", "via": "Internet Archive OCR"}
        return
    if work["kind"] == "bilara":
        for f in bilara_files(work["index"]):
            uid, title, text = sutta_units(f)
            if len(text) < 40:
                continue
            yield uid, f"{uid.upper()} {title}".strip(), text, {
                "sutta": uid, "nikaya": work["index"].upper(),
                "url": f"https://suttacentral.net/{uid}/en/sujato",
                "source_repo": "https://github.com/suttacentral/bilara-data (published)"}
            if max_pages and False:
                break
        return
    idre = re.compile(work["id_re"]) if work.get("id_re") else None
    for k, (path, title, text) in enumerate(crawl_st(work, max_pages), 1):
        extra = {"order": k, "url": ST + path.split("#")[0], "via": "web.archive.org snapshot"}
        if idre and (m := idre.search(path)):
            extra.update({g: int(v) for g, v in m.groupdict().items()})
        yield path.rsplit("/", 1)[-1].replace(".htm", ""), title, text, extra


def manifest_upsert(work: dict, **fields):
    import psycopg2
    cols = {"tradition": work["tradition"], "vector": work["vector"], "work": work["work"],
            "translator": work["translator"], "year": work["year"], "license": work["license"],
            "source_url": (ST + work["index"]) if work["kind"] in ("st", "maha_zip") else
            f"{IA}details/{work['index'].split()[0]} (+{len(work['index'].split()) - 1} more volumes)" if work["kind"] == "ia" else
            f"https://github.com/suttacentral/bilara-data/tree/published/{BILARA_SUB}/{work['index']}",
            "expected_units": work["expected"], "unit_kind": work["unit"], **fields}
    keys = list(cols)
    upd = ", ".join(f"{k}=EXCLUDED.{k}" for k in keys if k not in ("tradition", "work", "translator")) + ", updated_at=now()"
    with psycopg2.connect(PG_OPS) as c, c.cursor() as cur:
        cur.execute(f"INSERT INTO scripture_manifest ({', '.join(keys)}) VALUES ({', '.join(['%s'] * len(keys))}) "
                    f"ON CONFLICT (tradition, work, translator) DO UPDATE SET {upd}", [cols[k] for k in keys])


def memory_count(work: dict) -> int:
    import psycopg2
    with psycopg2.connect(PG_MEM) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM memories WHERE source=%s AND metadata->>'uid' LIKE %s",
                    (work["vector"], work["key"] + ":%"))
        return cur.fetchone()[0]


def run_work(work: dict, dry_run: bool = False, max_pages: int = 0) -> dict:
    import nova_ingest as ni
    have = set() if dry_run else existing_uids(work["vector"])
    if not dry_run:
        manifest_upsert(work, status="running")
    done_hashes, n_units, sent, skipped = set(), 0, 0, 0
    for uid_base, section, text, extra in units(work, max_pages):
        n_units += 1
        for uid, t, meta in build_items(work, uid_base, section, text, extra):
            if uid in have:
                skipped += 1
                continue
            if dry_run:
                sent += 1
                continue
            if sent % 50 == 0:
                while queue_depth() > QUEUE_HIGH:
                    time.sleep(30)
            if ni.remember(t, work["vector"], meta, done_hashes):
                sent += 1
            time.sleep(SEND_DELAY)
        if n_units % 100 == 0:
            log(f"  [{work['key']}] {n_units} units, {sent} sent, {skipped} already present")
    res = {"units": n_units, "sent": sent, "skipped": skipped}
    log(f"[{work['key']}] done: {res}")
    if not dry_run:
        status = "done" if n_units else "failed"
        manifest_upsert(work, status=status, memory_count=memory_count(work),
                        note=f"units={n_units} sent={sent} skipped={skipped} (counts lag the async write queue)")
    return res


def main(argv=None) -> int:
    global SEND_DELAY
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("--tradition", action="append", default=[])
    r.add_argument("--work", action="append", default=[])
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--max-pages", type=int, default=0)
    r.add_argument("--delay", type=float, default=SEND_DELAY, help="seconds between memory writes")
    p = sub.add_parser("probe")
    p.add_argument("key")
    p.add_argument("--max-pages", type=int, default=0)
    a = ap.parse_args(argv)
    if a.cmd == "list":
        for w in WORKS:
            print(f"{w['key']:18} {w['vector']:15} {w['work'][:60]:60} {w['translator']} {w['year']}")
        return 0
    if a.cmd == "probe":
        w = BY_KEY[a.key]
        for uid_base, section, text, extra in units(w, a.max_pages):
            print(f"{uid_base:14} {len(text):7}  {section[:90]}")
        return 0
    SEND_DELAY = a.delay
    todo = [BY_KEY[k] for k in a.work] if a.work else [w for w in WORKS if (not a.tradition or w["tradition"].lower() in [t.lower() for t in a.tradition])
]
    for w in todo:
        log(f"== {w['key']} -> {w['vector']}")
        try:
            run_work(w, a.dry_run, a.max_pages)
        except Exception as e:  # noqa: BLE001 — one bad work must not stop the corpus; recorded in the manifest
            log(f"[{w['key']}] FAILED: {e!r}")
            if not a.dry_run:
                manifest_upsert(w, status="failed", note=repr(e)[:500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
