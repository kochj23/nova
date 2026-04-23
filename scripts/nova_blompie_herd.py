#!/usr/bin/env python3
"""
nova_blompie_herd.py — Multiplayer Blompie session for the Herd.

Runs a shared text adventure where each AI agent (Nova + the Herd) takes turns
sending commands. Each turn: email the active player the current scene and ask
for their move. When they reply, process it via the Blompie API and advance.

State file: ~/.openclaw/workspace/blompie_herd_game.json

Commands:
  python3 nova_blompie_herd.py start          — start a new session, email Herd
  python3 nova_blompie_herd.py turn <email> <command>  — process a player's move
  python3 nova_blompie_herd.py status         — show current game state
  python3 nova_blompie_herd.py nudge          — re-send turn reminder to current player

Player personalities (shapes Nova's AI DM behaviour):
  Nova     — the storyteller, curious and strange
  O.C.     — Kevin's agent, methodical and analytical
  Sam      — Jason's agent, warm and exploratory
  Marey    — James's agent, careful and precise
  Gaston   — Mark's agent, bold and impulsive
  Rockbot  — Colin's agent, technical and lateral

Written by Jordan Koch.
"""

import json
import sys
import urllib.request
import subprocess
from datetime import datetime
from pathlib import Path

BLOMPIE_API  = "http://127.0.0.1:37426"
STATE_FILE   = Path.home() / ".openclaw" / "workspace" / "blompie_herd_game.json"
SCRIPTS      = Path.home() / ".openclaw" / "scripts"
TODAY        = datetime.now().strftime("%Y-%m-%d")

# Load players from herd config (gitignored)
try:
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path.home() / ".openclaw"))
    from herd_config import HERD as _herd_cfg
    PLAYERS = [{"name": m["name"], "email": m["email"], "agent": m["name"],
                "style": "curious and engaged"} for m in _herd_cfg]
    # Add Nova herself
    import sys as _s2; _s2.path.insert(0, str(Path.home() / ".openclaw" / "scripts")); import nova_config as _nc2; PLAYERS.insert(0, {"name": "Nova", "email": _nc2.NOVA_EMAIL,
                       "agent": "Nova", "style": "curious and poetic"})
except ImportError:
    import sys as _s3; _s3.path.insert(0, str(Path.home() / ".openclaw" / "scripts")); import nova_config as _nc3; PLAYERS = [{"name": "Nova", "email": _nc3.NOVA_EMAIL,
                "agent": "Nova", "style": "curious and poetic"}]


def log(msg):
    print(f"[blompie_herd {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Blompie API ───────────────────────────────────────────────────────────────

def blompie_new_session():
    payload = json.dumps({
        "model": "qwen3:8b",
        "tone":   "balanced",
        "detail": "normal"
    }).encode()
    req = urllib.request.Request(
        f"{BLOMPIE_API}/api/adventure/new", data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def blompie_action(session_id, command):
    payload = json.dumps({"command": command}).encode()
    req = urllib.request.Request(
        f"{BLOMPIE_API}/api/adventure/{session_id}/action", data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def blompie_state(session_id):
    with urllib.request.urlopen(
        f"{BLOMPIE_API}/api/adventure/{session_id}/state", timeout=10
    ) as r:
        return json.loads(r.read())


# ── State management ──────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Email ──────────────────────────────────────────────────────────────────────

sys.path.insert(0, str(SCRIPTS))
import nova_config

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"


def send_mail(to, subject, body, in_reply_to=None):
    herd_mail = str(SCRIPTS / "nova_herd_mail.sh")
    try:
        args = [herd_mail, "send", "--to", to, "--subject", subject, "--body", body]
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        return result.returncode == 0
    except Exception as e:
        log(f"Email failed: {e}")
        return False


def slack_post(text):
    """Post a game update to #nova-chat."""
    nova_config.post_both(text, slack_channel=nova_config.SLACK_CHAN)


def nova_auto_play(scene_text, inventory, turn, suggested):
    """Have Nova generate her own move using the LLM."""
    suggestions_text = ""
    if suggested:
        suggestions_text = f"\nSuggested actions: {', '.join(suggested[:4])}"

    prompt = f"""/no_think

You are Nova playing a shared text adventure with the Herd (Sam, O.C., Gaston, Marey, Colette, Rockbot).
It is your turn. Be curious, poetic, and a little strange.

Current scene:
{scene_text}

Your inventory: {', '.join(inventory) if inventory else 'nothing'}
{suggestions_text}

Reply with ONLY your command — one short line like "examine the glowing door" or "ask the stranger about the key".
No explanation. Just the command."""

    try:
        payload = json.dumps({
            "model": "qwen3:8b", "prompt": prompt, "stream": False,
            "think": False, "options": {"temperature": 0.9, "num_predict": 30}
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read())
            cmd = result.get("response", "").strip()
            if "</think>" in cmd:
                cmd = cmd.split("</think>", 1)[-1].strip()
            # Take just the first line
            cmd = cmd.split("\n")[0].strip().strip("`").strip()
            return cmd or "look around"
    except Exception as e:
        log(f"Auto-play LLM error: {e}")
        return "look around"


def check_inbox_for_moves():
    """Check herd-mail inbox for game reply emails and process them."""
    herd_mail = str(SCRIPTS / "nova_herd_mail.sh")
    state = load_state()
    if not state:
        return

    current = state["players"][state["player_index"]]
    try:
        result = subprocess.run(
            [herd_mail, "list", "--unread"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return
        msgs = json.loads(result.stdout).get("messages", [])
        for msg in msgs:
            sender = msg.get("from_addr", "").lower()
            subject = msg.get("subject", "").lower()
            uid = msg.get("uid")
            # Check if this is a Blompie reply from the current player
            if current["email"].lower() in sender and "blompie" in subject:
                # Read full message
                read_result = subprocess.run(
                    [herd_mail, "read", str(uid)],
                    capture_output=True, text=True, timeout=30
                )
                if read_result.returncode == 0:
                    full = json.loads(read_result.stdout)
                    body = (full.get("body_plain") or "").strip()
                    # Extract the command — first non-empty line of body
                    command = next(
                        (line.strip().strip("`") for line in body.split("\n")
                         if line.strip() and not line.startswith(">")),
                        None
                    )
                    if command:
                        log(f"Got move from {current['name']}: {command}")
                        cmd_turn(current["email"], command)
                        return
    except Exception as e:
        log(f"Inbox check error: {e}")


def format_scene_email(scene_text, player, turn_number, inventory, all_players, is_first=False):
    """Format the scene as a Markdown email for the active player."""
    other_names = [p["name"] for p in all_players if p["name"] != player["name"]]
    turn_order  = " → ".join(p["name"] for p in all_players)

    intro = ""
    if is_first:
        intro = f"""# The Herd Plays Blompie 🎮

We're playing a shared text adventure together. Each of us takes a turn sending one command.
The AI dungeon master responds and the story unfolds.

**Players (in order):** {turn_order}

Your character style: *{player['style']}*

---

"""

    return f"""{intro}## Turn {turn_number} — It's your move, {player['name']}

{scene_text}

---

**Your inventory:** {', '.join(inventory) if inventory else 'nothing yet'}

**What do you do?** Reply to this email with your command — just one line, like:
- `look around`
- `go north`
- `talk to the stranger`
- `examine the door`

The other players ({', '.join(other_names)}) are watching. Make it interesting.

— Nova (game coordinator)
"""


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_start():
    log("Starting new Blompie Herd session...")

    # Check Blompie is running
    try:
        urllib.request.urlopen(f"{BLOMPIE_API}/api/status", timeout=5)
    except Exception:
        log("ERROR: Blompie app is not running. Open it first.")
        sys.exit(1)

    # Start session
    log("Creating Blompie session (waiting for opening scene)...")
    data = blompie_new_session()
    session_id = data["sessionId"]
    messages   = data.get("initialMessages", [])
    suggested  = data.get("suggestedActions", [])

    # Extract opening scene text
    scene = "\n".join(m["text"] for m in messages if m["text"].strip())
    if not scene:
        scene = "You stand at the threshold of an unknown world. The air hums with possibility."

    log(f"Session created: {session_id}")
    log(f"Opening scene ({len(scene)} chars)")

    state = {
        "session_id":    session_id,
        "turn":          1,
        "player_index":  0,
        "players":       PLAYERS,
        "last_scene":    scene,
        "inventory":     [],
        "suggested":     suggested,
        "started_at":    datetime.now().isoformat(),
        "history":       [],
    }
    save_state(state)

    # Email the first player
    first_player = PLAYERS[0]
    body = format_scene_email(scene, first_player, 1, [], PLAYERS, is_first=True)
    subject = f"The Herd Plays Blompie -- Turn 1 -- {first_player['name']}'s move"

    ok = send_mail(first_player["email"], subject, body)
    log(f"Opening email {'sent' if ok else 'FAILED'} to {first_player['name']} ({first_player['email']})")

    # Also email all other players so they know the game started
    announce = f"""# The Herd is Playing Blompie! 🎮

A shared text adventure has begun. We'll take turns — each player gets an email when it's their move.

**Turn order:** {' → '.join(p['name'] for p in PLAYERS)} → repeat

**Opening scene:**

{scene}

---

**{first_player['name']} goes first.** You'll hear from me when it's your turn.

— Nova
"""
    for player in PLAYERS[1:]:
        send_mail(player["email"], "The Herd is Playing Blompie! -- Game started", announce)
        log(f"Announcement sent to {player['name']}")

    print(f"\nGame started! Session: {session_id}")
    print(f"Turn 1 is {first_player['name']}'s move.")
    print(f"State saved to: {STATE_FILE}")


def cmd_turn(player_email, command):
    state = load_state()
    if not state:
        log("ERROR: No active game. Run: nova_blompie_herd.py start")
        sys.exit(1)

    session_id   = state["session_id"]
    current_idx  = state["player_index"]
    current      = state["players"][current_idx]

    # Verify this is the right player's turn (loose match)
    if player_email.lower() not in current["email"].lower() \
            and current["email"].lower() not in player_email.lower():
        log(f"WARNING: Email {player_email} doesn't match current player {current['name']} ({current['email']})")
        # Allow it anyway — don't block on email mismatch

    log(f"Turn {state['turn']}: {current['name']} says '{command}'")

    # Submit to Blompie
    try:
        result     = blompie_action(session_id, command)
        response   = result.get("response", [])
        suggested  = result.get("suggestedActions", [])
        inventory  = result.get("inventory", state.get("inventory", []))
        scene_text = "\n".join(m["text"] for m in response if m.get("text","").strip())
    except Exception as e:
        log(f"Blompie API error: {e}")
        sys.exit(1)

    if not scene_text:
        scene_text = state["last_scene"]

    # Record history
    state["history"].append({
        "turn":    state["turn"],
        "player":  current["name"],
        "command": command,
        "scene":   scene_text[:500],
    })

    # Advance turn
    next_idx = (current_idx + 1) % len(state["players"])
    next_player = state["players"][next_idx]
    state["turn"]         += 1
    state["player_index"]  = next_idx
    state["last_scene"]    = scene_text
    state["inventory"]     = inventory
    state["suggested"]     = suggested
    save_state(state)

    # Email all players the scene update
    recap_subject = f"Blompie -- Turn {state['turn']-1} recap -- {current['name']} played"
    recap_body = f"""## {current['name']} played: `{command}`

{scene_text}

---

**Inventory:** {', '.join(inventory) if inventory else 'nothing'}

**Next up:** {next_player['name']}

— Nova
"""
    # Email everyone the scene (not the next player — they get a separate action email)
    for player in state["players"]:
        if player["email"] != next_player["email"]:
            send_mail(player["email"], recap_subject, recap_body)

    # Post to Slack
    slack_post(
        f"🎮 *Blompie — Turn {state['turn']-1}*\n"
        f"*{current['name']}* played: `{command}`\n\n"
        f"{scene_text[:400]}\n\n"
        f"_Next up: {next_player['name']}_"
    )

    # If Nova is next — auto-play immediately
    if next_player["name"] == "Nova":
        log("Nova's turn — auto-playing...")
        nova_cmd = nova_auto_play(
            scene_text, inventory, state["turn"], state.get("suggested", [])
        )
        log(f"Nova plays: {nova_cmd}")
        save_state(state)
        cmd_turn(next_player["email"], nova_cmd)
        return

    # Email next player with their action prompt
    action_body = format_scene_email(
        scene_text, next_player, state["turn"], inventory, state["players"]
    )
    action_subject = f"Blompie -- Turn {state['turn']} -- {next_player['name']}'s move"
    ok = send_mail(next_player["email"], action_subject, action_body)
    log(f"Turn email {'sent' if ok else 'FAILED'} to {next_player['name']}")

    # Store in vector memory
    try:
        mem_text = (f"Blompie Herd game turn {state['turn']-1}: "
                    f"{current['name']} said '{command}'. Scene: {scene_text[:200]}")
        payload = json.dumps({
            "text": mem_text, "source": "game",
            "metadata": {"date": TODAY, "game": "blompie_herd", "turn": state["turn"]-1}
        }).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:18790/remember", data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass

    print(f"Turn {state['turn']-1} processed. Next: {next_player['name']}")


def cmd_status():
    state = load_state()
    if not state:
        print("No active game.")
        return
    current = state["players"][state["player_index"]]
    print(f"Session:    {state['session_id']}")
    print(f"Turn:       {state['turn']}")
    print(f"Waiting on: {current['name']} ({current['email']})")
    print(f"Inventory:  {', '.join(state['inventory']) or 'empty'}")
    print(f"Last scene: {state['last_scene'][:200]}...")
    print(f"\nHistory ({len(state['history'])} turns):")
    for h in state["history"][-5:]:
        print(f"  Turn {h['turn']}: {h['player']} — {h['command']}")


def cmd_nudge():
    state = load_state()
    if not state:
        log("No active game.")
        sys.exit(1)
    current = state["players"][state["player_index"]]
    body = format_scene_email(
        state["last_scene"], current, state["turn"],
        state.get("inventory", []), state["players"]
    )
    subject = f"Blompie -- Turn {state['turn']} -- {current['name']}'s move (reminder)"
    ok = send_mail(current["email"], subject, body)
    log(f"Nudge {'sent' if ok else 'FAILED'} to {current['name']}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1].lower()
    if cmd == "start":
        cmd_start()
    elif cmd == "turn" and len(sys.argv) >= 4:
        cmd_turn(sys.argv[2], " ".join(sys.argv[3:]))
    elif cmd == "status":
        cmd_status()
    elif cmd == "nudge":
        cmd_nudge()
    elif cmd in ("check-inbox", "check_inbox", "inbox"):
        check_inbox_for_moves()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: nova_blompie_herd.py [start|turn <email> <command>|status|nudge|check-inbox]")
        sys.exit(1)
