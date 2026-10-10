#!/usr/bin/env python3
"""
nova_ask_one.py — one answerable question a day (six-month build #4, 2026-09-28).

Nova has asked Jordan 44 curiosity questions and he has answered 3 — not because he would
not, but because they were posted three at a time to a channel the gateway ignores, with
no ids, and nothing ever read the replies. Meanwhile her predictions organ kept betting on
his private states and losing every time. Replace the bets with a loop that closes:

  once a day, pick ONE unanswered question that is actually about him or the house, post
  it to #nova-chat as its own message, remember the message ts in slack_prompts, and let
  nova_slack_answers.py harvest the thread reply (or reaction, once the app has the scope).

  nova_ask_one.py            # post today's question (no-op if one is still open)
  nova_ask_one.py --dry-run  # print the question it would post
  nova_ask_one.py --selftest # pure-logic assertions
"""
import json
import re
import sys
import urllib.request
from datetime import datetime

import psycopg2

import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
CHANNEL = "C0AMNQ5GX70"           # #nova-chat — the channel he actually reads and the gateway listens to
OPEN_MAX_DAYS = 3                 # an unanswered question expires from the slot after this long
ABOUT_HIM_RE = re.compile(r"\bjordan\b|\byou\b|\byour\b|printer|house|home|garage|master bedroom|"
                          r"zigbee|camera|halloween|calendar|weekend", re.I)
SKIP_SOURCES = ("prediction_surprise",)   # "what did I misjudge?" is a question for her, not him


def log(m):
    print(f"[ask-one {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def ensure_schema(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS slack_prompts (
        id bigserial PRIMARY KEY, kind text NOT NULL, ref_id text NOT NULL,
        channel text NOT NULL, ts text NOT NULL, posted_at timestamptz NOT NULL DEFAULT now(),
        resolved_at timestamptz, result text, UNIQUE (kind, ref_id))""")


def pick(rows):
    """rows: (id, question, memory_source, asked_at) newest first. Prefer questions about him
    or the house; otherwise the newest. Skip self-directed sources. Pure."""
    live = [r for r in rows if (r[2] or "") not in SKIP_SOURCES and (r[1] or "").strip()]
    if not live:
        return None
    for r in live:
        if ABOUT_HIM_RE.search(r[1]):
            return r
    return live[0]


def compose(qid, question):
    return (f"One question, no rush (Q#{qid}): {question.strip()}\n"
            f"_Reply in this thread — a word is enough — and I'll remember the answer._")


def quiet_active(cur=None) -> bool:
    """nova_relationship.quiet_mode(): during a hard stretch, hold non-urgent nags and say less.
    Fails open to 'not quiet' (quiet_mode itself never raises; a missing module reads inactive)."""
    try:
        import nova_relationship
        return bool(nova_relationship.quiet_mode(cur).get("active"))
    except Exception:  # noqa: BLE001
        return False


def slack_post(text, attempts=3, base=2.0):
    """chat.postMessage with retry on transport errors (connection/5xx/429). A Slack-level
    {ok:false} is a real answer and is raised immediately, never retried."""
    import time
    import urllib.error
    last = None
    for i in range(attempts):
        try:
            return _slack_post_once(text)
        except urllib.error.HTTPError as e:
            last = e
            if not (e.code == 429 or e.code >= 500):
                raise
        except (urllib.error.URLError, ConnectionError) as e:
            # a read timeout may mean the post landed — never re-send (no double question)
            if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
                raise
            last = e
        if i < attempts - 1:
            log(f"slack post failed ({last}) — retry {i + 1}/{attempts - 1}")
            time.sleep(base * (2 ** i))
    raise last


def _slack_post_once(text):
    import subprocess
    tok = subprocess.check_output(["security", "find-generic-password", "-a", "nova",
                                   "-s", "nova-slack-bot-token", "-w"]).decode().strip() \
        if sys.platform == "darwin" else __import__("nova_secrets").get_secret("nova-slack-bot-token")
    req = urllib.request.Request("https://slack.com/api/chat.postMessage", method="POST",
                                 headers={"Authorization": f"Bearer {tok}",
                                          "Content-Type": "application/json; charset=utf-8"},
                                 data=json.dumps({"channel": CHANNEL, "text": text}).encode())
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    if not d.get("ok"):
        raise RuntimeError(d.get("error", "slack error"))
    return d["ts"]


def _pg_connect(attempts=3, base=2.0):
    import time
    for i in range(attempts):
        try:
            return psycopg2.connect(OPS_DSN, connect_timeout=5)
        except psycopg2.OperationalError as e:
            if i == attempts - 1:
                raise
            log(f"pg connect failed ({e}) — retry {i + 1}/{attempts - 1}")
            time.sleep(base * (i + 1))


def main():
    dry = "--dry-run" in sys.argv
    conn = _pg_connect(); conn.autocommit = True; cur = conn.cursor()
    ensure_schema(cur)
    cur.execute("SELECT count(*) FROM slack_prompts WHERE kind='question' AND resolved_at IS NULL "
                "AND posted_at > now() - (%s || ' days')::interval", (str(OPEN_MAX_DAYS),))
    if cur.fetchone()[0]:
        log("a question is still open — not stacking another"); return 0
    if quiet_active(cur):
        log("quiet mode (hard stretch) — skipping today's question"); return 0
    cur.execute("SELECT id, question, memory_source, asked_at FROM reflection_questions "
                "WHERE answer IS NULL AND id NOT IN (SELECT ref_id::int FROM slack_prompts WHERE kind='question') "
                "ORDER BY asked_at DESC LIMIT 40")
    row = pick(cur.fetchall())
    if not row:
        log("nothing to ask"); return 0
    qid, question = row[0], row[1]
    text = compose(qid, question)
    if dry:
        print(text); return 0
    ts = slack_post(text)
    cur.execute("INSERT INTO slack_prompts (kind, ref_id, channel, ts) VALUES ('question', %s, %s, %s) "
                "ON CONFLICT (kind, ref_id) DO NOTHING", (str(qid), CHANNEL, ts))
    log(f"asked Q#{qid} (ts {ts})")
    return 0


def demo():
    rows = [(5, "What was the reason the printers went offline?", "bambu", None),
            (4, "I predicted with 66% confidence that ... What did I misjudge?", "prediction_surprise", None),
            (3, "What were the motivations behind the founding of colonies?", "american_indian_wars", None)]
    assert pick(rows)[0] == 5                     # about the house beats older trivia
    assert pick(rows[1:])[0] == 3                 # self-directed source skipped, newest trivia wins
    assert pick([rows[1]]) is None
    t = compose(5, rows[0][1]); assert "Q#5" in t and "thread" in t
    print("all ask-one assertions passed")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        demo()
    else:
        sys.exit(main())
