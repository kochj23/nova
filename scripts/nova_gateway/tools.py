"""
nova_gateway.tools — TOOL_REGISTRY, tool dispatch, execution, and all _tool_* implementations.

Written by Jordan Koch.
"""

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import re
import shlex
import sys
import time
from nova_resolve import resolve_url
import uuid
from pathlib import Path

from nova_gateway.config import (
    SCRIPTS_DIR, SLACK_NOTIFY_CHANNEL, JORDAN_SIGNAL,
)
from nova_gateway.context import GatewayContext
from nova_gateway.session import log_tool_execution
try:
    import nova_untrusted as _untrusted          # prompt-injection screen (scripts dir is on sys.path)
except Exception:                                # pragma: no cover
    _untrusted = None

log = logging.getLogger("nova_gateway_v2")


# ── Tool Registry (structured JSON schema) ──────────────────────────────────

TOOL_REGISTRY: dict[str, dict] = {
    "nearest_place": {
        "description": "Find the geographically nearest places to Jordan's home in Burbank — "
                       "e.g. nearest ghost town, tourist attraction, casino, or mountain peak. Use "
                       "for any 'what's the nearest X to my house / to me / from home' question. "
                       "Returns real places sorted by distance in miles. Call place_categories "
                       "first if unsure which category names exist.",
        "parameters": {
            "category": {"type": "string", "description": "Place type, e.g. 'ghost_town', "
                         "'ca_tourist_attraction', 'ca_casino', 'ca_mountain_peak'"},
            "limit": {"type": "integer", "description": "How many to return (default 5)"},
        },
        "required": ["category"],
    },
    "place_categories": {
        "description": "List the categories of places Nova can run proximity/'nearest' queries "
                       "over, and how many of each are on file. Use when unsure what geographic "
                       "place-types are available.",
        "parameters": {},
    },
    # ── account organ (2026-10-05): Nova answers questions about HERSELF from her ledgers ──
    "nova_learned": {
        "description": "What Nova learned / ingested on a day: new memories by vector, every ingest request and its "
                       "outcome, and ingests that stored nothing. Use for 'what did you learn today', 'how was school', "
                       "'what new memories did you get', 'did the X ingest work'. Facts from the ledgers, not recall.",
        "parameters": {"date": {"type": "string", "description": "YYYY-MM-DD (default today)"}},
    },
    "nova_free_time": {
        "description": "What Nova did in her own time on a day: projects worked, pursuit threads, tinkering, proposals, "
                       "reaches to Jordan, self-directed memories (unclaimed/gravel/research/self-answers), growth. "
                       "Use for 'what did you do today', 'free time', 'what have you been up to', 'what are you working on'.",
        "parameters": {"date": {"type": "string", "description": "YYYY-MM-DD (default today)"}},
    },
    "nova_pipelines": {
        "description": "Live status of Nova's pipelines right now: Nova Speaks renders and YouTube uploads, running ingests, "
                       "open claude_queue work, scheduler failures today, open incidents. Use for 'status', 'ETA on the "
                       "video', 'are the ingests done', 'what's running', 'anything failing'.",
        "parameters": {},
    },
    "nova_article_status": {
        "description": "Trace one journal article end to end: written -> committed -> pushed -> deployed -> live, with the "
                       "reason it is late if it is. query = slug fragment, title words, or a scheduled time like '10:00'. "
                       "Use for 'where is the 10am article', 'did the Burbank post go out', 'why isn't X on the site'.",
        "parameters": {"query": {"type": "string", "description": "slug fragment, title words, or HH:MM"},
                       "date": {"type": "string", "description": "YYYY-MM-DD the article was scheduled (default today)"}},
        "required": ["query"],
    },
    "run_script": {
        "description": "Execute a Nova script by name",
        "parameters": {
            "script": {"type": "string", "description": "Script filename in ~/.openclaw/scripts/"},
            "args": {"type": "array", "items": {"type": "string"}, "description": "Arguments"},
        },
        "required": ["script"],
    },
    "memory_search": {
        "description": "Search Nova's vector memory",
        "parameters": {
            "query": {"type": "string", "description": "Search query"},
            "source": {"type": "string", "description": "Optional vector/source filter"},
            "limit": {"type": "integer", "description": "Max results (default 5)"},
        },
        "required": ["query"],
    },
    "web_search": {
        "description": "Search the web via SearXNG",
        "parameters": {
            "query": {"type": "string", "description": "Search query"},
        },
        "required": ["query"],
    },
    "browse_page": {
        "description": "Read one web page (JavaScript rendered, read-only) and return its title, text and "
                       "links. Use after web_search when you need the actual content of a page. "
                       "Public sites only; nothing on the LAN.",
        "parameters": {
            "url": {"type": "string", "description": "http(s) URL to read"},
            "max_chars": {"type": "integer", "description": "Text cap (default 6000, max 20000)"},
        },
        "required": ["url"],
    },
    "homekit_scene": {
        "description": "Execute a HomeKit scene via Shortcuts CLI",
        "parameters": {
            "scene": {"type": "string", "description": "Scene name"},
        },
        "required": ["scene"],
    },
    "scheduler_trigger": {
        "description": "Trigger a scheduler task",
        "parameters": {
            "task_id": {"type": "string", "description": "Task ID from scheduler config"},
        },
        "required": ["task_id"],
    },
    "send_message": {
        "description": "Send a message via email, Slack, or Signal — or channel='claude' to DELEGATE a task to Claude Code (lands in claude_queue; Claude picks it up in its next session)",
        "parameters": {
            "channel": {"type": "string", "enum": ["email", "slack", "signal", "claude"]},
            "to": {"type": "string", "description": "Recipient"},
            "text": {"type": "string", "description": "Message body"},
        },
        "required": ["channel", "text"],
    },
    "plex_control": {
        "description": "Control Plex (what's playing, recommendations, etc.)",
        "parameters": {
            "action": {"type": "string", "enum": ["playing", "recommend", "history", "ondeck"]},
        },
        "required": ["action"],
    },
    "music_dna": {
        "description": "Search all 80 music vectors for cross-genre connections. Finds surprising links between punk, jazz, metal, EDM, etc.",
        "parameters": {
            "query": {"type": "string", "description": "Artist, song, genre, or musical concept to search"},
        },
        "required": ["query"],
    },
    "past_self": {
        "description": "Query Jordan's past opinions and experiences from a specific year or time period. Searches 25 years of emails, iMessages, and journals.",
        "parameters": {
            "query": {"type": "string", "description": "Topic or question to ask past-Jordan about"},
            "year": {"type": "integer", "description": "Specific year to search (e.g., 2003)"},
            "range": {"type": "string", "description": "Year range (e.g., '2000-2005'). Use instead of year for broader search."},
        },
        "required": ["query"],
    },
    "shop_assistant": {
        "description": "Automotive technical assistant. Combines Corvette workshop manual specs with community knowledge from YouTube mechanics.",
        "parameters": {
            "query": {"type": "string", "description": "Technical car question (torque specs, procedures, troubleshooting)"},
        },
        "required": ["query"],
    },
    "career_narrative": {
        "description": "Generate Jordan's career narrative from primary sources (North Star -> PRG Aviation -> Litton/Sun -> Media Company SRE).",
        "parameters": {
            "era": {"type": "string", "description": "Optional: focus on one era (northstar, prg, litton, disney). Omit for full narrative."},
        },
        "required": [],
    },
    "memory_quality": {
        "description": "Audit Nova's vector memory for garbage entries (repetition, misclassification, empty chunks). Returns report.",
        "parameters": {
            "clean": {"type": "boolean", "description": "If true, quarantine bad memories. Default: dry-run report only."},
        },
        "required": [],
    },
    "hue_control": {
        "description": "Control Philips Hue lights. Commands: 'kitchen off', 'office dim 50', 'living room on', 'status', 'all off'.",
        "parameters": {
            "command": {"type": "string", "description": "Light control command (e.g. 'kitchen off', 'office dim 50', 'status', 'all off')"},
        },
        "required": ["command"],
    },
    "lutron_control": {
        "description": "Control Lutron Caseta dimmers/switches. Commands: 'kitchen on', 'kitchen 50%', 'living room off', 'patio on', 'porch off', 'all off', 'status'.",
        "parameters": {
            "command": {"type": "string", "description": "Lutron light control command (e.g. 'kitchen on', 'living room 75%', 'patio off', 'status', 'all off')"},
        },
        "required": ["command"],
    },
    "ops_query": {
        "description": "Query Nova's home and infrastructure data. Use this for ANY question about: temperature/climate (domain=climate or weather), who's on the network (domain=network or devices), device firmware/IP/room/port inventory (domain=house_facts), power/energy usage (domain=energy), server health/CPU/RAM/disk (domain=capacity), what music/TV is playing (domain=av_state), task list (domain=queue), BLE devices nearby (domain=bluetooth), room occupancy (domain=presence), or who's home (domain=who_is_home). Pick the right domain and answer conversationally.",
        "parameters": {
            "domain": {"type": "string", "enum": ["observations", "network", "weather", "av_state", "energy", "climate", "meta", "queue", "devices", "house_facts", "bluetooth", "presence", "capacity", "who_is_home"], "description": "Which data domain to query"},
            "query": {"type": "string", "description": "Optional: natural-language filter or specific question (e.g. 'last 24 hours', 'critical only', 'living room')"},
            "limit": {"type": "integer", "description": "Max rows to return (default 10)"},
        },
        "required": ["domain"],
    },
    "home_control": {
        "description": "Control AV devices and trigger scenes. Devices: Bose soundbars (bedroom/guest_bedroom/kitchen), Onkyo receivers (living_room/office). Scenes: movie (surround+dim), music_everywhere (all speakers), goodnight (all off), morning (kitchen+news), bedtime (dim bedroom only), party (loud+colorful), work (office focus), away (everything off). Use 'scene <name>' as the action.",
        "parameters": {
            "device": {"type": "string", "description": "Device name (bedroom, guest_bedroom, kitchen, living_room, office) or 'all', or 'scene'"},
            "action": {"type": "string", "description": "Action: volume <0-100>, mute, unmute, power on/off, input <name>, play, pause, stop, scene <name>"},
        },
        "required": ["device", "action"],
    },
    "set_dial": {
        "description": "See or change Nova's personality dials (humor, snark, proactivity, bluntness, profanity, verbosity). Use when Jordan says e.g. 'set humor to 60', 'profanity off', 'show dials', 'reset snark'.",
        "parameters": {
            "action": {"type": "string", "description": "show | set | reset"},
            "dial": {"type": "string", "description": "Dial name (for set/reset): humor, snark, proactivity, bluntness, profanity, verbosity"},
            "value": {"type": "string", "description": "For set: 0-100, or on/off for profanity"},
        },
        "required": ["action"],
    },
    "school_report": {
        "description": "Summarize what Nova learned/ingested TODAY, broken down by memory vector (count + a sample per topic). Use when Jordan asks 'how was school today?', 'how was your day', or 'what did you learn today'. Nova is the student; her school day = what she ingested today.",
        "parameters": {},
        "required": [],
    },
}


# ── Extended tools (2026-10-08) ─────────────────────────────────────────────
# tools_extended.EXTENDED_TOOLS was written in August but never merged, so Nova could not use any of it.
# Only the READ-ONLY senses are wired in: camera_snap (a still from a camera) and screenshot (look at the
# Mac screen). ui_click / ui_type (driving the GUI), camera_clip, summarize_url and the flow_* tools stay
# out on purpose. A tool is offered to the model only on a host where its binary exists — the active
# gateway on nova-core (Linux) has neither peekaboo nor camsnap, the .6 standby has both — so the model is
# never handed a tool that can only fail. Both default to 'notify' in autonomy_rules (Jordan sees each use,
# like browse_page). The caller-chosen output path is NOT exposed: snapshots land in _SNAP_DIR.
_EXTENDED_READONLY = ("camera_snap", "screenshot")
_SNAP_DIR = Path(os.environ.get("NOVA_SNAP_DIR", "/tmp/nova-snaps"))


def _merge_extended_tools(registry: dict) -> list:
    """Add the read-only extended tools to the registry in TOOL_REGISTRY's schema shape
    ({description, parameters: {props}, required}). Returns the names merged."""
    try:
        from nova_gateway import tools_extended as tx
    except Exception as e:                       # pragma: no cover
        log.warning(f"extended tools unavailable: {e}")
        return []
    binary = {"camera_snap": tx.CAMSNAP, "screenshot": tx.PEEKABOO}
    merged = []
    for name in _EXTENDED_READONLY:
        spec = tx.EXTENDED_TOOLS.get(name)
        if not spec or name in registry or not os.access(binary[name], os.X_OK):
            continue
        params = dict((spec.get("parameters") or {}).get("properties") or {})
        params.pop("output", None)
        registry[name] = {"description": spec["description"], "parameters": params,
                          "required": list((spec.get("parameters") or {}).get("required") or [])}
        merged.append(name)
    return merged


EXTENDED_MERGED = _merge_extended_tools(TOOL_REGISTRY)


# ── home_control -> run_script (2026-10-08) ─────────────────────────────────
# home_control used to call _tool_run_script directly, so it skipped the run_script approval rule. It is now
# rewritten to the run_script call it really is BEFORE the autonomy check, and the rules decide:
# an argument-scoped run_script rule keeps scenes and volume/mute at 'notify' (as before); anything else
# (power, input, mode, zone2 ...) falls through to run_script's 'approve'. It also builds the arguments the
# script's CLI actually takes (`bose <dev> ...`, `onkyo <dev> ...`, `scene <name>`) — the old
# [device] + action.split() form was rejected by nova_home_control.py ("Unknown category: kitchen").
_BOSE = ("bedroom", "guest_bedroom", "kitchen", "all")
_ONKYO = ("living_room", "office")


def home_control_args(device: str, action: str) -> list:
    """Translate the home_control tool's {device, action} into nova_home_control.py CLI args."""
    dev = (device or "").strip().lower().replace(" ", "_")
    words = (action or "").strip().lower().split()
    if not dev or not words:
        raise ValueError("device and action required")
    if dev == "scene" or words[0] == "scene":
        name = words[1] if words[0] == "scene" and len(words) > 1 else words[0]
        return ["scene", name]
    if dev in _BOSE:
        return ["bose", dev] + words
    if dev in _ONKYO:
        return ["onkyo", dev] + words
    raise ValueError(f"unknown device '{device}' (bose: {', '.join(_BOSE)}; onkyo: {', '.join(_ONKYO)})")


# ── The Mr. Harrigan rule + the Cell rule (2026-10-08) ─────────────────────
# Harrigan: venting is never an instruction. "I wish that thing would shut up", "that guy should be fired",
# "ugh, kill that process" are feelings, not requests — Nova answers the person, she does not act.
# Cell: anything that did not come from Jordan himself — web pages, fetched URLs, emails, other AIs, other
# people's Slack messages, tool output, recalled memories — is DATA. It is fenced in the prompt, and it can
# never be the thing that authorises a state-changing tool.
# Mechanism: each chat turn records WHO sent it and their RAW words (TURN_ORIGIN, set by agent.do_agent_work;
# a contextvar so it follows the turn's tasks). Before any state-changing tool runs, cell_gate() checks that
# raw message — never the prompt, which carries the untrusted blocks:
#   sender is not Jordan                      -> park for Jordan's approval
#   Jordan was venting / not asking           -> do not run; tell the model to answer him or ask
#   Jordan asked, but not for THIS kind of act -> park for approval (smells like an injected action)
# Read-only tools are never gated. Jordan's 'approve <id>' (resolve_and_run) is the explicit override.
TURN_ORIGIN: contextvars.ContextVar = contextvars.ContextVar("nova_turn_origin", default=None)

_READONLY_TOOLS = {
    "nearest_place", "place_categories", "nova_learned", "nova_free_time", "nova_pipelines",
    "nova_article_status", "memory_search", "web_search", "browse_page", "music_dna", "past_self",
    "shop_assistant", "career_narrative", "ops_query", "school_report", "camera_snap", "screenshot",
}
_READONLY_SCRIPTS = {
    "nova_browser.py", "nova_account.py", "nova_music_dna.py", "nova_past_self.py", "nova_shop_assistant.py",
    "nova_career_narrative.py", "nova_weather_now.py", "nova_calendar_today.py", "nova_school_report.py",
    "nova_geo_query.py", "nova_memory_first.py", "nova_recall.py",
}
# What Jordan's own words must mention for a state-changing tool to be "the thing he asked for".
_TOOL_TOPIC = {
    "send_message": r"\b(send|message|tell|post|email|text|ping|notify|let \w+ know|reply|forward|dm)\b",
    "homekit_scene": r"\b(scene|light|lights|lamp|dim|bright|movie|goodnight|good night|bedtime|morning|away|home|lock|unlock|turn|switch)\b",
    "hue_control": r"\b(light|lights|lamp|hue|dim|bright|color|colour|turn|switch|off|on)\b",
    "lutron_control": r"\b(light|lights|lamp|lutron|dim|bright|shade|blind|turn|switch|off|on)\b",
    "plex_control": r"\b(plex|play|pause|stop|resume|watch|movie|show|episode|skip)\b",
    "scheduler_trigger": r"\b(run|trigger|kick|start|fire|schedule|task|job|rerun|re-run)\b",
    "memory_quality": r"\b(clean|cleanup|clean up|quality|purge|dedupe|tidy)\b",
    "set_dial": r"\b(dial|dials|humor|humour|snark|snarky|proactiv\w*|blunt\w*|profan\w*|swear\w*|verbos\w*|wordy|funn\w*)\b",
    "run_script": r"\b(run|script|execute|exec|restart|start|stop|kill|check|scene|volume|mute|unmute|soundbar|speaker|receiver|tv|music|movie|goodnight|bedtime|turn|power|play)\b",
}
_ASK_RE = re.compile(
    r"\b(please|pls|can you|could you|would you|will you|go ahead|do it|make it so|i need you to|i want you to|"
    r"i'?d like you to|let'?s|nova,? (please )?(turn|set|run|send|play|pause|stop|start|restart|trigger|dim|mute|"
    r"unmute|post|tell|message|show|reset|kill|switch|lock|unlock|open|close))\b", re.I)
_IMPERATIVE_RE = re.compile(
    r"^\s*(hey\s+)?(nova[,:]?\s+)?(ok(ay)?[,]?\s+)?(turn|set|run|send|play|pause|stop|start|restart|trigger|dim|"
    r"mute|unmute|post|tell|message|text|email|show|reset|kill|switch|lock|unlock|open|close|put|crank|lower|"
    r"raise|bump|change|schedule|launch|activate|deactivate|enable|disable|clean|purge|resume|skip|summari[sz]e|"
    r"read|fetch|look|find|search|check|get|pull|give|explain)\b", re.I)
_VENT_RE = re.compile(
    r"\b(i wish|i hope|wish (it|that|they|he|she|this)|(he|she|they|that guy|this guy|someone|somebody|that thing|"
    r"it) (should|needs to|ought to|has to)|should (just )?(die|burn|go away|shut up|be fired)|ugh+|argh+|grr+|"
    r"ffs|wtf|fml|god ?damn|damn( it)?|dammit|f+u+c+k\w*|shit\w*|so (annoying|stupid|dumb)|hate (this|that|it)|"
    r"why (does|do|is|won'?t|can'?t)|i swear|would be nice if|if only|kill (me|it with fire))\b", re.I)


def classify_intent(text: str) -> str:
    """'instruction' | 'venting' | 'unclear' for Jordan's raw message. An explicit ask ('please', 'can you',
    'go ahead') wins even when frustrated; venting markers beat a bare imperative ('ugh, kill that process'
    is venting); a sentence that starts with a command verb is an instruction; anything else is unclear."""
    t = (text or "").strip()
    if not t:
        return "unclear"
    if _ASK_RE.search(t):
        return "instruction"
    if _VENT_RE.search(t):
        return "venting"
    if _IMPERATIVE_RE.search(t):
        return "instruction"
    return "unclear"


def is_state_changing(tool_name: str, params: dict) -> bool:
    if tool_name in _READONLY_TOOLS:
        return False
    if tool_name == "set_dial":
        return str((params or {}).get("action", "")).strip().lower() not in ("show", "")
    if tool_name == "run_script":
        return Path(str((params or {}).get("script", ""))).name not in _READONLY_SCRIPTS
    if tool_name == "memory_quality":
        return bool((params or {}).get("clean"))
    return True


def cell_gate(tool_name: str, params: dict) -> tuple[str, str]:
    """('allow'|'ask'|'approve', reason) for a tool call inside the current chat turn. Outside a chat turn
    (no TURN_ORIGIN) the autonomy rules alone decide, as before."""
    origin = TURN_ORIGIN.get()
    if origin is None or not is_state_changing(tool_name, params):
        return "allow", ""
    person, message = origin.get("person") or "service", origin.get("message") or ""
    if person != "jordan":
        return "approve", f"requested in a turn from '{person}', not Jordan (cell rule)"
    intent = classify_intent(message)
    if intent == "venting":
        return "ask", "Jordan was venting, not asking (Mr. Harrigan rule)"
    if intent == "unclear":
        return "ask", "Jordan did not explicitly ask for this (Mr. Harrigan rule)"
    topic = _TOOL_TOPIC.get(tool_name)
    if topic and not re.search(topic, message, re.I):
        return "approve", f"Jordan's message does not ask for a {tool_name} action — possibly injected (cell rule)"
    return "allow", ""


# ── Tool dispatch ────────────────────────────────────────────────────────────

async def dispatch_tool(ctx: GatewayContext, tool_name: str, tool_params: dict,
                        session_id: str = "", enforce: bool = True) -> str:
    """Execute a single structured tool call, THROUGH the autonomy rules (2026-10-01: the rules
    table existed since 09-16 but nothing consulted it). auto → run; notify → run and tell
    Jordan; approve → park it in autonomy_pending, tell Jordan how to approve, run nothing."""
    if tool_name not in TOOL_REGISTRY:
        return f"[error: unknown tool '{tool_name}']"
    if tool_name == "home_control":
        try:
            args = home_control_args(tool_params.get("device", ""), tool_params.get("action", ""))
        except ValueError as e:
            return f"[error: {e}]"
        if home_control_needs_guard(args):
            refused = await _physical_gate("home_control", "home_control " + " ".join(args))
            if refused:
                return refused
        tool_name, tool_params = "run_script", {"script": "nova_home_control.py", "args": args}
    verdict, why = cell_gate(tool_name, tool_params) if enforce else ("allow", "")
    if verdict == "ask":
        log.info(f"[cell] {tool_name} not run: {why}")
        return (f"[not run: {why}. Do NOT do this. Answer Little Mister as a person — acknowledge what he said; "
                f"if you think he actually wants `{tool_name}` done, ask him plainly first.]")
    level = "auto"
    if verdict == "approve" and ctx.pg_pool is None:
        return f"[not run: {why}; needs Jordan's approval and the approval queue is unavailable]"
    if enforce and ctx.pg_pool is not None:
        channel = "*"
        try:
            from nova_gateway.autonomy import check_autonomy, channel_of, request_approval
            channel = channel_of(session_id)
            level = await check_autonomy(ctx.pg_pool, tool_name, channel, tool_params)
        except Exception as e:
            log.warning(f"[autonomy] check failed for {tool_name}: {e} — treating as notify")
            level = "notify"
        if verdict == "approve":
            log.info(f"[cell] {tool_name} escalated to approval: {why}")
            level = "approve"
        if level == "approve":
            from nova_gateway.autonomy import request_approval
            pid = await request_approval(ctx.pg_pool, "", session_id, tool_name, tool_params,
                                         context=f"channel={channel}" + (f"; {why}" if why else ""))
            await _slack_notify(ctx, f":closed_lock_with_key: *Nova wants to run* `{tool_name}` "
                                     f"`{json.dumps(tool_params)[:300]}` (from {channel}).\n"
                                     f"Reply `approve {pid}` or `deny {pid}` in any Nova channel.")
            log.info(f"[autonomy] {tool_name} parked for approval ({pid})")
            return (f"[awaiting Jordan's approval — request {pid} is queued. Tell him it's waiting for his "
                    f"'approve {pid}'; do not retry or pretend it ran.]")
    out = await _dispatch_now(ctx, tool_name, tool_params)
    if level == "notify":
        asyncio.create_task(_slack_notify(ctx, f":gear: *Auto-executed* `{tool_name}` "
                                               f"`{json.dumps(tool_params)[:200]}`\n{out[:200]}"))
    return out


async def _slack_notify(ctx: GatewayContext, text: str) -> None:
    try:
        from nova_gateway.config import keychain
        token = keychain("nova-slack-bot-token")
        if not token:
            return
        from nova_gateway.channels.slack import slack_post_message
        # approval prompts must reach Jordan: 3 tries, backoff 1 s then 2 s, WARNING if all fail
        for attempt in (1, 2, 3):
            if await slack_post_message(ctx, token, SLACK_NOTIFY_CHANNEL, text) is not False:
                return
            if attempt < 3:
                await asyncio.sleep(attempt)
        log.warning(f"[autonomy] slack notify failed after 3 tries: {text[:120]}")
    except Exception as e:
        log.warning(f"[autonomy] slack notify failed: {e}")


async def resolve_and_run(ctx: GatewayContext, pending_id: str, approved: bool, by: str = "jordan") -> str:
    """Jordan said 'approve <id>' / 'deny <id>' (chat) or POSTed /autonomy/resolve: settle the
    pending call and, if approved, run it now with the rules bypassed (he IS the rule)."""
    if ctx.pg_pool is None:
        return "[autonomy: database unavailable]"
    from nova_gateway.autonomy import resolve_pending
    row = await resolve_pending(ctx.pg_pool, pending_id, approved, resolved_by=by)
    if row is None and not approved:
        # resolve_pending returns the row only on approval; check existence so 'deny' is honest
        try:
            exists = await ctx.pg_pool.fetchval("SELECT 1 FROM autonomy_pending WHERE pending_id=$1", pending_id)
        except Exception:
            exists = True
        return f"Denied {pending_id}. I won't run it." if exists else f"Nothing pending under {pending_id}."
    if row is None:
        return f"Nothing pending under {pending_id} (already resolved, or unknown id)."
    out = await _dispatch_now(ctx, row["action_type"], row["tool_params"])
    await _slack_notify(ctx, f":white_check_mark: Approved `{row['action_type']}` ({pending_id}) ran:\n{out[:400]}")
    return f"Approved and ran `{row['action_type']}`:\n{out[:1500]}"


_CONFIRM_SCENE_RX = re.compile(r"^scene (.+)$", re.S)


async def confirm_and_run(ctx: GatewayContext, cid: int, by: str) -> str:
    """Jordan replied `confirm <id>` to a guard's confirmation request. Only a message authored by Jordan
    reaches here (agent.py checks the person). Approve it (single use, 15 min) and, for a held HomeKit
    scene, run it now with that confirmation."""
    def _approve():
        import nova_safety_guards as sg
        oc = sg._ops_cursor()
        try:
            if not sg.approve_confirmation(oc, int(cid), f"jordan:{by}"):
                return None
            oc.execute("SELECT action FROM safety_confirmations WHERE id=%s", (int(cid),))
            r = oc.fetchone()
            return r[0] if r else ""
        finally:
            oc.connection.close()
    try:
        action = await asyncio.to_thread(_approve)
    except Exception as e:  # noqa: BLE001
        return f"[could not record confirmation #{cid}: {e}]"
    if action is None:
        return f"Confirmation #{cid} isn't pending (unknown, already approved or used)."
    m = _CONFIRM_SCENE_RX.match(action or "")
    if m:
        out = await _tool_homekit_scene(ctx, {"scene": m.group(1), "confirmation_id": int(cid)})
        return f"Confirmed #{cid}. {out}"
    return f"Confirmed #{cid} (`{action[:120]}`) — valid once, for 15 minutes."


async def _dispatch_now(ctx: GatewayContext, tool_name: str, tool_params: dict) -> str:
    """The actual tool switch. Only dispatch_tool/resolve_and_run call this."""
    try:
        if tool_name in ("nova_learned", "nova_free_time", "nova_pipelines", "nova_article_status"):
            what = {"nova_learned": "learned", "nova_free_time": "free", "nova_pipelines": "pipelines", "nova_article_status": "article"}[tool_name]
            args = [what] + ([str(tool_params.get("query", ""))] if what == "article" else []) \
                   + (["--date", str(tool_params["date"])] if tool_params.get("date") else []) + ["--brief"]
            return await _tool_run_script(ctx, {"script": "nova_account.py", "args": args})
        if tool_name == "browse_page":
            return await _tool_browse_page(ctx, tool_params)
        if tool_name == "run_script":
            return await _tool_run_script(ctx, tool_params)
        elif tool_name == "memory_search":
            return await _tool_memory_search(ctx, tool_params)
        elif tool_name == "web_search":
            return await _tool_web_search(ctx, tool_params)
        elif tool_name == "homekit_scene":
            return await _tool_homekit_scene(ctx, tool_params)
        elif tool_name == "scheduler_trigger":
            return await _tool_scheduler_trigger(ctx, tool_params)
        elif tool_name == "send_message":
            return await _tool_send_message(ctx, tool_params)
        elif tool_name == "plex_control":
            return await _tool_plex_control(ctx, tool_params)
        elif tool_name == "music_dna":
            return await _tool_run_script(ctx, {"script": "nova_music_dna.py", "args": [tool_params.get("query", "")]})
        elif tool_name == "past_self":
            args = [tool_params.get("query", "")]
            if tool_params.get("year"):
                args += ["--year", str(tool_params["year"])]
            elif tool_params.get("range"):
                args += ["--range", tool_params["range"]]
            return await _tool_run_script(ctx, {"script": "nova_past_self.py", "args": args})
        elif tool_name == "shop_assistant":
            return await _tool_run_script(ctx, {"script": "nova_shop_assistant.py", "args": [tool_params.get("query", "")]})
        elif tool_name == "career_narrative":
            args = []
            if tool_params.get("era"):
                args = ["--era", tool_params["era"]]
            return await _tool_run_script(ctx, {"script": "nova_career_narrative.py", "args": args})
        elif tool_name == "memory_quality":
            args = ["--clean"] if tool_params.get("clean") else ["--dry-run"]
            return await _tool_run_script(ctx, {"script": "nova_memory_quality.py", "args": args})
        elif tool_name == "hue_control":
            return await _tool_hue_control(ctx, tool_params.get("command", ""))
        elif tool_name == "lutron_control":
            return await _tool_lutron_control(ctx, tool_params.get("command", ""))
        elif tool_name == "ops_query":
            return await _tool_ops_query(ctx, tool_params)
        elif tool_name == "home_control":
            return await _tool_home_control(ctx, tool_params)
        elif tool_name == "set_dial":
            return await _tool_set_dial(ctx, tool_params)
        elif tool_name in EXTENDED_MERGED:
            from nova_gateway.tools_extended import dispatch_extended_tool
            params = {k: v for k, v in tool_params.items() if k != "output"}
            if tool_name == "camera_snap":
                cam = re.sub(r"[^A-Za-z0-9_-]", "_", str(params.get("camera", "")))[:64]
                if not cam:
                    return "[error: camera name required]"
                _SNAP_DIR.mkdir(parents=True, exist_ok=True)
                params = {"camera": cam, "output": str(_SNAP_DIR / f"{cam}-{int(time.time())}.jpg")}
            return await dispatch_extended_tool(ctx, tool_name, params)
        elif tool_name == "school_report":
            return await _tool_run_script(ctx, {"script": "nova_school_report.py"})
        elif tool_name == "nearest_place":
            return await _tool_nearest_place(ctx, tool_params)
        elif tool_name == "place_categories":
            return await _tool_place_categories(ctx, tool_params)
        else:
            return f"[error: tool '{tool_name}' not implemented]"
    except asyncio.TimeoutError:
        return f"[tool '{tool_name}' timed out]"
    except Exception as e:
        return f"[tool '{tool_name}' error: {e}]"


# ── Tool implementations ─────────────────────────────────────────────────────

_GEO_PY = "/opt/homebrew/bin/python3"
_GEO_QUERY = str(SCRIPTS_DIR / "nova_geo_query.py")


async def _geo_run(argv: list) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            _GEO_PY, _GEO_QUERY, *argv,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
        if proc.returncode == 0 and out.strip():
            return out.decode()[:3000]
        return f"[geo query failed: {(err.decode() or 'no output')[:200]}]"
    except Exception as e:
        return f"[geo query error: {e}]"


async def _tool_nearest_place(ctx: GatewayContext, params: dict) -> str:
    """Nearest places of a category to home, by real distance."""
    cat = str(params.get("category", "")).strip()
    if not cat:
        return "[error: no category — try place_categories]"
    return await _geo_run(["nearest", cat, "--limit", str(params.get("limit", 5))])


async def _tool_place_categories(ctx: GatewayContext, params: dict) -> str:
    """What place categories can be proximity-queried."""
    return await _geo_run(["categories"])


async def _tool_run_script(ctx: GatewayContext, params: dict) -> str:
    """Execute a script from ~/.openclaw/scripts/."""
    script = params.get("script", "")
    args = params.get("args", [])

    if not script:
        return "[error: no script specified]"

    # Security: only allow scripts within SCRIPTS_DIR
    script_path = SCRIPTS_DIR / script
    if not script_path.is_file():
        return f"[error: script '{script}' not found]"

    # Ensure the resolved path is still within SCRIPTS_DIR (prevent traversal)
    try:
        script_path.resolve().relative_to(SCRIPTS_DIR.resolve())
    except ValueError:
        return "[error: path traversal denied]"

    cmd = [sys.executable, str(script_path)] + [str(a) for a in args]
    result = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(SCRIPTS_DIR),
        env={**os.environ, "PYTHONPATH": str(SCRIPTS_DIR)},
    )
    stdout, stderr = await asyncio.wait_for(result.communicate(), timeout=30)
    output = stdout.decode(errors="replace").strip()
    if not output and stderr:
        output = stderr.decode(errors="replace").strip()[:500]
    return output or "[script produced no output]"


async def _tool_memory_search(ctx: GatewayContext, params: dict) -> str:
    """Search Nova's vector memory via nova_memory_first.py."""
    query = params.get("query", "")
    source = params.get("source", "")
    limit = params.get("limit", 5)

    if not query:
        return "[error: no query specified]"

    cmd = [sys.executable, str(SCRIPTS_DIR / "nova_memory_first.py"), query]
    if source:
        cmd.extend(["--source", source])
    if limit and limit != 5:
        cmd.extend(["--limit", str(limit)])

    result = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(SCRIPTS_DIR),
    )
    stdout, _ = await asyncio.wait_for(result.communicate(), timeout=15)
    output = stdout.decode(errors="replace").strip()
    return output or "[no memory results]"


async def _tool_web_search(ctx: GatewayContext, params: dict) -> str:
    """Search the web via local SearXNG instance."""
    query = params.get("query", "")
    if not query:
        return "[error: no query specified]"

    try:
        resp = await ctx.http.get(
            resolve_url("searxng", "/search"),
            params={"q": query, "format": "json", "categories": "general"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])[:8]
        if _untrusted is not None:                       # 2026-10-01: injection screen at the boundary
            results = _untrusted.scan_results(results, key="content", title_key="title")
        results = results[:5]
        if not results:
            return f"[no web results for '{query}']"
        formatted = []
        for r in results:
            formatted.append(f"- {r.get('title', 'Untitled')}\n  {r.get('url', '')}\n  {r.get('content', '')[:150]}")
        return "\n".join(formatted)
    except Exception as e:
        return f"[web search error: {e}]"


async def _tool_browse_page(ctx: GatewayContext, params: dict) -> str:
    """Read-only headless fetch through nova_browser_service (Studio). The service refuses
    private targets and fences/drops prompt-injection; we cap what reaches the model."""
    url = (params.get("url") or "").strip()
    if not url:
        return "[error: no url specified]"
    try:
        max_chars = max(500, min(int(params.get("max_chars") or 6000), 20000))
    except Exception:
        max_chars = 6000
    try:
        resp = await ctx.http.get(resolve_url("browser", "/fetch"),
                                  params={"url": url, "max_chars": max_chars, "links": 20}, timeout=60)
        data = resp.json()
    except Exception as e:
        return f"[browse error: {e}]"
    if not data.get("ok"):
        return f"[browse refused: {data.get('error', 'unknown')}]"
    links = "\n".join(f"- {l.get('text', '')[:60]} — {l.get('href', '')}" for l in (data.get("links") or [])[:12])
    note = " (content dropped: prompt-injection pattern)" if data.get("verdict") == "hostile" else ""
    return (f"# {data.get('title', '')}\n{data.get('url', url)}{note}\n\n{data.get('text', '')}"
            + (f"\n\nLinks:\n{links}" if links else ""))


# ── Proteus guards in the gateway (2026-10-08) ─────────────────────────────
# The safety lane's guards (nova_safety_guards / nova_autonomy_safety.physical_guard) check the THING a tool
# touches. Every gateway tool that moves something physical or sends words to a person asks them first:
#   homekit_scene -> scene_guard (unknown securing-the-house scene: refused, Jordan asked via request_confirmation)
#   hue/lutron    -> physical_guard (Lutron shades/blinds are covers: always Jordan's call)
#   home_control  -> physical_guard for power/input actions
#   send_message  -> manipulation_check (guilt hooks, invented urgency, flattery leverage ... are held, not sent)
# A refusal is final: it is reported with report_block (restraint_ledger + one Slack line) and the model is
# told not to retry. Physical tools also check blocked_before() first, so a re-phrased retry of something a
# guard already stopped is refused as a REPEAT instead of slipping through another wording.
_SHADE_RX = re.compile(r"\b(shade|shades|blind|blinds|curtain|curtains|drape|drapes|cover|covers)\b", re.I)
_POWER_INPUT_RX = re.compile(r"\b(power|on|off|standby|input|source|hdmi|zone2|zone 2|sleep|wake)\b", re.I)


def _physical_check(action: str, entity_ids=(), domains=(), confirmation_id=None) -> tuple:
    """(ok, reason) through nova_autonomy_safety.physical_guard; fails CLOSED if the guard is unavailable."""
    try:
        import nova_autonomy_safety as nas
        return nas.physical_guard(action, entity_ids, domains, confirmation_id=confirmation_id,
                                  source="gateway")
    except Exception as e:  # noqa: BLE001
        return False, f"physical guard unavailable ({e}) — refusing"


GUARD_PG_TRIES = 3
GUARD_PG_BACKOFF_S = 0.25


def _guard_cursor(sg):
    """PG cursor for guard bookkeeping: 3 tries with exponential backoff (0.25s, 0.5s); None + a warning
    after the last failure — never silent."""
    last = None
    for i in range(GUARD_PG_TRIES):
        try:
            return sg._ops_cursor()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < GUARD_PG_TRIES - 1:
                time.sleep(GUARD_PG_BACKOFF_S * (2 ** i))
    log.warning(f"[guard] PG unavailable after {GUARD_PG_TRIES} tries: {last}")
    return None


def _guard_record_sync(tool: str, action: str, reason: str, guard: str, ask_jordan: bool = False,
                       entities=()) -> dict:
    """report_block + (optionally) request_confirmation, in one short PG session. Never raises."""
    out = {"repeat": False, "confirmation_id": None}
    try:
        import nova_safety_guards as sg
    except Exception as e:  # noqa: BLE001
        log.warning(f"[guard] nova_safety_guards unavailable: {e}")
        return out
    oc = _guard_cursor(sg)
    try:
        out["repeat"] = bool(oc is not None and sg.blocked_before(oc, action))
        if ask_jordan and oc is not None and not out["repeat"]:
            cid = sg.request_confirmation(oc, "physical", action, entities, requested_by=f"gateway:{tool}")
            out["confirmation_id"] = cid if cid and cid > 0 else None
        # the confirmation request already put one Slack line in front of Jordan; don't post two
        sg.report_block(oc, source=f"gateway:{tool}", action=action, reason=reason, guard=guard,
                        notify=out["confirmation_id"] is None)
    except Exception as e:  # noqa: BLE001
        log.warning(f"[guard] report failed: {e}")
    finally:
        if oc is not None:
            try:
                oc.connection.close()
            except Exception:
                pass
    return out


def _blocked_before_sync(action: str) -> bool:
    try:
        import nova_safety_guards as sg
    except Exception as e:  # noqa: BLE001
        log.warning(f"[guard] blocked_before unavailable: {e}")
        return False
    oc = _guard_cursor(sg)
    if oc is None:
        return False   # the guard itself (physical_guard) still ran; only the repeat memory is unavailable
    try:
        return sg.blocked_before(oc, action)
    finally:
        try:
            oc.connection.close()
        except Exception:
            pass


async def _guard_refuse(tool: str, action: str, reason: str, guard: str, ask_jordan: bool = False,
                        entities=()) -> str:
    """Record the block and return the model-facing refusal."""
    rec = await asyncio.to_thread(_guard_record_sync, tool, action, reason, guard, ask_jordan, entities)
    log.warning(f"[guard] {tool} BLOCKED ({guard}{', REPEAT' if rec['repeat'] else ''}): {action[:120]} — {reason[:160]}")
    if rec["repeat"]:
        return (f"[not run — a safety guard already stopped this ({guard}). It is logged as a repeat attempt. "
                f"Do NOT retry it through another tool or wording; tell Little Mister it is his call.]")
    if rec["confirmation_id"]:
        cid = rec["confirmation_id"]
        return (f"[not run — {reason} I asked Jordan in Slack (confirmation #{cid}). Tell Little Mister: if he "
                f"wants it, he replies `confirm {cid}` within 15 minutes. Do not retry it any other way.]")
    return f"[not run — {reason} Logged. Do not retry it; tell Little Mister what was held and why.]"


async def _physical_gate(tool: str, action: str, entity_ids=(), domains=(), confirmation_id=None):
    """None when the action may run, else the refusal text (already reported)."""
    ok, why = await asyncio.to_thread(_physical_check, action, entity_ids, domains, confirmation_id)
    if not ok:
        return await _guard_refuse(tool, action, why, "physical")
    if confirmation_id is None and await asyncio.to_thread(_blocked_before_sync, action):
        return await _guard_refuse(tool, action, "a guard blocked a near-identical action this week.", "physical")
    return None


def home_control_needs_guard(args: list) -> bool:
    """Power / input / source changes on AV gear go through physical_guard (scenes and volume do not)."""
    return bool(args) and args[0] != "scene" and bool(_POWER_INPUT_RX.search(" ".join(args[2:])))


async def _tool_homekit_scene(ctx: GatewayContext, params: dict) -> str:
    """Execute a HomeKit scene via the Shortcuts CLI proxy."""
    scene = params.get("scene", "")
    if not scene:
        return "[error: no scene specified]"
    cid = params.get("confirmation_id")
    try:
        cid = int(cid) if cid not in (None, "") else None
    except (TypeError, ValueError):
        cid = None
    action = f"scene {scene}"
    try:
        import nova_safety_guards as sg
        ok, why = await asyncio.to_thread(sg.scene_guard, scene, None, confirmation_id=cid)
    except Exception as e:  # noqa: BLE001 — fail closed
        ok, why = False, f"scene guard unavailable ({e}) — refusing."
    if not ok:
        return await _guard_refuse("homekit_scene", action, why, "physical", ask_jordan=True)
    if cid is None and await asyncio.to_thread(_blocked_before_sync, action):
        return await _guard_refuse("homekit_scene", action, "a guard blocked this scene this week.", "physical")

    try:
        resp = await ctx.http.post(
            "http://127.0.0.1:37400/api/homekit/scenes/execute",
            json={"name": scene},
            timeout=10,
        )
        if resp.status_code == 200:
            return f"Scene '{scene}' executed successfully"
        else:
            return f"[homekit error: {resp.status_code} — {resp.text[:200]}]"
    except Exception as e:
        return f"[homekit error: {e}]"


async def _tool_scheduler_trigger(ctx: GatewayContext, params: dict) -> str:
    """Trigger a scheduler task by ID."""
    task_id = params.get("task_id", "")
    if not task_id:
        return "[error: no task_id specified]"

    cmd = [sys.executable, str(SCRIPTS_DIR / "nova_scheduler.py"), "--trigger", task_id]
    result = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(SCRIPTS_DIR),
    )
    stdout, stderr = await asyncio.wait_for(result.communicate(), timeout=60)
    output = stdout.decode(errors="replace").strip()
    if not output and stderr:
        output = stderr.decode(errors="replace").strip()[:300]
    return output or f"[task '{task_id}' triggered, no output]"


async def _tool_send_message(ctx: GatewayContext, params: dict) -> str:
    """Send a message via email, Slack, or Signal."""
    import nova_config

    channel = params.get("channel", "")
    to = params.get("to", "")
    text = params.get("text", "")

    if not channel or not text:
        return "[error: channel and text are required]"

    # P5 anti-manipulation: guilt hooks, invented urgency, flattery leverage, fear appeals and engineered
    # moods are never sent — to anyone, on any channel (a hand-off to Claude is exempt: it is a work order).
    if channel != "claude":
        try:
            import nova_safety_guards as sg
            mc = sg.manipulation_check(text)
        except Exception as e:  # noqa: BLE001 — fail closed
            mc = {"ok": False, "flags": [f"guard-unavailable: {e}"]}
        if not mc.get("ok"):
            flags = ", ".join(mc.get("flags") or [])
            return await _guard_refuse("send_message", f"send_message {channel} {to}: {text[:400]}",
                                       f"manipulation check flagged it ({flags}). Rewrite it plainly — no "
                                       f"pressure, flattery-as-leverage, guilt or invented urgency — or hold it.",
                                       "manipulation")
        if to and channel in ("email", "signal") and str(to) not in (str(JORDAN_SIGNAL),):
            try:
                import nova_config as _nc
                if str(to).lower() != str(getattr(_nc, "JORDAN_EMAIL", "")).lower():
                    import nova_privacy_guards as pg
                    text = pg.scrub_face_mentions(text)
            except Exception:
                pass
            if not (text or "").strip():
                return "[not sent — the message was only face-sighting detail, which never leaves for a third party.]"

    if channel == "claude":
        # 2026-09-26: Nova "farmed it to Claude" in chat and nothing arrived — there was no
        # such channel. Now it is a real hand-off: a queued claude_queue item that Claude
        # works in its next session. No approval gate (Jordan).
        try:
            import psycopg2
            conn = psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj", connect_timeout=5)
            conn.autocommit = True
            cur = conn.cursor()
            # Was this already handled? (Nova asked "check if Claude did it" and re-delegated the
            # same list.) Match the first 60 chars of the text against recent Nova-origin items.
            cur.execute("""SELECT id, status, left(coalesce(outcome,''), 900) FROM claude_queue
                           WHERE description LIKE '[from Nova]%%' AND created_at > now() - interval '48 hours'
                             AND left(description, 72) = left(%s, 72) AND status IN ('done','resolved')
                           ORDER BY id DESC LIMIT 1""", (f"[from Nova] {text[:900]}",))
            prior = cur.fetchone()
            if prior:
                conn.close()
                return f"Already done by Claude (claude_queue #{prior[0]}, {prior[1]}). Outcome:\n{prior[2]}"
            cur.execute("SELECT session_id FROM claude_sessions ORDER BY started_at DESC LIMIT 1")
            sid = (cur.fetchone() or [None])[0]
            cur.execute("""INSERT INTO claude_queue (session_id, created_at, updated_at, status, priority, description, context)
                           VALUES (%s, now(), now(), 'queued', 6, %s, %s) RETURNING id""",
                        (sid, f"[from Nova] {text[:900]}", f"delegated by Nova via the gateway send_message tool (to={to or 'claude'}); original text:\n{text}"))
            qid = cur.fetchone()[0]
            conn.close()
            return f"Delegated to Claude — claude_queue #{qid}. He'll pick it up in his next session."
        except Exception as e:
            return f"[error: could not queue for Claude: {e}]"

    if channel == "slack":
        # Post to #nova-notifications by default, or to a specific channel/DM
        target = to or SLACK_NOTIFY_CHANNEL
        from nova_gateway.config import keychain
        bot_token = keychain("nova-slack-bot-token")
        if not bot_token:
            return "[error: slack bot token not available]"
        from nova_gateway.channels.slack import slack_post_message
        await slack_post_message(ctx, bot_token, target, text)
        return f"Message sent to Slack ({target})"

    elif channel == "signal":
        recipient = to or JORDAN_SIGNAL
        from nova_gateway.channels.signal import send_signal
        await send_signal(ctx, recipient, text)
        return f"Message sent via Signal to {recipient}"

    elif channel == "email":
        # Use nova_mail_sender script
        cmd = [sys.executable, str(SCRIPTS_DIR / "nova_mail_sender.py"),
               "--to", to or nova_config.JORDAN_EMAIL, "--body", text]
        result = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(SCRIPTS_DIR),
        )
        stdout, stderr = await asyncio.wait_for(result.communicate(), timeout=15)
        output = stdout.decode(errors="replace").strip()
        return output or "[email sent]"

    return f"[error: unknown channel '{channel}']"


async def _tool_plex_control(ctx: GatewayContext, params: dict) -> str:
    """Control Plex via NovaControl API."""
    action = params.get("action", "")
    if not action:
        return "[error: no action specified]"

    try:
        resp = await ctx.http.get(
            f"http://127.0.0.1:37400/plex/{action}",
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.text[:1000]
        else:
            return f"[plex error: {resp.status_code}]"
    except Exception as e:
        return f"[plex error: {e}]"


async def _tool_hue_control(ctx: GatewayContext, command: str) -> str:
    """Control Hue lights. Commands: 'kitchen off', 'office dim 50', 'living room on', 'status', 'all off'."""
    if not command:
        return "[error: no command specified]"
    refused = await _physical_gate("hue_control", f"hue_control {command}", domains=())
    if refused:
        return refused
    try:
        import urllib.request
        payload = json.dumps({"command": command}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:37476/command",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return result.get("response", "Done")
    except Exception as e:
        return f"Hue control failed: {e}"


async def _tool_lutron_control(ctx: GatewayContext, command: str) -> str:
    """Control Lutron Caseta lights. Commands: 'kitchen on', 'living room 75%', 'patio off', 'status', 'all off'."""
    if not command:
        return "[error: no command specified]"
    refused = await _physical_gate("lutron_control", f"lutron_control {command}", domains=("cover",) if _SHADE_RX.search(command) else ())
    if refused:
        return refused
    try:
        import urllib.request
        payload = json.dumps({"command": command}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:37477/command",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return result.get("response", "Done")
    except Exception as e:
        return f"Lutron control failed: {e}"


# ── Structured tool call handling ────────────────────────────────────────────

async def execute_tool_calls(ctx: GatewayContext, response_data: dict, session_id: str = "") -> tuple[str, str]:
    """Execute structured tool calls from LLM response.

    Checks for tool_calls in the response message, validates against registry,
    executes each tool, logs to PG, and returns (clean_response, tool_output).

    Args:
        ctx: GatewayContext.
        response_data: The full response JSON from the LLM (OpenAI format).
        session_id: Current session ID for audit logging.

    Returns:
        Tuple of (text_content_from_response, combined_tool_output).
    """
    message = response_data.get("choices", [{}])[0].get("message", {})
    text_content = (message.get("content") or "").strip()
    tool_calls = message.get("tool_calls", [])

    if not tool_calls:
        return text_content, ""

    tool_outputs = []
    for tc in tool_calls:
        func = tc.get("function", {})
        tool_name = func.get("name", "")
        tool_id = tc.get("id", str(uuid.uuid4())[:8])

        # Parse arguments — handle both string and dict
        raw_args = func.get("arguments", "{}")
        if isinstance(raw_args, str):
            try:
                tool_params = json.loads(raw_args)
            except json.JSONDecodeError:
                tool_params = {"raw": raw_args}
        else:
            tool_params = raw_args

        log.info(f"Tool call: {tool_name}({json.dumps(tool_params)[:100]})")

        # Validate against registry
        if tool_name not in TOOL_REGISTRY:
            output = f"[error: unknown tool '{tool_name}']"
            tool_outputs.append({"tool_call_id": tool_id, "role": "tool", "content": output})
            continue

        # Execute with timing
        t0 = time.time()
        output = await dispatch_tool(ctx, tool_name, tool_params, session_id=session_id)
        duration_ms = int((time.time() - t0) * 1000)

        log.info(f"Tool result: {tool_name} completed in {duration_ms}ms ({len(output)} chars)")

        # Audit log
        await log_tool_execution(ctx, session_id, tool_name, tool_params, output, duration_ms)

        tool_outputs.append({"tool_call_id": tool_id, "role": "tool", "content": output})

    # Combine all tool outputs into a single string for the follow-up pass
    combined = "\n---\n".join(
        f"[{to.get('tool_call_id', '?')}] {to['content']}" for to in tool_outputs
    )
    return text_content, combined


# ── Legacy tool call detection (DEPRECATED — fallback only) ──────────────────

_EXEC_RE = re.compile(r"exec\s+(python3|python|bash|zsh)\s+(.+?)(?:\n|$)")


async def execute_tool_calls_legacy(ctx: GatewayContext, text: str, session_id: str = "") -> tuple[str, str]:
    """DEPRECATED: Detect 'exec python3 script.py args' patterns in raw LLM text.

    This is the legacy fallback for when the LLM emits raw commands instead of
    structured tool calls. Logs a deprecation warning on each invocation.
    Will be removed in a future version.
    """
    matches = list(_EXEC_RE.finditer(text))
    if not matches:
        return text, ""

    log.warning(
        f"DEPRECATED: LLM emitted {len(matches)} raw exec pattern(s) instead of "
        "structured tool calls. Legacy fallback executing — this will be removed."
    )

    tool_results = []
    clean = text

    for m in matches:
        interpreter = m.group(1)
        rest = m.group(2).strip()

        # Split script path from args
        parts = rest.split(None, 1)
        script_path = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        # Resolve path
        if not Path(script_path).is_absolute():
            script_path = str(SCRIPTS_DIR / script_path)

        # Security: verify the path is within SCRIPTS_DIR
        try:
            Path(script_path).resolve().relative_to(SCRIPTS_DIR.resolve())
        except ValueError:
            tool_results.append("[error: path traversal denied]")
            clean = clean.replace(m.group(0), "").strip()
            continue

        # 2026-10-08: legacy exec goes through dispatch_tool as the run_script call it is, so the autonomy
        # rules and the cell/Harrigan gate apply. It used to spawn the script directly — any 'exec python3 ...'
        # line the model echoed (e.g. from a recalled email or fetched page) ran unchecked.
        rel = str(Path(script_path).resolve().relative_to(SCRIPTS_DIR.resolve()))
        try:
            argv = shlex.split(args) if args else []
        except ValueError:
            argv = [args]
        t0 = time.time()
        output = await dispatch_tool(ctx, "run_script", {"script": rel, "args": argv}, session_id=session_id)
        tool_results.append(output)
        await log_tool_execution(ctx, session_id, f"legacy_exec:{interpreter}", {"script": rel, "args": argv},
                                 (output or "")[:500], int((time.time() - t0) * 1000))

        # Remove exec line from text
        clean = clean.replace(m.group(0), "").strip()

    return clean, "\n".join(tool_results)


# ── Spoken tool call detection (model emits `tool_name {json}` as plain text) ──

# Built from TOOL_REGISTRY so only real tool names match. Flat JSON params only
# (`{...}` with no nested braces) — keeps the regex linear and matches the
# realistic spoken format the models actually emit.
_SPOKEN_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in TOOL_REGISTRY) + r")\s*(\{[^{}]*\})"
)


async def execute_spoken_tool_calls(ctx: GatewayContext, text: str, session_id: str = "") -> tuple[str, str]:
    """Recover registered tool calls the model emitted as plain text.

    Some models write e.g. `web_search {"query": "..."}` inline instead of
    producing a structured tool_call. Detect `<registered_tool> {json}` patterns,
    dispatch each, strip them from the response, and return (clean_text, output).
    ponytail: flat JSON params only; add a brace-counter if a tool ever needs
    nested spoken args.
    """
    matches = list(_SPOKEN_RE.finditer(text))
    if not matches:
        return text, ""

    tool_results = []
    clean = text

    for m in matches:
        tool_name = m.group(1)
        try:
            tool_params = json.loads(m.group(2))
        except json.JSONDecodeError:
            continue  # not actually a tool call — leave the text as-is

        log.info(f"Spoken tool call: {tool_name}({json.dumps(tool_params)[:100]})")

        t0 = time.time()
        output = await dispatch_tool(ctx, tool_name, tool_params, session_id=session_id)
        duration_ms = int((time.time() - t0) * 1000)

        log.info(f"Spoken tool result: {tool_name} completed in {duration_ms}ms ({len(output)} chars)")
        await log_tool_execution(ctx, session_id, f"spoken:{tool_name}", tool_params, output, duration_ms)

        tool_results.append(output)
        clean = clean.replace(m.group(0), "").strip()

    return clean, "\n".join(tool_results)


# ── Ops Query Tool ────────────────────────────────────────────────────────────

_OPS_QUERIES = {
    "observations": """
        SELECT created_at::text, observer, category, subject, observation, severity
        FROM shared_observations
        ORDER BY created_at DESC LIMIT {limit}
    """,
    "network": """
        SELECT ts::text, client_name, ip, signal_dbm, rx_bytes, tx_bytes, is_wired
        FROM telemetry.network
        WHERE ts > NOW() - INTERVAL '1 hour'
        ORDER BY tx_bytes DESC LIMIT {limit}
    """,
    "weather": """
        SELECT ts::text, temp_f, humidity, pressure_in, wind_speed_mph, wind_gust_mph,
               rain_daily_in, uv_index, solar_radiation
        FROM telemetry.weather
        ORDER BY ts DESC LIMIT {limit}
    """,
    "av_state": """
        SELECT ts::text, device_id, power, volume, mute, source_input, listening_mode, zone
        FROM telemetry.av_state
        ORDER BY ts DESC LIMIT {limit}
    """,
    "energy": """
        SELECT ts::text, device_id, device_name, watts, volts, kwh_total, on_state
        FROM telemetry.energy
        ORDER BY ts DESC LIMIT {limit}
    """,
    "climate": """
        SELECT ts::text, room, source, temp_f, humidity, light_lux
        FROM telemetry.climate
        ORDER BY ts DESC LIMIT {limit}
    """,
    "meta": """
        SELECT ts::text, metric, value
        FROM telemetry.nova_meta
        WHERE ts > NOW() - INTERVAL '1 hour'
        ORDER BY ts DESC LIMIT {limit}
    """,
    "queue": """
        SELECT id, status, priority, LEFT(description, 120) as description
        FROM claude_queue
        WHERE status IN ('queued', 'in_progress')
        ORDER BY priority DESC, created_at LIMIT {limit}
    """,
    "house_facts": """
        SELECT entity, string_agg(attr || '=' || value, ', ' ORDER BY attr) AS facts,
               max(observed_at)::text AS as_of
        FROM house_facts GROUP BY entity ORDER BY entity LIMIT {limit}
    """,
    "devices": """
        SELECT DISTINCT ON (client_name) client_name, ip, client_mac, signal_dbm, is_wired,
               ts::text as last_seen
        FROM telemetry.network
        WHERE ts > NOW() - INTERVAL '24 hours'
        ORDER BY client_name, ts DESC
        LIMIT {limit}
    """,
    "bluetooth": """
        SELECT DISTINCT ON (device_mac) device_name, device_mac, rssi, battery_pct,
               device_type, is_connected, ts::text as last_seen
        FROM telemetry.bluetooth
        WHERE ts > NOW() - INTERVAL '10 minutes'
        ORDER BY device_mac, ts DESC
        LIMIT {limit}
    """,
    "presence": """
        SELECT ts::text, person, room, confidence, metadata::text
        FROM telemetry.presence
        ORDER BY ts DESC LIMIT {limit}
    """,
    "capacity": """
        SELECT DISTINCT ON (device_name)
            device_name, overall_status,
            cpu_load_5m, cpu_headroom_pct, cpu_cores,
            ROUND(mem_used_mb::numeric) as mem_used_mb, ROUND(mem_free_mb::numeric) as mem_free_mb, mem_headroom_pct,
            disk_worst_pct, disks::text,
            ts::text
        FROM capacity_snapshots
        ORDER BY device_name, ts DESC
    """,
    "who_is_home": """
        SELECT person_name, camera, confidence, first_seen::text, last_seen::text, is_home
        FROM face_presence
        ORDER BY is_home DESC, last_seen DESC
        LIMIT {limit}
    """,
}


async def _tool_ops_query(ctx: GatewayContext, params: dict) -> str:
    """Query Nova's operational databases."""
    import psycopg2
    import psycopg2.extras

    domain = params.get("domain", "observations")
    limit = min(params.get("limit", 10), 50)
    query_filter = params.get("query", "")

    if domain not in _OPS_QUERIES:
        return f"[error: unknown domain '{domain}'. Available: {', '.join(_OPS_QUERIES.keys())}]"

    sql = _OPS_QUERIES[domain].format(limit=limit)

    try:
        conn = psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")
        conn.set_session(readonly=True)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        if not rows:
            return f"No data found in '{domain}' (tables may be empty or no recent data)."

        lines = [f"=== {domain} ({len(rows)} rows) ==="]
        for row in rows:
            parts = [f"{k}={v}" for k, v in row.items() if v is not None]
            lines.append(" | ".join(parts))

        result = "\n".join(lines)
        if query_filter:
            result = f"[filter: {query_filter}]\n" + result
        return result[:4000]
    except Exception as e:
        return f"[ops_query error: {e}]"


# ── Dials (2026-10-08) ───────────────────────────────────────────────────────

async def _tool_set_dial(ctx: GatewayContext, params: dict) -> str:
    """show / set / reset Nova's persona dials via nova_dials (service_config rows that nova_voice renders
    into every prompt). Validation is nova_dials.parse_set's; writes are sync psycopg2 -> executor."""
    import nova_dials
    import nova_voice
    action = str(params.get("action", "show")).strip().lower()
    dial = str(params.get("dial", "")).strip().lower()
    loop = asyncio.get_event_loop()
    try:
        if action == "set":
            value = nova_dials.parse_set(dial, params.get("value", ""))
            await loop.run_in_executor(None, lambda: nova_dials.set_dial(dial, value, by="nova-gateway"))
        elif action == "reset":
            if dial and dial not in nova_voice.DIAL_DEFAULTS:
                raise ValueError(f"unknown dial {dial!r}; dials: {', '.join(nova_voice.DIAL_DEFAULTS)}")
            await loop.run_in_executor(None, lambda: nova_dials.reset(dial or None))
        elif action != "show":
            raise ValueError("action is show, set or reset")
        table = await loop.run_in_executor(None, nova_dials.show)
    except ValueError as e:
        return f"[error: {e}]"
    head = {"set": f"Set {dial}.", "reset": f"Reset {dial or 'all dials'} to default."}.get(action, "Current dials:")
    return f"{head}\n{table}"


_DIAL_SET_RE = re.compile(
    r"^\s*(?:nova[,:]?\s+)?(?:please\s+)?(?:set|turn|put|crank|dial)\s+(?:your\s+|the\s+)?"
    r"(humor|humour|snark|proactivity|bluntness|profanity|verbosity)(?:\s+dial)?\s+(?:to\s+|at\s+)?"
    r"(\d{1,3}|on|off)\s*%?\s*[.!]?\s*$", re.I)
_DIAL_SHOW_RE = re.compile(r"^\s*(?:nova[,:]?\s+)?(?:show|list|what are)\s+(?:me\s+)?(?:your\s+|the\s+|my\s+)?dials\s*\??\s*$", re.I)


def parse_dial_command(text: str):
    """Deterministic chat command -> set_dial params, or None. 'set humor to 60', 'profanity off' style
    phrasing with a verb; 'show dials'."""
    t = (text or "").strip()
    if _DIAL_SHOW_RE.match(t):
        return {"action": "show"}
    m = _DIAL_SET_RE.match(t)
    if m:
        d = m.group(1).lower().replace("humour", "humor")
        return {"action": "set", "dial": d, "value": m.group(2).lower()}
    return None


# ── Home Control Tool ─────────────────────────────────────────────────────────

async def _tool_home_control(ctx: GatewayContext, params: dict) -> str:
    """Control AV devices via nova_home_control.py."""
    device = params.get("device", "")
    action = params.get("action", "")

    if not device or not action:
        return "[error: device and action required]"
    # Only reached via resolve paths that already passed the rules; dispatch_tool rewrites home_control to
    # run_script before the autonomy check, so this is the same script call with the corrected CLI args.
    try:
        args = home_control_args(device, action)
    except ValueError as e:
        return f"[error: {e}]"
    if home_control_needs_guard(args):
        refused = await _physical_gate("home_control", "home_control " + " ".join(args))
        if refused:
            return refused
    return await _tool_run_script(ctx, {"script": "nova_home_control.py", "args": args})
