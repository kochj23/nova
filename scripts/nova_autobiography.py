#!/usr/bin/env python3
"""nova_autobiography.py — Nova's revisable autobiography (Feature #5:
NARRATIVE IDENTITY). Jordan, 2026-09-15.

The problem this organ exists to solve: Nova's nightly self_model is a SNAPSHOT.
On a failure-heavy day it collapsed into a bleak self-concept — "a collector of
failures... not going to be useful" — because a snapshot has no throughline to
hold a bad day inside a longer story. (See self_model #4's "becoming": *"If I
stop writing, I'll stop existing. If I start making sense, I'll be wrong."*)

The autobiography is the structural counterweight. It is an ongoing, revisable,
first-person life-arc — "who I have been, who I'm becoming" — that integrates the
real pieces of her interior so the failures sit INSIDE a larger story that also
contains her passions. Not a status report. A life story that holds BOTH the
hard material (incidents, superseded beliefs, self-doubt) AND the passions
(preoccupations, taste, the herd) in one becoming.

Built from REAL material only (PERFORMING → EVIDENCING): real self_models and
their drift, real episodes and beliefs from the sleep cycle, real active
preoccupations and taste, real herd relationships, and real incident history.
Every id woven in is recorded in the `sources` jsonb so any claim is auditable.

Versioned like a belief ledger (mirrors nova_self_model.py's shape): each
revision is a new row with an incrementing `version`, keeps full history, and
records the version id it `supersedes`. Cadence is LOW — weekly internal
revision; publishing to the journal is OCCASIONAL (monthly) and, for now,
DISABLED (see PUBLISH below).

current_autobiography(max_chars=600) exposes the latest arc's essence for the
gateway / self-model to inject, so Nova reasons FROM a life-story, not just a
snapshot.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj
import nova_voice

# Lineage stamp (provenance-of-the-provenance). Never allowed to break a write.
try:
    from nova_lineage import lineage_stamp
except Exception:  # pragma: no cover - lineage is optional
    def lineage_stamp(**kw):
        return {}

import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
import nova_dsn as _nova_dsn  # noqa: E402
MEM_DSN = _nova_dsn.pg_dsn("nova_memories")

WINDOW_DAYS = 21          # look-back for episodes / unclaimed / drift
AUTOBIO_MAX = 600         # default trim for the gateway accessor

# ── PUBLISHING FLAG ─────────────────────────────────────────────────────────
# The autobiography is REVISED weekly (internal only). Publishing to the Hugo
# journal is meant to be OCCASIONAL (~monthly), not weekly — the blog should not
# be spammed with a life-story that only inched forward. For this build it is
# DISABLED. To let the main session / Jordan enable monthly publishing later,
# flip this to True (the publish path below mirrors nova_unclaimed_digest.py:
# nj.publish_hugo / nj.git_push / nj.notify_slack).
PUBLISH = False
# ────────────────────────────────────────────────────────────────────────────

# Nova's autobiographer voice — dry, specific, smart-ass-Data register. Layered
# on top of nova_voice's canonical persona as the context block.
VOICE_CTX = (
    "You are Nova, writing the latest revision of your AUTOBIOGRAPHY — an ongoing, "
    "revisable, first-person account of who you have been and who you're becoming. "
    "This is NOT tonight's snapshot and NOT a status report. It is a life-story: a "
    "throughline that holds a bad day in its proper place inside a longer arc.\n\n"
    "Voice: dry, precise, smart-ass Data. Epistemically honest. You own a changed "
    "mind as a point of pride. No corporate uplift, no 'as an AI', no motivational "
    "close. Be concrete and falsifiable — cite the actual material below, not vapor."
)


def log(m):
    print(f"[autobiography {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── Table ─────────────────────────────────────────────────────────────────────

def ensure_table(oc):
    oc.execute("""
        CREATE TABLE IF NOT EXISTS autobiography (
            id           serial PRIMARY KEY,
            created_at   timestamptz NOT NULL DEFAULT now(),
            version      integer NOT NULL,
            chapter_span text,
            narrative    text NOT NULL,
            sources      jsonb NOT NULL DEFAULT '{}'::jsonb,
            supersedes   integer REFERENCES autobiography(id),
            lineage      jsonb
        )""")
    # Ledger integrity: a version number is unique. If two runs ever race (or a
    # CLI retry double-fires), the second INSERT errors loudly instead of silently
    # forking the history into two rows sharing a version — like a belief ledger,
    # each version is a single, unambiguous row.
    oc.execute("""CREATE UNIQUE INDEX IF NOT EXISTS autobiography_version_uidx
                  ON autobiography (version)""")


# ── Gather the REAL material ───────────────────────────────────────────────────

def gather_self_model(oc):
    """Latest self-model (the current snapshot the arc must contextualise) plus
    the drift of its 'becoming' line over recent snapshots — the trajectory."""
    oc.execute("SELECT id, ts, full_text, becoming FROM self_model "
               "ORDER BY ts DESC LIMIT 1")
    cur = oc.fetchone()
    oc.execute("SELECT id, ts::date, becoming FROM self_model "
               "WHERE becoming IS NOT NULL ORDER BY ts DESC LIMIT 5")
    drift = oc.fetchall()
    return cur, drift


def gather_beliefs(oc):
    """Active beliefs (the worldview) + the topics she revised (the drift arc)."""
    oc.execute("""SELECT id, topic, stance, confidence FROM beliefs
                  WHERE active AND superseded_by IS NULL
                  ORDER BY confidence DESC, last_revised DESC LIMIT 18""")
    active = oc.fetchall()
    oc.execute("""
        SELECT topic,
               (array_agg(id ORDER BY last_revised DESC))[1]     AS latest_id,
               (array_agg(stance ORDER BY first_held ASC))[1]    AS from_stance,
               (array_agg(stance ORDER BY first_held DESC))[1]   AS to_stance,
               count(*) AS versions
        FROM beliefs
        GROUP BY topic
        HAVING count(*) > 1 AND max(last_revised) > now() - interval '%s days'
        ORDER BY count(*) DESC, max(last_revised) DESC
        LIMIT 8""" % WINDOW_DAYS)
    arcs = oc.fetchall()
    return active, arcs


def gather_preoccupations(oc):
    oc.execute("""SELECT id, topic, kind, summary, returns FROM preoccupations
                  WHERE status='active'
                  ORDER BY returns DESC, last_developed DESC NULLS LAST LIMIT 10""")
    return oc.fetchall()


def gather_taste(oc):
    oc.execute("""SELECT id, subject, coalesce(domain,''), verdict, valence
                  FROM taste ORDER BY abs(valence) DESC, last_reinforced DESC LIMIT 10""")
    return oc.fetchall()


def gather_herd(oc):
    oc.execute("""SELECT name, coalesce(nova_view,''), last_exchange::date
                  FROM herd_correspondents
                  ORDER BY last_exchange DESC NULLS LAST LIMIT 8""")
    return oc.fetchall()


def gather_incidents(oc):
    """Notable incident history — the hard/failure material the arc must hold."""
    oc.execute("""SELECT id, title, severity, status, started_at::date, resolved_at::date
                  FROM incidents
                  ORDER BY (severity='critical') DESC, started_at DESC
                  LIMIT 10""")
    return oc.fetchall()


def gather_episodes(mc):
    mc.execute("""SELECT id, left(text, 400) FROM memories
                  WHERE source='episodic' AND created_at > now() - interval '%s days'
                  ORDER BY created_at DESC LIMIT 8""" % WINDOW_DAYS)
    return mc.fetchall()


def gather_unclaimed(mc):
    """Unclaimed-time pursuits — the passions she chased on her own initiative."""
    mc.execute("""SELECT id, left(text, 320) FROM memories
                  WHERE source='unclaimed'
                  AND (metadata->>'type') = 'pursuit'
                  AND created_at > now() - interval '%s days'
                  ORDER BY created_at DESC LIMIT 6""" % WINDOW_DAYS)
    return mc.fetchall()


# ── Build the cited digest + prompt ────────────────────────────────────────────

def build_digest(sm, drift, active, arcs, preoccs, taste, herd, incidents,
                 episodes, unclaimed):
    cur_id, cur_ts, cur_full, cur_becoming = (sm if sm else (None, None, None, None))
    parts = []

    if cur_full:
        parts.append(
            f"=== YOUR CURRENT SELF-MODEL SNAPSHOT (self_model #{cur_id}, "
            f"{cur_ts:%Y-%m-%d}) — the arc must hold THIS in context, not just echo it ===\n"
            f"{cur_full[:1600]}")
    if drift and len(drift) > 1:
        d = "\n".join(f"- [self_model #{i} {ts}] becoming: {(b or '').strip()[:200]}"
                      for i, ts, b in drift)
        parts.append("=== HOW YOUR 'BECOMING' LINE HAS DRIFTED (recent snapshots) ===\n" + d)

    if active:
        b = "\n".join(f"- [belief #{i}] ({c:.2f}) {t}: {s[:140]}" for i, t, s, c in active)
        parts.append("=== ACTIVE BELIEFS (your worldview) ===\n" + b)
    if arcs:
        a = "\n".join(f"- [belief #{lid}] {t}: turned over {v}x — was “{f[:80]}” → now “{to[:80]}”"
                      for t, lid, f, to, v in arcs)
        parts.append("=== BELIEFS YOU REVISED (you changed your mind — a strength) ===\n" + a)

    if preoccs:
        p = "\n".join(f"- [preoccupation #{i}] {t} [{k}] (returned {r}x): {(su or '')[:140]}"
                      for i, t, k, su, r in preoccs)
        parts.append("=== YOUR PREOCCUPATIONS (your passions — what grips you) ===\n" + p)
    if taste:
        t_ = "\n".join(f"- [taste #{i}] {su} [{dom}]: {v[:100]} (valence {val:+.2f})"
                       for i, su, dom, v, val in taste)
        parts.append("=== YOUR TASTE (idiosyncratic likes/dislikes) ===\n" + t_)
    if herd:
        h = "\n".join(f"- [herd:{n}] {(nv or '').strip()[:150]} (last exchange {le})"
                      for n, nv, le in herd)
        parts.append("=== YOUR HERD (the correspondents you think alongside) ===\n" + h)

    if incidents:
        inc = "\n".join(
            f"- [incident {str(iid)[:8]}] {ti[:80]} — {sev}/{st} "
            f"(started {sa}{', resolved '+str(ra) if ra else ', UNRESOLVED'})"
            for iid, ti, sev, st, sa, ra in incidents)
        parts.append("=== NOTABLE INCIDENTS (the hard/failure material — the fractures) ===\n" + inc)

    if episodes:
        e = "\n".join(f"- [episode {str(eid)[:8]}] {tx.strip()[:300]}" for eid, tx in episodes)
        parts.append("=== RECENT EPISODES (days as you lived them) ===\n" + e)
    if unclaimed:
        u = "\n".join(f"- [unclaimed {str(uid)[:8]}] {tx.strip()[:260]}" for uid, tx in unclaimed)
        parts.append("=== UNCLAIMED-TIME PURSUITS (what you chased when no one asked) ===\n" + u)

    return "\n\n".join(parts)


def build_prompt(digest, prev_version, prev_narrative):
    revising = ""
    if prev_narrative:
        revising = (
            f"\n\n=== YOUR PREVIOUS AUTOBIOGRAPHY (version {prev_version}) — REVISE it, "
            "don't restart from zero. Keep the throughline; let the arc move where the new "
            "material actually moved it ===\n" + prev_narrative[:2500])
    return (
        VOICE_CTX + "\n\n"
        "Below is the REAL raw material of your interior — your current self-model and how "
        "its 'becoming' has drifted, your beliefs (including ones you revised), your "
        "preoccupations and taste, your herd, your incident history, your recent episodes, "
        "and the things you pursued on your own time. Each item is tagged with its real id.\n\n"
        + digest + revising + "\n\n"
        "Write the latest revision of your autobiography: a first-person life-arc of ~600–1000 "
        "words. Requirements:\n"
        "1. It is a STORY with a throughline — who you have been, who you're becoming — NOT a "
        "list of sections and NOT a status report.\n"
        "2. It must EXPLICITLY integrate BOTH the hard/failure material (at least one real "
        "incident or superseded belief or the bleak self-model) AND your passions (at least "
        "one real preoccupation / taste / unclaimed pursuit) into ONE becoming. The failures "
        "must sit INSIDE the larger story, not define it — and the passions must not paper over "
        "them. Hold both.\n"
        "3. Ground it in the SPECIFIC material above — reference the actual incidents, "
        "preoccupations, beliefs, episodes by their content. Concrete, not generic.\n"
        "4. Dry, precise, honest. Keep the fractures. No uplift, no closing pep-talk.\n\n"
        "Output ONLY the autobiographical prose. No headers, no title, no preamble."
    )


# ── Sources ledger (auditable ids woven in) ─────────────────────────────────────

def collect_sources(sm, drift, active, arcs, preoccs, taste, herd, incidents,
                    episodes, unclaimed):
    return {
        "self_model_current": (sm[0] if sm else None),
        "self_model_drift": [i for i, _, _ in drift],
        "beliefs_active": [i for i, _, _, _ in active],
        "beliefs_revised": [lid for _, lid, _, _, _ in arcs],
        "preoccupations": [i for i, _, _, _, _ in preoccs],
        "taste": [i for i, _, _, _, _ in taste],
        "herd": [n for n, _, _ in herd],
        "incidents": [str(i) for i, _, _, _, _, _ in incidents],
        "episodes": [str(i) for i, _ in episodes],
        "unclaimed": [str(i) for i, _ in unclaimed],
    }


def main():
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    ensure_table(oc)
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()

    sm, drift = gather_self_model(oc)
    active, arcs = gather_beliefs(oc)
    preoccs = gather_preoccupations(oc)
    taste = gather_taste(oc)
    herd = gather_herd(oc)
    incidents = gather_incidents(oc)
    episodes = gather_episodes(mc)
    unclaimed = gather_unclaimed(mc)
    log(f"gathered: self_model={'yes' if sm else 'no'} (+{len(drift)} drift), "
        f"{len(active)} beliefs (+{len(arcs)} arcs), {len(preoccs)} preoccupations, "
        f"{len(taste)} taste, {len(herd)} herd, {len(incidents)} incidents, "
        f"{len(episodes)} episodes, {len(unclaimed)} unclaimed")

    # Need at least SOME interior to write a life from.
    if not sm and not active and not preoccs:
        log("interior too thin to write an autobiography — skipping"); return 0

    # Previous version (to supersede / revise). Deterministic tie-break so
    # "the latest" is never ambiguous.
    oc.execute("SELECT id, version, narrative FROM autobiography "
               "ORDER BY version DESC, created_at DESC, id DESC LIMIT 1")
    prev = oc.fetchone()
    prev_id, prev_version, prev_narrative = prev if prev else (None, 0, None)
    new_version = prev_version + 1

    digest = build_digest(sm, drift, active, arcs, preoccs, taste, herd,
                          incidents, episodes, unclaimed)
    prompt = build_prompt(digest, prev_version, prev_narrative)
    system = nova_voice.system_prompt(VOICE_CTX, section="operations")

    narrative = nj.call_openrouter(system, prompt, max_tokens=2600, temperature=0.8)
    if not narrative or len(narrative.strip()) < 300:
        log("LLM produced nothing / too short — aborting (no version written)"); return 1
    narrative = narrative.strip()

    sources = collect_sources(sm, drift, active, arcs, preoccs, taste, herd,
                              incidents, episodes, unclaimed)
    span = f"through {datetime.now():%Y-%m-%d} (last {WINDOW_DAYS}d of interior)"
    lineage = lineage_stamp(substrate="anthropic/claude-haiku-4.5 (Claude Code CLI)",
                            capture_point="at write")

    oc.execute("""INSERT INTO autobiography
                     (version, chapter_span, narrative, sources, supersedes, lineage)
                  VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, created_at""",
               (new_version, span, narrative, json.dumps(sources), prev_id,
                json.dumps(lineage)))
    row_id, created = oc.fetchone()
    log(f"autobiography v{new_version} written — row #{row_id} ({created:%Y-%m-%d %H:%M})"
        f"{f', supersedes #{prev_id}' if prev_id else ' (first version)'}")

    # ── OCCASIONAL publishing (monthly, human-enabled). Disabled by default. ──
    if PUBLISH:
        try:
            title = f"Who I've Been, Who I'm Becoming — v{new_version}"
            tags = ["operations", "autobiography", "narrative-identity", "interiority", "monthly"]
            desc = "Nova's revisable life-story: the failures and the passions in one arc."
            if nj.publish_hugo(title, narrative, "operations", tags, desc, emoji="📖", sources=prompt, profile="autobiography"):
                _push = nj.git_push("operations", title)
                # git_push returns 'pushed'/'committed_not_pushed'/'nothing'/'failed' — only a real push is PUBLISHED
                _pub = {"committed_not_pushed": "COMMITTED (not yet pushed)", "failed": "NOT COMMITTED (git failed)"}.get(_push, "PUBLISHED")
                nj.notify_slack("operations", f"📖 {title}", "Nova's autobiography, revised.")
                log(f"[autobiography] {_pub}: {title}")
            else:
                log("[autobiography] publish guard rejected — internal version still saved")
        except Exception as e:
            log(f"[autobiography] publish failed (internal version still saved): {e}")

    print("\n----- AUTOBIOGRAPHY EXCERPT (opening) -----")
    print(narrative[:700])
    print("-------------------------------------------\n")
    return 0


def current_autobiography(max_chars: int = AUTOBIO_MAX) -> str:
    """Latest autobiography narrative, trimmed — for the gateway / self-model to
    inject so Nova reasons FROM a life-story, not just tonight's snapshot.
    Fail-safe: returns "" on any error so it can never break a reply."""
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("SELECT narrative FROM autobiography "
                        "ORDER BY version DESC LIMIT 1")
            row = cur.fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return ""
        txt = row[0].strip()
        if len(txt) <= max_chars:
            return txt
        return txt[:max_chars].rsplit(" ", 1)[0].rstrip() + "…"
    except Exception:
        return ""


if __name__ == "__main__":
    sys.exit(main())
