"""
nova_gateway.tools_extended — Additional tool implementations for Nova.

Adds: peekaboo (UI automation), taskflow (orchestration), camsnap (cameras),
      summarize (content summarization).

These are registered into TOOL_REGISTRY at gateway startup.

Written by Jordan Koch (via Claude).
"""

import asyncio
import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("nova_gateway_v2")

PEEKABOO = "/opt/homebrew/bin/peekaboo"
CAMSNAP = "/opt/homebrew/bin/camsnap"
SUMMARIZE = "/opt/homebrew/bin/summarize"


# ── Tool Definitions (merge into TOOL_REGISTRY) ─────────────────────────────

EXTENDED_TOOLS = {
    "screenshot": {
        "description": "Take a screenshot of the Mac desktop or a specific app window. Returns the image path.",
        "parameters": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "App name to capture (optional — captures full screen if omitted)"},
                "analyze": {"type": "string", "description": "Optional prompt to analyze the screenshot with AI vision"},
            },
        },
    },
    "ui_click": {
        "description": "Click a UI element on screen. Use screenshot first to identify targets.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Element ID from peekaboo see, or text query to find"},
                "action": {"type": "string", "description": "click, double-click, right-click (default: click)"},
            },
            "required": ["target"],
        },
    },
    "ui_type": {
        "description": "Type text into the currently focused UI element.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type"},
            },
            "required": ["text"],
        },
    },
    "summarize_url": {
        "description": "Summarize a URL, YouTube video, PDF, or article. Returns a concise summary.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL or file path to summarize"},
                "length": {"type": "string", "description": "short, medium, long (default: medium)"},
            },
            "required": ["url"],
        },
    },
    "camera_snap": {
        "description": "Take a snapshot from a configured RTSP/ONVIF camera.",
        "parameters": {
            "type": "object",
            "properties": {
                "camera": {"type": "string", "description": "Camera name (as configured in camsnap)"},
                "output": {"type": "string", "description": "Output file path (optional)"},
            },
            "required": ["camera"],
        },
    },
    "camera_clip": {
        "description": "Record a short video clip from a camera.",
        "parameters": {
            "type": "object",
            "properties": {
                "camera": {"type": "string", "description": "Camera name"},
                "duration": {"type": "string", "description": "Duration (default: 5s)"},
            },
            "required": ["camera"],
        },
    },
    "flow_create": {
        "description": "Create a new durable multi-step workflow. Returns flow_id for tracking.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What this workflow aims to accomplish"},
                "first_step": {"type": "string", "description": "Name of the first step"},
                "state": {"type": "object", "description": "Initial state data (JSON object)"},
            },
            "required": ["goal", "first_step"],
        },
    },
    "flow_advance": {
        "description": "Advance a workflow to its next step.",
        "parameters": {
            "type": "object",
            "properties": {
                "flow_id": {"type": "string", "description": "Flow ID to advance"},
                "next_step": {"type": "string", "description": "Name of the next step"},
                "state_update": {"type": "object", "description": "State data to merge"},
            },
            "required": ["flow_id", "next_step"],
        },
    },
    "flow_wait": {
        "description": "Pause a workflow until human input or external event arrives.",
        "parameters": {
            "type": "object",
            "properties": {
                "flow_id": {"type": "string", "description": "Flow ID to pause"},
                "reason": {"type": "string", "description": "What we're waiting for"},
            },
            "required": ["flow_id", "reason"],
        },
    },
    "flow_resume": {
        "description": "Resume a paused workflow with new input.",
        "parameters": {
            "type": "object",
            "properties": {
                "flow_id": {"type": "string", "description": "Flow ID to resume"},
                "next_step": {"type": "string", "description": "Step to resume into"},
                "input_data": {"type": "object", "description": "Input data from the wait resolution"},
            },
            "required": ["flow_id", "next_step"],
        },
    },
    "flow_finish": {
        "description": "Mark a workflow as completed.",
        "parameters": {
            "type": "object",
            "properties": {
                "flow_id": {"type": "string", "description": "Flow ID to complete"},
            },
            "required": ["flow_id"],
        },
    },
    "flow_status": {
        "description": "Check the status of a workflow or list all active workflows.",
        "parameters": {
            "type": "object",
            "properties": {
                "flow_id": {"type": "string", "description": "Specific flow ID (optional — lists all active if omitted)"},
            },
        },
    },
}


# ── Tool Implementations ─────────────────────────────────────────────────────

async def _run_cmd(cmd: list, timeout: int = 30) -> tuple:
    """Run a subprocess and return (stdout, stderr, returncode)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode()[:5000], stderr.decode()[:2000], proc.returncode
    except asyncio.TimeoutError:
        proc.kill()
        return "", "Timeout", -1
    except Exception as e:
        return "", str(e), -1


async def dispatch_extended_tool(ctx, tool_name: str, params: dict) -> str:
    """Dispatch an extended tool call. Returns result string."""

    if tool_name == "screenshot":
        cmd = [PEEKABOO, "image"]
        if params.get("app"):
            cmd.extend(["--app", params["app"]])
        if params.get("analyze"):
            cmd = [PEEKABOO, "see"]
            if params.get("app"):
                cmd.extend(["--app", params["app"]])
            cmd.extend(["--analyze", params["analyze"]])
        stdout, stderr, rc = await _run_cmd(cmd, timeout=15)
        if rc == 0:
            return stdout or "Screenshot captured."
        return f"Error: {stderr}"

    elif tool_name == "ui_click":
        target = params["target"]
        action = params.get("action", "click")
        cmd = [PEEKABOO, action, "--query", target]
        stdout, stderr, rc = await _run_cmd(cmd, timeout=10)
        return stdout if rc == 0 else f"Error: {stderr}"

    elif tool_name == "ui_type":
        cmd = [PEEKABOO, "type", params["text"]]
        stdout, stderr, rc = await _run_cmd(cmd, timeout=10)
        return stdout if rc == 0 else f"Error: {stderr}"

    elif tool_name == "summarize_url":
        url = params["url"]
        length = params.get("length", "medium")
        cmd = [SUMMARIZE, url, "--length", length, "--json"]
        stdout, stderr, rc = await _run_cmd(cmd, timeout=60)
        if rc == 0:
            try:
                data = json.loads(stdout)
                return data.get("summary", stdout)
            except json.JSONDecodeError:
                return stdout
        return f"Error summarizing: {stderr}"

    elif tool_name == "camera_snap":
        camera = params["camera"]
        output = params.get("output", f"/tmp/camsnap_{camera}.jpg")
        cmd = [CAMSNAP, "snap", camera, "--out", output]
        stdout, stderr, rc = await _run_cmd(cmd, timeout=15)
        if rc == 0:
            return f"Snapshot saved: {output}"
        return f"Error: {stderr}"

    elif tool_name == "camera_clip":
        camera = params["camera"]
        duration = params.get("duration", "5s")
        output = f"/tmp/camsnap_{camera}_clip.mp4"
        cmd = [CAMSNAP, "clip", camera, "--dur", duration, "--out", output]
        stdout, stderr, rc = await _run_cmd(cmd, timeout=30)
        if rc == 0:
            return f"Clip recorded: {output}"
        return f"Error: {stderr}"

    elif tool_name == "flow_create":
        from nova_gateway.taskflow import create_flow
        pool = await _get_pool(ctx)
        flow_id = await create_flow(
            pool, params["goal"], params["first_step"],
            state=params.get("state", {})
        )
        return f"Flow created: {flow_id}" if flow_id else "Failed to create flow."

    elif tool_name == "flow_advance":
        from nova_gateway.taskflow import advance_step
        pool = await _get_pool(ctx)
        ok = await advance_step(pool, params["flow_id"], params["next_step"],
                                state_update=params.get("state_update"))
        return "Advanced." if ok else "Failed to advance (revision conflict or flow not running)."

    elif tool_name == "flow_wait":
        from nova_gateway.taskflow import set_waiting
        pool = await _get_pool(ctx)
        ok = await set_waiting(pool, params["flow_id"], params["reason"])
        return f"Flow paused. Waiting on: {params['reason']}" if ok else "Failed to pause."

    elif tool_name == "flow_resume":
        from nova_gateway.taskflow import resume_flow
        pool = await _get_pool(ctx)
        ok = await resume_flow(pool, params["flow_id"], params["next_step"],
                               input_data=params.get("input_data"))
        return "Flow resumed." if ok else "Failed to resume (not in waiting state)."

    elif tool_name == "flow_finish":
        from nova_gateway.taskflow import finish_flow
        pool = await _get_pool(ctx)
        ok = await finish_flow(pool, params["flow_id"])
        return "Flow completed." if ok else "Failed to finish."

    elif tool_name == "flow_status":
        from nova_gateway.taskflow import get_flow, list_active_flows
        pool = await _get_pool(ctx)
        if params.get("flow_id"):
            flow = await get_flow(pool, params["flow_id"])
            return json.dumps(flow, indent=2) if flow else "Flow not found."
        else:
            flows = await list_active_flows(pool)
            if not flows:
                return "No active flows."
            lines = []
            for f in flows:
                status = f["status"]
                step = f.get("current_step", "?")
                goal = f.get("goal", "?")[:60]
                lines.append(f"• {f['flow_id'][:8]} [{status}] step={step} — {goal}")
            return "\n".join(lines)

    return f"Unknown tool: {tool_name}"


async def _get_pool(ctx):
    """Get PG pool from context."""
    if hasattr(ctx, 'pg_pool') and ctx.pg_pool:
        return ctx.pg_pool
    from nova_gateway.session import get_pg
    return await get_pg(ctx)
