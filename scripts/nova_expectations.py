#!/usr/bin/env python3
"""nova_expectations.py — alert on work that SHOULD have happened and didn't.

THE PROBLEM THIS EXISTS FOR. Between 2026-07-24 and 07-29 the fleet lost, silently:
  * nightly Postgres dumps        3 days   (job logged "success", wrote empty directories)
  * Burbank scanner feeds        12 days   (systemd "active (running)", every poll refused)
  * Zigbee energy telemetry       2 days   (process alive at 0% CPU, dead DB handle, never raised)
  * Synology -> UNAS sync        12 days   (nothing ran, nothing complained)
  * fishbowl_daily article        4 days   (Errno 112, no alert)
  * the 18:00 ops column         nightly   (published, but future-dated so the site 404ed)
  * search ingest              unknown     ("Done: 0 chunks, 0 items, 0 errors")

Every one was found by a human noticing something felt off. NONE were caught by monitoring.

WHY THE EXISTING TOOLING MISSED ALL OF IT. scheduler_runs covers only what the Nova scheduler
runs. Those failures lived in launchd, systemd, NAS cron and long-running daemons — four
substrates, one instrumented. A query for zigbee_energy, broadcastify, memory_server, nas_mirror
or scanner in scheduler_runs returns ZERO rows. And where a job WAS logged, it logged "success"
while producing nothing, because exit code is a claim about the process, not about the work.

SO THIS DOES NOT WATCH JOBS. IT WATCHES ARTIFACTS.
An expectation says "this evidence should exist and be fresher than N hours" — a table that
should have grown, a file that should be newer, a URL that should answer. It does not care which
substrate produced it, whether a process is running, or what exit code anything returned. Absence
of the artifact IS the alert, which is the one thing every failure above had in common.

Add an expectation with --add; the registry lives in nova_ops.job_expectations.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_expectations (
    name          text PRIMARY KEY,
    kind          text NOT NULL,      -- pg_rows | file_mtime | http | pg_scalar
    target        text NOT NULL,      -- table/query, path, or url
    dsn           text,               -- for pg_* kinds; defaults to nova_ops
    host          text,               -- for file_mtime on another box (ssh)
    max_silence_h numeric NOT NULL,   -- alert if the newest evidence is older than this
    min_units     bigint DEFAULT 0,   -- alert if fewer than this were produced in the window
    note          text,
    enabled       boolean NOT NULL DEFAULT true,
    last_ok       timestamptz,
    last_checked  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);
"""


def conn(dsn=None):
    import psycopg2
    return psycopg2.connect(dsn or DSN)


def measure(e):
    """Return (age_hours, units, detail). age_hours None means 'no evidence at all'."""
    kind, target = e["kind"], e["target"]
    try:
        if kind == "pg_rows":
            # target = "table:timestamp_column"
            tbl, col = target.split(":", 1)
            c = conn(e.get("dsn")); cur = c.cursor()
            cur.execute(f"SELECT max({col}), count(*) FILTER (WHERE {col} > now() - interval '%s hours') "
                        f"FROM {tbl}" % e["max_silence_h"])
            newest, units = cur.fetchone(); c.close()
            if newest is None:
                return None, 0, f"{tbl} is empty"
            age = (datetime.now(timezone.utc) - newest).total_seconds() / 3600
            return age, units or 0, f"newest {newest:%Y-%m-%d %H:%M}"

        if kind == "pg_scalar":
            c = conn(e.get("dsn")); cur = c.cursor()
            cur.execute(target)
            row = cur.fetchone(); c.close()
            v = row[0] if row else 0
            return 0.0, int(v or 0), f"query returned {v}"

        if kind == "file_mtime":
            if e.get("host"):
                r = subprocess.run(["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
                                    e["host"], f'stat -c %Y "{target}" 2>/dev/null'],
                                   capture_output=True, text=True, timeout=45)
                out = r.stdout.strip()
                if not out.isdigit():
                    return None, 0, "file missing or host unreachable"
                mt = int(out)
            else:
                p = Path(target)
                if not p.exists():
                    return None, 0, "file missing"
                mt = int(p.stat().st_mtime)
            age = (time.time() - mt) / 3600
            return age, 1, f"mtime {datetime.fromtimestamp(mt):%Y-%m-%d %H:%M}"

        if kind == "http":
            r = subprocess.run(["curl", "-s", "-m", "15", "-o", "/dev/null",
                                "-w", "%{http_code}", target], capture_output=True, text=True, timeout=30)
            code = r.stdout.strip()
            ok = code.startswith("2") or code.startswith("3")
            return (0.0 if ok else None), (1 if ok else 0), f"HTTP {code}"

        if kind == "http_contains":
            # target: "<url>||<substring>", where {today} expands to YYYY-MM-DD.
            # Checks the PUBLISHED artifact, which is the only thing that proves an
            # article actually reached readers. A directory mtime cannot: it stays
            # fresh from yesterday's file and hides a missed morning for 26h — which
            # is exactly how 2026-07-30's ops articles went unnoticed while stranded
            # on a detached HEAD.
            url, _, needle = target.partition("||")
            needle = needle.replace("{today}", datetime.now().date().isoformat())
            r = subprocess.run(["curl", "-sL", "-m", "20", url],
                               capture_output=True, text=True, timeout=40)
            if r.returncode != 0:
                return None, 0, f"fetch failed rc={r.returncode}"
            hits = r.stdout.count(needle)
            return (0.0 if hits else None), hits, (
                f"found '{needle}'" if hits else f"'{needle}' NOT on page")
    except Exception as ex:
        return None, 0, f"check error: {str(ex).strip()[:90]}"
    return None, 0, f"unknown kind {kind}"


def check(args):
    c = conn(); cur = c.cursor()
    cur.execute(SCHEMA); c.commit()
    cur.execute("""SELECT name, kind, target, dsn, host, max_silence_h, min_units, note
                   FROM job_expectations WHERE enabled ORDER BY name""")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not rows:
        print("  no expectations registered — add some with --add"); return 0

    bad = []
    for e in rows:
        age, units, detail = measure(e)
        limit = float(e["max_silence_h"])
        if age is None:
            status, why = "MISSING", detail
        elif age > limit:
            status, why = "STALE", f"{age:.1f}h old (limit {limit:g}h) — {detail}"
        elif units < (e["min_units"] or 0):
            status, why = "NO WORK", f"only {units} units (expected >={e['min_units']}) — {detail}"
        else:
            status, why = "ok", f"{age:.1f}h, {units} units"
        if status != "ok":
            bad.append((e["name"], status, why, e["note"]))
        if not args.quiet or status != "ok":
            print(f"  [{status:7}] {e['name']:28} {why}")
        cur.execute("UPDATE job_expectations SET last_checked=now()" +
                    (", last_ok=now()" if status == "ok" else "") + " WHERE name=%s", (e["name"],))
    c.commit(); c.close()

    if bad:
        lines = ["🔴 *Work that should have happened and did not*", ""]
        for n, s, w, note in bad:
            lines.append(f"• *{n}* — {s}: {w}")
            if note:
                lines.append(f"   _{note}_")
        lines += ["", "This watches ARTIFACTS, not jobs — so it fires regardless of whether the "
                      "producer was a scheduler task, a launchd agent, a systemd unit, NAS cron or "
                      "a daemon, and regardless of what exit code it claimed."]
        msg = "\n".join(lines)
        print("\n" + msg)
        try:
            import nova_config
            # Missed-work is actionable — route to #nova-alerts, not the feed.
            nova_config.post_both(msg, slack_channel=nova_config.SLACK_ALERTS)
        except Exception as ex:
            print(f"(alert failed: {ex})", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"  === {len(rows)} expectations, all satisfied ===")
    return 0


def add(args):
    c = conn(); cur = c.cursor()
    cur.execute(SCHEMA)
    cur.execute("""INSERT INTO job_expectations (name,kind,target,dsn,host,max_silence_h,min_units,note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (name) DO UPDATE SET kind=EXCLUDED.kind, target=EXCLUDED.target,
                     dsn=EXCLUDED.dsn, host=EXCLUDED.host, max_silence_h=EXCLUDED.max_silence_h,
                     min_units=EXCLUDED.min_units, note=EXCLUDED.note, enabled=true""",
                (args.add, args.kind, args.target, args.dsn, args.host,
                 args.max_silence_h, args.min_units, args.note))
    c.commit(); c.close()
    print(f"  registered {args.add}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--quiet", action="store_true", help="only print problems")
    ap.add_argument("--add", metavar="NAME", help="register/update an expectation")
    ap.add_argument("--kind", choices=["pg_rows", "file_mtime", "http", "pg_scalar"])
    ap.add_argument("--target"); ap.add_argument("--dsn"); ap.add_argument("--host")
    ap.add_argument("--max-silence-h", type=float, dest="max_silence_h", default=26)
    ap.add_argument("--min-units", type=int, dest="min_units", default=0)
    ap.add_argument("--note", default="")
    a = ap.parse_args()
    return add(a) if a.add else check(a)


if __name__ == "__main__":
    sys.exit(main())
