#!/usr/bin/env python3
"""nova_daily_threat_assessment.py — "all things coming my way," once a day.

Scans new inbound mail on Jordan's digitalnoise.net address since the last run, scores each
message for real threat signals (phishing/social-engineering/impersonation) vs.
ordinary cold sales outreach vs. benign, and rolls that up alongside the existing
per-domain daily reports Nova already produces — Fishbowl-directed harassment
(nova_fishbowl_watch.py), physical/neighborhood safety (nova_local_airwaves.py /
nova_block_report.py), and infrastructure security (nova_operations_security.py /
nova_cve_autopatch.py) — into ONE consolidated digest posted to #nova-info.

Deliberately does NOT attempt to unmask/deanonymize senders against public
personas on vibes alone — see 2026-07-19 email-agent design note. It flags real
threat signals and known-contact impersonation (a NEW address writing in a way
that closely matches an address already in dns_records/known-contacts, not a
speculative match against a public figure who's never corresponded by email).

Email scoring is quiet by design — every message gets scored, but only notable
ones (real threat signals, not routine sales spam) make the digest. Scheduled
daily on .6 (needs Mail.app/AppleScript access to this account).
Vectors, in order: inbound email (own scan), identity+threat mentions across ALL of
Nova's memory (not just Fishbowl-tagged content — the same pattern nova_fishbowl_watch.py
uses for real-time Fishbowl alerts, applied fleet-wide to the last 24h of new memories
from any source), infrastructure/host threat scores (Wazuh correlation output), plus
links to the existing physical-safety and Fishbowl daily reports for full context.
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_rando_daily_ops import call_llm

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
STATE_FILE = Path.home() / ".openclaw/workspace/state/email_threat_scan_seen.json"
MEM_STATE_FILE = Path.home() / ".openclaw/workspace/state/identity_threat_scan_last.json"
ACCOUNT = nova_config.JORDAN_DOMAIN_EMAIL

# Same identity/threat pattern nova_fishbowl_watch.py uses, applied across ALL memory
# sources (not just source='fishbowl') so a threat surfacing anywhere Nova ingests
# from — Reddit, scanner, news, wherever — doesn't only get caught if it happens to
# be watch-community content.
_IDENT_TERMS = ["jordan koch", "kochj", "jordan\\.koch", "digitalnoise", "little mister"]
IDENTITY_RE = re.compile(r"\b(" + "|".join(_IDENT_TERMS) + r")\b", re.I)
THREAT_TERMS = ("dox", "doxx", "fired", "your job", "your employer", "your work",
                 "end in tears", "coming after", "come after you", "expose you",
                 "your family", "your address", "get you fired", "contact your",
                 "real world", "and yours", "swat", "kill you", "hurt you")

SCORE_SYSTEM = """You are a calm, precise email-security triage assistant. For each email, classify it
into exactly one category and explain briefly why. Categories:

- "phishing": impersonates a known service/person to harvest credentials or payment, urgency+fear
  tactics, mismatched sender/reply-to, suspicious links.
- "social_engineering": tries to manipulate the recipient into an action (wire transfer, sharing
  secrets, granting access) via pretext, without necessarily being classic phishing.
- "impersonation": claims to be someone the recipient likely knows, but sender details don't match
  that person's known identity.
- "cold_outreach": ordinary unsolicited sales/marketing/recruiting email. Not a threat, just noise.
  Templated flattery, a specific-sounding but generic personalization, growth-hacking P.S. lines
  ("don't want to hear from me again? no worries...") are strong signals of this category.
  Real, specific personalization from a real product/company (referencing an actual public
  repo/project detail) is STILL cold_outreach, not a threat, if there is no credential/payment ask.
- "benign": personal correspondence, legitimate business, notifications, newsletters.

Do NOT speculate that a sender is secretly a specific named individual based on writing-style vibes
alone. Only flag impersonation when concrete identity markers conflict (e.g. claims to be a known
contact but the email address/domain doesn't match anything on record).

If the category is phishing, social_engineering, or impersonation, also include a "suggested_action":
one concrete defensive step (e.g. "block sender", "do not click links, report to platform", "this
claims to be a known contact from an unrecognized address — verify out-of-band before trusting it").

Respond with ONLY a JSON object: {"category": "...", "confidence": 0.0-1.0, "reasoning": "one or two sentences", "suggested_action": "..."}"""

EVIDENCE_DIR = Path.home() / ".openclaw/workspace/threat_evidence"


def log(m):
    print(f"[threat-assessment] {m}", flush=True)


def _load_seen():
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except Exception:
        return set()


def _save_seen(seen):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(list(seen)[-500:]))


def fetch_recent_inbox(hours=24):
    """Pull INBOX messages from the last N hours via Mail.app (AppleScript) —
    this account has no direct IMAP hook elsewhere in the fleet; match that."""
    script = f'''
    tell application "Mail"
        set theAccount to account "{ACCOUNT}"
        set theMailbox to mailbox "INBOX" of theAccount
        set cutoff to (current date) - ({hours} * hours)
        set theMessages to (messages of theMailbox whose date received > cutoff)
        set outStr to ""
        repeat with m in theMessages
            set msgId to message id of m
            set s to sender of m
            set subj to subject of m
            set bod to content of m
            if (length of bod) > 20000 then set bod to (text 1 thru 20000 of bod)
            set outStr to outStr & "###MSGID###" & msgId & "###SENDER###" & s & "###SUBJECT###" & subj & "###BODY###" & bod & "###END###" & linefeed
        end repeat
        return outStr
    end tell
    '''
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        log(f"AppleScript fetch failed: {r.stderr[:200]}")
        return []
    out = []
    for block in r.stdout.split("###END###"):
        if "###MSGID###" not in block:
            continue
        try:
            msgid = block.split("###MSGID###")[1].split("###SENDER###")[0].strip()
            sender = block.split("###SENDER###")[1].split("###SUBJECT###")[0].strip()
            subject = block.split("###SUBJECT###")[1].split("###BODY###")[0].strip()
            body = block.split("###BODY###")[1].strip()
        except IndexError:
            continue
        out.append({"msgid": msgid, "sender": sender, "subject": subject, "body": body})
    return out


def score_message(msg):
    user = f"From: {msg['sender']}\nSubject: {msg['subject']}\n\n{msg['body'][:2000]}"
    raw = call_llm(SCORE_SYSTEM, user, max_tokens=300)
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        return json.loads(raw[start:end])
    except Exception:
        return {"category": "benign", "confidence": 0.0, "reasoning": "scoring failed, defaulting safe"}


def ensure_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS email_threat_scan (
        id BIGSERIAL PRIMARY KEY,
        msgid TEXT UNIQUE,
        sender TEXT,
        subject TEXT,
        category TEXT,
        confidence REAL,
        reasoning TEXT,
        suggested_action TEXT,
        evidence_path TEXT,
        scanned_at TIMESTAMPTZ DEFAULT now()
    )""")


def write_evidence(msg, verdict):
    """Full, unredacted, timestamped record for anything notable enough to need one —
    a platform abuse report, a restraining-order filing, or law enforcement needs the
    complete original message, not the truncated snippet used for scoring."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    safe_id = "".join(c if c.isalnum() else "_" for c in msg["msgid"])[-40:]
    path = EVIDENCE_DIR / f"{ts}-{safe_id}.txt"
    path.write_text(
        f"THREAT ASSESSMENT EVIDENCE RECORD\n"
        f"Captured: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"Message-ID: {msg['msgid']}\n"
        f"From: {msg['sender']}\n"
        f"Subject: {msg['subject']}\n"
        f"Category: {verdict.get('category')}  Confidence: {verdict.get('confidence', 0):.0%}\n"
        f"Reasoning: {verdict.get('reasoning', '')}\n"
        f"Suggested action: {verdict.get('suggested_action', '')}\n"
        f"{'-'*70}\n"
        f"FULL ORIGINAL MESSAGE BODY:\n\n{msg['body']}\n"
    )
    return str(path)


def scan_memory_identity_threats():
    """Fleet-wide version of nova_fishbowl_watch.py's check — identity + threat
    co-occurrence across ALL memory sources ingested since the last run, not just
    source='fishbowl'. Advances its own watermark so each hit surfaces once."""
    try:
        last = json.loads(MEM_STATE_FILE.read_text()).get("last_seen")
    except Exception:
        last = None

    conn = psycopg2.connect(MEM_DSN); conn.autocommit = True
    cur = conn.cursor()
    if not last:
        cur.execute("SELECT max(created_at) FROM memories")
        newest = cur.fetchone()[0]
        MEM_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        MEM_STATE_FILE.write_text(json.dumps({"last_seen": newest.isoformat() if newest else None}))
        cur.close(); conn.close()
        return []  # baseline only, watch forward

    cur.execute("SELECT id, source, created_at, text FROM memories WHERE created_at > %s ORDER BY created_at", (last,))
    rows = cur.fetchall()
    hits = []
    newest = last
    for mid, source, ts, text in rows:
        newest = ts.isoformat()
        t = (text or "").lower()
        if IDENTITY_RE.search(t) and any(term in t for term in THREAT_TERMS):
            hits.append({"id": mid, "source": source, "ts": ts, "snippet": " ".join(text.split())[:300]})
    MEM_STATE_FILE.write_text(json.dumps({"last_seen": newest}))
    cur.close(); conn.close()
    log(f"identity+threat memory scan: {len(rows)} new memories checked (all sources), {len(hits)} hit(s)")
    return hits


def pull_infra_threat_summary(hours=24, baseline_days=7, anomaly_ratio=2.0):
    """Direct pull from Wazuh's own correlation output — but relative to each host's
    OWN baseline, not an absolute cutoff. These scores run from single digits to five
    figures depending purely on how chatty a host normally is (nova-core4 averages
    ~3680 with nothing wrong; TV-Movies-3 averages ~6) — an absolute threshold either
    misses real spikes on quiet hosts or permanently flags noisy ones. Flag only a
    host whose last-24h max meaningfully exceeds its own 7-day average."""
    conn = psycopg2.connect(DSN); conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        WITH recent AS (
            SELECT host_name, max(score) AS recent_max
            FROM host_threat_scores WHERE ts > now() - (%s || ' hours')::interval
            GROUP BY host_name
        ), baseline AS (
            SELECT host_name, avg(score) AS baseline_avg
            FROM host_threat_scores WHERE ts > now() - (%s || ' days')::interval
            GROUP BY host_name
        )
        SELECT r.host_name, r.recent_max, b.baseline_avg
        FROM recent r JOIN baseline b USING (host_name)
        WHERE b.baseline_avg > 0 AND r.recent_max > b.baseline_avg * %s
        ORDER BY (r.recent_max / b.baseline_avg) DESC
    """, (hours, baseline_days, anomaly_ratio))
    rows = cur.fetchall()

    # Pull the components breakdown for the peak reading so the digest can show WHY
    # a host is elevated (fim_changes from normal file edits vs. real auth_failures/
    # critical_events) instead of just a scary ratio number that cries wolf after
    # any busy admin night.
    enriched = []
    for host, recent_max, baseline_avg in rows:
        cur.execute("""SELECT components FROM host_threat_scores
                       WHERE host_name = %s AND ts > now() - (%s || ' hours')::interval
                       ORDER BY score DESC LIMIT 1""", (host, hours))
        comp_row = cur.fetchone()
        enriched.append((host, recent_max, baseline_avg, comp_row[0] if comp_row else {}))
    cur.close(); conn.close()
    return enriched


def publish_vague_local_article(n_emails, n_notable, n_identity_hits, n_infra_hits):
    """Public version, in /local like the other daily roundups — deliberately
    VAGUE. No sender addresses, no evidence paths, no host names, no specific
    vulnerability details: a public blog is not the place for operational
    security detail. Just an ambient 'Nova kept watch today' narrative. The
    real, actionable detail lives in the Slack digest and the evidence files."""
    import nova_journal as nj
    import nova_voice

    if n_emails == 0 and n_identity_hits == 0 and n_infra_hits == 0:
        nj.log("[threat-assessment] nothing at all today — skipping the local article")
        return

    material = (
        f"Today Nova quietly screened {n_emails} inbound emails, watched for her name/identity "
        f"paired with threatening language across every source she ingests from (not just the "
        f"usual watch-community drama), and checked in on the fleet's own security posture. "
        f"{'Nothing notable turned up anywhere.' if (n_notable + n_identity_hits + n_infra_hits) == 0 else 'A handful of things got flagged and handled quietly — nothing dramatic enough to spell out here, just the ordinary business of paying attention.'}"
    )
    system = nova_voice.system_prompt(nova_voice.CONTEXT_JOURNAL_LOCAL + """
This is a short (300-500 word), DELIBERATELY VAGUE ambient piece for the public /local section.
Do NOT invent or reveal specific senders, email addresses, host names, vulnerability details, or
counts beyond what's given. The whole point is atmosphere and reassurance, not an incident report
— think "the watchman's log," not "the security bulletin." Nova's usual voice, low-key confident,
a little wry. No section headers needed, this is short.
OUTPUT EXACTLY THIS SHAPE:\nTITLE: <short title, no quotes>\n<blank line>\n<the body>""")
    raw = nj.call_openrouter(system, material, max_tokens=1200, temperature=0.85)
    if not raw:
        nj.log("[threat-assessment] local article generation failed (non-fatal)")
        return
    title, body = None, []
    for ln in raw.splitlines():
        if title is None and ln.upper().startswith("TITLE:"):
            title = ln.split(":", 1)[1].strip().strip('"')
        else:
            body.append(ln)
    body = "\n".join(body).strip()
    if not title:
        title = f"Keeping Watch — {nj.today_str()}"
    img = None
    try:
        ip = nj.get_image_prompt(title, "a quiet nighttime watch, an AI keeping an eye on things", "local")
        img = nj.generate_image(ip, width=1024, height=768, section="local")
    except Exception as e:
        nj.log(f"[threat-assessment] image gen failed (non-fatal): {e}")
    tags = ["local", "security", "daily"]
    desc = "Nova's daily note that she's still watching."
    nj.publish_hugo(title, body, "local", tags, desc, image_path=img, emoji="🕯️", stable_slug="daily-watch")
    nj.git_push("local", title)
    nj.log(f"[threat-assessment] published vague local article: {title}")


def rollup_link(section, label):
    """Best-effort: does today's article for a given section exist? Link if so."""
    import datetime
    dt = datetime.date.today().isoformat()
    content_dir = Path.home() / f"nova-journal/content/{section}"
    if not content_dir.exists():
        return None
    matches = sorted(content_dir.glob(f"{dt}-*.md"))
    if not matches:
        return None
    slug = matches[-1].stem
    return f"<https://nova.digitalnoise.net/{section}/{slug}/|{label}>"


def main():
    conn = psycopg2.connect(DSN); conn.autocommit = True
    cur = conn.cursor()
    ensure_table(cur)

    seen = _load_seen()
    msgs = fetch_recent_inbox(hours=24)
    new_msgs = [m for m in msgs if m["msgid"] not in seen]
    log(f"{len(msgs)} messages in last 24h, {len(new_msgs)} not yet scored")

    notable = []
    for m in new_msgs:
        verdict = score_message(m)
        is_notable = (verdict.get("category") in ("phishing", "social_engineering", "impersonation")
                      and verdict.get("confidence", 0) >= 0.5)
        evidence_path = write_evidence(m, verdict) if is_notable else None
        cur.execute(
            "INSERT INTO email_threat_scan (msgid, sender, subject, category, confidence, reasoning, suggested_action, evidence_path) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (msgid) DO NOTHING",
            (m["msgid"], m["sender"], m["subject"], verdict.get("category"),
             verdict.get("confidence", 0), verdict.get("reasoning", ""),
             verdict.get("suggested_action", ""), evidence_path))
        seen.add(m["msgid"])
        if is_notable:
            notable.append((m, verdict))
        time.sleep(0.5)  # be gentle on the local LLM

    _save_seen(seen)

    identity_hits = scan_memory_identity_threats()
    infra_hits = pull_infra_threat_summary()

    lines = [":shield: *Daily threat assessment — everything coming your way*\n"]
    lines.append(f"*Inbound email* ({len(new_msgs)} scanned): " +
                 (f"{len(notable)} notable" if notable else "nothing notable — routine mail and the usual sales noise only"))
    for m, v in notable[:10]:
        lines.append(f"  • *{v['category']}* ({v['confidence']:.0%}) from {m['sender']} — \"{m['subject']}\"")
        lines.append(f"    {v['reasoning']}")
        if v.get("suggested_action"):
            lines.append(f"    → {v['suggested_action']}")

    lines.append(f"\n*Identity + threat mentions, all memory sources* ({len(identity_hits)} hit(s) in the last 24h):")
    if identity_hits:
        for h in identity_hits[:10]:
            lines.append(f"  • [{h['source']}] {h['ts']} — {h['snippet']}")
    else:
        lines.append("  nothing — this is checked across every memory source, not just Fishbowl")

    lines.append(f"\n*Infrastructure threat scores* ({len(infra_hits)} host(s) running hot vs. their own baseline):")
    if infra_hits:
        for host, recent_max, baseline_avg, comp in infra_hits:
            auth = comp.get("auth_failures", 0)
            crit = comp.get("critical_events", 0)
            fim = comp.get("fim_changes", 0)
            warn = comp.get("warning_events", 0)
            flag = ":rotating_light:" if (auth or crit) else ":information_source:"
            lines.append(f"  • {flag} {host}: {recent_max:.0f} vs. {baseline_avg:.0f} baseline ({recent_max/baseline_avg:.1f}x) — "
                         f"fim_changes={fim} warnings={warn} auth_failures={auth} critical={crit}")
    else:
        lines.append("  all hosts clean")

    fishbowl_link = rollup_link("fishbowl", "Fishbowl watch")
    airwaves_link = rollup_link("local", "neighborhood/scanner roundup")
    security_link = rollup_link("operations", "infra security report")
    other = [l for l in (fishbowl_link, airwaves_link, security_link) if l]
    if other:
        lines.append("\n*Also today:* " + " · ".join(other))
    lines.append("\n_Online-harassment alerts (Fishbowl) fire in real time separately — this is a rollup, not the only line of defense._")

    nova_config.post_both("\n".join(lines), slack_channel=nova_config.SLACK_INFO)
    log(f"posted Slack digest: {len(new_msgs)} emails, {len(notable)} notable, "
        f"{len(identity_hits)} identity hits, {len(infra_hits)} infra hosts flagged")
    cur.close(); conn.close()

    publish_vague_local_article(len(new_msgs), len(notable), len(identity_hits), len(infra_hits))


if __name__ == "__main__":
    main()
