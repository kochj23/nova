#!/usr/bin/env python3
"""
nova_journal_security.py — Daily security intelligence briefing + breaking alerts.

Daily (9am): PDB-style briefing covering all security events from the past 24 hours
  - Cyber threats (CVEs, APTs, exploits, breaches)
  - Military/geopolitical (US force posture, NATO, conflict zones)
  - Physical security (SoCal/LA area, critical infrastructure)

Breaking: Immediate article + Slack/chat alert on:
  - Any actively-exploited CVE
  - Nation-state APT campaigns
  - Critical infrastructure attacks
  - Military escalations involving US/NATO
  - Mass-exploitation events
  - Major physical security events in SoCal/LA

Tone: Presidential Daily Brief — terse, factual, bullet-pointed, confidence levels,
sources cited. No personality, no humor.

Written by Jordan Koch (via Claude).
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
import nova_journal
from nova_image_utils import generate_image
from nova_notify import notify as nova_notify

# ── Config ────────────────────────────────────────────────────────────────────

HUGO_ROOT = (Path.home() / "nova-journal")
CONTENT_DIR = HUGO_ROOT / "content/operations"
IMAGES_DIR = HUGO_ROOT / "static/images/operations"
LOG_FILE = Path.home() / ".openclaw/logs/nova_journal_security.log"
# Internal Wazuh/firewall/IDS telemetry is summarized on-box (local Ollama)
# before any cloud call. The raw ops_brief must never reach the cloud LLM.
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "qwen3-coder:30b"
MEMORY_SERVER = f"http://{nova_config.NOVA_HOST}:18790"
from nova_resolve import resolve_url
SEARXNG_URL = resolve_url("searxng", "/search")

CONTENT_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)


# ── Ops Context (Wazuh + Big Brother + SNMP + syslog) ─────────────────────────
try:
    from nova_ops_context import get_full_context, format_security_brief
except ImportError:
    def get_full_context(hours=24): return {}
    def format_security_brief(ctx): return ""

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[security-journal {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ── API Key ───────────────────────────────────────────────────────────────────

def call_llm(system: str, user: str, max_tokens: int = 6000, temperature: float = 0.3) -> str:
    """Delegate to the shared local Claude Code CLI path (see nova_journal.call_openrouter —
    OpenRouter itself ran dry 2026-07-17; this also fixes nova-core, which has no working
    route to Keychain-backed secrets at all, unlike this script's old direct OpenRouter call."""
    return nova_journal.call_openrouter(system, user, max_tokens=max_tokens, temperature=temperature)


def _strip_internal_identifiers(text: str) -> str:
    """Belt-and-suspenders: scrub internal IPs/MACs/hostnames from text before
    it can reach the cloud, even after local summarization."""
    if not text:
        return ""
    s = str(text)
    s = re.sub(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2}\b",
               "an internal host", s)
    s = re.sub(r"\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b", "a device", s)
    s = re.sub(r"(?i)\bjordan'?s[-_ ]?\w*", "a personal device", s)
    return s


def call_local_llm(system: str, user: str, max_tokens: int = 1500) -> str:
    """Summarize on LOCAL Ollama only. Used to condense INTERNAL Wazuh/firewall/
    IDS telemetry into a generic posture summary before any cloud call. Returns
    text or '' on failure. Matches nova_inbox_claude.py's local-call idiom."""
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": f"/no_think\n\n{system}\n\n{user}",
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2, "num_predict": max_tokens},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read())
        text = (result.get("response") or "").strip()
        if "</think>" in text:
            text = text.split("</think>", 1)[-1].strip()
        return text
    except Exception as e:
        log(f"Local LLM error: {e} — internal telemetry will NOT be sent to cloud")
        return ""


def summarize_ops_brief_local(ops_brief: str) -> str:
    """Condense the INTERNAL infra/security telemetry (Wazuh SIEM, firewall
    blocks, IDS) into a short, generic posture line ON-BOX, then strip any
    residual internal IPs/MACs/hostnames. The raw ops_brief never leaves the
    machine — only this sanitized summary is eligible for the cloud prompt."""
    if not ops_brief or not ops_brief.strip():
        return ""
    system = (
        "You summarize a private home network's own security telemetry into ONE "
        "or TWO generic sentences for a public briefing. ABSOLUTELY NO internal "
        "IP addresses, MAC addresses, hostnames, device names, file paths, exact "
        "IDS signatures, or counts that could fingerprint the network. Describe "
        "only the overall posture in abstract terms (e.g. 'routine perimeter "
        "noise, nothing actioned' or 'elevated scanning, all blocked'). If the "
        "telemetry shows nothing notable, say 'No notable internal security "
        "activity.' Output only the summary sentence(s)."
    )
    summary = call_local_llm(system, f"INTERNAL TELEMETRY (do not echo specifics):\n{ops_brief}")
    if not summary:
        # On-box summarizer unavailable: fail closed — emit a generic line rather
        # than ever forwarding the raw internal telemetry to the cloud.
        return "No notable internal security activity (local summarizer unavailable)."
    return _strip_internal_identifiers(summary)


# ── Memory Fetching ───────────────────────────────────────────────────────────

def recall_memories(query: str, n: int = 30, source: str = None) -> list[dict]:
    params = {"q": query, "n": str(n)}
    if source:
        params["source"] = source
    url = f"{MEMORY_SERVER}/recall?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        return data if isinstance(data, list) else data.get("results", data.get("memories", []))
    except Exception as e:
        log(f"Memory recall failed: {e}")
        return []


def get_recent_security_memories(hours: int = 24) -> list[dict]:
    """Fetch recent memories from intelligence, military_history, law, and politics vectors."""
    memories = []
    for source in ["intelligence", "military_history", "law", "politics"]:
        try:
            result = subprocess.run(
                ["psql", "-h", "192.168.1.6", "-U", "kochj", "-d", "nova_memories", "-tA", "-c",
                 f"SELECT text, source, metadata::text FROM memories "
                 f"WHERE source = '{source}' "
                 f"AND created_at >= now() - interval '{hours} hours' "
                 f"AND LENGTH(text) > 60 "
                 f"ORDER BY created_at DESC LIMIT 40;"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if line.strip():
                        parts = line.split("|")
                        if parts[0].strip():
                            memories.append({
                                "text": parts[0].strip()[:500],
                                "source": parts[1].strip() if len(parts) > 1 else source,
                            })
        except Exception as e:
            log(f"PG query failed for {source}: {e}")
    return memories


def search_news(query: str, n: int = 5) -> list[dict]:
    """Search SearxNG for breaking news."""
    params = urllib.parse.urlencode({"q": query, "format": "json", "categories": "news", "time_range": "day"})
    url = f"{SEARXNG_URL}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        results = []
        for r in data.get("results", [])[:n]:
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", "")[:300],
            })
        return results
    except Exception:
        return []


def fetch_article_text(url: str, timeout: int = 15) -> str:
    """Fetch a news article page and strip it to plain text (best-effort).

    This is the retrieve step of retrieve-then-generate: the pipeline gets the SOURCE TEXT
    itself and hands it to the LLM, instead of giving it a bare headline and expecting it to
    browse (it can't — call_llm is plain text-gen with no web tools, which is exactly why it
    used to reply 'I need the URL to fetch the article'). Returns '' on any failure.
    """
    if not url:
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 NovaBot/1.0"})
        html = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="replace")
        text = re.sub(r"<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&#?\w+;", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:4000]
    except Exception:
        return ""


# ── Publishing ────────────────────────────────────────────────────────────────

def publish_hugo(title: str, body: str, tags: list[str], description: str,
                 image_path: str | None = None, is_breaking: bool = False) -> str:
    # Publish gate: block refusals / "I need the URL" clarifying-questions / stubs. This is
    # the pipeline that published the 2026-07-25 "I'm ready to fetch... need the URL" stub.
    from nova_journal_guard import is_publishable
    ok, reason = is_publishable(title, body)
    if not ok:
        log(f"[guard] BLOCKED security publish '{title[:60]}': {reason}")
        try:
            nova_config.post_both(f":no_entry: Suppressed a non-publishable security brief — {reason}\n  _{title[:90]}_",
                                  slack_channel=getattr(nova_config, "SLACK_INFO", None))
        except Exception:
            pass
        return ""
    dt = time.strftime("%Y-%m-%d")
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
    filename = f"{dt}-{slug}.md"

    hugo_image = ""
    if image_path and Path(image_path).exists():
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        img_dest = IMAGES_DIR / f"{dt}-{slug}.webp"
        try:
            subprocess.run(
                ["cwebp", "-q", "82", "-resize", "1200", "0", image_path, "-o", str(img_dest)],
                capture_output=True, timeout=30
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            import shutil
            shutil.copy2(image_path, img_dest)
        if img_dest.exists():
            hugo_image = f"/images/operations/{dt}-{slug}.webp"

    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-07:00")
    tags_yaml = json.dumps(tags)
    safe_title = title.replace('"', '')
    emoji = "🛡️"
    display_title = f"{emoji} {safe_title}"

    front_matter = f'''---
title: "{display_title}"
date: {timestamp}
draft: false
categories: ["operations"]
tags: {tags_yaml}
description: "{description.replace('"', "'")}"
'''
    if hugo_image:
        front_matter += f'cover:\n  image: "{hugo_image}"\n  alt: "{safe_title}"\n  relative: false\n'
    front_matter += "---\n\n"

    if hugo_image:
        body = f"![{safe_title}]({hugo_image})\n\n{body}"

    pub_time = time.strftime("%A, %B %d, %Y at %I:%M %p PT")
    byline = f"*Published {pub_time}*\n\n"

    output = CONTENT_DIR / filename
    output.write_text(front_matter + byline + body)
    log(f"Published: security/{filename}")

    # Git push (clear stale lock files if present)
    try:
        lock_file = HUGO_ROOT / ".git" / "index.lock"
        if lock_file.exists():
            # Check if lock is stale (older than 5 minutes)
            lock_age = time.time() - lock_file.stat().st_mtime
            if lock_age > 300:
                lock_file.unlink()
                log(f"Cleared stale git lock ({lock_age:.0f}s old)")
            else:
                log(f"Git lock exists ({lock_age:.0f}s old) — skipping push")
                return filename

        result = subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log(f"Git add failed: {result.stderr.strip()}")
            return filename
        msg = f"security: {dt} — {title[:50]}"
        result = subprocess.run(["git", "commit", "-m", msg], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            result = subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                log("Pushed to GitHub")
            else:
                # Another writer pushed first (non-fast-forward). Rebase on top and retry once.
                log(f"Push rejected, rebasing + retrying: {result.stderr[:120]}")
                subprocess.run(["git", "pull", "--rebase"], cwd=HUGO_ROOT, capture_output=True, timeout=60)
                result = subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=60)
                if result.returncode == 0:
                    log("Pushed to GitHub after rebase")
                else:
                    log(f"Push still failed after rebase: {result.stderr[:200]} — commit is safe, ships next run")
        elif "nothing to commit" in result.stdout:
            log("Nothing to commit (already pushed)")
        else:
            log(f"Git commit failed: {result.stderr.strip()}")
    except Exception as e:
        log(f"Git error: {e}")

    return filename


def notify(title: str, preview: str, is_breaking: bool = False):
    prefix = "BREAKING" if is_breaking else "Daily Briefing"
    # Declare intent: breaking security events are critical; the daily briefing
    # is an FYI digest. Routing is decided centrally by nova_notifier.
    nova_notify(
        f"Nova Security — {prefix}: {title}",
        body=preview[:250],
        level="critical" if is_breaking else "info",
        category="security",
        dedup_key=None if is_breaking else "security-daily-briefing",
    )
    # Breaking alerts also go to Nova's chat (interactive, non-alert) — leave as-is.
    if is_breaking:
        emoji = ":rotating_light::rotating_light:"
        msg = f"{emoji} *Nova Security — {prefix}*\n*{title}*\n_{preview[:250]}_"
        nova_config.post_both(msg, slack_channel=nova_config.SLACK_CHAT)


# ── Daily Briefing ────────────────────────────────────────────────────────────

def generate_daily_briefing():
    """Generate the daily PDB-style security briefing."""
    log("=== Generating daily security briefing ===")

    # Get unified ops/security context from Wazuh, BB, SNMP, syslog.
    # This is INTERNAL telemetry — summarize it on-box and strip identifiers
    # BEFORE it can reach the cloud LLM. Only `ops_brief_public` is sent up.
    ops_ctx = get_full_context(24)
    ops_brief_raw = format_security_brief(ops_ctx)
    log(f"Ops context: {ops_ctx.get('security', {}).get('security_event_count', 0)} security events, "
        f"{ops_ctx.get('syslog', {}).get('firewall_blocks', 0)} firewall blocks")
    ops_brief_public = summarize_ops_brief_local(ops_brief_raw)
    log(f"Internal telemetry condensed on-box → {len(ops_brief_public)} chars sent to cloud")

    memories = get_recent_security_memories(24)
    if not memories:
        log("No security memories in last 24h — skipping")
        return

    # Also search for breaking news
    cyber_news = search_news("cybersecurity vulnerability exploit breach 2026")
    military_news = search_news("US military NATO deployment 2026")
    local_news = search_news("Los Angeles security crime emergency 2026")

    memory_block = "\n".join(f"- [{m.get('source','?')}] {m['text'][:300]}" for m in memories[:60])
    news_block = ""
    if cyber_news:
        news_block += "\nCYBER NEWS (last 24h):\n" + "\n".join(f"- {n['title']}: {n['content'][:150]}" for n in cyber_news)
    if military_news:
        news_block += "\nMILITARY NEWS (last 24h):\n" + "\n".join(f"- {n['title']}: {n['content'][:150]}" for n in military_news)
    if local_news:
        news_block += "\nLOCAL (LA/SoCal):\n" + "\n".join(f"- {n['title']}: {n['content'][:150]}" for n in local_news)

    system = """You write Presidential Daily Brief-style security intelligence summaries. Rules:

FORMAT:
- Start with a one-line BLUF (Bottom Line Up Front) — the single most important thing
- Then 3-5 sections: CYBER, MILITARY/GEOPOLITICAL, PHYSICAL/LOCAL, NUCLEAR/WMD (if applicable), ASSESSMENT
- Each section: 3-7 bullet points maximum
- Each bullet: one fact, one source attribution in brackets, one confidence level if uncertain
- End with KEY JUDGMENTS (2-3 sentences of analytical assessment)

STYLE:
- Terse. No filler words. No adjectives unless they convey information.
- "[HIGH CONFIDENCE]", "[MODERATE CONFIDENCE]", "[LOW CONFIDENCE]" where applicable
- Source attribution: [CISA], [NCSC-UK], [Krebs], [SANS], [Unit42], etc.
- "NOSIG" (no significant activity) for quiet sections — don't fabricate threats
- Dates in DD MMM format (02 JUN)
- Times in 24h Zulu (1400Z) where relevant
- No editorializing, no recommendations unless specifically about immediate action required

CONTENT PRIORITIES (for the reader — a senior SRE/infrastructure engineer in Los Angeles):
1. Actively-exploited vulnerabilities affecting production infrastructure
2. APT campaigns targeting US/allied organizations
3. Military posture changes involving US/NATO forces
4. Critical infrastructure threats (power, water, telecom, internet backbone)
5. Physical security events in Southern California
6. Supply chain attacks, dependency compromises
7. Nuclear/WMD developments (IAEA reports, test activity)

OUTPUT: Title line (no markdown header) + body. No preamble. ~1000-2000 words."""

    user = f"""Write today's security intelligence briefing ({time.strftime('%d %b %Y')}).

INTELLIGENCE FROM LAST 24 HOURS (Nova's ingested feeds — CISA, NCSC, FBI, Krebs, Talos, Unit42, Bellingcat, War on the Rocks, etc.):
{memory_block}

LIVE NEWS SEARCH RESULTS:
{news_block}

INFRASTRUCTURE SECURITY (generic on-box posture summary — no internal specifics):
{ops_brief_public}

Write the PDB. If a section has no significant activity, mark it NOSIG and move on. Do not invent threats."""

    result = call_llm(system, user, max_tokens=4000)
    if not result or len(result) < 300:
        log("LLM generation failed or too short")
        return

    # Extract title
    lines = result.strip().split("\n")
    title = lines[0].strip().lstrip("#").strip()
    body = "\n".join(lines[1:]).strip()

    # Generate image
    img_prompt = "Satellite surveillance view of global threat map, dark blue tones, digital grid overlay, minimal, no text"
    try:
        img_path = generate_image(img_prompt, section="security")
    except Exception as e:
        log(f"Image gen failed: {e}")
        img_path = None

    snap = nova_journal.grafana_panel_image("security-posture", 5, "operations", "daily-briefing-posture")
    if snap:
        body += f"\n\n---\n\n**Our own posture, for context:**\n\n![Endpoint events by severity]({snap})"

    # Publish
    tags = ["daily-briefing", "pdb", "cyber", "military", "osint"]
    description = f"Daily security intelligence briefing — {time.strftime('%d %b %Y')}"
    publish_hugo(title, body, tags, description, image_path=img_path)
    notify(title, body[:200])
    log(f"=== Daily briefing complete: {title} ===")


# ── Breaking Alert ────────────────────────────────────────────────────────────

def generate_breaking_alert(trigger: str, details: str):
    """Generate an immediate breaking security alert."""
    log(f"=== BREAKING ALERT: {trigger} ===")

    # Gather context
    context_memories = recall_memories(trigger, n=15, source="intelligence")
    context_block = "\n".join(f"- {m.get('text', '')[:200]}" for m in context_memories[:10])

    # RETRIEVE the actual source article when the details are thin (just a headline). The LLM
    # can't browse, so we fetch it here and hand it the text — otherwise it replies "I need
    # the URL" (root cause of the 2026-07-25 stub). Search the headline -> fetch the top hit.
    source_block = ""
    if len(details or "") < 400:
        hits = search_news(trigger, n=3)
        for h in hits:
            body_txt = fetch_article_text(h.get("url", ""))
            if len(body_txt) > 300:
                source_block = f"\nSOURCE ARTICLE ({h.get('url')}):\n{body_txt}"
                break
        if not source_block and hits:   # fall back to search snippets if fetch failed
            source_block = "\nSEARCH RESULTS (headlines + snippets):\n" + \
                "\n".join(f"- {h['title']}: {h.get('content', '')} [{h.get('url')}]" for h in hits)

    system = """You write BREAKING security alerts in PDB style. Rules:

FORMAT:
- BLUF first line — what happened, who is affected, what to do
- DETAILS section — 3-5 bullets of confirmed facts only
- IMPACT section — who/what is affected, scope
- RECOMMENDED ACTIONS — immediate steps (if any)
- SOURCES — attribution

STYLE:
- URGENT tone but factual — no speculation, no fear-mongering
- If details are uncertain, say so explicitly
- ~300-600 words maximum
- No preamble, no sign-off

CRITICAL: This is a one-shot generation. You will receive NO further input. NEVER ask the
operator for anything — not a URL, not clarification, not "can you provide". Write ONLY from
the material below. If it's insufficient to confirm the event, write a short "DEVELOPING —
monitoring" note from what IS available and flag it as unconfirmed. Your entire output must be
a publishable alert; a request for input is never acceptable.

OUTPUT: Title line + body."""

    user = f"""BREAKING security event. Generate an alert.

TRIGGER: {trigger}

DETAILS PROVIDED:
{details}
{source_block}

RELATED CONTEXT FROM NOVA'S MEMORY:
{context_block}

Write the breaking alert from the material above (you cannot fetch anything — it's all here).
Only include confirmed information; flag uncertainty explicitly. Never ask for a URL or input."""

    result = call_llm(system, user, max_tokens=2000, temperature=0.2)
    if not result or len(result) < 100:
        log("Breaking alert generation failed")
        return

    lines = result.strip().split("\n")
    title = lines[0].strip().lstrip("#").strip()
    body = "\n".join(lines[1:]).strip()

    # Generate alert image
    try:
        img_path = generate_image(
            "Red alert warning screen, cyber attack visualization, urgent dark red glow, network under attack, no text",
            section="security"
        )
    except Exception:
        img_path = None

    snap = nova_journal.grafana_panel_image("nova-security", 8, "operations", "breaking-alert-posture")
    if snap:
        body += f"\n\n---\n\n**Recent high-severity events at publish time:**\n\n![Recent high-severity events]({snap})"

    import re as _re
    source_slug = _re.sub(r'[^a-z0-9]+', '-', trigger.lower()).strip('-')[:40]
    tags = ["breaking-alert", source_slug, "security"]
    description = f"BREAKING: {trigger[:100]}"
    publish_hugo(title, body, tags, description, image_path=img_path, is_breaking=True)
    notify(title, body[:200], is_breaking=True)
    log(f"=== Breaking alert published: {title} ===")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "breaking":
        trigger = sys.argv[2] if len(sys.argv) > 2 else "Unknown security event"
        details = sys.argv[3] if len(sys.argv) > 3 else ""
        generate_breaking_alert(trigger, details)
    else:
        generate_daily_briefing()
