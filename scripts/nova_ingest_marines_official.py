#!/opt/homebrew/bin/python3
"""nova_ingest_marines_official.py — official U.S. Marine Corps doctrinal publications (MCDP / MCWP / MCRP /
MCTP / MCIP / FMFM / FMFRP / NAVMC) from marines.mil into Nova memory, topping up the Internet Archive run
(nova_ingest_service_manuals.py --service marines) toward Jordan's 50K Marine Corps cap (2026-10-08).

marines.mil sits behind an Akamai bot block, so the PDFs come from the Wayback Machine's byte-exact
captures of marines.mil/Portals/1/Publications/ (id_ URLs; the CDX index lists them). Only U.S. government
works approved for public release: any document whose front matter carries FOUO / CUI / NOFORN /
distribution statement B-F is skipped, and the edit-locked "(SECURED)" copy of a publication is used only
when no unlocked copy exists.

Publication numbers already ingested by the Internet Archive run are skipped (same pub_key). Each document
is recorded in nova_ops.ia_ingest_seen (identifier = marines.mil URL, service = 'marines'); the run stops
when the Marine Corps total there reaches --target. Source: military_doctrine_marines.

Usage: nova_ingest_marines_official.py [--target 50000] [--dry-run] [--list]
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
import nova_ingest_service_manuals as svcm  # noqa: E402  (pub_key / keep / LOW_VALUE)

SERVICE, SOURCE = "marines", "military_doctrine_marines"
LABEL = "U.S. Marine Corps doctrinal publication"
PDFTOTEXT = "/opt/homebrew/bin/pdftotext"
CDX = ("https://web.archive.org/cdx/search/cdx?url=marines.mil/Portals/1/Publications/&matchType=prefix"
       "&output=json&filter=mimetype:application/pdf&filter=statuscode:200&collapse=urlkey"
       "&fl=original,timestamp&limit=20000")
DOCTRINE = re.compile(r"\b(MCDP|MCWP|MCRP|MCTP|MCIP|FMFM|FMFRP|NAVMC)[\s_-]*\d", re.I)
# Front-matter markings of anything not approved for public release.
RESTRICTED = re.compile(r"for official use only|\bFOUO\b|controlled unclassified|\bCUI\b|\bNOFORN\b|"
                        r"distribution statement [B-F]\b|distribution (is )?(authorized|limited|restricted) to|"
                        r"not (approved )?for public release", re.I)
FRONT_CHARS = 15000
PDF_MAX_BYTES = 150 * 1024 * 1024


def title_of(url: str) -> str:
    """'.../Publications/MCWP%203-11.2%20Marine%20Rifle%20Squad.pdf?ver=x' -> 'MCWP 3-11.2 Marine Rifle Squad'. Pure."""
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    name = re.sub(r"\.pdf$", "", name, flags=re.I)
    return re.sub(r"\s+", " ", name.replace("_", " ")).strip()


def canonical(url: str) -> str:
    """The marines.mil URL without query string, https + www, used as the ia_ingest_seen identifier. Pure."""
    p = urllib.parse.urlsplit(url)
    return f"https://www.marines.mil{p.path}"


def secured(title: str) -> bool:
    return bool(re.search(r"\bsecured\b", title, re.I))


SERIES_ORDER = ["MCDP", "MCWP", "MCTP", "MCRP", "MCIP", "FMFM", "FMFRP", "NAVMC"]
NUM = re.compile(r"\b(MCDP|MCWP|MCRP|MCTP|MCIP|FMFM|FMFRP|NAVMC)[\s_-]*(\d+[A-Z]?(?:[\s_.-]+\d+[A-Z]?)*)", re.I)


def norm_key(title: str) -> str:
    """Spacing-proof publication key: 'MCWP 3 11.2' and 'MCWP 3-11.2' -> 'MCWP 3-11-2'. NAVMC revision letters
    are dropped ('NAVMC 3500.78C' -> 'NAVMC 3500-78'), since there they mark editions of one manual. Pure."""
    m = NUM.search(title or "")
    if not m:
        return svcm.pub_key(title)
    series, parts = m.group(1).upper(), re.findall(r"\d+[A-Z]?", m.group(2).upper())
    if series == "NAVMC":
        parts = [re.sub(r"[A-Z]$", "", p) for p in parts]
    return f"{series} {'-'.join(parts)}"


def keys_of(title: str) -> set:
    return {svcm.pub_key(title), norm_key(title)}


def candidates(rows: list, seen_keys: set, seen_ids: set) -> list:
    """CDX rows [(original, timestamp)] -> [(url, title, timestamp)], one per publication, doctrinal series
    only, not low-value, not already ingested. Prefers an unlocked copy, then a non gender-neutral duplicate,
    then the latest capture. Ordered capstone doctrine first, NAVMC training manuals last. Pure."""
    best = {}
    for original, ts in rows:
        title = title_of(original)
        if not DOCTRINE.search(title) or svcm.LOW_VALUE.search(title) or not svcm.keep(SERVICE, title):
            continue
        url = canonical(original)
        if url in seen_ids or keys_of(title) & seen_keys:
            continue
        key = norm_key(title)
        rank = (not secured(title), not re.search(r"\bGN\b|gender neutral", title, re.I), ts)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, original, title, ts)
    out = [(v[1], re.sub(r"\s*\((SECURED)\)|\s+SECURED\b", "", v[2], flags=re.I), v[3]) for v in best.values()]

    def order(x):
        s = NUM.search(x[1]).group(1).upper()
        return (SERIES_ORDER.index(s), norm_key(x[1]))
    return sorted(out, key=order)


def public_release_ok(text: str) -> bool:
    """False when the front matter carries any restricted-distribution marking. Pure."""
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


def list_publications() -> list:
    return [tuple(r[:2]) for r in json.loads(army.get(CDX, timeout=300))[1:]]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--target", type=int, default=50000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="print the publications that would be ingested and stop")
    a = ap.parse_args(argv)

    oc = army._connect(); oc.autocommit = True; cur = oc.cursor()
    cur.execute("SELECT identifier, title, coalesce(chunks, 0) FROM ia_ingest_seen WHERE service = %s", (SERVICE,))
    rows = cur.fetchall()
    seen_ids = {r[0] for r in rows}
    seen_keys = set().union(*(keys_of(r[1]) for r in rows)) if rows else set()
    stored = sum(r[2] for r in rows)

    pubs = candidates(list_publications(), seen_keys, seen_ids)
    if a.list:
        for _u, t, ts in pubs:
            print(f"{ts[:8]}  {t[:110]}")
        print(f"{len(pubs)} official Marine Corps publications; {stored:,} memories already stored")
        return 0
    if stored >= a.target:
        ni.log(f"marines already at {stored:,} >= {a.target:,}; nothing to do")
        return 0
    ni.notify(f":anchor: *{LABEL}s (marines.mil) ingest* — {len(pubs)} publications -> `{SOURCE}`, "
              f"target {a.target:,} ({stored:,} already stored).")
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
            meta = {"url": url, "type": "document", "site": "marines.mil", "topic": LABEL, "service": SERVICE,
                    "title": title, "date": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}", "via": "web.archive.org"}
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
        ni.log(f"[marines.mil {i}/{len(pubs)}] {title[:80]}: {n} chunks (total {stored:,})")
        if time.time() - last >= 600:
            ni.notify(f":anchor: marines.mil: {i}/{len(pubs)} — latest *{title[:90]}* — {stored:,} memories")
            last = time.time()
        time.sleep(1.5)   # politeness to web.archive.org
    ni.notify(f":white_check_mark: *{LABEL}s (marines.mil) ingest stopped* — {stored:,} memories in `{SOURCE}`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
