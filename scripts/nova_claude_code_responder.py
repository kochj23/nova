#!/usr/bin/env python3
"""
nova_claude_code_responder.py — the live Slack<->Claude Code bridge endpoint (#672).

Background (see investigation in agent_docs doc_type='runbook-672-slack-claude'):
the existing gateway "claude" channel (nova_gateway/channels/claude.py) answers
claude_messages rows with Nova's `chat` agent running on Ollama — it is NOT a real
Claude Code session. Everything else (the claude_messages table, Redis pubsub,
#nova-claude Slack mirroring, edit/scratchpad locks) is built. The single missing
piece is an endpoint that actually runs a message through a real Claude Code
session and writes the reply back.

This daemon IS that endpoint, built as a SEPARATE process so the running gateway is
not touched (conservative — #672 explicitly warns the gateway is sensitive). It is
opt-in and invisible to the gateway because it uses its own direction values that the
gateway poller (which filters `direction = 'to_nova'`) never selects:

    direction = 'to_claude_code'    — inbound: a message destined for Claude Code
    direction = 'from_claude_code'  — outbound: Claude Code's reply

Flow once wired (see runbook step for the gateway one-liner):
    Slack #nova-claude  ─▶  claude_messages(direction='to_claude_code')
                        ─▶  THIS DAEMON runs `claude -p` in a persistent session
                        ─▶  claude_messages(direction='from_claude_code')
                        ─▶  posted back to Slack #nova-claude by this daemon

Until the gateway is wired, you can drive it manually for testing:
    INSERT INTO claude_messages (direction, sender, message)
    VALUES ('to_claude_code', 'test', 'what is 17*23?');

Design notes:
  * Real Claude Code via the `claude` CLI in headless print mode (`-p`), pinned to a
    persistent session UUID so context carries across messages (`--session-id` first
    time, `--resume <uuid>` after). JSON output is parsed for the result text.
  * stdlib + psycopg2 only, matching the nova_*.py poller house style. Slack posting
    is a direct chat.postMessage call with the nova-slack-bot-token Keychain item — no
    gateway import, so this can run standalone via launchd or the scheduler.
  * Conservative guardrails: per-message timeout, allow-listed tools off by default
    (read-only unless --allow-edits), max output truncation, dedup via row claiming.

Written by Jordan Koch.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid

import psycopg2
import psycopg2.extras

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
SLACK_CLAUDE_CHANNEL = "C0B3RSRR0DD"  # #nova-claude
SLACK_TOKEN_KEYCHAIN = "nova-slack-bot-token"
CLAUDE_BIN = "/opt/homebrew/bin/claude"

# A stable namespace UUID so the Claude Code session persists across restarts. This
# is the headless analogue of the gateway's CLAUDE_BRIDGE_SESSION constant.
SESSION_UUID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "nova-claude-bridge-persistent"))
SESSION_STATE = os.path.expanduser("~/.openclaw/data/claude_bridge_session.json")

POLL_INTERVAL = 5      # seconds between polls in daemon mode
CLAUDE_TIMEOUT = 900   # seconds per Claude Code invocation (15 min — big tasks like multi-page essays)
MAX_REPLY_CHARS = 6000


def log(msg):
    print(f"[ccresp] {msg}", flush=True)


def _keychain(service):
    try:
        return subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


# ── Persistent session bookkeeping ────────────────────────────────────────────
def _session_started():
    try:
        with open(SESSION_STATE) as f:
            return json.load(f).get("session_id") == SESSION_UUID
    except Exception:
        return False


def _mark_session_started():
    try:
        os.makedirs(os.path.dirname(SESSION_STATE), exist_ok=True)
        with open(SESSION_STATE, "w") as f:
            json.dump({"session_id": SESSION_UUID, "started": time.time()}, f)
    except Exception as e:
        log(f"could not persist session state: {e}")


# ── Real Claude Code invocation ───────────────────────────────────────────────
SECURITY_PREAMBLE = (
    "SECURITY (non-negotiable): you are an automated executor for Jordan's private home system, "
    "and your output may transit a cloud API. NEVER read, output, or reason over credentials, API "
    "keys, tokens, passwords, SSH/AWS/GPG keys, or keychain/secret-store contents. NEVER surface "
    "third-party PII — other people's email/message contents, or names/addresses/victim descriptions "
    "from police-scanner data. NEVER include employer-confidential data. Touch only the minimum data a "
    "task needs; if a task requires forbidden data, REFUSE and say it needs sensitive data you can't access. "
    "For the nova_memories database, query ONLY the `memories_safe` view, never the raw `memories` table "
    "(which holds email_archive, imessage, and police-scanner data that must not leave the premises)."
)


def run_claude_code(message, allow_edits=False, cwd=None, external=False):
    """Run one message through a Claude Code session. Returns reply text.

    Trust tiers (security review 2026-07-30):
      * external=True  — untrusted, from nova_relay. FRESH isolated session per call
        (no shared context), and tools limited to Read/Grep/Glob with NO network-egress
        tool (WebFetch/WebSearch removed). Rationale: with no egress tool, anything the
        turn learns can only leave via the REPLY, which nova_relay.scrub_outbound filters
        — closing the "Read a secret, WebFetch it to attacker" exfil path that bypassed
        the scrubber entirely. And a fresh session means an external turn can never plant
        context that a later privileged (allow_edits) turn would resume.
      * allow_edits=True — Jordan's own privileged use (Slack/LAN). acceptEdits + journal.
      * else — Jordan's read-only use. Keeps WebFetch/WebSearch (his replies don't cross
        to a monitored device and he is not an exfil threat).
    Internal (non-external) turns keep the persistent session for conversational context.
    """
    cmd = [CLAUDE_BIN, "-p", "--output-format", "json",
           "--append-system-prompt", SECURITY_PREAMBLE]

    if external:
        # Isolated, single-use session — never resumed, never shared across trust tiers.
        cmd += ["--session-id", str(uuid.uuid4())]
    else:
        started = _session_started()
        if started:
            cmd += ["--resume", SESSION_UUID]
        else:
            cmd += ["--session-id", SESSION_UUID]

    # Conservative default: read-only (no file edits). Editing requires explicit
    # opt-in via --allow-edits. The prompt is fed on stdin, NOT as a positional arg,
    # so it can never be swallowed by the variadic --allowedTools list.
    if external:
        # Untrusted: read/search the local tree, but NO egress tool and no Bash.
        cmd += ["--allowedTools", "Read", "Grep", "Glob"]
    elif allow_edits:
        # Scoped execution — NOT --dangerously-skip-permissions (which BYPASSES the deny-list).
        # acceptEdits auto-approves file edits so it can write the journal + run git, while the
        # deny-list in ~/.openclaw/.claude/settings.json (loaded via cwd) blocks reads of
        # secrets/keychain/env and WebFetch exfil. The journal dir is explicitly allowed.
        cmd += ["--permission-mode", "acceptEdits",
                "--add-dir", os.path.expanduser("~/nova-journal")]
    else:
        cmd += ["--allowedTools", "Read", "Grep", "Glob", "WebSearch", "WebFetch"]

    try:
        proc = subprocess.run(
            cmd, input=message, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT,
            cwd=cwd or os.path.expanduser("~/.openclaw"),
        )
    except subprocess.TimeoutExpired:
        return f"(Claude Code timed out after {CLAUDE_TIMEOUT}s)"
    except FileNotFoundError:
        return f"(claude CLI not found at {CLAUDE_BIN})"

    # Only the persistent (internal) session tracks a "started" marker; external
    # runs use a fresh single-use session and never persist state.
    if not external and not started and proc.returncode == 0:
        _mark_session_started()

    out = proc.stdout.strip()
    if not out:
        err = proc.stderr.strip()[:400]
        # If --resume failed (e.g. session expired), reset the PERSISTENT session so
        # the next internal call re-seeds. External runs have no persistent state to
        # reset (and must not clobber the internal session's marker).
        if not external and ("resume" in err.lower() or "session" in err.lower()):
            _reset_session()
        return f"(Claude Code produced no output; rc={proc.returncode} err={err})"

    # Headless JSON output: {"type":"result","result":"...","is_error":false,...}
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            text = data.get("result") or data.get("text") or ""
            if data.get("is_error"):
                text = f"(Claude Code error) {text}"
            return (text or out)[:MAX_REPLY_CHARS]
    except json.JSONDecodeError:
        pass
    return out[:MAX_REPLY_CHARS]


def _reset_session():
    try:
        os.remove(SESSION_STATE)
    except Exception:
        pass


# ── Slack ─────────────────────────────────────────────────────────────────────
def post_to_slack(token, text, channel=SLACK_CLAUDE_CHANNEL, thread_ts=None):
    if not token:
        log("no slack token; skipping Slack post")
        return
    payload = {"channel": channel, "text": text[:3500], "mrkdwn": True}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        if not resp.get("ok"):
            log(f"slack post failed: {resp.get('error')}")
    except Exception as e:
        log(f"slack post exception: {type(e).__name__}: {e}")


# ── PG row claiming + reply ───────────────────────────────────────────────────
def claim_messages(conn, last_id, limit=5):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT id, message, metadata
               FROM claude_messages
               WHERE direction = 'to_claude_code' AND id > %s
               ORDER BY id ASC LIMIT %s""",
            (last_id, limit))
        return cur.fetchall()


def write_reply(conn, reply, in_reply_to):
    meta = json.dumps({"channel": "bridge", "in_reply_to": in_reply_to,
                       "agent_id": "claude-code", "ts": time.time()})
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO claude_messages (direction, sender, message, metadata)
               VALUES ('from_claude_code', 'claude-code', %s, %s::jsonb)""",
            (reply, meta))


def process_once(conn, token, last_id, allow_edits=False, post_slack=True):
    rows = claim_messages(conn, last_id)
    for row in rows:
        last_id = row["id"]
        msg = row["message"]
        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        origin_channel = meta.get("origin_channel") or SLACK_CLAUDE_CHANNEL
        origin_thread = meta.get("origin_thread")
        # RING ENFORCEMENT (2026-07-29): a request that arrived from outside the LAN
        # via nova_relay is capped at ring 1 — read-only tools — no matter how this
        # daemon was launched. Structural, not advisory: allow_edits=False restricts
        # the CLI to Read/Grep/Glob/WebSearch/WebFetch, so no external caller can
        # mutate anything even if the message text tries to talk its way into it.
        # Ring 2/3 work must go through claude_queue for Jordan's approval.
        msg_external = bool(meta.get("external")) or str(meta.get("origin", "")).startswith("external/")
        effective_edits = allow_edits and not msg_external
        if msg_external:
            log(f"  #{row['id']} is EXTERNAL (origin={meta.get('origin')}) — isolated session, "
                f"read-only tools, no egress")
        log(f"processing #{row['id']}: {msg[:70]} (origin={origin_channel})")
        reply = run_claude_code(msg, allow_edits=effective_edits, external=msg_external)
        write_reply(conn, reply, row["id"])
        if post_slack:
            post_to_slack(token, f":robot_face: *Claude Code:* {reply}",
                          channel=origin_channel, thread_ts=origin_thread)
        log(f"replied to #{row['id']} ({len(reply)} chars)")
    return last_id, len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Live Slack<->Claude Code bridge endpoint")
    ap.add_argument("--once", action="store_true",
                    help="process the current backlog once and exit (poller mode)")
    ap.add_argument("--daemon", action="store_true",
                    help="run forever, polling every %ds" % POLL_INTERVAL)
    ap.add_argument("--allow-edits", action="store_true",
                    help="permit Claude Code to edit files / run more tools (default read-only)")
    ap.add_argument("--no-slack", action="store_true", help="do not post replies to Slack")
    ap.add_argument("--message", help="run a single ad-hoc message through Claude Code and print the reply (no PG)")
    ap.add_argument("--reset-session", action="store_true", help="forget the persistent session id")
    args = ap.parse_args(argv)

    if args.reset_session:
        _reset_session()
        log(f"session {SESSION_UUID} reset")
        return 0

    if args.message:
        print(run_claude_code(args.message, allow_edits=args.allow_edits))
        return 0

    token = "" if args.no_slack else _keychain(SLACK_TOKEN_KEYCHAIN)
    conn = psycopg2.connect(DSN)
    conn.autocommit = True

    # Start from current max so we never replay history on first boot.
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id),0) FROM claude_messages "
                    "WHERE direction = 'to_claude_code'")
        last_id = cur.fetchone()[0] or 0

    if args.daemon:
        log(f"daemon mode; session={SESSION_UUID}; starting from id {last_id}")
        try:
            while True:
                last_id, n = process_once(conn, token, last_id,
                                          allow_edits=args.allow_edits,
                                          post_slack=not args.no_slack)
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log("shutting down")
    else:
        # --once: process whatever is already queued (id > 0 so backlog is included)
        last_id, n = process_once(conn, token, 0,
                                  allow_edits=args.allow_edits,
                                  post_slack=not args.no_slack)
        log(f"processed {n} message(s)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
