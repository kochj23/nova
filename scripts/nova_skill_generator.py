#!/usr/bin/env python3
"""
nova_skill_generator.py — Generate Python scripts from detected skill patterns.

Takes a pattern_id, uses LLM (Haiku via OpenRouter) to generate a self-contained
Python script, writes it to auto_skills/, and registers in evolved_skills table.

Usage:
  python3 nova_skill_generator.py <pattern_id>
  python3 nova_skill_generator.py --approve <skill_id>
  python3 nova_skill_generator.py --reject <pattern_id>

Written by Jordan Koch (via Claude).
"""

import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

DB_HOST = "localhost"
DB_NAME = "nova_ops"
DB_USER = "kochj"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-haiku-4.5"
AUTO_SKILLS_DIR = Path.home() / ".openclaw/scripts/auto_skills"


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[skill-gen {ts}] {msg}", flush=True)


def db_query(sql: str) -> list:
    result = subprocess.run(
        ["psql", "-h", DB_HOST, "-U", DB_USER, "-d", DB_NAME, "-t", "-A", "-F", "\t", "-c", sql],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        return []
    return [line.split("\t") for line in result.stdout.strip().split("\n") if line.strip()]


def db_exec(sql: str):
    subprocess.run(
        ["psql", "-h", DB_HOST, "-U", DB_USER, "-d", DB_NAME, "-c", sql],
        capture_output=True, text=True, timeout=15
    )


def get_pattern(pattern_id: str) -> dict:
    rows = db_query(
        f"SELECT pattern_id, pattern_hash, tool_sequence::text, occurrence_count "
        f"FROM skill_patterns WHERE pattern_id = '{pattern_id}'"
    )
    if not rows or len(rows[0]) < 4:
        return {}
    return {
        "pattern_id": rows[0][0], "hash": rows[0][1],
        "sequence": json.loads(rows[0][2]), "count": int(rows[0][3])
    }


def generate_script(pattern: dict) -> tuple:
    """Use LLM to generate a Python script for this pattern. Returns (name, description, code)."""
    api_key = nova_config.openrouter_api_key()
    if not api_key:
        log("ERROR: No OpenRouter API key")
        return "", "", ""

    tools_desc = "\n".join(
        f"  {i+1}. {t['tool']}({', '.join(t.get('param_keys', []))})"
        for i, t in enumerate(pattern["sequence"])
    )

    system_prompt = """You are a Python script generator for Nova, an AI assistant system.
Given a tool-call sequence that's been detected as a repeated pattern, generate a self-contained
Python script that implements this workflow.

The script should:
1. Be a standalone executable Python script
2. Import from nova_config (sys.path includes the scripts dir)
3. Accept command-line arguments for any variable parameters
4. Print results to stdout
5. Exit with code 0 on success, 1 on failure
6. Be well-structured but concise (no excessive comments)

Available APIs:
- nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY) — post to Slack
- urllib.request for HTTP calls
- Memory server: http://192.168.1.6:18790/recall?q=QUERY&n=5
- Memory server: http://192.168.1.6:18790/remember (POST {text, source, metadata})
- Scheduler: http://127.0.0.1:37460/run/TASK_ID (trigger a task)

Output THREE things separated by ---SEPARATOR---:
1. A short snake_case name for this skill (e.g., morning_research_digest)
2. A one-line description
3. The complete Python script"""

    user_prompt = f"""Generate a skill script for this repeated pattern (seen {pattern['count']} times):

Tool sequence:
{tools_desc}

The script should automate this exact workflow as a single callable command."""

    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 2000,
    })

    req = urllib.request.Request(
        OPENROUTER_URL, data=payload.encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://nova.digitalnoise.net",
            "X-Title": "Nova Skill Generator",
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"].strip()

        parts = content.split("---SEPARATOR---")
        if len(parts) >= 3:
            name = re.sub(r'[^a-z0-9_]', '', parts[0].strip().lower())
            description = parts[1].strip()
            code = parts[2].strip()
            if code.startswith("```"):
                code = code.split("\n", 1)[1].rsplit("```", 1)[0]
            return name, description, code

        log("LLM response didn't match expected format")
        return "", "", ""
    except Exception as e:
        log(f"LLM generation failed: {e}")
        return "", "", ""


def write_and_register(pattern: dict, name: str, description: str, code: str):
    """Write script to auto_skills/ and register in evolved_skills table."""
    AUTO_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    script_path = AUTO_SKILLS_DIR / f"skill_{name}.py"
    script_path.write_text(code)
    script_path.chmod(0o755)
    log(f"Wrote: {script_path}")

    escaped_desc = description.replace("'", "''")
    escaped_path = str(script_path).replace("'", "''")
    params_json = json.dumps({
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Input query or parameters"}},
    }).replace("'", "''")

    db_exec(
        f"INSERT INTO evolved_skills (pattern_id, name, description, script_path, parameters, status) "
        f"VALUES ('{pattern['pattern_id']}', '{name}', '{escaped_desc}', "
        f"'{escaped_path}', '{params_json}'::jsonb, 'pending')"
    )
    db_exec(f"UPDATE skill_patterns SET status = 'approved' WHERE pattern_id = '{pattern['pattern_id']}'")
    log(f"Registered skill '{name}' (pending approval)")


def approve_skill(skill_id: str):
    """Activate a pending skill."""
    db_exec(f"UPDATE evolved_skills SET status = 'active', approved_at = now() WHERE skill_id = '{skill_id}'")
    rows = db_query(f"SELECT name FROM evolved_skills WHERE skill_id = '{skill_id}'")
    name = rows[0][0] if rows else skill_id
    nova_config.post_both(
        f":white_check_mark: Skill `{name}` activated. Gateway will pick it up on next reload.",
        slack_channel=nova_config.SLACK_NOTIFY
    )
    log(f"Approved skill: {name}")


def reject_pattern(pattern_id: str):
    """Reject a skill pattern."""
    db_exec(f"UPDATE skill_patterns SET status = 'rejected' WHERE pattern_id = '{pattern_id}'")
    log(f"Rejected pattern: {pattern_id}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: nova_skill_generator.py <pattern_id> | --approve <skill_id> | --reject <pattern_id>")
        sys.exit(1)

    if sys.argv[1] == "--approve" and len(sys.argv) >= 3:
        approve_skill(sys.argv[2])
    elif sys.argv[1] == "--reject" and len(sys.argv) >= 3:
        reject_pattern(sys.argv[2])
    else:
        pattern_id = sys.argv[1]
        pattern = get_pattern(pattern_id)
        if not pattern:
            log(f"Pattern not found: {pattern_id}")
            sys.exit(1)

        name, description, code = generate_script(pattern)
        if name and code:
            write_and_register(pattern, name, description, code)
        else:
            log("Generation failed — no script produced")
            sys.exit(1)
