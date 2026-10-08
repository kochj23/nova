#!/usr/bin/env python3
"""
nova_slack_answers.py — harvest one-word answers from Slack (six-month build #4/#7, 2026-09-28).

Reads every open row in slack_prompts and looks at its Slack thread (and its reactions,
when the app has the reactions:read scope). Two kinds:

  question  -> the first human reply in the thread becomes reflection_questions.answer;
               a 👍/👎 reaction counts as "yes"/"no".
  proposal  -> a reply starting yes/approve/ok/👍 approves the co-agency proposal via the
               existing CLI (so trust bookkeeping happens); no/reject/👎 rejects it.
               Pending proposals that have never been put to him are posted here, one line
               each, at most PROPOSALS_PER_RUN per run, so approval is one reply, not a psql.

Read-only over Slack, writes only its own table plus the two ledgers it exists to close.

  nova_slack_answers.py            # run
  nova_slack_answers.py --dry-run  # print what it would record
  nova_slack_answers.py --selftest
"""
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
CHANNEL = "C0AMNQ5GX70"
BOT_USER = "U0ANKLR3SUQ"
HUMANS = {"U049EPC2W"}            # Jordan. ONLY these users can answer or decide (2026-09-29: Nova's own
                                  # gateway replied "Yes" in her proposal threads and got counted as him).
try:  # proactivity dial — exactly 3 at the default (nova_voice.dial_scale)
    from nova_voice import dial_scale as _dial_scale
    _PPD = int(round(_dial_scale("proactivity", 1, 3, 6)))
except Exception:  # pragma: no cover
    _PPD = 3
PROPOSALS_PER_DAY = _PPD             # was per RUN every 10m -> 24 posts overnight. Now a daily allowance,
POST_HOURS = range(9, 18)         # posted only during his working hours.
YES_RE = re.compile(r"^\s*(all\s+(approved|good|yes)|approve(d)?\s+all|yes|y|yep|yeah|approve|approved|ok|okay|sure|do it|go|👍|:\+1:|:thumbsup:)(?=\W|$)", re.I)
BLANKET_RE = re.compile(r"^\s*(all\s+(approved|good|yes)|approve(d)?\s+all)\b", re.I)
BLANKET_WINDOW_MIN = 30           # a top-level "All approved" covers the prompts posted in the 30 min before it
NO_RE = re.compile(r"^\s*(no|n|nope|reject|rejected|keep|don'?t|leave it|👎|:-1:|:thumbsdown:)(?=\W|$)", re.I)
SCRIPTS = Path(__file__).resolve().parent


def log(m):
    print(f"[slack-answers {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def _token():
    if sys.platform == "darwin":
        return subprocess.check_output(["security", "find-generic-password", "-a", "nova",
                                        "-s", "nova-slack-bot-token", "-w"]).decode().strip()
    import nova_secrets
    return nova_secrets.get_secret("nova-slack-bot-token")


def slack(method, **params):
    tok = _token()
    if method.startswith("chat."):
        req = urllib.request.Request(f"https://slack.com/api/{method}", method="POST",
                                     headers={"Authorization": f"Bearer {tok}",
                                              "Content-Type": "application/json; charset=utf-8"},
                                     data=json.dumps(params).encode())
    else:
        req = urllib.request.Request(f"https://slack.com/api/{method}?{urllib.parse.urlencode(params)}",
                                     headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


# ── pure logic ────────────────────────────────────────────────────────────────

def verdict(text):
    """'yes' | 'no' | None from a reply's opening words."""
    if YES_RE.search(text or ""):
        return "yes"
    if NO_RE.search(text or ""):
        return "no"
    return None


def first_human_reply(messages, bot_user=BOT_USER, humans=HUMANS):
    """messages: conversations.replies list (root first). First reply from an allowlisted human —
    not the bot, not any other bot or app, not an unknown user."""
    for m in messages[1:]:
        if m.get("user") == bot_user or m.get("bot_id") or m.get("subtype") or m.get("user") not in humans:
            continue
        if (m.get("text") or "").strip():
            return m
    return None


def reaction_verdict(reactions, humans=HUMANS):
    names = {r.get("name") for r in (reactions or []) if set(r.get("users") or []) & humans}
    if names & {"+1", "thumbsup", "white_check_mark", "heavy_check_mark"}:
        return "yes"
    if names & {"-1", "thumbsdown", "x"}:
        return "no"
    return None


# ── harvesting ────────────────────────────────────────────────────────────────

def read_answer(channel, ts):
    """-> (answer_text, verdict) or (None, None)."""
    try:
        rep = slack("conversations.replies", channel=channel, ts=ts, limit=20)
        if rep.get("ok"):
            m = first_human_reply(rep.get("messages", []))
            if m:
                t = m["text"].strip()
                return t, verdict(t)
    except Exception as e:  # noqa: BLE001
        log(f"replies failed for {ts}: {e}")
    try:
        rx = slack("reactions.get", channel=channel, timestamp=ts, full="true")
        if rx.get("ok"):
            v = reaction_verdict(rx.get("message", {}).get("reactions"))
            if v:
                return v, v
    except Exception as e:  # noqa: BLE001
        log(f"reactions failed for {ts}: {e}")   # missing_scope until the app is granted reactions:read
    return None, None


def record_question(cur, qid, answer, dry):
    if dry:
        print(f"Q#{qid} -> {answer!r}"); return
    try:
        sys.path.insert(0, str(SCRIPTS)); import nova_reflection
        nova_reflection.record_answer(int(qid), answer)     # also ingests the Q&A as a memory
    except Exception:  # noqa: BLE001
        cur.execute("UPDATE reflection_questions SET answer=%s, answered_at=now() WHERE id=%s", (answer, int(qid)))


def confirm(channel, ts, text, dry):
    """One short line in the thread so he knows it was recorded (and the chat agent stays out of it)."""
    if dry:
        print(f"   (would confirm in thread {ts}: {text})"); return
    try:
        slack("chat.postMessage", channel=channel, thread_ts=ts, text=text)
    except Exception as e:  # noqa: BLE001
        log(f"confirm failed: {e}")


def blanket_approvals(cur, dry):
    """Jordan's top-level 'All approved' in the channel approves every open proposal prompt posted in
    the BLANKET_WINDOW_MIN before it (2026-09-28 11:58 — four proposals stayed open because the parser
    wanted the word first)."""
    import time as _t
    try:
        hist = slack("conversations.history", channel=CHANNEL, oldest=str(_t.time() - 48 * 3600), limit=200)
    except Exception as e:  # noqa: BLE001
        log(f"history failed: {e}"); return 0
    n = 0
    for m in hist.get("messages", []):
        if m.get("user") not in HUMANS or m.get("thread_ts") not in (None, m.get("ts")) or not BLANKET_RE.search(m.get("text") or ""):
            continue
        cur.execute("SELECT id, ref_id, ts FROM slack_prompts WHERE kind='proposal' AND resolved_at IS NULL "
                    "AND posted_at <= to_timestamp(%s) AND posted_at > to_timestamp(%s) - (%s || ' minutes')::interval",
                    (float(m["ts"]), float(m["ts"]), str(BLANKET_WINDOW_MIN)))
        for sid, ref, ts in cur.fetchall():
            if decide_proposal(ref, "yes", "All approved (blanket, top-level)", dry):
                if not dry:
                    cur.execute("UPDATE slack_prompts SET resolved_at=now(), result=%s WHERE id=%s", ("All approved (blanket)", sid))
                confirm(CHANNEL, ts, f"Recorded: approved #{ref} via your \"All approved\". Handed to Claude.", dry)
                n += 1
    return n


def decide_proposal(pid, v, note, dry):
    mode = "approve" if v == "yes" else "reject"
    if dry:
        print(f"proposal #{pid} -> {mode} ({note!r})"); return True
    r = subprocess.run([sys.executable, str(SCRIPTS / "nova_coagency.py"), "--mode", mode, "--id", str(pid),
                        "--by", "jordan (slack reply)", "--note", note[:200]],
                       capture_output=True, text=True, timeout=60)
    log(f"coagency {mode} #{pid}: rc={r.returncode} {(r.stdout or r.stderr)[-120:].strip()}")
    return r.returncode == 0


def _annie_ok(text):
    try:
        import nova_annie_rule
        return nova_annie_rule.ok(text)
    except Exception:  # noqa: BLE001
        return True


def _turning_point(cur, text, backlog):
    try:
        import nova_turning_point
        return nova_turning_point.decide(cur, "proposal", stakes=min(1.0, 0.65 + 0.05 * backlog),
                                         text=text, ceiling="recommend")
    except Exception as e:  # noqa: BLE001
        return {"allowed": True, "reason": f"turning point unavailable ({e})"}


def post_pending_proposals(cur, dry):
    if datetime.now().hour not in POST_HOURS:
        return
    cur.execute("SELECT count(*) FROM slack_prompts WHERE kind='proposal' AND posted_at > now() - interval '24 hours'")
    room = PROPOSALS_PER_DAY - cur.fetchone()[0]
    if room <= 0:
        return
    cur.execute("SELECT id, origin, proposed_action, left(rationale, 220) FROM coagency_proposals "
                "WHERE status='pending_human' AND id::text NOT IN "
                "(SELECT ref_id FROM slack_prompts WHERE kind='proposal') ORDER BY created_at LIMIT %s",
                (room,))
    rows = cur.fetchall()
    for pid, origin, action, why in rows:
        text = (f"Proposal #{pid} ({origin}): {action}\n_{why}_\n"
                f"_Reply *yes* or *no* in this thread._")
        if dry:
            print(text); continue
        # Annie Wilkes rule + turning point: a proposal is a "recommend" — one per post,
        # stakes rise with how long it has waited for him; held ones wait for next week.
        if not _annie_ok(text):
            log(f"proposal #{pid} text fails the Annie Wilkes rule — not posting it")
            continue
        tp = _turning_point(cur, text, len(rows))
        if not tp["allowed"]:
            log(f"proposal #{pid} held — turning point: {tp['reason']}")
            break
        d = slack("chat.postMessage", channel=CHANNEL, text=text)
        if d.get("ok"):
            cur.execute("INSERT INTO slack_prompts (kind, ref_id, channel, ts) VALUES ('proposal', %s, %s, %s) "
                        "ON CONFLICT DO NOTHING", (str(pid), CHANNEL, d["ts"]))
            log(f"put proposal #{pid} to him")


def main():
    dry = "--dry-run" in sys.argv
    conn = psycopg2.connect(OPS_DSN, connect_timeout=5); conn.autocommit = True; cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS slack_prompts (
        id bigserial PRIMARY KEY, kind text NOT NULL, ref_id text NOT NULL,
        channel text NOT NULL, ts text NOT NULL, posted_at timestamptz NOT NULL DEFAULT now(),
        resolved_at timestamptz, result text, UNIQUE (kind, ref_id))""")
    cur.execute("SELECT id, kind, ref_id, channel, ts FROM slack_prompts WHERE resolved_at IS NULL "
                "AND posted_at > now() - interval '14 days'")
    closed = 0
    for sid, kind, ref, ch, ts in cur.fetchall():
        answer, v = read_answer(ch, ts)
        if not answer:
            continue
        if kind == "question":
            record_question(cur, ref, answer, dry); ok = True
            confirm(ch, ts, f"Recorded your answer to Q#{ref}. Thank you.", dry)
        elif kind == "proposal":
            if not v:
                continue            # a comment, not a decision — leave it open
            ok = decide_proposal(ref, v, answer, dry)
            if ok:
                confirm(ch, ts, f"Recorded: {'approved' if v == 'yes' else 'rejected'} #{ref}."
                                + (" Handed to Claude." if v == "yes" else ""), dry)
        elif kind == "conflict":    # wish #58: any reply in the thread IS the decision (posted by nova_directive_decide.py)
            if not dry:
                cur.execute("UPDATE directive_conflicts SET status='decided', decided_by='jordan', decided_at=now(), "
                            "decision_note=%s WHERE id=%s AND status IN ('open','decided')", (answer[:600], int(ref)))
            ok = True
            confirm(ch, ts, f"Recorded your decision on directive conflict #{ref}. Thank you.", dry)
        else:
            continue
        if ok and not dry:
            cur.execute("UPDATE slack_prompts SET resolved_at=now(), result=%s WHERE id=%s", (answer[:300], sid))
        closed += 1
    closed += blanket_approvals(cur, dry)
    post_pending_proposals(cur, dry)
    log(f"closed {closed} prompt(s)")
    return 0


def demo():
    assert verdict("Yes, go ahead") == "yes" and verdict("nope") == "no" and verdict("the ex") is None
    assert verdict("All approved") == "yes" and verdict("approve all of them") == "yes" and BLANKET_RE.search("All approved")
    assert verdict("All of these need work") is None
    assert verdict("👍") == "yes" and verdict("keep it") == "no"
    msgs = [{"user": "U1", "text": "root"}, {"user": BOT_USER, "text": "bot noise"},
            {"user": "U0GATEWAY", "text": "Yes"},                       # another bot/app account: ignored
            {"user": "U049EPC2W", "text": "  Tricia  "}, {"user": "U049EPC2W", "text": "later"}]
    assert first_human_reply(msgs)["text"].strip() == "Tricia"
    assert first_human_reply(msgs[:3]) is None
    assert reaction_verdict([{"name": "+1", "users": ["U049EPC2W"]}]) == "yes"
    assert reaction_verdict([{"name": "+1", "users": ["U0GATEWAY"]}]) is None
    assert reaction_verdict([{"name": "x", "users": ["U049EPC2W"]}]) == "no"
    print("all slack-answers assertions passed")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        demo()
    else:
        sys.exit(main())
