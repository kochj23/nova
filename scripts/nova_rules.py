#!/usr/bin/env python3
"""
nova_rules.py — Correction-to-rule learning engine for Nova.

When Jordan corrects Nova, those corrections are captured and promoted into
persistent behavioral rules. Rules are checked before Nova responds, ensuring
she doesn't repeat the same mistakes.

Storage: PostgreSQL (nova_ops) table `rules`
Corrections source: corrections.json (existing) + conversational captures

Rule lifecycle:
  correction → candidate rule → active rule → (optionally retired)

Rules can be:
  - topic-scoped (e.g., "people", "homekit", "email")
  - global (apply to all responses)
  - time-bound (optional expiry)

Written by Jordan Koch.
"""

import json
import subprocess
import sys
import uuid
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN

DB = "nova_ops"
SOURCE = "nova_rules"
STATE_DIR = Path.home() / ".openclaw" / "workspace" / "state"
CORRECTIONS_FILE = STATE_DIR / "corrections.json"


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


# ── Schema ───────────────────────────────────────────────────────────────────


def ensure_schema():
    _exec("""
        CREATE TABLE IF NOT EXISTS rules (
            id              TEXT PRIMARY KEY,
            rule            TEXT NOT NULL,
            topic           TEXT DEFAULT 'global',
            source_type     TEXT NOT NULL DEFAULT 'correction',
            context         TEXT DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'active',
            confidence      FLOAT DEFAULT 1.0,
            times_applied   INTEGER DEFAULT 0,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at      TIMESTAMPTZ DEFAULT NULL,
            original_correction JSONB DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS rule_applications (
            id          TEXT PRIMARY KEY,
            rule_id     TEXT NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
            timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            context     TEXT DEFAULT '',
            prevented   TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_rules_status ON rules(status);
        CREATE INDEX IF NOT EXISTS idx_rules_topic ON rules(topic);
        CREATE INDEX IF NOT EXISTS idx_rule_apps_rule ON rule_applications(rule_id);
    """)


# ── Rule CRUD ────────────────────────────────────────────────────────────────


def add_rule(rule_text, topic="global", source_type="correction",
             context="", confidence=1.0, expires_at=None,
             original_correction=None):
    rule_id = str(uuid.uuid4())[:8]
    expires_sql = f"'{expires_at}'" if expires_at else "NULL"
    correction_json = json.dumps(original_correction) if original_correction else "NULL"
    if correction_json != "NULL":
        correction_json = f"'{_escape(correction_json)}'::jsonb"

    sql = f"""
        INSERT INTO rules (id, rule, topic, source_type, context, confidence, expires_at, original_correction)
        VALUES ('{rule_id}', '{_escape(rule_text)}', '{_escape(topic)}',
                '{source_type}', '{_escape(context)}', {confidence},
                {expires_sql}, {correction_json});
    """
    if _exec(sql):
        log(f"Rule added: [{rule_id}] ({topic}) {rule_text[:80]}", level=LOG_INFO, source=SOURCE)
        return rule_id
    return None


def retire_rule(rule_id, reason=""):
    sql = f"UPDATE rules SET status = 'retired', updated_at = NOW() WHERE id = '{rule_id}';"
    if _exec(sql):
        log(f"Rule retired: {rule_id} — {reason}", level=LOG_INFO, source=SOURCE)
        return True
    return False


def record_application(rule_id, context="", prevented=""):
    app_id = str(uuid.uuid4())[:8]
    _exec(f"""
        INSERT INTO rule_applications (id, rule_id, context, prevented)
        VALUES ('{app_id}', '{rule_id}', '{_escape(context)}', '{_escape(prevented)}');
        UPDATE rules SET times_applied = times_applied + 1, updated_at = NOW()
        WHERE id = '{rule_id}';
    """)


# ── Query operations ─────────────────────────────────────────────────────────


def get_active_rules(topic=None):
    where = "WHERE status = 'active' AND (expires_at IS NULL OR expires_at > NOW())"
    if topic:
        where += f" AND (topic = '{_escape(topic)}' OR topic = 'global')"
    rows = _query(f"""
        SELECT id, rule, topic, confidence, times_applied, created_at
        FROM rules
        {where}
        ORDER BY confidence DESC, created_at;
    """)
    rules = []
    for row in rows:
        parts = row.split("|")
        if len(parts) >= 6:
            rules.append({
                "id": parts[0],
                "rule": parts[1],
                "topic": parts[2],
                "confidence": float(parts[3]) if parts[3] else 1.0,
                "times_applied": int(parts[4]) if parts[4] else 0,
                "created_at": parts[5],
            })
    return rules


def get_all_rules():
    rows = _query("""
        SELECT id, rule, topic, status, confidence, times_applied, source_type
        FROM rules
        ORDER BY status, topic, created_at;
    """)
    rules = []
    for row in rows:
        parts = row.split("|")
        if len(parts) >= 7:
            rules.append({
                "id": parts[0],
                "rule": parts[1],
                "topic": parts[2],
                "status": parts[3],
                "confidence": float(parts[4]) if parts[4] else 1.0,
                "times_applied": int(parts[5]) if parts[5] else 0,
                "source_type": parts[6],
            })
    return rules


def format_rules_for_prompt(topic=None):
    rules = get_active_rules(topic)
    if not rules:
        return ""
    lines = ["## Active Rules (behavioral corrections — MUST follow)"]
    for r in rules:
        topic_tag = f"[{r['topic']}] " if r['topic'] != 'global' else ""
        lines.append(f"- {topic_tag}{r['rule']}")
    return "\n".join(lines)


# ── Promotion: corrections.json → rules ─────────────────────────────────────


def promote_corrections():
    if not CORRECTIONS_FILE.exists():
        return 0

    with open(CORRECTIONS_FILE) as f:
        corrections = json.load(f)

    existing = {r["rule"] for r in get_all_rules()}
    promoted = 0

    for c in corrections:
        rule_text = _correction_to_rule(c)
        if rule_text and rule_text not in existing:
            add_rule(
                rule_text=rule_text,
                topic=c.get("topic", "global"),
                source_type="correction",
                context=c.get("nova_response", ""),
                original_correction=c,
            )
            promoted += 1
            existing.add(rule_text)

    if promoted:
        log(f"Promoted {promoted} corrections to rules", level=LOG_INFO, source=SOURCE)
    return promoted


def _correction_to_rule(correction):
    nova_said = correction.get("nova_response", "")
    jordan_said = correction.get("jordan_correction", "")
    topic = correction.get("topic", "")

    if not jordan_said:
        return None

    if nova_said:
        return f"Do NOT say: '{nova_said}'. Correct answer: {jordan_said}"
    return jordan_said


# ── Ingest from conversation (called by gateway/agent) ───────────────────────


def ingest_correction(nova_response, jordan_correction, topic="global", context=None):
    correction = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now().isoformat(),
        "nova_response": nova_response,
        "jordan_correction": jordan_correction,
        "topic": topic,
        "context": context or {},
    }

    # Append to corrections.json for history
    corrections = []
    if CORRECTIONS_FILE.exists():
        with open(CORRECTIONS_FILE) as f:
            corrections = json.load(f)
    corrections.append(correction)
    with open(CORRECTIONS_FILE, "w") as f:
        json.dump(corrections, f, indent=2)

    # Immediately promote to active rule
    rule_text = _correction_to_rule(correction)
    if rule_text:
        return add_rule(
            rule_text=rule_text,
            topic=topic,
            source_type="correction",
            context=nova_response,
            original_correction=correction,
        )
    return None


def add_preference(preference, topic="global"):
    return add_rule(
        rule_text=preference,
        topic=topic,
        source_type="preference",
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _escape(s):
    if not s:
        return ""
    return s.replace("'", "''").replace("\\", "\\\\")


# ── CLI interface ────────────────────────────────────────────────────────────


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Nova Rules Engine")
    sub = parser.add_subparsers(dest="command")

    # init
    sub.add_parser("init", help="Create tables")

    # add
    p_add = sub.add_parser("add", help="Add a rule")
    p_add.add_argument("rule")
    p_add.add_argument("--topic", default="global")
    p_add.add_argument("--type", default="preference", dest="source_type")

    # retire
    p_ret = sub.add_parser("retire", help="Retire a rule")
    p_ret.add_argument("rule_id")
    p_ret.add_argument("--reason", default="")

    # list
    p_list = sub.add_parser("list", help="Show rules")
    p_list.add_argument("--topic", default=None)
    p_list.add_argument("--all", action="store_true")

    # prompt
    p_prompt = sub.add_parser("prompt", help="Format rules for LLM prompt injection")
    p_prompt.add_argument("--topic", default=None)

    # promote
    sub.add_parser("promote", help="Promote corrections.json entries to rules")

    # correct
    p_cor = sub.add_parser("correct", help="Record a correction and promote to rule")
    p_cor.add_argument("--nova", required=True, help="What Nova said (wrong)")
    p_cor.add_argument("--jordan", required=True, help="What Jordan corrected to")
    p_cor.add_argument("--topic", default="global")

    args = parser.parse_args()

    if args.command == "init":
        ensure_schema()
        print("Schema ready.")
    elif args.command == "add":
        rid = add_rule(args.rule, topic=args.topic, source_type=args.source_type)
        if rid:
            print(f"Rule [{rid}]: {args.rule}")
    elif args.command == "retire":
        retire_rule(args.rule_id, args.reason)
        print(f"Rule {args.rule_id} retired.")
    elif args.command == "list":
        rules = get_all_rules() if args.all else get_active_rules(args.topic)
        for r in rules:
            status = f" ({r['status']})" if "status" in r else ""
            applied = f" [applied {r['times_applied']}x]" if r['times_applied'] else ""
            print(f"  [{r['id']}] ({r['topic']}) {r['rule']}{status}{applied}")
    elif args.command == "prompt":
        print(format_rules_for_prompt(args.topic))
    elif args.command == "promote":
        n = promote_corrections()
        print(f"Promoted {n} corrections.")
    elif args.command == "correct":
        rid = ingest_correction(args.nova, args.jordan, args.topic)
        if rid:
            print(f"Correction recorded and promoted to rule [{rid}]")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
