#!/usr/bin/env python3
"""
nova_slack_watch.py — hourly sentinel over the #nova-* Slack channels.

Once an hour, read everything new in the nova-* channels, decide whether
anything is *notable* (alarming, unusual, or trending — not just routine
heartbeat chatter), and if so post a concise digest to #nova-critical.

Per Jordan's spec (2026-06-22):
  • Threshold:  "anything notable" — warnings, criticals, AND anything
                unusual/trending in the info channels.
  • Alert path: post the digest to #nova-critical (SLACK_BB).
  • Schedule:   permanent hourly launchd job, 24/7 (NO quiet hours).
  • Judgement:  local LLM (Ollama qwen3:30b-a3b) assesses the batch;
                deterministic keyword/severity heuristic is the fallback
                so the watch still fires if every LLM is down.

DATA SAFETY (Jordan, 2026-06-22 — "keep it as safe as possible about
sending data back to your LLM"):
  • Inference is LOCAL ONLY. The endpoint is asserted to be loopback
    (127.0.0.1 / localhost); a non-loopback URL is refused outright. No
    Slack content is EVER sent to a cloud LLM. If the local model is
    unreachable, we degrade to the offline heuristic — we never "fall
    forward" to a hosted API.
  • Every message is run through _redact() before it reaches the model:
    Slack/OpenAI/AWS/Bearer tokens, password=/secret= pairs, and long
    high-entropy strings are masked. The model sees the shape of an alert,
    not live credentials.
  • Each message is truncated to 400 chars — we send the minimum needed
    to judge notability, nothing more.

State lives in PostgreSQL (nova_ops):
  slack_watch_state(channel text pk, last_ts numeric)   — per-channel watermark
  slack_watch_reports(dedup_key text pk, ts timestamptz) — never repeat an item

Run:
  nova_slack_watch.py            # normal hourly run (reads, assesses, posts)
  nova_slack_watch.py --dry-run  # assess + print, do NOT post or advance state
"""
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

DB = "nova_ops"

# Channels we watch. We READ #nova-critical for context but never re-flag our
# own digests (they carry WATCH_MARKER) — that would be a feedback loop.
WATCH_CHANNELS = {
    "#nova-chat":     nova_config.SLACK_CHAN,
    "#nova-info":     nova_config.SLACK_FEED,
    "#nova-warning":  nova_config.SLACK_NOTIFY,
    "#nova-critical": nova_config.SLACK_BB,
    "#nova-email":    nova_config.SLACK_EMAIL,
}
ALERT_CHANNEL = nova_config.SLACK_BB          # where digests go (#nova-critical)
WATCH_MARKER  = "\U0001F52D Hourly Watch"      # 🔭 — tags our own posts

LOOKBACK_FIRST_RUN_S = 70 * 60                 # first ever scan: look back ~70 min

OLLAMA_URL   = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen3:30b-a3b"

# A message is inherently notable if it lands in these channels...
SEVERE_CHANNELS = {"#nova-warning", "#nova-critical"}
# ...or matches any of these (case-insensitive) — the heuristic fallback.
ALARM_WORDS = [
    "error", "fail", "failed", "failure", "critical", "fatal", "panic",
    "exception", "traceback", "crash", "crashed", "down", "offline",
    "unreachable", "timeout", "timed out", "denied", "unauthorized",
    "forbidden", "breach", "leak", "compromis", "intrusion", "malware",
    "disk full", "out of memory", "oom", "deadlock", "corrupt", "data loss",
    "503", "502", "500", "refused", "cannot connect", "no space",
]


# ── Slack I/O ─────────────────────────────────────────────────────────────────

def _slack_get(method: str, params: dict) -> dict:
    url = f"{nova_config.SLACK_API}/{method}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {nova_config.slack_bot_token()}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def fetch_new(channel_id: str, oldest_ts: float) -> list[dict]:
    """Return text messages STRICTLY newer than oldest_ts, oldest-first.

    Slack's `oldest` boundary is inclusive, so we also filter `ts > oldest_ts`
    in code — the watch is progress-only and must never re-assess (and thus
    risk re-alerting) a message it has already processed.
    """
    out, cursor = [], None
    for _ in range(10):  # page cap — plenty for one hour of traffic
        params = {"channel": channel_id, "oldest": f"{oldest_ts:.6f}", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _slack_get("conversations.history", params)
        if not data.get("ok"):
            print(f"[watch] slack error on {channel_id}: {data.get('error')}",
                  file=sys.stderr)
            break
        for m in data.get("messages", []):
            if m.get("subtype") in ("channel_join", "channel_leave"):
                continue
            ts = float(m["ts"])
            if ts <= oldest_ts:                       # strict: skip the boundary msg
                continue
            text = (m.get("text") or "").strip()
            # ignore our own watch digests (feedback-loop guard) + empty posts
            if not text or WATCH_MARKER in text:
                continue
            out.append({"ts": ts, "text": text})
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    out.sort(key=lambda m: m["ts"])
    return out


def post_alert(text: str) -> None:
    body = json.dumps({"channel": ALERT_CHANNEL, "text": text,
                       "unfurl_links": False, "unfurl_media": False}).encode()
    req = urllib.request.Request(
        f"{nova_config.SLACK_API}/chat.postMessage", data=body,
        headers={"Authorization": f"Bearer {nova_config.slack_bot_token()}",
                 "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read())
        if not resp.get("ok"):
            print(f"[watch] post failed: {resp.get('error')}", file=sys.stderr)


# ── State (PostgreSQL) ────────────────────────────────────────────────────────

def _db():
    import psycopg2
    conn = psycopg2.connect(f"host=localhost dbname={DB} user=kochj")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS slack_watch_state (
                channel text PRIMARY KEY, last_ts numeric NOT NULL);
            CREATE TABLE IF NOT EXISTS slack_watch_reports (
                dedup_key text PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now());
        """)
    return conn


def get_watermark(conn, channel: str) -> float:
    with conn.cursor() as cur:
        cur.execute("SELECT last_ts FROM slack_watch_state WHERE channel=%s", (channel,))
        row = cur.fetchone()
    if row:
        return float(row[0])
    return time.time() - LOOKBACK_FIRST_RUN_S


def set_watermark(conn, channel: str, ts: float) -> None:
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO slack_watch_state(channel,last_ts) VALUES(%s,%s)
                       ON CONFLICT(channel) DO UPDATE SET last_ts=EXCLUDED.last_ts""",
                    (channel, ts))


def already_reported(conn, key: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM slack_watch_reports WHERE dedup_key=%s", (key,))
        seen = cur.fetchone() is not None
        if not seen:
            cur.execute("INSERT INTO slack_watch_reports(dedup_key) VALUES(%s)", (key,))
    return seen


# ── Assessment ────────────────────────────────────────────────────────────────

def heuristic_flags(messages: list[dict]) -> list[dict]:
    """Deterministic fallback: flag severe-channel msgs + alarm-word matches."""
    flagged = []
    for m in messages:
        low = m["text"].lower()
        hit = m["channel"] in SEVERE_CHANNELS or any(w in low for w in ALARM_WORDS)
        if hit:
            flagged.append(m)
    return flagged


# Patterns masked before ANY text reaches the model. Order matters (specific
# token shapes first, generic high-entropy last).
_REDACTORS = [
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{8,}"),            "[slack-token]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),                "[api-key]"),
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{12,}"),             "[aws-key]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"),                 "[gh-token]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{8,}"),      "Bearer [redacted]"),
    (re.compile(r"(?i)\b(pass(word)?|secret|token|api[_-]?key)\s*[=:]\s*\S+"),
     r"\1=[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9._-]{10,}"), "[jwt]"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"),                   "[hex-secret]"),
]


def _redact(text: str) -> str:
    """Mask credential-shaped substrings so the model never sees live secrets."""
    for pat, repl in _REDACTORS:
        text = pat.sub(repl, text)
    return text


def _assert_local(url: str) -> None:
    """Refuse to send data anywhere but loopback. Hard guarantee, not a hope."""
    host = (urlparse(url).hostname or "").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError(
            f"refusing to send Slack content to non-loopback LLM endpoint: {host!r}")


def llm_assess(messages: list[dict]) -> dict | None:
    """Ask the LOCAL LLM what's notable. Returns dict or None if unreachable.

    Never contacts a non-loopback endpoint; never falls forward to the cloud.
    All message text is redacted before it leaves this process.
    """
    _assert_local(OLLAMA_URL)
    lines = [f"[{m['channel']}] {_redact(m['text'])[:400]}" for m in messages]
    try:
        import nova_voice
        facts = nova_voice.shared_context()
    except Exception:
        facts = ""
    prompt = (
        "You are Nova's operations sentinel. Below are the new messages from the "
        "last hour across Nova's Slack channels. Identify ONLY what is genuinely "
        "notable: errors, failures, outages, security concerns, or anything "
        "unusual/trending worth a human's attention. Ignore routine heartbeats, "
        "normal info digests, calendar items, and successful runs. If the CURRENT "
        "FACTS or RECENT ACTIVITY below (if present) show this is already a known, "
        "tracked, still-open finding rather than something new, say so plainly in "
        "\"why\" instead of treating it as a fresh discovery — do not invent a root "
        "cause or dramatize beyond what the messages actually say.\n"
        + facts + "\n\n"
        "Respond with STRICT JSON only:\n"
        '{"notable": true|false, "severity": "info|warning|critical", '
        '"headline": "one short line", "items": ['
        '{"channel": "#nova-x", "what": "what happened", "why": "why it matters"}]}\n'
        "If nothing is notable, return {\"notable\": false, \"items\": []}.\n\n"
        "MESSAGES:\n" + "\n".join(lines))
    body = json.dumps({
        "model": OLLAMA_MODEL, "stream": False,
        "format": "json", "options": {"temperature": 0.2},
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    try:
        req = urllib.request.Request(OLLAMA_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            content = json.loads(r.read())["message"]["content"]
        # qwen3 may wrap JSON in <think> or prose — grab the outermost object
        s, e = content.find("{"), content.rfind("}")
        return json.loads(content[s:e + 1]) if s >= 0 else None
    except Exception as ex:  # noqa: BLE001 — any failure → heuristic fallback
        print(f"[watch] LLM assess failed ({ex}); using heuristic", file=sys.stderr)
        return None


def build_digest(assessment: dict) -> str:
    sev = (assessment.get("severity") or "warning").lower()
    icon = {"critical": "\U0001F6A8", "warning": "⚠️", "info": "ℹ️"}.get(sev, "⚠️")
    head = assessment.get("headline") or "Notable activity in the nova channels"
    lines = [f"{WATCH_MARKER} {icon} *{head}*"]
    for it in assessment.get("items", []):
        ch = it.get("channel", "")
        what = it.get("what", "").strip()
        why = it.get("why", "").strip()
        bullet = f"• {('`'+ch+'` ') if ch else ''}{what}"
        if why:
            bullet += f" — _{why}_"
        lines.append(bullet)
    lines.append(f"_swept {len(assessment.get('items', []))} item(s) · "
                 f"{datetime.now(timezone.utc).astimezone():%Y-%m-%d %H:%M %Z}_")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry: bool = False) -> int:
    conn = _db()
    all_new: list[dict] = []
    newest: dict[str, float] = {}
    for name, cid in WATCH_CHANNELS.items():
        wm = get_watermark(conn, name)
        msgs = fetch_new(cid, wm)
        for m in msgs:
            m["channel"] = name
        all_new.extend(msgs)
        if msgs:
            newest[name] = msgs[-1]["ts"]

    if not all_new:
        print("[watch] no new messages; nothing to assess")
        return 0
    print(f"[watch] {len(all_new)} new message(s) across "
          f"{len(set(m['channel'] for m in all_new))} channel(s)")

    # Assess: LLM first, deterministic heuristic as the safety net.
    assessment = llm_assess(all_new)
    if assessment is None or not isinstance(assessment.get("items"), list):
        flagged = heuristic_flags(all_new)
        assessment = {
            "notable": bool(flagged),
            "severity": "critical" if any(
                m["channel"] == "#nova-critical" for m in flagged) else "warning",
            "headline": f"{len(flagged)} flagged message(s) (heuristic scan)",
            "items": [{"channel": m["channel"],
                       "what": m["text"][:200],
                       "why": "matched alarm heuristic"} for m in flagged[:15]],
        }

    notable = bool(assessment.get("notable")) and assessment.get("items")
    if notable:
        # dedup_key over the set of flagged items so we never repeat an alert
        sig = "|".join(sorted(f"{i.get('channel')}::{i.get('what','')[:120]}"
                              for i in assessment["items"]))
        key = hashlib.sha256(sig.encode()).hexdigest()[:24]
        digest = build_digest(assessment)
        if dry:
            print("[watch] DRY-RUN — would post:\n" + digest)
        elif already_reported(conn, key):
            print("[watch] notable, but identical to a prior alert — skipping post")
        else:
            post_alert(digest)
            print("[watch] posted digest to #nova-critical")
    else:
        print("[watch] nothing notable this hour")

    # Advance watermarks (skip in dry-run so a real run still sees the window)
    if not dry:
        for name, ts in newest.items():
            set_watermark(conn, name, ts)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(run(dry="--dry-run" in sys.argv))
