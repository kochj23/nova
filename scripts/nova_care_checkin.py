#!/opt/homebrew/bin/python3
"""
nova_care_checkin.py — Nova's weekly self-assessment (was the Baymax "are you satisfied with your care?").

Little Mister, 2026-10-15: "Nova's organs are for Nova ... Her questions go back to her ... She shouldn't
wait for me to answer." So this no longer posts a question to him. Each week Nova scores herself on RATER
(Reliability, Assurance, Tangibles, Empathy, Responsiveness, 1-10) from her own tables (nova_rater.py) and
writes one row per week to nova_ops.nova_care_checkins, with the indicator evidence and the concrete things
she did that week (alerts sent, articles, reach-outs). Jordan is informed through the daily summary.

His own five numbers are optional: if he volunteers "R6 A5 T4 E6 S3" in #nova-chat, the harvest pass
captures it into that week's row (jordan_scores). Nothing waits for it.

One-shot failure fixed (2026-10-11: the only weekly run died on ModuleNotFoundError psycopg2 under the
scheduler python, and no row was written): every run Sun-Wed assesses until this week's row exists; a
failed assessment still writes the row with its error (re-tried next run); if psycopg2 cannot import the
script re-execs under a pinned interpreter (NOVA_PYTHON, else /opt/homebrew/bin/python3) and, as a last
resort, records the failure through the psql CLI.

  nova_care_checkin.py              harvest (old thread replies + volunteered RATER numbers), then assess if due
  nova_care_checkin.py --ask        assess now if this week's row is missing (kept name; posts nothing)
  nova_care_checkin.py --dry-run    print, write nothing
  nova_care_checkin.py --selftest

Owner: Nova. Written by Jordan Koch.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
import nova_dsn as _nova_dsn  # noqa: E402
MEM_DSN = _nova_dsn.pg_dsn("nova_memories")
MEMSRV = "http://memory-server.digitalnoise.net:18790"
CHANNEL = "C0AMNQ5GX70"   # #nova-chat (same as nova_config.SLACK_CHAN / nova_slack_answers.CHANNEL)
MAX_ITEMS = 4

SCHEMA = """CREATE TABLE IF NOT EXISTS nova_care_checkins (
    id bigserial PRIMARY KEY,
    week date NOT NULL UNIQUE,
    items jsonb NOT NULL,
    message text,
    asked_at timestamptz,
    slack_channel text,
    slack_ts text,
    reply text,
    reply_at timestamptz,
    verdicts jsonb,
    memory_id text)"""
# 2026-10-15 self-assessment columns (added lock-safely by nova_pg_schema.safe_ddl)
SCHEMA_SELF = """ALTER TABLE nova_care_checkins
    ADD COLUMN IF NOT EXISTS scores jsonb,
    ADD COLUMN IF NOT EXISTS indicators jsonb,
    ADD COLUMN IF NOT EXISTS assessed_at timestamptz,
    ADD COLUMN IF NOT EXISTS jordan_scores jsonb,
    ADD COLUMN IF NOT EXISTS jordan_scores_at timestamptz,
    ADD COLUMN IF NOT EXISTS error text"""
PINNED_PYTHON = "/opt/homebrew/bin/python3"
ASSESS_WEEKDAYS = (6, 0, 1, 2)          # Sun, Mon, Tue, Wed (date.weekday())
VOLUNTEER_LOOKBACK_DAYS = 7

_VERDICT_WORDS = {"yes": "helped", "y": "helped", "helped": "helped", "good": "helped", "+": "helped",
                  "no": "noise", "n": "noise", "noise": "noise", "useless": "noise", "-": "noise",
                  "meh": "meh", "m": "meh", "eh": "meh"}
_REPLY_RE = re.compile(r"(\d)\s*[:.)=-]?\s*(yes|y|no|n|helped|good|noise|useless|meh|eh|m|\+|-)(?![a-z])", re.I)


def log(m):
    print(f"[care-checkin {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def week_of(d=None) -> date:
    """The Sunday that closes the week containing d (Sunday itself maps to itself)."""
    d = d or date.today()
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _clean(t, n=90):
    t = re.sub(r"^[^\w\"'(\[]+", "", (t or "").strip())          # drop leading emoji/symbols
    t = re.sub(r"\s+", " ", t)
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


# ── gathering (all parameterized, read-only) ─────────────────────────────────

def gather(ops_cur, mem_cur):
    items = []
    ops_cur.execute("""SELECT title, level, count(*) FROM telemetry.events
                       WHERE ts > now() - interval '7 days' AND status = 'sent' AND level IN ('critical','warning')
                       GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 2""")
    for title, level, n in ops_cur.fetchall():
        items.append({"kind": "alert", "text": f"{level} alert \"{_clean(title)}\" (sent {n}x)"})
    try:
        mem_cur.execute("""SELECT metadata->>'section', metadata->>'title' FROM memories
                           WHERE source = %s AND created_at > now() - interval '7 days'
                             AND metadata->>'section' IS NOT NULL AND metadata->>'title' IS NOT NULL
                           ORDER BY created_at DESC""", ("nova_articles",))
        arts = mem_cur.fetchall()
    except Exception:  # noqa: BLE001
        arts = []
    if arts:
        by = {}
        for sec, _ in arts:
            by[sec or "other"] = by.get(sec or "other", 0) + 1
        top = ", ".join(f"{s} {c}" for s, c in sorted(by.items(), key=lambda x: -x[1])[:3])
        items.append({"kind": "articles", "text": f"{len(arts)} articles published ({top}), "
                                                  f"latest \"{_clean(arts[0][1], 70)}\""})
    ops_cur.execute("""SELECT topic, message FROM reach_log WHERE lower(audience) = 'jordan'
                       AND status = 'sent' AND ts > now() - interval '7 days' ORDER BY ts DESC LIMIT 1""")
    r = ops_cur.fetchone()
    if r:
        items.append({"kind": "reach", "text": f"reach-out{(' on ' + r[0]) if r[0] else ''}: \"{_clean(r[1], 80)}\""})
    return items[:MAX_ITEMS]


def parse_reply(text, n_items):
    """-> {item_index(str): 'helped'|'noise'|'meh'}; a bare yes/no applies to every item."""
    out = {}
    for num, word in _REPLY_RE.findall(text or ""):
        if 1 <= int(num) <= n_items:
            out[num] = _VERDICT_WORDS[word.lower()]
    if not out:
        m = re.match(r"^\s*(yes|no|meh|helped|noise)\b", text or "", re.I)
        if m:
            out = {str(i): _VERDICT_WORDS[m.group(1).lower()] for i in range(1, n_items + 1)}
    return out


def summarize(week, items, reply, verdicts) -> str:
    parts = [f"{it['text']} -> {verdicts.get(str(i), 'no verdict')}" for i, it in enumerate(items, 1)]
    return (f"Jordan's weekly care check-in feedback (week ending {week}): " + "; ".join(parts)
            + f". His words: \"{reply.strip()[:500]}\"")


def _retry(fn, attempts=3, base=1.0, what="call"):
    """Run fn() up to `attempts` times with exponential backoff; re-raises the last error."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if i == attempts - 1:
                raise
            log(f"{what} attempt {i + 1} failed ({e}); retrying")
            time.sleep(base * 2 ** i)


def remember(text, metadata):
    """Store to vector memory (3 tries, backoff); returns id or None (never raises)."""
    body = json.dumps({"text": text, "source": "jordan_feedback", "metadata": metadata}).encode()

    def _go():
        req = urllib.request.Request(MEMSRV + "/remember", method="POST", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("id")
    try:
        return _retry(_go, what="remember")
    except Exception as e:  # noqa: BLE001
        log(f"remember failed: {e}")
        return None


# ── runs ─────────────────────────────────────────────────────────────────────

def _pinned_python():
    return os.environ.get("NOVA_PYTHON") or PINNED_PYTHON


def ensure_driver(argv=None, _exec=os.execv, _record=None) -> bool:
    """True when psycopg2 imports. Otherwise re-exec once under the pinned interpreter; if that is not
    possible, record the failure for this week through the psql CLI and return False."""
    try:
        import psycopg2  # noqa: F401
        return True
    except ImportError as e:
        err = f"psycopg2 unavailable under {sys.executable}: {e}"
    py = _pinned_python()
    if os.environ.get("NOVA_CARE_REEXEC") != "1" and os.path.realpath(py) != os.path.realpath(sys.executable) \
            and os.access(py, os.X_OK):
        log(f"{err} — re-exec under {py}")
        os.environ["NOVA_CARE_REEXEC"] = "1"
        _exec(py, [py, os.path.abspath(__file__), *(sys.argv[1:] if argv is None else argv)])
    log(err)
    (_record or record_error_psql)(week_of(), err)
    return False


def record_error_psql(week, err) -> bool:
    """Last resort when no python driver works: write this week's row with the error via psql.
    Values travel as psql variables (:'w' / :'e' are quoted by psql), never spliced into SQL."""
    psql = shutil.which("psql") or "/opt/homebrew/bin/psql"
    sql = ("INSERT INTO nova_care_checkins (week, items, error) VALUES (:'w'::date, '[]'::jsonb, :'e') "
           "ON CONFLICT (week) DO UPDATE SET error = EXCLUDED.error WHERE nova_care_checkins.assessed_at IS NULL")
    try:
        r = subprocess.run([psql, OPS_DSN, "-v", "ON_ERROR_STOP=1", "-v", f"w={week}", "-v", f"e={err[:500]}"],
                           input=sql, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log(f"psql fallback failed: {(r.stderr or '')[-200:]}")
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001
        log(f"psql fallback failed: {e}")
        return False


def _connect(dsn):
    import psycopg2
    c = psycopg2.connect(dsn, connect_timeout=5)
    c.autocommit = True
    return c


def _schema(oc):
    oc.execute(SCHEMA)
    try:
        from nova_pg_schema import safe_ddl
        safe_ddl(oc, SCHEMA_SELF)
    except ImportError:
        oc.execute(SCHEMA_SELF)


def due(today, have_assessed: bool, force=False) -> bool:
    """Assess on any run Sun-Wed until this week's row is assessed. Pure."""
    return force or (today.weekday() in ASSESS_WEEKDAYS and not have_assessed)


def self_assess(oc, mem_cur, days=7) -> dict:
    """Nova's own numbers: RATER scores + evidence, plus what she did this week."""
    import nova_rater
    res = nova_rater.assess(oc, days)
    res["did"] = gather(oc, mem_cur)
    return res


def ask(dry=False, force=False, today=None):
    """Self-assessment (the old name is kept so the scheduler entry keeps working). Posts nothing."""
    today = today or date.today()
    week = week_of(today)
    try:
        ops = _connect(OPS_DSN)
    except Exception as e:  # noqa: BLE001 — PG down: try the CLI so the miss is on record
        log(f"ops db unavailable: {e}")
        if not dry:
            record_error_psql(week, f"ops db unavailable: {e}")
        return 1
    oc = ops.cursor()
    if not dry:
        _schema(oc)
    have = False
    try:
        oc.execute("SELECT assessed_at IS NOT NULL FROM nova_care_checkins WHERE week = %s", (week,))
        r = oc.fetchone()
        have = bool(r and r[0])
    except Exception as e:  # noqa: BLE001 — dry run before the columns exist
        log(f"week lookup failed ({e}); treating as not assessed")
    if not due(today, have, force):
        log(f"week {week}: {'already assessed' if have else 'outside the Sun-Wed window'}"); return 0
    try:
        try:
            mem = _connect(MEM_DSN)
            mem_cur = mem.cursor()
        except Exception as e:  # noqa: BLE001 — memories DB down: alerts/reaches still count
            log(f"memories db unavailable ({e}); gathering without articles")
            mem_cur = _NullCursor()
        res = self_assess(oc, mem_cur)
    except Exception as e:  # noqa: BLE001 — write the row anyway, with the error; next run retries
        err = f"{type(e).__name__}: {str(e)[:400]}"
        log(f"self-assessment failed: {err}")
        if not dry:
            oc.execute("INSERT INTO nova_care_checkins (week, items, error) VALUES (%s, '[]'::jsonb, %s) "
                       "ON CONFLICT (week) DO UPDATE SET error = EXCLUDED.error", (week, err))
        return 1
    import nova_rater
    line = nova_rater.render_line(res["scores"])
    msg = f"Self-assessment, week ending {week}: {line}"
    if dry:
        print(msg)
        print(json.dumps(res, indent=2, default=str)[:4000])
        return 0
    oc.execute("""INSERT INTO nova_care_checkins (week, items, message, scores, indicators, assessed_at, error)
                  VALUES (%s, %s::jsonb, %s, %s::jsonb, %s::jsonb, now(), NULL)
                  ON CONFLICT (week) DO UPDATE SET items = EXCLUDED.items, message = EXCLUDED.message,
                      scores = EXCLUDED.scores, indicators = EXCLUDED.indicators,
                      assessed_at = EXCLUDED.assessed_at, error = NULL""",
               (week, json.dumps(res["did"]), msg, json.dumps(res["scores"]),
                json.dumps({"days": res["days"], "indicators": res["indicators"],
                            "not_instrumented": res["not_instrumented"]}, default=str)))
    log(f"assessed week {week}: {line} ({len(res['not_instrumented'])} indicator(s) not instrumented)")
    return 0


class _NullCursor:
    def execute(self, *a, **k): raise RuntimeError("no db")
    def fetchall(self): return []


def volunteered_scores(messages, humans) -> list:
    """[(date, {R..S}, text)] from top-level channel messages by an allowlisted human. Pure."""
    out = []
    for m in messages or []:
        if m.get("user") not in humans or m.get("bot_id") or m.get("subtype"):
            continue
        sc = __import__("nova_rater").parse_jordan_scores(m.get("text"))
        if sc:
            try:
                d = datetime.fromtimestamp(float(m.get("ts"))).date()
            except (TypeError, ValueError):
                continue
            out.append((d, sc, (m.get("text") or "")[:300]))
    return out


def capture_volunteered(oc, nsa, dry=False) -> int:
    """If Jordan volunteered his five numbers in #nova-chat, store them in that week's row (first one wins)."""
    try:
        hist = nsa.slack("conversations.history", channel=CHANNEL,
                         oldest=str(time.time() - VOLUNTEER_LOOKBACK_DAYS * 86400), limit=200)
    except Exception as e:  # noqa: BLE001
        log(f"history read failed: {e}"); return 0
    n = 0
    for d, sc, text in sorted(volunteered_scores(hist.get("messages", []), nsa.HUMANS), key=lambda x: x[0]):
        wk = week_of(d)
        if dry:
            print(f"Jordan's RATER for week {wk}: {sc}"); n += 1; continue
        oc.execute("""INSERT INTO nova_care_checkins (week, items, jordan_scores, jordan_scores_at, reply)
                      VALUES (%s, '[]'::jsonb, %s::jsonb, now(), %s)
                      ON CONFLICT (week) DO UPDATE SET jordan_scores = EXCLUDED.jordan_scores,
                          jordan_scores_at = now(), reply = coalesce(nova_care_checkins.reply, EXCLUDED.reply)
                      WHERE nova_care_checkins.jordan_scores IS NULL""", (wk, json.dumps(sc), text))
        n += getattr(oc, "rowcount", 1) or 0
    return n


def harvest(dry=False):
    import nova_slack_answers as nsa
    ops = _connect(OPS_DSN)
    oc = ops.cursor()
    if not dry:
        _schema(oc)
    # legacy: thread replies to check-ins posted before 2026-10-15
    oc.execute("SELECT id, week, items, slack_channel, slack_ts FROM nova_care_checkins "
               "WHERE reply IS NULL AND slack_ts IS NOT NULL AND asked_at > now() - interval '14 days'")
    n = 0
    for cid, week, items, chan, ts in oc.fetchall():
        text, _ = nsa.read_answer(chan, ts)
        if not text:
            continue
        verdicts = parse_reply(text, len(items))
        summary = summarize(week, items, text, verdicts)
        if dry:
            print(summary); continue
        mid = remember(summary, {"type": "care_checkin", "week": str(week), "verdicts": verdicts})
        oc.execute("UPDATE nova_care_checkins SET reply = %s, reply_at = now(), verdicts = %s::jsonb, "
                   "memory_id = %s WHERE id = %s", (text, json.dumps(verdicts), str(mid) if mid else None, cid))
        n += 1
    v = capture_volunteered(oc, nsa, dry) if hasattr(nsa, "HUMANS") else 0
    log(f"harvested {n} reply(ies), {v} volunteered RATER score(s)")
    return 0


def selftest():
    assert week_of(date(2026, 10, 11)) == date(2026, 10, 11)       # Sunday
    assert week_of(date(2026, 10, 8)) == date(2026, 10, 4)         # Thursday -> previous Sunday
    assert parse_reply("1 yes 2 no 3 meh", 3) == {"1": "helped", "2": "noise", "3": "meh"}
    assert parse_reply("yes all good", 2) == {"1": "helped", "2": "helped"}
    assert parse_reply("9 yes", 3) == {}
    assert due(date(2026, 10, 14), False) and not due(date(2026, 10, 15), False)   # Wed yes, Thu no
    assert not due(date(2026, 10, 12), True)
    items = [{"kind": "alert", "text": "a"}, {"kind": "reach", "text": "b"}]
    assert "-> helped" in summarize(date(2026, 10, 11), items, "1 yes", {"1": "helped"})
    print("selftest ok")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Nova's weekly RATER self-assessment")
    ap.add_argument("--ask", action="store_true", help="assess now if this week's row is missing (posts nothing)")
    ap.add_argument("--force", action="store_true", help="assess even outside Sun-Wed / when already assessed")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not ensure_driver():
        return 1
    if a.ask:
        return ask(a.dry_run, a.force)
    rc = 0
    try:
        rc = harvest(a.dry_run)
    except Exception as e:  # noqa: BLE001 — a Slack/PG blip in harvest must not skip the assessment
        log(f"harvest failed: {e}"); rc = 1
    return max(rc, ask(a.dry_run, a.force))


if __name__ == "__main__":
    sys.exit(main())
