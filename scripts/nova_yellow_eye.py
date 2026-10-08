#!/usr/bin/env python3
"""nova_yellow_eye.py — THE YELLOW EYE: stay with every new organ until it actually works.

From Mary Shelley's Frankenstein (1818): after two years of work Victor finishes his creature
on a dreary November night, watches its dull yellow eye open and its limbs stir, and cannot
bear what he has made. He runs from the room and leaves it alone at the moment it wakes.
Everything that follows comes from that abandonment at activation. Nova's version: when a new
organ is born (a new task in the scheduler), someone stays with it through a 72-hour
hypercare window, checks that it really runs and really writes, and signs it off citing one
real output row. Until then it is an "unattended birth".

Minimal first version:
  * births = scheduler tasks whose key first appears in config/scheduler.yaml (git history of
    the YAML; uncommitted tasks fall back to the file mtime) on or after START. Day one
    backfills the 2026-10-08 batch (the Lovecraft organs and the rest of that day's wave).
  * 72 h after birth: one exit-0 run in scheduler_runs, a non-empty table among those the
    script declares (CREATE TABLE IF NOT EXISTS ...; none declared = not checked), a test file.
  * failures of unsigned births go into ONE rolling claude_queue item until signed.
  * --sign TASK --row TABLE:KEY records the sign-off; the cited table must hold rows.
Read-only over everything except births and its own claude_queue item. Never blocks anything.
Discovery helpers (scheduler_births, script_births, current_tasks) are shared with nova_busab.py.

CLI:   --run [--dry-run] [--hours N]   --show   --sign TASK --row TABLE:KEY [--by NAME]   --selftest
Table: births.  service_config: yellow_eye/owner (default "Claude").
Schedule: daily 09:20.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
SCHED = Path(os.environ.get("NOVA_SCHED_CONFIG") or Path.home() / ".openclaw/config/scheduler.yaml")
START = datetime(2026, 10, 8, tzinfo=W.TZ)   # day-one backfill floor: the 10-08 wave
HYPERCARE_H = 72
QUEUE_SESSION = "nova-yellow-eye"
QUEUE_PREFIX = "Yellow Eye: "
OPEN = ("queued", "pending", "in_progress", "claimed")
TASK_ADD = re.compile(r"^\+  ([A-Za-z0-9_]+):\s*(#.*)?$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS births (
  task text PRIMARY KEY, script text, born timestamptz, owner text, closes_at timestamptz,
  ran_ok boolean, has_rows boolean, has_test boolean, state text, checked_at timestamptz,
  signed_by text, signed_at timestamptz, signed_row text);
"""


def log(m: str) -> None:
    print(f"[yellow-eye {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a failed read degrades to "nothing known"
        log(f"query failed: {e}")
        return []


# ── discovery (shared with nova_busab) ──────────────────────────────────────

def _git(cwd: Path, *args) -> str:
    """Read-only git; fails open to "" (no retry: local repo, a failure will not heal in seconds)."""
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60)
        return r.stdout if r.returncode == 0 else ""
    except Exception as e:  # noqa: BLE001
        log(f"git failed: {e}")
        return ""


def parse_first_seen(text: str, added=TASK_ADD) -> dict:
    """{name: first commit time} from `git log --reverse --format='@@C %cI' ...` output.
    `added` matches a line that introduces a name (group 1); None = the --name-only form."""
    out, ts = {}, None
    for line in text.splitlines():
        if line.startswith("@@C "):
            ts = datetime.fromisoformat(line[4:].strip())
        elif ts and line.strip():
            m = added.match(line) if added else None
            name = m.group(1) if m else (Path(line.strip()).name if added is None else None)
            if name:
                out.setdefault(name, ts)
    return out


def current_tasks(path: Path = SCHED) -> dict:
    """{task: script} from the live scheduler YAML ({} if unreadable)."""
    try:
        import yaml
        return {k: (v or {}).get("script") for k, v in (yaml.safe_load(path.read_text())["tasks"] or {}).items()}
    except Exception as e:  # noqa: BLE001
        log(f"scheduler yaml unreadable: {e}")
        return {}


def _mtime(p: Path) -> datetime:
    return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)


def scheduler_births(path: Path = SCHED) -> dict:
    """{task: born} for every task key ever added to the YAML; current tasks with no commit yet
    are born at the file's mtime."""
    born = parse_first_seen(_git(path.parent, "log", "--reverse", "--format=@@C %cI", "-p",
                                 "--unified=0", "--", path.name))
    for t in current_tasks(path):
        if t not in born and path.exists():
            born[t] = _mtime(path)
    return born


def script_births(d: Path = SCRIPTS) -> dict:
    """{nova_*.py: born} from the first commit that added each file; uncommitted files use mtime."""
    born = parse_first_seen(_git(d, "log", "--reverse", "--diff-filter=A", "--format=@@C %cI",
                                 "--name-only", "--", ":(glob)nova_*.py"), added=None)
    for p in d.glob("nova_*.py"):
        born.setdefault(p.name, _mtime(p))
    return born


# ── checks (pure) ───────────────────────────────────────────────────────────

def declared_tables(src: str) -> list:
    return sorted(set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+([\w.]+)", src, re.I)))


def has_test(script: str, corpus: dict) -> bool:
    """corpus = {test file name: text}. Dedicated file, short-named file, or any test that names it."""
    stem = Path(script).stem
    return (f"test_{stem}.py" in corpus or f"test_{stem.removeprefix('nova_')}.py" in corpus
            or any(stem in t for t in corpus.values()))


def state_of(born, now, checks: dict, signed: bool, hours: int = HYPERCARE_H) -> str:
    if signed:
        return "signed"
    if now < born + timedelta(hours=hours):
        return "hypercare"
    return "unattended" if any(v is False for v in checks.values()) else "ready"


def failures(checks: dict) -> list:
    return [k for k, v in checks.items() if v is False]


# ── PG ──────────────────────────────────────────────────────────────────────

def ran_ok(cur, task: str) -> bool:
    # ponytail: any exit-0 run of the task id counts (tasks often run before the YAML is committed);
    # a task id reused from a retired organ would pass early.
    return bool(_q(cur, "SELECT 1 FROM scheduler_runs WHERE task_id=%s AND exit_code=0 LIMIT 1", (task,)))


def has_rows(cur, tables: list):
    """True if any declared table holds a row; None when the script declares none."""
    if not tables:
        return None
    from psycopg2 import sql
    for t in tables:
        if not (_q(cur, "SELECT to_regclass(%s)", (t,)) or [(None,)])[0][0]:
            continue   # not created yet
        r = _q(cur, sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(sql.Identifier(*t.split("."))))
        if r and r[0][0]:
            return True
    return False


def signed_tasks(cur) -> dict:
    ok = _q(cur, "SELECT to_regclass('births')")
    if not ok or ok[0][0] is None:
        return {}
    return {t: by for t, by in _q(cur, "SELECT task, signed_by FROM births WHERE signed_at IS NOT NULL")}


def _test_corpus() -> dict:
    out = {}
    for p in (SCRIPTS / "tests").glob("test_*.py"):
        try:
            out[p.name] = p.read_text(errors="ignore")
        except OSError:
            pass
    return out


def assess(cur, now, hours: int = HYPERCARE_H) -> list:
    tasks = current_tasks()
    born = scheduler_births()
    signed = signed_tasks(cur)
    corpus = _test_corpus()
    rows = []
    for task, b in sorted(born.items(), key=lambda kv: (kv[1], kv[0])):
        if task not in tasks or b < START:
            continue
        script = tasks[task] or ""
        checks = {}
        if now >= b + timedelta(hours=hours):
            p = SCRIPTS / script
            src = p.read_text(errors="ignore") if script and p.is_file() else ""
            checks = {"ran_ok": ran_ok(cur, task), "has_rows": has_rows(cur, declared_tables(src)),
                      "has_test": has_test(script, corpus) if src else False}
        rows.append({"task": task, "script": script, "born": b, "closes_at": b + timedelta(hours=HYPERCARE_H),
                     "checks": checks, "signed_by": signed.get(task),
                     "state": state_of(b, now, checks, task in signed, hours)})
    return rows


def write(cur, rows: list, owner: str) -> None:
    ensure_schema(cur)
    for r in rows:
        c = r["checks"]
        cur.execute(
            "INSERT INTO births (task, script, born, owner, closes_at, ran_ok, has_rows, has_test, state, checked_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT (task) DO UPDATE SET script=EXCLUDED.script, "
            "born=EXCLUDED.born, closes_at=EXCLUDED.closes_at, ran_ok=EXCLUDED.ran_ok, has_rows=EXCLUDED.has_rows, "
            "has_test=EXCLUDED.has_test, checked_at=now(), "
            "state=CASE WHEN births.signed_at IS NOT NULL THEN 'signed' ELSE EXCLUDED.state END",
            (r["task"], r["script"], r["born"], owner, r["closes_at"], c.get("ran_ok"), c.get("has_rows"),
             c.get("has_test"), r["state"]))


def queue_text(rows: list) -> tuple:
    bad = [r for r in rows if r["state"] == "unattended"]
    ready = [r["task"] for r in rows if r["state"] == "ready"]
    desc = f"{QUEUE_PREFIX}{len(bad)} unattended birth(s): " + ", ".join(r["task"] for r in bad)
    ctx = "\n".join(f"- {r['task']} ({r['script']}), born {r['born']:%Y-%m-%d %H:%M}: failed "
                    f"{', '.join(failures(r['checks']))}" for r in bad)
    if ready:
        ctx += "\nPassed, awaiting sign-off: " + ", ".join(ready)
    ctx += "\nFix, then: nova_yellow_eye.py --sign <task> --row <table>:<key> (cite one real output row)."
    return desc[:300], ctx


def file_queue(cur, rows: list):
    """One rolling item: update the open one, else create it. Nothing when no birth is unattended."""
    if not any(r["state"] == "unattended" for r in rows):
        return None
    desc, ctx = queue_text(rows)
    open_ = _q(cur, "SELECT id FROM claude_queue WHERE session_id=%s AND status IN %s ORDER BY id DESC LIMIT 1",
               (QUEUE_SESSION, OPEN))
    if open_:
        cur.execute("UPDATE claude_queue SET description=%s, context=%s, updated_at=now() WHERE id=%s",
                    (desc, ctx, open_[0][0]))
        return open_[0][0]
    cur.execute("INSERT INTO claude_queue (session_id, created_at, updated_at, status, priority, description, context) "
                "VALUES (%s, now(), now(), 'queued', 3, %s, %s) RETURNING id", (QUEUE_SESSION, desc, ctx))
    return cur.fetchone()[0]


def _owner(cur) -> str:
    r = _q(cur, "SELECT value FROM service_config WHERE service='yellow_eye' AND key='owner'")
    v = r[0][0] if r else None
    return v if isinstance(v, str) and v else "Claude"


def run(dry: bool = False, hours: int = HYPERCARE_H) -> list:
    now = datetime.now(timezone.utc)
    conn = W.connect()
    try:
        cur = conn.cursor()
        rows = assess(cur, now, hours)
        counts = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        log(f"{'DRY RUN ' if dry else ''}{len(rows)} births since {START:%Y-%m-%d}: {counts}")
        for r in rows:
            chk = " ".join(f"{k}={'-' if v is None else 'ok' if v else 'NO'}" for k, v in r["checks"].items())
            print(f"  {r['state']:<10} {r['task']:<26} born {r['born'].astimezone(W.TZ):%m-%d %H:%M}  {chk}")
        if not dry:
            write(cur, rows, _owner(cur))
            qid = file_queue(cur, rows)
            log(f"wrote {len(rows)} births; queue item {qid or 'none'}")
        return rows
    finally:
        conn.close()


def sign(task: str, cited: str, by: str, dry: bool = False) -> int:
    table = cited.split(":", 1)[0]
    if ":" not in cited or not re.fullmatch(r"[\w.]+", table):
        print("--row must be TABLE:KEY")
        return 2
    conn = W.connect()
    try:
        cur = conn.cursor()
        r = _q(cur, "SELECT script FROM births WHERE task=%s", (task,))
        if not r:
            print(f"no birth recorded for {task} (run --run first)")
            return 2
        p = SCRIPTS / (r[0][0] or "")
        declared = declared_tables(p.read_text(errors="ignore")) if p.is_file() else []
        if declared and table not in declared:
            print(f"{table} is not one of {task}'s tables: {', '.join(declared)}")
            return 2
        if not has_rows(cur, [table]):
            print(f"{table} holds no rows; a sign-off must cite a real output row")
            return 2
        if dry:
            print(f"would sign {task} by {by} citing {cited}")
            return 0
        cur.execute("UPDATE births SET signed_by=%s, signed_at=now(), signed_row=%s, state='signed' WHERE task=%s",
                    (by, cited, task))
        print(f"signed {task} by {by} citing {cited}")
        return 0
    finally:
        conn.close()


def show() -> int:
    conn = W.connect()
    try:
        cur = conn.cursor()
        ok = _q(cur, "SELECT to_regclass('births')")
        if not ok or ok[0][0] is None:
            print("no births table yet")
            return 0
        for t, st, b, by, row in _q(cur, "SELECT task, state, born, signed_by, signed_row FROM births ORDER BY born"):
            print(f"{st:<10} {t:<26} {b:%Y-%m-%d %H:%M}  {by or ''} {row or ''}")
        return 0
    finally:
        conn.close()


def selftest() -> int:
    log_text = ("@@C 2026-10-01T10:00:00-07:00\n+  old_task:   # x\n+    script: nova_old.py\n"
                "+  tick_interval: 1\n@@C 2026-10-08T15:00:00-07:00\n+  old_task:\n+  new_task:\n")
    fs = parse_first_seen(log_text)
    assert set(fs) == {"old_task", "new_task"}, fs
    assert fs["old_task"].day == 1 and fs["new_task"].day == 8
    names = parse_first_seen("@@C 2026-10-08T00:00:00+00:00\nscripts/nova_a.py\n\n", added=None)
    assert list(names) == ["nova_a.py"], names
    assert declared_tables("CREATE TABLE IF NOT EXISTS births (\ncreate table if not exists x.y (") == ["births", "x.y"]
    corpus = {"test_hold.py": "", "test_misc.py": "import nova_other"}
    assert has_test("nova_hold.py", corpus) and has_test("nova_other.py", corpus)
    assert not has_test("nova_none.py", corpus)
    b, now = datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 10, 12, tzinfo=timezone.utc)
    assert state_of(b, b, {}, False) == "hypercare"
    assert state_of(b, now, {"ran_ok": True, "has_rows": None}, False) == "ready"
    assert state_of(b, now, {"ran_ok": False}, False) == "unattended"
    assert state_of(b, now, {"ran_ok": False}, True) == "signed"
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="assess births, write births, file failures")
    ap.add_argument("--dry-run", action="store_true", help="print only; write nothing")
    ap.add_argument("--hours", type=int, default=HYPERCARE_H, help="hypercare window (inspection only)")
    ap.add_argument("--show", action="store_true", help="stored births")
    ap.add_argument("--sign", metavar="TASK", help="sign off a birth")
    ap.add_argument("--row", help="with --sign: TABLE:KEY of one real output row")
    ap.add_argument("--by", default="Little Mister", help="with --sign: who signs")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.sign:
        return sign(a.sign, a.row or "", a.by, dry=a.dry_run)
    if a.run:
        run(dry=a.dry_run, hours=a.hours)
        return 0
    if a.show:
        return show()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
