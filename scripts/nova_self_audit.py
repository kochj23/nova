#!/usr/bin/env python3

"""
Nova Self-Audit — cross-checks claimed capabilities against reality.

Verifies:
1. Scripts referenced in MEMORY.md and scheduler.yaml actually exist on disk
2. Scripts on disk that aren't documented in MEMORY.md (unknown capabilities)
3. Services/ports claimed in MEMORY.md are actually listening
4. Scheduler tasks reference scripts that exist
5. Processes that should be running (scheduler, gateway, subagents, etc.)

Posts discrepancies to Slack. Designed to be run on-demand or weekly via cron.

Written by Jordan Koch.
"""

import json
import logging
import os
import re
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.expanduser("~/.openclaw/logs/self-audit.log")),
        logging.StreamHandler(),
    ],
)

SCRIPTS_DIR = Path.home() / ".openclaw/scripts"
MEMORY_MD = Path.home() / ".openclaw/workspace/MEMORY.md"
IDENTITY_MD = Path.home() / ".openclaw/workspace/IDENTITY.md"
SCHEDULER_YAML = Path.home() / ".openclaw/config/scheduler.yaml"

EXPECTED_SERVICES = {
    18789: {"name": "OpenClaw Gateway", "path": "/health"},
    18790: {"name": "Memory Server", "path": "/health"},
    11434: {"name": "Ollama", "path": "/"},
    37421: {"name": "OneOnOne", "path": "/api/status"},
    37432: {"name": "HomekitControl", "path": "/api/status"},
}

EXPECTED_PROCESSES = [
    {"name": "Scheduler", "match": "nova_scheduler.py"},
    {"name": "Gateway", "match": "openclaw-gateway"},
    {"name": "Slack Preprocessor", "match": "nova_slack_preprocessor.py"},
    {"name": "Memory Server", "match": "memory_server.py"},
]


AUDIT_STATE_FILE = Path.home() / ".openclaw/workspace/state/self_audit_state.json"


def _load_last_audit_state():
    try:
        if AUDIT_STATE_FILE.exists():
            with open(AUDIT_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_audit_state(state):
    AUDIT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_STATE_FILE, "w") as f:
        json.dump(state, f)


def _scripts_on_disk():
    scripts = set()
    for ext in ("*.py", "*.sh"):
        for p in SCRIPTS_DIR.glob(ext):
            scripts.add(p.name)
    return scripts


def _scripts_in_file(path):
    if not path.exists():
        return set()
    text = path.read_text()
    pattern = re.compile(r'(?:nova_[a-z0-9_]+\.(?:py|sh)|dream_[a-z0-9_]+\.(?:py|sh))')
    return set(pattern.findall(text))


def _scripts_in_scheduler():
    if not SCHEDULER_YAML.exists():
        return {}
    text = SCHEDULER_YAML.read_text()
    refs = {}
    current_task = None
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.endswith(":") and not stripped.startswith("#") and not stripped.startswith("-"):
            candidate = stripped.rstrip(":")
            if candidate not in ("tasks", "scheduler", "slack"):
                current_task = candidate
        if "script:" in stripped and current_task:
            script_name = stripped.split("script:")[1].strip()
            refs[current_task] = script_name
    return refs


def _port_listening(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(("127.0.0.1", port))
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def _process_running(match_str):
    try:
        result = subprocess.run(
            ["pgrep", "-f", match_str],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def audit_scripts():
    issues = []
    info = []

    on_disk = _scripts_on_disk()
    in_memory = _scripts_in_file(MEMORY_MD)
    scheduler_refs = _scripts_in_scheduler()
    scheduler_scripts = set(scheduler_refs.values())

    # Scripts referenced in MEMORY.md but missing from disk
    missing_from_disk = in_memory - on_disk
    for s in sorted(missing_from_disk):
        issues.append(f"MEMORY.md references `{s}` but it doesn't exist on disk")

    # Scripts in scheduler but missing from disk
    for task, script in sorted(scheduler_refs.items()):
        if script not in on_disk:
            issues.append(f"Scheduler task `{task}` references `{script}` but it doesn't exist")

    # Scripts on disk but not documented anywhere
    documented = in_memory | scheduler_scripts
    nova_scripts = {s for s in on_disk if s.startswith("nova_") or s.startswith("dream_")}
    undocumented = nova_scripts - documented
    # Filter out test/debug scripts and subagent scripts (they're mentioned by category)
    skip_prefixes = ("test_", "debug_", "nova_agent_")
    undocumented = {s for s in undocumented if not any(s.startswith(p) for p in skip_prefixes)}
    if undocumented:
        info.append(f"{len(undocumented)} scripts on disk not in MEMORY.md or scheduler:")
        for s in sorted(undocumented):
            info.append(f"  - {s}")

    # Scripts in scheduler that aren't in MEMORY.md
    sched_not_documented = scheduler_scripts - in_memory
    sched_not_documented = {s for s in sched_not_documented if not any(s.startswith(p) for p in skip_prefixes)}
    if sched_not_documented:
        info.append(f"{len(sched_not_documented)} scheduler scripts not in MEMORY.md:")
        for s in sorted(sched_not_documented):
            info.append(f"  - {s}")

    return issues, info, len(on_disk), len(in_memory), len(scheduler_refs)


def audit_services():
    issues = []
    ok = []

    for port, svc in sorted(EXPECTED_SERVICES.items()):
        name = svc["name"]
        if _port_listening(port):
            ok.append(f"{name} (:{port})")
        else:
            issues.append(f"{name} (:{port}) is not listening")

    return issues, ok


def audit_processes():
    issues = []
    ok = []

    for proc in EXPECTED_PROCESSES:
        if _process_running(proc["match"]):
            ok.append(proc["name"])
        else:
            issues.append(f"{proc['name']} (`{proc['match']}`) is not running")

    return issues, ok


def audit_docs():
    issues = []

    if not MEMORY_MD.exists():
        issues.append("MEMORY.md is missing")
    elif MEMORY_MD.stat().st_size < 100:
        issues.append("MEMORY.md appears empty or minimal")

    if not IDENTITY_MD.exists():
        issues.append("IDENTITY.md is missing")
    else:
        text = IDENTITY_MD.read_text()
        if "pick something you like" in text or "Name:**\n" in text:
            issues.append("IDENTITY.md still has blank template fields")

    return issues


def run_audit():
    logging.info("Starting self-audit...")
    all_issues = []
    all_info = []

    # 1. Script audit
    script_issues, script_info, disk_count, mem_count, sched_count = audit_scripts()
    all_issues.extend(script_issues)
    all_info.extend(script_info)

    # 2. Service audit
    svc_issues, svc_ok = audit_services()
    all_issues.extend(svc_issues)

    # 3. Process audit
    proc_issues, proc_ok = audit_processes()
    all_issues.extend(proc_issues)

    # 4. Documentation audit
    doc_issues = audit_docs()
    all_issues.extend(doc_issues)

    # Build report
    lines = ["*Nova Self-Audit Report*"]
    lines.append("")

    # Summary
    lines.append(f"*Scripts:* {disk_count} on disk, {mem_count} in MEMORY.md, {sched_count} in scheduler")
    lines.append(f"*Services:* {len(svc_ok)}/{len(EXPECTED_SERVICES)} up — {', '.join(svc_ok) if svc_ok else 'none'}")
    lines.append(f"*Processes:* {len(proc_ok)}/{len(EXPECTED_PROCESSES)} running — {', '.join(proc_ok) if proc_ok else 'none'}")

    if all_issues:
        lines.append("")
        lines.append(f"*Issues ({len(all_issues)}):*")
        for issue in all_issues:
            lines.append(f"  !! {issue}")

    if all_info:
        lines.append("")
        lines.append("*Info:*")
        for info_line in all_info:
            lines.append(f"  {info_line}")

    if not all_issues and not all_info:
        lines.append("")
        lines.append("All clear — no discrepancies found.")

    report = "\n".join(lines)
    print(report)

    # Only post to Slack if issues changed since last run (prevent spam)
    issue_key = json.dumps(sorted(all_issues))
    last_state = _load_last_audit_state()
    changed = issue_key != last_state.get("last_issue_key", "")

    if all_issues and changed:
        slack_post(report)
        logging.info(f"Self-audit complete: {len(all_issues)} issue(s) posted to Slack (new)")
    elif all_issues:
        logging.info(f"Self-audit complete: {len(all_issues)} issue(s), unchanged — skipping Slack")
    else:
        if last_state.get("last_issue_key", "") != "[]":
            slack_post(report)
            logging.info("Self-audit complete: all clear (issues resolved) — posted to Slack")
        else:
            logging.info("Self-audit complete: no issues found")

    _save_audit_state({"last_issue_key": issue_key, "last_run": str(datetime.now())})

    return len(all_issues)


def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_CHAN)


if __name__ == "__main__":
    issue_count = run_audit()
    sys.exit(1 if issue_count > 0 else 0)
