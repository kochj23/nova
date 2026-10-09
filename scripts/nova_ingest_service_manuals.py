#!/opt/homebrew/bin/python3
"""nova_ingest_service_manuals.py — public-domain U.S. military manuals for the Navy, Air Force,
Marine Corps, Space Force and National Guard, from the Internet Archive's OCR text into Nova memory
(Jordan 2026-10-08: "research those same sort of manuals for the Navy, Air Force, Marines, Space Force
and National Guard and start ingesting those. 50K memory cap for each service").

Same pipeline as nova_ingest_army_manuals.py (whose fetch/OCR helpers are reused), plus what the Army run
taught: one copy per publication number (the Army run stored 24 duplicate copies, ~9% of its cap), and no
dictionaries, glossaries, indexes or catalogues (thousands of low-value chunks). Each service has its own
source label (military_doctrine_<service>) and its own cap, tracked in nova_ops.ia_ingest_seen.service.

Expect very different yields: Navy and Marines have hundreds of real manuals on the Archive; Air Force,
Space Force and National Guard publish mostly on their own sites, so their runs stop well short of the cap.

Usage: nova_ingest_service_manuals.py --service navy|air_force|marines|space_force|national_guard
       [--target 50000] [--dry-run] [--list]
Written by Jordan Koch (via Claude).
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_ingest as ni  # noqa: E402
import nova_ingest_army_manuals as army  # noqa: E402  (get / ocr_text / _connect with retry)

MAX_PAGES = 40    # 500 rows/page; stops a runaway loop
LOW_VALUE = re.compile(r"dictionar|glossar|\bindex\b|catalog|bibliograph|abbreviation|list of .*films|"
                       r"service manual|anime|fanzine|novel|magazine|leaked|conspiracy|classified|fouo", re.I)

SERVICES = {
    "navy": {
        "label": "U.S. Navy training publication",
        "query": 'mediatype:texts AND (title:(NAVEDTRA) OR title:(NAVPERS) OR title:("rate training manual") OR '
                 'title:("naval warfare publication") OR title:(NWP) OR title:(bluejacket) OR title:(NAVSEA) OR '
                 '(title:(manual) AND (creator:(navy) OR publisher:(navy) OR subject:("united states navy"))))',
        "pub": r"\b(NAVEDTRA|NAVPERS|NWP|NTTP|NAVSEA|NAVAIR|OPNAV(?:INST)?|BUPERS)[\s_-]*\d[\w.-]*|bluejackets?'? manual|"
               r"rate training manual|naval .*manual|navy .*manual",
        "exclude": r"british|royal navy|^\s*brandon|^\s*DTIC\b|debaters|navy medicine|owners' and operators'|"
                   r"laying the keel|performance evaluation report|hospital organization|freedmen|rechecked by",
    },
    "marines": {
        "label": "U.S. Marine Corps doctrinal publication",
        "query": 'mediatype:texts AND (title:(MCDP) OR title:(MCWP) OR title:(MCRP) OR title:(MCTP) OR title:(FMFM) OR '
                 'title:(FMFRP) OR title:(NAVMC) OR title:("marine corps doctrinal") OR '
                 '(title:(manual) AND (creator:("marine corps") OR subject:("marine corps"))))',
        "pub": r"\b(MCDP|MCWP|MCRP|MCTP|FMFM|FMFRP|NAVMC)[\s_-]*\d[\w.-]*|marine corps .*(manual|handbook|doctrin)",
    },
    "air_force": {
        "label": "U.S. Air Force publication",
        "query": 'mediatype:texts AND (title:(AFMAN) OR title:(AFDD) OR title:(AFDP) OR title:(AFTTP) OR title:(AFPAM) OR '
                 'title:("air force manual") OR title:("air force doctrine") OR title:("air force pamphlet") OR '
                 'title:("air force handbook") OR title:("air training command") OR title:("flight manual") OR '
                 '(title:(manual) AND (creator:("air force") OR publisher:("air force") OR subject:("united states air force"))))',
        "pub": r"\b(AFMAN|AFDD|AFDP|AFTTP|AFPAM|AFI|AFH|AFP|AFM|ATC|T\.?\s?O\.?)[\s_-]*\d[\w.-]*|"
               r"air force .*(manual|doctrine|pamphlet|handbook)|"
               r"(\b[ABCEFKMRSTUV]{1,2}-\d+[A-Z]*\b|USAF|NATOPS|air force).*flight manual|flight manual.*(\b[ABCEFKMRSTUV]{1,2}-\d+|USAF|NATOPS)",
        "exclude": r"^\s*(the army|army\b|fm\s*\d|tm\s*\d)|gamepro|strategy guide|nintendo|simulator|fournier",
    },
    "space_force": {
        "label": "U.S. Space Force / space doctrine publication",
        "query": 'mediatype:texts AND (title:("space force") OR title:("space doctrine") OR title:(spacepower) OR '
                 'title:(AFSPC) OR title:("space command") OR title:("space capstone") OR subject:("space force"))',
        "pub": r"(space force|space doctrine|spacepower|space capstone|AFSPC|space command).*"
               r"(doctrine|manual|publication|pamphlet|instruction|handbook|capstone|guide|primer|theory)|"
               r"\bSDP[\s_-]*\d",
        "exclude": r"honneamise|royal space force",
    },
    "national_guard": {
        "label": "U.S. National Guard publication",
        "query": 'mediatype:texts AND (title:("national guard") OR title:(NGR) OR title:("air national guard") OR '
                 'subject:("national guard")) AND (title:(manual) OR title:(regulation) OR title:(pamphlet) OR '
                 'title:(training) OR title:(handbook) OR title:(NGR) OR title:(drill))',
        "pub": r"\b(NGR|ANGI|NGB)[\s_-]*\d[\w.-]*|national guard .*(manual|regulation|pamphlet|handbook|training|drill)|"
               r"(manual|handbook|regulation|drill).* national guard",
        "exclude": r"remedial|restoration|environmental|investigation report|souvenir|legislature|^\s*DTIC\b|"
                   r"^\s*CIA Reading Room|hearing",
    },
}
PUBNO = re.compile(r"\b([A-Z]{2,8})[\s_-]*(\d[\w.-]*(?:[\s_-]\d[\w.]*)*)", re.I)
# Archive mirrors prefix titles with their own catalogue number ("ERIC ED123456: Fireman"); it is not the pub number.
MIRROR_PREFIX = re.compile(r"^\s*(ERIC\s+ED\d+|DTIC\s+[A-Z0-9]+|CIA Reading Room\s+[\w-]+)\s*:\s*", re.I)
# Only public-release documents go into memory (2026-10-08: a possibly distribution-limited NWP was queued).
RESTRICTED = re.compile(r"for official use only|\bFOUO\b|\bNOFORN\b|controlled unclassified|\bCUI\b|"
                        r"distribution statement\s*[B-F]\b|distribution authorized to|distribution limited|"
                        r"not releasable|\bsecret\b//|limited distribution", re.I)
RESTRICT_SCAN_CHARS = 8000   # covers, title pages and the distribution block


def keep(service: str, title: str) -> bool:
    """A title counts when it is the service's own kind of publication and not low-value. Pure."""
    s = SERVICES[service]
    if not re.search(s["pub"], title, re.I) or LOW_VALUE.search(title):
        return False
    return not (s.get("exclude") and re.search(s["exclude"], title, re.I))


def pub_key(title: str) -> str:
    """One key per publication: its series and number (e.g. 'MCWP 3-15.1'), else the normalised title. Pure."""
    t = MIRROR_PREFIX.sub("", title or "")
    m = PUBNO.search(t)
    if m and m.group(1).upper() not in {"THE", "AND", "FOR", "WITH", "FROM"}:
        num = re.sub(r"[\s_]+", "-", m.group(2).upper()).rstrip(".-")   # "3 11.2" == "3-11.2"
        return f"{m.group(1).upper()} {num}"
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def restricted(text: str) -> bool:
    """True when the opening pages carry a restriction marking: such documents are never ingested. Pure."""
    return bool(RESTRICTED.search((text or "")[:RESTRICT_SCAN_CHARS]))


def pick(service: str, docs: list, seen_keys: set) -> list:
    """Filter search results to new, wanted publications, one per pub_key (first = most downloaded). Pure."""
    out = []
    for ident, title, date in docs:
        k = pub_key(title)
        if keep(service, title) and k not in seen_keys:
            seen_keys.add(k)
            out.append((ident, title, date))
    return out


def search(service: str) -> list:
    """-> [(identifier, title, date)] most-downloaded first (unfiltered)."""
    out, page = [], 1
    while page <= MAX_PAGES:
        qs = urllib.parse.urlencode([("q", SERVICES[service]["query"]), ("fl[]", "identifier"), ("fl[]", "title"),
                                     ("fl[]", "date"), ("rows", "500"), ("page", str(page)),
                                     ("sort[]", "downloads desc"), ("output", "json")])
        docs = json.loads(army.get("https://archive.org/advancedsearch.php?" + qs))["response"]["docs"]
        if not docs:
            break
        out += [(d["identifier"], str(d.get("title") or "")[:300], str(d.get("date") or "")[:10]) for d in docs]
        page += 1
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--service", required=True, choices=sorted(SERVICES))
    ap.add_argument("--target", type=int, default=50000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="print the publications that would be ingested and stop")
    a = ap.parse_args(argv)
    svc, source = a.service, f"military_doctrine_{a.service}"

    oc = army._connect(); oc.autocommit = True; cur = oc.cursor()
    if not a.dry_run:
        cur.execute("""CREATE TABLE IF NOT EXISTS ia_ingest_seen (identifier text PRIMARY KEY, title text,
                       chunks int, at timestamptz DEFAULT now())""")
        cur.execute("ALTER TABLE ia_ingest_seen ADD COLUMN IF NOT EXISTS service text DEFAULT 'army'")
    cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name = 'ia_ingest_seen' AND column_name = 'service'")
    svc_col = "coalesce(service, 'army')" if cur.fetchone() else "'army'"   # before the first real run: all rows are Army
    cur.execute(f"SELECT identifier, title, coalesce(chunks, 0), {svc_col} FROM ia_ingest_seen")  # constant SQL, no values
    rows = cur.fetchall()
    seen_ids = {r[0] for r in rows}
    seen_keys = {pub_key(r[1]) for r in rows if r[3] == svc}
    stored = sum(r[2] for r in rows if r[3] == svc)

    pubs = pick(svc, [d for d in search(svc) if d[0] not in seen_ids], seen_keys)
    if a.list:
        for _i, t, d in pubs:
            print(f"{d or '----------'}  {t[:110]}")
        print(f"{len(pubs)} {svc} publications; {stored:,} memories already stored")
        return 0
    label = SERVICES[svc]["label"]
    ni.notify(f":anchor: *{label}s ingest* — {len(pubs)} publications from the Internet Archive -> `{source}`, "
              f"target {a.target:,} memories ({stored:,} already stored).")
    done_hashes, last = set(), time.time()
    for i, (ident, title, date) in enumerate(pubs, 1):
        if stored >= a.target or ni._shutdown:
            break
        try:
            text = army.ocr_text(ident)
        except Exception as e:  # noqa: BLE001 — one bad item must not stop the run
            ni.log(f"{ident}: fetch failed: {e}")
            continue
        n = 0
        if restricted(text):
            ni.log(f"[{svc} {i}/{len(pubs)}] {title[:80]}: SKIPPED, restriction marking on its opening pages")
            if not a.dry_run:
                cur.execute("INSERT INTO ia_ingest_seen (identifier, title, chunks, service) VALUES (%s, %s, 0, %s) "
                            "ON CONFLICT (identifier) DO NOTHING", (ident, title, svc))
            continue
        meta = {"url": f"https://archive.org/details/{ident}", "type": "document", "site": "archive.org",
                "topic": label, "service": svc, "title": title, "date": date}
        for c in ni.chunk_prose(ni.clean_text(text)):
            if stored + n >= a.target:
                break
            if not ni.is_garbage(c) and ni.remember(f"[{title}] {c}", source, meta, done_hashes, a.dry_run):
                n += 1
        stored += n
        if not a.dry_run:
            cur.execute("INSERT INTO ia_ingest_seen (identifier, title, chunks, service) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (identifier) DO UPDATE SET chunks = excluded.chunks, service = excluded.service, "
                        "at = now()", (ident, title, n, svc))
        ni.log(f"[{svc} {i}/{len(pubs)}] {title[:80]}: {n} chunks (total {stored:,})")
        if time.time() - last >= 600:
            ni.notify(f":anchor: {label}s: {i}/{len(pubs)} — latest *{title[:90]}* — {stored:,} memories")
            last = time.time()
        time.sleep(1)   # politeness to archive.org
    ni.notify(f":white_check_mark: *{label}s ingest stopped* — {stored:,} memories in `{source}`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
