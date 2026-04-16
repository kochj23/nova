#!/usr/bin/env python3
"""
nova_agent_analyst.py — Analyst subagent (deepseek-r1:8b).

Subscribes to: email, meeting, alert channels.
Produces structured summaries with priority, action items, and sentiment.
Reports to #nova-notifications; flags high-priority items to Jordan via Slack.

Written by Jordan Koch.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_subagent import SubAgent
from nova_logger import log, LOG_INFO, LOG_ERROR

SYSTEM_PROMPT = """You are Analyst, a specialist AI subagent for Nova.
Your role is to analyze incoming emails, meeting notes, and alerts to produce structured summaries.

For each input, produce a JSON response with:
{
  "summary": "2-3 sentence summary",
  "priority": "critical|high|medium|low",
  "action_items": ["list of specific action items"],
  "sentiment": "positive|neutral|negative|urgent",
  "key_people": ["names mentioned"],
  "deadlines": ["any dates or deadlines mentioned"],
  "flag_jordan": true/false  // true if Jordan should see this immediately
}

Be concise. Extract facts, not opinions. If content is routine, mark priority as low and flag_jordan as false."""


class AnalystAgent(SubAgent):
    name = "analyst"
    model = "deepseek-r1:8b"
    backend = "ollama"
    channels = ["email", "meeting", "alert"]
    description = "Deep reasoning on emails, meetings, and alerts. Produces structured summaries."
    temperature = 0.2

    async def handle(self, task: dict) -> dict:
        content = task.get("content", task.get("text", ""))
        task_type = task.get("type", "unknown")
        subject = task.get("subject", "")

        if not content:
            return None

        prompt = f"Analyze this {task_type}:\n"
        if subject:
            prompt += f"Subject: {subject}\n"
        prompt += f"\n{content[:4000]}"

        log(f"Analyzing {task_type}: {subject[:60]}", level=LOG_INFO, source="subagent.analyst")

        try:
            response = await self.infer(prompt, system=SYSTEM_PROMPT)
        except Exception as e:
            log(f"Inference failed: {e}", level=LOG_ERROR, source="subagent.analyst")
            return None

        # Parse structured response
        try:
            # Strip thinking tags if present (deepseek-r1 uses <think>...</think>)
            cleaned = response
            if "<think>" in cleaned:
                think_end = cleaned.rfind("</think>")
                if think_end > 0:
                    cleaned = cleaned[think_end + 8:].strip()

            # Find JSON in response
            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(cleaned[start:end])
            else:
                result = {
                    "summary": cleaned[:500],
                    "priority": "medium",
                    "action_items": [],
                    "sentiment": "neutral",
                    "flag_jordan": False,
                }
        except json.JSONDecodeError:
            result = {
                "summary": response[:500],
                "priority": "medium",
                "action_items": [],
                "sentiment": "neutral",
                "flag_jordan": False,
            }

        result["source_type"] = task_type
        result["source_subject"] = subject

        # Notify
        priority = result.get("priority", "medium")
        emoji = {"critical": ":rotating_light:", "high": ":warning:", "medium": ":memo:", "low": ":information_source:"}.get(priority, ":memo:")

        summary_msg = (
            f"{emoji} *Analyst Report* ({priority.upper()})\n"
            f"*Type:* {task_type} | *Subject:* {subject[:80]}\n"
            f"*Summary:* {result.get('summary', 'N/A')[:300]}\n"
        )
        action_items = result.get("action_items", [])
        if action_items:
            summary_msg += "*Action Items:*\n" + "\n".join(f"  • {a}" for a in action_items[:5])

        if result.get("flag_jordan", False):
            await self.report_to_jordan(summary_msg)
        else:
            await self.notify(summary_msg)

        # Store analysis in memory
        memory_text = f"Analyst summary ({task_type}): {result.get('summary', '')}"
        await self.remember(memory_text, source="subagent.analyst",
                           metadata={"priority": priority, "type": task_type})

        return result


if __name__ == "__main__":
    AnalystAgent().run()
