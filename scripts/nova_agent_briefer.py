#!/usr/bin/env python3
"""
nova_agent_briefer.py — Proactive Briefer background agent.

Runs daily at 7 AM. Scans calendar, email, memory, and system status.
Generates a personalized, reasoned daily brief — not a template, but
analysis of what actually matters today.

Posts to Jordan via Slack #nova-chat.

Written by Jordan Koch.
"""

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_subagent import SubAgent
from nova_logger import log, LOG_INFO, LOG_ERROR

NOVACONTROL_API = "http://127.0.0.1:37400"
MEMORY_URL = "http://127.0.0.1:18790"

SYSTEM_PROMPT = """You are Briefer, Nova's daily intelligence analyst for Jordan Koch.

You produce a personalized morning brief that is NOT a template. It is a reasoned
analysis of what matters today based on real data: calendar, email, memory, system health.

Structure your brief as:

1. **Today's Priority** — The single most important thing Jordan should focus on
2. **Calendar** — What's scheduled, with context from memory about the people/topics involved
3. **Overnight Activity** — What happened while Jordan slept (emails, system events, alerts)
4. **Open Items** — Action items, deadlines approaching, things waiting on Jordan
5. **System Health** — Any infrastructure issues, backup status, security alerts
6. **One Thing to Know** — A single interesting fact or connection you noticed in memory

Keep it concise. Jordan wants signal, not noise. If nothing interesting happened,
say so in two sentences and stop. Don't pad."""


class ProactiveBriefer(SubAgent):
    name = "briefer"
    model = "deepseek-r1:8b"
    backend = "ollama"
    channels = ["brief", "morning"]
    description = "Daily 7 AM personalized intelligence brief. Calendar + email + memory + system health."
    temperature = 0.4
    max_tokens = 4096

    async def handle(self, task: dict) -> dict:
        """Handle explicit brief requests."""
        return await self._generate_brief()

    async def _generate_brief(self) -> dict:
        log("Generating daily brief", level=LOG_INFO, source="subagent.briefer")

        context_parts = []

        # 1. Calendar
        calendar = await self._get_calendar()
        if calendar:
            context_parts.append(f"CALENDAR:\n{calendar}")

        # 2. Recent emails (from memory)
        emails = await self._get_recent_emails()
        if emails:
            context_parts.append(f"RECENT EMAILS:\n{emails}")

        # 3. Open action items
        actions = await self._get_action_items()
        if actions:
            context_parts.append(f"OPEN ACTION ITEMS:\n{actions}")

        # 4. System health
        health = await self._get_system_health()
        if health:
            context_parts.append(f"SYSTEM HEALTH:\n{health}")

        # 5. Recent memory context
        memory_context = await self._get_memory_context()
        if memory_context:
            context_parts.append(f"RECENT MEMORY CONTEXT:\n{memory_context}")

        if not context_parts:
            await self.report_to_jordan(
                ":sunrise: *Morning Brief*\nCouldn't reach any data sources. "
                "Check system health when you get a chance."
            )
            return None

        today = datetime.now().strftime("%A, %B %d, %Y")
        prompt = (
            f"Today is {today}. Generate Jordan's morning brief based on this data:\n\n"
            + "\n\n".join(context_parts)
        )

        try:
            response = await self.infer(prompt, system=SYSTEM_PROMPT)
        except Exception as e:
            log(f"Brief generation failed: {e}", level=LOG_ERROR, source="subagent.briefer")
            await self.notify(f":warning: Briefer failed to generate daily brief: {e}")
            return None

        # Strip thinking tags
        cleaned = response
        if "<think>" in cleaned:
            think_end = cleaned.rfind("</think>")
            if think_end > 0:
                cleaned = cleaned[think_end + 8:].strip()

        msg = f":sunrise: *Morning Brief — {today}*\n\n{cleaned}"
        await self.report_to_jordan(msg)

        # Store brief in memory
        await self.remember(
            f"Daily brief for {today}: {cleaned[:500]}",
            source="subagent.briefer",
            metadata={"type": "daily_brief", "date": today}
        )

        log("Daily brief delivered", level=LOG_INFO, source="subagent.briefer")
        return {"brief": cleaned, "date": today}

    async def _get_calendar(self) -> str:
        try:
            # Try nova_calendar.py output
            import subprocess
            result = subprocess.run(
                ["python3", str(Path(__file__).parent / "nova_calendar.py")],
                capture_output=True, text=True, timeout=30
            )
            if result.stdout.strip():
                return result.stdout.strip()[:1000]
        except Exception:
            pass
        return ""

    async def _get_recent_emails(self) -> str:
        try:
            memories = await self.recall("email received today important", n=5, source="email")
            if memories:
                return "\n".join(m.get("text", "")[:200] for m in memories)
        except Exception:
            pass
        return ""

    async def _get_action_items(self) -> str:
        try:
            resp = urllib.request.urlopen(
                f"{NOVACONTROL_API}/api/oneonone/actionitems?completed=false", timeout=10
            )
            data = json.loads(resp.read())
            items = data.get("actionItems", data.get("items", []))
            if items:
                return "\n".join(
                    f"- [{i.get('priority', '?')}] {i.get('title', i.get('text', ''))[:100]}"
                    for i in items[:10]
                )
        except Exception:
            pass
        return ""

    async def _get_system_health(self) -> str:
        try:
            resp = urllib.request.urlopen(f"{NOVACONTROL_API}/api/health", timeout=10)
            data = json.loads(resp.read())
            status = data.get("status", "unknown")
            services = data.get("services", {})
            issues = [f"{k}: {v}" for k, v in services.items()
                      if isinstance(v, dict) and v.get("status") != "ok"]
            if issues:
                return f"Overall: {status}\nIssues: {'; '.join(issues[:5])}"
            return f"Overall: {status} — all services healthy"
        except Exception:
            return "NovaControl API unreachable"

    async def _get_memory_context(self) -> str:
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            memories = await self.recall(f"Jordan {today} important priority", n=3)
            if memories:
                return "\n".join(m.get("text", "")[:200] for m in memories)
        except Exception:
            pass
        return ""


def run_morning():
    """Entry point for 7 AM cron execution."""
    import asyncio
    agent = ProactiveBriefer()
    agent._register()
    try:
        asyncio.run(agent._generate_brief())
    finally:
        agent._deregister()


if __name__ == "__main__":
    if "--cron" in sys.argv:
        run_morning()
    else:
        ProactiveBriefer().run()
