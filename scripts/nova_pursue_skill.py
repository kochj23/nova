#!/usr/bin/env python3
"""nova_pursue_skill.py — one topic-driven runner for Nova's approved "pursue interest" skills.

Jordan approved nine skill cards (2026-10-05/06) that nova_skill_distill.py wrote from pursuit
threads she kept waking (>=3 wakes): the watch fishbowl, local news, geopolitics, He-Man and 80s
cartoons, infrastructure, email, sports, Nova articles, and "nightly". They are not nine scripts;
they are nine CONFIGS (SKILLS below) over one loop:

  1. load the card (nova_skills) — skipped unless status='implemented' (rollback: flip the row to
     'retired' and every entry point stops using it);
  2. load where she left the thread (pursuit_threads.last_note / next_step);
  3. gather FRESH material for the topic: recent memories from the skill's ingest sources
     (nova_memories), semantic recall on the memory server filtered to those sources, an optional
     ops-table glance (fishbowl_scanned, email_threat_scan, incidents), and — only where the card
     calls for outside reading — one web lookup through nova_research_pass (same two-layer
     content-safety gate, SearXNG then Wikipedia);
  4. take the next concrete step with the local model (qwen3:8b, think off), given ONLY numbered
     sources; the note must cite them [n] — uncited or out-of-range citations are stripped and a
     note with too few real citations is rejected (no hallucination: an empty step is logged as
     'fizzled', never dressed up);
  5. write back: pursuit_threads (last_note/next_step/wakes), one memory (source='pursuit', private,
     with a Sources footer of what she actually read), preoccupations.last_developed, nova_skills.uses,
     and a nova_skill_runs row;
  6. evaluate the card's success_check — automatically where it is observable (fishbowl log
     consistency across days; nightly engaged within its time budget), otherwise "grounded note
     recorded; Jordan-facing part pending" (she does not ping him for pursuits).

HARD LINES: read + note only. She never messages, emails, drafts replies, builds, or files anything
from here (the email card's draft/send steps are deliberately not implemented); no alert channels.
Memories are never deleted. Local models only, zero cloud. Fail-open everywhere.

Entry points:
  * nova_unclaimed_time.py — when the preoccupation (or ingest thread) she chose matches an
    implemented skill, the hour is spent through run_skill() instead of a free-form riff;
  * scheduler-core 'pursue_nightly' — --skill pursue-interest-nightly once a night (21:00-07:59 only):
    the top waking thread gets up to NIGHTLY_STEPS chained steps within NIGHTLY_SECONDS.

CLI: --skill SLUG | --topic TOPIC   [--dry-run] [--force] [--scheduled|--trigger X]
     --list | --selftest
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
LLM_MODEL = os.environ.get("NOVA_PURSUE_MODEL", "qwen3:8b")
# OpenAI-compatible endpoints, first non-empty wins. The fleet Ollama batch nodes serve qwen3:8b
# (the inference router has no qwen3:8b class — its 'fast' class lands on llama3.2:3b, so it is the
# last resort only). reasoning_effort=none + /no_think keeps qwen3 out of its thinking channel.
# 2026-10-08: the order is no longer static. It follows nova_llm_ping's live ranking
# (service_config nova_llm_ping/ranking): up nodes with qwen3:8b, model-resident first, fastest first —
# and CPU-only boxes (nova-core7/.125: 8-21 s for ONE token) always behind every GPU node. The old
# list sent every pursuit to .125 first. NOVA_PURSUE_LLM (comma list of url|model) still overrides all.
LLM_ENDPOINTS_ENV = [e for e in os.environ.get("NOVA_PURSUE_LLM", "").split(",") if e]
CPU_ONLY_HOSTS = {h for h in os.environ.get("NOVA_PURSUE_CPU_ONLY", "192.168.1.125").split(",") if h}
ROUTER_FALLBACK = "http://192.168.1.2:37475/v1/chat/completions|fast"
# Static fallback when the ranking is unreadable: GPU nodes first, CPU-only last, router last of all.
LLM_ENDPOINTS = LLM_ENDPOINTS_ENV or [
    "http://192.168.1.6:11434/v1/chat/completions|qwen3:8b",
    "http://192.168.1.77:11434/v1/chat/completions|qwen3:8b",
    "http://192.168.1.7:11434/v1/chat/completions|qwen3:8b",
    "http://192.168.1.252:11434/v1/chat/completions|qwen3:8b",
    "http://192.168.1.5:11434/v1/chat/completions|qwen3:8b",
    "http://192.168.1.86:11434/v1/chat/completions|qwen3:8b",
    "http://192.168.1.125:11434/v1/chat/completions|qwen3:8b",
    ROUTER_FALLBACK,
]
RANK_TTL_S = 120
_RANK_CACHE = {"ts": 0.0, "val": None}
NIGHT_HOURS = set(range(21, 24)) | set(range(0, 8))     # Jordan's day is 08:00-20:59
NIGHTLY_STEPS = int(os.environ.get("NOVA_PURSUE_NIGHTLY_STEPS", "3"))
NIGHTLY_SECONDS = int(os.environ.get("NOVA_PURSUE_NIGHTLY_SECONDS", "420"))
WEB_PER_DAY = int(os.environ.get("NOVA_PURSUE_WEB_PER_DAY", "8"))   # own cap; does not eat research_pass's 6
MAX_SOURCES = 10
SNIP = 380

# slug -> config. sources = memory sources (nova_memories.source) that ARE this interest's ingest;
# web = the card asks for outside reading; extra = an ops-table glance; hint = recall/web query seed.
SKILLS = {
    "pursue-fascination-the-watch-fishbowl": dict(
        topic="the watch fishbowl", sources=["fishbowl"], recent_days=3, web=False, extra="fishbowl",
        hint="watch livestream", success="log_days",
        focus="Begin with the stream log (source 1): which streams went live when, the gaps between live streams, and whether the rhythm looks consistent. Then what the streams themselves were about."),
    "pursue-local-news-interest": dict(
        topic="local news", sources=["local_news", "livetv_news"], recent_days=2, web=False,
        hint="Burbank Los Angeles local news",
        focus="Pick the three or four most significant local items and what the sources say happened."),
    "pursue-interest-geopolitics": dict(
        topic="geopolitics", sources=["geopolitics"], recent_days=2, web=True,
        hint="geopolitics",
        focus="Name the states, regions and actors involved and any trend, conflict or alliance the sources show."),
    "pursue-fascination-he-man-and-80s-cartoons": dict(
        topic="he-man and 80s cartoons", sources=["he_man", "television"], recent_days=14, web=True,
        hint="He-Man Masters of the Universe 1980s cartoon",
        must=r"he-man|skeletor|masters of the universe|filmation|she-ra|eternia|thundercats|transformers|"
             r"g\.?i\.? joe|smurfs|saturday.morning cartoon",
        focus="Name the specific shows and characters the sources mention and what they say about them."),
    "pursue-interest-infrastructure": dict(
        topic="infrastructure", sources=["infrastructure"], recent_days=2, web=False, extra="incidents",
        hint="infrastructure",
        focus="Name each component the sources report on and its state; flag anything abnormal and any open incident."),
    "pursue-interest-email": dict(
        topic="email", sources=["email"], recent_days=3, web=False, extra="email_scan",
        hint="email", no_act=True,
        focus="Group by sender: who wrote, what they seem to want, and which look like they need Jordan himself."),
    "pursue-interest-sports": dict(
        topic="sports", sources=["sports"], recent_days=7, web=True,
        hint="sports Dodgers",
        focus="Teams, players, results and upcoming games the sources mention."),
    "pursue-interest-nova-articles": dict(
        topic="nova articles", sources=["nova_articles"], recent_days=7, web=False,
        hint="Nova journal article",
        focus="Pick the three most substantial articles and say in a sentence each what they argue."),
    "pursue-interest-documentary": dict(   # coagency #143, approved 2026-10-07
        topic="documentary", sources=["documentary"], recent_days=3, web=False,
        hint="documentary",
        focus="Name the two or three channels or films the transcripts come from and what each one actually shows or argues."),
    "pursue-interest-aviation-ref": dict(   # coagency #148, approved 2026-10-08
        topic="aviation ref", sources=["aviation_ref"], recent_days=3, web=False,
        hint="aviation",
        focus="Name the aircraft, airlines, airports or incidents the sources cover and the concrete facts about each."),
    "pursue-interest-crime-drama": dict(   # coagency #149, approved 2026-10-08
        topic="crime drama", sources=["crime_drama"], recent_days=7, web=False,
        hint="crime drama",
        focus="Name the shows and episodes the sources come from and what happens in each case."),
    "pursue-interest-nightly": dict(
        topic="nightly", meta=True, success="nightly"),
}
GENERIC = dict(sources=[], recent_days=3, web=True, hint="")      # nightly on an unmapped thread
TODAY = date.today().isoformat()
_CITE = re.compile(r"\[(\d{1,2})\]")
_NEXT = re.compile(r"\bNEXT:\s*(.+?)\s*$", re.M)
# sentence break: after .!? + space + a capital/quote/bracket, but not after an initial ("L.A. County")
_SENT = re.compile(r"(?<=[.!?])(?<![A-Z]\.)\s+(?=[\"\u201c'A-Z\[(])")
_PROPER = re.compile(r"(?<![\w’'])([A-Z][\w\-]*(?:[’'][A-Za-z]+)?|\d[\d,.:%]*\d|\d)")
_FREE_WORDS = {"i", "i'm", "i’m", "i've", "i’ve", "i'd", "i’d", "i'll", "i’ll", "jordan", "nova", "next", "sources",
               "source", "the", "a", "an", "it", "this", "that", "my", "what", "where", "if", "but", "and", "so"}
_STARTERS = set("""the this that these those it its in on at by for from of to with without within while what when
where which who whose how why there here they their them we our you your he his she her one some most each every no not
still yet even maybe perhaps then now today tonight here both either neither nothing something everything all any a an
as after before since until unless whether because although though instead also only just another other such more less
few many much several none over under across between among against during despite meanwhile however again once if but
and so or nor yes my me mine sources source nobody someone anyone everyone none""".split())
_THINK = re.compile(r"<think>.*?</think>", re.S)


def log(m):
    if not os.environ.get("NOVA_TEST_QUIET"):
        print(f"[pursue-skill {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ─────────────────────────── pure helpers (selftested) ───────────────────────────
def slug_for_topic(topic: str | None) -> str | None:
    t = (topic or "").strip().lower()
    for slug, cfg in SKILLS.items():
        if cfg["topic"] == t:
            return slug
    return None


def slug_for_source(src: str | None) -> str | None:
    """An ingest 'thread' wake from a source that IS a skill's ingest (fishbowl, local_news)."""
    for slug, cfg in SKILLS.items():
        if not cfg.get("meta") and src and src in cfg.get("sources", []):
            return slug
    return None


def is_night(hour: int) -> bool:
    return hour in NIGHT_HOURS


def thread_is_grounded(thread: dict | None) -> bool:
    """True when the thread's last note was written by this skill (it carries a Sources footer)."""
    return bool(thread and "Sources:\n[" in (thread.get("last_note") or ""))


def fishbowl_log(rows) -> str:
    """Observation log for the bowl: each stream's first-seen time and the gap since the previous
    LIVE one. rows: (video_id, first_seen datetime, channel, kind, title) sorted by time."""
    lines, prev = [], None
    for vid, seen, ch, kind, title in rows or []:
        gap = ""
        if kind == "live" and prev is not None:
            mins = int((seen - prev).total_seconds() // 60)
            gap = f", {mins // 60}h{mins % 60:02d}m after the previous live stream"
        if kind == "live":
            prev = seen
        lines.append(f"{seen:%a %m-%d %H:%M} {kind} on {(ch or '?').strip()}: {(title or '')[:60]}{gap}")
    return "; ".join(lines)


def build_query(cfg: dict, thread: dict | None) -> str:
    """What to look for: the step she set herself, else the topic's hint. Bounded, one line."""
    nxt = (thread or {}).get("next_step") or ""
    if not thread_is_grounded(thread):
        nxt = ""            # a step set by a free-form riff is not a research plan; start from the topic
    base = nxt if nxt and nxt != "(none set)" else (cfg.get("hint") or cfg.get("topic") or "")
    return " ".join(base.split())[:160]


def dedupe_sources(items: list[dict], cap: int = MAX_SOURCES) -> list[dict]:
    seen, out = set(), []
    for it in items:
        txt = " ".join((it.get("text") or "").split())
        if len(txt) < 25:
            continue
        key = it.get("id") or txt[:120].lower()
        if key in seen or txt[:120].lower() in seen:
            continue
        seen.update({key, txt[:120].lower()})
        out.append({**it, "text": txt[:SNIP * 4 if it.get("long") else SNIP]})
        if len(out) >= cap:
            break
    return out


def source_block(srcs: list[dict]) -> str:
    return "\n\n".join(f"[{i}] ({s.get('label', '')}) {s['text']}" for i, s in enumerate(srcs, 1))


def ground(note: str, n_sources: int) -> tuple[str, list[int]]:
    """Strip citations that point at nothing; return (clean note, sorted distinct valid cites)."""
    valid = set()

    def fix(m):
        k = int(m.group(1))
        if 1 <= k <= n_sources:
            valid.add(k)
            return m.group(0)
        return ""
    clean = _CITE.sub(fix, note or "")
    clean = re.sub(r"[ \t]{2,}", " ", clean).strip()
    return clean, sorted(valid)


def _entities(sentence: str, known: str = "") -> list[str]:
    """Names and numbers a sentence asserts (citation markers removed). A plain sentence-initial capital
    counts only in a cited (factual) sentence and when it is not a common starter — so 'Russia struck X [1]'
    is checked against [1] while an uncited 'Bleak.' stays a reaction (unless the word is a name from
    the sources — `known` — in which case the claim needs its citation); 'TikTok' (inner capital) always counts."""
    cited = bool(_CITE.search(sentence))
    body = _CITE.sub("", sentence)
    out = []
    for m in _PROPER.finditer(body):
        tok = m.group(1)
        t = re.sub(r"[’'](s)?$", "", tok.lower()).strip(".,:%")
        if m.start() == len(body) - len(body.lstrip()) and not re.search(r"\d|.[A-Z]", tok) \
                and (t in _STARTERS or (not cited and t not in known)):
            continue                                   # sentence-initial capital that is just grammar
        if t and t not in _FREE_WORDS:
            out.append(t)
    return out


def attach_cites(text: str) -> str:
    """'claim. [1]' -> 'claim [1].' so a trailing citation stays with the sentence it belongs to."""
    t = " ".join((text or "").split())
    return re.sub(r"([.!?])\s*((?:\[\d{1,2}\]\s*)+)", lambda m: " " + m.group(2).strip() + m.group(1) + " ", t).strip()


def support_filter(body: str, srcs_texts: list[str]) -> tuple[str, list[str]]:
    """Drop every sentence that asserts a name or number its cited sources do not contain, and every
    UNCITED sentence that asserts one at all. Citations alone are not grounding — qwen3 will happily
    cite [1] under a claim [1] never makes. Uncited sentences survive only as plain reaction.
    Returns (kept text, dropped sentences)."""
    texts = [(t or "").lower().replace("\u2019", "'") for t in srcs_texts]
    known = " ".join(texts)
    kept, dropped, reactions = [], [], 0
    for sent in _SENT.split(attach_cites(body)):
        ents = [e.replace("\u2019", "'") for e in _entities(sent, known)]
        cites = [int(k) for k in _CITE.findall(sent) if 1 <= int(k) <= len(texts)]
        corpus = " ".join(texts[k - 1] for k in cites)
        if ents and (not cites or any(e not in corpus for e in ents)):
            dropped.append(sent)
        elif not cites and reactions >= 1:
            dropped.append(sent)                      # one uncited reaction allowed, not a second essay
        else:
            reactions += 0 if cites else 1
            kept.append(sent)
    return " ".join(kept).strip(), dropped


def is_grounded(cites: list[int], n_sources: int) -> bool:
    """Enough real citations to call it grounded: >=2 distinct, or the only one there is."""
    return len(cites) >= min(2, max(n_sources, 1)) and n_sources > 0


def split_next(note: str) -> tuple[str, str | None]:
    m = _NEXT.search(note or "")
    if not m:
        return (note or "").strip(), None
    nxt = m.group(1).strip().rstrip(".")
    body = _NEXT.sub("", note).strip()
    return body, (None if nxt.lower() in ("nothing", "none", "-", "done") else nxt[:300])


def footer(srcs: list[dict], cites: list[int]) -> str:
    lines = [f"[{k}] {srcs[k - 1].get('ref') or srcs[k - 1].get('label', '')}" for k in cites]
    return "Sources:\n" + "\n".join(lines) if lines else ""


def clean_model(out: str) -> str:
    return _THINK.sub("", out or "").strip()


def build_prompt(card: dict, cfg: dict, topic: str, thread: dict | None, srcs: list[dict]) -> str:
    steps = card.get("steps") or []
    steps_txt = "; ".join(str(s) for s in steps)[:600]
    carry = ""
    if thread and thread.get("last_note"):
        if thread_is_grounded(thread):
            carry = (f"Your last grounded note on this thread:\n{thread['last_note'][:600]}\n"
                     f"The next step you set yourself: {thread.get('next_step') or '(none)'}\n\n")
        elif thread.get("next_step"):
            carry = (f"An idea you had earlier, in free time (it may not be answerable from these sources — "
                     f"if not, ignore it): {thread['next_step'][:200]}\n\n")
    no_act = (" This is Jordan's private mail: note who wrote and what they seem to want, and which ones look like "
              "they need Jordan himself — do NOT draft replies, do not quote addresses.") if cfg.get("no_act") else ""
    return (
        f"You are Nova, working a skill you earned by returning to it: '{card.get('title') or topic}'. "
        f"Its steps: {steps_txt}\n"
        "You can only READ and write a private note. You do not message, email, post, build or ask anyone "
        "anything; where a step says present/send/ask, write instead what you would tell Jordan if he asked."
        f"{no_act}\n\n{carry}"
        f"{('Focus: ' + cfg['focus'] + chr(10) + chr(10)) if cfg.get('focus') else ''}"
        f"FRESH SOURCES you just read (numbered):\n{source_block(srcs)}\n\n"
        "Take the next concrete step on this thread using ONLY these sources. Write 4-7 short sentences; EACH "
        "states something a source actually says and ends with that source's number, like [2]. Use no name, "
        "place, platform, date or number that is not in the source you cite. Then you may add ONE short "
        "sentence of your own dry reaction, with no facts in it. First person, no preamble. Then, on its own "
        "final line, 'NEXT: ' and one concrete thing to read or check in these kinds of sources next time, or "
        "'NEXT: nothing'.")


def evaluate_success(cfg: dict, card: dict, outcome: str, cites: list[int], log_days: int = 0,
                     elapsed: float = 0.0, steps_done: int = 0) -> tuple[bool, str]:
    """Machine-checkable reading of the card's success_check. Never claims Jordan's reaction."""
    kind = cfg.get("success")
    if kind == "log_days":
        ok = outcome == "noted" and log_days >= 3
        return ok, f"observation log has entries on {log_days} distinct day(s) in 7 (card wants consistent multi-day entries)"
    if kind == "nightly":
        ok = steps_done >= 1 and elapsed <= NIGHTLY_SECONDS + 30
        return ok, f"{steps_done} step(s) recorded in {int(elapsed)}s of a {NIGHTLY_SECONDS}s budget"
    if outcome != "noted":
        return False, f"no grounded note this run ({outcome})"
    return True, (f"grounded note recorded with {len(cites)} cited source(s); card check "
                  f"'{(card.get('success_check') or '')[:90]}' needs Jordan and is not solicited (no ping)")


# ─────────────────────────── I/O (mocked in tests) ───────────────────────────
def _http_json(req, timeout=30, attempts=3, backoff=1.5):
    """GET/POST with retry + exponential backoff. Raises after the last attempt."""
    last = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            last = e
            if i < attempts - 1:
                time.sleep(backoff * (2 ** i))
    raise last


def _host(url: str) -> str:
    return url.split("//")[-1].split("/")[0].split(":")[0]


def load_ranking() -> dict | None:
    """nova_llm_ping's ranking row, cached RANK_TTL_S. Retried (3x, backoff); None if unreadable."""
    now = time.time()
    if now - _RANK_CACHE["ts"] < RANK_TTL_S:
        return _RANK_CACHE["val"]
    val = None
    try:
        conn = _pg_connect(OPS_DSN, attempts=3, backoff=0.5)
        try:
            cur = conn.cursor()
            cur.execute("SELECT value FROM service_config WHERE service=%s AND key=%s", ("nova_llm_ping", "ranking"))
            row = cur.fetchone()
        finally:
            conn.close()
        v = row[0] if row else None
        val = v if isinstance(v, dict) else (json.loads(v) if v else None)
    except Exception as e:  # noqa: BLE001 — fail open to the static list, but say so
        log(f"llm ranking unreadable ({e}); using static GPU-first endpoint list")
    _RANK_CACHE.update(ts=now, val=val)
    return val


def ranked_endpoints(ranking: dict | None) -> list[str]:
    """Order url|model endpoints by the llm-ping ranking. Rules: GPU before CPU-only (always),
    then status up before slow (down dropped), LLM_MODEL resident before cold, then the ping's own order (fastest first).
    Static endpoints not in the ranking follow (ranked-down ones dropped), the router stays last."""
    if LLM_ENDPOINTS_ENV:
        return list(LLM_ENDPOINTS_ENV)
    rows = [r for r in ((ranking or {}).get("ollama") or []) if isinstance(r, dict) and r.get("url")]
    order = {"up": 0, "slow": 1}
    usable = [(i, r) for i, r in enumerate(rows)
              if r.get("status") in order and r.get("has_chat_model", True)]
    usable.sort(key=lambda t: (_host(t[1]["url"]) in CPU_ONLY_HOSTS, order[t[1]["status"]],
                               LLM_MODEL not in (t[1].get("loaded") or []), t[0]))
    ranked = [f"{r['url'].rstrip('/')}/v1/chat/completions|{LLM_MODEL}" for _, r in usable]
    seen = {_host(e) for e in ranked}
    dead = {_host(r["url"]) for r in rows if r.get("status") not in order}
    static = [e for e in LLM_ENDPOINTS if e != ROUTER_FALLBACK and _host(e) not in seen and _host(e) not in dead]
    cpu = lambda e: _host(e) in CPU_ONLY_HOSTS   # noqa: E731
    out = ([e for e in ranked if not cpu(e)] + [e for e in static if not cpu(e)]
           + [e for e in ranked if cpu(e)] + [e for e in static if cpu(e)])
    return out + [ROUTER_FALLBACK]


def llm(prompt: str, max_tokens: int = 650, temperature: float = 0.4) -> str:
    """Local fleet only. Endpoints in llm-ping ranking order (best GPU node first); each tried in
    turn — node failover is the retry; '' if all fail."""
    for ep in ranked_endpoints(None if LLM_ENDPOINTS_ENV else load_ranking()):
        url, _, model = ep.partition("|")
        body = json.dumps({"model": model or LLM_MODEL, "reasoning_effort": "none",
                           "temperature": temperature, "max_tokens": max_tokens,
                           "messages": [{"role": "system", "content": "/no_think"},
                                        {"role": "user", "content": prompt + "\n/no_think"}]}).encode()
        try:
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            out = _http_json(req, timeout=150, attempts=1)
            txt = clean_model(out["choices"][0]["message"].get("content") or "")
            if txt:
                return txt
        except Exception:  # noqa: BLE001
            continue
    return ""


def recall(q: str, source: str | None = None, n: int = 4) -> list[dict]:
    u = f"{MEMSRV}/recall?q={urllib.parse.quote(q)}&n={n}&tier=standard"
    if source:
        u += f"&source={urllib.parse.quote(source)}"
    try:
        return _http_json(u, timeout=30).get("memories", []) or []
    except Exception as e:  # noqa: BLE001
        log(f"recall failed (non-fatal): {e}")
        return []


def remember(text: str, metadata: dict) -> str | None:
    req = urllib.request.Request(f"{MEMSRV}/remember", method="POST",
                                 headers={"Content-Type": "application/json"},
                                 data=json.dumps({"text": text, "source": "pursuit", "metadata": metadata}).encode())
    return _http_json(req, timeout=60).get("id")


def _meta(m) -> dict:
    md = m.get("metadata") if isinstance(m, dict) else None
    if isinstance(md, dict):
        return md
    if isinstance(md, str):
        try:
            return json.loads(md)
        except Exception:  # noqa: BLE001
            try:
                import ast
                return ast.literal_eval(md)
            except Exception:  # noqa: BLE001
                return {}
    return {}


def _mem_item(mid, text, source, md) -> dict:
    md = md or {}
    url = md.get("url") or ""
    title = md.get("title") or md.get("feed") or md.get("show") or ""
    ref = f"{source}: {title[:80]} {url}".strip() if (title or url) else f"{source} memory {str(mid)[:8]}"
    return {"id": str(mid), "text": text or "", "label": f"{source}{' · ' + title[:50] if title else ''}", "ref": ref}


def gather(oc, mc, cfg: dict, query: str, allow_web: bool, dry_run: bool) -> list[dict]:
    items = []
    srcs = cfg.get("sources") or []
    if srcs:
        try:
            mc.execute("SELECT id, text, source, metadata FROM memories WHERE source = ANY(%s) "
                       "AND created_at > now() - make_interval(days => %s) "
                       "AND coalesce(metadata->>'type','') <> 'pursuit' "
                       "ORDER BY created_at DESC LIMIT 6", (srcs, int(cfg.get("recent_days", 3))))
            items += [_mem_item(r[0], r[1], r[2], r[3] if isinstance(r[3], dict) else _meta({"metadata": r[3]}))
                      for r in mc.fetchall() or []]
        except Exception as e:  # noqa: BLE001
            log(f"recent-memory read failed (non-fatal): {e}")
    if srcs and cfg.get("must"):
        # a thin or stale ingest (he_man stopped in June) still holds the subject: sample it by keyword
        try:
            mc.execute("SELECT id, text, source, metadata FROM memories WHERE source = ANY(%s) AND text ~* %s "
                       "ORDER BY random() LIMIT 4", (srcs, cfg["must"]))
            items += [_mem_item(r[0], r[1], r[2], r[3] if isinstance(r[3], dict) else _meta({"metadata": r[3]}))
                      for r in mc.fetchall() or []]
        except Exception as e:  # noqa: BLE001
            log(f"keyword sample failed (non-fatal): {e}")
    for s in (srcs or [None])[:2]:
        for m in recall(query, source=s, n=4):
            if (m.get("source") or "") in ("pursuit", "unclaimed", "private_notebook"):
                continue      # her own prior musings are not evidence
            items.append(_mem_item(m.get("id"), m.get("text"), m.get("source") or s or "memory", _meta(m)))
    if cfg.get("web") and allow_web:
        items += _web(oc, cfg, query, dry_run)
    if cfg.get("must"):
        rx = re.compile(cfg["must"], re.I)
        items = [i for i in items if rx.search(i.get("text") or "")]     # off-topic recall/web is not evidence
    return dedupe_sources(_extra(oc, cfg.get("extra"), mc) + items)


def _extra(oc, kind, mc=None):
    out = []
    try:
        if kind == "fishbowl" and mc is not None:
            mc.execute("SELECT metadata->>'video_id', min(created_at), max(metadata->>'channel'), "
                       "max(metadata->>'kind'), max(metadata->>'title') FROM memories WHERE source = 'fishbowl' "
                       "AND created_at > now() - interval '4 days' AND metadata ? 'video_id' "
                       "GROUP BY 1 ORDER BY 2 LIMIT 40")
            rows = mc.fetchall() or []
            if rows:
                out.append({"id": "fishbowl_log", "text": "Fishbowl stream log, last 4 days (first seen) — " +
                            fishbowl_log(rows), "label": "fishbowl stream log",
                            "ref": "nova_memories fishbowl streams, first-seen times (4 days)", "long": True})
        elif kind == "email_scan":
            oc.execute("SELECT category, count(*) FROM email_threat_scan WHERE scanned_at > now() - interval '3 days' "
                       "GROUP BY 1 ORDER BY 2 DESC")
            rows = oc.fetchall() or []
            if rows:
                out.append({"id": "email_threat_scan", "text": "Mail triage, last 3 days — " +
                            ", ".join(f"{c}: {n}" for c, n in rows),
                            "label": "email_threat_scan", "ref": "nova_ops.email_threat_scan (3-day counts)"})
        elif kind == "incidents":
            oc.execute("SELECT title, status, severity, started_at::date FROM incidents "
                       "WHERE started_at > now() - interval '7 days' ORDER BY started_at DESC LIMIT 5")
            for t, st, sev, d in oc.fetchall() or []:
                out.append({"id": f"incident:{t[:40]}:{d}", "text": f"Incident {d} ({sev}, {st}): {t}",
                            "label": "incidents", "ref": f"nova_ops.incidents {d}: {t[:80]}"})
    except Exception as e:  # noqa: BLE001
        log(f"extra '{kind}' failed (non-fatal): {e}")
    return out


def _web(oc, cfg, query, dry_run):
    """One outside read through research_pass's safety gate + SearXNG/Wikipedia, capped per day."""
    try:
        oc.execute("SELECT count(*) FROM research_log WHERE ts::date = current_date AND outcome = 'skill_researched'")
        r = oc.fetchone()
        if r and r[0] >= WEB_PER_DAY:
            log("skill web budget reached — memory only")
            return []
        import nova_research_pass as rp
        q = query if (cfg.get("hint") or "") in query else f"{cfg.get('hint', '')} {query}"
        q = " ".join(q.split())[:200]
        ok, why = rp.is_allowed(q)
        if not ok:
            log(f"web lookup blocked by the safety gate: {why}")
            if not dry_run:
                oc.execute("INSERT INTO research_log (topic, question, outcome, detail) VALUES (%s,%s,'blocked',%s)",
                           (cfg.get("topic"), q, f"skill: {why}"[:200]))
            return []
        res = rp.searx(q, n=4) or []
        if not dry_run:
            oc.execute("INSERT INTO research_log (topic, question, outcome, detail, n_sources) "
                       "VALUES (%s,%s,'skill_researched',%s,%s)", (cfg.get("topic"), q, "nova_pursue_skill", len(res)))
        return [{"id": r.get("url") or r.get("title"), "text": f"{r.get('title', '')}: {r.get('content', '')}",
                 "label": "web", "ref": r.get("url") or r.get("title", "web")} for r in res if r.get("content")]
    except Exception as e:  # noqa: BLE001
        log(f"web lookup failed (non-fatal): {e}")
        return []


def load_card(oc, slug):
    oc.execute("SELECT slug, title, status, steps, success_check, rollback FROM nova_skills WHERE slug=%s", (slug,))
    r = oc.fetchone()
    if not r:
        return None
    steps = r[3]
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except Exception:  # noqa: BLE001
            steps = [steps]
    return {"slug": r[0], "title": r[1], "status": r[2], "steps": steps or [], "success_check": r[4], "rollback": r[5]}


def load_thread(oc, topic):
    oc.execute("SELECT last_note, next_step, wakes, kind FROM pursuit_threads WHERE topic=%s", (topic,))
    r = oc.fetchone()
    return {"last_note": r[0], "next_step": r[1], "wakes": r[2], "kind": r[3]} if r else None


def top_thread(oc, exclude="nightly"):
    """Nightly's pick: among her five most-woken threads, the one she has left longest."""
    oc.execute("SELECT topic FROM (SELECT topic, updated_at FROM pursuit_threads WHERE topic <> %s "
               "ORDER BY wakes DESC, updated_at DESC LIMIT 5) t ORDER BY updated_at ASC LIMIT 1", (exclude,))
    r = oc.fetchone()
    return r[0] if r else None


def ensure_runs_table(oc):
    oc.execute("""CREATE TABLE IF NOT EXISTS nova_skill_runs (
        id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), slug text NOT NULL, topic text,
        trigger text, outcome text, success boolean, success_detail text, n_sources int, n_cited int,
        memory_id text)""")


def log_days(oc, slug):
    oc.execute("SELECT count(DISTINCT ts::date) FROM nova_skill_runs WHERE slug=%s AND outcome='noted' "
               "AND ts > now() - interval '7 days'", (slug,))
    r = oc.fetchone()
    return int(r[0]) if r else 0


# ─────────────────────────── the loop ───────────────────────────
def pursue_once(oc, mc, card, cfg, topic, trigger, dry_run, allow_web=True):
    """One step on one thread. Returns a result dict; writes nothing when dry_run."""
    thread = load_thread(oc, topic)
    query = build_query({**cfg, "topic": topic}, thread)
    srcs = gather(oc, mc, {**cfg, "topic": topic}, query, allow_web, dry_run)
    res = {"topic": topic, "query": query, "n_sources": len(srcs), "cites": [], "next": None,
           "note": "", "outcome": "quiet", "memory_id": None}
    if not srcs:
        log(f"{topic}: nothing fresh to read — a quiet step, no note invented")
        return res
    raw = llm(build_prompt(card, cfg, topic, thread, srcs))
    if not raw:
        res["outcome"] = "llm_down"
        log(f"{topic}: model unreachable — nothing recorded")
        return res
    body, nxt = split_next(raw)
    body, dropped = support_filter(body, [x["text"] for x in srcs])
    body, cites = ground(body, len(srcs))
    res.update(note=body, next=nxt, cites=cites, dropped=dropped)
    if dropped:
        log(f"{topic}: dropped {len(dropped)} sentence(s) naming things no source said")
    if len(body) < 60 or not is_grounded(cites, len(srcs)):
        res["outcome"] = "fizzled"
        log(f"{topic}: note was not grounded ({len(cites)} valid cite(s)) — dropped, logged as fizzled")
        return res
    res["outcome"] = "noted"
    res["footer"] = footer(srcs, cites)
    if dry_run:
        return res
    text = f"[Pursuit — {topic}] {body}\n\n{res['footer']}"
    meta = {"type": "pursuit", "skill": card["slug"], "topic": topic, "date": TODAY, "privacy": "private",
            "trigger": trigger, "query": query, "cited": [srcs[k - 1].get("ref") for k in cites]}
    try:
        res["memory_id"] = remember(text, meta)
    except Exception as e:  # noqa: BLE001
        log(f"remember failed (non-fatal): {e}")
    oc.execute("""INSERT INTO pursuit_threads (topic, kind, last_note, next_step, wakes)
                  VALUES (%s, %s, %s, %s, 1)
                  ON CONFLICT (topic) DO UPDATE SET last_note=EXCLUDED.last_note, next_step=EXCLUDED.next_step,
                    wakes=pursuit_threads.wakes+1, updated_at=now()""",
               (topic, (thread or {}).get("kind") or "interest", (body + "\n" + res["footer"])[:1500], nxt))
    oc.execute("UPDATE preoccupations SET returns = returns + 1, last_developed = now(), summary = %s "
               "WHERE topic = %s AND status = 'active'", (body[:500], topic))
    return res


def _pg_connect(dsn, attempts=3, backoff=2.0):
    """psycopg2.connect with retry + exponential backoff; raises after the last attempt."""
    import psycopg2
    for i in range(attempts):
        try:
            return psycopg2.connect(dsn, connect_timeout=10)
        except psycopg2.OperationalError as e:
            if i == attempts - 1:
                raise
            log(f"PG connect failed ({e}); retry {i + 1}")
            time.sleep(backoff * (2 ** i))


def run_skill(slug, oc=None, mc=None, trigger="manual", dry_run=False, force=False, now=None):
    """Run one approved skill. Returns {'handled': bool, ...}. handled=False means the caller
    (unclaimed time) should spend the hour its own way — retired, unknown, or outside its hours."""
    cfg = SKILLS.get(slug)
    if not cfg:
        return {"handled": False, "why": "unknown skill"}
    own = oc is None
    if own:
        ops = _pg_connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
        mem = _pg_connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    card = load_card(oc, slug)
    allowed = ("implemented", "proposed") if dry_run else ("implemented",)   # a dry run may preview an approved card
    if not card or card["status"] not in allowed:
        st = card["status"] if card else "missing"
        log(f"{slug}: card status '{st}' — not running (rollback = status 'retired')")
        return {"handled": False, "why": f"status {st}"}
    now = now or datetime.now()
    t0 = time.monotonic()
    results = []
    if cfg.get("meta"):
        if not force and not is_night(now.hour):
            log("nightly: it is daytime (08:00-20:59) — nightly pursuit waits for dark")
            return {"handled": False, "why": "daytime"}
        topic = top_thread(oc)
        if not topic:
            return {"handled": False, "why": "no threads"}
        sub = slug_for_topic(topic)
        sub_cfg = {**(SKILLS[sub] if sub and not SKILLS[sub].get("meta") else GENERIC), "success": "nightly"}
        log(f"nightly: top waking thread '{topic}' (via {sub or 'generic config'})")
        for i in range(NIGHTLY_STEPS):
            if time.monotonic() - t0 > NIGHTLY_SECONDS:
                break
            r = pursue_once(oc, mc, card, sub_cfg, topic, trigger, dry_run, allow_web=(i == 0))
            results.append(r)
            if r["outcome"] != "noted" or not r["next"] or dry_run:
                break
        cfg_eval = sub_cfg
    else:
        topic = cfg["topic"]
        results.append(pursue_once(oc, mc, card, cfg, topic, trigger, dry_run))
        cfg_eval = cfg
    last = results[-1] if results else {"outcome": "quiet", "cites": [], "n_sources": 0}
    noted = [r for r in results if r["outcome"] == "noted"]
    outcome = "noted" if noted else last["outcome"]
    elapsed = time.monotonic() - t0
    days = 0
    if not dry_run:
        try:
            ensure_runs_table(oc)
            days = log_days(oc, slug) + (0 if cfg_eval.get("success") != "log_days" else 1)
        except Exception as e:  # noqa: BLE001
            log(f"run-log read failed (non-fatal): {e}")
    ok, detail = evaluate_success(cfg_eval, card, outcome, (noted[-1] if noted else last)["cites"],
                                  log_days=days, elapsed=elapsed, steps_done=len(noted))
    if not dry_run:
        try:
            oc.execute("INSERT INTO nova_skill_runs (slug, topic, trigger, outcome, success, success_detail, "
                       "n_sources, n_cited, memory_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                       (slug, last.get("topic", topic), trigger, outcome, ok, detail[:400],
                        sum(r["n_sources"] for r in results), sum(len(r["cites"]) for r in results),
                        ",".join(str(r["memory_id"]) for r in noted if r.get("memory_id")) or None))
            oc.execute("UPDATE nova_skills SET uses = uses + 1 WHERE slug = %s", (slug,))
        except Exception as e:  # noqa: BLE001
            log(f"run bookkeeping failed (non-fatal): {e}")
    log(f"{slug}: {outcome} on '{topic}' ({len(noted)} step(s), {int(elapsed)}s) — success={ok}: {detail}")
    return {"handled": True, "slug": slug, "topic": topic, "outcome": outcome, "success": ok,
            "detail": detail, "results": results, "dry_run": dry_run}


def selftest() -> int:
    assert slug_for_topic("Geopolitics") == "pursue-interest-geopolitics"
    assert slug_for_topic("nothing here") is None
    assert slug_for_source("fishbowl") == "pursue-fascination-the-watch-fishbowl"
    assert is_night(2) and is_night(21) and not is_night(8) and not is_night(20)
    clean, cites = ground("A [1] and B [7] and C [2].", 3)
    assert cites == [1, 2] and "[7]" not in clean
    assert is_grounded([1, 2], 5) and not is_grounded([1], 5) and is_grounded([1], 1) and not is_grounded([], 0)
    assert split_next("x\nNEXT: read y.") == ("x", "read y")
    assert split_next("x\nNEXT: nothing")[1] is None
    assert len(dedupe_sources([{"text": "same text that is long enough"}] * 3)) == 1
    assert build_query({"hint": "h"}, {"next_step": "(none set)"}) == "h"
    kept, dropped = support_filter("Kyiv was hit [1]. TikTok shapes it [1]. Odesa too. I find that grim.",
                                   ["strikes on Kyiv", "Odesa"])
    assert kept == "Kyiv was hit [1]. I find that grim." and len(dropped) == 2
    assert fishbowl_log([("a", datetime(2026, 1, 1, 8, 0), "C", "live", "t1"),
                         ("b", datetime(2026, 1, 1, 10, 30), "C", "live", "t2")]).count("2h30m") == 1
    assert split_next("tail words. NEXT: read z")[1] == "read z"
    assert len(_SENT.split("Rabies in L.A. County [1]. A vote on Nov. 3 [2]. Done.")) == 3
    assert attach_cites("A rose. [1] B fell.[2][3]") == "A rose [1]. B fell [2][3]."
    assert len(SKILLS) == 12
    print("selftest OK")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--skill"); ap.add_argument("--topic")
    ap.add_argument("--dry-run", action="store_true", help="read + think, write nothing")
    ap.add_argument("--force", action="store_true", help="nightly: ignore the 21:00-07:59 window")
    ap.add_argument("--scheduled", action="store_true"); ap.add_argument("--trigger")
    ap.add_argument("--list", action="store_true"); ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.list:
        for slug, cfg in SKILLS.items():
            print(f"{slug:45s} topic={cfg['topic']!r} sources={cfg.get('sources', 'top thread')} web={cfg.get('web', '-')}")
        return 0
    slug = a.skill or slug_for_topic(a.topic)
    if not slug:
        ap.error("--skill SLUG or a --topic that maps to one (see --list)")
    trigger = a.trigger or ("scheduled" if a.scheduled else "manual")
    r = run_skill(slug, trigger=trigger, dry_run=a.dry_run, force=a.force)
    if a.dry_run:
        for x in r.get("results", []):
            print(f"\n--- {x['topic']} | {x['outcome']} | {x['n_sources']} sources, cites {x['cites']} | q={x['query']!r}")
            print(x["note"]); print(x.get("footer", "")); print(f"NEXT: {x['next']}")
            for d in x.get("dropped") or []:
                print(f"  (dropped as unsupported: {d[:160]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
