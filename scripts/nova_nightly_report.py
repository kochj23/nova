#!/usr/bin/env python3
"""
nova_nightly_report.py — Nova's nightly 11pm digest.

Runs all 6 report modules and posts each as a Slack section:
  2. GitHub digest
  3. Email action items
  4. Nova's daily memory log
  5. Package tracker
  6. Weather forecast
  8. HomeKit status

Written by Jordan Koch.
"""

import json
import math
import re
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta
from pathlib import Path
import nova_config


VECTOR_MEM_URL = "http://127.0.0.1:18790/remember"


def vector_remember(text: str, source: str = "nightly", metadata: dict = None):
    """Store text in Nova's vector memory. Silently skips if server is down."""
    try:
        payload = json.dumps({
            "text": text,
            "source": source,
            "metadata": metadata or {},
        }).encode()
        req = urllib.request.Request(
            VECTOR_MEM_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


SLACK_TOKEN  = nova_config.slack_bot_token()
SLACK_CHAN   = "C0AMNQ5GX70"
SLACK_API    = "https://slack.com/api"
SCRIPTS      = Path.home() / ".openclaw" / "scripts"
WORKSPACE    = Path.home() / ".openclaw" / "workspace"
MEMORY_DIR   = WORKSPACE / "memory"
GATEWAY_LOG  = Path.home() / ".openclaw" / "logs" / "gateway.log"
TODAY        = date.today().isoformat()
NOW          = datetime.now()


# ── Slack ─────────────────────────────────────────────────────────────────────

def slack_post(text):
    chunks = [text[i:i+3000] for i in range(0, len(text), 3000)]
    for chunk in chunks:
        data = json.dumps({"channel": SLACK_CHAN, "text": chunk, "mrkdwn": True}).encode()
        req  = urllib.request.Request(
            f"{SLACK_API}/chat.postMessage", data=data,
            headers={"Authorization": "Bearer " + SLACK_TOKEN,
                     "Content-Type": "application/json; charset=utf-8"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                log(f"Slack error: {result.get('error')}")


def log(msg):
    print(f"[nightly {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── 2. GitHub Digest ──────────────────────────────────────────────────────────

def github_digest():
    log("GitHub digest...")
    try:
        # Get events from last 24h across all repos
        result = subprocess.run(
            ["gh", "api", "/users/kochj23/events  # noqa — gh username?per_page=100"],
            capture_output=True, text=True, timeout=30
        )
        events = json.loads(result.stdout)
        cutoff = NOW - timedelta(hours=24)

        pushes, issues, prs, stars, comments = [], [], [], [], []

        for e in events:
            try:
                ts = datetime.strptime(e["created_at"], "%Y-%m-%dT%H:%M:%SZ")
                if ts < cutoff:
                    continue
                repo = e["repo"]["name"].replace("kochj23/", "")  # noqa
                if repo.startswith("awesome-"):
                    continue  # skip curated list repos
                etype = e["type"]
                payload = e.get("payload", {})

                if etype == "PushEvent":
                    commits = payload.get("commits", [])
                    for c in commits:
                        msg = c.get("message", "").split("\n")[0][:60]
                        pushes.append(f"  • `{repo}` — {msg}")
                elif etype == "IssuesEvent":
                    action = payload.get("action", "")
                    title  = payload.get("issue", {}).get("title", "")[:50]
                    issues.append(f"  • [{action}] `{repo}` — {title}")
                elif etype == "PullRequestEvent":
                    action = payload.get("action", "")
                    title  = payload.get("pull_request", {}).get("title", "")[:50]
                    prs.append(f"  • [{action}] `{repo}` — {title}")
                elif etype == "WatchEvent":
                    stars.append(f"  • ⭐ `{repo}`")
                elif etype in ("IssueCommentEvent", "CommitCommentEvent"):
                    body = payload.get("comment", {}).get("body", "")[:60]
                    comments.append(f"  • `{repo}` — {body}")
            except Exception:
                continue

        # Also check for open PRs needing review
        pr_result = subprocess.run(
            ["gh", "search", "prs", "--author=kochj23  # noqa", "--state=open", "--json", "title,repository,createdAt,url"],
            capture_output=True, text=True, timeout=30
        )
        open_prs = []
        if pr_result.returncode == 0:
            for pr in json.loads(pr_result.stdout or "[]")[:5]:
                repo = pr.get("repository", {}).get("name", "?")
                if repo.startswith("awesome-"):
                    continue  # skip curated list repos
                title = pr.get("title", "")[:50]
                open_prs.append(f"  • `{repo}` — {title}")

        lines = [f"*🐙 GitHub Digest — {TODAY}*"]
        if not any([pushes, issues, prs, stars, comments, open_prs]):
            lines.append("  _No activity in the last 24 hours._")
        else:
            if pushes:
                lines.append(f"*Commits ({len(pushes)}):*")
                lines.extend(pushes[:8])
                if len(pushes) > 8: lines.append(f"  _+{len(pushes)-8} more_")
            if prs:
                lines.append(f"*Pull Requests:*")
                lines.extend(prs)
            if issues:
                lines.append(f"*Issues:*")
                lines.extend(issues)
            if stars:
                lines.append(f"*New Stars ({len(stars)}):*")
                lines.extend(stars[:5])
            if open_prs:
                lines.append(f"*Open PRs:*")
                lines.extend(open_prs)
        return "\n".join(lines)

    except Exception as e:
        return f"*🐙 GitHub Digest*\n_Error: {e}_"


# ── 3. Email Action Items ─────────────────────────────────────────────────────

KNOWN_SENDERS = [
    "kochj23" + "@gmail.com", "kochj" + "@digitalnoise.net", "mjramos76" + "@gmail.com",  # noqa
    "jason.cox@disney.com", "james.tatum@disney.com", "amy.mccain@gmail.com",
    "sam@jasonacox.com", "marey@makehorses.org", "digitalnoise.net",
    "disney.com", "apple.com", "americanexpress.com", "adt.com",
    "partnersfcu.org", "networksolutions.com", "wellsfargo.com"
]

NOISE_SENDERS = [
    "wayfair", "hulu", "ihg", "turbotax", "magazines.com", "boy smells",
    "printables", "hims", "sendafriend", "happy gardening", "overlord caps",
    "morimoto", "bob's watches", "teepublic", "skillshare", "capital grille",
    "usps informed delivery", "yahoo mail app"
]

def get_mail_data():
    """Return mail summary content, using cache if < 12h old, else re-fetching."""
    summary_file = Path("/tmp/nova_mail_fetch.txt")
    import time
    if summary_file.exists():
        age_hours = (time.time() - summary_file.stat().st_mtime) / 3600
        if age_hours < 12:
            log(f"Using cached mail data ({age_hours:.1f}h old)")
            return summary_file.read_text(encoding="utf-8")
    log("Fetching fresh mail data...")
    subprocess.run(["python3", str(SCRIPTS / "nova_mail_fetch.py")],
                   capture_output=True, text=True, timeout=150)
    if summary_file.exists():
        return summary_file.read_text(encoding="utf-8")
    return None


def email_action_items():
    log("Email action items...")
    try:
        content = get_mail_data()
        if not content:
            return "*📋 Action Items*\n_Could not fetch mail data._"
        lines   = content.splitlines()

        action_items = []
        current_msg  = {}
        current_acct = None
        eight_hrs_ago = NOW - timedelta(hours=8)

        for line in lines:
            line = line.strip()
            m = re.match(r"📬\s+(\S+@\S+)\s+—", line)
            if m:
                current_acct = m.group(1)
                continue

            if line.startswith("[UNREAD]") and "FROM:" in line:
                sender = re.sub(r"\[UNREAD\]\s*FROM:\s*|\s*\[UNREAD\]", "", line).strip()
                current_msg = {"sender": sender, "subject": "", "acct": current_acct or ""}
            elif line.startswith("SUBJ:") and current_msg:
                current_msg["subject"] = line[5:].strip()

                sender_lower = current_msg["sender"].lower()
                subj_lower   = current_msg["subject"].lower()

                is_noise = any(n in sender_lower for n in NOISE_SENDERS)
                is_known = any(k in sender_lower for k in KNOWN_SENDERS)
                is_financial = any(w in sender_lower + subj_lower for w in
                    ["american express", "amex", "bank", "credit", "payment", "invoice", "bill",
                     "statement", "fraud", "transaction", "charge"])
                is_security = any(w in sender_lower + subj_lower for w in
                    ["security", "alert", "warning", "unauthorized", "verify", "password",
                     "2fa", "login", "breach", "adt", "low battery"])
                is_question = "?" in current_msg["subject"]
                is_nova_msg = "nova@digitalnoise.net" in sender_lower

                if not is_noise and not is_nova_msg:
                    priority = None
                    if is_security or is_financial:
                        priority = "🔴 HIGH"
                    elif is_known and is_question:
                        priority = "🟡 REPLY"
                    elif is_known:
                        priority = "🟡 REPLY"
                    elif is_question:
                        priority = "🔵 FYI"

                    if priority:
                        action_items.append({
                            "priority": priority,
                            "sender":   current_msg["sender"],
                            "subject":  current_msg["subject"],
                            "acct":     current_msg["acct"]
                        })
                current_msg = {}

        lines_out = [f"*📋 Email Action Items — {TODAY}*"]
        if not action_items:
            lines_out.append("  _No action items identified._")
        else:
            for item in action_items[:15]:
                lines_out.append(f"  {item['priority']} *{item['subject'][:55]}*")
                lines_out.append(f"    From: {item['sender']}  →  _{item['acct']}_")
        return "\n".join(lines_out)

    except Exception as e:
        return f"*📋 Email Action Items*\n_Error: {e}_"


# ── 4. Nova's Daily Memory Log ────────────────────────────────────────────────

def nova_memory_log():
    log("Writing memory log...")
    try:
        today_str = date.today().isoformat()
        mem_file  = MEMORY_DIR / f"{today_str}.md"
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)

        # Count cron runs today from JSONL files
        cron_runs_dir = Path.home() / ".openclaw" / "cron" / "runs"
        run_counts = {}
        import glob
        for jf in glob.glob(str(cron_runs_dir / "*.jsonl")):
            job_id = Path(jf).stem
            try:
                # Map job IDs to names
                jobs_file = Path.home() / ".openclaw" / "cron" / "jobs.json"
                jobs_data = json.loads(jobs_file.read_text())
                jobs = jobs_data.get("jobs", jobs_data) if isinstance(jobs_data, dict) else jobs_data
                job_name_map = {j["id"]: j["name"] for j in jobs}
            except Exception:
                job_name_map = {}

            count = 0
            with open(jf) as f:
                for line in f:
                    try:
                        e = json.loads(line.strip())
                        ts_ms = e.get("ts", 0)
                        ts    = datetime.fromtimestamp(ts_ms / 1000)
                        if ts.date() == date.today() and e.get("action") == "finished":
                            count += 1
                    except Exception:
                        pass
            if count > 0:
                name = job_name_map.get(job_id, job_id[:8])
                run_counts[name] = count

        # Count Slack deliveries today from gateway log
        slack_count = 0
        if GATEWAY_LOG.exists():
            today_prefix = f"{today_str}T"
            with open(GATEWAY_LOG) as f:
                for line in f:
                    if today_prefix in line and "delivered reply to channel" in line:
                        slack_count += 1

        # Check which apps are running
        running_apps = []
        for port in [37421, 37422, 37423, 37424, 37432, 37443]:
            try:
                r = subprocess.run(
                    ["curl", "-s", "--connect-timeout", "0.5", f"http://127.0.0.1:{port}/api/status"],
                    capture_output=True, text=True, timeout=2
                )
                if r.returncode == 0 and r.stdout.strip():
                    d = json.loads(r.stdout)
                    running_apps.append(d.get("app", f":{port}"))
            except Exception:
                pass

        # Write memory file
        entry = f"""# Nova Daily Log — {today_str}
*Auto-generated at {NOW.strftime('%H:%M')}*

## Cron Activity Today
"""
        for name, count in sorted(run_counts.items()):
            entry += f"- {name}: {count} run(s)\n"

        entry += f"\n## Slack\n- Messages delivered to Jordan today: {slack_count}\n"

        entry += f"\n## Apps Running at 11pm\n"
        if running_apps:
            for a in running_apps:
                entry += f"- {a}\n"
        else:
            entry += "- No monitored apps running\n"

        entry += f"\n## Notes\n- Log written by nova_nightly_report.py\n"

        mem_file.write_text(entry, encoding="utf-8")

        # Also update the work-status.md with a brief today line
        ws = MEMORY_DIR / "work-status.md"
        today_line = f"- {today_str}: {slack_count} Slack messages, {sum(run_counts.values())} cron runs, apps: {', '.join(running_apps) or 'none'}\n"
        existing = ws.read_text(encoding="utf-8") if ws.exists() else "# Work Status Log\n"
        if today_str not in existing:
            ws.write_text(existing.rstrip() + "\n" + today_line, encoding="utf-8")

        lines_out = [
            f"*📓 Nova Daily Log — {today_str}*",
            f"  Cron jobs run today: {sum(run_counts.values())} across {len(run_counts)} job(s)",
        ]
        for name, count in sorted(run_counts.items()):
            lines_out.append(f"  • {name}: {count}x")
        lines_out.append(f"  Slack messages sent: {slack_count}")
        lines_out.append(f"  Apps running: {', '.join(running_apps) or 'none'}")
        lines_out.append(f"  _Memory written to {mem_file.name}_")
        return "\n".join(lines_out)

    except Exception as e:
        return f"*📓 Nova Daily Log*\n_Error: {e}_"


# ── 5. Package Tracker ────────────────────────────────────────────────────────

CARRIER_PATTERNS = {
    "USPS": [
        r"\b(9[2345]\d{18,21})\b",
        r"\b(420\d{5}9[2345]\d{18,21})\b",
    ],
    "UPS": [r"\b(1Z[A-Z0-9]{16})\b"],
    "FedEx": [r"\b(\d{12})\b", r"\b(\d{15})\b", r"\b(\d{20})\b"],
    "Amazon": [r"\b(TBA\d{12})\b"],
}

def package_tracker():
    log("Package tracker...")
    try:
        # Use cached mail data rather than re-running the slow applescript
        content = get_mail_data()
        raw = content if content else ""

        packages = []
        current_subject = ""
        current_from = ""

        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("[UNREAD]") and "FROM:" in line:
                current_from = re.sub(r"\[UNREAD\]\s*FROM:\s*|\s*\[UNREAD\]", "", line).strip().lower()
            elif line.startswith("[READ]") and "FROM:" in line:
                current_from = re.sub(r"\[READ\]\s*FROM:\s*", "", line).strip().lower()
            elif line.startswith("SUBJ:"):
                current_subject = line[5:].strip()
                subj_lower = current_subject.lower()
                from_lower = current_from.lower()

                # Detect package-related emails
                is_pkg = any(w in subj_lower + from_lower for w in [
                    "shipment", "shipped", "delivery", "delivering", "delivered",
                    "package", "tracking", "order", "arriving", "out for delivery",
                    "usps", "fedex", "ups", "amazon", "tougher than tom"
                ])

                if is_pkg and current_subject:
                    # Extract status from subject
                    status = "📦 In transit"
                    if any(w in subj_lower for w in ["delivered", "arrived"]):
                        status = "✅ Delivered"
                    elif any(w in subj_lower for w in ["out for delivery", "arriving today"]):
                        status = "🚚 Out for delivery"
                    elif any(w in subj_lower for w in ["expected", "arriving by", "scheduled"]):
                        status = "📬 Expected today"
                    elif any(w in subj_lower for w in ["shipped", "shipment", "on the way"]):
                        status = "📦 Shipped"

                    # Try to extract carrier
                    carrier = "Unknown"
                    if "usps" in subj_lower + from_lower: carrier = "USPS"
                    elif "fedex" in subj_lower + from_lower: carrier = "FedEx"
                    elif "ups" in subj_lower + from_lower: carrier = "UPS"
                    elif "amazon" in subj_lower + from_lower: carrier = "Amazon"

                    pkg = f"  {status} [{carrier}] {current_subject[:60]}"
                    if pkg not in packages:
                        packages.append(pkg)

                current_subject = ""
                current_from = ""

        lines = [f"*📦 Package Tracker — {TODAY}*"]
        if not packages:
            lines.append("  _No package notifications in the last 24 hours._")
        else:
            lines.extend(packages[:10])
        return "\n".join(lines)

    except Exception as e:
        return f"*📦 Package Tracker*\n_Error: {e}_"


# ── 6. Weather ────────────────────────────────────────────────────────────────

def weather_report():
    log("Weather...")
    try:
        # Current conditions
        r = subprocess.run(
            ["curl", "-s", "https://wttr.in/Burbank,CA?format=%C+%t+feels+%f+humidity+%h+wind+%w"],
            capture_output=True, text=True, timeout=15
        )
        current = r.stdout.strip()

        # 3-day forecast
        r2 = subprocess.run(
            ["curl", "-s", "https://wttr.in/Burbank,CA?format=3"],
            capture_output=True, text=True, timeout=15
        )
        forecast = r2.stdout.strip()

        tomorrow = (date.today() + timedelta(days=1)).strftime("%A")
        day2     = (date.today() + timedelta(days=2)).strftime("%A")

        lines = [
            f"*🌤 Burbank Weather — {TODAY}*",
            f"  Now: {current}",
        ]
        if forecast:
            # wttr format=3 gives emoji+temp lines — clean it up
            flines = [l.strip() for l in forecast.splitlines() if l.strip()]
            if flines:
                lines.append(f"  Forecast: {' | '.join(flines[:3])}")
        return "\n".join(lines)

    except Exception as e:
        return f"*🌤 Weather*\n_Error: {e}_"


# ── 8. HomeKit Status ─────────────────────────────────────────────────────────

def homekit_status():
    log("HomeKit status...")
    try:
        # Check if HomekitControl is running
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", "1", "http://127.0.0.1:37432/api/status"],
            capture_output=True, text=True, timeout=3
        )

        if r.returncode != 0 or not r.stdout.strip():
            return f"*🏠 HomeKit Status*\n  _HomekitControl app is not running._"

        status = json.loads(r.stdout)
        uptime = status.get("uptimeSeconds", 0)
        uptime_str = f"{uptime // 60}m {uptime % 60}s" if uptime < 3600 else f"{uptime // 3600}h {(uptime % 3600) // 60}m"

        # Get accessories
        r2 = subprocess.run(
            ["curl", "-s", "--connect-timeout", "1", "http://127.0.0.1:37432/api/accessories"],
            capture_output=True, text=True, timeout=5
        )
        raw_acc = json.loads(r2.stdout) if r2.returncode == 0 and r2.stdout.strip() else {}

        # Check if accessories are available (only on iOS/tvOS device)
        if isinstance(raw_acc, dict) and "note" in raw_acc:
            return (f"*🏠 HomeKit Status — {TODAY}*\n"
                    f"  App running (uptime {uptime_str})\n"
                    f"  _Full accessory data only available from iOS/tvOS device_")

        accessories = raw_acc if isinstance(raw_acc, list) else raw_acc.get("accessories", [])

        alerts, on_devices, offline_devices = [], [], []
        for acc in accessories:
            if not isinstance(acc, dict):
                continue
            name  = acc.get("name", "?")
            room  = acc.get("room", "")
            for c in acc.get("characteristics", []):
                ctype = c.get("type", "").lower()
                val   = c.get("value")
                if "reachable" in ctype and val is False:
                    offline_devices.append(f"{name} ({room})")
                elif "battery" in ctype and isinstance(val, (int, float)) and val < 20:
                    alerts.append(f"🔋 Low battery: {name} ({room}) — {val}%")
                elif "on" in ctype and val is True:
                    on_devices.append(f"{name} ({room})")

        lines = [f"*🏠 HomeKit Status — {TODAY}*",
                 f"  App running · uptime {uptime_str} · {len(accessories)} accessories"]
        if alerts:
            lines.append("  *⚠️ Alerts:*")
            lines.extend(f"    {a}" for a in alerts)
        if offline_devices:
            lines.append(f"  *📵 Offline ({len(offline_devices)}):*")
            lines.extend(f"    • {d}" for d in offline_devices[:5])
        if on_devices:
            lines.append(f"  *💡 On ({len(on_devices)}):*")
            lines.extend(f"    • {d}" for d in on_devices[:8])
            if len(on_devices) > 8:
                lines.append(f"    _+{len(on_devices)-8} more_")
        if not alerts and not offline_devices and not on_devices:
            lines.append("  _All accessories nominal._")

        return "\n".join(lines)

    except Exception as e:
        return f"*🏠 HomeKit Status*\n_Error: {e}_"


# ── Memory Writer ─────────────────────────────────────────────────────────────

def write_dream_context(results: dict):
    """
    Write all nightly report data to Nova's memory files so the 2am
    dream journal cron has rich context to draw from.

    Writes to:
      ~/.openclaw/workspace/memory/YYYY-MM-DD.md  — daily memory (read by dream cron)
      ~/.openclaw/workspace/HEARTBEAT.md           — current state snapshot
    """
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    mem_file       = MEMORY_DIR / f"{TODAY}.md"
    heartbeat_file = WORKSPACE / "HEARTBEAT.md"

    # Strip Slack markdown (*bold*, _italic_) for cleaner plain text
    def clean(s):
        s = re.sub(r"\*(.+?)\*", r"\1", s)
        s = re.sub(r"_(.+?)_", r"\1", s)
        return s.strip()

    sections = []
    sections.append(f"# Nova Daily Memory — {TODAY}")
    sections.append(f"*Written at {NOW.strftime('%H:%M')} by nova_nightly_report.py*\n")

    label_map = {
        "GitHub Digest":      "## What happened on GitHub today",
        "Email Action Items": "## Emails that need attention",
        "Daily Memory Log":   "## Nova's activity today",
        "Package Tracker":    "## Packages in transit",
        "Weather":            "## Weather in Burbank",
        "HomeKit Status":     "## Home status",
        "Moon & Sky":         "## Moon phase and sky tonight",
        "Burbank Reddit":     "## What Burbank is talking about",
        "Meeting Notes":      "## Meetings today",
    }

    for name, content in results.items():
        if not content or "Error:" in content:
            continue
        heading = label_map.get(name, f"## {name}")
        sections.append(heading)
        sections.append(clean(content))
        sections.append("")

    # Preserve any sections already written by other scripts today
    # (e.g. "On This Day in History" written at 3pm by nova_this_day.py)
    preserved = []
    if mem_file.exists():
        existing = mem_file.read_text(encoding="utf-8")
        in_preserved = False
        preserve_headers = {"## On This Day in History"}
        for line in existing.splitlines():
            if any(line.startswith(h) for h in preserve_headers):
                in_preserved = True
            elif line.startswith("## ") and in_preserved:
                in_preserved = False
            if in_preserved:
                preserved.append(line)

    mem_text = "\n".join(sections)
    if preserved:
        mem_text = mem_text.rstrip() + "\n\n" + "\n".join(preserved)

    mem_file.write_text(mem_text, encoding="utf-8")
    log(f"Memory written: {mem_file}")

    # HEARTBEAT — brief current-state snapshot Nova reads as live context
    weather_line = clean(results.get("Weather", "")).replace("Burbank Weather", "").strip()
    github_line  = ""
    for line in results.get("GitHub Digest", "").splitlines():
        if "Commits" in line or "Stars" in line or "No activity" in line:
            github_line = clean(line).strip()
            break
    pkg_count = results.get("Package Tracker", "").count("📦") + \
                results.get("Package Tracker", "").count("🚚") + \
                results.get("Package Tracker", "").count("✅") + \
                results.get("Package Tracker", "").count("📬")

    # Clean weather to just first line
    weather_line = weather_line.splitlines()[0] if weather_line else ""

    hb_lines = [
        f"# Nova Heartbeat — {TODAY} {NOW.strftime('%H:%M')}",
        f"",
        f"Today is {NOW.strftime('%A, %B %d %Y')}. It is {NOW.strftime('%I:%M %p')} in Burbank.",
        f"",
        f"## Right now",
        f"- Weather: {weather_line}" if weather_line else "",
        f"- GitHub: {github_line}" if github_line else "- GitHub: no activity today",
        f"- Packages in transit: {pkg_count}" if pkg_count else "- No packages tracked today",
        f"",
        f"## What Nova did today",
    ]

    # Pull activity summary from memory log
    for line in results.get("Daily Memory Log", "").splitlines():
        stripped = clean(line).strip()
        if stripped.startswith("•") or stripped.startswith("Cron") or stripped.startswith("Slack") or stripped.startswith("Apps"):
            hb_lines.append(f"- {stripped.lstrip('• ')}")

    hb_lines.append("")
    hb_lines.append("## Action items for Jordan")
    for line in results.get("Email Action Items", "").splitlines():
        stripped = clean(line).strip()
        if stripped.startswith("🔴") or stripped.startswith("🟡"):
            hb_lines.append(f"- {stripped}")

    heartbeat_file.write_text("\n".join(l for l in hb_lines if l is not None), encoding="utf-8")
    log(f"Heartbeat written: {heartbeat_file}")

    # Store key daily facts in vector memory
    log("Storing nightly digest in vector memory...")
    meta = {"date": TODAY}

    # Email action items — store each high/medium priority item
    for line in results.get("Email Action Items", "").splitlines():
        stripped = re.sub(r"[*_]", "", line).strip()
        if stripped.startswith("🔴") or stripped.startswith("🟡"):
            vector_remember(stripped, source="email", metadata={**meta, "type": "action_item"})

    # GitHub activity
    gh = clean(results.get("GitHub Digest", ""))
    if gh and "No activity" not in gh and "Error" not in gh:
        vector_remember(f"GitHub activity on {TODAY}: {gh[:400]}", source="github", metadata=meta)

    # Activity summary
    activity_lines = [
        re.sub(r"[*_]", "", l).strip()
        for l in results.get("Daily Memory Log", "").splitlines()
        if l.strip() and not l.strip().startswith("📓")
    ]
    if activity_lines:
        activity_text = f"Nova activity log for {TODAY}: " + "; ".join(activity_lines[:8])
        vector_remember(activity_text, source="nightly", metadata=meta)

    # Package tracking
    pkg = clean(results.get("Package Tracker", ""))
    if pkg and "No packages" not in pkg and "Error" not in pkg:
        vector_remember(f"Package status on {TODAY}: {pkg[:300]}", source="nightly", metadata={**meta, "type": "packages"})

    # HomeKit / home status
    hk = clean(results.get("HomeKit Status", ""))
    if hk and "Error" not in hk:
        vector_remember(f"Home status on {TODAY}: {hk[:300]}", source="homekit", metadata=meta)

    log("Vector memory updated.")


# ── 9. Moon Phase + Sky Events ────────────────────────────────────────────────

# 2026 notable astronomical events — (month, day, description)
SKY_EVENTS_2026 = [
    (1,  3,  "Quadrantid Meteor Shower peak"),
    (3,  20, "Spring Equinox"),
    (4,  3,  "Total Lunar Eclipse — visible from Americas, Europe, Africa"),
    (4,  8,  "Mercury at greatest eastern elongation"),
    (5,  6,  "Eta Aquarid Meteor Shower peak"),
    (6,  21, "Summer Solstice"),
    (7,  28, "Delta Aquarid Meteor Shower peak"),
    (8,  12, "Perseid Meteor Shower peak"),
    (8,  12, "Total Solar Eclipse — visible from Arctic, Greenland, Russia, Spain"),
    (9,  22, "Autumnal Equinox"),
    (10, 21, "Orionid Meteor Shower peak"),
    (11, 5,  "Taurid Meteor Shower peak"),
    (11, 17, "Leonid Meteor Shower peak"),
    (12, 13, "Geminid Meteor Shower peak"),
    (12, 21, "Winter Solstice"),
    (12, 22, "Ursid Meteor Shower peak"),
]


def _moon_phase_for(d):
    """Return (phase_name, emoji, phase_days, days_to_full) for a given date."""
    # Reference: known new moon = Jan 6, 2000
    reference   = date(2000, 1, 6)
    days_since  = (d - reference).days
    cycle       = 29.530589
    phase_days  = days_since % cycle

    if phase_days < 1.85:
        name, emoji = "New Moon", "🌑"
    elif phase_days < 7.38:
        name, emoji = "Waxing Crescent", "🌒"
    elif phase_days < 9.22:
        name, emoji = "First Quarter", "🌓"
    elif phase_days < 14.77:
        name, emoji = "Waxing Gibbous", "🌔"
    elif phase_days < 16.61:
        name, emoji = "Full Moon", "🌕"
    elif phase_days < 22.15:
        name, emoji = "Waning Gibbous", "🌖"
    elif phase_days < 23.99:
        name, emoji = "Last Quarter", "🌗"
    else:
        name, emoji = "Waning Crescent", "🌘"

    if phase_days <= 14.77:
        days_to_full = 14.77 - phase_days
    else:
        days_to_full = 14.77 + (cycle - phase_days)

    return name, emoji, round(phase_days, 1), int(days_to_full)


def moon_and_sky():
    log("Moon phase + sky events...")
    try:
        today = date.today()
        name, emoji, phase_days, days_to_full = _moon_phase_for(today)

        illumination = round(50 * (1 - math.cos(2 * math.pi * phase_days / 29.530589)), 0)
        illumination = int(min(illumination, 100))

        lines = [f"*{emoji} Moon & Sky — {TODAY}*"]
        lines.append(f"  {emoji} {name} — {illumination}% illuminated (day {phase_days} of cycle)")
        if name == "Full Moon":
            lines.append("  _Full moon tonight — the sky is bright._")
        elif days_to_full <= 1:
            lines.append(f"  _Full moon tomorrow._")
        elif days_to_full <= 3:
            lines.append(f"  _Full moon in {days_to_full} days._")

        # Check for sky events within ±2 days
        upcoming = []
        for m, d_, desc in SKY_EVENTS_2026:
            event_date = date(today.year, m, d_)
            delta = (event_date - today).days
            if -1 <= delta <= 3:
                if delta == 0:
                    upcoming.append(f"  🌠 *Today:* {desc}")
                elif delta == 1:
                    upcoming.append(f"  🌠 *Tomorrow:* {desc}")
                elif delta < 0:
                    upcoming.append(f"  🌠 *Yesterday:* {desc}")
                else:
                    upcoming.append(f"  🌠 *In {delta} days:* {desc}")
        if upcoming:
            lines.append("  *Upcoming sky events:*")
            lines.extend(upcoming)

        return "\n".join(lines)

    except Exception as e:
        return f"*🌙 Moon & Sky*\n_Error: {e}_"


# ── 10. Burbank Reddit ─────────────────────────────────────────────────────────

def burbank_reddit():
    log("Burbank subreddit...")
    try:
        req = urllib.request.Request(
            "https://www.reddit.com/r/burbank/.json?limit=10",
            headers={"User-Agent": "Nova/1.0 (nova@digitalnoise.net) nova_nightly_report.py"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        posts = data.get("data", {}).get("children", [])
        lines = [f"*🏘 Burbank Subreddit — {TODAY}*"]
        if not posts:
            lines.append("  _No posts found._")
            return "\n".join(lines)

        for p in posts[:8]:
            pd = p.get("data", {})
            title  = pd.get("title", "")[:80]
            score  = pd.get("score", 0)
            flair  = pd.get("link_flair_text", "")
            prefix = f"[{flair}] " if flair else ""
            lines.append(f"  • {prefix}{title} _(↑{score})_")

        return "\n".join(lines)

    except Exception as e:
        return f"*🏘 Burbank Subreddit*\n_Error: {e}_"


# ── 11. Meeting Notes ──────────────────────────────────────────────────────────

def meeting_notes():
    log("Meeting notes...")
    try:
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", "1",
             "http://127.0.0.1:37421/api/meetings?limit=20"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode != 0 or not r.stdout.strip():
            return ""  # App not running — skip silently

        meetings = json.loads(r.stdout)
        if isinstance(meetings, dict):
            meetings = meetings.get("meetings", meetings.get("data", []))

        today_str = date.today().isoformat()
        today_meetings = []

        for m in meetings:
            # Accept date fields in various formats
            m_date = m.get("date", m.get("created_at", m.get("timestamp", "")))
            if today_str in str(m_date):
                today_meetings.append(m)

        if not today_meetings:
            return ""  # No meetings today — skip, nothing for the dream

        lines = [f"*📋 Meetings Today — {TODAY}*"]
        for m in today_meetings:
            title   = m.get("title", m.get("name", m.get("person", "Meeting")))
            summary = m.get("summary", m.get("notes", m.get("content", "")))
            action_items = m.get("action_items", m.get("actionItems", []))

            lines.append(f"  *{title}*")
            if summary:
                # Trim to 200 chars for memory
                snippet = summary[:200].replace("\n", " ")
                lines.append(f"    {snippet}{'...' if len(summary) > 200 else ''}")
            if action_items:
                if isinstance(action_items, list):
                    for item in action_items[:3]:
                        text = item if isinstance(item, str) else item.get("text", str(item))
                        lines.append(f"    → {text[:100]}")
                elif isinstance(action_items, str):
                    lines.append(f"    → {action_items[:100]}")

        return "\n".join(lines)

    except Exception as e:
        return f"*📋 Meetings*\n_Error: {e}_"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log(f"Starting nightly report — {TODAY}")

    header = f"*🌙 Nova Nightly Report — {NOW.strftime('%A, %B %d %Y · %I:%M %p')}*\n{'─'*44}"
    slack_post(header)

    modules = [
        ("GitHub Digest",      github_digest),
        ("Email Action Items", email_action_items),
        ("Daily Memory Log",   nova_memory_log),
        ("Package Tracker",    package_tracker),
        ("Weather",            weather_report),
        ("HomeKit Status",     homekit_status),
        ("Moon & Sky",         moon_and_sky),
        ("Burbank Reddit",     burbank_reddit),
        ("Meeting Notes",      meeting_notes),
    ]

    results = {}
    for name, fn in modules:
        log(f"Running: {name}")
        try:
            result = fn()
            results[name] = result or ""
            if result:
                slack_post(result)
        except Exception as e:
            results[name] = f"Error: {e}"
            slack_post(f"*{name}*\n_Failed: {e}_")

    # Write everything to Nova's memory so the 2am dream has context
    write_dream_context(results)

    slack_post(f"_— Nova nightly report complete · {NOW.strftime('%I:%M %p')}_")
    log("Done.")


if __name__ == "__main__":
    main()
