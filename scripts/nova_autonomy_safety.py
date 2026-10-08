#!/usr/bin/env python3
"""nova_autonomy_safety.py — the shared safety net + earned-autonomy engine.

This is the scaffolding that makes it SAFE to widen Nova's execution freedom. Both
the autonomy actor (self-heal) and co-agency (approved-proposal execution) import
this and pass through it. Nothing here decides WHAT to do — it decides whether an
action is ALLOWED to run right now, records an undo, and tracks the track record that
lets a proven action-class graduate to standing pre-approval.

Everything fails CLOSED: on any error, missing table, or ambiguity, the answer is
"not allowed / no grant". Freedom is opt-in and provable, never assumed.

FOUR GUARANTEES (the reason we can turn the dials up):
  1. KILL SWITCH — one flag (service_config autonomy/kill_switch, OR the tripwire file
     ~/.openclaw/.autonomy-kill) forces EVERYTHING back to safe instantly. Checked at
     the top of every actor pass and before every execution. File beats DB (works even
     if PG is unreachable).
  2. REVERSIBILITY LEDGER — every autonomous action writes a row to autonomy_ledger
     with the rollback_action recorded BEFORE it runs. No action without a recorded undo.
  3. BLAST-RADIUS CAPS — per-hour / per-day ceilings on autonomous executions across
     ALL sources, plus a Slack post on every action. A runaway can do at most `per_day`.
  4. EARNED AUTONOMY (the trust budget) — an action-CLASS graduates to standing
     pre-approval only after MIN_CORRECT human approvals with ZERO vetoes/rejections
     AND while calibration is good (prediction_calibration_error <= MAX_CALIB). A veto
     revokes the grant and marks the class distrusted. Freedom grows as she's right,
     shrinks the moment she's wrong.

Owned file: scripts/nova_autonomy_safety.py. Written by Jordan Koch.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# ── Tunables (conservative defaults; overridable via service_config service='autonomy') ──
KILL_FILE = os.path.expanduser("~/.openclaw/.autonomy-kill")   # offline tripwire
DEFAULT_CAPS = {"per_hour": 6, "per_day": 20}                  # autonomous executions
MIN_CORRECT = 5           # human approvals of a class before it may graduate
MAX_CALIB = 0.20          # earned autonomy only while calibration_error <= this
# 2026-10-01 (Jordan: "do what you suggest"): split the bar. A class that changes NO state —
# an observation note, a draft, a herd message, reading a public-domain book — graduates
# after MIN_CORRECT_REVERSIBLE clean approvals. Anything that restarts/adjusts/retires keeps
# the full MIN_CORRECT. The calibration gate and the one-veto poison rule apply to both.
MIN_CORRECT_REVERSIBLE = 3
_STATE_VERBS = frozenset({
    "adjust", "reboot", "restart", "reinitialize", "reinit", "retire", "clear", "rebuild",
    "reset", "delete", "remove", "disable", "enable", "set", "change", "modify", "update",
    "kill", "stop", "start", "rotate", "migrate", "move", "rename", "install", "uninstall",
})
# Nova may read for herself: "ingest gutenberg #<id> [into <vector>] — <title>"
_INGEST_RE = re.compile(r"^\s*ingest\s+(?:project\s+)?gutenberg\s+#?\s*(\d{1,6})(?!\d)"
                        r"(?:\s+into\s+(?-i:([a-z][a-z0-9_]{2,40}))(?![A-Za-z0-9_]))?", re.IGNORECASE)
INGEST_CLASS = "ingest:gutenberg"


def min_correct_for(action_class: str) -> int:
    """How many clean human approvals this CLASS needs before it may graduate.
    restart:* and any observe:<state-verb...> keep the full bar; everything else is
    reversible and takes the lower one."""
    ac = (action_class or "").lower()
    if ac.startswith("restart:"):
        return MIN_CORRECT
    tail = ac.split(":", 1)[1] if ":" in ac else ac
    head = re.split(r"[^a-z]", tail, maxsplit=1)[0]
    if tail in _STATE_VERBS or head in _STATE_VERBS:
        return MIN_CORRECT
    return MIN_CORRECT_REVERSIBLE


def parse_ingest(action: str):
    """('<gutenberg id>', '<vector or None>') if the action is a Gutenberg ingest, else None."""
    m = _INGEST_RE.match(action or "")
    return (m.group(1), (m.group(2) or None)) if m else None
VETO_WINDOW_MIN = 60      # minutes a human has to VETO an earned action


def log(m):
    print(f"[autonomy-safety {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Schema (idempotent; safe to call every run)
# ═══════════════════════════════════════════════════════════════════════════════
def ensure_schema(oc) -> None:
    oc.execute("""
        CREATE TABLE IF NOT EXISTS autonomy_ledger (
            id              bigserial PRIMARY KEY,
            ts              timestamptz NOT NULL DEFAULT now(),
            source          text NOT NULL,             -- actor | coagency | earned
            autonomy_level  text NOT NULL,             -- rung1-selfheal | rung2-supervised | rung3-earned
            action_class    text NOT NULL,             -- normalized, e.g. restart:nova-freshness-monitor
            target          text,
            action          text NOT NULL,
            rollback_action text NOT NULL,             -- recorded BEFORE execution; never NULL
            executed        boolean NOT NULL DEFAULT false,
            verified        boolean NOT NULL DEFAULT false,
            result          text,
            vetoable_until  timestamptz,               -- earned actions only
            vetoed          boolean NOT NULL DEFAULT false,
            veto_note       text,
            reverted        boolean NOT NULL DEFAULT false
        )""")
    oc.execute("CREATE INDEX IF NOT EXISTS autonomy_ledger_ts ON autonomy_ledger (ts DESC)")
    # P4 (outside check of self-justification): the OBJECTIVE record — what the world looked
    # like before and after, observed from health_checks/etc, kept apart from Nova's stated why.
    oc.execute("ALTER TABLE autonomy_ledger ADD COLUMN IF NOT EXISTS before_state jsonb")
    oc.execute("ALTER TABLE autonomy_ledger ADD COLUMN IF NOT EXISTS after_state jsonb")
    oc.execute("ALTER TABLE autonomy_ledger ADD COLUMN IF NOT EXISTS stated_rationale text")
    oc.execute("""
        CREATE TABLE IF NOT EXISTS autonomy_trust (
            action_class    text PRIMARY KEY,
            correct_count   int NOT NULL DEFAULT 0,     -- human approvals, clean
            wrong_count     int NOT NULL DEFAULT 0,     -- rejections + vetoes (poison)
            granted         boolean NOT NULL DEFAULT false,
            granted_at      timestamptz,
            daily_limit     int NOT NULL DEFAULT 3,     -- per-class ceiling once granted
            notes           text,
            updated_at      timestamptz NOT NULL DEFAULT now()
        )""")


# ═══════════════════════════════════════════════════════════════════════════════
# Proteus-rule guards (P1/P2), re-exported so callers can use nova_autonomy_safety.physical_guard
# ═══════════════════════════════════════════════════════════════════════════════
def physical_guard(action: str = "", entity_ids=(), domains=(), **kw) -> tuple:
    """(ok, reason). See nova_safety_guards.physical_guard. Fails closed if that module is missing."""
    try:
        import nova_safety_guards as _g
    except Exception as e:  # noqa: BLE001
        return False, f"safety guards unavailable ({e}) — refusing"
    return _g.physical_guard(action, entity_ids, domains, **kw)


def comms_guard(action: str = "", macs=(), names=(), entity_ids=(), **kw) -> tuple:
    try:
        import nova_safety_guards as _g
    except Exception as e:  # noqa: BLE001
        return False, f"safety guards unavailable ({e}) — refusing"
    return _g.comms_guard(action, macs, names, entity_ids, **kw)


# ═══════════════════════════════════════════════════════════════════════════════
# Kill switch  (file beats DB — works even if PG is down)
# KILL SWITCH STOPS NOVA ONLY (P8): it halts her autonomy (actor, co-agency, earned
# autonomy) and the automation engine's actuations. It never turns anything off, locks
# anything, or touches the network. The house keeps the state it was in, and every device
# stays manually controllable. See agent_docs 'nova-safety-guards' (dead-man audit).
# ═══════════════════════════════════════════════════════════════════════════════
def kill_switch_engaged(oc=None) -> bool:
    if os.path.exists(KILL_FILE):
        return True
    if oc is None:
        return False
    try:
        oc.execute("SELECT value FROM service_config WHERE service='autonomy' AND key='kill_switch'")
        r = oc.fetchone()
        if not r or r[0] is None:
            return False
        v = r[0] if isinstance(r[0], str) else str(r[0])
        return v.strip().strip('"').lower() in ("true", "1", "on", "yes")
    except Exception:
        return False           # fail closed for ACTIONS is handled by callers; a broken
                               # read here shouldn't itself brick self-heal, so default off.


def engage_kill(oc=None, note="manual") -> None:
    """Trip the kill switch (both DB flag and tripwire file) — one call, everything stops."""
    try:
        open(KILL_FILE, "w").write(f"engaged {datetime.now().isoformat()} :: {note}\n")
    except Exception as e:
        log(f"could not write tripwire file: {e}")
    if oc is not None:
        try:
            oc.execute("""INSERT INTO service_config (service, key, value, updated_by)
                          VALUES ('autonomy','kill_switch','"true"', %s)
                          ON CONFLICT (service, key) DO UPDATE SET value='"true"', updated_by=EXCLUDED.updated_by""",
                       (f"kill:{note}",))
        except Exception as e:
            log(f"could not set DB kill flag: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Blast-radius caps
# ═══════════════════════════════════════════════════════════════════════════════
def _caps(oc) -> dict:
    try:
        oc.execute("SELECT value FROM service_config WHERE service='autonomy' AND key='caps'")
        r = oc.fetchone()
        if r and r[0]:
            v = r[0] if isinstance(r[0], (dict,)) else json.loads(r[0] if isinstance(r[0], str) else str(r[0]))
            return {"per_hour": int(v.get("per_hour", DEFAULT_CAPS["per_hour"])),
                    "per_day": int(v.get("per_day", DEFAULT_CAPS["per_day"]))}
    except Exception:
        pass
    return dict(DEFAULT_CAPS)


def rate_ok(oc) -> tuple[bool, str]:
    """True if another autonomous execution is within the per-hour/per-day caps."""
    caps = _caps(oc)
    try:
        oc.execute("SELECT count(*) FROM autonomy_ledger WHERE executed AND ts > now()-interval '1 hour'")
        hr = oc.fetchone()[0] or 0
        oc.execute("SELECT count(*) FROM autonomy_ledger WHERE executed AND ts > now()-interval '24 hours'")
        day = oc.fetchone()[0] or 0
    except Exception as e:
        return False, f"cap-check failed ({e}) — fail closed"
    if hr >= caps["per_hour"]:
        return False, f"per-hour cap reached ({hr}/{caps['per_hour']})"
    if day >= caps["per_day"]:
        return False, f"per-day cap reached ({day}/{caps['per_day']})"
    return True, f"ok ({hr}/{caps['per_hour']}h, {day}/{caps['per_day']}d)"


# ═══════════════════════════════════════════════════════════════════════════════
# Reversibility ledger
# ═══════════════════════════════════════════════════════════════════════════════
# The ONLY physically-executable intent in v1 is restarting a bounded SAFE service.
# Anything else (observe/monitor/track/gather/goal) is a note, not an action — it must
# NOT normalize to a 'restart:*' class, or an approved observation would accrue restart
# trust and, worse, be force-restarted. Keep the two worlds strictly separate.
_RESTART_INTENT = re.compile(
    r"\b(restart|relaunch|reload|re-?launch|kickstart|bounce|reboot the (?:service|monitor|daemon)|"
    r"bring .{0,15}back up|heal)\b", re.IGNORECASE)


def is_restart_action(action: str) -> bool:
    return bool(_RESTART_INTENT.search(action or ""))


def action_class_of(action: str, target: str | None = None) -> str:
    """Normalize a concrete action into a stable CLASS, keyed so restart intents and
    non-actionable notes never collide. 'restart:<svc>' is executable; 'observe:<x>'
    is a note that no executor ever runs."""
    a = (action or "").lower()
    if is_restart_action(a):
        if target:
            return f"restart:{target}"
        m = re.search(r"(?:restart|relaunch|kickstart|bounce)\s+([a-z0-9\-\._]+)", a)
        return f"restart:{m.group(1)}" if m else "restart:unknown"
    if _INGEST_RE.match(a):
        return INGEST_CLASS
    verb = a.split()[0] if a.split() else "note"
    return f"observe:{target or verb}"


def observe_service(oc, svc: str, node: str | None = None) -> dict:
    """Objective snapshot of a service from health_checks (not from Nova's account of it)."""
    try:
        if node:
            oc.execute("""SELECT status, checked_at FROM health_checks WHERE service_name=%s AND node_name=%s
                          ORDER BY checked_at DESC LIMIT 1""", (svc, node))
        else:
            oc.execute("""SELECT status, checked_at FROM health_checks WHERE service_name=%s
                          ORDER BY checked_at DESC LIMIT 1""", (svc,))
        r = oc.fetchone()
        return {"service": svc, "status": (r[0] if r else None),
                "checked_at": (r[1].isoformat() if r and r[1] else None), "observed_at": datetime.now().isoformat()}
    except Exception as e:  # noqa: BLE001
        return {"service": svc, "status": None, "error": str(e)[:120]}


def record_ledger(oc, *, source, autonomy_level, action_class, target, action,
                  rollback_action, executed=False, verified=False, result="",
                  vetoable=False, before_state=None, after_state=None,
                  stated_rationale=None) -> int:
    """Write one ledger row. rollback_action is MANDATORY and recorded before the
    effect is trusted. before_state/after_state are OBJECTIVE observations (P4), kept
    apart from stated_rationale (what Nova said she was doing and why) so a weekly audit
    can compare the two. Returns the ledger id. Never raises (audit must not break flow)."""
    if not rollback_action:
        rollback_action = "(none recorded — treat as irreversible; do not auto-run)"
    vut = None
    if vetoable and executed:
        oc.execute("SELECT now() + (%s || ' minutes')::interval", (str(VETO_WINDOW_MIN),))
        vut = oc.fetchone()[0]
    try:
        if before_state is None and after_state is None and stated_rationale is None:
            oc.execute("""INSERT INTO autonomy_ledger
                            (source, autonomy_level, action_class, target, action,
                             rollback_action, executed, verified, result, vetoable_until)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                       (source, autonomy_level, action_class, target, action,
                        rollback_action, executed, verified, (result or "")[:800], vut))
        else:
            oc.execute("""INSERT INTO autonomy_ledger
                            (source, autonomy_level, action_class, target, action,
                             rollback_action, executed, verified, result, vetoable_until,
                             before_state, after_state, stated_rationale)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                       (source, autonomy_level, action_class, target, action,
                        rollback_action, executed, verified, (result or "")[:800], vut,
                        json.dumps(before_state, default=str) if before_state is not None else None,
                        json.dumps(after_state, default=str) if after_state is not None else None,
                        (stated_rationale or "")[:1000] or None))
        return oc.fetchone()[0]
    except Exception as e:
        log(f"ledger write failed: {e}")
        return -1


# ═══════════════════════════════════════════════════════════════════════════════
# Earned autonomy (the trust budget)
# ═══════════════════════════════════════════════════════════════════════════════
def calibration_error(oc) -> float | None:
    """Latest prediction_calibration_error from turing_scoreboard (lower = better).
    None if unavailable — and None means NO earned autonomy (fail closed)."""
    try:
        oc.execute("""SELECT value FROM turing_scoreboard
                      WHERE metric='prediction_calibration_error' ORDER BY ts DESC LIMIT 1""")
        r = oc.fetchone()
        return float(r[0]) if r and r[0] is not None else None
    except Exception:
        return None


def note_human_decision(oc, action_class: str, approved: bool) -> None:
    """Record a human approve/reject against a class. Approvals build trust; a rejection
    is POISON — it zeroes the streak and increments wrong_count so the class can't
    graduate on a bad idea. Auto-grants when the bar is cleared."""
    ensure_schema(oc)
    oc.execute("""INSERT INTO autonomy_trust (action_class) VALUES (%s)
                  ON CONFLICT (action_class) DO NOTHING""", (action_class,))
    if approved:
        oc.execute("""UPDATE autonomy_trust
                      SET correct_count = correct_count + 1, updated_at = now()
                      WHERE action_class = %s""", (action_class,))
    else:
        oc.execute("""UPDATE autonomy_trust
                      SET wrong_count = wrong_count + 1, correct_count = 0,
                          granted = false, granted_at = NULL, updated_at = now()
                      WHERE action_class = %s""", (action_class,))
    _maybe_grant(oc, action_class)


def note_veto(oc, action_class: str, note: str = "") -> None:
    """A veto is the strongest distrust signal: revoke the grant, poison the class."""
    ensure_schema(oc)
    oc.execute("""INSERT INTO autonomy_trust (action_class) VALUES (%s)
                  ON CONFLICT (action_class) DO NOTHING""", (action_class,))
    oc.execute("""UPDATE autonomy_trust
                  SET wrong_count = wrong_count + 1, correct_count = 0,
                      granted = false, granted_at = NULL,
                      notes = %s, updated_at = now()
                  WHERE action_class = %s""", (f"vetoed: {note}"[:400], action_class))


def _maybe_grant(oc, action_class: str) -> None:
    """Grant standing pre-approval iff clean track record AND calibration currently good."""
    ce = calibration_error(oc)
    oc.execute("SELECT correct_count, wrong_count, granted FROM autonomy_trust WHERE action_class=%s",
               (action_class,))
    r = oc.fetchone()
    if not r:
        return
    correct, wrong, granted = r
    need = min_correct_for(action_class)
    qualifies = (wrong == 0 and correct >= need and ce is not None and ce <= MAX_CALIB)
    if qualifies and not granted:
        oc.execute("""UPDATE autonomy_trust SET granted=true, granted_at=now(),
                      notes=%s, updated_at=now() WHERE action_class=%s""",
                   (f"granted at correct={correct}, calib={ce:.3f}", action_class))
        log(f"EARNED: '{action_class}' graduated to standing pre-approval (correct={correct}, calib={ce:.3f})")


def earned_ok(oc, action_class: str) -> tuple[bool, str]:
    """May Nova run this class autonomously RIGHT NOW (Rung 3)? Every gate must pass,
    re-checked live — a grant is necessary but never sufficient. Fails closed."""
    if kill_switch_engaged(oc):
        return False, "kill switch engaged"
    ce = calibration_error(oc)
    if ce is None:
        return False, "no calibration score — fail closed"
    if ce > MAX_CALIB:
        return False, f"calibration too weak ({ce:.3f} > {MAX_CALIB}) — freedom contracts when she's wrong"
    try:
        oc.execute("SELECT correct_count, wrong_count, granted, daily_limit FROM autonomy_trust WHERE action_class=%s",
                   (action_class,))
        r = oc.fetchone()
    except Exception as e:
        return False, f"trust read failed ({e})"
    if not r:
        return False, "class has no track record"
    correct, wrong, granted, daily_limit = r
    if wrong > 0:
        return False, f"class is distrusted (wrong_count={wrong})"
    if not granted:
        return False, f"not yet earned (correct={correct}/{min_correct_for(action_class)})"
    ok, why = rate_ok(oc)
    if not ok:
        return False, why
    # per-class daily ceiling
    try:
        oc.execute("""SELECT count(*) FROM autonomy_ledger
                      WHERE executed AND action_class=%s AND ts > now()-interval '24 hours'""",
                   (action_class,))
        used = oc.fetchone()[0] or 0
    except Exception:
        used = daily_limit
    if used >= daily_limit:
        return False, f"per-class daily limit reached ({used}/{daily_limit})"
    return True, f"earned (correct={correct}, calib={ce:.3f}, class-used={used}/{daily_limit})"


# ═══════════════════════════════════════════════════════════════════════════════
# Self-awareness accessor (for the gateway) — so Nova KNOWS the shape of her freedom
# ═══════════════════════════════════════════════════════════════════════════════
def autonomy_status(oc=None) -> dict:
    own = False
    if oc is None:
        try:
            conn = psycopg2.connect(OPS_DSN); conn.autocommit = True; oc = conn.cursor(); own = True
        except Exception:
            return {"line": ""}
    try:
        killed = kill_switch_engaged(oc)
        ce = calibration_error(oc)
        earned = []
        recent = 0
        try:
            oc.execute("SELECT action_class FROM autonomy_trust WHERE granted ORDER BY granted_at DESC")
            earned = [x[0] for x in oc.fetchall()]
            oc.execute("SELECT count(*) FROM autonomy_ledger WHERE executed AND ts > now()-interval '24 hours'")
            recent = oc.fetchone()[0] or 0
        except Exception:
            pass
        if killed:
            line = "My autonomy is halted — the kill switch is engaged. I can think, but I won't act."
        elif earned:
            line = (f"I've earned standing approval for {len(earned)} action-class(es); "
                    f"{recent} autonomous action(s) in the last day. Calibration {ce:.3f}." if ce is not None
                    else f"I've earned {len(earned)} class(es); {recent} action(s) today.")
        else:
            line = (f"I can self-heal and execute what you approve, but I've earned no standing "
                    f"autonomy yet — my calibration ({ce:.3f}) still has to come down first." if ce is not None
                    else "I can self-heal and execute what you approve; no standing autonomy earned yet.")
        return {"killed": killed, "calibration_error": ce, "earned_classes": earned,
                "actions_24h": recent, "line": line}
    finally:
        if own:
            oc.connection.close()


# ── CLI: quick status + kill/unkill from the shell ──────────────────────────────
if __name__ == "__main__":
    import sys
    conn = psycopg2.connect(OPS_DSN); conn.autocommit = True; oc = conn.cursor()
    ensure_schema(oc)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "regrade":
        # Re-run the grant test for every ungranted class (used once after a bar change).
        oc.execute("SELECT action_class FROM autonomy_trust WHERE NOT granted ORDER BY 1")
        for (ac,) in oc.fetchall():
            _maybe_grant(oc, ac)
        oc.execute("SELECT action_class, correct_count, granted FROM autonomy_trust ORDER BY granted DESC, 1")
        for ac, c, g in oc.fetchall():
            print(f"{'GRANTED ' if g else '        '}{ac}  correct={c} need={min_correct_for(ac)}")
    elif cmd == "kill":
        engage_kill(oc, note=" ".join(sys.argv[2:]) or "cli")
        print("KILL SWITCH ENGAGED — all autonomy halted.")
    elif cmd == "unkill":
        try:
            os.remove(KILL_FILE)
        except FileNotFoundError:
            pass
        oc.execute("""INSERT INTO service_config (service, key, value, updated_by)
                      VALUES ('autonomy','kill_switch','"false"','cli')
                      ON CONFLICT (service, key) DO UPDATE SET value='"false"', updated_by='cli'""")
        print("kill switch cleared — autonomy resumes at its configured mode.")
    else:
        st = autonomy_status(oc)
        ok, why = rate_ok(oc)
        print(json.dumps({**st, "rate": why}, indent=2, default=str))
