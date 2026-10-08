#!/usr/bin/env python3
"""nova_speedy_circle.py — SPEEDY'S CIRCLE: catch Nova orbiting between two pulls.

From Asimov's "Runaround": on Mercury, Powell and Donovan send the robot Speedy for selenium.
The order was given casually, so the Second Law pull is weak; Speedy is an expensive model
with a strengthened Third Law, and the pool sits in a hazard. At one radius the two drives
balance exactly, so Speedy circles the pool, reciting nonsense, neither arriving nor leaving.
Pushing harder on either side only moves the circle. Powell breaks it by walking out into
the sun where Speedy can see him in danger: the First Law outranks both pulls and the robot
comes straight to him. (Clarke's "Dial F for Frankenstein" is the cross-system version: the
newly linked phone network wakes and every telephone on Earth rings at once.)

Nova's version, the minimal first version of the spec: one SQL query over the last 7 days of
claude_actions, the autonomy ledger and co-agency proposals. Each row is read as a state for
its target: on (enable/start/load/approve/...) or off (disable/stop/unload/reject/revert/...);
a proposal is "on" when drafted and "off" when rejected, refused, superseded or withdrawn; an
autonomy action that was reverted adds its own "off". Any target that flips at least three
full cycles (on/off/on/off/on/off) is filed to the Buick 8 Logbook as 'oscillation' with the
two pulls marked unknown. Naming the pulls is a later, labelled hypothesis; the fix is never
to add weight to one side but to escalate to the higher principle or ask Little Mister.

Cross-organ mode (Dial F, --loops) reuses the dependency graph from nova_usher_fissure rather
than building its own: it lists feedback loops where one organ's table output is read by
another whose output comes back around.

CLI:   --run [--dry-run]   --loops   --selftest
Writes: unexplained_events via nova_buick8_log (kind 'oscillation'); no table of its own.
service_config: nova_speedy_circle/min_cycles (default 3), nova_speedy_circle/window_days (7)
Schedule: daily 05:40.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

SERVICE = "nova_speedy_circle"
MIN_CYCLES = 3
WINDOW_DAYS = 7
HUB_READERS = 8   # a table read by more scripts than this is shared plumbing, not a loop edge
# PostgreSQL word boundaries (\m \M). Off is tested first, so "disable" never reads as "enable".
OFF = (r"\m(disable[ds]?|stop(ped)?|unload(ed)?|bootout|paused?|remove[ds]?|uninstall(ed)?|revoked?|"
       r"reject(ed)?|retired?|suppress(ed)?|withdraw(n)?|revert(ed)?|rollback|kill(ed)?)\M")
ON = (r"\m(enable[ds]?|start(ed)?|load(ed)?|bootstrap|resumed?|unpause[ds]?|add(ed)?|install(ed)?|"
      r"grant(ed)?|approved?|raised?)\M")
# ponytail: claude_actions 'command' rows carry the raw command as target, so they never line
# up per target; they are skipped until actions carry a normalised target.
SKIP_ACTIONS = ("command", "tool", "agent", "file_read", "staleness-check", "reap")

ORBIT_SQL = """
WITH ev AS (
  SELECT 'claude_actions' AS src, lower(btrim(target)) AS tgt, ts,
         CASE WHEN description ~* %(off)s THEN -1 WHEN description ~* %(on)s THEN 1 END AS st
    FROM claude_actions WHERE ts >= now() - make_interval(days => %(days)s)
     AND action_type <> ALL(%(skip)s) AND coalesce(target, '') <> ''
  UNION ALL
  SELECT 'autonomy_ledger', lower(btrim(target)), ts,
         CASE WHEN action ~* %(off)s THEN -1 WHEN action ~* %(on)s THEN 1 END
    FROM autonomy_ledger WHERE ts >= now() - make_interval(days => %(days)s) AND coalesce(target, '') <> ''
  UNION ALL
  SELECT 'autonomy_ledger', lower(btrim(target)), ts + interval '1 microsecond', -1
    FROM autonomy_ledger WHERE ts >= now() - make_interval(days => %(days)s) AND reverted
     AND coalesce(target, '') <> ''
  UNION ALL
  SELECT 'coagency_proposals', lower(btrim(target_service)), created_at, 1
    FROM coagency_proposals WHERE created_at >= now() - make_interval(days => %(days)s)
     AND coalesce(target_service, '') <> ''
  UNION ALL
  SELECT 'coagency_proposals', lower(btrim(target_service)), decided_at, -1
    FROM coagency_proposals WHERE created_at >= now() - make_interval(days => %(days)s)
     AND coalesce(target_service, '') <> '' AND decided_at IS NOT NULL
     AND status IN ('rejected', 'refused', 'superseded', 'withdrawn')
), s AS (
  SELECT src, tgt, ts, st, lag(st) OVER (PARTITION BY tgt ORDER BY ts) AS prev FROM ev WHERE st IS NOT NULL
)
SELECT tgt, count(*) FILTER (WHERE st <> prev) AS flips, count(*) AS events,
       array_agg(DISTINCT src) AS sources, min(ts) AS first, max(ts) AS last
  FROM s GROUP BY tgt HAVING count(*) FILTER (WHERE st <> prev) >= 2 * %(cycles)s
 ORDER BY flips DESC
"""


def log(m: str) -> None:
    print(f"[speedy-circle {datetime.now():%H:%M:%S}] {m}", flush=True)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a failed read degrades to "nothing found"
        log(f"query failed: {e}")
        return []


def _cfg(cur, key, default):
    try:
        return int(W.get_config(cur, SERVICE, key, default))
    except Exception as e:  # noqa: BLE001 — bad or missing config falls back to the default
        log(f"config {key}: {e}")
        return default


# ── pure ────────────────────────────────────────────────────────────────────

def finding(row) -> dict:
    """One orbit row -> what the Logbook gets. Pulls stay 'unknown' (no guessing in v1)."""
    tgt, flips, events, sources, first, last = row
    return {"target": tgt, "cycles": flips // 2, "flips": flips, "events": events,
            "sources": sorted(sources or []), "first": str(first), "last": str(last), "pulls": "unknown"}


def describe(f: dict) -> str:
    return (f"{f['target']} alternated {f['cycles']} full cycles ({f['flips']} flips over "
            f"{f['events']} events, {', '.join(f['sources'])}) between {f['first'][:16]} and "
            f"{f['last'][:16]}; the two pulls are unknown")


def loops(graph: dict, hub: int = HUB_READERS) -> list:
    """Feedback loops among organs: strongly connected groups of svc/table nodes holding two or
    more services (a script that reads its own table is not a loop). Hub tables are dropped.
    Iterative Tarjan; returns sorted lists of member names."""
    g = {k: {v for v in vs if not v.startswith("node:")} for k, vs in graph.items()
         if not k.startswith("node:") and not (k.startswith("table:") and len(vs) > hub)}
    g = {k: {v for v in vs if v in g} for k, vs in g.items()}
    index, low, on, stack, out, n = {}, {}, set(), [], [], 0
    for root in g:
        if root in index:
            continue
        work = [(root, iter(g[root]))]
        index[root] = low[root] = n
        n += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            w = next(it, None)
            if w is not None:
                if w not in index:
                    index[w] = low[w] = n
                    n += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(g[w])))
                elif w in on:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if sum(c.startswith("svc:") for c in comp) >= 2:
                    out.append(sorted(comp))
    return sorted(out, key=lambda c: (-len(c), c))


# ── run ─────────────────────────────────────────────────────────────────────

def orbits(cur) -> list:
    args = {"off": OFF, "on": ON, "days": _cfg(cur, "window_days", WINDOW_DAYS),
            "cycles": _cfg(cur, "min_cycles", MIN_CYCLES), "skip": list(SKIP_ACTIONS)}
    return [finding(r) for r in _q(cur, ORBIT_SQL, args)]


def run(dry: bool = False) -> list:
    conn = W.connect()
    try:
        cur = conn.cursor()
        found = orbits(cur)
        log(f"{'DRY RUN ' if dry else ''}{len(found)} targets in orbit")
        for f in found:
            print(f"  {describe(f)}")
        if not dry and found:
            from nova_buick8_log import log_unexplained
            day = datetime.now().strftime("%Y-%m-%d")
            for f in found:
                log_unexplained("oscillation", f["target"], describe(f), evidence=f,
                                occurrence_key=day, source="speedy_circle", cur=cur)
            log(f"filed {len(found)} to Buick 8")
        return found
    finally:
        conn.close()


def show_loops() -> int:
    from nova_usher_fissure import build_graph
    found = loops(build_graph())
    print(f"{len(found)} cross-organ loops (hub tables read by >{HUB_READERS} scripts ignored)")
    for c in found:
        print("  " + ", ".join(c))
    return 0


def selftest() -> int:
    py = lambda rx: re.compile(rx.replace(r"\m", r"\b").replace(r"\M", r"\b"), re.I)  # noqa: E731
    assert py(OFF).search("launchctl disable x") and not py(ON).search("launchctl disable x")
    assert py(ON).search("enabled nova-hue") and not py(OFF).search("enabled nova-hue")
    f = finding(("x", 7, 9, ["claude_actions"], "2026-10-01 00:00", "2026-10-07 00:00"))
    assert f["cycles"] == 3 and f["pulls"] == "unknown" and "unknown" in describe(f)
    g = {"svc:a": {"table:t", "node:studio"}, "table:t": {"svc:b"}, "svc:b": {"table:u"},
         "table:u": {"svc:a"}, "svc:c": {"table:v"}, "table:v": {"svc:c"}}
    assert loops(g) == [["svc:a", "svc:b", "table:t", "table:u"]], loops(g)   # c reading itself is no loop
    assert loops(g, hub=0) == []                                             # every table a hub
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="find targets in orbit, file them to Buick 8")
    ap.add_argument("--dry-run", action="store_true", help="with --run: print the orbits, write nothing")
    ap.add_argument("--loops", action="store_true", help="cross-organ loops in Usher's dependency graph")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.run:
        run(dry=a.dry_run)
        return 0
    if a.loops:
        return show_loops()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
