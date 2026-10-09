#!/opt/homebrew/bin/python3
"""nova_ingest_national_guard_official.py — official public-release National Guard publications into Nova memory
(Jordan 2026-10-08: National Guard manuals, 50K memory cap, after the Internet Archive run found only ~42).

Sources: the National Guard Bureau publications office (ngbpmc.ng.mil: NGRs, CNGB instructions/manuals/notices,
NGB pamphlets, policy memos, DTMs) and the Air National Guard section of Air Force
e-Publishing (static.e-publishing.af.mil/production/1/ang/). Both sit behind an Akamai edge that returns 403 to
any automated client, so the PDFs are fetched from their Internet Archive Wayback captures (the exact bytes the
public site served). Forms, posters and publication bulletins (lists of
new/rescinded publications) are skipped.

Only documents approved for public release are kept: a PDF whose first pages carry a FOUO / CUI banner, a
DoD distribution statement B-F, or a releasability restriction is skipped (and recorded with 0 chunks so it is
not retried). Publication numbers already ingested for the Guard (nova_ingest_service_manuals.pub_key over
ia_ingest_seen titles) are skipped; the newest capture of each publication wins. Each document is recorded in
nova_ops.ia_ingest_seen (identifier = document URL, service = 'national_guard'); the run stops when the Guard
total reaches --target. Memories use source military_doctrine_national_guard.

Usage: nova_ingest_national_guard_official.py [--target 50000] [--dry-run] [--list]
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
import nova_ingest_service_manuals as svcman  # noqa: E402  (pub_key)

SERVICE = "national_guard"
SOURCE = "military_doctrine_national_guard"
LABEL = "U.S. National Guard publication"
PDFTOTEXT = "/opt/homebrew/bin/pdftotext"
CDX = "https://web.archive.org/cdx/search/cdx"
PREFIXES = {   # url prefix -> site label
    "ngbpmc.ng.mil/Portals/27/Publications/": "ngbpmc.ng.mil",
    "static.e-publishing.af.mil/production/1/ang/publication/": "e-publishing.af.mil (ANG)",
}
SKIP_PATH = re.compile(r"/forms?/|/bulletins/|poster|\.(?!pdf$)[a-z0-9]+$", re.I)
PAUSE = 1.5            # seconds between remote requests
MAX_PDF_BYTES = 60_000_000

# Restrictive markings, checked on the first pages only (regulations about CUI mention it in the body).
RESTRICTED = re.compile(
    r"^\s*(?:\(?U//)?(?:FOUO|CUI|FOR OFFICIAL USE ONLY|CONTROLLED UNCLASSIFIED INFORMATION|"
    r"CONTROLLED BY:.*|LIMITED DISTRIBUTION|NOT RELEASABLE.*|NOFORN|SECRET|CONFIDENTIAL)\)?\s*$"
    r"|distribution\s+statement\s*:?\s*[B-F]\b"
    r"|distribution\s+(?:is\s+)?(?:authorized|limited)\s+to\b"
    r"|not\s+(?:approved|releasable)\s+for\s+public\s+release"
    r"|access\s+to\s+this\s+publication\s+is\s+restricted"
    r"|releasability\s+restrictions?\s+(?:apply|exist)",
    re.I | re.M)


def is_public(head: str) -> bool:
    """True unless the first pages carry a restrictive marking. Pure."""
    return not RESTRICTED.search(head or "")


def doc_title(url: str) -> str:
    """Readable publication title from a PDF URL: 'NGR%20350-1_20210623.pdf' -> 'NGR 350-1'. Pure."""
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    name = re.sub(r"\.pdf$", "", name, flags=re.I)
    name = re.sub(r"[_\s-]+(?:v\d+[_.]\d+|\d{8}|\d{4}-\d{2}-\d{2})(?=$|[_\s])", "", name, flags=re.I)  # dates/versions
    name = re.sub(r"^U_", "", name)                                    # 'U_' = unclassified file prefix
    name = re.sub(r"(?<=\d)_(?=\d)", ".", name)                        # CNGBI_2000_01C -> CNGBI_2000.01C
    name = re.sub(r"^([A-Za-z]{2,8})(?=\d)", r"\1 ", name)             # afi10-206 -> afi 10-206, pb19 -> pb 19
    name = re.sub(r"[_\s]+", " ", name).strip()
    return name.upper() if re.match(r"^[A-Za-z]{2,8} \d", name) else name


def file_date(url: str, ts: str) -> str:
    """Best-known date of a document: the YYYYMMDD in its filename, else the capture timestamp. Pure."""
    m = re.search(r"(?<!\d)((?:19|20)\d{6})(?!\d)", urllib.parse.unquote(url))
    return (m.group(1) if m else ts[:8])


def _rank(url: str, ts: str) -> tuple:
    """Version preference: a dated filename beats an undated one (a capture date is only an upper bound), then
    the newer date. Pure."""
    return (bool(re.search(r"(?<!\d)(?:19|20)\d{6}(?!\d)", urllib.parse.unquote(url))), file_date(url, ts), ts)


def candidates(cdx_rows: list, seen_ids: set, seen_keys: set) -> list:
    """CDX rows [timestamp, original] -> [(url, ts, title, site)] newest publication first, one per pub_key,
    skipping forms/posters, already-recorded URLs and already-ingested publication numbers. Pure."""
    best = {}
    for ts, orig in cdx_rows:
        url = re.sub(r"^http://", "https://", orig.split("?", 1)[0])
        site = next((s for p, s in PREFIXES.items() if p.lower() in url.lower()), None)
        if not site or SKIP_PATH.search(url) or url in seen_ids:
            continue
        title = doc_title(url)
        key = svcman.pub_key(title)
        if not key or key in seen_keys:
            continue
        cur = best.get(key)
        if cur is None or _rank(url, ts) > _rank(cur[0], cur[1]):
            best[key] = (url, ts, title, site)
    return sorted(best.values(), key=lambda c: (file_date(c[0], c[1]), c[1]), reverse=True)


def list_archived() -> list:
    """-> [[timestamp, original]] of every archived 200/PDF capture under the official prefixes."""
    rows = []
    for prefix in PREFIXES:
        qs = urllib.parse.urlencode([("url", prefix + "*"), ("filter", "mimetype:application/pdf"),
                                     ("filter", "statuscode:200"), ("collapse", "urlkey"),
                                     ("fl", "timestamp,original"), ("output", "json"), ("limit", "20000")])
        data = json.loads(army.get(f"{CDX}?{qs}", timeout=120) or b"[]")
        rows += [r[:2] for r in data[1:]]    # first row is the header
        time.sleep(PAUSE)
    return rows


def wayback_url(url: str, ts: str) -> str:
    return f"https://web.archive.org/web/{ts}id_/{url}"


def pdf_text(data: bytes, first_pages: int = 0) -> str:
    """Text of a PDF via pdftotext (layout-free reading order); '' on failure. first_pages limits the range."""
    if not data.startswith(b"%PDF"):
        return ""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data); f.flush()
        cmd = [PDFTOTEXT, "-q", "-enc", "UTF-8"] + (["-l", str(first_pages)] if first_pages else []) + [f.name, "-"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=180)
        except (subprocess.TimeoutExpired, OSError) as e:
            ni.log(f"pdftotext failed: {e}", "WARN")
            return ""
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


def ingest_doc(url, ts, title, site, budget, done_hashes, dry_run):
    """Fetch, check releasability, chunk and store one document. -> (chunks stored, status)."""
    data = army.get(wayback_url(url, ts), timeout=180)
    if len(data) > MAX_PDF_BYTES:
        return 0, "too large"
    if not is_public(pdf_text(data, first_pages=2)):
        return 0, "restricted marking, skipped"
    text = pdf_text(data)
    if len(text.strip()) < 500:
        return 0, "no text layer"
    meta = {"url": url, "archived": wayback_url(url, ts), "type": "document", "site": site, "topic": LABEL,
            "service": SERVICE, "title": title, "date": file_date(url, ts), "release": "public"}
    n = 0
    for c in ni.chunk_prose(ni.clean_text(text)):
        if n >= budget:
            break
        if not ni.is_garbage(c) and ni.remember(f"[{title}] {c}", SOURCE, meta, done_hashes, dry_run):
            n += 1
    return n, "ok"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--target", type=int, default=50000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="print the publications that would be ingested and stop")
    a = ap.parse_args(argv)

    oc = army._connect(); oc.autocommit = True; cur = oc.cursor()
    cur.execute("SELECT identifier, title, coalesce(chunks, 0), coalesce(service, 'army') FROM ia_ingest_seen")
    rows = cur.fetchall()
    seen_ids = {r[0] for r in rows}
    seen_keys = {svcman.pub_key(r[1]) for r in rows if r[3] == SERVICE}
    stored = sum(r[2] for r in rows if r[3] == SERVICE)

    docs = candidates(list_archived(), seen_ids, seen_keys)
    if a.list:
        for url, ts, title, site in docs:
            print(f"{file_date(url, ts)}  {site:26} {title[:90]}")
        print(f"{len(docs)} official National Guard publications; {stored:,} Guard memories already stored")
        return 0
    say = (lambda _m: None) if a.dry_run else ni.notify   # a dry run posts nothing
    say(f":shield: *National Guard official publications* — {len(docs)} public-release documents "
              f"(NGB + ANG) -> `{SOURCE}`, target {a.target:,} ({stored:,} already stored).")
    done_hashes, last = set(), time.time()
    for i, (url, ts, title, site) in enumerate(docs, 1):
        if stored >= a.target or ni._shutdown:
            break
        try:
            n, status = ingest_doc(url, ts, title, site, a.target - stored, done_hashes, a.dry_run)
        except Exception as e:  # noqa: BLE001 — one bad document must not stop the run
            ni.log(f"{url}: fetch failed: {e}")
            time.sleep(PAUSE)
            continue
        stored += n
        if not a.dry_run:
            cur.execute("INSERT INTO ia_ingest_seen (identifier, title, chunks, service) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (identifier) DO UPDATE SET chunks = excluded.chunks, service = excluded.service, "
                        "at = now()", (url, title, n, SERVICE))
        ni.log(f"[ng-official {i}/{len(docs)}] {title[:80]}: {n} chunks, {status} (total {stored:,})")
        if time.time() - last >= 600:
            say(f":shield: National Guard official: {i}/{len(docs)} — latest *{title[:90]}* — {stored:,} memories")
            last = time.time()
        time.sleep(PAUSE)
    say(f":white_check_mark: *National Guard official publications stopped* — {stored:,} memories in `{SOURCE}`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
