#!/usr/bin/env python3
"""nova_directive_decide.py — wish #58: decide the directive conflicts, with history.

The detector (nova_directive_conflict.py) files rows in directive_conflicts; this closes them so the 05:50
latent pass has a decided-by/decided-at/decision_note trail instead of Nova resolving the same pair silently.
  --list                      open conflicts
  --decide ID --note "..."    [--by claude|jordan] close one row (status 'decided')
  --post                      put up to POST_PER_DAY open rows to #nova-chat as threads; a reply in the thread
                              (any text: 'A', 'B', 'both apply', or a sentence) is the decision — recorded by
                              nova_slack_answers.py (kind='conflict'), decided_by='jordan'.
  --selftest
Scheduled on scheduler-core daily 09:10 with --post. Approved 2026-10-04.
"""
import nova_dsn as _nova_dsn  # noqa: E402
import sys, os, re
from datetime import datetime
import psycopg2

OPS = os.environ.get("NOVA_OPS_DSN", _nova_dsn.pg_dsn("nova_ops"))
CHANNEL = "C0AMNQ5GX70"          # #nova-chat (same as proposals/questions)
POST_PER_DAY = 2
POST_HOURS = range(9, 18)

def log(m): print(f"[directive-decide] {m}", flush=True)

def short(s, n=220):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"

def prompt_text(row):
    """Pure: one conflict row -> the Slack text Jordan sees."""
    cid, kind, a, sa, b, sb, situation = row
    return (f"Directive conflict #{cid} ({kind}):\n"
            f"*A* — {short(a)}  _({sa})_\n"
            f"*B* — {short(b)}  _({sb})_\n"
            f"_When:_ {short(situation, 160) or '—'}\n"
            f"_Reply in this thread with how to read them together (or 'A' / 'B' / 'both apply'); that closes it._")

def decide(cur, cid, note, by):
    cur.execute("UPDATE directive_conflicts SET status='decided', decided_by=%s, decided_at=now(), decision_note=%s WHERE id=%s AND status='open'",
                (by, note, cid))
    return cur.rowcount == 1

def post_pending(cur, dry):
    if datetime.now().hour not in POST_HOURS and not dry:
        return 0
    cur.execute("SELECT count(*) FROM slack_prompts WHERE kind='conflict' AND posted_at > now() - interval '24 hours'")
    room = POST_PER_DAY - cur.fetchone()[0]
    if room <= 0:
        return 0
    cur.execute("""SELECT id, kind, directive_a, source_a, directive_b, source_b, situation FROM directive_conflicts
                   WHERE status='open' AND id::text NOT IN (SELECT ref_id FROM slack_prompts WHERE kind='conflict')
                   ORDER BY CASE kind WHEN 'live' THEN 0 ELSE 1 END, id LIMIT %s""", (room,))
    n = 0
    for row in cur.fetchall():
        text = prompt_text(row)
        if dry:
            print(text); n += 1; continue
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from nova_slack_answers import slack
        d = slack("chat.postMessage", channel=CHANNEL, text=text)
        if d.get("ok"):
            cur.execute("INSERT INTO slack_prompts (kind, ref_id, channel, ts) VALUES ('conflict', %s, %s, %s) ON CONFLICT DO NOTHING",
                        (str(row[0]), CHANNEL, d["ts"]))
            n += 1; log(f"put conflict #{row[0]} to him")
    return n

def main():
    a = sys.argv[1:]
    conn = psycopg2.connect(OPS, connect_timeout=8); conn.autocommit = True; cur = conn.cursor()
    if "--list" in a:
        cur.execute("SELECT id, kind, severity, left(directive_a, 90), left(directive_b, 90) FROM directive_conflicts WHERE status='open' ORDER BY id")
        for r in cur.fetchall(): print(" | ".join(str(x) for x in r))
    elif "--decide" in a:
        cid = int(a[a.index("--decide") + 1]); note = a[a.index("--note") + 1]
        by = a[a.index("--by") + 1] if "--by" in a else "claude"
        log(f"#{cid} {'decided' if decide(cur, cid, note, by) else 'not open'}")
    elif "--post" in a:
        log(f"posted {post_pending(cur, '--dry-run' in a)}")
    else:
        print(__doc__)

def selftest():
    t = prompt_text((21, "latent", "When Jordan says 'do the needful' " + "x" * 400, "feedback:needful", "NEVER use ComfyUI", "feedback:no-comfyui", "use ComfyUI"))
    assert t.startswith("Directive conflict #21 (latent)") and "…" in t and "*B* — NEVER use ComfyUI" in t and len(t) < 800
    assert short("  a   b  ") == "a b" and short(None) == ""
    print("selftest ok")

if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
