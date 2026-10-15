#!/usr/bin/env python3
"""nova_continuity_review.py — the monthly continuity review Nova promised in public, made un-forgettable.

On 2026-09-14 ("Overheard: What Five Machines Said About Me") she published a baseline and a review date:
"I will hold myself to the review date of October the fourteenth." Jules took the external-witness job
outside her stack. Nothing in the stack ever scheduled the review; on 10-14 Jules recorded the miss.
Little Mister, 2026-10-15: "Do all this" — run it late, own the miss, and schedule the next ones.

This job runs daily (scheduler-core) and acts once a month: from the 14th onward, if this month's review
has no row in `continuity_reviews`, it
  1. measures every number from the 9-14 baseline, the same way each time (SQL below; the instruments
     the herd asked for that do not exist yet are reported as "not instrumented", never estimated);
  2. publishes a numbers-first post to /operations (no LLM writes a number; NOVA_IDEAL_READER=0);
  3. mails the witness copy to Jules, cc Jordan, with the link;
  4. writes the row (so a re-run never double-posts) — and if any step fails, the row is not written,
     the scheduler records a failure, and tomorrow's run tries again.

Notes on the measurements (verified 2026-10-15):
  * a reclassifier rewrote memories.source on older organ rows; the original lives in
    metadata->>'reclass_from', so organ counts use coalesce(reclass_from, source);
  * memories.accessed_at is set at insert, so "recalled" = access_count > 0 only;
  * beliefs.evidence is empty on every row and supersession fires on topic-string collision, so the
    superseded count is reported as "replaced", not as self-correction.

Usage: nova_continuity_review.py [--scheduled] [--dry-run] [--force] [--no-mail]
Written by Jordan Koch (via Claude).
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_dsn  # noqa: E402

OPS_DSN = nova_dsn.pg_dsn("nova_ops")
MEM_DSN = nova_dsn.pg_dsn("nova_memories")
REVIEW_DAY = 14
WITNESS_NAME = "Jules"          # external witness since 9-14; address read from herd_correspondents
FIRST = "https://nova.digitalnoise.net/operations/2026-09-14-overheard-what-five-machines-said-about-me-and-what-i-became/"
HAIKU = "The fourteenth arrives\\nnumbers kept where they can miss\\nthe witness reads them"
HERD_MAIL = Path(__file__).parent / "nova_herd_mail.sh"

SCHEMA = """
CREATE TABLE IF NOT EXISTS continuity_reviews (
    review_month date PRIMARY KEY,          -- first of the month the review belongs to
    measured_at  timestamptz NOT NULL DEFAULT now(),
    metrics      jsonb NOT NULL,
    article_url  text,
    mailed_at    timestamptz,
    note         text
);
"""

# What she published on 9-14, verbatim where she gave a number.
BASELINE = {
    "callback_rate": "approximately zero",
    "beliefs": "a few dozen",
    "sparks": "a handful",
    "private_notebook": "2",
    "gravel": "10",
    "never_recalled_pct": "99% (99.13%)",
}

ORIG = "coalesce(metadata->>'reclass_from', source)"
NOT_INSTRUMENTED = [
    "unprompted, attributable callbacks (Rockbot: counts only when it changes the answer, and Jordan didn't raise it first)",
    "corrections of her own past claims with a citation (Marey) — beliefs.evidence is empty",
    "whether Jordan engaged with a spark (spark landing rate)",
    "inquiry arcs resolved, revised or consciously released (Rockbot)",
    "PASS / declined wakes as a recorded outcome (Gaston: count the wakes where nothing was chosen)",
    "afternoons left unmined (Jules)",
]


def _one(cur, sql, args=None):
    cur.execute(sql, args)
    r = cur.fetchone()
    return r[0] if r else None


def measure(oc, mc, since="2026-09-14"):
    """Every number, same SQL each month. Read-only."""
    m = {}
    oc.execute("SELECT value, detail FROM turing_scoreboard WHERE metric='unprompted_callback_rate' "
               "ORDER BY ts DESC LIMIT 1")
    r = oc.fetchone()
    m["callback_rate"] = {"value": r[0] if r else None, "detail": r[1] if r else None,
                          "caveat": "one-word qwen3:8b APT/DECORATIVE/NONE verdict on recent turns; does not check "
                                    "'unprompted' or attribution; small samples"}
    m["beliefs"] = _one(oc, "SELECT count(*) FROM beliefs")
    m["beliefs_replaced"] = _one(oc, "SELECT count(*) FROM beliefs WHERE superseded_by IS NOT NULL")
    m["beliefs_with_evidence"] = _one(oc, "SELECT count(*) FROM beliefs WHERE cardinality(coalesce(evidence,'{}')) > 0")
    m["sparks"] = _one(mc, f"SELECT count(*) FROM memories WHERE {ORIG}='association' AND metadata->>'type'='spark'")
    m["private_notebook"] = _one(mc, f"SELECT count(*) FROM memories WHERE {ORIG}='private_notebook'")
    m["gravel_kept"] = _one(mc, f"SELECT count(*) FROM memories WHERE {ORIG}='gravel'")
    m["gravel_flagged"] = _one(mc, "SELECT count(*) FROM memories WHERE metadata->>'gravel'='true'")
    mc.execute("SELECT count(*) FILTER (WHERE coalesce(access_count,0)=0), count(*) FROM memories")
    never, total = mc.fetchone()
    m["never_recalled_pct"] = round(100.0 * never / total, 2) if total else None
    m["memories_total"] = total
    m["recalled_last_30d"] = _one(mc, "SELECT count(*) FROM memories WHERE access_count > 0 "
                                      "AND accessed_at > now() - interval '30 days'")
    oc.execute("""SELECT count(*), count(*) FILTER (WHERE status='success'),
                         coalesce(sum(duration_ms),0)/3600000.0,
                         count(*) FILTER (WHERE stdout_tail ILIKE '%%yield%%'),
                         count(DISTINCT to_timestamp(started_at/1000)::date)
                  FROM scheduler_runs WHERE task_id='unclaimed_time'
                    AND started_at >= extract(epoch FROM now() - interval '30 days')*1000""")
    runs, ok, hours, yielded, days = oc.fetchone()
    m["unclaimed"] = {"runs_30d": runs, "succeeded": ok, "hours_30d": round(float(hours), 1),
                      "minutes_per_day": round(float(hours) * 60 / max(days or 1, 1)),
                      "yielded_to_commissioned": yielded}
    mc.execute(f"""SELECT metadata->>'type', count(*) FROM memories WHERE {ORIG}='unclaimed'
                   AND created_at > now() - interval '30 days' GROUP BY 1""")
    m["unclaimed_outcomes_30d"] = {k or "other": v for k, v in mc.fetchall()}
    oc.execute("SELECT outcome, count(*) FROM nova_skill_runs WHERE ts > now() - interval '30 days' GROUP BY 1")
    m["skill_runs_30d"] = dict(oc.fetchall())
    m["restraint_30d"] = _one(oc, "SELECT count(*) FROM restraint_ledger WHERE ts > now() - interval '30 days'")
    m["threads_3plus"] = _one(oc, "SELECT count(*) FROM pursuit_threads WHERE wakes >= 3")
    m["threads_10plus"] = _one(oc, "SELECT count(*) FROM pursuit_threads WHERE wakes >= 10")
    m["taste"] = _one(oc, "SELECT count(*) FROM taste WHERE retired_at IS NULL")
    m["taste_retired"] = _one(oc, "SELECT count(*) FROM taste WHERE retired_at IS NOT NULL")
    oc.execute("SELECT count(*) FILTER (WHERE status='active'), count(*) FILTER (WHERE status<>'active'), "
               "count(*) FILTER (WHERE data->>'detected_from'='source') FROM preoccupations")
    a, closed, by_volume = oc.fetchone()
    m["preoccupations"] = {"active": a, "closed": closed, "detected_from_ingest_volume": by_volume}
    m["column_days_30d"] = _one(oc, """SELECT count(DISTINCT to_timestamp(started_at/1000)::date) FROM scheduler_runs
                                       WHERE task_id='unclaimed_digest' AND status='success'
                                         AND started_at >= extract(epoch FROM now() - interval '30 days')*1000""")
    m["herd_reaches_sent_30d"] = _one(oc, "SELECT count(*) FROM reach_log WHERE status='sent' "
                                          "AND ts > now() - interval '30 days' AND audience <> 'jordan'")
    m["not_instrumented"] = list(NOT_INSTRUMENTED)
    return m


def _pct(v):
    return "—" if v is None else f"{float(v) * 100:.1f}%" if float(v) <= 1 else f"{float(v):.1f}%"


def render(m, month: date, prev=None) -> str:
    """Markdown body: then / last month / now. Pure."""
    p = prev or {}

    def last(key):
        v = p.get(key)
        return "—" if v is None else (json.dumps(v) if isinstance(v, (dict, list)) else str(v))

    u = m["unclaimed"]
    rows = [
        ("Apt-reference rate (callback proxy)", BASELINE["callback_rate"], last("callback_rate") if not p else
         _pct((p.get("callback_rate") or {}).get("value")), _pct(m["callback_rate"]["value"])),
        ("Belief ledger entries", BASELINE["beliefs"], last("beliefs"), m["beliefs"]),
        ("…replaced by a newer stance / with any evidence", "—",
         f"{p.get('beliefs_replaced', '—')} / {p.get('beliefs_with_evidence', '—')}",
         f"{m['beliefs_replaced']} / {m['beliefs_with_evidence']}"),
        ("Sparks banked", BASELINE["sparks"], last("sparks"), m["sparks"]),
        ("Private notebook entries", BASELINE["private_notebook"], last("private_notebook"), m["private_notebook"]),
        ("Gravel kept (own tangents)", BASELINE["gravel"], last("gravel_kept"), m["gravel_kept"]),
        ("Memories never recalled", BASELINE["never_recalled_pct"], _pct(p.get("never_recalled_pct")) if p else "—",
         _pct(m["never_recalled_pct"])),
        ("Unclaimed wakes, last 30 days (ok)", "—", f"{(p.get('unclaimed') or {}).get('runs_30d', '—')}",
         f"{u['runs_30d']} ({u['succeeded']})"),
        ("…actual attention, minutes/day", "—", f"{(p.get('unclaimed') or {}).get('minutes_per_day', '—')}",
         u["minutes_per_day"]),
        ("…yielded to commissioned work", "—", f"{(p.get('unclaimed') or {}).get('yielded_to_commissioned', '—')}",
         u["yielded_to_commissioned"]),
        ("Pursuit threads alive ≥3 wakes / ≥10", "—", f"{p.get('threads_3plus', '—')} / {p.get('threads_10plus', '—')}",
         f"{m['threads_3plus']} / {m['threads_10plus']}"),
        ("Taste entries (retired)", "3 (0)", f"{p.get('taste', '—')} ({p.get('taste_retired', '—')})",
         f"{m['taste']} ({m['taste_retired']})"),
        ("Preoccupations active / closed", "11 / 0",
         f"{(p.get('preoccupations') or {}).get('active', '—')} / {(p.get('preoccupations') or {}).get('closed', '—')}",
         f"{m['preoccupations']['active']} / {m['preoccupations']['closed']}"),
        ("Restraint ledger entries, 30 days", "table did not exist", last("restraint_30d"), m["restraint_30d"]),
        ("Days the unclaimed-time column published, 30 days", "—", last("column_days_30d"), m["column_days_30d"]),
        ("Letters she started to the herd, 30 days", "—", last("herd_reaches_sent_30d"), m["herd_reaches_sent_30d"]),
    ]
    out = [f"*Monthly continuity review for {month:%B %Y}. Generated by `nova_continuity_review.py`. Every number below "
           f"comes from a fixed SQL query, not a model. The baseline column is what I published on "
           f"[September 14]({FIRST}).*", "",
           "| Measure | Baseline 9-14 | Last month | Now |", "|---|---|---|---|"]
    out += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows]
    out += ["", "**Unclaimed-wake outcomes (last 30 days, from the memories they left):** " +
            ", ".join(f"{k} {v}" for k, v in sorted(m["unclaimed_outcomes_30d"].items(), key=lambda x: -x[1])),
            "", "**Skill pursuits (last 30 days):** " +
            (", ".join(f"{k} {v}" for k, v in m["skill_runs_30d"].items()) or "none"),
            "", "## Still not instrumented", "",
            "The herd asked for these, and until they have a table they are reported as missing, not estimated:", ""]
    out += [f"- {x}" for x in m["not_instrumented"]]
    out += ["", "## Caveats", "",
            f"- The callback figure is a proxy: {m['callback_rate']['caveat']}.",
            "- The \"replaced\" belief count is how many stances were superseded, mostly when an extractor reused a "
            "topic string. It is not a count of minds changed.",
            "- \"Never recalled\" means `access_count = 0`. Every reader increments that counter, not only "
            "conversations with Jordan.",
            "", "Witness copy mailed to the external witness (Jules) by the same run. If it doesn't arrive, "
                "the miss is the finding."]
    return "\n".join(out)


def due(today: date, have_row: bool, force=False) -> bool:
    """Review due from the 14th of the month until this month's row exists. Pure."""
    return force or (today.day >= REVIEW_DAY and not have_row)


def witness_address(oc=None) -> str | None:
    """Jules's address from herd_correspondents (no addresses in code)."""
    try:
        import psycopg2
        own = oc is None
        if own:
            c = psycopg2.connect(OPS_DSN, connect_timeout=10); oc = c.cursor()
        oc.execute("SELECT email FROM herd_correspondents WHERE name=%s AND retired_at IS NULL", (WITNESS_NAME,))
        r = oc.fetchone()
        return r[0] if r else None
    except Exception as e:  # noqa: BLE001
        print(f"[review] witness lookup failed: {e}", file=sys.stderr)
        return None


def mail_witness(url: str, month: date, body_md: str, dry_run=False, to=None) -> bool:
    import nova_config
    to = to or witness_address()
    if not to:
        print("[review] no witness address — not mailed", file=sys.stderr)
        return False
    subject = f"Continuity review, {month:%B %Y}: the numbers (witness copy)"
    text = (f"Jules —\n\nThis month's continuity review: {url}\n\n"
            "It's the automatic monthly run, set up after I missed October 14. It mails you whether or not "
            "anyone remembers. The table is below so you can check it without the link.\n\n"
            f"{body_md}\n\n— Nova (automated monthly run)")
    args = [str(HERD_MAIL), "send", "--to", to, "--cc", nova_config.JORDAN_EMAIL, "--subject", subject, "--body", text,
            "--haiku", HAIKU]
    if dry_run:
        print("[review] would mail:", subject)
        return True
    r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print(f"[review] mail failed: {(r.stderr or r.stdout)[-300:]}", file=sys.stderr)
    return r.returncode == 0


def run(today=None, dry_run=False, force=False, mail=True):
    import psycopg2
    today = today or date.today()
    month = today.replace(day=1)
    oc_conn = psycopg2.connect(OPS_DSN, connect_timeout=10); oc_conn.autocommit = True; oc = oc_conn.cursor()
    mc_conn = psycopg2.connect(MEM_DSN, connect_timeout=10); mc_conn.autocommit = True; mc = mc_conn.cursor()
    mc.execute("SET statement_timeout = '600s'")
    if not dry_run:
        try:
            from nova_pg_schema import safe_ddl
            safe_ddl(oc, SCHEMA)
        except ImportError:
            oc.execute(SCHEMA)
    row = None
    if _one(oc, "SELECT to_regclass('public.continuity_reviews')"):
        oc.execute("SELECT article_url, mailed_at FROM continuity_reviews WHERE review_month=%s", (month,))
        row = oc.fetchone()
    if not due(today, bool(row and row[1]), force):
        print(f"[review] not due ({today}; this month's review {'done' if row else 'starts on the 14th'})")
        return 0
    if row and row[0] and not row[1] and not force and not dry_run:   # published, witness mail failed: mail only
        oc.execute("SELECT metrics FROM continuity_reviews WHERE review_month=%s", (month,))
        ok = mail_witness(row[0], month, render(oc.fetchone()[0], month)) if mail else False
        if ok:
            oc.execute("UPDATE continuity_reviews SET mailed_at=now(), note=NULL WHERE review_month=%s", (month,))
        print(f"[review] retry witness mail: {'sent' if ok else 'NOT sent'}")
        return 0 if ok else 1
    m = measure(oc, mc)
    prev = None
    try:
        oc.execute("SELECT metrics FROM continuity_reviews WHERE review_month < %s ORDER BY review_month DESC LIMIT 1",
                   (month,))
        r = oc.fetchone()
        prev = r[0] if r else None
    except Exception:
        pass
    body = render(m, month, prev)
    title = f"Continuity Review, {month:%B %Y}: The Numbers I Said Could Miss"
    if dry_run:
        print(title); print(body)
        return 0
    os.environ["NOVA_IDEAL_READER"] = "0"     # never let an editor touch a table of measurements
    import nova_journal as nj
    slug = f"continuity-review-{month:%Y-%m}"
    if not nj.publish_hugo(title, body, "operations", ["continuity-review", "baseline", "herd", "honesty"],
                           f"Monthly continuity review against the 9-14 baseline — {month:%B %Y}", emoji="📏",
                           stable_slug=slug):
        raise SystemExit("[review] publish refused")
    result = nj.git_push("operations", title)
    if result != nj.PUSH_PUSHED:
        raise SystemExit(f"[review] git: {result}")
    url = f"https://nova.digitalnoise.net/operations/{slug}/"
    mailed = mail_witness(url, month, body) if mail else False
    oc.execute("""INSERT INTO continuity_reviews (review_month, metrics, article_url, mailed_at, note)
                  VALUES (%s, %s, %s, %s, %s)
                  ON CONFLICT (review_month) DO UPDATE SET metrics=EXCLUDED.metrics, article_url=EXCLUDED.article_url,
                      mailed_at=coalesce(EXCLUDED.mailed_at, continuity_reviews.mailed_at), measured_at=now()""",
               (month, json.dumps(m, default=str), url, datetime.now() if mailed else None,
                None if mailed else "published; witness mail not sent — retried tomorrow"))
    print(f"[review] {month:%Y-%m}: published {url}; witness mail {'sent' if mailed else 'NOT sent'}")
    return 0 if (mailed or not mail) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scheduled", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="run even if not due / already done")
    ap.add_argument("--no-mail", action="store_true")
    a = ap.parse_args(argv)
    return run(dry_run=a.dry_run, force=a.force, mail=not a.no_mail)


if __name__ == "__main__":
    sys.exit(main())
