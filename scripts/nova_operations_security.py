#!/usr/bin/env python3
"""nova_operations_security.py — Nova's DAILY morning Security Operations report (07:30).

A dated /operations article in Nova's OPERATIONS voice: did the overnight security scans run
on each machine (rkhunter/aide/chkrootkit host scans, the Strix purple-team pentest, Wazuh),
what did they find, plus the overnight Wazuh event picture, new vendor CVEs, open security
queue items, and remediations taken. Fires 07:30 daily — after the 07:00 Wazuh daily summary,
the last scan to wrap up.
"""
import sys
from collections import defaultdict
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj
import nova_voice

OPS_DSN = "host=localhost dbname=nova_ops user=kochj"


def q(cur, sql, args=None):
    try:
        cur.execute(sql, args) if args else cur.execute(sql)  # no args -> skip %-subst so literal % in ILIKE is safe
        return cur.fetchall()
    except Exception:
        cur.connection.rollback()
        return []


def main():
    c = psycopg2.connect(OPS_DSN); c.autocommit = True; cur = c.cursor()

    scans = q(cur, "SELECT host_name, scan_type, status, coalesce(findings::text,'[]') FROM security_scan_results "
                   "WHERE scan_time > now() - interval '30 h' ORDER BY host_name, scan_type")
    wz = q(cur, "SELECT count(*), mode() WITHIN GROUP (ORDER BY rule_description) FROM security_events "
                "WHERE ts > now() - interval '14 h'")
    wz_hi = q(cur, "SELECT rule_description, count(*) FROM security_events WHERE ts > now() - interval '14 h' "
                   "AND rule_level >= 10 GROUP BY 1 ORDER BY 2 DESC LIMIT 5")
    strix = q(cur, "SELECT title, coalesce(body,'') FROM telemetry.events WHERE ts > now() - interval '30 h' "
                   "AND category ILIKE '%strix%' ORDER BY ts DESC LIMIT 4")
    cves = q(cur, "SELECT DISTINCT title FROM telemetry.events WHERE ts > now() - interval '30 h' "
                  "AND (category ILIKE '%vendor%' OR category ILIKE '%cve%' OR title ILIKE '%CVE-%') LIMIT 6")
    queue = q(cur, "SELECT description FROM claude_queue WHERE status='queued' "
                   "AND (description ILIKE '%security%' OR description ILIKE '%CVE%') "
                   "ORDER BY priority NULLS LAST LIMIT 8")
    rem = q(cur, "SELECT action, tier, status FROM remediations WHERE requested_at > now() - interval '30 h' "
                 "ORDER BY requested_at DESC LIMIT 6")

    byhost = defaultdict(list)
    for hn, st, status, findings in scans:
        detail = f"={status}"
        if status != "clean" and findings and findings != "[]":
            detail += f" {findings[:90]}"
        byhost[hn].append(f"{st}{detail}")
    scan_block = "\n".join(f"- {h}: {'; '.join(v)}" for h, v in sorted(byhost.items())) or "(no host scans recorded in the last 30h)"
    wz_block = (f"{wz[0][0]} events overnight; most common rule: {wz[0][1]}") if wz and wz[0][0] else "(no Wazuh events recorded)"
    wzhi_block = "; ".join(f"{d} ({n})" for d, n in wz_hi) or "none (nothing at level 10+)"
    strix_block = "\n".join(f"- {t}: {(b or '')[:220]}" for t, b in strix) or "(no Strix run logged to the event bus in the window)"
    cve_block = "\n".join(f"- {t}" for (t,) in cves) or "none new"
    queue_block = "\n".join(f"- {d[:120]}" for (d,) in queue) or "none open"
    rem_block = "\n".join(f"- {a} [{t}] {s}" for a, t, s in rem) or "none in the window"

    if not scans and not wz:
        nj.log("[ops-security] no scan data at all — aborting (scans may not have run)"); return 1

    ctx = (
        "Write TODAY'S morning SECURITY OPERATIONS report for Nova's journal (the /operations section), in "
        "Nova's OPERATIONS voice — the steward who ran the overnight scans and reports plainly, with dry wit. "
        "This is a daily infrastructure-security report that fires at 07:30 once the overnight scans wrap up:\n"
        "- Lead with the bottom line: are we clean, or is there something that actually needs attention? A quiet "
        "night is a GOOD report — say so plainly, do NOT manufacture drama or hype.\n"
        "- Cover the scan RUNS: host rootkit/integrity scans (rkhunter, chkrootkit, aide) per machine, the Strix "
        "purple-team pentest, and Wazuh. Say which machines scanned and their status.\n"
        "- Call out anything REAL. DISMISS known false positives plainly: chkrootkit's 'basename'/'bindshell' noise, "
        "and scan ERRORS from RETIRED hosts — 'lts01' was retired ~a month ago, so its scan errors/criticals are "
        "stale artifacts, NOT a threat; note it should be dropped from the scan list.\n"
        "- Then the overnight Wazuh event picture, any new vendor CVEs affecting our gear, open security-queue items, "
        "and remediations taken.\n"
        "- Honest and precise. Dry wit, not a thriller. If it was a clean night, a short clean report is the right report.\n"
        "400-800 words, markdown, section headers welcome, no H1 title (added separately).\n\n"
        "OUTPUT EXACTLY THIS SHAPE:\nTITLE: <one clear title, no quotes>\n<blank line>\n<the body>")
    system = nova_voice.system_prompt(ctx)
    user = (
        f"--- HOST SCANS (rkhunter/aide/chkrootkit, last 30h) ---\n{scan_block}\n\n"
        f"--- STRIX PURPLE-TEAM PENTEST ---\n{strix_block}\n\n"
        f"--- WAZUH (overnight) ---\n{wz_block}\nHigh-severity (level 10+): {wzhi_block}\n\n"
        f"--- NEW VENDOR CVEs (our gear) ---\n{cve_block}\n\n"
        f"--- OPEN SECURITY QUEUE ---\n{queue_block}\n\n"
        f"--- REMEDIATIONS (last 30h) ---\n{rem_block}\n\n"
        "Write today's morning security operations report.")
    raw = nj.call_openrouter(system, user, max_tokens=2400, temperature=0.6)
    if not raw:
        nj.log("[ops-security] LLM produced nothing — aborting"); return 1

    title, body = None, []
    for ln in raw.splitlines():
        if title is None and ln.upper().startswith("TITLE:"):
            title = ln.split(":", 1)[1].strip().strip('"')
        else:
            body.append(ln)
    body = "\n".join(body).strip()
    if not title or len(title) < 6:
        title = f"Security Operations — {nj.today_str()}"

    img = None
    try:
        ip = nj.get_image_prompt(title, "a pre-dawn security operations center — server racks, scan dashboards, calm", "operations")
        img = nj.generate_image(ip, width=1024, height=768, section="operations")
    except Exception as e:
        nj.log(f"[ops-security] image gen failed (non-fatal): {e}")

    tags = ["operations", "security", "scans", "daily"]
    desc = "Nova's daily morning security-operations report — overnight scan health + posture across the fleet."
    nj.publish_hugo(title, body, "operations", tags, desc, image_path=img, emoji="🛡️")  # dated post
    nj.git_push("operations", title)
    nj.notify_slack("operations", f"🛡️ {title}", "Nova's morning security-ops report.")
    nj.log(f"[ops-security] PUBLISHED: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
