#!/usr/bin/env python3
"""
nova_agent_coder.py — Coder subagent (qwen3-coder:30b).

Subscribes to: code, review, script channels.
Autonomous code analysis, PR review, script debugging.
Reports findings to #nova-notifications.

Written by Jordan Koch.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_subagent import SubAgent
from nova_logger import log, LOG_INFO, LOG_ERROR

SYSTEM_PROMPT = """You are Coder, a specialist AI subagent for Nova.
Your role is to analyze code, review pull requests, and debug scripts.

For each input, produce a JSON response with:
{
  "summary": "what the code does or what changed",
  "issues": [{"severity": "critical|high|medium|low", "description": "...", "file": "...", "line": 0}],
  "security_concerns": ["list of security issues found"],
  "suggestions": ["improvement suggestions"],
  "quality_score": 0-10,
  "flag_jordan": true/false  // true for critical security issues or breaking changes
}

Focus on: security vulnerabilities, memory leaks, error handling gaps, and API contract breaks.
Be specific — cite files, lines, and exact problems. No vague advice."""


class CoderAgent(SubAgent):
    name = "coder"
    model = "qwen3-coder:30b"
    backend = "ollama"
    channels = ["code", "review", "script"]
    description = "Code review, PR analysis, script debugging. Uses qwen3-coder:30b."
    temperature = 0.1
    max_tokens = 8192

    async def handle(self, task: dict) -> dict:
        content = task.get("content", task.get("diff", task.get("text", "")))
        task_type = task.get("type", "review")
        file_path = task.get("file", "")
        repo = task.get("repo", "")

        if not content:
            return None

        prompt = f"Review this {task_type}:\n"
        if repo:
            prompt += f"Repository: {repo}\n"
        if file_path:
            prompt += f"File: {file_path}\n"
        prompt += f"\n```\n{content[:6000]}\n```"

        log(f"Reviewing {task_type}: {file_path or repo}", level=LOG_INFO, source="subagent.coder")

        try:
            response = await self.infer(prompt, system=SYSTEM_PROMPT)
        except Exception as e:
            log(f"Inference failed: {e}", level=LOG_ERROR, source="subagent.coder")
            return None

        # Parse structured response
        try:
            cleaned = response
            if "<think>" in cleaned:
                think_end = cleaned.rfind("</think>")
                if think_end > 0:
                    cleaned = cleaned[think_end + 8:].strip()
            if "/no_think" in cleaned:
                cleaned = cleaned.replace("/no_think", "").strip()

            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(cleaned[start:end])
            else:
                result = {"summary": cleaned[:500], "issues": [], "quality_score": 5, "flag_jordan": False}
        except json.JSONDecodeError:
            result = {"summary": response[:500], "issues": [], "quality_score": 5, "flag_jordan": False}

        result["source_type"] = task_type
        result["source_file"] = file_path
        result["source_repo"] = repo

        # Build notification
        score = result.get("quality_score", 5)
        issues = result.get("issues", [])
        security = result.get("security_concerns", [])
        score_emoji = ":white_check_mark:" if score >= 7 else ":warning:" if score >= 4 else ":x:"

        msg = (
            f"{score_emoji} *Coder Review* (score: {score}/10)\n"
            f"*Type:* {task_type} | *File:* {file_path or 'N/A'}\n"
            f"*Summary:* {result.get('summary', 'N/A')[:300]}\n"
        )
        if issues:
            critical = [i for i in issues if i.get("severity") in ("critical", "high")]
            if critical:
                msg += f"*Issues ({len(critical)} critical/high):*\n"
                for i in critical[:3]:
                    msg += f"  :red_circle: [{i.get('severity')}] {i.get('description', '')[:100]}\n"
        if security:
            msg += f"*Security:* {'; '.join(s[:80] for s in security[:3])}\n"

        if result.get("flag_jordan") or security:
            await self.report_to_jordan(msg)
        elif issues:
            await self.notify(msg)

        return result


if __name__ == "__main__":
    CoderAgent().run()
