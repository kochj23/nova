#!/usr/bin/env python3
"""nova_speaks_narration.py — the narration stage of Nova Speaks: article prose -> text a voice can say well.

Jordan, 2026-10-06, after listening: "91°F" came out as "ninety-one-F", sentences read like a screen not a person,
XTTS growled now and then, and Qapla' went by with no hint it was Klingon. Four fixes, applied per paragraph
before (and around) XTTS:

  D  gloss()        foreign phrases -> "in Klingon, Qapla', which means "success"" (first use per article, phrasebook in
                    PG service_config nova_speaks/phrasebook — Jordan adds entries there); non-Latin script romanized
                    (pinyin) or said as "a phrase in <language>". Never an invented translation.
  B  rewrite()      "write for the ear" with the resident local model (Ollama qwen3:8b on the Studio, think off; the
                    fleet router's "conversation" pool as fallback). Grounding guard: every number and capitalized
                    name in the output must be in the input — else the original is used. Fail open. Cached in PG
                    (nova_speaks_rewrites) by paragraph hash, so re-renders don't recompute.
  A  spoken()       deterministic spoken form: temperatures, units, money, times, dates, ordinals, ranges, versions,
                    CVE ids, IPs, acronyms (letter-spaced so XTTS spells them), symbols, emoji out. Pure function.
  C  BackCheck      every voiced part is transcribed (mlx-whisper on Macs, faster-whisper on Linux) and compared to
                    its text (WER) plus a seconds-per-character sanity bound; a bad part is re-synthesized with another
                    seed (3 tries max, best kept). Stats go to the render's QUALITY line -> nova_speaks_renders.quality.

Pipeline per paragraph: respell(guarded(rewrite(gloss(p)))) -> spoken() -> XTTS.
ponytail: the grounding guard is token-level, not semantic — it stops invented numbers/names, not a subtly changed claim.
"""
import hashlib, json, os, re, sys, time, unicodedata, urllib.request
from concurrent.futures import ThreadPoolExecutor

DSN = os.environ.get("NOVA_OPS_DSN", "dbname=nova_ops user=kochj host=pg-primary.digitalnoise.net port=5432 connect_timeout=5")
OLLAMA = os.environ.get("NOVA_SPEAKS_OLLAMA", "http://192.168.1.6:11434")      # the Studio; qwen3:8b is resident there
ROUTER = os.environ.get("NOVA_SPEAKS_ROUTER", "http://192.168.1.2:37475")      # fleet inference router (OpenAI-compatible)
MODEL = os.environ.get("NOVA_SPEAKS_REWRITE_MODEL", "qwen3:8b")
REWRITE_TIMEOUT = float(os.environ.get("NOVA_SPEAKS_REWRITE_TIMEOUT", "30"))
PROMPT_VERSION = "ear-v1"
WER_MAX = 0.35
MAX_TRIES = 3
XTTS_KW = dict(temperature=0.65, repetition_penalty=7.0, top_k=40, top_p=0.80, length_penalty=1.0)


def log(m): print(f"[nova-speaks {time.strftime('%H:%M:%S')}] {m}", flush=True)


# ════════════════════════════════════ A. spoken form ════════════════════════════════════
_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_SCALES = [(10**12, "trillion"), (10**9, "billion"), (10**6, "million"), (1000, "thousand"), (100, "hundred")]
_ORD = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
_MON_RE = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"


def _card_builtin(n: int) -> str:
    if n < 0: return "minus " + _card_builtin(-n)
    if n < 20: return _ONES[n]
    if n < 100: return _TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")
    for v, w in _SCALES:
        if n >= v:
            hi, lo = divmod(n, v)
            return f"{_card_builtin(hi)} {w}" + (f" {_card_builtin(lo)}" if lo else "")
    return str(n)


def card(n: int) -> str:
    """Cardinal words, American style ('one hundred fifty-six', no 'and', no commas). num2words when installed."""
    try:
        from num2words import num2words
        w = num2words(n, lang="en")
        return re.sub(r"\s+", " ", w.replace(",", "").replace(" and ", " ")).strip()
    except Exception:
        return _card_builtin(n)


def ordinal(n: int) -> str:
    w = card(n)
    head, sep, last = w.rpartition(" ") if " " in w else ("", "", w)
    pre, hy, tail = last.rpartition("-")
    if tail in _ORD: tail = _ORD[tail]
    elif tail.endswith("y"): tail = tail[:-1] + "ieth"
    else: tail += "th"
    return f"{head}{sep}{pre}{hy}{tail}"


def year(n: int) -> str:
    if 2000 <= n <= 2009: return card(n)
    if 1100 <= n <= 2099:
        hi, lo = divmod(n, 100)
        return card(hi) + (" hundred" if lo == 0 else (" oh " + _ONES[lo] if lo < 10 else " " + card(lo)))
    return card(n)


def digits(s: str) -> str:
    return " ".join(_ONES[int(c)] for c in s if c.isdigit())


def num(raw: str) -> str:
    """'2,156' -> two thousand one hundred fifty-six; '3.14' -> three point one four; '007' -> zero zero seven."""
    s = raw.replace(",", "").strip()
    neg = s.startswith(("-", "−"))
    s = s.lstrip("-−+")
    if not s or not re.fullmatch(r"\d*\.?\d*", s) or s == ".": return raw
    whole, _, frac = s.partition(".")
    if len(whole) > 1 and whole.startswith("0"): out = digits(whole)
    else: out = card(int(whole or "0"))
    if frac: out += " point " + digits(frac)
    return ("minus " + out) if neg else out


# acronyms XTTS should say as a word, not spell
SAY_AS_WORD = {"NASA", "NATO", "FEMA", "SCADA", "LIDAR", "RADAR", "LASER", "SWAT", "GIF", "JPEG", "PIN", "ZIP", "SIM", "OPEC",
               "ASCII", "POSIX", "CAPTCHA", "COVID", "UNESCO", "NOAA", "WAN", "LAN", "VLAN", "WLAN", "RAM", "ROM", "CRUD", "AWOL",
               "SNAFU", "FUBAR", "YAML", "JSON", "TOML", "REST", "SIEM", "SOAR", "NIC", "SoC", "SaaS", "PaaS", "IaaS", "LoRa",
               "MIDI", "SWIFT", "UNIX", "LINUX", "CERN", "ICANN", "NAFTA", "OSHA", "DARPA", "INTERPOL", "MOSFET", "TOR", "DEFCON",
               "SPAM", "WIFI", "SONAR", "BASIC", "COBOL", "FORTRAN", "LEGO", "IKEA", "ASAP", "SCUBA", "NIMBY", "YOLO", "FOMO",
               "GIGO", "SARS", "MERS", "AIDS", "UNICEF", "MASH", "FAQ", "OAuth", "CAL", "OPSEC", "INFOSEC", "SIGINT", "OSINT",
               "HUMINT", "DEVCON", "ARPANET", "TASER", "NOVA", "GOES"}
# always letter-spaced, even when the letters happen to spell an English word (LED, MAC, ABS) or sit in a shouted run
ALWAYS_SPELL = {"AI", "IPS", "IDS", "CVE", "UDM", "NAS", "UNAS", "GPU", "CPU", "BLE", "MAC", "SKU", "RSSI", "LED", "API", "DNS",
                "SSH", "VPN", "USB", "HVAC", "NVR", "POE", "SSD", "HDD", "URL", "HTTP", "HTTPS", "TLS", "SSL", "IOT", "EDR", "XDR",
                "MFA", "SSO", "CEO", "CTO", "CFO", "FBI", "CIA", "NSA", "DHS", "CISA", "NIST", "LAPD", "LAFD", "DMV", "UPS",
                "USPS", "NWS", "UV", "TV", "PC", "OS", "IT", "US", "UK", "EU", "UN", "HA", "MQTT", "SMB", "NFS", "ZFS", "RAID",
                "CSV", "PDF", "SQL", "AWS", "GCP", "IBM", "AMD", "LLM", "GPT", "MCP", "RAG", "SRE", "SLA", "SLO", "KPI", "ROI",
                "ETA", "DIY", "LOL", "WTF", "OMG", "BBQ", "CDC", "CAD", "CNC", "PLA", "PETG", "ABS", "AMS", "HDMI", "DVR", "OTA",
                "ISP", "LTE", "GPS", "NFC", "RFID", "SDR", "SDS", "VHF", "UHF", "AQI", "EPA", "IRS", "SEC", "FTC", "FCC", "DOJ",
                "DOD", "NHTSA", "NTSB", "FAA", "ICE", "ATF", "DEA", "MLB", "NFL", "NBA", "CNN", "BBC", "NPR", "ABC", "NBC",
                "CBS", "ESPN", "HBO", "MLX", "TTS", "XTTS", "PG", "DB", "VM", "VMS", "OCR", "QA", "PR", "PRS", "CI", "CD", "UI",
                "UX", "ID", "IDS", "PID", "SAN", "DAS", "SAS", "UPS", "PDU", "IPMI", "BMC", "PHP", "CSS", "HTML", "XML", "AES",
                "RSA", "SHA", "MD", "PGP", "GPG", "DMZ", "VOIP", "SIP", "RTSP", "ONVIF", "NTP", "DHCP", "ARP", "BGP", "OSPF",
                "TCP", "UDP", "ICMP", "IP", "LA", "SF", "NYC", "DC", "USA", "PST", "PDT", "UTC", "EST", "EDT", "GMT", "SUV",
                "EV", "EVS", "AC", "DC", "HP", "LG", "JBL", "BMW", "GM", "VW", "NYT", "WSJ", "AP", "AFP", "SDK", "CLI", "GUI",
                "IDE", "OSS", "FOSS", "ML", "NLP", "AGI", "ASR", "STT", "NPC", "RPG", "FPS", "PS", "TCO", "SMS", "MMS", "RCS"}
# all-caps words that are just emphasis — lowercase them instead of spelling
CAPS_WORDS = {"NOT", "ALL", "THE", "AND", "BUT", "STOP", "NEVER", "EVERY", "YES", "NO", "WHY", "HOW", "WHAT", "ONE", "TWO",
              "NOW", "BAD", "GOOD", "REAL", "DONE", "DEAD", "HELL", "FUCK", "SHIT", "DAMN", "WAS", "ARE", "IS", "THIS", "THAT",
              "WILL", "CAN", "DID", "DOES", "NOTHING", "NONE", "ONLY", "MUST", "VERY", "MORE", "LESS", "BIG", "HUGE", "DOWN",
              "UP", "OFF", "ON", "OUT", "WARNING", "ERROR", "ALERT", "NEW", "LIVE", "FREE", "HOT", "MY", "YOUR", "IT", "WE",
              "YOU", "HE", "SHE", "THEY", "OR", "IF", "SO", "TOO", "OK", "AT", "AN", "OF", "TO", "IN", "BY", "FOR", "WITH",
              "AGAIN", "STILL", "JUST", "ANY", "SOME", "MANY", "MOST", "TRUE", "FALSE", "ZERO", "FIRE", "HELP", "WAIT",
              "WRONG", "RIGHT", "BROKEN", "CRITICAL", "HIGH", "LOW", "MEDIUM", "FAILED", "PASSED", "BLOCKED", "OPEN", "CLOSED",
              "GO", "ME", "US"}
# CAPS_WORDS that are also common acronyms: spell them unless they sit in a shouted run
_ALSO_ACRONYM = {"IT", "US", "OK"}
_WORDS = None


def _is_word(w):
    """English word list (macOS /usr/share/dict/words; Linux hosts get a copy at $TTS_HOME/words)."""
    global _WORDS
    if _WORDS is None:
        _WORDS = set()
        for f in (os.environ.get("NOVA_SPEAKS_WORDS", ""), "/usr/share/dict/words", os.path.join(os.environ.get("TTS_HOME", ""), "words")):
            if f and os.path.isfile(f):
                try:
                    _WORDS = {x.strip().lower() for x in open(f, errors="ignore") if x.strip().islower()}; break
                except OSError: pass
    return w.lower() in _WORDS
_GREEK = {"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "Δ": "delta", "ε": "epsilon", "θ": "theta", "λ": "lambda",
          "μ": "mu", "π": "pi", "σ": "sigma", "Σ": "sigma", "τ": "tau", "φ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega", "Ω": "omega"}
_UNITS = [  # (regex after a number, words) — longest first where prefixes collide
    (r"mph", "miles per hour"), (r"km/h|kph|kmh", "kilometers per hour"), (r"inHg", "inches of mercury"),
    (r"hPa", "hectopascals"), (r"mb(?:ar)?", "millibars"), (r"Gbps", "gigabits per second"), (r"Mbps", "megabits per second"),
    (r"Kbps|kbps", "kilobits per second"), (r"TB", "terabytes"), (r"GB", "gigabytes"), (r"MB", "megabytes"), (r"KB|kB", "kilobytes"),
    (r"GiB", "gibibytes"), (r"MiB", "mebibytes"), (r"GHz", "gigahertz"), (r"MHz", "megahertz"), (r"kHz", "kilohertz"),
    (r"Hz", "hertz"), (r"kWh", "kilowatt hours"), (r"kW", "kilowatts"), (r"MW", "megawatts"), (r"W", "watts"),
    (r"ms", "milliseconds"), (r"µs|μs|us", "microseconds"), (r"ns", "nanoseconds"), (r"sec|secs", "seconds"),
    (r"min|mins", "minutes"), (r"hrs?", "hours"), (r"km", "kilometers"), (r"cm", "centimeters"), (r"mm", "millimeters"),
    (r"mi", "miles"), (r"ft", "feet"), (r"lbs?", "pounds"), (r"oz", "ounces"), (r"kg", "kilograms"), (r"mg", "milligrams"),
    (r"dB", "decibels"), (r"AQI", "A Q I"), (r"ppm", "parts per million"), (r"x", "times"), (r"k|K", "thousand"),
]
_UNIT_RE = re.compile(r"(?<![\w.,])(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s?(" + "|".join(u for u, _ in _UNITS) + r")(?![\w/])")
_UNIT_MAP = [(re.compile(rf"^(?:{u})$"), w) for u, w in _UNITS]
_SCALE_WORDS = {"k": "thousand", "K": "thousand", "M": "million", "B": "billion", "T": "trillion", "bn": "billion", "m": "million"}
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0000200D\U000020E3\U0001F1E6-\U0001F1FF]")
_IPA = re.compile(r"[ɐ-ʯˈˌːʰʷⁿ]")
_COMMON_START = {"the", "it", "this", "that", "and", "but", "so", "i", "in", "we", "you", "he", "she", "they", "there", "here",
                 "what", "when", "which", "then", "now", "that's", "it's", "i'm", "i've", "i'd", "i'll", "a", "an", "if", "as",
                 "on", "at", "for", "with", "by", "of", "to", "my", "our", "your", "their", "his", "her", "its", "no", "not",
                 "yes", "still", "just", "also", "even", "both", "each", "every", "one", "some", "all", "most", "many", "more",
                 "less", "after", "before", "while", "because", "since", "though", "although", "instead", "meanwhile", "today",
                 "tonight", "yesterday", "tomorrow", "this", "these", "those", "nothing", "everything", "something", "nobody",
                 "someone", "anyone", "or", "nor", "yet", "maybe", "perhaps", "first", "second", "third", "finally", "next",
                 "last", "let's", "let", "don't", "doesn't", "didn't", "isn't", "wasn't", "aren't", "weren't", "can't",
                 "won't", "wouldn't", "shouldn't", "couldn't", "there's", "here's", "what's", "who", "why", "how", "where",
                 "well", "okay", "oh", "sure", "right", "again", "only", "except", "either", "neither", "whether", "until",
                 "once", "twice", "about", "around", "over", "under", "between", "through", "during", "without", "within",
                 "against", "toward", "towards", "into", "onto", "from", "out", "up", "down", "off", "do", "does", "did", "is",
                 "are", "was", "were", "be", "been", "being", "have", "has", "had", "will", "would", "should", "could", "can",
                 "may", "might", "must", "shall", "otherwise", "overall", "plus", "together", "basically", "honestly",
                 "frankly", "clearly", "apparently", "anyway", "besides", "however", "still", "then", "thus", "hence",
                 "translation", "meaning", "which", "whose", "whom"}


def fold(s: str) -> str:
    """déjà -> deja, tā -> ta; leaves non-Latin scripts alone (gloss handles those)."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _time(h, m, sec, ap):
    h, m = int(h), int(m)
    if ap: ap = ap.upper()[0] + " M"
    elif h == 0 or h > 12:
        ap = "A M" if h < 12 else "P M"
    if ap and h > 12: h -= 12
    if ap and h == 0: h = 12
    if h > 23 or m > 59: return None
    out = card(h)
    if m == 0: out += " " + (ap if ap else "o'clock")
    else:
        out += " " + ("oh " + _ONES[m] if m < 10 else card(m)) + (" " + ap if ap else "")
    if sec and int(sec): out += f" and {card(int(sec))} seconds"
    return out


def _month(name):
    k = name[:3].lower()
    return next(mn for mn in _MONTHS if mn[:3].lower() == k)


def _spell(tok):
    return " ".join(tok)


def segment(w: str) -> str:
    """dashboardmemorycounthistory -> dashboard memory count history (fewest dictionary words, each ≥3 letters).
    Unknown run-together identifiers are unreadable for XTTS and unrecognizable for the back-check."""
    if len(w) < 9 or _is_word(w) or not _WORDS and not _is_word("the"): return w
    ok = lambda x: _is_word(x) or (x.endswith("s") and _is_word(x[:-1]))
    n = len(w); best = [None] * (n + 1); best[0] = []
    for i in range(1, n + 1):
        for j in range(max(0, i - 20), i - (2 if n >= 12 else 3)):
            if best[j] is not None and ok(w[j:i]) and (best[i] is None or len(best[j]) + 1 < len(best[i])):
                best[i] = best[j] + [w[j:i]]
    return " ".join(best[n]) if best[n] and len(best[n]) <= len(w) // 4 else w


def _acronyms(t: str) -> str:
    def shouted_run(m):
        out = []
        for w in m.group(0).split():
            core = w.rstrip(",.!?")
            out.append(w if core in SAY_AS_WORD or core in ALWAYS_SPELL else w.lower())
        return " ".join(out)
    # 3+ all-caps words in a row is shouting, not acronyms
    t = re.sub(r"\b[A-Z]{2,}[,.!?]?(?:\s+[A-Z]{2,}[,.!?]?){2,}\b", shouted_run, t)

    def one(m):
        w, plural = m.group(1), m.group(2) or ""
        if w in ALWAYS_SPELL: return _spell(w) + (" s" if plural else "")
        if w in SAY_AS_WORD: return (w.capitalize() if len(w) > 3 else w) + plural
        if w == "OK": return "okay"
        if w in CAPS_WORDS or (len(w) >= 3 and _is_word(w + plural)): return (w + plural).lower()
        if len(w) >= 6 and not plural: return w.capitalize()               # long all-caps words are emphasis
        return _spell(w) + (" s" if plural else "")
    return re.sub(r"\b([A-Z]{2,6})(s)?\b", one, t)


def spoken(text: str) -> str:
    """Deterministic spoken form of one paragraph. Pure; idempotent on its own output."""
    if not text: return ""
    t = unicodedata.normalize("NFC", text)
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("…", "...").replace(" ", " ")
    t = _EMOJI.sub("", t)
    t = re.sub(r"/[^/\s]*" + _IPA.pattern + r"[^/]*/", "", t)                              # IPA transcriptions
    t = re.sub(r"\[[^\]]*" + _IPA.pattern + r"[^\]]*\]", "", t)
    t = re.sub(r"https?://\S+|www\.\S+", "", t)
    t = re.sub(r"[*_`]+", "", t)
    t = re.sub("[" + "".join(_GREEK) + "]", lambda m: " " + _GREEK[m.group(0)] + " ", t)
    t = t.replace("²", " squared").replace("³", " cubed")
    t = fold(t)
    # specials
    t = re.sub(r"\bC\+\+", "C plus plus", t)
    t = re.sub(r"\bIPv([46])\b", lambda m: "I P v " + card(int(m.group(1))), t)
    t = re.sub(r"\b24/7\b", "twenty-four seven", t)
    t = re.sub(r"\bDDoS\b", "D dos", t)
    t = re.sub(r"\biOS\b", "eye O S", t); t = re.sub(r"\bmacOS\b", "mac O S", t); t = re.sub(r"\btvOS\b", "T V O S", t)
    t = re.sub(r"\bAI's\b", "A I's", t)
    # CVE ids, IPs (+ optional port / CIDR), versions
    t = re.sub(r"\bCVE-(\d{4})-(\d{4,7})\b", lambda m: f"C V E {year(int(m.group(1)))}, {digits(m.group(2))}", t)

    def ip(m):
        parts = m.group(1, 2, 3, 4)
        if any(int(p) > 255 for p in parts): return m.group(0)
        out = " dot ".join(digits(p) for p in parts)
        if m.group(5): out += " port " + card(int(m.group(5)))
        if m.group(6): out += " slash " + card(int(m.group(6)))
        return out
    t = re.sub(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?::(\d{1,5}))?(?:/(\d{1,2}))?(?![\w.]*\d)", ip, t)
    t = re.sub(r"(?<![\w.])([vV])?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?![\w.]*\d)",
               lambda m: ("version " if m.group(1) else "") + " point ".join(card(int(g)) for g in m.group(2, 3, 4, 5) if g is not None), t)
    t = re.sub(r"(?<![\w.])[vV](\d+)(?:\.(\d+))?\b", lambda m: "version " + card(int(m.group(1))) + (" point " + digits(m.group(2)) if m.group(2) else ""), t)
    # dates
    t = re.sub(r"(?<=\d)Z\b", " UTC", t)
    t = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::\d{2})?)?\b",
               lambda m: (f"{_MONTHS[int(m.group(2)) - 1]} {ordinal(int(m.group(3)))}, {year(int(m.group(1)))}"
                          if 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 31 else m.group(0))
               + (f" at {_time(m.group(4), m.group(5), None, None)}" if m.group(4) else ""), t)
    t = re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b",
               lambda m: (f"{_MONTHS[int(m.group(1)) - 1]} {ordinal(int(m.group(2)))}, {year(int(m.group(3)) if len(m.group(3)) == 4 else 2000 + int(m.group(3)))}"
                          if 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31 else m.group(0)), t)
    t = re.sub(r"\b" + _MON_RE + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4})\b)?",
               lambda m: f"{_month(m.group(1))} {ordinal(int(m.group(2)))}" + (f", {year(int(m.group(3)))}" if m.group(3) else "")
               if 1 <= int(m.group(2)) <= 31 else m.group(0), t)
    # times
    ampm = r"([AaPp])(?:\.\s?[Mm]\.|\s?[Mm](?![\w]))"
    t = re.sub(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\s*" + ampm, lambda m: _time(*m.group(1, 2, 3, 4)) or m.group(0), t)
    t = re.sub(r"\b(\d{1,2})\s*" + ampm, lambda m: _time(m.group(1), "0", None, m.group(2)) if int(m.group(1)) <= 12 else m.group(0), t)
    t = re.sub(r"(?<![\w:.])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\w:]|\.\d)", lambda m: _time(*m.group(1, 2, 3), None) or m.group(0), t)
    t = re.sub(r"\b(PDT|PST|PT|UTC|GMT|EDT|EST|ET)\b(?=[\s,.;)]|$)", lambda m: {"PT": "Pacific", "ET": "Eastern"}.get(m.group(1), _spell(m.group(1))), t)
    # codes and identifiers: EEEA56F8 / NL8ZC / !9633912f are spelled out; telemetry.activity -> telemetry dot activity
    t = re.sub(r"\be\.g\.,?", "for example,", t); t = re.sub(r"\bi\.e\.,?", "that is,", t)
    t = re.sub(r"!(?=[0-9a-f]{6,}\b)", "", t)
    def code(m):
        w = m.group(0)
        if len(re.findall(r"[A-Za-z]+|\d+", w)) < 3 and not re.fullmatch(r"[0-9a-f]{6,}", w): return w
        if re.fullmatch(r"[a-z]+\d+[a-z]+", w): return re.sub(r"(\d+)", r" \1 ", w)          # sds200calls -> sds 200 calls
        return " ".join(_ONES[int(c)] if c.isdigit() else c.upper() for c in w)
    t = re.sub(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{4,}\b", code, t)
    t = re.sub(r"(?<=[a-z])\.(?=[a-z]{2,})", " dot ", t)
    t = re.sub(r"\b[a-z]{9,}\b", lambda m: segment(m.group(0)), t)
    t = re.sub(r"(?<![\w.])\.(\d{1,3})\b(?!\.\d)", lambda m: "dot " + card(int(m.group(1))), t)
    # temperatures (ranges first)
    deg = lambda u: " degrees" + (" Celsius" if u and u.upper() == "C" else "")
    t = re.sub(r"(-?\d+(?:\.\d+)?)\s*°?\s*(?:–|—|-|to)\s*(-?\d+(?:\.\d+)?)\s*°\s*([FC])?(?![a-zA-Z])",
               lambda m: f"{num(m.group(1))} to {num(m.group(2))}{deg(m.group(3))}", t)
    t = re.sub(r"(-?\d+(?:\.\d+)?)\s*(?:°\s*([FC])?|℉|℃)(?![a-zA-Z])", lambda m: num(m.group(1)) + deg(m.group(2) or ("C" if "℃" in m.group(0) else None)), t)
    t = re.sub(r"(?<![\w.])(-?\d{2,3})\s?F\b", lambda m: num(m.group(1)) + " degrees", t)
    t = re.sub(r"\bdegrees (?:F|Fahrenheit)\b", "degrees", t)
    # money
    def money(m):
        sym, amt, scale = m.group(1), m.group(2), m.group(3)
        unit = {"$": "dollars", "€": "euros", "£": "pounds"}[sym]
        if scale:
            sw = _SCALE_WORDS.get(scale, scale.lower())
            return f"{num(amt)} {sw} {unit}"
        whole, _, cents = amt.replace(",", "").partition(".")
        w = card(int(whole or "0"))
        one = unit[:-1] if whole == "1" else unit
        if cents and len(cents) == 2 and int(cents):
            return f"{w} {one} and {card(int(cents))} cent" + ("" if int(cents) == 1 else "s") if sym == "$" else f"{w} {one} {card(int(cents))}"
        return f"{w} {one}"
    t = re.sub(r"([$€£])((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?:\s?(k|K|M|B|T|bn|thousand|million|billion|trillion)\b)?", money, t)
    # percent, units, multipliers
    t = re.sub(r"\b([48])K\b", lambda m: card(int(m.group(1))) + " K", t)              # 4K / 8K video
    t = re.sub(r"(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s?%", lambda m: num(m.group(1)) + " percent", t)
    t = re.sub(r"(?<![\w.,])((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s?(?:–|—|-)\s?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s?%", lambda m: f"{num(m.group(1))} to {num(m.group(2))} percent", t)

    def unit(m):
        u = m.group(2)
        w = next(w for rx, w in _UNIT_MAP if rx.match(u))
        n = m.group(1)
        if w in ("times", "thousand"): return f"{num(n)} {w}"
        if n == "1" and w not in ("hertz",):
            first, _, rest = w.partition(" ")
            first = {"feet": "foot", "inches": "inch"}.get(first, first[:-1] if first.endswith("s") else first)
            w = (first + " " + rest).strip()
        return f"{num(n)} {w}"
    t = _UNIT_RE.sub(unit, t)
    # ordinals, ranges, fractions
    t = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", lambda m: ordinal(int(m.group(1))), t)
    t = re.sub(r"(?<![\w.,/-])((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s?(?:–|—|-)\s?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?![\w.]*\d|-)",
               lambda m: f"{_numtok(m.group(1))} to {_numtok(m.group(2))}", t)
    fr = {(1, 2): "one half", (1, 3): "one third", (2, 3): "two thirds", (1, 4): "one quarter", (3, 4): "three quarters"}
    t = re.sub(r"(?<![\w./])(\d+)/(\d+)(?![\w/])", lambda m: fr.get((int(m.group(1)), int(m.group(2))), f"{num(m.group(1))} out of {num(m.group(2))}"), t)
    # money-free scale suffixes: 2.5M users
    t = re.sub(r"(?<![\w.])(\d+(?:\.\d+)?)(M|B|bn)\b", lambda m: f"{num(m.group(1))} {_SCALE_WORDS[m.group(2)]}", t)
    # letter+digit tokens: M4 -> M four, core2 -> core two, x86 -> x eighty-six; digit+letters: 3D -> three D
    t = re.sub(r"\b([A-Za-z]+)(\d+)\b", lambda m: f"{m.group(1)} {_numtok(m.group(2))}", t)
    t = re.sub(r"\b(\d+)([A-Z]{1,3})\b", lambda m: f"{_numtok(m.group(1))} {_spell(m.group(2))}", t)
    # symbols
    t = re.sub(r"\s*(?:→|->|⇒|=>|⟶)\s*", " to ", t)
    t = re.sub(r"\s*(?:←|<-)\s*", " from ", t)
    t = re.sub(r"\s*&\s*", " and ", t)
    t = re.sub(r"\s*(?:±|\+/-)\s*", " plus or minus ", t)
    t = re.sub(r"(?<![\w])[~≈]\s*(?=\d)", "about ", t)
    t = re.sub(r"\s*[~≈]\s*", " about ", t)
    t = re.sub(r"\s*×\s*", " times ", t)
    t = re.sub(r"\s*≥\s*", " at least ", t); t = re.sub(r"\s*≤\s*", " at most ", t)
    t = re.sub(r"\s+>\s+(?=\d)", " more than ", t); t = re.sub(r"\s+<\s+(?=\d)", " less than ", t)
    t = re.sub(r"(?<!\w)\+(?=\d)", "plus ", t)
    t = re.sub(r"\s+\+\s+", " plus ", t)
    t = re.sub(r"\s+=\s+", " equals ", t)
    t = re.sub(r"#(?=\d)", "number ", t); t = re.sub(r"#(?=\w)", "", t)
    t = re.sub(r"(?<=\w)@(?=\w)", " at ", t); t = re.sub(r"\s@\s", " at ", t)
    t = re.sub(r"\band/or\b", "and or", t)
    t = re.sub(r"\b([A-Za-z][a-z]+)/([a-z]+)\b", lambda m: f"{m.group(1)} or {m.group(2)}", t)
    t = re.sub(r"(?<=\w)/(?=\w)", " slash ", t)
    t = re.sub(r"\s*\|\s*", ", ", t)
    # numbers that are left: years, then everything else
    t = re.sub(r"(?<![\w.,])(1[1-9]\d\d|20\d\d)(?![\w.,]*\d)(?!s\b)", lambda m: year(int(m.group(1))), t)
    t = re.sub(r"(?<![\w.,])(1[1-9]\d\d|20\d\d)s\b", lambda m: re.sub(r"y$", "ie", year(int(m.group(1)))) + "s", t)
    t = re.sub(r"(?<![\w.,])(\d+0)s\b", lambda m: re.sub(r"y$", "ie", card(int(m.group(1)))) + "s", t)
    t = re.sub(r"(?<![\w.])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\w])", lambda m: _numtok(m.group(0)), t)
    t = re.sub(r"\d+", lambda m: num(m.group(0)), t)                      # anything still glued to letters
    # roman numerals after a capitalized word: World War II, Part III
    t = re.sub(r"\b([A-Z][a-z]+) (II|III|IV|VI|VII|VIII|IX)\b",
               lambda m: m.group(1) + " " + {"II": "two", "III": "three", "IV": "four", "VI": "six", "VII": "seven", "VIII": "eight", "IX": "nine"}[m.group(2)], t)
    t = _acronyms(t)
    # punctuation for the ear: dashes/parentheticals -> commas, semicolons -> periods
    t = re.sub(r"\s*[—–]\s*|\s+-\s+|\s*--\s*", ", ", t)
    t = re.sub(r"\s*\(\s*", ", ", t); t = re.sub(r"\s*\)\s*", ", ", t)
    t = re.sub(r"\s*\[\s*|\s*\]\s*", " ", t)
    t = re.sub(r"\s*;\s*", ". ", t)
    t = re.sub(r"[^\x00-\x7F]", "", t)                                     # nothing XTTS can't say
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([,.!?:])", r"\1", t)
    t = re.sub(r",(\s*,)+", ",", t)
    t = re.sub(r"([.!?:]),", r"\1", t)
    t = re.sub(r"^[,\s]+|[,\s]+$", "", t)
    t = re.sub(r",\s*([.!?])", r"\1", t)
    t = re.sub(r"((?<!\.)[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
    return t.strip()


def _numtok(s: str) -> str:
    s2 = s.replace(",", "")
    if re.fullmatch(r"(1[1-9]\d\d|20\d\d)", s2) and "," not in s: return year(int(s2))
    return num(s)


# ════════════════════════════════════ D. foreign phrases ════════════════════════════════════
# Seed. The live copy is PG service_config (service='nova_speaks', key='phrasebook'); Jordan adds entries there.
# say = how XTTS (English) should pronounce it. Borrowed phrases English already owns (per se, de facto, status quo,
# deja vu, schadenfreude) are deliberately absent: they need no introduction.
SEED_PHRASEBOOK = [
    {"phrase": "Qapla'", "aliases": ["Qapla", "Qaplah"], "language": "Klingon", "meaning": "success", "say": "Kapla"},
    {"phrase": "nuqneH", "language": "Klingon", "meaning": "what do you want", "say": "nookneh"},
    {"phrase": "Heghlu'meH QaQ jajvam", "language": "Klingon", "meaning": "today is a good day to die", "say": "Heghloomeh kahk jajvam"},
    {"phrase": "Hab SoSlI' Quch", "language": "Klingon", "meaning": "your mother has a smooth forehead", "say": "Hab soshlee kooch"},
    {"phrase": "batlh", "language": "Klingon", "meaning": "honor", "say": "batl"},
    {"phrase": "majQa'", "language": "Klingon", "meaning": "well done", "say": "majkah"},
    {"phrase": "petaQ", "language": "Klingon", "meaning": "a Klingon insult", "say": "petakh"},
    {"phrase": "tlhIngan maH", "language": "Klingon", "meaning": "we are Klingons", "say": "Klingon mah"},
    {"phrase": "Dif-tor heh smusma", "language": "Vulcan", "meaning": "live long and prosper", "say": "Diftor heh smoosma"},
    {"phrase": "gorram", "aliases": ["gorramit", "gorramn"], "language": "the Firefly crew's slang", "meaning": "goddamn", "say": "gorram", "context": ["Firefly", "Serenity"]},
    {"phrase": "ta ma de", "aliases": ["tā mā de", "tah mah duh", "ta-ma-de"], "language": "Mandarin", "meaning": "damn it", "say": "tah mah duh"},
    {"phrase": "wo de ma", "aliases": ["wǒ de mā", "wuh duh mah"], "language": "Mandarin", "meaning": "mother of god", "say": "woh duh mah"},
    {"phrase": "gou shi", "aliases": ["gǒu shǐ", "go se", "goh se"], "language": "Mandarin", "meaning": "dog crap", "say": "go shir"},
    {"phrase": "bi zui", "aliases": ["bì zuǐ"], "language": "Mandarin", "meaning": "shut up", "say": "bee dzway"},
    {"phrase": "dong ma", "aliases": ["dǒng ma"], "language": "Mandarin", "meaning": "understand", "say": "dong mah"},
    {"phrase": "tian xiao de", "aliases": ["tiān xiǎo de"], "language": "Mandarin", "meaning": "heaven knows", "say": "tyen shyow duh"},
    {"phrase": "zao gao", "aliases": ["zāo gāo"], "language": "Mandarin", "meaning": "crap", "say": "dzow gow"},
    {"phrase": "xie xie", "aliases": ["xiè xie"], "language": "Mandarin", "meaning": "thank you", "say": "shyeh shyeh"},
    {"phrase": "ni hao", "aliases": ["nǐ hǎo"], "language": "Mandarin", "meaning": "hello", "say": "nee how"},
    {"phrase": "bao bei", "aliases": ["bǎo bèi"], "language": "Mandarin", "meaning": "sweetheart", "say": "bow bay"},
    {"phrase": "hwoon dahn", "aliases": ["huai dan", "huài dàn"], "language": "Mandarin", "meaning": "bastard", "say": "hwhy dahn"},
    {"phrase": "我的妈", "language": "Mandarin", "meaning": "mother of god", "say": "woh duh mah"},
    {"phrase": "他妈的", "language": "Mandarin", "meaning": "damn it", "say": "tah mah duh"},
    {"phrase": "Valar morghulis", "language": "High Valyrian", "meaning": "all men must die", "say": "Valar morghoolis"},
    {"phrase": "Valar dohaeris", "language": "High Valyrian", "meaning": "all men must serve", "say": "Valar doh-hairis"},
    {"phrase": "Dracarys", "language": "High Valyrian", "meaning": "dragonfire", "say": "Drakarris"},
    {"phrase": "Me nem nesa", "language": "Dothraki", "meaning": "it is known", "say": "Meh nem nessa"},
    {"phrase": "Mae govannen", "language": "Sindarin Elvish", "meaning": "well met", "say": "My govannen"},
    {"phrase": "mellon", "language": "Sindarin Elvish", "meaning": "friend", "say": "mellon", "case_sensitive": True},
    {"phrase": "Namárië", "aliases": ["Namarie"], "language": "Quenya Elvish", "meaning": "farewell", "say": "Nah-mah-ree-eh"},
    {"phrase": "Fus Ro Dah", "aliases": ["Fus Ro Dah!"], "language": "Dovahzul, the dragon tongue of Skyrim", "meaning": "force, balance, push", "say": "Foos Roh Dah"},
    {"phrase": "Krosis", "language": "Dovahzul, the dragon tongue of Skyrim", "meaning": "sorrow", "say": "Krosis"},
    {"phrase": "horrorshow", "language": "Nadsat, the slang of A Clockwork Orange", "meaning": "good", "say": "horrorshow", "context": ["droog", "Nadsat", "Clockwork"]},
    {"phrase": "La Llorona", "language": "Spanish", "meaning": "the weeping woman", "say": "La Yorona"},
    {"phrase": "hasta la vista", "language": "Spanish", "meaning": "see you later", "say": "asta la vista"},
    {"phrase": "mi casa es su casa", "language": "Spanish", "meaning": "my house is your house", "say": "mee casa es soo casa"},
    {"phrase": "vaya con dios", "language": "Spanish", "meaning": "go with God", "say": "vaya con dee-ose"},
    {"phrase": "ay caramba", "language": "Spanish", "meaning": "good grief", "say": "eye caramba"},
    {"phrase": "memento mori", "language": "Latin", "meaning": "remember that you will die", "say": "memento mori"},
    {"phrase": "carpe diem", "language": "Latin", "meaning": "seize the day", "say": "carpay dee-em"},
    {"phrase": "alea iacta est", "language": "Latin", "meaning": "the die is cast", "say": "alaya yakta est"},
    {"phrase": "veni, vidi, vici", "aliases": ["veni vidi vici"], "language": "Latin", "meaning": "I came, I saw, I conquered", "say": "wenee, weedee, weekee"},
    {"phrase": "quis custodiet ipsos custodes", "language": "Latin", "meaning": "who watches the watchmen", "say": "kwis custodee-et ipsos custodays"},
    {"phrase": "in vino veritas", "language": "Latin", "meaning": "in wine there is truth", "say": "in veeno veritas"},
    {"phrase": "et tu, Brute", "aliases": ["et tu Brute"], "language": "Latin", "meaning": "and you, Brutus", "say": "et too, Brootay"},
    {"phrase": "cogito, ergo sum", "aliases": ["cogito ergo sum"], "language": "Latin", "meaning": "I think, therefore I am", "say": "cogito, ergo sum"},
    {"phrase": "c'est la vie", "language": "French", "meaning": "that's life", "say": "say la vee"},
    {"phrase": "je ne sais quoi", "language": "French", "meaning": "a certain something", "say": "zhuh nuh say kwah"},
    {"phrase": "raison d'être", "aliases": ["raison d'etre"], "language": "French", "meaning": "reason for being", "say": "ray-zon detra"},
    {"phrase": "fait accompli", "language": "French", "meaning": "a done deal", "say": "fet accomplee"},
]
_SCRIPTS = [  # (regex, language, romanizer)
    (r"[一-鿿㐀-䶿]+", "Mandarin", "pinyin"),
    (r"[぀-ヿ]+[一-鿿぀-ヿ]*", "Japanese", None),
    (r"[가-힯ᄀ-ᇿ]+", "Korean", None),
    (r"[Ѐ-ӿ]+(?:[\s,.'-]+[Ѐ-ӿ]+)*", "Russian", None),
    (r"[؀-ۿ]+(?:\s+[؀-ۿ]+)*", "Arabic", None),
    (r"[֐-׿]+(?:\s+[֐-׿]+)*", "Hebrew", None),
    (r"[ऀ-ॿ]+(?:\s+[ऀ-ॿ]+)*", "Hindi", None),
    (r"[฀-๿]+", "Thai", None),
    (r"[Ͱ-Ͽ]{2,}(?:\s+[Ͱ-Ͽ]+)*", "Greek", None),
]


def load_phrasebook():
    """PG copy if reachable (seeding it on first use), else the built-in seed."""
    try:
        import psycopg2
        c = psycopg2.connect(DSN); c.autocommit = True; cur = c.cursor()
        cur.execute("SELECT value FROM service_config WHERE service='nova_speaks' AND key='phrasebook'")
        r = cur.fetchone()
        if r: return r[0]
        cur.execute("INSERT INTO service_config (service, key, value, updated_by) VALUES ('nova_speaks','phrasebook',%s,'nova_speaks') "
                    "ON CONFLICT DO NOTHING", (json.dumps(SEED_PHRASEBOOK),))
    except Exception as e:
        log(f"phrasebook from seed ({type(e).__name__})")
    return SEED_PHRASEBOOK


def _phrase_re(p):
    forms = [p["phrase"]] + list(p.get("aliases", []))
    alts = sorted({fold(f) for f in forms}, key=len, reverse=True)
    body = "|".join(re.escape(a).replace(r"\ ", r"[\s,-]+") for a in alts)
    return re.compile(r"(?<![\w'])(?:" + body + r")(?![\w])['!]?", 0 if p.get("case_sensitive") else re.I)


def _cap(s, at_start):
    return s[0].upper() + s[1:] if at_start and s else s


def gloss(text: str, book=None, seen=None) -> str:
    """Introduce foreign phrases the first time each one is used in an article; romanize or name non-Latin script.
    The phrase keeps its written form here (so the rewrite and its guard see it); respell() swaps in the say form."""
    book = SEED_PHRASEBOOK if book is None else book
    seen = set() if seen is None else seen
    t = fold(unicodedata.normalize("NFC", text)).replace("’", "'")
    for p in sorted(book, key=lambda p: -len(p["phrase"])):
        rx = _phrase_re(p)
        lang, meaning = p["language"], p["meaning"]
        cues = p.get("context") or [w for w in re.findall(r"[A-Za-z]+", lang) if len(w) >= 5 and w.lower() not in ("dragon", "tongue", "slang", "crew's")]
        explained = any(c.lower() in t.lower() for c in cues) or fold(meaning).lower() in t.lower()

        def sub(m, p=p, lang=lang, meaning=meaning, explained=explained):
            key = p["phrase"].lower()
            word = m.group(0).rstrip("!")
            bang = "!" if m.group(0).endswith("!") else ""
            if key in seen or explained:
                seen.add(key); return word + bang
            seen.add(key)
            at_start = m.start() == 0 or bool(re.search(r"[.!?:]\s*[\"']?$", t[:m.start()]))
            return f'{_cap("in", at_start)} {lang}, {word}, which means "{meaning}"' + (bang or ",")
        t = rx.sub(sub, t)
    for rx, lang, rom in _SCRIPTS:
        def script(m, lang=lang, rom=rom):
            s = m.group(0)
            hit = next((p for p in book if p["phrase"] == s), None)
            if hit: return hit["say"]                      # the phrasebook pass already introduced it
            if rom == "pinyin":
                try:
                    from pypinyin import lazy_pinyin
                    return f"in {lang}, " + " ".join(lazy_pinyin(s))
                except Exception:
                    pass
            return f"a phrase in {lang}"
        t = re.sub(rx, script, t)
    t = re.sub(r",\s*([.!?])", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


def respell(text: str, book=None) -> str:
    """Swap each phrasebook phrase for its English-voice pronunciation."""
    book = SEED_PHRASEBOOK if book is None else book
    for p in sorted(book, key=lambda p: -len(p["phrase"])):
        if p.get("say"):
            text = _phrase_re(p).sub(lambda m, say=p["say"]: (say[0].upper() + say[1:] if m.group(0)[0].isupper() else say)
                                     + ("!" if m.group(0).endswith("!") else ""), text)
    return text


# ════════════════════════════════════ B. write for the ear ════════════════════════════════════
SYSTEM = ("You rewrite one paragraph of Nova's journal so it sounds natural when a narrator reads it aloud in Nova's voice: "
          "first person, dry, warm, a little sardonic. Rules: keep every fact, number, name and claim. Add NO facts, names, "
          "numbers, quotes, opinions or jokes that are not already in the paragraph. Break long sentences into shorter spoken "
          "ones. Turn parentheticals, dashes, arrows, slashes and inline lists into natural spoken clauses. Expand an acronym "
          "on first use only if you are certain what it stands for. Keep foreign phrases and their explanations exactly as "
          "written. Keep about the same length. Output only the rewritten paragraph: no preamble, no quotes around it, no "
          "markdown, no notes.")
_NUMWORDS = set(_ONES) | {w for w in _TENS if w != "_"} | {"hundred", "thousand", "million", "billion", "trillion", "point",
                                                         "first", "second", "third", "fifth", "eighth", "ninth", "twelfth"}


def _numruns(s):
    toks = re.findall(r"[a-z]+", spoken(s).lower().replace("-", " "))
    runs, cur = [], []
    for w in toks:
        if w in _NUMWORDS or (w.endswith("th") and w[:-2] in _NUMWORDS) or (w.endswith("ieth") and (w[:-4] + "y") in _NUMWORDS):
            cur.append(w)
        elif cur: runs.append(" ".join(cur)); cur = []
    if cur: runs.append(" ".join(cur))
    return runs


def grounded(out: str, inp: str) -> tuple[bool, str]:
    """Is the rewrite free of invented numbers, names and quotes? Returns (ok, reason)."""
    if not out or not out.strip(): return False, "empty"
    o = out.strip()
    pre = re.match(r"(?i)^(here('s| is)|sure|okay,? here|rewritten|certainly)\b", o)
    if (pre and not inp.lower().startswith(pre.group(0).lower())) or "**" in o or o.startswith("#"):
        return False, "preamble/markdown"
    wi, wo = len(inp.split()), len(o.split())
    if wi >= 8 and not (0.6 <= wo / wi <= 1.6): return False, f"length {wo}/{wi}"
    in_runs = " | ".join(_numruns(inp))
    for r in _numruns(o):
        if r not in in_runs: return False, f"number '{r}'"
    in_low = {w.lower().strip("'") for w in re.findall(r"[\w'-]+", inp)}
    in_low |= {p.lower() for w in re.findall(r"[\w'-]+", inp) for p in w.split("-")}
    in_low |= {w.lower() for w in re.findall(r"[\w']+", spoken(inp))} | {"fahrenheit", "celsius", "degrees"}
    acr = set(re.findall(r"\b[A-Z]{2,6}\b", inp))
    toks = re.findall(r"[\w'-]+", o)
    i = 0
    while i < len(toks):
        w = toks[i]
        if not w[0].isupper():
            i += 1; continue
        j = i
        while j < len(toks) and toks[j][0].isupper(): j += 1
        run = toks[i:j]
        covered = set()                                 # acronym expansion: (The) Intrusion Prevention System for IPS
        for a_ in range(len(run)):
            for b_ in range(a_ + 2, len(run) + 1):
                if "".join(x[0] for x in run[a_:b_]).upper() in acr: covered |= set(range(a_, b_))
        for k, x in enumerate(run):
            if k in covered: continue
            xl = x.lower().strip("'")
            if xl in in_low or xl in _COMMON_START or xl.rstrip("s") in in_low or xl.endswith("'s") and xl[:-2] in in_low: continue
            if x.isupper() and len(x) > 1 and x not in acr: return False, f"acronym '{x}'"
            return False, f"name '{x}'"
        i = j
    for q in re.findall(r'"([^"]{3,})"', o):
        if fold(q).lower().strip(" ,.") not in fold(inp).lower(): return False, f"quote '{q[:30]}'"
    return True, "ok"


class RewriteCache:
    """PG-backed (nova_speaks_rewrites) with an in-memory layer; degrades to memory-only when PG is unreachable."""
    DDL = ("CREATE TABLE IF NOT EXISTS nova_speaks_rewrites (key text PRIMARY KEY, model text, input text, output text, "
           "created_at timestamptz DEFAULT now())")

    def __init__(self, dsn=DSN):
        self.mem, self.cur = {}, None
        try:
            import psycopg2
            c = psycopg2.connect(dsn); c.autocommit = True; self.cur = c.cursor(); self.cur.execute(self.DDL)
        except Exception as e:
            log(f"rewrite cache memory-only ({type(e).__name__})")

    @staticmethod
    def key(text, model=MODEL):
        return hashlib.sha256(f"{PROMPT_VERSION}\0{model}\0{text}".encode()).hexdigest()

    def get(self, k):
        if k in self.mem: return self.mem[k]
        if self.cur:
            try:
                self.cur.execute("SELECT output FROM nova_speaks_rewrites WHERE key=%s", (k,)); r = self.cur.fetchone()
                if r: self.mem[k] = r[0]; return r[0]
            except Exception: pass
        return None

    def put(self, k, inp, out, model=MODEL):
        self.mem[k] = out
        if self.cur:
            try:
                self.cur.execute("INSERT INTO nova_speaks_rewrites (key, model, input, output) VALUES (%s,%s,%s,%s) "
                                 "ON CONFLICT (key) DO UPDATE SET output=EXCLUDED.output", (k, model, inp, out))
            except Exception: pass


def _post(url, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def llm(text, timeout=REWRITE_TIMEOUT):
    """Ollama qwen3:8b directly (think off, server-default num_ctx); the router's conversation pool as fallback."""
    try:
        r = _post(f"{OLLAMA}/api/chat", {"model": MODEL, "stream": False, "think": False, "keep_alive": -1,
                                         "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
                                         "options": {"temperature": 0.3}}, timeout)
        return r["message"]["content"]
    except Exception as e:
        log(f"ollama rewrite failed ({type(e).__name__}); trying router")
    r = _post(f"{ROUTER}/v1/chat/completions", {"model": "conversation", "temperature": 0.3,
                                               "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "/no_think\n" + text}]}, timeout)
    return r["choices"][0]["message"]["content"]


def _clean_llm(s):
    s = re.sub(r"<think>.*?</think>", "", s or "", flags=re.S).strip()
    return s.strip('"').strip()


def rewrite(text: str, cache=None, call=None) -> tuple[str, str]:
    """(text_to_use, verdict). Fails open to the input on any error, timeout or grounding miss."""
    if os.environ.get("NOVA_SPEAKS_REWRITE", "1") == "0": return text, "disabled"
    if len(text.split()) < 12: return text, "short"
    call = call or llm
    k = RewriteCache.key(text)
    out = cache.get(k) if cache else None
    verdict = "cached"
    if out is None:
        try:
            out = _clean_llm(call(text))
            verdict = "rewritten"
            if cache: cache.put(k, text, out)
        except Exception as e:
            return text, f"error:{type(e).__name__}"
    ok, why = grounded(out, text)
    return (out, verdict) if ok else (text, f"rejected:{why}")


# ════════════════════════════════════ paragraph pipeline ════════════════════════════════════
def merge_fragments(paras, min_words=4):
    """Paragraphs under min_words ('Qapla'.', 'Fine.') ride along with a neighbour — XTTS growls on tiny inputs."""
    out = []
    for p in paras:
        if out and len(out[-1].split()) < min_words: out[-1] = f"{out[-1]} {p}"
        else: out.append(p)
    if len(out) > 1 and len(out[-1].split()) < min_words: last = out.pop(); out[-1] = f"{out[-1]} {last}"
    return out


def narrate(chapters, book=None, cache=None, call=None, workers=4):
    """chapters [(heading, [paragraph])] -> [(heading, [spoken paragraph])] plus rewrite stats."""
    book = load_phrasebook() if book is None else book
    seen = set()
    glossed = [(h, [gloss(p, book, seen) for p in merge_fragments(ps)]) for h, ps in chapters]
    flat = [p for _, ps in glossed for p in ps]
    with ThreadPoolExecutor(max(1, workers)) as ex:
        res = list(ex.map(lambda p: rewrite(p, cache, call), flat))
    stats = {}
    for _, v in res: stats[v.split(":")[0]] = stats.get(v.split(":")[0], 0) + 1
    it = iter(res)
    out = []
    for h, ps in glossed:
        out.append((h, [s for s in (spoken(respell(next(it)[0], book)) for _ in ps) if s]))
    return [(h, ps) for h, ps in out if ps], stats


# ════════════════════════════════════ C. back-check ════════════════════════════════════
def _norm_words(s):
    """Comparable words: numbers dropped on both sides (Whisper writes '2.15 pm' for 'two fifteen P M' — formatting,
    not a misread), spelled letters joined ('u d m' == 'udm'), punctuation gone."""
    s = fold(s).lower().replace("-", " ").replace("'", "")
    toks = [w for w in re.sub(r"[^a-z0-9 ]", " ", s).split()
            if not re.search(r"\d", w) and w not in _NUMWORDS and not (w.endswith("th") and w[:-2] in _NUMWORDS)
            and not w.endswith("ieth") and w not in ("dot", "point", "oh")
            and not (w.endswith("ies") and w[:-3] + "y" in _NUMWORDS) and not (w.endswith("s") and w[:-1] in _NUMWORDS)]
    out, run = [], []
    for w in toks + [""]:
        if len(w) == 1: run.append(w); continue
        if run: out.append("".join(run) if len(run) > 1 else run[0]); run = []
        if w: out.append(w)
    return out


def wer(ref: str, hyp: str) -> float:
    r, h = _norm_words(ref), _norm_words(hyp)
    if not r: return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hw in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw))
        prev = cur
    return prev[-1] / len(r)


CPS = 14.5          # XTTS Gracie Wise ≈ 14–15 characters per second


def duration_ok(text, dur):
    exp = len(text) / CPS
    return exp * 0.45 <= dur <= exp * 1.9 + 1.5


class BackCheck:
    """Whisper transcription of each voiced part. mlx-whisper on Apple Silicon, faster-whisper elsewhere; None if absent."""

    def __init__(self, backend=None):
        self.backend, self._m = backend, None
        if self.backend is None:
            if not os.environ.get("HF_HOME"):
                for d in ("/Volumes/Data/huggingface", os.path.join(os.environ.get("TTS_HOME", ""), "hf")):
                    if d and os.path.isdir(os.path.dirname(d.rstrip("/"))): os.environ["HF_HOME"] = d; break
            try:
                if sys.platform == "darwin":
                    import mlx_whisper  # noqa: F401
                    self.backend = "mlx"
            except Exception: pass
            if self.backend is None:
                try:
                    import faster_whisper  # noqa: F401
                    self.backend = "faster"
                except Exception: self.backend = "none"

    def transcribe(self, path):
        if self.backend == "mlx":
            import mlx_whisper
            return mlx_whisper.transcribe(path, path_or_hf_repo="mlx-community/whisper-base.en-mlx", language="en",
                                          condition_on_previous_text=False)["text"]
        if self.backend == "faster":
            if self._m is None:
                from faster_whisper import WhisperModel
                self._m = WhisperModel("base.en", device="cpu", compute_type="int8", cpu_threads=4)
            import subprocess, numpy as np                 # decode with ffmpeg: faster-whisper's PyAV path breaks on av>=15
            pcm = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                                 capture_output=True, check=True).stdout
            segs, _ = self._m.transcribe(np.frombuffer(pcm, np.float32).copy(), language="en", beam_size=1, condition_on_previous_text=False)
            return " ".join(s.text for s in segs)
        return None

    def score(self, path, text, dur):
        """(badness, wer|None, dur_ok). badness < WER_MAX and dur_ok = keep."""
        dok = duration_ok(text, dur)
        try:
            hyp = self.transcribe(path)
        except Exception as e:
            log(f"back-check transcribe failed: {e}"); hyp = None
        w = None if hyp is None else wer(text, hyp)
        bad = (w if w is not None else 0.0) + (0.0 if dok else 0.5)
        return bad, w, dok


def new_stats(backend):
    return {"backend": backend, "parts": 0, "retries": 0, "worst_wer": 0.0, "wer_sum": 0.0, "wer_n": 0, "flagged": 0,
            "dur_outliers": 0}


def finish_stats(st):
    st = dict(st)
    total, n = st.pop("wer_sum", 0.0), st.pop("wer_n", 0)
    st["mean_wer"] = round(total / n, 3) if n else None
    st["worst_wer"] = round(st["worst_wer"], 3)
    return st


def synth_checked(synth, part, wav, checker, stats, dur_of, seed_base=1234):
    """synth(text, wav_path, seed) up to MAX_TRIES times; keep the best by WER + duration sanity."""
    best = None
    for attempt in range(MAX_TRIES):
        tmp = f"{wav}.try{attempt}.wav"
        synth(part, tmp, seed_base + attempt * 7919)
        d = dur_of(tmp)
        bad, w, dok = checker.score(tmp, part, d) if checker else (0.0 if duration_ok(part, d) else 0.5, None, duration_ok(part, d))
        if best is None or bad < best[0]:
            if best: _rm(best[1])
            best = (bad, tmp, w, dok)
        else:
            _rm(tmp)
        if bad < WER_MAX and dok: break
        stats["retries"] += 1
        log(f"back-check retry {attempt + 1}: wer={w if w is None else round(w, 2)} dur_ok={dok} seed={seed_base + attempt * 7919} '{part[:60]}'")
    bad, tmp, w, dok = best
    os.replace(tmp, wav)
    stats["parts"] += 1
    if w is not None:
        stats["worst_wer"] = max(stats["worst_wer"], w); stats["wer_sum"] += w; stats["wer_n"] += 1
    if not dok: stats["dur_outliers"] += 1
    if bad >= WER_MAX or not dok: stats["flagged"] += 1
    return w


def _rm(p):
    try: os.remove(p)
    except OSError: pass
