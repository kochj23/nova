#!/usr/bin/env python3
"""
nova_gtnw_world_brief.py — daily "world brief" for the game GTNW, built from Nova's RSS-fed memories.

Input : nova_memories.memories, last 7 days, RSS-ingested rows only (metadata.type 'gov_rss'
        from nova_gov_rss_ingest.py, 'reddit' from nova_reddit_rss_ingest.py). Chunks are grouped
        back into articles by metadata url/title. One SQL pull, everything else in Python.
Scoring: deterministic keywords/co-occurrence, no LLM. Countries are GTNW ids read (read-only) from
        GTNW/Shared/Resources/world_countries.json; names, demonyms, capitals/cities and cyber
        threat-actor names map to those ids.
Output: games DB (192.168.1.5:5433, role gtnw, Keychain nova-games-pg/gtnw) play.world_brief(day, created_at,
        brief jsonb) — one row per day, upserted. Table owned by play_owner; gtnw has SELECT/INSERT/UPDATE.

brief contract (GTNW consumes exactly this — keep keys stable):
  {"generated": ISO8601, "window_days": 7, "items": int,
   "countries": {ISO3: {"military": 0-100, "cyber": 0-100, "mentions": int, "headline": str<=140}},
   "pairs": [{"a": ISO3 actor, "b": ISO3 target, "kind": "war"|"tension"|"cyber",
              "weight": 0-100, "headline": str<=140}]}

  nova_gtnw_world_brief.py              # build + upsert today's row
  nova_gtnw_world_brief.py --dry-run    # print the JSON, write nothing
  nova_gtnw_world_brief.py --selftest   # asserts on alias/APT mapping + scoring

Scheduler: scheduler.yaml task gtnw_world_brief (cron 30 5 * * *). Written by Jordan Koch (via Claude).
"""
import html
import json
import math
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

WINDOW_DAYS = 7
MAX_PAIRS = 40
PAIR_TOP_COUNTRIES = 4      # per item, only the most-mentioned N countries form pairs
COUNTRIES_JSON = Path("/Volumes/Data/oss/kochj23/GTNW/Shared/Resources/world_countries.json")
GAMES_DSN = "host=192.168.1.5 port=5433 dbname=games user=gtnw sslmode=require connect_timeout=15"
RSS_TYPES = ("gov_rss", "reddit")
DEBUG = False

# ── Aliases → GTNW id. Case-insensitive unless listed in CASE_SENSITIVE. ──────────────────────────
ALIASES = {
    "RUS": ["Russia", "Russian", "Russians", "Kremlin", "Moscow", "Putin", "Russian Federation"],
    "CHN": ["China", "Chinese", "Beijing", "PRC", "People's Republic of China", "Xi Jinping", "PLA",
            "People's Liberation Army", "CCP"],
    "IRN": ["Iran", "Iranian", "Iranians", "Tehran", "IRGC", "Islamic Republic"],
    "PRK": ["North Korea", "North Korean", "North Koreans", "DPRK", "Pyongyang", "Kim Jong Un", "Kim Jong-un"],
    "KOR": ["South Korea", "South Korean", "Seoul", "Republic of Korea", "ROK"],
    "USA": ["United States", "United States of America", "US", "U.S.", "USA", "U.S.A.", "America", "American",
            "Americans", "Pentagon", "Washington", "White House", "US military", "Trump administration"],
    "GBR": ["United Kingdom", "UK", "U.K.", "Britain", "British", "Great Britain", "London", "Downing Street",
            "England", "Scotland", "Wales"],
    "UKR": ["Ukraine", "Ukrainian", "Ukrainians", "Kyiv", "Kiev", "Kharkiv", "Odesa", "Odessa", "Zaporizhzhia",
            "Dnipro", "Kherson", "Donetsk", "Luhansk", "Donbas", "Crimea", "Zelensky", "Zelenskyy", "Lviv", "Sumy"],
    "ISR": ["Israel", "Israeli", "Israelis", "IDF", "Tel Aviv", "Jerusalem", "Netanyahu"],
    "PSE": ["Palestine", "Palestinian", "Palestinians", "Gaza", "West Bank", "Hamas", "Ramallah"],
    "TWN": ["Taiwan", "Taiwanese", "Taipei"],
    "JPN": ["Japan", "Japanese", "Tokyo"],
    "IND": ["India", "Indian", "New Delhi"],
    "PAK": ["Pakistan", "Pakistani", "Islamabad"],
    "SYR": ["Syria", "Syrian", "Damascus"],
    "IRQ": ["Iraq", "Iraqi", "Baghdad"],
    "LBN": ["Lebanon", "Lebanese", "Beirut", "Hezbollah"],
    "YEM": ["Yemen", "Yemeni", "Houthi", "Houthis", "Sanaa"],
    "AFG": ["Afghanistan", "Afghan", "Kabul", "Taliban"],
    "BLR": ["Belarus", "Belarusian", "Minsk", "Lukashenko"],
    "DEU": ["Germany", "German", "Berlin", "Bundeswehr"],
    "FRA": ["France", "French", "Paris", "Elysee", "Élysée"],
    "POL": ["Poland", "Polish", "Warsaw"],
    "TUR": ["Turkey", "Turkish", "Türkiye", "Turkiye", "Ankara", "Erdogan"],
    "SAU": ["Saudi Arabia", "Saudi", "Riyadh"],
    "ARE": ["United Arab Emirates", "UAE", "Emirati", "Abu Dhabi", "Dubai"],
    "EGY": ["Egypt", "Egyptian", "Cairo"],
    "VEN": ["Venezuela", "Venezuelan", "Caracas", "Maduro"],
    "CUB": ["Cuba", "Cuban", "Havana"],
    "MEX": ["Mexico", "Mexican"],
    "CAN": ["Canada", "Canadian", "Ottawa"],
    "AUS": ["Australia", "Australian", "Canberra"],
    "ITA": ["Italy", "Italian", "Rome"],
    "ESP": ["Spain", "Spanish", "Madrid"],
    "NLD": ["Netherlands", "Dutch", "The Hague", "Amsterdam"],
    "SWE": ["Sweden", "Swedish", "Stockholm"],
    "FIN": ["Finland", "Finnish", "Helsinki"],
    "NOR": ["Norway", "Norwegian", "Oslo"],
    "DNK": ["Denmark", "Danish", "Copenhagen"],
    "EST": ["Estonia", "Estonian", "Tallinn"],
    "LVA": ["Latvia", "Latvian", "Riga"],
    "LTU": ["Lithuania", "Lithuanian", "Vilnius"],
    "MDA": ["Moldova", "Moldovan", "Chisinau", "Transnistria"],
    "GEO": ["Georgia", "Georgian", "Tbilisi"],
    "ARM": ["Armenia", "Armenian", "Yerevan"],
    "AZE": ["Azerbaijan", "Azerbaijani", "Baku"],
    "SRB": ["Serbia", "Serbian", "Belgrade"],
    "HUN": ["Hungary", "Hungarian", "Budapest", "Orban", "Orbán"],
    "ROU": ["Romania", "Romanian", "Bucharest"],
    "BGR": ["Bulgaria", "Bulgarian", "Sofia"],
    "GRC": ["Greece", "Greek", "Athens"],
    "CZE": ["Czechia", "Czech Republic", "Czech", "Prague"],
    "SVK": ["Slovakia", "Slovak"],
    "KAZ": ["Kazakhstan", "Kazakh", "Astana"],
    "VNM": ["Vietnam", "Vietnamese", "Hanoi"],
    "PHL": ["Philippines", "Philippine", "Filipino", "Manila"],
    "IDN": ["Indonesia", "Indonesian", "Jakarta"],
    "MYS": ["Malaysia", "Malaysian", "Kuala Lumpur"],
    "SGP": ["Singapore", "Singaporean"],
    "THA": ["Thailand", "Thai", "Bangkok"],
    "MMR": ["Myanmar", "Burma", "Burmese", "Naypyidaw"],
    "SDN": ["Sudan", "Sudanese", "Khartoum", "RSF"],
    "SSD": ["South Sudan", "South Sudanese", "Juba"],
    "ETH": ["Ethiopia", "Ethiopian", "Addis Ababa"],
    "SOM": ["Somalia", "Somali", "Mogadishu", "al-Shabaab", "Al Shabaab"],
    "LBY": ["Libya", "Libyan", "Tripoli"],
    "NGA": ["Nigeria", "Nigerian", "Abuja"],
    "MLI": ["Mali", "Malian", "Bamako"],
    "NER": ["Niger", "Nigerien", "Niamey"],
    "BFA": ["Burkina Faso", "Burkinabe"],
    "COD": ["Democratic Republic of the Congo", "Democratic Republic of Congo", "DR Congo", "DRC", "Kinshasa", "M23"],
    "COG": ["Republic of the Congo", "Brazzaville"],
    "BIH": ["Bosnia and Herzegovina", "Bosnia", "Bosnian", "Sarajevo"],
    "CAF": ["Central African Republic"],
    "DOM": ["Dominican Republic"],
    "QAT": ["Qatar", "Qatari", "Doha"],
    "JOR": ["Jordanian", "Amman"],      # bare "Jordan" omitted: too often a person's name
    "BRA": ["Brazil", "Brazilian", "Brasilia"],
    "ARG": ["Argentina", "Argentine", "Buenos Aires"],
    "COL": ["Colombia", "Colombian", "Bogota"],
    "ZAF": ["South Africa", "South African", "Pretoria"],
    "KOS": ["Kosovo"],
    "NZL": ["New Zealand"],
    "IRL": ["Ireland", "Irish", "Dublin"],
    "BEL": ["Belgium", "Belgian", "Brussels"],
    "CHE": ["Switzerland", "Swiss", "Geneva"],
    "AUT": ["Austria", "Austrian", "Vienna"],
    "PRT": ["Portugal", "Portuguese", "Lisbon"],
    "HKG": ["Hong Kong"],
    "GRL": ["Greenland"],
    "CYN": ["Northern Cyprus"],
}
# Cyber threat actors → attributed state. These both mention the state and mark it as the actor.
APT_ACTORS = {
    "RUS": ["APT28", "APT29", "APT44", "Sandworm", "Fancy Bear", "Cozy Bear", "Midnight Blizzard",
            "Forest Blizzard", "Seashell Blizzard", "Turla", "Gamaredon", "Star Blizzard", "Cadet Blizzard",
            "Nobelium", "Callisto", "Killnet", "NoName057(16)", "NoName057", "Cold River", "ColdRiver", "GRU", "SVR", "FSB"],
    "CHN": ["Volt Typhoon", "Salt Typhoon", "Flax Typhoon", "Silk Typhoon", "Linen Typhoon", "Violet Typhoon",
            "APT41", "APT40", "APT31", "APT10", "APT3", "APT15", "Mustang Panda", "Hafnium", "Winnti",
            "UNC3886", "UNC5221", "Earth Lusca", "BlackTech", "Storm-0558", "MSS"],
    "PRK": ["Lazarus", "Lazarus Group", "Kimsuky", "APT38", "APT37", "APT43", "Andariel", "BlueNoroff",
            "Famous Chollima", "ScarCruft", "TraderTraitor", "Jade Sleet", "Moonstone Sleet", "Emerald Sleet"],
    "IRN": ["APT33", "APT34", "APT35", "APT42", "Charming Kitten", "MuddyWater", "OilRig", "Peach Sandstorm",
            "Mint Sandstorm", "CyberAv3ngers", "Pioneer Kitten", "Imperial Kitten", "Agrius", "Handala"],
    "VNM": ["APT32", "OceanLotus"],
    "PAK": ["APT36", "Transparent Tribe", "SideCopy"],
    "IND": ["SideWinder", "Patchwork", "Bitter APT"],
    "BLR": ["Ghostwriter", "UNC1151"],
}
AMBIGUOUS = {"Jordan", "Georgia", "Chad"}
CASE_SENSITIVE = {"US", "U.S.", "USA", "U.S.A.", "UK", "U.K.", "PRC", "PLA", "CCP", "DPRK", "IDF", "IRGC", "UAE",
                  "DRC", "RSF", "ROK", "GRU", "SVR", "FSB", "MSS", "M23", "Turkey", "Georgia", "Niger", "Chad",
                  "Mali", "Thai", "Polish", "Rome", "Sofia", "Riga", "Paris", "Washington", "America", "Cuba",
                  "Sudan", "Lazarus", "Handala", "Patchwork", "Turla"}

MILITARY_TERMS = [
    "strike", "strikes", "struck", "airstrike", "airstrikes", "air strike", "missile", "missiles", "drone", "drones",
    "invasion", "invade", "invaded", "invading", "troops", "soldiers", "shelling", "shelled", "naval", "warship",
    "warships", "mobilization", "mobilisation", "offensive", "counteroffensive", "artillery", "bombing", "bombed",
    "bombardment", "ballistic", "frontline", "front line", "military", "army", "air force", "navy", "war",
    "warfare", "combat", "killed", "casualties", "ceasefire", "occupied", "occupation", "shot down", "attack",
    "attacks", "attacked", "nuclear", "warhead", "fighter jet", "fighter jets", "tank", "tanks", "battalion",
    "brigade", "air defense", "air defence", "sanctions", "blockade", "incursion", "skirmish", "clashes",
]
WAR_TERMS = {"invasion", "invade", "invaded", "invading", "war", "warfare", "offensive", "counteroffensive",
             "frontline", "front line", "occupied", "occupation", "shelling", "shelled", "bombardment", "combat"}
WEAK_MILITARY = {"attack", "attacks", "attacked", "sanctions", "military", "killed", "nuclear", "tank", "tanks"}
CYBER_TERMS = [
    "ransomware", "APT", "breach", "breached", "data breach", "malware", "intrusion", "zero-day", "0-day",
    "zero day", "DDoS", "espionage", "cyberespionage", "cyber espionage", "hack", "hacked", "hacker", "hackers",
    "hacking", "cyberattack", "cyberattacks", "cyber attack", "cyber attacks", "phishing", "spyware", "backdoor",
    "exploit", "exploited", "botnet", "wiper", "state-sponsored", "threat actor", "threat actors", "cyber",
    "infostealer", "supply chain attack", "compromised", "CVE",
]
CYBER_CASE_SENSITIVE = {"APT", "DDoS", "CVE"}
# Count only when a strong cyber term is also present ("missiles breach airspace" is not cyber).
WEAK_CYBER = {"breach", "breached", "compromised", "exploit", "exploited", "cyber", "backdoor", "hack", "hacked",
              "cve", "wiper"}
# Capitals that mostly appear as meeting venues ("a summit in Dublin"): ignored in a venue phrase.
VENUES = {"dublin", "abu dhabi", "dubai", "doha", "geneva", "paris", "london", "washington", "brussels", "the hague",
          "amsterdam", "rome", "madrid", "vienna", "oslo", "stockholm", "helsinki", "copenhagen", "berlin", "warsaw",
          "riyadh", "cairo", "ankara", "new delhi", "tokyo", "canberra", "ottawa", "lisbon", "prague", "budapest",
          "bucharest", "sofia", "athens", "tallinn", "riga", "vilnius", "singapore", "kuala lumpur", "jakarta",
          "bangkok", "manila", "baku", "yerevan", "tbilisi", "astana", "amman", "beijing", "moscow", "minsk"}
_VENUE_BEFORE = re.compile(r"(?:\b(?:in|at|to|from|visit(?:s|ed)?)\s+(?:the\s+|a\s+)?(?:\w+\s+)?)$", re.I)
_VENUE_AFTER = re.compile(r"^\s*(?:summit|talks|meeting|forum|conference|format|process|declaration|on\s+Nov|on\s+\w+\s+\d)", re.I)
# Historical names that would otherwise read as live conflict between two countries.
_HISTORICAL_RE = re.compile(r"\b(?:Vietnam|Korean|Gulf|Iraq|Afghan(?:istan)?|Cold|First World|Second World|World|Civil|"
                            r"Napoleonic|Crimean|Falklands|Six[- ]Day|Yom Kippur)\s+Wars?\b|\bWorld War (?:I|II|One|Two|1|2)\b")
# "<A>('s) [forces|troops|…] <verb>" — the first country in such a phrase is the actor.
ACTOR_VERB_RE = re.compile(
    r"^(?:'s|’s|s)?\s*(?:[A-Z][\w-]+\s+){0,2}?(?:forces|troops|army|military|navy|drones?|missiles?|jets?|"
    r"hackers?|warships?|artillery|troops)?\s*(?:launch(?:es|ed)?\s+|carr(?:y|ies|ied)\s+out\s+)?"
    r"(?:strikes?|struck|attack(?:s|ed)?|hits?|shell(?:s|ed|ing)?|bomb(?:s|ed|ing)?|invade[sd]?|invasion|"
    r"targets?|targeted|hack(?:s|ed)?|breach(?:es|ed)?|seize[sd]?|shoots?\s+down|shot\s+down|fires?|fired|"
    r"kill(?:s|ed)?|advances?|advanced|captures?|captured|raids?|raided|occup(?:y|ies|ied)|threatens?)\b")

_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_EMAIL_RE = re.compile(r"\S+@\S+\.\w+")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_MD_RE = re.compile(r"[*_`#>\[\]|]+")
_LABEL_RE = re.compile(r"^\s*\[[^\]]{1,120}\]\s*")
_BROKEN_ENT_RE = re.compile(r"&#?(x[0-9a-fA-F]{2,5}|\d{2,6});")


def unescape(s):
    """HTML entities, including the ingester's broken '&x2019;' / '&8217;' forms."""
    def fix(m):
        v = m.group(1)
        try:
            return chr(int(v[1:], 16) if v[0] in "xX" else int(v))
        except (ValueError, OverflowError):
            return m.group(0)
    return html.unescape(_BROKEN_ENT_RE.sub(fix, s or ""))


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_gtnw_countries(path=COUNTRIES_JSON):
    data = json.loads(Path(path).read_text())
    return {c["id"]: c["name"] for c in data["countries"]}


def _alt(term, cs):
    pat = re.escape(term).replace(r"\ ", r"[\s-]+")
    return pat if cs else f"(?i:{pat})"


class Matcher:
    """Finds GTNW countries (and APT actors) in text; one regex, longest alternative first."""

    def __init__(self, gtnw):
        self.gtnw = gtnw
        self.lookup = {}   # normalized term -> (iso, is_apt)
        terms = {}
        for name_iso in gtnw.items():
            iso, name = name_iso
            if "." not in name and len(name) > 3:   # skip abbreviated Natural Earth names ("Dem. Rep. Congo")
                terms.setdefault(name, (iso, False))
        for iso, names in ALIASES.items():
            for n in names:
                terms[n] = (iso, False)
        for iso, names in APT_ACTORS.items():
            for n in names:
                terms[n] = (iso, True)
        for amb in AMBIGUOUS:            # GTNW names that are mostly people / US states in English news
            terms.pop(amb, None)
        for t, v in terms.items():
            if v[0] not in gtnw:
                raise ValueError(f"alias {t!r} -> {v[0]} not a GTNW id")
            self.lookup[self._key(t)] = v
        alts = sorted(terms, key=len, reverse=True)
        self.re = re.compile(r"(?<![\w-])(?:" + "|".join(_alt(t, t in CASE_SENSITIVE or self._is_acronym(t))
                                                         for t in alts) + r")(?![\w-])")
        mil = sorted(MILITARY_TERMS, key=len, reverse=True)
        cyb = sorted(CYBER_TERMS, key=len, reverse=True)
        self.mil_re = re.compile(r"(?<![\w-])(?:" + "|".join(_alt(t, False) for t in mil) + r")(?![\w-])")
        self.cyb_re = re.compile(r"(?<![\w-])(?:" + "|".join(_alt(t, t in CYBER_CASE_SENSITIVE) for t in cyb)
                                 + r")(?![\w-])")

    @staticmethod
    def _is_acronym(t):
        return t.isupper() or bool(re.match(r"^[A-Z0-9]+[0-9()]+", t))

    @staticmethod
    def _key(t):
        return re.sub(r"[\s-]+", " ", t).lower()

    def countries(self, text):
        """[(iso, is_apt, end_pos)] in order of appearance."""
        out = []
        for m in self.re.finditer(text):
            k = self._key(m.group(0))
            hit = self.lookup.get(k)
            if hit and k in VENUES and (_VENUE_BEFORE.search(text[max(0, m.start() - 40):m.start()])
                                        or _VENUE_AFTER.match(text[m.end():m.end() + 30])):
                continue
            if hit:
                out.append((hit[0], hit[1], m.end()))
        return out

    def terms(self, regex, text):
        return Counter(self._key(m.group(0)) for m in regex.finditer(text))


def clean_headline(s, limit=140):
    s = _LABEL_RE.sub("", unescape(s))
    s = s.split("\n", 1)[0]
    s = _URL_RE.sub("", s)
    s = _EMAIL_RE.sub("", s)
    s = _PHONE_RE.sub("", s)
    s = re.sub(r"\bu/[\w-]+", "", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = _MD_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" -:–—.")
    if len(s) > limit:
        s = s[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-–—") + "…"
    return s


def score_item(matcher, title, body):
    """Per-item analysis. Returns dict or None if no country."""
    title = unescape(title)
    text = _LABEL_RE.sub("", unescape(body))
    if title and title not in text:
        text = f"{title}. {text}"
    text = _HISTORICAL_RE.sub(" ", text)
    hits = matcher.countries(text)
    if not hits:
        return None
    counts = Counter(iso for iso, _, _ in hits)
    apt_actors = Counter(iso for iso, apt, _ in hits if apt)
    cyb = matcher.terms(matcher.cyb_re, text)
    mil = matcher.terms(matcher.mil_re, text)
    if apt_actors:
        cyb["apt"] += 1
    if any(t not in WEAK_CYBER for t in cyb):   # "attack(s)" in a cyber story is a cyber attack, not a military one
        for w in ("attack", "attacks", "attacked", "compromised"):
            mil.pop(w, None)
    mil_score = sum(min(n, 3) * (0.5 if t in WEAK_MILITARY else 1.0) for t, n in mil.items())
    war_score = sum(min(n, 3) for t, n in mil.items() if t in WAR_TERMS)
    strong = any(t not in WEAK_CYBER for t in cyb)
    cyb_score = sum(min(n, 3) * (0.5 if t in WEAK_CYBER else 1.0) for t, n in cyb.items()) if strong else 0.0
    actor = None
    if apt_actors:
        actor = apt_actors.most_common(1)[0][0]
    else:
        for iso, _, end in hits:
            if ACTOR_VERB_RE.match(text[end:end + 60]):
                actor = iso
                break
    return {"counts": counts, "mil": mil_score, "war": war_score, "cyber": cyb_score, "actor": actor,
            "headline": clean_headline(title or text)}


def build_brief(items, matcher, now=None):
    """items: iterable of (title, body). Returns the brief dict (contract above)."""
    now = now or datetime.now(timezone.utc)
    c_mil, c_cyb, c_ment = Counter(), Counter(), Counter()
    c_head = {}            # iso -> (signal, headline)
    pairs = {}             # frozenset(a,b) -> aggregate
    n = 0
    for title, body in items:
        n += 1
        r = score_item(matcher, title, body)
        if not r:
            continue
        signal = r["mil"] + r["cyber"]
        for iso, k in r["counts"].items():
            c_ment[iso] += 1
            share = 1.0 if (k >= 2 or len(r["counts"]) <= 3) else 0.5   # passing mentions in long lists count half
            c_mil[iso] += min(r["mil"], 6) * share
            c_cyb[iso] += min(r["cyber"], 6) * share * (2.0 if iso == r["actor"] and r["cyber"] else 1.0)
            if signal and r["headline"] and signal > c_head.get(iso, (0, ""))[0]:
                c_head[iso] = (signal, r["headline"])
        if signal <= 0 or len(r["counts"]) < 2:
            continue
        top = [iso for iso, _ in r["counts"].most_common(PAIR_TOP_COUNTRIES)]
        if r["actor"] and r["actor"] not in top:
            top = [r["actor"]] + top[:PAIR_TOP_COUNTRIES - 1]
        for i in range(len(top)):
            for j in range(i + 1, len(top)):
                a, b = top[i], top[j]
                if r["actor"] in (a, b):
                    a, b = (r["actor"], b if a == r["actor"] else a)
                elif r["counts"][b] > r["counts"][a]:
                    a, b = b, a
                key = frozenset((a, b))
                p = pairs.setdefault(key, {"dir": Counter(), "mil": 0.0, "war": 0.0, "cyber": 0.0, "items": 0,
                                           "war_items": 0, "directed": 0, "best_mil": (0, ""),
                                           "best_cyber": (0, "")})
                w = 2 if r["actor"] == a else 1
                p["dir"][(a, b)] += w * signal
                p["mil"] += r["mil"]
                p["war"] += r["war"]
                p["cyber"] += r["cyber"]
                p["items"] += 1
                p["war_items"] += 1 if r["war"] else 0
                p["directed"] += 1 if (r["actor"] == a and r["mil"]) else 0
                for k in ("mil", "cyber"):
                    if r[k] > p["best_" + k][0] and r["headline"]:
                        p["best_" + k] = (r[k], r["headline"])

    def norm(v, vmax):
        return 0 if v <= 0 or vmax <= 0 else max(1, round(100 * math.log1p(v) / math.log1p(vmax)))

    mmax = max(c_mil.values(), default=0)
    cmax = max(c_cyb.values(), default=0)
    countries = {}
    for iso in sorted(set(c_mil) | set(c_cyb)):
        if c_mil[iso] <= 0 and c_cyb[iso] <= 0:
            continue
        countries[iso] = {"military": norm(c_mil[iso], mmax), "cyber": norm(c_cyb[iso], cmax),
                          "mentions": c_ment[iso], "headline": c_head.get(iso, (0, ""))[1]}

    ranked = []
    for key, p in pairs.items():
        raw = p["mil"] + p["cyber"]
        # war = recurring war-term coverage AND one side repeatedly named as the attacker of the other
        if p["cyber"] > p["mil"]:
            kind = "cyber"
        elif p["war_items"] >= 2 and p["directed"] >= max(2, 0.15 * p["items"]) and p["war_items"] >= 0.3 * p["items"]:
            kind = "war"
        else:
            kind = "tension"
        (a, b), _ = p["dir"].most_common(1)[0]
        head = p["best_cyber" if kind == "cyber" else "best_mil"][1] or p["best_mil"][1] or p["best_cyber"][1]
        if DEBUG:
            print(f"DBG {a}->{b} {kind} items={p['items']} war_items={p['war_items']} directed={p['directed']} "
                  f"mil={p['mil']:.0f} war={p['war']:.0f} cyber={p['cyber']:.0f}", file=sys.stderr)
        ranked.append((raw * math.sqrt(p["items"]), a, b, kind, head))
    ranked.sort(key=lambda t: -t[0])
    ranked = ranked[:MAX_PAIRS]
    pmax = ranked[0][0] if ranked else 0
    out_pairs = [{"a": a, "b": b, "kind": kind, "weight": norm(s, pmax), "headline": h}
                 for s, a, b, kind, h in ranked]
    return {"generated": now.isoformat(timespec="seconds"), "window_days": WINDOW_DAYS, "items": n,
            "countries": countries, "pairs": out_pairs}


def fetch_items(days=WINDOW_DAYS):
    """One pull of RSS-ingested chunks, regrouped into articles keyed by url/title."""
    import nova_dsn
    conn = nova_dsn.pg_connect("nova_memories")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT coalesce(nullif(metadata->>'url',''), metadata->>'title', id) AS k,
                       coalesce(metadata->>'title', '') AS title, text
                  FROM memories
                 WHERE created_at > now() - make_interval(days => %s)
                   AND metadata->>'type' = ANY(%s)
                 ORDER BY k, created_at, id""", (days, list(RSS_TYPES)))
            rows = cur.fetchall()
    finally:
        conn.close()
    arts = {}
    for k, title, text in rows:
        a = arts.setdefault(k, [title, []])
        a[1].append(_LABEL_RE.sub("", text))
    return [(t, " ".join(parts)) for t, parts in arts.values()], len(rows)


def write_brief(brief, day=None):
    import psycopg2
    from psycopg2.extras import Json
    pw = subprocess.run(["security", "find-generic-password", "-s", "nova-games-pg", "-a", "gtnw", "-w"],
                        capture_output=True, text=True, check=True).stdout.strip()
    conn = psycopg2.connect(GAMES_DSN, password=pw)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO play.world_brief(day, brief) VALUES (%s, %s)
                           ON CONFLICT (day) DO UPDATE SET brief = EXCLUDED.brief, created_at = now()""",
                        (day or date.today(), Json(brief)))
    finally:
        conn.close()


def summary(brief):
    lines = [f"items={brief['items']} countries={len(brief['countries'])} pairs={len(brief['pairs'])}",
             "top pairs:"]
    for p in brief["pairs"][:8]:
        lines.append(f"  {p['a']}->{p['b']} {p['kind']:7} {p['weight']:3}  {p['headline'][:90]}")
    c = brief["countries"]
    for k in ("military", "cyber"):
        top = sorted(c, key=lambda i: -c[i][k])[:8]
        lines.append(f"top {k}: " + ", ".join(f"{i} {c[i][k]}" for i in top))
    return "\n".join(lines)


def selftest():
    gtnw = load_gtnw_countries()
    m = Matcher(gtnw)
    iso = lambda s: [c for c, _, _ in m.countries(s)]   # noqa: E731
    assert iso("Kremlin says Moscow will respond") == ["RUS", "RUS"]
    assert iso("Beijing and the PRC") == ["CHN", "CHN"]
    assert iso("DPRK missile launch from Pyongyang") == ["PRK", "PRK"]
    assert iso("North Korean hackers") == ["PRK"]
    assert iso("the Pentagon and U.S. forces") == ["USA", "USA"]
    assert iso("let us know") == [], iso("let us know")
    assert iso("Britain and the UK") == ["GBR", "GBR"]
    assert iso("strikes on Kyiv") == ["UKR"]
    assert iso("Gaza ceasefire") == ["PSE"]
    assert iso("Papua New Guinea") == ["PNG"]
    assert iso("South Sudan and Sudan") == ["SSD", "SDN"]
    assert iso("Taipei") == ["TWN"]
    assert iso("Michael Jordan scores") == [] and iso("Atlanta, Georgia") == []
    assert iso("summit in Dublin on Nov. 13") == [] and iso("Ireland and Dublin") == ["IRL", "IRL"]
    for name, want in [("APT28", "RUS"), ("Fancy Bear", "RUS"), ("Cozy Bear", "RUS"), ("Sandworm", "RUS"),
                       ("APT29", "RUS"), ("Volt Typhoon", "CHN"), ("Salt Typhoon", "CHN"), ("APT41", "CHN"),
                       ("APT40", "CHN"), ("Lazarus", "PRK"), ("Kimsuky", "PRK"), ("APT38", "PRK"),
                       ("APT33", "IRN"), ("APT34", "IRN"), ("APT35", "IRN"), ("Charming Kitten", "IRN"),
                       ("MuddyWater", "IRN")]:
        got = m.countries(f"Researchers tie the campaign to {name} operators")
        assert got and got[0][0] == want and got[0][1], (name, got)
    assert all(v in gtnw for v in ALIASES) and all(v in gtnw for v in APT_ACTORS)

    r = score_item(m, "Russia strikes Ukraine with missiles and drones",
                   "Russian forces launched missiles at Kyiv overnight; Ukraine air defense shot down drones.")
    assert r["actor"] == "RUS" and r["mil"] > 3 and r["cyber"] == 0, r
    r = score_item(m, "Volt Typhoon breached US water utilities",
                   "The Chinese state-sponsored group used a zero-day to gain intrusion into US networks.")
    assert r["actor"] == "CHN" and r["cyber"] > 3, r
    assert score_item(m, "Vietnam War memoir", "A veteran of the Vietnam War and the Korean War writes.") is None
    assert clean_headline("Ukraine&x2019;s drones") == "Ukraine\u2019s drones"
    assert clean_headline("**Breaking**: see https://x.com/a now [x]") == "Breaking: see now x"
    assert len(clean_headline("word " * 60)) <= 140

    items = [("Russia strikes Ukraine with missiles", "Russian invasion offensive continues; troops shelling Kharkiv.")] * 3 + \
            [("Volt Typhoon breached US water utilities", "Chinese hackers used a zero-day and malware.")] * 2 + \
            [("Iran threatens Israel", "Tehran warns of missile strikes on Israel.")] + \
            [("Recipe for soup", "Nothing to see here.")]
    b = build_brief(items, m)
    assert b["items"] == 7 and b["window_days"] == WINDOW_DAYS
    p = {(x["a"], x["b"]): x for x in b["pairs"]}
    assert ("RUS", "UKR") in p and p[("RUS", "UKR")]["kind"] == "war", b["pairs"]
    assert ("CHN", "USA") in p and p[("CHN", "USA")]["kind"] == "cyber", b["pairs"]
    assert ("IRN", "ISR") in p and p[("IRN", "ISR")]["kind"] == "tension", b["pairs"]
    assert b["countries"]["CHN"]["cyber"] == 100 and b["countries"]["RUS"]["military"] == 100
    assert all(0 <= c["military"] <= 100 and 0 <= c["cyber"] <= 100 for c in b["countries"].values())
    assert set(b) == {"generated", "window_days", "items", "countries", "pairs"}
    json.dumps(b)
    print("selftest OK")


def main(argv):
    if "--selftest" in argv:
        selftest()
        return 0
    t0 = datetime.now()
    items, nrows = fetch_items()
    brief = build_brief(items, Matcher(load_gtnw_countries()))
    if "--dry-run" in argv:
        print(json.dumps(brief, indent=1, ensure_ascii=False))
        print(summary(brief), file=sys.stderr)
        return 0
    write_brief(brief)
    log(f"rows={nrows} {summary(brief)}")
    log(f"wrote play.world_brief {date.today()} in {(datetime.now() - t0).total_seconds():.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
