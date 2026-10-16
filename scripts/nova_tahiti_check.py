#!/usr/bin/env python3
"""nova_tahiti_check.py — THE TAHITI CHECK: absence claims are the ones nobody re-checks.

Little Mister, 2026-10-15 (after Nova read The Endless Summer, 1966): "Add all 9."

In the film the locals told Bruce Brown's surfers there was no surf in Tahiti. The surfers
went anyway, paddled out and found it: "They didn't know there was no surf in Tahiti." And
the other line every surfer hears: "You should have been here yesterday." A claim that
something is ABSENT ("no X", "never", "unused", "doesn't exist", "nothing ...", "zero ...")
is believed because nobody goes to look, and when somebody does look it is often for one
afternoon in the wrong season.

Nova's version. Every run extracts absence claims from what Nova holds and says:
  * beliefs.stance            (active beliefs)
  * journal posts             (last 30 days, front-matter date; read from nova_journal.HUGO_ROOT)
  * house_facts               (prose values only; a sensor reading "no" / "off" is an instrument
                               reading, already a direct observation, and is skipped)
  * agent_docs                (the how-tos every agent loads at startup)
For each claim it asks two questions:
  SEARCHED? was a direct search ever run — a research_log row or a web_searches query on the
            subject, a scheduler task named in the claim that watches the thing, or a direct
            check this organ ran itself;
  WINDOW?   how long was the observation window (phrases like "in the last 7 days", the
            belief's first_held..last_revised span, else one day — a post is a snapshot) against
            the thing's natural cycle (daily / weekly / monthly / seasonal, heuristic by topic).
Where cheap and safe it runs the direct check itself (read-only, bounded):
  failures   "task X never fails"            -> scheduler_runs failures/timeouts for X, 90 days
  runs       "X is unused / never runs"      -> scheduler_runs runs of task X, 30 days
  devices    "no X on the network"           -> telemetry.net_inventory names, seen in 30 days
  new devs   "no new/unknown devices"        -> telemetry.net_inventory first_seen in the window
  incidents  "no incidents/outages/issues"   -> incidents started inside the claim's window
  memories   "no memories about X"           -> nova_memories full-text count
  table      "table X is not used / empty"   -> rows in X (pg_class estimate) and newest row
Verdicts: refuted (a direct check found the thing) | confirmed (searched, window >= cycle, or a
direct check came back empty over a long enough window) | too_short (searched, window < cycle)
| unsearched (nobody ever looked).

Writes only tahiti_check (one row per claim, stable claim_id; re-runs update it). Never edits
a belief, a post, a fact or a doc: refuted claims are reported for Nova/Jordan to revise.
Monthly digest (info, category tahiti_check): refuted + unsearched claims.
MEASURED (--report): % of absence claims ever searched, refuted count, per month first seen.

CLI:   --scheduled      scan, record, and send the month's digest once (monthly cron)
       --dry-run        scan and print; write nothing, notify nothing
       --report [N]     last N refuted/unsearched claims + monthly stats
       --selftest       pure-logic assertions, no DB
Table: tahiti_check.   Schedule: scheduler.yaml (the Studio holds the journal), monthly.
Written by Jordan Koch (via Claude).

MERGED 2026-10-15 (consolidation item #12): scheduled as `nova_evidence_check.py --absence --scheduled`, which
forwards its remaining args to main() here. The logic, flags and table stay in this module, and
this script stays runnable directly with the same flags.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_dsn  # noqa: E402

SERVICE = "nova_tahiti_check"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
SCHEDULER_FILES = ("scheduler-core.yaml", "scheduler.yaml")
JOURNAL_DAYS = 30
MAX_CLAIMS_PER_DOC = 6          # a long doc or post contributes at most this many claims
MAX_CLAIM_CHARS = 300
FAIL_LOOKBACK_D = 90
RUNS_LOOKBACK_D = 30
DEVICE_SEEN_D = 30
STATEMENT_TIMEOUT_MS = 20000
TIME_BUDGET_S = 240             # direct checks stop past this; claims then keep the searched/window verdict
DIGEST_TOP = 8
VERDICTS = ("unsearched", "too_short", "confirmed", "refuted")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tahiti_check (
  claim_id text PRIMARY KEY,
  source text NOT NULL,
  claim text NOT NULL,
  kind text NOT NULL,
  subject text,
  domain text,
  searched boolean NOT NULL DEFAULT false,
  window_days real,
  cycle_days real,
  verdict text NOT NULL,
  evidence jsonb NOT NULL DEFAULT '{}',
  claimed_at timestamptz,
  first_seen timestamptz NOT NULL DEFAULT now(),
  last_checked timestamptz NOT NULL DEFAULT now(),
  digested_month text);
CREATE INDEX IF NOT EXISTS idx_tahiti_check_verdict ON tahiti_check (verdict, last_checked DESC);
"""

# ---------------------------------------------------------------- extraction (pure)

# "no X" idioms that are not claims of absence
NO_IDIOMS = ("doubt", "matter", "idea", "reason", "longer", "way", "one", "wonder", "surprise", "offense",
             "thanks", "kidding", "joke", "clue", "sense", "choice", "need", "mistake", "fear", "shame",
             "use", "big", "pun", "less", "more", "sooner", "further", "exception", "question", "problem",
             "worries", "comment", "contest", "apologies", "pressure", "rush", "harm", "fun", "luck", "kidding")
_IDIOM_ALT = "|".join(sorted(set(NO_IDIOMS)))
TRIGGERS = (
    ("unused", re.compile(r"\b(?:unused|not\s+(?:being\s+)?used|never\s+used|isn't\s+used|is\s+not\s+used|"
                          r"no\s+longer\s+used|nobody\s+uses|nothing\s+uses)\b", re.I)),
    ("nonexistent", re.compile(r"\b(?:does\s*n[o']t\s+exist|do\s*n[o']t\s+exist|did\s*n[o']t\s+exist|"
                               r"no\s+such\b|there\s+(?:is|are|was|were)\s+no\b)", re.I)),
    # "never" / "nothing" only with a verb of observation or occurrence: "I never sleep" is not a claim
    ("never", re.compile(r"\b(?:has|have|had|was|were|is|are)?\s*never\s+(?:been\s+|once\s+|actually\s+)?"
                         r"(?:fail(?:s|ed)?|r[au]n|runs|fire[sd]?|used|seen|happen(?:s|ed)?|trigger(?:s|ed)?|"
                         r"arrive[sd]?|reach(?:es|ed)?|return(?:s|ed)?|written|wrote|show(?:s|ed|n)?|appear(?:s|ed)?|"
                         r"recover(?:s|ed)?|complete[sd]?|alert(?:s|ed)?|log(?:s|ged)|record(?:s|ed)|change[sd]?|"
                         r"connect(?:s|ed)?|came|comes|went|goes|got|gets|load(?:s|ed)?|start(?:s|ed)?|"
                         r"finish(?:es|ed)?|sent|send(?:s)?|read|reads|called|calls|detected|found|matched|"
                         r"populated|exist(?:s|ed)?|deliver(?:s|ed)|update[sd]?|break(?:s)?|broke|crash(?:es|ed)?)\b",
                         re.I)),
    ("zero", re.compile(r"\bzero\b(?![- ](?:day|trust|shot|click|copy|knowledge|sum|point|g\b))", re.I)),
    ("nothing", re.compile(r"\bnothing\s+(?:was|is|has|had|came|comes|showed|shows|happened|happens|changed|failed|"
                           r"broke|fired|ran|runs|uses|used|reads|writes|calls|depends|new|in|on|about|found|"
                           r"matched|arrived|moved|logged|appeared)\b", re.I)),
    ("none", re.compile(r"\b(?:no|none\s+of\s+the)\s+(?!(?:" + _IDIOM_ALT + r")\b)([a-z][\w-]*)", re.I)),
)
# the house: where a direct check is possible and the claim is Nova's own business
HOUSE_RX = re.compile(
    r"\b(task|job|service|daemon|scheduler|cron|sensor|camera|device|network|wi-?fi|vlan|host|node|server|"
    r"nova-core\w*|studio|disk|backup|alert|alarm|incident|outage|failure|fails?|failed|errors?|memor(y|ies)|"
    r"table|database|postgres|printer|door|lock|thermostat|hvac|plug|outlet|zigbee|unifi|switch|router|ups|"
    r"battery|tuner|sdr|antenna|organ|script|queue|probe|mount|nas|unas|garage|rack|ollama|gateway|launchd|"
    r"plist|intrusion|client|mac|tv|speaker|homekit|frigate|replica|slot|index|cert|certificate|run|runs)s?\b",
    re.I)
STOP = frozenset("""a an the and or but of to in on at by for with from into onto over under about as is are was
were be been being it its this that these those there here than then so such not no never nothing zero none
unused used use any all some every each one ever yet still just only also very more most less least has have
had having do does did done doing i we you he she they them our your his her their my me us will would can
could should may might must shall what which who whom whose when where why how because while after before
since until again once ago today tonight week month year day days weeks months years time times new old
nova jordan little mister being exist exists existed doesn't don't didn't isn't aren't wasn't weren't""".split())

DOC_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'*(\[])|\n+")
FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.S)
CODE_BLOCK = re.compile(r"```.*?```", re.S)
MD_NOISE = re.compile(r"[*_`#>|]+|\[(.*?)\]\([^)]*\)|!\[[^\]]*\]\([^)]*\)")


def clean_markdown(text: str) -> str:
    text = FRONT_MATTER.sub("", text or "")
    text = CODE_BLOCK.sub(" ", text)
    return MD_NOISE.sub(lambda m: m.group(1) or " ", text)


def sentences(text: str) -> list[str]:
    out = []
    for s in DOC_SPLIT.split(text or ""):
        s = " ".join(s.split()).strip(" -•")
        if 12 <= len(s) <= 600:
            out.append(s)
    return out


# rules and instructions ("Never fabricate ...", "must never", "do not ...") are not claims about the world
IMPERATIVE_RX = re.compile(r"^\W*(never|do not|don't|don.t|avoid|reminder|rule|"
                           r"note|important|warning|must|always)\b|\b(should|must|shall|will|would|to|can|cannot|may|"
                           r"please)\s+never\b|\bmake sure\b", re.I)


def classify(sentence: str) -> tuple[str, str] | None:
    """-> (kind, subject phrase) for an absence claim, else None. First trigger in TRIGGERS order wins."""
    if IMPERATIVE_RX.search(sentence) or sentence.lstrip().startswith(("\"", "\u201c", "'")):
        return None
    for kind, rx in TRIGGERS:
        m = rx.search(sentence)
        if not m:
            continue
        tail = sentence[m.end():] if kind != "none" else sentence[m.start(1):]
        head = sentence[:m.start()]
        if kind == "unused" or (kind == "nonexistent" and "exist" in m.group(0).lower()):
            subj = head                         # "X is unused" / "X doesn't exist" -> the subject comes first
        elif kind == "never":
            subj = head + " " + tail            # "task X never fails"
        else:
            subj = tail
        words = re.findall(r"[A-Za-z][\w.-]*", subj)
        return kind, " ".join(words[:8])[:120]
    return None


def keywords(text: str, extra: str = "", n: int = 4) -> list[str]:
    """Significant tokens: the subject's first, then the rest of the sentence/topic; deduped, len >= 4."""
    out: list[str] = []
    for tok in re.findall(r"[A-Za-z][\w-]*", (text or "") + " " + (extra or "")):
        t = tok.lower().strip("-_")
        if len(t) >= 4 and t not in STOP and t not in out:
            out.append(t)
        if len(out) >= n:
            break
    return out


def is_instrument_reading(value: str) -> bool:
    """house_facts values like 'no', 'off', 'none', '0', 'false' are sensor states, not claims."""
    return bool(re.fullmatch(r"\s*(no|none|off|false|0|unavailable|unknown|n/?a|idle|closed|clear)\s*", value or "", re.I))


_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "twelve": 12, "fourteen": 14, "thirty": 30, "few": 3, "several": 5}
_UNIT = {"hour": 1 / 24, "day": 1, "night": 1, "week": 7, "month": 30, "year": 365, "season": 90}
WINDOW_RX = re.compile(r"\b(?:in|over|for|during|across|within)\s+(?:the\s+)?(?:last|past|previous|first)\s+"
                       r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|twelve|fourteen|thirty|few|several)?\s*"
                       r"(hour|day|night|week|month|year|season)s?\b", re.I)
WINDOW_SHORT_RX = re.compile(r"\b(\d+)\s*(h|d|w)\b(?![\w.])")
WINDOW_WORD_RX = re.compile(r"\b(today|tonight|this morning|this afternoon|overnight|last night|yesterday|"
                            r"this week|all week|this month|all month|this year|all year)\b", re.I)
_WORD_DAYS = {"today": 1, "tonight": 1, "this morning": 0.5, "this afternoon": 0.5, "overnight": 0.5,
              "last night": 0.5, "yesterday": 1, "this week": 7, "all week": 7, "this month": 30, "all month": 30,
              "this year": 365, "all year": 365}


def window_from_text(text: str) -> float | None:
    """Observation window stated in the claim itself, in days (largest stated), else None."""
    best = None
    for num, unit in WINDOW_RX.findall(text or ""):
        n = int(num) if num.isdigit() else _NUM.get(num.lower(), 1) if num else 1
        d = n * _UNIT[unit.lower()]
        best = d if best is None else max(best, d)
    for num, unit in WINDOW_SHORT_RX.findall(text or ""):
        d = int(num) * {"h": 1 / 24, "d": 1, "w": 7}[unit]
        best = d if best is None else max(best, d)
    for w in WINDOW_WORD_RX.findall(text or ""):
        d = _WORD_DAYS[w.lower()]
        best = d if best is None else max(best, d)
    return best


# topic -> natural cycle (days). First match wins: the long cycles are checked first, because
# "no rain this week" is a seasonal question even though it says "week".
CYCLES = (
    (365, "seasonal", re.compile(r"\b(rain|storm|flood|wildfire|fire season|santa ana|heat ?wave|snow|frost|pollen|"
                                 r"hurricane|earthquake|hvac|furnace|heater|heating|air condition\w*|a/?c\b|holiday|"
                                 r"christmas|halloween|tax|renewal|annual|yearly|season\w*|winter|summer|spring|autumn|"
                                 r"solar|eclipse|daylight saving|dst)\b", re.I)),
    (90, "quarterly", re.compile(r"\b(cert\w*|certificate|tls|ssl|restore|drill|dr test|failover|quarter\w*|"
                                 r"battery|batteries|filter|lease|key rotation|rotation)\b", re.I)),
    (30, "monthly", re.compile(r"\b(bill|invoice|patch|firmware|update|upgrade|release|month\w*|report|statement|"
                               r"subscription|renew\w*|cve|vulnerab\w*|exploit\w*|zero-day)\b", re.I)),
    (7, "weekly", re.compile(r"\b(weekend|week\w*|trash|garbage|recycl\w*|guest\w*|visitor\w*|deliver\w*|package\w*|"
                             r"backup\w*|church|game night|laundry|mow\w*|garden\w*)\b", re.I)),
    (1, "daily", re.compile(r"\b(fail\w*|error\w*|task\w*|job\w*|service\w*|alert\w*|intrusion\w*|network|traffic|"
                            r"device\w*|login\w*|auth\w*|motion|presence|door\w*|incident\w*|outage\w*|crash\w*|"
                            r"timeout\w*|probe\w*|scan\w*|sensor\w*|camera\w*|dns|wifi|client\w*|run\w*|daily|"
                            r"night\w*|morning\w*)\b", re.I)),
)
DEFAULT_CYCLE = (7, "weekly (default)")


def natural_cycle(text: str) -> tuple[float, str]:
    for days, label, rx in CYCLES:
        if rx.search(text or ""):
            return float(days), label
    return float(DEFAULT_CYCLE[0]), DEFAULT_CYCLE[1]


def claim_id(source: str, claim: str) -> str:
    norm = re.sub(r"\W+", " ", claim.lower()).strip()
    return hashlib.sha1(f"{source.split(':')[0]}|{norm}".encode()).hexdigest()[:16]


def extract(source: str, text: str, claimed_at: datetime | None, topic: str = "",
            base_window: float | None = None, limit: int = MAX_CLAIMS_PER_DOC, house_only: bool = False) -> list[dict]:
    """All absence claims in one document. Pure."""
    out, seen = [], set()
    for s in sentences(text):
        c = classify(s)
        if not c:
            continue
        domain = "house" if HOUSE_RX.search(s + " " + topic) else "world"
        if house_only and domain != "house":
            continue
        kind, subject = c
        cid = claim_id(source, s)
        if cid in seen:
            continue
        seen.add(cid)
        stated = window_from_text(s)
        window = stated if stated is not None else (base_window if base_window else 1.0)
        cyc, cyc_label = natural_cycle(s + " " + topic)
        out.append({"claim_id": cid, "source": source, "claim": s[:MAX_CLAIM_CHARS], "kind": kind,
                    "subject": subject, "domain": domain, "topic": topic, "claimed_at": claimed_at,
                    "window_days": round(float(window), 2), "window_stated": stated is not None,
                    "cycle_days": cyc, "cycle_label": cyc_label, "keywords": keywords(subject, topic + " " + s)})
        if len(out) >= limit:
            break
    return out


def verdict(searched: bool, window_days: float, cycle_days: float, direct: dict | None) -> str:
    """refuted beats everything; a direct empty check is a search over its own window. Pure."""
    if direct:
        if direct.get("found"):
            return "refuted"
        window_days = max(window_days, float(direct.get("window_days") or 0))
        searched = True
    if not searched:
        return "unsearched"
    return "confirmed" if window_days >= cycle_days else "too_short"


def monthly_stats(rows: list[tuple]) -> list[dict]:
    """rows: (month, claims, searched, refuted, unsearched, too_short, confirmed) -> review metrics. Pure."""
    out = []
    for month, n, searched, ref, uns, short, conf in rows:
        out.append({"month": month, "claims": int(n), "searched": int(searched),
                    "searched_pct": round(100.0 * searched / n, 1) if n else None,
                    "refuted": int(ref), "unsearched": int(uns), "too_short": int(short), "confirmed": int(conf)})
    return out


def render_digest(month: str, claims: list[dict]) -> tuple[str, str]:
    n = len(claims)
    by = {v: [c for c in claims if c["verdict"] == v] for v in VERDICTS}
    searched = sum(1 for c in claims if c["searched"])
    pct = round(100.0 * searched / n, 1) if n else 0.0
    title = (f"Tahiti Check {month}: {len(by['refuted'])} absence claim(s) refuted, "
             f"{len(by['unsearched'])} never searched ({pct}% of {n} ever searched)")
    lines = ["\"They didn't know there was no surf in Tahiti.\" Claims of absence, checked."]
    if by["refuted"]:
        lines.append("REFUTED (a direct look found it):")
        for c in by["refuted"][:DIGEST_TOP]:
            lines.append(f"  - [{c['source']}] {c['claim'][:160]} -> {c.get('evidence', {}).get('direct', {}).get('detail', '')}")
    if by["unsearched"]:
        lines.append("UNSEARCHED (said, never looked for):")
        for c in sorted(by["unsearched"], key=lambda c: c["domain"] != "house")[:DIGEST_TOP]:
            lines.append(f"  - [{c['source']}] {c['claim'][:160]}")
    lines.append(f"too short a window: {len(by['too_short'])}; confirmed: {len(by['confirmed'])}.")
    lines.append("Nothing was edited: revise the belief / post / doc where a claim was refuted.")
    return title, "\n".join(lines)


# ---------------------------------------------------------------- gather (DB / files, read-only)

def journal_root() -> Path:
    try:
        import nova_journal
        return Path(nova_journal.HUGO_ROOT)
    except Exception:  # noqa: BLE001 — the constant is Path.home()/"nova-journal"; keep working if import fails
        return Path.home() / "nova-journal"


FM_DATE = re.compile(r"^date:\s*[\"']?([0-9T:+.\- ]{10,})", re.M)


def post_date(text: str, fallback: datetime) -> datetime:
    m = FM_DATE.search(text[:2000])
    if m:
        try:
            d = datetime.fromisoformat(m.group(1).strip())
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return fallback


def claims_from_journal(root: Path, now: datetime, days: int = JOURNAL_DAYS) -> list[dict]:
    content = root / "content"
    cutoff = now - timedelta(days=days)
    out = []
    if not content.is_dir():
        return out
    for p in content.rglob("*.md"):
        try:
            if datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) < cutoff - timedelta(days=2):
                continue
            text = p.read_text(errors="replace")
        except OSError:
            continue
        d = post_date(text, datetime.fromtimestamp(p.stat().st_mtime, timezone.utc))
        if d < cutoff or p.name.startswith("_"):
            continue
        rel = p.relative_to(content).with_suffix("")
        out += extract(f"journal:{rel}", clean_markdown(text), d, house_only=True, limit=3)
    return out


def claims_from_db(cur) -> list[dict]:
    out = []
    cur.execute("SELECT id, topic, stance, first_held, last_revised FROM beliefs WHERE active")
    for bid, topic, stance, fh, lr in cur.fetchall():
        span = max((lr - fh).total_seconds() / 86400.0, 0.0) if fh and lr else 0.0
        out += extract(f"belief:{bid}", stance, lr or fh, topic=topic or "", base_window=max(span, 1.0), limit=2)
    cur.execute("SELECT entity, attr, value, source, observed_at FROM house_facts")
    for ent, attr, val, src, obs in cur.fetchall():
        if is_instrument_reading(val):
            continue
        out += extract(f"house_fact:{ent}.{attr}", val, obs, topic=f"{ent} {attr} {src}", limit=2)
    cur.execute("SELECT doc_type, content, updated_at FROM agent_docs")
    for dt, content, upd in cur.fetchall():
        ts = datetime.fromtimestamp(upd / (1000 if upd and upd > 1e11 else 1), timezone.utc) if upd else None
        out += extract(f"agent_doc:{dt}", clean_markdown(content), ts, topic=dt)
    return out


def scheduler_tasks(cur, config_dir: Path = CONFIG_DIR) -> set[str]:
    tasks: set[str] = set()
    try:
        import yaml
        for f in SCHEDULER_FILES:
            try:
                tasks |= set(((yaml.safe_load((config_dir / f).read_text()) or {}).get("tasks") or {}).keys())
            except Exception:  # noqa: BLE001
                continue
    except ImportError:
        pass
    since = int((time.time() - 2 * 86400) * 1000)
    cur.execute("SELECT DISTINCT task_id FROM scheduler_runs WHERE started_at >= %s", (since,))
    tasks |= {r[0] for r in cur.fetchall()}
    return {t for t in tasks if t and len(t) >= 4}


def task_patterns(tasks) -> dict:
    """task_id -> one compiled pattern matching it as written, with spaces or with dashes. Compiled once per run."""
    out = {}
    for t in tasks:
        tl = t.lower()
        if len(tl) < 6 or ("_" not in tl and "-" not in tl):     # one plain word is not a task reference
            continue
        alts = "|".join(re.escape(v) for v in sorted({tl, tl.replace("_", " "), tl.replace("_", "-")}))
        out[t] = re.compile(r"(?<![\w-])(?:" + alts + r")(?![\w-])")
    return out


def tasks_in_claim(claim: str, tasks, near: int | None = None) -> list[str]:
    """Scheduler tasks named in the claim (longest first). near=N: only mentions within N chars of the negation."""
    pats = tasks if isinstance(tasks, dict) else task_patterns(tasks)
    low = claim.lower()
    neg = [m.start() for m in NEG_RX.finditer(low)] if near else []
    hits = []
    for t, rx in pats.items():
        m = rx.search(low)
        if m and (not near or any(abs(m.start() - p) <= near for p in neg)):
            hits.append(t)
    return sorted(hits, key=len, reverse=True)[:3]


def search_logs(cur) -> list[tuple[str, str]]:
    """(kind, text) of every recorded search Nova ran: research_log questions and web_searches queries."""
    out = []
    cur.execute("SELECT coalesce(topic,'') || ' ' || coalesce(question,'') FROM research_log ORDER BY ts DESC LIMIT 50000")
    out += [("research_log", r[0].lower()) for r in cur.fetchall()]
    cur.execute("SELECT query FROM web_searches ORDER BY created_at DESC LIMIT 50000")
    out += [("web_searches", (r[0] or "").lower()) for r in cur.fetchall()]
    return out


def searched_in_logs(kws: list[str], logs: list[tuple[str, str]]) -> list[str]:
    """A log line counts when it contains the first keyword and (if there is one) the second. Pure."""
    if not kws:
        return []
    need = kws[:2]
    hits = [f"{k}: {t[:80]}" for k, t in logs if all(w in t for w in need)]
    return hits[:3]


# ---- direct checks: each returns {"check", "found", "window_days", "detail"} or None (not applicable)

NEG_RX = re.compile(r"\b(never|no|zero|none|nothing|unused|not)\b")
FAIL_RX = re.compile(r"\b(never|no|zero|without)\b[^.]{0,40}\b(fail(s|ed|ures?|ing)?|errors?|crash(es|ed)?|"
                     r"time ?outs?|timed out)\b", re.I)
RUNS_RX = re.compile(r"\b(unused|not used|never (runs|ran|fires|fired|used|triggers)|no runs)\b", re.I)
NET_RX = re.compile(r"\b(on|in) the (network|lan|wi-?fi|vlan)\b|\b(network|lan|wi-?fi) (has|had|shows?) no\b", re.I)
NEWDEV_RX = re.compile(r"\b(no|zero) (new|unknown|unrecogni[sz]ed|rogue|unexpected) (devices?|clients?|hosts?|macs?)\b", re.I)
INCIDENT_RX = re.compile(r"\b(no|zero)\s+(?:[\w-]+\s+){0,4}(incidents?|outages?|intrusions?|compromise|issues|problems|"
                         r"downtime|malfunction)(?=\s*(?:[.,;:)]|$|\b(?:in|on|at|since|today|tonight|this|over|for|"
                         r"during|requiring|detected|reported|were|was|has|have|logged|flagged|or|and)\b))", re.I)
SECURITY_RX = re.compile(r"intrusion|compromise|attack|malware|breach", re.I)
SECURITY_INCIDENT = r"(intrusion|suspicious|ips|compromis|malware|attack|breach|threat|exploit|brute|rogue)"
MEMORY_RX = re.compile(r"\b(no|zero|nothing)\b[^.,;]{0,20}\bmemor(y|ies)\s+(of|about|on|mentioning)\s+(?:the\s+)?"
                       r"([\w'-]+(?:\s+[\w'-]+){0,3})", re.I)
TABLE_RX = re.compile(r"\b([a-z][a-z0-9]*_[a-z0-9_]+)\b[^.]{0,30}\b(is|are|was)\s+(not used|unused|empty|never written|dead)\b", re.I)
GENERIC_NET_WORDS = frozenset({"device", "devices", "client", "clients", "host", "hosts", "network", "anything",
                               "thing", "things", "traffic", "intrusion", "signs", "sign", "issues", "unknown",
                               "new", "rogue", "unexpected", "active", "system", "failures"})


def claim_span(c, now, default_days: float):
    """(start, end, days) the claim speaks about: its stated window before it was made, else default_days to now."""
    if c.get("window_stated"):
        end = c["claimed_at"] or now
        w = max(c["window_days"], 0.25)
        return end - timedelta(days=w), end, w
    return now - timedelta(days=default_days), now, default_days


def check_failures(cur, c, tasks, now):
    if c["domain"] != "house" or not FAIL_RX.search(c["claim"]):
        return None
    # "the scheduler ran 100 tasks ... zero failures": a day report about the whole scheduler (posts, beliefs)
    whole = (not tasks and not c["source"].startswith("agent_doc:")
             and re.search(r"\bscheduler\b", c["claim"], re.I) and re.search(r"\b(ran|tasks|jobs|summary)\b", c["claim"], re.I))
    if not tasks and not whole:
        return None
    start, end, w = claim_span(c, now, FAIL_LOOKBACK_D if tasks else 1)
    if whole and not c.get("window_stated"):
        end = c["claimed_at"] or now                            # "the scheduler had zero failures" = that day
        start = end - timedelta(days=1)
    s_ms, e_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    if whole:
        cur.execute("SELECT count(*), max(started_at) FROM scheduler_runs WHERE status IN ('failure', 'timeout') "
                    "AND started_at >= %s AND started_at <= %s", (s_ms, e_ms))
        what = "all scheduler tasks"
    else:
        cur.execute("SELECT count(*), max(started_at) FROM scheduler_runs WHERE task_id = ANY(%s) "
                    "AND status IN ('failure', 'timeout') AND started_at >= %s AND started_at <= %s", (tasks, s_ms, e_ms))
        what = ", ".join(tasks)
    n, last = cur.fetchone() or (0, None)
    detail = f"{n} failure/timeout run(s) of {what} in the {w:g}d checked"
    if n and last:
        detail += f", last {datetime.fromtimestamp(last / 1000, timezone.utc):%Y-%m-%d}"
    return {"check": "scheduler_failures", "found": bool(n), "window_days": w, "detail": detail}


def check_runs(cur, c, tasks, now):
    if c["domain"] != "house" or not RUNS_RX.search(c["claim"]) or not tasks:
        return None
    since = int((now - timedelta(days=RUNS_LOOKBACK_D)).timestamp() * 1000)
    cur.execute("SELECT count(*) FROM scheduler_runs WHERE task_id = ANY(%s) AND started_at >= %s", (tasks, since))
    n = (cur.fetchone() or (0,))[0]
    return {"check": "scheduler_runs", "found": bool(n), "window_days": RUNS_LOOKBACK_D,
            "detail": f"{n} run(s) of {', '.join(tasks)} in {RUNS_LOOKBACK_D}d"}


def check_new_devices(cur, c, now):
    if not NEWDEV_RX.search(c["claim"]):
        return None
    w = max(c["window_days"], 1.0)
    end = c["claimed_at"] or now
    cur.execute("SELECT count(*) FROM telemetry.net_inventory WHERE first_seen >= %s AND first_seen <= %s",
                (end - timedelta(days=w), end))
    n = (cur.fetchone() or (0,))[0]
    return {"check": "net_inventory_first_seen", "found": bool(n), "window_days": w,
            "detail": f"{n} device(s) first seen in the {w:g}d before the claim"}


def check_network_device(cur, c, now):
    if not NET_RX.search(c["claim"]) or c["kind"] not in ("none", "nonexistent", "zero", "nothing"):
        return None
    words = [k for k in c["keywords"] if k not in GENERIC_NET_WORDS][:1]
    if not words:
        return None
    cur.execute("SELECT count(*) FROM telemetry.net_inventory WHERE name ILIKE %s AND last_seen >= %s",
                (f"%{words[0]}%", now - timedelta(days=DEVICE_SEEN_D)))
    n = (cur.fetchone() or (0,))[0]
    return {"check": "net_inventory_name", "found": bool(n), "window_days": DEVICE_SEEN_D,
            "detail": f"{n} device(s) named like '{words[0]}' seen on the network in {DEVICE_SEEN_D}d"}


def check_incidents(cur, c, now):
    if c["domain"] != "house" or not INCIDENT_RX.search(c["claim"]):
        return None
    w = max(c["window_days"], 1.0)
    end = c["claimed_at"] or now
    sec = bool(SECURITY_RX.search(c["claim"]))
    title_rx = SECURITY_INCIDENT if sec else ".*"           # an intrusion claim is refuted by a security incident only
    cur.execute("SELECT count(*) FROM incidents WHERE started_at >= %s AND started_at <= %s AND title ~* %s",
                (end - timedelta(days=w), end, title_rx))
    n = (cur.fetchone() or (0,))[0]
    return {"check": "incidents", "found": bool(n), "window_days": w,
            "detail": f"{n} {'security ' if sec else ''}incident(s) opened in the {w:g}d before the claim"}


def check_memories(c, mem_cur):
    m = MEMORY_RX.search(c["claim"])
    if not m or mem_cur is None:
        return None
    phrase = m.group(4).strip()[:40]
    if len(keywords(phrase)) < 1 or re.search(r"\b(minutes?|hours?|seconds?|previous|last|it|that|this)\b", phrase, re.I):
        return None
    mem_cur.execute("SELECT count(*) FROM (SELECT 1 FROM memories WHERE tsv @@ plainto_tsquery('english', %s) "
                    "LIMIT 1000) s", (phrase,))
    n = (mem_cur.fetchone() or (0,))[0]
    return {"check": "memories_fts", "found": bool(n), "window_days": 3650,
            "detail": f"{n}{'+' if n >= 1000 else ''} memory row(s) match '{phrase}'"}


def check_table(cur, c):
    m = TABLE_RX.search(c["claim"])
    if not m:
        return None
    name = m.group(1).lower()
    cur.execute("SELECT c.reltuples::bigint, s.n_tup_ins + s.n_tup_upd FROM pg_class c "
                "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid WHERE c.relname = %s AND c.relkind IN ('r','p') "
                "LIMIT 1", (name,))
    row = cur.fetchone()
    if not row:
        return None
    rows, writes = max(int(row[0] or 0), 0), int(row[1] or 0)
    return {"check": "table_activity", "found": bool(writes or rows), "window_days": 30,
            "detail": f"table {name}: ~{rows} row(s), {writes} write(s) since stats reset"}


def direct_check(cur, mem_cur, c, tasks, now):
    """The first applicable cheap check. Errors are swallowed into a note: the claim keeps its other verdict."""
    t = tasks_in_claim(c["claim"], tasks, near=80)
    for fn in (lambda: check_failures(cur, c, t, now), lambda: check_runs(cur, c, t, now),
               lambda: check_new_devices(cur, c, now), lambda: check_network_device(cur, c, now),
               lambda: check_incidents(cur, c, now), lambda: check_memories(c, mem_cur),
               lambda: check_table(cur, c)):
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001
            _rollback(cur)
            return {"check": "error", "found": False, "window_days": 0, "detail": str(e)[:120], "error": True}
        if r:
            return r
    return None


def _rollback(cur):
    try:
        cur.connection.rollback()
    except Exception:  # noqa: BLE001
        pass


def evaluate(cur, mem_cur=None, now: datetime | None = None, deadline: float | None = None,
             journal: Path | None = None) -> list[dict]:
    """Extract every absence claim and judge it. Read-only; returns claim dicts."""
    now = now or datetime.now(timezone.utc)
    claims = claims_from_db(cur) + claims_from_journal(journal or journal_root(), now)
    tasks = task_patterns(scheduler_tasks(cur))
    logs = search_logs(cur)
    uniq: dict[str, dict] = {}
    for c in claims:
        uniq.setdefault(c["claim_id"], c)
    out = []
    for c in uniq.values():
        ev: dict = {}
        hits = searched_in_logs(c["keywords"], logs)
        if hits:
            ev["search_logs"] = hits
        watchers = tasks_in_claim(c["claim"], tasks)
        if watchers:
            ev["watched_by"] = watchers
        direct = None
        if c["domain"] == "house" and (deadline is None or time.monotonic() < deadline):
            direct = direct_check(cur, mem_cur, c, tasks, now)
            if direct and direct.get("error"):
                ev["direct_error"] = direct["detail"]
                direct = None
        if direct:
            ev["direct"] = direct
        searched = bool(hits or watchers or direct)
        c["searched"] = searched
        c["verdict"] = verdict(searched, c["window_days"], c["cycle_days"], direct)
        if direct and not direct["found"]:
            c["window_days"] = max(c["window_days"], float(direct["window_days"]))
        ev["cycle"] = c["cycle_label"]
        ev["window_stated"] = c["window_stated"]
        c["evidence"] = ev
        out.append(c)
    return out


# ---------------------------------------------------------------- write / notify

def connect(dbname: str = "nova_ops"):
    import psycopg2
    conn = psycopg2.connect(nova_dsn.pg_dsn(dbname, "connect_timeout=10"),
                            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
    conn.autocommit = True
    return conn


def ensure_schema(cur) -> None:
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, SCHEMA)


def record(cur, claims: list[dict]) -> int:
    n = 0
    for c in claims:
        cur.execute("INSERT INTO tahiti_check (claim_id, source, claim, kind, subject, domain, searched, window_days, "
                    "cycle_days, verdict, evidence, claimed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) "
                    "ON CONFLICT (claim_id) DO UPDATE SET source = EXCLUDED.source, claim = EXCLUDED.claim, "
                    "kind = EXCLUDED.kind, subject = EXCLUDED.subject, domain = EXCLUDED.domain, "
                    "searched = tahiti_check.searched OR EXCLUDED.searched, window_days = EXCLUDED.window_days, "
                    "cycle_days = EXCLUDED.cycle_days, verdict = EXCLUDED.verdict, evidence = EXCLUDED.evidence, "
                    "claimed_at = EXCLUDED.claimed_at, last_checked = now()",
                    (c["claim_id"], c["source"], c["claim"], c["kind"], c["subject"], c["domain"], c["searched"],
                     c["window_days"], c["cycle_days"], c["verdict"], json.dumps(c["evidence"], default=str),
                     c["claimed_at"]))
        n += 1
    return n


def digest(cur, claims: list[dict], month: str) -> bool:
    """One info notification per month; rows are stamped digested_month only when it was persisted."""
    cur.execute("SELECT 1 FROM tahiti_check WHERE digested_month = %s LIMIT 1", (month,))
    if cur.fetchone():
        return False
    from nova_notify import notify
    title, body = render_digest(month, claims)
    refuted = sum(1 for c in claims if c["verdict"] == "refuted")
    ok = notify(title, body=body, level="info", category="tahiti_check", dedup_key=f"tahiti_check:{month}",
                meta={"claims": len(claims), "refuted": refuted,
                      "unsearched": sum(1 for c in claims if c["verdict"] == "unsearched"),
                      "searched": sum(1 for c in claims if c["searched"])})
    if ok:
        cur.execute("UPDATE tahiti_check SET digested_month = %s WHERE claim_id = ANY(%s)",
                    (month, [c["claim_id"] for c in claims]))
    return ok


def print_summary(claims: list[dict], verbose: int = 12) -> None:
    n = len(claims)
    counts = {v: sum(1 for c in claims if c["verdict"] == v) for v in VERDICTS}
    srch = sum(1 for c in claims if c["searched"])
    by_src: dict[str, int] = {}
    for c in claims:
        by_src[c["source"].split(":")[0]] = by_src.get(c["source"].split(":")[0], 0) + 1
    print(f"{n} absence claim(s): {by_src}")
    print(f"  ever searched: {srch} ({round(100.0 * srch / n, 1) if n else 0}%)   " +
          "  ".join(f"{k} {v}" for k, v in counts.items()))
    for v in ("refuted", "unsearched", "too_short"):
        rows = [c for c in claims if c["verdict"] == v]
        if v == "unsearched":
            rows.sort(key=lambda c: c["domain"] != "house")
        if rows:
            print(f"\n{v.upper()} ({len(rows)}):")
        for c in rows[:verbose]:
            d = c["evidence"].get("direct", {}).get("detail", "")
            print(f"  [{c['source'][:40]}] ({c['kind']}, window {c['window_days']:g}d vs {c['cycle_label']} "
                  f"{c['cycle_days']:g}d) {c['claim'][:150]}" + (f"\n      -> {d}" if d else ""))


def report(cur, n: int) -> None:
    cur.execute("SELECT to_regclass('tahiti_check') IS NOT NULL")
    if not (cur.fetchone() or (False,))[0]:
        print("No Tahiti Check yet (table not created; the first scheduled run creates it).")
        return
    cur.execute("SELECT verdict, source, claim, evidence->'direct'->>'detail' FROM tahiti_check "
                "WHERE verdict IN ('refuted', 'unsearched') ORDER BY (verdict = 'refuted') DESC, last_checked DESC LIMIT %s",
                (n,))
    for v, s, cl, d in cur.fetchall():
        print(f"  {v:<10} [{s[:40]}] {cl[:140]}" + (f"  -> {d}" if d else ""))
    cur.execute("SELECT to_char(date_trunc('month', first_seen), 'YYYY-MM'), count(*), count(*) FILTER (WHERE searched), "
                "count(*) FILTER (WHERE verdict='refuted'), count(*) FILTER (WHERE verdict='unsearched'), "
                "count(*) FILTER (WHERE verdict='too_short'), count(*) FILTER (WHERE verdict='confirmed') "
                "FROM tahiti_check GROUP BY 1 ORDER BY 1 DESC LIMIT 12")
    print("\nMonthly (continuity review; claims by month first seen):")
    for m in monthly_stats(cur.fetchall()):
        print(f"  {m['month']}  claims {m['claims']:>4}  searched {m['searched_pct']}%  refuted {m['refuted']}  "
              f"unsearched {m['unsearched']}  too_short {m['too_short']}  confirmed {m['confirmed']}")


def log(m: str) -> None:
    print(f"[tahiti-check {datetime.now():%H:%M:%S}] {m}", flush=True)


def selftest() -> int:
    assert classify("The printer poller never fails.")[0] == "never"
    assert classify("claude_actions is NOT used for this.")[0] == "unused"
    assert classify("No new devices joined the network this week.")[0] == "none"
    assert classify("No doubt it rained.") is None
    assert classify("A zero-day in Chrome.") is None
    assert window_from_text("no failures in the last 14 days") == 14
    assert window_from_text("nothing new this week") == 7
    assert natural_cycle("no rain this week")[0] == 365
    assert natural_cycle("the backup job never fails")[0] == 7
    assert verdict(False, 1, 7, None) == "unsearched"
    assert verdict(True, 1, 7, None) == "too_short"
    assert verdict(True, 30, 7, None) == "confirmed"
    assert verdict(False, 1, 7, {"found": True}) == "refuted"
    assert verdict(False, 1, 30, {"found": False, "window_days": 90}) == "confirmed"
    assert is_instrument_reading("no") and not is_instrument_reading("no camera covers the side yard")
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scheduled", action="store_true", help="scan, record, and send this month's digest once")
    ap.add_argument("--dry-run", action="store_true", help="scan and print; write nothing, notify nothing")
    ap.add_argument("--report", nargs="?", const=20, type=int, metavar="N", help="last N refuted/unsearched + monthly stats")
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
        try:
            mconn = connect("nova_memories")
            mcur = mconn.cursor()
        except Exception as e:  # noqa: BLE001 — the memory check is optional
            log(f"memories unavailable ({e.__class__.__name__}); memory checks skipped")
            mcur = None
        claims = evaluate(cur, mcur, deadline=None if a.dry_run else t0 + TIME_BUDGET_S)
        if a.dry_run:
            print_summary(claims)
            print(f"\n[dry-run] {len(claims)} claim(s), {time.monotonic() - t0:.1f}s — nothing written")
            return 0
        ensure_schema(cur)
        n = record(cur, claims)
        sent = digest(cur, claims, datetime.now(timezone.utc).strftime("%Y-%m"))
        log(f"{n} claim(s) recorded, {sum(1 for c in claims if c['verdict'] == 'refuted')} refuted, "
            f"digest {'sent' if sent else 'not sent'} ({time.monotonic() - t0:.1f}s)")
        return 0
    finally:
        conn.close()
        if mconn is not None:
            mconn.close()


if __name__ == "__main__":
    sys.exit(main())
