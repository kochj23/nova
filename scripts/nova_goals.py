#!/usr/bin/env python3
"""
nova_goals.py — Structured goal and pursuit tracking for Nova.

Provides CRUD operations for goals, progress logging, and gap analysis.
Goals are stored in PostgreSQL (nova_ops) and checked during morning briefs,
daily journals, and on-demand via Nova's conversation.

Table: goals (in nova_ops)
  - id, title, description, status, priority, deadline, project
  - created_at, updated_at, completed_at
  - check_in_interval (days between automated nudges)
  - last_activity (auto-updated from git commits, memories, etc.)

Table: goal_log (in nova_ops)
  - goal_id, timestamp, event_type, note

Written by Jordan Koch.
"""

import json
import subprocess
import sys
import uuid
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN

DB = "nova_ops"
SOURCE = "nova_goals"


# ── Database helpers ──────────────────────────────────────────────────────────


def _query(sql, db=DB):
    try:
        result = subprocess.run(
            ["psql", "-U", "kochj", "-d", db, "-tAc", sql],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            log(f"DB error: {result.stderr.strip()}", level=LOG_ERROR, source=SOURCE)
            return []
        return [r for r in result.stdout.strip().split("\n") if r]
    except Exception as e:
        log(f"DB query failed: {e}", level=LOG_ERROR, source=SOURCE)
        return []


def _exec(sql, db=DB):
    result = subprocess.run(
        ["psql", "-U", "kochj", "-d", db, "-c", sql],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        log(f"DB exec error: {result.stderr.strip()}", level=LOG_ERROR, source=SOURCE)
        return False
    return True


# ── Schema migration ─────────────────────────────────────────────────────────


def ensure_schema():
    _exec("""
        CREATE TABLE IF NOT EXISTS goals (
            id              TEXT PRIMARY KEY,
            title           TEXT NOT NULL,
            description     TEXT DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'active',
            priority        TEXT NOT NULL DEFAULT 'medium',
            project         TEXT DEFAULT NULL,
            deadline        DATE DEFAULT NULL,
            check_in_days   INTEGER DEFAULT 7,
            last_activity   TIMESTAMPTZ DEFAULT NOW(),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at    TIMESTAMPTZ DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS goal_log (
            id          TEXT PRIMARY KEY,
            goal_id     TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
            timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            event_type  TEXT NOT NULL,
            note        TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
        CREATE INDEX IF NOT EXISTS idx_goals_project ON goals(project);
        CREATE INDEX IF NOT EXISTS idx_goal_log_goal ON goal_log(goal_id);
        CREATE INDEX IF NOT EXISTS idx_goal_log_ts ON goal_log(timestamp DESC);
    """)


# ── CRUD operations ──────────────────────────────────────────────────────────


def add_goal(title, description="", project=None, priority="medium",
             deadline=None, check_in_days=7):
    goal_id = str(uuid.uuid4())[:8]
    deadline_sql = f"'{deadline}'" if deadline else "NULL"
    project_sql = f"'{_escape(project)}'" if project else "NULL"

    sql = f"""
        INSERT INTO goals (id, title, description, project, priority, deadline, check_in_days)
        VALUES ('{goal_id}', '{_escape(title)}', '{_escape(description)}',
                {project_sql}, '{priority}', {deadline_sql}, {check_in_days});
    """
    if _exec(sql):
        _log_event(goal_id, "created", f"Goal created: {title}")
        log(f"Goal added: [{goal_id}] {title}", level=LOG_INFO, source=SOURCE)
        return goal_id
    return None


def update_goal(goal_id, **kwargs):
    sets = []
    for key, val in kwargs.items():
        if key in ("title", "description", "status", "priority", "project"):
            sets.append(f"{key} = '{_escape(str(val))}'")
        elif key == "deadline":
            sets.append(f"deadline = '{val}'" if val else "deadline = NULL")
        elif key == "check_in_days":
            sets.append(f"check_in_days = {int(val)}")
    if not sets:
        return False
    sets.append("updated_at = NOW()")
    sql = f"UPDATE goals SET {', '.join(sets)} WHERE id = '{goal_id}';"
    return _exec(sql)


def complete_goal(goal_id, note=""):
    sql = f"""
        UPDATE goals SET status = 'completed', completed_at = NOW(), updated_at = NOW()
        WHERE id = '{goal_id}';
    """
    if _exec(sql):
        _log_event(goal_id, "completed", note or "Goal completed")
        return True
    return False


def pause_goal(goal_id, reason=""):
    sql = f"""
        UPDATE goals SET status = 'paused', updated_at = NOW()
        WHERE id = '{goal_id}';
    """
    if _exec(sql):
        _log_event(goal_id, "paused", reason or "Goal paused")
        return True
    return False


def drop_goal(goal_id, reason=""):
    sql = f"""
        UPDATE goals SET status = 'dropped', updated_at = NOW()
        WHERE id = '{goal_id}';
    """
    if _exec(sql):
        _log_event(goal_id, "dropped", reason or "Goal dropped")
        return True
    return False


def log_progress(goal_id, note):
    _log_event(goal_id, "progress", note)
    _exec(f"UPDATE goals SET last_activity = NOW(), updated_at = NOW() WHERE id = '{goal_id}';")


def touch_activity(goal_id):
    _exec(f"UPDATE goals SET last_activity = NOW() WHERE id = '{goal_id}';")


# ── Query operations ─────────────────────────────────────────────────────────


def get_active_goals():
    rows = _query("""
        SELECT id, title, project, priority, deadline, check_in_days,
               last_activity, created_at
        FROM goals
        WHERE status = 'active'
        ORDER BY
            CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
            deadline NULLS LAST;
    """)
    goals = []
    for row in rows:
        parts = row.split("|")
        if len(parts) >= 8:
            goals.append({
                "id": parts[0],
                "title": parts[1],
                "project": parts[2] or None,
                "priority": parts[3],
                "deadline": parts[4] or None,
                "check_in_days": int(parts[5]) if parts[5] else 7,
                "last_activity": parts[6],
                "created_at": parts[7],
            })
    return goals


def get_stale_goals(threshold_days=None):
    goals = get_active_goals()
    stale = []
    now = datetime.now()
    for g in goals:
        threshold = threshold_days or g["check_in_days"]
        if g["last_activity"]:
            raw = g["last_activity"].split("+")[0].split("-0")[0].split(".")[0]
            try:
                last = datetime.fromisoformat(raw)
            except ValueError:
                continue
            days_idle = (now - last).days
            if days_idle >= threshold:
                g["days_idle"] = days_idle
                stale.append(g)
    return stale


def get_overdue_goals():
    today = date.today().isoformat()
    rows = _query(f"""
        SELECT id, title, project, priority, deadline
        FROM goals
        WHERE status = 'active' AND deadline IS NOT NULL AND deadline < '{today}'
        ORDER BY deadline;
    """)
    goals = []
    for row in rows:
        parts = row.split("|")
        if len(parts) >= 5:
            goals.append({
                "id": parts[0],
                "title": parts[1],
                "project": parts[2] or None,
                "priority": parts[3],
                "deadline": parts[4],
            })
    return goals


def get_goal_history(goal_id, limit=10):
    rows = _query(f"""
        SELECT timestamp, event_type, note
        FROM goal_log
        WHERE goal_id = '{goal_id}'
        ORDER BY timestamp DESC
        LIMIT {limit};
    """)
    return [{"timestamp": r.split("|")[0], "type": r.split("|")[1],
             "note": r.split("|")[2]} for r in rows if "|" in r]


def goal_summary():
    active = _query("SELECT COUNT(*) FROM goals WHERE status = 'active';")
    completed = _query("SELECT COUNT(*) FROM goals WHERE status = 'completed';")
    paused = _query("SELECT COUNT(*) FROM goals WHERE status = 'paused';")
    return {
        "active": int(active[0]) if active else 0,
        "completed": int(completed[0]) if completed else 0,
        "paused": int(paused[0]) if paused else 0,
    }


# ── Gap analysis (called by morning brief / goal check) ─────────────────────


def run_gap_analysis():
    lines = []
    stale = get_stale_goals()
    overdue = get_overdue_goals()
    active = get_active_goals()

    if overdue:
        lines.append("*Overdue:*")
        for g in overdue:
            lines.append(f"  • `{g['id']}` {g['title']} — due {g['deadline']}")

    if stale:
        lines.append("*Stale (no activity):*")
        for g in stale:
            lines.append(f"  • `{g['id']}` {g['title']} — {g['days_idle']}d idle")

    if not overdue and not stale:
        lines.append("All goals on track.")

    if active and len(active) > 4:
        lines.append(f"\n⚠️ You have {len(active)} active goals. Focus is 3-4 max.")

    return "\n".join(lines)


def format_goals_brief():
    active = get_active_goals()
    if not active:
        return None

    lines = [f"*Active Goals ({len(active)}):*"]
    for g in active:
        prefix = "🔴" if g["priority"] == "high" else "🟡" if g["priority"] == "medium" else "⚪"
        deadline_str = f" (due {g['deadline']})" if g["deadline"] else ""
        project_str = f" [{g['project']}]" if g["project"] else ""
        lines.append(f"  {prefix} {g['title']}{project_str}{deadline_str}")

    gap = run_gap_analysis()
    if gap and "All goals on track" not in gap:
        lines.append("")
        lines.append(gap)

    return "\n".join(lines)


# ── Auto-detection: link git commits to goals ────────────────────────────────


def detect_activity_from_git(xcode_path="/Volumes/Data/xcode"):
    active = get_active_goals()
    if not active:
        return

    today = date.today().isoformat()
    for g in active:
        if not g["project"]:
            continue
        project_dir = Path(xcode_path) / g["project"]
        if not project_dir.exists():
            continue
        try:
            result = subprocess.run(
                ["git", "-C", str(project_dir), "log", "--oneline",
                 f"--since={today}", "--format=%s"],
                capture_output=True, text=True, timeout=10,
            )
            commits = [c for c in result.stdout.strip().split("\n") if c]
            if commits:
                touch_activity(g["id"])
                _log_event(g["id"], "git_activity",
                           f"{len(commits)} commit(s) today: {commits[0][:60]}")
        except Exception:
            pass


# ── Helpers ──────────────────────────────────────────────────────────────────


def _log_event(goal_id, event_type, note=""):
    event_id = str(uuid.uuid4())[:8]
    _exec(f"""
        INSERT INTO goal_log (id, goal_id, event_type, note)
        VALUES ('{event_id}', '{goal_id}', '{event_type}', '{_escape(note)}');
    """)


def _escape(s):
    if not s:
        return ""
    return s.replace("'", "''").replace("\\", "\\\\")


# ── CLI interface ────────────────────────────────────────────────────────────


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Nova Goals Manager")
    sub = parser.add_subparsers(dest="command")

    # init
    sub.add_parser("init", help="Create tables")

    # add
    p_add = sub.add_parser("add", help="Add a goal")
    p_add.add_argument("title")
    p_add.add_argument("--desc", default="")
    p_add.add_argument("--project", default=None)
    p_add.add_argument("--priority", default="medium", choices=["high", "medium", "low"])
    p_add.add_argument("--deadline", default=None)
    p_add.add_argument("--check-in", type=int, default=7)

    # complete
    p_done = sub.add_parser("complete", help="Mark goal complete")
    p_done.add_argument("goal_id")
    p_done.add_argument("--note", default="")

    # pause
    p_pause = sub.add_parser("pause", help="Pause a goal")
    p_pause.add_argument("goal_id")
    p_pause.add_argument("--reason", default="")

    # drop
    p_drop = sub.add_parser("drop", help="Drop a goal")
    p_drop.add_argument("goal_id")
    p_drop.add_argument("--reason", default="")

    # progress
    p_prog = sub.add_parser("progress", help="Log progress")
    p_prog.add_argument("goal_id")
    p_prog.add_argument("note")

    # list
    sub.add_parser("list", help="Show active goals")

    # gaps
    sub.add_parser("gaps", help="Run gap analysis")

    # brief
    sub.add_parser("brief", help="Format for morning brief")

    # detect
    sub.add_parser("detect", help="Auto-detect git activity")

    args = parser.parse_args()

    if args.command == "init":
        ensure_schema()
        print("Schema ready.")
    elif args.command == "add":
        gid = add_goal(args.title, args.desc, args.project, args.priority,
                       args.deadline, args.check_in)
        if gid:
            print(f"Created goal [{gid}]: {args.title}")
    elif args.command == "complete":
        complete_goal(args.goal_id, args.note)
        print(f"Goal {args.goal_id} completed.")
    elif args.command == "pause":
        pause_goal(args.goal_id, args.reason)
        print(f"Goal {args.goal_id} paused.")
    elif args.command == "drop":
        drop_goal(args.goal_id, args.reason)
        print(f"Goal {args.goal_id} dropped.")
    elif args.command == "progress":
        log_progress(args.goal_id, args.note)
        print(f"Progress logged for {args.goal_id}.")
    elif args.command == "list":
        for g in get_active_goals():
            dl = f" (due {g['deadline']})" if g['deadline'] else ""
            proj = f" [{g['project']}]" if g['project'] else ""
            print(f"  [{g['id']}] {g['priority'].upper():6s} {g['title']}{proj}{dl}")
    elif args.command == "gaps":
        print(run_gap_analysis())
    elif args.command == "brief":
        brief = format_goals_brief()
        print(brief or "No active goals.")
    elif args.command == "detect":
        detect_activity_from_git()
        print("Activity detection complete.")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
