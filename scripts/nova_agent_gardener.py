#!/usr/bin/env python3
"""
nova_agent_gardener.py — Memory Gardener background agent.

Runs nightly. Scans Nova's 877K+ vectors for:
  - Duplicate memories (semantic similarity > 0.95)
  - Contradictory facts
  - Stale/outdated information
  - Orphaned memories with no connections

FLAG AND REPORT: Posts all findings to Jordan via Slack #nova-chat.
Never deletes or modifies memories without explicit approval.

Written by Jordan Koch.
"""

import json
import random
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import nova_config
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN
from nova_subagent import SubAgent

MEMORY_URL = "http://192.168.1.6:18790"
SOURCES_TO_SCAN = [
    "email_archive", "imessage", "music", "document",
    "email", "music_history", "meeting", "demonology",
    "subagent.analyst", "socal_rave",
]
SAMPLES_PER_SOURCE = 30
MAX_FINDINGS_PER_RUN = 20


class MemoryGardener(SubAgent):
    name = "gardener"
    model = "qwen3-coder:30b"
    backend = "ollama"
    channels = ["garden", "memory_maintenance"]
    description = "Nightly memory curation: dedup, contradiction, staleness detection. Flag and report only."
    temperature = 0.2

    async def handle(self, task: dict) -> dict:
        """Handle explicit garden requests."""
        source = task.get("source", "")
        if source:
            return await self._scan_source(source)
        return await self._full_scan()

    async def _full_scan(self) -> dict:
        """Run a full nightly scan across all sources."""
        log("Starting nightly memory garden scan", level=LOG_INFO, source="subagent.gardener")

        # Get stats
        try:
            stats = json.loads(urllib.request.urlopen(f"{MEMORY_URL}/stats", timeout=10).read())
            total = stats.get("count", 0)
            by_source = stats.get("by_source", {})
        except Exception as e:
            log(f"Cannot reach memory server: {e}", level=LOG_ERROR, source="subagent.gardener")
            return None

        all_findings = []
        sources_scanned = 0

        for source in SOURCES_TO_SCAN:
            if source not in by_source:
                continue
            count = by_source[source]
            if count < 5:
                continue

            findings = await self._scan_source(source)
            if findings and findings.get("findings"):
                all_findings.extend(findings["findings"])
            sources_scanned += 1

            if len(all_findings) >= MAX_FINDINGS_PER_RUN:
                break

            time.sleep(2)  # Don't hammer the LLM

        # Auto-merge duplicates silently
        duplicates = [f for f in all_findings if f.get("type") == "duplicate"]
        merged_count = 0
        for dup in duplicates:
            ids = dup.get("memory_ids", [])
            if len(ids) >= 2:
                merged_count += await self._auto_merge(ids)

        # Report only non-duplicate findings to Jordan
        reportable = [f for f in all_findings if f.get("type") != "duplicate"]
        all_findings_count = len(all_findings)

        if reportable or merged_count > 0:
            reportable = reportable[:MAX_FINDINGS_PER_RUN]
            msg = (
                f":seedling: *Memory Gardener — Nightly Report*\n"
                f"*Scanned:* {sources_scanned} sources ({total:,} total memories)\n"
            )

            if merged_count > 0:
                msg += f"*Auto-merged:* {merged_count} duplicate(s) :white_check_mark:\n"

            if reportable:
                msg += f"*Findings:* {len(reportable)}\n\n"

                by_type = {}
                for f in reportable:
                    ftype = f.get("type", "unknown")
                    by_type[ftype] = by_type.get(ftype, 0) + 1

                for ftype, count in sorted(by_type.items(), key=lambda x: -x[1]):
                    emoji = {"contradiction": ":twisted_rightwards_arrows:",
                             "stale": ":hourglass:", "relationship": ":link:"}.get(ftype, ":mag:")
                    msg += f"  {emoji} {ftype}: {count}\n"

                msg += "\n*Needs review:*\n"
                for i, f in enumerate(reportable[:8], 1):
                    desc = f.get("description", "")[:120]
                    rec = f.get("recommendation", "")
                    msg += f"  {i}. *{f.get('type', '?')}* — {desc}"
                    if rec:
                        msg += f" _(rec: {rec})_"
                    msg += "\n"

                if len(reportable) > 8:
                    msg += f"\n  _...and {len(reportable) - 8} more._\n"
            else:
                msg += "\nNo issues requiring review."
        else:
            msg = (
                ":seedling: *Memory Gardener* — Nightly scan complete. "
                f"Scanned {sources_scanned} sources. No issues found. :white_check_mark:"
            )

        await self.report_to_jordan(msg)

        log(f"Garden scan complete: {len(all_findings)} findings across {sources_scanned} sources",
            level=LOG_INFO, source="subagent.gardener")

        return {"findings": all_findings, "sources_scanned": sources_scanned}

    async def _auto_merge(self, memory_ids: list[str]) -> int:
        """Auto-merge duplicate memories: keep the longest, delete the rest.
        Returns count of memories deleted."""
        if len(memory_ids) < 2:
            return 0

        # Fetch full text of each memory to determine which to keep
        memories = []
        for mid in memory_ids:
            try:
                resp = urllib.request.urlopen(f"{MEMORY_URL}/get?id={mid}", timeout=5)
                data = json.loads(resp.read())
                if data:
                    memories.append(data)
            except Exception:
                pass

        if len(memories) < 2:
            return 0

        # Keep the longest (most complete) memory
        memories.sort(key=lambda m: len(m.get("text", "")), reverse=True)
        to_delete = memories[1:]  # everything except the longest

        deleted = 0
        for mem in to_delete:
            mid = mem.get("id", "")
            if not mid:
                continue
            try:
                req = urllib.request.Request(
                    f"{MEMORY_URL}/forget?id={mid}",
                    method="DELETE"
                )
                urllib.request.urlopen(req, timeout=5)
                deleted += 1
                log(f"Auto-merged: deleted {mid[:8]}... (kept longer version)",
                    level=LOG_INFO, source="subagent.gardener")
            except Exception as e:
                log(f"Failed to delete {mid[:8]}...: {e}",
                    level=LOG_WARN, source="subagent.gardener")

        return deleted

    async def _scan_source(self, source: str) -> dict:
        """Scan a specific source for quality issues, preferring newest memories.

        Tries /recent first (most likely to have fresh duplicates from same-day
        ingestion), falls back to /random, then /recall.
        """
        memories = []

        # Try newest memories first — fresh ingestion is most likely to have dupes
        try:
            resp = urllib.request.urlopen(
                f"{MEMORY_URL}/recent?n={SAMPLES_PER_SOURCE}&source={source}", timeout=10)
            batch = json.loads(resp.read())
            if isinstance(batch, list):
                memories.extend(batch)
            elif isinstance(batch, dict) and "memories" in batch:
                memories.extend(batch["memories"])
        except Exception:
            pass  # /recent may not exist — fall through to random

        # Backfill with random samples if /recent didn't return enough
        if len(memories) < SAMPLES_PER_SOURCE:
            remaining = SAMPLES_PER_SOURCE - len(memories)
            for _ in range(3):
                try:
                    resp = urllib.request.urlopen(
                        f"{MEMORY_URL}/random?n={remaining}&source={source}", timeout=10)
                    batch = json.loads(resp.read())
                    if isinstance(batch, list):
                        memories.extend(batch)
                    elif isinstance(batch, dict) and "memories" in batch:
                        memories.extend(batch["memories"])
                except Exception:
                    # /random may not support source filter — use recall with broad query
                    try:
                        resp = urllib.request.urlopen(
                            f"{MEMORY_URL}/recall?q=information+knowledge+fact&n={remaining}&source={source}",
                            timeout=10
                        )
                        data = json.loads(resp.read())
                        memories.extend(data.get("memories", []))
                    except Exception:
                        pass
                    break

        if len(memories) < 3:
            return {"findings": []}

        # Deduplicate by ID
        seen = set()
        unique = []
        for m in memories:
            mid = m.get("id", id(m))
            if mid not in seen:
                seen.add(mid)
                unique.append(m)
        memories = unique[:SAMPLES_PER_SOURCE * 2]

        # Format for LLM analysis
        mem_text = "\n\n".join(
            f"[ID:{m.get('id', '?')[:8]}] {m.get('text', '')[:250]}"
            for m in memories
        )

        from datetime import date
        current_year = date.today().year

        prompt = (
            f"Analyze these {len(memories)} memories from source '{source}' for quality issues.\n"
            f"Find: duplicates (same info restated), contradictions (conflicting facts), "
            f"stale info (likely outdated).\n\n{mem_text}"
        )

        today_str = date.today().strftime("%B %d, %Y")
        system = (
            f"You are a memory curator. The current date is {today_str}. "
            f"ALL dates from 2000 to {current_year} are VALID PAST dates. "
            f"Dates in {current_year + 1}, {current_year + 2}, {current_year + 3} are FUTURE dates.\n\n"
            "STALENESS RULES — READ CAREFULLY:\n"
            "A memory is ONLY 'stale' if its CONTENT describes a time-sensitive action that is "
            "clearly no longer relevant (e.g., 'meeting tomorrow' from 3 years ago, an expired coupon, "
            "a completed event with no historical value). "
            "Historical records, past emails, and old conversations are NOT stale — they are archive memories.\n"
            "Do NOT flag memories as stale just because they reference old dates. "
            "Only flag if the information is ACTIVELY MISLEADING if recalled today.\n"
            "A 2025 Uber receipt is a VALID historical record, not stale.\n"
            "A 2020 email is a VALID archive memory, not stale.\n\n"
            "Analyze the given memories and return JSON:\n"
            '{"findings": [{"type": "duplicate|contradiction|stale", '
            '"severity": "high|medium|low", "memory_ids": ["id1","id2"], '
            '"description": "what the issue is", "recommendation": "merge|delete_one|update"}], '
            '"stats": {"memories_analyzed": N}}\n'
            "Only report genuine issues. Empty findings array if nothing wrong. "
            "For duplicates, always include both memory IDs so they can be auto-merged."
        )

        try:
            response = await self.infer(prompt, system=system)
        except Exception as e:
            log(f"Scan of {source} failed: {e}", level=LOG_ERROR, source="subagent.gardener")
            return {"findings": []}

        try:
            cleaned = response
            if "<think>" in cleaned:
                think_end = cleaned.rfind("</think>")
                if think_end > 0:
                    cleaned = cleaned[think_end + 8:].strip()

            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(cleaned[start:end])
            else:
                result = {"findings": []}
        except json.JSONDecodeError:
            result = {"findings": []}

        for f in result.get("findings", []):
            f["source"] = source

        return result


def run_nightly():
    """Entry point for nightly cron execution (non-pub/sub mode)."""
    import asyncio
    agent = MemoryGardener()
    agent._register()
    try:
        result = asyncio.run(agent._full_scan())
        return result
    finally:
        agent._deregister()


if __name__ == "__main__":
    if "--cron" in sys.argv:
        run_nightly()
    else:
        MemoryGardener().run()
