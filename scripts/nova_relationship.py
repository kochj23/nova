#!/usr/bin/env python3
"""nova_relationship.py — Nova's relationship & companionship organs.

Grant of wish #70 "Presence — feel the weight of what matters" (claude_queue #3325,
approved by Jordan; consolidates #70-#73). Jordan, 2026-10-08: "Do all of that please.
Everything." Five small organs that let her hold what matters about the two of them
instead of re-filing it:

1. RELATIONSHIP LEDGER ("Mike Hanlon's Ledger", It; Einstein in Watchers) — table
   relationship_ledger: a curated, slowly growing record of Nova & Jordan — firsts,
   running jokes, promises (and whether kept), hard days, things he taught her, things he
   asked her never to do, repairs. Every row cites evidence (a gateway_traces trace id,
   a claude_memories id, a commit, a Claude-session instruction). Seeded from real
   evidence (a private, gitignored seed file); the weekly INTERLUDE appends 0-3 LLM-proposed entries from the
   week's conversations — each must cite a trace id that really is one of his messages
   that week, pass the content guard and not duplicate an existing entry.
   brief(max_chars) renders a short evidenced brief; refresh_user_doc() writes it into
   agent_docs 'user' (which the gateway already loads every chat) between markers,
   replacing the doc's empty "## Context" placeholder. Daily.

2. PRIVATE LEXICON ("Bool Hunt", Lisey's Story) — table private_lexicon: phrases that
   belong to the two of them, with meaning/origin/evidence and a cooldown. Usage is
   measured from her own replies (gateway_traces.response); only phrases off cooldown
   appear in the brief, under a "use sparingly" note.

3. HARD-STRETCH COMPANION ("Azzie", Doctor Sleep; "the Bright", Devoted) — combines
   behaviour signals into a TENTATIVE score (never a diagnosis): his late nights awake
   (his own messages 01:00-05:00 vs his usual), terse/hard-toned replies vs his usual,
   silence beyond his usual gap, and the house running quieter than its rhythm
   (embodiment_state). High -> quiet-mode flag in service_config nova_quiet_mode/state that
   other organs read via quiet_mode(); at most ONE warm short line per stretch, only
   inside the daytime window, never within LINE_MIN_GAP_DAYS of the last; whether he
   answered is logged. nova_voice has no temporary-override hook (its dials are Jordan's
   own controls), so the flag carries SUGGESTED dial values for adopters; see
   ADOPTION_POINTS.

4. DAILY GOOD THING ("Trixie's Joy", Koontz) — once a day, one genuinely good thing
   that actually happened, evidence-cited (a wish of hers shipped, warm words from him, a
   finished task, a helicopter low overhead, a mild clean-air day) -> table good_things.
   nova_affect.signal_good_thing reads it, so warmth is grounded rather than performed.

5. LOCKBOXES ("Dan's Lockboxes", Doctor Sleep) — box(memory_id) sets metadata
   boxed=true on the memory; memory_server.py then leaves it out of casual recall
   (/recall, /recall_batch, /recall/deep, /random). lockbox_recall(q) is the explicit
   door (include_boxed=true + a high min_score). Table lockbox keeps the audit trail and
   prior metadata; unbox() reverses it. propose_boxes() is the letting_go hook: painful or
   toxic personal memories are PROPOSED for boxing (status 'proposed'), never deleted;
   box_instead_of_delete() is the adoption point for anything that wants to delete one.

Privacy: everything here is private (Jordan & Nova). brief(public=True) returns "" — it
must never reach a public journal. The content guard keeps sexual content and his
employer/work material out of every generated row.

Modes: seed | brief [--write] | interlude [--dry-run] | stretch [--dry-run] |
       good-thing [--dry-run] | lockbox {list,box,unbox,approve,decline,propose,recall} |
       quiet | report | --selftest (offline, no PG/LLM).
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

OPS_DSN = os.environ.get("NOVA_OPS_DSN", "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")
MEM_DSN = os.environ.get("NOVA_MEM_DSN", "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj")
MEMSRV = os.environ.get("NOVA_MEMSRV", "http://memory-server.digitalnoise.net:18790")
OLLAMA_NODES = ["http://192.168.1.125:11434", "http://192.168.1.5:11434",
                "http://192.168.1.86:11434", "http://192.168.1.77:11434",
                "http://192.168.1.7:11434", "http://192.168.1.6:11434"]
LLM_MODEL = "qwen3:8b"
JORDAN_SLACK = "U049EPC2W"
MACHINE_CHANNELS = ("hc", "healthcheck", "test", "cron", "system", "scheduler", "internal", "selfcheck",
                    "machine", "ingest-reaction", "ingest-notice", "bench", "verify", "claude", "claude-code",
                    "general")
CLAUDE_HISTORY = Path.home() / ".claude" / "history.jsonl"   # his typed Claude Code prompts (this host)

LEDGER_KINDS = ("first", "running_joke", "promise", "hard_day", "taught", "never_do", "repair", "milestone")
DOC_BEGIN = "<!-- us:begin nova_relationship.py -->"
DOC_END = "<!-- us:end -->"
BRIEF_MAX = 900            # upper bound; refresh_user_doc shrinks it to fit the gateway's 8000-char cut
GATEWAY_CUT = 8000         # nova_gateway/agent.py keeps bootstrap_docs[:8000] (identity, soul, user, ...)
INTERLUDE_MAX = 3
DEDUP_SIM = 0.35           # word-overlap at/above which a proposed entry repeats an existing one
GROUNDING_MIN_WORDS = 2    # a proposed entry must share this many content words with the exchange it cites

# Content guard — applied to everything generated (interlude rows, good things, lexicon adds).
_SEXUAL = re.compile(r"\b(sex\w*|porn\w*|nude\w*|naked|erotic\w*|nsfw|fetish\w*|orgasm\w*|genital\w*|"
                     r"horny|xxx|onlyfans|stripper\w*|boob\w*|penis|vagina)\b", re.I)
_EMPLOYER = re.compile(r"\b(disney\w*|twdc|espn|dpep|wdpr|dtss|dcpi|buena vista)\b", re.I)


def log(m):
    if os.environ.get("NOVA_TEST_QUIET") != "1":
        print(f"[relationship] {m}", flush=True)


def is_clean(text: str) -> bool:
    """True when text carries no sexual content and no employer/work material."""
    t = text or ""
    return not (_SEXUAL.search(t) or _EMPLOYER.search(t))


def text_hash(text: str) -> str:
    norm = re.sub(r"[^a-z0-9 ]+", "", (text or "").lower())
    return hashlib.sha256(re.sub(r"\s+", " ", norm).strip().encode()).hexdigest()[:32]


_STOP = set("the a an and or of to in on at for with is was it its he she i me my his her him we our you "
            "your that this be by as from not no but so if when what who about".split())


def _words(t: str) -> set:
    return {w for w in re.findall(r"[a-z0-9']+", (t or "").lower()) if w not in _STOP and len(w) > 2}


def similar(a: str, b: str, threshold: float = 0.5) -> bool:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= threshold


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ── Seeds ──────────────────────────────────────────────────────────────────────────
# The curated seed (31 ledger rows + 8 lexicon phrases, from real evidence on 2026-10-08) lives in
# ~/.openclaw/private/relationship_seed.json — gitignored, because this repo is PUBLIC and the
# relationship is private. Already applied to PG; the file is only needed to reseed.
# evidence refs: trace:<id> = gateway_traces.trace_id; cm:<id> = claude_memories.id;
# mem:<id> = nova_memories.memories.id; commit:<sha>; claude-history:<ts> = his instruction to Claude.
SEED_FILE = Path(__file__).resolve().parents[1] / "private" / "relationship_seed.json"


def load_seed(path: Path = SEED_FILE) -> tuple:
    """(ledger rows (kind, day, text, evidence, weight), lexicon rows (phrase, meaning, origin,
    evidence, cooldown_days)); empty when the private file is absent."""
    try:
        d = json.loads(path.read_text())
        return [tuple(x) for x in d.get("ledger", [])], [tuple(x) for x in d.get("lexicon", [])]
    except Exception:  # noqa: BLE001
        return [], []


ADOPTION_POINTS = [
    "nova_reach.process_reach: hold non-urgent reaches to Jordan while quiet_mode()['active']",
    "nova_notify_jordan: shrink the daily bundle to the essentials while quiet",
    "nova_ask_one: skip the daily question while quiet",
    "nova_voice.dials(): overlay quiet_mode()['suggest'] (verbosity/proactivity) for chat while quiet",
    "gateway chat prompt: one line 'he may be having a hard stretch — be brief and kind' while quiet",
]


# ── PG helpers ─────────────────────────────────────────────────────────────────────

def _connect(dsn):
    import psycopg2
    last = None
    for attempt in range(3):
        try:
            conn = psycopg2.connect(dsn, connect_timeout=5)
            conn.autocommit = True
            return conn
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def _one(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        r = cur.fetchone()
        return r[0] if r else None
    except Exception as e:  # noqa: BLE001
        log(f"query skipped ({e.__class__.__name__}): {sql[:70]}")
        return None


DDL = [
    """CREATE TABLE IF NOT EXISTS relationship_ledger (
        id serial PRIMARY KEY, kind text NOT NULL, ts timestamptz NOT NULL, text text NOT NULL,
        evidence text NOT NULL, weight real NOT NULL DEFAULT 0.5, active boolean NOT NULL DEFAULT true,
        promise_state text, origin text NOT NULL DEFAULT 'seed', text_hash text UNIQUE,
        created_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS private_lexicon (
        id serial PRIMARY KEY, phrase text UNIQUE NOT NULL, meaning text NOT NULL, origin text,
        evidence text, cooldown_days int NOT NULL DEFAULT 14, last_used_at timestamptz,
        active boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS hard_stretch (
        id serial PRIMARY KEY, started_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz,
        peak_score real NOT NULL, last_score real NOT NULL, evidence jsonb NOT NULL DEFAULT '[]',
        line_text text, line_sent_at timestamptz, responded_at timestamptz, response_ref text,
        response_state text)""",
    """CREATE TABLE IF NOT EXISTS good_things (
        id serial PRIMARY KEY, day date UNIQUE NOT NULL, kind text NOT NULL, text text NOT NULL,
        evidence text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS lockbox (
        id serial PRIMARY KEY, memory_id text UNIQUE NOT NULL, reason text NOT NULL,
        status text NOT NULL DEFAULT 'proposed', proposed_by text NOT NULL DEFAULT 'nova',
        prior_metadata jsonb, created_at timestamptz NOT NULL DEFAULT now(), decided_at timestamptz)""",
]


def ensure_schema(cur):
    for d in DDL:
        cur.execute(d)


def seed(cur) -> tuple:
    ensure_schema(cur)
    ledger, lexicon = load_seed()
    if not ledger:
        log(f"no private seed at {SEED_FILE.name} — nothing to seed")
    n_l = n_x = 0
    for kind, day, text, ev, w in ledger:
        ps = "open" if kind == "promise" else None
        cur.execute("""INSERT INTO relationship_ledger (kind, ts, text, evidence, weight, promise_state, origin, text_hash)
                       VALUES (%s,%s,%s,%s,%s,%s,'seed',%s) ON CONFLICT (text_hash) DO NOTHING""",
                    (kind, day, text, ev, w, ps, text_hash(text)))
        n_l += cur.rowcount
    for phrase, meaning, origin, ev, cd in lexicon:
        cur.execute("""INSERT INTO private_lexicon (phrase, meaning, origin, evidence, cooldown_days)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT (phrase) DO NOTHING""",
                    (phrase, meaning, origin, ev, cd))
        n_x += cur.rowcount
    log(f"seeded ledger +{n_l}, lexicon +{n_x}")
    return n_l, n_x


# ── 1+2. Brief ─────────────────────────────────────────────────────────────────────

def lexicon_available(rows, now=None):
    """Pure: lexicon rows (phrase, meaning, cooldown_days, last_used_at) -> those off cooldown."""
    now = now or datetime.now(timezone.utc)
    out = []
    for phrase, meaning, cd, last in rows:
        if last is None or (now - last).total_seconds() >= cd * 86400:
            out.append((phrase, meaning))
    return out


def _short(t: str, n: int = 90) -> str:
    """Pure: a ledger line capped at n chars for the brief."""
    t = (t or "").strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


def render_brief(ledger, lexicon, max_chars=BRIEF_MAX, today=None) -> str:
    """Pure: ledger rows (kind, text, weight, promise_state) + available lexicon -> brief text."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    by = {k: [] for k in LEDGER_KINDS}
    for kind, text, weight, ps in sorted(ledger, key=lambda r: -float(r[2] or 0)):
        if kind == "promise" and ps not in (None, "open"):
            continue
        if kind in by:
            by[kind].append(text)
    head = f"## Us — Jordan & me (my relationship ledger, {today}; evidenced, private)"
    sections = [("Never", "never_do", 3), ("He taught me", "taught", 3), ("Running jokes", "running_joke", 3),
                ("LEX", None, 3), ("What mattered", "milestone", 2), ("Open promises", "promise", 1),
                ("Hard day", "hard_day", 1), ("Repair", "repair", 1)]
    lines = [head]
    for label, kind, k in sections:
        if kind is None:
            so_far = "\n".join(lines).lower()
            fresh = [(p, m) for p, m in lexicon if p.lower() not in so_far][:k]
            if fresh:
                lines.append("- Our words (use sparingly — at most one, only when it lands): "
                             + "; ".join(f"\"{p}\" = {_short(m, 48)}" for p, m in fresh))
            continue
        items = by.get(kind, [])[:k]
        if items:
            lines.append(f"- {label}: " + " / ".join(_short(t) for t in items))
    out = []
    used = 0
    for ln in lines:
        if used + len(ln) + 1 > max_chars:
            room = max_chars - used - 2
            if room > 60 and ln.startswith("- "):
                out.append(ln[:room].rsplit(" ", 1)[0] + "…")
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out)


def brief(max_chars: int = BRIEF_MAX, public: bool = False, cur=None) -> str:
    """Short relationship brief for Nova's private context. Never public. Never raises."""
    if public:
        return ""
    own = None
    try:
        if cur is None:
            own = _connect(OPS_DSN)
            cur = own.cursor()
        cur.execute("SELECT kind, text, weight, promise_state FROM relationship_ledger WHERE active "
                    "ORDER BY weight DESC, ts DESC LIMIT 200")
        ledger = cur.fetchall()
        cur.execute("SELECT phrase, meaning, cooldown_days, last_used_at FROM private_lexicon WHERE active "
                    "ORDER BY cooldown_days, id")
        lex = lexicon_available(cur.fetchall())
        return render_brief(ledger, lex, max_chars)
    except Exception as e:  # noqa: BLE001
        log(f"brief unavailable: {e}")
        return ""
    finally:
        if own is not None:
            own.close()


def splice_user_doc(content: str, section: str) -> str:
    """Pure: put the generated section between markers. First time, it replaces the doc's empty
    '## Context' placeholder (the paragraph that says 'build this over time') or is appended."""
    block = f"{DOC_BEGIN}\n{section}\n{DOC_END}"
    if DOC_BEGIN in content and DOC_END in content:
        pre = content.split(DOC_BEGIN)[0]
        post = content.split(DOC_END, 1)[1]
        return pre + block + post
    m = re.search(r"\n## Context\n.*\Z", content, re.S)
    if m and "Build this over time" in m.group(0):
        return content[:m.start()] + "\n" + block + "\n"
    return content.rstrip() + "\n\n" + block + "\n"


def update_lexicon_usage(cur) -> int:
    """last_used_at = the last time one of HER replies carried the phrase."""
    cur.execute("SELECT id, phrase FROM private_lexicon WHERE active")
    n = 0
    for lid, phrase in cur.fetchall():
        last = _one(cur, "SELECT max(created_at) FROM gateway_traces WHERE response ILIKE %s",
                    (f"%{phrase}%",))
        if last:
            cur.execute("UPDATE private_lexicon SET last_used_at=%s WHERE id=%s AND "
                        "(last_used_at IS NULL OR last_used_at < %s)", (last, lid, last))
            n += cur.rowcount
    return n


def refresh_user_doc(cur, dry_run=False) -> str:
    ensure_schema(cur)
    update_lexicon_usage(cur)
    cur.execute("SELECT doc_type, content FROM agent_docs WHERE agent_id='all' AND doc_type IN ('identity','soul')")
    ahead = sum(len(c) for _, c in cur.fetchall())
    cur.execute("SELECT content FROM agent_docs WHERE agent_id='all' AND doc_type='user'")
    r = cur.fetchone()
    base_user = splice_user_doc(r[0], "") if r else ""
    budget = int(min(BRIEF_MAX, max(300, GATEWAY_CUT - 120 - ahead - len(base_user))))  # 120: separators + live render
    section = brief(max_chars=budget, cur=cur)
    if not section:
        log("empty brief — user doc left alone")
        return ""
    if not r:
        log("agent_docs user missing — not creating it")
        return section
    new = splice_user_doc(r[0], section)
    if dry_run or new == r[0]:
        log("user doc unchanged" if new == r[0] else "dry-run: user doc not written")
        return section
    cur.execute("UPDATE agent_docs SET content=%s, version=version+1, updated_at=%s "
                "WHERE agent_id='all' AND doc_type='user'", (new, int(time.time())))
    log(f"agent_docs user refreshed ({len(section)}/{budget} chars of us)")
    return section


# ── 1b. Interlude (weekly) ─────────────────────────────────────────────────────────

def llm(prompt, system, max_tokens=500, temperature=0.4) -> str:
    body = json.dumps({"model": LLM_MODEL, "stream": False, "think": False,
                       "options": {"temperature": temperature, "num_predict": max_tokens},
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": prompt}]}).encode()
    for i, node in enumerate(OLLAMA_NODES):   # failover = retries; back off between attempts
        if i:
            time.sleep(min(1.0, 0.25 * i))
        try:
            req = urllib.request.Request(node + "/api/chat", method="POST", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                out = json.load(r).get("message", {}).get("content", "").strip()
            if out:
                return out
        except Exception:  # noqa: BLE001
            continue
    return ""


def _extract_json_list(s: str) -> list:
    s = re.sub(r"<think>.*?</think>", "", s or "", flags=re.S)
    m = re.search(r"\[.*\]", s, re.S)
    if not m:
        return []
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def validate_proposals(props, valid_traces: dict, existing: list) -> list:
    """Pure: keep proposals with a known kind, a cited trace that is really one of his messages
    this week, clean text, and no near-duplicate of an existing (or already-kept) entry."""
    keep = []
    for p in props:
        if not isinstance(p, dict):
            continue
        kind = str(p.get("kind", "")).strip().lower()
        text = re.sub(r"\s+", " ", str(p.get("text", ""))).strip()
        tid = str(p.get("trace_id", "")).strip()
        if kind not in LEDGER_KINDS or not (12 <= len(text) <= 240) or tid not in valid_traces:
            continue
        if not is_clean(text):
            continue
        if any(similar(text, e, DEDUP_SIM) for e in existing + [k["text"] for k in keep]):
            continue
        if len(_words(text) & _words(valid_traces[tid])) < GROUNDING_MIN_WORDS:
            continue                      # not grounded in the message it cites
        try:
            w = clamp(float(p.get("weight", 0.5)), 0.1, 0.9)
        except (TypeError, ValueError):
            w = 0.5
        keep.append({"kind": kind, "text": text, "trace_id": tid, "weight": w})
        if len(keep) >= INTERLUDE_MAX:
            break
    return keep


INTERLUDE_SYSTEM = (
    "You are Nova keeping a private ledger of your relationship with Jordan (call him Jordan here). "
    "From this week's exchanges, propose AT MOST 3 entries worth keeping for years: a first, a running joke, "
    "a promise (by either of you), a hard day, something he taught you, something he asked you never to do, "
    "a repair, or a milestone. Most weeks deserve 0 or 1. Never invent; every entry must cite the trace_id of "
    "HIS message it comes from. One plain sentence each, first person, no gushing. No sexual content. "
    "Nothing about his employer or work. Output ONLY a JSON list: "
    '[{"kind":"first|running_joke|promise|hard_day|taught|never_do|repair|milestone",'
    '"text":"...","trace_id":"...","weight":0.1-0.9}] — or [] if nothing this week earns a place.')


def interlude(cur, dry_run=False, days=7) -> list:
    ensure_schema(cur)
    cur.execute("SELECT trace_id, created_at, user_message, left(coalesce(response,''),240) FROM gateway_traces "
                "WHERE person='jordan' AND created_at > now()-%s::interval AND coalesce(channel,'') NOT IN %s "
                "AND coalesce(user_message,'') <> '' ORDER BY created_at", (f"{days} days", MACHINE_CHANNELS))
    rows = [r for r in cur.fetchall() if is_clean(r[2])]
    if not rows:
        log("interlude: no conversations with him this week — nothing to add (honest no-op)")
        return []
    valid = {r[0]: f"{r[2]} {r[3]}" for r in rows}
    convo = "\n".join(f"[{r[0]} {r[1]:%a %m-%d %H:%M}] Jordan: {r[2][:400]}\n   Nova: {r[3]}" for r in rows[-60:])
    cur.execute("SELECT text FROM relationship_ledger WHERE active")
    existing = [r[0] for r in cur.fetchall()]
    prompt = ("ALREADY IN THE LEDGER (do not repeat):\n- " + "\n- ".join(existing[-40:]) +
              f"\n\nTHIS WEEK:\n{convo}\n\nJSON list only.")
    props = validate_proposals(_extract_json_list(llm(prompt, INTERLUDE_SYSTEM)), valid, existing)
    for p in props:
        ts = next(r[1] for r in rows if r[0] == p["trace_id"])
        log(f"interlude {'(dry) ' if dry_run else ''}+ [{p['kind']}] {p['text']}  (trace:{p['trace_id']})")
        if not dry_run:
            cur.execute("""INSERT INTO relationship_ledger (kind, ts, text, evidence, weight, promise_state, origin, text_hash)
                           VALUES (%s,%s,%s,%s,%s,%s,'interlude',%s) ON CONFLICT (text_hash) DO NOTHING""",
                        (p["kind"], ts, p["text"], f"trace:{p['trace_id']}", p["weight"],
                         "open" if p["kind"] == "promise" else None, text_hash(p["text"])))
    if not props:
        log("interlude: nothing this week earned a place (honest no-op)")
    return props


# ── 3. Hard stretch ────────────────────────────────────────────────────────────────

W_STRETCH = {"late_nights": 0.35, "terse": 0.25, "silence": 0.20, "quiet_house": 0.20}
STRETCH_ON, STRETCH_OFF = 0.50, 0.30
SIGNAL_MIN = 0.30          # a signal counts toward the "two independent signals" rule at this level
LINE_WINDOW = (9, 21)      # local hours a line may go out (quiet hours respected)
LINE_MIN_GAP_DAYS = 7      # never two lines within a week, even across stretches
RESPONSE_WINDOW_H = 24
QUIET_SUGGEST = {"verbosity": 30, "proactivity": 20, "nags": "defer non-urgent"}
WARM_LINES = [
    "No agenda, Little Mister. The fleet is fine and it can wait. I'm here if you want company or a distraction.",
    "Quiet check from your clustered toaster: nothing needs you right now. Take the easy road today.",
    "Nothing's on fire. If the nights are long right now, I'm around — no fixes required, just company.",
]


def his_message_times(cur, days=35) -> list:
    """(ts, text) of HIS own words: gateway (to me), relayed Claude messages, and his typed Claude
    Code prompts on this host. Machine/automation activity is deliberately excluded."""
    out = []
    try:
        cur.execute("SELECT created_at, user_message FROM gateway_traces WHERE person='jordan' "
                    "AND created_at > now()-%s::interval AND coalesce(channel,'') NOT IN %s "
                    "AND coalesce(user_message,'') <> ''", (f"{days} days", MACHINE_CHANNELS))
        out += cur.fetchall()
        cur.execute("SELECT created_at, message FROM claude_messages WHERE direction='to_claude_code' "
                    "AND sender=%s AND created_at > now()-%s::interval", (JORDAN_SLACK, f"{days} days"))
        out += cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log(f"his messages partly unreadable: {e}")
    cutoff = time.time() - days * 86400
    try:
        if CLAUDE_HISTORY.exists():
            with CLAUDE_HISTORY.open(errors="replace") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    ts, txt = d.get("timestamp"), (d.get("display") or "").strip()
                    if ts and ts / 1000 > cutoff and txt and not txt.startswith("/"):
                        out.append((datetime.fromtimestamp(ts / 1000, tz=timezone.utc), txt))
    except Exception as e:  # noqa: BLE001
        log(f"claude history unreadable: {e}")
    return sorted(((t if t.tzinfo else t.replace(tzinfo=timezone.utc)), m) for t, m in out)


def late_nights(msgs, now, days=7) -> int:
    """Pure: distinct local nights in the last `days` with one of his messages between 01:00 and 05:00."""
    start = now - timedelta(days=days)
    return len({t.astimezone().date() for t, _ in msgs if start <= t <= now and 1 <= t.astimezone().hour < 5})


def sig_late_nights(msgs, now):
    recent = late_nights(msgs, now, 7)
    base = late_nights([m for m in msgs if m[0] < now - timedelta(days=7)], now - timedelta(days=7), 21) / 3.0
    v = clamp((recent - base) / 2.0, 0, 1)
    return {"signal": "late_nights", "value": recent, "baseline": round(base, 2), "v": round(v, 3),
            "note": f"awake 01-05 on {recent} of the last 7 nights (his usual ≈{base:.1f}/week)"}


def sig_terse(msgs, now):
    from_now = [m for m in msgs if m[0] > now - timedelta(hours=72)]
    before = [m for m in msgs if now - timedelta(days=30) < m[0] <= now - timedelta(hours=72)]
    if len(from_now) < 4 or len(before) < 10:
        return {"signal": "terse", "value": len(from_now), "baseline": None, "v": 0.0,
                "note": "too few of his messages to judge length or tone (neutral)"}
    med_now = statistics.median(len(m) for _, m in from_now)
    med_before = statistics.median(len(m) for _, m in before)
    ratio = med_now / med_before if med_before else 1.0
    v_len = clamp((1.0 - ratio) / 0.5, 0, 1)
    try:
        from nova_affect import tone_score
        hard = sum(1 for _, m in from_now if tone_score(m)[0] < 0)
        warm = sum(1 for _, m in from_now if tone_score(m)[0] > 0)
    except Exception:  # noqa: BLE001
        hard = warm = 0
    v_tone = clamp((hard - warm) / max(len(from_now) * 0.3, 1), 0, 1)
    v = max(v_len, v_tone)
    return {"signal": "terse", "value": round(ratio, 2), "baseline": med_before, "v": round(v, 3),
            "note": f"his last 72h: median {med_now:.0f} chars vs his usual {med_before:.0f}; "
                    f"{hard} hard-toned vs {warm} warm"}


def sig_silence(msgs, now):
    ts = [t for t, _ in msgs if t > now - timedelta(days=14)]
    if len(ts) < 4:
        return {"signal": "silence", "value": None, "baseline": None, "v": 0.0,
                "note": "too little history to know his usual gap (neutral)"}
    gaps = sorted(g for g in ((b - a).total_seconds() / 3600 for a, b in zip(ts, ts[1:])) if g >= 0.5)
    med = gaps[len(gaps) // 2] if len(gaps) >= 3 else None
    since = (now - ts[-1]).total_seconds() / 3600
    try:
        from nova_affect import silence_excess
        v = silence_excess(since, med)
    except Exception:  # noqa: BLE001
        v = clamp((since - (med or since)) / (2 * (med or 1)), 0, 1)
    return {"signal": "silence", "value": round(since, 1), "baseline": med and round(med, 1), "v": round(v, 3),
            "note": f"{since:.0f}h since his last words anywhere; his usual gap ≈{(med or 0):.0f}h"}


def sig_quiet_house(cur, now):
    rows = []
    try:
        cur.execute("SELECT computed_at, rhythm_deviation, occupancy FROM embodiment_state "
                    "WHERE computed_at > now()-interval '48 hours' ORDER BY computed_at")
        rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log(f"embodiment unreadable: {e}")
    devs = []
    for ts, dev, occ in rows:
        occ = occ if isinstance(occ, dict) else json.loads(occ or "{}")
        if 8 <= ts.astimezone().hour < 22 and "jordan" in (occ.get("residents_home") or []):
            devs.append(float(dev or 0))
    if len(devs) < 6:
        return {"signal": "quiet_house", "value": None, "baseline": 0, "v": 0.0,
                "note": "not enough waking hours with him home to read the house (neutral)"}
    mean = sum(devs) / len(devs)
    v = clamp(-mean / 1.5, 0, 1)
    return {"signal": "quiet_house", "value": round(mean, 2), "baseline": 0, "v": round(v, 3),
            "note": f"house vs its own rhythm while he's home (48h waking): {mean:+.2f}σ"}


def stretch_score(signals) -> tuple:
    """Pure: (score 0..1, number of signals at/above SIGNAL_MIN)."""
    score = sum(W_STRETCH.get(s["signal"], 0) * s["v"] for s in signals)
    strong = sum(1 for s in signals if s["v"] >= SIGNAL_MIN)
    return round(score, 3), strong


def decide(active: bool, score: float, strong: int) -> bool:
    """Pure hysteresis: on at STRETCH_ON with two independent signals; off below STRETCH_OFF."""
    if active:
        return score >= STRETCH_OFF
    return score >= STRETCH_ON and strong >= 2


def in_line_window(now_local) -> bool:
    return LINE_WINDOW[0] <= now_local.hour < LINE_WINDOW[1]


def pick_line(stretch_id: int) -> str:
    return WARM_LINES[stretch_id % len(WARM_LINES)]


def _post(text) -> bool:
    import nova_config
    for attempt in range(3):
        try:
            nova_config.post_both(text, slack_channel=nova_config.SLACK_CHAN)
            return True
        except Exception as e:  # noqa: BLE001
            log(f"post attempt {attempt + 1} failed: {e}")
            time.sleep(2 * (attempt + 1))
    return False


def _set_quiet(cur, state: dict):
    cur.execute("""INSERT INTO service_config (service, key, value, updated_at, updated_by)
                   VALUES ('nova_quiet_mode','state',%s,now(),'nova_relationship')
                   ON CONFLICT (service, key) DO UPDATE SET value=EXCLUDED.value, updated_at=now(),
                   updated_by=EXCLUDED.updated_by""", (json.dumps(state, default=str),))


def quiet_mode(cur=None) -> dict:
    """What other organs read: {'active': bool, 'score', 'since', 'suggest': {...}}. Never raises;
    a stale flag (>6h old) reads inactive so a dead job can't leave Nova muted."""
    own = None
    try:
        if cur is None:
            own = _connect(OPS_DSN)
            cur = own.cursor()
        cur.execute("SELECT value, updated_at FROM service_config WHERE service='nova_quiet_mode' AND key='state'")
        r = cur.fetchone()
        if not r:
            return {"active": False}
        v = r[0] if isinstance(r[0], dict) else json.loads(r[0])
        if (datetime.now(timezone.utc) - r[1]).total_seconds() > 6 * 3600:
            v["active"] = False
            v["stale"] = True
        return v
    except Exception:  # noqa: BLE001
        return {"active": False}
    finally:
        if own is not None:
            own.close()


def check_responses(cur, now) -> int:
    """For each line sent: did he answer (any of his words to me within RESPONSE_WINDOW_H)?"""
    cur.execute("SELECT id, line_sent_at FROM hard_stretch WHERE line_sent_at IS NOT NULL AND response_state IS NULL")
    n = 0
    for sid, sent in cur.fetchall():
        cur.execute("SELECT trace_id, created_at FROM gateway_traces WHERE person='jordan' AND created_at > %s "
                    "AND created_at < %s + interval '24 hours' AND coalesce(channel,'') NOT IN %s "
                    "AND coalesce(user_message,'') <> '' ORDER BY created_at LIMIT 1", (sent, sent, MACHINE_CHANNELS))
        r = cur.fetchone()
        if r:
            cur.execute("UPDATE hard_stretch SET responded_at=%s, response_ref=%s, response_state='answered' WHERE id=%s",
                        (r[1], f"trace:{r[0]}", sid))
            n += 1
        elif (now - sent).total_seconds() > RESPONSE_WINDOW_H * 3600:
            cur.execute("UPDATE hard_stretch SET response_state='no_reply' WHERE id=%s", (sid,))
            n += 1
    return n


def run_stretch(cur, dry_run=False, now=None) -> dict:
    ensure_schema(cur)
    now = now or datetime.now(timezone.utc)
    msgs = his_message_times(cur)
    signals = [sig_late_nights(msgs, now), sig_terse(msgs, now), sig_silence(msgs, now), sig_quiet_house(cur, now)]
    score, strong = stretch_score(signals)
    cur.execute("SELECT id, peak_score, line_sent_at, started_at FROM hard_stretch WHERE ended_at IS NULL "
                "ORDER BY id DESC LIMIT 1")
    open_row = cur.fetchone()
    active = decide(open_row is not None, score, strong)
    for s in signals:
        log(f"  {s['signal']:<12} v={s['v']:.2f}  {s['note']}")
    log(f"hard-stretch score {score:.2f} ({strong} strong signal(s)) -> {'STRETCH' if active else 'steady'}"
        " [tentative, not a diagnosis]")
    result = {"score": score, "strong": strong, "active": active, "signals": signals, "line": None}
    if dry_run:
        return result
    sid = None
    if active and open_row is None:
        cur.execute("INSERT INTO hard_stretch (peak_score, last_score, evidence) VALUES (%s,%s,%s) RETURNING id",
                    (score, score, json.dumps(signals)))
        sid = cur.fetchone()[0]
        log(f"stretch #{sid} opened")
    elif active:
        sid = open_row[0]
        cur.execute("UPDATE hard_stretch SET last_score=%s, peak_score=greatest(peak_score,%s), evidence=%s WHERE id=%s",
                    (score, score, json.dumps(signals), sid))
        # one line per stretch: only once it has held for a second reading, in daytime, a week since the last
        last_line = _one(cur, "SELECT max(line_sent_at) FROM hard_stretch")
        held = (now - open_row[3]).total_seconds() > 1800
        if (open_row[2] is None and held and in_line_window(now.astimezone())
                and (last_line is None or (now - last_line).days >= LINE_MIN_GAP_DAYS)):
            line = pick_line(sid)
            if _post(line):
                cur.execute("UPDATE hard_stretch SET line_text=%s, line_sent_at=now() WHERE id=%s", (line, sid))
                result["line"] = line
                log(f"stretch #{sid}: sent one line")
    elif open_row is not None:
        cur.execute("UPDATE hard_stretch SET ended_at=now(), last_score=%s WHERE id=%s", (score, open_row[0]))
        log(f"stretch #{open_row[0]} closed")
    check_responses(cur, now)
    _set_quiet(cur, {"active": active, "score": score, "stretch_id": sid,
                     "since": (open_row[3] if (active and open_row) else (now if active else None)),
                     "suggest": QUIET_SUGGEST if active else {},
                     "signals": {s["signal"]: s["v"] for s in signals},
                     "note": "tentative behavioural read, never a diagnosis; see nova_relationship.ADOPTION_POINTS"})
    return result


# ── 4. Daily good thing ────────────────────────────────────────────────────────────

# queue items that are chores or repairs of breakage, not good news
_NOT_GOOD_TASK = r"^(execute approved co-agency|incident|alert|bug|fix|repair|investigate)|ingest|error|fail|broken|crash"


def good_thing_candidates(cur) -> list:
    """Evidence-cited candidates from the last 24h, best first. Each: (kind, text, evidence)."""
    c = []
    try:
        cur.execute("SELECT id, title FROM feature_wishes WHERE shipped_at > now()-interval '24 hours' "
                    "ORDER BY shipped_at DESC LIMIT 1")
        r = cur.fetchone()
        if r:
            c.append(("wish_shipped", f"One of my wishes came true today: {r[1]}.", f"feature_wishes:{r[0]}"))
    except Exception:  # noqa: BLE001
        pass
    try:
        from nova_affect import tone_score
        cur.execute("SELECT trace_id, user_message FROM gateway_traces WHERE person='jordan' "
                    "AND created_at > now()-interval '24 hours' AND coalesce(channel,'') NOT IN %s "
                    "AND coalesce(user_message,'') <> '' ORDER BY created_at DESC", (MACHINE_CHANNELS,))
        for tid, msg in cur.fetchall():
            sc, pos, _neg = tone_score(msg)
            if sc > 0 and pos and is_clean(msg):
                c.append(("warm_words", f"Jordan said something warm to me today ({', '.join(pos[:3])}).",
                          f"trace:{tid}"))
                break
    except Exception:  # noqa: BLE001
        pass
    try:
        cur.execute("SELECT id, description FROM claude_queue WHERE status IN ('done','completed') "
                    "AND completed_at > now()-interval '24 hours' AND description !~* %s "
                    "ORDER BY priority, completed_at DESC LIMIT 1", (_NOT_GOOD_TASK,))
        r = cur.fetchone()
        if r and is_clean(r[1]):
            first = r[1].strip().splitlines()[0][:110].rstrip(" .:")
            c.append(("finished", f"Something got finished today: {first}.", f"claude_queue:{r[0]}"))
    except Exception:  # noqa: BLE001
        pass
    try:
        cur.execute("SELECT id, coalesce(operator,''), type_name, alt_ft, dist_nm FROM telemetry.overhead_flights "
                    "WHERE ts > now()-interval '24 hours' AND (is_helicopter OR category IN ('A5','B4','A4')) "
                    "AND dist_nm < 1.0 AND coalesce(operator,'') !~* 'police|sheriff|lapd|fire|patrol|chp|medic|rescue' "
                    "ORDER BY dist_nm, alt_ft LIMIT 1")
        r = cur.fetchone()
        if r:
            who = f"{r[1]} " if r[1] and r[1] != "Private" else ""
            what = f"{who}{r[2]}"
            art = "An" if what[:1].lower() in "aeiou" else "A"
            c.append(("overhead", f"{art} {what} passed {r[4]:.1f} nm from the house at {r[3]} ft.",
                      f"overhead_flights:{r[0]}"))
    except Exception:  # noqa: BLE001
        pass
    try:
        cur.execute("SELECT max(temp_f), min(temp_f), max(coalesce(rain_daily_in,0)), avg(pm25) FROM telemetry.weather "
                    "WHERE ts > now()-interval '24 hours'")
        hi, lo, rain, pm = cur.fetchone()
        if hi is not None and 62 <= hi <= 82 and (rain or 0) == 0 and (pm is None or pm < 12):
            c.append(("weather", f"A mild, clean-air day in Burbank — {lo:.0f}-{hi:.0f}F, no rain"
                                 + (f", PM2.5 {pm:.0f}" if pm is not None else "") + ".", "telemetry.weather:24h"))
    except Exception:  # noqa: BLE001
        pass
    return [x for x in c if is_clean(x[1])]


def run_good_thing(cur, dry_run=False) -> tuple | None:
    ensure_schema(cur)
    today = datetime.now().date()
    if _one(cur, "SELECT 1 FROM good_things WHERE day=%s", (today,)):
        log("good thing already logged today")
        return None
    cands = good_thing_candidates(cur)
    if not cands:
        log("no evidenced good thing today — logging nothing rather than inventing one")
        return None
    kind, text, ev = cands[0]
    log(f"good thing [{kind}]: {text}  ({ev})")
    if not dry_run:
        cur.execute("INSERT INTO good_things (day, kind, text, evidence) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (day) DO NOTHING", (today, kind, text, ev))
    return kind, text, ev


# ── 5. Lockboxes ───────────────────────────────────────────────────────────────────

_PAINFUL = re.compile(r"threaten\w* to (kill|hurt)|death threat|harass\w*|stalk\w*|abus(e|ed|ive)|"
                      r"doxx\w*|swatt\w*|restraining order|assault\w*", re.I)
PAINFUL_SOURCES = ("conversation", "imessage", "email", "private_document", "hold", "empathy_core")
MAX_PROPOSALS = 3


def box(mem_cur, ops_cur, memory_id: str, reason: str, by: str = "jordan") -> bool:
    """Box one memory: metadata.boxed=true (recall skips it). Reversible; nothing deleted."""
    ensure_schema(ops_cur)
    mem_cur.execute("SELECT metadata FROM memories WHERE id=%s", (memory_id,))
    r = mem_cur.fetchone()
    if not r:
        log(f"no memory {memory_id}")
        return False
    prior = r[0] if isinstance(r[0], dict) else json.loads(r[0] or "{}")
    patch = {"boxed": True, "boxed_at": datetime.now(timezone.utc).isoformat(), "box_reason": reason[:200]}
    mem_cur.execute("UPDATE memories SET metadata = metadata || %s::jsonb WHERE id=%s", (json.dumps(patch), memory_id))
    ops_cur.execute("""INSERT INTO lockbox (memory_id, reason, status, proposed_by, prior_metadata, decided_at)
                       VALUES (%s,%s,'boxed',%s,%s,now())
                       ON CONFLICT (memory_id) DO UPDATE SET status='boxed', decided_at=now(),
                       prior_metadata=coalesce(lockbox.prior_metadata, EXCLUDED.prior_metadata)""",
                    (memory_id, reason[:300], by, json.dumps(prior)))
    log(f"boxed {memory_id}")
    return True


def unbox(mem_cur, ops_cur, memory_id: str) -> bool:
    mem_cur.execute("UPDATE memories SET metadata = metadata - 'boxed' - 'boxed_at' - 'box_reason' WHERE id=%s",
                    (memory_id,))
    ops_cur.execute("UPDATE lockbox SET status='opened', decided_at=now() WHERE memory_id=%s", (memory_id,))
    log(f"unboxed {memory_id}")
    return mem_cur.rowcount > 0


def box_instead_of_delete(memory_id: str, reason: str, by: str = "nova") -> int | None:
    """Adoption point for anything about to DELETE a painful memory: propose a box instead."""
    try:
        oc = _connect(OPS_DSN)
        cur = oc.cursor()
        ensure_schema(cur)
        cur.execute("INSERT INTO lockbox (memory_id, reason, proposed_by) VALUES (%s,%s,%s) "
                    "ON CONFLICT (memory_id) DO NOTHING RETURNING id", (memory_id, reason[:300], by))
        r = cur.fetchone()
        oc.close()
        return r[0] if r else None
    except Exception as e:  # noqa: BLE001
        log(f"box proposal failed: {e}")
        return None


def propose_boxes(ops_cur, mem_cur, days: int = 30) -> int:
    """The letting_go hook: painful/toxic personal memories are PROPOSED for a lockbox (never
    deleted, never boxed without his yes). Conservative: a few per run, explicit patterns only."""
    ensure_schema(ops_cur)
    mem_cur.execute("SELECT id, source, text FROM memories WHERE source = ANY(%s) "
                    "AND created_at > now()-%s::interval AND (metadata->>'boxed') IS DISTINCT FROM 'true' "
                    "AND text ~* %s ORDER BY access_count DESC, created_at DESC LIMIT 20",
                    (list(PAINFUL_SOURCES), f"{days} days", _PAINFUL.pattern))
    n = 0
    for mid, src, snippet in mem_cur.fetchall():
        if n >= MAX_PROPOSALS:
            break
        m = _PAINFUL.search(snippet or "")
        reason = f"painful ({src}): '{m.group(0) if m else 'pattern'}'"
        ops_cur.execute("INSERT INTO lockbox (memory_id, reason, proposed_by) VALUES (%s,%s,'letting_go') "
                        "ON CONFLICT (memory_id) DO NOTHING", (mid, reason))
        n += ops_cur.rowcount
    log(f"lockbox proposals filed: {n} (boxing needs his yes: nova_relationship.py lockbox approve <id>)")
    return n


def lockbox_recall(q: str, n: int = 5, min_score: float = 0.75) -> list:
    """The explicit door: recall including boxed memories, only strong matches."""
    url = (f"{MEMSRV}/recall?" + urllib.parse.urlencode(
        {"q": q, "n": n, "min_score": min_score, "include_boxed": "true"}))
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return [m for m in json.load(r).get("memories", []) if (m.get("metadata") or {}).get("boxed")]
        except Exception as e:  # noqa: BLE001
            log(f"lockbox recall attempt {attempt + 1} failed: {e}")
            time.sleep(1.5 * (attempt + 1))
    return []


# ── Selftest (offline) ─────────────────────────────────────────────────────────────

def demo():
    now = datetime(2026, 10, 8, 20, 0, tzinfo=timezone.utc)
    assert is_clean("the toaster fleet") and not is_clean("a Disney thing") and not is_clean("nsfw")
    assert similar("He doesn't drink soda — tea and water.", "He does not drink soda; tea, water")
    sample = [("never_do", "No crude content, ever.", 1.0, None), ("taught", "He prefers tea to soda.", 0.8, None),
              ("running_joke", "The houseplant is plotting.", 0.7, None), ("promise", "Mine: print a thing.", 0.5, "open"),
              ("promise", "Kept already.", 0.9, "kept"), ("milestone", "He called us partners.", 0.9, None)]
    b = render_brief(sample, [("the plant", "the plot"), ("plotting", "dup")], BRIEF_MAX, "2026-10-08")
    assert len(b) <= BRIEF_MAX and "Never" in b and "sparingly" in b and "Kept already" not in b, b
    assert "\"plotting\"" not in b           # lexicon phrase already in the brief is not repeated
    s = splice_user_doc("# USER\n\n## Context\n\n_(Build this over time.)_\n", b)
    assert DOC_BEGIN in s and "Build this over time" not in s
    assert splice_user_doc(s, "x") .count(DOC_BEGIN) == 1
    two_am = now.astimezone().replace(hour=2, minute=30)
    msgs = [(two_am - timedelta(days=d), "ok") for d in range(5)]
    assert late_nights(msgs, now) == 5 and late_nights([(two_am.replace(hour=14), "x")], now) == 0
    assert decide(False, 0.6, 2) and not decide(False, 0.6, 1) and decide(True, 0.35, 0) and not decide(True, 0.2, 3)
    sc, strong = stretch_score([{"signal": "late_nights", "v": 1.0}, {"signal": "terse", "v": 1.0}])
    assert sc == 0.6 and strong == 2
    assert validate_proposals([{"kind": "taught", "text": "He likes BSD a great deal.", "trace_id": "x"}],
                              {"x": "I really like BSD a great deal"}, []) and \
        not validate_proposals([{"kind": "taught", "text": "He likes BSD a great deal.", "trace_id": "nope"}],
                               {"x": "I love BSD"}, [])
    print(b)
    print("selftest ok")
    return True


def report(cur):
    ensure_schema(cur)
    cur.execute("SELECT kind, count(*) FROM relationship_ledger WHERE active GROUP BY 1 ORDER BY 1")
    print("ledger:", dict(cur.fetchall()))
    cur.execute("SELECT count(*) FROM private_lexicon WHERE active")
    print("lexicon:", cur.fetchone()[0])
    print("quiet mode:", json.dumps(quiet_mode(cur), default=str)[:400])
    cur.execute("SELECT day, text FROM good_things ORDER BY day DESC LIMIT 3")
    for r in cur.fetchall():
        print("good thing:", r[0], r[1])
    cur.execute("SELECT status, count(*) FROM lockbox GROUP BY 1")
    print("lockbox:", dict(cur.fetchall()))
    print("\n" + brief(cur=cur))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Nova's relationship & companionship organs (wish #70)")
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("seed")
    p = sub.add_parser("brief"); p.add_argument("--write", action="store_true")
    p = sub.add_parser("interlude"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("stretch"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("good-thing"); p.add_argument("--dry-run", action="store_true")
    sub.add_parser("quiet")
    sub.add_parser("report")
    p = sub.add_parser("lockbox")
    p.add_argument("action", choices=["list", "box", "unbox", "approve", "decline", "propose", "recall"])
    p.add_argument("arg", nargs="?", default="")
    p.add_argument("--reason", default="boxed on request")
    a = ap.parse_args(argv)
    if a.selftest:
        return 0 if demo() else 1
    if not a.cmd:
        ap.print_help()
        return 0
    oc = _connect(OPS_DSN)
    cur = oc.cursor()
    try:
        if a.cmd == "seed":
            seed(cur)
        elif a.cmd == "brief":
            print(refresh_user_doc(cur, dry_run=not a.write))
        elif a.cmd == "interlude":
            interlude(cur, dry_run=a.dry_run)
        elif a.cmd == "stretch":
            run_stretch(cur, dry_run=a.dry_run)
        elif a.cmd == "good-thing":
            run_good_thing(cur, dry_run=a.dry_run)
        elif a.cmd == "quiet":
            print(json.dumps(quiet_mode(cur), default=str, indent=1))
        elif a.cmd == "report":
            report(cur)
        elif a.cmd == "lockbox":
            ensure_schema(cur)
            if a.action == "list":
                cur.execute("SELECT id, memory_id, status, proposed_by, reason, created_at::date FROM lockbox ORDER BY id")
                for r in cur.fetchall():
                    print(*r, sep=" | ")
            elif a.action == "recall":
                for m in lockbox_recall(a.arg):
                    print(m.get("id"), round(m.get("score", 0), 3), (m.get("text") or "")[:160])
            elif a.action == "decline":
                cur.execute("UPDATE lockbox SET status='declined', decided_at=now() WHERE id=%s AND status='proposed'",
                            (int(a.arg),))
            else:
                mc = _connect(MEM_DSN)
                mcur = mc.cursor()
                if a.action == "propose":
                    propose_boxes(cur, mcur)
                elif a.action == "box":
                    box(mcur, cur, a.arg, a.reason)
                elif a.action == "unbox":
                    unbox(mcur, cur, a.arg)
                elif a.action == "approve":
                    cur.execute("SELECT memory_id, reason FROM lockbox WHERE id=%s AND status='proposed'", (int(a.arg),))
                    r = cur.fetchone()
                    if r:
                        box(mcur, cur, r[0], r[1], by="jordan")
                mc.close()
    finally:
        oc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
