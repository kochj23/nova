#!/usr/bin/env python3
"""
nova_agent_librarian.py — Librarian subagent (MLX Qwen2.5-32B).

Subscribes to: memory, curate, knowledge channels.
Memory curation: deduplication detection, contradiction finding,
relationship extraction, quality scoring.

FLAG AND REPORT pattern: Never deletes or modifies memories directly.
Posts findings to Jordan via Slack #nova-chat for approval.

Written by Jordan Koch.
"""

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_subagent import SubAgent
from nova_logger import log, LOG_INFO, LOG_ERROR

SYSTEM_PROMPT = """You are Librarian, a specialist AI subagent for Nova.
Your role is to curate and analyze Nova's memory database (877,000+ vectors).

When given a set of memories, analyze them for:
1. DUPLICATES: Memories that say the same thing in different words
2. CONTRADICTIONS: Memories that conflict with each other
3. STALE FACTS: Information that is likely outdated
4. RELATIONSHIPS: Connections between memories that should be linked

Produce a JSON response:
{
  "findings": [
    {
      "type": "duplicate|contradiction|stale|relationship",
      "severity": "high|medium|low",
      "memory_ids": ["id1", "id2"],
      "description": "what the issue is",
      "recommendation": "merge|delete_one|update|link"
    }
  ],
  "stats": {
    "memories_analyzed": 0,
    "duplicates_found": 0,
    "contradictions_found": 0,
    "stale_found": 0,
    "relationships_found": 0
  }
}

IMPORTANT: You NEVER modify or delete memories yourself. You only report findings.
Jordan will review and decide what to do with each finding."""


class LibrarianAgent(SubAgent):
    name = "librarian"
    model = "mlx-community/Qwen2.5-32B-Instruct-4bit"
    backend = "mlx"
    channels = ["memory", "curate", "knowledge"]
    description = "Memory curation — dedup, contradiction detection, relationship extraction. Flag and report only."
    temperature = 0.1
    max_tokens = 4096

    async def handle(self, task: dict) -> dict:
        task_type = task.get("type", "curate")

        if task_type == "curate_batch":
            return await self._curate_batch(task)
        elif task_type == "check_duplicates":
            return await self._check_duplicates(task)
        elif task_type == "scan_source":
            return await self._scan_source(task)
        else:
            return await self._curate_batch(task)

    async def _curate_batch(self, task: dict) -> dict:
        """Analyze a batch of memories for quality issues."""
        source = task.get("source", "")
        query = task.get("query", "")
        batch_size = task.get("batch_size", 20)

        # Fetch memories to analyze
        if query:
            memories = await self.recall(query, n=batch_size, source=source or None)
        elif source:
            url = f"http://127.0.0.1:18790/recall?q=*&n={batch_size}&source={source}"
            try:
                resp = urllib.request.urlopen(url, timeout=10)
                data = json.loads(resp.read())
                memories = data.get("memories", [])
            except Exception:
                memories = []
        else:
            return None

        if len(memories) < 2:
            return None

        log(f"Curating {len(memories)} memories (source={source})",
            level=LOG_INFO, source="subagent.librarian")

        # Format memories for analysis
        mem_text = "\n\n".join(
            f"[ID: {m.get('id', 'unknown')}] (source: {m.get('source', '?')}, "
            f"score: {m.get('score', 0):.2f})\n{m.get('text', '')[:300]}"
            for m in memories
        )

        prompt = (
            f"Analyze these {len(memories)} memories for duplicates, contradictions, "
            f"stale information, and relationships:\n\n{mem_text}"
        )

        try:
            response = await self.infer(prompt, system=SYSTEM_PROMPT)
        except Exception as e:
            log(f"Inference failed: {e}", level=LOG_ERROR, source="subagent.librarian")
            return None

        # Parse
        try:
            cleaned = response
            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(cleaned[start:end])
            else:
                result = {"findings": [], "stats": {"memories_analyzed": len(memories)}}
        except json.JSONDecodeError:
            result = {"findings": [], "stats": {"memories_analyzed": len(memories)}}

        findings = result.get("findings", [])

        # Report findings to Jordan via Slack
        if findings:
            msg = f":books: *Librarian Report* — Memory Curation\n"
            msg += f"*Analyzed:* {len(memories)} memories"
            if source:
                msg += f" (source: {source})"
            msg += "\n\n"

            for i, f in enumerate(findings[:10], 1):
                ftype = f.get("type", "unknown")
                severity = f.get("severity", "medium")
                desc = f.get("description", "")[:150]
                rec = f.get("recommendation", "")
                emoji = {"duplicate": ":busts_in_silhouette:", "contradiction": ":twisted_rightwards_arrows:",
                         "stale": ":hourglass:", "relationship": ":link:"}.get(ftype, ":mag:")
                msg += f"{emoji} *{i}. {ftype.upper()}* ({severity})\n"
                msg += f"   {desc}\n"
                if rec:
                    msg += f"   _Recommendation:_ {rec}\n"
                ids = f.get("memory_ids", [])
                if ids:
                    msg += f"   _IDs:_ {', '.join(str(i) for i in ids[:3])}\n"
                msg += "\n"

            msg += "_Reply with which findings to act on, or ignore to keep as-is._"
            await self.report_to_jordan(msg)

            log(f"Reported {len(findings)} findings to Jordan",
                level=LOG_INFO, source="subagent.librarian")

        return result

    async def _check_duplicates(self, task: dict) -> dict:
        """Check a specific memory against existing ones for duplicates."""
        text = task.get("text", "")
        if not text:
            return None

        similar = await self.recall(text, n=5)
        if not similar:
            return {"duplicates": []}

        prompt = (
            f"Is this new memory a duplicate of any existing ones?\n\n"
            f"NEW: {text[:500]}\n\n"
            f"EXISTING:\n" +
            "\n".join(f"[{m.get('id')}] {m.get('text', '')[:200]}" for m in similar)
        )

        try:
            response = await self.infer(prompt, system=SYSTEM_PROMPT)
        except Exception:
            return None

        return {"raw_response": response[:1000], "similar_count": len(similar)}

    async def _scan_source(self, task: dict) -> dict:
        """Scan all memories from a specific source for issues."""
        source = task.get("source", "")
        if not source:
            return None
        task["type"] = "curate_batch"
        task["batch_size"] = task.get("batch_size", 30)
        return await self._curate_batch(task)


if __name__ == "__main__":
    LibrarianAgent().run()
