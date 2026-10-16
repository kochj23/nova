#!/usr/bin/env python3
"""
nova_slack_answers.py — harvest one-word answers from Slack (six-month build #4/#7, 2026-09-28).

Reads every open row in slack_prompts and looks at its Slack thread (and its reactions,
when the app has the reactions:read scope). Two kinds:

  question  -> the first human reply in the thread becomes reflection_questions.answer;
               a 👍/👎 reaction counts as "yes"/"no".
  proposal  -> a reply starting yes/approve/ok/👍 approves the co-agency proposal via the
               existing CLI (so trust bookkeeping happens); no/reject/👎 rejects it.
               (2026-10-15: no longer posts pending proposals to him — Nova decides them on her
               own path; ASK_JORDAN_PROPOSALS = False. His replies to older prompts still count.)

Read-only over Slack, writes only its own table plus the two ledgers it exists to close.

  nova_slack_answers.py            # run
  nova_slack_answers.py --dry-run  # print what it would record
  nova_slack_answers.py --selftest
"""
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import psycopg2

import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
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
    """Slack Web API call, 3 tries with backoff on transient failures (connection errors,
    429, 5xx, keychain hiccups). A chat.* write is NOT retried after a read timeout —
    the post may have landed and a retry would double-post."""
    last = None
    for attempt in range(3):
        try:
            return _slack_once(method, params)
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise
            last = e
        except (urllib.error.URLError, subprocess.CalledProcessError) as e:
            last = e
        except OSError as e:
            if method.startswith("chat."):
                raise
            last = e
        log(f"slack {method} failed ({last}) — attempt {attempt + 1}/3")
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    raise last


def _slack_once(method, params):
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


def quiet_active(cur=None) -> bool:
    """nova_relationship.quiet_mode(): during a hard stretch, hold non-urgent nags and say less.
    Fails open to 'not quiet' (quiet_mode itself never raises; a missing module reads inactive)."""
    try:
        import nova_relationship
        return bool(nova_relationship.quiet_mode(cur).get("active"))
    except Exception:  # noqa: BLE001
        return False


_QUIET = None   # memoised per run by main(); None = not yet read


def confirm(channel, ts, text, dry):
    """One short line in the thread so he knows it was recorded (and the chat agent stays out of it).
    Quiet mode lowers verbosity: just "Recorded." """
    if _QUIET:
        text = "Recorded."
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
            # #29: a blanket yes never executes a proposal her own value_check denied — mode_decide refuses
            # it (rc 1), so it is neither confirmed nor resolved here.
            if decide_proposal(ref, "yes", "All approved (blanket, top-level)", dry):
                if not dry:
                    cur.execute("UPDATE slack_prompts SET resolved_at=now(), result=%s WHERE id=%s", ("All approved (blanket)", sid))
                confirm(CHANNEL, ts, f"Recorded: approved #{ref} via your \"All approved\". Handed to Claude.", dry)
                n += 1
    return n


def decide_wish(cur, wid, v, note, dry):
    """Jordan's reply under a wish post -> the line to confirm in the thread. yes approves (or says it already
    is), no declines it and cancels a not-yet-started build item, anything else is kept as his note."""
    cur.execute("SELECT status FROM feature_wishes WHERE id=%s", (int(wid),))
    row = cur.fetchone()
    if not row:
        return f"Wish #{wid} not found."
    status = row[0]
    cur.execute("SELECT id, status FROM claude_queue WHERE description LIKE %s AND status <> 'cancelled' "
                "ORDER BY id DESC LIMIT 1", (f"Build Nova's wish #{wid}:%",))
    q = cur.fetchone()
    note_js = json.dumps({"jordan_reply": note[:300], "at": datetime.now().isoformat(timespec="seconds")})
    if dry:
        return f"(dry) wish #{wid} {status}: {v or 'note'}"
    cur.execute("UPDATE feature_wishes SET lineage=coalesce(lineage,'{}'::jsonb) || %s::jsonb WHERE id=%s",
                (note_js, int(wid)))
    if v == "yes":
        if status == "wished":
            import nova_aspirations
            qid = nova_aspirations.approve_wish(cur, int(wid), by="jordan (Slack reply)")
            return f"Approved wish #{wid}." + (f" Build queued as #{qid}." if qid else "")
        return f"Wish #{wid} is already {status}" + (f"; its build is claude_queue #{q[0]} ({q[1]})." if q else ".")
    if v == "no":
        if status in ("shipped",):
            return f"Wish #{wid} already shipped; nothing declined. Noted."
        cur.execute("UPDATE feature_wishes SET status='declined' WHERE id=%s", (int(wid),))
        if q and q[1] in ("pending", "queued"):
            cur.execute("UPDATE claude_queue SET status='cancelled', outcome='declined by Jordan in Slack', "
                        "updated_at=now() WHERE id=%s", (q[0],))
        return f"Declined wish #{wid}." + (" Its build item was cancelled." if q and q[1] in ("pending", "queued") else "")
    return f"Noted on wish #{wid}."


def decide_proposal(pid, v, note, dry):
    mode = "approve" if v == "yes" else "reject"
    if dry:
        print(f"proposal #{pid} -> {mode} ({note!r})"); return True
    argv = [sys.executable, str(SCRIPTS / "nova_coagency.py"), "--mode", mode, "--id", str(pid),
            "--by", "jordan (slack reply)", "--note", note[:200]]
    # Spawn failures retry with backoff; a non-zero rc is a real answer (e.g. already decided)
    # and a timeout may already have applied the decision — neither is retried.
    for attempt in range(3):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            break
        except OSError as e:
            if attempt == 2:
                log(f"coagency {mode} #{pid}: could not run ({e})"); return False
            time.sleep(1.5 * (attempt + 1))
    log(f"coagency {mode} #{pid}: rc={r.returncode} {(r.stdout or r.stderr)[-120:].strip()}")
    return r.returncode == 0


def _annie_ok(text):
    try:
        import nova_annie_rule
        return nova_annie_rule.ok(text)   # manipulation_check + absence guilt + nudge consent
    except Exception:  # noqa: BLE001
        return True


def _turning_point(cur, text, backlog):
    try:
        import nova_turning_point
        return nova_turning_point.decide(cur, "proposal", stakes=min(1.0, 0.65 + 0.05 * backlog),
                                         text=text, ceiling="recommend")
    except Exception as e:  # noqa: BLE001
        return {"allowed": True, "reason": f"turning point unavailable ({e})"}


ASK_JORDAN_PROPOSALS = False   # 2026-10-15 (Jordan: "Her questions go back to her ... She shouldn't wait for
                               # me to answer"): pending proposals are decided on Nova's own path
                               # (nova_coagency.mode_dispose + nova_claude_reviewer as her second key; red lines
                               # -> claude_queue 'red_line_decision'). Nothing is put to him here any more; his
                               # replies to older prompts are still harvested below.


def post_pending_proposals(cur, dry):
    if not ASK_JORDAN_PROPOSALS:
        return
    if datetime.now().hour not in POST_HOURS:
        return
    if _QUIET if _QUIET is not None else quiet_active(cur):
        log("quiet mode (hard stretch) — holding proposal prompts"); return
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
    global _QUIET
    _QUIET = quiet_active(cur)
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
        elif kind == "conflict":    # wish #58: any reply in the thread IS the decision (older posts; Nova no longer asks)
            if not dry:
                ok_organ = False
                try:   # the directive organ's decide() closes an OPEN row (nova_directive_conflict, 2026-10-15)
                    import nova_directive_conflict as _dc
                    ok_organ = bool(_dc.decide(conflict_id=int(ref), decision=answer[:600], decided_by="jordan",
                                               reason="his reply in the Slack thread", cur=cur))
                except Exception as e:  # noqa: BLE001
                    log(f"directive organ decide() unavailable ({e}) — recording directly")
                if not ok_organ:   # already decided (by Nova) or organ unavailable: his word overrides hers
                    cur.execute("UPDATE directive_conflicts SET status='decided', decided_by='jordan', decided_at=now(), "
                                "decision_note=%s WHERE id=%s AND status IN ('open','decided')", (answer[:600], int(ref)))
            ok = True
            confirm(ch, ts, f"Recorded your decision on directive conflict #{ref}. Thank you.", dry)
        elif kind == "wish":        # wish announcements (nova_aspirations._post_wish): his reply decides it
            confirm(ch, ts, decide_wish(cur, ref, v, answer, dry), dry)
            ok = True
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
