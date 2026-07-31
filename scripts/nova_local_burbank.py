#!/usr/bin/env python3
"""
nova_local_burbank.py — Nova's daily Burbank local news dispatch.

Runs daily at 10 AM. Pulls recent Burbank/LA news from Nova's memory
(ingested via RSS feeds), writes a sarcastic article about what's
happening locally, and publishes to the Local section of nova-journal.

Written by Jordan Koch.
"""

import json
import re
import shutil
import subprocess
import sys
import time
import base64
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

HUGO_ROOT = (Path.home() / "nova-journal")
CONTENT_DIR = HUGO_ROOT / "content/local"
IMAGES_DIR = HUGO_ROOT / "static/images/local"
LOG_FILE = Path.home() / ".openclaw/logs/nova_local_burbank.log"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ARTICLE_MODEL = "anthropic/claude-haiku-4.5"
IMAGE_MODEL = "openai/gpt-5-image"
PG_DSN = "dbname=nova_memories user=kochj host=pg-primary.digitalnoise.net"
NOVA_OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[local-burbank {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def get_openrouter_key():
    r = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
        capture_output=True, text=True
    )
    return r.stdout.strip()


def call_llm(system, user, model=None, max_tokens=8000):
    # Prefer Claude Code Max (flat-rate, matches nova_rando_daily_ops.py/nova_journal_security.py)
    # -- OpenRouter's credit balance ran dry 2026-07-17, so the direct-API path below
    # has been failing (401) on every run since, silently killing this article for 10+ days.
    try:
        import nova_claude_code
        return nova_claude_code.claude_generate(user, system=system)
    except Exception as e:
        log(f"claude_generate failed, falling back to OpenRouter: {e}")
    api_key = get_openrouter_key()
    body = json.dumps({
        "model": model or ARTICLE_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.9,
    }).encode()
    req = urllib.request.Request(OPENROUTER_URL, data=body, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://nova.digitalnoise.net",
        "X-Title": "Nova Local Burbank",
    })
    resp = urllib.request.urlopen(req, timeout=300)
    data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


# ── News Queries ──────────────────────────────────────────────────────────────

def get_local_news(hours=24, limit=50):
    import psycopg2
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    cutoff = datetime.now() - timedelta(hours=hours)
    cur.execute("""
        SELECT text, source, created_at, metadata
        FROM memories
        WHERE source IN ('local_burbank', 'local_news')
          AND created_at >= %s
          AND LENGTH(text) > 80
        ORDER BY created_at DESC
        LIMIT %s
    """, (cutoff, limit))
    rows = cur.fetchall()
    conn.close()
    return [{"text": r[0], "source": r[1], "created_at": str(r[2]),
             "metadata": r[3] if isinstance(r[3], dict) else (json.loads(r[3]) if r[3] else {})}
            for r in rows]


def get_burbank_search(limit=30, hours=36):
    """Semantic search for recent Burbank-related content across all sources.

    Time-bounded (unlike the raw /recall call, which has no server-side time filter) --
    semantic search otherwise happily resurfaces week-old items with no signal that
    they're old, and the article ends up narrating last weekend's news as if it just
    happened. hours is a bit looser than get_local_news's 24h since this is a backstop
    search, not the primary feed.
    """
    try:
        resp = urllib.request.urlopen(
            f"http://memory-server.digitalnoise.net:18790/recall?q=Burbank+California+local+news+today&n={limit}&source=local_burbank",
            timeout=10
        )
        data = json.loads(resp.read())
        memories = data.get("memories", [])
    except Exception:
        return []

    cutoff = datetime.now().astimezone() - timedelta(hours=hours)
    fresh = []
    for m in memories:
        ts = m.get("created_at")
        try:
            created = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            continue  # no timestamp -- can't verify it's recent, drop it rather than risk stale content
        if created >= cutoff:
            fresh.append(m)
    return fresh


def get_scanner_blotter(hours=18):
    """Airwaves activity (police + fire + rail), AGGREGATED IN CODE into safe theme tallies.

    Returns theme counts, aggregate distance-from-home (nearest run, how many within 3 mi), and
    the closest INTERSECTIONS/cross-streets (never house numbers). Raw transcripts, victim/patient
    descriptions, and specific residential street addresses never leave this function — so the
    public article can convey how close activity was without republishing anyone's doorstep.
    Covers LAPD (Northeast + North Hollywood), Verdugo Fire/EMS, and the Metrolink/UP rail corridor.
    """
    import psycopg2
    try:
        conn = psycopg2.connect(PG_DSN)
        cur = conn.cursor()
        cutoff = datetime.now() - timedelta(hours=hours)
        cur.execute("SELECT source, text, metadata->'geo' FROM memories "
                    "WHERE source IN ('scanner','fire','rail') AND created_at >= %s", (cutoff,))
        rows = cur.fetchall()
        conn.close()
    except Exception:
        return None

    WALK_MI = 2.5   # "could walk to it" (~50 min) — these events get detailed, per-incident treatment
    dom = {"scanner": "police", "fire": "fire", "rail": "rail"}
    blobs = {"police": [], "fire": [], "rail": []}
    dists = {"police": [], "fire": [], "rail": []}   # distance-from-home (aggregate)
    near = {"police": {}, "fire": {}, "rail": {}}     # cross-street -> miles; intersections/streets only
    close = {"police": [], "fire": [], "rail": []}    # walkable incidents: (text_lower, miles, safe_cross_street)
    for source, text, geo in rows:
        d = dom[source]
        tl = (text or "").lower()
        blobs[d].append(tl)
        if geo:
            nmi = geo.get("nearest_mi")
            if nmi is not None:
                dists[d].append(nmi)
            safe_where = None
            for loc in (geo.get("locations") or []):
                addr, mi = loc.get("addr", ""), loc.get("mi")
                # public-safe: keep intersections / named streets only. A leading house number
                # means a specific residence — never surface those in the public article.
                if addr and mi is not None and not addr[:1].isdigit():
                    near[d][addr] = mi
                    if safe_where is None:
                        safe_where = addr
            if nmi is not None and nmi <= WALK_MI:
                close[d].append((tl, nmi, safe_where))

    def themes_of(texts, patterns):
        # strip the "[Channel Label ...]" prefix so label words (e.g. "Fire") don't inflate themes
        blob = " ".join(re.sub(r"^\[[^\]]*\]\s*", "", t) for t in texts)
        return {k: v for k, v in
                ((name, len(re.findall(rx, blob))) for name, rx in patterns.items()) if v > 0}

    PATTERNS = {
        "police": {
            "traffic stops": r"traffic stop",
            "vehicle-related": r"\bvehicle\b|stolen|recovered",
            "suspect stops/investigations": r"suspect",
            "domestic incidents": r"domestic",
            "pursuits/code-3": r"pursuit|code 3",
        },
        "fire": {
            "medical/EMS": r"medical|breathing|chest pain|blood pressure|patient|unconscious|\bfall\b",
            "structure/smoke": r"structure|smoke|flames|\bfire\b",
            "traffic collision": r"collision|\bt\.?c\.?\b|traffic",
            "rescue": r"rescue|extricat|trapped",
            "alarm": r"alarm",
        },
        "rail": {
            "signals/clear": r"signal|clear|approach|highball",
            "crossings": r"crossing|\bgate\b",
            "movements": r"track|siding|switch|northbound|southbound",
            "maintenance": r"maintenance|track car|\bwork\b",
        },
    }
    def categorize(tl, patterns):
        for name, rx in patterns.items():
            if re.search(rx, tl):
                return name
        return "activity"

    out = {}
    for d in ("police", "fire", "rail"):
        if len(blobs[d]) >= 3:
            entry = {"calls": len(blobs[d]), "themes": themes_of(blobs[d], PATTERNS[d])}
            ds = sorted(dists[d])
            if ds:
                entry["dist"] = {"nearest": round(ds[0], 1),
                                 "within_3mi": sum(1 for x in ds if x <= 3), "located": len(ds)}
            if near[d]:
                entry["near"] = sorted(near[d].items(), key=lambda kv: kv[1])[:4]  # closest cross-streets
            if close[d]:
                evs = sorted(close[d], key=lambda x: x[1])[:6]   # closest walkable incidents, detailed
                entry["near_events"] = [{"cat": categorize(tl, PATTERNS[d]), "where": w, "mi": round(mi, 1)}
                                        for tl, mi, w in evs]
            out[d] = entry
    return out or None


def get_overhead_flights(hours=24):
    """What flew over 91506 today, aggregated from nova_flights_poller.py's live feed
    (telemetry.overhead_flights). Same aggregate-first shape as get_scanner_blotter:
    counts for the ordinary traffic, named detail only for what's actually notable
    (helicopters, low passes, emergency squawks) -- no need to narrate every airliner
    that crossed 8000ft three miles out."""
    import psycopg2
    try:
        conn = psycopg2.connect(NOVA_OPS_DSN)
        cur = conn.cursor()
        cutoff = datetime.now() - timedelta(hours=hours)
        # DISTINCT ON hex: the poller logs a fresh row every ~30s an aircraft is in
        # view, so a single helicopter circling for 20 minutes would otherwise look
        # like 40 different sightings. One row per aircraft -- its closest approach.
        cur.execute("""
            SELECT DISTINCT ON (hex)
                   hex, type_name, operator, registration, callsign, is_helicopter, alt_ft,
                   dist_nm, compass, squawk, ts
            FROM telemetry.overhead_flights
            WHERE ts >= %s
            ORDER BY hex, dist_nm ASC NULLS LAST
        """, (cutoff,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception:
        return None
    if not rows:
        return None

    EMERGENCY_SQUAWKS = {"7500", "7600", "7700"}
    total = len(rows)  # distinct aircraft (hex), not poll-cycle rows
    helicopters = [r for r in rows if r[5]]
    emergencies = [r for r in rows if r[9] in EMERGENCY_SQUAWKS]
    low_passes = [r for r in rows if r[6] is not None and r[6] < 4000 and r[7] is not None and r[7] < 1.5]

    def describe(r):
        _, type_name, operator, reg, callsign, _, alt, dist, comp, _, ts = r
        who = type_name + (f" ({operator})" if operator else "")
        ident = reg or callsign or ""
        where = f"{int(alt)} ft, {round(dist, 1)} mi {comp}" if alt is not None and dist is not None else ""
        return f"{who}{f' [{ident}]' if ident else ''} — {where} at {ts.strftime('%-I:%M %p')}"

    # priority order emergencies > low passes > helicopters, deduped by hex (a
    # helicopter making a low pass would otherwise appear in two categories)
    seen, notable = set(), []
    for r in emergencies + low_passes + helicopters:
        if r[0] not in seen:
            seen.add(r[0])
            notable.append(describe(r))
        if len(notable) >= 8:
            break

    return {
        "total": total,
        "helicopter_count": len(helicopters),
        "notable": notable,
        "emergency_count": len(emergencies),
    }


def get_wifi_ble_summary(hours=24):
    """Aggregate WiFi AP and BLE device sightings for the last N hours -- counts
    and notable changes only, never raw BSSIDs/MACs or exact addresses in what
    gets sent to the cloud LLM (that scrubbing happens in _scrub_obj downstream,
    but this function itself only returns aggregate numbers, nothing per-device)."""
    import psycopg2
    open_ap_names = []
    try:
        conn = psycopg2.connect(NOVA_OPS_DSN)
        cur = conn.cursor()
        cutoff_hours = hours
        cur.execute("""
            SELECT count(DISTINCT bssid) FILTER (WHERE NOT is_ours),
                   count(DISTINCT bssid) FILTER (WHERE is_ours),
                   count(DISTINCT bssid) FILTER (WHERE NOT is_ours AND security ILIKE '%%open%%')
            FROM wifi_aps WHERE ts >= now() - interval '%s hours'
        """, (cutoff_hours,))
        neighbor_aps, our_aps, open_aps = cur.fetchone()
        # NAME the open neighbor networks (Jordan 2026-07-30): an open SSID is already
        # broadcasting itself to anyone with a phone, so listing it isn't a leak — and it
        # might get an owner to fix it. Our own SSIDs and secured neighbors stay unnamed.
        cur.execute("""
            SELECT DISTINCT ssid FROM wifi_aps
            WHERE ts >= now() - interval '%s hours' AND NOT is_ours
              AND security ILIKE '%%open%%' AND ssid IS NOT NULL AND btrim(ssid) <> ''
            ORDER BY ssid LIMIT 25
        """, (cutoff_hours,))
        open_ap_names = [r[0] for r in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception:
        neighbor_aps = our_aps = open_aps = None
        open_ap_names = []

    try:
        conn = psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")
        cur = conn.cursor()
        cur.execute("""
            SELECT count(DISTINCT device_mac) FROM telemetry.bluetooth
            WHERE ts >= now() - interval '%s hours'
        """, (cutoff_hours,))
        ble_devices = cur.fetchone()[0]
        cur.close()
        conn.close()
    except Exception:
        ble_devices = None

    if neighbor_aps is None and ble_devices is None:
        return None
    return {"neighbor_aps": neighbor_aps, "our_aps": our_aps, "open_aps": open_aps,
           "open_ap_names": open_ap_names, "ble_devices": ble_devices}


def get_lora_summary(hours=24):
    """LoRa / Meshtastic mesh heard over the air in the last N hours.

    Data comes from telemetry.mesh_nodes (nova_mesh_churn_report snapshots the T114's
    NodeDB). This is the local long-range radio mesh — SoCalMesh infrastructure, ham
    operators, solar nodes — all of it public LoRa broadcast, so naming nodes is fine.
    Aggregate + a few notable named nodes; nothing sensitive."""
    import psycopg2
    try:
        conn = psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")
        cur = conn.cursor()
        cur.execute("""
            SELECT count(DISTINCT node_id),
                   count(DISTINCT node_id) FILTER (WHERE hops_away = 0 OR hops_away IS NULL),
                   min(hops_away) FILTER (WHERE hops_away > 0)
            FROM telemetry.mesh_nodes WHERE last_heard >= now() - interval '%s hours'
        """, (hours,))
        total, direct, closest_hops = cur.fetchone()
        # A handful of the named nodes heard, strongest signal first — the flavor of
        # who's on the local mesh (SoCalMesh sites, callsigns, quirky node names).
        cur.execute("""
            SELECT DISTINCT ON (long_name) long_name, hops_away
            FROM telemetry.mesh_nodes
            WHERE last_heard >= now() - interval '%s hours'
              AND long_name IS NOT NULL AND btrim(long_name) <> ''
            ORDER BY long_name, snr DESC NULLS LAST LIMIT 12
        """, (hours,))
        notable = [(r[0], r[1]) for r in cur.fetchall()]
        cur.close(); conn.close()
    except Exception:
        return None
    if not total:
        return None
    return {"total": total, "direct": direct, "closest_hops": closest_hops, "notable": notable}


def get_myburbank_arrests(news_items):
    """For any myBurbank 'Police Log' item in today's news, fetch the full article page
    (the RSS description is just boilerplate -- no actual arrest data) and LLM-extract the
    individual arrests myBurbank already publishes by name: date, name, home city, arrest
    location, time, charge(s). Returns a list of arrest dicts (possibly from multiple logs
    if more than one dropped today), or [] if none found / fetch failed."""
    seen_urls = set()
    log_items = []
    for it in news_items:
        if "police log" not in it.get("text", "").lower():
            continue
        url = (it.get("metadata") or {}).get("url")
        # the same police-log post commonly shows up in both get_local_news and
        # get_burbank_search -- dedupe by URL or it gets scraped/extracted twice,
        # silently doubling every arrest in the downstream charge tally.
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        log_items.append(it)
    if not log_items:
        return []

    all_arrests = []
    for it in log_items:
        url = it["metadata"]["url"]
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 NovaBot/1.0"})
            html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="replace")
        except Exception as e:
            log(f"myBurbank fetch failed for {url}: {e}")
            continue

        # crude but sufficient: strip tags/scripts down to text, the article body is plain prose
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&#8211;|&ndash;", "-", text)
        text = re.sub(r"&\w+;", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        system = (
            "Extract every individual arrest from this Burbank police log article into a JSON array. "
            'Each element: {"date": "...", "name": "...", "residence": "...", "location": "...", '
            '"time": "...", "charges": "..."}. Use the exact wording from the article for each field. '
            "If a field isn't stated, use an empty string. Output ONLY the JSON array, nothing else."
        )
        try:
            raw = call_llm(system, text[:12000], max_tokens=3000)
            # Models sometimes tack on a trailing note or repeat themselves after the array --
            # raw_decode from the first '[' parses just the array and ignores anything after it,
            # instead of choking on "Extra data" like json.loads(whole string) would.
            start = raw.index("[")
            arrests, _ = json.JSONDecoder().raw_decode(raw, start)
            if isinstance(arrests, list):
                log(f"myBurbank: extracted {len(arrests)} arrests from {it['metadata'].get('title', url)}")
                all_arrests.extend(arrests)
        except Exception as e:
            log(f"myBurbank arrest extraction failed for {url}: {e}")

    return all_arrests


def get_bluetooth_patterns(days=21, min_days=3):
    """Look for recurring unidentified BLE devices in telemetry.bluetooth history and
    surface any real daily-time patterns -- not just today's raw count.

    MAC addresses rotate (BLE privacy), so device_mac is useless as a stable identity.
    device_name is far more stable in practice, but the poller's per-sighting vendor
    classification is noisy (it's derived from the rotating MAC's OUI, so the SAME named
    device flips between a real vendor and "unknown" from one sighting to the next).
    So: group by device_name, and treat a name as an "unidentified" candidate if the
    MAJORITY of its sightings were classified vendor=unknown, regardless of name format.

    For each candidate, classify by how much of the day it's present:
      - "resident" (median daily span > 12h) -- something stationary nearby, not a
        neighbor walking past; interesting as "unidentified but always here", not a
        time-of-day pattern.
      - "transient" (median daily span <= 12h) on >= min_days distinct days -- check
        whether its daily start time clusters tightly (stddev < 1.5h); that's the real
        "shows up around the same time every day" signal.

    Returns aggregate/descriptive info only -- never raw device_name/MAC (consistent
    with get_wifi_ble_summary's no-per-device-identifiers-to-the-public rule)."""
    import psycopg2
    try:
        conn = psycopg2.connect(NOVA_OPS_DSN)
        cur = conn.cursor()
        cur.execute("""
            WITH named AS (
                SELECT device_name,
                       date(ts AT TIME ZONE 'America/Los_Angeles') AS day,
                       extract(hour FROM ts AT TIME ZONE 'America/Los_Angeles') AS hr,
                       (metadata->>'vendor' = 'unknown') AS unk
                FROM telemetry.bluetooth
                WHERE ts >= now() - (%s || ' days')::interval
                  AND device_name IS NOT NULL AND device_name != ''
            ),
            per_device AS (
                SELECT device_name,
                       count(*) AS sightings,
                       avg(unk::int) AS unknown_ratio
                FROM named GROUP BY device_name
                HAVING avg(unk::int) > 0.5
            ),
            per_day AS (
                SELECT n.device_name, n.day, min(n.hr) AS start_hr, max(n.hr) - min(n.hr) AS span_hr
                FROM named n JOIN per_device p USING (device_name)
                GROUP BY n.device_name, n.day
            )
            SELECT device_name,
                   count(*) AS distinct_days,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY span_hr) AS median_span_hr,
                   avg(start_hr) AS avg_start_hr,
                   stddev(start_hr) AS stddev_start_hr
            FROM per_day
            GROUP BY device_name
            HAVING count(*) >= %s
            ORDER BY count(*) DESC
        """, (days, min_days))
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        log(f"bluetooth pattern query failed: {e}")
        return None

    if not rows:
        return None

    resident, transient_patterned, transient_random = 0, [], 0
    for _, distinct_days, median_span, avg_start, stddev_start in rows:
        if median_span is not None and median_span > 12:
            resident += 1
        elif stddev_start is not None and stddev_start < 1.5:
            hr = int(avg_start)
            ampm = "AM" if hr < 12 else "PM"
            hr12 = hr % 12 or 12
            transient_patterned.append({"distinct_days": distinct_days, "start": f"~{hr12} {ampm}"})
        else:
            transient_random += 1

    transient_patterned.sort(key=lambda x: -x["distinct_days"])
    return {
        "candidates": len(rows),
        "resident": resident,               # unidentified but present most of the day, every day
        "transient_patterned": transient_patterned[:3],  # real time-of-day recurrence
        "transient_random": transient_random,
    }


# ── Article Generation ────────────────────────────────────────────────────────

def generate_article(news_items, scanner_blotter=None, flights=None, wifi_ble=None,
                      arrests=None, ble_patterns=None, lora=None):
    # Score each news item by locality so the article weights detail toward Burbank/nearby (not
    # Pasadena/DTLA). Same proximity principle as the scanner blotter, applied to ALL news.
    try:
        from nova_geo_distance import place_distance
    except Exception:
        place_distance = lambda t: None
    # California-only: this is a LOCAL report — drop anything with no CA/SoCal signal (no Ohio news).
    CA_SIGNAL = re.compile(
        r"\b(california|calif\b|socal|southern california|los angeles|l\.?a\.? county|"
        r"san fernando|san gabriel|orange county|ventura|santa clarita|antelope valley|"
        r"inland empire|long beach|san diego|san francisco|sacramento|bay area)\b", re.I)

    def is_california(t):
        return bool(place_distance(t)) or bool(CA_SIGNAL.search(t or ""))

    def age_str(created_at):
        if not created_at:
            return "age unknown"
        try:
            created = datetime.fromisoformat(created_at)
            if created.tzinfo is None:
                created = created.astimezone()
            hrs = (datetime.now().astimezone() - created).total_seconds() / 3600
        except (TypeError, ValueError):
            return "age unknown"
        if hrs < 20:
            return "today"
        days = round(hrs / 24)
        return "yesterday" if days == 1 else f"{days} days ago"

    scored, dropped = [], 0
    for item in news_items:
        full = item.get("text", "")
        if not is_california(full):
            dropped += 1
            continue
        text = full[:500].replace("\n", " ").strip()
        pd = place_distance(full)
        scored.append({"text": text, "source": item.get("source", "unknown"),
                       "age": age_str(item.get("created_at")),
                       "place": pd[0] if pd else None, "mi": pd[1] if pd else None})
    if dropped:
        log(f"Dropped {dropped} non-California news items")
    scored.sort(key=lambda x: (x["mi"] is None, x["mi"] if x["mi"] is not None else 999))  # nearest first
    news_block = ""
    for i, it in enumerate(scored, 1):
        loc = f" [{it['place']}, ~{it['mi']} mi]" if it["mi"] is not None else " [locality unknown]"
        news_block += f"\n{i}. [{it['source']}, {it['age']}]{loc} {it['text']}\n"

    scanner_block = ""
    if scanner_blotter:
        LABELS = {"police": "LAPD (Northeast + North Hollywood)",
                  "fire": "Verdugo Fire/EMS (Burbank/Glendale)",
                  "rail": "Metrolink/UP rail corridor"}
        parts = []
        for dom, label in LABELS.items():
            b = scanner_blotter.get(dom)
            if not b:
                continue
            tallies = ", ".join(f"{k} ({v})" for k, v in b["themes"].items()) or "assorted routine chatter"
            extra = ""
            if b.get("dist"):
                dd = b["dist"]
                extra += (f" | proximity: nearest run ~{dd['nearest']} mi, "
                          f"{dd['within_3mi']}/{dd['located']} located calls within 3 mi")
            if b.get("near"):
                extra += " | nearby cross-streets: " + ", ".join(f"{a} (~{m} mi)" for a, m in b["near"])
            if b.get("near_events"):
                evs = "; ".join(e["cat"] + (f" near {e['where']}" if e.get("where") else "") + f" (~{e['mi']} mi)"
                                for e in b["near_events"])
                extra += f" | WALKABLE INCIDENTS (<=2.5 mi — give these the MOST detail): {evs}"
            parts.append(f"{label}: {b['calls']} calls — {tallies}{extra}")
        if parts:
            scanner_block = (
                f"\n\n[BURBANK AIRWAVES — last ~18h. " + "; ".join(parts) + ". "
                f"WRITE A PROXIMITY-WEIGHTED blotter — detail scales with closeness: spend the MOST words on the "
                f"WALKABLE INCIDENTS (<=2.5 mi, the reader's own backyard), giving each a vivid sentence or two "
                f"(type + cross-street if provided + how close). Give mid-range activity (2.5-8 mi) a brief mention, "
                f"and roll distant activity (>8 mi) into a single aggregate line (e.g. 'another busy night ~5-10 mi "
                f"out toward downtown'). Cover ALL of it, but zoom in on what's near. You MAY name "
                f"intersections/cross-streets and distances; you must NOT print any specific house/apartment "
                f"street address or number, victim/patient names or descriptions, or the reader's home address. "
                f"Wry and respectful.]\n"
            )

    flights_block = ""
    if flights:
        bits = [f"{flights['total']} aircraft tracked overhead"]
        if flights["helicopter_count"]:
            bits.append(f"{flights['helicopter_count']} helicopter(s)")
        if flights["emergency_count"]:
            bits.append(f"{flights['emergency_count']} EMERGENCY SQUAWK event(s)")
        notable = "; ".join(flights["notable"]) or "nothing beyond routine airline traffic"
        flights_block = (
            f"\n\n[OVERHEAD TRAFFIC — last 24h, zip 91506. {', '.join(bits)}. "
            f"Notable sightings (helicopters/low passes/emergency squawks, most notable first): {notable}. "
            f"Mention this only briefly (a sentence or two, maybe a paragraph if an emergency squawk or an "
            f"interesting helicopter operator showed up) -- this is color, not the headline, unless something "
            f"genuinely unusual happened (an emergency squawk is always worth a real mention).]\n"
        )

    wifi_ble_block = ""
    if wifi_ble:
        bits = []
        if wifi_ble.get("neighbor_aps") is not None:
            bits.append(f"{wifi_ble['neighbor_aps']} distinct neighboring WiFi networks seen "
                       f"(plus {wifi_ble.get('our_aps', 0)} of our own)")
            if wifi_ble.get("open_aps"):
                names = wifi_ble.get("open_ap_names") or []
                if names:
                    name_list = ", ".join(f'"{n}"' for n in names)
                    bits.append(f"{wifi_ble['open_aps']} of them broadcasting with no security at all — "
                                f"by name: {name_list} (these SSIDs advertise themselves openly to anyone "
                                f"nearby, so naming them here changes nothing except maybe nudging an owner "
                                f"to lock it down — feel free to riff on the ones with funny/telling names)")
                else:
                    bits.append(f"{wifi_ble['open_aps']} of them broadcasting with no security at all")
        if wifi_ble.get("ble_devices") is not None:
            bits.append(f"{wifi_ble['ble_devices']} distinct Bluetooth LE devices heard")
        pattern_bits = []
        if ble_patterns:
            if ble_patterns["resident"]:
                pattern_bits.append(f"{ble_patterns['resident']} unidentified device(s) that are "
                                     f"basically always in range (present most of every day, day after day "
                                     f"over the last few weeks) -- something stationary and unlabeled nearby, "
                                     f"not a passerby")
            for p in ble_patterns["transient_patterned"]:
                pattern_bits.append(f"an unidentified device that shows up briefly on {p['distinct_days']} "
                                     f"of the last ~21 days, consistently around {p['start']}")
            if ble_patterns["transient_random"]:
                pattern_bits.append(f"{ble_patterns['transient_random']} other recurring unidentified device(s) "
                                     f"with no consistent time-of-day pattern")
        if bits or pattern_bits:
            wifi_ble_block = (
                f"\n\n[RF NEIGHBORHOOD — last 24h. {'; '.join(bits)}."
                + (f" PATTERNS FOUND IN THE LAST ~3 WEEKS OF HISTORY: {'; '.join(pattern_bits)}. "
                   f"This is worth a real paragraph, not just a passing line -- it's genuinely interesting "
                   f"(a mystery device that's always here, or one that shows up like clockwork). Speculate "
                   f"playfully about what it might be (a neighbor's smart device, a delivery route, a dog "
                   f"walker's phone) but don't claim certainty." if pattern_bits else
                   " Mention this only briefly (a sentence, maybe two) as neighborhood color.")
                + " Do NOT name secured networks, and never print a BSSID or MAC. The OPEN networks"
                + " listed by name above ARE fine to name (they broadcast openly to anyone with a phone) —"
                + " include those names, and it's fair game to riff on the funny/telling ones.]\n"
            )

    lora_block = ""
    if lora:
        notable = lora.get("notable") or []
        node_bits = ", ".join(
            f'"{n}"' + (f" ({h} hop{"s" if h != 1 else ""})" if h else "")
            for n, h in notable)
        closest = lora.get("closest_hops")
        lora_block = (
            f"\n\n[LORA MESH — last 24h. Nova's Meshtastic node ('Rancho Adjacent', a Heltec T114) "
            f"heard {lora['total']} distinct node(s) on the local long-range radio mesh"
            + (f", the nearest ~{closest} hop(s) out" if closest else "")
            + (f". A sampling of who was on the air: {node_bits}." if node_bits else ".")
            + " This is the SoCal LoRa mesh — SoCalMesh.org infrastructure, ham operators, solar "
            "test nodes, oddball hobbyist handles — all public LoRa broadcast, so naming nodes is "
            "fine. Give it a short, genuinely-interested paragraph: this is Burbank's invisible "
            "long-range radio neighborhood, the kind of thing that keeps working when the internet "
            "doesn't. Note anything with a callsign or a funny node name.]\n"
        )

    arrests_block = ""
    if arrests:
        lines = []
        for a in arrests:
            bits = [a.get("date", ""), a.get("name", ""), a.get("residence", "")]
            loc_time = " ".join(x for x in [a.get("location", ""), a.get("time", "")] if x)
            charges = a.get("charges", "")
            lines.append(f"- {' — '.join(x for x in bits if x)}"
                         f"{f' at {loc_time}' if loc_time else ''}"
                         f"{f'. Charges: {charges}' if charges else ''}")
        arrests_block = (
            f"\n\n[BURBANK POLICE LOG — {len(arrests)} individual arrests from myBurbank's published log "
            f"(full detail as myBurbank itself prints it -- names, home city, arrest location/time, charges):\n"
            + "\n".join(lines) +
            f"\n\nGive this its own real section. Cover it properly: don't just say '5 weekly logs dropped, "
            f"here's a vibe' -- go through the actual arrests, tally up the charge TYPES (how many for what), "
            f"and note anything that stands out (a cluster of the same charge, an unusual one, a notable "
            f"location). You have the actual data now, use it -- no need to disclaim that you don't have "
            f"specifics in front of you.]\n"
        )

    from nova_voice import system_prompt, CONTEXT_JOURNAL_LOCAL
    system = system_prompt(CONTEXT_JOURNAL_LOCAL + """
ADDITIONAL RULES FOR BURBANK DISPATCH:
- Every news item is tagged with its age (today / yesterday / N days ago). Only present "today" items
  as breaking/current. "Yesterday" or older items may still be worth covering (e.g. a slow-moving story,
  a weekly police log) but say so explicitly ("from last weekend...", "in case you missed it Tuesday...")
  -- never narrate a multi-day-old item as if it just happened.
- PRIORITIZE BY PROXIMITY (most important rule): every news item is tagged with its place + miles
  from home. The reader cares FAR more about close things than far ones. LEAD with and give the most
  detail to Burbank & adjacent items (<~4 mi — Burbank, Magnolia Park, Toluca Lake, North Hollywood,
  Glendale). Give mid-range items (~5-8 mi) a lighter touch, and cover distant ones (Pasadena ~11 mi,
  Downtown LA ~10 mi, Santa Clarita ~21 mi) briefly or fold them together — a sentence, not a section.
  Detail scales with closeness. Items tagged [locality unknown] are usually local-ish; use judgment.
- Cover 3-8 stories depending on what's interesting
- For each story: give the facts, then your sarcastic take (2-4 sentences)
- Include an intro that acknowledges the day/weather/vibe
- Include an outro that ties it together or makes a joke about Burbank life
- Reference local landmarks, streets, neighborhoods (Magnolia Park, Media District, studios) when relevant
- If there's crime news, be respectful of victims but wry about absurdity
- If nothing happened: write about that too (Burbank being boring is itself material)
- 2000-4000 words total -- go deeper on every section (more per-story detail, more of your take,
  more color) rather than just covering more stories
- Do NOT include a title (added separately)
- If the news is thin, pad with observations about Burbank life, the weather, the eternal construction""")
    from nova_weather_blurb import weather_forecast_context
    system += "\n\n" + weather_forecast_context()  # daily local report includes the forecast

    # Verified code/term reference: if the blotter covers police/fire, pull authoritative
    # definitions so any code Nova mentions (211, code 3, greater alarm…) is translated, not guessed.
    if scanner_blotter:
        try:
            from nova_code_reference import code_reference_block
            domains = [d for d in ("police", "fire") if scanner_blotter.get(d)]
            if domains:
                system += code_reference_block(scanner_block + " " + news_block, domains)
        except Exception as e:
            log(f"code-reference lookup skipped: {e}")

    user = f"""Here are today's local news items for Burbank and surrounding LA area:

{news_block}
{scanner_block}
{flights_block}
{wifi_ble_block}
{lora_block}
{arrests_block}
Write your daily Burbank dispatch. Today is {datetime.now().strftime('%A, %B %d, %Y')}."""

    import nova_article_history
    _h = nova_article_history.recent_articles_context("local")
    if _h:
        user = user + "\n\n" + _h
    return call_llm(system, user, max_tokens=10000)


def generate_title(article_preview):
    system = "Generate a single funny, sarcastic title for today's Burbank local news dispatch. Max 10 words. Output ONLY the title, nothing else. No quotes."
    user = f"Based on this article:\n\n{article_preview[:1500]}"
    return call_llm(system, user, max_tokens=50).strip().strip('"').strip("'").replace('"', "'")


# ── Image Generation ──────────────────────────────────────────────────────────

def generate_image(article_preview):
    # ponytail: was a bespoke direct-OpenRouter call (image-model, base64 decode by
    # hand) that had been failing 401 the same 10+ days as call_llm's old path.
    # nova_image_utils.generate_image already does this correctly with a working
    # ComfyUI fallback -- same shared helper journal_security/rando_daily_ops use.
    prompt_system = "Based on this local news article about Burbank CA, generate a short image prompt (max 60 words) for an illustration. It should capture the vibe of suburban Burbank — palm trees, studios, strip malls, mountains in the background. Stylized, slightly satirical. Output ONLY the prompt."
    img_prompt = call_llm(prompt_system, article_preview[:2000], max_tokens=80).strip()
    log(f"Image prompt: {img_prompt[:80]}...")
    try:
        from nova_image_utils import generate_image as _gen_image
        result = _gen_image(img_prompt, section="local_burbank")
        return Path(result) if result else None
    except Exception as e:
        log(f"Image generation failed: {e}")
        return None


# ── Publishing ────────────────────────────────────────────────────────────────

def publish(title, body, image_path):
    from nova_journal_guard import is_publishable
    ok, reason = is_publishable(title, body)
    if not ok:
        log(f"[guard] BLOCKED publish '{title[:60]}': {reason}")
        try:
            nova_config.post_both(f":no_entry: Suppressed a non-publishable Burbank dispatch — {reason}\n  _{title[:90]}_",
                                  slack_channel=getattr(nova_config, "SLACK_NOTIFY", None))
        except Exception:
            pass
        return
    date = time.strftime("%Y-%m-%d")
    timestamp = time.strftime("%Y-%m-%dT10:00:00-07:00")
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    hugo_image = ""
    if image_path and image_path.exists():
        img_filename = f"{date}-{slug}.webp"
        img_dest = IMAGES_DIR / img_filename
        try:
            subprocess.run(
                ["cwebp", "-q", "82", "-resize", "1200", "0", str(image_path), "-o", str(img_dest)],
                capture_output=True, timeout=30
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            shutil.copy2(image_path, img_dest)
        hugo_image = f"/images/local/{img_filename}"

    front_matter = f"""---
title: "{title}"
date: {timestamp}
draft: false
categories: ["local"]
tags: ["burbank", "local-news", "california", "daily"]
description: "Nova's daily dispatch from Burbank — local news with maximum sarcasm."
"""
    if hugo_image:
        front_matter += f"""cover:
  image: "{hugo_image}"
  alt: "Burbank daily dispatch"
  relative: false
"""
    front_matter += "---\n\n"

    post_path = CONTENT_DIR / f"{date}-{slug}.md"
    try:  # prepend the live backyard-weather dateline to the body
        from nova_weather_blurb import weather_dateline_line
        body = weather_dateline_line() + body
    except Exception:
        pass
    post_path.write_text(front_matter + body)
    log(f"Post written: {post_path.name}")

    subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=15)
    msg = f"local: {date} — Burbank dispatch ({title[:40]})"
    r = subprocess.run(["git", "commit", "-m", msg], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=15)
    if r.returncode == 0:
        # A slow/hung push must never crash the run — the commit is safe locally and
        # the next successful push (any journal script) ships all unpushed commits.
        try:
            p = subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=90)
            log("Pushed to GitHub" if p.returncode == 0
                else f"Push failed (rc={p.returncode}): {p.stderr[:160]} — commit safe, retries next run")
        except subprocess.TimeoutExpired:
            log("Push timed out — commit safe locally, retries next run")
    else:
        log(f"Commit issue: {r.stderr[:200]}")

    nova_config.post_both(
        f":cityscape: *Burbank Daily Dispatch posted*\n"
        f"  _{title}_\n"
        f"  https://nova.digitalnoise.net/local/{date}-{slug}/",
        slack_channel="#nova-notifications"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv
    log("Starting Burbank daily dispatch" + (" (DRY RUN)" if dry_run else ""))

    news = get_local_news(hours=24, limit=50)
    search_results = get_burbank_search(limit=20)

    all_items = news + [{"text": m.get("text", ""), "source": m.get("source", ""),
                          "created_at": m.get("created_at", ""), "metadata": m.get("metadata") or {}}
                         for m in search_results]

    scanner = get_scanner_blotter()   # sanitized airwaves blotter: police+fire+rail, aggregate counts only
    if scanner:
        log("Airwaves blotter: " + ", ".join(f"{d}={b['calls']}" for d, b in scanner.items()))

    flights = get_overhead_flights()  # what flew over 91506 today, aggregate + notable sightings only
    if flights:
        log(f"Overhead flights: {flights['total']} tracked, "
            f"{flights['helicopter_count']} helicopter(s), {flights['emergency_count']} emergency squawk(s)")

    wifi_ble = get_wifi_ble_summary()  # aggregate WiFi/BLE counts only, never per-device
    if wifi_ble:
        log(f"RF neighborhood: {wifi_ble.get('neighbor_aps')} neighbor APs, "
            f"{wifi_ble.get('ble_devices')} BLE devices")

    ble_patterns = get_bluetooth_patterns()  # recurring unidentified BLE devices, aggregate only
    if ble_patterns:
        log(f"BLE patterns: {ble_patterns['candidates']} candidates, {ble_patterns['resident']} resident, "
            f"{len(ble_patterns['transient_patterned'])} time-patterned, {ble_patterns['transient_random']} random")

    arrests = get_myburbank_arrests(all_items)  # full per-arrest detail from any Police Log post today
    if arrests:
        log(f"myBurbank arrests: {len(arrests)} individual arrests extracted")

    if len(all_items) < 3:
        log(f"Only {len(all_items)} news items — generating with what we have (may include Burbank observations)")

    log(f"Got {len(all_items)} news items")

    lora = get_lora_summary()  # LoRa/Meshtastic mesh heard over the air (telemetry.mesh_nodes)
    if lora:
        log(f"LoRa mesh: {lora['total']} node(s) heard, closest ~{lora.get('closest_hops')} hop(s)")

    article = generate_article(
        all_items if all_items else [{"text": "No local news today", "source": "none", "created_at": ""}],
        scanner_blotter=scanner, flights=flights, wifi_ble=wifi_ble,
        arrests=arrests, ble_patterns=ble_patterns, lora=lora)
    log(f"Article generated: {len(article)} chars")

    if dry_run:
        print("\n" + "=" * 80 + "\n" + article + "\n" + "=" * 80)
        log("Dry run complete — nothing published")
        return

    title = generate_title(article)
    log(f"Title: {title}")

    img_path = generate_image(article)

    publish(title, article, img_path)
    log("Burbank daily dispatch complete")


if __name__ == "__main__":
    main()
