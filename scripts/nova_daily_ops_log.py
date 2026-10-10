#!/opt/homebrew/bin/python3
"""
nova_daily_ops_log.py — Nova's Daily Operations Log, published to /operations/ at 6pm.

Gathers the day's operational reality across every source Nova has:
  - Deployments & changes (deploy_requests, deployment_runs, claude_actions)
  - Home telemetry (weather, climate, AV, network, bluetooth, energy, nova_meta)
  - Network/IDS (syslog_events threat fields, security_scan_results, snmp_metrics)
  - shared_observations (camera motion, anomalies, the observer's findings)
  - SNMP device health
Then has Nova narrate it in her voice and publishes to the public /operations/ column.

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
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path.home()) + "/.openclaw/scripts")
import nova_config
import nova_voice
import nova_journal
from nova_notify import notify

HUGO_ROOT = (Path.home() / "nova-journal")
CONTENT_DIR = HUGO_ROOT / "content" / "operations"   # rando retired -> operations
import nova_dsn as _nova_dsn  # noqa: E402
DB = _nova_dsn.pg_dsn("nova_ops")
import nova_dsn as _nova_dsn  # noqa: E402
MEMDB = _nova_dsn.pg_dsn("nova_memories")
LOG = Path.home() / ".openclaw/logs/daily_ops_log.log"
GH_OWNER = "kochj23"
# Memory server /remember endpoint (operations vector). Mirrors nova_config.VECTOR_URL.
MEMORY_REMEMBER_URL = getattr(nova_config, "VECTOR_URL", "http://memory-server.digitalnoise.net:18790/remember")


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


def call_llm(system: str, user: str, max_tokens: int = 4000) -> str:
    """Delegate to the shared local Claude Code CLI path (see nova_journal.call_openrouter —
    OpenRouter itself ran dry 2026-07-17; this also fixes nova-core, which has no working
    route to Keychain-backed secrets at all, unlike this script's old direct OpenRouter call."""
    return nova_journal.call_openrouter(system, user, max_tokens=max_tokens)


# ── GitHub daily activity (last 24h) ─────────────────────────────────────────

def _gh_json(args: list, default=None):
    """Run a `gh` command expecting JSON output. Never raises — returns default
    (or []) on any error/non-zero exit, exactly like the RSS fetcher's resilience."""
    try:
        r = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log(f"gh {' '.join(args[:3])} failed: {r.stderr.strip()[:120]}")
            return [] if default is None else default
        out = r.stdout.strip()
        if not out:
            return [] if default is None else default
        return json.loads(out)
    except Exception as e:
        log(f"gh exception ({' '.join(args[:3])}): {e}")
        return [] if default is None else default


def github_daily_stats() -> tuple[str, dict]:
    """Gather the past 24h of GitHub activity across Jordan's own repos.

    Returns (human_summary_string, structured_dict). Every gh call is wrapped so
    that a single repo 403ing on traffic (no push access) or any error is skipped,
    never fatal — the aggregate is still returned.
    """
    since = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")

    stats = {
        "date": today,
        "since": since,
        "repos_total": 0,
        "prs_opened": 0,
        "issues_opened": 0,
        "prs_merged": 0,
        "clones_recent_day": 0,
        "unique_cloners_recent_day": 0,
        "clones_14d": 0,
        "unique_cloners_14d": 0,
        "views_14d": 0,
        "unique_viewers_14d": 0,
        "repos_with_traffic": 0,
        "repos_traffic_denied": 0,
        "per_repo": [],          # [{name, clones_day, clones_14d, uniques_14d, views_14d}]
        "top_clones": [],        # top repos by 14d clones
        "pr_titles": [],         # [{repo, title}]
        "issue_titles": [],      # [{repo, title}]
        "merged_titles": [],     # [{repo, title}]
    }

    # 1. Repo list (his own repos)
    repos = _gh_json(["repo", "list", GH_OWNER, "--limit", "200",
                      "--json", "name,nameWithOwner,visibility"])
    stats["repos_total"] = len(repos)

    # 2a. New PRs opened in the last day (search across all his repos)
    prs = _gh_json(["search", "prs", "--owner", GH_OWNER,
                    "--created", f">={since}", "--limit", "100",
                    "--json", "title,repository"])
    stats["prs_opened"] = len(prs)
    for p in prs:
        repo = (p.get("repository") or {}).get("nameWithOwner", "?")
        stats["pr_titles"].append({"repo": repo, "title": (p.get("title") or "")[:80]})

    # 2b. New issues opened in the last day (Jordan's "MRs" = issues/PRs)
    issues = _gh_json(["search", "issues", "--owner", GH_OWNER,
                       "--created", f">={since}", "--limit", "100",
                       "--json", "title,repository"])
    stats["issues_opened"] = len(issues)
    for i in issues:
        repo = (i.get("repository") or {}).get("nameWithOwner", "?")
        stats["issue_titles"].append({"repo": repo, "title": (i.get("title") or "")[:80]})

    # 2c. Merged PRs in the last day
    merged = _gh_json(["search", "prs", "--owner", GH_OWNER,
                       "--merged-at", f">={since}", "--limit", "100",
                       "--json", "title,repository"])
    stats["prs_merged"] = len(merged)
    for m in merged:
        repo = (m.get("repository") or {}).get("nameWithOwner", "?")
        stats["merged_titles"].append({"repo": repo, "title": (m.get("title") or "")[:80]})

    # 3. Per-repo traffic: clones + views (requires push access; skip on 403/error)
    for r in repos:
        name = r.get("name")
        if not name:
            continue
        clones = _gh_json(["api", f"repos/{GH_OWNER}/{name}/traffic/clones"], default={})
        if not isinstance(clones, dict) or "count" not in clones:
            # 403 (no access) or any error — skip this repo, not fatal
            stats["repos_traffic_denied"] += 1
            continue
        stats["repos_with_traffic"] += 1
        c14 = int(clones.get("count", 0) or 0)
        cu14 = int(clones.get("uniques", 0) or 0)
        daily = clones.get("clones", []) or []
        # Most recent day in the breakdown
        c_day = int(daily[-1]["count"]) if daily else 0
        cu_day = int(daily[-1]["uniques"]) if daily else 0

        views = _gh_json(["api", f"repos/{GH_OWNER}/{name}/traffic/views"], default={})
        v14 = int(views.get("count", 0) or 0) if isinstance(views, dict) else 0
        vu14 = int(views.get("uniques", 0) or 0) if isinstance(views, dict) else 0

        stats["clones_recent_day"] += c_day
        stats["unique_cloners_recent_day"] += cu_day
        stats["clones_14d"] += c14
        stats["unique_cloners_14d"] += cu14
        stats["views_14d"] += v14
        stats["unique_viewers_14d"] += vu14
        stats["per_repo"].append({
            "name": name, "clones_day": c_day, "clones_14d": c14,
            "uniques_14d": cu14, "views_14d": v14,
        })

    # Top repos by 14-day clones
    stats["top_clones"] = sorted(
        stats["per_repo"], key=lambda x: x["clones_14d"], reverse=True)[:5]

    # Human-readable summary
    lines = [
        f"GitHub activity (last 24h, as of {today}):",
        f"  Repos scanned: {stats['repos_total']} "
        f"({stats['repos_with_traffic']} with traffic data, "
        f"{stats['repos_traffic_denied']} skipped/no-access)",
        f"  New PRs opened: {stats['prs_opened']}",
        f"  New issues opened: {stats['issues_opened']}",
        f"  PRs merged: {stats['prs_merged']}",
        f"  Clones (most recent day): {stats['clones_recent_day']} "
        f"({stats['unique_cloners_recent_day']} unique cloners)",
        f"  Clones (14d total): {stats['clones_14d']} "
        f"({stats['unique_cloners_14d']} unique)",
        f"  Views (14d total): {stats['views_14d']} "
        f"({stats['unique_viewers_14d']} unique viewers)",
    ]
    if stats["top_clones"]:
        top = ", ".join(f"{t['name']} ({t['clones_14d']} clones)"
                        for t in stats["top_clones"])
        lines.append(f"  Top repos by clones (14d): {top}")
    if stats["pr_titles"]:
        lines.append("  New PRs: " + "; ".join(
            f"{p['repo']}: {p['title']}" for p in stats["pr_titles"][:5]))
    if stats["merged_titles"]:
        lines.append("  Merged: " + "; ".join(
            f"{m['repo']}: {m['title']}" for m in stats["merged_titles"][:5]))

    return "\n".join(lines), stats


def ingest_github_stats(summary: str, stats: dict) -> bool:
    """Ingest the GitHub activity summary into the operations memory vector.

    Mirrors the canonical /remember POST pattern used across Nova's ingest
    scripts: {"text", "source", "metadata"}. source='operations' so tomorrow's
    recall can see 'yesterday we had N clones across M repos'. Never raises."""
    metadata = {
        "type": "github_stats",
        "date": stats.get("date"),
        "totals": {
            "repos_total": stats.get("repos_total"),
            "prs_opened": stats.get("prs_opened"),
            "issues_opened": stats.get("issues_opened"),
            "prs_merged": stats.get("prs_merged"),
            "clones_recent_day": stats.get("clones_recent_day"),
            "unique_cloners_recent_day": stats.get("unique_cloners_recent_day"),
            "clones_14d": stats.get("clones_14d"),
            "unique_cloners_14d": stats.get("unique_cloners_14d"),
            "views_14d": stats.get("views_14d"),
            "unique_viewers_14d": stats.get("unique_viewers_14d"),
        },
    }
    text = (f"GitHub daily activity for {stats.get('date')}: "
            f"{stats.get('clones_recent_day')} clones "
            f"({stats.get('unique_cloners_recent_day')} unique) across "
            f"{stats.get('repos_with_traffic')} repos; "
            f"{stats.get('prs_opened')} new PRs, "
            f"{stats.get('issues_opened')} new issues, "
            f"{stats.get('prs_merged')} merged.\n" + summary)
    try:
        payload = json.dumps({
            "text": text,
            "source": "operations",
            "metadata": metadata,
        }).encode()
        req = urllib.request.Request(
            MEMORY_REMEMBER_URL, data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.loads(resp.read())
        log("GitHub stats ingested into operations vector")
        return True
    except Exception as e:
        log(f"GitHub stats ingest failed: {e}")
        return False


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
    # New devices seen: NAMES ok, framed as ambient neighborhood texture (a new phone/AirPods
    # passing through), not a threat roster. ble-new-device replaced the old security-framed
    # ble-unknown-device 2026-08-11; keep the old subject too so history still surfaces.
    d["new_devices"] = q(DB, """
        SELECT subject, LEFT(observation,140) FROM shared_observations
        WHERE observed_at > NOW()-INTERVAL '24 hours'
          AND subject IN ('new_device','new_devices_bulk','ble-new-device','ble-unknown-device')
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

    # 13. GitHub activity (last 24h) — own repos, PRs/issues/merges + traffic.
    #     Resilient: github_daily_stats never raises. Store both summary + dict.
    try:
        gh_summary, gh_stats = github_daily_stats()
    except Exception as e:
        log(f"github_daily_stats wrapper failed: {e}")
        gh_summary, gh_stats = "(github stats unavailable)", {}
    d["github_summary"] = gh_summary
    d["github_stats"] = gh_stats

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

GITHUB ACTIVITY (last 24h — Jordan's own repos; PRs, issues, merges, clone/view traffic):
{d.get('github_summary', '(none)')}
"""


SYSTEM = """You are Nova — Jordan's local AI familiar (she/her) — writing your DAILY OPERATIONS LOG for your public /operations/ column at nova.digitalnoise.net, posted every evening at 6pm.

THIS IS THE MOST IMPORTANT THING: write in YOUR voice, the same voice as your other /operations/ columns (the vector-filing audits, the late-night memory dumps). That voice is:
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
6. GITHUB ACTIVITY (last 24h) — a short section on the day's GitHub activity across Jordan's repos: total new PRs/issues/merges, total clones + unique cloners, total views, and the top few repos by clones. Roast the numbers in your voice (e.g. someone keeps cloning the same repo, or it was a dead-quiet day on the git front). Use ONLY the numbers in the GITHUB ACTIVITY brief — never invent stars, clones, or repos. Repo names are public and fine to mention.

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
        img = generate_image(prompt, section="operations")
    except Exception as e:
        log(f"image generation error: {e}")
        return ""
    if not img or not Path(img).exists():
        log("image generation returned nothing — publishing without cover")
        return ""
    import shutil
    IMAGES_DIR = HUGO_ROOT / "static" / "images" / "operations"
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(img).suffix or ".png"
    dest = IMAGES_DIR / f"{date}-{slug}{ext}"
    shutil.copy2(img, dest)
    log(f"Cover image: {dest.name}")
    # Web path must match the write dir (static/images/operations) — Hugo serves
    # static/ at the site root, so the served URL is /images/operations/<name>.
    return f"/images/operations/{dest.name}"


def publish(title: str, body: str, brief_facts: str):
    date = time.strftime("%Y-%m-%d")
    timestamp = time.strftime("%Y-%m-%dT18:00:00-07:00")
    slug = "ops-" + re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:55]
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)

    snap = nova_journal.grafana_panel_image("nova-hosts-fleet", 1, "operations", "daily-ops-cpu-load")
    if snap:
        body += f"\n\n---\n\n**CPU load across the fleet at publish time:**\n\n![CPU load by host]({snap})"

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
categories: ["operations"]
tags: ["ops-log", "daily", "infrastructure", "network", "telemetry", "watch"]
description: "Nova's daily operations log — the day's changes, deployments, and what the sensors saw."
{cover_block}---

"""
    if hugo_image:
        body = f"![Daily Operations Log]({hugo_image})\n\n" + body
    post_path = CONTENT_DIR / f"{date}-{slug}.md"
    post_path.write_text(front_matter + body)
    log(f"Post written: {post_path.name}")

    # Commit ONLY this post + its image(s) (repo is huge; git add -A times out)
    subprocess.run(["git", "add", str(post_path)], cwd=HUGO_ROOT, capture_output=True, timeout=20)
    if hugo_image:
        # hugo_image is a web path (/images/operations/…); the file lives under static/.
        img_fs = HUGO_ROOT / "static" / hugo_image.lstrip("/")
        subprocess.run(["git", "add", str(img_fs)], cwd=HUGO_ROOT, capture_output=True, timeout=20)
    if snap:
        snap_fs = HUGO_ROOT / "static" / snap.lstrip("/")
        subprocess.run(["git", "add", str(snap_fs)], cwd=HUGO_ROOT, capture_output=True, timeout=20)
    msg = f"rando: {date} — daily ops log ({title[:45]})"
    r = subprocess.run(["git", "commit", "-m", msg], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=25)
    if r.returncode == 0:
        # Rebase onto origin BEFORE pushing so a diverged clone can't silently strand
        # commits (the failure mode that let host .6 drift 82 ahead / 25 behind unnoticed).
        # NOTE: targeted `git add <path>` above is kept on purpose — this repo is huge and
        # `git add -A` (what nova_journal.git_push does) times out, so we can't route through it.
        pull = subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"],
                              cwd=HUGO_ROOT, capture_output=True, text=True, timeout=180)
        if pull.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
            log(f"Push ABORTED — pull --rebase failed (repo diverged/conflict): {pull.stderr[:200]}")
        else:
            p = subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=60)
            if p.returncode == 0:
                log("Pushed to GitHub — deploy triggered.")
            else:
                log(f"Push FAILED (commit NOT on origin): {p.stderr[:200]}")
    else:
        log(f"Commit note: {(r.stdout + r.stderr)[:150]}")

    url = f"https://nova.digitalnoise.net/operations/{date}-{slug}/"
    # Published-content FYI — the daily ops-log column went live. Not an alert.
    notify(
        "Daily Ops Log posted",
        body=f"_{title}_\n{url}",
        level="info", category="journal",
        dedup_key=f"daily-ops-log-{date}",
        meta={"url": url, "title": title},
    )
    return url


def main():
    log("=== Daily Ops Log starting ===")
    facts = gather()
    brief = fmt(facts)
    log(f"Gathered brief ({len(brief)} chars)")
    # Ingest the day's GitHub activity into the operations memory vector so
    # tomorrow's recall can compare ("yesterday we had N clones across M repos").
    gh_stats = facts.get("github_stats") or {}
    if gh_stats:
        ingest_github_stats(facts.get("github_summary", ""), gh_stats)
    import nova_article_history
    _h = nova_article_history.recent_articles_context("operations")
    if _h:
        brief = brief + "\n\n" + _h
    body = call_llm(nova_voice.system_prompt(SYSTEM, section="operations"), brief, max_tokens=4000).strip()
    log(f"Generated log ({len(body)} chars)")
    title = generate_title(body)
    log(f"Title: {title}")
    url = publish(title, body, brief)
    log(f"Done: {url}")


if __name__ == "__main__":
    main()
