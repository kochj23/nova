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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

RECALL_URL       = "http://192.168.1.6:18790/recall"
RECALL_BATCH_URL = "http://192.168.1.6:18790/recall_batch"
SEARCH_URL       = "http://192.168.1.6:18790/search"
RECALL_COUNT = 8
SEARCH_COUNT = 5

# ── Source classification ────────────────────────────────────────────────────
# Maps query keywords/patterns to the most likely memory sources.
# Multiple sources are tried in order. First match wins.

SOURCE_RULES = [
    # iMessage / text messages
    {
        "patterns": [
            r"\b(imessage|text|texted|sms|message from|message to)\b",
            r"\b(i ?message|texts?|iphone message)\b",
        ],
        "sources": ["imessage"],
        "label": "iMessage",
    },
    # Slack conversations
    {
        "patterns": [
            r"\b(slack|channel|thread|posted in|said in slack)\b",
        ],
        "sources": ["slack_general", "slack_conversation", "slack_jordan", "slack_home_alerts", "slack_todo"],
        "label": "slack",
    },
    # Security / cameras / protect
    {
        "patterns": [
            r"\b(camera|security|protect|motion|detect|surveillance|nvr)\b",
            r"\b(front door|alley|patio|exterior|driveway|ring)\b",
        ],
        "sources": ["security", "app_watchdog"],
        "label": "security/cameras",
    },
    # Local knowledge (gangs, radio, comedy, geopolitics, general facts)
    {
        "patterns": [
            r"\b(gang|crip|blood|piru|sure.?o|nort|armenian power|18th street|ms.?13)\b",
            r"\b(kroq|kmdy|comedy radio|groove radio|mars.?fm)\b",
            r"\b(ukraine|russia|putin|zelensky|invasion|crimea|donbas)\b",
            r"\b(who would win|superhero|comic|marvel|dc )\b",
        ],
        "sources": ["local_knowledge", "gang_data"],
        "label": "local knowledge/facts",
    },
    # Calendar / meetings / schedule
    {
        "patterns": [
            r"\b(meeting|calendar|schedule|appointment|standup|1.?on.?1)\b",
            r"\b(today|tomorrow|this week|next week|agenda)\b",
        ],
        "sources": ["calendar", "oneonone", "oneonone_meetings"],
        "label": "calendar/meetings",
    },
    # Punk / hardcore music
    {
        "patterns": [
            r"\b(punk|hardcore|straight edge|black flag|minor threat|bad brains|dead kennedys)\b",
            r"\b(mosh|pit|zine|diy|squat|crust|grind)\b",
        ],
        "sources": ["hardcore_punk", "music", "socal_rave"],
        "label": "punk/hardcore",
    },
    # Reddit / local news
    {
        "patterns": [
            r"\b(reddit|subreddit|r/|posted on reddit|upvote)\b",
        ],
        "sources": ["reddit", "burbank", "local"],
        "label": "reddit/local news",
    },
    # Personal email / conversations / mailing lists
    {
        "patterns": [
            r"\b(email|e-mail|mail|inbox|wrote|sent|reply|replied|forward)\b",
            r"\b(scr|socal.?raves?|mailing list|stormriders)\b",
            r"\b(remember when|do you remember|what did .+ say|what did .+ write)\b",
            r"\b(conversation|correspondence|letter|message from)\b",
        ],
        "sources": ["email_archive", "email", "imessage"],
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
    # Music lyrics / song words
    {
        "patterns": [
            r"\b(lyric|verse|chorus|song\s*words)\b",
            r"\b(what\s*(are|is)\s*the\s*(lyrics|words)\s*(to|of))\b",
        ],
        "sources": ["music_lyrics", "music", "music_history"],
        "label": "lyrics",
    },
    # AppViewX Migration Project / PKI / certificates / work project
    {
        "patterns": [
            r"\b(appviewx|avx|cert\+|migration project)\b",
            r"\b(m?pki|entrust|managed pki|certificate migration)\b",
            r"\b(clearpass|wpa[23]|802\.1x|radius|scep|waep)\b",
            r"\b(pci cert|pci compliance|roc|dkam|dcam)\b",
            r"\b(enrollment server|cert.?lifecycle|cert.?automat)\b",
            r"\b(sectigo|code.?sign|keystone|webvan)\b",
            r"\b(disney.?connect|wlan|ssid|dual.?ca)\b",
            r"\b(david burland|sean sullivan|mark randall|ian funk|laura iwasaki)\b",
            r"\b(joe von schmidt|taher|harut|ganesh mallaya|justice london)\b",
            r"\b(the project|the migration|avx project)\b",
        ],
        "sources": ["work_knowledge"],
        "label": "AppViewX Migration Project",
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
    # Comics / superheroes / who would win battles
    {
        "patterns": [
            r"\b(hulk|superman|batman|spider.?man|thanos|thor|iron man|captain america)\b",
            r"\b(marvel|dc comics|avengers|x.?men|justice league)\b",
            r"\b(omni.?man|invincible|thragg|homelander|viltrumite)\b",
            r"\b(who would win|versus|legendary fight|battle beast|juggernaut|sentry)\b",
            r"\b(ghost rider|colossus|black adam|doctor strange)\b",
        ],
        "sources": ["comic_books", "youtube_transcript", "video"],
        "label": "comics/superheroes",
    },
    # Horror movies / slashers
    {
        "patterns": [
            r"\b(horror|slasher|jason|freddy|michael myers|pennywise|ghostface)\b",
            r"\b(halloween|friday the 13th|nightmare on elm|scream|saw|evil dead)\b",
            r"\b(zombie|vampire|werewolf|demon|exorcist|haunting|paranormal)\b",
            r"\b(art the clown|terrifier|jeepers creepers|vecna|pinhead|hellraiser)\b",
        ],
        "sources": ["horror", "youtube_transcript", "video"],
        "label": "horror",
    },
    # Home / Burbank / local
    {
        "patterns": [
            r"\b(burbank|glendale|los angeles|la |socal|california)\b",
            r"\b(homekit|home automation|lights|thermostat|scene)\b",
            r"\b(neighbor|neighborhood|house|apartment|rent|mortgage)\b",
        ],
        "sources": ["local", "california", "home_repair", "youtube_transcript"],
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
    # Demonology / occult / mythology / folklore / Kabbalah
    {
        "patterns": [
            r"\b(demon|demonology|devil|satan|lucifer|hell)\b",
            r"\b(goetia|grimoire|occult|exorcis[mt]|possession)\b",
            r"\b(jinn|djinn|ifrit|oni|yokai|asura|rakshasa)\b",
            r"\b(witch.?craft|witch.?trial|sabbath|familiar)\b",
            r"\b(folklore|mythology|spirit|supernatural)\b",
            r"\b(vodou|voodoo|candombl|shaman)\b",
            r"\b(kabbalah|kabbalistic|sephir|zohar|sefer|talmud)\b",
            r"\b(solomon|solomonic|lilith|metatron|dybbuk)\b",
            r"\b(angel\s*magic|incantation|amulet|mysticism)\b",
            r"\b(enoch|hekhalot|merkabah|qliphoth|klippot)\b",
        ],
        "sources": ["demonology", "occult", "religion", "document"],
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
    # Drag racing / street racing / NHRA / quarter mile
    {
        "patterns": [
            r"\b(drag\s*rac|street\s*rac|quarter\s*mile|1320|eighth\s*mile)\b",
            r"\b(nhra|pro\s*stock|top\s*fuel|funny\s*car|pro\s*mod)\b",
            r"\b(burnout|launch|staging|christmas\s*tree|reaction\s*time)\b",
            r"\b(roadkill|engine\s*masters|hot\s*rod|muscle\s*car)\b",
            r"\b(nitrous|nos|blower|supercharg|turbo.*boost|dyno)\b",
            r"\b(wheelie|tire\s*shake|hook|traction|slick)\b",
            r"\b(elapsed\s*time|e\.?t\.\s*\d|trap\s*speed|mph\s*\d)\b",
        ],
        "sources": ["drag_racing", "vehicles", "corvette_workshop_manual"],
        "label": "drag/street racing",
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
# These are searched when no specific rule matches the query
DEFAULT_SOURCES = [
    "video", "youtube_transcript", "comic_books",
    "email_archive", "music", "document",
    "imessage", "local_knowledge", "private_document",
    "slack_general", "slack_conversation", "security",
    "burbank", "reddit", "calendar",
]


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


def batch_recall(queries):
    """Send multiple recall queries in one HTTP request to /recall_batch.

    Args:
        queries: list of dicts, each with keys q, n (optional), source (optional).
    Returns:
        list of result dicts, each with keys query and memories.
    """
    payload = json.dumps({"queries": queries}).encode()
    req = urllib.request.Request(
        RECALL_BATCH_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            return data.get("results", [])
    except Exception:
        # Fallback: run individual recalls
        results = []
        for q in queries:
            items = recall(q.get("q", ""), source=q.get("source"), n=q.get("n", RECALL_COUNT))
            results.append({"query": q.get("q", ""), "memories": items})
        return results


def format_result(item, index):
    """Format a single memory result for Nova."""
    text = item.get("text", "")[:400]
    source = item.get("source", item.get("metadata", {}).get("source", "?"))
    score = item.get("score", item.get("similarity", ""))
    score_str = f" (relevance: {score:.2f})" if isinstance(score, (int, float)) else ""
    return f"[{index}] ({source}{score_str})\n{text}"


def memory_lookup(query):
    """Run the full memory-first lookup pipeline.

    Uses /recall_batch to send all source-filtered queries in one request,
    and runs text search in parallel via ThreadPoolExecutor.
    """
    sources, labels, prefer_search = classify_query(query)

    results = []
    sources_searched = []

    # Step 1: BATCH RECALL — all source-filtered queries in one HTTP request
    batch_queries = [{"q": query, "n": 3, "source": s} for s in sources[:4]]
    batch_results = batch_recall(batch_queries)
    for br in batch_results:
        memories = br.get("memories", [])
        if memories:
            results.extend(memories[:3])
            # Track which source returned results
            if memories and memories[0].get("source"):
                src = memories[0]["source"]
                if src not in sources_searched:
                    sources_searched.append(src)
        if len(results) >= 8:
            break

    # Step 2: SEARCH + ALWAYS run broad recall (no source filter) to catch all 1.4M+ memories
    need_search = prefer_search or len(results) < 3

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {}
        if need_search:
            futures["search"] = executor.submit(search, query)
        # Always run a broad (source-free) recall to ensure no memories are invisible
        futures["broad"] = executor.submit(recall, query, None, RECALL_COUNT)

        for key, future in futures.items():
            try:
                items = future.result(timeout=10)
            except Exception:
                items = []
            if key == "search":
                for item in items:
                    item_text = item.get("text", "")[:50]
                    if not any(item_text in r.get("text", "")[:50] for r in results):
                        results.append(item)
            elif key == "broad":
                # Merge broad results, deduplicating against existing
                for item in items:
                    item_text = item.get("text", "")[:50]
                    if not any(item_text in r.get("text", "")[:50] for r in results):
                        results.append(item)
                if items:
                    sources_searched.append("(all sources)")

    # Deduplicate by text prefix
    seen = set()
    unique = []
    for r in results:
        item_text = r.get("text", "")[:50]
        if item_text not in seen:
            seen.add(item_text)
            unique.append(r)
    results = unique[:RECALL_COUNT]

    return results, sources_searched, labels


def _print_active_rules(labels=None):
    """Append active behavioral rules to output so Nova sees them before responding."""
    try:
        from nova_rules import get_active_rules
        rules = get_active_rules()  # All active rules, always
        if not rules:
            return
        print("\n## ACTIVE RULES (behavioral corrections — MUST follow)")
        for r in rules:
            topic_tag = f"[{r['topic']}] " if r['topic'] != 'global' else ""
            print(f"- {topic_tag}{r['rule']}")
    except Exception:
        pass


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

    # Route "what was added/ingested recently" questions to the dedicated tool
    _q_lower = query.lower()
    _recency_signals = ["added", "ingested", "new memories", "recently added",
                        "what was added", "what memories were", "past 24", "past 48",
                        "past 72", "yesterday", "last 24", "last 48", "last 72",
                        "how many memories", "what's new in"]
    if any(sig in _q_lower for sig in _recency_signals) and any(
            w in _q_lower for w in ["memor", "vector", "postgres", "db", "database",
                                     "added", "ingested", "new"]):
        import subprocess as _sp
        hours = 72 if "72" in _q_lower or "3 day" in _q_lower else (
            48 if "48" in _q_lower or "2 day" in _q_lower else 24)
        _result = _sp.run(
            [sys.executable, str(Path(__file__).parent / "nova_recent_memories.py"),
             "--hours", str(hours)],
            capture_output=True, text=True, timeout=30)
        if _result.returncode == 0:
            print("MEMORY INGESTION REPORT (last " + str(hours) + " hours):")
            print(_result.stdout)
            sys.exit(0)

    results, sources_searched, labels = memory_lookup(query)

    if results:
        print("MEMORY FOUND — " + str(len(results)) + " result(s) from " + ', '.join(labels) + " "
              f"(searched: {', '.join(sources_searched)})")
        print("---")
        for i, r in enumerate(results):
            print(format_result(r, i + 1))
            print("---")
    else:
        print(f"NO MEMORIES FOUND for: {query}")
        print(f"Searched: {', '.join(sources_searched)} ({', '.join(labels)})")
        print("Nova should try: LOCAL LLM reasoning → WEB search → never cloud for private data")

    # Append active behavioral rules (corrections + preferences Nova must follow)
    _print_active_rules(labels)


if __name__ == "__main__":
    main()
