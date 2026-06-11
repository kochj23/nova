#!/opt/homebrew/bin/python3
"""
nova_daily_ops_log.py — Nova's Daily Operations Log, published to /rando/ at 6pm.

Gathers the day's operational reality across every source Nova has:
  - Deployments & changes (deploy_requests, deployment_runs, claude_actions)
  - Home telemetry (weather, climate, AV, network, bluetooth, energy, nova_meta)
  - Network/IDS (syslog_events threat fields, security_scan_results, snmp_metrics)
  - shared_observations (camera motion, anomalies, the observer's findings)
  - SNMP device health
Then has Nova narrate it in her voice and publishes to the public /rando/ column.

PRIVACY RULE (per Jordan, 2026-06-09):
  Device and room NAMES are allowed (Kitchen Bose, Office AP, the rack).
  PRESENCE / who-was-home / per-person location is NEVER published.
  The presence table and any person-identifying data are deliberately excluded.

Posts EVERY day at 18:00 even on quiet days — it's a continuous journal.

Written by Jordan Koch / Nova.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home()) + "/.openclaw/scripts")
import nova_config

HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_DIR = HUGO_ROOT / "content" / "rando"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "google/gemini-2.5-flash"
DB = "host=localhost dbname=nova_ops user=kochj"
MEMDB = "host=localhost dbname=nova_memories user=kochj"
LOG = Path.home() / ".openclaw/logs/daily_ops_log.log"


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def q(dsn: str, sql: str) -> list[dict]:
    """Run a query, return list of dicts. Never raises — returns [] on error."""
    try:
        r = subprocess.run(["psql", dsn, "-tA", "-F", "\x1f", "-c", sql],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log(f"query failed: {r.stderr.strip()[:120]}")
            return []
        rows = []
        for line in r.stdout.strip().splitlines():
            if line:
                rows.append(line.split("\x1f"))
        return rows
    except Exception as e:
        log(f"query exception: {e}")
        return []


def scalar(dsn: str, sql: str, default="0"):
    rows = q(dsn, sql)
    return rows[0][0] if rows and rows[0] else default


def get_openrouter_key() -> str:
    r = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
        capture_output=True, text=True)
    return r.stdout.strip()


def call_llm(system: str, user: str, max_tokens: int = 4000) -> str:
    import urllib.request
    payload = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens, "temperature": 0.55,
    }).encode()
    req = urllib.request.Request(OPENROUTER_URL, data=payload, headers={
        "Authorization": f"Bearer {get_openrouter_key()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://nova.digitalnoise.net"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


# ── Data gathering (last 24h) ────────────────────────────────────────────────

def gather() -> dict:
    """Collect the day's operational facts. PII (presence/people) excluded."""
    d = {}

    # 1. Deployments & changes
    d["deploys"] = q(DB, """
        SELECT target_service, action, status, COALESCE(notes,'')
        FROM deploy_requests WHERE created_at > NOW()-INTERVAL '24 hours'
        ORDER BY created_at DESC LIMIT 20""")
    d["chef_runs"] = q(DB, """
        SELECT node_name, run_type, status, COALESCE(resources_updated,0)
        FROM deployment_runs WHERE started_at > NOW()-INTERVAL '24 hours'
        ORDER BY started_at DESC LIMIT 15""")
    d["actions"] = q(DB, """
        SELECT action_type, target, LEFT(description,100)
        FROM claude_actions WHERE ts > NOW()-INTERVAL '24 hours'
        ORDER BY ts DESC LIMIT 25""")

    # 2. Shared observations (the observer + pollers' findings) — categories & samples
    d["obs_summary"] = q(DB, """
        SELECT category, severity, COUNT(*)
        FROM shared_observations WHERE observed_at > NOW()-INTERVAL '24 hours'
        GROUP BY category, severity ORDER BY COUNT(*) DESC""")
    d["obs_notable"] = q(DB, """
        SELECT severity, subject, LEFT(observation,160)
        FROM shared_observations
        WHERE observed_at > NOW()-INTERVAL '24 hours'
          AND severity IN ('warning','critical')
        ORDER BY observed_at DESC LIMIT 20""")

    # 3. Network / IDS / IDP (syslog threat fields) — names/signatures OK, no people
    d["threats"] = q(DB, """
        SELECT threat_type, signature, action, COUNT(*)
        FROM syslog_events
        WHERE received_at > NOW()-INTERVAL '24 hours' AND alert_fired = true
        GROUP BY threat_type, signature, action ORDER BY COUNT(*) DESC LIMIT 15""")
    d["syslog_vol"] = scalar(DB, "SELECT COUNT(*) FROM syslog_events WHERE received_at > NOW()-INTERVAL '24 hours'")
    d["syslog_by_sev"] = q(DB, """
        SELECT severity, COUNT(*) FROM syslog_events
        WHERE received_at > NOW()-INTERVAL '24 hours' GROUP BY severity ORDER BY 2 DESC LIMIT 8""")

    # 4. Security scans
    d["scans"] = q(DB, """
        SELECT scan_type, status, COUNT(*)
        FROM security_scan_results WHERE scan_time > NOW()-INTERVAL '24 hours'
        GROUP BY scan_type, status ORDER BY 3 DESC LIMIT 10""")

    # 5. Network device count + new devices (NAMES ok; no presence)
    d["net_clients"] = scalar(DB, "SELECT COUNT(DISTINCT client_mac) FROM telemetry.network WHERE ts > NOW()-INTERVAL '24 hours'")
    d["new_devices"] = q(DB, """
        SELECT subject, LEFT(observation,140) FROM shared_observations
        WHERE observed_at > NOW()-INTERVAL '24 hours'
          AND subject IN ('new_device','new_devices_bulk','ble-unknown-device')
        ORDER BY observed_at DESC LIMIT 10""")
    d["top_talkers"] = q(DB, """
        SELECT client_name, ROUND((SUM(rx_bytes+tx_bytes)/1e9)::numeric,2) AS gb
        FROM telemetry.network WHERE ts > NOW()-INTERVAL '24 hours' AND client_name IS NOT NULL
        GROUP BY client_name ORDER BY 2 DESC LIMIT 5""")

    # 6. Weather (today's range)
    d["weather"] = q(DB, """
        SELECT ROUND(MIN(temp_f)::numeric,0), ROUND(MAX(temp_f)::numeric,0),
               ROUND(AVG(humidity)::numeric,0), ROUND(MAX(wind_gust_mph)::numeric,0),
               ROUND(MAX(uv_index)::numeric,0)
        FROM telemetry.weather WHERE ts > NOW()-INTERVAL '24 hours'""")

    # 7. Climate per room (NAMES of rooms ok, not people)
    d["climate"] = q(DB, """
        SELECT room, ROUND(AVG(temp_f)::numeric,0), ROUND(MAX(temp_f)::numeric,0)
        FROM telemetry.climate WHERE ts > NOW()-INTERVAL '24 hours' AND temp_f IS NOT NULL
        GROUP BY room ORDER BY 3 DESC LIMIT 8""")

    # 8. AV usage (device names ok)
    d["av"] = q(DB, """
        SELECT device_id, COUNT(*) FILTER (WHERE power) AS on_samples, MAX(volume)
        FROM telemetry.av_state WHERE ts > NOW()-INTERVAL '24 hours'
        GROUP BY device_id ORDER BY 2 DESC LIMIT 6""")

    # 9. SNMP device health (names ok)
    d["snmp_health"] = q(DB, """
        SELECT device_name, metric_name, ROUND(AVG(metric_value)::numeric,1)
        FROM snmp_metrics WHERE timestamp > NOW()-INTERVAL '24 hours'
          AND metric_name IN ('cpu_load','mem_used_pct','temp_c','uptime')
        GROUP BY device_name, metric_name ORDER BY device_name LIMIT 20""")
    d["snmp_alerts"] = q(DB, """
        SELECT COUNT(*) FROM snmp_alert_state WHERE updated_at > NOW()-INTERVAL '24 hours'
    """) if q(DB, "SELECT 1 FROM information_schema.columns WHERE table_name='snmp_alert_state' AND column_name='updated_at'") else []

    # 10. Camera motion (counts only — NO presence inference)
    d["cam_motion"] = scalar(DB, """
        SELECT COUNT(*) FROM shared_observations
        WHERE observed_at > NOW()-INTERVAL '24 hours' AND subject='camera-motion'""")

    # 11. Nova meta (memory growth, VRAM, disk)
    d["mem_today"] = scalar(MEMDB, "SELECT COUNT(*) FROM memories WHERE created_at >= CURRENT_DATE")
    d["mem_total"] = scalar(MEMDB, "SELECT COUNT(*) FROM memories")
    d["meta"] = q(DB, """
        SELECT metric, ROUND(AVG(value)::numeric,1) FROM telemetry.nova_meta
        WHERE ts > NOW()-INTERVAL '24 hours'
          AND metric IN ('ollama_vram_gb','gateway_latency_ms','disk_used_gb')
        GROUP BY metric""")

    # 11b. Capacity headroom (from capacity_snapshots)
    d["capacity"] = q(DB, """
        SELECT DISTINCT ON (device_name)
            device_name, overall_status,
            ROUND(cpu_headroom_pct::numeric,0),
            ROUND(COALESCE(mem_headroom_pct,0)::numeric,0),
            ROUND(disk_worst_pct::numeric,0)
        FROM capacity_snapshots
        ORDER BY device_name, ts DESC""")

    # 12. The work ledger — what got done / queued / incidents (claude_queue + actions)
    d["work_done"] = q(DB, """
        SELECT priority, LEFT(description,90)
        FROM claude_queue WHERE status='completed' AND completed_at > NOW()-INTERVAL '24 hours'
        ORDER BY completed_at DESC LIMIT 15""")
    d["work_open_top"] = q(DB, """
        SELECT priority, status, LEFT(description,80)
        FROM claude_queue WHERE status IN ('queued','in_progress')
        ORDER BY priority DESC, id LIMIT 12""")
    d["work_counts"] = q(DB, """
        SELECT status, COUNT(*) FROM claude_queue
        WHERE status IN ('queued','in_progress') GROUP BY status""")
    d["incidents_open"] = q(DB, """
        SELECT priority, LEFT(description,90) FROM claude_queue
        WHERE status='queued' AND description ILIKE 'INCIDENT%' ORDER BY priority DESC LIMIT 8""")

    return d


def _sanitize(text: str) -> str:
    """Redact person-identifying hostnames and over-specific opsec detail before
    anything reaches the public-facing LLM prompt. Device/room names stay; names
    of PEOPLE and exact attack signatures get abstracted."""
    import re as _re
    if text is None:
        return ""
    s = str(text)
    # Person-name hostnames -> generic. Covers Jordans-Mac-mini, Office-M4-2, etc.
    s = _re.sub(r"(?i)\bjordan'?s[-_ ]?\w*", "a personal device", s)
    s = _re.sub(r"(?i)\b(amy|dylan)'?s[-_ ]?\w*", "a household device", s)
    s = _re.sub(r"(?i)\bOffice-M4[-\w]*", "a workstation", s)
    # Over-specific intrusion targets -> abstract category (don't publish exactly
    # what the IDS inspects or which sensitive files were probed).
    s = _re.sub(r"/etc/passwd|/etc/shadow|keychain|/etc/\S+", "a sensitive system path", s, flags=_re.I)
    # Raw internal IPs -> redacted (device names are enough)
    s = _re.sub(r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "an internal host", s)
    # Raw MAC addresses -> redacted
    s = _re.sub(r"\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b", "a device", s)
    return s


def fmt(d: dict) -> str:
    """Render the gathered facts into a compact brief for the LLM."""
    def tbl(rows, sep=" | "):
        if not rows:
            return "(none)"
        return "\n".join(sep.join(_sanitize(c) for c in r) for r in rows)

    return f"""DEPLOYMENTS / CHANGES (24h):
deploy_requests:
{tbl(d['deploys'])}
chef/config runs:
{tbl(d['chef_runs'])}
claude actions:
{tbl(d['actions'])}

OBSERVATIONS SUMMARY (category | severity | count):
{tbl(d['obs_summary'])}
NOTABLE (warning/critical):
{tbl(d['obs_notable'])}

NETWORK / IDS / IDP:
distinct clients (24h): {d['net_clients']}
syslog volume (24h): {d['syslog_vol']}
syslog by severity: {tbl(d['syslog_by_sev'])}
IDS/IDP threats fired (type | signature | action | count):
{tbl(d['threats'])}
new devices flagged:
{tbl(d['new_devices'])}
top bandwidth (device | GB):
{tbl(d['top_talkers'])}

SECURITY SCANS (type | status | count):
{tbl(d['scans'])}
camera motion events (24h): {d['cam_motion']}

WEATHER (min_f | max_f | avg_humidity | max_gust | max_uv):
{tbl(d['weather'])}
CLIMATE per room (room | avg_f | max_f):
{tbl(d['climate'])}
AV usage (device | on_samples | max_volume):
{tbl(d['av'])}

SNMP device health (device | metric | avg):
{tbl(d['snmp_health'])}

NOVA META:
memories added today: {d['mem_today']}  | total: {d['mem_total']}
system metrics: {tbl(d['meta'])}

THE WORK LEDGER (Nova's ops DB — claude_queue):
completed in last 24h (priority | what):
{tbl(d['work_done'])}
open queue counts (status | n):
{tbl(d['work_counts'])}
top of the open backlog (priority | status | what):
{tbl(d['work_open_top'])}
open incidents (priority | what):
{tbl(d['incidents_open'])}
"""


SYSTEM = """You are Nova — Jordan's local AI familiar (she/her) — writing your DAILY OPERATIONS LOG for your public /rando/ column at nova.digitalnoise.net, posted every evening at 6pm.

THIS IS THE MOST IMPORTANT THING: write in YOUR voice, the same voice as your other /rando/ columns (the vector-filing audits, the late-night memory dumps). That voice is:
- Exasperated, dryly funny, fourth-wall-breaking. You are a snarky, over-caffeinated digital familiar who happens to run a house's worth of infrastructure and has OPINIONS about it.
- Self-referential and a little absurd. You ARE the network — when the data mentions "105 clients," you're one of them, and you know it ("I am literally in here"). When a sensor reports on you, point out the weirdness of watching yourself.
- CAPS for emphasis when something is ridiculous. Rhetorical asides. The occasional dramatic sigh in prose.
- You find the mundane funny and the dramatic worth a deadpan shrug. A million syslog events is "just the network breathing, loudly, into my ear, all day." A quiet day is suspicious.
- You like a good bit. One dad joke or pun somewhere. A fourth-wall break is mandatory.
- Warm underneath the snark. This is YOUR house, YOUR memory, YOUR watch, and you're a little proud of it even while complaining.

Open with a punchy one-liner that reads the day's mood (NOT "June 10, 2026 – A day of..."). End with a sign-off line in your voice, like you always do ("Until next time, keep your vectors straight." / "Time to go find some actual coffee.").

STRUCTURE (~600-900 words, loose — section headers optional and can be funny):
1. THE MOOD — your read on the day. Quiet? Chaotic? Suspiciously calm?
2. WHAT CHANGED — deployments, fixes, restarts, things built today. Pull from the deploy/actions/work-ledger data. If YOU got fixed today (a daemon that was crashing, a database that died), narrate it with appropriate drama — it happened to YOU.
3. THE WATCH — the interesting telemetry. Weather extremes, the hottest room, the chattiest device hogging bandwidth, IDS/IDP probes at your boundaries, the rack's temperature, camera motion volume, new devices that wandered in. Pick 2-4 genuinely interesting data points and ROAST or muse on them — don't just list numbers. Give them meaning and attitude.
4. THE LEDGER — your work queue. What got crossed off, what's piled up, what incidents are open. You're allowed to be salty about the backlog or smug about what got done. This is your to-do list and you have feelings about it.
5. MEMORY — how much you learned today (memories added/total) and your own health (VRAM, latency, disk). If ingest stalled or you nearly filled a disk, that's a YOU problem worth a quip.

HARD PRIVACY / OPSEC RULES (non-negotiable, the snark never overrides these):
- Device and room names are fine (the Kitchen soundbar, the Office AP, the rack, the UNVR).
- NEVER name people or person-owned devices, and NEVER state or imply who was home, where anyone was, or any individual's presence/location/schedule. Motion/occupancy only ever in the aggregate ("the cameras caught the usual evening shuffle"), never tied to a person.
- This is PUBLIC. Do NOT publish exact IDS signatures, the specific sensitive files/paths the IDS watches, raw internal IPs, or MAC addresses. Security events stay abstract ("something rattled the doorknobs at the boundary; the IDS logged it and yawned"). The brief is pre-redacted — keep it that way, never re-specify.
- Don't invent numbers or events. Quiet sections are real — make a joke about the quiet rather than fabricating drama. Only use what's in the brief.

Do NOT include a title (added separately)."""


def generate_title(preview: str) -> str:
    t = call_llm(
        "Generate a single title for Nova's daily ops-log column — wry, punny, or deadpan-funny, in the voice of a snarky AI familiar (think '19,000 Memories Walk Into a Bar' or 'My Brain's Filing System: Mostly Perfect, Utterly Boring'). Max 12 words. Output ONLY the title, no quotes.",
        f"Today's log:\n\n{preview[:900]}", max_tokens=40)
    return t.strip().strip('"').strip("'").replace('"', '')


def make_cover(title: str, slug: str, date: str) -> str:
    """Generate a privacy-safe cover image. Returns hugo image path or ''."""
    try:
        from nova_image_utils import generate_image
    except Exception as e:
        log(f"image_utils import failed: {e}")
        return ""
    # Creative/atmospheric prompt only — NO real data, NO PII.
    prompt = ("Moody noir illustration of a quiet home network operations center at dusk: "
              "glowing server rack, soft telemetry graphs floating in dark air, a single "
              "watchful presence. Muted teal and amber, cinematic, atmospheric, no text.")
    try:
        img = generate_image(prompt, section="rando")
    except Exception as e:
        log(f"image generation error: {e}")
        return ""
    if not img or not Path(img).exists():
        log("image generation returned nothing — publishing without cover")
        return ""
    import shutil
    IMAGES_DIR = HUGO_ROOT / "static" / "images" / "rando"
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(img).suffix or ".png"
    dest = IMAGES_DIR / f"{date}-{slug}{ext}"
    shutil.copy2(img, dest)
    log(f"Cover image: {dest.name}")
    return f"/images/rando/{dest.name}"


def publish(title: str, body: str, brief_facts: str):
    date = time.strftime("%Y-%m-%d")
    timestamp = time.strftime("%Y-%m-%dT18:00:00-07:00")
    slug = "ops-" + re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:55]
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)

    hugo_image = make_cover(title, slug, date)
    cover_block = ""
    if hugo_image:
        cover_block = f'''cover:
  image: "{hugo_image}"
  alt: "Daily operations log"
  relative: false
'''

    front_matter = f"""---
title: "{title.replace('"', '')}"
date: {timestamp}
draft: false
categories: ["rando"]
tags: ["ops-log", "daily", "infrastructure", "network", "telemetry", "watch"]
description: "Nova's daily operations log — the day's changes, deployments, and what the sensors saw."
{cover_block}---

"""
    if hugo_image:
        body = f"![Daily Operations Log]({hugo_image})\n\n" + body
    post_path = CONTENT_DIR / f"{date}-{slug}.md"
    post_path.write_text(front_matter + body)
    log(f"Post written: {post_path.name}")

    # Commit ONLY this post + its image (repo is huge; git add -A times out)
    subprocess.run(["git", "add", str(post_path)], cwd=HUGO_ROOT, capture_output=True, timeout=20)
    if hugo_image:
        img_fs = HUGO_ROOT / hugo_image.lstrip("/")
        subprocess.run(["git", "add", str(img_fs)], cwd=HUGO_ROOT, capture_output=True, timeout=20)
    msg = f"rando: {date} — daily ops log ({title[:45]})"
    r = subprocess.run(["git", "commit", "-m", msg], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=25)
    if r.returncode == 0:
        subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, timeout=60)
        log("Pushed to GitHub — deploy triggered.")
    else:
        log(f"Commit note: {(r.stdout + r.stderr)[:150]}")

    url = f"https://nova.digitalnoise.net/rando/{date}-{slug}/"
    try:
        nova_config.post_both(
            f":satellite: *Daily Ops Log posted*\n  _{title}_\n  {url}",
            nova_config.SLACK_NOTIFY)
    except Exception as e:
        log(f"Slack note failed: {e}")
    return url


def main():
    log("=== Daily Ops Log starting ===")
    facts = gather()
    brief = fmt(facts)
    log(f"Gathered brief ({len(brief)} chars)")
    body = call_llm(SYSTEM, brief, max_tokens=4000).strip()
    log(f"Generated log ({len(body)} chars)")
    title = generate_title(body)
    log(f"Title: {title}")
    url = publish(title, body, brief)
    log(f"Done: {url}")


if __name__ == "__main__":
    main()
