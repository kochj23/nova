#!/usr/bin/env python3
"""nova_flop_slate.py — THE FLOP SLATE: how many of Nova's initiatives were allowed to fail, each quarter.

Inspiration: Peter Bart, "Boffo! How I Learned to Love the Blockbuster and Fear the Bomb" (and the
Tinseltown series built on it). A studio plan budgets four flops in a thirty-film slate; a Sony
executive reads it and asks, "why do we have to make the flops?" Nobody can tell in advance which four
— William Goldman's rule for the whole business: "Nobody knows anything." A slate with no flops is not
a run of genius; it is a slate where nobody took a risk, or nobody is counting honestly.
Little Mister, 2026-10-15: "Add all 9." — after the continuity review found organs reported as
shipped that had produced nothing, and no record anywhere of anything Nova built that did not work.

Once a quarter every initiative is put in one of four boxes:
  hit      produced findings someone acted on: an incident opened from its notifications (or one of them
           acknowledged), or claude_queue items it created
  quiet    runs and produces output (stdout, notifications, memories, fresh rows), but nobody ever acted on it
  dud      no output, always errors, or never ran in the quarter
  retired  disabled in every scheduler file (the YAML comment is kept as the evidence)
Initiatives = every scheduler entry (both YAMLs), every organ named in an organ agent_doc or commissioned
by Jordan that is not scheduled, and every feature wish shipped since 2026-09-01. "Organ" and "output"
mean exactly what nova_mark_to_market.py means by them (shared helpers live there).

The measure, per quarter: FLOP RATE = (dud + retired) / total, and the dud list. A flop rate under 5%
across more than 50 initiatives is flagged as "grade inflation or no real risk-taking". Duds are listed
as RETIREMENT CANDIDATES for Jordan; this organ never retires, disables or edits anything.

CLI:   --scheduled         daily; acts once a quarter (first run after a quarter ends with no rows for it)
       --quarter 2026Q3    which quarter (default: the last completed one; the current one = to date)
       --dry-run           classify and print; write and notify nothing
       --force             re-classify a quarter that already has rows
       --report [N]        last N quarters: counts, flop rate, flag, duds
       --selftest          offline checks of the pure functions
Table: flop_slate (quarter, initiative) primary key.

MERGED 2026-10-15 (consolidation item #13; Claude, as integrator, approved — no coverage lost): one
ledger. The slate is now `nova_mark_to_market.py --slate`, and its daily/quarterly step runs inside the
single `mark_to_market --scheduled` entry (scheduler-core, daily 05:40). The logic and the flop_slate
table stay here (run_slate); main() is a thin wrapper so `nova_flop_slate.py ...` keeps working.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_mark_to_market as mtm  # noqa: E402  (shared inventory + output-signal helpers)

SERVICE = "nova_flop_slate"
WISHES_SINCE = date(2026, 9, 1)
FLAG_MIN_INITIATIVES = 50
FLAG_MAX_FLOP_RATE = 0.05
TABLE_PROBE_BUDGET_S = 120
GIT_LOOKUPS_MAX = 40            # never-ran tasks whose YAML add date is looked up in git
ORDER = ("hit", "quiet", "dud", "retired")

SCHEMA = """
CREATE TABLE IF NOT EXISTS flop_slate (
    quarter     text NOT NULL,          -- '2026Q3'
    initiative  text NOT NULL,          -- 'task:<id>' | 'organ:<script>' | 'wish:<id>'
    class       text NOT NULL CHECK (class IN ('hit','quiet','dud','retired')),
    evidence    jsonb NOT NULL DEFAULT '{}',
    measured_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (quarter, initiative)
);
"""


def log(msg: str) -> None:
    print(f"[flop-slate] {msg}", file=sys.stderr)


# ---------------------------------------------------------------- pure

def quarter_label(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def quarter_bounds(label: str) -> tuple[date, date]:
    """'2026Q3' -> (2026-07-01, 2026-10-01). Raises ValueError on a bad label. Pure."""
    m = re.fullmatch(r"(\d{4})Q([1-4])", (label or "").strip().upper())
    if not m:
        raise ValueError(f"bad quarter {label!r} (want e.g. 2026Q3)")
    y, q = int(m.group(1)), int(m.group(2))
    start = date(y, 3 * q - 2, 1)
    end = date(y + (q == 4), 1 if q == 4 else 3 * q + 1, 1)
    return start, end


def previous_quarter(today: date) -> str:
    start, _ = quarter_bounds(quarter_label(today))
    return quarter_label(start - timedelta(days=1))


def due(have_rows: bool, force: bool = False) -> bool:
    return force or not have_rows


def classify(sig: dict, retired_note: str | None = None, shared_fresh: bool = False) -> tuple[str, str]:
    """(class, reason) for one initiative from its output signals. Pure.
    sig keys as nova_mark_to_market.unit_signals, plus 'queue' (claude_queue items it created)."""
    if retired_note:
        return "retired", retired_note
    tasks = sig.get("tasks") or []
    out = mtm.realised(sig) or bool(sig.get("via_host")) or shared_fresh
    if tasks and not sig.get("runs") and not out:
        return "dud", "never ran in the quarter"
    if tasks and sig.get("runs") and not sig.get("ok"):
        return "dud", f"always errors ({sig['runs']} runs, 0 succeeded)"
    if not out:
        return "dud", "no output this slate can attribute: " + mtm.output_summary(sig)
    acted = []
    if sig.get("incidents"):
        acted.append(f"{sig['incidents']} incident(s) opened from its notifications"
                     + (f", {sig['acked']} acknowledged" if sig.get("acked") else ""))
    if sig.get("queue"):
        acted.append(f"{sig['queue']} claude_queue item(s) created")
    if acted:
        return "hit", "; ".join(acted)
    how = (f"output via {sig['via_host']}" if sig.get("via_host") and not mtm.realised(sig)
           else "writes to shared tables only" if shared_fresh and not mtm.realised(sig)
           else mtm.output_summary(sig))
    return "quiet", f"{how}; nothing acted on"


def flop_stats(classes: list[str]) -> dict:
    """counts, flop rate and the inflation flag. Pure."""
    counts = {k: classes.count(k) for k in ORDER}
    total = len(classes)
    rate = round((counts["dud"] + counts["retired"]) / total, 4) if total else None
    flag = bool(total > FLAG_MIN_INITIATIVES and rate is not None and rate < FLAG_MAX_FLOP_RATE)
    return {"total": total, **counts, "flop_rate": rate,
            "flag": "grade inflation or no real risk-taking" if flag else None}


def best(classes: list[str]) -> str:
    """The best outcome among an initiative's parts (hit > quiet > dud). Pure."""
    for k in ORDER:
        if k in classes:
            return k
    return "dud"


def written_tables(src: str | None) -> list[str]:
    """Tables a script inserts into (they may be shared; used only for silent jobs). Pure."""
    out = []
    for t in re.findall(r"(?i)\bINSERT\s+INTO\s+([a-z_][\w]*(?:\.[a-z_]\w*)?)", src or ""):
        if t.lower() not in out:
            out.append(t.lower())
    return out


def render(quarter: str, stats: dict, duds: list[dict], prev: dict | None = None) -> str:
    rate = "—" if stats["flop_rate"] is None else f"{stats['flop_rate'] * 100:.1f}%"
    was = (f" (last quarter {prev['flop_rate'] * 100:.1f}%)"
           if prev and prev.get("flop_rate") is not None else "")
    lines = [f"Flop slate {quarter}: {stats['total']} initiatives — {stats['hit']} hit, {stats['quiet']} quiet, "
             f"{stats['dud']} dud, {stats['retired']} retired. Flop rate {rate}{was}."]
    if stats.get("flag"):
        lines.append(f"FLAG: under {FLAG_MAX_FLOP_RATE:.0%} flops across more than {FLAG_MIN_INITIATIVES} "
                     f"initiatives — {stats['flag']}.")
    if duds:
        lines.append("")
        lines.append(f"Retirement candidates ({len(duds)} duds; nothing is retired automatically):")
        lines += [f"• {d['initiative']}: {d['reason']}" for d in duds[:25]]
        if len(duds) > 25:
            lines.append(f"• … {len(duds) - 25} more (nova_flop_slate.py --report)")
    lines.append("")
    lines.append("Nobody knows anything. The flops are how we find out.")
    return "\n".join(lines)


# ---------------------------------------------------------------- gather (DB / git)

def first_runs(oc) -> dict[str, int]:
    oc.execute("SELECT task_id, min(started_at) FROM scheduler_runs GROUP BY 1")
    return dict(oc.fetchall())


def queue_counts(oc, since: datetime, until: datetime) -> dict[str, int]:
    oc.execute("SELECT session_id, count(*) FROM claude_queue WHERE created_at >= %s AND created_at < %s "
               "GROUP BY 1", (since, until))
    out: dict[str, int] = {}
    for s, n in oc.fetchall():
        k = mtm.norm(s)
        if k:
            out[k] = out.get(k, 0) + n
    return out


def yaml_added(task_id: str, repo_dir: Path = mtm.REPO_DIR) -> date | None:
    """When the task's YAML entry first appeared in git (None = unknown / no git)."""
    try:
        r = subprocess.run(["git", "-C", str(repo_dir), "log", "--reverse", "--format=%cs", "-S", f"  {task_id}:",
                            "--", "config/scheduler-core.yaml", "config/scheduler.yaml"],
                           capture_output=True, text=True, timeout=30)
        first = r.stdout.split()[0] if r.returncode == 0 and r.stdout.split() else None
        return date.fromisoformat(first) if first else None
    except Exception:  # noqa: BLE001
        return None


def files_added(repo_dir: Path = mtm.REPO_DIR) -> dict[str, date]:
    """scripts/<name> -> date first added in git (empty when git is unavailable)."""
    try:
        r = subprocess.run(["git", "-C", str(repo_dir), "log", "--diff-filter=A", "--name-only", "--format=@%cs",
                            "--", "scripts/"], capture_output=True, text=True, timeout=120)
    except Exception:  # noqa: BLE001
        return {}
    out, cur = {}, None
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        if line.startswith("@"):
            cur = line[1:]
        elif line.strip() and cur:
            out[Path(line).name] = date.fromisoformat(cur)     # log is newest first: oldest add wins
    return out


def to_dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day).astimezone()


def build_slate(oc, mc, quarter: str, now: datetime | None = None) -> list[dict]:
    """Classify every initiative for the quarter. Read-only. Returns [{initiative, class, evidence}]."""
    qs, qe = quarter_bounds(quarter)
    since = to_dt(qs)
    until = min(to_dt(qe), now or datetime.now().astimezone())
    sched = mtm.load_scheduler()
    texts = mtm.script_texts()
    wishes = mtm.load_wishes(oc)
    inv = mtm.organ_inventory(oc, texts, sched, wishes)
    runs = mtm.run_stats(oc, since, until)
    events = mtm.event_stats(oc, since, until)
    queue = queue_counts(oc, since, until)
    firsts = first_runs(oc)
    added = files_added()
    hosts = mtm.live_hosts(sched, runs)

    units, meta = {}, {}
    git_left = GIT_LOOKUPS_MAX
    for tid, s in sorted(sched.items()):
        first = firsts.get(tid)
        if first is not None and first >= mtm.ms(until):
            continue                                        # launched after the quarter
        if s["enabled"] and first is None:
            when = yaml_added(tid) if git_left > 0 else None
            git_left -= 1
            if when and when >= qe:
                continue                                    # added to the YAML after the quarter
        key = f"task:{tid}"
        units[key] = {"script": s.get("script"), "tasks": [tid] if s["enabled"] else [], "mem_sources": []}
        meta[key] = {"retired": None if s["enabled"] else f"disabled: {s['disabled_note']}", "script": s.get("script"),
                     "files": s["files"]}
    for script, e in sorted(inv.items()):
        if e["tasks"] or any(sched.get(t, {}).get("script") == script for t in sched):
            continue                                        # already on the slate as a task
        if added.get(script) and added[script] >= qe:
            continue
        key = f"organ:{script}"
        units[key] = {"script": script, "tasks": [], "mem_sources": e["mem_sources"]}
        meta[key] = {"retired": None, "script": script, "booked_via": e["from"]}

    sigs = mtm.unit_signals(oc, mc, units, texts, since, until, runs=runs, events=events,
                            budget_s=TABLE_PROBE_BUDGET_S)
    deadline = time.monotonic() + TABLE_PROBE_BUDGET_S
    shared_cache: dict[str, bool | None] = {}
    slate = []
    for key, u in units.items():
        sig = sigs[key]
        names = {mtm.norm(u["script"])} | {mtm.norm(t) for t in u["tasks"]}
        sig["queue"] = sum(queue.get(n, 0) for n in names if n)
        script = u.get("script")
        if not mtm.realised(sig) and script and not u["tasks"]:
            host = next((h for h in sorted(hosts) if h != script and mtm.imported_by(script, texts.get(h, ""))), None)
            sig["via_host"] = host
        shared = False
        if not mtm.realised(sig) and sig.get("ok") and script and time.monotonic() < deadline:
            for t in written_tables(texts.get(script)):
                if t not in shared_cache:
                    shared_cache[t] = mtm.table_fresh(oc, t, since, until)
                if shared_cache[t]:
                    shared = True
                    sig["shared_table"] = t
                    break
        cls, reason = classify(sig, meta[key]["retired"], shared)
        slate.append({"initiative": key, "class": cls, "reason": reason,
                      "evidence": {"reason": reason, **{k: v for k, v in meta[key].items() if v and k != "retired"},
                                   **{k: sig.get(k) for k in ("runs", "ok", "ok_out", "events", "incidents", "acked",
                                                              "queue", "memories", "tables", "via_host",
                                                              "shared_table") if sig.get(k)}}})

    by_script = {}
    for row in slate:
        sc = row["evidence"].get("script")
        if sc:
            by_script.setdefault(sc, []).append(row["class"])
    oc.execute("SELECT doc_type, content FROM agent_docs")
    docs = dict(oc.fetchall())
    files = mtm.repo_files() | set(texts)
    commits = mtm.git_log_text()
    for w in wishes:
        shipped = mtm.wish_shipped_on(w)
        if w["status"] not in ("shipped", "retired") or not shipped or shipped < WISHES_SINCE or shipped >= qe:
            continue
        key = f"wish:{w['id']}"
        if w["status"] == "retired":
            slate.append({"initiative": key, "class": "retired", "reason": "wish retired",
                          "evidence": {"reason": "wish retired", "title": w["title"]}})
            continue
        ev = mtm.wish_evidence(w, files, texts, set(sched), set(docs), docs, commits)
        parts = [c for tok in mtm.script_tokens(json.dumps(w["lineage"])) for c in by_script.get(tok, [])]
        if not ev:
            cls, reason = "dud", "marked shipped; no code, task, doc or commit references it"
        elif parts:
            cls = best(parts)
            reason = f"best of its organs: {cls}"
        else:
            cls, reason = "quiet", "code exists; no measurable output channel of its own"
        slate.append({"initiative": key, "class": cls, "reason": reason,
                      "evidence": {"reason": reason, "title": w["title"], "shipped": str(shipped), "code": ev[:4]}})
    return slate


# ---------------------------------------------------------------- run

def have_quarter(oc, quarter: str) -> bool:
    oc.execute("SELECT to_regclass('public.flop_slate') IS NOT NULL")
    if not oc.fetchone()[0]:
        return False
    oc.execute("SELECT 1 FROM flop_slate WHERE quarter = %s LIMIT 1", (quarter,))
    return oc.fetchone() is not None


def previous_stats(oc, quarter: str) -> dict | None:
    oc.execute("SELECT to_regclass('public.flop_slate') IS NOT NULL")
    if not oc.fetchone()[0]:
        return None
    oc.execute("SELECT class FROM flop_slate WHERE quarter = (SELECT max(quarter) FROM flop_slate WHERE quarter < %s)",
               (quarter,))
    rows = [c for (c,) in oc.fetchall()]
    return flop_stats(rows) if rows else None


def write(oc, quarter: str, slate: list[dict]) -> None:
    for r in slate:
        oc.execute("""INSERT INTO flop_slate (quarter, initiative, class, evidence) VALUES (%s, %s, %s, %s)
                      ON CONFLICT (quarter, initiative) DO UPDATE SET class=EXCLUDED.class,
                        evidence=EXCLUDED.evidence, measured_at=now()""",
                   (quarter, r["initiative"], r["class"], json.dumps(r["evidence"], default=str)))


def duds_of(slate: list[dict]) -> list[dict]:
    return sorted((r for r in slate if r["class"] == "dud"), key=lambda r: r["initiative"])


def report(oc, n: int) -> None:
    oc.execute("SELECT to_regclass('public.flop_slate') IS NOT NULL")
    if not oc.fetchone()[0]:
        print("No flop slate yet (table not created; the first scheduled run creates it).")
        return
    oc.execute("SELECT DISTINCT quarter FROM flop_slate ORDER BY 1 DESC LIMIT %s", (n,))
    for (q,) in oc.fetchall():
        oc.execute("SELECT initiative, class, evidence->>'reason' FROM flop_slate WHERE quarter=%s ORDER BY 1", (q,))
        rows = oc.fetchall()
        st = flop_stats([c for _, c, _ in rows])
        print(render(q, st, [{"initiative": i, "reason": r} for i, c, r in rows if c == "dud"]))
        print()


def selftest() -> int:
    assert quarter_bounds("2026Q4") == (date(2026, 10, 1), date(2027, 1, 1))
    assert previous_quarter(date(2026, 10, 15)) == "2026Q3" and previous_quarter(date(2027, 2, 1)) == "2026Q4"
    assert classify({"tasks": ["a"], "runs": 0})[0] == "dud"
    assert classify({"tasks": ["a"], "runs": 3, "ok": 0})[0] == "dud"
    assert classify({"tasks": ["a"], "runs": 3, "ok": 3, "ok_out": 3, "incidents": 1})[0] == "hit"
    assert classify({"tasks": ["a"], "runs": 3, "ok": 3, "ok_out": 3})[0] == "quiet"
    assert classify({}, retired_note="disabled: retired")[0] == "retired"
    st = flop_stats(["hit"] * 60)
    assert st["flop_rate"] == 0 and st["flag"]
    assert flop_stats(["dud"] * 10 + ["hit"] * 50)["flag"] is None
    assert written_tables("INSERT INTO telemetry.x (a) ...; insert into foo") == ["telemetry.x", "foo"]
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    """Thin wrapper (merge 2026-10-15): same options as before; the survivor is mark_to_market --slate."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(x in argv for x in ("--selftest", "-h", "--help")):
        log("merged into nova_mark_to_market.py --slate on 2026-10-15; running the slate")
    return run_slate(argv)


def run_slate(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nova_mark_to_market.py --slate", description=__doc__.split("\n")[0])
    ap.add_argument("--scheduled", action="store_true", help="daily; acts once a quarter")
    ap.add_argument("--quarter", default=None, help="e.g. 2026Q3 (default: last completed quarter)")
    ap.add_argument("--dry-run", action="store_true", help="classify and print; write and notify nothing")
    ap.add_argument("--force", action="store_true", help="re-classify a quarter that already has rows")
    ap.add_argument("--report", nargs="?", const=4, type=int, metavar="N", help="last N quarters")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    quarter = a.quarter or previous_quarter(date.today())
    try:
        quarter_bounds(quarter)
    except ValueError as e:
        ap.error(str(e))
    t0 = time.monotonic()
    oc_conn = mtm.connect("nova_ops")
    oc = oc_conn.cursor()
    try:
        if a.report is not None:
            report(oc, max(1, a.report))
            return 0
        if not a.dry_run and not due(have_quarter(oc, quarter), a.force):
            print(f"[flop-slate] {quarter} already on the slate; nothing to do")
            return 0
        mc_conn = mtm.connect("nova_memories")
        try:
            slate = build_slate(oc, mc_conn.cursor(), quarter)
        finally:
            mc_conn.close()
        st = flop_stats([r["class"] for r in slate])
        duds = duds_of(slate)
        body = render(quarter, st, [{"initiative": d["initiative"], "reason": d["reason"]} for d in duds],
                      previous_stats(oc, quarter))
        if a.dry_run:
            print(body)
            print("\nRetired:")
            for r in slate:
                if r["class"] == "retired":
                    print(f"  {r['initiative']}: {r['reason']}")
            print("\nHits:")
            for r in slate:
                if r["class"] == "hit":
                    print(f"  {r['initiative']}: {r['reason']}")
            print(f"\n[dry-run] {quarter}: {json.dumps(st)} — {time.monotonic() - t0:.1f}s, nothing written")
            return 0
        import nova_pg_schema
        nova_pg_schema.safe_ddl(oc, SCHEMA)
        write(oc, quarter, slate)
        from nova_notify import notify
        notify(f"Flop slate {quarter}: flop rate "
               f"{'—' if st['flop_rate'] is None else format(st['flop_rate'] * 100, '.1f') + '%'}, {st['dud']} duds",
               body=body, level="warning" if st["flag"] else "info", category="flop_slate",
               dedup_key=f"flop_slate:{quarter}", meta={"quarter": quarter, **st})
        log(f"{quarter}: {json.dumps(st)} ({time.monotonic() - t0:.1f}s)")
        return 0
    finally:
        oc_conn.close()


if __name__ == "__main__":
    sys.exit(main())
