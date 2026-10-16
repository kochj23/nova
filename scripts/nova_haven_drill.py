#!/usr/bin/env python3
"""nova_haven_drill.py — THE HAVEN DRILL: prove the house runs with Nova's motors off.

Little Mister, 2026-10-15 (after Nova read Maximum Overdrive, 1986): "Do it all!"
At the end of the film the survivors sail to Haven, an island where no motor vehicles are allowed. Nova's
version: once a quarter, list everything she automates in the house, the manual fallback for each, and
ask Jordan for a window in which he turns her off (her existing kill switch) and checks, by hand, that the
house still works. A function with no recorded manual fallback is itself a finding.

SAFETY (the reason this organ exists at all is to be trusted with the kill switch):
  * Nothing scheduled ever flips the kill switch. --scheduled only opens the quarter's drill, builds the
    checklist and asks Jordan for a window. --live refuses under --scheduled and without --confirm.
  * There is ONE kill switch: nova_autonomy_safety (tripwire file + service_config autonomy/kill_switch).
    The drill engages it with nova_autonomy_safety.engage_kill(note='haven-drill:<id>') and restores it the
    same way that module's `unkill` CLI does. It never invents a second switch.
  * OWNERSHIP: the drill clears only a switch it set. It refuses to start if the switch is already
    engaged, and on restore it removes the file / clears the flag only if they still carry its own
    marker. If someone engaged the switch for real during the window, that engagement stands.
  * BOUNDED: window default 30 min, capped at 2 h. Restore is guaranteed by, in order: the finally block,
    an in-process timer at the deadline, an atexit hook, SIGTERM/SIGHUP/SIGINT handlers, a detached
    restore-at-deadline process that survives this one dying, and --watchdog (cheap, schedulable) for a
    reboot in the middle of a drill.
  * FAIL-SAFE: any error during a live drill restores the switch. A restore that fails posts a critical.

Checklist sources (all real, read-only): the automation engine's rule_* functions that actuate
(nova_automation_engine.py), every script that calls the Proteus guards physical_guard / scene_guard /
_actuation_ok (the guarded actuators), the HomeKit scene shell path, scheduled tasks whose script actuates
devices (config/scheduler*.yaml), the notifications that page Jordan (telemetry.events sent critical, 30d)
and the daily briefs. Manual fallbacks come from house_facts (entity 'haven:<key>', attr
'manual_fallback'), from agent_docs lines that say what works without Nova, and from the Butlerian
Ledger's Lagash runbooks. A drill result 'worked' with a "how" note is written back to house_facts.

Measures: % of automated functions with a VERIFIED manual fallback (worked by hand in the latest live
drill); last drill date. Table haven_drills.

BUTLERIAN SECTION (merged 2026-10-15, consolidation item #16 — approved by Claude for Little Mister):
the Butlerian Ledger (nova_butlerian_ledger.py) is now a section of this organ. Its logic and tables
(butlerian_weekly, butlerian_drills) stay in that module; this one runs it:
  --butlerian [--weeks N] [--dry-run]        the weekly share ("how much of his judgement moved to Nova")
  --aub-offer [--dry-run]                    Aub's Drill: the quarterly offer (no-op without consent)
  --aub-record TASK --result R [--note T]    record a hand drill he ran
--report carries a "Butlerian Ledger" section (latest week, trend, Aub's Drill state). None of these
paths can touch the kill switch: they never reach engage()/live(); --live keeps its own refusals.

CLI: --scheduled | --dry-run | --report | --selftest | --watchdog
     --butlerian | --aub-offer | --aub-record TASK --result R   (the Butlerian section)
     --schedule-window "YYYY-MM-DD HH:MM"         record the window Jordan picked
     --live --confirm [--window-min 30] [--results-file F] [--drill-id N]   HUMAN-RUN ONLY
Run --live on the host that runs the automation engine (the tripwire file is per-host).
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import ast
import atexit
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import nova_autonomy_safety as S  # noqa: E402  (the ONE kill switch; read/engage only)

DEFAULT_WINDOW_MIN = 30
MAX_WINDOW_MIN = 120
MIN_WINDOW_MIN = 5
RESULTS = ("worked", "failed", "skipped")
NO_FALLBACK = "no manual fallback recorded"
MARK = "haven-drill:{id}"
OPEN_STATES = ("awaiting_window", "scheduled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS haven_drills (
  id                  bigserial PRIMARY KEY,
  quarter             text NOT NULL,
  opened_at           timestamptz NOT NULL DEFAULT now(),
  scheduled_for       timestamptz,
  status              text NOT NULL DEFAULT 'awaiting_window'
                      CHECK (status IN ('awaiting_window','scheduled','live','completed','aborted','cancelled')),
  checklist           jsonb NOT NULL DEFAULT '[]'::jsonb,
  results             jsonb NOT NULL DEFAULT '{}'::jsonb,
  kill_switch_flipped boolean NOT NULL DEFAULT false,
  started_at          timestamptz,
  deadline            timestamptz,
  restored_at         timestamptz,
  restore_detail      jsonb,
  host                text,
  notes               text);
CREATE INDEX IF NOT EXISTS haven_drills_quarter ON haven_drills (quarter, opened_at DESC);
"""

DOMAINS = {   # domain -> word prefixes that put an item, or a fallback line, in it (first domain wins)
    "lights": ("light", "lamp", "hue", "lutron"),
    "devices": ("patio", "strip", "outlet", "plug", "power", "accessor"),
    "climate": ("climate", "heat", "thermostat", "weather"),
    "scenes": ("scene", "homekit", "shortcut"),
    "network": ("network", "dns", "unifi", "wifi", "quarantine"),
    "services": ("restart", "service", "fleet", "daemon", "co-agency", "coagency"),
    "notifications": ("alert", "notification", "brief", "page", "slack"),
}
# A fallback line has to say, about Nova, that the thing works without her. "by hand" alone is not enough
# (agent_docs is full of "test it by hand" lines that are not a fallback for anything).
FALLBACK_PHRASE = re.compile(r"without nova|independent of nova|manually controllable|reached directly|"
                             r"nova dying (?:only|changes nothing)|stays? in (?:its|their) last state", re.I)
ACTUATE_RX = re.compile(r"scenes/execute|_shortcuts_run\(|run_scene\(|services/(?:light|switch|scene|script)/|"
                        r"/lights/[^\"']*/state|set_power\(|nova_home_control\.py|homekit_scene\.sh|execute_scene\(")
GUARD_CALL = re.compile(r"\b(physical_guard|scene_guard|_actuation_ok)\(")
GUARD_HOMES = {"nova_safety_guards.py", "nova_autonomy_safety.py", "nova_haven_drill.py"}


def log(m: str) -> None:
    print(f"[haven-drill {datetime.now():%H:%M:%S}] {m}", flush=True)


# ── pure ────────────────────────────────────────────────────────────────────

def clamp_window(minutes) -> int:
    """Window in minutes, default 30, never more than 2 h, never less than 5. Pure."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        m = DEFAULT_WINDOW_MIN
    return max(MIN_WINDOW_MIN, min(MAX_WINDOW_MIN, m))


def quarter(ts: datetime) -> str:
    return f"{ts.year}-Q{(ts.month - 1) // 3 + 1}"


def _has(text: str, words) -> bool:
    t = (text or "").lower().replace("_", " ")
    return any(re.search(rf"\b{re.escape(w)}", t) for w in words)


def domain_of(text: str) -> str | None:
    for d, words in DOMAINS.items():
        if _has(text, words):
            return d
    return None


def _label(line: str) -> str:
    """'Lights: Hue bridges work without Nova' -> 'Lights'; '' when the line has no short label. Pure."""
    head = line.split(":", 1)[0] if ":" in line else ""
    return head if 0 < len(head) <= 40 and len(head.split()) <= 8 else ""


def item(key, kind, title, detail="", domain=None) -> dict:
    return {"key": key, "kind": kind, "title": title, "detail": detail[:300],
            "domain": domain or domain_of(f"{title} {detail}"), "fallback": None, "fallback_source": None}


def fallback_lines(docs: dict) -> list:
    """(doc_type, line) for every agent_docs line that says what works without Nova. Pure."""
    out = []
    for doc, content in docs.items():
        for line in str(content or "").splitlines():
            s = line.strip(" -*\t")
            if 20 < len(s) and FALLBACK_PHRASE.search(s):
                out.append((doc, s[:400]))
    return out


def attach_fallbacks(items: list, facts: dict, lines: list, runbooks: dict) -> list:
    """Fill each item's fallback: explicit house_facts first, then an agent_docs line in its domain, then a
    Lagash runbook in its domain. Otherwise NO_FALLBACK (a finding). Pure."""
    for it in items:
        if facts.get(it["key"]):
            it["fallback"], it["fallback_source"] = facts[it["key"]], "house_facts"
            continue
        words = DOMAINS.get(it["domain"] or "", ())
        hit = next(((d, l) for d, l in lines if _has(_label(l) or l, words)), None) if words else None
        if hit:
            it["fallback"], it["fallback_source"] = hit[1], f"agent_docs:{hit[0]}"
            continue
        rb = next(((k, v) for k, v in runbooks.items() if _has(v, words)), None) if words else None
        if rb:
            it["fallback"], it["fallback_source"] = f"Lagash runbook '{rb[0]}': {rb[1]}", "runbook"
            continue
        it["fallback"], it["fallback_source"] = NO_FALLBACK, None
    return items


def automation_rules(src: str) -> list:
    """Actuating rule_* functions of the automation engine (name, docstring). A rule actuates when it calls
    _actuation_ok, or calls a helper that does (hue_set_light, _hk_power, run_scene...). Pure (AST)."""
    tree = ast.parse(src)
    fns = {n.name: ast.get_source_segment(src, n) or "" for n in tree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    helpers = {"_actuation_ok"} | {n for n, body in fns.items()
                                   if n != "_actuation_ok" and "_actuation_ok(" in body and not n.startswith("rule_")}
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("rule_"):
            body = fns[node.name]
            if any(re.search(rf"\b{re.escape(h)}\b", body) for h in helpers):   # call or callback
                out.append((node.name, (ast.get_docstring(node) or "").split("\n")[0]))
    return out


def guarded_functions(src: str) -> list:
    """Names of functions in a script that call a Proteus guard. Pure."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith(("physical_guard",
                                                                                                  "scene_guard")):
            seg = ast.get_source_segment(src, node) or ""
            if GUARD_CALL.search(seg) and node.name != "_actuation_ok":
                out.append(node.name)
    return sorted(set(out))


def summarize(checklist: list, results: dict) -> dict:
    """Findings + measures for one drill. Pure."""
    n = len(checklist)
    no_fb = [i["key"] for i in checklist if (i.get("fallback") or NO_FALLBACK) == NO_FALLBACK]
    worked = [k for k, r in results.items() if r.get("result") == "worked"]
    failed = [k for k, r in results.items() if r.get("result") == "failed"]
    keys = {i["key"] for i in checklist}
    verified = [k for k in worked if k in keys]
    return {"items": n, "no_fallback": no_fb, "worked": worked, "failed": failed,
            "untested": sorted(keys - set(results)),
            "recorded_fallback_pct": round(100.0 * (n - len(no_fb)) / n, 1) if n else 0.0,
            "verified_pct": round(100.0 * len(verified) / n, 1) if n else 0.0}


def parse_results(data) -> dict:
    """Results file -> {key: {result, how}}. Accepts {key: "worked"} or {key: {result, how}} or a list. Pure."""
    if isinstance(data, list):
        data = {d.get("key"): d for d in data if isinstance(d, dict)}
    out = {}
    for k, v in (data or {}).items():
        v = {"result": v} if isinstance(v, str) else dict(v or {})
        r = str(v.get("result", "")).lower()
        if k and r in RESULTS:
            out[str(k)] = {"result": r, "how": str(v.get("how", ""))[:400]}
    return out


def render(drill: dict, summ: dict) -> str:
    """Plain report text. Pure."""
    lines = [f"Haven Drill #{drill.get('id')} ({drill.get('quarter')}, {drill.get('status')}): "
             f"{summ['items']} things Nova automates."]
    lines.append(f"Manual fallback recorded for {summ['recorded_fallback_pct']}%; verified by hand "
                 f"{summ['verified_pct']}%.")
    by = {i["key"]: i for i in drill.get("checklist") or []}
    if summ["no_fallback"]:
        lines.append("No manual fallback recorded:")
        lines += [f"  • {by[k]['title']}" for k in summ["no_fallback"] if k in by]
    if summ["failed"]:
        lines.append("Failed by hand:")
        lines += [f"  • {by.get(k, {}).get('title', k)}" for k in summ["failed"]]
    return "\n".join(lines)


# ── checklist (real data, read-only) ────────────────────────────────────────

def _read(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def build_checklist(cur, scripts_dir: Path = HERE) -> tuple:
    """-> (items, notes). Every source is read-only; one that fails is noted, not fatal."""
    items, notes = [], []
    eng = _read(scripts_dir / "nova_automation_engine.py")
    try:
        for name, doc in automation_rules(eng):
            items.append(item(f"automation:{name}", "automation", f"Automation rule {name[5:]}: {doc}", doc))
    except Exception as e:  # noqa: BLE001
        notes.append(f"automation engine unreadable: {e}")
    for f in sorted(scripts_dir.glob("nova_*.py")):
        if f.name in GUARD_HOMES or f.name == "nova_automation_engine.py":
            continue
        src = _read(f)
        if not GUARD_CALL.search(src):
            continue
        fns = guarded_functions(src)
        doc = (src.split('"""')[1].strip().splitlines() or [""])[0] if src.count('"""') >= 2 else ""
        items.append(item(f"actuator:{f.stem}", "guarded_actuator",
                          f"Guarded actuator {f.name} ({', '.join(fns[:4]) or 'guard call'})", doc))
    sh = scripts_dir / "nova_homekit_scene.sh"
    if sh.exists():
        items.append(item("actuator:homekit_scene_sh", "guarded_actuator", "HomeKit scene runner (shell path)",
                          "runs HomeKit scenes after scene-check", domain="scenes"))
    try:
        import yaml
        sched = {}
        for y in sorted((scripts_dir.parent / "config").glob("scheduler*.yaml")):
            for tid, t in ((yaml.safe_load(_read(y)) or {}).get("tasks") or {}).items():
                if isinstance(t, dict) and t.get("enabled", True) and t.get("script"):
                    sched[tid] = str(t["script"]).split()[0]
        hits = [(tid, s) for tid, s in sorted(sched.items()) if ACTUATE_RX.search(_read(scripts_dir / s))]
        for tid, s in hits:
            items.append(item(f"scheduled:{tid}", "scheduled_task", f"Scheduled task {tid} ({s})"))
        notes.append(f"{len(hits)} of {len(sched)} enabled scheduled tasks actuate devices")
    except Exception as e:  # noqa: BLE001
        notes.append(f"scheduler config unreadable: {e}")
    try:   # autocommit connection: a failed read leaves nothing to roll back
        cur.execute("""SELECT coalesce(category,'(none)'), count(*) FROM telemetry.events
                       WHERE status = 'sent' AND level = 'critical' AND ts > now() - interval '30 days'
                       GROUP BY 1 HAVING count(*) >= 3 ORDER BY 2 DESC LIMIT 8""")
        for cat, n in cur.fetchall():
            items.append(item(f"notify:{cat}", "notification", f"Critical alerts '{cat}' ({n} sent in 30d)",
                              domain="notifications"))
        cur.execute("""SELECT task_id, count(*) FROM scheduler_runs WHERE task_id ~ '(brief|bluf)$'
                       AND status = 'success' AND started_at > extract(epoch FROM now() - interval '7 days') * 1000
                       GROUP BY 1 ORDER BY 1""")
        for tid, n in cur.fetchall():
            items.append(item(f"notify:{tid}", "notification", f"Daily brief '{tid}' ({n} delivered in 7d)",
                              domain="notifications"))
    except Exception as e:  # noqa: BLE001
        notes.append(f"notifications unreadable: {str(e).splitlines()[0][:100]}")
    facts, docs, runbooks = {}, {}, {}
    try:
        cur.execute("SELECT substr(entity, 7), value FROM house_facts WHERE entity LIKE %s "
                    "AND attr = 'manual_fallback'", ("haven:%",))
        facts = dict(cur.fetchall())
        cur.execute("SELECT doc_type, content FROM agent_docs")
        docs = dict(cur.fetchall())
    except Exception as e:  # noqa: BLE001
        notes.append(f"fallback sources unreadable: {str(e).splitlines()[0][:100]}")
    try:
        import nova_butlerian_ledger as B
        rdir = B.runbook_dir(cur)
        runbooks = {k: v for k, v in B.TASKS.items() if (Path(rdir) / f"{k}.md").exists()}
    except Exception as e:  # noqa: BLE001
        notes.append(f"Lagash runbooks unreadable: {str(e)[:100]}")
    seen, uniq = set(), []
    for it in items:
        if it["key"] not in seen:
            seen.add(it["key"])
            uniq.append(it)
    return attach_fallbacks(uniq, facts, fallback_lines(docs), runbooks), notes


# ── the kill switch (the EXISTING one in nova_autonomy_safety) ──────────────

def _db_flag(cur):
    """(engaged bool, updated_by) of service_config autonomy/kill_switch, or (None, None) if unreadable."""
    try:
        cur.execute("SELECT value, updated_by FROM service_config WHERE service='autonomy' AND key='kill_switch'")
        r = cur.fetchone()
        if not r:
            return False, None
        v = r[0] if isinstance(r[0], str) else json.dumps(r[0])
        return v.strip().strip('"').lower() in ("true", "1", "on", "yes"), r[1]
    except Exception:  # noqa: BLE001
        return None, None


def engage(cur, drill_id: int) -> None:
    """Flip the one kill switch, marked as this drill's."""
    S.engage_kill(cur, note=MARK.format(id=drill_id))


def restore_kill(cur, drill_id: int) -> dict:
    """Undo ONLY what this drill set (same steps as `nova_autonomy_safety.py unkill`, ownership-checked).
    Never raises. cur may be None (PG down): the file is still handled; the DB flag waits for --watchdog."""
    mark = MARK.format(id=drill_id)
    out = {"file": "absent", "db": "untouched", "engaged_after": None}
    try:
        if os.path.exists(S.KILL_FILE):
            if mark in _read(Path(S.KILL_FILE)):
                os.remove(S.KILL_FILE)
                out["file"] = "removed"
            else:
                out["file"] = "foreign (left engaged)"
    except Exception as e:  # noqa: BLE001
        out["file"] = f"error: {e}"
    if cur is not None:
        engaged, by = _db_flag(cur)
        if engaged is None:
            out["db"] = "unreadable"
        elif engaged and by == f"kill:{mark}":
            try:
                cur.execute("""UPDATE service_config SET value='"false"', updated_by=%s
                               WHERE service='autonomy' AND key='kill_switch' AND updated_by=%s""",
                            (f"{mark}:restore", f"kill:{mark}"))
                out["db"] = "cleared"
            except Exception as e:  # noqa: BLE001
                out["db"] = f"error: {e}"
        elif engaged:
            out["db"] = "foreign (left engaged)"
    try:
        out["engaged_after"] = S.kill_switch_engaged(cur)
    except Exception:  # noqa: BLE001
        out["engaged_after"] = os.path.exists(S.KILL_FILE)
    out["ok"] = not str(out["file"]).startswith("error") and not str(out["db"]).startswith(("error", "unreadable"))
    return out


class Restorer:
    """Idempotent restore shared by the finally block, the deadline timer, atexit and signal handlers."""

    def __init__(self, cur_factory, drill_id: int):
        self.cur_factory, self.drill_id = cur_factory, drill_id
        self.lock, self.result = threading.Lock(), None

    def __call__(self, *_a) -> dict:
        with self.lock:
            if self.result is not None:
                return self.result
            cur = None
            try:
                cur = self.cur_factory()
            except Exception as e:  # noqa: BLE001
                log(f"restore: no database ({e}); clearing the file, --watchdog will clear the flag")
            self.result = restore_kill(cur, self.drill_id)
            log(f"kill switch restored: {self.result}")
            return self.result


def spawn_deadline_restorer(drill_id: int, deadline_epoch: float):
    """Detached process that restores at the deadline even if this one is killed. Best effort."""
    try:
        return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--restore-due",
                                 "--drill-id", str(drill_id), "--deadline", str(int(deadline_epoch))],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except Exception as e:  # noqa: BLE001
        log(f"could not spawn the deadline restorer ({e}); relying on the in-process timer and --watchdog")
        return None


# ── DB helpers ──────────────────────────────────────────────────────────────

def connect():
    import nova_dsn
    conn = nova_dsn.pg_connect("nova_ops")
    conn.autocommit = True
    return conn


def ensure_schema(cur) -> None:
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, SCHEMA)


def _row(cur, where: str, args=()) -> dict | None:
    cur.execute(f"SELECT id, quarter, status, checklist, results, scheduled_for, deadline, host "   # noqa: S608
                f"FROM haven_drills WHERE {where} ORDER BY opened_at DESC LIMIT 1", args)
    r = cur.fetchone()
    if not r:
        return None
    return dict(zip(("id", "quarter", "status", "checklist", "results", "scheduled_for", "deadline", "host"), r))


def open_drill(cur, now: datetime, checklist: list, notes: list) -> dict:
    cur.execute("INSERT INTO haven_drills (quarter, checklist, notes) VALUES (%s,%s,%s) RETURNING id",
                (quarter(now), json.dumps(checklist), "; ".join(notes)[:1000]))
    return {"id": cur.fetchone()[0], "quarter": quarter(now), "status": "awaiting_window",
            "checklist": checklist, "results": {}}


# ── modes ───────────────────────────────────────────────────────────────────

def scheduled(dry: bool = False, now: datetime | None = None) -> int:
    """Quarterly: open this quarter's drill (once), build its checklist, ask Jordan for a window.
    Never touches the kill switch (beyond --watchdog's restore of an overdue drill)."""
    now = now or datetime.now(timezone.utc)
    conn = connect()
    try:
        cur = conn.cursor()
        if not dry:
            ensure_schema(cur)
            watchdog(cur)
        items, notes = build_checklist(cur)
        summ = summarize(items, {})
        if dry:
            print(render({"id": "(dry)", "quarter": quarter(now), "status": "dry-run", "checklist": items}, summ))
            for it in items:
                print(f"  - [{it['kind']}] {it['title']}\n      fallback: {it['fallback'][:150]}"
                      f"{'  (' + it['fallback_source'] + ')' if it['fallback_source'] else ''}")
            for n in notes:
                print(f"  note: {n}")
            return 0
        if _row(cur, "quarter = %s", (quarter(now),)):
            log(f"{quarter(now)} drill already opened; nothing to do")
            return 0
        d = open_drill(cur, now, items, notes)
        from nova_notify import notify
        notify(f"Haven Drill {d['quarter']}: pick a window to run the house without me",
               body=(render(d, summ) + "\n\nNothing has been switched off. When you have 30 minutes, run "
                     "`nova_haven_drill.py --live --confirm` on the automation host (window max 2 h; my kill "
                     "switch restores itself at the deadline). To note a time: `--schedule-window "
                     "\"YYYY-MM-DD HH:MM\"`."),
               level="info", category="haven_drill", source="nova_haven_drill",
               dedup_key=f"haven_drill:{d['quarter']}", meta={"drill_id": d["id"], **summ})
        log(f"opened drill #{d['id']} with {len(items)} items")
        return 0
    finally:
        conn.close()


def watchdog(cur, now_epoch: float | None = None) -> int:
    """Restore any live drill past its deadline (ownership-checked). Cheap; safe to run every few minutes."""
    now_epoch = now_epoch or time.time()
    try:
        cur.execute("SELECT id, extract(epoch FROM deadline), host FROM haven_drills "
                    "WHERE status = 'live' AND deadline IS NOT NULL")
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001 — no table yet means no drill
        return 0
    n = 0
    for did, dl, host in rows:
        if dl is None or float(dl) > now_epoch:
            continue
        res = restore_kill(cur, did)
        if host and host != socket.gethostname() and res["file"] == "absent":
            res["file"] = f"not on this host ({host}); file there is cleared by its own timer"
        cur.execute("""UPDATE haven_drills SET status = 'aborted', restored_at = now(), restore_detail = %s,
                       notes = coalesce(notes,'') || ' | watchdog restored an overdue drill' WHERE id = %s""",
                    (json.dumps(res), did))
        log(f"watchdog: drill #{did} overdue, restored: {res}")
        n += 1
    return n


def _interactive(checklist: list, deadline: float, ask=input) -> dict:
    """Prompt per item until done or the deadline passes."""
    out = {}
    print("\nNova is OFF. Try each by hand. Answer w(orked) / f(ailed) / s(kip) / q(uit).")
    for it in checklist:
        if time.time() >= deadline:
            print("Window over.")
            break
        print(f"\n• {it['title']}\n  fallback on record: {it['fallback']}")
        a = (ask("  result [w/f/s/q]: ") or "").strip().lower()[:1]
        if a == "q":
            break
        r = {"w": "worked", "f": "failed"}.get(a, "skipped")
        how = ask("  how did you do it by hand? (enter to skip): ").strip() if r == "worked" else ""
        out[it["key"]] = {"result": r, "how": how[:400]}
    return out


def _wait_then_file(path: str, deadline: float, ask=input) -> dict:
    """Wait for ENTER (or the deadline), then read the results file."""
    print(f"\nNova is OFF until {datetime.fromtimestamp(deadline):%H:%M}. Work the checklist by hand, "
          f"write results to {path}, then press ENTER.")
    t = threading.Thread(target=lambda: ask(""), daemon=True)
    t.start()
    t.join(timeout=max(0.0, deadline - time.time()))
    return parse_results(json.loads(Path(path).read_text()))


def _interrupt(signum, _frame):
    raise KeyboardInterrupt(f"signal {signum}")   # unwinds into live()'s finally, which restores


def live(cur, cur_factory, drill: dict, window_min: int, collect, spawn=True) -> dict:
    """Run one live window. The kill switch is restored on every path out of here."""
    window_min = clamp_window(window_min)
    did = drill["id"]
    if S.kill_switch_engaged(cur):
        raise RuntimeError("kill switch is already engaged; the drill will not take ownership of it")
    deadline = time.time() + window_min * 60
    restorer = Restorer(cur_factory, did)
    atexit.register(restorer)
    old = {}
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        try:
            old[sig] = signal.signal(sig, _interrupt)
        except Exception:  # noqa: BLE001 — not in the main thread (tests)
            pass
    timer = threading.Timer(max(0.0, deadline - time.time()), restorer)
    timer.daemon = True
    proc = None
    results, status, err = {}, "aborted", None
    try:
        cur.execute("""UPDATE haven_drills SET status = 'live', started_at = now(),
                       deadline = to_timestamp(%s), host = %s, kill_switch_flipped = true WHERE id = %s""",
                    (deadline, socket.gethostname(), did))
        if spawn:
            proc = spawn_deadline_restorer(did, deadline)
        timer.start()
        engage(cur, did)
        if not S.kill_switch_engaged(cur):
            raise RuntimeError("kill switch did not engage; drill aborted")
        log(f"drill #{did}: kill switch ENGAGED for {window_min} min (auto-restore at the deadline)")
        results = collect(drill["checklist"], deadline)
        status = "completed"
    except BaseException as e:  # noqa: BLE001 — fail SAFE: every exit restores
        err = f"{type(e).__name__}: {e}"
        log(f"drill #{did} interrupted: {err}")
    finally:
        res = restorer()
        timer.cancel()
        if proc is not None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        for sig, h in old.items():
            try:
                signal.signal(sig, h)
            except Exception:  # noqa: BLE001
                pass
        try:
            atexit.unregister(restorer)
        except Exception:  # noqa: BLE001
            pass
    try:
        cur.execute("""UPDATE haven_drills SET status = %s, results = %s, restored_at = now(), restore_detail = %s,
                       notes = coalesce(notes,'') || %s WHERE id = %s""",
                    (status, json.dumps(results), json.dumps(res), f" | {err}" if err else "", did))
        for k, r in results.items():
            if r.get("result") == "worked" and r.get("how"):
                cur.execute("""INSERT INTO house_facts (entity, attr, value, source) VALUES (%s,'manual_fallback',%s,'haven_drill')
                               ON CONFLICT (entity, attr) DO UPDATE SET value = EXCLUDED.value,
                               source = EXCLUDED.source, observed_at = now()""", (f"haven:{k}", r["how"]))
    except Exception as e:  # noqa: BLE001
        log(f"could not record results ({e}); restore already done: {res}")
    if (not res.get("ok")) or (res.get("engaged_after") and "foreign" not in json.dumps(res)):
        _alert_restore_failed(did, res)
    return {"status": status, "results": results, "restore": res, "error": err}


def _alert_restore_failed(did, res) -> None:
    log(f"RESTORE NEEDS A HAND: {res}. Run: nova_autonomy_safety.py unkill")
    try:
        from nova_notify import notify
        notify(f"Haven Drill #{did}: kill switch restore needs a hand",
               body=f"Restore result {res}. Run `nova_autonomy_safety.py unkill` on the automation host.",
               level="critical", category="haven_drill", source="nova_haven_drill",
               dedup_key=f"haven_drill:restore:{did}")
    except Exception:  # noqa: BLE001
        pass


def restore_due(drill_id: int, deadline: float) -> int:
    """The detached deadline restorer: sleep, then restore only what the drill still owns."""
    while time.time() < deadline:
        time.sleep(min(30.0, max(0.5, deadline - time.time())))
    cur = None
    try:
        cur = connect().cursor()
    except Exception:  # noqa: BLE001
        pass
    res = restore_kill(cur, drill_id)
    if cur is not None and (res["file"] == "removed" or res["db"] == "cleared"):
        try:
            cur.execute("""UPDATE haven_drills SET status = 'aborted', restored_at = now(), restore_detail = %s,
                           notes = coalesce(notes,'') || ' | deadline restorer fired' WHERE id = %s AND status = 'live'""",
                        (json.dumps(res), drill_id))
        except Exception:  # noqa: BLE001
            pass
    return 0


def report() -> int:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('haven_drills') IS NOT NULL")
        if not cur.fetchone()[0]:
            print("No Haven Drill has been opened yet.")
            return 0
        d = _row(cur, "true")
        cur.execute("SELECT max(started_at) FROM haven_drills WHERE kill_switch_flipped AND status = 'completed'")
        last = cur.fetchone()[0]
        last_live = _row(cur, "status = 'completed' AND kill_switch_flipped")
        summ = summarize(d["checklist"], (last_live or {}).get("results") or {})
        print(render(d, summ))
        print(f"Last live drill: {last:%Y-%m-%d} " if last else "Last live drill: never")
        print("\n".join(butlerian_lines(cur)))
        return 0
    finally:
        conn.close()


def schedule_window(when: str) -> int:
    ts = datetime.strptime(when, "%Y-%m-%d %H:%M")
    conn = connect()
    try:
        cur = conn.cursor()
        d = _row(cur, "status IN ('awaiting_window','scheduled')")
        if not d:
            print("No open drill to schedule.")
            return 1
        cur.execute("UPDATE haven_drills SET scheduled_for = %s, status = 'scheduled' WHERE id = %s", (ts, d["id"]))
        print(f"Drill #{d['id']} scheduled for {ts:%Y-%m-%d %H:%M} (nothing is switched off until you run --live).")
        return 0
    finally:
        conn.close()


def run_live(a) -> int:
    if a.scheduled:
        print("REFUSED: --live is human-run only and never runs under --scheduled.", file=sys.stderr)
        return 2
    if not a.confirm:
        print("REFUSED: --live needs --confirm (it flips Nova's kill switch for the window).", file=sys.stderr)
        return 2
    window = clamp_window(a.window_min)
    if window != a.window_min:
        log(f"window {a.window_min} min clamped to {window} min (max {MAX_WINDOW_MIN})")
    if a.dry_run:
        print(f"(dry-run) would engage Nova's kill switch for {window} min, collect results, and restore it "
              f"at the deadline (or sooner). Nothing written, nothing switched.")
        return 0
    if not a.results_file and not sys.stdin.isatty():
        print("REFUSED: no terminal to answer prompts; pass --results-file.", file=sys.stderr)
        return 2
    conn = connect()
    try:
        cur = conn.cursor()
        ensure_schema(cur)
        d = _row(cur, "id = %s", (a.drill_id,)) if a.drill_id else _row(cur, "status IN ('awaiting_window','scheduled')")
        if not d:
            items, notes = build_checklist(cur)
            d = open_drill(cur, datetime.now(timezone.utc), items, notes)
        if d["status"] not in OPEN_STATES:
            print(f"REFUSED: drill #{d['id']} is {d['status']}.", file=sys.stderr)
            return 2
        collect = (lambda cl, dl: _wait_then_file(a.results_file, dl)) if a.results_file else _interactive
        try:
            out = live(cur, lambda: connect().cursor(), d, window, collect)
        except RuntimeError as e:          # refused before anything was flipped
            print(f"REFUSED: {e}", file=sys.stderr)
            return 2
        summ = summarize(d["checklist"], out["results"])
        print(render({**d, "status": out["status"]}, summ))
        try:
            from nova_notify import notify
            notify(f"Haven Drill #{d['id']} {out['status']}",
                   body=render({**d, "status": out["status"]}, summ) + f"\nKill switch: {out['restore']}",
                   level="info", category="haven_drill", source="nova_haven_drill",
                   dedup_key=f"haven_drill:done:{d['id']}", meta=summ)
        except Exception:  # noqa: BLE001
            pass
        return 0 if out["status"] == "completed" else 1
    finally:
        conn.close()


# ── Butlerian section (Butlerian Ledger + Aub's Drill, merged 2026-10-15) ───
# Read-only with respect to the kill switch: nothing here calls the engage, live or restore paths.

def butlerian_lines(cur) -> list:
    """The report's Butlerian section from butlerian_weekly / butlerian_drills. Never raises."""
    try:
        import nova_butlerian_ledger as B
        if not B._table(cur, "butlerian_weekly"):
            return ["Butlerian Ledger: no week recorded yet."]
        rows = B._q(cur, "SELECT week, decided, him_unchanged, delegated, nova_decided, dependence "
                         "FROM butlerian_weekly ORDER BY week DESC LIMIT 6")
        out = []
        if rows:
            wk, dec, unch, dlg, nova, dep = rows[0]
            weeks = {str(r[0]): {"decided": r[1] or 0, "dependence": r[5]} for r in reversed(rows)
                     if r[5] is not None}
            out.append(f"Butlerian Ledger, week of {wk}: {dec} decisions on Nova's proposals; he took {unch} "
                       f"unchanged, {dlg} decided by the Claude reviewer, {nova} by Nova "
                       f"(dependence {dep}; trend {B.trend(weeks, n=3)}).")
        consent = B.consented(cur)
        last = (B._q(cur, "SELECT quarter, task, result, coalesce(ran_at, offered_at) FROM butlerian_drills "
                          "ORDER BY id DESC LIMIT 1") if B._table(cur, "butlerian_drills") else [])
        aub = f"Aub's Drill: consent {'yes' if consent else 'no'}"
        if last:
            q, task, res, ts = last[0]
            aub += f"; last {task} ({q or '-'}) {res or 'offered, not yet run'}{f' {ts:%Y-%m-%d}' if ts else ''}"
        return out + [aub + "."]
    except Exception as e:  # noqa: BLE001 — a section, never a reason the report fails
        return [f"Butlerian Ledger: unreadable ({str(e)[:100]})."]


def butlerian(a) -> int:
    """--butlerian / --aub-offer / --aub-record: the Butlerian Ledger's runs, logic unchanged."""
    import nova_butlerian_ledger as B
    if a.aub_record:
        if a.result not in B.RESULTS:
            print(f"--aub-record needs --result one of {', '.join(B.RESULTS)}", file=sys.stderr)
            return 2
        return B.record_drill(a.aub_record, a.result, a.note or "", dry=a.dry_run)
    if a.aub_offer:
        B.offer_drill(dry=a.dry_run)
        return 0
    B.run(dry=a.dry_run, n_weeks=max(1, min(a.weeks, 104)))
    return 0


def selftest() -> int:
    assert clamp_window(30) == 30 and clamp_window(500) == 120 and clamp_window(1) == 5 and clamp_window("x") == 30
    assert quarter(datetime(2026, 10, 15)) == "2026-Q4" and quarter(datetime(2026, 1, 1)) == "2026-Q1"
    it = attach_fallbacks([item("a", "automation", "Turn on room lights"), item("b", "notification", "Critical x",
                                                                                domain="notifications")],
                          {}, [("doc", "Lights: Hue bridges and wall switches work without Nova.")], {})
    assert it[0]["fallback_source"] == "agent_docs:doc" and it[1]["fallback"] == NO_FALLBACK
    s = summarize(it, {"a": {"result": "worked"}})
    assert s["verified_pct"] == 50.0 and s["no_fallback"] == ["b"], s
    assert parse_results({"a": "worked", "b": {"result": "failed"}, "c": "maybe"}) == \
        {"a": {"result": "worked", "how": ""}, "b": {"result": "failed", "how": ""}}
    src = "async def rule_x():\n    '''Lights on.'''\n    _actuation_ok('x')\nasync def rule_y():\n    '''Alert.'''\n"
    assert automation_rules(src) == [("rule_x", "Lights on.")]
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scheduled", action="store_true", help="quarterly: open the drill, ask for a window")
    ap.add_argument("--dry-run", action="store_true", help="build and print the checklist; write nothing")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--watchdog", action="store_true", help="restore any live drill past its deadline")
    ap.add_argument("--schedule-window", metavar="'YYYY-MM-DD HH:MM'")
    ap.add_argument("--live", action="store_true", help="HUMAN-RUN: flip the kill switch for a bounded window")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--window-min", type=int, default=DEFAULT_WINDOW_MIN)
    ap.add_argument("--results-file")
    ap.add_argument("--drill-id", type=int)
    ap.add_argument("--restore-due", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--deadline", type=float, help=argparse.SUPPRESS)
    ap.add_argument("--butlerian", action="store_true", help="Butlerian Ledger: compute and store the weekly share")
    ap.add_argument("--weeks", type=int, default=8, help="with --butlerian: complete weeks (default 8)")
    ap.add_argument("--aub-offer", action="store_true", help="Aub's Drill: this quarter's offer (needs consent)")
    ap.add_argument("--aub-record", metavar="TASK", help="Aub's Drill: record a drill he ran (with --result)")
    ap.add_argument("--result", help="with --aub-record: could | partly | could_not")
    ap.add_argument("--note", default="", help="with --aub-record")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.live:
        return run_live(a)
    if a.restore_due:
        return restore_due(a.drill_id, a.deadline or time.time())
    if a.watchdog:
        conn = connect()
        try:
            return 0 if watchdog(conn.cursor()) >= 0 else 1
        finally:
            conn.close()
    if a.schedule_window:
        return schedule_window(a.schedule_window)
    if a.butlerian or a.aub_offer or a.aub_record:
        return butlerian(a)
    if a.report:
        return report()
    if a.scheduled or a.dry_run:
        return scheduled(dry=a.dry_run)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
