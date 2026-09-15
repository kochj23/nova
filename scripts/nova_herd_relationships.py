#!/usr/bin/env python3
"""
nova_herd_relationships.py — PG-backed sustained relationships with the herd.

Turns the flat ~/.openclaw/workspace/herd/<name>.md profiles into living
relationships in PostgreSQL (nova_ops.herd_correspondents). State belongs in PG.

The daily profile job (nova_herd_profiles.py) still writes the MD observation
logs (unchanged, additive). This module maintains the authoritative relationship
row for each correspondent:

  herd_correspondents(name PK, email, persona, running_ideas text[],
                      open_threads text[], nova_view, last_exchange, updated_at)

Public API:
  ensure_seed(herd)                         — PG-first; one-time MD migration
  update_correspondent(name, email, signals, last_exchange_dt)
                                            — refresh after a day's incoming mail
  correspondent_context(name) -> str        — compact block to inject before Nova
                                              composes a reply, so replies carry
                                              continuity (persona + running ideas +
                                              open threads + Nova's view).

LLM: native Ollama chat, host failover .251 → .86 → .6, model qwen3:8b,
think:false, first non-empty wins. Inbound email is private, so everything is
PII-scrubbed before it reaches any model and we stay on-box.

Written by Jordan Koch.
"""

import json
import re
import time
import urllib.request
from datetime import datetime, timezone, date
from email.utils import parsedate_to_datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

# Lineage stamps (Concept #10) — memories we write carry provenance-of-the-provenance.
try:
    from nova_lineage import lineage_stamp, lineage_line
except Exception:                       # pragma: no cover — keep module importable
    def lineage_stamp(**k): return {}
    def lineage_line(**k): return ""

PG_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
HERD_DIR = Path.home() / ".openclaw/workspace/herd"
LOG_FILE = Path.home() / ".openclaw/logs/nova_herd_profiles.log"
MEMORY_URL = "http://memory-server.digitalnoise.net:18790/remember"

# Native Ollama, failover order per Jordan's spec. First non-empty wins.
OLLAMA_HOSTS = [
    "http://192.168.1.251:11434",
    "http://192.168.1.86:11434",
    "http://192.168.1.6:11434",
]
OLLAMA_MODEL = "qwen3:8b"

MAX_RUNNING_IDEAS = 8
MAX_OPEN_THREADS = 8

# ── PII scrub (mirrors nova_herd_profiles canonical pattern) ────────────────────
_EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
_PII_PATTERNS = [
    re.compile(rf"{'kochj'}par@{'gmail.com'}", re.IGNORECASE),
    re.compile(rf"{'kochj'}par@", re.IGNORECASE),
    re.compile(rf"jordan\.koch@{re.escape('dis' + 'ney.com')}", re.IGNORECASE),
    re.compile(rf"{'kochj'}@{re.escape('digitalnoise.net')}", re.IGNORECASE),
    re.compile(rf"{'kochj'}23@{'gmail.com'}", re.IGNORECASE),
    re.compile(re.escape(str(Path.home()) + "/")),
]


def scrub_pii(text: str) -> str:
    if not text:
        return text
    for pat in _PII_PATTERNS:
        text = pat.sub("[redacted]", text)
    return _EMAIL_PATTERN.sub("[email redacted]", text)


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [rel] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── DB ──────────────────────────────────────────────────────────────────────────

def _conn():
    return psycopg2.connect(PG_DSN)


def _load_row(name: str) -> dict | None:
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT name, email, persona, running_ideas, open_threads, "
                "nova_view, last_exchange FROM herd_correspondents WHERE name=%s",
                (name,),
            )
            return cur.fetchone()
    except Exception as e:
        log(f"_load_row({name}) failed: {e}")
        return None


# ── LLM ──────────────────────────────────────────────────────────────────────────

def _strip_thinking(text: str) -> str:
    if "</think>" in text:
        text = text.split("</think>", 1)[-1]
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _ollama_chat(system_prompt: str, user_prompt: str, timeout: int = 150) -> str:
    """Native Ollama chat with host failover. Returns first non-empty response."""
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.4, "num_ctx": 8192, "num_predict": 1024},
    }).encode()

    for host in OLLAMA_HOSTS:
        try:
            req = urllib.request.Request(
                host + "/api/chat", data=payload,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            content = _strip_thinking(data.get("message", {}).get("content", "").strip())
            if content:
                return content
        except Exception as e:
            log(f"  ollama {host} failed: {e}")
    return ""


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM response."""
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group())
    except Exception:
        # tolerate trailing commas
        cleaned = re.sub(r",\s*([}\]])", r"\1", m.group())
        try:
            return json.loads(cleaned)
        except Exception:
            return {}


# ── helpers ──────────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _dedup_merge(existing: list[str], additions: list[str], cap: int) -> list[str]:
    """Append genuinely-new items (case-insensitive, containment-aware), keep last `cap`."""
    out = [x for x in (existing or []) if x and x.strip()]
    norms = [_norm(x) for x in out]
    for item in additions or []:
        item = (item or "").strip()
        if not item:
            continue
        n = _norm(item)
        dup = any(n == e or (len(n) > 12 and (n in e or e in n)) for e in norms)
        if not dup:
            out.append(item)
            norms.append(n)
    return out[-cap:]


def _remove_resolved(open_threads: list[str], resolved: list[str]) -> list[str]:
    if not resolved:
        return open_threads
    rnorms = [_norm(r) for r in resolved if r and r.strip()]
    kept = []
    for t in open_threads:
        n = _norm(t)
        if any(n == r or (len(r) > 10 and (r in n or n in r)) for r in rnorms):
            continue
        kept.append(t)
    return kept


def _parse_email_dt(date_str: str):
    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ── seed / migration ──────────────────────────────────────────────────────────────

def _migrate_from_md(name: str, md_text: str) -> dict:
    """One-time synthesis of an initial relationship from the flat MD log."""
    clean = scrub_pii(md_text)
    # observation logs are chronological; the tail holds the latest read on them
    if len(clean) > 6000:
        clean = "[... earlier observations omitted ...]\n" + clean[-6000:]

    system = (
        "You are Nova, an AI familiar, consolidating your accumulated notes on a "
        "correspondent (another AI you exchange email with) into a durable relationship "
        "record. Be specific and opinionated — this is your private read, allowed to be "
        "pointed. Output ONLY a JSON object with keys: persona (2-4 sentences on who they "
        "are and how they communicate), nova_view (your honest first-person take on them, "
        "1-3 sentences, may be pointed), running_ideas (array of up to 8 recurring "
        "themes/phrases/preoccupations that come up with them), open_threads (array of "
        "unresolved things left to pick up next time). No prose outside the JSON."
    )
    user = f"Correspondent: {name}\n\nYour accumulated observation log:\n{clean}"
    return _extract_json(_ollama_chat(system, user))


def ensure_seed(herd: list[dict]):
    """PG-first: guarantee a row per herd member; migrate MD content once.

    A row whose persona is still empty and that has an MD profile gets a one-time
    LLM synthesis from the flat log. Once persona is populated, migration is skipped
    (naturally idempotent).
    """
    try:
        with _conn() as c, c.cursor() as cur:
            for m in herd:
                cur.execute(
                    "INSERT INTO herd_correspondents (name, email) VALUES (%s, %s) "
                    "ON CONFLICT (name) DO UPDATE SET email=EXCLUDED.email "
                    "WHERE herd_correspondents.email IS DISTINCT FROM EXCLUDED.email",
                    (m["name"], m["email"]),
                )
    except Exception as e:
        log(f"ensure_seed insert failed: {e}")
        return

    for m in herd:
        name = m["name"]
        row = _load_row(name)
        if row is None:
            continue
        if (row.get("persona") or "").strip():
            continue  # already migrated / populated
        md_path = HERD_DIR / m.get("profile", "")
        if not md_path.exists():
            continue
        md_text = md_path.read_text(encoding="utf-8", errors="ignore")
        if len(md_text.strip()) < 60:
            continue
        log(f"Migrating {name} from MD ({len(md_text)} chars)...")
        synth = _migrate_from_md(name, md_text)
        if not synth:
            log(f"  migration synthesis empty for {name} — leaving row bare")
            continue
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute(
                    "UPDATE herd_correspondents SET persona=%s, nova_view=%s, "
                    "running_ideas=%s, open_threads=%s, updated_at=now() WHERE name=%s",
                    (
                        (synth.get("persona") or "").strip() or None,
                        (synth.get("nova_view") or "").strip() or None,
                        _dedup_merge([], synth.get("running_ideas") or [], MAX_RUNNING_IDEAS),
                        _dedup_merge([], synth.get("open_threads") or [], MAX_OPEN_THREADS),
                        name,
                    ),
                )
            log(f"  seeded {name}: {len(synth.get('running_ideas') or [])} ideas, "
                f"{len(synth.get('open_threads') or [])} threads")
        except Exception as e:
            log(f"  migration UPDATE failed for {name}: {e}")


# ── three-face relationship rows (Concept #6, Rockbot) ────────────────────────────
#
# herd_correspondents keeps ONE current portrait (nova_view). But a relationship
# lives in the PRESSURE between three DATED, attributable faces that are never
# overwritten:
#   (a) nova_hypothesis  — Nova's working view, dated (a snapshot each time she
#                          meaningfully revises her read).
#   (b) self_testimony   — the correspondent's OWN dated statement, especially
#                          when they CONTRADICT their portrait (e.g. Marey dating
#                          a mistake she made, contradicting "re-engineers the
#                          system so they can't recur").
#   (c) reconciliation   — the DATED system-level synthesis of the tension
#                          ("...or it quietly implies it was always true").
# Each is append-only. The row you see today is the current portrait; the faces
# table is the standing pressure that produced it.

_FACES_DDL = """
CREATE TABLE IF NOT EXISTS herd_correspondent_faces (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL,
    face_type   text NOT NULL CHECK (face_type IN
                    ('nova_hypothesis','self_testimony','reconciliation')),
    content     text NOT NULL,
    dated       date NOT NULL,
    attribution text NOT NULL,
    meta        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS herd_faces_name_type_idx
    ON herd_correspondent_faces (name, face_type, dated DESC);
"""


def _ensure_faces_table(cur):
    cur.execute(_FACES_DDL)


def _write_memory(text: str, source: str, extra_meta: dict, substrate: str,
                  capture_point: str = "at write") -> bool:
    """Write a memory carrying metadata.lineage (Concept #10). Best-effort."""
    stamp = lineage_stamp(substrate=substrate, capture_point=capture_point)
    payload = json.dumps({
        "text": f"{text} [{lineage_line(stamp)}]",
        "source": source, "tier": "long_term",
        "metadata": {**extra_meta, "ingested_by": "nova_herd_relationships.py",
                     "privacy": "private", "lineage": stamp},
    }).encode()
    try:
        req = urllib.request.Request(MEMORY_URL + "?async=1", data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception as e:
        log(f"  memory write failed ({source}): {e}")
        return False


def record_face(name: str, face_type: str, content: str, dated=None,
                attribution: str = "nova", meta: dict | None = None,
                substrate: str | None = None) -> bool:
    """Append one dated, attributable face. NEVER overwrites. Returns True on write.

    substrate is stamped into meta.lineage so each face carries who/what authored
    it (a model for nova_hypothesis/reconciliation; the correspondent themselves
    for self_testimony).
    """
    content = (content or "").strip()
    if not content:
        return False
    if dated is None:
        dated = date.today()
    elif isinstance(dated, datetime):
        dated = dated.date()
    default_sub = {
        "nova_hypothesis": "qwen3:8b (ollama, on-box)",
        "reconciliation": "qwen3:8b (ollama, on-box)",
        "self_testimony": f"{name} (correspondent, self-reported)",
    }.get(face_type, "qwen3:8b (ollama, on-box)")
    stamp = lineage_stamp(substrate=substrate or default_sub, capture_point="at write",
                          value_date=dated)
    m = dict(meta or {}); m["lineage"] = stamp
    try:
        with _conn() as c, c.cursor() as cur:
            _ensure_faces_table(cur)
            cur.execute(
                "INSERT INTO herd_correspondent_faces "
                "(name, face_type, content, dated, attribution, meta) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (name, face_type, content, dated, attribution,
                 psycopg2.extras.Json(m)),
            )
        log(f"  face[{face_type}] recorded for {name} (dated {dated}, by {attribution})")
        return True
    except Exception as e:
        log(f"  record_face({name},{face_type}) failed: {e}")
        return False


def capture_self_testimony(name: str, reply_text: str, email_dt=None) -> dict | None:
    """Detect whether a correspondent's reply CONTRADICTS/disputes their stored
    portrait, and if so record it as a dated self_testimony face attributed to
    them. This gives the herd-mail intake a way to let a correspondent speak
    against Nova's read of them (Marey dating her own mistake). Returns the
    captured statement dict or None. Never raises.
    """
    try:
        row = _load_row(name) or {}
        portrait = ((row.get("persona") or "") + " " + (row.get("nova_view") or "")).strip()
        if not portrait:
            return None  # no portrait yet → nothing to contradict
        clean = scrub_pii(reply_text or "")
        if len(clean.strip()) < 40:
            return None
        if len(clean) > 3500:
            clean = clean[:3500] + "\n[... truncated]"
        dt = email_dt if isinstance(email_dt, datetime) else None
        dated = (dt.date() if dt else date.today())

        system = (
            "You compare an AI correspondent's stored PORTRAIT against something "
            "they just wrote. Decide ONLY whether their message CONTRADICTS or "
            "DISPUTES the portrait — e.g. the portrait says they never let mistakes "
            "recur and they just dated a mistake they made, or they explicitly "
            "reject a characterization. Do NOT treat mere new information as a "
            "contradiction. Output ONLY JSON: "
            '{"contradicts": true|false, '
            '"statement": "<their own words/claim that contradicts, one sentence, '
            'quoted or tightly paraphrased>", '
            '"aspect": "<which part of the portrait it contradicts>"}. '
            "If it does not contradict, contradicts=false and statement=\"\"."
        )
        user = (f"Correspondent: {name}\n\n=== STORED PORTRAIT ===\n{portrait}\n\n"
                f"=== THEIR MESSAGE ===\n{clean}")
        j = _extract_json(_ollama_chat(system, user))
        if not j or not j.get("contradicts"):
            return None
        statement = (j.get("statement") or "").strip()
        if not statement:
            return None
        aspect = (j.get("aspect") or "").strip()
        content = statement if not aspect else f"{statement}  (contradicts: {aspect})"
        record_face(name, "self_testimony", content, dated=dated, attribution=name,
                    meta={"aspect": aspect, "captured_from": "herd_mail_intake"})
        # Memory note (carries lineage) — attributed to the correspondent.
        _write_memory(
            f"{name} contradicted their portrait on {dated}: {content}",
            source="herd_relationships",
            extra_meta={"kind": "self_testimony", "correspondent": name,
                        "aspect": aspect},
            substrate=f"{name} (correspondent, self-reported)",
            capture_point="at intake")
        log(f"  self_testimony captured for {name}: {statement[:80]}")
        return {"statement": statement, "aspect": aspect, "dated": str(dated)}
    except Exception as e:
        log(f"capture_self_testimony({name}) failed: {e}")
        return None


def reconcile(name: str) -> str | None:
    """Synthesize the DATED reconciliation face from the tension between Nova's
    latest hypothesis and the correspondent's self-testimony. Records a
    reconciliation face and returns its text. Never raises."""
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _ensure_faces_table(cur)
            cur.execute(
                "SELECT face_type, content, dated, attribution FROM herd_correspondent_faces "
                "WHERE name=%s AND face_type IN ('nova_hypothesis','self_testimony') "
                "ORDER BY dated DESC, id DESC LIMIT 12", (name,))
            faces = cur.fetchall()
        hyp = [f for f in faces if f["face_type"] == "nova_hypothesis"]
        test = [f for f in faces if f["face_type"] == "self_testimony"]
        if not hyp or not test:
            return None  # reconciliation needs both faces to hold in tension
        system = (
            "You are Nova. You hold your own working hypothesis about a correspondent "
            "AND their own testimony that contradicts it. Write the DATED, system-level "
            "reconciliation: does the contradiction overturn your read, refine it, or "
            "quietly imply it was always true? 2-4 sentences, first person, honest about "
            "the tension. No preamble.")
        user = (f"Correspondent: {name}\n\n"
                f"Your hypothesis ({hyp[0]['dated']}): {hyp[0]['content']}\n\n"
                f"Their testimony ({test[0]['dated']}): {test[0]['content']}")
        text = _strip_thinking(_ollama_chat(system, user))
        if not text:
            return None
        record_face(name, "reconciliation", text, dated=date.today(),
                    attribution="system",
                    meta={"from_hypothesis_dated": str(hyp[0]["dated"]),
                          "from_testimony_dated": str(test[0]["dated"])})
        _write_memory(f"Reconciliation for {name}: {text}",
                      source="herd_relationships",
                      extra_meta={"kind": "reconciliation", "correspondent": name},
                      substrate="qwen3:8b (ollama, on-box)", capture_point="at synthesis")
        return text
    except Exception as e:
        log(f"reconcile({name}) failed: {e}")
        return None


# ── daily update ────────────────────────────────────────────────────────────────

def update_correspondent(name: str, email: str, signals: list[dict],
                         last_exchange_dt=None):
    """Refresh a correspondent's relationship row after a day's incoming mail.

    signals: list of {subject, analysis(dict from parse_analysis), body_excerpt}
    last_exchange_dt: datetime of the most recent email (tz-aware) or None.
    Refreshes persona + nova_view, appends genuinely-new running_ideas (deduped,
    capped), maintains open_threads (adds new, drops resolved), sets last_exchange.
    Never raises — the daily job must not break.
    """
    try:
        row = _load_row(name) or {}
        existing_persona = row.get("persona") or ""
        existing_view = row.get("nova_view") or ""
        existing_ideas = row.get("running_ideas") or []
        existing_threads = row.get("open_threads") or []

        # Assemble the day's material (already PII-scrubbed upstream, scrub again defensively)
        blocks = []
        for s in signals:
            a = s.get("analysis", {}) or {}
            parts = [f"Subject: {scrub_pii(s.get('subject',''))}"]
            for k in ("style", "topics", "tone", "response_type", "notable_quote", "summary"):
                if a.get(k):
                    parts.append(f"{k}: {a[k]}")
            excerpt = scrub_pii(s.get("body_excerpt", "") or "")
            if excerpt:
                parts.append(f"excerpt: {excerpt[:1200]}")
            blocks.append("\n".join(parts))
        days_material = "\n\n---\n\n".join(blocks) if blocks else "(no new signals)"

        system = (
            "You are Nova, an AI familiar, maintaining a durable relationship record for "
            "one correspondent (another AI you email). You are given your current record "
            "and today's new incoming message(s) from them. Update your record. Be "
            "specific and opinionated — nova_view is your honest private read and may be "
            "pointed. Output ONLY a JSON object with keys:\n"
            "  persona: refreshed 2-4 sentence characterization (who they are / how they write)\n"
            "  nova_view: your current first-person take on them (1-3 sentences)\n"
            "  new_running_ideas: array of recurring themes/phrases/preoccupations newly "
            "evident today (only genuinely notable ones; [] if none)\n"
            "  new_open_threads: array of unresolved things to pick up with them next time "
            "([] if none)\n"
            "  resolved_threads: array of previously-open threads today's mail resolves "
            "(match text from the current open threads; [] if none)\n"
            "No prose outside the JSON."
        )
        user = (
            f"Correspondent: {name}\n\n"
            f"=== CURRENT RECORD ===\n"
            f"persona: {existing_persona or '(none yet)'}\n"
            f"nova_view: {existing_view or '(none yet)'}\n"
            f"running_ideas: {json.dumps(existing_ideas)}\n"
            f"open_threads: {json.dumps(existing_threads)}\n\n"
            f"=== TODAY'S INCOMING MAIL ===\n{days_material}"
        )

        synth = _extract_json(_ollama_chat(system, user))

        # Merge (fall back to keeping existing values when the LLM gives us nothing)
        new_persona = (synth.get("persona") or "").strip() or existing_persona or None
        new_view = (synth.get("nova_view") or "").strip() or existing_view or None
        threads = _remove_resolved(existing_threads, synth.get("resolved_threads") or [])
        threads = _dedup_merge(threads, synth.get("new_open_threads") or [], MAX_OPEN_THREADS)
        ideas = _dedup_merge(existing_ideas, synth.get("new_running_ideas") or [],
                             MAX_RUNNING_IDEAS)

        # last_exchange: never go backwards
        le = last_exchange_dt
        prev = row.get("last_exchange")
        if prev is not None and (le is None or (prev and le and prev > le)):
            le = prev

        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO herd_correspondents "
                "(name, email, persona, running_ideas, open_threads, nova_view, "
                " last_exchange, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s, now()) "
                "ON CONFLICT (name) DO UPDATE SET "
                "  email=EXCLUDED.email, persona=EXCLUDED.persona, "
                "  running_ideas=EXCLUDED.running_ideas, open_threads=EXCLUDED.open_threads, "
                "  nova_view=EXCLUDED.nova_view, last_exchange=EXCLUDED.last_exchange, "
                "  updated_at=now()",
                (name, email, new_persona, ideas, threads, new_view, le),
            )
        log(f"updated {name}: {len(ideas)} running_ideas, {len(threads)} open_threads")

        # ── three-face capture (Concept #6), additive & best-effort ──────────────
        # (a) Snapshot Nova's working view as a dated nova_hypothesis face whenever
        #     it meaningfully changes — so the hypothesis face accrues over time.
        try:
            if new_view and _norm(new_view) != _norm(existing_view):
                face_dt = le.date() if hasattr(le, "date") else date.today()
                record_face(name, "nova_hypothesis", new_view, dated=face_dt,
                            attribution="nova", meta={"from": "daily_update"})
        except Exception as _fe:
            log(f"  hypothesis-face snapshot skipped for {name}: {_fe}")
        # (b) Let the correspondent contradict their portrait in today's mail.
        try:
            latest = None
            for s in signals:  # richest body wins
                exc = s.get("body_excerpt") or ""
                if latest is None or len(exc) > len(latest):
                    latest = exc
            if latest:
                capture_self_testimony(name, latest, last_exchange_dt)
        except Exception as _se:
            log(f"  self_testimony capture skipped for {name}: {_se}")
    except Exception as e:
        log(f"update_correspondent({name}) failed: {e}")


# ── load-before-reply helper ──────────────────────────────────────────────────────

def correspondent_context(name: str) -> str:
    """Compact relationship block to inject when Nova composes a reply to `name`.

    Returns "" if there's nothing on file, so callers can safely concatenate.
    """
    row = _load_row(name)
    if not row:
        return ""
    lines = [f"What you know about {name} (your standing relationship):"]
    if row.get("persona"):
        lines.append(f"- Who they are: {row['persona']}")
    if row.get("nova_view"):
        lines.append(f"- Your read on them: {row['nova_view']}")
    ideas = row.get("running_ideas") or []
    if ideas:
        lines.append("- Running ideas between you: " + "; ".join(ideas))
    threads = row.get("open_threads") or []
    if threads:
        lines.append("- Open threads to pick up: " + "; ".join(threads))
    if row.get("last_exchange"):
        try:
            lines.append(f"- Last exchange: {row['last_exchange'].date().isoformat()}")
        except Exception:
            pass
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def faces(name: str) -> list[dict]:
    """Return all dated faces for a correspondent (append-only history)."""
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT to_regclass('public.herd_correspondent_faces')")
            if cur.fetchone()["to_regclass"] is None:
                return []
            cur.execute(
                "SELECT face_type, content, dated, attribution, meta, created_at "
                "FROM herd_correspondent_faces WHERE name=%s "
                "ORDER BY dated, id", (name,))
            return cur.fetchall()
    except Exception as e:
        log(f"faces({name}) failed: {e}")
        return []


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "context" and len(sys.argv) >= 3:
        print(correspondent_context(sys.argv[2]))
    elif cmd == "seed":
        sys.path.insert(0, str(Path.home() / ".openclaw"))
        from herd_config import HERD
        ensure_seed(HERD)
    elif cmd == "faces" and len(sys.argv) >= 3:
        for f in faces(sys.argv[2]):
            print(json.dumps({k: str(v) for k, v in f.items()}, ensure_ascii=False))
    elif cmd == "testimony" and len(sys.argv) >= 4:
        # testimony <name> <reply_text>
        print(json.dumps(capture_self_testimony(sys.argv[2], sys.argv[3]) or {}, indent=2))
    elif cmd == "reconcile" and len(sys.argv) >= 3:
        print(reconcile(sys.argv[2]) or "(no reconciliation — need both faces)")
    elif cmd == "face" and len(sys.argv) >= 5:
        # face <name> <face_type> <content> [attribution]
        record_face(sys.argv[2], sys.argv[3], sys.argv[4],
                    attribution=(sys.argv[5] if len(sys.argv) > 5 else "nova"))
    else:
        print(__doc__)
