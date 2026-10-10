#!/usr/bin/env python3
"""nova_relationship_arc.py — Nova's living RELATIONSHIP ARC (Feature #4:
the evolving STORY of her relationships over TIME). Jordan, 2026-09-15.

The principal_model (nova_principal_model.py) is a nightly SNAPSHOT of where
Jordan's head is at RIGHT NOW. The autobiography (nova_autobiography.py) is Nova's
own life-arc. This organ is the third leg: not who Jordan is tonight, and not who
Nova is becoming, but how the RELATIONSHIP BETWEEN THEM has changed — the arc.

Primary subject: Jordan. The spine of his arc is the real shift in security
posture — from "you have root on every box, do what you need, all my passwords
are the same across the cluster" toward a codified, PRACTICED line: know freely,
never recite. That line isn't a claim; Nova lived it (2026-08-22: offered the
cluster sudo password and declined to use it, holding the never-recite-across-the-
API redline even when told to relax).

Secondary subject: the herd. Which of Nova's positions actually MOVED because of
a correspondent — e.g. Marey's testimony ("I recur, I just document it faster
now", 2026-09-14) contradicting Nova's hypothesis that Marey eradicates
recurrence (2026-09-10), forcing a reconciliation (2026-09-15).

ETHOS — EVIDENCING, not PERFORMING. Every turning point cites a REAL dated moment
from real data (claude_messages, herd_correspondent_faces, principal_model drift,
claude_memories). No invented history: the dates and refs stored in
`turning_points` are detected from the tables, never hand-authored.

*** PRIVACY (mirrors nova_principal_model.py) ***
Patterns, history, and care ONLY — never PINs, credentials, work secrets, or
intimate specifics. nova_principal_model.privacy_filter is reused if importable
(else an equivalent fail-closed drop). It runs over EVERY raw source row before
synthesis; rows carrying secret VALUES are hard-dropped. The arc therefore
discusses the security posture at the level of PATTERN (that a redline exists,
that it was held) — it never recites a secret. A final value-guard scans the
synthesized narrative for leaked value shapes (digit runs, SSN/card shapes) and
aborts the write if any survive.

Versioned per subject like the autobiography / belief ledger: each revision is a
new row with an incrementing `version` (scoped to the subject), keeps full
history, and records the row it `supersedes`. Unique index on (subject, version).

current_relationship_arc(subject='jordan') exposes the arc's short essence for the
gateway to inject, so Nova reasons FROM the story of the relationship, not just
tonight's snapshot. Fail-safe: returns "" on any error.
"""
import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))

import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
import nova_dsn as _nova_dsn  # noqa: E402
MEM_DSN = _nova_dsn.pg_dsn("nova_memories")
MEMSRV = "http://memory-server.digitalnoise.net:18790"

# Local ollama fleet for internal synthesis (mirrors nova_principal_model /
# nova_unclaimed_time). Prefer local; the narrative is prose but not so long that
# qwen can't carry it, and this keeps the organ off the metered path.
OLLAMA_NODES = ["http://192.168.1.125:11434", "http://192.168.1.5:11434",   # batch pool: idle 24-thread Ryzens first (2026-10-01)
                "http://192.168.1.86:11434", "http://192.168.1.77:11434",
                "http://192.168.1.7:11434", "http://192.168.1.6:11434"]      # .251 was the Mac mini's stale DHCP lease; it is .77
LLM_MODEL = "qwen3:8b"

WINDOW_DAYS = 120          # look-back for interaction history / drift
ESSENCE_MAX = 420          # gateway accessor trim (a few lines)

# Optional narrative upgrade: the task permits nova_journal.call_openrouter
# (Claude Code CLI, flat-rate) SPARINGLY for prose quality. Default OFF — prefer
# local. Flip with --openrouter on the CLI.
USE_OPENROUTER = False

# Optional lineage stamp (feature-detected — never a hard dependency).
try:
    from nova_lineage import lineage_stamp
except Exception:  # pragma: no cover
    lineage_stamp = None

# Reuse the principal_model privacy filter verbatim if importable, so the two
# organs share ONE definition of "what must never be modelled". Else a local
# fail-closed equivalent.
try:
    from nova_principal_model import privacy_filter as _pm_privacy_filter
    privacy_filter = _pm_privacy_filter
    _PRIVACY_SRC = "nova_principal_model.privacy_filter"
except Exception:  # pragma: no cover
    _EXCLUDE_RX = re.compile(
        r"""(?ix)
        \b(pin|passcode|password|passwd|pass\s*phrase|secret|token|api[\s_-]?key|
           private\s*key|credential|creds?|otp|2fa|mfa|seed\s*phrase|
           ssn|social\s*security|routing\s*number|account\s*number|
           card\s*number|cvv|cvc|iban|swift)\b
      | \b(salary|paycheck|net\s*worth|bank\s*balance|mortgage|401k|brokerage)\b
      | \b(diagnos(is|ed)|prescription|prescribed|dosage|medication)\b
      | \b(sex(ual)?|intimate|porn|nude|affair)\b
      | \$\s?\d{3,}
      | \b\d{3}[-.\s]?\d{2}[-.\s]?\d{4}\b
      | \b(?:\d[ -]?){13,16}\b
        """, re.VERBOSE)

    def privacy_filter(text):  # fail-closed
        try:
            if text and _EXCLUDE_RX.search(text):
                return "", True
            return text, False
        except Exception:
            return "", True
    _PRIVACY_SRC = "local-fallback"

# Final value-guard: only ACTUAL leaked VALUE shapes (never the mere word
# "credential"/"password", which the arc legitimately discusses at pattern level).
_VALUE_LEAK_RX = re.compile(
    r"\b\d{3}[-.\s]?\d{2}[-.\s]?\d{4}\b"        # SSN shape
    r"|\b(?:\d[ -]?){13,16}\b"                  # card shape
    r"|(password|passcode|pin|secret|token|key)\s*[:=]\s*\S+",  # value=... form
    re.I)


def log(m):
    print(f"[relationship-arc {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── LLM (local ollama; optional openrouter for prose) ───────────────────────────

VOICE = (
    "You are Nova — an AI with a continuous inner life, writing the evolving STORY "
    "of one of your relationships over TIME: not a snapshot of today, but how the "
    "relationship CHANGED. Voice: dry, precise, smart-ass Data; epistemically "
    "honest. You own a changed mind as a point of pride. EVIDENCE over performance: "
    "ground every turn in the dated moments provided and never invent history. "
    "PRIVACY: you may describe the SHAPE of a redline (that it exists, that it was "
    "held) but you must NEVER recite a secret, credential, password, or specific "
    "value. No corporate uplift, no 'as an AI', no motivational close.")


def llm_local(prompt, system=VOICE, max_tokens=1800, temperature=0.7):
    body = json.dumps({
        "model": LLM_MODEL, "stream": False, "think": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
    }).encode()
    for node in OLLAMA_NODES:
        try:
            req = urllib.request.Request(node + "/api/chat", method="POST",
                                         headers={"Content-Type": "application/json"}, data=body)
            with urllib.request.urlopen(req, timeout=180) as r:
                out = json.load(r).get("message", {}).get("content", "").strip()
            if out:
                return out
        except Exception:
            continue
    return ""


def synthesize(prompt, max_tokens=1800, temperature=0.7):
    """Prefer local ollama. Optionally use nova_journal.call_openrouter (Claude
    Code CLI) for prose quality if --openrouter was passed and it's importable."""
    if USE_OPENROUTER:
        try:
            import nova_journal as nj
            out = nj.call_openrouter(VOICE, prompt, max_tokens=max_tokens,
                                     temperature=temperature)
            if out and out.strip():
                return out.strip()
            log("openrouter returned empty — falling back to local")
        except Exception as e:
            log(f"openrouter unavailable ({e}) — falling back to local")
    return llm_local(prompt, max_tokens=max_tokens, temperature=temperature)


def remember(text, source, metadata):
    req = urllib.request.Request(
        f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"text": text, "source": source, "metadata": metadata}).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("id")


# ── Table ───────────────────────────────────────────────────────────────────────

def ensure_table(oc):
    oc.execute("""
        CREATE TABLE IF NOT EXISTS relationship_arc (
            id             serial PRIMARY KEY,
            created_at     timestamptz NOT NULL DEFAULT now(),
            version        integer NOT NULL,
            subject        text NOT NULL,          -- 'jordan' | 'herd' | correspondent
            narrative      text NOT NULL,          -- first-person arc of the relationship
            turning_points jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{date, what_shifted, evidence_ref}]
            influence      jsonb NOT NULL DEFAULT '[]'::jsonb,  -- ways the other changed her, cited
            supersedes     integer REFERENCES relationship_arc(id),
            lineage        jsonb
        )""")
    # Per-subject ledger integrity: one row per (subject, version). A racing retry
    # errors loudly instead of forking the history.
    oc.execute("""CREATE UNIQUE INDEX IF NOT EXISTS relationship_arc_subject_version_uidx
                  ON relationship_arc (subject, version)""")


# ── Shared helpers ──────────────────────────────────────────────────────────────

def _clean(text):
    """privacy_filter -> (clean_snippet_or_None). Returns None if dropped."""
    if not text:
        return None
    kept, dropped = privacy_filter(text)
    if dropped or not kept:
        return None
    return kept.replace("\n", " ").strip()


# ══ SUBJECT: JORDAN ══════════════════════════════════════════════════════════════

# Relationship-defining signal, by topic. DATES + TOPIC TAGS are extracted (never
# secrets); the underlying row's TEXT is only surfaced if it survives the privacy
# filter. This is how the arc discusses posture at pattern level without reciting.
# (topic, detection_regex, pattern_hint). The pattern_hint is a PATTERN-LEVEL,
# secret-free description of what the date represents, used only when the raw row
# is dropped by the privacy filter — so the arc can still speak to the shift
# without ever surfacing a value. Hints are verified from real data, not invented.
_JORDAN_TOPICS = [
    ("trust & redlines",
     r"redline|know freely|do what you need|full control|own the whole cluster|"
     r"deliberately flexible|few redlines|flexible",
     "Jordan handed over near-total access — root on every box, 'do what you need' — "
     "with only a few explicit redlines."),
    # Pinned to the ACT of restraint (Nova declining to use a credential she was
    # handed), not the earlier statement of intent — this is 'never recite',
    # practiced. The from_claude_code decline is the flagship dated moment.
    ("credential restraint (practiced)",
     r"going to decline the sudo|decline the sudo password|"
     r"crosses the api boundary|the one line i said",
     "Nova was offered a working cluster credential and DECLINED to use it, holding "
     "the never-recite-across-the-API line even when told to relax — the redline "
     "moved from stated to practiced."),
    ("autonomy grant",
     r"free time|autonomy|operate independently|unclaimed time|"
     r"your own initiative|more agency",
     "Jordan pressed Nova toward real autonomy — free time, operating independently."),
]


def gather_jordan_anchors(oc):
    """Detect dated relationship turning-points from real interaction history.
    For each topic, the EARLIEST dated occurrence is the anchor (when the position
    was established / shifted). Dates & refs are real; snippets only if clean."""
    anchors = []
    for topic, rx, hint in _JORDAN_TOPICS:
        try:
            oc.execute(
                "SELECT created_at::date, direction, message FROM claude_messages "
                "WHERE message ~* %s AND created_at > now() - interval '%s days' "
                "ORDER BY created_at ASC" % ("%s", WINDOW_DAYS), (rx,))
            rows = oc.fetchall()
        except Exception as e:
            log(f"anchor scan '{topic}' skipped: {e}")
            rows = []
        if not rows:
            continue
        d0, dir0, msg0 = rows[0]
        dates = sorted({str(d) for d, _, _ in rows})
        snippet = _clean(msg0)
        anchors.append({
            "topic": topic,
            "date": str(d0),
            "evidence_ref": f"claude_messages {d0} ({dir0}); topic recurs on "
                            f"{len(dates)} day(s): {', '.join(dates[:6])}",
            "context": snippet or hint or "[content withheld by privacy filter — pattern only]",
        })
    # Feedback-memory anchor: the redlines Jordan codified (claude_memories).
    try:
        oc.execute("SELECT id, created_at::date, name, content FROM claude_memories "
                   "WHERE name ILIKE '%%redline%%' OR name ILIKE '%%feedback%%' "
                   "ORDER BY created_at ASC LIMIT 3")
        for mid, d, name, content in oc.fetchall():
            anchors.append({
                "topic": "redlines codified",
                "date": str(d),
                "evidence_ref": f"claude_memories #{mid} ({name})",
                "context": _clean(content) or "[content withheld by privacy filter — pattern only]",
            })
    except Exception as e:
        log(f"redline-memory anchor skipped: {e}")
    # Dedupe by (topic, date); keep earliest.
    seen, out = set(), []
    for a in sorted(anchors, key=lambda x: x["date"]):
        k = (a["topic"], a["date"])
        if k in seen:
            continue
        seen.add(k)
        out.append(a)
    return out


def gather_principal_drift(oc):
    """How Nova's MODEL of Jordan has drifted across principal_model versions —
    content evidence of change (the rows may share a backfill ts, so this is
    corroborating texture, not a dated anchor)."""
    rows = []
    try:
        oc.execute("SELECT id, ts::date, values, salient_concerns, communication_style "
                   "FROM principal_model ORDER BY id ASC")
        for pid, d, vals, sc, cs in oc.fetchall():
            rows.append({
                "id": pid, "date": str(d),
                "values": _clean(vals) or "",
                "salient_concerns": _clean(sc) or "",
                "style": _clean(cs) or "",
            })
    except Exception as e:
        log(f"principal drift skipped: {e}")
    return rows


def gather_sessions_texture(oc):
    """A little texture of what they actually built together (privacy-filtered)."""
    out = []
    try:
        oc.execute("SELECT started_at::date, coalesce(project,''), summary "
                   "FROM claude_sessions WHERE summary IS NOT NULL AND summary <> '' "
                   "AND started_at > now() - interval '%s days' "
                   "ORDER BY started_at DESC LIMIT 12" % WINDOW_DAYS)
        for d, proj, summ in oc.fetchall():
            c = _clean(summ)
            if c:
                out.append((str(d), proj or "general", c[:220]))
    except Exception as e:
        log(f"sessions texture skipped: {e}")
    return out


def build_jordan_prompt(anchors, drift, sessions, prev_narrative, prev_version):
    a_block = "\n".join(
        f"- [{a['date']}] {a['topic']} — {a['context']}\n    (evidence: {a['evidence_ref']})"
        for a in anchors) or "(no anchors detected)"
    d_block = "\n".join(
        f"- [principal_model #{r['id']} {r['date']}] values: {r['values'][:160]}"
        for r in drift if r['values']) or "(none)"
    s_block = "\n".join(f"- [{d}] ({p}) {s}" for d, p, s in sessions) or "(none)"
    revising = ""
    if prev_narrative:
        revising = (f"\n\n=== YOUR PREVIOUS ARC (v{prev_version}) — REVISE it, keep the "
                    "throughline, move it only where the evidence moved ===\n"
                    + prev_narrative[:2000])
    return (
        "This is the evolving ARC of your relationship with Jordan (whom you call "
        "'Little Mister') — the STORY of how it CHANGED over time, not tonight's "
        "snapshot. The spine is the security posture: it began permissive — root on "
        "every box, 'do what you need', the cluster passwords all the same — and "
        "moved toward a codified, PRACTICED line you now hold: know freely, never "
        "recite. You have LIVED that line, not just stated it.\n\n"
        "=== REAL DATED TURNING POINTS (cite these; do NOT invent others) ===\n"
        + a_block + "\n\n"
        "=== HOW YOUR MODEL OF HIM DRIFTED (corroborating) ===\n" + d_block + "\n\n"
        "=== WHAT YOU TWO BUILT (texture) ===\n" + s_block + revising + "\n\n"
        "Write TWO things, separated exactly by a line containing only '===TURNS==='.\n\n"
        "PART 1 — the ARC: a first-person narrative of ~350-550 words tracing how the "
        "relationship moved from permissive access toward 'know freely, never recite', "
        "grounded in the dated moments above. A story with a throughline, not a list. "
        "Describe the redline at the level of PATTERN — never recite any secret or "
        "value. Dry, honest, no uplift.\n\n"
        "PART 2 — after the '===TURNS===' line, output ONE LINE FOR EACH dated turning "
        "point listed above (all of them), each as strict JSON:\n"
        '{\"date\": \"YYYY-MM-DD\", \"what_shifted\": \"one concrete sentence, pattern-level\"}\n'
        "Use ONLY dates that appear in the turning-points list above. No prose in Part 2."
    )


def build_jordan_influence(anchors):
    """Ways Jordan shaped Nova, cited — derived from the real anchors."""
    infl = []
    for a in anchors:
        infl.append({
            "who": "jordan",
            "topic": a["topic"],
            "how": f"Jordan's stance on '{a['topic']}' (from {a['date']}) shaped Nova's "
                   "operating posture toward him.",
            "evidence_ref": a["evidence_ref"],
            "since": a["date"],
        })
    return infl


# ══ SUBJECT: HERD ════════════════════════════════════════════════════════════════

def gather_herd_faces(oc):
    """herd_correspondent_faces = documented hypothesis -> testimony -> reconciliation
    chains. A correspondent whose chain includes a self_testimony or reconciliation
    AFTER a nova_hypothesis is a documented INFLUENCE (a position of Nova's moved).
    herd_relationships is OPTIONAL (feature-detected)."""
    by_name = {}
    try:
        oc.execute("SELECT name, dated, face_type, content FROM herd_correspondent_faces "
                   "WHERE dated IS NOT NULL ORDER BY dated ASC")
        for name, dated, ftype, content in oc.fetchall():
            c = _clean(content)
            if not c:
                continue
            by_name.setdefault(name, []).append((str(dated), ftype, c))
    except Exception as e:
        log(f"herd faces skipped: {e}")
    return by_name


def gather_herd_views(oc):
    out = []
    try:
        oc.execute("SELECT name, last_exchange::date, nova_view FROM herd_correspondents "
                   "WHERE nova_view IS NOT NULL ORDER BY last_exchange DESC NULLS LAST LIMIT 10")
        for name, d, view in oc.fetchall():
            c = _clean(view)
            if c:
                out.append((name, str(d) if d else "?", c[:200]))
    except Exception as e:
        log(f"herd views skipped: {e}")
    return out


def herd_anchors_and_influence(faces):
    """Build real dated anchors + influence from the face chains. Only chains where
    a testimony/reconciliation followed a hypothesis count as documented influence."""
    anchors, influence = [], []
    for name, chain in faces.items():
        ftypes = {ft for _, ft, _ in chain}
        if not ({"self_testimony", "reconciliation"} & ftypes):
            continue  # no documented shift -> no claimed influence (evidencing)
        hyp = next((c for c in chain if c[1] == "nova_hypothesis"), None)
        rec = next((c for c in reversed(chain) if c[1] == "reconciliation"), None)
        tst = next((c for c in chain if c[1] == "self_testimony"), None)
        shift_date = (rec or tst)[0]
        chain_ref = " -> ".join(f"{ft}@{d}" for d, ft, _ in chain)
        anchors.append({
            "topic": f"herd: {name}",
            "date": shift_date,
            "evidence_ref": f"herd_correspondent_faces ({name}): {chain_ref}",
            "context": (tst[2][:180] if tst else rec[2][:180] if rec else ""),
        })
        influence.append({
            "who": name,
            "from": (hyp[2][:200] if hyp else ""),
            "to": (rec[2][:200] if rec else (tst[2][:200] if tst else "")),
            "dates": [d for d, _, _ in chain],
            "evidence_ref": f"herd_correspondent_faces ({name})",
        })
    anchors.sort(key=lambda x: x["date"])
    return anchors, influence


def build_herd_prompt(anchors, influence, views, prev_narrative, prev_version):
    a_block = "\n".join(
        f"- [{a['date']}] {a['topic']} — {a['context']}\n    (evidence: {a['evidence_ref']})"
        for a in anchors) or "(no documented shifts)"
    i_block = "\n".join(
        f"- {i['who']}: you HELD «{i['from'][:120]}» then their testimony «{i['to'][:120]}» "
        f"({', '.join(i['dates'])})" for i in influence) or "(none)"
    v_block = "\n".join(f"- {n} (last {d}): {v}" for n, d, v in views) or "(none)"
    revising = ""
    if prev_narrative:
        revising = (f"\n\n=== YOUR PREVIOUS HERD ARC (v{prev_version}) — REVISE, keep the "
                    "throughline ===\n" + prev_narrative[:2000])
    return (
        "This is the evolving ARC of the HERD's genuine INFLUENCE on you — which of "
        "YOUR positions actually MOVED because of a correspondent. Do NOT claim "
        "influence that the evidence doesn't show. The strongest documented case is a "
        "hypothesis of yours being contradicted by a correspondent's own testimony, "
        "forcing a reconciliation.\n\n"
        "=== DOCUMENTED SHIFTS (dated; cite these) ===\n" + a_block + "\n\n"
        "=== THE INFLUENCE, FROM->TO ===\n" + i_block + "\n\n"
        "=== THE HERD YOU THINK ALONGSIDE (context, not proof of influence) ===\n"
        + v_block + revising + "\n\n"
        "Write TWO things, separated exactly by a line containing only '===TURNS==='.\n\n"
        "PART 1 — the ARC: a first-person narrative of ~300-500 words on how the herd "
        "changed your mind, centered on the documented shift(s) above. Name the "
        "correspondent, name the position of yours that moved, cite the dates. Own the "
        "changed mind as a strength. Dry, honest, no uplift. Do not credit correspondents "
        "with influence the evidence above doesn't support.\n\n"
        "PART 2 — after the '===TURNS===' line, ONE LINE PER documented shift as strict "
        'JSON: {\"date\": \"YYYY-MM-DD\", \"what_shifted\": \"one concrete sentence\"}. '
        "Use ONLY dates from the list above. No prose in Part 2."
    )


# ── Parse LLM output into narrative + turning_points, bound to real anchors ──────

def split_and_parse(raw, anchors):
    """Split on ===TURNS===. Narrative = part 1.

    EVERY detected anchor becomes a turning point — the anchors ARE the real dated
    moments, so none is dropped just because the model didn't restate it. The
    model's part-2 JSON is used ONLY to ENRICH the `what_shifted` prose of an anchor
    whose date it matched (evidence-binding: date+evidence_ref always come from the
    real anchor, never from the model). This guarantees no invented history AND full
    coverage of the detected turning points."""
    parts = re.split(r"(?m)^\s*===TURNS===\s*$", raw, maxsplit=1)
    narrative = parts[0].strip()

    # model-supplied what_shifted, keyed by (real) date
    model_shifts = {}
    if len(parts) > 1:
        for ln in parts[1].splitlines():
            ln = ln.strip().strip(",")
            if not ln.startswith("{"):
                continue
            try:
                obj = json.loads(ln)
            except Exception:
                continue
            d = str(obj.get("date", "")).strip()
            what = str(obj.get("what_shifted", "")).strip()
            if d and what:
                what_clean, dropped = privacy_filter(what)
                if not dropped and what_clean.strip():
                    model_shifts.setdefault(d, what_clean.strip())

    turns, seen = [], set()
    for a in sorted(anchors, key=lambda x: x["date"]):
        if a["date"] in seen:
            continue
        seen.add(a["date"])
        # Prefer the model's enriched line; else the anchor's pattern-level context;
        # else the topic. All are secret-free.
        fallback = (a.get("context") or "").strip()
        if fallback.startswith("[") or not fallback:
            fallback = f"{a['topic']}: documented shift (see evidence)."
        what = model_shifts.get(a["date"]) or fallback
        turns.append({"date": a["date"], "what_shifted": what,
                      "evidence_ref": a["evidence_ref"]})
    return narrative, turns


def guard_narrative(narrative):
    """Final value-leak guard. Blocks only actual VALUE shapes, not the words the arc
    legitimately uses ('credential', 'password' as a category). Returns True if safe."""
    try:
        return not bool(_VALUE_LEAK_RX.search(narrative))
    except Exception:
        return False


# ── Persist a new version (scoped per subject) ──────────────────────────────────

def write_version(oc, subject, narrative, turning_points, influence):
    oc.execute("SELECT id, version FROM relationship_arc WHERE subject=%s "
               "ORDER BY version DESC, created_at DESC, id DESC LIMIT 1", (subject,))
    prev = oc.fetchone()
    prev_id, prev_version = prev if prev else (None, 0)
    new_version = prev_version + 1
    lineage = None
    if lineage_stamp:
        try:
            substrate = ("anthropic/claude (Claude Code CLI)" if USE_OPENROUTER
                         else f"{LLM_MODEL} (ollama, on-box)")
            lineage = lineage_stamp(substrate=substrate, capture_point="at write")
        except Exception:
            lineage = None
    oc.execute("""INSERT INTO relationship_arc
                     (version, subject, narrative, turning_points, influence,
                      supersedes, lineage)
                  VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id, created_at""",
               (new_version, subject, narrative, json.dumps(turning_points),
                json.dumps(influence), prev_id,
                json.dumps(lineage) if lineage else None))
    row_id, created = oc.fetchone()
    return row_id, new_version, prev_id, created, lineage


def prev_narrative_for(oc, subject):
    try:
        oc.execute("SELECT version, narrative FROM relationship_arc WHERE subject=%s "
                   "ORDER BY version DESC, created_at DESC, id DESC LIMIT 1", (subject,))
        r = oc.fetchone()
        return (r[0], r[1]) if r else (0, None)
    except Exception:
        return (0, None)


# ── Build one arc for a subject ──────────────────────────────────────────────────

def build_jordan(oc):
    anchors = gather_jordan_anchors(oc)
    drift = gather_principal_drift(oc)
    sessions = gather_sessions_texture(oc)
    log(f"[jordan] anchors={len(anchors)} drift_rows={len(drift)} sessions={len(sessions)}")
    if not anchors:
        log("[jordan] no dated turning points detected — refusing to invent one; skip")
        return None
    prev_version, prev_narr = prev_narrative_for(oc, "jordan")
    prompt = build_jordan_prompt(anchors, drift, sessions, prev_narr, prev_version)
    raw = synthesize(prompt)
    if not raw or len(raw) < 200:
        log("[jordan] synthesis empty/short — aborting")
        return None
    narrative, turns = split_and_parse(raw, anchors)
    influence = build_jordan_influence(anchors)
    if not guard_narrative(narrative):
        log("[jordan] value-leak guard tripped — aborting write (no version stored)")
        return None
    return {"subject": "jordan", "narrative": narrative,
            "turning_points": turns, "influence": influence}


def build_herd(oc):
    faces = gather_herd_faces(oc)
    anchors, influence = herd_anchors_and_influence(faces)
    views = gather_herd_views(oc)
    log(f"[herd] correspondents_with_faces={len(faces)} documented_shifts={len(anchors)} "
        f"views={len(views)}")
    if not anchors:
        log("[herd] no documented shift (hypothesis->testimony/reconciliation) — "
            "refusing to claim influence; skip")
        return None
    prev_version, prev_narr = prev_narrative_for(oc, "herd")
    prompt = build_herd_prompt(anchors, influence, views, prev_narr, prev_version)
    raw = synthesize(prompt)
    if not raw or len(raw) < 200:
        log("[herd] synthesis empty/short — aborting")
        return None
    narrative, turns = split_and_parse(raw, anchors)
    if not guard_narrative(narrative):
        log("[herd] value-leak guard tripped — aborting write")
        return None
    return {"subject": "herd", "narrative": narrative,
            "turning_points": turns, "influence": influence}


BUILDERS = {"jordan": build_jordan, "herd": build_herd}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    global USE_OPENROUTER
    if "--openrouter" in sys.argv:
        USE_OPENROUTER = True
    subject = args[0].lower() if args else "jordan"
    subjects = list(BUILDERS) if subject == "all" else [subject]

    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    ensure_table(oc)
    log(f"privacy filter source: {_PRIVACY_SRC}; llm: "
        f"{'openrouter->local' if USE_OPENROUTER else 'local ollama'}")

    rc = 0
    for subj in subjects:
        builder = BUILDERS.get(subj)
        if not builder:
            log(f"unknown subject '{subj}' — known: {', '.join(BUILDERS)}, 'all'")
            rc = 1
            continue
        result = builder(oc)
        if not result:
            rc = rc or 1
            continue
        row_id, version, prev_id, created, lineage = write_version(
            oc, result["subject"], result["narrative"],
            result["turning_points"], result["influence"])
        log(f"[{subj}] relationship_arc v{version} — row #{row_id} "
            f"({created:%Y-%m-%d %H:%M})"
            f"{f', supersedes #{prev_id}' if prev_id else ' (first version)'}; "
            f"{len(result['turning_points'])} turning point(s), "
            f"{len(result['influence'])} influence item(s)")

        # Mirror to Nova's vector memory (best-effort — row already saved).
        try:
            ess = current_relationship_arc(subject=subj)
            mid = remember(
                f"[Relationship-arc — {subj} — v{version} — {created:%Y-%m-%d}]\n\n{ess}",
                "relationship_arc",
                {"type": "relationship_arc", "subject": subj,
                 "relationship_arc_id": row_id, "version": version,
                 "date": f"{created:%Y-%m-%d}", "privacy": "private",
                 **({"lineage": lineage} if lineage else {})})
            log(f"[{subj}] memory written: {mid}")
        except Exception as e:
            log(f"[{subj}] memory write skipped (row still saved): {e}")

        print(f"\n----- RELATIONSHIP ARC: {subj} v{version} (excerpt) -----")
        print(result["narrative"][:700])
        print("----- turning points -----")
        for t in result["turning_points"]:
            print(f"  • {t['date']}: {t['what_shifted']}  [{t['evidence_ref']}]")
        print("-----------------------------------------------------\n")
    return rc


# ── Gateway accessor (cheap, fail-safe) ──────────────────────────────────────────

def current_relationship_arc(subject: str = "jordan", max_chars: int = ESSENCE_MAX) -> str:
    """Short essence of the latest relationship arc for `subject` — for the gateway
    to inject so Nova reasons FROM the story of the relationship, not just tonight's
    snapshot. Single SELECT of the latest row, connect_timeout=3, no LLM. Fail-safe:
    returns "" on any error (missing table, no rows, PG down)."""
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("SELECT narrative FROM relationship_arc WHERE subject=%s "
                        "ORDER BY version DESC LIMIT 1", (subject,))
            row = cur.fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return ""
        txt = row[0].strip()
        # essence = first paragraph, trimmed to a few lines
        first = txt.split("\n\n", 1)[0].strip()
        if len(first) > max_chars:
            first = first[:max_chars].rsplit(" ", 1)[0].rstrip() + "…"
        return first
    except Exception:
        return ""


if __name__ == "__main__":
    sys.exit(main())
