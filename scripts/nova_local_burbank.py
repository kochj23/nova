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
PG_DSN = "dbname=nova_memories user=kochj host=192.168.1.6"
NOVA_OPS_DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"

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
        SELECT text, source, created_at
        FROM memories
        WHERE source IN ('local_burbank', 'local_news')
          AND created_at >= %s
          AND LENGTH(text) > 80
        ORDER BY created_at DESC
        LIMIT %s
    """, (cutoff, limit))
    rows = cur.fetchall()
    conn.close()
    return [{"text": r[0], "source": r[1], "created_at": str(r[2])} for r in rows]


def get_burbank_search(limit=30):
    """Semantic search for recent Burbank-related content across all sources."""
    try:
        resp = urllib.request.urlopen(
            f"http://192.168.1.6:18790/recall?q=Burbank+California+local+news+today&n={limit}&source=local_burbank",
            timeout=10
        )
        data = json.loads(resp.read())
        return data.get("memories", [])
    except Exception:
        return []


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
        cur.close()
        conn.close()
    except Exception:
        neighbor_aps = our_aps = open_aps = None

    try:
        conn = psycopg2.connect("host=127.0.0.1 dbname=nova_ops user=kochj")
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
           "ble_devices": ble_devices}


# ── Article Generation ────────────────────────────────────────────────────────

def generate_article(news_items, scanner_blotter=None, flights=None, wifi_ble=None):
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

    scored, dropped = [], 0
    for item in news_items:
        full = item.get("text", "")
        if not is_california(full):
            dropped += 1
            continue
        text = full[:500].replace("\n", " ").strip()
        pd = place_distance(full)
        scored.append({"text": text, "source": item.get("source", "unknown"),
                       "place": pd[0] if pd else None, "mi": pd[1] if pd else None})
    if dropped:
        log(f"Dropped {dropped} non-California news items")
    scored.sort(key=lambda x: (x["mi"] is None, x["mi"] if x["mi"] is not None else 999))  # nearest first
    news_block = ""
    for i, it in enumerate(scored, 1):
        loc = f" [{it['place']}, ~{it['mi']} mi]" if it["mi"] is not None else " [locality unknown]"
        news_block += f"\n{i}. [{it['source']}]{loc} {it['text']}\n"

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
                bits.append(f"{wifi_ble['open_aps']} of them broadcasting with no security at all")
        if wifi_ble.get("ble_devices") is not None:
            bits.append(f"{wifi_ble['ble_devices']} distinct Bluetooth LE devices heard")
        if bits:
            wifi_ble_block = (
                f"\n\n[RF NEIGHBORHOOD — last 24h. {'; '.join(bits)}. Mention this only briefly "
                f"(a sentence, maybe two) as neighborhood color -- this is not the headline. Never "
                f"name a specific network name/BSSID/MAC, just the aggregate numbers.]\n"
            )

    from nova_voice import system_prompt, CONTEXT_JOURNAL_LOCAL
    system = system_prompt(CONTEXT_JOURNAL_LOCAL + """
ADDITIONAL RULES FOR BURBANK DISPATCH:
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
- 1000-2000 words total
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
Write your daily Burbank dispatch. Today is {datetime.now().strftime('%A, %B %d, %Y')}."""

    return call_llm(system, user, max_tokens=6000)


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
    log("Starting Burbank daily dispatch")

    news = get_local_news(hours=24, limit=50)
    search_results = get_burbank_search(limit=20)

    all_items = news + [{"text": m.get("text", ""), "source": m.get("source", ""), "created_at": ""} for m in search_results]

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

    if len(all_items) < 3:
        log(f"Only {len(all_items)} news items — generating with what we have (may include Burbank observations)")

    log(f"Got {len(all_items)} news items")

    article = generate_article(all_items if all_items else [{"text": "No local news today", "source": "none", "created_at": ""}], scanner_blotter=scanner, flights=flights, wifi_ble=wifi_ble)
    log(f"Article generated: {len(article)} chars")

    title = generate_title(article)
    log(f"Title: {title}")

    img_path = generate_image(article)

    publish(title, article, img_path)
    log("Burbank daily dispatch complete")


if __name__ == "__main__":
    main()
