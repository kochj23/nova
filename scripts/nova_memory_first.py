#!/usr/bin/env python3
"""
nova_memory_first.py — Memory-first query middleware for Nova.

RULE: Nova checks her own memories BEFORE anything else. Always.
1.2M+ memories are her lived experience. Going to web or LLM training
data before checking what she actually knows is wrong.

Lookup order:
  1. MEMORY RECALL  — /recall with automatic source filter
  2. MEMORY SEARCH  — /search for names and keywords
  3. LOCAL LLM      — if memory has nothing, reason locally
  4. WEB            — only if memory AND local LLM have nothing
  5. CLOUD          — never for private data, only for conversation

This script is a tool Nova calls on every incoming question. It:
  - Classifies the query to pick the right memory source(s)
  - Runs recall with source filters
  - Runs text search for names/keywords
  - Returns formatted context for Nova to use in her response
  - If nothing found, returns "no memories" so Nova knows to try other sources

Usage (by Nova via exec):
  python3 nova_memory_first.py "what raves do you remember from 2002?"
  python3 nova_memory_first.py "what was my blood pressure last week?"
  python3 nova_memory_first.py "tell me about Sam's latest email"
  python3 nova_memory_first.py --classify "who is CONTACT_NAME_REDACTED?"

Written by Jordan Koch.
"""

import json
import re
import sys
import urllib.request
import urllib.parse
from pathlib import Path

RECALL_URL = "http://127.0.0.1:18790/recall"
SEARCH_URL = "http://127.0.0.1:18790/search"
RECALL_COUNT = 8
SEARCH_COUNT = 5

# ── Source classification ────────────────────────────────────────────────────
# Maps query keywords/patterns to the most likely memory sources.
# Multiple sources are tried in order. First match wins.

SOURCE_RULES = [
    # Personal email / conversations / mailing lists
    {
        "patterns": [
            r"\b(email|e-mail|mail|inbox|wrote|sent|reply|replied|forward)\b",
            r"\b(scr|socal.?raves?|mailing list|stormriders)\b",
            r"\b(remember when|do you remember|what did .+ say|what did .+ write)\b",
            r"\b(conversation|correspondence|letter|message from)\b",
        ],
        "sources": ["email_archive", "email"],
        "label": "personal email",
    },
    # Music / raves / DJs / events
    {
        "patterns": [
            r"\b(rave|raves|party|parties|club|dj|lineup|flyer|promoter|venue)\b",
            r"\b(jungle|drum.?n.?bass|dnb|techno|house|trance|hardcore|idm)\b",
            r"\b(devo|booji.?boy|mothersbaugh|gerald casale|de.?evolution|energy dome|whip it|jocko homo)\b",
            r"\b(turntabl|breakbeat|prodigy|aphex|squarepusher|amen break)\b",
            r"\b(music|song|album|track|band|artist|genre|record|vinyl)\b",
            r"\b(edc|insomniac|bassrush|together as one|nocturnal)\b",
        ],
        "sources": ["music", "email_archive", "socal_rave", "music_history"],
        "label": "music/rave",
    },
    # Health
    {
        "patterns": [
            r"\b(blood pressure|bp|heart rate|hr|pulse|spo2|oxygen|glucose)\b",
            r"\b(weight|bmi|sleep|steps|hrv|vo2|resting heart|walking)\b",
            r"\b(health|medical|doctor|medication|prescription|symptoms)\b",
            r"\b(diabetes|rosacea|depression|anxiety)\b",
        ],
        "sources": ["apple_health", "health"],
        "label": "health",
    },
    # SRE / work knowledge (not work emails — just SRE concepts)
    {
        "patterns": [
            r"\b(sre|site reliability|slo\b|sli\b|sla\b|error.?budgets?|toil)\b",
            r"\b(incident|postmortem|blameless|on.?call|pager|escalat)\b",
            r"\b(kubernetes|k8s|terraform|ci.?cd|deploy|pipeline|rollback|canary)\b",
            r"\b(availability|latency|throughput|four golden signals|burn.?rate)\b",
            r"\b(chaos.?engineer|resilience|circuit.?breaker|load.?shed)\b",
            r"\b(capacity.?plan|change.?manage|mttr|mttd|mtbf|nines)\b",
        ],
        "sources": ["sre"],
        "label": "SRE",
    },
    # People / contacts
    {
        "patterns": [
            r"\b(who is|tell me about|do you know)\s+[A-Z]",
            r"\b(sam|gaston|colette|marey|rockbot|ara|o\.?c\.?)\b",
            r"\b(jason cox|mark ramos|nadia|kevin duane|james tatum|harut)\b",
        ],
        "sources": ["email_archive", "email", "disney"],
        "label": "people",
        "prefer_search": True,  # Use /search instead of /recall for names
    },
    # Cars / Corvette
    {
        "patterns": [
            r"\b(corvette|c6|c7|vette|ls2|ls3|z06|grand sport)\b",
            r"\b(torque|horsepower|engine|transmission|differential)\b",
        ],
        "sources": ["corvette_workshop_manual"],
        "label": "corvette",
    },
    # Home / Burbank / local
    {
        "patterns": [
            r"\b(burbank|glendale|los angeles|la |socal|california)\b",
            r"\b(homekit|home automation|lights|thermostat|scene)\b",
            r"\b(neighbor|neighborhood|house|apartment|rent|mortgage)\b",
        ],
        "sources": ["local", "california", "home_repair"],
        "label": "local/home",
    },
    # Gardening
    {
        "patterns": [
            r"\b(garden|plant|seed|soil|compost|vegetable|tomato|herb)\b",
        ],
        "sources": ["gardening"],
        "label": "gardening",
    },
    # World knowledge / factbook
    {
        "patterns": [
            r"\b(country|nation|capital|population|gdp|continent)\b",
            r"\b(president|prime minister|government|democracy)\b",
        ],
        "sources": ["world_factbook"],
        "label": "world knowledge",
    },
    # Meetings / presentations / all-hands / videos / work projects
    {
        "patterns": [
            r"\b(meeting|all.?hands|presentation|talk|lightning.?talk|conference)\b",
            r"\b(discussed|presented|talked about|agenda|slides)\b",
            r"\b(video|recording|transcript|watch)\b",
            r"\b(migration|deploy|deployment|infrastructure|pipeline|platform)\b",
            r"\b(ddm|matterhorn|gke|kubernetes|terraform|vault|jenkins|gitlab)\b",
            r"\b(chef|ansible|puppet|docker|container|aws|cloud)\b",
            r"\b(wdi|dtss|dcpi|parks|studios|media.?networks)\b",
        ],
        "sources": ["video", "email_archive", "oneonone"],
        "label": "meetings/video/work",
    },
    # Projects / GitHub
    {
        "patterns": [
            r"\b(mlxcode|nmapscanner|rsyncgui|xcode|swift|project)\b",
            r"\b(github|repo|commit|pull request|issue|branch)\b",
        ],
        "sources": ["project_docs"],
        "label": "projects",
    },
    # Cooking / food
    {
        "patterns": [
            r"\b(recipe|cook|bake|ingredient|meal|dinner|lunch|breakfast)\b",
            r"\b(cocktail|drink|whiskey|bourbon|beer|wine)\b",
            r"\b(iron\s*chef|kitchen\s*stadium|cuisine|chef)\b",
        ],
        "sources": ["cooking", "cocktails"],
        "label": "food/drink",
    },
    # Infrastructure / NAS / network
    {
        "patterns": [
            r"\b(nas|synology|network|router|switch|wifi|unifi)\b",
            r"\b(server|port|ip address|dns|firewall|vpn)\b",
        ],
        "sources": ["infrastructure", "networking", "unifi"],
        "label": "infrastructure",
    },
    # Demonology / occult / mythology / folklore
    {
        "patterns": [
            r"\b(demon|demonology|devil|satan|lucifer|hell)\b",
            r"\b(goetia|grimoire|occult|exorcis[mt]|possession)\b",
            r"\b(jinn|djinn|ifrit|oni|yokai|asura|rakshasa)\b",
            r"\b(witch.?craft|witch.?trial|sabbath|familiar)\b",
            r"\b(folklore|mythology|spirit|supernatural)\b",
            r"\b(vodou|voodoo|candombl|shaman)\b",
        ],
        "sources": ["demonology", "music", "document"],
        "label": "demonology/occult",
    },
    # History / connections / civilizations
    {
        "patterns": [
            r"\b(history|historical|ancient|medieval|century|civilization)\b",
            r"\b(connections|james\s*burke|invention|innovation)\b",
            r"\b(war|revolution|empire|dynasty|colonial)\b",
        ],
        "sources": ["history", "world_factbook", "document"],
        "label": "history",
    },
    # Religion / Christianity / theology
    {
        "patterns": [
            r"\b(religion|christian|church|bible|gospel|theology)\b",
            r"\b(catholic|protestant|orthodox|reformation|crusade)\b",
            r"\b(jesus|apostle|saint|monastery|pope|bishop)\b",
        ],
        "sources": ["religion", "demonology", "document"],
        "label": "religion",
    },
    # Trivia / Jeopardy / quiz / general knowledge
    {
        "patterns": [
            r"\b(trivia|jeopardy|quiz|game\s*show|general\s*knowledge)\b",
            r"\b(who\s*(is|was|wrote|invented|discovered))\b",
            r"\b(what\s*(year|country|city|capital|language))\b",
        ],
        "sources": ["trivia", "world_factbook", "history"],
        "label": "trivia",
    },
    # Home repair / renovation / DIY / plumbing / electrical
    {
        "patterns": [
            r"\b(home\s*repair|renovation|remodel|this\s*old\s*house)\b",
            r"\b(plumbing|electrical|wiring|drywall|framing|roofing)\b",
            r"\b(deck|patio|fence|foundation|insulation|siding)\b",
            r"\b(water\s*heater|furnace|hvac|duct|pipe|drain)\b",
            r"\b(tile|flooring|hardwood|cabinet|countertop|backsplash)\b",
            r"\b(contractor|building\s*code|permit|inspection)\b",
        ],
        "sources": ["home_repair", "gardening", "local"],
        "label": "home repair/DIY",
    },
    # Vehicles / cars / motorcycles / planes / builds
    {
        "patterns": [
            r"\b(vehicle|car\s*build|chopper|motorcycle|4x4|off.?road)\b",
            r"\b(racing\s*car|race\s*car|formula|drag\s*racing)\b",
            r"\b(plane|aircraft|aviation|airplane)\b",
            r"\b(engine\s*swap|rebuild|restoration|custom\s*build)\b",
            r"\b(a\s+(car|chopper|plane|4x4|racing\s*car)\s+is\s+(born|reborn))\b",
            r"\b(fabricat|weld|chassis|suspension|turbo|supercharg)\b",
        ],
        "sources": ["vehicles", "corvette_workshop_manual", "video"],
        "label": "vehicles/builds",
    },
    # Comedy / stand-up / comedians
    {
        "patterns": [
            r"\b(comedy|comedian|stand.?up|special|bit|routine|joke)\b",
            r"\b(louis\s*c\.?k|dave\s*chappelle|eddie\s*izzard|lewis\s*black)\b",
            r"\b(patton\s*oswalt|katt\s*williams|kevin\s*smith|bill\s*cosby)\b",
            r"\b(john\s*waters|filthy\s*world)\b",
            r"\b(killing.*softly|chewed\s*up|dress\s*to\s*kill|shameless)\b",
        ],
        "sources": ["comedy", "video", "document"],
        "label": "comedy",
    },
]

# Default sources when no pattern matches — search broadly
# video is always included because transcripts contain diverse topics
DEFAULT_SOURCES = ["video", "email_archive", "music", "document"]


def classify_query(query):
    """Classify a query and return the best memory sources to search."""
    query_lower = query.lower()
    matched = []

    for rule in SOURCE_RULES:
        for pattern in rule["patterns"]:
            if re.search(pattern, query_lower):
                matched.append(rule)
                break

    if matched:
        # Merge sources from all matched rules, preserving order
        sources = []
        labels = []
        prefer_search = False
        for rule in matched:
            for s in rule["sources"]:
                if s not in sources:
                    sources.append(s)
            labels.append(rule["label"])
            if rule.get("prefer_search"):
                prefer_search = True
        return sources, labels, prefer_search

    return DEFAULT_SOURCES, ["general"], False


def recall(query, source=None, n=RECALL_COUNT):
    """Vector semantic search."""
    params = {"q": query, "n": n}
    if source:
        params["source"] = source
    url = f"{RECALL_URL}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
            items = data.get("memories", data) if isinstance(data, dict) else data
            return items if isinstance(items, list) else []
    except Exception:
        return []


def search(query, source=None, n=SEARCH_COUNT):
    """Text keyword search."""
    params = {"q": query, "n": n}
    if source:
        params["source"] = source
    url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
            items = data.get("memories", data.get("results", data)) if isinstance(data, dict) else data
            return items if isinstance(items, list) else []
    except Exception:
        return []


def format_result(item, index):
    """Format a single memory result for Nova."""
    text = item.get("text", "")[:400]
    source = item.get("source", item.get("metadata", {}).get("source", "?"))
    score = item.get("score", item.get("similarity", ""))
    score_str = f" (relevance: {score:.2f})" if isinstance(score, (int, float)) else ""
    return f"[{index}] ({source}{score_str})\n{text}"


def memory_lookup(query):
    """Run the full memory-first lookup pipeline."""
    sources, labels, prefer_search = classify_query(query)

    results = []
    sources_searched = []

    # Step 1: RECALL with source filters
    for source in sources[:4]:  # Max 4 sources to avoid timeout
        items = recall(query, source=source)
        if items:
            results.extend(items[:3])  # Top 3 per source
            sources_searched.append(source)
        if len(results) >= 8:
            break

    # Step 2: SEARCH for names/keywords (especially for people queries)
    if prefer_search or len(results) < 3:
        # Extract potential names or keywords for text search
        search_items = search(query)
        for item in search_items:
            # Don't add duplicates
            item_text = item.get("text", "")[:50]
            if not any(item_text in r.get("text", "")[:50] for r in results):
                results.append(item)

    # Step 3: Broad recall without source filter if still nothing
    if not results:
        results = recall(query, n=RECALL_COUNT)
        sources_searched.append("(broad)")

    # Deduplicate by text prefix
    seen = set()
    unique = []
    for r in results:
        key = r.get("text", "")[:80]
        if key not in seen:
            seen.add(key)
            unique.append(r)
    results = unique[:RECALL_COUNT]

    return results, sources_searched, labels


def main():
    if len(sys.argv) < 2:
        print("Usage: nova_memory_first.py \"your question here\"")
        print("       nova_memory_first.py --classify \"your question\"")
        sys.exit(1)

    if sys.argv[1] == "--classify":
        query = " ".join(sys.argv[2:])
        sources, labels, prefer_search = classify_query(query)
        print(f"Query: {query}")
        print(f"Classification: {', '.join(labels)}")
        print(f"Sources: {', '.join(sources)}")
        print(f"Prefer search: {prefer_search}")
        sys.exit(0)

    query = " ".join(sys.argv[1:])
    results, sources_searched, labels = memory_lookup(query)

    if results:
        print(f"MEMORY FOUND — {len(results)} result(s) from {', '.join(labels)} "
              f"(searched: {', '.join(sources_searched)})")
        print("---")
        for i, r in enumerate(results):
            print(format_result(r, i + 1))
            print("---")
    else:
        print(f"NO MEMORIES FOUND for: {query}")
        print(f"Searched: {', '.join(sources_searched)} ({', '.join(labels)})")
        print("Nova should try: LOCAL LLM reasoning → WEB search → never cloud for private data")


if __name__ == "__main__":
    main()
