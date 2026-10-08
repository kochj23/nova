#!/usr/bin/env python3
"""nova_derry_clock.py — the Derry Clock: what comes around every year.

In King's "It" the town runs on a 27-year clock. Nova's clock is annual: equipment that fails
the same month every year, fire-season scanner volume, holiday patterns, anniversaries of
notable events. Two parts:

  1. Monthly fingerprints -> nova_ops.derry_monthly (metric, year, month, value, days_covered).
     Recomputed daily from the raw feeds for every month that has data, so next year has
     something true to compare against. Metrics: scanner volume / near-home / fire mentions,
     CHP volume, low-helicopter hours, incidents opened / critical, per-recurrence-key incident
     counts, never-seen network devices, unexplained-event kinds.
  2. A "this time last year..." note on the 1st of each month (info, category digest) and
     upcoming_cycles(days=30) for other organs.

HONESTY RULE: a cycle needs at least two occurrences a year apart. With one prior year it is
reported as "last year" (one data point, not a pattern); with none it says so and names the
first date a like-for-like comparison becomes possible. Telemetry here begins mid-2026, so the
early notes will mostly say that — which is the truth.

    from nova_derry_clock import upcoming_cycles
    for c in upcoming_cycles(days=30): print(c["date"], c["kind"], c["label"], c["evidence"])

Usage:
    nova_derry_clock.py --refresh       # recompute derry_monthly
    nova_derry_clock.py --note          # post the monthly note (also refreshes)
    nova_derry_clock.py --upcoming 30   # print upcoming_cycles
    add --dry-run to print without writing/posting
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import calendar
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

TAG = "derry"
MIN_DAYS = 20          # a month needs this many days of data to be compared
FIXED_HOLIDAYS = {(1, 1): "New Year's Day", (2, 14): "Valentine's Day", (7, 4): "Independence Day",
                  (10, 31): "Halloween", (12, 24): "Christmas Eve", (12, 25): "Christmas Day",
                  (12, 31): "New Year's Eve"}


# ── pure calendar helpers ───────────────────────────────────────────────────

def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th weekday (0=Mon) of month; n=-1 for the last one."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(year, month, calendar.monthrange(year, month)[1])
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def holidays(year: int) -> dict:
    h = {date(year, m, d): name for (m, d), name in FIXED_HOLIDAYS.items()}
    h[nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    h[nth_weekday(year, 5, 0, -1)] = "Memorial Day"
    h[nth_weekday(year, 9, 0, 1)] = "Labor Day"
    h[nth_weekday(year, 11, 3, 4)] = "Thanksgiving"
    return h


def compare(this_val, last_val, this_days, last_days) -> str | None:
    """Evidence sentence for one metric year-over-year, or None if not comparable."""
    if last_val is None or this_days < MIN_DAYS or last_days < MIN_DAYS:
        return None
    if last_val == 0:
        return f"{this_val:g} vs 0 last year"
    pct = (this_val - last_val) / last_val * 100
    return f"{this_val:g} vs {last_val:g} last year ({pct:+.0f}%)"


def classify_recurrence(years_seen: list) -> str:
    """'cycle' needs >=2 distinct prior years; one is 'last year'; none is 'no history'."""
    n = len(set(years_seen))
    return "cycle" if n >= 2 else ("last_year" if n == 1 else "no_history")


# ── monthly fingerprints ────────────────────────────────────────────────────

def _month_rows(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchall()


def compute_metrics(cur) -> list:
    """-> [(metric, year, month, value, days_covered, evidence)] from every feed with history."""
    out = []
    mconn = W.connect(W.MEM_DSN)
    mc = mconn.cursor()
    try:
        for metric, where, extra in (
                ("scanner_transmissions", "source='scanner'", ()),
                ("scanner_near_home_2mi", "source='scanner' AND (metadata->'geo'->>'nearest_mi')::float <= 2", ()),
                ("scanner_fire_mentions", "source='scanner' AND text ~* %s", (r"\m(fire|smoke|brush|structure fire)\M",))):
            for y, m, v, d in _month_rows(mc, (
                    "SELECT extract(year FROM created_at)::int, extract(month FROM created_at)::int, count(*), "
                    "count(DISTINCT created_at::date) FROM memories WHERE " + where + " GROUP BY 1, 2"), extra):
                out.append((metric, y, m, float(v), int(d), "nova_memories.memories source=scanner"))
        # notable older memories (anniversary material) — counts only, never content
        for src, y, m, v in _month_rows(mc, (
                "SELECT source, extract(year FROM extracted_date)::int, extract(month FROM extracted_date)::int, count(*) "
                "FROM memories WHERE extracted_date IS NOT NULL AND extracted_date >= '2000-01-01' "
                "AND extracted_date < date_trunc('year', now()) GROUP BY 1, 2, 3")):
            out.append((f"memories:{src}", y, m, float(v), 0, "nova_memories extracted_date"))
    finally:
        mconn.close()

    lat, lon = W.home()
    d = 1.5 / 55.0 + 0.01
    for metric, sql, params in (
            ("chp_incidents", "SELECT extract(year FROM ts)::int, extract(month FROM ts)::int, count(DISTINCT incident_id), "
                              "count(DISTINCT ts::date) FROM telemetry.chp_incidents GROUP BY 1, 2", ()),
            ("chp_near_home", "SELECT extract(year FROM ts)::int, extract(month FROM ts)::int, count(DISTINCT incident_id), "
                              "count(DISTINCT ts::date) FROM telemetry.chp_incidents WHERE lat BETWEEN %s AND %s "
                              "AND lon BETWEEN %s AND %s GROUP BY 1, 2", (lat - d, lat + d, lon - d * 1.3, lon + d * 1.3)),
            ("helicopter_low_hours", "SELECT extract(year FROM h)::int, extract(month FROM h)::int, count(*), "
                                     "count(DISTINCT h::date) FROM (SELECT DISTINCT hex, date_trunc('hour', ts) h "
                                     "FROM telemetry.overhead_flights WHERE is_helicopter AND alt_ft < 2500 "
                                     "AND dist_nm < 1.5) x GROUP BY 1, 2", ()),
            ("incidents_opened", "SELECT extract(year FROM opened_at)::int, extract(month FROM opened_at)::int, count(*), "
                                 "count(DISTINCT opened_at::date) FROM telemetry.incidents GROUP BY 1, 2", ()),
            ("incidents_critical", "SELECT extract(year FROM opened_at)::int, extract(month FROM opened_at)::int, count(*), "
                                   "count(DISTINCT opened_at::date) FROM telemetry.incidents WHERE severity='critical' "
                                   "GROUP BY 1, 2", ()),
            ("new_network_devices", "SELECT extract(year FROM ts)::int, extract(month FROM ts)::int, count(*), "
                                    "count(DISTINCT ts::date) FROM telemetry.events WHERE source='nova_security_organ' "
                                    "AND title ILIKE '%%NEW DEVICE%%' GROUP BY 1, 2", ())):
        for y, m, v, dd in _month_rows(cur, sql, params):
            out.append((metric, y, m, float(v), int(dd), metric.split("_")[0]))
    # equipment / service that keeps breaking: incidents per recurrence key per month
    for key, y, m, v in _month_rows(cur, (
            "SELECT recurrence_key, extract(year FROM opened_at)::int, extract(month FROM opened_at)::int, count(*) "
            "FROM telemetry.incidents WHERE recurrence_key IS NOT NULL AND recurrence_key <> '' "
            "GROUP BY 1, 2, 3 HAVING count(*) >= 2")):
        out.append((f"incident:{key[:120]}", y, m, float(v), 0, "telemetry.incidents.recurrence_key"))
    try:
        for kind, y, m, v in _month_rows(cur, (
                "SELECT kind, extract(year FROM first_seen)::int, extract(month FROM first_seen)::int, count(*) "
                "FROM unexplained_events GROUP BY 1, 2, 3")):
            out.append((f"unexplained:{kind}", y, m, float(v), 0, "unexplained_events"))
    except Exception:
        pass
    return out


def refresh(cur, dry_run=False) -> int:
    rows = compute_metrics(cur)
    if dry_run:
        for r in sorted(rows)[:40]:
            print(r)
        return len(rows)
    W.ensure_schema(cur)
    for metric, y, m, v, d, ev in rows:
        cur.execute("INSERT INTO derry_monthly (metric, year, month, value, days_covered, evidence) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (metric, year, month) DO UPDATE SET "
                    "value=EXCLUDED.value, days_covered=EXCLUDED.days_covered, evidence=EXCLUDED.evidence, "
                    "computed_at=now()", (metric, y, m, v, d, ev))
    return len(rows)


# ── upcoming cycles (callable by other organs) ──────────────────────────────

def upcoming_cycles(days: int = 30, cur=None, today: date | None = None) -> list:
    """Annual recurrences expected in the next `days`. Every item carries its evidence.
    -> [{"date": date|None, "kind": str, "label": str, "evidence": str, "strength": str}]"""
    today = today or datetime.now(W.TZ).date()
    end = today + timedelta(days=days)
    own = cur is None
    conn = W.connect() if own else None
    cur = cur or conn.cursor()
    out = []
    try:
        for y in {today.year, end.year}:
            for d, name in holidays(y).items():
                if today <= d <= end:
                    out.append({"date": d, "kind": "holiday", "label": name, "strength": "calendar",
                                "evidence": "fixed/computed US calendar date"})
        months = sorted({(today.year, today.month), (end.year, end.month)})
        for y, m in months:
            cur.execute("SELECT metric, year, value, days_covered FROM derry_monthly WHERE month=%s AND year < %s",
                        (m, y))
            prior: dict = {}
            for metric, py, v, dd in cur.fetchall():
                prior.setdefault(metric, []).append((py, v, dd))
            for metric, hist in sorted(prior.items()):
                years = [py for py, v, _dd in hist if v > 0]
                strength = classify_recurrence(years)
                if strength == "no_history":
                    continue
                if metric.startswith("incident:"):
                    out.append({"date": None, "kind": "recurring_incident", "strength": strength,
                                "label": f"{metric[9:]} opened incidents in {calendar.month_name[m]} of "
                                         f"{', '.join(map(str, sorted(set(years))))}",
                                "evidence": "; ".join(f"{py}: {v:g} incidents" for py, v, _ in sorted(hist))})
                elif metric.startswith("memories:"):
                    out.append({"date": None, "kind": "anniversary", "strength": strength,
                                "label": f"{metric[9:]} memories dated {calendar.month_name[m]} "
                                         f"{', '.join(map(str, sorted(set(years))))}",
                                "evidence": "; ".join(f"{py}: {v:g} memories" for py, v, _ in sorted(hist))})
        cur.execute("SELECT opened_at, title FROM telemetry.incidents WHERE severity='critical' "
                    "AND opened_at < now() - interval '300 days'")
        for opened, title in cur.fetchall():
            ann = opened.astimezone(W.TZ).date()
            for y in {today.year, end.year}:
                try:
                    a = ann.replace(year=y)
                except ValueError:
                    continue
                if today <= a <= end and a.year > ann.year:
                    out.append({"date": a, "kind": "anniversary", "strength": "last_year",
                                "label": f"{a.year - ann.year} year(s) since critical incident: {title[:100]}",
                                "evidence": f"telemetry.incidents opened {ann.isoformat()}"})
    finally:
        if own:
            conn.close()
    return sorted(out, key=lambda c: (c["date"] or date.max, c["kind"]))


# ── monthly note ────────────────────────────────────────────────────────────

def first_data_month(cur):
    cur.execute("SELECT min(make_date(year, month, 1)) FROM derry_monthly "
                "WHERE days_covered >= %s AND metric NOT LIKE 'memories:%%'", (MIN_DAYS,))
    return cur.fetchone()[0]


def compose_note(cur, today: date) -> str:
    y, m = today.year, today.month
    mname = calendar.month_name[m]
    cur.execute("SELECT a.metric, a.value, a.days_covered, b.value, b.days_covered FROM derry_monthly b "
                "LEFT JOIN derry_monthly a ON a.metric=b.metric AND a.year=%s AND a.month=%s "
                "WHERE b.year=%s AND b.month=%s AND b.metric NOT LIKE 'memories:%%' ORDER BY b.metric",
                (y - 1, m, y - 1, m))
    last_year = cur.fetchall()
    lines = [f"*Derry Clock — {mname} {y}: this time last year…*"]
    if not last_year:
        fd = first_data_month(cur)
        if fd:
            nxt = date(fd.year + 1, fd.month, 1)
            lines.append(f"• No telemetry exists for {mname} {y - 1}; Nova's monthly history begins "
                         f"{fd:%B %Y}. First like-for-like comparison: {nxt:%B %Y}.")
        else:
            lines.append(f"• No telemetry exists for {mname} {y - 1} yet.")
    else:
        for metric, _a, _ad, v, dd in last_year[:6]:
            if dd >= MIN_DAYS or metric.startswith(("incident:", "unexplained:")):
                lines.append(f"• {mname} {y - 1}: {metric} = {v:g}" + (f" over {dd} days" if dd else ""))
    for c in upcoming_cycles(days=calendar.monthrange(y, m)[1] - today.day, cur=cur, today=today)[:6]:
        when = f"{c['date']:%b %d}" if c["date"] else mname
        tag = {"cycle": "recurring", "last_year": "once before", "calendar": "calendar"}.get(c["strength"], c["strength"])
        lines.append(f"• {when} — {c['label']} [{tag}; {c['evidence']}]")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--note", action="store_true")
    ap.add_argument("--upcoming", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date")
    a = ap.parse_args(argv)
    conn = W.connect()
    cur = conn.cursor()
    today = datetime.strptime(a.date, "%Y-%m-%d").date() if a.date else datetime.now(W.TZ).date()
    if a.refresh or a.note:
        n = refresh(cur, a.dry_run)
        W.log(TAG, f"{n} monthly fingerprint rows")
    if a.upcoming is not None:
        for c in upcoming_cycles(a.upcoming, cur, today):
            print(f"{c['date'] or '(month)'}  {c['kind']:<18} {c['label']}  [{c['strength']}: {c['evidence']}]")
    if a.note:
        note = compose_note(cur, today)
        print(note)
        if not a.dry_run:
            from nova_notify import notify
            title, _, body = note.partition("\n")
            W.retry(notify, title.strip("*"), body=body, level="info", category="digest",
                    source="nova_derry_clock", dedup_key=f"derry-{today:%Y-%m}", tag=TAG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
