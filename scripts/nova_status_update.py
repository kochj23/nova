#!/usr/bin/env python3
"""
nova_status_update.py — Write a STATUS.md with live system state every 30 minutes.

Nova reads this file when answering status questions — no need to run live checks.
This is more reliable than hoping the LLM executes curl commands correctly.

Written by Jordan Koch.
"""

import json
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

WORKSPACE = Path.home() / ".openclaw/workspace"
STATUS_FILE = WORKSPACE / "STATUS.md"


def check(url: str, timeout: int = 3) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def app_status(port: int, name: str) -> str:
    d = check(f"http://127.0.0.1:{port}/api/status")
    if d.get("status") == "running" or d.get("app"):
        return f"✅ {name} running (port {port})"
    return f"❌ {name} not running (port {port})"


def main():
    now = datetime.now()

    # Memory server
    mem = check("http://127.0.0.1:18790/health")
    mem_status = f"✅ ONLINE — {mem.get('count', '?')} memories, model: {mem.get('model', '?')}" \
        if mem.get("status") == "ok" else "❌ DOWN"

    # Vector DB breakdown
    mem_detail = ""
    if mem.get("status") == "ok":
        stats = check("http://127.0.0.1:18790/stats")
        by_source = stats.get("by_source", {})
        if by_source:
            lines = [f"  - {src}: {cnt}" for src, cnt in sorted(by_source.items(), key=lambda x: -x[1])[:8]]
            mem_detail = "\n" + "\n".join(lines)

    # Apps
    apps = [
        app_status(37421, "OneOnOne"),
        app_status(37422, "MLXCode"),
        app_status(37423, "NMAPScanner"),
        app_status(37424, "RsyncGUI"),
        app_status(37432, "HomekitControl"),
    ]

    # Ollama
    ollama = check("http://127.0.0.1:11434/api/tags")
    if ollama.get("models"):
        models = [m["name"] for m in ollama["models"]]
        ollama_status = f"✅ Ollama running — {len(models)} models loaded"
    else:
        ollama_status = "❌ Ollama not responding"

    # OpenClaw
    try:
        result = subprocess.run(
            ["openclaw", "cron", "status"],
            capture_output=True, text=True, timeout=5
        )
        cron_data = json.loads(result.stdout) if result.returncode == 0 else {}
        cron_status = f"✅ Gateway up — {cron_data.get('jobs', '?')} cron jobs active" \
            if cron_data.get("enabled") else "❌ Gateway not responding"
    except Exception:
        cron_status = "❌ Gateway not responding"

    content = f"""# Nova System Status
*Updated: {now.strftime('%Y-%m-%d %H:%M')} (auto-refreshed every 30 minutes)*

**This file is always current. Use it to answer status questions. Do NOT rely on HEARTBEAT.md for status.**

## Memory System
{mem_status}{mem_detail}

## Ollama / Model
{ollama_status}

## OpenClaw
{cron_status}

## Apps
{chr(10).join(apps)}

## Key Facts
- Vector DB has **{mem.get('count', 0):,} memories** including full Corvette C6 workshop manual (9,664 chunks)
- Dream journal: generates at 2am, delivers at 9am
- Email: checked every 5 minutes via system cron
- `openclaw plugin install` does NOT exist — never suggest it
"""

    STATUS_FILE.write_text(content, encoding="utf-8")
    print(f"[nova_status_update] Status written: {STATUS_FILE}")
    print(f"  Memory: {mem_status[:60]}")


if __name__ == "__main__":
    main()
