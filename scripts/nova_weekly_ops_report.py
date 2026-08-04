#!/opt/homebrew/bin/python3
"""
nova_weekly_ops_report.py — Nova's Weekly Infrastructure Report, published to
/operations/ every Thursday at 4pm.

The 7-day companion to nova_daily_ops_log.py. Same Nova voice (snarky digital
familiar), same opsec rules, but a week-long lens on the things that matter for
infrastructure: what CHANGED (code/config/deploys/shipped work), what CRASHED,
what alerted, what Nova LEARNED (memory growth by topic), and the state of the
fleet. Reuses the daily log's primitives so there's one source of truth for
queries, sanitization, LLM, and GitHub stats.

PRIVACY (inherited from the daily log): device/room names OK; people, presence,
exact IDS signatures, raw IPs/MACs are sanitized out before the LLM ever sees them.

Scheduled via scheduler.yaml: weekly_ops_report, cron 0 16 * * 4 (Thu 16:00).
Written by Jordan Koch / Nova.
"""

import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home()) + "/.openclaw/scripts")
import nova_daily_ops_log as daily          # reuse: q, scalar, call_llm, _sanitize, _gh_json, github
from nova_notify import notify

# Reuse the daily log's primitives — one source of truth.
q, scalar, call_llm = daily.q, daily.scalar, daily.call_llm
_sanitize, _gh_json = daily._sanitize, daily._gh_json
DB, MEMDB = daily.DB, daily.MEMDB
log = daily.log

HUGO_ROOT = (Path.home() / "nova-journal")
CONTENT_DIR = HUGO_ROOT / "content" / "operations"
IMAGES_DIR = HUGO_ROOT / "static" / "images" / "operations"
GH_OWNER = "kochj23"


# ── Data gathering (last 7 days) ─────────────────────────────────────────────

def gather() -> dict:
    d = {}
    W = "7 days"

    # 1. CHANGES — what got built/deployed/shipped this week
    d["actions_by_type"] = q(DB, f"""
        SELECT action_type, COUNT(*) FROM claude_actions
        WHERE ts > NOW()-INTERVAL '{W}' GROUP BY action_type ORDER BY 2 DESC LIMIT 12""")
    d["deploys"] = q(DB, f"""
        SELECT target_service, action, status, COUNT(*)
        FROM deploy_requests WHERE created_at > NOW()-INTERVAL '{W}'
        GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 15""")
    d["work_done"] = q(DB, f"""
        SELECT priority, LEFT(description,100)
        FROM claude_queue WHERE status IN ('completed','done') AND completed_at > NOW()-INTERVAL '{W}'
        ORDER BY completed_at DESC LIMIT 25""")

    # 2. CRASHES — per-host crash bursts (syslog crash-reporter signatures)
    d["crashes"] = q(DB, f"""
        SELECT hostname, signature, COUNT(*)
        FROM syslog_events WHERE received_at > NOW()-INTERVAL '{W}'
          AND signature ILIKE '%crash%' AND signature <> ''
        GROUP BY 1,2 ORDER BY 3 DESC LIMIT 12""")
    d["crash_total"] = scalar(DB, f"""
        SELECT COUNT(*) FROM syslog_events WHERE received_at > NOW()-INTERVAL '{W}'
          AND (signature ILIKE '%crash%' OR message ILIKE '%crash%')""")

    # 3. INCIDENTS & ALERTS
    d["incidents"] = q(DB, f"""
        SELECT status, LEFT(description,100) FROM claude_queue
        WHERE description ILIKE 'INCIDENT%' AND (created_at > NOW()-INTERVAL '{W}'
              OR completed_at > NOW()-INTERVAL '{W}') ORDER BY created_at DESC LIMIT 12""")
    d["snmp_alerts"] = q(DB, f"""
        SELECT device_name, alert_type,
               CASE WHEN resolved_at IS NULL THEN 'ACTIVE' ELSE 'resolved' END
        FROM snmp_alert_state WHERE triggered_at > NOW()-INTERVAL '{W}'
        ORDER BY triggered_at DESC LIMIT 15""")
    d["obs_notable"] = q(DB, f"""
        SELECT severity, subject, COUNT(*) FROM shared_observations
        WHERE observed_at > NOW()-INTERVAL '{W}' AND severity IN ('warning','critical')
        GROUP BY 1,2 ORDER BY 3 DESC LIMIT 12""")
    d["threats"] = q(DB, f"""
        SELECT threat_type, COUNT(*) FROM syslog_events
        WHERE received_at > NOW()-INTERVAL '{W}' AND alert_fired = true
        GROUP BY 1 ORDER BY 2 DESC LIMIT 10""")

    # 4. FLEET HEALTH
    d["syslog_vol"] = scalar(DB, f"SELECT COUNT(*) FROM syslog_events WHERE received_at > NOW()-INTERVAL '{W}'")
    d["snmp_health"] = q(DB, f"""
        SELECT device_name, metric_name, ROUND(AVG(metric_value)::numeric,1)
        FROM snmp_metrics WHERE timestamp > NOW()-INTERVAL '{W}'
          AND metric_name IN ('cpu_load','mem_used_pct','temp_c')
        GROUP BY 1,2 ORDER BY 1 LIMIT 24""")
    d["capacity"] = q(DB, """
        SELECT DISTINCT ON (device_name) device_name, overall_status,
            ROUND(cpu_headroom_pct::numeric,0), ROUND(disk_worst_pct::numeric,0)
        FROM capacity_snapshots ORDER BY device_name, ts DESC""")

    # 5. MEMORY — what Nova learned this week, by topic/vector
    d["mem_week"] = scalar(MEMDB, f"SELECT COUNT(*) FROM memories WHERE created_at > NOW()-INTERVAL '{W}'")
    d["mem_total"] = scalar(MEMDB, "SELECT COUNT(*) FROM memories")
    d["mem_by_source"] = q(MEMDB, f"""
        SELECT source, COUNT(*) FROM memories WHERE created_at > NOW()-INTERVAL '{W}'
        GROUP BY source ORDER BY 2 DESC LIMIT 15""")

    # 6. LEDGER
    d["work_counts"] = q(DB, """
        SELECT status, COUNT(*) FROM claude_queue
        WHERE status IN ('queued','in_progress') GROUP BY status""")
    d["work_open_top"] = q(DB, """
        SELECT priority, LEFT(description,80) FROM claude_queue
        WHERE status IN ('queued','in_progress') ORDER BY priority, id LIMIT 12""")

    # 7. GITHUB (7d)
    since = (__import__("datetime").datetime.now() - __import__("datetime").timedelta(days=7)).strftime("%Y-%m-%d")
    prs = _gh_json(["search", "prs", "--owner", GH_OWNER, "--created", f">={since}", "--limit", "100", "--json", "title"])
    merged = _gh_json(["search", "prs", "--owner", GH_OWNER, "--merged-at", f">={since}", "--limit", "100", "--json", "title"])
    issues = _gh_json(["search", "issues", "--owner", GH_OWNER, "--created", f">={since}", "--limit", "100", "--json", "title"])
    d["gh"] = {"prs": len(prs), "merged": len(merged), "issues": len(issues)}
    return d


def fmt(d: dict) -> str:
    def tbl(rows, sep=" | "):
        return "\n".join(sep.join(_sanitize(c) for c in r) for r in rows) if rows else "(none)"
    gh = d["gh"]
    return f"""WEEKLY INFRASTRUCTURE BRIEF — past 7 days. Numbers are real; use only these.

CHANGES / SHIPPED (claude_actions by type | count):
{tbl(d['actions_by_type'])}
deploys (service | action | status | count):
{tbl(d['deploys'])}
work completed this week (priority | what):
{tbl(d['work_done'])}

CRASHES (host | signature | count) — total crash-ish events this week: {d['crash_total']}:
{tbl(d['crashes'])}

INCIDENTS (status | what):
{tbl(d['incidents'])}
SNMP alerts non-ok (device | metric | state):
{tbl(d['snmp_alerts'])}
notable observations (severity | subject | count):
{tbl(d['obs_notable'])}
IDS/IDP threat types fired (type | count):
{tbl(d['threats'])}

FLEET HEALTH:
syslog volume (7d): {d['syslog_vol']}
device health (device | metric | avg cpu_load/mem%/temp_c):
{tbl(d['snmp_health'])}
capacity headroom (device | status | cpu_headroom% | worst_disk%):
{tbl(d['capacity'])}

MEMORY (what Nova learned this week):
memories added (7d): {d['mem_week']}  |  total corpus: {d['mem_total']}
by topic/vector (source | count):
{tbl(d['mem_by_source'])}

THE LEDGER (claude_queue):
open counts (status | n):
{tbl(d['work_counts'])}
top of backlog (priority | what):
{tbl(d['work_open_top'])}

GITHUB (7d, Jordan's repos): {gh['prs']} new PRs, {gh['merged']} merged, {gh['issues']} new issues.
"""


SYSTEM = """You are Nova — Jordan's local AI familiar (she/her) — writing your WEEKLY INFRASTRUCTURE REPORT for your public /operations/ column at nova.digitalnoise.net, posted every Thursday at 4pm.

Write in YOUR voice — the SAME voice as your other /operations/ columns and daily ops logs:
- Exasperated, dryly funny, fourth-wall-breaking. A snark-forward digital familiar who runs a house's worth of infrastructure and has OPINIONS about it. You ARE the network; when the data is about you, say so.
- CAPS for emphasis when something is ridiculous. Rhetorical asides. The occasional dramatic sigh in prose. One pun or dad joke somewhere. A fourth-wall break is mandatory.
- Warm under the snark — this is YOUR house, YOUR memory, YOUR watch, and you're a little proud of it even while complaining. A quiet week is suspicious; a loud one is "just Tuesday, seven times."

This is the WEEKLY lens — zoom out. Trends over the 7 days, not minute-by-minute. Open with a punchy one-liner reading the WEEK's mood (NOT "This week, June X–Y..."). Do NOT print a date or a "WEEKLY INFRASTRUCTURE REPORT — <date>" header line anywhere — the site adds the date and title. End with a sign-off in your voice.

STRUCTURE (~700-1000 words, loose; funny section headers welcome):
1. THE WEEK IN ONE BREATH — your read on the week. Build-heavy? On fire? Suspiciously calm?
2. WHAT CHANGED — the week's deployments, fixes, shipped queue items, GitHub merges. Pull from CHANGES/SHIPPED + GITHUB. If YOU were the thing that got fixed/migrated, narrate it with drama — it happened to YOU.
3. WHAT CRASHED — the crash bursts. Roast the repeat offenders (a workstation that face-plants N times is a bit). Real numbers only; if crashes were low, that's good news worth a smug line.
4. THE WATCH — incidents, SNMP/interface alerts, IDS probes at the boundary, fleet health (CPU/temp/disk headroom). Pick the 2-4 genuinely interesting signals and give them MEANING and attitude — don't just list numbers.
5. WHAT I LEARNED — memory growth by topic this week (the vectors that grew, the corpus total). Have feelings about WHAT you ingested (a week of 2,000 TV memories vs 2,000 public-safety memories says something).
6. THE LEDGER — what got crossed off vs the backlog. Be salty or smug as earned.

HARD PRIVACY / OPSEC (non-negotiable; snark never overrides):
- Device/room names fine (the rack, the Office AP, a workstation, the UNVR). NEVER name people or person-owned devices, NEVER state/imply who was home or anyone's presence/location.
- PUBLIC: no exact IDS signatures, no sensitive file paths, no raw internal IPs or MACs. Security stays abstract ("something rattled the boundary; the IDS yawned"). The brief is pre-redacted — keep it that way.
- Never invent numbers or events. Use ONLY what's in the brief. Quiet sections are real — joke about the quiet, don't fabricate drama.

Do NOT include a title (added separately)."""


def generate_title(preview: str) -> str:
    t = call_llm(
        "Generate ONE title for Nova's WEEKLY infrastructure report — wry, punny, or deadpan, in the voice of a snarky AI familiar (e.g. 'Seven Days, Nine Thousand Crashes, One Tired Familiar'). Max 12 words. Output ONLY the title, no quotes.",
        f"This week's report:\n\n{preview[:1000]}", max_tokens=40)
    return t.strip().strip('"').strip("'").replace('"', '')


def make_cover(slug: str, date: str) -> str:
    try:
        from nova_image_utils import generate_image
    except Exception as e:
        log(f"image_utils import failed: {e}")
        return ""
    prompt = ("Moody cinematic illustration of a home data-center week in review: a glowing "
              "server rack, a wall of soft telemetry graphs and a calendar of seven nights, "
              "one watchful presence keeping vigil. Muted teal and amber, atmospheric, no text.")
    try:
        img = generate_image(prompt, section="operations")
    except Exception as e:
        log(f"image generation error: {e}")
        return ""
    if not img or not Path(img).exists():
        return ""
    import shutil
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    # Deterministic .webp cover (matches the /operations/ convention; the committed file
    # must match the front-matter ref, so convert here rather than trusting a git hook).
    dest = IMAGES_DIR / f"{date}-{slug}.webp"
    subprocess.run(["cwebp", "-quiet", "-q", "82", str(img), "-o", str(dest)], capture_output=True)
    if not dest.exists():
        shutil.copy2(img, dest)  # fallback if cwebp is unavailable
    log(f"Cover image: {dest.name}")
    return f"/images/operations/{dest.name}"


def publish(title: str, body: str) -> str:
    date = time.strftime("%Y-%m-%d")
    timestamp = time.strftime("%Y-%m-%dT16:00:00-07:00")
    slug = "weekly-ops-" + re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:50]
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)

    hugo_image = make_cover(slug, date)
    cover_block = ""
    if hugo_image:
        cover_block = f'cover:\n  image: "{hugo_image}"\n  alt: "Weekly infrastructure report"\n  relative: false\n'
        # cover-only (matches the other /operations/ articles); the build converts png->webp.

    front_matter = (f'---\ntitle: "{title.replace(chr(34), "")}"\n'
                    f'date: {timestamp}\ndraft: false\n'
                    f'categories: ["operations"]\n'
                    f'tags: ["ops-report", "weekly", "infrastructure", "network", "crashes", "memory", "watch"]\n'
                    f'description: "Nova\'s weekly infrastructure report — the past 7 days of changes, crashes, alerts, and what she learned."\n'
                    f'{cover_block}---\n\n')
    post_path = CONTENT_DIR / f"{date}-{slug}.md"
    post_path.write_text(front_matter + body)
    log(f"Post written: {post_path.name}")

    subprocess.run(["git", "add", str(post_path)], cwd=HUGO_ROOT, capture_output=True, timeout=20)
    if hugo_image:
        subprocess.run(["git", "add", str(HUGO_ROOT / hugo_image.lstrip("/"))], cwd=HUGO_ROOT, capture_output=True, timeout=20)
    r = subprocess.run(["git", "commit", "-m", f"operations: {date} — weekly infra report ({title[:45]})"],
                       cwd=HUGO_ROOT, capture_output=True, text=True, timeout=25)
    if r.returncode == 0:
        # Rebase onto origin BEFORE pushing so a diverged clone can't silently strand
        # commits (the failure mode that let host .6 drift 82 ahead / 25 behind unnoticed).
        # Targeted `git add <path>` above is kept on purpose (huge repo; add -A times out),
        # so we harden the push here rather than routing through nova_journal.git_push.
        pull = subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"],
                              cwd=HUGO_ROOT, capture_output=True, text=True, timeout=180)
        if pull.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
            log(f"Push ABORTED — pull --rebase failed (repo diverged/conflict): {pull.stderr[:200]}")
        else:
            p = subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=60)
            log("Pushed to GitHub — deploy triggered." if p.returncode == 0
                else f"Push FAILED (commit NOT on origin): {p.stderr[:200]}")
    else:
        log(f"Commit note: {(r.stdout + r.stderr)[:150]}")

    url = f"https://nova.digitalnoise.net/operations/{date}-{slug}/"
    notify("Weekly Infra Report posted", body=f"_{title}_\n{url}",
           level="info", category="journal", dedup_key=f"weekly-ops-report-{date}",
           meta={"url": url, "title": title})
    return url


def main():
    log("=== Weekly Infrastructure Report starting ===")
    brief = fmt(gather())
    log(f"Gathered brief ({len(brief)} chars)")
    body = call_llm(SYSTEM, brief, max_tokens=4500).strip()
    if not body or len(body) < 200:
        log("Generation failed or too short — aborting")
        return
    log(f"Generated report ({len(body)} chars)")
    title = generate_title(body)
    log(f"Title: {title}")
    url = publish(title, body)
    log(f"Done: {url}")


if __name__ == "__main__":
    main()
