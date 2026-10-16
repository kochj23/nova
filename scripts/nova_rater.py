#!/opt/homebrew/bin/python3
"""nova_rater.py — Nova's RATER self-scores, measured from her own tables (plan items #51/#52/#54, 2026-10-15).

Little Mister, 2026-10-15: "Nova's organs are for Nova ... Nova is the primary customer. I just need to be
informed." So the weekly care check stops asking him and becomes her own self-assessment, and the monthly
continuity review carries the same scorecard. RATER is the SERVQUAL service-quality frame:

  R  Reliability     SLO attainment, promises kept, mark-to-market realisation
  A  Assurance       cited share of her Slack replies (proxy), calibration gap (soft_certainty_state)
  T  Tangibles       reply latency p50 / p95 (gateway_traces); journal pageviews (reported, not scored)
  E  Empathy         weight of presence (was she there for his asks), Annie Wilkes blocks, notification volume
  S  Responsiveness  unanswered share of his Slack asks, the chat_jordan SLO (when A has added it)

Every indicator is a fixed SQL read. A source table/row that does not exist is reported as
{"status": "not instrumented"} — never estimated. A source that exists but had nothing in the window is
{"status": "no data"}. Each dimension score is 1-10 = round(1 + 9 * mean(component in [0, 1])) over the
components that have data; a dimension with none is None ("not instrumented").

Jordan may volunteer his own five numbers in #nova-chat, e.g. "R6 A5 T4 E6 S3"; parse_jordan_scores()
reads that. Optional; nothing waits for it.

  nova_rater.py               print this week's indicators and scores (read-only)
  nova_rater.py --days 30     another window
  nova_rater.py --json
  nova_rater.py --selftest

Owner: Nova. Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

NI = "not instrumented"
NO_DATA = "no data"
DIMENSIONS = ("R", "A", "T", "E", "S")
NAMES = {"R": "Reliability", "A": "Assurance", "T": "Tangibles", "E": "Empathy", "S": "Responsiveness"}
CITE_RE = r"(source:|\[[0-9]+\]|https?://|memory #)"      # proxy for a grounded/cited reply
ERROR_REPLY_RE = r"(went wrong on my end|^\s*error\b)"
LAT_GOOD_MS, LAT_BAD_MS = 10_000, 120_000                  # p95 at/below good = 1.0, at/above bad = 0.0
NOTIF_GOOD_PER_DAY, NOTIF_BAD_PER_DAY = 20, 200
ANNIE_BAD = 10                                             # 10+ blocks in the window = 0.0
_JORDAN_RE = re.compile(r"\b([RATES])\s*[:=]?\s*(10|[0-9])\b")
_PRESENCE_ASKED = re.compile(r"asked something of me (\d+) time")
_PRESENCE_THERE = re.compile(r"I was there for (\d+) of them")


def ni(why: str) -> dict:
    return {"status": NI, "why": why}


def _rollback(cur):
    try:
        cur.connection.rollback()
    except Exception:  # noqa: BLE001 — autocommit cursors / fakes
        pass


def _exists(cur, rel: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (rel,))
    r = cur.fetchone()
    return bool(r and r[0])


def _guard(name, fn):
    """Run one indicator; any failure is that indicator's 'not instrumented', never the whole card."""
    def run(cur, days):
        try:
            return fn(cur, days)
        except Exception as e:  # noqa: BLE001
            _rollback(cur)
            return ni(f"{name} unreadable: {str(e)[:120]}")
    run.__name__ = name
    return run


# ── indicators (read-only SQL; every value parameterised) ────────────────────────────────────

def _slo_attainment(cur, days):
    if not _exists(cur, "public.slo_status"):
        return ni("slo_status table missing")
    cur.execute("""SELECT DISTINCT ON (name) name, sli, target FROM slo_status
                   WHERE at > now() - interval '1 day' ORDER BY name, at DESC""")
    rows = [(n, s, t) for n, s, t in cur.fetchall() if s is not None and t is not None]
    if not rows:
        return {"status": NO_DATA, "why": "no slo_status row in the last day"}
    met = [n for n, s, t in rows if float(s) >= float(t)]
    return {"value": round(len(met) / len(rows), 4), "met": len(met), "total": len(rows),
            "missed": sorted(n for n, s, t in rows if float(s) < float(t)), "source": "slo_status"}


def _slo_chat_jordan(cur, days):
    if not _exists(cur, "public.slo_status"):
        return ni("slo_status table missing")
    cur.execute("SELECT sli, target FROM slo_status WHERE name = %s ORDER BY at DESC LIMIT 1", ("chat_jordan",))
    r = cur.fetchone()
    if not r or r[0] is None:
        return ni("no chat_jordan SLO yet")
    return {"value": round(float(r[0]), 4), "target": float(r[1]) if r[1] is not None else None,
            "source": "slo_status chat_jordan"}


def _promises_kept(cur, days):
    kept = broken = 0
    srcs = []
    if _exists(cur, "public.relationship_ledger"):
        srcs.append("relationship_ledger")
        cur.execute("""SELECT count(*) FILTER (WHERE promise_state = 'kept'),
                              count(*) FILTER (WHERE promise_state IN ('broken', 'missed', 'lapsed'))
                       FROM relationship_ledger WHERE kind = 'promise'""")
        k, b = cur.fetchone()
        kept, broken = kept + (k or 0), broken + (b or 0)
    if _exists(cur, "public.growth_commitments"):
        srcs.append("growth_commitments")
        cur.execute("""SELECT count(*) FILTER (WHERE status = 'succeeded'), count(*) FILTER (WHERE status = 'failed')
                       FROM growth_commitments WHERE resolved_at IS NULL OR resolved_at > now() - make_interval(days => %s)""",
                    (max(days, 30),))
        k, b = cur.fetchone()
        kept, broken = kept + (k or 0), broken + (b or 0)
    if not srcs:
        return ni("no promise ledger")
    if kept + broken == 0:
        return {"status": NO_DATA, "why": "no promise resolved", "source": "+".join(srcs)}
    return {"value": round(kept / (kept + broken), 4), "kept": kept, "broken": broken, "source": "+".join(srcs)}


def _m2m_realisation(cur, days):
    if not _exists(cur, "public.mark_to_market"):
        return ni("mark_to_market table missing")
    cur.execute("""SELECT class, ratio FROM mark_to_market
                   WHERE month = (SELECT max(month) FROM mark_to_market) AND ratio IS NOT NULL""")
    rows = cur.fetchall()
    if not rows:
        return {"status": NO_DATA, "why": "no marking yet"}
    ratios = {c: round(float(r), 4) for c, r in rows}
    return {"value": round(sum(min(1.0, v) for v in ratios.values()) / len(ratios), 4), "by_class": ratios,
            "source": "mark_to_market (mean class ratio, latest month)"}


def _slack_replies(cur, days, extra_sql, params=()):
    cur.execute("SELECT count(*) FILTER (WHERE " + extra_sql + "), count(*) FROM gateway_traces "
                "WHERE channel = 'slack' AND created_at > now() - make_interval(days => %s)",
                (*params, days))
    return cur.fetchone()


def _cited_share(cur, days):
    if not _exists(cur, "public.gateway_traces"):
        return ni("gateway_traces missing")
    hit, n = _slack_replies(cur, days, "coalesce(response, '') ~* %s", (CITE_RE,))
    if not n:
        return {"status": NO_DATA, "why": "no Slack replies in window"}
    return {"value": round(hit / n, 4), "cited": hit, "replies": n,
            "source": "gateway_traces (proxy: reply carries a source marker)"}


def _calibration(cur, days):
    if not _exists(cur, "public.soft_certainty_state"):
        return ni("soft_certainty_state missing")
    cur.execute("SELECT n, mean_conf, hit_rate, gap FROM soft_certainty_state ORDER BY computed_at DESC LIMIT 1")
    r = cur.fetchone()
    if not r or r[3] is None:
        return {"status": NO_DATA, "why": "no calibration computed"}
    gap = float(r[3])
    return {"value": round(max(0.0, 1.0 - 2.0 * abs(gap)), 4), "gap": round(gap, 4), "n": r[0],
            "mean_conf": r[1], "hit_rate": r[2], "source": "soft_certainty_state"}


def _weight_of_presence(cur, days):
    cur.execute("SELECT value FROM service_config WHERE service = %s AND key = %s",
                ("nova_weight_of_presence", "latest"))
    r = cur.fetchone()
    if not r or not r[0]:
        return ni("weight_of_presence has not written service_config latest")
    v = r[0]
    text = (v.get("text") if isinstance(v, dict) else None) or str(v)
    try:
        text = json.loads(text).get("text", text) if isinstance(text, str) and text.lstrip().startswith("{") else text
    except Exception:  # noqa: BLE001
        pass
    a, t = _PRESENCE_ASKED.search(text), _PRESENCE_THERE.search(text)
    if not a or not t or int(a.group(1)) == 0:
        return {"status": NO_DATA, "why": "latest presence pass has no counts"}
    asked, there = int(a.group(1)), int(t.group(1))
    return {"value": round(there / asked, 4), "asked": asked, "there": there,
            "source": "service_config nova_weight_of_presence/latest"}


def _annie_flags(cur, days):
    if not _exists(cur, "public.guard_decisions"):
        return ni("guard_decisions missing")
    cur.execute("""SELECT count(*) FILTER (WHERE decision = 'block'), count(*) FROM guard_decisions
                   WHERE guard = 'annie_rule' AND ts > now() - make_interval(days => %s)""", (days,))
    blocks, n = cur.fetchone()
    return {"value": round(max(0.0, 1.0 - (blocks or 0) / ANNIE_BAD), 4), "blocks": blocks or 0, "checks": n or 0,
            "source": "guard_decisions annie_rule"}


def _latency(cur, days):
    if not _exists(cur, "public.gateway_traces"):
        return ni("gateway_traces missing")
    cur.execute("""SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY total_ms),
                          percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms), count(*)
                   FROM gateway_traces WHERE channel = 'slack' AND total_ms IS NOT NULL
                     AND created_at > now() - make_interval(days => %s)""", (days,))
    p50, p95, n = cur.fetchone()
    if not n:
        return {"status": NO_DATA, "why": "no timed Slack replies"}
    return {"value": round(lat_component(p95), 4), "p50_ms": round(float(p50)), "p95_ms": round(float(p95)),
            "n": n, "source": "gateway_traces total_ms (slack)"}


def _unanswered(cur, days):
    if not _exists(cur, "public.gateway_traces"):
        return ni("gateway_traces missing")
    lost, n = _slack_replies(cur, days, "coalesce(response, '') = '' OR response ~* %s", (ERROR_REPLY_RE,))
    if not n:
        return {"status": NO_DATA, "why": "no Slack asks in window"}
    return {"value": round(1.0 - lost / n, 4), "unanswered_share": round(lost / n, 4), "unanswered": lost, "asks": n,
            "source": "gateway_traces (empty/error reply)"}


def _pageviews(cur, days):
    if not _exists(cur, "public.analytics_pageviews"):
        return ni("analytics_pageviews missing")
    cur.execute("""SELECT count(*), count(DISTINCT visitor_hash) FROM analytics_pageviews
                   WHERE ts > now() - make_interval(days => %s)""", (days,))
    n, v = cur.fetchone()
    return {"pageviews": n, "visitors": v, "source": "analytics_pageviews"}   # informational, unscored


def _notifications(cur, days):
    if not _exists(cur, "telemetry.events"):
        return ni("telemetry.events missing")
    cur.execute("SELECT count(*) FROM telemetry.events WHERE status = 'sent' AND ts > now() - make_interval(days => %s)",
                (days,))
    n = cur.fetchone()[0] or 0
    per_day = n / max(days, 1)
    return {"value": round(notif_component(per_day), 4), "sent": n, "per_day": round(per_day, 1),
            "source": "telemetry.events sent"}


INDICATORS = {
    "slo_attainment": _guard("slo_attainment", _slo_attainment),
    "slo_chat_jordan": _guard("slo_chat_jordan", _slo_chat_jordan),
    "promises_kept": _guard("promises_kept", _promises_kept),
    "mark_to_market_realisation": _guard("mark_to_market_realisation", _m2m_realisation),
    "cited_reply_share": _guard("cited_reply_share", _cited_share),
    "calibration": _guard("calibration", _calibration),
    "weight_of_presence": _guard("weight_of_presence", _weight_of_presence),
    "annie_flags": _guard("annie_flags", _annie_flags),
    "latency": _guard("latency", _latency),
    "unanswered": _guard("unanswered", _unanswered),
    "journal_pageviews": _guard("journal_pageviews", _pageviews),
    "notification_volume": _guard("notification_volume", _notifications),
}
COMPONENTS = {
    "R": ("slo_attainment", "promises_kept", "mark_to_market_realisation"),
    "A": ("cited_reply_share", "calibration"),
    "T": ("latency",),
    "E": ("weight_of_presence", "annie_flags", "notification_volume"),
    "S": ("unanswered", "slo_chat_jordan"),
}


# ── pure scoring ─────────────────────────────────────────────────────────────────────────────

def _lin(x, good, bad):
    if x is None:
        return None
    x = float(x)
    if x <= good:
        return 1.0
    if x >= bad:
        return 0.0
    return 1.0 - (x - good) / (bad - good)


def lat_component(p95_ms):
    return _lin(p95_ms, LAT_GOOD_MS, LAT_BAD_MS)


def notif_component(per_day):
    return _lin(per_day, NOTIF_GOOD_PER_DAY, NOTIF_BAD_PER_DAY)


def score(indicators: dict) -> dict:
    """{dim: 1-10 or None}. Only components with a numeric 'value' count. Pure."""
    out = {}
    for dim, keys in COMPONENTS.items():
        vals = []
        for k in keys:
            v = (indicators.get(k) or {}).get("value")
            if isinstance(v, (int, float)):
                vals.append(min(1.0, max(0.0, float(v))))
        out[dim] = round(1 + 9 * sum(vals) / len(vals)) if vals else None
    return out


def parse_jordan_scores(text: str | None) -> dict | None:
    """'R6 A5 T4 E6 S3' (any order, optional ':'/'=') -> {'R': 6, ...}; None unless all five are present
    and each is 1-10. A number on its own is never a score. Pure."""
    found = {}
    for k, v in _JORDAN_RE.findall((text or "")[:500].upper()):
        found.setdefault(k, int(v))
    if set(found) != set(DIMENSIONS) or not all(1 <= x <= 10 for x in found.values()):
        return None
    return {k: found[k] for k in DIMENSIONS}


def render_line(scores: dict) -> str:
    return " ".join(f"{d}{scores.get(d) if scores.get(d) is not None else '?'}" for d in DIMENSIONS)


def measure(cur, days: int = 7) -> dict:
    """{indicator: result} for every indicator; one bad source never sinks the card."""
    return {name: fn(cur, days) for name, fn in INDICATORS.items()}


def assess(cur, days: int = 7) -> dict:
    ind = measure(cur, days)
    return {"days": days, "indicators": ind, "scores": score(ind),
            "not_instrumented": sorted(k for k, v in ind.items() if v.get("status") == NI)}


def selftest() -> int:
    assert parse_jordan_scores("R6 A5 T4 E6 S3") == {"R": 6, "A": 5, "T": 4, "E": 6, "S": 3}
    assert parse_jordan_scores("r6 a5 t4 e6") is None and parse_jordan_scores("R0 A5 T4 E6 S3") is None
    assert score({"slo_attainment": {"value": 1.0}, "promises_kept": {"status": NI}})["R"] == 10
    assert score({})["A"] is None
    assert lat_component(5000) == 1.0 and lat_component(200_000) == 0.0
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    import nova_dsn
    conn = nova_dsn.pg_connect("nova_ops")
    conn.autocommit = True
    res = assess(conn.cursor(), a.days)
    if a.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print(f"RATER ({a.days}d): {render_line(res['scores'])}")
        for k, v in res["indicators"].items():
            print(f"  {k:28s} {json.dumps(v, default=str)[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
