#!/opt/homebrew/bin/python3
"""nova_ingest_space_force_official.py — official U.S. Space Force doctrine and publications into Nova memory,
topping up the Internet Archive run (nova_ingest_service_manuals.py --service space_force) toward Jordan's
50K Space Force cap (2026-10-08).

What it takes (all U.S. government works approved for public release):
  * doctrine: Space Capstone Publication "Spacepower", Space Force Doctrine Document 1, the Space Doctrine
    Publications (SDP 1-0 ... 6-0, 3-100 ... 3-104) and the doctrine fact sheets (spaceforce.mil, STARCOM);
  * Space Force-specific departmental publications on AF e-Publishing (SPFI / SPFMAN / SPFH / SPFGM / SPFPD,
    SSC / SpOC / STARCOM / legacy AFSPC instructions) — DAF/AF-wide publications belong to the Air Force run;
  * official strategy and leadership guidance: CSO Planning Guidance, The Guardian Ideal, Space Warfighting
    framework, Competing in Space, Future Operating Environment 2040, Objective Force, C-Notes, B-Lines,
    STARCOM vision and unit fact sheets.
Not taken: biographies, forms, templates, personnel/transfer announcements, memos, training slides and
executive summaries (duplicates of the full publication).

spaceforce.mil, starcom.spaceforce.mil and e-publishing.af.mil sit behind an Akamai bot block, so the PDFs come
from the Wayback Machine's byte-exact captures (id_ URLs, listed by the CDX index). Any document whose front
matter carries FOUO / CUI / NOFORN / distribution statement B-F / restricted releasability is skipped.

Publication numbers already ingested are skipped (nova_ingest_service_manuals.pub_key). Each document is
recorded in nova_ops.ia_ingest_seen (identifier = the document's official URL, service = 'space_force'); the run
stops when the Space Force total there reaches --target. Source: military_doctrine_space_force.

Usage: nova_ingest_space_force_official.py [--target 50000] [--dry-run] [--list]
Written by Jordan Koch (via Claude).
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_ingest as ni  # noqa: E402
import nova_ingest_army_manuals as army  # noqa: E402  (get / _connect with retry)
import nova_ingest_service_manuals as svcm  # noqa: E402  (pub_key)

SERVICE, SOURCE = "space_force", "military_doctrine_space_force"
LABEL = "U.S. Space Force publication"
PDFTOTEXT = "/opt/homebrew/bin/pdftotext"
PREFIXES = ["www.spaceforce.mil/Portals/", "starcom.spaceforce.mil/Portals/"] + [
    f"static.e-publishing.af.mil/production/1/{org}/" for org in
    ("ussf", "ussf_coo", "ussf_csro", "ussf_cso", "hqsf", "spoc", "ssc", "starcom")]
CDX = ("https://web.archive.org/cdx/search/cdx?url={prefix}&matchType=prefix&output=json"
       "&filter=mimetype:application/pdf&filter=statuscode:200&collapse=urlkey&fl=original,timestamp&limit=20000")
EPUB_HOST = "static.e-publishing.af.mil"
# e-Publishing file stems that are Space Force-specific (DAFI / AFI / DAFGM / HAFMD are the Air Force run's).
EPUB_SERIES = re.compile(r"^(spf[a-z]*|ussf[a-z]*|hqsf[a-z]*|spoc[a-z]*|ssc[a-z]*|starcom[a-z]*|afspc[a-z]*)\d",
                         re.I)
# spaceforce.mil / STARCOM titles worth keeping, best first (index = rank).
WANTED = [
    re.compile(r"\bSDP\s*\d|space force doctrine docu|space capstone|doctrine (fact sheet|hierarchy)", re.I),
    None,   # rank 1 = e-Publishing instructions / manuals
    re.compile(r"planning guidance|guardian ideal|space warfighting|competing in space|future operating environment|"
               r"\bOFD\b|objective force|competitive endurance|commercial space strategy|partnership strategy|"
               r"data and ai|ussf 101|test vision|satcom vision|otti vision|digital service|case for change|"
               r"gpc key decisions|one team one fight|acquisition tenets|chronology|strategic vision|mission booklet|"
               r"nsttc vision|force development framework|career path narrative|comprehensive strategy", re.I),
    re.compile(r"\bC[\s_-]?Note\b|\bB[\s_-]?Line\b", re.I),
    re.compile(r"fact sheet", re.I),
]
UNWANTED = re.compile(r"executive summary|slides|pdf safe|\bbio\b|biography|\bform\b|template|endorsement|myvector|"
                      r"transfer|\bFAQs?\b|poster|tri-fold|announcement|checklist|\bmemo\b|transcript", re.I)
# Front-matter markings of anything not approved for public release.
RESTRICTED = re.compile(r"for official use only|\bFOUO\b|controlled unclassified|^\s*CUI\s*$|\bCUI//|\bNOFORN\b|"
                        r"distribution statement [B-F]\b|distribution (is )?(authorized|limited|restricted) to|"
                        r"not (approved )?for public release|access to this publication is restricted", re.I | re.M)
FRONT_CHARS = 15000
PDF_MAX_BYTES = 150 * 1024 * 1024


def title_of(url: str) -> str:
    """Official URL -> readable title. '.../spfi36-2903/spfi36-2903.pdf' -> 'SPFI 36-2903';
    '.../SDP%203-0%20Operations%20(19%20July%202023)_1.pdf?ver=x' -> 'SDP 3-0 Operations (19 July 2023)'. Pure."""
    p = urllib.parse.urlsplit(url)
    name = re.sub(r"\.pdf$", "", urllib.parse.unquote(p.path.rsplit("/", 1)[-1]), flags=re.I)
    if p.netloc.lower() == EPUB_HOST:
        m = re.match(r"([a-z]+)(\d.*)$", name, re.I)
        return f"{m.group(1).upper()} {m.group(2).upper()}" if m else name.upper()
    name = re.sub(r"(_\d)+$", "", name)
    return re.sub(r"\s+", " ", name.replace("_", " ")).strip()


def canonical(url: str) -> str:
    """The official URL (https, www for spaceforce.mil hosts, no query, no doubled slashes): the identifier. Pure."""
    p = urllib.parse.urlsplit(url)
    host = p.netloc.lower().split(":")[0]
    if host.endswith("spaceforce.mil") and not host.startswith("www."):
        host = "www." + host
    return f"https://{host}{re.sub(r'/{2,}', '/', p.path)}"


def rank(url: str, title: str):
    """0 doctrine, 1 Space Force instructions/manuals, 2 strategy/vision, 3 CSO/CMSSF notes, 4 fact sheets;
    None when the document is not wanted. Pure."""
    if urllib.parse.urlsplit(url).netloc.lower() == EPUB_HOST:
        stem = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        return 1 if EPUB_SERIES.match(stem) else None
    if UNWANTED.search(title):
        return None
    for i, rx in enumerate(WANTED):
        if rx is not None and rx.search(title):
            return i
    return None


def key_of(title: str) -> str:
    """Publication key: shared pub_key on the title without 'final'/'signed' markers; the capstone's two file
    names fold together; C-Notes / B-Lines key on their subject words (their numbers restart under a new
    CSO and their file names carry dates in several spellings). Pure."""
    if re.search(r"space capstone", title, re.I):
        return "SPACE CAPSTONE PUBLICATION"
    note = re.match(r"\s*(?:the\s+)?([CB])[\s_-]?(note|line)\b", title, re.I)
    if note:
        words = [w for w in re.findall(r"[a-z]{2,}", title.lower()[note.end():]) if w not in _MONTHS | {"final"}]
        return f"{note.group(1).upper()}-{note.group(2).upper()} " + " ".join(words)
    return svcm.pub_key(re.sub(r"\b(final|signed)\b", " ", title, flags=re.I).strip(" -_"))


_MONTHS = {"jan", "feb", "mar", "apr", "may", "jun", "june", "jul", "july", "aug", "sep", "sept", "oct", "nov", "dec"}


def candidates(rows: list, seen_keys: set, seen_ids: set) -> list:
    """CDX rows [(original, timestamp)] -> [(original, title, timestamp)], one per publication (latest capture),
    wanted only, not already ingested, doctrine first. Pure."""
    best = {}
    for original, ts in rows:
        title = title_of(original)
        r = rank(original, title)
        if r is None or canonical(original) in seen_ids or canonical(original).lower() in seen_ids:
            continue
        key = key_of(title)
        if key in seen_keys:
            continue
        if key not in best or ts > best[key][3]:
            best[key] = (r, original, title, ts)
    return [(v[1], v[2], v[3]) for v in sorted(best.values(), key=lambda v: (v[0], v[2].lower()))]


def public_release_ok(text: str) -> bool:
    """False when empty or the front matter carries any restricted-distribution marking. Pure."""
    return bool(text.strip()) and not RESTRICTED.search(text[:FRONT_CHARS])


def wayback_url(original: str, ts: str) -> str:
    return f"https://web.archive.org/web/{ts}id_/{original}"


def pdf_text(data: bytes) -> str:
    """PDF bytes -> text via pdftotext; '' on failure (fails open: one bad PDF must not stop the run)."""
    if not data.startswith(b"%PDF") or len(data) > PDF_MAX_BYTES:
        return ""
    with tempfile.TemporaryDirectory() as d:
        src, dst = Path(d) / "doc.pdf", Path(d) / "doc.txt"
        src.write_bytes(data)
        try:
            subprocess.run([PDFTOTEXT, "-q", "-enc", "UTF-8", str(src), str(dst)], check=True, timeout=300,
                           capture_output=True)
            return dst.read_text("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            ni.log(f"pdftotext failed: {e}", "WARN")
            return ""


def list_publications(prefixes=PREFIXES, tries=3) -> list:
    """All PDF captures under the official prefixes. army.get retries transport errors; a CDX 'temporarily
    offline' HTML page (HTTP 200) is retried here, and a prefix that never answers is skipped."""
    rows = []
    for prefix in prefixes:
        for attempt in range(tries):
            try:
                data = json.loads(army.get(CDX.format(prefix=prefix), timeout=300))
                rows += [tuple(r[:2]) for r in data[1:]]
                break
            except Exception as e:  # noqa: BLE001
                ni.log(f"CDX {prefix}: {e}; {'retry' if attempt < tries - 1 else 'skipped'}", "WARN")
                time.sleep(10 * (attempt + 1))
        time.sleep(1)   # politeness to web.archive.org
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--target", type=int, default=50000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="print the publications that would be ingested and stop")
    a = ap.parse_args(argv)

    oc = army._connect(); oc.autocommit = True; cur = oc.cursor()
    cur.execute("SELECT identifier, title, coalesce(chunks, 0) FROM ia_ingest_seen WHERE service = %s", (SERVICE,))
    rows = cur.fetchall()
    seen_ids = {r[0] for r in rows} | {r[0].lower() for r in rows}
    seen_keys = {key_of(r[1] or "") for r in rows}
    stored = sum(r[2] for r in rows)

    pubs = candidates(list_publications(), seen_keys, seen_ids)
    if a.list:
        for _u, t, ts in pubs:
            print(f"{ts[:8]}  {t[:110]}")
        print(f"{len(pubs)} official Space Force publications; {stored:,} memories already stored")
        return 0
    if stored >= a.target:
        ni.log(f"space_force already at {stored:,} >= {a.target:,}; nothing to do")
        return 0
    ni.notify(f":rocket: *{LABEL}s (spaceforce.mil / STARCOM / e-Publishing) ingest* — {len(pubs)} publications "
              f"-> `{SOURCE}`, target {a.target:,} ({stored:,} already stored).")
    done_hashes, last = set(), time.time()
    for i, (original, title, ts) in enumerate(pubs, 1):
        if stored >= a.target or ni._shutdown:
            break
        url = canonical(original)
        try:
            text = pdf_text(army.get(wayback_url(original, ts), timeout=300))
        except Exception as e:  # noqa: BLE001 — one bad item must not stop the run
            ni.log(f"{url}: fetch failed: {e}")
            time.sleep(1)
            continue
        n = 0
        if not public_release_ok(text):
            ni.log(f"{title}: skipped (empty or not marked for public release)")
        else:
            meta = {"url": url, "type": "document", "site": urllib.parse.urlsplit(url).netloc, "topic": LABEL,
                    "service": SERVICE, "title": title, "date": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}",
                    "via": "web.archive.org"}
            for c in ni.chunk_prose(ni.clean_text(text)):
                if stored + n >= a.target:
                    break
                if not ni.is_garbage(c) and ni.remember(f"[{title}] {c}", SOURCE, meta, done_hashes, a.dry_run):
                    n += 1
        stored += n
        if not a.dry_run:
            cur.execute("INSERT INTO ia_ingest_seen (identifier, title, chunks, service) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (identifier) DO UPDATE SET chunks = excluded.chunks, service = excluded.service, "
                        "at = now()", (url, title, n, SERVICE))
        ni.log(f"[space_force official {i}/{len(pubs)}] {title[:80]}: {n} chunks (total {stored:,})")
        if time.time() - last >= 600:
            ni.notify(f":rocket: Space Force official: {i}/{len(pubs)} — latest *{title[:90]}* — {stored:,} memories")
            last = time.time()
        time.sleep(1.5)   # politeness to web.archive.org
    ni.notify(f":white_check_mark: *{LABEL}s ingest stopped* — {stored:,} memories in `{SOURCE}`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
