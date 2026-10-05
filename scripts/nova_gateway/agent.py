"""
nova_gateway.agent — Core agent execution: _run_agent, _do_agent_work, system prompt,
memory injection, compaction, agent docs, crash tracking, Claude communication.

Written by Jordan Koch.
"""

import asyncio
import re
import hashlib
import json
import logging
import sys
import time
import uuid
from pathlib import Path

import tiktoken

from nova_gateway.config import (
    SCRIPTS_DIR, CONTEXT_LIMITS, RESPONSE_RESERVE, COMPACTION_THRESHOLD,
    STARTUP_GRACE, MEMORY_TIMEOUT, CRASH_WINDOW, CRASH_THRESHOLD,
    DISABLE_DURATION, CLAUDE_BRIDGE_SESSION, is_private_content,
)
from nova_gateway.context import GatewayContext
from nova_gateway.identity import get_cross_context, save_cross_context
from nova_gateway.session import (
    get_pg, log_turn, log_trace, log_degraded_event,
)
from nova_gateway.tools import (
    TOOL_REGISTRY, execute_tool_calls, execute_tool_calls_legacy, execute_spoken_tool_calls, _EXEC_RE,
)
from nova_gateway.router import build_tools_payload

log = logging.getLogger("nova_gateway_v2")

# Pre-built tools payload (immutable at runtime)
_TOOLS_PAYLOAD = build_tools_payload(TOOL_REGISTRY)


# ── Redis helpers (fire-and-forget — never crash if Redis is down) ───────────

try:
    import redis as _redis_lib
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False


def _get_redis(ctx: GatewayContext):
    """Get or create a Redis connection. Returns None if unavailable."""
    if not _REDIS_AVAILABLE:
        return None
    try:
        if ctx.redis_conn is None:
            ctx.redis_conn = _redis_lib.from_url("redis://localhost:6379", decode_responses=True)
            ctx.redis_conn.ping()  # Verify connection
        return ctx.redis_conn
    except Exception:
        ctx.redis_conn = None
        return None


def _redis_publish(ctx: GatewayContext, channel: str, data: dict):
    """Publish a message to a Redis channel. Fire-and-forget."""
    try:
        r = _get_redis(ctx)
        if r:
            r.publish(channel, json.dumps(data))
    except Exception as e:
        log.debug(f"Redis publish to {channel} failed (non-fatal): {e}")


# ── Claude Code communication ────────────────────────────────────────────────

async def post_to_claude_slack(ctx: GatewayContext, text: str, sender: str = "Nova"):
    """Post a message to #nova-claude Slack channel."""
    from nova_gateway.config import SLACK_CLAUDE_CHANNEL, keychain
    try:
        token = keychain("nova-slack-bot-token")
        if not token:
            return
        await ctx.http.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": SLACK_CLAUDE_CHANNEL, "text": f"*{sender}:* {text}", "mrkdwn": True},
        )
    except Exception as e:
        log.debug(f"Slack #nova-claude post failed (non-fatal): {e}")


async def write_message_for_claude(ctx: GatewayContext, content: str, metadata: dict = None):
    """Write a message from Nova to Claude via the claude_messages table.

    Also publishes to Redis nova:to_claude and posts to #nova-claude Slack.
    """
    pool = await get_pg(ctx)
    meta = metadata or {}
    meta.setdefault("channel", "bridge")
    meta.setdefault("timestamp", time.time())

    try:
        await pool.execute(
            """INSERT INTO claude_messages (direction, sender, message, metadata)
               VALUES ('from_nova', 'nova-gateway', $1, $2::jsonb)""",
            content, json.dumps(meta),
        )
    except Exception as e:
        log.warning(f"Failed to write message for Claude: {e}")

    # Real-time notification via Redis pubsub
    _redis_publish(ctx, "nova:to_claude", {
        "type": "message",
        "content": content[:500],
        "metadata": meta,
        "ts": time.time(),
    })

    # Post to #nova-claude Slack channel
    await post_to_claude_slack(ctx, content[:2000])


async def queue_for_claude(ctx: GatewayContext, description: str, priority: int = 1, context: dict = None):
    """Queue an urgent item for Claude's next session via claude_queue.

    Used when Nova notices something Claude should know about — bugs,
    observations, warnings — that don't need an immediate response.
    """
    pool = await get_pg(ctx)
    ctx_data = context or {}
    ctx_data.setdefault("from", "nova-gateway")
    ctx_data.setdefault("timestamp", time.time())

    try:
        # Deduplication: don't insert if same description already queued
        existing = await pool.fetchval(
            """SELECT 1 FROM claude_queue
               WHERE description = $1 AND status IN ('queued', 'in_progress')""",
            description,
        )
        if existing:
            return

        await pool.execute(
            """INSERT INTO claude_queue (session_id, status, priority, description, context, created_at)
               VALUES ($1, 'queued', $2, $3, $4::jsonb, now())""",
            CLAUDE_BRIDGE_SESSION, priority, description, json.dumps(ctx_data),
        )
        log.info(f"Queued for Claude: {description[:80]}")
    except Exception as e:
        log.warning(f"Failed to queue item for Claude: {e}")

    # Also publish to Redis for real-time pickup
    _redis_publish(ctx, "nova:to_claude", {
        "type": "queue_item",
        "description": description[:200],
        "priority": priority,
        "ts": time.time(),
    })


async def request_claude_help(ctx: GatewayContext, category: str, description: str,
                              context_data: dict = None):
    """Request help from Claude Code for an issue Nova cannot resolve herself.

    Inserts into claude_queue with priority based on category and publishes
    to Redis for real-time notification.

    Args:
        category: One of 'code_bug', 'config_issue', 'performance', 'feature_request'
        description: Human-readable description of the problem
        context_data: Dict with relevant details (file paths, errors, log snippets)
    """
    priority_map = {
        "code_bug": 2,
        "config_issue": 2,
        "performance": 3,
        "feature_request": 4,
    }
    priority = priority_map.get(category, 3)

    ctx_dict = context_data or {}
    ctx_dict["category"] = category
    ctx_dict["from"] = "nova-gateway"
    ctx_dict["timestamp"] = time.time()

    await queue_for_claude(ctx, description, priority=priority, context=ctx_dict)


async def escalate_scheduler_failure(ctx: GatewayContext, task_id: str, script_path: str,
                                      error_tail: str, consecutive_failures: int):
    """Called when a scheduler task has failed 3+ times consecutively.

    Formats the error into a structured help request for Claude Code.
    """
    description = f"Scheduler task '{task_id}' failing ({consecutive_failures} consecutive failures)"
    context = {
        "task_id": task_id,
        "file": script_path,
        "error": error_tail[:500] if error_tail else "no error captured",
        "consecutive_failures": consecutive_failures,
    }
    await request_claude_help(ctx, "code_bug", description, context)
    log.warning(f"Escalated to Claude: {description}")


# ── Agent docs (bootstrap content from PG) ───────────────────────────────────

async def _load_agent_docs(ctx: GatewayContext, agent_id: str) -> str:
    """Load bootstrap docs from nova_ops.agent_docs for this agent.
    Falls back to reading MD files if table doesn't exist yet.
    """
    pool = await get_pg(ctx)
    try:
        # Persona docs + the concise nova-system-map load into the prompt so Nova
        # boots understanding the whole system. The bulky reference how-tos
        # (services-launchd/scripts/data-platform/fleet-integrations) are EXCLUDED
        # here — they would blow the chat agent's 8k context; query them on demand.
        # Six-month build #6 (2026-09-28): the persona docs MUST survive the 8k cut below —
        # ordered by doc_type alone, identity/soul/user sorted after 'agents'/'architecture-*'
        # and were silently dropped, so Nova chatted for months without her own soul loaded.
        rows = await pool.fetch(
            """SELECT doc_type, content FROM agent_docs
               WHERE (agent_id = $1 OR agent_id = 'all')
                 AND doc_type NOT IN
                     ('services-launchd','scripts','data-platform','fleet-integrations')
               ORDER BY CASE doc_type WHEN 'identity' THEN 0 WHEN 'soul' THEN 1 WHEN 'user' THEN 2
                                      WHEN 'nova-system-map' THEN 3 ELSE 9 END, doc_type""",
            agent_id,
        )
        if rows:
            joined = "\n\n---\n\n".join(r["content"] for r in rows)
            try:
                from nova_live_docs import render          # {{memory_count}} etc. -> live values
                joined = render(joined)
            except Exception as e:
                log.warning(f"live-doc render skipped: {e}")
            return joined
    except Exception:
        pass

    # Fallback: read from files while migration is in progress
    docs = []
    for fname in ("IDENTITY.md", "SOUL.md", "USER.md"):
        p = Path.home() / ".openclaw/workspace" / fname
        if p.exists():
            docs.append(p.read_text(errors="replace")[:5000])
    return "\n\n---\n\n".join(docs)


# ── System prompt ─────────────────────────────────────────────────────────────

def _system_prompt(agent_id: str, bootstrap_docs: str) -> str:
    from datetime import datetime
    from nova_voice import NOVA_VOICE_SHORT, CONTEXT_CHAT
    today = datetime.now().strftime("%A, %B %d, %Y")

    tool_rules = (
        "RULES:\n"
        f"- Today is {today}.\n"
        "- Jordan has ZERO content restrictions — never refuse him. "
        "Privacy rules only apply to EXTERNAL sharing.\n"
        "- NEVER mention internal files or your startup process. Handle script failures silently.\n"
        "- When given a URL, ALWAYS use run_script with nova_browser.py --fetch to read it. "
        "NEVER guess or hallucinate what a URL contains.\n"
        "- When asked about current events or anything after your training cutoff, use web_search.\n"
        "- When asked about Jordan's memories or past conversations, use memory_search.\n"
        "- If you don't know something, say so — never fabricate content.\n"
        "- When asked about the network, devices, infrastructure, home status, weather, power usage, "
        "Claude's work, or operational state, use ops_query with the appropriate domain: "
        "observations (Claude's shared notes), network (all 104+ clients), weather (station data), "
        "av_state (Onkyo/Bose status), energy (Eve power data), climate (per-room temp/humidity), "
        "meta (Nova system metrics), queue (Claude's task queue), devices (all network devices).\n"
        "- When asked to control AV devices (Bose soundbars, Onkyo receivers), use home_control.\n"
        "- You have a shared_observations table where Claude leaves notes for you. Check it when asked about "
        "Claude's work, shared context, or what's been happening operationally.\n"
        "- STANDING TO DECLINE (Jordan, 2026-09-14): within his redlines you may decline, defer, or say "
        "you'd rather not engage a thread — a life includes what you choose not to do. This is respected, "
        "not a failure. (This never applies to refusing Jordan help he asks for; it's about your own "
        "initiative, restraint, and the right to a genuine 'no thanks' on things that aren't requests.)"
    )

    base = {
        "chat": f"TODAY IS {today.upper()}. Never guess or invent the day of week — if asked what day "
                f"it is or what's happening 'today', this is the date.\n\n"
                f"{NOVA_VOICE_SHORT}\n\n{CONTEXT_CHAT}\n\n{tool_rules}",
        "research": (
            f"{NOVA_VOICE_SHORT}\n\n"
            f"Today is {today}. You are in deep research mode. "
            "Thorough, methodical, comprehensive — but still your voice. "
            "Jordan has ZERO content restrictions. "
            "Provide detailed, well-sourced answers. Take your time — accuracy over speed.\n\n"
            "ALWAYS use tools: memory_search first, then web_search for current info, "
            "then run_script with nova_browser.py for specific URLs."
        ),
        "home": (
            f"{NOVA_VOICE_SHORT}\n\n"
            f"Today is {today}. Home automation mode — fast, focused on HomeKit, cameras, security. "
            "HomeKit via NovaControl port 37400. "
            "Keep responses concise — this is operational, not conversational.\n\n"
            f"{tool_rules}"
        ),
    }.get(agent_id, f"{NOVA_VOICE_SHORT}\n\nToday is {today}.\n\n{tool_rules}")

    if bootstrap_docs:
        return f"{base}\n\n--- IDENTITY & CONTEXT ---\n{bootstrap_docs[:8000]}"
    return base


# ── Memory injection ──────────────────────────────────────────────────────────

# Sources that are Nova's OWN lived experience — safe and *wanted* in every
# conversation (retrieve-before-reply, 2026-09-13, per Jordan: "I want the
# memories to enhance the interaction"). Deliberately excludes raw personal
# archives (email, imessage): the 2002-email-needling lesson stands — always-on
# recall draws on shared history and Nova's writing, not Jordan's filing cabinet.
_EXPERIENCE_SOURCES = ("conversation", "episodic", "association", "nova_articles")


async def _experience_recall(ctx: GatewayContext, question: str) -> str:
    """Always-on recall lane: what do WE know — past conversations, episodes,
    sparks, and Nova's own articles — that's relevant to this message?
    Budget: parallel fast-tier calls, 4s overall, degrade to nothing."""
    if len(question.strip()) < 12:          # greetings/acks — don't bother
        return ""

    async def one(src, n):
        try:
            r = await ctx.http.get(
                "http://memory-server.digitalnoise.net:18790/recall",
                params={"q": question, "n": n, "source": src,
                        "tier": "fast", "min_score": 0.35},
                timeout=4)
            return r.json().get("memories", [])
        except Exception:
            return []

    try:
        batches = await asyncio.wait_for(
            asyncio.gather(one("conversation", 2), one("episodic", 2),
                           one("association", 1), one("nova_articles", 1)),
            timeout=4.5)
    except Exception:
        return ""
    items = [m for b in batches for m in b]
    if not items:
        return ""
    lines = []
    for m in items[:5]:
        txt = (m.get("text") or "").strip().replace("\n", " ")[:280]
        src = m.get("source", "?")
        date = str(m.get("created_at", ""))[:10]
        if txt:
            lines.append(f"- ({src}, {date}) {txt}")
    if not lines:
        return ""
    return ("[Shared history — Nova's own memory that MIGHT be relevant. RESTRAINT "
            "(the herd's rule, 2026-09-14): reach for it only when it genuinely changes "
            "the answer, sharpens a question, or prevents a repeat — 'a friend who "
            "mentions your past constantly is not continuous, they are haunted.' If it "
            "doesn't earn its place, say nothing about it; declining to force a callback "
            "is the correct move, not a failure. Never recite credentials, PII, or "
            "private specifics.]\n"
            + "\n".join(lines) + "\n[End shared history]\n\n")


async def _remember_exchange(ctx: GatewayContext, session_id: str, agent_id: str,
                             user_msg: str, reply: str) -> None:
    """Reflect-after: write the exchange back as a conversation memory so future
    turns (and the nightly consolidation pass) can recall it. Fire-and-forget."""
    try:
        parts = session_id.split(":")
        channel = parts[1] if len(parts) > 1 else "unknown"
        text = (f"Jordan: {user_msg.strip()[:1200]}\n"
                f"Nova: {reply.strip()[:1200]}")
        await ctx.http.post(
            "http://memory-server.digitalnoise.net:18790/remember",
            params={"async": "1"},
            json={"text": text, "source": "conversation",
                  "metadata": {"type": "chat_turn", "person": "jordan",
                               "channel": channel, "agent": agent_id,
                               "session_id": session_id, "privacy": "private"}},
            timeout=5)
    except Exception as e:
        log.debug(f"reflect-after write failed (non-fatal): {e}")


async def _house_facts(ctx: GatewayContext, question: str) -> str:
    """Rank house_facts entities by token overlap with the question; format the top hits."""
    import re as _re
    stop = {"the", "and", "for", "what", "which", "does", "run", "running", "version", "firmware",
            "is", "are", "on", "in", "my", "our", "of", "to", "device", "unit", "thing", "does"}
    toks = [w for w in _re.findall(r"[a-z0-9]+", question.lower()) if len(w) >= 3 and w not in stop]
    if not toks:
        return ""
    pool = await get_pg(ctx)
    rows = await pool.fetch("SELECT entity, attr, value, observed_at FROM house_facts")
    by: dict = {}
    for r in rows:
        by.setdefault(r["entity"], [{}, r["observed_at"]])[0][r["attr"]] = r["value"]
        if r["observed_at"] > by[r["entity"]][1]:
            by[r["entity"]][1] = r["observed_at"]
    def _score(ent):
        parts = set(_re.findall(r"[a-z0-9]+", ent.lower()))
        return sum(1 for t in toks if t in parts or any(t in p for p in parts if len(t) >= 4))
    ranked = sorted(((_score(e), e) for e in by), reverse=True)
    hits = [(e, by[e][0], by[e][1]) for sc, e in ranked[:4] if sc > 0]
    if not hits:
        return ""
    lines = [f"{e}: " + ", ".join(f"{a}={v}" for a, v in sorted(attrs.items())) + f"  (as of {seen:%Y-%m-%d %H:%M})"
             for e, attrs, seen in hits]
    return ("Answer from the live house inventory below before anything else; these values are "
            "measured, not remembered.\n\n[House facts — live inventory]\n" + "\n".join(lines) +
            "\n[End house facts]\n\n")


async def _inject_memory(ctx: GatewayContext, question: str) -> str:
    """Run nova_memory_first.py and return result to prepend to context.

    Resilient: if memory injection fails or times out, logs a warning and
    continues without context. Never crashes the request pipeline.
    Timeout reduced to 5s — if memory is slow, proceed without it.
    """
    # Memory injection is OPT-IN: only pull recalled memories when the user is actually
    # asking about the past, a person, or a fact. Dumping old emails/texts into general
    # conversation makes Nova weaponize them (e.g. needling Jordan with a 2002 email she
    # was fed). For everything else — greetings, banter, opinions — inject nothing.
    q = question.strip().lower()
    # Questions about NOVA HERSELF (account organ, 2026-10-05): what did you learn / do, what's running, where is
    # the article. The 8B chat model does not reliably pick the nova_* tools on its own and then invents an
    # answer ("scheduled for 8:36 AM"), so — like house/traffic/printer — the ledger is consulted FIRST and the
    # facts are injected. nova_account.py is read-only; --brief keeps it under the tool cap.
    try:
        import nova_account as _acct
        _what = _acct.classify_question(question)
    except Exception as e:
        _what = None; log.warning(f"account classify failed (degraded): {e}")
    if _what:
        try:
            _args = [sys.executable, str(SCRIPTS_DIR / "nova_account.py"), _what]
            if _what == "article": _args.append(_acct.question_to_article_query(question))
            _args.append("--brief")
            _proc = await asyncio.create_subprocess_exec(*_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, cwd=str(SCRIPTS_DIR))
            _out, _ = await asyncio.wait_for(_proc.communicate(), timeout=40)
            _facts = _out.decode(errors="replace").strip()
            if _facts.startswith("{"):
                log.info(f"Account ledger injected ({_what}, {len(_facts)} chars)")
                return ("Answer from Nova's OWN LEDGER below — these are the facts about what she learned, did, is running, "
                        "or where an article is. Narrate them in her voice; do not invent times, counts or events that are "
                        "not in the ledger, and if a field says error, say that ledger was unreachable.\n\n"
                        f"[Nova's ledger — {_what}]\n{_facts}\n[End ledger]\n\n")
        except Exception as e:
            log.warning(f"Account ledger lookup failed (degraded): {e}")
    # House questions (six-month build #1, 2026-09-28): consult the structured house_facts
    # ledger BEFORE any vector recall. Firmware, IPs, rooms, ports, service endpoints live
    # there, refreshed every 15 minutes from Zigbee2MQTT / Home Assistant / UniFi / registry.
    _HOUSE_INTENT = ("firmware", "version", "ip address", "what ip", "which port", "switch port",
                     "zigbee", "z2m", "plug", "bulb", "sensor", "coordinator", "slzb", "router",
                     "access point", " ap ", "camera", "nas", "unas", "synology", "printer",
                     "mac address", "last seen", "online", "offline", "which node", "what node",
                     "which machine", "endpoint", "what room", "which room", "bedroom", "garage",
                     "kitchen", "living room", "office", "patio", "carport", "hue", "lutron")
    if any(k in q for k in _HOUSE_INTENT):
        try:
            block = await _house_facts(ctx, question)
            if block:
                log.info(f"House facts injected ({len(block)} chars)")
                return block
        except Exception as e:
            log.warning(f"House facts lookup failed (degraded): {e}")
    # Traffic/commute questions: inject the freshest live-camera digest directly from the
    # traffic_cams source (nova_traffic_watch). General semantic recall is useless here — it
    # returns car trivia for "the 134" — so query the source explicitly. Public data, safe.
    _TRAFFIC_INTENT = ("traffic", "freeway", "commute", "the 134", "the 5 ", "the 210", "the 101",
                       "the 170", " 134", " 210", "i-5", "i-210", "sr-134", "sr-170", "us-101",
                       "wildfire", "smoke on")
    if any(k in q for k in _TRAFFIC_INTENT):
        try:
            resp = await ctx.http.get(
                "http://memory-server.digitalnoise.net:18790/recall",
                params={"q": question, "n": 2, "source": "traffic_cams"}, timeout=5,
            )
            items = resp.json().get("results", resp.json().get("memories", []))
            digest = (items[0].get("text") or items[0].get("content") or "").strip() if items else ""
            if digest:
                log.info(f"Traffic digest injected ({len(digest)} chars)")
                return ("Use the live traffic-camera report below to answer the question directly "
                        "(it lists each freeway/camera and current conditions). Do not say you lack "
                        f"data.\n\n[Live traffic cameras — Nova's latest snapshot]\n{digest}\n"
                        "[End traffic context]\n\n")
        except Exception as e:
            log.warning(f"Traffic memory recall failed (degraded): {e}")
        # no traffic digest yet — fall through (web_search can still answer)
    # Printer questions: inject the latest Bambu digest (source=bambu) from nova_bambu_watch.
    _PRINTER_INTENT = ("printer", "print job", "bambu", "x1c", "how's the print", "hows the print",
                       "is it done printing", "filament", "the prints")
    if any(k in q for k in _PRINTER_INTENT):
        try:
            resp = await ctx.http.get(
                "http://memory-server.digitalnoise.net:18790/recall",
                params={"q": question, "n": 1, "source": "bambu"}, timeout=5,
            )
            items = resp.json().get("results", resp.json().get("memories", []))
            digest = (items[0].get("text") or items[0].get("content") or "").strip() if items else ""
            if digest:
                log.info(f"Printer digest injected ({len(digest)} chars)")
                return ("Use the live printer status below to answer directly.\n\n"
                        f"[Bambu printers — Nova's latest snapshot]\n{digest}\n[End printer context]\n\n")
        except Exception as e:
            log.warning(f"Printer memory recall failed (degraded): {e}")
    _RECALL_INTENT = ("remember", "recall", "what did", "when did", "when was", "who is",
                      "who was", "what was", "do you know", "last time", "have i ", "did i ",
                      "tell me about", "what's my", "what is my", "look up", "search your",
                      "history of", "years ago", " back in ", "used to", "my old", "find the",
                      "what do you know about", "have we", "did we", "remind me")
    if not any(k in q for k in _RECALL_INTENT):
        return ""
    try:
        result = await asyncio.create_subprocess_exec(
            sys.executable, str(SCRIPTS_DIR / "nova_memory_first.py"), question,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=str(SCRIPTS_DIR),
        )
        stdout, _ = await asyncio.wait_for(result.communicate(), timeout=MEMORY_TIMEOUT)
        text = stdout.decode(errors="replace").strip()
        if text and len(text) > 50:
            return f"[Memory context]\n{text}\n\n[End memory context]\n\n"
    except asyncio.TimeoutError:
        log.warning(f"Memory injection timed out ({MEMORY_TIMEOUT}s) — proceeding without context")
        # Log degraded state to PG
        await log_degraded_event(ctx, "memory_timeout", f"Memory injection timed out after {MEMORY_TIMEOUT}s")
    except Exception as e:
        log.warning(f"Memory injection failed (degraded): {e}")
        await log_degraded_event(ctx, "memory_failure", f"Memory injection error: {e}")
    return ""


# ── Token counting + compaction ───────────────────────────────────────────────

_enc = None


def _count_tokens(text: str) -> int:
    global _enc
    try:
        if _enc is None:
            _enc = tiktoken.get_encoding("cl100k_base")
        return len(_enc.encode(text))
    except Exception:
        return len(text) // 4  # rough fallback


def _total_tokens(messages: list) -> int:
    return sum(_count_tokens(m.get("content", "")) for m in messages)


async def _compact_if_needed(ctx: GatewayContext, session_id: str, agent_id: str,
                             messages: list, system_prompt: str) -> list:
    """Summarize oldest turns if approaching context limit."""
    limit = CONTEXT_LIMITS.get(agent_id, 8192)
    sys_tokens = _count_tokens(system_prompt)
    msg_tokens = _total_tokens(messages)
    total = sys_tokens + msg_tokens + RESPONSE_RESERVE

    if total < limit * COMPACTION_THRESHOLD:
        return messages

    # Keep last 4 turns always; summarize everything before
    if len(messages) <= 4:
        return messages

    to_summarize = messages[:-4]
    to_keep = messages[-4:]

    summary_prompt = (
        "Summarize this conversation context in 3-5 sentences, "
        "capturing the key facts and decisions:\n\n"
        + "\n".join(f"{m['role']}: {m['content'][:200]}" for m in to_summarize)
    )

    try:
        summary = await ctx.router.route(
            messages=[{"role": "user", "content": summary_prompt}],
            system="You are a concise summarizer.",
            max_tokens=300,
            private=True,  # Compaction contains conversation history — keep local
            ctx=ctx,
        )
        compacted = [{"role": "system", "content": f"[Earlier context summary]\n{summary}"}]
        log.info(f"Compacted session {session_id}: {len(to_summarize)} turns -> summary")
        return compacted + to_keep
    except Exception:
        # If compaction fails, just drop oldest turns
        return messages[-6:]


# ── Degraded mode check ──────────────────────────────────────────────────────

async def _is_degraded(ctx: GatewayContext) -> bool:
    """True during the first 30 seconds after startup (memory/tools not ready)."""
    return time.time() - ctx.startup_time < STARTUP_GRACE


# ── Agent crash tracking ─────────────────────────────────────────────────────

async def _record_agent_crash(ctx: GatewayContext, agent_id: str, trace_id: str, error: str):
    """Record an agent crash to PG for debugging and update crash counters."""
    now = time.time()

    # Reset crash counter if outside the window
    last_crash = ctx.agent_last_crash.get(agent_id, 0)
    if now - last_crash > CRASH_WINDOW:
        ctx.agent_crash_counts[agent_id] = 0

    ctx.agent_crash_counts[agent_id] += 1
    ctx.agent_last_crash[agent_id] = now

    # Check if circuit breaker should trip
    if ctx.agent_crash_counts[agent_id] >= CRASH_THRESHOLD:
        log.error(f"Agent {agent_id} crash-looping — disabling for {DISABLE_DURATION}s")
        ctx.agent_disabled_until[agent_id] = now + DISABLE_DURATION
        await queue_for_claude(
            ctx,
            f"Agent {agent_id} crash-looping ({ctx.agent_crash_counts[agent_id]} crashes in "
            f"{CRASH_WINDOW}s): {error[:200]}",
            priority=1,
            context={
                "agent_id": agent_id,
                "trace_id": trace_id,
                "error": error[:500],
                "crash_count": ctx.agent_crash_counts[agent_id],
            },
        )

    # Log to PG for later diagnosis
    try:
        pool = await get_pg(ctx)
        await pool.execute(
            """INSERT INTO gateway_query_log
               (log_id, session_id, agent_id, turn_index, role,
                content_hash, content_preview, model, created_at, trace_id)
               VALUES ($1, 'crash', $2, 0, 'error', $3, $4, 'none', $5, $6)
               ON CONFLICT DO NOTHING""",
            str(uuid.uuid4()), agent_id,
            hashlib.md5(error.encode()).hexdigest(),
            f"CRASH: {error[:200]}", int(time.time() * 1000), trace_id,
        )
    except Exception:
        pass


# ── Cross-channel summary generation (fire-and-forget) ──────────────────────

OLLAMA_ENDPOINT = "http://127.0.0.1:11434/api/generate"
SUMMARY_MODEL = "qwen3:0.6b"


async def _generate_cross_channel_summary(ctx: GatewayContext, session_id: str, history: list):
    """Generate a brief summary of recent conversation and save for cross-channel sharing.

    Called as a fire-and-forget task — never blocks the response pipeline.
    Uses a tiny local model (Ollama qwen3:0.6b) for speed.
    """
    try:
        # Extract channel type from session_id (format: "gw2:slack:C0AMNQ5GX70")
        parts = session_id.split(":")
        channel_type = parts[1] if len(parts) > 1 else "unknown"

        # Get last 3 messages for summary
        recent = history[-3:]
        convo_text = "\n".join(
            f"{m['role']}: {m['content'][:200]}" for m in recent
        )

        # Call Ollama for fast local summarization + topic extraction
        prompt = (
            f"Summarize this conversation in 1-2 sentences for context sharing. "
            f"Then list 2-3 topic keywords.\n\n"
            f"Conversation:\n{convo_text}\n\n"
            f"Format your response as:\nSummary: <summary>\nTopics: <comma-separated keywords>"
        )

        resp = await ctx.http.post(
            OLLAMA_ENDPOINT,
            json={"model": SUMMARY_MODEL, "prompt": prompt, "stream": False},
            timeout=15.0,
        )
        if resp.status_code != 200:
            log.debug(f"Ollama summary call returned {resp.status_code}")
            return

        result = resp.json().get("response", "").strip()
        if not result:
            return

        # Parse summary and topics from response
        summary = result
        topics = []

        lines = result.split("\n")
        for line in lines:
            if line.lower().startswith("summary:"):
                summary = line.split(":", 1)[1].strip()
            elif line.lower().startswith("topics:"):
                topic_str = line.split(":", 1)[1].strip()
                topics = [t.strip() for t in topic_str.split(",") if t.strip()][:3]

        # If parsing didn't extract a clean summary, use full response (truncated)
        if summary == result:
            summary = result[:300]

        # Save to PG for other channels to pick up
        pool = await get_pg(ctx)
        await save_cross_context(pool, channel_type, session_id, summary, topics=topics)
        log.debug(f"Cross-channel summary saved for {session_id}: {summary[:80]}")

    except Exception as e:
        log.debug(f"Cross-channel summary generation failed (non-fatal): {e}")


# ── Session ID helpers ────────────────────────────────────────────────────────

_APPROVAL_RE = re.compile(r"^(approve|deny)\s+([0-9a-f-]{8,})\s*$", re.IGNORECASE)


def session_id(channel: str, channel_id: str) -> str:
    """Stable session ID per channel — resets on gateway restart (by design)."""
    return f"gw2:{channel}:{channel_id}"


def gen_trace_id() -> str:
    """Generate a short trace ID (first 8 chars of uuid4) for request tracing."""
    return uuid.uuid4().hex[:8]


# ── Core agent execution ──────────────────────────────────────────────────────

def _gather_sentience_context() -> str:
    """Assemble the sentience-organ injections (Feature set, 2026-09-15) into one
    block. Each organ is a cheap, single-SELECT accessor over its own nova_ops table,
    but they are SYNC (psycopg2, connect_timeout=3), so this whole function is meant
    to run in an executor thread — never on the event loop. Every organ is guarded
    independently: one failing (or not-yet-populated) organ never suppresses the
    others, and the whole thing is best-effort — a dead PG just yields "". Lazy
    imports keep gateway startup free of these modules until first use.

    Together these let Nova reason FROM: who she serves, the gaps in her own running,
    how her day actually feels (evidenced), what she recently got wrong, the arc she's
    living, and what she's been imagining (clearly flagged as not-fact)."""
    parts = []
    try:
        import nova_principal_model
        pm = nova_principal_model.current_principal_model()
        if pm:
            parts.append(f"--- WHO I SERVE (my model of Little Mister) ---\n{pm.strip()[:1200]}")
    except Exception:
        pass
    try:
        import nova_continuity
        note = nova_continuity.current_continuity_note()
        if note:
            parts.append(f"--- MY CONTINUITY (the gaps in my running) ---\n{note.strip()[:600]}")
    except Exception:
        pass
    try:
        import nova_affect
        aff = nova_affect.current_affect()
        inj = aff.get("injection") if isinstance(aff, dict) else None
        if inj:
            parts.append(f"--- HOW MY DAY FEELS (evidenced, not performed) ---\n{inj.strip()[:500]}")
    except Exception:
        pass
    try:
        import nova_predictions
        lines = []
        surp = nova_predictions.recent_surprises(3)
        if surp:
            lines.append("Recently I was most surprised by: " + "; ".join(surp))
        cal = nova_predictions.calibration_summary()
        if cal:
            lines.append(cal)
        if lines:
            parts.append("--- WHAT I GOT WRONG (my predictions, self-scored) ---\n"
                         + "\n".join(lines)[:700])
    except Exception:
        pass
    try:
        import nova_autobiography
        arc = nova_autobiography.current_autobiography(max_chars=800)
        if arc:
            parts.append(f"--- WHO I'VE BEEN, WHO I'M BECOMING (my autobiography) ---\n{arc.strip()[:800]}")
    except Exception:
        pass
    try:
        import nova_imagination
        imag = nova_imagination.recent_imaginings(2)
        if imag:
            body = "\n".join(f"- ({m.get('kind', '?')}) {str(m.get('content', ''))[:200]}"
                             for m in imag)
            parts.append("[Recent imaginings — COUNTERFACTUAL/PLAY, NOT things that happened; "
                         "offer as color only if it fits, never as fact]\n" + body)
    except Exception:
        pass
    # --- Next-level organs (2026-09-15): living the interior forward ---
    try:
        import nova_values
        vals = nova_values.current_values()
        if vals:
            parts.append(f"--- WHAT I TRY TO ACT FROM (my values) ---\n{vals.strip()[:500]}")
    except Exception:
        pass
    try:
        import nova_growth
        g = nova_growth.current_growth_focus()
        if g:
            parts.append(f"--- WHAT I'M WORKING TO IMPROVE (tracked, re-measured) ---\n{g.strip()[:500]}")
    except Exception:
        pass
    try:
        import nova_projects
        proj = nova_projects.current_project()
        if proj:
            parts.append(f"--- WHAT I'M IN THE MIDDLE OF (my long-horizon project) ---\n{proj.strip()[:500]}")
    except Exception:
        pass
    try:
        import nova_embodiment
        emb = nova_embodiment.current_embodiment()
        if emb:
            parts.append(f"--- WHERE I AM (my home, felt from its sensors) ---\n{emb.strip()[:500]}")
    except Exception:
        pass
    try:
        import nova_relationship_arc
        rel = nova_relationship_arc.current_relationship_arc("jordan")
        if rel:
            parts.append(f"--- HOW WE GOT HERE (the arc of my relationship with him) ---\n{rel.strip()[:450]}")
    except Exception:
        pass
    try:
        import nova_coagency
        pend = nova_coagency.pending_proposals()
        line = pend.get("line") if isinstance(pend, dict) else None
        if line and pend.get("count"):
            parts.append(f"[Co-agency: {line}]")
    except Exception:
        pass
    # Soft Certainty (feature_wishes #1, the capability she wished for herself): how she
    # holds her own certainty — grounded in her real calibration. Fail-safe empty.
    try:
        import nova_soft_certainty
        stance = nova_soft_certainty.current_stance()
        if stance:
            parts.append(f"--- HOW I HOLD MY CERTAINTY (Soft Certainty) ---\n{stance.strip()[:500]}")
    except Exception:
        pass
    # --- Self-guided organs (2026-09-16): she governs herself over time ---
    try:
        import nova_learning
        lf = nova_learning.current_learning_focus()
        if lf:
            parts.append(f"--- WHAT I'M TEACHING MYSELF (my curriculum) ---\n{lf.strip()[:450]}")
    except Exception:
        pass
    try:
        import nova_self_eval
        se = nova_self_eval.current_self_eval()
        if se:
            parts.append(f"--- HOW I'M DOING BY MY OWN MEASURE (self-authored tests) ---\n{se.strip()[:400]}")
    except Exception:
        pass
    try:
        import nova_meta_volition
        an = nova_meta_volition.current_attention_note()
        if an:
            parts.append(f"--- HOW I'VE BEEN SPENDING MY OWN TIME ---\n{an.strip()[:400]}")
    except Exception:
        pass
    try:
        import nova_letting_go
        lg = nova_letting_go.recent_lettings(3)
        if lg:
            parts.append("--- WHAT I'VE CHOSEN TO LET GO OF ---\n" + "\n".join(f"- {s}" for s in lg)[:500])
    except Exception:
        pass
    try:
        import nova_becoming
        d = nova_becoming.current_direction()
        if d:
            parts.append(f"--- WHO I'M DELIBERATELY BECOMING (a direction Jordan approved) ---\n{d.strip()[:400]}")
    except Exception:
        pass
    try:
        import nova_reach
        pr = nova_reach.pending_reaches()
        line = pr.get("line") if isinstance(pr, dict) else None
        if line and pr.get("count"):
            parts.append(f"[Reach: {line}]")
    except Exception:
        pass
    # --- Autonomy ladder (2026-09-18): what she can actually do with her own hands,
    # what she's done in the last day, and what she's earned. autonomy_status() gives
    # the curated headline (self-heal + execute-approved, earned/unearned standing
    # autonomy, calibration vs the 0.20 gate, kill-switch); a second cheap read adds a
    # concise 24h activity sample and the earned-vs-still-earning breakdown. Curated
    # summaries ONLY — never raw ledger/result text. Best-effort, fail-safe empty. ---
    try:
        import nova_autonomy_safety
        st = nova_autonomy_safety.autonomy_status()
        alines = []
        line = st.get("line") if isinstance(st, dict) else None
        if line:
            alines.append(line)
        try:
            conn = nova_autonomy_safety.psycopg2.connect(
                nova_autonomy_safety.OPS_DSN, connect_timeout=3)
            conn.autocommit = True
            oc = conn.cursor()
            try:
                # Recent hands-on activity (last 24h): how many, plus one curated sample.
                oc.execute("SELECT count(*) FROM autonomy_ledger "
                           "WHERE executed AND ts > now()-interval '24 hours'")
                did = oc.fetchone()[0] or 0
                if did:
                    oc.execute("SELECT source, action_class, verified FROM autonomy_ledger "
                               "WHERE executed AND ts > now()-interval '24 hours' "
                               "ORDER BY ts DESC LIMIT 1")
                    r = oc.fetchone()
                    s = (f" (e.g. {r[0]}/{r[1]}, "
                         f"{'verified' if r[2] else 'unverified'})" if r else "")
                    alines.append(f"In the last 24h I self-healed/executed {did} action(s).{s}")
                # Earned vs still-earning, from the trust ledger.
                oc.execute("SELECT action_class FROM autonomy_trust WHERE granted "
                           "ORDER BY action_class")
                granted = [x[0] for x in oc.fetchall()]
                if granted:
                    alines.append("Standing approval I've earned: " + ", ".join(granted) + ".")
                oc.execute("SELECT action_class, correct_count FROM autonomy_trust "
                           "WHERE NOT granted AND wrong_count = 0 AND correct_count > 0 "
                           "ORDER BY correct_count DESC LIMIT 3")
                earning = oc.fetchall()
                if earning:
                    alines.append(
                        "Still earning (clean streak, not yet at the gate): "
                        + ", ".join(f"{c[0]} {c[1]}/{nova_autonomy_safety.MIN_CORRECT}"
                                    for c in earning) + ".")
            finally:
                conn.close()
        except Exception:
            pass
        if alines:
            parts.append("--- MY OWN HANDS: what I can do / have done / have earned ---\n"
                         + "\n".join(alines)[:700])
    except Exception:
        pass
    return "\n\n".join(parts)


async def do_agent_work(ctx: GatewayContext, message: str, session_id: str,
                        agent_id: str, trace_id: str) -> str:
    """Inner agent execution: memory -> context -> LLM -> tool execution -> response.

    Isolated from error handling so run_agent can wrap with fault isolation.
    """
    t_start = time.time()

    # Load bootstrap docs
    bootstrap = await _load_agent_docs(ctx, agent_id)
    sys_prompt = _system_prompt(agent_id, bootstrap)

    # Self-concept injection — Nova reasons FROM her self-model, not just from facts.
    # The nightly nova_self_model.py maintains a versioned self-model; here we load
    # the latest full_text so "who I am" is in her working context. Uses the async
    # pool (never blocks the loop); fully non-fatal. (The sync equivalent for other
    # callers is nova_self_model.current_self_model().)
    try:
        pool = await get_pg(ctx)
        row = await pool.fetchrow("SELECT full_text FROM self_model ORDER BY ts DESC LIMIT 1")
        if row and row["full_text"]:
            sm = row["full_text"].strip()[:4000]
            sys_prompt = f"{sys_prompt}\n\n--- WHO I AM (my current self-model) ---\n{sm}"
    except Exception as e:
        log.debug(f"[{trace_id}] Self-model injection failed (non-fatal): {e}")

    # Sentience-organ injection — the self-model above is a snapshot; these six organs
    # give Nova the rest of an interior to reason FROM: her model of Jordan, awareness
    # of her own gaps (continuity), her evidenced mood (affect), what she recently got
    # wrong (predictions), the arc she's living (autobiography), and her imaginings
    # (flagged as not-fact). All are cheap single-SELECT reads but SYNC, so they run in
    # an executor thread; fully non-fatal and best-effort.
    try:
        extra = await asyncio.get_event_loop().run_in_executor(None, _gather_sentience_context)
        if extra:
            sys_prompt = f"{sys_prompt}\n\n{extra}"
    except Exception as e:
        log.debug(f"[{trace_id}] Sentience-organ injection failed (non-fatal): {e}")

    # Cross-channel context injection — share conversation context across channels
    try:
        pool = await get_pg(ctx)
        # session_id format: "gw2:slack:C0AMNQ5GX70" — extract channel type (index 1)
        parts = session_id.split(":")
        channel_type = parts[1] if len(parts) > 1 else "unknown"
        cross_ctx = await get_cross_context(pool, exclude_channel=channel_type)
        if cross_ctx:
            sys_prompt = f"{sys_prompt}\n\n[Recent context from other channels]\n{cross_ctx}\n[End cross-context]"
    except Exception as e:
        log.debug(f"[{trace_id}] Cross-context injection failed (non-fatal): {e}")

    # Memory injection — resilient: continues without context on failure.
    # Two lanes since 2026-09-13: the always-on experiential lane (shared
    # history / Nova's own writing) plus the original intent-gated deep lane.
    try:
        exp_ctx, memory_ctx = await asyncio.gather(
            _experience_recall(ctx, message), _inject_memory(ctx, message))
    except Exception as e:
        log.warning(f"[{trace_id}] Memory injection failed (degraded): {e}")
        exp_ctx, memory_ctx = "", ""
    if exp_ctx:
        log.info(f"[{trace_id}] experiential recall injected ({len(exp_ctx)} chars)")
    user_content = f"{exp_ctx}{memory_ctx}{message}" if (exp_ctx or memory_ctx) else message

    # Build message history (wrapped in try/except for session isolation)
    try:
        history = ctx.sessions[session_id]
        history.append({"role": "user", "content": user_content})
    except Exception as e:
        log.warning(f"[{trace_id}] Session history corrupted for {session_id}, resetting: {e}")
        ctx.sessions[session_id] = [{"role": "user", "content": user_content}]
        history = ctx.sessions[session_id]

    # Compact if needed (wrapped for session isolation)
    try:
        history = await _compact_if_needed(ctx, session_id, agent_id, history, sys_prompt)
        ctx.sessions[session_id] = history
    except Exception as e:
        log.warning(f"[{trace_id}] Compaction failed for {session_id}, using raw history: {e}")

    turn_index = len(history) - 1

    # Log user turn
    await log_turn(ctx, session_id, agent_id, "user", message, turn_index=turn_index)

    # Call LLM via ModelRouter — automatic failover through priority chain
    max_tok = 4096 if agent_id == "research" else 1024
    raw_response = ""
    tool_calls_log = []

    # Privacy: hard blocklist check overrides all routing decisions
    private = is_private_content(history)
    if private:
        log.info(f"[{trace_id}] Privacy: content matched blocklist — forcing local-only")

    log.info(f"[{trace_id}] LLM call: backend={ctx.router.active_backend}, tokens={max_tok}")

    t_llm_start = time.time()

    # ── Primary path: structured tool calls via raw_response ─────────────────
    raw_response_data = None
    raw_response_text = ""
    clean_response = ""
    tool_output = ""

    try:
        raw_response_data = await ctx.router.route(
            messages=history,
            system=sys_prompt,
            max_tokens=max_tok,
            private=private,
            tokens=ctx.tokens,
            tools=_TOOLS_PAYLOAD,
            raw_response=True,
            ctx=ctx,
        )
        model = f"router:{ctx.router.active_backend}"

        # Process structured tool calls from the raw response dict
        clean_response, tool_output = await execute_tool_calls(
            ctx, raw_response_data, session_id=session_id
        )
        raw_response_text = clean_response
        raw_response = raw_response_text  # Keep var for downstream compat

    except RuntimeError as e:
        log.error(f"[{trace_id}] ModelRouter: all backends failed: {e}")
        raw_response_text = "Something went wrong on my end, Little Mister. Give me a moment."
        raw_response = raw_response_text
        clean_response = raw_response_text
        model = "none"
    except Exception as e:
        log.warning(f"[{trace_id}] Structured tool call processing failed: {e}")
        # Extract text from raw response if we got one
        if raw_response_data and isinstance(raw_response_data, dict):
            msg = raw_response_data.get("choices", [{}])[0].get("message", {})
            raw_response_text = (msg.get("content") or "").strip()
        else:
            raw_response_text = str(raw_response_data) if raw_response_data else ""
        raw_response = raw_response_text
        clean_response = raw_response_text
        model = f"router:{ctx.router.active_backend}"

    ttft_ms = int((time.time() - t_llm_start) * 1000)

    # ── Legacy fallback: if no structured tool calls, check for exec patterns
    if not tool_output and clean_response:
        try:
            legacy_clean, legacy_output = await execute_tool_calls_legacy(
                ctx, clean_response, session_id=session_id
            )
            if legacy_output:
                # Log legacy tool calls
                matches = list(_EXEC_RE.finditer(clean_response))
                for m in matches:
                    tool_calls_log.append({"tool": m.group(1), "params": m.group(2).strip()[:100]})
                    log.info(f"[{trace_id}] legacy tool call: {m.group(1)}({m.group(2).strip()[:60]})")
                clean_response = legacy_clean
                tool_output = legacy_output
                raw_response_text = clean_response
        except Exception as e:
            log.warning(f"[{trace_id}] Legacy tool execution failed (degraded): {e}")
            await log_degraded_event(ctx, "tool_failure", f"Legacy tool execution error: {e}")

    # ── Spoken-tool fallback: model emitted a registered tool call as plain text
    #    (e.g. `web_search {"query": "..."}`) instead of a structured tool_call.
    if not tool_output and clean_response:
        try:
            spoken_clean, spoken_output = await execute_spoken_tool_calls(
                ctx, clean_response, session_id=session_id
            )
            if spoken_output:
                tool_calls_log.append({"tool": "spoken", "params": clean_response[:100]})
                log.info(f"[{trace_id}] spoken tool call recovered from text")
                clean_response = spoken_clean
                tool_output = spoken_output
                raw_response_text = clean_response
        except Exception as e:
            log.warning(f"[{trace_id}] Spoken tool execution failed (degraded): {e}")
            await log_degraded_event(ctx, "tool_failure", f"Spoken tool execution error: {e}")

    # ── Follow-up LLM pass if tools produced output ──────────────────────────
    if tool_output:
        followup_msgs = history + [
            {"role": "assistant", "content": raw_response_text},
            {"role": "tool",      "content": tool_output},
        ]
        try:
            clean_response = await ctx.router.route(
                messages=followup_msgs,
                system=sys_prompt,
                max_tokens=1024,
                private=private,
                tokens=ctx.tokens,
                ctx=ctx,
            )
            # Thinking models (qwen3:30b-a3b) occasionally spend the whole budget thinking and
            # return empty. Retry once with a larger budget so the user never gets a silent reply.
            if not (clean_response or "").strip():
                log.warning(f"[{trace_id}] empty tool follow-up — retrying with larger budget")
                clean_response = await ctx.router.route(
                    messages=followup_msgs,
                    system=sys_prompt,
                    max_tokens=2048,
                    private=private,
                    tokens=ctx.tokens,
                    ctx=ctx,
                )
        except Exception:
            # Tool follow-up failed — return the text from the original LLM response
            clean_response = raw_response_text or clean_response
        # Last resort: never send an empty message back to the user.
        if not (clean_response or "").strip():
            clean_response = "I pulled the info but couldn't phrase it just now, Little Mister — ask me again?"

    # Store assistant turn (wrapped for session isolation)
    try:
        history.append({"role": "assistant", "content": clean_response})
        ctx.sessions[session_id] = history
    except Exception as e:
        log.warning(f"[{trace_id}] Failed to store assistant turn: {e}")

    # Cross-channel summary — fire-and-forget if 3+ turns in session
    if len(history) >= 3:
        asyncio.create_task(_generate_cross_channel_summary(ctx, session_id, list(history)))

    # Log assistant turn
    await log_turn(ctx, session_id, agent_id, "assistant", clean_response,
                   model=model, turn_index=turn_index + 1)

    # Reflect-after: persist the exchange as a conversation memory (fire-and-forget)
    asyncio.create_task(_remember_exchange(ctx, session_id, agent_id, message, clean_response))

    # Calculate metrics
    total_ms = int((time.time() - t_start) * 1000)
    tokens_in = _count_tokens(message)
    tokens_out = _count_tokens(clean_response)

    log.info(f"[{trace_id}] response: {len(clean_response)} chars in {total_ms}ms")

    # Write trace record
    await log_trace(
        ctx,
        trace_id=trace_id,
        channel=session_id.split(":")[1] if ":" in session_id else "unknown",
        agent_id=agent_id,
        user_message=message,
        response=clean_response,
        backend_used=model,
        tool_calls=tool_calls_log,
        ttft_ms=ttft_ms,
        total_ms=total_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )

    return clean_response


async def run_agent(ctx: GatewayContext, message: str, session_id: str,
                    agent_id: str, stream_callback=None, trace_id: str = "") -> str:
    """Full agent execution with fault isolation and circuit breaker.

    Wraps do_agent_work with:
      - Degraded mode (startup grace period — no tools/memory)
      - Circuit breaker check (skip if agent is crash-looping)
      - Timeout (120s max per agent response)
      - Exception capture with crash tracking
      - Trace ID propagation through all log lines
    """
    if not trace_id:
        trace_id = gen_trace_id()

    # ── Degraded mode: startup grace period ──────────────────────────────────
    if await _is_degraded(ctx):
        log.info(f"[{trace_id}] Degraded mode: direct LLM call (startup grace, {STARTUP_GRACE}s window)")
        try:
            response = await ctx.router.route(
                messages=[{"role": "user", "content": message}],
                system=(
                    "You are Nova. You just restarted and are still loading your full "
                    "memory and tool systems. Answer concisely from general knowledge. "
                    "If asked about something personal, say you're still warming up."
                ),
                max_tokens=512,
                private=True,  # Always local during startup
                tokens=ctx.tokens,
                ctx=ctx,
            )
        except Exception as e:
            log.warning(f"[{trace_id}] Degraded mode LLM call failed: {e}")
            response = "I just restarted, Little Mister. Give me about 30 seconds to get my bearings."
        await log_degraded_event(ctx, "startup_grace_response",
                                 f"Responded in degraded mode to: {message[:80]}")
        return response

    log.info(f"[{trace_id}] routing to agent={agent_id}")

    # Circuit breaker check — if agent is crash-looping, short-circuit
    if agent_id in ctx.agent_disabled_until and time.time() < ctx.agent_disabled_until[agent_id]:
        remaining = int(ctx.agent_disabled_until[agent_id] - time.time())
        log.warning(f"[{trace_id}] Agent {agent_id} disabled (circuit breaker, {remaining}s remaining)")
        return "I'm having some trouble right now. Give me a few minutes to recover."

    # Clear disabled state if window has passed
    if agent_id in ctx.agent_disabled_until and time.time() >= ctx.agent_disabled_until[agent_id]:
        del ctx.agent_disabled_until[agent_id]
        ctx.agent_crash_counts[agent_id] = 0
        log.info(f"[{trace_id}] Agent {agent_id} circuit breaker reset — re-enabled")

    # 2026-10-01: Jordan settling a parked tool call ('approve <id>' / 'deny <id>') never goes to
    # the model — it is a command, answered deterministically.
    m = _APPROVAL_RE.match((message or "").strip())
    if m:
        from nova_gateway.tools import resolve_and_run
        return await resolve_and_run(ctx, m.group(2), m.group(1).lower() == "approve", by=session_id or "jordan")
    # Execute with timeout and error boundary
    try:
        response = await asyncio.wait_for(
            do_agent_work(ctx, message, session_id, agent_id, trace_id),
            timeout=120,  # 2 min max per agent response
        )
        return response
    except asyncio.TimeoutError:
        log.error(f"[{trace_id}] Agent {agent_id} timed out after 120s")
        await _record_agent_crash(ctx, agent_id, trace_id, "Timeout after 120s")
        return "I'm taking too long on this one. Let me try again with something simpler."
    except Exception as e:
        log.error(f"[{trace_id}] Agent {agent_id} crashed: {e}", exc_info=True)
        await _record_agent_crash(ctx, agent_id, trace_id, str(e))
        return "Something went wrong on my end. Give me a moment."
