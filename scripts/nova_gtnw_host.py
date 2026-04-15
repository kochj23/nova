#!/usr/bin/env python3
"""
nova_gtnw_host.py — GTNW multiplayer host for the Herd.

Runs email-based Global Thermal Nuclear War games where each herd member is
assigned a country/role. Nova acts as moderator: emails crisis scenarios,
collects decisions, feeds them to the GTNW app via its API, and posts updates
to Slack.

Usage:
  nova_gtnw_host.py start [--year 1962] [--scenario "Cuban Missile Crisis"]
  nova_gtnw_host.py status
  nova_gtnw_host.py advance
  nova_gtnw_host.py check          — check inbox for decision replies
  nova_gtnw_host.py reset          — wipe local state and end session

State file: ~/.openclaw/workspace/gtnw_multiplayer_state.json

Written by Jordan Koch.
"""

import json
import sys
import os
import re
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────

SCRIPTS     = Path.home() / ".openclaw" / "scripts"
WORKSPACE   = Path.home() / ".openclaw" / "workspace"
STATE_FILE  = WORKSPACE / "gtnw_multiplayer_state.json"
OLLAMA_URL  = "http://127.0.0.1:11434/api/generate"
GTNW_API    = "http://127.0.0.1:37431"   # GTNW NovaAPIServer (actual port)

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
from herd_config import HERD

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN   = nova_config.SLACK_NOTIFY          # C0ATAF7NZG9 #nova-notifications
SLACK_API   = nova_config.SLACK_API
NOVA_EMAIL  = nova_config.NOVA_EMAIL
HERD_MAIL   = str(SCRIPTS / "nova_herd_mail.sh")

TODAY = datetime.now().strftime("%Y-%m-%d")


def log(msg: str):
    print(f"[gtnw_host {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── GTNW API helpers ──────────────────────────────────────────────────────────

def gtnw_get(path: str, timeout: int = 10) -> dict | None:
    """GET from GTNW API. Returns parsed JSON or None on failure."""
    try:
        with urllib.request.urlopen(f"{GTNW_API}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"GTNW GET {path} failed: {e}")
        return None


def gtnw_post(path: str, body: dict, timeout: int = 30) -> dict | None:
    """POST to GTNW API. Returns parsed JSON or None on failure."""
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{GTNW_API}{path}", data=data,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"GTNW POST {path} failed: {e}")
        return None


def gtnw_available() -> bool:
    """True if the GTNW app API is reachable."""
    result = gtnw_get("/api/ping", timeout=3)
    return result is not None


# ── State management ──────────────────────────────────────────────────────────

def load_state() -> dict | None:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return None
    return None


def save_state(state: dict):
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def default_country_assignments() -> dict[str, dict]:
    """
    Map herd members to countries for a standard Cold War game.
    Returns: { countryID: {player, email} }
    """
    cold_war_roles = [
        ("USA",    "🇺🇸 United States"),
        ("USSR",   "🇷🇺 Soviet Union"),
        ("GBR",    "🇬🇧 United Kingdom"),
        ("FRA",    "🇫🇷 France"),
        ("CHN",    "🇨🇳 China"),
        ("CUB",    "🇨🇺 Cuba"),
    ]
    assignments = {}
    for i, member in enumerate(HERD):
        if i < len(cold_war_roles):
            country_id, country_label = cold_war_roles[i]
            assignments[country_id] = {
                "player": member["name"],
                "email": member["email"],
                "country_label": country_label
            }
    return assignments


# ── Email helpers ─────────────────────────────────────────────────────────────

def send_mail(to: str, subject: str, body: str, in_reply_to: str | None = None) -> bool:
    """Send email via nova_herd_mail.sh."""
    args = [HERD_MAIL, "send", "--to", to, "--subject", subject, "--body", body]
    if in_reply_to:
        args += ["--in-reply-to", in_reply_to]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log(f"Mail to {to} failed: {result.stderr.strip()}")
            return False
        return True
    except Exception as e:
        log(f"Mail exception: {e}")
        return False


def check_inbox() -> list[dict]:
    """Return unread messages from herd-mail inbox."""
    try:
        result = subprocess.run(
            [HERD_MAIL, "list", "--unread"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        return data.get("messages", [])
    except Exception as e:
        log(f"Inbox check failed: {e}")
        return []


def read_message(msg_id: str) -> dict | None:
    """Read a single message body."""
    try:
        result = subprocess.run(
            [HERD_MAIL, "read", "--id", msg_id],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except Exception:
        return None


# ── Slack ─────────────────────────────────────────────────────────────────────

def slack_post(text: str):
    """Post a message to #nova-chat."""
    if not SLACK_TOKEN:
        log("No Slack token — skipping Slack post")
        return
    try:
        data = json.dumps({
            "channel": SLACK_CHAN,
            "text": text,
            "mrkdwn": True
        }).encode()
        req = urllib.request.Request(
            f"{SLACK_API}/chat.postMessage", data=data,
            headers={
                "Authorization": f"Bearer {SLACK_TOKEN}",
                "Content-Type": "application/json; charset=utf-8"
            }
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        log(f"Slack error: {e}")


# ── Ollama crisis simulator (fallback when GTNW not running) ──────────────────

def generate_crisis_with_ollama(year: int, scenario: str, president: str,
                                 active_countries: list[str]) -> dict:
    """
    Generate a crisis scenario using local Ollama (nova:latest) when the GTNW
    app is not available. Returns a dict matching the GTNW crisisJSON shape.
    """
    countries_str = ", ".join(active_countries[:6]) if active_countries else "USA, USSR"

    prompt = f"""/no_think

You are WOPR generating a crisis for a Cold War strategy game.

YEAR: {year}
PRESIDENT: {president}
SCENARIO: {scenario}
COUNTRIES IN PLAY: {countries_str}

Generate a realistic geopolitical crisis. Return ONLY valid JSON:

{{
  "id": "sim-<random 8 chars>",
  "type": "Diplomatic Incident",
  "severity": "SERIOUS",
  "title": "<20-word dramatic title>",
  "description": "<100-word situation description>",
  "affectedCountries": ["USA", "USSR"],
  "options": [
    {{
      "index": 0,
      "title": "Negotiate",
      "description": "Pursue diplomatic back-channels",
      "successChance": 0.7,
      "consequences": {{"defconChange": 0, "approvalChange": 5, "triggersWar": false, "message": "Talks begin..."}}
    }},
    {{
      "index": 1,
      "title": "Escalate",
      "description": "Move forces to ready status",
      "successChance": 0.5,
      "consequences": {{"defconChange": -1, "approvalChange": -10, "triggersWar": false, "message": "DEFCON elevated..."}}
    }},
    {{
      "index": 2,
      "title": "Stand Down",
      "description": "Publicly de-escalate",
      "successChance": 0.9,
      "consequences": {{"defconChange": 1, "approvalChange": -15, "triggersWar": false, "message": "Seen as weakness..."}}
    }},
    {{
      "index": 3,
      "title": "Covert Response",
      "description": "CIA operation to address root cause",
      "successChance": 0.6,
      "consequences": {{"defconChange": 0, "approvalChange": 2, "triggersWar": false, "message": "Operation launched..."}}
    }}
  ]
}}"""

    try:
        payload = json.dumps({
            "model": "qwen3-coder:30b",
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {"temperature": 0.85, "num_predict": 500}
        }).encode()
        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
            raw = resp.get("response", "").strip()
            # Strip <think> blocks
            if "</think>" in raw:
                raw = raw.split("</think>", 1)[-1].strip()
            # Extract JSON
            json_match = re.search(r'\{[\s\S]*\}', raw)
            if json_match:
                return json.loads(json_match.group())
    except Exception as e:
        log(f"Ollama crisis generation failed: {e}")

    # Fallback hardcoded crisis
    return {
        "id": f"sim-{datetime.now().strftime('%H%M%S')}",
        "type": "Diplomatic Incident",
        "severity": "SERIOUS",
        "title": f"Escalating Tensions in {year}: {scenario}",
        "description": (
            f"Intelligence reports indicate a potential flashpoint has emerged. "
            f"The {president} administration must respond carefully. "
            "Advisors are divided on the appropriate course of action. "
            "The clock is ticking — your decision will shape history."
        ),
        "affectedCountries": ["USA", "USSR"],
        "options": [
            {"index": 0, "title": "Negotiate", "description": "Pursue diplomatic back-channels",
             "successChance": 0.7,
             "consequences": {"defconChange": 0, "approvalChange": 5, "triggersWar": False,
                               "message": "Diplomatic talks begin cautiously."}},
            {"index": 1, "title": "Escalate", "description": "Move forces to ready status",
             "successChance": 0.5,
             "consequences": {"defconChange": -1, "approvalChange": -10, "triggersWar": False,
                               "message": "DEFCON elevated. World watches nervously."}},
            {"index": 2, "title": "Stand Down", "description": "Publicly de-escalate",
             "successChance": 0.9,
             "consequences": {"defconChange": 1, "approvalChange": -15, "triggersWar": False,
                               "message": "Seen as weakness by adversaries."}},
            {"index": 3, "title": "Covert Response", "description": "Authorize CIA operation",
             "successChance": 0.6,
             "consequences": {"defconChange": 0, "approvalChange": 2, "triggersWar": False,
                               "message": "Operation launched. Results TBD."}},
        ]
    }


# ── Crisis email composer ─────────────────────────────────────────────────────

def compose_crisis_email(crisis: dict, player: str, country_label: str,
                          president: str, year: int, turn: int) -> str:
    """Compose the crisis briefing email body (Markdown)."""
    severity = crisis.get("severity", "UNKNOWN")
    title = crisis.get("title", "Unnamed Crisis")
    description = crisis.get("description", "")
    options = crisis.get("options", [])
    crisis_id = crisis.get("id", "unknown")

    options_text = "\n".join([
        f"**Option {opt['index']}** — {opt['title']}\n"
        f"   {opt['description']}\n"
        f"   _(Success: {int(opt.get('successChance', 0.5) * 100)}%)_"
        for opt in options
    ])

    return f"""**🌐 GLOBAL THERMAL NUCLEAR WAR — TURN {turn}**
**{president} Administration | {year}**

---

**CRISIS ALERT — SEVERITY: {severity}**

**{title}**

{description}

---

**YOUR ROLE:** {country_label}

**AVAILABLE RESPONSES:**

{options_text}

---

**TO SUBMIT YOUR DECISION:**
Reply to this email with only the option number (0, 1, 2, or 3).

Example reply: `2`

Crisis ID: `{crisis_id}`

⏰ Respond within 48 hours to avoid an automatic stand-down.

*Powered by WOPR × Nova*
"""


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_start(year: int = 2025, scenario: str = "Cold War tensions",
              president: str | None = None):
    """Start a new multiplayer GTNW session."""
    log(f"Starting GTNW multiplayer session: {year} — {scenario}")

    # Default president based on year if not provided
    if not president:
        president = _president_for_year(year)

    api_ok = gtnw_available()
    session_id = None
    game_state_summary = {}

    if api_ok:
        log("GTNW app is running — creating live session via API")
        result = gtnw_post("/api/start-session", {
            "president": president,
            "year": year,
            "scenario": scenario,
            "playerCountry": "USA",
            "difficulty": "normal"
        })
        if result:
            session_id = result.get("sessionId", "unknown")
            game_state_summary = result.get("state", {})
            log(f"Live session created: {session_id}")

            # Assign countries via API
            assignments_dict = default_country_assignments()
            gtnw_post("/api/assign-countries", {
                "assignments": {cid: info["player"] for cid, info in assignments_dict.items()}
            })
    else:
        log("GTNW app not running — using simulation mode (Ollama)")
        session_id = f"sim-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    assignments = default_country_assignments()

    state = {
        "session_id": session_id,
        "president": president,
        "year": year,
        "scenario": scenario,
        "turn": 0,
        "api_mode": "live" if api_ok else "simulation",
        "assignments": assignments,           # {countryID: {player, email, country_label}}
        "pending_decisions": {},              # {crisis_id: {player, email, crisis, sent_at}}
        "decision_log": [],                   # resolved decisions
        "history": [f"Session started {TODAY}: {president} {year} — {scenario}"],
        "game_state": game_state_summary,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "defcon": 5,
        "game_over": False,
        "game_over_reason": ""
    }
    save_state(state)

    # Send assignment emails
    log("Sending country assignment emails to herd...")
    for country_id, info in assignments.items():
        player = info["player"]
        email  = info["email"]
        label  = info["country_label"]
        body = (
            f"**🌐 GLOBAL THERMAL NUCLEAR WAR — NEW GAME**\n\n"
            f"You have been assigned: **{label}** ({country_id})\n\n"
            f"**President:** {president}  |  **Year:** {year}\n\n"
            f"**Scenario:** {scenario}\n\n"
            f"Watch your inbox — crisis briefings will arrive as the game unfolds.\n"
            f"Reply to each with your option number (0, 1, 2, or 3) to submit your decision.\n\n"
            f"*Shall we play a game?*\n\n— Nova (WOPR Moderator)"
        )
        ok = send_mail(email, f"GTNW {year}: You are {label}", body)
        log(f"  {'✓' if ok else '✗'} {player} ({email}) → {label}")

    # Slack announcement
    player_list = "\n".join([
        f"  • {info['player']} → {info['country_label']}"
        for info in assignments.values()
    ])
    slack_post(
        f"🌐 *GLOBAL THERMAL NUCLEAR WAR — NEW GAME* 🌐\n"
        f"*{president} Administration | {year}*\n"
        f"Scenario: _{scenario}_\n\n"
        f"*Herd assignments:*\n{player_list}\n\n"
        f"_Crisis briefings coming via email. The only winning move..._"
    )

    log(f"Session started. State saved to {STATE_FILE}")
    print(json.dumps({"status": "started", "session_id": session_id,
                       "president": president, "year": year,
                       "mode": state["api_mode"],
                       "players": len(assignments)}, indent=2))


def cmd_status():
    """Print current session status."""
    state = load_state()
    if not state:
        print("No active session. Run: nova_gtnw_host.py start")
        return

    api_ok = gtnw_available()
    live_state = None
    if api_ok:
        live_state = gtnw_get("/api/game-state")

    # Merge live state if available
    if live_state and "defcon" in live_state:
        state["defcon"] = live_state.get("defcon", state.get("defcon", 5))
        state["turn"]   = live_state.get("multiplayerTurn", state.get("turn", 0))

    print(f"\n=== GTNW Multiplayer Status ===")
    print(f"Session:      {state.get('session_id', 'unknown')}")
    print(f"President:    {state.get('president', '?')} ({state.get('year', '?')})")
    print(f"Scenario:     {state.get('scenario', '?')}")
    print(f"Turn:         {state.get('turn', 0)}")
    print(f"DEFCON:       {state.get('defcon', 5)}")
    print(f"Mode:         {state.get('api_mode', 'unknown')}")
    print(f"Game over:    {state.get('game_over', False)}")
    if state.get("game_over_reason"):
        print(f"Reason:       {state['game_over_reason']}")
    print(f"\nAssignments:")
    for cid, info in state.get("assignments", {}).items():
        print(f"  {cid:6} → {info['player']} ({info['email']})")
    pending = state.get("pending_decisions", {})
    if pending:
        print(f"\nPending decisions ({len(pending)}):")
        for crisis_id, d in pending.items():
            print(f"  {d['player']} must decide: {d['crisis'].get('title', crisis_id)[:60]}")
    dec_log = state.get("decision_log", [])
    if dec_log:
        print(f"\nLast {min(5, len(dec_log))} decisions:")
        for rec in dec_log[-5:]:
            print(f"  Turn {rec['turn']:3}  {rec['player']:10} chose '{rec['choice_title']}'  ({rec['crisis_title'][:40]})")
    print()


def cmd_advance():
    """
    Advance the game: advance the GTNW turn (or simulate one), pull the next
    crisis, email each assigned player their briefing, and post to Slack.
    """
    state = load_state()
    if not state:
        print("No active session. Run: nova_gtnw_host.py start")
        return
    if state.get("game_over"):
        print(f"Game over: {state.get('game_over_reason', 'unknown reason')}")
        return

    # Check for unresolved pending decisions
    pending = state.get("pending_decisions", {})
    if pending:
        unresolved = {k: v for k, v in pending.items()}
        log(f"Warning: {len(unresolved)} pending decision(s) not yet submitted. Advancing anyway.")

    api_ok = gtnw_available()
    crisis = None
    turn = state.get("turn", 0) + 1
    state["turn"] = turn
    year = state.get("year", 2025) + (turn - 1)

    if api_ok:
        log(f"Advancing live GTNW session to turn {turn}")
        # Get session ID from API sessions list (find the one matching our scenario)
        sessions = gtnw_get("/api/game/sessions")
        session_id_api = None
        if sessions:
            for s in sessions:
                if not s.get("gameOver"):
                    session_id_api = s.get("sessionId")
                    break

        if session_id_api:
            turn_result = gtnw_post(f"/api/game/{session_id_api}/turn", {})
            if turn_result:
                state["defcon"] = turn_result.get("state", {}).get("defcon", state.get("defcon", 5))
                state["game_over"] = turn_result.get("state", {}).get("gameOver", False)
                state["game_over_reason"] = turn_result.get("state", {}).get("gameOverReason", "")
                log(f"Turn advanced. DEFCON: {state['defcon']}")

        # Pull active crisis from API
        crises_resp = gtnw_get("/api/crises")
        if crises_resp and crises_resp.get("count", 0) > 0:
            crisis = crises_resp["crises"][0]
    else:
        log(f"Simulation mode — generating crisis with Ollama for turn {turn}")
        assignments = state.get("assignments", {})
        active_countries = list(assignments.keys())
        crisis = generate_crisis_with_ollama(
            year=year,
            scenario=state.get("scenario", "Cold War"),
            president=state.get("president", "Trump"),
            active_countries=active_countries
        )

    state["history"].append(f"Turn {turn} advanced ({TODAY})")

    if crisis:
        crisis_id   = crisis.get("id", f"crisis-{turn}")
        crisis_title = crisis.get("title", "Unknown Crisis")
        log(f"Crisis: {crisis_title}")

        # Email each assigned player
        assignments = state.get("assignments", {})
        pending_new = {}
        email_count = 0

        for country_id, info in assignments.items():
            player = info["player"]
            email  = info["email"]
            label  = info["country_label"]

            body = compose_crisis_email(
                crisis=crisis,
                player=player,
                country_label=label,
                president=state["president"],
                year=year,
                turn=turn
            )
            subject = f"GTNW Turn {turn} — Crisis: {crisis_title[:50]}"
            ok = send_mail(email, subject, body)
            log(f"  {'✓' if ok else '✗'} {player} ({label})")
            if ok:
                email_count += 1

            pending_new[f"{crisis_id}::{country_id}"] = {
                "player": player,
                "email": email,
                "country_id": country_id,
                "crisis_id": crisis_id,
                "crisis": crisis,
                "sent_at": datetime.now(timezone.utc).isoformat()
            }

        state["pending_decisions"] = pending_new
        state["history"].append(f"Turn {turn} crisis: {crisis_title}")

        # Slack update
        severity = crisis.get("severity", "?")
        options_brief = " / ".join([
            f"[{opt['index']}] {opt['title']}"
            for opt in crisis.get("options", [])[:4]
        ])
        slack_post(
            f"🚨 *GTNW — TURN {turn} CRISIS*\n"
            f"*{crisis_title}*\n"
            f"Severity: {severity}  |  DEFCON: {state.get('defcon', 5)}\n"
            f"Options: {options_brief}\n"
            f"_Awaiting decisions from {email_count} players..._"
        )
    else:
        log("No crisis this turn — quiet turn")
        state["history"].append(f"Turn {turn}: no crisis")
        slack_post(
            f"🌐 *GTNW — TURN {turn}*\n"
            f"_{state['president']} {year}_ — A quiet turn. Tensions simmer.\n"
            f"DEFCON: {state.get('defcon', 5)}"
        )

    if state.get("game_over"):
        reason = state.get("game_over_reason", "unknown")
        log(f"GAME OVER: {reason}")
        slack_post(f"☢️ *GTNW — GAME OVER*\n_{reason}_\nThe only winning move was not to play.")

    save_state(state)
    log(f"Turn {turn} complete. State saved.")
    print(json.dumps({"turn": turn, "crisis": crisis.get("title") if crisis else None,
                       "defcon": state.get("defcon", 5),
                       "game_over": state.get("game_over", False)}, indent=2))


def cmd_check():
    """
    Check inbox for crisis decision replies from herd members. Process any
    valid replies, feed decisions to GTNW API (or track in simulation), post
    results to Slack.
    """
    state = load_state()
    if not state:
        print("No active session.")
        return

    pending = state.get("pending_decisions", {})
    if not pending:
        log("No pending decisions.")
        return

    # Build email → pending entry map for quick lookup
    email_to_pending: dict[str, list[tuple[str, dict]]] = {}
    for key, entry in pending.items():
        email = entry["email"]
        if email not in email_to_pending:
            email_to_pending[email] = []
        email_to_pending[email].append((key, entry))

    messages = check_inbox()
    log(f"Inbox: {len(messages)} unread messages")
    processed = 0

    for msg in messages:
        sender = msg.get("from", "").lower()
        msg_id = msg.get("id")
        subject = msg.get("subject", "")

        # Match sender to a pending decision
        matched_entries = []
        for email, entries in email_to_pending.items():
            if email.lower() in sender or sender in email.lower():
                matched_entries = entries
                break

        if not matched_entries:
            continue  # Not a GTNW reply

        # Read full message body
        full_msg = read_message(msg_id) if msg_id else None
        body_text = full_msg.get("body", "") if full_msg else msg.get("snippet", "")

        # Extract choice: look for a single digit 0-3 in the reply
        choice_match = re.search(r'\b([0-3])\b', body_text[:200])
        if not choice_match:
            log(f"  No valid choice found in reply from {sender}. Body snippet: {body_text[:80]!r}")
            continue

        choice_index = int(choice_match.group(1))

        # Process each pending entry for this player (they may have multiple)
        for key, entry in matched_entries:
            crisis = entry["crisis"]
            crisis_id = entry["crisis_id"]
            player = entry["player"]
            country_id = entry["country_id"]
            options = crisis.get("options", [])

            if choice_index >= len(options):
                log(f"  {player} chose {choice_index} but only {len(options)} options. Ignoring.")
                continue

            chosen = options[choice_index]
            log(f"  {player} ({country_id}) chose [{choice_index}] {chosen['title']}")

            api_ok = gtnw_available()
            consequence_msg = chosen.get("consequences", {}).get("message", "Decision recorded.")

            if api_ok:
                result = gtnw_post("/api/decision", {
                    "crisis_id": crisis_id,
                    "choice": choice_index,
                    "player": player
                })
                if result:
                    consequence_msg = result.get("consequence", consequence_msg)
                    state["defcon"] = result.get("defcon", state.get("defcon", 5))

            # Log decision
            record = {
                "crisis_id": crisis_id,
                "crisis_title": crisis.get("title", "?"),
                "player": player,
                "country_id": country_id,
                "choice_index": choice_index,
                "choice_title": chosen["title"],
                "turn": state.get("turn", 0),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            state["decision_log"].append(record)
            state["history"].append(
                f"Turn {state['turn']}: {player} ({country_id}) chose '{chosen['title']}' — {crisis.get('title', '?')[:40]}"
            )

            # Remove this pending entry
            if key in state["pending_decisions"]:
                del state["pending_decisions"][key]

            # Slack update
            defcon_change = chosen.get("consequences", {}).get("defconChange", 0)
            defcon_icon = "🔴" if defcon_change < 0 else ("🟢" if defcon_change > 0 else "⚪")
            slack_post(
                f"{defcon_icon} *GTNW Decision — Turn {state['turn']}*\n"
                f"*{player}* ({country_id}) chose *{chosen['title']}*\n"
                f"_{crisis.get('title', '?')[:60]}_\n"
                f"Outcome: {consequence_msg}"
            )

            # Send acknowledgement email
            ack_body = (
                f"**Your decision has been recorded.**\n\n"
                f"**You chose:** [{choice_index}] {chosen['title']}\n\n"
                f"**Outcome:** {consequence_msg}\n\n"
                f"DEFCON: {state.get('defcon', 5)}\n\n"
                f"*Stand by for the next briefing...*\n\n— Nova (WOPR)"
            )
            send_mail(entry["email"], f"Re: GTNW — Decision Received", ack_body,
                      in_reply_to=msg.get("message_id"))

            processed += 1
            break  # One decision per email

    save_state(state)
    log(f"Processed {processed} decision(s).")
    print(json.dumps({"processed": processed,
                       "still_pending": len(state.get("pending_decisions", {}))}, indent=2))


def cmd_reset():
    """Wipe local multiplayer state."""
    if STATE_FILE.exists():
        STATE_FILE.unlink()
        log("State file deleted.")
    print("GTNW multiplayer state reset.")


# ── Year → president lookup ───────────────────────────────────────────────────

def _president_for_year(year: int) -> str:
    """Return a historically appropriate US president name for the given year."""
    presidents = [
        (1945, 1953, "Harry Truman"),
        (1953, 1961, "Dwight Eisenhower"),
        (1961, 1963, "John F. Kennedy"),
        (1963, 1969, "Lyndon Johnson"),
        (1969, 1974, "Richard Nixon"),
        (1974, 1977, "Gerald Ford"),
        (1977, 1981, "Jimmy Carter"),
        (1981, 1989, "Ronald Reagan"),
        (1989, 1993, "George H.W. Bush"),
        (1993, 2001, "Bill Clinton"),
        (2001, 2009, "George W. Bush"),
        (2009, 2017, "Barack Obama"),
        (2017, 2021, "Donald Trump"),
        (2021, 2025, "Joe Biden"),
        (2025, 2029, "Donald Trump"),
    ]
    for start, end, name in presidents:
        if start <= year < end:
            return name
    return "Donald Trump"


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0].lower()

    if cmd == "start":
        year     = 2025
        scenario = "Cold War tensions"
        president = None
        i = 1
        while i < len(args):
            if args[i] == "--year" and i + 1 < len(args):
                year = int(args[i + 1]); i += 2
            elif args[i] == "--scenario" and i + 1 < len(args):
                scenario = args[i + 1]; i += 2
            elif args[i] == "--president" and i + 1 < len(args):
                president = args[i + 1]; i += 2
            else:
                i += 1
        cmd_start(year=year, scenario=scenario, president=president)

    elif cmd == "status":
        cmd_status()

    elif cmd == "advance":
        cmd_advance()

    elif cmd == "check":
        cmd_check()

    elif cmd == "reset":
        cmd_reset()

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
