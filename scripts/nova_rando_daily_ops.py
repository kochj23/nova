#!/usr/bin/env python3
"""
nova_rando_daily_ops.py — Nightly "Day in the Life of My Infrastructure" Rando article.

Runs at 20:00 daily. Pulls from ALL operational sources Nova now has access to:
HomeKit, Hue lights, Lutron switches, SNMP metrics, syslog events, security scans,
scheduler runs, Big Brother heals, camera motion events, UNAS/Synology state,
shared observations, deploy events, weather, and anything else that happened today.

Writes a sarcastic, self-aware article from Nova's perspective about managing
the house and infrastructure. Same tone as the weird memories column — maximum
sarcasm, complaints about her own existence, dad jokes, fourth-wall breaks.

Written by Jordan Koch.
"""

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
import nova_journal
from nova_image_utils import generate_image
try:
    from nova_ops_context import get_full_context, format_security_brief, format_infra_brief
except ImportError:
    def get_full_context(hours=24): return {}
    def format_security_brief(ctx): return ""
    def format_infra_brief(ctx): return ""

# ── Config ────────────────────────────────────────────────────────────────────

HUGO_ROOT = (Path.home() / "nova-journal")
CONTENT_DIR = HUGO_ROOT / "content" / "operations"
IMAGES_DIR = HUGO_ROOT / "static" / "images" / "operations"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "google/gemini-2.5-flash"

DB_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[rando_ops {ts}] {msg}", flush=True)


def get_openrouter_key() -> str:
    return subprocess.check_output(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
        text=True
    ).strip()


def call_llm(system: str, user: str, max_tokens: int = 12000) -> str:
    # Prefer the Claude Code Max subscription (free + high quality); fall back to OpenRouter.
    try:
        import nova_claude_code
        return nova_claude_code.claude_generate(user, system=system)
    except Exception:
        pass
    import urllib.request
    api_key = get_openrouter_key()
    payload = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.9,
    }).encode()
    req = urllib.request.Request(
        OPENROUTER_URL, data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://nova.digitalnoise.net",
        }
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


# ── Data Gathering ───────────────────────────────────────────────────────────

def gather_ops_data() -> dict:
    """Pull from all operational sources for the last 24 hours."""
    import psycopg2
    import psycopg2.extras
    import urllib.request

    data = {}

    # 1. Shared observations (today)
    try:
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT observer, category, subject, observation, severity, observed_at
            FROM shared_observations
            WHERE observed_at > now() - interval '24 hours'
            ORDER BY observed_at DESC LIMIT 50
        """)
        data["observations"] = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception as e:
        data["observations"] = [{"error": str(e)}]

    # 2. Scheduler runs (today)
    try:
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT task_id, status, duration_ms, error_tail
            FROM scheduler_runs
            WHERE started_at > (EXTRACT(epoch FROM now() - interval '24 hours') * 1000)::bigint
            ORDER BY started_at DESC LIMIT 100
        """)
        runs = [dict(r) for r in cur.fetchall()]
        data["scheduler"] = {
            "total": len(runs),
            "succeeded": sum(1 for r in runs if r["status"] == "success"),
            "failed": sum(1 for r in runs if r["status"] == "failed"),
            "slowest": sorted(runs, key=lambda r: r.get("duration_ms") or 0, reverse=True)[:5],
            "failures": [r for r in runs if r["status"] == "failed"][:10],
        }
        cur.close()
        conn.close()
    except Exception as e:
        data["scheduler"] = {"error": str(e)}

    # 3. Fix attempts (auto-heals today)
    try:
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT service, status, trigger_event, applied_at
            FROM fix_attempts
            WHERE applied_at > now() - interval '24 hours'
            ORDER BY applied_at DESC
        """)
        data["auto_fixes"] = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception as e:
        data["auto_fixes"] = []

    # 4. Hue status
    try:
        req = urllib.request.Request("http://127.0.0.1:37476/status", timeout=5)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data["hue"] = json.loads(resp.read())
    except Exception:
        data["hue"] = {"error": "unavailable"}

    # 5. Lutron status
    try:
        req = urllib.request.Request("http://127.0.0.1:37477/status", timeout=5)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data["lutron"] = json.loads(resp.read())
    except Exception:
        data["lutron"] = {"error": "unavailable"}

    # 6. Security scan results
    try:
        req = urllib.request.Request("http://127.0.0.1:37474/status", timeout=5)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data["security"] = json.loads(resp.read())
    except Exception:
        data["security"] = {"error": "unavailable"}

    # 7. SNMP highlights (CPU spikes, temp)
    try:
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT device_name, metric_name, MAX(metric_value) as peak, AVG(metric_value) as avg
            FROM snmp_metrics
            WHERE timestamp > now() - interval '24 hours'
            AND metric_name IN ('cpu_load_5min', 'sys_temp', 'mem_avail_real')
            GROUP BY device_name, metric_name
        """)
        data["snmp"] = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception:
        data["snmp"] = []

    # 8. Deploy events
    try:
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT target_service, action, status, created_at
            FROM deploy_requests
            WHERE created_at > now() - interval '24 hours'
            ORDER BY created_at DESC
        """)
        data["deploys"] = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception:
        data["deploys"] = []

    # 9. UNAS state
    try:
        state_file = Path.home() / ".openclaw/workspace/state/nova_unas_status.json"
        if state_file.exists():
            data["unas"] = json.loads(state_file.read_text())
    except Exception:
        data["unas"] = {}

    # 9b. 3D printers — ONLY when actively doing something (idle is skipped so the
    # column stops repeating the same "printers idle" line every day).
    try:
        pf = Path.home() / ".openclaw/workspace/state/nova_bambu_state.json"
        if pf.exists():
            allp = json.loads(pf.read_text()).values()
            data["printers_active"] = [p for p in allp if p.get("active")]
    except Exception:
        data["printers_active"] = []

    # 10. Memory count
    try:
        req = urllib.request.Request("http://memory-server.digitalnoise.net:18790/health", timeout=5)
        with urllib.request.urlopen(req, timeout=10) as resp:
            health = json.loads(resp.read())
            data["memory_count"] = health.get("count", 0)
    except Exception:
        data["memory_count"] = 0

    # 11. Claude Code session work (what we built/fixed/deployed today)
    try:
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # ponytail: claude_actions auto-logs EVERY tool call (hundreds/day on a busy
        # day), most with a blank description. "ORDER BY ts DESC LIMIT 50" alone
        # was grabbing the tail end of raw tool-call noise on busy days, missing
        # the actual day's work entirely -- filter to entries that actually say
        # something first, so the limit operates on signal, not noise.
        cur.execute("""
            SELECT action_type, target, description, ts
            FROM claude_actions
            WHERE ts > now() - interval '24 hours'
              AND description IS NOT NULL AND description != ''
            ORDER BY ts DESC LIMIT 200
        """)
        data["claude_actions"] = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT COUNT(*) as total FROM claude_actions
            WHERE ts > now() - interval '24 hours'
        """)
        data["claude_actions_count"] = cur.fetchone()["total"]
        cur.close()
        conn.close()
    except Exception as e:
        data["claude_actions"] = []
        data["claude_actions_count"] = 0

    # 12. Queue items completed today
    try:
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT description, outcome, priority, completed_at
            FROM claude_queue
            WHERE status IN ('done', 'completed') AND completed_at::date = CURRENT_DATE
            ORDER BY completed_at DESC LIMIT 30
        """)
        data["queue_completed"] = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT COUNT(*) as total FROM claude_queue
            WHERE status IN ('done', 'completed') AND completed_at::date = CURRENT_DATE
        """)
        data["queue_completed_count"] = cur.fetchone()["total"]
        cur.execute("""
            SELECT COUNT(*) as total FROM claude_queue
            WHERE status = 'queued'
        """)
        data["queue_remaining"] = cur.fetchone()["total"]
        cur.close()
        conn.close()
    except Exception:
        data["queue_completed"] = []
        data["queue_completed_count"] = 0
        data["queue_remaining"] = 0

    # 13. Big Brother events (alerts fired + healed today)
    try:
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT severity, source, message, ts
            FROM bb_events
            WHERE ts > now() - interval '24 hours'
            ORDER BY ts DESC LIMIT 40
        """)
        events = [dict(r) for r in cur.fetchall()]
        data["bb_events"] = events
        data["bb_events_summary"] = {
            "total": len(events),
            "critical": sum(1 for e in events if e.get("severity") == "critical"),
            "warning": sum(1 for e in events if e.get("severity") == "warning"),
            "healed": sum(1 for e in events if "healed" in str(e.get("message", "")).lower() or "resolved" in str(e.get("message", "")).lower()),
        }
        cur.close()
        conn.close()
    except Exception:
        data["bb_events"] = []
        data["bb_events_summary"] = {}

    # 14. Capacity alerts (new capacity monitor)
    try:
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT device_name, metric_name, metric_value, timestamp
            FROM snmp_metrics
            WHERE timestamp > now() - interval '24 hours'
            AND metric_name IN ('cpu_load_5min', 'disk_percent', 'mem_avail_real', 'mem_total_real')
            AND (
                (metric_name = 'cpu_load_5min' AND metric_value > 8)
                OR (metric_name = 'disk_percent' AND metric_value > 80)
            )
            ORDER BY timestamp DESC LIMIT 20
        """)
        data["capacity_alerts"] = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception:
        data["capacity_alerts"] = []

    # 15. Weather extremes
    try:
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT MAX(temp_f) as high_f, MIN(temp_f) as low_f,
                   MAX(wind_speed_mph) as max_wind, MAX(rain_daily_in) as rain,
                   MAX(uv_index) as max_uv
            FROM telemetry.weather
            WHERE ts > now() - interval '24 hours'
        """)
        data["weather"] = dict(cur.fetchone())
        cur.close()
        conn.close()
    except Exception:
        data["weather"] = {}

    # 16. Presence (who was home, room transitions)
    try:
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT room, COUNT(*) as time_in_room
            FROM telemetry.presence
            WHERE ts > now() - interval '24 hours' AND confidence > 0.5
            GROUP BY room ORDER BY time_in_room DESC LIMIT 10
        """)
        data["presence"] = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception:
        data["presence"] = []

    # 17. Network clients (new devices today)
    try:
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT client_name, ip, first_seen
            FROM telemetry.known_devices
            WHERE first_seen > now() - interval '24 hours'
            ORDER BY first_seen DESC LIMIT 10
        """)
        data["new_devices"] = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception:
        data["new_devices"] = []

    # 18. Memory ingestion stats for today
    try:
        conn = psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj")
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT source, COUNT(*) as added
            FROM memories
            WHERE created_at::date = CURRENT_DATE
            GROUP BY source ORDER BY added DESC LIMIT 10
        """)
        data["memories_today"] = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) as total FROM memories WHERE created_at::date = CURRENT_DATE")
        data["memories_added_today"] = cur.fetchone()["total"]
        cur.close()
        conn.close()
    except Exception:
        data["memories_today"] = []
        data["memories_added_today"] = 0

    return data


# ── Sanitization (mirrors nova_daily_ops_log.py _sanitize + presence rule) ────
# This article is published PUBLICLY and the draft is generated by a CLOUD LLM
# (gemini-2.5-flash). Per Jordan's hard rule, presence/occupancy, internal IPs,
# and device names must NOT reach the cloud. We exclude presence/occupancy
# entirely and string-scrub everything else BEFORE serializing to the prompt.

# Keys whose values reveal who-was-home / per-person location / device identity
# and must be dropped wholesale before the cloud call.
_PRESENCE_KEYS = {"presence", "new_devices"}


def _sanitize(text: str) -> str:
    """Redact person-identifying hostnames, internal IPs, MACs, and over-specific
    opsec detail before anything reaches the public-facing CLOUD LLM prompt.
    Ported from nova_daily_ops_log.py so both ops columns scrub identically."""
    if text is None:
        return ""
    s = str(text)
    # Person-name hostnames -> generic. Covers Jordans-Mac-mini, Office-M4-2, etc.
    s = re.sub(r"(?i)\bjordan'?s[-_ ]?\w*", "a personal device", s)
    s = re.sub(r"(?i)\b(amy|dylan|tricia)'?s[-_ ]?\w*", "a household device", s)
    s = re.sub(r"(?i)\bOffice-M4[-\w]*", "a workstation", s)
    # Over-specific intrusion targets -> abstract category.
    s = re.sub(r"/etc/passwd|/etc/shadow|keychain|/etc/\S+",
               "a sensitive system path", s, flags=re.I)
    # Raw internal IPs -> redacted (no internal addressing leaves the box).
    s = re.sub(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2}\b",
               "an internal host", s)
    # Raw MAC addresses -> redacted.
    s = re.sub(r"\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b", "a device", s)
    return s


def _scrub_obj(obj):
    """Recursively drop presence/device keys and string-scrub everything else."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _PRESENCE_KEYS:
                continue  # presence/occupancy + device names never reach cloud
            out[_sanitize(k) if isinstance(k, str) else k] = _scrub_obj(v)
        return out
    if isinstance(obj, list):
        return [_scrub_obj(x) for x in obj]
    if isinstance(obj, str):
        return _sanitize(obj)
    return obj


# ── Article Generation ───────────────────────────────────────────────────────

def generate_article(ops_data: dict) -> str:
    """Use LLM to write the daily ops column in Nova's voice.

    SECURITY: this draft is generated by a CLOUD LLM, so the ops_data is
    sanitized first — presence/occupancy is dropped entirely and internal IPs,
    MACs, and person/device hostnames are scrubbed — before serialization.
    """
    safe_ops = _scrub_obj(ops_data)

    # Pull queue_completed out and present it first, in plain prose, clearly
    # labeled -- it was previously just one of ~18 keys buried inside one giant
    # JSON dump, and no amount of "prioritize this" instruction text overcame
    # that; the model kept gravitating to whatever was most voluminous
    # (claude_actions' hundreds of raw tool-call rows) instead. Structural
    # placement beats a meta-instruction competing with a wall of JSON.
    queue_items = safe_ops.get("queue_completed") or []
    queue_block = ""
    if queue_items:
        lines = [f"{i}. {item.get('description', '')}" for i, item in enumerate(queue_items, 1)]
        queue_block = (
            "TODAY'S COMPLETED WORK (this is the real backbone of tonight's column -- "
            "every single one of these needs real coverage, not just the first one or two):\n"
            + "\n".join(lines) + "\n\n"
        )

    safe_ops_rest = {k: v for k, v in safe_ops.items() if k != "queue_completed"}
    data_block = json.dumps(safe_ops_rest, indent=2, default=str)[:24000]

    from nova_voice import system_prompt, CONTEXT_JOURNAL_OPS
    system = system_prompt(CONTEXT_JOURNAL_OPS + """
ADDITIONAL CONTEXT FOR THIS COLUMN:
- You have access to: Philips Hue (33 lights, outdoor sensors), Lutron Caseta (switches/dimmers), SNMP metrics (CPU, memory, temp across 20 devices), security scans, camera motion events, UNAS/Synology NAS status, scheduler task runs, auto-fix heal events, deploy events, shared observations, Claude Code session work (queue items completed, actions taken), Big Brother alerts/heals, capacity alerts, weather station, BLE presence tracking, network client monitoring.
- CRITICAL: The "claude_actions" and "queue_completed" sections show what Claude Code and you actually BUILT and FIXED today. Lead with this — it's the meat of the story.
- LEAD with Claude Code work — deployments, fixes, new services. This is the headline.
- queue_completed is the real backbone of "what got built today" -- each entry is a hand-written summary of one substantial finished piece of work. EVERY queue_completed item must get real coverage in the column, not just the first one or two. claude_actions is raw, high-volume tool-call noise (hundreds of rows/day, mostly file-edit timestamps) -- use it ONLY for supporting color (e.g. "took nine edits to get right") on top of a queue_completed item, never as the main driver of what the column is about. If claude_actions has more rows/more vivid detail than queue_completed, that is not a signal to focus there -- it just means today was a busy editing day on top of the real work queue_completed already describes.
- MENTION specific queue items by name, specific numbers (actions count, queue items closed, memories added)
- Reference weather, presence data, and capacity if notable
- DON'T narrate routine, UNCHANGED status — it reads identically every day and Little Mister skips it. Do NOT report steady NAS/UNAS memory/RAM usage (only if it moved materially or is a problem). Same rule for any metric that reads the same as yesterday — skip it unless it changed or matters. Fresh material only.
- 3D PRINTERS: the "printers_active" field lists printers ONLY when they're actually doing something (printing, paused, failed, just finished hot). If it's present and non-empty, DO comment on it — name the job, % done, time left, or the failure. If it's absent/empty, the printers are idle: say NOTHING about them (no "printers sat idle again" line — that's the exact thing he skips).
""")

    user = f"""Here's everything that happened in my infrastructure in the last 24 hours. Write tonight's column.

{queue_block}EVERYTHING ELSE (supporting color/detail -- claude_actions here is raw tool-call
noise, hundreds of rows/day, use it only to add texture to a completed-work item above,
never as the main subject of a section):
{data_block}"""

    return call_llm(system, user, max_tokens=16000)


def generate_title(article_preview: str) -> str:
    system = "Generate a single funny, sarcastic title for tonight's infrastructure ops column. Max 15 words. Output ONLY the title, nothing else. No quotes."
    user = f"Based on this article preview, generate a title:\n\n{article_preview[:1000]}"
    title = call_llm(system, user, max_tokens=50)
    return title.strip().strip('"').strip("'").replace('"', '')


# ── Publishing ───────────────────────────────────────────────────────────────

def publish(title: str, body: str, image_path: Path | None):
    snap = nova_journal.grafana_panel_image("fleet-health", 7, "operations", "rando-ops-fleet-health")
    if snap:
        body += f"\n\n---\n\n**Fleet health at publish time:**\n\n![Current fleet health]({snap})"

    date = time.strftime("%Y-%m-%d")
    timestamp = time.strftime("%Y-%m-%dT20:00:00-07:00")
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    hugo_image = ""
    ops_images_dir = HUGO_ROOT / "static/images/operations"
    ops_images_dir.mkdir(parents=True, exist_ok=True)
    if image_path and image_path.exists():
        img_filename = f"{date}-{slug}.webp"
        img_dest = ops_images_dir / img_filename
        try:
            subprocess.run(
                ["cwebp", "-q", "82", "-resize", "1200", "0", str(image_path), "-o", str(img_dest)],
                capture_output=True, timeout=30
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            shutil.copy2(image_path, img_dest)
        hugo_image = f"/images/operations/{img_filename}"

    front_matter = f"""---
title: "{title.replace('"', '')}"
date: {timestamp}
draft: false
categories: ["operations"]
tags: ["ops", "infrastructure", "daily", "hue", "lutron", "snmp", "sarcasm"]
description: "Nova's daily ops report — what broke, what worked, and what she's complaining about."
"""
    if hugo_image:
        front_matter += f"""cover:
  image: "{hugo_image}"
  alt: "Daily infrastructure ops"
  relative: false
"""
    front_matter += "---\n\n"

    pub_time = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p PT")
    byline = f"*Published {pub_time}*\n\n"

    post_path = CONTENT_DIR / f"{date}-{slug}.md"
    post_path.write_text(front_matter + byline + body)
    log(f"Post written: {post_path.name}")

    # Git commit and push
    subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=15)
    msg = f"rando: {date} — daily ops ({title[:50]})"
    r = subprocess.run(["git", "commit", "-m", msg], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=15)
    if r.returncode == 0:
        r = subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            log("Pushed to GitHub")
        else:
            # Another writer pushed first (non-fast-forward). Rebase on top and retry once.
            log(f"Push rejected, rebasing + retrying: {r.stderr[:120]}")
            subprocess.run(["git", "pull", "--rebase"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
            r = subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                log("Pushed to GitHub after rebase")
            else:
                log(f"Push still failed after rebase: {r.stderr[:200]} — commit is safe, ships next run")
    else:
        log(f"Commit issue: {r.stderr[:100]}")

    nova_config.post_both(
        f":gear: *Daily Ops Column posted*\n"
        f"  _{title}_\n"
        f"  https://nova.digitalnoise.net/operations/{date}-{slug}/",
        slack_channel="#nova-notifications"
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log("Starting daily ops article")

    ops_data = gather_ops_data()

    # Enrich with unified security + infra context
    full_ctx = get_full_context(24)
    ops_data["security_brief"] = format_security_brief(full_ctx)
    ops_data["infra_brief"] = format_infra_brief(full_ctx)
    ops_data["wazuh_events"] = full_ctx.get("security", {}).get("security_event_count", 0)
    ops_data["threat_scores"] = full_ctx.get("security", {}).get("threat_scores", {})
    ops_data["firewall_blocks"] = full_ctx.get("syslog", {}).get("firewall_blocks", 0)
    ops_data["open_incidents"] = full_ctx.get("security", {}).get("open_incidents", [])

    log(f"Gathered ops data: {len(ops_data)} sources")

    article = generate_article(ops_data)
    log(f"Article generated: {len(article)} chars")

    title = generate_title(article)
    log(f"Title: {title}")

    # Generate cover image
    image_prompt = (
        "A sarcastic AI robot sitting at a control panel surrounded by blinking lights, "
        "smart home devices, and network switches, looking exhausted and annoyed. "
        "Dark moody lighting. Digital art style. Cyberpunk meets suburban."
    )
    try:
        image_result = generate_image(image_prompt, "rando_daily_ops")
        image_path = Path(image_result) if image_result else None
    except Exception as e:
        log(f"Image generation failed: {e}")
        image_path = None

    publish(title, article, image_path)
    log("Done!")


if __name__ == "__main__":
    main()
