#!/usr/bin/env python3
"""nova_noise_rollup.py — one daily roll-up instead of a notification per event.

Little Mister, 2026-10-15 (plan item #22): negative_space, home_reachability and output_drift
notified per event. home_reachability was designed for one alert per dark room per day and
produced ~450 events per room per week (it re-emitted every run and its level "warn" fell to the
1-hour info dedup window). None of it is an emergency; all of it is worth counting.

So those detectors call record() instead of nova_notify.notify(): one row per (day, source, key)
in noise_rollup, with a count of how many times the condition was seen (the metric is kept, the
dedup is structural — one row per room per day). Once a day --digest posts ONE info event
(category digest -> #nova-digest) listing yesterday's/today's rows; nova's 18:00 daily summary
can include the same lines via summary_lines(cur, day).

    from nova_noise_rollup import record
    record("nova_home_reachability", "homekit-dark-Garage", "HomeKit: Garage unreachable", "warning")

CLI: --digest [--day YYYY-MM-DD] [--dry-run]   |   --show [--day ...]
Table: nova_ops.noise_rollup.  Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCHEMA = """
CREATE TABLE IF NOT EXISTS noise_rollup (
  day date NOT NULL,
  source text NOT NULL,
  key text NOT NULL,
  title text NOT NULL,
  level text NOT NULL DEFAULT 'warning',
  body text,
  count integer NOT NULL DEFAULT 1,
  first_at timestamptz NOT NULL DEFAULT now(),
  last_at timestamptz NOT NULL DEFAULT now(),
  digested_at timestamptz,
  PRIMARY KEY (day, source, key));
"""
MAX_LINES = 40


def log(m: str) -> None:
    print(f"[noise-rollup {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur) -> None:
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, SCHEMA)


def record(source: str, key: str, title: str, level: str = "warning", body: str | None = None,
           day: date | None = None, cur=None) -> bool:
    """Count one sighting. Never raises (a detector must not die on its roll-up). -> True on write."""
    if not source or not key:
        return False
    conn = None
    try:
        if cur is None:
            import psycopg2
            import nova_dsn
            conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"), connect_timeout=5)
            conn.autocommit = True
            cur = conn.cursor()
        ensure_schema(cur)
        cur.execute(
            "INSERT INTO noise_rollup (day, source, key, title, level, body) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (day, source, key) DO UPDATE SET count=noise_rollup.count+1, last_at=now(), "
            "title=EXCLUDED.title, body=coalesce(EXCLUDED.body, noise_rollup.body)",
            (day or date.today(), source, key[:300], (title or key)[:300], level or "warning",
             (body or None) and body[:1000]))
        return True
    except Exception as e:  # noqa: BLE001
        log(f"record failed ({source}/{key}): {e}")
        return False
    finally:
        if conn:
            conn.close()


def render(rows: list, day) -> tuple[str, str]:
    """rows: [(source, title, count, first_at, last_at)] -> (title, body). Pure."""
    by: dict = {}
    for src, title, n, first, last in rows:
        by.setdefault(src, []).append((title, n, first, last))
    head = f"Daily roll-up {day}: {len(rows)} condition(s) from {len(by)} detector(s)"
    lines = []
    for src in sorted(by):
        lines.append(f"*{src}*")
        for title, n, first, last in sorted(by[src], key=lambda r: -r[1]):
            span = f"{first:%H:%M}-{last:%H:%M}" if first and last else ""
            lines.append(f"• {title} — seen {n}x {span}".rstrip())
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES] + [f"… {len(lines) - MAX_LINES} more lines (nova_noise_rollup.py --show)"]
    return head, "\n".join(lines)


def summary_lines(cur, day) -> list:
    """For the 18:00 daily summary: one line per detector."""
    cur.execute("SELECT source, count(*), sum(count) FROM noise_rollup WHERE day=%s GROUP BY 1 ORDER BY 3 DESC", (day,))
    return [f"{src}: {k} condition(s), {n} sighting(s)" for src, k, n in cur.fetchall()]


def digest(cur, day, dry: bool = False, notify=None) -> int:
    cur.execute("SELECT source, title, count, first_at, last_at FROM noise_rollup WHERE day=%s AND digested_at IS NULL "
                "ORDER BY source, count DESC", (day,))
    rows = cur.fetchall()
    if not rows:
        log(f"nothing to roll up for {day}")
        return 0
    title, body = render(rows, day)
    if dry:
        print(title + "\n" + body)
        return len(rows)
    if notify is None:
        from nova_notify import notify
    notify(title, body=body, level="info", category="digest", source="nova_noise_rollup",
           dedup_key=f"noise-rollup-{day}")
    cur.execute("UPDATE noise_rollup SET digested_at=now() WHERE day=%s AND digested_at IS NULL", (day,))
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--digest", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--day", default=None, help="YYYY-MM-DD (default today)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not (a.digest or a.show):
        ap.print_help()
        return 0
    day = date.fromisoformat(a.day) if a.day else date.today()
    import psycopg2
    import nova_dsn
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"), connect_timeout=8)
    try:
        cur = conn.cursor()
        ensure_schema(cur)
        if a.show:
            cur.execute("SELECT source, title, count, first_at, last_at FROM noise_rollup WHERE day=%s "
                        "ORDER BY source, count DESC", (day,))
            t, b = render(cur.fetchall(), day)
            print(t + "\n" + b)
        else:
            digest(cur, day, a.dry_run)
        conn.rollback() if a.dry_run else conn.commit()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
