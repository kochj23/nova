"""
nova_gateway.tools — TOOL_REGISTRY, tool dispatch, execution, and all _tool_* implementations.

Written by Jordan Koch.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path

from nova_gateway.config import (
    SCRIPTS_DIR, SLACK_NOTIFY_CHANNEL, JORDAN_SIGNAL,
)
from nova_gateway.context import GatewayContext
from nova_gateway.session import log_tool_execution

log = logging.getLogger("nova_gateway_v2")


# ── Tool Registry (structured JSON schema) ──────────────────────────────────

TOOL_REGISTRY: dict[str, dict] = {
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
        "description": "Send a message via email, Slack, or Signal",
        "parameters": {
            "channel": {"type": "string", "enum": ["email", "slack", "signal"]},
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
        "description": "Generate Jordan's career narrative from primary sources (North Star -> PRG Aviation -> Litton/Sun -> Disney SRE).",
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
}


# ── Tool dispatch ────────────────────────────────────────────────────────────

async def dispatch_tool(ctx: GatewayContext, tool_name: str, tool_params: dict) -> str:
    """Execute a single structured tool call. Returns the tool output string."""
    if tool_name not in TOOL_REGISTRY:
        return f"[error: unknown tool '{tool_name}']"

    try:
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
        else:
            return f"[error: tool '{tool_name}' not implemented]"
    except asyncio.TimeoutError:
        return f"[tool '{tool_name}' timed out]"
    except Exception as e:
        return f"[tool '{tool_name}' error: {e}]"


# ── Tool implementations ─────────────────────────────────────────────────────

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
            "http://127.0.0.1:8888/search",
            params={"q": query, "format": "json", "categories": "general"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])[:5]
        if not results:
            return f"[no web results for '{query}']"
        formatted = []
        for r in results:
            formatted.append(f"- {r.get('title', 'Untitled')}\n  {r.get('url', '')}\n  {r.get('content', '')[:150]}")
        return "\n".join(formatted)
    except Exception as e:
        return f"[web search error: {e}]"


async def _tool_homekit_scene(ctx: GatewayContext, params: dict) -> str:
    """Execute a HomeKit scene via the Shortcuts CLI proxy."""
    scene = params.get("scene", "")
    if not scene:
        return "[error: no scene specified]"

    try:
        resp = await ctx.http.post(
            "http://127.0.0.1:37432/scene",
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
        output = await dispatch_tool(ctx, tool_name, tool_params)
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

        cmd = [sys.executable if "python" in interpreter else interpreter,
               script_path]
        if args:
            cmd.append(args)

        t0 = time.time()
        try:
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
                output = stderr.decode(errors="replace").strip()[:200]
            tool_results.append(output)
        except asyncio.TimeoutError:
            tool_results.append("[tool timed out]")
            output = "[tool timed out]"
        except Exception as e:
            tool_results.append(f"[tool error: {e}]")
            output = f"[tool error: {e}]"

        duration_ms = int((time.time() - t0) * 1000)

        # Audit log for legacy calls too
        await log_tool_execution(
            ctx,
            session_id,
            f"legacy_exec:{interpreter}",
            {"script": script_path, "args": args},
            output[:500] if output else "",
            duration_ms,
        )

        # Remove exec line from text
        clean = clean.replace(m.group(0), "").strip()

    return clean, "\n".join(tool_results)
