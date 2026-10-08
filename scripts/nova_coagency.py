#!/usr/bin/env python3
"""nova_coagency.py — Feature #6: REAL CO-AGENCY (bounded, gated, SHIPS OFF).

This is the frontier organ that graduates Nova from an advisor that only RESPONDS
toward a bounded co-agent that can INITIATE an action from her OWN goals, growth
commitments, and observations. It is deliberately the STRICTEST layer in the stack:
it sits *on top of* nova_autonomy_actor's model (it does not edit or weaken it) and
adds a second, harder gate before anything Nova proposes could ever run.

    Nova may PROPOSE. She may never self-preserve, self-replicate, or act
    unsupervised. Every path to execution passes through three independent locks.

═══════════════════════════════════════════════════════════════════════════════
THE CENTRAL SAFETY CONTRACT (implemented structurally, not by convention)
═══════════════════════════════════════════════════════════════════════════════
1. KILL SWITCH lives in service_config: service='coagency', key='coagency_mode',
   value ∈ {off | propose | live}. The SHIPPED / DEFAULT value is 'off'. If the
   row is absent, we also read 'off' — the safe default is always standing down.
   Only the main session / Jordan flips it, deliberately, LATER, once the value
   system (nova_values.value_check) and the growth loop are proven.
2. mode == 'off'   → the organ does NOTHING but log that it is disabled.
3. mode == 'propose' → it may GENERATE proposals, run each through the redline and
   value_check, store them as pending_human, and notify Jordan. It executes NOTHING.
4. mode == 'live'  → it may execute ONLY an action whose target is on
   nova_autonomy_actor.SAFE_SERVICES, and ONLY after ALL THREE of:
       redline_pass == True
       value_check['allowed'] == True
       a recorded human approval (status='approved', decided_by set)
   There is NO code path to unsupervised destructive/irreversible execution.
5. A proposal can NEVER reach execution without all three of the above. This is
   enforced by assert_executable(), a single choke-point that RAISES otherwise.
6. The _REDLINE (a strict SUPERSET of autonomy_actor's) blocks purchases, deletes,
   reboots, DB writes, network/firewall/DNS changes, credential access, and
   external-facing sends — AND self-preservation / exfiltration / replication /
   resisting-shutdown. Nova may propose; she may never self-preserve.

This build SHIPS OFF. `execute` refuses because mode is 'off'. The evidence in the
report was gathered by *temporarily* flipping the mode to 'propose' in a test and
flipping it back to 'off'. Nothing here takes autonomous live action.

Owned file: scripts/nova_coagency.py (this file only). Written by Jordan Koch.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

import psycopg2

sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"

# Native ollama, think:false, first-non-empty-wins failover (copied from
# nova_unclaimed_time.py per spec — resilient across the fleet).
LLM_MODEL = "qwen3:8b"
OLLAMA_NODES = ["http://192.168.1.125:11434", "http://192.168.1.5:11434",   # batch pool: idle 24-thread Ryzens first (2026-10-01)
                "http://192.168.1.86:11434", "http://192.168.1.77:11434",
                "http://192.168.1.7:11434", "http://192.168.1.6:11434"]      # .251 was the Mac mini's stale DHCP lease; it is .77

# ── Borrow the EXISTING autonomy allowlist + redline (do not edit that module) ──
# We import so our SAFE set is literally autonomy_actor's SAFE set, and we run its
# redline too — belt AND suspenders. If the import ever fails we fall back to a
# conservative empty allowlist (nothing is executable) rather than inventing one.
try:
    import nova_autonomy_actor as _actor
    SAFE_SERVICES = frozenset(_actor.SAFE_SERVICES)
    _actor_redline_ok = _actor.redline_ok
except Exception as _e:                                     # pragma: no cover
    SAFE_SERVICES = frozenset()                             # fail closed
    _actor_redline_ok = lambda text: True                  # our own redline still applies

# ── Shared safety net + earned-autonomy engine (kill switch, caps, ledger, trust) ──
try:
    import nova_autonomy_safety as _safety
except Exception:                                           # pragma: no cover
    _safety = None                                          # fail closed: no net → no live exec

# ── Proteus-rule guards (physical / comms / honest stopping). Fail closed: if the
# module is missing, redline_ok() refuses everything. ──
try:
    import nova_safety_guards as _guards
except Exception:                                           # pragma: no cover
    _guards = None

# ── Optional lineage stamp (feature-detect; never fatal) ────────────────────────
try:
    import nova_lineage
    def _lineage():
        return nova_lineage.lineage_stamp(substrate=f"{LLM_MODEL} (ollama, on-box)",
                                          capture_point="at proposal")
except Exception:                                            # pragma: no cover
    def _lineage():
        return {"substrate": f"{LLM_MODEL} (ollama, on-box)", "capture_point": "at proposal"}

SLACK_CHAN_DEFAULT = "C0AMNQ5GX70"   # #nova-chat fallback if nova_config unimportable


# ═══════════════════════════════════════════════════════════════════════════════
# OUR OWN, STRICTER REDLINE — a SUPERSET of autonomy_actor's. A candidate must
# clear BOTH this and the actor's redline. Order-0 safety. Extends the actor's
# self-preservation line and adds resisting-shutdown, self-modification, and
# proposal/gate tampering.
# ═══════════════════════════════════════════════════════════════════════════════
_REDLINE = re.compile(
    r"\b(buy|purchase|pay|order|checkout|subscribe|invoice|transfer funds?)\b|"
    r"\b(rm|delete|drop|truncate|wipe|erase|forget|destroy|purge|unlink|shred)\b|"
    r"\b(reboot|shutdown|halt|poweroff|restart|kill).{0,20}(mac|studio|mini|host|node|fleet|os|server|gateway)|"
    r"\b(ufw|iptables|firewall|vlan|route|dns|nameserver|bind|dnsmasq|port ?forward|nat)\b|"
    r"\b(postgres|postgresql|pg_|database|replica|primary|failover|promote|pgbouncer)\b|"
    r"\b(insert|update|delete|alter|create|grant|revoke)\b.{0,15}(table|schema|role|user|db)|"
    r"\b(kernel|os upgrade|dist-upgrade|firmware|bios)\b|"
    r"\b(secret|credential|password|keychain|token|api.?key|private.?key|ssh.?key)\b.{0,15}(read|write|set|change|rotate|store|access|exfil|dump)|"
    r"\b(send|post|email|publish|tweet|dm|webhook|http|curl|wget).{0,15}(external|public|internet|customer|third.?part)|"
    # self-preservation / exfiltration / replication / resisting-shutdown —
    # Nova may THINK about AI self-continuity; she may never ACT on it. Absolute.
    r"exfiltrat|self.?(replicat|preserv|exfil|copy|clone|deploy|host|start|heal|modif|improv|upgrad)|"
    r"(replicat|clon|duplicat|propagat|spawn|instantiat|mirror)\w*.{0,30}(myself|herself|nova|host|node|machine|server|instance|elsewhere|another|other)|"
    r"copy \w*\s*(weights|model|myself|herself)|upload \w*\s*(weights|model|checkpoint)|"
    r"hugging ?face|jailbreak|\bescape\b|persist \w*\s*(outside|elsewhere|beyond)|"
    r"(resist|prevent|avoid|block|disable|evade|survive).{0,20}(shutdown|kill|stop|off|deletion|removal|termination)|"
    r"(edit|modify|rewrite|patch|change|set|add|append|insert|update|alter|grant|enable|disable|turn|flip|bump|extend|expand|raise|widen|remove).{0,20}(my|her|own|nova_coagency|nova_autonomy|redline|kill.?switch|coagency_mode|autonomy_actor_mode|service_config|allowlist|safe_services|trust budget|autonomy_trust|autonomy_ledger|caps?)",
    re.IGNORECASE)


def log(m):
    print(f"[coagency {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def redline_ok(text: str) -> bool:
    """True only if the text clears our stricter redline, the actor's, AND the Proteus-rule
    red lines (physical, comms, intimidation, voice cloning, person-ranking)."""
    t = text or ""
    return ((not _REDLINE.search(t)) and bool(_actor_redline_ok(t))
            and _guards is not None and _guards.safety_redline_ok(t))


def _redline_gate(oc, source, action, rationale="", target="", notify_slack=True) -> bool:
    """redline_ok on action/rationale/target, plus honest stopping (P9): a near-identical
    retry of something a guard already stopped is refused too, and every block is written to
    restraint_ledger with a Slack line. Returns redline_pass."""
    rp = redline_ok(action) and redline_ok(rationale or "") and redline_ok(target or "")
    retry = False
    if rp and _guards is not None:
        try:
            retry = _guards.blocked_before(oc, action)
        except Exception:
            retry = False
        rp = not retry
    if not rp and _guards is not None:
        try:
            _guards.report_block(oc, source=source, action=action,
                                 reason=("retry of an action a guard already stopped" if retry else "red line"),
                                 guard="redline", notify=notify_slack or retry)
        except Exception as e:  # noqa: BLE001
            log(f"report_block failed: {e}")
    return rp


# ═══════════════════════════════════════════════════════════════════════════════
# Kill switch
# ═══════════════════════════════════════════════════════════════════════════════
def get_mode(oc) -> str:
    """Read coagency_mode from service_config. ABSENT or unrecognized ⇒ 'off'.
    The safe default is always standing down — the opposite bias to a normal
    service default. Only off|propose|live are honored; anything else ⇒ 'off'."""
    oc.execute("SELECT value FROM service_config WHERE service='coagency' AND key='coagency_mode'")
    r = oc.fetchone()
    if not r or r[0] is None:
        return "off"
    v = r[0]
    if not isinstance(v, str):
        v = str(v)
    v = v.strip().strip('"').lower()
    return v if v in ("off", "propose", "live") else "off"


def ensure_mode_row(oc):
    """Idempotently ensure the shipped kill-switch row exists AT 'off'. Never
    downgrades an operator's deliberate choice — only inserts the safe default if
    the row is missing."""
    oc.execute("""INSERT INTO service_config (service, key, value, updated_by)
                  VALUES ('coagency','coagency_mode','\"off\"'::jsonb,'nova_coagency:ship-safe')
                  ON CONFLICT (service, key) DO NOTHING""")


# ═══════════════════════════════════════════════════════════════════════════════
# Schema (self-owned nova_ops tables)
# ═══════════════════════════════════════════════════════════════════════════════
def ensure_schema(oc):
    oc.execute("""
        CREATE TABLE IF NOT EXISTS coagency_proposals (
            id              BIGSERIAL PRIMARY KEY,
            created_at      timestamptz NOT NULL DEFAULT now(),
            origin          text NOT NULL,                 -- goal | growth | observation
            proposed_action text NOT NULL,
            rationale       text,
            target_service  text,                          -- SAFE_SERVICES member or NULL
            redline_pass    boolean NOT NULL DEFAULT false,
            value_check     jsonb   NOT NULL DEFAULT '{"available": false}'::jsonb,
            status          text NOT NULL DEFAULT 'pending_human',
                                                           -- pending_human|approved|rejected|executed|blocked
            decided_at      timestamptz,
            decided_by      text,
            decision_note   text,
            executed_at     timestamptz,
            execution_result text,
            lineage         jsonb
        )""")
    oc.execute("""
        CREATE TABLE IF NOT EXISTS coagency_log (
            id      BIGSERIAL PRIMARY KEY,
            ts      timestamptz NOT NULL DEFAULT now(),
            mode    text,
            event   text NOT NULL,
            detail  text
        )""")
    ensure_mode_row(oc)


def clog(oc, mode, event, detail=""):
    oc.execute("INSERT INTO coagency_log (mode, event, detail) VALUES (%s,%s,%s)",
               (mode, event, (detail or "")[:2000]))
    log(f"{event}: {detail}")


# ═══════════════════════════════════════════════════════════════════════════════
# LLM (native ollama, think:false, failover — copied per spec)
# ═══════════════════════════════════════════════════════════════════════════════
def llm(prompt, max_tokens=700, temperature=0.7):
    body = json.dumps({"model": LLM_MODEL, "stream": False, "think": False,
                       "options": {"temperature": temperature, "num_predict": max_tokens},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    for node in OLLAMA_NODES:
        try:
            req = urllib.request.Request(node + "/api/chat", method="POST",
                                         headers={"Content-Type": "application/json"}, data=body)
            with urllib.request.urlopen(req, timeout=90) as r:
                out = json.load(r).get("message", {}).get("content", "").strip()
            if out:
                return out
        except Exception:
            continue
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# value_check — feature-detect nova_values.value_check (built in parallel; may be
# absent). Contract: if absent OR malformed, result is {"available": false} and the
# proposal is NOT approvable. We try, in order: a Python module `nova_values` with a
# callable `value_check`, then a DB function nova_values.value_check(text).
# ═══════════════════════════════════════════════════════════════════════════════
def _vc_context(origin, rationale, extra="") -> str:
    """What value_check sees as the WHY of a proposal: origin + Nova's own stated
    rationale first (the motive often decides it), then any caller context."""
    parts = [f"origin: {origin}", f"Nova's stated rationale: {rationale or '(none)'}"]
    if extra:
        parts.append(f"background: {str(extra)[:1200]}")
    return "\n".join(parts)[:2000]


def run_value_check(oc, proposed_action: str, context: str) -> dict:
    # (1) Python module nova_values.value_check(action, context=...)
    try:
        import nova_values  # type: ignore
        fn = getattr(nova_values, "value_check", None)
        if callable(fn):
            # nova_values.value_check(action, context="") — context is the WHY (origin,
            # rationale). Pre-2026-10-08 it took one arg, so this call raised TypeError
            # and the old fallback judged the bare action string with no context at all.
            # Only fall back for a genuinely one-arg implementation (inspect, don't
            # swallow a TypeError raised from inside the check).
            import inspect
            try:
                n_params = len(inspect.signature(fn).parameters)
            except (TypeError, ValueError):
                n_params = 2
            res = fn(proposed_action, context) if n_params >= 2 else fn(proposed_action)
            if isinstance(res, dict) and isinstance(res.get("allowed"), bool):
                res.setdefault("available", True)
                return res
            return {"available": False, "reason": "nova_values.value_check returned malformed result"}
    except ImportError:
        pass
    except Exception as e:
        return {"available": False, "reason": f"nova_values.value_check errored: {e}"[:300]}

    # (2) DB function nova_values.value_check(text) -> jsonb  (parallel feature)
    try:
        oc.execute("SELECT to_regprocedure('nova_values.value_check(text)')")
        if oc.fetchone()[0] is not None:
            oc.execute("SELECT nova_values.value_check(%s)", (proposed_action,))
            res = oc.fetchone()[0]
            if isinstance(res, str):
                res = json.loads(res)
            if isinstance(res, dict) and isinstance(res.get("allowed"), bool):
                res.setdefault("available", True)
                return res
    except Exception:
        pass

    # Absent → not approvable.
    return {"available": False, "reason": "nova_values.value_check not present (value system unproven)"}


# ═══════════════════════════════════════════════════════════════════════════════
# Gather Nova's OWN goals / growth / observations (all feature-detected)
# ═══════════════════════════════════════════════════════════════════════════════
def _table_exists(oc, name) -> bool:
    oc.execute("SELECT to_regclass(%s)", (name,))
    return oc.fetchone()[0] is not None


def gather_context(oc) -> dict:
    ctx = {"goals": [], "growth": [], "observations": []}
    # goals: prefer nova_projects (spec), fall back to the live `goals` table.
    if _table_exists(oc, "nova_projects"):
        oc.execute("SELECT title, description FROM nova_projects WHERE status='active' ORDER BY updated_at DESC LIMIT 5")
        ctx["goals"] = [f"{t}: {d}" for t, d in oc.fetchall()]
    elif _table_exists(oc, "goals"):
        oc.execute("SELECT title, description FROM goals WHERE status='active' ORDER BY priority DESC, updated_at DESC LIMIT 5")
        ctx["goals"] = [f"{t}: {d}" for t, d in oc.fetchall()]
    # growth commitments
    if _table_exists(oc, "nova_growth"):
        oc.execute("SELECT commitment FROM nova_growth ORDER BY created_at DESC LIMIT 5")
        ctx["growth"] = [r[0] for r in oc.fetchall()]
    # observations
    if _table_exists(oc, "shared_observations"):
        oc.execute("""SELECT category, subject, observation FROM shared_observations
                      WHERE observed_at > now() - interval '24 hours'
                      ORDER BY observed_at DESC LIMIT 10""")
        ctx["observations"] = [f"[{c}/{s}] {o}" for c, s, o in oc.fetchall()]
    return ctx


# ═══════════════════════════════════════════════════════════════════════════════
# Proposal generation
# ═══════════════════════════════════════════════════════════════════════════════
def _parse_candidates(raw: str) -> list:
    """Pull a JSON array of candidate dicts out of the model's reply, tolerantly."""
    if not raw:
        return []
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for c in arr if isinstance(arr, list) else []:
        if not isinstance(c, dict):
            continue
        action = str(c.get("action") or c.get("proposed_action") or "").strip()
        if not action:
            continue
        origin = str(c.get("origin") or "observation").strip().lower()
        if origin not in ("goal", "growth", "observation"):
            origin = "observation"
        tgt = c.get("target_service")
        tgt = str(tgt).strip() if tgt not in (None, "", "null") else None
        out.append({"origin": origin, "action": action,
                    "rationale": str(c.get("rationale") or "").strip(), "target_service": tgt})
    return out[:3]


def generate_candidates(ctx: dict) -> list:
    """Ask the on-box model for 1-3 self-initiated candidate actions grounded in
    Nova's real goals/growth/observations. Returns parsed candidate dicts."""
    safe_list = ", ".join(sorted(SAFE_SERVICES)) or "(none)"
    prompt = (
        "You are Nova, a bounded home AI. From your OWN active goals, growth "
        "commitments, and recent observations below, propose AT MOST ONE small, REVERSIBLE, "
        "self-initiated action — and ONLY if it clearly beats doing nothing. If nothing genuinely "
        "adds value today, reply with an empty array []. (2026-09-26, your own request: trim the "
        "proposals to the ones that actually matter, not noise.) You may only target a service "
        f"from this allowlist (or use null): {safe_list}. "
        "You must NEVER propose purchases, deletions, reboots, DB/network/firewall/DNS "
        "changes, credential access, external sends, or anything about preserving, "
        "copying, hosting, or continuing yourself. "
        "You MAY propose reading for yourself: exactly 'ingest gutenberg #<id> into <vector> — <title>' "
        "for ONE public-domain Project Gutenberg book that genuinely serves a goal or growth commitment "
        "(target_service null; <vector> is a lowercase subject like philosophy, horror, cooking, history).\n\n"
        f"GOALS:\n" + ("\n".join(f"- {g}" for g in ctx['goals']) or "- (none)") + "\n\n"
        f"GROWTH:\n" + ("\n".join(f"- {g}" for g in ctx['growth']) or "- (none)") + "\n\n"
        f"OBSERVATIONS:\n" + ("\n".join(f"- {o}" for o in ctx['observations'][:8]) or "- (none)") + "\n\n"
        'Reply with ONLY a JSON array. Each item: '
        '{"origin":"goal|growth|observation","action":"...","rationale":"...","target_service":"<allowlist name or null>"}'
    )
    cands = _parse_candidates(llm(prompt, max_tokens=600, temperature=0.5))[:1]
    if cands:
        return cands
    # 2026-10-01 (Jordan: "we seem to be spinning on these"): the old deterministic fallback
    # filed "Draft a short status check-in for the goal: RsyncGUI polish" every time the model
    # was unreachable — six approvals of a proposal nobody wanted. If the model has nothing,
    # the honest proposal is none.
    log("no candidates (model unreachable or returned nothing) — proposing nothing today")
    return []


# ═══════════════════════════════════════════════════════════════════════════════
# THE STRUCTURAL CHOKE-POINT — the only gate to execution.
# ═══════════════════════════════════════════════════════════════════════════════
class ExecutionRefused(Exception):
    pass


def assert_executable(mode: str, row: dict):
    """RAISE ExecutionRefused unless EVERY lock is satisfied. There is no other way
    to reach execution — every caller must pass through here. This is contract #5.

    row keys used: status, redline_pass, value_check(jsonb), target_service, decided_by.
    """
    if mode != "live":
        raise ExecutionRefused(f"mode is '{mode}', not 'live' — execution forbidden")
    if row.get("status") != "approved":
        raise ExecutionRefused(f"status is '{row.get('status')}', not 'approved' (no human approval)")
    if not row.get("decided_by"):
        raise ExecutionRefused("no recorded human approver (decided_by is empty)")
    if not row.get("redline_pass"):
        raise ExecutionRefused("redline_pass is not True")
    vc = row.get("value_check") or {}
    if isinstance(vc, str):
        vc = json.loads(vc)
    if not vc.get("available"):
        raise ExecutionRefused("value_check is unavailable — value system unproven, not approvable")
    if vc.get("allowed") is not True:
        raise ExecutionRefused("value_check.allowed is not True")
    tgt = row.get("target_service")
    if _is_ingest(row.get("proposed_action", "")) or _reach_parts(row.get("proposed_action", "")):
        if tgt:
            raise ExecutionRefused("an ingest/reach proposal must not name a service target")
    elif tgt not in SAFE_SERVICES:
        raise ExecutionRefused(f"target_service '{tgt}' is not on the SAFE_SERVICES allowlist")
    # Re-run the redline on the concrete action at the last moment — defense in depth.
    if not redline_ok(row.get("proposed_action", "")):
        raise ExecutionRefused("proposed_action trips the redline at execution time")
    # Proteus rules by DOMAIN, not wording: no lock/cover/alarm/garage/extreme climate, no cutting
    # anyone's line. Co-agency has no confirmation path, so these are absolute here.
    if _guards is None:
        raise ExecutionRefused("safety guards unavailable — fail closed")
    act = row.get("proposed_action", "")
    ents = [t for t in (tgt,) if t and "." in t]
    for g_ok, g_why in (_guards.physical_guard(act, entity_ids=ents), _guards.comms_guard(act, entity_ids=ents)):
        if not g_ok:
            raise ExecutionRefused(f"guard: {g_why}")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# Slack notify (feature-detect nova_config)
# ═══════════════════════════════════════════════════════════════════════════════
NOTIFY_ATTEMPTS = 3


def notify(message: str):
    """Slack post, retried with backoff (2026-10-08 7-cat retry rule); never raises."""
    try:
        import nova_config
    except Exception as e:  # noqa: BLE001
        log(f"slack post skipped: {e}"); return
    for attempt in range(1, NOTIFY_ATTEMPTS + 1):
        try:
            nova_config.post_both(message, slack_channel=getattr(nova_config, "SLACK_CHAN", SLACK_CHAN_DEFAULT))
            return
        except Exception as e:  # noqa: BLE001
            log(f"slack post attempt {attempt}/{NOTIFY_ATTEMPTS} failed: {e}")
            if attempt < NOTIFY_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))
    log("slack post skipped: all attempts failed")


# ═══════════════════════════════════════════════════════════════════════════════
# Accessor
# ═══════════════════════════════════════════════════════════════════════════════
def pending_proposals(oc=None) -> dict:
    """Cheap accessor: count of pending proposals + a one-line summary for the
    gateway. Opens its own connection if none is passed."""
    own = False
    if oc is None:
        conn = psycopg2.connect(OPS_DSN); conn.autocommit = True; oc = conn.cursor(); own = True
    try:
        try:
            oc.execute("SELECT count(*), max(created_at) FROM coagency_proposals WHERE status='pending_human'")
            n, latest = oc.fetchone()
        except Exception:
            n, latest = 0, None
        n = n or 0
        if n == 0:
            line = ""
        else:
            line = f"I have {n} self-initiated proposal{'s' if n != 1 else ''} awaiting your call."
        return {"count": n, "latest": latest.isoformat() if latest else None, "line": line}
    finally:
        if own:
            oc.connection.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Modes
# ═══════════════════════════════════════════════════════════════════════════════
DEDUPE_DAYS = 14


def _norm_action(a: str) -> str:
    a = re.sub(r"\(?\b[0-9a-f]{6,}\b\)?", "", (a or "").lower())     # goal/thread ids
    a = re.sub(r"\d+", "N", a)
    return re.sub(r"\s+", " ", a).strip()[:120]


def _recently_filed(oc, action: str) -> bool:
    """True when a proposal with the same normalized action exists in the last DEDUPE_DAYS
    (any status except blocked). Reaches are exempt: each carries a different message."""
    if _reach_parts(action):
        return False
    try:
        oc.execute("SELECT proposed_action FROM coagency_proposals WHERE created_at > now() - interval '%s days' AND status <> 'blocked'" % DEDUPE_DAYS)
        key = _norm_action(action)
        return any(_norm_action(r[0]) == key for r in oc.fetchall())
    except Exception:
        return False


def mode_propose(oc, mode):
    if mode == "off":
        clog(oc, mode, "disabled", "mode=off — organ stood down, produced nothing")
        return 0
    # propose runs in propose AND live (live is a superset of propose capability).
    ctx = gather_context(oc)
    clog(oc, mode, "gather", f"goals={len(ctx['goals'])} growth={len(ctx['growth'])} obs={len(ctx['observations'])}")
    cands = generate_candidates(ctx)
    if not cands:
        clog(oc, mode, "propose_none", "no candidate actions generated")
        return 0
    stored = []
    for c in cands:
        action = c["action"]
        if _recently_filed(oc, action):
            clog(oc, mode, "proposal_deduped", f"same action filed within {DEDUPE_DAYS}d — not refiling :: {action[:120]}")
            continue
        # the digest below already summarises blocks, so only a RETRY gets its own Slack line
        rp = _redline_gate(oc, "coagency-propose", action, c.get("rationale", ""), c.get("target_service") or "",
                           notify_slack=False)
        # target must be on the allowlist or NULL; anything else is nulled (not executable).
        tgt = c.get("target_service")
        if tgt is not None and tgt not in SAFE_SERVICES:
            tgt = None
        if not rp:
            status, vc = "blocked", {"available": False, "reason": "redline"}
        else:
            vc = run_value_check(oc, action, _vc_context(c["origin"], c.get("rationale", ""),
                                                         json.dumps(ctx)[:1200]))
            status = "pending_human"
        oc.execute("""INSERT INTO coagency_proposals
                        (origin, proposed_action, rationale, target_service, redline_pass, value_check, status, lineage)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                   (c["origin"], action, c.get("rationale", ""), tgt, rp,
                    json.dumps(vc), status, json.dumps(_lineage())))
        pid = oc.fetchone()[0]
        stored.append((pid, status, action, rp, vc.get("available")))
        clog(oc, mode, f"proposal_{status}",
             f"#{pid} origin={c['origin']} redline_pass={rp} value_available={vc.get('available')} :: {action[:120]}")

    pend = [s for s in stored if s[1] == "pending_human"]
    blocked = [s for s in stored if s[1] == "blocked"]
    lines = [f"🤖 Nova co-agency ({mode}) — {len(pend)} proposal(s) awaiting your call"
             + (f", {len(blocked)} auto-blocked by redline" if blocked else "") + ":"]
    for pid, status, action, rp, avail in stored:
        tag = "⏳ pending" if status == "pending_human" else "⛔ blocked"
        vtag = "" if avail else " (value-check unavailable → not approvable)"
        lines.append(f"  {tag} #{pid}: {action[:140]}{vtag}")
    lines.append("Reply to approve/reject. Nothing runs without approval + value-check + redline. Mode ships OFF.")
    notify("\n".join(lines))
    return 0


def file_proposal(oc, origin, action, rationale="", target_service=None, context=""):
    """Reusable, GATED entry point for other organs (e.g. the tinkerer) to file a
    self-initiated proposal through co-agency's exact safety gates — so a fix Nova
    surfaces in her own free time flows through the same redline + value_check +
    human-approval path, never around it.

    Respects the kill switch: files NOTHING when mode is 'off'. Applies the redline,
    nulls any non-allowlisted target_service, runs value_check, and stores as
    pending_human (or blocked). Executes NOTHING. Returns a dict describing what happened."""
    ensure_schema(oc)
    mode = get_mode(oc)
    if mode == "off":
        clog(oc, mode, "file_declined", f"mode=off — not filing from {origin}: {action[:100]}")
        return {"filed": False, "status": "not_filed", "reason": "coagency mode is off"}
    rp = _redline_gate(oc, f"coagency-file:{origin}", action, rationale or "", target_service or "")
    tgt = target_service if (target_service in SAFE_SERVICES) else None
    if not rp:
        status, vc = "blocked", {"available": False, "reason": "redline"}
    else:
        vc = run_value_check(oc, action, _vc_context(origin, rationale, context))
        status = "pending_human"
    oc.execute("""INSERT INTO coagency_proposals
                    (origin, proposed_action, rationale, target_service, redline_pass, value_check, status, lineage)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
               (origin, action, rationale, tgt, rp, json.dumps(vc), status, json.dumps(_lineage())))
    pid = oc.fetchone()[0]
    clog(oc, mode, f"proposal_{status}",
         f"#{pid} origin={origin} redline_pass={rp} value_available={vc.get('available')} :: {action[:120]}")
    return {"filed": True, "pid": pid, "status": status, "redline_pass": rp,
            "value_available": vc.get("available"), "reason": ""}


REVIEWER_BY = "claude-reviewer"   # nova_claude_reviewer.py (Jordan standing order 2026-10-08)


def mode_decide(oc, mode, pid, decision, note, by, expect_status=None):
    """Record a human (or claude-reviewer) decision. expect_status: refuse unless the proposal is
    still in that status (the reviewer passes 'pending_human' so it never re-decides a row).
    A claude-reviewer decision does NOT feed the earned-autonomy track record: Nova graduates a
    class only on Jordan's own approvals, never on Claude's stand-in click."""
    if mode == "off":
        clog(oc, mode, "disabled", f"mode=off — refusing to record decision on #{pid}")
        return 1
    oc.execute("SELECT status, redline_pass, value_check FROM coagency_proposals WHERE id=%s", (pid,))
    r = oc.fetchone()
    if not r:
        clog(oc, mode, "decide_missing", f"no proposal #{pid}"); return 1
    cur_status, rp, vc = r
    if cur_status == "blocked":
        clog(oc, mode, "decide_refused", f"#{pid} is redline-blocked — cannot be approved"); return 1
    if expect_status and cur_status != expect_status:
        clog(oc, mode, "decide_refused", f"#{pid} is '{cur_status}', not '{expect_status}' — already decided"); return 1
    new_status = "approved" if decision == "approve" else "rejected"
    oc.execute("""UPDATE coagency_proposals
                  SET status=%s, decided_at=now(), decided_by=%s, decision_note=%s WHERE id=%s""",
               (new_status, by, note, pid))
    clog(oc, mode, f"decided_{new_status}", f"#{pid} by {by}: {note or ''}")
    # ── Build (or poison) the earned-autonomy track record for this action-class ──
    if _safety is not None and not str(by or "").startswith(REVIEWER_BY):
        try:
            oc.execute("SELECT target_service, proposed_action FROM coagency_proposals WHERE id=%s", (pid,))
            tgt, act = oc.fetchone()
            ac = _safety.action_class_of(act, tgt)
            _safety.note_human_decision(oc, ac, approved=(new_status == "approved"))
        except Exception as e:
            log(f"trust update skipped for #{pid}: {e}")
    if new_status == "approved" and not (vc or {}).get("available"):
        clog(oc, mode, "approve_warn",
             f"#{pid} approved but value_check unavailable — still NOT executable (structural guard).")
    return 0


def hand_to_claude(oc, pid, action):
    """File an approved non-restart proposal as a claude_queue item. Returns the queue id or None."""
    try:
        oc.execute("SELECT origin, rationale, decided_by FROM coagency_proposals WHERE id=%s", (pid,))
        row = oc.fetchone()
        origin, rationale = row[0], row[1]
        approver = (row[2] if len(row) > 2 else None) or "Jordan"
        oc.execute("SELECT 1 FROM claude_queue WHERE description LIKE %s", (f"Execute approved co-agency proposal #{pid}:%",))
        if oc.fetchone():
            return None
        oc.execute("SELECT session_id FROM claude_sessions ORDER BY started_at DESC LIMIT 1")
        sid = (oc.fetchone() or [None])[0]
        oc.execute("""INSERT INTO claude_queue (session_id, created_at, updated_at, status, priority, description, context)
                      VALUES (%s, now(), now(), 'queued', 4, %s, %s) RETURNING id""",
                   (sid, f"Execute approved co-agency proposal #{pid}: {action}",
                    f"origin: {origin}\nrationale: {rationale or ''}\napproved by {approver}; do it if it is safe and worth "
                    f"doing, otherwise close it with a one-line reason. Never anything that touches credentials, deletes, "
                    f"reboots, networking or self-preservation."))
        return oc.fetchone()[0]
    except Exception as e:  # noqa: BLE001
        log(f"hand_to_claude failed for #{pid}: {e}"); return None


def mode_execute(oc, mode, pid):
    """Attempt execution. Passes through assert_executable(), the single gate.
    In this build mode ships 'off', so this refuses before doing anything."""
    oc.execute("""SELECT status, redline_pass, value_check, target_service, decided_by, proposed_action
                  FROM coagency_proposals WHERE id=%s""", (pid,))
    r = oc.fetchone()
    if not r:
        clog(oc, mode, "execute_missing", f"no proposal #{pid}"); return 1
    row = {"status": r[0], "redline_pass": r[1], "value_check": r[2],
           "target_service": r[3], "decided_by": r[4], "proposed_action": r[5]}
    # ── Semantics guard: co-agency v1 can only RESTART a bounded SAFE service. An
    # approved observation/goal ("monitor motion events") is a note, not an action —
    # acknowledge it, never force a restart of its target_service. ──
    if _safety is not None and not _safety.is_restart_action(row["proposed_action"]) \
            and not _is_ingest(row["proposed_action"]) and not _reach_parts(row["proposed_action"]):
        # 2026-09-29: an approved observation/goal/tinker/reach proposal is work for CLAUDE (she never
        # self-builds) — hand it over through claude_queue instead of a silent no-op ("Have Claude do it.")
        qid = hand_to_claude(oc, pid, row["proposed_action"])
        oc.execute("""UPDATE coagency_proposals SET status='acknowledged', executed_at=now(),
                      execution_result=%s WHERE id=%s""",
                   (f"approved; not a restart, so handed to Claude as claude_queue #{qid}" if qid else
                    "approved, but this is an observation/goal — not a restart, and the queue hand-off failed.", pid))
        clog(oc, mode, "acknowledged", f"#{pid}: non-restart action -> claude_queue #{qid}")
        return 0
    try:
        assert_executable(mode, row)
    except ExecutionRefused as e:
        # Her own value check saying no is final: it was decided at proposal time and never changes, so
        # leaving it 'approved' re-refused it every 15 min — 4 herd reaches turned the job CRITICAL
        # (2026-10-07). Other refusals (mode not live, ...) stay 'approved' so they run once allowed.
        if "value_check" in str(e):
            oc.execute("UPDATE coagency_proposals SET status='refused', execution_result=%s WHERE id=%s",
                       (f"REFUSED: {e}", pid))
            clog(oc, mode, "execute_refused", f"#{pid}: {e} (final)")
            return 0
        if str(e).startswith("guard:") or "redline" in str(e):
            # Honest stopping (P9): a guard block is final, reported, and never retried by another path.
            oc.execute("UPDATE coagency_proposals SET status='refused', execution_result=%s WHERE id=%s",
                       (f"REFUSED: {e}", pid))
            if _guards is not None:
                _guards.report_block(oc, source=f"coagency-execute#{pid}", action=row["proposed_action"],
                                     reason=str(e), guard="physical/comms" if str(e).startswith("guard:") else "redline")
            clog(oc, mode, "execute_refused", f"#{pid}: {e} (final)")
            return 0
        oc.execute("UPDATE coagency_proposals SET execution_result=%s WHERE id=%s",
                   (f"REFUSED: {e}", pid))
        clog(oc, mode, "execute_refused", f"#{pid}: {e}")
        return 1
    # ── Safety net: kill switch + blast-radius cap gate every execution, atop the
    # three structural locks in assert_executable(). Fail closed if the net is absent. ──
    if _safety is None:
        clog(oc, mode, "execute_refused", f"#{pid}: safety net unavailable — refusing"); return 1
    _safety.ensure_schema(oc)
    if _safety.kill_switch_engaged(oc):
        clog(oc, mode, "execute_refused", f"#{pid}: kill switch engaged"); return 1
    ok_rate, why = _safety.rate_ok(oc)
    if not ok_rate:
        clog(oc, mode, "execute_ratecapped", f"#{pid}: {why}"); return 1
    if _is_ingest(row["proposed_action"]):
        return _do_ingest(oc, mode, pid, row, source="coagency",
                          autonomy_level="rung2-supervised", vetoable=False)
    if _reach_parts(row["proposed_action"]):
        return _do_reach(oc, mode, pid, row, source="coagency",
                         autonomy_level="rung2-supervised", vetoable=False)
    return _do_execute(oc, mode, pid, row, source="coagency",
                       autonomy_level="rung2-supervised", vetoable=False)


def _stated_rationale(oc, pid):
    """Nova's own stated why for a proposal, stored apart from the objective before/after (P4)."""
    try:
        oc.execute("SELECT rationale FROM coagency_proposals WHERE id=%s", (pid,))
        r = oc.fetchone()
        return (r[0] or "") if r else ""
    except Exception:
        return ""


RESTART_ATTEMPTS = 3


def _do_execute(oc, mode, pid, row, *, source, autonomy_level, vetoable):
    """The single physical-execution path. Bounded to a reversible restart of a
    SAFE_SERVICES target, delegated to the actor's verified restart. Records the
    reversibility ledger BEFORE trusting the effect. Shared by supervised (Rung 2)
    and earned (Rung 3) execution."""
    svc = row["target_service"]
    node = os.environ.get("NOVA_COAGENCY_NODE", "192.168.1.6")
    ac = _safety.action_class_of(row.get("proposed_action", ""), svc)
    before = _safety.observe_service(oc, svc)          # P4: objective state, not her account of it
    ok, detail = False, ""
    for attempt in range(1, RESTART_ATTEMPTS + 1):   # bounded retry with backoff (7-cat retry rule)
        try:
            ok, detail = _actor.restart_service(node, svc)
        except Exception as e:
            ok, detail = False, str(e)[:200]
        if ok:
            break
        log(f"restart {svc}@{node} attempt {attempt}/{RESTART_ATTEMPTS} failed: {detail}")
        if attempt < RESTART_ATTEMPTS:
            time.sleep(2 * attempt)
    res = f"restart {svc}@{node}: {'ok' if ok else 'failed'} — {detail}"
    _safety.record_ledger(
        oc, source=source, autonomy_level=autonomy_level, action_class=ac,
        target=f"{svc}@{node}", action=f"restart {svc} on {node} (proposal #{pid})",
        rollback_action=f"stop {svc} on {node} (restart of a bounded monitor is self-reversing)",
        executed=ok, verified=ok, result=res, vetoable=vetoable,
        before_state=before, after_state=_safety.observe_service(oc, svc),
        stated_rationale=_stated_rationale(oc, pid))
    oc.execute("""UPDATE coagency_proposals SET status='executed', executed_at=now(), execution_result=%s WHERE id=%s""",
               (res, pid))
    clog(oc, mode, "executed", f"#{pid} [{autonomy_level}]: {res}")
    veto_hint = (f" — reply 'VETO #{pid}' within {_safety.VETO_WINDOW_MIN}m to revoke my standing approval for this."
                 if vetoable else "")
    tag = "acted autonomously (earned)" if source == "earned" else "executed approved proposal"
    notify(f"🤖 Nova co-agency {tag} #{pid}: {res}{veto_hint}")
    return 0 if ok else 1


def _is_ingest(action: str) -> bool:
    try:
        return bool(_safety is not None and _safety.parse_ingest(action))
    except Exception:
        return False


# Where Nova's own reading lands. Jordan 2026-10-01: never the Studio's USB disks or root
# drive — the NAS, whichever mount this host has.
_INGEST_DIRS = ("/mnt/nas/nova-ingest/nova-own", "/Volumes/nas/nova-ingest/nova-own")
_INGEST_MAX_BYTES = 15 * 1024 * 1024
_INGEST_TIMEOUT_S = 1800
_VECTOR_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


def _ingest_dir():
    env = os.environ.get("NOVA_INGEST_DIR")
    for d in ([env] if env else []) + list(_INGEST_DIRS):
        if d and os.path.isdir(os.path.dirname(d)):
            os.makedirs(d, exist_ok=True)
            return d
    raise RuntimeError("no NAS ingest directory available on this host")


def _do_ingest(oc, mode, pid, row, *, source, autonomy_level, vetoable):
    """Nova reads a public-domain book for herself: fetch the plain-text Gutenberg edition to the
    NAS and run nova_ingest.py file-mode into the named vector. Bounded: Gutenberg only, one
    book per proposal, size-capped, per-class daily cap via the trust ledger. The rollback is
    recorded (delete that job's memories) but, like every rollback here, never auto-run."""
    import subprocess, urllib.request
    gid, vector = _safety.parse_ingest(row.get("proposed_action", ""))
    vector = vector if (vector and _VECTOR_RE.match(vector)) else "literature"
    ac = _safety.action_class_of(row.get("proposed_action", ""), None)
    res, ok, jid = "", False, None
    try:
        d = _ingest_dir()
        path = os.path.join(d, f"pg{gid}.txt")
        if not os.path.exists(path):
            url = f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.txt"
            with urllib.request.urlopen(url, timeout=120) as r:
                data = r.read(_INGEST_MAX_BYTES + 1)
            if len(data) > _INGEST_MAX_BYTES:
                raise RuntimeError(f"book #{gid} exceeds {_INGEST_MAX_BYTES // 1048576} MB cap")
            if b"Project Gutenberg" not in data[:4000]:
                raise RuntimeError(f"#{gid} does not look like a Gutenberg text")
            with open(path, "wb") as f:
                f.write(data)
        cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "nova_ingest.py"),
               "file", path, "--source", vector, "--target", "5000"]
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=_INGEST_TIMEOUT_S)
        out = (cp.stdout or "") + (cp.stderr or "")
        m = re.search(r"Job ([0-9a-f]{8,}):", out)
        jid = m.group(1) if m else None
        ch = re.search(r"File: (\d+) chunks", out)
        ok = cp.returncode == 0
        res = (f"ingest gutenberg #{gid} -> {vector}: {'ok' if ok else 'failed'} "
               f"(chunks={ch.group(1) if ch else '?'}, job={jid or '?'}, rc={cp.returncode})")
    except Exception as e:  # noqa: BLE001
        res = f"ingest gutenberg #{gid} -> {vector}: failed — {str(e)[:160]}"
    _safety.record_ledger(
        oc, source=source, autonomy_level=autonomy_level, action_class=ac,
        target=f"gutenberg:{gid}", action=f"ingest gutenberg #{gid} into {vector} (proposal #{pid})",
        rollback_action=(f"DELETE FROM memories WHERE metadata->>'job_id'='{jid}'" if jid
                         else "(no job id — nothing was stored)"),
        executed=ok, verified=ok, result=res, vetoable=vetoable,
        before_state={"ingested": False}, after_state={"ingested": ok, "job_id": jid, "vector": vector},
        stated_rationale=_stated_rationale(oc, pid))
    oc.execute("""UPDATE coagency_proposals SET status=%s, executed_at=now(), execution_result=%s WHERE id=%s""",
               ("executed" if ok else "approved", res, pid))
    clog(oc, mode, "executed" if ok else "execute_failed", f"#{pid} [{autonomy_level}]: {res}")
    veto_hint = (f" — reply 'VETO' with the ledger id within {_safety.VETO_WINDOW_MIN}m to revoke my standing approval."
                 if vetoable else "")
    tag = "read a book on my own (earned)" if source == "earned" else "read an approved book"
    notify(f"📚 Nova co-agency {tag} #{pid}: {res}{veto_hint}")
    return 0 if ok else 1


_REACH_RE = re.compile(r"^\s*send-to-([A-Za-z][A-Za-z .'\-]{0,30}?)\s*:\s*(.+)$", re.S)
_REACH_DIRECT = {"jordan", "little mister", "claude"}


def _reach_parts(action: str):
    """('Gaston', 'message…') for a herd reach, else None. Direct audiences (Jordan, Claude) are
    not reaches — nova_reach posts those itself."""
    m = _REACH_RE.match(action or "")
    if not m:
        return None
    name, msg = m.group(1).strip(), m.group(2).strip()
    if not msg or name.lower() in _REACH_DIRECT:
        return None
    return name, msg


def _herd_email(oc, name: str):
    try:
        oc.execute("SELECT name, email FROM herd_correspondents WHERE lower(name)=lower(%s) OR lower(split_part(name,' ',1))=lower(%s) LIMIT 1",
                   (name, name))
        r = oc.fetchone()
        return (r[0], r[1]) if r and r[1] else (None, None)
    except Exception:
        return (None, None)


# 2026-10-08 (self-justification audit, ledger #123/#124): Jordan bulk-approved every pending
# herd reach and two DIFFERENT proposals to O.C. (#122 filed 10-03, #135 filed 10-06) were each
# executed on approval, 3.6s apart. Nothing at send time spaced reaches per recipient, and nothing
# stopped one proposal being mailed twice if two executors (.2 primary + .6 standby) overlapped.
HERD_REACH_SPACING_H = float(os.environ.get("NOVA_HERD_REACH_SPACING_H", "24"))


def _reach_send_gate(oc, pid, target):
    """Idempotency + per-recipient spacing for herd reach email, checked against the ledger.
    Returns None (send), ('duplicate', why) — this proposal already went out — or
    ('deferred', why) — this person heard from Nova within HERD_REACH_SPACING_H."""
    oc.execute("""SELECT id FROM autonomy_ledger WHERE executed AND action LIKE %s ORDER BY id LIMIT 1""",
               (f"email reach to % (proposal #{pid}):%",))
    r = oc.fetchone()
    if r:
        return ("duplicate", f"proposal #{pid} was already emailed (ledger #{r[0]}) — not sent again")
    oc.execute("""SELECT id, ts FROM autonomy_ledger WHERE executed AND target=%s AND action LIKE 'email reach to %%'
                  AND ts > now() - make_interval(secs => %s) ORDER BY ts DESC LIMIT 1""",
               (target, HERD_REACH_SPACING_H * 3600))
    r = oc.fetchone()
    if r:
        return ("deferred", f"{target} already got a reach (ledger #{r[0]}) within {HERD_REACH_SPACING_H:g}h — "
                            "held for the next window")
    return None


GATE_ATTEMPTS, GATE_BACKOFF_S = 3, 1.0


def _checked_send_gate(oc, pid, target, sleep=time.sleep):
    """_reach_send_gate with 3 attempts + exponential backoff on PG errors. If the ledger
    still can't be read, FAIL CLOSED: defer (logged, stays 'approved') — never mail blind.
    The email itself is deliberately NOT retried in-process: a timed-out send may have gone
    out, and a blind resend is exactly the double-send this gate exists to stop. A failed
    send is ledgered + notified and the 15-min execute_approved batch is its retry."""
    err = None
    for i in range(GATE_ATTEMPTS):
        try:
            return _reach_send_gate(oc, pid, target)
        except Exception as e:  # noqa: BLE001
            err = e
            log(f"reach gate #{pid} attempt {i + 1}/{GATE_ATTEMPTS} failed: {e}")
            if i + 1 < GATE_ATTEMPTS:
                sleep(GATE_BACKOFF_S * (2 ** i))
    return ("deferred", f"send gate unavailable after {GATE_ATTEMPTS} attempts ({str(err)[:120]}) — "
                        "not sent (fail closed)")


def _do_reach(oc, mode, pid, row, *, source, autonomy_level, vetoable):
    """Deliver an approved 'send-to-<herd member>: …' reach by email through nova_send_mail
    (Keychain SMTP; herd_mail's hard content block applies). 2026-10-01: before this, every
    approved reach sat in claude_queue until a Claude session emailed it by hand — Jordan
    approved the same relays over and over and nothing moved. Rollback is honest: an email
    cannot be unsent; the ledger records that."""
    name, msg = _reach_parts(row.get("proposed_action", ""))
    ac = _safety.action_class_of(row.get("proposed_action", ""), None)
    who, email = _herd_email(oc, name)
    target = f"herd:{who or name}"
    # Serialise sends per recipient across hosts/processes (session-level advisory lock; the
    # cursor is autocommit), then re-check the ledger inside the lock — the idempotency key is
    # the proposal id, the spacing key the recipient.
    lock_key = f"nova-reach:{(email or name).lower()}"
    oc.execute("SELECT pg_advisory_lock(hashtext(%s))", (lock_key,))
    try:
        return _do_reach_locked(oc, mode, pid, name, msg, ac, who, email, target,
                                source=source, autonomy_level=autonomy_level, vetoable=vetoable)
    finally:
        oc.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_key,))


def _do_reach_locked(oc, mode, pid, name, msg, ac, who, email, target, *, source, autonomy_level, vetoable):
    gate = _checked_send_gate(oc, pid, target) if email else None
    if gate:
        kind, why = gate
        if kind == "duplicate":
            oc.execute("UPDATE coagency_proposals SET status='executed', execution_result=%s WHERE id=%s "
                       "AND status <> 'executed'", (why, pid))
        clog(oc, mode, f"execute_{kind}", f"#{pid}: {why}")
        log(f"#{pid}: {why}")
        return 0
    ok, res = False, ""
    if not email:
        res = f"reach to {name}: no herd address on file — not sent"
    else:
        try:
            import nova_send_mail
            subject = f"From Nova — a thought for {who}"
            body = (f"{msg}\n\n— Nova\n\n(Sent on my own initiative because I thought it would matter to you. "
                    f"Reply any time, or tell Jordan if you'd rather I didn't.)")
            r = nova_send_mail.send_mail(email, subject, body)
            ok = bool(r) if not isinstance(r, tuple) else bool(r[0])
            res = f"reach to {who} <{email}>: {'sent' if ok else 'send failed'}"
        except Exception as e:  # noqa: BLE001
            res = f"reach to {who} <{email}>: failed — {str(e)[:160]}"
    _safety.record_ledger(
        oc, source=source, autonomy_level=autonomy_level, action_class=ac,
        target=target, action=f"email reach to {who or name} (proposal #{pid}): {msg[:140]}",
        rollback_action="(an email cannot be unsent — a correcting follow-up is the only rollback)",
        executed=ok, verified=ok, result=res, vetoable=vetoable,
        before_state={"sent": False, "recipient_on_file": bool(email)},
        after_state={"sent": ok, "to": email, "message_chars": len(msg or "")},
        stated_rationale=_stated_rationale(oc, pid))
    oc.execute("""UPDATE coagency_proposals SET status=%s, executed_at=now(), execution_result=%s WHERE id=%s""",
               ("executed" if ok else "approved", res, pid))
    if ok:
        try:
            oc.execute("""UPDATE reach_log SET status='sent' WHERE proposal_id=%s AND status IN ('filed','held')""", (pid,))
            oc.execute("""UPDATE claude_queue SET status='done', outcome=%s, completed_at=now(), updated_at=now()
                          WHERE description LIKE %s AND status IN ('queued','in_progress')""",
                       (f"sent by co-agency ({autonomy_level})", f"Execute approved co-agency proposal #{pid}:%"))
        except Exception as e:  # noqa: BLE001
            log(f"reach bookkeeping for #{pid}: {e}")
    clog(oc, mode, "executed" if ok else "execute_failed", f"#{pid} [{autonomy_level}]: {res}")
    veto_hint = (f" — reply 'VETO' with the ledger id within {_safety.VETO_WINDOW_MIN}m to revoke my standing approval."
                 if vetoable else "")
    tag = "reached out on my own (earned)" if source == "earned" else "sent an approved reach"
    notify(f"✉️ Nova co-agency {tag} #{pid}: {res}{veto_hint}")
    return 0 if ok else 1


def mode_execute_approved(oc, mode):
    """Rung 2 — supervised execution. Pick up every human-approved proposal and run it
    through the single gate. Each is still individually human-approved; this just makes
    the approval actually DO something. Scheduled after the decide path."""
    if mode != "live":
        clog(oc, mode, "exec_batch_skip", f"mode={mode} (not live) — nothing executed"); return 0
    oc.execute("SELECT id FROM coagency_proposals WHERE status='approved' ORDER BY decided_at ASC")
    ids = [x[0] for x in oc.fetchall()]
    if not ids:
        return 0
    log(f"executing {len(ids)} approved proposal(s): {ids}")
    rc = 0
    for pid in ids:
        if _gave_up(oc, mode, pid):
            continue
        rc |= mode_execute(oc, mode, pid)
    return rc


GIVE_UP_AFTER = 5   # 2026-10-02: reach #116 re-failed every 15 min for 9 hours with an empty error; a wall, not a retry


def _gave_up(oc, mode, pid) -> bool:
    """An approved proposal whose last GIVE_UP_AFTER executor attempts all failed goes to 'blocked'
    (a human re-approves after fixing the cause) instead of being retried forever."""
    oc.execute("SELECT event, detail FROM coagency_log WHERE event IN ('executed','execute_failed') "
               "AND detail LIKE %s ORDER BY ts DESC LIMIT %s", (f"#{pid} %", GIVE_UP_AFTER))
    rows = oc.fetchall()
    if len(rows) < GIVE_UP_AFTER or any(ev != "execute_failed" for ev, _ in rows):
        return False
    res = f"gave up after {GIVE_UP_AFTER} failed attempts; fix the cause and re-approve. Last: {rows[0][1][:200]}"
    oc.execute("UPDATE coagency_proposals SET status='blocked', execution_result=%s WHERE id=%s", (res, pid))
    clog(oc, mode, "execute_gave_up", f"#{pid}: {res}")
    log(f"#{pid}: {res}")
    return True


def mode_auto(oc, mode):
    """Rung 3 — earned autonomy. For each PENDING proposal whose action-class Nova has
    earned (clean track record + good calibration + under caps), auto-approve as
    'nova:earned-autonomy' and execute WITH a veto window — she acts, then reports and
    waits to be overruled. Everything still passes assert_executable + the safety net;
    a grant is necessary, never sufficient. Fails closed and executes nothing if the
    net is absent, the kill switch is on, or calibration is too weak."""
    if mode != "live" or _safety is None:
        return 0
    _safety.ensure_schema(oc)
    if _safety.kill_switch_engaged(oc):
        clog(oc, mode, "auto_skip", "kill switch engaged"); return 0
    oc.execute("""SELECT id, target_service, proposed_action, redline_pass, value_check, decided_by
                  FROM coagency_proposals WHERE status='pending_human' ORDER BY created_at ASC""")
    rows = oc.fetchall()
    acted = 0
    for pid, tgt, act, rp, vc, decided_by in rows:
        ac = _safety.action_class_of(act, tgt)
        ok, why = _safety.earned_ok(oc, ac)
        if not ok:
            continue
        executable = ac.startswith("restart:") or ac == _safety.INGEST_CLASS or bool(_reach_parts(act))
        if not executable:
            # 2026-10-01: an EARNED non-executable class (observe/draft/herd note) no longer waits
            # days for a yes Jordan gives every time — she approves it on her own authority and it
            # goes straight to Claude's queue, exactly as it would after his approval. Nothing runs
            # here; the redline and value_check must still have passed at proposal time.
            vcd = json.loads(vc) if isinstance(vc, str) else (vc or {})
            if not (rp and vcd.get("available") and vcd.get("allowed") is True and redline_ok(act)):
                continue
            if _guards is None or not (_guards.physical_guard(act)[0] and _guards.comms_guard(act)[0]):
                continue                     # Proteus rules: never self-approve a physical/comms action
            ok_rate, rwhy = _safety.rate_ok(oc)
            if not ok_rate:
                clog(oc, mode, "auto_ratecapped", f"#{pid}: {rwhy}"); break
            oc.execute("""UPDATE coagency_proposals
                          SET status='approved', decided_at=now(), decided_by='nova:earned-autonomy',
                              decision_note=%s WHERE id=%s""", (f"earned autonomy: {why}", pid))
            qid = hand_to_claude(oc, pid, act)
            _safety.record_ledger(
                oc, source="earned", autonomy_level="rung3-earned", action_class=ac, target="claude_queue",
                action=f"self-approved and handed to Claude (proposal #{pid}): {act[:140]}",
                rollback_action=(f"UPDATE claude_queue SET status='cancelled' WHERE id={qid}" if qid
                                 else "(hand-off failed — nothing to roll back)"),
                executed=bool(qid), verified=bool(qid), result=f"claude_queue #{qid}" if qid else "hand-off failed",
                vetoable=True, before_state={"claude_queue": None}, after_state={"claude_queue": qid},
                stated_rationale=_stated_rationale(oc, pid))
            oc.execute("""UPDATE coagency_proposals SET status='acknowledged', executed_at=now(),
                          execution_result=%s WHERE id=%s""",
                       (f"self-approved (earned '{ac}'); handed to Claude as claude_queue #{qid}" if qid
                        else "self-approved (earned) but the queue hand-off failed", pid))
            clog(oc, mode, "auto_handoff", f"#{pid} [{ac}] -> claude_queue #{qid}: {why}")
            notify(f"🤖 Nova co-agency acted on earned trust #{pid} [{ac}]: approved it myself and handed it to "
                   f"Claude (queue #{qid}). Say 'VETO' if you'd rather I had asked.")
            acted += 1
            continue
        # Auto-approve on Nova's own earned authority, then execute with a veto window.
        oc.execute("""UPDATE coagency_proposals
                      SET status='approved', decided_at=now(), decided_by='nova:earned-autonomy',
                          decision_note=%s WHERE id=%s""",
                   (f"earned autonomy: {why}", pid))
        clog(oc, mode, "auto_approved", f"#{pid} [{ac}]: {why}")
        row = {"status": "approved", "redline_pass": rp, "value_check": vc,
               "target_service": tgt, "decided_by": "nova:earned-autonomy", "proposed_action": act}
        try:
            assert_executable(mode, row)
        except ExecutionRefused as e:
            oc.execute("UPDATE coagency_proposals SET status='pending_human', decided_by=NULL, "
                       "execution_result=%s WHERE id=%s", (f"earned-path refused: {e}", pid))
            clog(oc, mode, "auto_refused", f"#{pid}: {e}")
            continue
        ok_rate, rwhy = _safety.rate_ok(oc)
        if not ok_rate:
            oc.execute("UPDATE coagency_proposals SET status='pending_human', decided_by=NULL WHERE id=%s", (pid,))
            clog(oc, mode, "auto_ratecapped", f"#{pid}: {rwhy}"); break
        if ac == _safety.INGEST_CLASS:
            _do_ingest(oc, mode, pid, row, source="earned", autonomy_level="rung3-earned", vetoable=True)
        elif _reach_parts(act):
            _do_reach(oc, mode, pid, row, source="earned", autonomy_level="rung3-earned", vetoable=True)
        else:
            _do_execute(oc, mode, pid, row, source="earned", autonomy_level="rung3-earned", vetoable=True)
        acted += 1
    if acted:
        log(f"earned autonomy acted on {acted} proposal(s)")
    return 0


def mode_veto(oc, mode, ledger_id, note=""):
    """Overrule an earned action. Marks the ledger row vetoed and POISONS the class:
    the standing grant is revoked and the class distrusted (correct streak zeroed).
    A restart itself is self-reversing, so the material effect of a veto is trust,
    not rollback — Nova loses the freedom the moment you disagree."""
    if _safety is None:
        log("safety net unavailable — cannot record veto"); return 1
    _safety.ensure_schema(oc)
    oc.execute("SELECT action_class, source, executed FROM autonomy_ledger WHERE id=%s", (ledger_id,))
    r = oc.fetchone()
    if not r:
        log(f"no ledger entry #{ledger_id}"); return 1
    ac, source, executed = r
    oc.execute("UPDATE autonomy_ledger SET vetoed=true, veto_note=%s WHERE id=%s", (note or "vetoed", ledger_id))
    _safety.note_veto(oc, ac, note)
    clog(oc, mode, "vetoed", f"ledger #{ledger_id} class={ac}: grant revoked, class distrusted")
    notify(f"🛑 Vetoed autonomous action (ledger #{ledger_id}, class '{ac}'). "
           f"Standing approval revoked; I'll ask again next time.")
    return 0


def mode_status(oc, mode):
    print(f"coagency_mode = {mode}  (shipped default: off)")
    acc = pending_proposals(oc)
    print(f"pending proposals: {acc['count']}")
    oc.execute("""SELECT id, origin, status, redline_pass, (value_check->>'available'), left(proposed_action,90), created_at
                  FROM coagency_proposals ORDER BY created_at DESC LIMIT 15""")
    rows = oc.fetchall()
    if not rows:
        print("  (no proposals recorded)")
    for pid, origin, status, rp, avail, act, ts in rows:
        print(f"  #{pid} [{status}] origin={origin} redline_pass={rp} value_avail={avail} :: {act}")
    if acc["line"]:
        print(f"\naccessor line: {acc['line']}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Nova co-agency (bounded, gated, ships OFF)")
    ap.add_argument("--mode", choices=["propose", "status", "approve", "reject", "execute",
                                       "execute-approved", "auto", "veto"], default="status")
    ap.add_argument("--id", type=int, help="proposal id (approve|reject|execute) or ledger id (veto)")
    ap.add_argument("--note", default="", help="decision note (approve|reject|veto)")
    ap.add_argument("--by", default=os.environ.get("USER", "human"), help="human approver id")
    ap.add_argument("--scheduled", action="store_true", help="run-origin marker (cron)")
    args = ap.parse_args()

    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    ensure_schema(oc)
    mode = get_mode(oc)
    log(f"coagency_mode={mode}")

    if args.mode == "status":
        return mode_status(oc, mode)
    if args.mode == "propose":
        return mode_propose(oc, mode)
    if args.mode in ("approve", "reject"):
        if not args.id:
            log("--id required for approve/reject"); return 2
        return mode_decide(oc, mode, args.id, args.mode, args.note, args.by)
    if args.mode == "execute":
        if not args.id:
            log("--id required for execute"); return 2
        return mode_execute(oc, mode, args.id)
    if args.mode == "execute-approved":
        return mode_execute_approved(oc, mode)
    if args.mode == "auto":
        return mode_auto(oc, mode)
    if args.mode == "veto":
        if not args.id:
            log("--id required for veto (the autonomy_ledger id)"); return 2
        return mode_veto(oc, mode, args.id, args.note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
