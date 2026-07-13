#!/opt/homebrew/bin/python3
"""
nova_autonomy_executor.py — The action-gating layer for JARVIS autonomy (Queue #513).

JARVIS observes and suggests, but never acts through a single governed path —
nova_automation_engine.py calls Hue directly, bypassing the 13 autonomy_rules
that define what Nova may do on her own. This executor is that missing gate:

  proposed action  ->  look up autonomy_rules.level  ->  act per level
     level=auto     -> execute autonomously
     level=notify   -> execute + tell Jordan
     level=approve  -> queue for approval, do NOT execute

It runs in SHADOW mode first (MODE below): it evaluates every proposed action
and LOGS what it would do to anticipation_log (+ a periodic Slack digest), but
executes nothing. After Jordan reviews a few days of shadow output, flip MODE
to "live" and wire the execute_* handlers.

Proposals come from two places:
  1. Native presence/time triggers (goodnight, away-securing) off fused
     presence_state — the authoritative signal nova_presence_engine now writes.
  2. Actions the other engines RECORD in shared_observations — so the gate has
     visibility into what the system is already doing on its own.

Written by Jordan Koch (via Claude).
"""

import json
import signal
import sys
import time
import urllib.request
from datetime import datetime

import psycopg2

MODE = "shadow"  # "shadow" (log only, execute nothing) | "live" (gate + execute)

PG_DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"
POLL_INTERVAL = 60          # seconds between evaluation cycles
DIGEST_INTERVAL = 3600      # seconds between Slack shadow-digests
ACTION_COOLDOWN = 1800      # per-action de-dupe window

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_API = nova_config.SLACK_API
REPORT_CHANNEL = nova_config.SLACK_BB

_shutdown = False
_last_fired = {}            # action_key -> ts (cooldown)
_shadow_since_digest = []   # accumulates shadow decisions for the digest


def log(msg, level="INFO"):
    print(f"[autonomy {time.strftime('%H:%M:%S')}] [{level}] {msg}", flush=True)


def on_cooldown(key, window=ACTION_COOLDOWN):
    now = time.time()
    if now - _last_fired.get(key, 0) < window:
        return True
    _last_fired[key] = now
    return False


# ── Rule lookup ───────────────────────────────────────────────────────────────

def rule_level(conn, action_type):
    """Return the autonomy level for an action_type ('auto'|'notify'|'approve'),
    defaulting to 'approve' (most conservative) when no rule exists."""
    with conn.cursor() as cur:
        cur.execute("SELECT level FROM autonomy_rules WHERE action_type=%s AND channel='*' LIMIT 1",
                    (action_type,))
        row = cur.fetchone()
    return row[0] if row else "approve"


# ── Proposal generators ───────────────────────────────────────────────────────
# Each returns a list of proposed actions: {action_type, summary, reason, params}.

def propose_from_presence(conn):
    """Native triggers off fused presence_state."""
    proposals = []
    with conn.cursor() as cur:
        cur.execute("""SELECT person, room, confidence FROM presence_state
                       WHERE last_confirmed > now() - interval '5 minutes'""")
        rows = cur.fetchall()
    if not rows:
        return proposals
    home = [(p, r, c) for p, r, c in rows if r != "away"]
    hour = datetime.now().hour

    # Goodnight: late + someone in a bedroom
    if 22 <= hour <= 23:
        for person, room, conf in home:
            if room in ("bedroom", "master_bedroom") and conf > 0.4:
                proposals.append({
                    "action_type": "homekit_scene",
                    "summary": "set 'goodnight' scene",
                    "reason": f"{person} in {room} at {hour}:00 (conf {conf:.2f})",
                    "params": {"scene": "goodnight"},
                })

    # Secure-when-away: rows exist but nobody is home
    if rows and not home:
        proposals.append({
            "action_type": "homekit_scene",
            "summary": "set 'away' scene (lights off, secure)",
            "reason": "no occupants detected at home",
            "params": {"scene": "away"},
        })
    return proposals


def propose_from_engine_actions(conn):
    """Surface actions the other engines took/recorded so the gate has visibility.
    These already happened (automation_engine acts directly today) — logging them
    here shows what a governed path WOULD have decided."""
    proposals = []
    with conn.cursor() as cur:
        cur.execute("""SELECT subject, observation FROM shared_observations
                       WHERE observer='automation_engine'
                         AND observed_at > now() - interval '2 minutes'
                       ORDER BY observed_at DESC LIMIT 20""")
        for subject, obs in cur.fetchall():
            # automation_engine light/scene actions map to homekit_scene-level risk
            atype = "homekit_scene" if any(k in (subject or "").lower()
                                           for k in ("light", "scene", "climate")) else "send_message"
            proposals.append({
                "action_type": atype,
                "summary": f"engine action: {subject}",
                "reason": (obs or "")[:160],
                "params": {"origin": "automation_engine"},
            })
    return proposals


# ── Decision + shadow logging ───────────────────────────────────────────────────

def log_shadow(conn, action, level, decision):
    msg = f"[SHADOW] {decision}: {action['summary']} (type={action['action_type']}, level={level}) — {action['reason']}"
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO anticipation_log (ts, observation_type, message, activity_state, delivered, queued)
                       VALUES (now(), 'shadow_action', %s, %s, false, true)""",
                    (msg, None))
    conn.commit()
    _shadow_since_digest.append(msg)
    log(msg)


def evaluate(conn, action):
    key = f"{action['action_type']}:{action['summary']}"
    if on_cooldown(key):
        return
    level = rule_level(conn, action["action_type"])
    decision = {"auto": "would execute", "notify": "would execute + notify",
                "approve": "would queue for approval"}.get(level, "would queue")
    if MODE == "shadow":
        log_shadow(conn, action, level, decision)
    else:
        # LIVE mode: wire execute handlers here (Hue/HomeKit/scheduler) per level.
        # Intentionally not implemented until shadow review promotes this.
        log(f"LIVE mode not yet wired — skipping {action['summary']}", "WARN")


def post_digest():
    if not _shadow_since_digest or not SLACK_TOKEN:
        return
    n = len(_shadow_since_digest)
    body = ":crystal_ball: *JARVIS autonomy — SHADOW digest* (executing nothing yet)\n" + \
           f"_{n} decision(s) in the last hour:_\n" + \
           "\n".join(f"• {m.replace('[SHADOW] ', '')}" for m in _shadow_since_digest[-15:])
    payload = json.dumps({"channel": REPORT_CHANNEL, "text": body, "unfurl_links": False}).encode()
    req = urllib.request.Request(f"{SLACK_API}/chat.postMessage", data=payload,
                                 headers={"Authorization": f"Bearer {SLACK_TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"digest post failed: {e}", "WARN")
    _shadow_since_digest.clear()


def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    conn = psycopg2.connect(PG_DSN)
    log(f"Autonomy executor started in {MODE.upper()} mode (every {POLL_INTERVAL}s)")
    last_digest = time.time()
    while not _shutdown:
        try:
            proposals = propose_from_presence(conn) + propose_from_engine_actions(conn)
            for action in proposals:
                evaluate(conn, action)
            if time.time() - last_digest > DIGEST_INTERVAL:
                post_digest()
                last_digest = time.time()
        except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
            log(f"DB lost ({e}); reconnecting", "WARN")
            try:
                conn = psycopg2.connect(PG_DSN)
            except Exception:
                pass
        except Exception as e:
            log(f"cycle error: {e}", "WARN")
            try:
                conn.rollback()
            except Exception:
                pass
        time.sleep(POLL_INTERVAL)
    post_digest()
    conn.close()
    log("Shutdown complete.")


if __name__ == "__main__":
    main()
