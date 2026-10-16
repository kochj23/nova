#!/usr/bin/env python3
"""
nova_notify_jordan.py — skill 'notify-jordan-of-system-observations' (co-agency #118, 2026-10-02).

Nova's own numbers: she held back 64% of what she wanted to tell Jordan, and 66% of his sessions
fall in the 10:00–12:59 band — outside it "a message from me is an interruption, not a
conversation". So her direct reaches to him are no longer posted the moment she has them:
nova_reach holds them outside the window (reach_log status 'held'), and this delivers the drawer
as ONE bundled #nova-chat post inside the window, once a day. Empty drawer -> no post.

  nova_notify_jordan.py            # deliver (legacy 10:15 drawer; superseded by --daily)
  nova_notify_jordan.py --dry-run  # print the bundle, send nothing, mark nothing

2026-10-15 — Nova is her own customer (Jordan: "Nova's organs are for Nova. She can feed me some
information about them but, ultimately, Nova is the primary customer. I just need to be informed.").
He is informed by ONE daily summary at 18:00 plus immediate messages ONLY for real impact:

  nova_notify_jordan.py --daily            # the 18:00 summary: what she decided / did / fixed / retired today,
                                           # what went to Claude as a red line, the held drawer, and at most
                                           # ONE optional question about HIM (his preferences — never a decision)
  nova_notify_jordan.py --daily --dry-run  # print it, send nothing, mark nothing
  is_real_impact(text, level, category, tier) -> (bool, why)   # the bar for an immediate message

Sources are read tolerantly (a missing table/column drops that line, never the summary): autonomy_ledger,
outbound_ledger, coagency_proposals, claude_reviewer_log, claude_queue, directive_conflicts, problems,
postmortems, letting_go_log, herd_correspondents, flop_slate, reach_log (the held drawer). One row per day
in nova_daily_summaries makes it idempotent: a failed post is retried by the next run that day.
"""
import argparse
import re
import sys
import time
from datetime import date, datetime

import psycopg2

import nova_config
from nova_reach import DIRECT_AUDIENCES, OPS_DSN

try:  # proactivity dial — exactly 6 at the default
    from nova_voice import dial_scale as _dial_scale
    MAX_ITEMS = max(1, int(round(_dial_scale("proactivity", 2, 6, 10))))
except Exception:  # pragma: no cover
    MAX_ITEMS = 6
MAX_AGE_DAYS = 7
QUIET_MAX_ITEMS = 1   # quiet mode: the bundle shrinks to the single oldest item; the rest stay held


def log(m): print(f"[notify-jordan {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def bundle(rows, quiet=False):
    if quiet:   # lower verbosity during a hard stretch: no header, no timestamps
        return "\n".join(f"• {msg.strip()}" for _, _, _, msg in rows)
    lines = [f"*Things I noticed* ({len(rows)}):"]   # Annie Wilkes rule: no "since we last talked"
    for _, ts, topic, msg in rows:
        lines.append(f"• _{ts.strftime('%a %H:%M')}_ {('[' + topic + '] ') if topic else ''}{msg.strip()}")
    return "\n".join(lines)


def quiet_active(cur=None) -> bool:
    """nova_relationship.quiet_mode(): during a hard stretch, hold non-urgent nags and say less.
    Fails open to 'not quiet' (quiet_mode itself never raises; a missing module reads inactive)."""
    try:
        import nova_relationship
        return bool(nova_relationship.quiet_mode(cur).get("active"))
    except Exception:  # noqa: BLE001
        return False


def _annie_ok(text):
    try:
        import nova_annie_rule
        return nova_annie_rule.ok(text)   # manipulation_check + absence guilt + nudge consent
    except Exception:  # noqa: BLE001
        return True


def turning_point(oc, rows, text):
    try:
        import nova_turning_point
        stakes = min(1.0, 0.35 + 0.1 * len(rows))
        return nova_turning_point.decide(oc, "notify-bundle", stakes=stakes, text=text, ceiling="mention")
    except Exception as e:  # noqa: BLE001
        return {"allowed": True, "reason": f"turning point unavailable ({e})"}


# ── immediate messages: only for real impact ───────────────────────────────────────────────────
_TIER0_RX = re.compile(r"\btier.?0\b.{0,60}\b(down|outage|unreachable|failed|failing|offline)\b"
                       r"|\b(down|outage|unreachable|offline)\b.{0,60}\btier.?0\b", re.I)
_HOUSE_RX = re.compile(r"\b(smoke|carbon monoxide|co alarm|fire alarm|fire|water leak|leak detected|flood(ing)?"
                       r"|intrusion|intruder|break.?in|forced entry|glass break|alarm (triggered|tripped|sounding)"
                       r"|door (left )?(open|unlocked)|garage (door )?(left )?open|front door unlocked"
                       r"|freez(e|ing) pipes?|no heat)\b", re.I)
_MONEY_RX = re.compile(r"\b(charged?|payment|purchase[ds]?|refund|bank|credit card|fraud\w*|invoice|billing"
                       r"|data loss|deleted|wiped|unrecoverable|irreversible|ransom\w*)\b", re.I)
IMPACT_CATEGORIES = {"tier0", "tier-0", "outage", "safety", "security", "house_safety", "money", "irreversible"}


def is_real_impact(text="", level=None, category=None, tier=None):
    """Jordan 2026-10-15: immediate messages ONLY for real impact to him or the house. -> (bool, why).
    Real impact = a Tier-0 outage, a house safety/security event, or money / something irreversible.
    Everything else waits for the 18:00 summary. Pure."""
    t = text or ""
    if tier is not None and str(tier).strip().lower() in ("0", "tier0", "tier-0", "t0"):
        return True, "tier-0 outage"
    if str(category or "").lower() in IMPACT_CATEGORIES and str(level or "").lower() in ("critical", "page", "high"):
        return True, f"{category} at {level}"
    if _TIER0_RX.search(t):
        return True, "tier-0 outage"
    if _HOUSE_RX.search(t):
        return True, "house safety/security"
    if _MONEY_RX.search(t):
        return True, "money/irreversible"
    return False, "not impact — waits for the 18:00 summary"


# ── the 18:00 daily summary ───────────────────────────────────────────────────────────────────
DAILY_TABLE = """CREATE TABLE IF NOT EXISTS nova_daily_summaries (
    day date PRIMARY KEY, built_at timestamptz NOT NULL DEFAULT now(), posted_at timestamptz,
    slack_ts text, text text, question_id int, error text)"""
SUMMARY_LINES = 4          # per section: terse
DONE_STATUSES = ("done", "completed")


def _q(oc, sql, params=()):
    """Tolerant read: rows, or [] when the table/column is missing (B is adding fields in parallel)."""
    try:
        oc.execute(sql, params)
        return oc.fetchall()
    except Exception as e:  # noqa: BLE001
        log(f"source skipped ({str(e).splitlines()[0][:120]})")
        try:
            oc.connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        return []


def _short(t, n=90):
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def gather_day(oc, day=None):
    """-> {section: [line, ...]} for the day (default today). Each source is optional."""
    day = day or date.today()
    p = (day,)
    out = {"decided": [], "did": [], "fixed": [], "retired": [], "claude": []}
    # decided: her own path (co-agency dispositions, directive conflicts, second-key reviews)
    rows = _q(oc, """SELECT status, count(*) FROM coagency_proposals
                     WHERE decided_at::date = %s AND (decided_by LIKE 'nova%%' OR decided_by LIKE 'claude%%')
                     GROUP BY 1 ORDER BY 2 DESC""", p)
    if rows:
        out["decided"].append("proposals: " + ", ".join(f"{n} {st}" for st, n in rows))
    rows = _q(oc, """SELECT count(*), min(left(decision_note, 80)) FROM directive_conflicts
                     WHERE decided_at::date = %s AND decided_by = 'nova'""", p)
    if rows and rows[0][0]:
        out["decided"].append(f"{rows[0][0]} directive conflict(s) resolved myself (e.g. {_short(rows[0][1], 70)})")
    rows = _q(oc, """SELECT decision, count(*) FROM claude_reviewer_log WHERE ts::date = %s AND NOT dry_run
                     AND source <> 'error' GROUP BY 1 ORDER BY 2 DESC""", p)
    if rows:
        out["decided"].append("second key (Claude): " + ", ".join(f"{n} {d.lower()}" for d, n in rows))
    # did: actions she took, what she sent, what Claude finished for her
    rows = _q(oc, """SELECT coalesce(action_class, source), count(*) FROM autonomy_ledger
                     WHERE ts::date = %s AND executed GROUP BY 1 ORDER BY 2 DESC LIMIT %s""", (day, SUMMARY_LINES))
    if rows:
        out["did"].append("acted: " + ", ".join(f"{n}× {c}" for c, n in rows))
    rows = _q(oc, """SELECT channel_kind, count(*) FROM outbound_ledger WHERE ts::date = %s
                     GROUP BY 1 ORDER BY 2 DESC""", p)
    if rows:
        out["did"].append("sent: " + ", ".join(f"{n} {k}" for k, n in rows))
    rows = _q(oc, """SELECT count(*), max(left(description, 90)) FROM claude_queue
                     WHERE completed_at::date = %s AND status = ANY(%s)""", (day, list(DONE_STATUSES)))
    if rows and rows[0][0]:
        out["did"].append(f"Claude closed {rows[0][0]} queue item(s), e.g. {_short(rows[0][1], 70)}")
    # fixed: problems closed, postmortems finalised
    rows = _q(oc, """SELECT title FROM problems WHERE state IN ('closed','resolved','fixed')
                     AND state_changed_at::date = %s ORDER BY state_changed_at DESC LIMIT %s""", (day, SUMMARY_LINES))
    out["fixed"] += [f"problem closed: {_short(r[0], 80)}" for r in rows]
    rows = _q(oc, """SELECT title FROM postmortems WHERE finalized_at::date = %s ORDER BY finalized_at DESC LIMIT %s""",
              (day, SUMMARY_LINES))
    out["fixed"] += [f"postmortem finalised: {_short(r[0], 80)}" for r in rows]
    rows = _q(oc, "SELECT state, count(*) FROM problems GROUP BY 1 ORDER BY 2 DESC")
    if rows:
        out["fixed"].append("problems: " + ", ".join(f"{r[1]} {r[0]}" for r in rows if len(r) >= 2))
    rows = _q(oc, """SELECT source, count(*), sum(count) FROM noise_rollup WHERE day = %s
                     GROUP BY 1 ORDER BY 3 DESC LIMIT %s""", (day, SUMMARY_LINES))
    if rows:
        out["did"].append("kept quiet (rolled up): " + ", ".join(f"{r[0]} {r[-1]}" for r in rows if len(r) >= 2))
    # retired
    rows = _q(oc, """SELECT kind, subject FROM letting_go_log WHERE ts::date = %s ORDER BY ts DESC LIMIT %s""",
              (day, SUMMARY_LINES))
    out["retired"] += [f"{k}: {_short(sub, 80)}" for k, sub in rows]
    rows = _q(oc, "SELECT name FROM herd_correspondents WHERE retired_at::date = %s", p)
    out["retired"] += [f"herd correspondent: {r[0]}" for r in rows]
    rows = _q(oc, "SELECT initiative FROM flop_slate WHERE class = 'retired' AND measured_at::date = %s LIMIT %s",
              (day, SUMMARY_LINES))
    out["retired"] += [f"organ: {_short(r[0], 80)}" for r in rows]
    # red lines she handed to Claude today (and how many are still open)
    rows = _q(oc, """SELECT count(*) FILTER (WHERE created_at::date = %s),
                            count(*) FILTER (WHERE status NOT IN ('done','completed','cancelled'))
                     FROM claude_queue WHERE description LIKE %s""", (day, "[red_line_decision]%"))
    if rows and (rows[0][0] or rows[0][1]):
        out["claude"].append(f"{rows[0][0] or 0} new red-line decision(s) for Claude today; {rows[0][1] or 0} open")
    return out


def compose_daily(day, sections, drawer=(), question=None):
    """Pure: the terse 18:00 text. Empty sections are left out. None when there is nothing at all."""
    names = (("decided", "Decided"), ("did", "Did"), ("fixed", "Fixed"), ("retired", "Retired"),
             ("claude", "Red lines → Claude"))
    lines = []
    for key, label in names:
        items = [i for i in sections.get(key, []) if i][:SUMMARY_LINES]
        if items:
            lines.append(f"*{label}:* " + "; ".join(items))
    if drawer:
        lines.append(f"*Noticed* ({len(drawer)}): " + " · ".join(_short(m, 140) for _, _, _, m in drawer))
    if not lines and not question:
        return None
    head = f"Nova — {day.strftime('%a %b %-d')}, FYI (no action needed):"
    text = "\n".join([head] + lines)
    if question:
        text += f"\n_One question about you, only if you feel like it (Q#{question[0]}):_ {question[1].strip()} — reply in this thread."
    return text


def _post_with_ts(text):
    """chat.postMessage (need the ts so a reply in the thread answers the question); falls back to post_both."""
    try:
        from nova_slack_answers import slack
        r = slack("chat.postMessage", channel=nova_config.SLACK_CHAN, text=text)
        if r.get("ok"):
            return True, r.get("ts")
    except Exception as e:  # noqa: BLE001
        log(f"chat.postMessage failed ({e}) — falling back to post_both")
    return bool(nova_config.post_both(text, slack_channel=nova_config.SLACK_CHAN)), None


def daily(oc, dry=False, day=None, post=None):
    """Build and (unless dry) post today's summary once. Returns 0 on success/no-op, 1 when the post failed."""
    day = day or date.today()
    if not dry:
        try:
            import nova_pg_schema
            nova_pg_schema.safe_ddl(oc, DAILY_TABLE)
        except Exception as e:  # noqa: BLE001
            log(f"summary table ensure failed ({e})")
        rows = _q(oc, "SELECT posted_at FROM nova_daily_summaries WHERE day = %s", (day,))
        if rows and rows[0][0]:
            log(f"summary for {day} already posted"); return 0
    sections = gather_day(oc, day)
    drawer = _q(oc, "SELECT id, ts, coalesce(topic,''), message FROM reach_log WHERE lower(audience) = ANY(%s) "
                    "AND status='held' AND ts > now() - interval '%s days' ORDER BY ts LIMIT %s",
                (list(DIRECT_AUDIENCES), MAX_AGE_DAYS, MAX_ITEMS))
    drawer = [r for r in drawer if _annie_ok(r[3])]
    if quiet_active(oc) and len(drawer) > QUIET_MAX_ITEMS:
        drawer = drawer[:QUIET_MAX_ITEMS]
    question = None
    try:
        import nova_ask_one
        question = nova_ask_one.question_for_summary(oc)
    except Exception as e:  # noqa: BLE001
        log(f"no question today ({e})")
    text = compose_daily(day, sections, drawer, question)
    if not text:
        log("nothing to report today — no summary"); return 0
    if dry:
        print(text); return 0
    ok, ts = (post or _post_with_ts)(text)
    _q_write(oc, """INSERT INTO nova_daily_summaries (day, text, posted_at, slack_ts, question_id, error)
                    VALUES (%s, %s, CASE WHEN %s THEN now() END, %s, %s, %s)
                    ON CONFLICT (day) DO UPDATE SET text=EXCLUDED.text, posted_at=EXCLUDED.posted_at,
                      slack_ts=EXCLUDED.slack_ts, question_id=EXCLUDED.question_id, error=EXCLUDED.error,
                      built_at=now()""",
             (day, text, ok, ts, question[0] if (question and ok and ts) else None, None if ok else "post failed"))
    if not ok:
        log("summary post failed — the next run today retries"); return 1
    if drawer:
        _q_write(oc, "UPDATE reach_log SET status='sent' WHERE id = ANY(%s)", ([r[0] for r in drawer],))
    if question and ts:
        _q_write(oc, "INSERT INTO slack_prompts (kind, ref_id, channel, ts) VALUES ('question', %s, %s, %s) "
                     "ON CONFLICT (kind, ref_id) DO NOTHING", (str(question[0]), nova_config.SLACK_CHAN, ts))
    log(f"daily summary posted ({len(text)} chars, drawer {len(drawer)}, question {question[0] if question else '-'})")
    return 0


def _q_write(oc, sql, params):
    try:
        oc.execute(sql, params)
    except Exception as e:  # noqa: BLE001
        log(f"write skipped ({str(e).splitlines()[0][:120]})")


def _connect():
    for attempt in range(3):   # house retry rule: PG gets 3 tries with backoff
        try:
            return psycopg2.connect(OPS_DSN, connect_timeout=5)
        except psycopg2.OperationalError as e:
            if attempt == 2:
                raise
            log(f"pg connect failed ({e}) — retry {attempt + 1}/2")
            time.sleep(2 * (attempt + 1))


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--daily", action="store_true", help="the 18:00 daily summary (2026-10-15)")
    args = ap.parse_args()
    if args.daily:
        conn = _connect(); conn.autocommit = True
        try:
            return daily(conn.cursor(), dry=args.dry_run)
        finally:
            conn.close()
    for attempt in range(3):   # house retry rule: PG gets 3 tries with backoff
        try:
            conn = psycopg2.connect(OPS_DSN, connect_timeout=5)
            break
        except psycopg2.OperationalError as e:
            if attempt == 2:
                raise
            log(f"pg connect failed ({e}) — retry {attempt + 1}/2")
            time.sleep(2 * (attempt + 1))
    conn.autocommit = True
    oc = conn.cursor()
    oc.execute("SELECT id, ts, coalesce(topic,''), message FROM reach_log WHERE lower(audience) = ANY(%s) "
               "AND status='held' AND ts > now() - interval '%s days' ORDER BY ts LIMIT %s",
               (list(DIRECT_AUDIENCES), MAX_AGE_DAYS, MAX_ITEMS))
    rows = oc.fetchall()
    # Annie Wilkes rule: an item that guilt-trips him for silence/absence never goes out.
    bad = [r for r in rows if not _annie_ok(r[3])]
    rows = [r for r in rows if r not in bad]
    if bad and not args.dry_run:
        oc.execute("UPDATE reach_log SET status='dropped' WHERE id = ANY(%s)", ([r[0] for r in bad],))
        log(f"dropped {len(bad)} held reach(es) that failed the Annie Wilkes rule")
    if not rows:
        log("drawer empty — nothing to deliver"); return 0
    quiet = quiet_active(oc)
    if quiet and len(rows) > QUIET_MAX_ITEMS:
        log(f"quiet mode — delivering {QUIET_MAX_ITEMS} of {len(rows)}; the rest stay held")
        rows = rows[:QUIET_MAX_ITEMS]
    text = bundle(rows, quiet=quiet)
    if args.dry_run:
        print(text); return 0
    # Turning point: the bundle is one mention; stakes grow with how much is in the drawer.
    tp = turning_point(oc, rows, text)
    if not tp["allowed"]:
        log(f"holding the drawer — turning point: {tp['reason']}"); return 0
    if not nova_config.post_both(text, slack_channel=nova_config.SLACK_CHAN):
        # nothing delivered — leave the drawer 'held' so the next run retries
        log("post failed — drawer left held for the next run"); return 1
    oc.execute("UPDATE reach_log SET status='sent' WHERE id = ANY(%s)", ([r[0] for r in rows],))
    log(f"delivered {len(rows)} held reach(es) to #nova-chat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
