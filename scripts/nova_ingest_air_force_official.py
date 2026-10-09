#!/opt/homebrew/bin/python3
"""nova_ingest_air_force_official.py — official public-release U.S. Air Force publications (doctrine, AFTTP,
AFMAN, AFH, AFPAM, AFPD, AFI) into Nova memory, after the Internet Archive run of
nova_ingest_service_manuals.py --service air_force (Jordan 2026-10-08: 50K memory cap per service).

Sources: the Air Force's own public sites — static.e-publishing.af.mil (departmental publications),
www.e-publishing.af.mil/shared/media/epubs (older departmental copies) and www.doctrine.af.mil (LeMay Center
AFDP / doctrine annexes). Those sites sit behind an Akamai rule that answers 403 to this host, so each PDF is
fetched from its Internet Archive Wayback capture (raw `id_` bytes, latest HTTP-200 capture), enumerated with
the Wayback CDX API. The document URL recorded is the official one.

Rules: only documents approved for public release (anything marked FOUO / CUI / not releasable / access
restricted / distribution B-F is skipped); departmental publications only (no local supplements); doctrine
and training/operations manuals first, administrative instructions last; one copy per publication number
(pub_key from nova_ingest_service_manuals, DAF* folded onto AF*); progress in nova_ops.ia_ingest_seen with
service='air_force'; source military_doctrine_air_force; stops when the Air Force total reaches --target.

Usage: nova_ingest_air_force_official.py [--target 50000] [--dry-run] [--list] [--limit N]
Written by Jordan Koch (via Claude).
"""
import argparse
import os
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
from nova_ingest_service_manuals import pub_key  # noqa: E402

SERVICE, SOURCE = "air_force", "military_doctrine_air_force"
PDFTOTEXT = "/opt/homebrew/bin/pdftotext"
CDX = "https://web.archive.org/cdx/search/cdx"
PREFIXES = ["www.doctrine.af.mil/Portals/", "static.e-publishing.af.mil/production/1/",
            "www.e-publishing.af.mil/shared/media/epubs/"]
SLEEP = 2.0          # seconds between remote requests (Wayback asks for a gentle rate)
MIN_TEXT = 2000      # extracted text shorter than this is a scan / stub: skip

SERIES = re.compile(r"^(dafman|dafpam|dafpd|dafh|dafi|afjman|afjpam|afdp|afdd|afdn|afttp|afman|afpam|afpd|afh|afi)"
                    r"(\d{1,2}-[0-9a-z.-]+?)(?:v(\d+))?$", re.I)
OPS_SERIES = {"10", "11", "13", "14", "15", "16", "31", "91"}     # operations, flying, space/C2, intel, weather, nuclear, security, safety
DOCTRINE_KEEP = re.compile(r"AFDP|AFDD|AFDN|Annex|Paragon|^du_\d|Doctrine[\s_-]*(Advisory|101|for[\s_-]*Newcomers)|"
                           r"Primer[\s_-]*on[\s_-]*Doctrine|BlueBook|BrownBook", re.I)
DOCTRINE_SKIP = re.compile(r"summary|one.?pager|flyer|invitation|contest|essay|glossary|placemat|kneeboard|cache|"
                           r"wargame|pocket|booklet|smartbook|-D\d+[-_]", re.I)
RESTRICTED = re.compile(r"FOR OFFICIAL USE ONLY|\(FOUO\)|//FOUO|CONTROLLED UNCLASSIFIED INFORMATION|^\s*CUI\s*$|"
                        r"not releasable|releasability restricted|access to this publication is restricted|"
                        r"DISTRIBUTION STATEMENT [B-F]\b|distribution authorized to|NOFORN", re.I | re.M)
RELEASE_LINE = re.compile(r"RELEASABILITY:\s*(.{0,240})", re.I | re.S)
RELEASE_OK = re.compile(r"no releasability restrictions|public release|approved for public", re.I)


# ---------- pure helpers ----------

def release_status(text: str) -> tuple:
    """(ok, reason): True only for documents approved for public release. Pure."""
    head = (text or "")[:10000]
    m = RESTRICTED.search(head)
    if m:
        return False, f"restricted marking: {m.group(0).strip()[:40]}"
    rl = RELEASE_LINE.search(head)
    if rl and not RELEASE_OK.search(rl.group(1)):
        return False, "releasability line is not a public release"
    return True, "public release" if rl else "no restriction marking (public site)"


def title_from_filename(name: str):
    """'afman11-202v3.pdf' -> 'AFMAN 11-202V3'; local units / supplements / non-series -> None. Pure."""
    stem = urllib.parse.unquote(name).rsplit("/", 1)[-1]
    stem = re.sub(r"\.pdf$", "", stem, flags=re.I).strip()
    if "_" in stem or "sup" in stem.lower():
        return None
    m = SERIES.match(stem)
    if not m:
        return None
    vol = f"V{m.group(3)}" if m.group(3) else ""
    return f"{m.group(1).upper()} {m.group(2).upper().rstrip('.-')}{vol}"


def doctrine_title(name: str):
    """doctrine.af.mil file name -> 'AFDP 3-01 Counterair' style title, or None if not a doctrine document. Pure."""
    stem = re.sub(r"\.pdf$", "", urllib.parse.unquote(name).rsplit("/", 1)[-1], flags=re.I).strip()
    if not DOCTRINE_KEEP.search(stem) or DOCTRINE_SKIP.search(stem):
        return None
    words = lambda s: re.sub(r"[-_\s]+", " ", s).strip().title()  # noqa: E731
    m = re.match(r"^(\d+(?:-\d+)*(?:\.\d+)?)-(AFDP|Annex)-?(.*)$", stem, re.I)
    if m:   # '3-01-AFDP-COUNTERAIR' / '3-60-Annex-TARGETING' (annexes were renamed AFDPs in 2021)
        return f"AFDP {m.group(1)} {words(m.group(3))}".strip()
    m = re.match(r"^Annex[\s_-]*(\d+(?:-\d+)*(?:\.\d+)?)[\s_-]*(.*)$", stem, re.I)
    if m:
        return f"AFDP {m.group(1)} {words(m.group(2))}".strip()
    m = re.match(r"^(AFD[PDN])[\s_-]*(\d+(?:-\d+)*(?:\.\d+)?)[\s_-]*(.*)$", stem, re.I)
    if m:
        return f"{m.group(1).upper()} {m.group(2)} {words(m.group(3))}".strip()
    return words(stem)


def dedup_key(title: str) -> str:
    """pub_key with DAF* folded onto AF* (DAFMAN 91-203 supersedes AFMAN 91-203). Pure."""
    return pub_key(re.sub(r"^DAF", "AF", title or ""))


def rank(title: str, source_rank: int) -> tuple:
    """Sort key: doctrine, AFTTP, ops AFMANs, other manuals, handbooks/pamphlets, ops AFIs, policy, other AFIs. Pure."""
    t = title.upper()
    series = t.split(" ", 1)[0]
    num = t.split(" ", 1)[1].split("-", 1)[0] if " " in t else ""
    if series in {"AFDP", "AFDD", "AFDN"} or source_rank == 0:
        r = 0
    elif series == "AFTTP":
        r = 1
    elif series in {"AFMAN", "DAFMAN", "AFJMAN"}:
        r = 2 if num in OPS_SERIES else 3
    elif series in {"AFH", "DAFH", "AFPAM", "DAFPAM", "AFJPAM"}:
        r = 4
    elif series in {"AFI", "DAFI"}:
        r = 5 if num in OPS_SERIES else 7
    else:   # AFPD / DAFPD
        r = 6
    return (r, source_rank, 0 if series.startswith("DAF") else 1, t)


def latest_captures(cdx_text: str) -> dict:
    """CDX rows 'urlkey timestamp original' -> {urlkey: (timestamp, original)} keeping the newest capture. Pure."""
    out = {}
    for line in (cdx_text or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        key, ts, orig = parts[0].split("?")[0], parts[1], parts[2]
        if key not in out or ts > out[key][0]:
            out[key] = (ts, orig.split("?")[0])
    return out


def official_url(orig: str) -> str:
    """Normalise a captured URL to the canonical https official URL. Pure."""
    return re.sub(r"^https?://([^/:]+)(:\d+)?", r"https://\1", orig)


def candidates(captures_by_prefix: dict, seen_ids: set, seen_keys: set) -> list:
    """-> [(official_url, wayback_url, title)] wanted, new, one per publication, best first. Pure."""
    rows = []
    for src_rank, prefix in enumerate(PREFIXES):
        for _k, (ts, orig) in captures_by_prefix.get(prefix, {}).items():
            name = orig.rsplit("/", 1)[-1]
            title = doctrine_title(name) if src_rank == 0 else title_from_filename(name)
            if title:
                url = official_url(orig)
                rows.append((rank(title, src_rank), url, f"https://web.archive.org/web/{ts}id_/{orig}", title))
    rows.sort()
    out, keys = [], set(seen_keys)
    for _r, url, wb, title in rows:
        k = dedup_key(title)
        if url in seen_ids or k in keys:
            continue
        keys.add(k)
        out.append((url, wb, title))
    return out


def subject_line(text: str) -> str:
    """The publication's subject (line after the date on the cover), for a readable title. Pure."""
    lines = [l.strip() for l in (text or "")[:3000].splitlines() if l.strip()]
    for i, l in enumerate(lines[:30]):
        if re.fullmatch(r"\d{1,2}\s+[A-Z]+\s+\d{4}", l) and i + 1 < len(lines):
            s = lines[i + 1]   # cover: date, category ('Tactical Doctrine'), then the TITLE IN CAPS
            nxt = lines[i + 2] if i + 2 < len(lines) else ""
            if nxt.isupper() and 3 < len(nxt) < 120 and not re.search(r"COMPLIANCE|ACCESSIBILITY", nxt):
                s = nxt
            if 3 < len(s) < 120 and not re.search(r"COMPLIANCE|ACCESSIBILITY", s, re.I):
                return s.title()
    return ""


# ---------- I/O ----------

def cdx_captures(prefix: str) -> dict:
    """Wayback CDX for one URL prefix, HTTP-200 PDFs only -> {urlkey: (timestamp, original)}."""
    qs = urllib.parse.urlencode([("url", prefix), ("matchType", "prefix"), ("filter", "statuscode:200"),
                                 ("filter", "mimetype:application/pdf"), ("fl", "urlkey,timestamp,original"),
                                 ("limit", "500000")])
    return latest_captures(army.get(f"{CDX}?{qs}", timeout=300).decode("utf-8", "replace"))


def pdf_text(data: bytes) -> str:
    """PDF bytes -> text with pdftotext (fresh temp dir, timeout). '' when not a PDF or extraction fails."""
    if not data.startswith(b"%PDF"):
        return ""
    with tempfile.TemporaryDirectory(prefix="af_pdf_") as d:
        p = os.path.join(d, "doc.pdf")
        with open(p, "wb") as f:
            f.write(data)
        try:
            r = subprocess.run([PDFTOTEXT, "-q", "-enc", "UTF-8", p, "-"], capture_output=True, timeout=300)
        except (subprocess.TimeoutExpired, OSError) as e:
            ni.log(f"pdftotext failed: {e}", "WARN")
            return ""
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--target", type=int, default=50000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="print the publications that would be ingested and stop")
    ap.add_argument("--limit", type=int, default=0, help="stop after N documents (0 = no limit)")
    a = ap.parse_args(argv)

    oc = army._connect(); oc.autocommit = True; cur = oc.cursor()
    cur.execute("SELECT identifier, title, coalesce(chunks, 0) FROM ia_ingest_seen WHERE service = %s", (SERVICE,))
    rows = cur.fetchall()
    seen_ids = {r[0] for r in rows}
    seen_keys = {dedup_key(r[1]) for r in rows}
    stored = sum(r[2] for r in rows)

    caps = {}
    for p in PREFIXES:
        try:
            caps[p] = cdx_captures(p)
        except Exception as e:  # noqa: BLE001 — one source down must not stop the others
            ni.log(f"CDX {p} failed: {e}", "WARN")
        time.sleep(SLEEP)
    pubs = candidates(caps, seen_ids, seen_keys)
    if a.list:
        for url, _wb, t in pubs:
            print(f"{t[:60]:60}  {url}")
        print(f"{len(pubs)} official air_force publications; {stored:,} memories already stored")
        return 0

    ni.notify(f":airplane: *U.S. Air Force official publications ingest* — {len(pubs)} candidates "
              f"(doctrine.af.mil + e-Publishing via Wayback) -> `{SOURCE}`, target {a.target:,} ({stored:,} stored).")
    done_hashes, last, n_docs = set(), time.time(), 0
    for i, (url, wb, title) in enumerate(pubs, 1):
        if stored >= a.target or ni._shutdown or (a.limit and n_docs >= a.limit):
            break
        try:
            text = pdf_text(army.get(wb, timeout=180))
        except Exception as e:  # noqa: BLE001 — one bad document must not stop the run
            ni.log(f"{url}: fetch failed: {e}", "WARN")
            time.sleep(SLEEP)
            continue
        time.sleep(SLEEP)
        ok, why = release_status(text)
        if len(text) < MIN_TEXT:
            ok, why = False, "no extractable text"
        n = 0
        subj = subject_line(text) if ok else ""
        full = f"{title} {subj}".strip() if subj and subj.lower() not in title.lower() else title
        if ok:
            meta = {"url": url, "archived_copy": wb, "type": "document", "site": urllib.parse.urlparse(url).netloc,
                    "topic": "U.S. Air Force publication", "service": SERVICE, "title": full, "release": why}
            for c in ni.chunk_prose(ni.clean_text(text)):
                if stored + n >= a.target:
                    break
                if not ni.is_garbage(c) and ni.remember(f"[{full}] {c}", SOURCE, meta, done_hashes, a.dry_run):
                    n += 1
            stored += n
            n_docs += 1
        if not a.dry_run:   # skipped documents are recorded with 0 chunks so they are not refetched
            cur.execute("INSERT INTO ia_ingest_seen (identifier, title, chunks, service) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (identifier) DO UPDATE SET chunks = excluded.chunks, service = excluded.service, "
                        "at = now()", (url, full, n, SERVICE))
        ni.log(f"[af-official {i}/{len(pubs)}] {full[:80]}: " + (f"{n} chunks (total {stored:,})" if ok else f"SKIP {why}"))
        if time.time() - last >= 600:
            ni.notify(f":airplane: Air Force official: {i}/{len(pubs)} — latest *{full[:90]}* — {stored:,} memories")
            last = time.time()
    ni.notify(f":white_check_mark: *U.S. Air Force official publications ingest stopped* — {stored:,} memories in `{SOURCE}`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
