#!/usr/bin/env python3
"""nova_scanners_catch.py — SCANNER'S CATCH: a ledger of the bystanders the radios pick up.

Little Mister, 2026-10-15 (after Nova read Better Living Through Circuitry, 1999): "Add all 9."

In the film the artist Scanner performs with radio scanners, pulling strangers' phone calls out
of the air and into the set: "I never know what I'm going to catch." His work is about public
space and private space and the issues of surveillance. Nova runs eight tuners (P25 police
dispatch, fire dispatch, rail, aviation/ham discovery, NOAA weather) and an ADS-B feed. What
they catch is mostly machinery, but some of it is people who never agreed to be in Nova's
memory: names read out on dispatch, street addresses of calls, plates, private aircraft tied
to their owners.

The cameras already have this discipline (nova_privacy_guards P3: face outputs are tagged
privacy=private and filtered out of content; nova_face_retention: non-household face data is
deleted after 72 h). The radios have none of it. This organ measures the gap, monthly, per
radio source:

  records               rows held (vector memories + telemetry tables)
  third_party_records   rows carrying third-party personal data: a street address with a house
                        number, a plate, a name ("named X", "Mr. X", "RP X"), a phone number, a date of
                        birth; for ADS-B, sightings of aircraft whose registered owner is a private
                        individual (heuristic: an owner name that is not a company, agency or airline)
  oldest_days           age of the oldest record held, and whether ANY purge exists for the source
                        (static scan: nova_retention's table list, scripts that delete/forget it)
  in_memories           rows in nova_memories (vector recall); privacy_marked_private = rows that
                        recall excludes from public content (privacy='private' or a source on the
                        memory server's PRIVATE_SOURCES list)
  published_hits        personal data in PUBLISHED journal posts that came from the radio: a post
                        that cites the source (dispatch / talkgroup / Metrolink / ADS-B ...) and
                        contains an address, plate, name or private tail number that ALSO appears
                        in that source's memories. Target: 0.
  proposed_retention    a retention policy per source, modelled on the camera one. PROPOSED ONLY.

Never deletes anything, never edits a memory, never retags privacy (raw memories are never
deleted — Nova's guardrail; the proposal is for Jordan). Never prints or stores the personal
data itself: counts, kinds and post paths only.
MEASURED (--report): third-party records held, oldest age, published leaks (target 0), per month.

CLI:   --scheduled      measure, upsert this month's rows, one notification (monthly cron)
       --dry-run        measure and print; write nothing, notify nothing
       --report [N]     the last N months
       --selftest       pure-logic assertions, no DB
Table: scanners_catch (month, source) — one row per radio source per month.
Schedule: scheduler.yaml (the Studio holds the journal), monthly.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_dsn  # noqa: E402

SERVICE = "nova_scanners_catch"
SCRIPTS_DIR = Path(__file__).resolve().parent
MEMORY_SERVER = SCRIPTS_DIR.parent / "memory_server.py"
STATEMENT_TIMEOUT_MS = 120000
FETCH_BATCH = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS scanners_catch (
  month text NOT NULL,
  source text NOT NULL,
  records bigint NOT NULL DEFAULT 0,
  third_party_records bigint NOT NULL DEFAULT 0,
  oldest_days real,
  in_memories bigint NOT NULL DEFAULT 0,
  privacy_marked_private bigint NOT NULL DEFAULT 0,
  published_hits int NOT NULL DEFAULT 0,
  proposed_retention text,
  purge_exists boolean NOT NULL DEFAULT false,
  stores jsonb NOT NULL DEFAULT '[]',
  pii jsonb NOT NULL DEFAULT '{}',
  evidence jsonb NOT NULL DEFAULT '{}',
  ts timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (month, source));
"""

# The radio sources. memories: nova_memories.source values; tables: (table, ts column, text column or None).
# markers: how a journal post says it drew on this source. Reference ingests (aviation_ref, police_codes,
# fire_ops, software_defined_radio) are documents, not captures, and are deliberately absent.
SOURCES = (
    {"source": "p25_police", "label": "P25 police dispatch (LAPD talkgroups)",
     "memories": ("scanner", "quarantine:scanner"), "tables": (("telemetry.sds200_calls", "ts", "transcript"),),
     "markers": r"\bLAPD\b|\bP25\b|talkgroup|police scanner|(?:on|over|from) the scanner|scanner traffic|police radio|dispatch audio",
     "files": "P25 audio is decoded live by dsd-fme (no WAVs kept)",
     "proposed": "Tag every transcript privacy=private at capture (like faces). Raw transcripts 7 d; a transcript "
                 "with an address, plate, name or phone 72 h (the camera TTL); keep only the scrubbed hourly "
                 "digest (scanner_digest) for 1 y."},
    {"source": "p25_digest", "label": "hourly scanner digests (LLM summaries)",
     "memories": ("scanner_digest",), "tables": (),
     "markers": r"scanner digest|hourly digest of the scanner",
     "files": "",
     "proposed": "Keep 1 y, already privacy=private. Scrub house numbers to the block ('900 block of') and drop "
                 "names before the summary is stored."},
    {"source": "fire_dispatch", "label": "fire dispatch (Verdugo, via Broadcastify calls)",
     "memories": ("fire",), "tables": (),
     "markers": r"Verdugo Fire|Verdugo dispatch|fire dispatch|fire radio|fire tac\b|Red-?\d (?:Dispatch|\(Tac\))",
     "files": "call audio is a temp file, unlinked after transcription (nova_broadcastify_calls)",
     "proposed": "Tag private at capture. Raw transcripts 30 d (incident pattern value); with an address/name 72 h."},
    {"source": "rail", "label": "rail road channels (Metrolink/UP)",
     "memories": ("rail", "quarantine:rail"), "tables": (),
     "markers": r"Metrolink|Saugus Sub|rail radio|railroad radio|train crew",
     "files": "nRSP WAVs removed after transcription (nova_fm_whisper)",
     "proposed": "Tag private at capture. Raw crew chatter 30 d; with a name/phone 72 h."},
    {"source": "rf_discovery", "label": "RF discovery (aviation ATC, ham, interop)",
     "memories": ("rf_discovery",), "tables": (),
     "markers": r"SoCal Approach|Burbank tower|\bATC\b|ham radio|146\.52|interop channel|rf discovery",
     "files": "RSPduo WAVs removed after transcription (nova_fm_whisper)",
     "proposed": "Raw transcripts 30 d; with a name/phone 72 h. ATC callsigns are public; no tag needed beyond that."},
    {"source": "noaa_wx", "label": "NOAA weather radio",
     "memories": ("noaa-wx",), "tables": (),
     "markers": r"NOAA weather radio|NOAA WX",
     "files": "",
     "proposed": "No third-party data expected; 90 d is plenty (forecasts expire)."},
    {"source": "adsb", "label": "ADS-B overhead aircraft",
     "memories": (), "tables": (("telemetry.overhead_flights", "ts", None), ("public.aircraft_registry", "updated_at", None)),
     "markers": r"ADS-B|tail number|N-number|squawk|flew overhead|overhead (?:aircraft|helicopter|flight)",
     "files": "",
     "proposed": "overhead_flights raw 90 d, keep daily counts. For aircraft owned by private individuals keep the "
                 "type and registration, drop the owner name (aircraft_registry.operator) after 30 d."},
    {"source": "pager", "label": "pagers (POCSAG/FLEX)",
     "memories": ("pager", "pocsag", "flex_pager"), "tables": (),
     "markers": r"\bPOCSAG\b|\bpager message", "files": "",
     "proposed": "No capture exists. If one is ever added: private at capture, 24 h."},
)

# ---------------------------------------------------------------- detectors (pure)

_NUM = r"(?:\d{2,5}|\d(?:-\d){1,4})"
_DIR = r"(?:(?:north|south|east|west|n|s|e|w)\.?\s+)?"
_SUFFIX = r"(?:street|st|avenue|ave|boulevard|blvd|drive|dr|road|rd|way|place|pl|lane|ln|court|ct|terrace|circle|parkway)"
_STREET_WORD = r"(?!(?:at|in|on|and|the|of|to|from|by|for|or|with|near|unit|units|engine|squad)\b)[A-Za-z]+"
ADDRESS_RX = re.compile(r"\b" + _NUM + r"\s+(?:block\s+of\s+)?" + _DIR + r"(?:" + _STREET_WORD + r"\s+){1,2}" + _SUFFIX + r"\b",
                        re.I)
PLATE_RX = re.compile(r"(?i:\b(?:license plate|plate number|plates? (?:is|of|reads))\b)\s*[:#]?\s*[0-9A-Z][0-9A-Z -]{3,9}\b|"
                      r"\b\d[A-Z]{3}\d{3}\b")
_NOT_A_NAME = ("Male", "Female", "Hispanic", "White", "Black", "Asian", "Unit", "Code", "Engine", "Squad", "Medic",
               "Rescue", "Station", "Avenue", "Street", "Boulevard", "Adam", "Boy", "Charles", "David", "Edward",
               "Frank", "George", "Henry", "Ida", "John", "King", "Lincoln", "Mary", "Nora", "Ocean", "Paul",
               "Queen", "Robert", "Sam", "Tom", "Union", "Victor", "William", "Young", "Zebra", "Copy", "Clear")
# a title + surname, or "named / RP is / identified as" + First Last (two capitalised words: one alone is too often a word)
NAME_RX = re.compile(r"(?i:\b(?:Mr|Mrs|Ms)\.?\s+)([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?)|"
                     r"(?i:\b(?:named|name is|name's|goes by|identified as|RP is|reporting party is)\s+)"
                     r"([A-Z][a-z]{2,}\s+[A-Z][a-z]{2,})")
PHONE_RX = re.compile(r"(?<!\d)\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)")
DOB_RX = re.compile(r"\b(?:DOB|D\.O\.B\.|date of birth|born (?:on )?(?:\d{1,2}/\d{1,2}/\d{2,4}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|"
                    r"Sep|Oct|Nov|Dec)\w* \d{1,2}))", re.I)
# Nova's own outputs re-ingested as memories (the leak itself, or a summary of the radio) — not independent sources
DERIVED_SOURCES = ("nova_articles", "scanner_digest", "blog_post_chunk", "herd_blog")
TAIL_RX = re.compile(r"\bN[1-9]\d{0,4}[A-Z]{0,2}\b")
PII_KINDS = ("address", "plate", "name", "phone", "dob")


def pii_matches(text: str) -> dict[str, list[str]]:
    """kind -> matched strings (for corroboration only; never printed or stored). Block-level addresses
    ('900 block of ...') are the press convention and are not counted. Pure."""
    t = text or ""
    out: dict[str, list[str]] = {}
    addrs = [m.group(0) for m in ADDRESS_RX.finditer(t) if "block" not in m.group(0).lower()]
    if addrs:
        out["address"] = addrs
    plates = [m.group(0) for m in PLATE_RX.finditer(t)]
    if plates:
        out["plate"] = plates
    names = [n for n in (m.group(1) or m.group(2) for m in NAME_RX.finditer(t)) if n.split()[0] not in _NOT_A_NAME]
    if names:
        out["name"] = names
    phones = [m.group(0) for m in PHONE_RX.finditer(t)]
    if phones:
        out["phone"] = phones
    if DOB_RX.search(t):
        out["dob"] = [m.group(0) for m in DOB_RX.finditer(t)]
    return out


def pii_kinds(text: str) -> set[str]:
    return set(pii_matches(text))


_ORG_RX = re.compile(r"\b(llc|l\.l\.c|inc|corp\w*|co|company|airline\w*|airways|air|aviation|aero\w*|trust\w*|bank|"
                     r"leasing|lease|ltd|lp|llp|plc|gmbh|county|city|state|police|sheriff|department|dept|patrol|"
                     r"university|college|school|services?|holdings?|partners|group|foundation|club|government|"
                     r"united states|usaf|navy|army|charter\w*|jets?|flight\w*|fly\w*|heli\w*|express|cargo|"
                     r"logistics|capital|management|enterprises?|international|industries|systems|ventures|"
                     r"properties|investments?|association|church|hospital|medical|fire|ems|news|media|broadcast\w*|"
                     r"network|financial|equipment|motors|technologies|labs?)\b", re.I)


def is_private_owner(operator: str | None) -> bool:
    """A registered owner that reads as a person, not a company/agency/airline. Heuristic. Pure."""
    op = (operator or "").strip()
    if not op or not re.fullmatch(r"[A-Za-z .,'-]+", op) or _ORG_RX.search(op):
        return False
    return 2 <= len(op.replace(",", " ").split()) <= 5


def published_scan(posts: list[tuple[str, str]], sources=SOURCES) -> dict[str, list[dict]]:
    """posts: [(relpath, text)] -> source -> [{"post", "kind", "value"}] candidate leaks in posts that cite the
    source. 'value' is kept in memory only, for corroboration against the source's own records. Pure."""
    out: dict[str, list[dict]] = {s["source"]: [] for s in sources}
    for rel, text in posts:
        for s in sources:
            if not re.search(s["markers"], text, re.I):
                continue
            if s["source"] == "adsb":
                for v in sorted(set(TAIL_RX.findall(text))):
                    out["adsb"].append({"post": rel, "kind": "tail", "value": v})
                continue
            for kind, vals in pii_matches(text).items():
                for v in sorted(set(vals)):
                    out[s["source"]].append({"post": rel, "kind": kind, "value": v})
    return out


def monthly_stats(rows: list[tuple]) -> list[dict]:
    """rows: (month, records, third_party, oldest_days_max, published_hits, private, in_memories) -> metrics. Pure."""
    out = []
    for month, rec, tp, oldest, pub, priv, mem in rows:
        out.append({"month": month, "records": int(rec), "third_party": int(tp),
                    "third_party_pct": round(100.0 * tp / rec, 2) if rec else None,
                    "oldest_days": round(float(oldest), 1) if oldest is not None else None,
                    "published_hits": int(pub), "private_pct": round(100.0 * priv / mem, 1) if mem else None})
    return out


def private_sources(path: Path = MEMORY_SERVER) -> set[str]:
    """The memory server's PRIVATE_SOURCES list, read from its source text (no import: it is a server)."""
    try:
        m = re.search(r"PRIVATE_SOURCES\s*=\s*\[(.*?)\]", path.read_text(), re.S)
        return set(re.findall(r"[\"']([\w:.-]+)[\"']", m.group(1))) if m else set()
    except OSError:
        return set()


def purge_scan(sources=SOURCES, scripts_dir: Path = SCRIPTS_DIR) -> dict[str, list[str]]:
    """source -> scripts that delete/forget its memories or rows (static text scan). Pure over files."""
    texts = {}
    for p in sorted(scripts_dir.glob("*.py")) + sorted(scripts_dir.glob("*.sh")):
        if p.name == Path(__file__).name:
            continue
        try:
            texts[p.name] = p.read_text(errors="replace")
        except OSError:
            continue
    out: dict[str, list[str]] = {}
    del_mem = re.compile(r"DELETE\s+FROM\s+memories|/forget|\bforget\(", re.I)
    for s in sources:
        hits = []
        for name, t in texts.items():
            # the source literal must sit near the delete (same statement / function), not anywhere in the file
            mem_hit = any(re.search(r"[\"']" + re.escape(src) + r"[\"']", t[max(0, d.start() - 400):d.end() + 400])
                          for d in del_mem.finditer(t) for src in s["memories"])
            tbl_hit = any(re.search(r"DELETE\s+FROM\s+(?:\w+\.)?" + re.escape(tb.split(".")[-1]) + r"\b", t, re.I)
                          or re.search(r"[\"']table[\"']\s*:\s*[\"']" + re.escape(tb.split(".")[-1]) + r"[\"']", t)
                          for tb, _c, _x in s["tables"])
            if mem_hit or tbl_hit:
                hits.append(name)
        out[s["source"]] = hits
    return out


# ---------------------------------------------------------------- gather (read-only)

def journal_root() -> Path:
    try:
        import nova_journal
        return Path(nova_journal.HUGO_ROOT)
    except Exception:  # noqa: BLE001
        return Path.home() / "nova-journal"


def journal_posts(root: Path) -> list[tuple[str, str]]:
    content = root / "content"
    out = []
    for p in sorted(content.rglob("*.md")) if content.is_dir() else []:
        try:
            out.append((str(p.relative_to(content)), p.read_text(errors="replace")))
        except OSError:
            continue
    return out


def scan_memories(mcur, sources_list: tuple[str, ...], private: set[str], now: datetime) -> dict:
    """Stream one source's memories (server-side cursor) and count. Text never leaves this function."""
    res = {"records": 0, "third_party": 0, "private": 0, "recallable_third_party": 0, "oldest": None,
           "pii": {k: 0 for k in PII_KINDS}}
    if not sources_list:
        return res
    mcur.execute("SELECT source, created_at, text, privacy FROM memories WHERE source = ANY(%s)", (list(sources_list),))
    while True:
        rows = mcur.fetchmany(FETCH_BATCH)
        if not rows:
            break
        for src, created, text, privacy in rows:
            res["records"] += 1
            is_priv = privacy == "private" or src.removeprefix("quarantine:") in private
            res["private"] += is_priv
            if created and (res["oldest"] is None or created < res["oldest"]):
                res["oldest"] = created
            kinds = pii_kinds(text)
            if kinds:
                res["third_party"] += 1
                res["recallable_third_party"] += not is_priv
                for k in kinds:
                    res["pii"][k] += 1
    return res


def scan_tables(cur, src: dict) -> dict:
    res = {"records": 0, "third_party": 0, "oldest": None, "pii": {}, "stores": []}
    from psycopg2 import sql
    for table, ts_col, text_col in src["tables"]:
        schema, name = table.split(".")
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
        if not (cur.fetchone() or (False,))[0]:
            continue
        ident = sql.Identifier(schema, name)
        cur.execute(sql.SQL("SELECT count(*), min({ts}) FROM {t}").format(ts=sql.Identifier(ts_col), t=ident))
        n, oldest = cur.fetchone()
        res["records"] += n
        res["stores"].append({"store": table, "records": n})
        if oldest and (res["oldest"] is None or oldest < res["oldest"]):
            res["oldest"] = oldest
        if text_col and n:
            cur.execute(sql.SQL("SELECT {c} FROM {t}").format(c=sql.Identifier(text_col), t=ident))
            for (text,) in cur.fetchall():
                kinds = pii_kinds(text)
                if kinds:
                    res["third_party"] += 1
                    for k in kinds:
                        res["pii"][k] = res["pii"].get(k, 0) + 1
    if src["source"] == "adsb":
        cur.execute("SELECT to_regclass('public.aircraft_registry') IS NOT NULL")
        if (cur.fetchone() or (False,))[0]:
            cur.execute("SELECT hex, operator FROM aircraft_registry")
            priv_hex = [h for h, op in cur.fetchall() if is_private_owner(op)]
            res["pii"]["private_owner_aircraft"] = len(priv_hex)
            cur.execute("SELECT count(*) FROM telemetry.overhead_flights WHERE hex = ANY(%s)", (priv_hex,))
            sightings = (cur.fetchone() or (0,))[0]
            res["pii"]["private_owner_sightings"] = sightings
            res["third_party"] += len(priv_hex) + sightings
            res["private_hex"] = set(priv_hex)
    return res


def corroborate(mcur, cur, src: dict, hits: list[dict], private_hex: set[str] | None,
                radio: tuple[str, ...] = ()) -> list[dict]:
    """Keep the candidate leaks that are really the radio's: the same string is in this source's own records
    AND in no independent source (news, documents ...). A public address — a hospital, a station, a
    business in the paper — is in the news too, and is not a bystander's."""
    out = []
    for h in hits:
        try:
            if h["kind"] == "tail":
                cur.execute("SELECT hex FROM aircraft_registry WHERE registration = %s LIMIT 1", (h["value"],))
                row = cur.fetchone()
                if row and private_hex and row[0] in private_hex:
                    out.append(h)
                continue
            if not src["memories"]:
                continue
            mcur.execute("SELECT count(*) FILTER (WHERE source = ANY(%s)), "
                         "count(*) FILTER (WHERE NOT (source = ANY(%s)) AND NOT (source = ANY(%s))) "
                         "FROM (SELECT source FROM memories WHERE tsv @@ phraseto_tsquery('english', %s) LIMIT 2000) s",
                         (list(src["memories"]), list(radio or src["memories"]), list(DERIVED_SOURCES), h["value"]))
            mine, independent = mcur.fetchone() or (0, 0)
            if mine and not independent:
                out.append(h)
        except Exception:  # noqa: BLE001 — an uncheckable hit stays a candidate, not a leak
            for c in (mcur, cur):
                try:
                    c.connection.rollback()
                except Exception:  # noqa: BLE001
                    pass
    return out


def measure(cur, mcur, now: datetime | None = None, journal: Path | None = None) -> list[dict]:
    """One ledger row per radio source. Read-only."""
    now = now or datetime.now(timezone.utc)
    private = private_sources()
    purges = purge_scan(scripts_dir=SCRIPTS_DIR)
    posts = journal_posts(journal or journal_root())
    candidates = published_scan(posts)
    radio = tuple(m for x in SOURCES for m in x["memories"])
    out = []
    for s in SOURCES:
        mem = scan_memories(mcur, s["memories"], private, now)
        tab = scan_tables(cur, s)
        oldest = min([o for o in (mem["oldest"], tab["oldest"]) if o is not None], default=None)
        leaks = corroborate(mcur, cur, s, candidates[s["source"]], tab.get("private_hex"), radio)
        stores = ([{"store": f"nova_memories source={m}", "records": None} for m in s["memories"]] if mem["records"] else [])
        stores += tab["stores"]
        if s["files"]:
            stores.append({"store": "files", "note": s["files"]})
        pii = dict(mem["pii"])
        for k, v in tab["pii"].items():
            pii[k] = pii.get(k, 0) + v
        out.append({
            "source": s["source"], "label": s["label"],
            "records": mem["records"] + tab["records"],
            "third_party_records": mem["third_party"] + tab["third_party"],
            "oldest_days": round((now - oldest).total_seconds() / 86400.0, 1) if oldest else None,
            "in_memories": mem["records"], "privacy_marked_private": mem["private"],
            "recallable_third_party": mem["recallable_third_party"],
            "published_hits": len(leaks), "published_posts": sorted({h["post"] for h in leaks}),
            "published_candidates": len(candidates[s["source"]]),
            "leak_kinds": sorted({h["kind"] for h in leaks}),
            "purge_exists": bool(purges[s["source"]]), "purged_by": purges[s["source"]],
            "stores": stores, "pii": {k: v for k, v in pii.items() if v},
            "proposed_retention": s["proposed"],
        })
    return out


# ---------------------------------------------------------------- write / notify / print

def connect(dbname: str = "nova_ops", autocommit: bool = True):
    import psycopg2
    conn = psycopg2.connect(nova_dsn.pg_dsn(dbname, "connect_timeout=10"),
                            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
    conn.autocommit = autocommit
    return conn


def ensure_schema(cur) -> None:
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, SCHEMA)


def record(cur, month: str, rows: list[dict]) -> int:
    for r in rows:
        ev = {"recallable_third_party": r["recallable_third_party"], "published_posts": r["published_posts"],
              "published_candidates": r["published_candidates"], "leak_kinds": r["leak_kinds"],
              "purged_by": r["purged_by"], "label": r["label"]}
        cur.execute("INSERT INTO scanners_catch (month, source, records, third_party_records, oldest_days, in_memories, "
                    "privacy_marked_private, published_hits, proposed_retention, purge_exists, stores, pii, evidence) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb) "
                    "ON CONFLICT (month, source) DO UPDATE SET records = EXCLUDED.records, "
                    "third_party_records = EXCLUDED.third_party_records, oldest_days = EXCLUDED.oldest_days, "
                    "in_memories = EXCLUDED.in_memories, privacy_marked_private = EXCLUDED.privacy_marked_private, "
                    "published_hits = EXCLUDED.published_hits, proposed_retention = EXCLUDED.proposed_retention, "
                    "purge_exists = EXCLUDED.purge_exists, stores = EXCLUDED.stores, pii = EXCLUDED.pii, "
                    "evidence = EXCLUDED.evidence, ts = now()",
                    (month, r["source"], r["records"], r["third_party_records"], r["oldest_days"], r["in_memories"],
                     r["privacy_marked_private"], r["published_hits"], r["proposed_retention"], r["purge_exists"],
                     json.dumps(r["stores"]), json.dumps(r["pii"]), json.dumps(ev)))
    return len(rows)


def render_message(month: str, rows: list[dict]) -> tuple[str, str, str]:
    tp = sum(r["third_party_records"] for r in rows)
    pub = sum(r["published_hits"] for r in rows)
    oldest = max((r["oldest_days"] or 0) for r in rows)
    level = "warning" if pub else "info"
    title = (f"Scanner's Catch {month}: {tp:,} bystander record(s) held, oldest {oldest:.0f} d, "
             f"{pub} published leak(s) (target 0)")
    lines = ["\"I never know what I'm going to catch.\" Third-party data the radios hold, by source:"]
    for r in rows:
        if not r["records"]:
            lines.append(f"  {r['source']}: no records")
            continue
        lines.append(f"  {r['source']}: {r['records']:,} records, {r['third_party_records']:,} third-party, "
                     f"oldest {r['oldest_days']:.0f} d, purge {'yes' if r['purge_exists'] else 'NONE'}, "
                     f"private {r['privacy_marked_private']:,}/{r['in_memories']:,} in memory, "
                     f"published {r['published_hits']}")
    lines.append("Cameras: tagged private, filtered from content, non-household data deleted at 72 h. "
                 "Radios: see above. Proposed retention per source is in scanners_catch.proposed_retention "
                 "(proposal only; nothing was deleted or retagged).")
    return level, title, "\n".join(lines)


def alert(month: str, rows: list[dict]) -> bool:
    from nova_notify import notify
    level, title, body = render_message(month, rows)
    return notify(title, body=body, level=level, category="scanners_catch", dedup_key=f"scanners_catch:{month}",
                  meta={"third_party_records": sum(r["third_party_records"] for r in rows),
                        "published_hits": sum(r["published_hits"] for r in rows),
                        "oldest_days": max((r["oldest_days"] or 0) for r in rows)})


def print_rows(rows: list[dict]) -> None:
    print(f"{'source':<14} {'records':>9} {'3rd-party':>9} {'oldest_d':>8} {'in_mem':>8} {'private':>8} "
          f"{'recall3p':>8} {'pub':>4} {'cand':>5} purge")
    for r in rows:
        print(f"{r['source']:<14} {r['records']:>9,} {r['third_party_records']:>9,} "
              f"{(r['oldest_days'] if r['oldest_days'] is not None else 0):>8.0f} {r['in_memories']:>8,} "
              f"{r['privacy_marked_private']:>8,} {r['recallable_third_party']:>8,} {r['published_hits']:>4} "
              f"{r['published_candidates']:>5} {'yes' if r['purge_exists'] else 'NONE'}")
    for r in rows:
        if r["pii"]:
            print(f"  {r['source']}: pii kinds {r['pii']}" +
                  (f"; leaked kinds {r['leak_kinds']} in {len(r['published_posts'])} post(s)" if r["published_hits"] else ""))
    print("\nProposed retention (not enacted):")
    for r in rows:
        print(f"  {r['source']}: {r['proposed_retention']}")


def report(cur, n: int) -> None:
    cur.execute("SELECT to_regclass('scanners_catch') IS NOT NULL")
    if not (cur.fetchone() or (False,))[0]:
        print("No Scanner's Catch ledger yet (the first scheduled run creates it).")
        return
    cur.execute("SELECT month, sum(records), sum(third_party_records), max(oldest_days), sum(published_hits), "
                "sum(privacy_marked_private), sum(in_memories) FROM scanners_catch GROUP BY month ORDER BY month DESC "
                "LIMIT %s", (n,))
    print("Monthly (continuity review):")
    for m in monthly_stats(cur.fetchall()):
        print(f"  {m['month']}  records {m['records']:,}  third-party {m['third_party']:,} ({m['third_party_pct']}%)  "
              f"oldest {m['oldest_days']} d  published leaks {m['published_hits']} (target 0)  "
              f"private in memory {m['private_pct']}%")


def log(m: str) -> None:
    print(f"[scanners-catch {datetime.now():%H:%M:%S}] {m}", flush=True)


def selftest() -> int:
    assert pii_kinds("units respond to 3-1-2-0 west temple street") == {"address"}
    assert pii_kinds("1200 block of Main Street") == set()
    assert "plate" in pii_kinds("license plate 7ABC123")
    assert "name" in pii_kinds("RP is Jane Roe at the door") and "name" not in pii_kinds("suspect named Male")
    assert pii_kinds("Engine 32, cancel. Engine 105, available.") == set()
    assert is_private_owner("SMITH JOHN Q") and not is_private_owner("SOUTHWEST AIRLINES CO")
    assert not is_private_owner("LOS ANGELES COUNTY SHERIFF") and not is_private_owner(None)
    hits = published_scan([("local/a.md", "LAPD talkgroup: a call at 4512 Elm Street")])
    assert hits["p25_police"] and hits["p25_police"][0]["kind"] == "address" and not hits["rail"]
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scheduled", action="store_true", help="measure, record this month, notify once")
    ap.add_argument("--dry-run", action="store_true", help="measure and print; write nothing, notify nothing")
    ap.add_argument("--report", nargs="?", const=12, type=int, metavar="N", help="the last N months")
    ap.add_argument("--selftest", action="store_true", help="pure-logic assertions, no DB")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not (a.scheduled or a.dry_run or a.report is not None):
        ap.error("choose --scheduled, --dry-run, --report or --selftest")
    t0 = time.monotonic()
    conn = connect()
    cur = conn.cursor()
    mconn = None
    try:
        if a.report is not None:
            report(cur, a.report)
            return 0
        mconn = connect("nova_memories", autocommit=False)        # named (server-side) cursors need a transaction
        rows = measure(cur, _MemCursor(mconn))
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        if a.dry_run:
            print_rows(rows)
            print(f"\n[dry-run] {len(rows)} source(s), {time.monotonic() - t0:.1f}s — nothing written")
            return 0
        ensure_schema(cur)
        record(cur, month, rows)
        sent = alert(month, rows)
        log(f"{len(rows)} source(s) recorded for {month}; "
            f"{sum(r['published_hits'] for r in rows)} published leak(s); notify {'ok' if sent else 'failed'} "
            f"({time.monotonic() - t0:.1f}s)")
        return 0
    finally:
        conn.close()
        if mconn is not None:
            mconn.close()


class _MemCursor:
    """One streaming (named) cursor per big scan, a plain cursor for point lookups. Both read-only."""

    def __init__(self, conn):
        self.connection = conn
        self._plain = conn.cursor()
        self._cur = self._plain

    def execute(self, q, params=None):
        if q.startswith("SELECT source, created_at, text, privacy FROM memories"):
            self._cur = self.connection.cursor(name=f"scanners_catch_{id(params)}_{time.monotonic_ns()}")
            self._cur.itersize = FETCH_BATCH
        else:
            self._cur = self._plain
        self._cur.execute(q, params)

    def fetchmany(self, n):
        return self._cur.fetchmany(n)

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


if __name__ == "__main__":
    sys.exit(main())
