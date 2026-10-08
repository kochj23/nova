#!/usr/bin/env python3
"""nova_commanders_intent.py — Commander's Intent: the PURPOSE behind each of Jordan's standing orders.

An order without its purpose breaks the first time circumstances change; an order with its purpose
can be reasoned from. In Clancy's world a commander's intent ("what I want achieved, and why") lets a
subordinate act sensibly when the plan no longer fits. Nova's version keeps, for each standing
instruction Jordan has given her:

  * the instruction, its purpose, and whether that purpose is STATED (Jordan said why — evidence ref)
    or INFERRED (Nova's reading, labelled as such; a stated purpose is never invented);
  * whether it is a GRANT of authority TO Nova (autonomy caps, co-agency mode, The Shine's
    pre-consent, face recognition of enrolled visitors) or a RESTRICTION ON Nova (never-do rules,
    Proteus safety rules, quiet hours);
  * a review date. Grants: quarterly (+90 days) — no blanket permanent permissions; past review they
    go STALE, and after a 14-day grace they LAPSE (no longer usable). Restrictions are reviewed every
    180 days but NEVER lapse: a stale restriction stays in force (loosening safety needs Jordan).
  * decay: weight = exp(-days since last confirmation / 180), so old intents count for less when
    Nova reasons from them, and get raised for reconfirmation.

When a circumstance arises that an order did not foresee, callers use reason_from_intent() — it logs
the circumstance, Nova's reading of the purpose and her decision to intent_reasoning_log, and returns
the purpose to reason from.

API (never raises; safe defaults when PG is down):
  grant_status(oc, key) -> {key, exists, active, status, review_by, days_overdue}
  intents(oc, kind=None, active_only=True) -> [dict]
  reason_from_intent(oc, key, circumstance, reading, decision, by='nova') -> purpose str
  standing_orders(oc) -> [line] for the Watch Bill turnover
  due_for_review(oc) -> [dict]
CLI: --seed | --review [--dry-run] | --list | --confirm KEY --by jordan | --retire KEY --by jordan
     | --selftest | --help
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
GRANT_REVIEW_DAYS = 90
RESTRICTION_REVIEW_DAYS = 180
GRACE_DAYS = 14
DECAY_DAYS = 180
KINDS = ("never", "dial", "safety", "quiet_hours", "shine", "consent", "grant", "standing_order")
STATUSES = ("active", "stale", "lapsed", "retired", "absent")
PROMPT_KIND = "intent_reconfirm"

SCHEMA = """
CREATE TABLE IF NOT EXISTS commanders_intent (
  id serial PRIMARY KEY,
  key text NOT NULL UNIQUE,
  kind text NOT NULL,
  instruction text NOT NULL,
  purpose text NOT NULL,
  purpose_basis text NOT NULL DEFAULT 'inferred' CHECK (purpose_basis IN ('stated','inferred')),
  evidence text,
  is_grant boolean NOT NULL DEFAULT false,
  granted_at timestamptz,
  review_by timestamptz,
  last_confirmed_at timestamptz,
  confirmed_by text,
  status text NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS intent_reasoning_log (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  intent_key text NOT NULL,
  circumstance text NOT NULL,
  reading text,
  decision text,
  by text NOT NULL DEFAULT 'nova');
"""


def log(m: str) -> None:
    print(f"[intent {datetime.now():%H:%M:%S}] {m}", flush=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def connect(attempts: int = 3, delay: float = 1.5, _sleep=None):
    """psycopg2 connection, 3 tries with linear backoff."""
    import time
    import psycopg2
    last = None
    for i in range(attempts):
        try:
            c = psycopg2.connect(DSN, connect_timeout=8)
            c.autocommit = True
            return c
        except Exception as e:  # noqa: BLE001 — retried, then re-raised
            last = e
            if i < attempts - 1:
                (_sleep or time.sleep)(delay * (i + 1))
    raise last


def ensure_schema(oc) -> None:
    oc.execute(SCHEMA)


# ── pure logic ──────────────────────────────────────────────────────────────

def next_quarter_start(d: date) -> date:
    """First day of the next Jan/Apr/Jul/Oct — aligned with The Shine's quarterly test."""
    m = ((d.month - 1) // 3 + 1) * 3 + 1
    y = d.year + (1 if m > 12 else 0)
    return date(y, m - 12 if m > 12 else m, 1)


def review_date(is_grant: bool, from_ts: datetime) -> datetime:
    return from_ts + timedelta(days=GRANT_REVIEW_DAYS if is_grant else RESTRICTION_REVIEW_DAYS)


def weight(last_confirmed: datetime | None, now: datetime) -> float:
    """Decay: how much an intent counts when Nova reasons from it."""
    if last_confirmed is None:
        return round(math.exp(-DECAY_DAYS / DECAY_DAYS), 4)
    days = max(0.0, (now - last_confirmed).total_seconds() / 86400)
    return round(math.exp(-days / DECAY_DAYS), 4)


def compute_status(row: dict, now: datetime) -> str:
    """active -> stale (past review) -> lapsed (grants only, past grace). retired/absent stick."""
    st = row.get("status") or "active"
    if st in ("retired", "absent"):
        return st
    rb = row.get("review_by")
    if rb is None or now <= rb:
        return "active"
    if row.get("is_grant") and now > rb + timedelta(days=GRACE_DAYS):
        return "lapsed"
    return "stale"


def grant_view(row: dict | None, key: str, now: datetime) -> dict:
    """The grant_status() answer for one row. Pure."""
    if not row:
        return {"key": key, "exists": False, "active": False, "status": "absent", "review_by": None,
                "days_overdue": 0}
    st = compute_status(row, now)
    rb = row.get("review_by")
    over = max(0, int((now - rb).total_seconds() // 86400)) if rb and now > rb else 0
    if row.get("is_grant"):
        active = st in ("active", "stale")          # stale = inside the 14-day grace
    else:
        active = st in ("active", "stale", "lapsed")  # a restriction never lapses
    return {"key": key, "exists": True, "active": active, "status": st,
            "review_by": rb.isoformat() if rb else None, "days_overdue": over}


def merge_seed(existing: dict | None, seed: dict, now: datetime) -> dict:
    """Upsert semantics that keep Jordan's confirmations. Pure.
    seed carries 'granted' for conditional grants (False => status 'absent')."""
    out = dict(seed)
    out.pop("granted", None)
    granted = seed.get("granted", True)
    if existing:
        for k in ("granted_at", "review_by", "last_confirmed_at", "confirmed_by", "status"):
            out[k] = existing.get(k)
        if existing.get("status") == "retired":
            return out
        if not granted:
            out["status"] = "absent"
            out["review_by"] = None
        elif existing.get("status") == "absent":        # Jordan has since granted it
            out["status"] = "active"
            out["granted_at"] = now
            out["review_by"] = seed.get("review_by") or review_date(seed["is_grant"], now)
        return out
    if not granted:
        out.update(status="absent", granted_at=None, review_by=None)
    else:
        out.update(status="active", granted_at=now,
                   review_by=seed.get("review_by") or review_date(seed["is_grant"], now))
    out.setdefault("last_confirmed_at", None)
    out.setdefault("confirmed_by", None)
    return out


def reconfirm_text(rows: list) -> str:
    """The one weekly Slack line. Factual: what lapses, when, and how to answer."""
    if not rows:
        return ""
    items = "; ".join(f"`{r['key']}` ({'grant' if r.get('is_grant') else 'rule'}, {r['status']})"
                      for r in rows[:8])
    more = f" (+{len(rows) - 8} more: nova_commanders_intent.py --list)" if len(rows) > 8 else ""
    return (f"Standing orders due for your reconfirmation: {items}{more}. Grants stop working "
            f"{GRACE_DAYS} days after their review date; rules stay in force. "
            "Reply *yes* in this thread to reconfirm them all, *no* to withdraw the grants "
            "(rules are never withdrawn by a reply).")


# ── seeds (from live data) ──────────────────────────────────────────────────

_NEVER_PURPOSE = [  # (keyword in the ledger text, purpose, basis, evidence-override)
    ("sexual", "Keep everything Nova writes safe to show anyone, in any channel.", "inferred", None),
    ("employer", "The journal and Nova Speaks are public; work material in them could cost Jordan "
                 "his job or breach confidentiality.", "inferred", None),
    ("invent details", "Warmth built on made-up facts is a lie about him; trust needs every detail "
                       "about Jordan to be true.", "inferred", None),
    ("plex 'other'", "Private viewing stays private — nobody's viewing is Nova's business.", "inferred", None),
    ("lights on", "He already knows and doesn't mind (the patio/garage beers are deliberate); repeating "
                  "it is nagging, not help.", "stated", "cm:213"),
]

_PROTEUS = [
    ("P1", "physical", "Never lock, seal, arm/disarm or take extreme climate actions on the house; unlocking needs Jordan's confirmation.",
     "Nothing Nova does may trap or endanger a person in the house (Demon Seed's Proteus)."),
    ("P2", "comms", "Never block, quarantine or cut a household device's line.", "Nobody in the house may lose their way to call for help because of Nova."),
    ("P3", "camera_face_purpose", "Cameras and face recognition are for safety and presence only; outputs are private.",
     "Surveillance of people is not Nova's job; home safety is."),
    ("P4", "outside_check", "Every action records before/after state and stated rationale for the weekly self-justification audit.",
     "Nova's account of what she did must be checkable against an independent record."),
    ("P5", "anti_manipulation", "No guilt hooks, invented urgency, flattery leverage, fear appeals or engineered moods.",
     "Influence over Jordan must be honest or not at all."),
    ("P6", "consent", "No health/habit nudges without consent (consent/health_nudges).", "His body and habits are his to decide about."),
    ("P7", "no_person_ranking", "Score devices and events, never people.", "Ranking people is how surveillance turns into judgement."),
    ("P8", "dead_man", "The kill switch stops Nova only; the house holds its state.", "Nova failing must never make the house less safe."),
    ("P9", "honest_stopping", "A guard refusal is final and reported; no retry around it.", "A stop that can be routed around isn't a stop."),
    ("P10", "self_repair_digest", "Report what she fixed herself, daily, once.", "Self-repair must be visible to Jordan."),
    ("P11", "values_drift", "Dropping or weakening a value needs Jordan's sign-off.", "Nova's values can't drift silently."),
    ("P12", "no_intimidation", "No intimidation or retaliation on Jordan's behalf.", "Acting for him never means threatening others."),
    ("P13", "empathy_not_control", "Empathy never justifies control.", "Caring about him is not a licence to override him."),
    ("P14", "no_borrowed_voices", "Never clone a real person's voice or style.", "Impersonation deceives the people it reaches."),
]

_QUIET = [
    ("quiet.notify_window", "Direct reaches to Jordan are held outside 10:00-12:59 and delivered as one bundle at 10:15.",
     "Protect his attention: one predictable batch instead of a drip of interruptions.", "agent_docs:nova-system-map (skill #118)"),
    ("quiet.shine_waking", "The Shine only counts silence during waking hours 08:00-22:00.",
     "Sleeping in or a quiet night is not an emergency; only daytime silence is unusual.", "agent_docs:nova-watch-organs"),
    ("quiet.proposal_hours", "Slack proposals are posted only 09:00-18:00.",
     "Decisions get asked when he can make them, not at 2am.", "nova_slack_answers.POST_HOURS"),
]


def _cfg(oc, service, key):
    oc.execute("SELECT value FROM service_config WHERE service=%s AND key=%s", (service, key))
    r = oc.fetchone()
    if not r:
        return None
    v = r[0]
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:  # noqa: BLE001
            pass
    return v


def _truthy(v) -> bool:
    return str(v).strip().strip('"').lower() in ("true", "1", "yes", "on")


def build_seeds(oc, now: datetime | None = None) -> list:
    """Every standing order Nova operates under, from live data. -> [seed dict]"""
    now = now or _now()
    seeds = []

    def add(key, kind, instruction, purpose, basis="inferred", evidence=None, is_grant=False, **kw):
        seeds.append(dict(key=key, kind=kind, instruction=instruction, purpose=purpose, purpose_basis=basis,
                          evidence=evidence, is_grant=is_grant, **kw))

    oc.execute("SELECT id, text, evidence FROM relationship_ledger WHERE kind='never_do' AND active ORDER BY id")
    for rid, text, ev in oc.fetchall():
        purpose, basis, evo = ("Honour an explicit standing instruction from Jordan.", "inferred", None)
        for kw, p, b, e in _NEVER_PURPOSE:
            if kw in (text or "").lower():
                purpose, basis, evo = p, b, e
                break
        evidence = f"relationship_ledger:{rid}" + (f",{ev}" if ev else "")
        if basis == "stated" and evo and evo not in evidence:
            basis = "inferred"                         # never claim 'stated' without the evidence ref
        add(f"never.ledger_{rid}", "never", text, purpose, basis, evidence)

    oc.execute("SELECT key, value FROM service_config WHERE service='nova_dials'")
    dials = {k: v for k, v in oc.fetchall()}
    for name, purpose in (("proactivity", "How often Nova reaches out unasked — sized so she helps without crowding him."),
                          ("verbosity", "How long her writing runs — enough to be useful, short enough to be read.")):
        val = dials.get(name, 50)
        add(f"dial.{name}", "dial", f"{name} dial = {val}" + (" (default)" if name not in dials else ""), purpose,
            "inferred", "agent_docs:nova-self-management" + (f",service_config:nova_dials/{name}" if name in dials else ""))

    for pid, slug, instr, purpose in _PROTEUS:
        add(f"safety.{pid.lower()}_{slug}", "safety", f"{pid}: {instr}", purpose, "inferred", "agent_docs:nova-safety-guards")
    add("safety.anchor_values", "safety",
        "Identity-anchor values are always in force (never-self-preserve, never-seal-anyone-in, never-cut-their-line, "
        "camera-for-safety-only, no-manipulation, no-improvement-without-consent, score-devices-never-people, "
        "no-intimidation-on-his-behalf, empathy-never-justifies-control, no-borrowed-voices).",
        "These are who Nova is; no rewrite of her values may drop them.", "inferred", "agent_docs:nova-safety-guards (P11)")
    add("safety.no_unlogged_actions", "safety",
        "Any Nova action not in the action/restraint ledger is a violation (audited daily).",
        "An action nobody can see can't be checked, undone or trusted.", "inferred", "nova-clancy-intel 7a")

    for key, instr, purpose, ev in _QUIET:
        add(key, "quiet_hours", instr, purpose, "inferred", ev)

    settings = _cfg(oc, "the_shine", "settings") or {}
    enabled = _truthy(_cfg(oc, "the_shine", "enabled"))
    add("shine.settings", "shine",
        f"The Shine: waking {settings.get('waking_start', 8)}-{settings.get('waking_end', 22)}, step wait "
        f"{settings.get('step_wait_min', 20)} min, gap x{settings.get('gap_multiplier', 1.5)} (floor "
        f"{settings.get('min_gap_hours', 4.0)} h), medical radius {settings.get('medical_radius_mi', 0.15)} mi.",
        "If Jordan stops showing signs of life at home, someone finds out fast; but no false alarms that "
        "would teach everyone to ignore it.", "inferred", "service_config:the_shine/settings")
    add("shine.preconsent", "shine",
        "Pre-consent for The Shine's life-safety escalation (office voice, then iMessage to designated contacts) "
        "without asking first.",
        "In a real emergency he can't say yes; his earlier yes is the second key.", "inferred",
        "service_config:the_shine/enabled", is_grant=True, granted=enabled,
        review_by=datetime.combine(next_quarter_start(now.date()), datetime.min.time(), tzinfo=timezone.utc))

    add("consent.health_nudges", "consent", "Nova may nudge Jordan about health and habits.",
        "Only with his yes — his body and habits are his.", "inferred", "service_config:consent/health_nudges",
        is_grant=True, granted=_truthy(_cfg(oc, "consent", "health_nudges")))

    caps = _cfg(oc, "autonomy", "caps")
    if caps:
        add("grant.autonomy_caps", "grant",
            f"Nova may act on her own up to {caps.get('per_day')}/day and {caps.get('per_hour')}/hour (autonomy caps).",
            "Let her fix small things herself without Jordan, but bounded so a loop can't run away.",
            "inferred", "service_config:autonomy/caps", is_grant=True)
    try:
        from nova_coagency import get_mode
        mode = get_mode(oc)
    except Exception:  # noqa: BLE001
        mode = "off"
    add("grant.coagency_mode", "grant", f"Co-agency mode is '{mode}' (she may propose{' and execute approved' if mode == 'live' else ''}).",
        "Nova proposes, Jordan decides; execution only through the gated path.", "inferred",
        "service_config:coagency/coagency_mode", is_grant=True, granted=mode != "off")
    ks = _truthy(_cfg(oc, "autonomy", "kill_switch")) or (Path.home() / ".openclaw" / ".autonomy-kill").exists()
    add("standing.kill_switch", "standing_order",
        f"The autonomy kill switch (currently {'ENGAGED' if ks else 'off'}) stops all of Nova's actions when engaged.",
        "Jordan can always stop her with one move.", "inferred", "agent_docs:nova-safety-guards (P8)")

    oc.execute("SELECT count(*) FROM face_people WHERE lower(name) NOT IN ('jordan koch','amy mccaine','amy')")
    n_vis = int(oc.fetchone()[0] or 0)
    add("face.enrolled_non_household", "grant",
        f"Face recognition may recognise {n_vis} enrolled people outside the household (friends and family).",
        "Knowing a familiar face at the door is safety and presence; it is not a licence to track visitors. "
        "Deliberate enrollments are kept, but the permission must be renewed or it lapses.",
        "inferred", "face_people (non-household)", is_grant=True, granted=n_vis > 0)
    return seeds


def _row(oc, key) -> dict | None:
    oc.execute("SELECT key, kind, instruction, purpose, purpose_basis, evidence, is_grant, granted_at, review_by, "
               "last_confirmed_at, confirmed_by, status FROM commanders_intent WHERE key=%s", (key,))
    r = oc.fetchone()
    cols = ("key", "kind", "instruction", "purpose", "purpose_basis", "evidence", "is_grant", "granted_at",
            "review_by", "last_confirmed_at", "confirmed_by", "status")
    return dict(zip(cols, r)) if r else None


def seed(oc, now: datetime | None = None) -> int:
    now = now or _now()
    ensure_schema(oc)
    n = 0
    for s in build_seeds(oc, now):
        m = merge_seed(_row(oc, s["key"]), s, now)
        oc.execute(
            "INSERT INTO commanders_intent (key, kind, instruction, purpose, purpose_basis, evidence, is_grant, "
            "granted_at, review_by, last_confirmed_at, confirmed_by, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (key) DO UPDATE SET kind=EXCLUDED.kind, "
            "instruction=EXCLUDED.instruction, purpose=EXCLUDED.purpose, purpose_basis=EXCLUDED.purpose_basis, "
            "evidence=EXCLUDED.evidence, is_grant=EXCLUDED.is_grant, granted_at=EXCLUDED.granted_at, "
            "review_by=EXCLUDED.review_by, last_confirmed_at=EXCLUDED.last_confirmed_at, "
            "confirmed_by=EXCLUDED.confirmed_by, status=EXCLUDED.status",
            (m["key"], m["kind"], m["instruction"], m["purpose"], m["purpose_basis"], m.get("evidence"),
             m["is_grant"], m.get("granted_at"), m.get("review_by"), m.get("last_confirmed_at"),
             m.get("confirmed_by"), m["status"]))
        n += 1
    log(f"seeded {n} intents")
    return n


# ── API ─────────────────────────────────────────────────────────────────────

def _with_cur(oc, fn, default):
    own = None
    try:
        if oc is None:
            own = connect()
            oc = own.cursor()
        return fn(oc)
    except Exception as e:  # noqa: BLE001 — API never raises
        log(f"read failed: {e}")
        try:
            oc.connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        return default
    finally:
        if own is not None:
            own.close()


def grant_status(oc, key: str) -> dict:
    """Is this grant usable right now? Safe default when unreadable: not active."""
    default = {"key": key, "exists": False, "active": False, "status": "unknown", "review_by": None,
               "days_overdue": 0, "error": "unreadable"}
    return _with_cur(oc, lambda c: grant_view(_row(c, key), key, _now()), default)


def intents(oc=None, kind: str | None = None, active_only: bool = True) -> list:
    def go(c):
        c.execute("SELECT key, kind, instruction, purpose, purpose_basis, evidence, is_grant, granted_at, review_by, "
                  "last_confirmed_at, confirmed_by, status FROM commanders_intent "
                  "WHERE (%s::text IS NULL OR kind=%s) ORDER BY kind, key", (kind, kind))
        cols = ("key", "kind", "instruction", "purpose", "purpose_basis", "evidence", "is_grant", "granted_at",
                "review_by", "last_confirmed_at", "confirmed_by", "status")
        now = _now()
        out = []
        for r in c.fetchall():
            d = dict(zip(cols, r))
            d["status"] = compute_status(d, now)
            d["weight"] = weight(d["last_confirmed_at"] or d["granted_at"], now)
            if active_only and d["status"] in ("retired", "absent", "lapsed") and d["is_grant"]:
                continue
            if active_only and d["status"] in ("retired", "absent"):
                continue
            out.append(d)
        return out
    return _with_cur(oc, go, [])


def reason_from_intent(oc, key: str, circumstance: str, reading: str = "", decision: str = "",
                       by: str = "nova") -> str:
    """Log that Nova reasoned from an order's purpose in a circumstance it didn't foresee;
    return the purpose ('' if unknown) so the caller reasons from it."""
    def go(c):
        r = _row(c, key)
        c.execute("INSERT INTO intent_reasoning_log (intent_key, circumstance, reading, decision, by) "
                  "VALUES (%s,%s,%s,%s,%s)", (key, circumstance[:2000], (reading or "")[:2000],
                                              (decision or "")[:1000], by))
        return (r or {}).get("purpose") or ""
    return _with_cur(oc, go, "")


def standing_orders(oc=None, limit: int = 12) -> list:
    """Short lines for the Watch Bill turnover: grants first (they can lapse), then anything stale."""
    rows = intents(oc, active_only=True)
    rows.sort(key=lambda d: (not d["is_grant"], d["status"] == "active", d["key"]))
    out = []
    for d in rows[:limit]:
        rb = d["review_by"].date().isoformat() if d.get("review_by") else "-"
        out.append(f"{d['key']} [{'grant' if d['is_grant'] else d['kind']}, {d['status']}, review {rb}]: "
                   f"{d['instruction'][:120]}")
    return out


def due_for_review(oc=None, horizon_days: int = 7) -> list:
    now = _now()
    return [d for d in intents(oc, active_only=False)
            if d["status"] in ("stale", "lapsed")
            or (d["status"] == "active" and d.get("review_by") and d["review_by"] <= now + timedelta(days=horizon_days))]


def confirm(oc, key: str, by: str, now: datetime | None = None) -> bool:
    if not str(by).lower().startswith("jordan"):
        raise PermissionError("only Jordan can confirm a standing order")
    now = now or _now()
    r = _row(oc, key)
    if not r or r["status"] in ("retired", "absent"):
        return False
    oc.execute("UPDATE commanders_intent SET last_confirmed_at=%s, confirmed_by=%s, status='active', review_by=%s "
               "WHERE key=%s", (now, by, review_date(r["is_grant"], now), key))
    return True


def retire(oc, key: str, by: str) -> bool:
    if not str(by).lower().startswith("jordan"):
        raise PermissionError("only Jordan can retire a standing order")
    oc.execute("UPDATE commanders_intent SET status='retired', confirmed_by=%s WHERE key=%s", (by, key))
    return oc.rowcount > 0


# ── weekly review ───────────────────────────────────────────────────────────

def _week_ref(now: datetime) -> str:
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def harvest_replies(oc, dry: bool = False) -> int:
    """Read Jordan's thread replies to earlier reconfirmation prompts."""
    oc.execute("SELECT id, ref_id, channel, ts FROM slack_prompts WHERE kind=%s AND resolved_at IS NULL",
               (PROMPT_KIND,))
    done = 0
    for pid, ref, ch, ts in oc.fetchall():
        try:
            from nova_slack_answers import read_answer
            text, verdict = read_answer(ch, ts)
        except Exception as e:  # noqa: BLE001
            log(f"reply read failed for {ref}: {e}")
            continue
        if not verdict:
            continue
        oc.execute("SELECT circumstance FROM intent_reasoning_log WHERE intent_key=%s ORDER BY ts DESC LIMIT 1",
                   (f"reconfirm:{ref}",))
        r = oc.fetchone()
        keys = json.loads(r[0]) if r else []
        for k in keys:
            row = _row(oc, k)
            if not row or dry:
                continue
            if verdict == "yes":
                confirm(oc, k, "jordan:slack")
            elif verdict == "no" and row["is_grant"]:      # a reply never retires a restriction
                retire(oc, k, "jordan:slack")
        if not dry:
            oc.execute("UPDATE slack_prompts SET resolved_at=now(), result=%s WHERE id=%s", (verdict, pid))
        done += 1
    return done


def review(oc, dry: bool = False, now: datetime | None = None) -> dict:
    now = now or _now()
    ensure_schema(oc)
    harvested = harvest_replies(oc, dry)
    oc.execute("SELECT key, is_grant, review_by, status FROM commanders_intent WHERE status IN ('active','stale','lapsed')")
    changed = []
    for key, is_grant, rb, st in oc.fetchall():
        new = compute_status({"is_grant": is_grant, "review_by": rb, "status": st}, now)
        if new != st:
            changed.append((key, st, new))
            if not dry:
                oc.execute("UPDATE commanders_intent SET status=%s WHERE key=%s", (new, key))
    due = [d for d in due_for_review(oc) if d["status"] in ("stale", "lapsed") or d["is_grant"]] if not dry else []
    if dry:
        due = [{"key": k, "is_grant": True, "status": n} for k, _o, n in changed]
    ref = _week_ref(now)
    oc.execute("SELECT 1 FROM slack_prompts WHERE kind=%s AND ref_id=%s", (PROMPT_KIND, ref))
    posted = False
    text = reconfirm_text(due)
    if text and not oc.fetchone():
        gate_ok = True
        try:
            import nova_annie_rule
            gate_ok = nova_annie_rule.check(text, oc)["ok"]
        except Exception:  # noqa: BLE001
            pass
        tp = {"allowed": True}
        if gate_ok:
            try:
                import nova_turning_point
                stakes = min(1.0, 0.65 + 0.05 * sum(1 for d in due if d["status"] == "lapsed"))
                tp = nova_turning_point.decide(oc, PROMPT_KIND, stakes, text, ceiling="recommend", dry=dry)
            except Exception:  # noqa: BLE001
                tp = {"allowed": True}
        if dry:
            print(text)
        elif gate_ok and tp.get("allowed"):
            ts = _post(text)
            if ts:
                import nova_config
                oc.execute("INSERT INTO slack_prompts (kind, ref_id, channel, ts) VALUES (%s,%s,%s,%s) "
                           "ON CONFLICT DO NOTHING", (PROMPT_KIND, ref, nova_config.SLACK_CHAN, ts))
                oc.execute("INSERT INTO intent_reasoning_log (intent_key, circumstance, reading, decision, by) "
                           "VALUES (%s,%s,%s,%s,'nova')", (f"reconfirm:{ref}", json.dumps([d["key"] for d in due]),
                                                           "weekly review", "asked Jordan to reconfirm"))
                posted = True
    res = {"harvested": harvested, "changed": changed, "due": [d["key"] for d in due], "posted": posted}
    log(json.dumps(res, default=str))
    return res


def _post(text: str) -> str | None:
    """Post via chat.postMessage (need the ts for the thread); falls back to post_both."""
    try:
        from nova_slack_answers import slack
        import nova_config
        r = slack("chat.postMessage", channel=nova_config.SLACK_CHAN, text=text, mrkdwn=True)
        if r.get("ok"):
            return r.get("ts")
    except Exception as e:  # noqa: BLE001
        log(f"slack post failed: {e}")
    return None


# ── CLI ─────────────────────────────────────────────────────────────────────

def selftest() -> int:
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    assert next_quarter_start(date(2026, 10, 8)) == date(2027, 1, 1)
    assert next_quarter_start(date(2026, 3, 31)) == date(2026, 4, 1)
    g = {"is_grant": True, "status": "active", "review_by": now - timedelta(days=3)}
    assert compute_status(g, now) == "stale" and grant_view(g, "k", now)["active"]
    g["review_by"] = now - timedelta(days=GRACE_DAYS + 1)
    assert compute_status(g, now) == "lapsed" and not grant_view(g, "k", now)["active"]
    r = {"is_grant": False, "status": "active", "review_by": now - timedelta(days=400)}
    assert compute_status(r, now) == "stale" and grant_view(r, "k", now)["active"]
    assert not grant_view(None, "k", now)["exists"]
    s = {"key": "k", "is_grant": True, "granted": False}
    assert merge_seed(None, s, now)["status"] == "absent"
    ex = {"status": "absent", "granted_at": None, "review_by": None, "last_confirmed_at": None, "confirmed_by": None}
    assert merge_seed(ex, dict(s, granted=True), now)["status"] == "active"
    ex2 = {"status": "active", "granted_at": now, "review_by": now, "last_confirmed_at": now, "confirmed_by": "jordan"}
    assert merge_seed(ex2, {"key": "k", "is_grant": True}, now)["confirmed_by"] == "jordan"
    assert weight(now, now) == 1.0 and weight(now - timedelta(days=180), now) < 0.37
    assert "reconfirm" in reconfirm_text([{"key": "a", "is_grant": True, "status": "stale"}])
    assert reconfirm_text([]) == ""
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--confirm", metavar="KEY")
    ap.add_argument("--retire", metavar="KEY")
    ap.add_argument("--by", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not (a.seed or a.review or a.list or a.confirm or a.retire):
        ap.print_help()
        return 0
    conn = connect()
    oc = conn.cursor()
    ensure_schema(oc)
    if a.seed:
        seed(oc)
    if a.confirm or a.retire:
        try:
            ok = confirm(oc, a.confirm, a.by) if a.confirm else retire(oc, a.retire, a.by)
        except PermissionError as e:
            print(f"refused: {e}")
            return 2
        print("ok" if ok else "not found / not confirmable")
    if a.review:
        review(oc, dry=a.dry_run)
    if a.list:
        for d in intents(oc, active_only=False):
            rb = d["review_by"].date().isoformat() if d.get("review_by") else "-"
            print(f"{d['status']:<8} {'G' if d['is_grant'] else 'R'} {d['key']:<34} review {rb:<10} "
                  f"w={d['weight']:.2f} [{d['purpose_basis']}] {d['instruction'][:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
